from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace

import pytest

from supernote_module_generator.errors import ConcurrentSourceMutation
import supernote_module_generator.filesystem as filesystem
from supernote_module_generator.filesystem import (
    _apply_descriptor_atime_only,
    _apply_symlink_authority_metadata,
    _close_symlink_metadata_authority,
    _is_build_or_cache_path,
    _kind_from_mode,
    _read_symlink_authority_target,
    _same_observed_entry,
)
from supernote_module_generator.filesystem_inventory import (
    InventoryOperations,
    _open_symlink_authority,
    _restore_file_atime,
    _restore_symlink_atime,
    _verify_directory,
    inventory_posix_tree,
)


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="descriptor-relative POSIX inventory contract",
)


def _operations(*, fail_same_entry_call: int | None = None) -> InventoryOperations:
    calls = 0

    def same_entry(before: os.stat_result, after: os.stat_result) -> bool:
        nonlocal calls
        calls += 1
        if calls == fail_same_entry_call:
            return False
        return _same_observed_entry(before, after)

    return InventoryOperations(
        same_entry=same_entry,
        is_excluded=_is_build_or_cache_path,
        kind_from_mode=_kind_from_mode,
        apply_descriptor_atime_only=_apply_descriptor_atime_only,
        read_symlink_target=_read_symlink_authority_target,
        apply_symlink_atime=lambda authority, metadata: (
            _apply_symlink_authority_metadata(
                authority,
                metadata,
                atime_only=True,
            )
        ),
        close_symlink_authority=_close_symlink_metadata_authority,
    )


