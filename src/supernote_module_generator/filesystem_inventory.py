"""Descriptor-bound POSIX source-tree inventory traversal."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Callable, Dict, Optional, Tuple

from .errors import ConcurrentSourceMutation, FilesystemError


SourceTreeInventory = Dict[str, Tuple[str, int, int, Optional[str]]]
SymlinkAuthority = tuple[str, int]


@dataclass(frozen=True)
class InventoryOperations:
    same_entry: Callable[[os.stat_result, os.stat_result], bool]
    is_excluded: Callable[[str], bool]
    kind_from_mode: Callable[[int], str]
    apply_descriptor_atime_only: Callable[[int, int], None]
    read_symlink_target: Callable[[SymlinkAuthority], str]
    apply_symlink_atime: Callable[[SymlinkAuthority, os.stat_result], None]
    close_symlink_authority: Callable[[SymlinkAuthority], None]


def inventory_posix_tree(
    root: Path,
    operations: InventoryOperations,
) -> SourceTreeInventory:
    inventory: SourceTreeInventory = {}
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(root, directory_flags)
    try:
        _walk(
            root,
            inventory,
            root_descriptor,
            PurePosixPath(),
            parent_descriptor=None,
            directory_name=None,
            directory_flags=directory_flags,
            file_flags=file_flags,
            operations=operations,
        )
    finally:
        os.close(root_descriptor)
    return dict(sorted(inventory.items()))


def _read_symlink(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
    path: Path,
    operations: InventoryOperations,
) -> str:
    authority = _open_symlink_authority(parent_descriptor, name, path)
    try:
        opened = os.fstat(authority[1])
        if not operations.same_entry(before, opened):
            raise _changed("symbolic link", path)
        target = operations.read_symlink_target(authority)
        after = os.fstat(authority[1])
        live = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not operations.same_entry(opened, after) or not operations.same_entry(
            after, live
        ):
            raise _changed("symbolic link", path)
        if after.st_atime_ns != opened.st_atime_ns:
            _restore_symlink_atime(
                authority,
                parent_descriptor,
                name,
                path,
                opened,
                after,
                operations,
            )
        return target
    finally:
        operations.close_symlink_authority(authority)


def _open_symlink_authority(
    parent_descriptor: int,
    name: str,
    path: Path,
) -> SymlinkAuthority:
    if sys.platform == "darwin":
        return (
            "darwin",
            os.open(name, os.O_RDONLY | 0x00200000, dir_fd=parent_descriptor),
        )
    if hasattr(os, "O_PATH") and hasattr(os, "O_NOFOLLOW"):
        return (
            "linux",
            os.open(
                name,
                getattr(os, "O_PATH") | getattr(os, "O_NOFOLLOW"),
                dir_fd=parent_descriptor,
            ),
        )
    raise FilesystemError(f"Cannot retain symbolic-link inventory authority: {path}")


def _restore_symlink_atime(
    authority: SymlinkAuthority,
    parent_descriptor: int,
    name: str,
    path: Path,
    opened: os.stat_result,
    after: os.stat_result,
    operations: InventoryOperations,
) -> None:
    operations.apply_symlink_atime(authority, opened)
    restored = os.fstat(authority[1])
    restored_live = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not operations.same_entry(after, restored)
        or not operations.same_entry(restored, restored_live)
        or restored.st_atime_ns != opened.st_atime_ns
    ):
        raise _changed("symbolic link", path)


def _live_directory_metadata(
    path: Path,
    parent_descriptor: int | None,
    name: str | None,
) -> os.stat_result:
    if parent_descriptor is None:
        return path.lstat()
    assert name is not None
    return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)


def _verify_directory(
    descriptor: int,
    before: os.stat_result,
    *,
    parent_descriptor: int | None,
    name: str | None,
    path: Path,
    operations: InventoryOperations,
) -> None:
    after = os.fstat(descriptor)
    live = _live_directory_metadata(path, parent_descriptor, name)
    if not operations.same_entry(before, after) or not operations.same_entry(
        after, live
    ):
        raise _changed("directory", path)
    if after.st_atime_ns == before.st_atime_ns:
        return
    operations.apply_descriptor_atime_only(descriptor, before.st_atime_ns)
    restored = os.fstat(descriptor)
    restored_live = _live_directory_metadata(path, parent_descriptor, name)
    if (
        not operations.same_entry(after, restored)
        or not operations.same_entry(restored, restored_live)
        or restored.st_atime_ns != before.st_atime_ns
    ):
        raise _changed("directory", path)


def _hash_regular(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
    path: Path,
    file_flags: int,
    operations: InventoryOperations,
) -> str:
    descriptor = os.open(name, file_flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if not operations.same_entry(before, opened):
            raise _changed("file", path)
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(descriptor)
        live = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not operations.same_entry(opened, after) or not operations.same_entry(
            after, live
        ):
            raise _changed("file", path)
        if after.st_atime_ns != opened.st_atime_ns:
            _restore_file_atime(
                descriptor,
                parent_descriptor,
                name,
                path,
                opened,
                after,
                operations,
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _restore_file_atime(
    descriptor: int,
    parent_descriptor: int,
    name: str,
    path: Path,
    opened: os.stat_result,
    after: os.stat_result,
    operations: InventoryOperations,
) -> None:
    operations.apply_descriptor_atime_only(descriptor, opened.st_atime_ns)
    restored = os.fstat(descriptor)
    restored_live = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not operations.same_entry(after, restored)
        or not operations.same_entry(restored, restored_live)
        or restored.st_atime_ns != opened.st_atime_ns
    ):
        raise _changed("file", path)


def _walk(
    root: Path,
    inventory: SourceTreeInventory,
    descriptor: int,
    relative_parent: PurePosixPath,
    *,
    parent_descriptor: int | None,
    directory_name: str | None,
    directory_flags: int,
    file_flags: int,
    operations: InventoryOperations,
) -> None:
    path = root.joinpath(*relative_parent.parts)
    before = os.fstat(descriptor)
    for name in sorted(os.listdir(descriptor)):
        relative = relative_parent / name
        relative_text = relative.as_posix()
        if operations.is_excluded(relative_text):
            continue
        child_path = root.joinpath(*relative.parts)
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        kind = operations.kind_from_mode(metadata.st_mode)
        mode = stat.S_IMODE(metadata.st_mode)
        if kind == "directory":
            inventory[relative_text] = ("directory", mode, 0, None)
            _walk_directory(
                root,
                inventory,
                descriptor,
                name,
                relative,
                child_path,
                metadata,
                directory_flags,
                file_flags,
                operations,
            )
        elif kind == "file":
            inventory[relative_text] = (
                "file",
                mode,
                metadata.st_mtime_ns,
                _hash_regular(
                    descriptor,
                    name,
                    metadata,
                    child_path,
                    file_flags,
                    operations,
                ),
            )
        elif kind == "symlink":
            inventory[relative_text] = (
                "symlink",
                mode,
                metadata.st_mtime_ns,
                _read_symlink(descriptor, name, metadata, child_path, operations),
            )
        else:
            inventory[relative_text] = ("other", mode, metadata.st_mtime_ns, None)
    _verify_directory(
        descriptor,
        before,
        parent_descriptor=parent_descriptor,
        name=directory_name,
        path=path,
        operations=operations,
    )


def _walk_directory(
    root: Path,
    inventory: SourceTreeInventory,
    parent_descriptor: int,
    name: str,
    relative: PurePosixPath,
    child_path: Path,
    metadata: os.stat_result,
    directory_flags: int,
    file_flags: int,
    operations: InventoryOperations,
) -> None:
    descriptor = os.open(name, directory_flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if not operations.same_entry(metadata, opened):
            raise _changed("directory", child_path)
        _walk(
            root,
            inventory,
            descriptor,
            relative,
            parent_descriptor=parent_descriptor,
            directory_name=name,
            directory_flags=directory_flags,
            file_flags=file_flags,
            operations=operations,
        )
    finally:
        os.close(descriptor)


def _changed(kind: str, path: Path) -> ConcurrentSourceMutation:
    return ConcurrentSourceMutation(
        f"Source {kind} changed while it was inventoried: {path}"
    )
