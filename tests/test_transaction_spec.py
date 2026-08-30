from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.errors import ConcurrentSourceMutation, FilesystemError
import supernote_module_generator.transaction as transaction_module
import supernote_module_generator.filesystem as filesystem_module
from supernote_module_generator.errors import ConfigurationError
from supernote_module_generator.project import read_parent_package
from supernote_module_generator.transaction import JOURNAL_NAME, Transaction, recover_pending
from supernote_module_generator.filesystem import (
    ProtectedSourceGuard,
    read_contained_regular_bytes_no_follow,
    copy_entry_no_follow,
    protected_directory_metadata,
    restore_protected_directory_metadata,
    retain_directory_metadata_recovery,
    validate_transaction_metadata_recovery,
    hash_entry_no_follow,
    source_tree_inventory,
)
from project_inventory import inventory_project


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_contained_read_rejects_final_symlink_substitution_without_external_read(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    state = root / ".supernote-module/manifest.json"
    state.parent.mkdir(parents=True)
    state.write_text("inside\n")
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n")
    before = outside.lstat()
    original_open = os.open
    substituted = False

    def substituting_open(path, flags, *args, **kwargs):
        nonlocal substituted
        if path == "manifest.json" and kwargs.get("dir_fd") is not None:
            substituted = True
            state.unlink()
            state.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(filesystem_module.os, "open", substituting_open)

    with pytest.raises(FilesystemError):
        read_contained_regular_bytes_no_follow(root, state)

    after = outside.lstat()
    assert substituted
    assert outside.read_bytes() == b"outside\n"
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        before.st_mode,
        before.st_atime_ns,
        before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_contained_read_stays_on_open_root_after_ancestor_substitution(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    state = root / ".supernote-module/manifest.json"
    state.parent.mkdir(parents=True)
    state.write_text("inside\n")
    outside = tmp_path / "outside-state"
    outside.mkdir()
    external = outside / "manifest.json"
    external.write_text("outside\n")
    before = external.lstat()
    original_open = os.open
    substituted = False

    def substituting_open(path, flags, *args, **kwargs):
        nonlocal substituted
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == ".supernote-module" and kwargs.get("dir_fd") is not None:
            substituted = True
            state.parent.rename(root / ".supernote-module-detached")
            state.parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem_module.os, "open", substituting_open)

    content, _metadata = read_contained_regular_bytes_no_follow(root, state)

    after = external.lstat()
    assert substituted
    assert content == b"inside\n"
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        before.st_mode,
        before.st_atime_ns,
        before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_contained_metadata_restore_uses_open_directory_not_replacement_symlink(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    protected = root / "android/app"
    protected.mkdir(parents=True)
    (protected / "source.kt").write_text("class Source\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o751)
    outside_stat = outside.lstat()
    baseline = protected_directory_metadata(root)
    original_open = os.open
    substituted = False

    def substituting_open(path, flags, *args, **kwargs):
        nonlocal substituted
        descriptor = original_open(path, flags, *args, **kwargs)
        if (
            path == "app"
            and kwargs.get("dir_fd") is not None
            and not substituted
        ):
            substituted = True
            protected.rename(root / "android/app-detached")
            protected.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem_module.os, "open", substituting_open)

    try:
        failures = restore_protected_directory_metadata(root, baseline)
    except FilesystemError:
        failures = ("rejected",)

    after = outside.lstat()
    assert substituted
    assert failures
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        outside_stat.st_mode,
        outside_stat.st_atime_ns,
        outside_stat.st_mtime_ns,
    )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_contained_metadata_restore_rejects_replaced_ancestor_without_external_write(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    protected = root / "android/app"
    protected.mkdir(parents=True)
    (protected / "source.kt").write_text("class Source\n")
    outside = tmp_path / "outside"
    (outside / "app").mkdir(parents=True)
    outside.chmod(0o751)
    (outside / "app").chmod(0o750)
    outside_before = {
        ".": outside.lstat(),
        "app": (outside / "app").lstat(),
    }
    baseline = protected_directory_metadata(root)
    original_open = os.open
    substituted = False

    def substituting_open(path, flags, *args, **kwargs):
        nonlocal substituted
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "android" and kwargs.get("dir_fd") is not None and not substituted:
            substituted = True
            (root / "android").rename(root / "android-detached")
            (root / "android").symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem_module.os, "open", substituting_open)

    try:
        failures = restore_protected_directory_metadata(root, baseline)
    except FilesystemError:
        failures = ("rejected",)

    assert substituted
    assert failures
    for relative, before in outside_before.items():
        after = (outside if relative == "." else outside / relative).lstat()
        assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
            before.st_mode,
            before.st_atime_ns,
            before.st_mtime_ns,
        )


def test_rollback_restores_files_and_removes_created_tree(tmp_path: Path):
    parent = tmp_path / "package.json"
    parent.write_text('{"before": true}\n', encoding="utf-8")
    created = tmp_path / "local_modules/local-math"
    transaction = Transaction(tmp_path, "add", ["local-math"])
    transaction.snapshot([parent, tmp_path / "local_modules"])
    (tmp_path / "local_modules").mkdir()
    created.mkdir()
    (created / "file").write_text("new", encoding="utf-8")
    parent.write_text('{"after": true}\n', encoding="utf-8")
    transaction.mark_write()

    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert parent.read_text(encoding="utf-8") == '{"before": true}\n'
    assert not (tmp_path / "local_modules").exists()
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_completed_rollback_restores_exact_whole_project_inventory(tmp_path: Path):
    package = tmp_path / "package.json"
    package.write_text('{"before":true}\n', encoding="utf-8")
    package.chmod(0o640)
    settings = tmp_path / "android/settings.gradle"
    settings.parent.mkdir(parents=True)
    settings.write_text("include ':app'\n", encoding="utf-8")
    source = tmp_path / "local_modules/existing/source.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int preserved = 1;\n", encoding="utf-8")
    before = inventory_project(tmp_path)

    transaction = Transaction(tmp_path, "update", ["existing"])
    transaction.snapshot([package, settings])
    generated_parent = tmp_path / "android/.supernote-module"
    generated_child = generated_parent / "v4-staging/generated.cpp"
    transaction.track_created_directory(generated_parent)
    transaction.track_created_directory(generated_child.parent)
    generated_child.parent.mkdir(parents=True)
    generated_child.write_text("int generated = 2;\n", encoding="utf-8")
    transaction.track_created(generated_child.parent)
    package.write_text('{"after":true}\n', encoding="utf-8")
    package.chmod(0o600)
    settings.write_text("include ':changed'\n", encoding="utf-8")
    transaction.mark_write()

    rollback = transaction.rollback()

    assert rollback.status == "completed"
    assert inventory_project(tmp_path) == before


def test_transaction_snapshot_and_rollback_preserve_direct_symlink_entries(
    tmp_path: Path,
):
    target_file = tmp_path / "target.txt"
    target_file.write_text("target remains\n", encoding="utf-8")
    target_directory = tmp_path / "target-directory"
    target_directory.mkdir()
    (target_directory / "sentinel").write_text("untouched\n", encoding="utf-8")
    links = {
        tmp_path / "relative-file": ("target.txt", False),
        tmp_path / "absolute-directory": (str(target_directory), True),
        tmp_path / "broken-file": ("missing.txt", False),
        tmp_path / "broken-directory": ("missing-directory", True),
    }
    for link, (target, target_is_directory) in links.items():
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symbolic links are unavailable on this host: {exc}")
    before = inventory_project(tmp_path)

    transaction = Transaction(tmp_path, "update", ["links"])
    transaction.snapshot(links)
    for link in links:
        link.unlink()
        link.write_text("wrong entry kind\n", encoding="utf-8")
    transaction.mark_write()

    rollback = transaction.rollback()

    assert rollback.status == "completed"
    assert inventory_project(tmp_path) == before
    assert (target_directory / "sentinel").read_text(encoding="utf-8") == (
        "untouched\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink metadata contract")
def test_direct_symlink_copy_preserves_exact_metadata_after_target_observation(
    tmp_path: Path,
):
    source = tmp_path / "source-link"
    destination = tmp_path / "destination-link"
    source.symlink_to("relative-target")
    expected_times = (11_000_000_000, 12_000_000_000)
    os.utime(source, ns=expected_times, follow_symlinks=False)
    expected = source.lstat()

    copy_entry_no_follow(source, destination)

    copied = destination.lstat()
    assert os.readlink(destination) == "relative-target"
    assert stat.S_IMODE(copied.st_mode) == stat.S_IMODE(expected.st_mode)
    assert (copied.st_atime_ns, copied.st_mtime_ns) == expected_times