def test_rollback_inventory_accepts_only_probed_timestamp_representation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requested = 1_788_523_465_519_600_393
    represented_mtime = requested // 1_000_000_000 * 1_000_000_000
    before = {"source.cpp": ("file", 0o644, requested, "content-hash")}
    represented = {
        "source.cpp": ("file", 0o644, represented_mtime, "content-hash")
    }
    wrong_timestamp = {
        "source.cpp": ("file", 0o644, represented_mtime + 1, "content-hash")
    }
    wrong_content = {
        "source.cpp": ("file", 0o644, represented_mtime, "other-hash")
    }
    original_utime = os.utime

    def rounded_utime(path, *, ns, **options):
        original_utime(
            path,
            ns=(ns[0], ns[1] // 1_000_000_000 * 1_000_000_000),
            **options,
        )

    monkeypatch.setattr(filesystem.os, "utime", rounded_utime)

    assert filesystem.source_tree_changes_after_restore(
        tmp_path,
        before,
        represented,
    ) == ()
    assert filesystem.source_tree_changes_after_restore(
        tmp_path,
        before,
        wrong_timestamp,
    ) == ("modified:source.cpp",)
    assert filesystem.source_tree_changes_after_restore(
        tmp_path,
        before,
        wrong_content,
    ) == ("modified:source.cpp",)

    before_link = {"link": ("symlink", 0o755, requested, "target")}
    represented_link = {
        "link": ("symlink", 0o755, represented_mtime, "target")
    }
    assert filesystem.source_tree_changes_after_restore(
        tmp_path,
        before_link,
        represented_link,
    ) == ()


def test_timestamp_representation_probe_rejects_exact_and_unsupported(
    tmp_path: Path,
) -> None:
    requested = 1_788_523_465_519_600_393

    assert filesystem._probe_stable_mtime_representation(
        tmp_path,
        "file",
        requested,
    ) is None
    assert filesystem._probe_stable_mtime_representation(
        tmp_path,
        "directory",
        requested,
    ) is None


def test_rollback_inventory_reports_structural_and_directory_timestamp_changes(
    tmp_path: Path,
) -> None:
    before = {
        "deleted.cpp": ("file", 0o644, 10, "deleted-hash"),
        "folder": ("directory", 0o755, 20, None),
    }
    after = {
        "created.cpp": ("file", 0o644, 30, "created-hash"),
        "folder": ("directory", 0o755, 21, None),
    }

    assert filesystem.source_tree_changes_after_restore(
        tmp_path,
        before,
        after,
    ) == (
        "created:created.cpp",
        "deleted:deleted.cpp",
        "modified:folder",
    )


def test_timestamp_representation_probe_rejects_unstable_and_failed_application(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requested = 1_788_523_465_519_600_393
    original_utime = os.utime
    calls = 0

    def unstable_utime(path, *, ns, **options):
        nonlocal calls
        calls += 1
        represented = ns[1] // 1_000_000_000 * 1_000_000_000
        if calls == 2:
            represented += 1
        original_utime(path, ns=(ns[0], represented), **options)

    monkeypatch.setattr(filesystem.os, "utime", unstable_utime)
    assert filesystem._probe_stable_mtime_representation(
        tmp_path,
        "file",
        requested,
    ) is None

    def unsupported_utime(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(filesystem.os, "utime", unsupported_utime)
    assert filesystem._probe_stable_mtime_representation(
        tmp_path,
        "file",
        requested,
    ) is None


def _timestamp_matches_filesystem(
    path: Path,
    probe_parent: Path,
    actual: int,
    expected: int,
    *,
    attribute: str,
) -> bool:
    if actual == expected or (os.name == "nt" and actual // 100 == expected // 100):
        return True
    mode = path.lstat().st_mode
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


def test_inventory_boundary_records_each_entry_family_and_excludes_build_cache(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/source.cpp").write_bytes(b"int value = 1;\n")
    (tmp_path / "owned-link").symlink_to("nested/source.cpp")
    (tmp_path / "android/app/build").mkdir(parents=True)
    (tmp_path / "android/app/build/ignored.bin").write_bytes(b"ignored")
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "special")

    inventory = inventory_posix_tree(tmp_path, _operations())

    assert inventory["nested"][0] == "directory"
    assert inventory["nested/source.cpp"][0] == "file"
    assert inventory["owned-link"][0] == "symlink"
    assert inventory["owned-link"][3] == "nested/source.cpp"
    assert "android/app/build/ignored.bin" not in inventory
    if hasattr(os, "mkfifo"):
        assert inventory["special"][0] == "other"


@pytest.mark.parametrize(
    ("entry_factory", "failed_call", "message"),
    [
        (lambda root: (root / "link").symlink_to("target"), 1, "symbolic link"),
        (lambda root: (root / "source.cpp").write_bytes(b"source\n"), 1, "file"),
        (lambda root: (root / "nested").mkdir(), 1, "directory"),
    ],
)
def test_inventory_boundary_rejects_identity_change_before_consuming_entry(
    tmp_path: Path,
    entry_factory,
    failed_call: int,
    message: str,
) -> None:
    entry_factory(tmp_path)

    with pytest.raises(ConcurrentSourceMutation, match=message):
        inventory_posix_tree(
            tmp_path,
            _operations(fail_same_entry_call=failed_call),
        )


def test_inventory_boundary_rejects_directory_change_at_final_verification(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConcurrentSourceMutation, match="directory"):
        inventory_posix_tree(tmp_path, _operations(fail_same_entry_call=1))


def _advance_directory_atime_during_listing(
    monkeypatch,
    directory: Path,
    atime_ns: int,
) -> None:
    original_listdir = os.listdir

    def advancing_listdir(descriptor: int):
        entries = original_listdir(descriptor)
        current = os.fstat(descriptor)
        os.utime(descriptor, ns=(atime_ns, current.st_mtime_ns))
        return entries

    monkeypatch.setattr(filesystem.os, "listdir", advancing_listdir)


def test_observed_directory_accepts_stable_filesystem_timestamp_rounding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requested_atime_ns = 1_788_523_464_419_500_292
    requested_mtime_ns = 1_788_523_465_519_600_393
    os.utime(tmp_path, ns=(requested_atime_ns, requested_mtime_ns))
    before = tmp_path.lstat()
    if before.st_atime_ns != requested_atime_ns:
        pytest.skip("fixture requires subsecond timestamp representation")
    _advance_directory_atime_during_listing(
        monkeypatch,
        tmp_path,
        before.st_atime_ns + 2_000_000_000,
    )
    original_set_atime = filesystem._set_descriptor_atime_only

    def set_rounded_atime(descriptor: int, atime_ns: int) -> None:
        original_set_atime(descriptor, atime_ns // 1_000_000_000 * 1_000_000_000)

    monkeypatch.setattr(
        filesystem,
        "_set_descriptor_atime_only",
        set_rounded_atime,
    )

    children, observed = filesystem._observed_directory_entries(tmp_path)

    restored = tmp_path.lstat()
    assert children == []
    assert observed.st_atime_ns == before.st_atime_ns
    assert restored.st_atime_ns == before.st_atime_ns // 1_000_000_000 * 1_000_000_000
    assert restored.st_mtime_ns == before.st_mtime_ns


def test_observed_directory_preserves_exact_nanoseconds_when_representable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    requested_atime_ns = 1_788_523_464_419_500_292
    requested_mtime_ns = 1_788_523_465_519_600_393
    os.utime(tmp_path, ns=(requested_atime_ns, requested_mtime_ns))
    before = tmp_path.lstat()
    if before.st_atime_ns != requested_atime_ns:
        pytest.skip("fixture requires subsecond timestamp representation")
    _advance_directory_atime_during_listing(
        monkeypatch,
        tmp_path,
        before.st_atime_ns + 2_000_000_000,
    )

    filesystem._observed_directory_entries(tmp_path)

    restored = tmp_path.lstat()
    assert restored.st_atime_ns == before.st_atime_ns
    assert restored.st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("st_ino", lambda value: value + 1),
        ("st_mode", lambda value: value ^ stat.S_IWUSR),
        ("st_size", lambda value: value + 1),
        ("st_mtime_ns", lambda value: value + 1),
    ],
)
def test_observed_directory_still_rejects_non_atime_metadata_changes(
    tmp_path: Path,
    monkeypatch,
    field: str,
    changed_value,
) -> None:
    original_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        current = original_fstat(descriptor)
        if calls == 1:
            return current
        values = {
            "st_dev": current.st_dev,
            "st_ino": current.st_ino,
            "st_mode": current.st_mode,
            "st_size": current.st_size,
            "st_atime_ns": current.st_atime_ns,
            "st_mtime_ns": current.st_mtime_ns,
        }
        values[field] = changed_value(values[field])
        return SimpleNamespace(**values)

    monkeypatch.setattr(filesystem.os, "fstat", changing_fstat)

    with pytest.raises(ConcurrentSourceMutation, match="directory changed"):
        filesystem._observed_directory_entries(tmp_path)


@pytest.mark.parametrize(
    ("name", "failed_call", "message"),
    [
        ("link", 2, "symbolic link"),
        ("source.cpp", 2, "file"),
    ],
)
def test_inventory_boundary_rejects_identity_change_after_observation(
    tmp_path: Path,
    name: str,
    failed_call: int,
    message: str,
) -> None:
    path = tmp_path / name
    if name == "link":
        path.symlink_to("target")
    else:
        path.write_bytes(b"source\n")

    with pytest.raises(ConcurrentSourceMutation, match=message):
        inventory_posix_tree(
            tmp_path,
            _operations(fail_same_entry_call=failed_call),
        )


@pytest.mark.parametrize("entry_kind", ["file", "directory", "symlink"])
def test_inventory_atime_repair_uses_retained_entry_authority(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    entry = tmp_path / entry_kind
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    descriptor: int | None = None
    authority = None
    try:
        if entry_kind == "file":
            entry.write_bytes(b"value\n")
            descriptor = os.open(entry, os.O_RDONLY)
            opened = os.fstat(descriptor)
        elif entry_kind == "directory":
            entry.mkdir()
            descriptor = os.open(
                entry,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            opened = os.fstat(descriptor)
        else:
            entry.symlink_to("target")
            authority = _open_symlink_authority(parent_descriptor, entry.name, entry)
            opened = os.fstat(authority[1])

        changed_atime = max(1_000_000_000, opened.st_atime_ns - 1_000_000_000)
        if descriptor is not None:
            filesystem._set_descriptor_atime_only(descriptor, changed_atime)
        else:
            os.utime(
                entry,
                ns=(changed_atime, opened.st_mtime_ns),
                follow_symlinks=False,
            )

        if entry_kind == "file":
            assert descriptor is not None
            _restore_file_atime(
                descriptor,
                parent_descriptor,
                entry.name,
                entry,
                opened,
                os.fstat(descriptor),
                _operations(),
            )
            restored = os.fstat(descriptor)
        elif entry_kind == "directory":
            assert descriptor is not None
            _verify_directory(
                descriptor,
                opened,
                parent_descriptor=parent_descriptor,
                name=entry.name,
                path=entry,
                operations=_operations(),
            )
            restored = os.fstat(descriptor)
        else:
            assert authority is not None
            _restore_symlink_atime(
                authority,
                parent_descriptor,
                entry.name,
                entry,
                opened,
                os.fstat(authority[1]),
                _operations(),
            )
            restored = os.fstat(authority[1])

        assert _timestamp_matches_filesystem(
            entry,
            tmp_path.parent,
            restored.st_atime_ns,
            opened.st_atime_ns,
            attribute="atime",
        )
        assert _timestamp_matches_filesystem(
            entry,
            tmp_path.parent,
            restored.st_mtime_ns,
            opened.st_mtime_ns,
            attribute="mtime",
        )
    finally:
        if authority is not None:
            _close_symlink_metadata_authority(authority)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


@pytest.mark.parametrize("entry_kind", ["file", "directory", "symlink"])
def test_inventory_atime_repair_rejects_incomplete_restoration(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    entry = tmp_path / entry_kind
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    descriptor: int | None = None
    authority = None
    try:
        if entry_kind == "file":
            entry.write_bytes(b"value\n")
            descriptor = os.open(entry, os.O_RDONLY)
            opened = os.fstat(descriptor)
        elif entry_kind == "directory":
            entry.mkdir()
            descriptor = os.open(
                entry,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            opened = os.fstat(descriptor)
        else:
            entry.symlink_to("target")
            authority = _open_symlink_authority(parent_descriptor, entry.name, entry)
            opened = os.fstat(authority[1])

        changed_atime = max(1_000_000_000, opened.st_atime_ns - 1_000_000_000)
        os.utime(
            entry,
            ns=(changed_atime, opened.st_mtime_ns),
            follow_symlinks=entry_kind != "symlink",
        )
        no_restore = _operations()
        no_restore = InventoryOperations(
            same_entry=no_restore.same_entry,
            is_excluded=no_restore.is_excluded,
            kind_from_mode=no_restore.kind_from_mode,
            apply_descriptor_atime_only=lambda _descriptor, _atime_ns: None,
            read_symlink_target=no_restore.read_symlink_target,
            apply_symlink_atime=lambda _authority, _metadata: None,
            close_symlink_authority=no_restore.close_symlink_authority,
        )

        with pytest.raises(ConcurrentSourceMutation, match=entry_kind):
            if entry_kind == "file":
                assert descriptor is not None
                _restore_file_atime(
                    descriptor,
                    parent_descriptor,
                    entry.name,
                    entry,
                    opened,
                    os.fstat(descriptor),
                    no_restore,
                )
            elif entry_kind == "directory":
                assert descriptor is not None
                _verify_directory(
                    descriptor,
                    opened,
                    parent_descriptor=parent_descriptor,
                    name=entry.name,
                    path=entry,
                    operations=no_restore,
                )
            else:
                assert authority is not None
                _restore_symlink_atime(
                    authority,
                    parent_descriptor,
                    entry.name,
                    entry,
                    opened,
                    os.fstat(authority[1]),
                    no_restore,
                )
    finally:
        if authority is not None:
            _close_symlink_metadata_authority(authority)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def test_windows_symlink_preservation_probe_covers_direct_and_nested_links(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"preserve\n")
    direct = tmp_path / "direct"
    direct.symlink_to(target)
    nested_root = tmp_path / "nested"
    nested_root.mkdir()
    (nested_root / "link").symlink_to(target)
    calls = 0

    def unsupported() -> None:
        nonlocal calls
        calls += 1
        raise OSError("symlinks unavailable")

    monkeypatch.setattr(filesystem, "_probe_windows_symlink_support", unsupported)

    with pytest.raises(filesystem.SymlinkPreservationError, match="Developer Mode"):
        filesystem.validate_source_symlink_support(
            [direct, nested_root],
            platform_name="nt",
        )

    assert calls == 1
    assert direct.readlink() == target
    assert (nested_root / "link").readlink() == target
    assert target.read_bytes() == b"preserve\n"


def test_nonwindows_symlink_preservation_never_runs_windows_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    link = tmp_path / "link"
    link.symlink_to("target")
    monkeypatch.setattr(
        filesystem,
        "_probe_windows_symlink_support",
        lambda: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )

    filesystem.validate_source_symlink_support([link], platform_name="posix")
