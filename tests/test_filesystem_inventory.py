from __future__ import annotations

import os
from pathlib import Path

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
        os.utime(
            entry,
            ns=(changed_atime, opened.st_mtime_ns),
            follow_symlinks=entry_kind != "symlink",
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

        assert restored.st_atime_ns == opened.st_atime_ns
        assert restored.st_mtime_ns == opened.st_mtime_ns
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
