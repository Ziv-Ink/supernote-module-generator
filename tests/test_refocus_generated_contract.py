from __future__ import annotations

import json
import io
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
from types import SimpleNamespace

import pytest

import supernote_module_generator.filesystem as filesystem
import supernote_module_generator.filesystem_inventory as filesystem_inventory
import supernote_module_generator.feature_generator as feature_generator
import supernote_module_generator.jvm_frontend_service as jvm_frontend
from supernote_module_generator.cli import main
from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import (
    FeatureOperationError,
    FeatureOperationService,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.jvm_frontend_service import JvmFrontendService
from supernote_module_generator.project_model import ProjectModel
from supernote_module_generator.semantic import SemanticApi
from supernote_module_generator.integrity_manifest import (
    INCOMPLETE_MARKER_PATH,
    INTEGRITY_MANIFEST_PATH,
)
from supernote_module_generator.validation import GeneratedProjectValidator


def _plugin(root: Path) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (root / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (root / "android/app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (root / "package.json").write_text(
        '{"name":"refocus-fixture","dependencies":{}}\n', encoding="utf-8"
    )
    return root


def _invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        arguments,
        cwd=root,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _add_native(root: Path) -> Path:
    output = root / "local_modules/alpha"
    return FeatureOperationService(root).add(
        FeatureConfig(
            output=output,
            npm_name="alpha",
            package_version="1.0.0",
            android_namespace="com.example.alpha",
            public_name="Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )


def _update(root: Path) -> None:
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan)


def test_authored_manifest_is_separate_from_rerendered_output(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    author_manifest = feature / ".supernote-module.json"
    package = feature / "package.json"
    cmake = feature / "CMakeLists.txt"
    baselines = {
        path: (path.read_bytes(), stat.st_mode, stat.st_mtime_ns)
        for path in (author_manifest, package, cmake)
        for stat in (path.stat(),)
    }

    _update(root)

    for path, baseline in baselines.items():
        stat = path.stat()
        assert (path.read_bytes(), stat.st_mode, stat.st_mtime_ns) == baseline
    assert (feature / ".supernote-generated/index.js").is_file()
    ownership = json.loads(
        (feature / ".supernote-generated/ownership.json").read_text(encoding="utf-8")
    )
    authored = json.loads(author_manifest.read_text(encoding="utf-8"))
    assert ownership["feature_id"] == authored["feature_id"]
    manifest = json.loads((root / INTEGRITY_MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2.0"
    assert "template_capability" not in manifest
    assert GeneratedProjectValidator(root).validate().status == "success"
    assert not (root / INCOMPLETE_MARKER_PATH).exists()


def test_plain_update_rewrites_every_generated_leaf_and_overwrites_edits(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    _update(root)
    generated = feature / ".supernote-generated/index.js"
    expected = generated.read_bytes()
    first_inode = generated.stat().st_ino
    generated.write_text("manual generated edit\n", encoding="utf-8")

    _update(root)

    assert generated.read_bytes() == expected
    if os.name != "nt":
        assert generated.stat().st_ino != first_inode


def test_input_change_before_completion_leaves_marker_and_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    source = feature / "android/src/main/cpp/feature.cpp"
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    original = service.validate_completion_preconditions

    def mutate_then_validate(value):
        source.write_text(source.read_text(encoding="utf-8") + "// author edit\n")
        original(value)

    monkeypatch.setattr(service, "validate_completion_preconditions", mutate_then_validate)
    with pytest.raises(Exception, match="project state changed"):
        service.execute(plan)

    assert source.read_text(encoding="utf-8").endswith("// author edit\n")
    assert (root / INCOMPLETE_MARKER_PATH).is_file()
    validation = GeneratedProjectValidator(root).validate()
    assert validation.status == "failure"
    assert validation.issues[0].code == "SNMG_GENERATION_INCOMPLETE"

    _update(root)
    assert not (root / INCOMPLETE_MARKER_PATH).exists()
    assert source.read_text(encoding="utf-8").endswith("// author edit\n")


def test_zero_feature_update_removes_owned_runtime_leaves(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    _update(root)
    for path in sorted(feature.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    feature.rmdir()

    service = GenerationService(root)
    plan = service.plan(operation="update", requested_targets=())
    service.execute(plan)

    manifest = json.loads((root / INTEGRITY_MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest["features"] == []
    assert manifest["artifacts"] == []
    assert not any(
        path.is_file()
        for path in (root / "android/.supernote-module/runtime").rglob("*")
    )


def test_update_and_validate_never_repair_authored_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    authored_root = feature.resolve()
    original_utime = filesystem.os.utime
    original_chmod = filesystem.os.chmod

    def reject_authored_utime(path, *args, **kwargs):
        if not isinstance(path, int):
            candidate = Path(path).resolve(strict=False)
            if candidate == authored_root or authored_root in candidate.parents:
                raise AssertionError(f"utime called for authored input: {candidate}")
        return original_utime(path, *args, **kwargs)

    def reject_authored_chmod(path, *args, **kwargs):
        if not isinstance(path, int):
            candidate = Path(path).resolve(strict=False)
            if candidate == authored_root or authored_root in candidate.parents:
                raise AssertionError(f"chmod called for authored input: {candidate}")
        return original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(filesystem.os, "utime", reject_authored_utime)
    monkeypatch.setattr(filesystem.os, "chmod", reject_authored_chmod)
    _update(root)
    assert GeneratedProjectValidator(root).validate().status == "success"


def test_public_add_and_plain_update_do_not_edit_parent_dependency_metadata(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path)
    package = root / "package.json"
    before = package.read_bytes()

    code, _stdout, stderr = _invoke(root, ["add", "alpha", "--yes", "--plain"])
    assert code == 0
    assert "error:" not in stderr
    assert package.read_bytes() == before
    assert not (root / "package-lock.json").exists()

    generated = root / "local_modules/alpha/.supernote-generated/index.js"
    first_inode = generated.stat().st_ino
    code, _stdout, stderr = _invoke(root, ["update", "--plain"])
    assert code == 0
    assert "error:" not in stderr
    assert package.read_bytes() == before
    if os.name != "nt":
        assert generated.stat().st_ino != first_inode


def test_old_pending_journal_is_a_safe_stop_without_recovery(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    journal = root / ".supernote-module-transaction.json"
    journal.write_text('{"historical":true}\n', encoding="utf-8")
    before = journal.read_bytes()

    code, _stdout, stderr = _invoke(root, ["update", "--plain"])

    assert code == 3
    assert "legacy_pending_transaction" not in stderr
    assert "pending transaction" in stderr.lower()
    assert journal.read_bytes() == before
    assert not (root / INCOMPLETE_MARKER_PATH).exists()


def test_second_add_keeps_first_feature_in_prior_manifest(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    first = _add_native(root)
    _update(root)
    second = root / "local_modules/beta"
    FeatureOperationService(root).add(
        FeatureConfig(
            output=second,
            npm_name="beta",
            package_version="1.0.0",
            android_namespace="com.example.beta",
            public_name="Beta",
            starters=(StarterFamily.NATIVE,),
        )
    )

    service = GenerationService(root)
    plan = service.plan(
        operation="add",
        requested_targets=("beta",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan)

    manifest = json.loads((root / INTEGRITY_MANIFEST_PATH).read_text())
    assert [item["package_name"] for item in manifest["features"]] == [
        "alpha",
        "beta",
    ]
    assert first.is_dir()


def test_add_activation_interruption_needs_no_transaction_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path)
    destination = root / "local_modules/alpha"
    from supernote_module_generator import feature_operations

    original_activate = feature_operations.activate_contained_directory_no_replace

    def activate_then_interrupt(project, source, target, **options):
        original_activate(project, source, target, **options)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        feature_operations,
        "activate_contained_directory_no_replace",
        activate_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)
    monkeypatch.setattr(
        feature_operations,
        "activate_contained_directory_no_replace",
        original_activate,
    )

    assert destination.is_dir()
    assert not (root / ".supernote-module-transaction.json").exists()
    source = destination / "android/src/main/cpp/feature.cpp"
    edited = source.read_text() + "// edit after completed activation\n"
    source.write_text(edited)
    with pytest.raises(FeatureOperationError, match="feature already exists"):
        _add_native(root)
    assert source.read_text() == edited
    _update(root)
    assert (destination / ".supernote-generated/index.js").is_file()


@pytest.mark.parametrize(
    "boundary",
    [
        "before-first-authored-file",
        "during-nested-creation",
        "before-authored-metadata",
        "after-authored-metadata",
    ],
)
def test_interrupted_authored_staging_requires_explicit_scoped_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    root = _plugin(tmp_path / "plugin")
    original_write = feature_generator._write
    calls = 0

    def interrupt_at_boundary(stage: Path, relative: str, content: str) -> None:
        nonlocal calls
        calls += 1
        if boundary == "before-first-authored-file" and calls == 1:
            raise KeyboardInterrupt
        if boundary == "during-nested-creation" and calls == 1:
            (stage / "android/src/main").mkdir(parents=True)
            raise KeyboardInterrupt
        if boundary == "before-authored-metadata" and relative == ".supernote-module.json":
            raise KeyboardInterrupt
        original_write(stage, relative, content)
        if boundary == "after-authored-metadata" and relative == ".supernote-module.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(feature_generator, "_write", interrupt_at_boundary)
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    residues = list(root.glob(".sn-module-gen-feature-stage-*"))
    assert len(residues) == 1
    residue = residues[0]
    intervening_edit = residue / "author-intervening-edit.txt"
    intervening_edit.write_text("preserve this edit\n")

    monkeypatch.setattr(feature_generator, "_write", original_write)
    for arguments in (
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
        ["update", "--plain"],
        ["validate", "--plain"],
    ):
        code, _stdout, stderr = _invoke(root, arguments)
        assert code != 0
        assert "incomplete authored feature staging" in stderr
        assert str(residue) in stderr
        assert "Remove only the listed staging paths" in stderr
        assert intervening_edit.read_text() == "preserve this edit\n"
        assert not (root / "local_modules/alpha").exists()

    shutil.rmtree(residue)
    created = _add_native(root)
    assert (created / ".supernote-module.json").is_file()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race")
def test_public_add_never_writes_through_parent_swapped_after_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_ensure = filesystem.ensure_contained_parent_directories

    def ensure_then_swap(project, destination):
        original_ensure(project, destination)
        local_modules = root / "local_modules"
        local_modules.rename(root / "local_modules-original")
        local_modules.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        filesystem,
        "ensure_contained_parent_directories",
        ensure_then_swap,
    )

    code, _stdout, _stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code != 0
    assert not (outside / "alpha").exists()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-replace primitive")
def test_public_add_never_replaces_concurrently_created_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    original_rename = filesystem._posix_rename_directory_no_replace

    def create_destination_then_rename(*args, **kwargs):
        destination.mkdir()
        (destination / "canary.txt").write_text("concurrent owner\n")
        return original_rename(*args, **kwargs)

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        create_destination_then_rename,
    )

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 2
    assert "feature already exists" in stderr
    assert (destination / "canary.txt").read_text() == "concurrent owner\n"
    assert not (destination / ".supernote-module.json").exists()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory publication")
def test_real_posix_add_publishes_complete_authored_directory(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 0, stderr
    assert (destination / ".supernote-module.json").is_file()
    assert (destination / "android/src/main/cpp/feature.cpp").is_file()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-replace primitive")
def test_supported_noreplace_never_enters_linux_empty_directory_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")

    def supported_noreplace(source_parent, source_name, destination_parent, destination_name):
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
        )

    def reject_fallback(*_args, **_kwargs):
        raise AssertionError("weaker Linux fallback was reached")

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        supported_noreplace,
    )
    monkeypatch.setattr(
        filesystem,
        "_publish_linux_add_scaffold",
        reject_fallback,
    )

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 0, stderr
    assert (root / "local_modules/alpha/.supernote-module.json").is_file()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_unsupported_noreplace_uses_descriptor_relative_linux_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    observed: dict[str, object] = {}
    original_fallback = filesystem._publish_linux_add_scaffold

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def capture(
        canonical_root,
        source_parent,
        source_name,
        destination_parent,
        destination_name,
        source_before,
        destination,
        expected_files,
    ):
        observed.update(
            source_parent=source_parent,
            source_name=source_name,
            destination_parent=destination_parent,
            destination_name=destination_name,
            expected_files=expected_files,
        )
        return original_fallback(
            canonical_root,
            source_parent,
            source_name,
            destination_parent,
            destination_name,
            source_before,
            destination,
            expected_files,
        )

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_publish_linux_add_scaffold",
        capture,
    )

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 0, stderr
    assert observed["source_name"].startswith(".sn-module-gen-feature-stage-")
    assert observed["destination_name"] == "alpha"
    assert isinstance(observed["source_parent"], int)
    assert isinstance(observed["destination_parent"], int)
    assert ".supernote-module.json" in observed["expected_files"]
    assert (root / "local_modules/alpha/.supernote-module.json").is_file()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_fallback_refuses_a_concurrently_created_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    original_fallback = filesystem._publish_linux_add_scaffold

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def create_empty_then_publish(*args, **kwargs):
        destination.mkdir()
        return original_fallback(*args, **kwargs)

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_publish_linux_add_scaffold",
        create_empty_then_publish,
    )

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 2
    assert "feature already exists" in stderr
    assert destination.is_dir()
    assert not list(destination.iterdir())
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_fallback_detects_parent_substitution_after_retaining_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    local_modules = root / "local_modules"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_parent = root / "local_modules-original"
    original_fallback = filesystem._publish_linux_add_scaffold

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def swap_parent_then_publish(*args, **kwargs):
        local_modules.rename(original_parent)
        local_modules.symlink_to(outside, target_is_directory=True)
        return original_fallback(*args, **kwargs)

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_publish_linux_add_scaffold",
        swap_parent_then_publish,
    )

    with pytest.raises(filesystem.ConcurrentSourceMutation, match="ambiguous"):
        _add_native(root)

    assert not list(outside.iterdir())
    assert (original_parent / "alpha/.supernote-module.json").is_file()
    assert local_modules.is_symlink()
    assert local_modules.resolve() == outside.resolve()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
@pytest.mark.parametrize("destination_kind", ["empty", "nonempty", "file", "symlink"])
def test_linux_fallback_preserves_every_nonempty_or_nondirectory_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_kind: str,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_fallback = filesystem._publish_linux_add_scaffold

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def create_conflict_then_publish(*args, **kwargs):
        if destination_kind in {"empty", "nonempty"}:
            destination.mkdir()
            if destination_kind == "nonempty":
                (destination / "canary.txt").write_text("peer authored\n")
        elif destination_kind == "file":
            destination.write_text("peer authored file\n")
        else:
            destination.symlink_to(outside, target_is_directory=True)
        return original_fallback(*args, **kwargs)

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_publish_linux_add_scaffold",
        create_conflict_then_publish,
    )

    code, _stdout, stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 2
    assert "feature already exists" in stderr
    if destination_kind == "empty":
        assert destination.is_dir()
        assert not list(destination.iterdir())
    elif destination_kind == "nonempty":
        assert (destination / "canary.txt").read_text() == "peer authored\n"
        assert not (destination / ".supernote-module.json").exists()
    elif destination_kind == "file":
        assert destination.read_text() == "peer authored file\n"
    else:
        assert destination.is_symlink()
        assert destination.resolve() == outside.resolve()
        assert not list(outside.iterdir())
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
@pytest.mark.parametrize("source_kind", ["file", "symlink"])
@pytest.mark.parametrize("destination_kind", ["empty", "nonempty", "file", "symlink"])
def test_linux_fallback_source_substitution_preserves_every_destination_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    destination_kind: str,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    source_target = tmp_path / "source-target"
    destination_target = tmp_path / "destination-target"
    source_target.mkdir()
    destination_target.mkdir()
    saved_stage = root / "saved-original-stage"

    def substitute_then_report_unsupported(*_args, **_kwargs):
        staged = next(root.glob(".sn-module-gen-feature-stage-*"))
        staged.rename(saved_stage)
        if source_kind == "file":
            staged.write_text("peer source file\n")
        else:
            staged.symlink_to(source_target, target_is_directory=True)
        if destination_kind in {"empty", "nonempty"}:
            destination.mkdir()
            if destination_kind == "nonempty":
                (destination / "canary.txt").write_text("peer destination\n")
        elif destination_kind == "file":
            destination.write_text("peer destination file\n")
        else:
            destination.symlink_to(destination_target, target_is_directory=True)
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        substitute_then_report_unsupported,
    )

    code, _stdout, _stderr = _invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code != 0
    assert saved_stage.is_dir()
    if destination_kind == "empty":
        assert destination.is_dir()
        assert not list(destination.iterdir())
    elif destination_kind == "nonempty":
        assert (destination / "canary.txt").read_text() == "peer destination\n"
        assert len(list(destination.iterdir())) == 1
    elif destination_kind == "file":
        assert destination.read_text() == "peer destination file\n"
    else:
        assert destination.is_symlink()
        assert destination.resolve() == destination_target.resolve()
        assert not list(destination_target.iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
@pytest.mark.parametrize("boundary", ["after-marker", "after-file", "before-complete"])
def test_linux_destination_first_interruption_reruns_the_exact_incomplete_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    interrupted = False

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_once(current: str, _relative: str) -> None:
        nonlocal interrupted
        if not interrupted and current == boundary:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(filesystem, "_add_publication_checkpoint", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    marker = destination / filesystem.FEATURE_ADD_INCOMPLETE_MARKER
    assert interrupted
    assert marker.is_file()
    assert not list(root.glob(".sn-module-gen-feature-stage-*"))

    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        lambda *_args: None,
    )
    code, _stdout, stderr = _invoke(
        root,
        [
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--yes",
            "--plain",
            "--package-version",
            "1.0.0",
            "--description",
            "Local Supernote feature",
            "--android-namespace",
            "com.example.alpha",
            "--javascript-name",
            "Alpha",
        ],
    )
    assert code == 0, stderr
    assert not marker.exists()
    assert (destination / ".supernote-module.json").is_file()
    assert (destination / "android/src/main/cpp/feature.cpp").is_file()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_incomplete_add_refuses_and_preserves_an_intervening_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"
    completed_file: str | None = None

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_after_file(boundary: str, relative: str) -> None:
        nonlocal completed_file
        if boundary == "after-file":
            completed_file = relative
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        interrupt_after_file,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    assert completed_file is not None
    edited = destination / completed_file
    edited.write_bytes(edited.read_bytes() + b"peer edit\n")
    before = edited.read_bytes()
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        lambda *_args: None,
    )

    code, _stdout, stderr = _invoke(
        root,
        [
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--yes",
            "--plain",
            "--package-version",
            "1.0.0",
            "--description",
            "Local Supernote feature",
            "--android-namespace",
            "com.example.alpha",
            "--javascript-name",
            "Alpha",
        ],
    )

    assert code != 0
    assert "modified" in stderr
    assert edited.read_bytes() == before
    assert (destination / filesystem.FEATURE_ADD_INCOMPLETE_MARKER).is_file()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_public_add_refuses_and_preserves_an_incomplete_root_mode_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_after_marker(boundary: str, _relative: str) -> None:
        if boundary == "after-marker":
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        interrupt_after_marker,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    marker = destination / filesystem.FEATURE_ADD_INCOMPLETE_MARKER
    marker_before = marker.read_bytes()
    contents_before = set(destination.iterdir())
    destination.chmod(0o750)
    monkeypatch.setattr(filesystem, "_add_publication_checkpoint", lambda *_args: None)

    code, _stdout, stderr = _invoke(
        root,
        [
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--yes",
            "--plain",
            "--package-version",
            "1.0.0",
            "--description",
            "Local Supernote feature",
            "--android-namespace",
            "com.example.alpha",
            "--javascript-name",
            "Alpha",
        ],
    )

    assert code != 0
    assert "modified" in stderr
    assert stat.S_IMODE(destination.stat().st_mode) == 0o750
    assert marker.read_bytes() == marker_before
    assert set(destination.iterdir()) == contents_before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_public_add_refuses_a_mismatched_incomplete_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_after_marker(boundary: str, _relative: str) -> None:
        if boundary == "after-marker":
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        interrupt_after_marker,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    marker = destination / filesystem.FEATURE_ADD_INCOMPLETE_MARKER
    before = marker.read_bytes()
    monkeypatch.setattr(filesystem, "_add_publication_checkpoint", lambda *_args: None)
    code, _stdout, stderr = _invoke(
        root,
        [
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--yes",
            "--plain",
            "--package-version",
            "1.0.0",
            "--description",
            "Local Supernote feature",
            "--android-namespace",
            "com.example.alpha",
            "--javascript-name",
            "DifferentAlpha",
        ],
    )

    assert code != 0
    assert "marker changed" in stderr
    assert marker.read_bytes() == before
    assert set(destination.iterdir()) == {marker}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
@pytest.mark.parametrize("mutation", ["malformed-marker", "extra-entry"])
def test_linux_public_add_preserves_an_ambiguous_incomplete_scaffold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = _plugin(tmp_path / "plugin")
    destination = root / "local_modules/alpha"

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_after_marker(boundary: str, _relative: str) -> None:
        if boundary == "after-marker":
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        interrupt_after_marker,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)

    marker = destination / filesystem.FEATURE_ADD_INCOMPLETE_MARKER
    if mutation == "malformed-marker":
        marker.write_text("not a valid add marker\n", encoding="utf-8")
    else:
        (destination / "peer.txt").write_text("peer bytes\n", encoding="utf-8")
    before = {
        path.relative_to(destination).as_posix(): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in destination.rglob("*")
    }
    monkeypatch.setattr(filesystem, "_add_publication_checkpoint", lambda *_args: None)

    code, _stdout, _stderr = _invoke(
        root,
        [
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--yes",
            "--plain",
            "--package-version",
            "1.0.0",
            "--description",
            "Local Supernote feature",
            "--android-namespace",
            "com.example.alpha",
            "--javascript-name",
            "Alpha",
        ],
    )

    after = {
        path.relative_to(destination).as_posix(): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in destination.rglob("*")
    }
    assert code != 0
    assert after == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fallback")
def test_linux_public_add_refuses_an_unrelated_incomplete_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EINVAL, "filesystem does not support rename flags")

    def interrupt_after_marker(boundary: str, _relative: str) -> None:
        if boundary == "after-marker":
            raise KeyboardInterrupt

    monkeypatch.setattr(
        filesystem,
        "_posix_rename_directory_no_replace",
        unsupported,
    )
    monkeypatch.setattr(
        filesystem,
        "_add_publication_checkpoint",
        interrupt_after_marker,
    )
    with pytest.raises(KeyboardInterrupt):
        _add_native(root)
    marker = root / "local_modules/alpha" / filesystem.FEATURE_ADD_INCOMPLETE_MARKER
    before = marker.read_bytes()
    monkeypatch.setattr(filesystem, "_add_publication_checkpoint", lambda *_args: None)

    code, _stdout, stderr = _invoke(
        root,
        ["add", "beta", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code != 0
    assert "incomplete authored feature activation" in stderr
    assert marker.read_bytes() == before
    assert not (root / "local_modules/beta").exists()


def test_incomplete_authored_activation_marker_is_never_ignored(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = root / "local_modules/alpha"
    feature.mkdir(parents=True)
    marker = feature / ".supernote-add-incomplete"
    marker.write_text("incomplete authored feature activation\n")

    assert marker.is_file()
    for arguments in (["update", "--plain"], ["validate", "--plain"]):
        code, _stdout, stderr = _invoke(root, list(arguments))
        assert code == 1
        assert "incomplete authored feature activation" in stderr
        assert "remove only this incomplete scaffold" in stderr
        assert marker.is_file()


def test_doctor_reports_and_preserves_a_concurrent_authored_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = _add_native(root)
    _update(root)
    source = feature / "android/src/main/cpp/feature.cpp"
    edited = source.read_text() + "// concurrent doctor edit\n"

    def mutate_during_probe(self, *_args, **_kwargs):
        source.write_text(edited)
        return []

    monkeypatch.setattr(DoctorService, "_javascript_checks", mutate_during_probe)
    monkeypatch.setattr(DoctorService, "_android_checks", lambda self, *args: [])
    monkeypatch.setattr(DoctorService, "_native_checks", lambda self, *args: [])

    code, stdout, _stderr = _invoke(root, ["--json", "doctor"])
    result = json.loads(stdout)

    assert code == 3
    assert result["status"] == "partial"
    assert result["doctor"]["required_passed"] is False
    integrity = next(
        check
        for check in result["doctor"]["checks"]
        if check["id"] == "doctor_source_integrity"
    )
    assert integrity["status"] == "failed"
    assert source.read_text() == edited


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race")
def test_parent_creation_refuses_swapped_symlink_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path / "plugin")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = filesystem.os.mkdir
    raced = False

    def swap_before_create(path, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if path == "scope" and dir_fd is not None and not raced:
            raced = True
            (root / "scope").symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem.os, "mkdir", swap_before_create)
    with pytest.raises(OSError):
        filesystem.ensure_contained_parent_directories(
            root,
            root / "scope/package/output.json",
        )

    assert not (outside / "package").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race")
def test_descriptor_inventory_never_hashes_through_swapped_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    original_open = filesystem_inventory._open_read_descriptor

    def swap_before_open(path, flags, *, dir_fd=None):
        if path == "nested" and dir_fd is not None and nested.is_dir():
            nested.rename(root / "nested-original")
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_inventory, "_open_read_descriptor", swap_before_open)

    with pytest.raises(OSError):
        filesystem.source_tree_inventory(root)
    assert (root / "nested").resolve() == outside.resolve()
    assert (outside / "secret.txt").read_text() == "outside"


def test_plan_rejects_author_edit_during_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path)
    feature = _add_native(root)
    source = feature / "android/src/main/cpp/feature.cpp"
    service = GenerationService(root)
    original_render = service._render_owned

    def render_then_edit(*args, **kwargs):
        result = original_render(*args, **kwargs)
        source.write_text(source.read_text() + "// concurrent author edit\n")
        return result

    monkeypatch.setattr(service, "_render_owned", render_then_edit)
    with pytest.raises(Exception, match="changed while the generation plan was rendered"):
        service.plan(
            operation="update",
            requested_targets=("alpha",),
            allow_unmanifested_bootstrap=True,
        )


def test_windows_observed_read_does_not_request_metadata_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)

    def capture(_path, **options):
        observed.update(options)
        raise RuntimeError("captured")

    monkeypatch.setattr(filesystem, "_windows_open_no_follow_handle", capture)
    with pytest.raises(RuntimeError, match="captured"):
        filesystem._windows_open_observed_descriptor(tmp_path / "source.kt")
    assert observed["write_metadata"] is False


def test_jvm_frontend_uses_standalone_generated_analysis_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(tmp_path)
    gradle = tmp_path / ("gradle.bat" if os.name == "nt" else "gradle")
    gradle.write_text("#!/bin/sh\n")
    gradle.chmod(0o755)
    monkeypatch.setenv("SUPERNOTE_GRADLE_COMMAND", str(gradle))
    feature = FeatureOperationService(root).add(
        FeatureConfig(
            output=root / "local_modules/jvm",
            npm_name="jvm",
            package_version="1.0.0",
            android_namespace="com.example.jvm",
            public_name="Jvm",
            starters=(StarterFamily.JVM,),
        )
    )
    project = ProjectModel.discover(root, allow_unmanifested_bootstrap=True)
    feature_id = project.features[0].identity.feature_id
    service = GenerationService(root)
    bootstrap = service.plan(
        operation="add",
        requested_targets=("jvm",),
        jvm_apis={feature_id: SemanticApi()},
        allow_unmanifested_bootstrap=True,
    )
    service.execute(bootstrap, finalize=False)
    captured = {}

    def run(command, *, cwd, timeout):
        captured.update(command=command, cwd=cwd, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(jvm_frontend, "run_process", run)
    monkeypatch.setattr(jvm_frontend, "load_jvm_frontend_manifests", lambda *_args: {})

    assert JvmFrontendService(root).manifests(
        allow_unmanifested_bootstrap=True
    ) == {}
    analysis = root / "android/.supernote-module/runtime/analysis"
    assert captured["cwd"] == analysis
    assert captured["command"][0] == str(gradle)
    assert captured["command"][-2:] == [str(analysis), ":subject:kspKotlin"]
    assert str(root / "android/gradlew") not in captured["command"]
    assert ":supernote-runtime:kspDebugKotlin" not in captured["command"]
    assert (analysis / "settings.gradle").is_file()
    assert (analysis / "subject/build.gradle").is_file()
    assert (root / INCOMPLETE_MARKER_PATH).is_file()
    assert feature.is_dir()
