from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.errors import (
    ConcurrentSourceMutation,
    UnsupportedLegacyProject,
)
from supernote_module_generator.filesystem import (
    contained_directory_entries_no_follow,
    iter_tree_no_follow,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.project_model import (
    ExistingGeneration,
    ProjectModel,
    detect_existing_generation,
)
from project_inventory import inventory_project


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n', encoding="utf-8"
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (tmp_path / "android/app/build.gradle").write_text(
        "plugins {}\n", encoding="utf-8"
    )
    return tmp_path


def invoke(root: Path, arguments: list[str]) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--json", *arguments],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    assert stderr.getvalue() == ""
    return code, json.loads(stdout.getvalue())


def exact_metadata(root: Path) -> dict[str, tuple[int, int, int]]:
    # Path.rglob()/os.walk() can advance directory atime on Linux. Traverse
    # through the production no-follow observer so this assertion does not
    # create the metadata drift it is intended to detect.
    paths = tuple(iter_tree_no_follow(root))
    return {
        ".": _metadata(root),
        **{
            path.relative_to(root).as_posix(): _metadata(path)
            for path in paths
            if not path.is_symlink()
        },
    }


def _metadata(path: Path) -> tuple[int, int, int]:
    value = path.lstat()
    return value.st_mode, value.st_atime_ns, value.st_mtime_ns


def assert_metadata_equivalent(
    root: Path,
    actual: dict[str, tuple[int, int, int]],
    expected: dict[str, tuple[int, int, int]],
) -> None:
    assert actual.keys() == expected.keys()
    for relative, (mode, atime_ns, mtime_ns) in actual.items():
        expected_mode, expected_atime_ns, expected_mtime_ns = expected[relative]
        assert mode == expected_mode
        path = root if relative == "." else root.joinpath(*relative.split("/"))
        assert _timestamp_matches_filesystem(
            path,
            root.parent,
            mode,
            atime_ns,
            expected_atime_ns,
            attribute="atime",
        )
        assert _timestamp_matches_filesystem(
            path,
            root.parent,
            mode,
            mtime_ns,
            expected_mtime_ns,
            attribute="mtime",
        )


def _timestamp_matches_filesystem(
    path: Path,
    probe_parent: Path,
    mode: int,
    actual: int,
    expected: int,
    *,
    attribute: str,
) -> bool:
    if actual == expected or (os.name == "nt" and actual // 100 == expected // 100):
        return True
    probed = _probe_timestamp_representation(
        path,
        probe_parent,
        mode,
        expected,
        attribute=attribute,
    )
    if probed is None:
        return False
    represented, stable = probed
    if represented == expected:
        return False
    if not stable:
        return attribute == "atime" and stat.S_ISDIR(mode)
    return actual == represented


def _probe_timestamp_representation(
    path: Path,
    probe_parent: Path,
    mode: int,
    requested: int,
    *,
    attribute: str,
) -> tuple[int, bool] | None:
    try:
        with tempfile.TemporaryDirectory(
            prefix="sn-module-gen-timestamp-probe-",
            dir=probe_parent,
        ) as temporary:
            probe_root = Path(temporary)
            if stat.S_ISDIR(mode):
                probe = probe_root
            elif stat.S_ISREG(mode):
                probe = probe_root / "probe"
                probe.write_bytes(b"")
            elif stat.S_ISLNK(mode):
                probe = probe_root / "probe"
                probe.symlink_to("target")
            else:
                return None
            probe_before = probe.lstat()
            if probe_before.st_dev != path.lstat().st_dev:
                return None
            first_request = (
                (requested, probe_before.st_mtime_ns)
                if attribute == "atime"
                else (probe_before.st_atime_ns, requested)
            )
            os.utime(
                probe,
                ns=first_request,
                follow_symlinks=False,
            )
            represented = getattr(probe.lstat(), f"st_{attribute}_ns")
            if represented == requested:
                return represented, True
            probe_current = probe.lstat()
            stable_request = (
                (represented, probe_current.st_mtime_ns)
                if attribute == "atime"
                else (probe_current.st_atime_ns, represented)
            )
            os.utime(probe, ns=stable_request, follow_symlinks=False)
            stable = getattr(probe.lstat(), f"st_{attribute}_ns") == represented
            if stable and attribute == "atime" and stat.S_ISDIR(mode):
                os.listdir(probe)
                stable = probe.lstat().st_atime_ns == represented
            return represented, stable
    except (NotImplementedError, OSError):
        return None


def test_metadata_equivalence_keeps_directory_atime_strict_on_capable_filesystem(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    metadata = _metadata(root)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_probe_timestamp_representation",
        lambda _path, _parent, _mode, requested, **_kwargs: (requested, True),
    )

    with pytest.raises(AssertionError):
        assert_metadata_equivalent(
            root,
            {".": (metadata[0], metadata[1] + 10_000, metadata[2])},
            {".": metadata},
        )


def test_metadata_equivalence_omits_only_unstable_directory_atime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    source = root / "source.cpp"
    source.write_text("int value = 1;\n", encoding="utf-8")
    root_metadata = _metadata(root)
    source_metadata = _metadata(source)
    expected = {".": root_metadata, "source.cpp": source_metadata}
    monkeypatch.setattr(
        sys.modules[__name__],
        "_probe_timestamp_representation",
        lambda _path, _parent, _mode, requested, **_kwargs: (
            requested - 1_000,
            False,
        ),
    )

    assert_metadata_equivalent(
        root,
        {
            ".": (root_metadata[0], root_metadata[1] + 10_000, root_metadata[2]),
            "source.cpp": source_metadata,
        },
        expected,
    )
    with pytest.raises(AssertionError):
        assert_metadata_equivalent(
            root,
            {
                ".": (
                    root_metadata[0],
                    root_metadata[1],
                    root_metadata[2] + 10_000,
                ),
                "source.cpp": source_metadata,
            },
            expected,
        )
    with pytest.raises(AssertionError):
        assert_metadata_equivalent(
            root,
            {
                ".": root_metadata,
                "source.cpp": (
                    source_metadata[0],
                    source_metadata[1] + 10_000,
                    source_metadata[2],
                ),
            },
            expected,
        )


def test_timestamp_mismatch_requires_stable_probed_representation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.cpp"
    source.write_text("int value = 1;\n", encoding="utf-8")
    mode = source.lstat().st_mode
    monkeypatch.setattr(
        sys.modules[__name__],
        "_probe_timestamp_representation",
        lambda *_args, **_kwargs: (4_000, True),
    )

    assert _timestamp_matches_filesystem(
        source,
        tmp_path.parent,
        mode,
        4_000,
        4_999,
        attribute="mtime",
    )
    assert not _timestamp_matches_filesystem(
        source,
        tmp_path.parent,
        mode,
        4_001,
        4_999,
        attribute="mtime",
    )


def test_timestamp_mismatch_rejects_probe_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.cpp"
    source.write_text("int value = 1;\n", encoding="utf-8")
    monkeypatch.setattr(
        sys.modules[__name__],
        "_probe_timestamp_representation",
        lambda *_args, **_kwargs: None,
    )

    assert not _timestamp_matches_filesystem(
        source,
        tmp_path.parent,
        source.lstat().st_mode,
        4_000,
        4_999,
        attribute="atime",
    )


PUBLIC_COMMANDS = (
    ("add", "new"),
    ("update", "alpha", "--yes"),
    ("check",),
    ("repair", "--yes"),
    ("validate", "--all"),
    ("remove", "alpha", "--yes"),
    ("doctor",),
)


def install_historical_layout(root: Path, family: str) -> None:
    if family in {"native_metadata", "rn_metadata"}:
        feature = root / "local_modules/alpha"
        feature.mkdir(parents=True, exist_ok=True)
        name = (
            ".supernote-native-module.json"
            if family == "native_metadata"
            else ".rn-legacy-module.json"
        )
        (feature / name).write_text('{"npm_name":"alpha"}\n')
        return
    if family in {"local-modules", "modules"}:
        feature = root / family / "alpha"
        feature.mkdir(parents=True)
        (feature / ".supernote-native-module.json").write_text(
            '{"npm_name":"alpha"}\n'
        )
        return
    if family in {"local_native_wiring", "rn_legacy_wiring", "local_kotlin_wiring"}:
        marker = {
            "local_native_wiring": "local-native-module",
            "rn_legacy_wiring": "rn-legacy-module",
            "local_kotlin_wiring": "local-kotlin-module",
        }[family]
        settings = root / "android/settings.gradle"
        settings.write_text(
            settings.read_text()
            + f"// {marker}: alpha\nlegacy\n// end {marker}: alpha\n"
        )
        return
    feature = root / "local_modules/alpha/android"
    relative = {
        "native_module_codegen": ".native-module/processor/build.gradle.kts",
        "codegen_config": ".supernote-module/codegen-config.json",
        "copied_codegen": ".supernote-module/supernote_codegen/__init__.py",
    }[family]
    target = feature / relative
    target.parent.mkdir(parents=True)
    target.write_text("legacy codegen\n")

@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor/symlink contract")
@pytest.mark.parametrize("substitution", ("final", "ancestor"))
def test_direct_directory_finalization_never_stats_through_substitution(
    tmp_path: Path,
    monkeypatch,
    substitution: str,
):
    root = plugin(tmp_path)
    parent = root / "modules"
    observed = parent
    parent.mkdir()
    if substitution == "ancestor":
        observed = parent / "legacy"
        observed.mkdir()
    (observed / "entry.txt").write_text("observed\n")

    outside = tmp_path / "outside"
    outside_observed = outside if substitution == "final" else outside / "legacy"
    outside_observed.mkdir(parents=True)
    outside_sentinel = outside_observed / "sentinel.txt"
    outside_sentinel.write_text("external sentinel\n")
    outside_bytes = outside_sentinel.read_bytes()
    for index, entry in enumerate((outside, outside_observed, outside_sentinel), 1):
        os.utime(
            entry,
            ns=(index * 1_000_000_000, (index + 10) * 1_000_000_000),
        )
    outside_metadata = {
        entry: _metadata(entry)
        for entry in (outside, outside_observed, outside_sentinel)
    }

    saved = root / "modules-observed"
    original_listdir = os.listdir
    original_lstat = os.lstat
    substituted = False

    def substitute_after_listing(descriptor):
        nonlocal substituted
        rows = original_listdir(descriptor)
        if not substituted:
            substituted = True
            parent.rename(saved)
            parent.symlink_to(outside, target_is_directory=True)

            def reject_pathname_lstat(path, *args, **kwargs):
                if os.fspath(path) == os.fspath(observed):
                    raise AssertionError(
                        "contained finalization performed pathname lstat"
                    )
                return original_lstat(path, *args, **kwargs)

            monkeypatch.setattr(os, "lstat", reject_pathname_lstat)
        return rows

    monkeypatch.setattr(os, "listdir", substitute_after_listing)
    try:
        with pytest.raises(ConcurrentSourceMutation):
            contained_directory_entries_no_follow(root, observed)
        assert {
            entry: _metadata(entry)
            for entry in (outside, outside_observed, outside_sentinel)
        } == outside_metadata
        assert outside_sentinel.read_bytes() == outside_bytes
    finally:
        monkeypatch.setattr(os, "lstat", original_lstat)
        if parent.is_symlink():
            parent.unlink()
        if saved.exists():
            saved.rename(parent)


def test_unmanifested_feature_metadata_is_rejected_as_legacy_state(tmp_path: Path):
    root = plugin(tmp_path)
    feature = root / "local_modules/alpha"
    feature.mkdir(parents=True)
    metadata = feature / ".supernote-module.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "kind": "supernote_v3_feature",
                "npm_name": "alpha",
            }
        )
        + "\n"
    )
    sentinel = feature / "sentinel.cpp"
    sentinel.write_text("int user_source = 1;\n")
    before = inventory_project(root)

    with pytest.raises(UnsupportedLegacyProject):
        ProjectModel.discover(root)
    with pytest.raises(UnsupportedLegacyProject):
        GenerationService(root).plan(
            operation="update", requested_targets=("alpha",)
        )

    assert detect_existing_generation(root) is ExistingGeneration.V3
    assert inventory_project(root) == before
    assert sentinel.read_text() == "int user_source = 1;\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink identity fixture")
