from __future__ import annotations

import os
from pathlib import Path
import stat
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


def test_observed_directory_does_not_rewrite_access_time(
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
    monkeypatch.setattr(
        filesystem,
        "_set_descriptor_atime_only",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("read-only observation must not rewrite atime")
        ),
    )

    children, observed = filesystem._observed_directory_entries(tmp_path)

    restored = tmp_path.lstat()
    assert children == []
    assert observed.st_atime_ns == before.st_atime_ns
    assert restored.st_atime_ns == before.st_atime_ns + 2_000_000_000
    assert restored.st_mtime_ns == before.st_mtime_ns


def test_observed_directory_allows_read_induced_access_time_change(
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
    assert restored.st_atime_ns == before.st_atime_ns + 2_000_000_000
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


def test_inventory_never_invokes_metadata_write_callbacks(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/source.cpp").write_bytes(b"source\n")
    (tmp_path / "link").symlink_to("nested/source.cpp")
    operations = _operations()
    operations = InventoryOperations(
        same_entry=operations.same_entry,
        is_excluded=operations.is_excluded,
        kind_from_mode=operations.kind_from_mode,
        apply_descriptor_atime_only=lambda *_args: (_ for _ in ()).throw(
            AssertionError("inventory must not write file or directory metadata")
        ),
        read_symlink_target=operations.read_symlink_target,
        apply_symlink_atime=lambda *_args: (_ for _ in ()).throw(
            AssertionError("inventory must not write symlink metadata")
        ),
        close_symlink_authority=operations.close_symlink_authority,
    )

    inventory = inventory_posix_tree(tmp_path, operations)

    assert inventory["nested/source.cpp"][3]
    assert inventory["link"][3] == "nested/source.cpp"


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
