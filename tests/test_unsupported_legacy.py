from __future__ import annotations

import io
import json
import os
from pathlib import Path

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
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.project_model import (
    ExistingGeneration,
    ProjectModel,
    detect_existing_generation,
)
from supernote_module_generator.transaction import Transaction
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


PUBLIC_COMMANDS = (
    ("add", "new"),
    ("update", "alpha", "--yes"),
    ("check",),
    ("repair", "--yes"),
    ("validate", "--all"),
    ("remove", "alpha", "--yes"),
    ("doctor",),
)


@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
@pytest.mark.parametrize("sentinel_kind", ("file", "directory", "symlink"))
def test_public_commands_reject_legacy_v4_runtime_without_mutation(
    tmp_path: Path,
    arguments: tuple[str, ...],
    sentinel_kind: str,
):
    if sentinel_kind == "symlink" and os.name == "nt":
        pytest.skip("POSIX symlink identity fixture")
    root = plugin(tmp_path / "plugin")
    runtime = root / "android/.supernote-module/v4-runtime"
    runtime.mkdir(parents=True)
    sentinel = runtime / "user-sentinel"
    outside = tmp_path / "outside-sentinel"
    outside.write_text("external user bytes\n")
    if sentinel_kind == "file":
        sentinel.write_text("unmanifested user bytes\n")
    elif sentinel_kind == "directory":
        sentinel.mkdir()
        (sentinel / "nested.txt").write_text("unmanifested nested bytes\n")
    else:
        sentinel.symlink_to(outside)
    journal = root / ".supernote-module-transaction.json"
    journal.write_text('{"schema":1,"phase":"apply","bad":true}\n')
    before = inventory_project(root)
    before_metadata = exact_metadata(root)
    outside_before = outside.read_bytes()

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert result["error"]["phase"] == "preflight"
    assert "does not migrate or reinterpret V1-V4" in result["error"]["message"]
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before
    assert journal.read_text() == '{"schema":1,"phase":"apply","bad":true}\n'
    assert outside.read_bytes() == outside_before


@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
def test_public_commands_reject_legacy_v4_wiring_without_mutation(
    tmp_path: Path,
    arguments: tuple[str, ...],
):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text()
        + "// supernote-module-v4-runtime\n"
        + "include ':unmanifested-v4-runtime'\n"
        + "// end supernote-module-v4-runtime\n"
    )
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before


def test_clean_public_add_uses_transaction_scoped_bootstrap(tmp_path: Path):
    root = plugin(tmp_path)
    wrapper = root / ("android/gradlew.bat" if os.name == "nt" else "android/gradlew")
    wrapper.write_text("@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n")
    if os.name != "nt":
        wrapper.chmod(0o755)

    code, result = invoke(
        root,
        ["add", "fresh", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 0, result
    assert (root / ".supernote-module/manifest.json").is_file()
    assert detect_existing_generation(root) is ExistingGeneration.CURRENT
    assert invoke(root, ["check"])[0] == 0


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


def install_canonical_v4(root: Path) -> None:
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/alpha",
            "alpha",
            "4.0.0-dev.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    service = GenerationService(root)
    plan = service.plan(
        operation="add",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "add", ("alpha",)))


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
@pytest.mark.parametrize(
    "arguments",
    [
        ["add", "new"],
        ["update", "alpha", "--yes"],
        ["check"],
        ["repair", "--yes"],
        ["validate", "--all"],
        ["remove", "alpha", "--yes"],
        ["doctor"],
    ],
)
def test_public_commands_reject_legacy_runtime_before_any_mutation(
    tmp_path: Path,
    version: str,
    arguments: list[str],
):
    root = plugin(tmp_path)
    runtime = root / f"android/.supernote-module/{version}-runtime"
    runtime.mkdir(parents=True)
    (runtime / "sentinel.txt").write_text("legacy bytes\n", encoding="utf-8")
    # An invalid pending journal proves the public boundary runs before startup
    # recovery and leaves legacy projects untouched.
    journal = root / ".supernote-module-transaction.json"
    journal.write_text('{"schema":1,"phase":"apply","bad":true}\n')
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, arguments)

    assert code == 1
    assert result["status"] == "failure"
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert result["error"]["phase"] == "preflight"
    assert "does not migrate" in result["error"]["message"]
    assert "Create a clean plugin" in result["next_action"]
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before
    assert journal.read_text() == '{"schema":1,"phase":"apply","bad":true}\n'


@pytest.mark.parametrize(
    "family",
    (
        "native_metadata",
        "rn_metadata",
        "local-modules",
        "modules",
        "local_native_wiring",
        "rn_legacy_wiring",
        "local_kotlin_wiring",
        "native_module_codegen",
        "codegen_config",
        "copied_codegen",
    ),
)
@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
def test_every_public_command_rejects_known_historical_layouts_exactly(
    tmp_path: Path,
    family: str,
    arguments: tuple[str, ...],
):
    root = plugin(tmp_path)
    install_historical_layout(root, family)
    journal = root / ".supernote-module-transaction.json"
    journal.write_text('{"schema":1,"phase":"apply","bad":true}\n')
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert result["error"]["phase"] == "preflight"
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before
    assert journal.read_text() == '{"schema":1,"phase":"apply","bad":true}\n'