@pytest.mark.parametrize("state_name", ["manifest", "journal"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory"])
def test_build_hook_trust_rejects_unsafe_state_without_following_it(
    tmp_path: Path,
    monkeypatch,
    state_name: str,
    unsafe_kind: str,
):
    root = plugin(tmp_path / "plugin")
    manifest = root / ".supernote-module/manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"schema_version":"1.0","generation_id":"generation"}\n'
    )
    journal = root / ".supernote-module-transaction.json"
    target = manifest if state_name == "manifest" else journal
    if state_name == "manifest":
        manifest.unlink()
    outside = tmp_path / f"outside-{state_name}.json"
    outside.write_text(
        (
            '{"schema_version":"1.0","generation_id":"generation"}\n'
            if state_name == "manifest"
            else '{"schema":1,"id":"transaction","phase":"apply"}\n'
        )
    )
    outside_bytes = outside.read_bytes()
    outside_before = _metadata(outside)
    if unsafe_kind == "symlink":
        target.symlink_to(outside)
    else:
        target.mkdir()
    monkeypatch.setenv("SUPERNOTE_MODULE_PARENT_GENERATION_ID", "generation")
    monkeypatch.setenv("SUPERNOTE_MODULE_PARENT_TRANSACTION_ID", "transaction")

    code, result = invoke(root, ["check", "--build-hook"])

    assert code != 0
    assert result["status"] in {"failure", "partial"}
    assert _metadata(outside) == outside_before
    assert outside.read_bytes() == outside_bytes
    assert target.is_symlink() if unsafe_kind == "symlink" else target.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink identity fixture")
def test_build_hook_boundary_rejects_manifest_symlink_ancestor_without_reading(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path / "plugin")
    outside = tmp_path / "outside-state"
    outside.mkdir()
    manifest = outside / "manifest.json"
    manifest.write_text(
        '{"schema_version":"1.0","generation_id":"generation"}\n'
    )
    manifest_bytes = manifest.read_bytes()
    before = _metadata(manifest)
    (root / ".supernote-module").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SUPERNOTE_MODULE_PARENT_GENERATION_ID", "generation")

    code, result = invoke(root, ["check", "--build-hook"])

    assert code != 0
    assert result["status"] == "failure"
    assert _metadata(manifest) == before
    assert manifest.read_bytes() == manifest_bytes
    assert (root / ".supernote-module").is_symlink()