def test_snapshot_deduplicates_repeated_lexical_paths_in_one_call(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["feature"])

    transaction.snapshot([path, path, tmp_path / "." / "package.json"])

    entries = transaction.data["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 1
    assert entries[0]["path"] == str(path)
    assert transaction.rollback().status == "completed"


def test_transaction_rejects_managed_destination_below_escaping_symlink_parent(
    tmp_path: Path,
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-managed"
    outside.mkdir()
    parent = tmp_path / "managed-parent"
    try:
        parent.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable on this host: {exc}")
    transaction = Transaction(tmp_path, "update", ["feature"])

    with pytest.raises(FilesystemError, match="escapes the plugin root"):
        transaction.snapshot([parent / "generated.cpp"])

    assert not (outside / "generated.cpp").exists()
    assert transaction.rollback().status == "completed"


def test_rollback_removes_only_empty_generator_created_parent_directories(
    tmp_path: Path,
):
    empty_parent = tmp_path / "local_modules"
    preserved_parent = tmp_path / "android/.supernote-module"
    transaction = Transaction(tmp_path, "add", ["local-math"])
    transaction.track_created_directory(empty_parent)
    transaction.track_created_directory(preserved_parent)
    empty_parent.mkdir()
    preserved_parent.mkdir(parents=True)
    (preserved_parent / "user-file").write_text("keep", encoding="utf-8")

    rollback = transaction.rollback()

    assert rollback.status == "completed"
    assert not empty_parent.exists()
    assert (preserved_parent / "user-file").read_text(encoding="utf-8") == "keep"


def test_startup_recovery_uses_persistent_journal(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_write()

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert path.read_text(encoding="utf-8") == "before"
    assert outcome.warning is not None
    assert 'interrupted Update for "local-math"' in outcome.warning.message


def test_fresh_process_rollback_restores_persisted_directory_metadata(tmp_path: Path):
    source = tmp_path / "local_modules/alpha/source.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("before\n")
    metadata = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.record_directory_metadata(metadata)
    transaction.snapshot([source])
    source.write_text("after\n")
    os.utime(source.parent, ns=(1_000_000_000, 2_000_000_000))
    transaction.mark_write()

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "completed"
    assert protected_directory_metadata(tmp_path) == metadata
    assert source.read_text() == "before\n"


def test_startup_recovery_finishes_durable_abandon_without_restoring_snapshot(
    tmp_path: Path,
):
    package = tmp_path / "package.json"
    package.write_text('{"baseline":true}\n')
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.snapshot([package])
    staging = tmp_path / ".sn-module-gen-plan-staging"
    staging.mkdir()
    (staging / "generated").write_text("staged\n")
    transaction.track_created(staging)
    package.write_text('{"concurrent_user_edit":true}\n')
    transaction.data["phase"] = "abandon"
    transaction._persist()

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "not_needed"
    assert json.loads(package.read_text()) == {"concurrent_user_edit": True}
    assert not staging.exists()
    assert not (tmp_path / JOURNAL_NAME).exists()
    assert not transaction.state_dir.exists()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside",
        "/tmp/outside",
        "C:/outside",
        "//server/share",
        r"..\outside",
    ],
)
def test_startup_recovery_rejects_unsafe_persisted_directory_metadata_before_mutation(
    tmp_path: Path,
    unsafe_path: str,
):
    source = tmp_path / "source.txt"
    source.write_text("before\n")
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    outside_before = outside.lstat()
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.snapshot([source])
    source.write_text("after\n")
    transaction.mark_write()
    transaction.data["directory_metadata"] = {
        unsafe_path: [0o700, 1_000_000_000, 2_000_000_000]
    }
    transaction._persist()

    with pytest.raises(Exception, match="recovery|metadata|path"):
        recover_pending(tmp_path)

    assert source.read_text() == "after\n"
    outside_after = outside.lstat()
    assert (outside_after.st_mode, outside_after.st_mtime_ns) == (
        outside_before.st_mode,
        outside_before.st_mtime_ns,
    )
    assert (tmp_path / JOURNAL_NAME).exists()


def test_startup_recovery_rejects_directory_metadata_symlink_ancestor(tmp_path: Path):
    outside = tmp_path.parent / "outside-link-target"
    child = outside / "child"
    child.mkdir(parents=True)
    before = child.lstat()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.data["directory_metadata"] = {
        "link/child": [0o700, 1_000_000_000, 2_000_000_000]
    }
    transaction._persist()

    with pytest.raises(Exception, match="recovery|metadata|symbolic-link"):
        recover_pending(tmp_path)

    after = child.lstat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    assert (tmp_path / JOURNAL_NAME).exists()


def test_restore_directory_metadata_verifies_atime(tmp_path: Path, monkeypatch):
    metadata = protected_directory_metadata(tmp_path)
    if os.name == "nt":
        original_apply = filesystem_module._windows_apply_handle_metadata_values

        def wrong_windows_atime(handle, **values):
            if values["atime_ns"] is not None:
                values["atime_ns"] += 1_000_000_000
            original_apply(handle, **values)

        monkeypatch.setattr(
            filesystem_module,
            "_windows_apply_handle_metadata_values",
            wrong_windows_atime,
        )
        assert restore_protected_directory_metadata(tmp_path, metadata) == (
            "modified:.",
        )
        return
    original_utime = os.utime

    def wrong_atime(path, *, ns, follow_symlinks=None):
        options = (
            {}
            if follow_symlinks is None
            else {"follow_symlinks": follow_symlinks}
        )
        original_utime(
            path,
            ns=(ns[0] + 1_000_000_000, ns[1]),
            **options,
        )

    monkeypatch.setattr(os, "utime", wrong_atime)
    assert restore_protected_directory_metadata(tmp_path, metadata) == ("modified:.",)


@pytest.mark.parametrize("phase", ["commit", "abandon"])
def test_fresh_process_durable_cleanup_restores_directory_metadata(
    tmp_path: Path,
    phase: str,
):
    metadata = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.record_directory_metadata(metadata)
    transaction.data["phase"] = phase
    transaction._persist()
    os.chmod(tmp_path, 0o750)
    os.utime(tmp_path, ns=(1_000_000_000, 2_000_000_000))

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "not_needed"
    assert protected_directory_metadata(tmp_path) == metadata
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_external_reconciliation_failure_remains_recoverable(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "remove", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_external(["npm", "install"])

    rollback = transaction.rollback(reconcile=lambda _: False)
    assert rollback.status == "partial"
    assert (tmp_path / JOURNAL_NAME).is_file()
    journal = json.loads((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8"))
    assert journal["phase"] == "rollback_partial"


def test_update_activation_restores_original_directory(tmp_path: Path):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "implementation.kt").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated.kt").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    transaction.activate(staged, destination)
    assert (destination / "generated.kt").is_file()

    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert (destination / "implementation.kt").read_text() == "original"


def test_partial_rollback_can_be_retried_without_reapplying_restored_entries(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_external(["npm", "install"])

    first = transaction.rollback(reconcile=lambda _: False)
    assert first.status == "partial"
    assert path.read_text(encoding="utf-8") == "before"

    second = recover_pending(tmp_path, reconcile=lambda _: True)
    assert second.rollback.status == "completed"
    assert path.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_corrupt_startup_journal_is_a_structured_partial_result(tmp_path: Path):
    (tmp_path / "android").mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n', encoding="utf-8"
    )
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    (tmp_path / JOURNAL_NAME).write_text("not json\n", encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        ["--json", "validate", "--all"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=tmp_path,
    )

    value = json.loads(stdout.getvalue())
    assert code == 3
    assert stderr.getvalue() == ""
    assert value["status"] == "partial"
    assert value["rollback"]["status"] == "failed"
    assert value["error"]["kind"] == "startup_recovery_failed"


def test_recovery_does_not_delete_original_when_activation_rename_never_started(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "source").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    original_replace = os.replace

    def fail_first_module_move(source, target):
        if Path(source) == destination:
            raise OSError("simulated crash window")
        original_replace(source, target)

    monkeypatch.setattr("supernote_module_generator.transaction.os.replace", fail_first_module_move)
    try:
        transaction.activate(staged, destination)
    except OSError:
        pass
    monkeypatch.setattr("supernote_module_generator.transaction.os.replace", original_replace)

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert (destination / "source").read_text(encoding="utf-8") == "original"


def test_recovery_recognizes_original_restored_before_journal_update(tmp_path: Path):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "source").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    transaction.activate(staged, destination)
    entry = transaction.data["entries"][1]
    restore = Path(entry["restore"])
    for child in destination.iterdir():
        child.unlink()
    destination.rmdir()
    os.replace(restore, destination)

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert (destination / "source").read_text(encoding="utf-8") == "original"


def test_startup_finalizes_a_transaction_that_reached_commit(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "add", ["local-math"])
    transaction.snapshot([path])
    path.write_text("committed", encoding="utf-8")
    transaction.data["phase"] = "commit"
    transaction._persist()

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "not_needed"
    assert path.read_text(encoding="utf-8") == "committed"
    assert outcome.warning is not None
    assert "already committed" in outcome.warning.message


@pytest.mark.parametrize(
    "phase", ["stage", "apply", "rollback", "rollback_partial", "commit", "abandon"]
)
@pytest.mark.parametrize(
    "corruption",
    [
        "false_existed",
        "wrong_restore_slot",
        "duplicate",
        "plugin_root",
        "unrelated_source",
    ],
)
def test_startup_recovery_rejects_unauthorized_transaction_entries_without_mutation(
    tmp_path: Path,
    phase: str,
    corruption: str,
):
    sentinel = tmp_path / "local_modules/existing/source.cpp"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["existing"])
    transaction.snapshot([sentinel])
    transaction.data["phase"] = phase
    transaction._persist()
    journal_path = tmp_path / JOURNAL_NAME
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    entry = journal["entries"][0]
    if corruption == "false_existed":
        entry["existed"] = False
    elif corruption == "wrong_restore_slot":
        entry["restore"] = str(transaction.state_dir / "restore/999")
    elif corruption == "duplicate":
        journal["entries"].append(dict(entry))
    elif corruption == "plugin_root":
        entry["path"] = str(tmp_path)
    else:
        unrelated = tmp_path / "android/app/src/main/App.kt"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep app\n", encoding="utf-8")
        entry["path"] = str(unrelated)
    journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(Exception, match="recovery|authority|entry|transaction"):
        recover_pending(tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    if corruption == "unrelated_source":
        assert (tmp_path / "android/app/src/main/App.kt").read_text() == "keep app\n"
    assert journal_path.exists()
    assert transaction.state_dir.exists()


@pytest.mark.parametrize("phase", ["commit", "abandon"])
def test_fresh_process_durable_metadata_failure_exposes_retained_recovery_authority(
    tmp_path: Path,
    monkeypatch,
    phase: str,
):
    metadata = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.record_directory_metadata(metadata)
    transaction.data["phase"] = phase
    transaction._persist()
    calls = 0

    def fail_final_root_restore(_root, current):
        nonlocal calls
        calls += 1
        return ("modified:.",) if "." in current else ()

    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        fail_final_root_restore,
    )

    with pytest.raises(Exception) as raised:
        recover_pending(tmp_path)

    message = str(raised.value)
    match = re.search(r"Recovery authority remains at ([^.]\S*)", message)
    assert calls >= 1
    assert match is not None, message
    assert Path(match.group(1).rstrip(".")).is_dir()
    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        restore_protected_directory_metadata,
    )
    recover_pending(tmp_path)


@pytest.mark.parametrize(
    "corruption",
    [
        "transaction_id",
        "outcome",
        "guard_bundle",
        "nonempty_entries",
        "tampered_manifest",
        "unsafe_ancestry",
    ],
)
def test_recovery_pointer_is_bound_to_transaction_metadata_bundle_before_mutation(
    tmp_path: Path,
    corruption: str,
):
    sentinel = tmp_path / "source.cpp"
    sentinel.write_text("preserve\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["alpha"])
    metadata = protected_directory_metadata(tmp_path)
    transaction.record_directory_metadata(metadata)
    transaction.data["phase"] = "commit"
    transaction._persist()
    recovery, bundle_id, digest = retain_directory_metadata_recovery(
        tmp_path,
        metadata,
        transaction_id=transaction.identifier,
        outcome="commit",
    )
    pointer = transaction_module._publish_recovery_pointer(
        tmp_path,
        transaction.data,
        recovery,
        "commit",
        bundle_id=bundle_id,
        manifest_sha256=digest,
    )
    pointer_data = json.loads(pointer.read_text(encoding="utf-8"))
    if corruption == "transaction_id":
        pointer_data["transaction_id"] = "0" * 32
    elif corruption == "outcome":
        pointer_data["outcome"] = "abandon"
    elif corruption == "guard_bundle":
        guard = ProtectedSourceGuard(tmp_path)
        pointer_data["recovery_path"] = str(guard._temporary.resolve(strict=True))
        manifest_payload = (guard._temporary / "recovery-manifest.json").read_bytes()
        pointer_data["manifest_sha256"] = hashlib.sha256(manifest_payload).hexdigest()
    elif corruption in {"nonempty_entries", "tampered_manifest"}:
        manifest_path = recovery / "recovery-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if corruption == "nonempty_entries":
            manifest["entries"] = [{"destination": "source.cpp"}]
        else:
            manifest["directories"][0]["mode"] ^= 0o111
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(payload)
        if corruption == "nonempty_entries":
            pointer_data["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    else:
        unsafe = tmp_path / "unsafe-bundle"
        unsafe.mkdir()
        (unsafe / "recovery-manifest.json").write_bytes(
            (recovery / "recovery-manifest.json").read_bytes()
        )
        pointer_data["recovery_path"] = str(unsafe)
    pointer.write_text(json.dumps(pointer_data, indent=2) + "\n", encoding="utf-8")
    journal_before = transaction.journal_path.read_bytes()
    authority = transaction.state_dir / str(transaction.data["entry_authority"])
    authority_before = authority.read_bytes()
    sentinel_before = sentinel.read_bytes()

    with pytest.raises(Exception, match="recovery|pointer|binding|manifest|unsafe"):
        recover_pending(tmp_path)

    assert sentinel.read_bytes() == sentinel_before
    assert transaction.journal_path.read_bytes() == journal_before
    assert authority.read_bytes() == authority_before
    assert pointer.exists()
    assert recovery.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_recovery_registry_rejects_a_forged_shared_directory(
    tmp_path: Path,
    monkeypatch,
):
    identity = str(os.getuid())
    registry = tmp_path / f"supernote-module-recovery-v2-{identity}"
    registry.mkdir(mode=0o777)
    registry.chmod(0o777)
    sentinel = registry / "sentinel"
    sentinel.write_bytes(b"untrusted\n")
    before = sentinel.lstat()
    monkeypatch.setattr(transaction_module.tempfile, "gettempdir", lambda: str(tmp_path))

    with pytest.raises(FilesystemError, match="not private"):
        transaction_module._recovery_registry()

    after = sentinel.lstat()
    assert sentinel.read_bytes() == b"untrusted\n"
    assert stat.S_IMODE(registry.lstat().st_mode) == 0o777
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        before.st_mode,
        before.st_atime_ns,
        before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_metadata_recovery_rejects_a_world_accessible_bundle(
    tmp_path: Path,
):
    metadata = protected_directory_metadata(tmp_path)
    transaction_id = "1" * 32
    recovery, bundle_id, digest = retain_directory_metadata_recovery(
        tmp_path,
        metadata,
        transaction_id=transaction_id,
        outcome="rollback",
    )
    recovery.chmod(0o777)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserve\n")
    before = sentinel.lstat()

    with pytest.raises(FilesystemError, match="ancestry is unsafe"):
        validate_transaction_metadata_recovery(
            recovery,
            tmp_path,
            transaction_id=transaction_id,
            outcome="rollback",
            bundle_id=bundle_id,
            manifest_sha256=digest,
        )

    after = sentinel.lstat()
    assert sentinel.read_bytes() == b"preserve\n"
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        before.st_mode,
        before.st_atime_ns,
        before.st_mtime_ns,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda _manifest: b"not-json\n", "manifest is invalid"),
        (lambda _manifest: b"[]\n", "binding is invalid"),
        (
            lambda manifest: {
                **manifest,
                "schema_version": 3,
            },
            "binding is invalid",
        ),
        (
            lambda manifest: {
                **manifest,
                "entries": [{"destination": "source.cpp"}],
            },
            "binding is invalid",
        ),
        (
            lambda manifest: {
                **manifest,
                "directories": [{"destination": ".", "mode": 0o700}],
            },
            "entry is invalid",
        ),
        (
            lambda manifest: {
                **manifest,
                "directories": [
                    {
                        "destination": ".",
                        "mode": True,
                        "atime_ns": 1,
                        "mtime_ns": 2,
                    }
                ],
            },
            "entry is invalid",
        ),
        (
            lambda manifest: {
                **manifest,
                "directories": [
                    manifest["directories"][0],
                    manifest["directories"][0],
                ],
            },
            "entries are duplicated",
        ),
    ],
)
def test_metadata_recovery_rejects_invalid_manifest_shapes_without_mutation(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserve\n")
    sentinel_before = sentinel.lstat()
    transaction_id = "2" * 32
    recovery, bundle_id, _digest = retain_directory_metadata_recovery(
        tmp_path,
        protected_directory_metadata(tmp_path),
        transaction_id=transaction_id,
        outcome="rollback",
    )
    manifest_path = recovery / "recovery-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = mutation(manifest)
    if isinstance(changed, bytes):
        payload = changed
    else:
        payload = (json.dumps(changed, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(payload)
    manifest_path.chmod(0o600)

    with pytest.raises(FilesystemError, match=message):
        validate_transaction_metadata_recovery(
            recovery,
            tmp_path,
            transaction_id=transaction_id,
            outcome="rollback",
            bundle_id=bundle_id,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
        )

    sentinel_after = sentinel.lstat()
    assert sentinel.read_bytes() == b"preserve\n"
    assert (
        sentinel_after.st_mode,
        sentinel_after.st_atime_ns,
        sentinel_after.st_mtime_ns,
    ) == (
        sentinel_before.st_mode,
        sentinel_before.st_atime_ns,
        sentinel_before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file mode contract")
def test_metadata_recovery_rejects_a_nonprivate_manifest(tmp_path: Path) -> None:
    transaction_id = "3" * 32
    recovery, bundle_id, digest = retain_directory_metadata_recovery(
        tmp_path,
        protected_directory_metadata(tmp_path),
        transaction_id=transaction_id,
        outcome="commit",
    )
    (recovery / "recovery-manifest.json").chmod(0o644)

    with pytest.raises(FilesystemError, match="manifest is not private"):
        validate_transaction_metadata_recovery(
            recovery,
            tmp_path,
            transaction_id=transaction_id,
            outcome="commit",
            bundle_id=bundle_id,
            manifest_sha256=digest,
        )


def test_metadata_recovery_rejects_an_unavailable_manifest_after_path_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transaction_id = "4" * 32
    recovery, bundle_id, digest = retain_directory_metadata_recovery(
        tmp_path,
        protected_directory_metadata(tmp_path),
        transaction_id=transaction_id,
        outcome="abandon",
    )
    manifest_path = recovery / "recovery-manifest.json"
    original_open = filesystem_module.os.open

    def unavailable_open(path, flags, *args, **kwargs):
        if Path(path) == manifest_path:
            raise OSError("manifest unavailable")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(filesystem_module.os, "open", unavailable_open)

    with pytest.raises(FilesystemError, match="manifest is unavailable"):
        validate_transaction_metadata_recovery(
            recovery,
            tmp_path,
            transaction_id=transaction_id,
            outcome="abandon",
            bundle_id=bundle_id,
            manifest_sha256=digest,
        )


def test_metadata_recovery_directory_parser_rejects_nonlist_input() -> None:
    with pytest.raises(FilesystemError, match="binding is invalid"):
        filesystem_module._metadata_recovery_directories({})


def test_metadata_recovery_rejects_a_missing_recovery_directory(
    tmp_path: Path,
) -> None:
    missing = Path(filesystem_module.tempfile.gettempdir()) / (
        "sn-module-gen-metadata-recovery-missing"
    )
    assert not missing.exists()

    with pytest.raises(FilesystemError, match="ancestry is unsafe"):
        validate_transaction_metadata_recovery(
            missing,
            tmp_path,
            transaction_id="5" * 32,
            outcome="rollback",
            bundle_id="6" * 32,
            manifest_sha256="7" * 64,
        )


def test_metadata_recovery_containment_decision_is_explicit(tmp_path: Path) -> None:
    nested = tmp_path / "nested/value"
    outside = tmp_path.parent / "outside"

    assert filesystem_module._path_is_within(nested, tmp_path)
    assert not filesystem_module._path_is_within(outside, tmp_path)


@pytest.mark.parametrize(
    ("transaction_id", "outcome"),
    [
        ("short", "rollback"),
        ("G" * 32, "rollback"),
        ("8" * 32, "unknown"),
    ],
)
def test_metadata_recovery_rejects_noncanonical_creation_binding(
    tmp_path: Path,
    transaction_id: str,
    outcome: str,
) -> None:
    with pytest.raises(FilesystemError, match="binding is invalid"):
        retain_directory_metadata_recovery(
            tmp_path,
            protected_directory_metadata(tmp_path),
            transaction_id=transaction_id,
            outcome=outcome,
        )


def test_atomic_backup_restoration_mismatch_preserves_displaced_live_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "recovery-source"
    destination = tmp_path / "destination"
    source.write_bytes(b"old baseline\n")
    destination.write_bytes(b"external current\n")
    destination.chmod(0o640)
    os.utime(destination, ns=(1_700_000_111_000_000_000, 1_700_000_222_000_000_000))
    before = destination.lstat()

    monkeypatch.setattr(
        filesystem_module,
        "hash_entry_no_follow",
        lambda path: "published" if path == destination else "baseline",
    )

    with pytest.raises(FilesystemError, match="activation mismatch"):
        filesystem_module._restore_backup_entry_atomic(source, destination)

    after = destination.lstat()
    assert destination.read_bytes() == b"external current\n"
    assert (
        stat.S_IMODE(after.st_mode),
        after.st_atime_ns,
        after.st_mtime_ns,
    ) == (
        stat.S_IMODE(before.st_mode),
        before.st_atime_ns,
        before.st_mtime_ns,
    )
    assert not tuple(tmp_path.glob(".destination.sn-module-gen-*"))


def test_hash_inventory_snapshot_and_guard_preserve_directory_atimes(tmp_path: Path):
    nested = tmp_path / "local_modules/alpha/src/deep"
    nested.mkdir(parents=True)
    (nested / "source.cpp").write_text("int value = 1;\n", encoding="utf-8")
    directories = [tmp_path, *[path for path in tmp_path.rglob("*") if path.is_dir()]]
    expected: dict[Path, tuple[int, int, int]] = {}
    for index, directory in enumerate(reversed(directories)):
        values = (0o750, 20_000_000_000 + index, 30_000_000_000 + index)
        directory.chmod(values[0])
        os.utime(directory, ns=values[1:])
        expected[directory] = values

    assert hash_entry_no_follow(tmp_path) is not None
    source_tree_inventory(tmp_path)
    guard = ProtectedSourceGuard(tmp_path)
    assert guard.finish() == ()
    transaction = Transaction(tmp_path, "check", [])
    transaction.record_directory_metadata(
        {
            "." if directory == tmp_path else directory.relative_to(tmp_path).as_posix(): values
            for directory, values in expected.items()
        }
    )
    transaction.snapshot([tmp_path / "local_modules"])
    transaction.commit()

    for directory, values in expected.items():
        current = directory.lstat()
        assert current.st_mode & 0o7777 == values[0]
        assert current.st_atime_ns == values[1]
        assert current.st_mtime_ns == values[2]


@pytest.mark.parametrize("concurrent_change", ["mode_mtime", "replacement"])
def test_directory_observation_never_restores_over_concurrent_metadata_or_entry(
    tmp_path: Path,
    monkeypatch,
    concurrent_change: str,
):
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "source.cpp").write_text("int value = 1;\n")
    original_listdir = filesystem_module.os.listdir
    injected = False

    def racing_listdir(path):
        nonlocal injected
        if isinstance(path, int) and not injected:
            injected = True
            if concurrent_change == "mode_mtime":
                observed.chmod(0o711)
                os.utime(observed, ns=(41_000_000_000, 42_000_000_000))
            else:
                displaced = tmp_path / "observed-before"
                os.replace(observed, displaced)
                observed.mkdir(mode=0o700)
                (observed / "concurrent").write_text("replacement remains\n")
        return original_listdir(path)

    monkeypatch.setattr(filesystem_module.os, "listdir", racing_listdir)
    with pytest.raises(FilesystemError, match="changed while"):
        list(filesystem_module.iter_tree_no_follow(observed))

    if concurrent_change == "mode_mtime":
        current = observed.lstat()
        assert current.st_mode & 0o7777 == 0o711
        assert current.st_mtime_ns == 42_000_000_000
    else:
        assert (observed / "concurrent").read_text() == "replacement remains\n"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_directory_observation_never_enumerates_a_substituted_symlink_target(
    tmp_path: Path,
    monkeypatch,
):
    observed = tmp_path / "observed"
    observed.mkdir()
    (observed / "inside.txt").write_text("inside\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside\n", encoding="utf-8")
    os.utime(external, ns=(51_000_000_000, 52_000_000_000))
    os.utime(sentinel, ns=(53_000_000_000, 54_000_000_000))
    expected_directory = external.lstat()
    expected_sentinel = sentinel.lstat()
    original_listdir = filesystem_module.os.listdir
    injected = False

    def substitute_before_enumeration(path):
        nonlocal injected
        if isinstance(path, int) and not injected:
            injected = True
            os.replace(observed, tmp_path / "observed-before")
            observed.symlink_to(external, target_is_directory=True)
        return original_listdir(path)

    monkeypatch.setattr(
        filesystem_module.os,
        "listdir",
        substitute_before_enumeration,
    )
    with pytest.raises(FilesystemError, match="changed while"):
        list(filesystem_module.iter_tree_no_follow(observed))

    observed_directory = external.lstat()
    observed_sentinel = sentinel.lstat()
    assert (
        observed_directory.st_atime_ns,
        observed_directory.st_mtime_ns,
    ) == (expected_directory.st_atime_ns, expected_directory.st_mtime_ns)
    assert (
        observed_sentinel.st_atime_ns,
        observed_sentinel.st_mtime_ns,
    ) == (expected_sentinel.st_atime_ns, expected_sentinel.st_mtime_ns)
    assert sentinel.read_bytes() == b"outside\n"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX fixture")
def test_source_inventory_never_enumerates_a_replaced_directory_path(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    nested = root / "local_modules/alpha"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside\n", encoding="utf-8")
    os.utime(external, ns=(51_000_000_000, 52_000_000_000))
    os.utime(sentinel, ns=(53_000_000_000, 54_000_000_000))
    expected_directory = external.lstat()
    expected_sentinel = sentinel.lstat()
    original_open = filesystem_module.os.open
    substituted = False

    def substitute_after_open(path, flags, *args, **kwargs):
        nonlocal substituted
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "alpha" and kwargs.get("dir_fd") is not None and not substituted:
            substituted = True
            os.replace(nested, root / "local_modules/alpha-before")
            nested.symlink_to(external, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(filesystem_module.os, "open", substitute_after_open)

    with pytest.raises(ConcurrentSourceMutation, match="changed while"):
        source_tree_inventory(root)

    observed_directory = external.lstat()
    observed_sentinel = sentinel.lstat()
    assert substituted
    assert sentinel.read_bytes() == b"outside\n"
    assert (
        observed_directory.st_atime_ns,
        observed_directory.st_mtime_ns,
    ) == (expected_directory.st_atime_ns, expected_directory.st_mtime_ns)
    assert (
        observed_sentinel.st_atime_ns,
        observed_sentinel.st_mtime_ns,
    ) == (expected_sentinel.st_atime_ns, expected_sentinel.st_mtime_ns)


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link inventory fixture")
def test_source_inventory_reads_symlink_target_from_retained_identity_during_aba(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "plugin"
    root.mkdir()
    owned = root / "source-link"
    owned.symlink_to("owned-target")
    external = tmp_path / "external-link"
    external.symlink_to("external-target")
    os.utime(external, ns=(2_000_000_021_000_000_000, 2_000_000_022_000_000_000), follow_symlinks=False)
    external_before = external.lstat()
    root_before = root.lstat()
    displaced = tmp_path / "owned-displaced"
    original_read = filesystem_module._read_symlink_authority_target
    injected = False

    def swap_out_and_back(authority):
        nonlocal injected
        if not injected:
            injected = True
            os.replace(owned, displaced)
            os.replace(external, owned)
            try:
                target = original_read(authority)
            finally:
                os.replace(owned, external)
                os.replace(displaced, owned)
                os.utime(
                    root,
                    ns=(root_before.st_atime_ns, root_before.st_mtime_ns),
                )
            return target
        return original_read(authority)

    monkeypatch.setattr(
        filesystem_module,
        "_read_symlink_authority_target",
        swap_out_and_back,
    )

    inventory = source_tree_inventory(root)
    external_after = external.lstat()

    assert injected
    assert inventory["source-link"][3] == "owned-target"
    assert os.readlink(owned) == "owned-target"
    assert os.readlink(external) == "external-target"
    assert (
        stat.S_IMODE(external_after.st_mode),
        external_after.st_atime_ns,
        external_after.st_mtime_ns,
    ) == (
        stat.S_IMODE(external_before.st_mode),
        external_before.st_atime_ns,
        external_before.st_mtime_ns,
    )


def test_failed_file_copy_preserves_a_concurrent_destination_replacement(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    replacement = tmp_path / "editor-save.txt"
    source.write_bytes(b"generator source\n")
    replacement.write_bytes(b"concurrent editor save\n")
    replacement.chmod(0o604)
    replacement_times = (2_000_000_001_000_000_000, 2_000_000_002_000_000_000)
    os.utime(replacement, ns=replacement_times)
    original_finish = filesystem_module._finish_observed_atime
    injected = False

    def replace_before_failed_copy_cleanup(path, descriptor, before):
        nonlocal injected
        if path == source and not injected:
            injected = True
            os.replace(replacement, destination)
            return False
        return original_finish(path, descriptor, before)

    monkeypatch.setattr(
        filesystem_module,
        "_finish_observed_atime",
        replace_before_failed_copy_cleanup,
    )

    with pytest.raises(ConcurrentSourceMutation, match="changed while it was copied"):
        copy_entry_no_follow(source, destination)

    live = destination.lstat()
    assert injected
    assert destination.read_bytes() == b"concurrent editor save\n"
    assert stat.S_IMODE(live.st_mode) == 0o604
    assert (live.st_atime_ns, live.st_mtime_ns) == replacement_times


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link regression")
def test_recovery_symlink_copy_preserves_a_concurrent_destination_replacement(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source-link"
    destination = tmp_path / "recovery-link"
    detached = tmp_path / "detached-generated-link"
    source.symlink_to("generator-target")
    replacement_times = (2_000_000_011_000_000_000, 2_000_000_012_000_000_000)
    original_apply = filesystem_module._apply_symlink_authority_metadata
    injected = False
    expected_metadata = None

    def replace_before_retained_metadata_apply(authority, metadata, *, atime_only=False):
        nonlocal injected, expected_metadata
        # Coverage instrumentation can make the source-link observation itself
        # require atime neutralization.  Inject only after the copy has created
        # the destination whose retained-authority boundary this test targets.
        if not injected and filesystem_module.lexists(destination):
            injected = True
            os.replace(destination, detached)
            destination.symlink_to("external-editor-target")
            os.utime(destination, ns=replacement_times, follow_symlinks=False)
            expected_metadata = destination.lstat()
        return original_apply(authority, metadata, atime_only=atime_only)

    monkeypatch.setattr(
        filesystem_module,
        "_apply_symlink_authority_metadata",
        replace_before_retained_metadata_apply,
    )

    with pytest.raises(ConcurrentSourceMutation, match="Destination symbolic link"):
        transaction_module._copy_verified_entry(source, destination, attempts=1)

    assert injected
    assert expected_metadata is not None
    live = destination.lstat()
    assert stat.S_ISLNK(live.st_mode)
    assert os.readlink(destination) == "external-editor-target"
    assert stat.S_IMODE(live.st_mode) == stat.S_IMODE(expected_metadata.st_mode)
    assert (live.st_atime_ns, live.st_mtime_ns) == replacement_times


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link descriptor regression")
def test_symlink_atime_restore_never_writes_a_concurrent_replacement(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source-link"
    source.symlink_to("first-target")
    os.utime(
        source,
        ns=(1_000_000_000, 2_000_000_000),
        follow_symlinks=False,
    )
    original_readlink = os.readlink
    original_authority_read = filesystem_module._read_symlink_authority_target
    original_apply = filesystem_module._apply_symlink_authority_metadata
    replaced = False

    def atime_changing_readlink(authority):
        target = original_authority_read(authority)
        os.utime(
            source,
            ns=(3_000_000_000, 2_000_000_000),
            follow_symlinks=False,
        )
        return target

    def replace_before_restore(authority, metadata, *, atime_only=False):
        nonlocal replaced
        if not replaced:
            replaced = True
            displaced = tmp_path / "source-link-before"
            os.replace(source, displaced)
            source.symlink_to("external-target")
            os.utime(
                source,
                ns=(11_000_000_000, 12_000_000_000),
                follow_symlinks=False,
            )
        original_apply(authority, metadata, atime_only=atime_only)

    monkeypatch.setattr(
        filesystem_module,
        "_read_symlink_authority_target",
        atime_changing_readlink,
    )
    monkeypatch.setattr(
        filesystem_module,
        "_apply_symlink_authority_metadata",
        replace_before_restore,
    )

    with pytest.raises(ConcurrentSourceMutation, match="changed while"):
        filesystem_module.hash_entry_no_follow(source)

    current = source.lstat()
    assert current.st_atime_ns == 11_000_000_000
    assert current.st_mtime_ns == 12_000_000_000
    assert original_readlink(source) == "external-target"


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link descriptor regression")
def test_symlink_target_read_is_bound_across_aba_path_substitution(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source-link"
    source.symlink_to("owned-target")
    baseline = hash_entry_no_follow(source)
    external = tmp_path / "external-link"
    external.symlink_to("external-target")
    # ``readlink`` itself is allowed to advance a symlink atime on Linux.
    # Establish the baseline after asserting the external target so the ABA
    # check observes only the retained-descriptor read under test.
    assert os.readlink(external) == "external-target"
    external_before = external.lstat()
    original_read = filesystem_module._read_symlink_authority_target
    injected = False

    def aba_read(authority):
        nonlocal injected
        displaced = tmp_path / "owned-displaced"
        external_displaced = tmp_path / "external-displaced"
        os.replace(source, displaced)
        os.replace(external, source)
        try:
            injected = True
            return original_read(authority)
        finally:
            os.replace(source, external_displaced)
            os.replace(displaced, source)
            os.replace(external_displaced, external)

    monkeypatch.setattr(
        filesystem_module,
        "_read_symlink_authority_target",
        aba_read,
    )

    assert hash_entry_no_follow(source) == baseline
    assert injected
    external_after = external.lstat()
    assert (
        external_after.st_mode,
        external_after.st_atime_ns,
        external_after.st_mtime_ns,
    ) == (
        external_before.st_mode,
        external_before.st_atime_ns,
        external_before.st_mtime_ns,
    )
    assert os.readlink(source) == "owned-target"
    assert os.readlink(external) == "external-target"


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link descriptor regression")
def test_retained_symlink_target_reader_grows_and_rejects_unknown_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "long-link"
    target = "segment/" * 50
    source.symlink_to(target)

    assert filesystem_module._read_symlink_identity_bound(
        source,
        operation="read",
    )[0] == target
    with pytest.raises(OSError, match="Unknown symlink metadata authority"):
        filesystem_module._read_symlink_authority_target(("unknown", 0))


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-link descriptor regression")
def test_symlink_atime_restore_preserves_concurrent_same_inode_mtime(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source-link"
    source.symlink_to("target")
    os.utime(source, ns=(1_000_000_000, 2_000_000_000), follow_symlinks=False)
    original_read = filesystem_module._read_symlink_authority_target
    original_apply = filesystem_module._apply_symlink_authority_metadata
    external_mtime = 12_000_000_000

    def advance_atime(authority):
        target = original_read(authority)
        os.utime(source, ns=(3_000_000_000, 2_000_000_000), follow_symlinks=False)
        return target

    def change_mtime_then_restore(authority, metadata, *, atime_only=False):
        current = source.lstat()
        os.utime(
            source,
            ns=(current.st_atime_ns, external_mtime),
            follow_symlinks=False,
        )
        original_apply(authority, metadata, atime_only=atime_only)

    monkeypatch.setattr(
        filesystem_module,
        "_read_symlink_authority_target",
        advance_atime,
    )
    monkeypatch.setattr(
        filesystem_module,
        "_apply_symlink_authority_metadata",
        change_mtime_then_restore,
    )

    with pytest.raises(ConcurrentSourceMutation, match="changed while"):
        hash_entry_no_follow(source)

    assert source.lstat().st_mtime_ns == external_mtime


@pytest.mark.skipif(os.name == "nt", reason="POSIX retained-atime regression")
def test_observation_atime_restore_preserves_concurrent_mtime_update(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.cpp"
    source.write_text("int value = 1;\n", encoding="utf-8")
    created = source.lstat()
    os.utime(source, ns=(1_000_000_000, created.st_mtime_ns))
    original_apply = filesystem_module._apply_descriptor_atime_only
    external_mtime = source.lstat().st_mtime_ns + 9_000_000_000
    injected = False

    def change_mtime_then_restore(descriptor, atime_ns):
        nonlocal injected
        injected = True
        current = source.lstat()
        os.utime(source, ns=(current.st_atime_ns, external_mtime))
        original_apply(descriptor, atime_ns)

    monkeypatch.setattr(
        filesystem_module,
        "_apply_descriptor_atime_only",
        change_mtime_then_restore,
    )

    with pytest.raises(FilesystemError, match="changed while"):
        filesystem_module.read_regular_bytes_no_follow(source)

    assert injected
    assert source.lstat().st_mtime_ns == external_mtime


def test_regular_copy_metadata_uses_retained_destination_identity(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.txt"
    source.write_text("generated\n", encoding="utf-8")
    destination = tmp_path / "destination.txt"
    detached = tmp_path / "detached-generated.txt"
    original_apply = filesystem_module._apply_descriptor_metadata
    external_times = (31_000_000_000, 32_000_000_000)
    injected = False

    def substitute_then_apply(descriptor, metadata):
        nonlocal injected
        injected = True
        os.replace(destination, detached)
        destination.write_text("external\n", encoding="utf-8")
        destination.chmod(0o640)
        os.utime(destination, ns=external_times)
        original_apply(descriptor, metadata)

    monkeypatch.setattr(
        filesystem_module,
        "_apply_descriptor_metadata",
        substitute_then_apply,
    )

    with pytest.raises(ConcurrentSourceMutation, match="Destination file changed"):
        copy_entry_no_follow(source, destination)

    external = destination.lstat()
    assert injected
    assert destination.read_bytes() == b"external\n"
    assert stat.S_IMODE(external.st_mode) == 0o640
    assert (external.st_atime_ns, external.st_mtime_ns) == external_times
    assert detached.read_bytes() == b"generated\n"


@pytest.mark.parametrize("reader", ["hash", "package"])
@pytest.mark.parametrize("concurrent_change", ["mode_mtime", "replacement"])
def test_file_observation_never_restores_over_concurrent_metadata_or_entry(
    tmp_path: Path,
    monkeypatch,
    reader: str,
    concurrent_change: str,
):
    source = tmp_path / ("package.json" if reader == "package" else "source.cpp")
    source.write_text(
        '{"name":"fixture","dependencies":{}}\n'
        if reader == "package"
        else "int value = 1;\n",
        encoding="utf-8",
    )
    original_read = filesystem_module.os.read
    injected = False

    def racing_read(descriptor, size):
        nonlocal injected
        content = original_read(descriptor, size)
        if content and not injected:
            injected = True
            if concurrent_change == "mode_mtime":
                source.chmod(0o611)
                os.utime(source, ns=(51_000_000_000, 52_000_000_000))
            else:
                replacement = source.with_name(f"{source.name}.replacement")
                replacement.write_text("replacement remains\n", encoding="utf-8")
                os.replace(replacement, source)
        return content

    monkeypatch.setattr(filesystem_module.os, "read", racing_read)
    expected_exception = ConfigurationError if reader == "package" else FilesystemError
    with pytest.raises(expected_exception, match="changed while|could not be read"):
        if reader == "package":
            read_parent_package(tmp_path)
        else:
            filesystem_module.hash_entry_no_follow(source)

    if concurrent_change == "mode_mtime":
        current = source.lstat()
        assert current.st_mode & 0o7777 == 0o611
        assert current.st_mtime_ns == 52_000_000_000
    else:
        assert source.read_text() == "replacement remains\n"


@pytest.mark.parametrize("phase", ["stage", "commit", "abandon"])
@pytest.mark.parametrize("authority_kind", ["missing", "directory", "symlink"])
def test_every_recovery_phase_requires_regular_state_authority_before_mutation(
    tmp_path: Path,
    phase: str,
    authority_kind: str,
):
    if authority_kind == "symlink" and os.name == "nt":
        pytest.skip("Windows symlink capability is environment-specific")
    sentinel = tmp_path / "local_modules/existing/source.cpp"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("baseline\n", encoding="utf-8")
    staged = tmp_path / ".generator-stage"
    staged.write_text("internal\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["existing"])
    transaction.snapshot([sentinel])
    transaction.track_created(staged)
    sentinel.write_text("live mutation\n", encoding="utf-8")
    transaction.data["phase"] = phase
    transaction._persist()
    journal_before = (tmp_path / JOURNAL_NAME).read_bytes()
    authority = transaction.state_dir / str(transaction.data["entry_authority"])
    authority_bytes = authority.read_bytes()
    authority.unlink()
    if authority_kind == "directory":
        authority.mkdir()
    elif authority_kind == "symlink":
        external = tmp_path.parent / f"{tmp_path.name}-external-authority.json"
        external.write_bytes(authority_bytes)
        os.symlink(external, authority)

    with pytest.raises(Exception, match="authority|recovery"):
        recover_pending(tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "live mutation\n"
    assert staged.read_text(encoding="utf-8") == "internal\n"
    assert (tmp_path / JOURNAL_NAME).read_bytes() == journal_before
    assert transaction.state_dir.is_dir()
    assert os.path.lexists(authority) is (authority_kind != "missing")


@pytest.mark.parametrize("payload_kind", ["missing", "directory", "symlink", "corrupt"])
def test_complete_restore_set_is_validated_before_any_live_rollback_mutation(
    tmp_path: Path,
    payload_kind: str,
):
    if payload_kind == "symlink" and os.name == "nt":
        pytest.skip("Windows symlink capability is environment-specific")
    first = tmp_path / "local_modules/alpha/first.cpp"
    second = tmp_path / "local_modules/beta/second.cpp"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("first baseline\n", encoding="utf-8")
    second.write_text("second baseline\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["alpha", "beta"])
    transaction.snapshot([first, second])
    first.write_text("first live\n", encoding="utf-8")
    second.write_text("second live\n", encoding="utf-8")
    transaction.mark_write()
    journal_before = (tmp_path / JOURNAL_NAME).read_bytes()
    authority = transaction.state_dir / str(transaction.data["entry_authority"])
    authority_before = authority.read_bytes()
    payload = Path(str(transaction.data["entries"][0]["restore"]))
    if payload_kind == "missing":
        payload.unlink()
    elif payload_kind == "directory":
        payload.unlink()
        payload.mkdir()
    elif payload_kind == "symlink":
        external = tmp_path.parent / f"{tmp_path.name}-payload.cpp"
        external.write_text("first baseline\n", encoding="utf-8")
        payload.unlink()
        os.symlink(external, payload)
    else:
        payload.write_text("corrupt baseline\n", encoding="utf-8")

    with pytest.raises(Exception, match="Restore|recovery|payload"):
        recover_pending(tmp_path)

    assert first.read_text(encoding="utf-8") == "first live\n"
    assert second.read_text(encoding="utf-8") == "second live\n"
    assert (tmp_path / JOURNAL_NAME).read_bytes() == journal_before
    assert authority.read_bytes() == authority_before


@pytest.mark.parametrize("phase", ["commit", "abandon"])
@pytest.mark.parametrize(
    "interrupt_point",
    ["after_abandon_journal_unlink", "after_abandon_state_removal"],
)
def test_fresh_process_discovers_durable_cleanup_after_internal_authority_removal(
    tmp_path: Path,
    phase: str,
    interrupt_point: str,
):
    baseline_metadata = protected_directory_metadata(tmp_path)
    fired = False

    def interrupt(name: str) -> None:
        nonlocal fired
        if name == interrupt_point and not fired:
            fired = True
            raise KeyboardInterrupt

    transaction = Transaction(
        tmp_path,
        "update",
        ["alpha"],
        fault_injector=interrupt,
    )
    transaction.record_directory_metadata(baseline_metadata)
    if phase == "commit":
        with pytest.raises(KeyboardInterrupt):
            transaction.commit()
    else:
        staged = tmp_path / ".planned-stage"
        staged.write_text("temporary\n", encoding="utf-8")
        transaction.track_created(staged)
        with pytest.raises(KeyboardInterrupt):
            transaction.abandon_unmutated()
        assert not staged.exists()
    assert fired is True

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status in {"not_needed", "completed"}
    # Observe exact metadata before globbing the root: directory enumeration is
    # itself allowed to advance atime on Linux filesystems using relatime.
    assert _directory_metadata_matches(tmp_path, baseline_metadata)
    assert not (tmp_path / JOURNAL_NAME).exists()
    assert not list(tmp_path.glob(".supernote-module-transaction-*"))


def test_rollback_metadata_failure_is_discoverable_and_retryable_in_a_new_process(
    tmp_path: Path,
    monkeypatch,
):
    sentinel = tmp_path / "source.cpp"
    sentinel.write_text("before\n", encoding="utf-8")
    baseline_metadata = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.record_directory_metadata(baseline_metadata)
    transaction.snapshot([sentinel])
    sentinel.write_text("after\n", encoding="utf-8")
    transaction.mark_write()
    original_restore = restore_protected_directory_metadata
    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        lambda *_args, **_kwargs: ("modified:.",),
    )

    first = transaction.rollback()

    assert first.status == "partial"
    assert sentinel.read_text(encoding="utf-8") == "before\n"
    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        original_restore,
    )
    second = recover_pending(tmp_path)
    assert second.rollback.status == "completed"
    assert _directory_metadata_matches(tmp_path, baseline_metadata)


def _entry_stat(path: Path) -> tuple[int, int, int]:
    value = path.lstat()
    return value.st_mode & 0o7777, value.st_atime_ns, value.st_mtime_ns


def _directory_metadata_matches(root: Path, expected) -> bool:
    for relative, metadata in expected.items():
        path = root if relative == "." else root.joinpath(*relative.split("/"))
        if _entry_stat(path) != metadata:
            return False
    return True


def test_file_and_tree_snapshots_preserve_source_and_destination_metadata(tmp_path: Path):
    source_file = tmp_path / "source.txt"
    source_file.write_text("content\n", encoding="utf-8")
    source_file.chmod(0o600)
    os.utime(source_file, ns=(3_000_000_000, 4_000_000_000))
    file_before = _entry_stat(source_file)
    copied_file = tmp_path / "copied.txt"

    copy_entry_no_follow(source_file, copied_file)

    assert _entry_stat(source_file) == file_before
    assert _entry_stat(copied_file) == file_before

    source_tree = tmp_path / "tree"
    child_directory = source_tree / "nested"
    child_directory.mkdir(parents=True)
    child_file = child_directory / "value.txt"
    child_file.write_text("tree content\n", encoding="utf-8")
    child_file.chmod(0o640)
    os.utime(child_file, ns=(5_000_000_000, 6_000_000_000))
    os.utime(child_directory, ns=(7_000_000_000, 8_000_000_000))
    os.utime(source_tree, ns=(9_000_000_000, 10_000_000_000))
    tree_before = {
        path.relative_to(source_tree).as_posix(): _entry_stat(path)
        for path in (source_tree, child_directory, child_file)
    }
    copied_tree = tmp_path / "tree-copy"

    copy_entry_no_follow(source_tree, copied_tree)

    assert {
        path.relative_to(source_tree).as_posix(): _entry_stat(path)
        for path in (source_tree, child_directory, child_file)
    } == tree_before
    assert {
        path.relative_to(copied_tree).as_posix(): _entry_stat(path)
        for path in (copied_tree, copied_tree / "nested", copied_tree / "nested/value.txt")
    } == tree_before


def test_successful_transaction_and_read_only_guard_do_not_advance_file_atime(
    tmp_path: Path,
):
    source = tmp_path / "source.cpp"
    source.write_text("int value = 1;\n", encoding="utf-8")
    os.utime(source, ns=(11_000_000_000, 12_000_000_000))
    before = _entry_stat(source)
    transaction = Transaction(tmp_path, "update", ["alpha"])
    transaction.snapshot([source])
    transaction.commit()
    assert _entry_stat(source) == before

    guard = ProtectedSourceGuard(tmp_path)
    assert guard.finish() == ()
    assert _entry_stat(source) == before


@pytest.mark.parametrize("source_kind", ["file", "tree"])
@pytest.mark.parametrize("race_point", ["during_copy", "after_copy"])
def test_snapshot_versions_a_stable_recovery_payload_across_copy_races(
    tmp_path: Path,
    monkeypatch,
    source_kind: str,
    race_point: str,
):
    source = tmp_path / ("source.cpp" if source_kind == "file" else "source-tree")
    leaf = source
    if source_kind == "tree":
        leaf = source / "nested/source.cpp"
        leaf.parent.mkdir(parents=True)
    leaf.write_text("before\n", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["alpha"])
    injected = False

    def concurrent_change() -> None:
        nonlocal injected
        if injected:
            return
        injected = True
        leaf.write_text("concurrent\n", encoding="utf-8")
        leaf.chmod(0o600)
        os.utime(leaf, ns=(61_000_000_000, 62_000_000_000))
        if source_kind == "tree":
            source.chmod(0o710)
            os.utime(source, ns=(63_000_000_000, 64_000_000_000))

    if race_point == "during_copy":
        original_read = filesystem_module.os.read
        source_inode = leaf.lstat().st_ino

        def racing_read(descriptor, size):
            content = original_read(descriptor, size)
            if content and os.fstat(descriptor).st_ino == source_inode:
                concurrent_change()
            return content

        monkeypatch.setattr(filesystem_module.os, "read", racing_read)
    else:
        original_copy = transaction_module._copy_verified_entry

        def racing_copy(source_path, destination, *, attempts=3):
            result = original_copy(source_path, destination, attempts=attempts)
            if Path(source_path) == source:
                concurrent_change()
            return result

        monkeypatch.setattr(transaction_module, "_copy_verified_entry", racing_copy)

    transaction.snapshot([source])
    expected = transaction_module._exact_entry_state(source)
    leaf.write_text("generator\n", encoding="utf-8")
    transaction.mark_write()

    rollback = recover_pending(tmp_path).rollback

    assert rollback.status == "completed"
    assert transaction_module._exact_entry_state(source) == expected
    assert leaf.read_text(encoding="utf-8") == "concurrent\n"
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


def test_conflict_adoption_never_replays_stale_live_metadata_after_copy(
    tmp_path: Path,
    monkeypatch,
):
    source_root = tmp_path / "local_modules/existing"
    source = source_root / "source.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    baseline = source_tree_inventory(tmp_path)
    directory_baseline = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "add", ["safe"])
    transaction.record_directory_metadata(directory_baseline)
    transaction.snapshot([source_root])
    source.write_text("external\n", encoding="utf-8")
    source.chmod(0o640)
    os.utime(source, ns=(71_000_000_000, 72_000_000_000))
    injected = False
    original_copy = transaction_module._copy_verified_entry
    expected_source_times = (73_000_000_000, 74_000_000_000)
    expected_parent_times = (75_000_000_000, 76_000_000_000)

    def racing_copy(source_path, destination, *, attempts=3):
        nonlocal injected
        result = original_copy(source_path, destination, attempts=attempts)
        if Path(source_path) == source and not injected:
            injected = True
            source.chmod(0o600)
            os.utime(source, ns=expected_source_times)
            source_root.chmod(0o710)
            os.utime(source_root, ns=expected_parent_times)
        return result

    monkeypatch.setattr(transaction_module, "_copy_verified_entry", racing_copy)

    transaction.preserve_external_source_changes(baseline)
    source.write_text("generator\n", encoding="utf-8")
    transaction.mark_write()
    rollback = recover_pending(tmp_path).rollback

    assert rollback.status == "completed"
    assert _entry_stat(source) == (0o600, *expected_source_times)
    assert _entry_stat(source_root) == (0o710, *expected_parent_times)
    assert source.read_text(encoding="utf-8") == "external\n"
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


def test_exact_entry_state_distinguishes_different_supported_atimes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.cpp"
    source.write_text("stable\n", encoding="utf-8")
    first_atime = 61_000_000_000
    second_atime = 63_000_000_000
    mtime = 62_000_000_000
    os.utime(source, ns=(first_atime, mtime))

    first = transaction_module._exact_entry_state(source)
    os.utime(source, ns=(second_atime, mtime))
    second = transaction_module._exact_entry_state(source)

    precision = 100 if os.name == "nt" else 1
    assert first[0][3] == first_atime // precision * precision
    assert second[0][3] == second_atime // precision * precision
    assert first != second


def test_exact_entry_state_restores_nested_directory_atimes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "value.txt").write_text("stable\n", encoding="utf-8")
    root_times = (65_000_000_000, 66_000_000_000)
    nested_times = (67_000_000_000, 68_000_000_000)
    os.utime(source, ns=root_times)
    os.utime(nested, ns=nested_times)

    state = transaction_module._exact_entry_state(source)

    precision = 100 if os.name == "nt" else 1
    rows = {row[0]: row for row in state}
    assert rows["."][3] == root_times[0] // precision * precision
    assert rows["nested"][3] == nested_times[0] // precision * precision
    assert source.lstat().st_atime_ns == root_times[0]
    assert nested.lstat().st_atime_ns == nested_times[0]


def test_matching_content_addressed_entry_authority_is_not_rewritten(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transaction = Transaction(tmp_path, "update", ["safe"])
    authority_name = str(transaction.data["entry_authority"])

    monkeypatch.setattr(
        transaction_module,
        "_write_json_atomic",
        lambda *_args, **_kwargs: pytest.fail("matching authority was rewritten"),
    )

    transaction_module._write_entry_authority(
        transaction.state_dir,
        transaction.identifier,
        transaction.data["entries"],
        authority_name,
    )


def test_windows_atomic_json_keeps_source_authority_through_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "journal.json"
    if os.name == "nt":
        transaction_module._write_json_atomic(destination, {"value": 1})
        transaction_module._write_json_atomic(destination, {"value": 2})
        assert json.loads(destination.read_text(encoding="utf-8")) == {"value": 2}
        return
    created: list[Path] = []
    replaced: list[tuple[Path, Path, int]] = []
    closed_handles: list[int] = []

    def create(temporary: Path) -> int:
        created.append(temporary)
        return os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)

    def replace(descriptor: int, path: Path, *, root_directory: int) -> None:
        os.fstat(descriptor)
        replaced.append((created[0], path, root_directory))
        os.replace(created[0], path)

    monkeypatch.setattr(transaction_module, "_windows_host", lambda: True)
    monkeypatch.setattr(
        transaction_module,
        "_windows_open_conditional_parent_handle",
        lambda _path: 91,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_create_atomic_regular_descriptor",
        create,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_descriptor_path_matches",
        lambda _descriptor, _path: True,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_rename_descriptor_replace",
        replace,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_close_handle",
        closed_handles.append,
    )

    transaction_module._write_json_atomic(destination, {"value": 1})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"value": 1}
    assert replaced == [(created[0], destination, 91)]
    assert closed_handles == [91]


def test_content_addressed_entry_authority_rejects_conflicting_payload(
    tmp_path: Path,
) -> None:
    transaction = Transaction(tmp_path, "update", ["safe"])
    authority_name = str(transaction.data["entry_authority"])
    authority = transaction.state_dir / authority_name
    authority.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FilesystemError, match="authority is inconsistent"):
        transaction_module._write_entry_authority(
            transaction.state_dir,
            transaction.identifier,
            transaction.data["entries"],
            authority_name,
        )


def test_recover_pending_reconciles_windows_authority_before_root_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []

    class ObservedPath(type(tmp_path)):
        def resolve(self, *args, **kwargs):
            if "resolved" not in events:
                assert events == ["reconciled"]
                events.append("resolved")
            return super().resolve(*args, **kwargs)

    monkeypatch.setattr(
        transaction_module.filesystem_ops,
        "reconcile_retained_windows_authority",
        lambda: events.append("reconciled"),
    )

    outcome = recover_pending(ObservedPath(tmp_path))

    assert outcome.rollback.status == "not_needed"
    assert events[:2] == ["reconciled", "resolved"]


def test_tree_conflict_adoption_versions_post_copy_changes_before_recovery(
    tmp_path: Path,
    monkeypatch,
):
    source_root = tmp_path / "local_modules/existing"
    source_root.mkdir(parents=True)
    (source_root / "baseline.cpp").write_text("before\n", encoding="utf-8")
    baseline = source_tree_inventory(tmp_path)
    directory_baseline = protected_directory_metadata(tmp_path)
    transaction = Transaction(tmp_path, "add", ["safe"])
    transaction.record_directory_metadata(directory_baseline)
    transaction.snapshot([source_root])
    external_tree = source_root / "concurrent/nested"
    external_tree.mkdir(parents=True)
    external_file = external_tree / "value.txt"
    external_file.write_text("external first\n", encoding="utf-8")
    original_copy = transaction_module._copy_verified_entry
    injected = False
    expected_tree_times = (85_000_000_000, 86_000_000_000)

    def racing_copy(source_path, destination, *, attempts=3):
        nonlocal injected
        result = original_copy(source_path, destination, attempts=attempts)
        if Path(source_path) == source_root / "concurrent" and not injected:
            injected = True
            external_file.write_text("external final\n", encoding="utf-8")
            external_tree.chmod(0o710)
            os.utime(external_tree, ns=expected_tree_times)
        return result

    monkeypatch.setattr(transaction_module, "_copy_verified_entry", racing_copy)

    transaction.preserve_external_source_changes(baseline)
    remove_entry_no_follow = filesystem_module.remove_entry_no_follow
    remove_entry_no_follow(source_root / "concurrent")
    transaction.mark_write()
    rollback = recover_pending(tmp_path).rollback
    tree_metadata = external_tree.lstat()

    assert rollback.status == "completed"
    assert external_file.read_text(encoding="utf-8") == "external final\n"
    assert tree_metadata.st_mode & 0o7777 == 0o710
    assert (tree_metadata.st_atime_ns, tree_metadata.st_mtime_ns) == expected_tree_times
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


def test_failed_adoption_keeps_the_previous_recovery_payload_actionable(
    tmp_path: Path,
    monkeypatch,
):
    source_root = tmp_path / "local_modules/existing"
    source = source_root / "source.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("baseline\n", encoding="utf-8")
    baseline = source_tree_inventory(tmp_path)
    transaction = Transaction(tmp_path, "add", ["safe"])
    transaction.snapshot([source_root])
    entry = transaction.data["entries"][0]
    assert isinstance(entry, dict)
    original_restore = Path(str(entry["restore"]))
    original_payload = transaction_module._exact_entry_state(original_restore)
    source.write_text("external racing state\n", encoding="utf-8")
    attempted = tmp_path / "local_modules/safe"
    attempted.mkdir()
    (attempted / "generated.txt").write_text("remove me\n", encoding="utf-8")
    transaction.track_created(attempted)
    original_copy = transaction_module._copy_verified_entry

    def failing_external_copy(source_path, destination, *, attempts=3):
        if Path(source_path) == source:
            raise FilesystemError("persistent external copy race")
        return original_copy(source_path, destination, attempts=attempts)

    monkeypatch.setattr(
        transaction_module, "_copy_verified_entry", failing_external_copy
    )

    with pytest.raises(FilesystemError, match="persistent external copy race"):
        transaction.preserve_external_source_changes(baseline)

    current_entry = transaction.data["entries"][0]
    assert isinstance(current_entry, dict)
    assert Path(str(current_entry["restore"])) == original_restore
    assert transaction_module._exact_entry_state(original_restore) == original_payload
    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "completed"
    assert source.read_text(encoding="utf-8") == "baseline\n"
    assert not attempted.exists()
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


@pytest.mark.parametrize(
    "interrupt_point",
    [
        "after_package_baseline_payload_creation",
        "after_package_baseline_entry_update",
        "after_package_baseline_state_authority_write",
        "after_package_baseline_journal_write",
    ],
)
def test_package_baseline_switch_is_recoverable_at_every_durable_boundary(
    tmp_path: Path,
    interrupt_point: str,
):
    package = tmp_path / "package.json"
    package.write_bytes(b'{"generator":"visible"}\n')
    fired = False

    def interrupt(name: str) -> None:
        nonlocal fired
        if name == interrupt_point and not fired:
            fired = True
            raise KeyboardInterrupt

    transaction = Transaction(
        tmp_path,
        "add",
        ["safe"],
        fault_injector=interrupt,
    )
    transaction.snapshot([package])
    external = b'{\n\t"external": true\n}\n'
    external_times = (91_000_000_000, 92_000_000_000)

    with pytest.raises(KeyboardInterrupt):
        transaction.replace_snapshot_file_baseline(
            package,
            external,
            mode=0o600,
            atime_ns=external_times[0],
            mtime_ns=external_times[1],
        )

    assert fired is True
    outcome = recover_pending(tmp_path)
    metadata = package.lstat()
    assert outcome.rollback.status == "completed"
    if os.name == "nt":
        assert metadata.st_mode & stat.S_IWRITE == 0o600 & stat.S_IWRITE
    else:
        assert metadata.st_mode & 0o7777 == 0o600
    assert (metadata.st_atime_ns, metadata.st_mtime_ns) == external_times
    assert package.read_bytes() == external
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt, SystemExit, FilesystemError],
)
def test_recovery_copy_retries_only_explicit_concurrent_source_mutations(
    tmp_path: Path,
    monkeypatch,
    failure,
):
    source = tmp_path / "source.cpp"
    source.write_text("source remains\n", encoding="utf-8")
    destination = tmp_path / "prospective-baseline"
    attempts = 0

    def fail_copy(_source, _destination):
        nonlocal attempts
        attempts += 1
        raise failure("stop immediately")

    monkeypatch.setattr(transaction_module, "copy_entry_no_follow", fail_copy)

    with pytest.raises(failure, match="stop immediately"):
        transaction_module._copy_verified_entry(source, destination)

    assert attempts == 1
    assert source.read_text(encoding="utf-8") == "source remains\n"
    assert not destination.exists()


def _install_fake_windows_conditional_io(monkeypatch):
    native_open_parent = transaction_module._windows_open_conditional_parent_handle
    native_create_atomic = transaction_module._windows_create_atomic_regular_descriptor
    native_replace_descriptor = transaction_module._windows_rename_descriptor_replace
    native_descriptor_matches = transaction_module._windows_descriptor_path_matches
    native_close_descriptor = transaction_module._close_descriptor
    native_close_handle = transaction_module._windows_close_handle
    paths: dict[int, Path] = {}
    parent_paths: dict[int, Path] = {}
    atomic_descriptors: set[int] = set()
    closed: list[int] = []
    next_descriptor = 70
    next_handle = 700

    def open_descriptor(path: Path) -> int:
        nonlocal next_descriptor
        descriptor = next_descriptor
        next_descriptor += 1
        paths[descriptor] = Path(path)
        return descriptor

    def read_descriptor(descriptor: int):
        path = paths[descriptor]
        return path.read_bytes(), path.stat()

    def open_parent(path: Path) -> int:
        nonlocal next_handle
        if os.name == "nt":
            handle = native_open_parent(path)
        else:
            handle = next_handle
            next_handle += 1
        parent_paths[handle] = Path(path)
        return handle

    def rename_descriptor(
        descriptor: int,
        destination: Path,
        *,
        root_directory: int | None = None,
    ) -> None:
        assert root_directory is not None
        assert parent_paths[root_directory] == destination.parent
        source = paths[descriptor]
        if destination.exists():
            raise FileExistsError(destination)
        source.rename(destination)
        paths[descriptor] = destination

    def create_atomic(path: Path) -> int:
        descriptor = (
            native_create_atomic(path)
            if os.name == "nt"
            else os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        )
        paths[descriptor] = path
        atomic_descriptors.add(descriptor)
        return descriptor

    def replace_descriptor(
        descriptor: int,
        destination: Path,
        *,
        root_directory: int,
    ) -> None:
        assert parent_paths[root_directory] == destination.parent
        if os.name == "nt":
            native_replace_descriptor(
                descriptor,
                destination,
                root_directory=root_directory,
            )
        else:
            os.replace(paths[descriptor], destination)
        paths[descriptor] = destination

    def close_descriptor(descriptor: int) -> None:
        if descriptor in atomic_descriptors:
            if os.name == "nt":
                native_close_descriptor(descriptor)
            else:
                os.close(descriptor)
            atomic_descriptors.remove(descriptor)
            return
        closed.append(descriptor)

    def descriptor_matches(descriptor: int, path: Path) -> bool:
        if descriptor in atomic_descriptors and os.name == "nt":
            return native_descriptor_matches(descriptor, path)
        return paths[descriptor] == path

    def close_handle(handle: int) -> None:
        if os.name == "nt":
            native_close_handle(handle)

    monkeypatch.setattr(transaction_module, "_windows_host", lambda: True)
    monkeypatch.setattr(
        transaction_module,
        "_hash_path",
        lambda path: transaction_module._regular_entry_hash(
            Path(path).read_bytes(),
            transaction_module.Transaction._windows_portable_mode(
                Path(path).stat().st_mode
            ),
        ),
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_open_conditional_regular_descriptor",
        open_descriptor,
    )
    monkeypatch.setattr(
        transaction_module,
        "_read_windows_conditional_regular_descriptor",
        read_descriptor,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_open_conditional_parent_handle",
        open_parent,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_rename_descriptor_no_replace",
        rename_descriptor,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_create_atomic_regular_descriptor",
        create_atomic,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_rename_descriptor_replace",
        replace_descriptor,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_descriptor_path_matches",
        descriptor_matches,
    )
    monkeypatch.setattr(
        transaction_module,
        "_conditional_destination_parents_match",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        transaction_module,
        "_close_descriptor",
        close_descriptor,
    )
    monkeypatch.setattr(transaction_module, "_windows_close_handle", close_handle)
    return paths, closed, rename_descriptor


def test_windows_conditional_batch_releases_identity_after_durable_finalization(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "generated.js"
    destination.write_bytes(b"baseline\n")
    transaction = Transaction(tmp_path, "update", ["safe"])
    staged = transaction.state_dir / "template" / "0"
    staged.parent.mkdir()
    staged.write_bytes(b"replacement\n")
    _paths, closed, _rename = _install_fake_windows_conditional_io(monkeypatch)

    transaction.replace_regular_batch_if_matches(
        ((staged, destination, hashlib.sha256(b"baseline\n").hexdigest(), 0o644),)
    )

    assert destination.read_bytes() == b"replacement\n"
    assert (transaction.state_dir / "modules" / "0").read_bytes() == b"baseline\n"
    assert len(closed) == 2
    assert transaction._windows_conditional_descriptors == []
    assert transaction._windows_conditional_handles == []
    transaction.commit()
    assert len(closed) == 2
    assert not transaction.journal_path.exists()
    assert not transaction.state_dir.exists()


def test_windows_conditional_batch_never_clobbers_reappearing_destination(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "generated.js"
    destination.write_bytes(b"baseline\n")
    transaction = Transaction(tmp_path, "update", ["safe"])
    staged = transaction.state_dir / "template" / "0"
    staged.parent.mkdir()
    staged.write_bytes(b"replacement\n")
    paths, closed, rename_descriptor = _install_fake_windows_conditional_io(
        monkeypatch
    )
    injected = False

    def inject_concurrent_destination(
        descriptor: int,
        target: Path,
        *,
        root_directory: int | None = None,
    ) -> None:
        nonlocal injected
        source = paths[descriptor]
        if not injected and source == staged and target == destination:
            injected = True
            destination.write_bytes(b"external\n")
        rename_descriptor(
            descriptor,
            target,
            root_directory=root_directory,
        )

    monkeypatch.setattr(
        transaction_module,
        "_windows_rename_descriptor_no_replace",
        inject_concurrent_destination,
    )

    with pytest.raises(
        transaction_module.TransactionCleanupError,
        match="could not restore its exact retained identities",
    ):
        transaction.replace_regular_batch_if_matches(
            (
                (
                    staged,
                    destination,
                    hashlib.sha256(b"baseline\n").hexdigest(),
                    0o644,
                ),
            )
        )

    assert injected
    assert destination.read_bytes() == b"external\n"
    assert staged.read_bytes() == b"replacement\n"
    assert (transaction.state_dir / "modules" / "0").read_bytes() == b"baseline\n"
    assert transaction.data["phase"] == "conflict"
    assert len(closed) == 2


def test_windows_conditional_stale_baseline_restores_unmutated_state(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "generated.js"
    destination.write_bytes(b"changed\n")
    transaction = Transaction(tmp_path, "update", ["safe"])
    staged = transaction.state_dir / "template" / "0"
    staged.parent.mkdir()
    staged.write_bytes(b"replacement\n")
    _paths, closed, _rename = _install_fake_windows_conditional_io(monkeypatch)

    with pytest.raises(ConcurrentSourceMutation, match="Destination changed"):
        transaction.replace_regular_batch_if_matches(
            ((staged, destination, hashlib.sha256(b"baseline\n").hexdigest(), 0o644),)
        )

    assert destination.read_bytes() == b"changed\n"
    assert staged.read_bytes() == b"replacement\n"
    assert transaction.data["entries"] == []
    assert transaction.mutated is False
    assert len(closed) == 2


def test_windows_conditional_parent_authority_uses_retained_identity(
    tmp_path: Path,
    monkeypatch,
):
    metadata = tmp_path.lstat()
    closed: list[int] = []
    monkeypatch.setattr(transaction_module, "_windows_host", lambda: True)
    monkeypatch.setattr(
        transaction_module,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 91,
    )
    monkeypatch.setattr(transaction_module, "_windows_close_handle", closed.append)
    data = {
        "conditional_destination_parents": [
            {"path": ".", "dev": metadata.st_dev, "ino": metadata.st_ino}
        ]
    }

    assert transaction_module._conditional_destination_parents_match(tmp_path, data)
    data["conditional_destination_parents"][0]["ino"] = metadata.st_ino + 1
    assert not transaction_module._conditional_destination_parents_match(
        tmp_path, data
    )
    assert closed == [91, 91]


def test_windows_conditional_authority_reader_uses_private_state_paths(
    tmp_path: Path,
    monkeypatch,
):
    identifier = "a" * 32
    modules = tmp_path / f"{transaction_module.STATE_PREFIX}{identifier}" / "modules"
    modules.mkdir(parents=True)
    retention = modules / "conditional-retention-authority.json"
    payload = {"schema_version": 1, "transaction_id": identifier}
    retention.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(transaction_module, "_windows_host", lambda: True)
    monkeypatch.setattr(
        transaction_module,
        "read_regular_bytes_no_follow",
        lambda path: (Path(path).read_bytes(), Path(path).stat()),
    )

    authority, retention_authority = transaction_module._read_conditional_authorities(
        tmp_path,
        identifier,
        transaction_module.CONDITIONAL_CONFLICT_AUTHORITY_NAME,
    )

    assert authority is None
    assert retention_authority == payload


def test_windows_conditional_descriptor_release_reports_first_close_failure(
    tmp_path: Path,
    monkeypatch,
):
    transaction = Transaction(tmp_path, "update", ["safe"])
    transaction._windows_conditional_descriptors[:] = [71, 72]
    transaction._windows_conditional_handles[:] = [91, 92]
    closed_descriptors: list[int] = []
    closed_handles: list[int] = []

    def close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        if descriptor == 72:
            raise OSError("close failed")

    monkeypatch.setattr(transaction_module, "_close_descriptor", close)
    monkeypatch.setattr(
        transaction_module,
        "_windows_close_handle",
        closed_handles.append,
    )

    with pytest.raises(OSError, match="close failed"):
        transaction._release_windows_conditional_descriptors()

    assert closed_descriptors == [72, 71]
    assert closed_handles == [92, 91]
    assert transaction._windows_conditional_descriptors == []
    assert transaction._windows_conditional_handles == []


def test_windows_conditional_reset_failure_still_drains_owned_identities(
    tmp_path: Path,
    monkeypatch,
):
    transaction = Transaction(tmp_path, "update", ["safe"])
    closed_descriptors: list[int] = []
    closed_handles: list[int] = []
    authorization_calls = 0

    def prepare(
        _replacements,
        _modules,
        _entry_count,
        opened,
        opened_handles,
        _prepared,
        _parent_authority,
    ) -> None:
        opened.extend((70, 71))
        opened_handles.extend((700, 701, 702))

    def authorize() -> None:
        nonlocal authorization_calls
        authorization_calls += 1
        if authorization_calls == 2:
            raise OSError("journal reset failed")

    monkeypatch.setattr(
        transaction,
        "_prepare_windows_conditional_replacements",
        prepare,
    )
    monkeypatch.setattr(transaction, "_authorize_entries", authorize)
    monkeypatch.setattr(
        transaction_module,
        "_write_windows_conditional_retention_authority",
        lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
    )
    monkeypatch.setattr(
        transaction,
        "_restore_windows_conditional_batch",
        lambda _prepared: True,
    )
    monkeypatch.setattr(
        transaction_module,
        "_close_descriptor",
        closed_descriptors.append,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_close_handle",
        closed_handles.append,
    )

    with pytest.raises(OSError, match="journal reset failed"):
        transaction._replace_windows_regular_batch_if_matches(())

    assert closed_descriptors == [71, 70]
    assert closed_handles == [702, 701, 700]
    assert transaction._windows_conditional_descriptors == []
    assert transaction._windows_conditional_handles == []


def test_windows_conditional_conflict_retention_failure_drains_owned_identities(
    tmp_path: Path,
    monkeypatch,
):
    transaction = Transaction(tmp_path, "update", ["safe"])
    closed_descriptors: list[int] = []
    closed_handles: list[int] = []

    def prepare(
        _replacements,
        _modules,
        _entry_count,
        opened,
        opened_handles,
        _prepared,
        _parent_authority,
    ) -> None:
        opened.extend((70, 71))
        opened_handles.extend((700, 701, 702))

    monkeypatch.setattr(
        transaction,
        "_prepare_windows_conditional_replacements",
        prepare,
    )
    monkeypatch.setattr(transaction, "_authorize_entries", lambda: None)
    monkeypatch.setattr(
        transaction_module,
        "_write_windows_conditional_retention_authority",
        lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
    )
    monkeypatch.setattr(
        transaction,
        "_restore_windows_conditional_batch",
        lambda _prepared: False,
    )
    monkeypatch.setattr(
        transaction,
        "retain_conflict",
        lambda: (_ for _ in ()).throw(OSError("conflict retention failed")),
    )
    monkeypatch.setattr(
        transaction_module,
        "_close_descriptor",
        closed_descriptors.append,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_close_handle",
        closed_handles.append,
    )

    with pytest.raises(OSError, match="conflict retention failed"):
        transaction._replace_windows_regular_batch_if_matches(())

    assert closed_descriptors == [71, 70]
    assert closed_handles == [702, 701, 700]
    assert transaction._windows_conditional_descriptors == []
    assert transaction._windows_conditional_handles == []


def test_windows_conditional_local_close_failure_preserves_primary_and_drains_all(
    tmp_path: Path,
    monkeypatch,
):
    transaction = Transaction(tmp_path, "update", ["safe"])
    closed_descriptors: list[int] = []
    closed_handles: list[int] = []

    def prepare(
        _replacements,
        _modules,
        _entry_count,
        opened,
        opened_handles,
        _prepared,
        _parent_authority,
    ) -> None:
        opened.extend((70, 71))
        opened_handles.extend((700, 701, 702))
        raise OSError("prepare failed")

    def close_descriptor(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        if descriptor == 71:
            raise OSError("descriptor close failed")

    monkeypatch.setattr(
        transaction,
        "_prepare_windows_conditional_replacements",
        prepare,
    )
    monkeypatch.setattr(transaction, "_authorize_entries", lambda: None)
    monkeypatch.setattr(
        transaction,
        "_restore_windows_conditional_batch",
        lambda _prepared: True,
    )
    monkeypatch.setattr(
        transaction_module,
        "_close_descriptor",
        close_descriptor,
    )
    monkeypatch.setattr(
        transaction_module,
        "_windows_close_handle",
        closed_handles.append,
    )

    with pytest.raises(OSError, match="prepare failed") as caught:
        transaction._replace_windows_regular_batch_if_matches(())

    assert closed_descriptors == [71, 70]
    assert closed_handles == [702, 701, 700]
    assert len(caught.value.cleanup_failures) == 1
    assert str(caught.value.cleanup_failures[0]) == "descriptor close failed"
    assert transaction._windows_conditional_descriptors == []
    assert transaction._windows_conditional_handles == []