@pytest.mark.parametrize(
    "family",
    (
        "native_metadata",
        "rn_metadata",
        "local-modules",
        "modules",
        "local_native_wiring",
        "rn_legacy_wiring",
        "local_kotlin_wiring",
        "native_module_codegen",
        "codegen_config",
        "copied_codegen",
    ),
)
@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
def test_manifest_never_masks_known_historical_layouts(
    tmp_path: Path,
    family: str,
    arguments: tuple[str, ...],
):
    root = plugin(tmp_path)
    install_canonical_v4(root)
    install_historical_layout(root, family)
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert result["error"]["phase"] == "preflight"
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before


@pytest.mark.parametrize("version", ("v1", "v2", "v3"))
@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
def test_manifest_never_masks_legacy_runtime_roots(
    tmp_path: Path,
    version: str,
    arguments: tuple[str, ...],
):
    root = plugin(tmp_path)
    install_canonical_v4(root)
    runtime = root / f"android/.supernote-module/{version}-runtime"
    runtime.mkdir(parents=True)
    (runtime / "sentinel.txt").write_text("legacy bytes\n")
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before


@pytest.mark.parametrize("legacy_version", ("v1", "v2", "v3"))
@pytest.mark.parametrize("arguments", PUBLIC_COMMANDS)
def test_manifest_claim_never_masks_live_legacy_feature_metadata(
    tmp_path: Path,
    legacy_version: str,
    arguments: tuple[str, ...],
):
    root = plugin(tmp_path)
    install_canonical_v4(root)
    metadata = root / "local_modules/alpha/.supernote-module.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": int(legacy_version[1:]),
                "kind": f"supernote_{legacy_version}_feature",
                "npm_name": "alpha",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(root, list(arguments))

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert result["error"]["phase"] == "preflight"
    assert exact_metadata(root) == before_metadata
    assert inventory_project(root) == before


@pytest.mark.parametrize("directory_name", ["local-modules", "modules"])
def test_unrelated_bare_legacy_named_directory_is_valid_user_state(
    tmp_path: Path,
    directory_name: str,
):
    root = plugin(tmp_path)
    unrelated = root / directory_name / "application-data"
    unrelated.mkdir(parents=True)
    sentinel = unrelated / "sentinel.txt"
    sentinel.write_text("user-owned data\n")
    wrapper = root / ("android/gradlew.bat" if os.name == "nt" else "android/gradlew")
    wrapper.write_text("@exit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n")
    if os.name != "nt":
        wrapper.chmod(0o755)
    before = sentinel.read_bytes()
    observed_directory = root / directory_name
    os.utime(
        observed_directory,
        ns=(1_000_000_000, 2_000_000_000),
    )
    before_observed_metadata = _metadata(observed_directory)

    assert detect_existing_generation(root) is ExistingGeneration.NONE
    assert _metadata(observed_directory) == before_observed_metadata

    check_code, check_result = invoke(root, ["check"])
    assert check_code == 1, check_result
    assert check_result["error"]["kind"] != "unsupported_legacy_project"
    assert _metadata(observed_directory) == before_observed_metadata

    code, result = invoke(
        root,
        ["add", "fresh", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 0, result
    assert sentinel.read_bytes() == before
    assert _metadata(observed_directory) == before_observed_metadata


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


def test_legacy_wiring_without_runtime_is_rejected_without_rewrite(tmp_path: Path):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text()
        + "// supernote-module-v2-runtime\nlegacy\n"
        + "// end supernote-module-v2-runtime\n"
    )
    before = settings.read_bytes()

    code, result = invoke(root, ["check"])

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert settings.read_bytes() == before


def test_add_rejects_real_v2_wiring_without_any_mutation(tmp_path: Path):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text()
        + "// supernote-module-v2-runtime\nlegacy\n"
        + "// end supernote-module-v2-runtime\n"
    )
    before = inventory_project(root)
    before_metadata = exact_metadata(root)

    code, result = invoke(
        root,
        ["add", "fresh", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert inventory_project(root) == before
    assert exact_metadata(root) == before_metadata
    assert not (root / ".supernote-module/manifest.json").exists()


@pytest.mark.parametrize("legacy_schema", (1, 2, 3, 4))
def test_manifest_schema_is_required_and_legacy_schema_is_not_reinterpreted(
    tmp_path: Path,
    legacy_schema: int,
):
    root = plugin(tmp_path)
    manifest = root / ".supernote-module/manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"schema_version": legacy_schema}) + "\n")
    before = inventory_project(root)

    code, result = invoke(root, ["repair", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert inventory_project(root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink identity fixture")
def test_legacy_runtime_symlink_is_rejected_without_following_target(tmp_path: Path):
    root = plugin(tmp_path / "plugin")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n")
    managed = root / "android/.supernote-module"
    managed.mkdir(parents=True)
    (managed / "v3-runtime").symlink_to(outside, target_is_directory=True)
    before = sentinel.read_bytes()

    code, result = invoke(root, ["check"])

    assert code == 1
    assert result["error"]["kind"] == "unsupported_legacy_project"
    assert sentinel.read_bytes() == before
    assert (managed / "v3-runtime").is_symlink()


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
