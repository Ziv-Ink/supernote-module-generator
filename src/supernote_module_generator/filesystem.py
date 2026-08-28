"""Link-aware filesystem primitives used by V4 preservation and transactions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
import uuid
from typing import Dict, Iterable, Iterator, Optional, Protocol, Tuple

from .errors import (
    ConcurrentSourceMutation,
    FilesystemError,
    SymlinkPreservationError,
)


def _detect_descriptor_relative_io_support() -> bool:
    return (
        os.mkdir in os.supports_dir_fd
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.link in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.utime in os.supports_fd
        and os.stat in os.supports_follow_symlinks
        and os.link in os.supports_follow_symlinks
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


_DESCRIPTOR_RELATIVE_IO_SUPPORTED = _detect_descriptor_relative_io_support()


class _Digest(Protocol):
    def update(self, data: bytes) -> None:
        ...


class ProtectedSourceRestoreError(FilesystemError):
    """A read-only guard could not re-establish its protected baseline."""

    def __init__(
        self,
        message: str,
        *,
        mutations: Tuple[str, ...],
        remaining: Tuple[str, ...],
        recovery_path: Path,
        interrupted: bool = False,
        diagnostics: Tuple[str, ...] = (),
        remaining_verified: bool = True,
    ) -> None:
        super().__init__(message)
        self.mutations = mutations
        self.remaining = remaining
        self.recovery_path = recovery_path
        self.interrupted = interrupted
        self.diagnostics = diagnostics
        self.remaining_verified = remaining_verified


def lexists(path: Path) -> bool:
    """Return true for every directory entry, including a broken symlink."""

    return os.path.lexists(path)


def entry_kind(path: Path) -> Optional[str]:
    """Classify an entry without dereferencing it, or return ``None`` if absent."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _open_observed(path: Path, *, directory: bool = False) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    return descriptor, os.fstat(descriptor)


def _same_observed_entry(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and stat.S_IMODE(before.st_mode) == stat.S_IMODE(after.st_mode)
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_size == after.st_size
    )


def _finish_observed_atime(
    path: Path,
    descriptor: int,
    before: os.stat_result,
) -> bool:
    """Restore only read-induced atime on the same otherwise-unchanged entry."""

    after = os.fstat(descriptor)
    try:
        live = path.lstat()
    except OSError:
        return False
    if not _same_observed_entry(before, after) or not _same_observed_entry(after, live):
        return False
    if after.st_atime_ns != before.st_atime_ns:
        os.utime(descriptor, ns=(before.st_atime_ns, after.st_mtime_ns))
    return True


def _finish_contained_directory_atime(
    root: Path,
    relative: str,
    descriptor: int,
    before: os.stat_result,
) -> bool:
    """Verify a contained directory by descriptors and neutralize read atime."""

    after = os.fstat(descriptor)
    if not _same_observed_entry(before, after):
        return False
    try:
        live_descriptor = _open_contained_directory_descriptor(root, relative)
    except OSError:
        return False
    try:
        live = os.fstat(live_descriptor)
    finally:
        os.close(live_descriptor)
    if not _same_observed_entry(after, live):
        return False
    if after.st_atime_ns != before.st_atime_ns:
        os.utime(descriptor, ns=(before.st_atime_ns, after.st_mtime_ns))
    return True


def _restore_observed_path_atime(path: Path, before: os.stat_result) -> bool:
    try:
        descriptor, _current = _open_observed(
            path, directory=stat.S_ISDIR(before.st_mode)
        )
    except OSError:
        return False
    try:
        return _finish_observed_atime(path, descriptor, before)
    finally:
        os.close(descriptor)


def read_regular_bytes_no_follow(path: Path) -> tuple[bytes, os.stat_result]:
    """Read one stable regular entry without following links or changing atime."""

    try:
        descriptor, before = _open_observed(path)
    except OSError as exc:
        raise FilesystemError(f"Cannot read regular source entry {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(before.st_mode):
            raise FilesystemError(f"Source entry is not a regular file: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if not _finish_observed_atime(path, descriptor, before):
            raise FilesystemError(f"Source entry changed while it was read: {path}")
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def validate_contained_path_no_follow(
    root: Path,
    path: Path,
    *,
    allowed_final_kinds: set[str | None],
) -> Path:
    """Validate one project path without resolving or following its components."""

    canonical_root = root.resolve(strict=True)
    try:
        relative = path.relative_to(canonical_root).as_posix()
    except ValueError as exc:
        raise FilesystemError(f"Project path is outside the plugin root: {path}") from exc
    candidate = canonical_root.joinpath(
        *validate_persisted_relative_path(relative).parts
    )
    kind = contained_entry_kind_no_follow(canonical_root, candidate)
    if kind not in allowed_final_kinds:
        raise FilesystemError(
            f"Project path has an unsafe final entry: {relative!r}"
        )
    return candidate


def contained_entry_kind_no_follow(root: Path, path: Path) -> Optional[str]:
    """Classify an optional contained path without inspecting through symlinks."""

    canonical_root = root.resolve(strict=True)
    try:
        relative = path.relative_to(canonical_root).as_posix()
    except ValueError as exc:
        raise FilesystemError(f"Project path is outside the plugin root: {path}") from exc
    parsed = validate_persisted_relative_path(relative)
    if _descriptor_relative_io_supported():
        try:
            parent_descriptor, leaf = _open_contained_parent_descriptor(
                canonical_root, path
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FilesystemError(
                f"Cannot inspect contained project path {relative!r}: {exc}"
            ) from exc
        try:
            try:
                metadata = os.stat(
                    leaf,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            return _kind_from_mode(metadata.st_mode)
        finally:
            os.close(parent_descriptor)
    current = canonical_root
    for index, part in enumerate(parsed.parts):
        current = current / part
        kind = entry_kind(current)
        final = index == len(parsed.parts) - 1
        if final:
            return kind
        if kind is None:
            return None
        if kind == "symlink":
            raise FilesystemError(
                f"Project path has a symbolic-link ancestor: {relative!r}"
            )
        if kind != "directory":
            raise FilesystemError(
                f"Project path has a non-directory ancestor: {relative!r}"
            )
    return entry_kind(canonical_root)


def contained_directory_entries_no_follow(
    root: Path,
    path: Path,
) -> Tuple[Tuple[str, str], ...]:
    """List direct children through one no-follow contained directory handle."""

    if not _descriptor_relative_io_supported():
        directory = validate_contained_path_no_follow(
            root,
            path,
            allowed_final_kinds={"directory"},
        )
        before = directory.lstat()
        observation_error: BaseException | None = None
        rows: Tuple[Tuple[str, str], ...] = ()
        try:
            rows = tuple(
                sorted(
                    (child.name, entry_kind(child) or "missing")
                    for child in directory.iterdir()
                )
            )
        except BaseException as exc:
            observation_error = exc
        if not _restore_observed_path_atime(directory, before):
            raise ConcurrentSourceMutation(
                f"Contained project directory changed while inspected: {path}"
            ) from observation_error
        if isinstance(observation_error, OSError):
            raise FilesystemError(
                f"Cannot list contained project directory {path}: {observation_error}"
            ) from observation_error
        if observation_error is not None:
            raise observation_error
        return rows
    relative = path.relative_to(root.resolve(strict=True)).as_posix()
    descriptor = _open_contained_directory_descriptor(root, relative)
    try:
        before = os.fstat(descriptor)
        descriptor_rows: list[tuple[str, str]] = []
        descriptor_error: BaseException | None = None
        try:
            for name in os.listdir(descriptor):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                descriptor_rows.append((name, _kind_from_mode(metadata.st_mode)))
        except BaseException as exc:
            descriptor_error = exc
        if not _finish_contained_directory_atime(root, relative, descriptor, before):
            raise ConcurrentSourceMutation(
                f"Contained project directory changed while inspected: {path}"
            ) from descriptor_error
        if isinstance(descriptor_error, OSError):
            raise FilesystemError(
                f"Cannot list contained project directory {path}: {descriptor_error}"
            ) from descriptor_error
        if descriptor_error is not None:
            raise descriptor_error
        return tuple(sorted(descriptor_rows))
    finally:
        os.close(descriptor)


def contained_tree_entries_no_follow(
    root: Path,
    path: Path,
) -> Tuple[Tuple[Path, str], ...]:
    """Walk a contained tree through directory handles without following links."""

    canonical_root = root.resolve(strict=True)
    if not _descriptor_relative_io_supported():
        directory = validate_contained_path_no_follow(
            canonical_root,
            path,
            allowed_final_kinds={"directory"},
        )
        return tuple((item, entry_kind(item) or "missing") for item in iter_tree_no_follow(directory))
    relative = path.relative_to(canonical_root).as_posix()
    descriptor = _open_contained_directory_descriptor(canonical_root, relative)
    rows: list[tuple[Path, str]] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def walk(current_descriptor: int, current_path: Path) -> None:
        before = os.fstat(current_descriptor)
        try:
            names = sorted(os.listdir(current_descriptor))
            for name in names:
                metadata = os.stat(
                    name,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                kind = _kind_from_mode(metadata.st_mode)
                child_path = current_path / name
                rows.append((child_path, kind))
                if kind != "directory":
                    continue
                child_descriptor = os.open(
                    name,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
                try:
                    walk(child_descriptor, child_path)
                finally:
                    os.close(child_descriptor)
            after = os.fstat(current_descriptor)
            if not _same_observed_entry(before, after):
                raise ConcurrentSourceMutation(
                    f"Contained project directory changed while inspected: {current_path}"
                )
            if after.st_atime_ns != before.st_atime_ns:
                os.utime(
                    current_descriptor,
                    ns=(before.st_atime_ns, after.st_mtime_ns),
                )
        except OSError as exc:
            raise FilesystemError(
                f"Cannot walk contained project directory {current_path}: {exc}"
            ) from exc

    try:
        walk(descriptor, path)
    finally:
        os.close(descriptor)
    return tuple(rows)


def _kind_from_mode(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def read_contained_regular_bytes_no_follow(
    root: Path,
    path: Path,
) -> tuple[bytes, os.stat_result]:
    """Read one contained regular file after rejecting every symlink component."""

    if not _descriptor_relative_io_supported():
        validated = validate_contained_path_no_follow(
            root,
            path,
            allowed_final_kinds={"file"},
        )
        return read_regular_bytes_no_follow(validated)
    parent_descriptor, leaf = _open_contained_parent_descriptor(root, path)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FilesystemError(f"Source entry is not a regular file: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            live = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ConcurrentSourceMutation(
                f"Source entry changed while it was read: {path}"
            ) from exc
        stable = _same_observed_entry(before, after) and _same_observed_entry(
            after, live
        )
        if after.st_atime_ns != before.st_atime_ns:
            os.utime(descriptor, ns=(before.st_atime_ns, after.st_mtime_ns))
        if not stable:
            raise ConcurrentSourceMutation(
                f"Source entry changed while it was read: {path}"
            )
        return b"".join(chunks), before
    except OSError as exc:
        raise FilesystemError(f"Cannot read contained regular entry {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _descriptor_relative_io_supported() -> bool:
    return _DESCRIPTOR_RELATIVE_IO_SUPPORTED


def _open_contained_parent_descriptor(root: Path, path: Path) -> tuple[int, str]:
    canonical_root = root.resolve(strict=True)
    try:
        relative = path.relative_to(canonical_root).as_posix()
    except ValueError as exc:
        raise FilesystemError(f"Project path is outside the plugin root: {path}") from exc
    parsed = validate_persisted_relative_path(relative)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(canonical_root, flags)
    try:
        for part in parsed.parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parsed.parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _open_contained_directory_descriptor(
    root: Path,
    relative: str,
) -> int:
    canonical_root = root.resolve(strict=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if relative == ".":
        return os.open(canonical_root, flags)
    path = canonical_root.joinpath(*validate_persisted_relative_path(relative).parts)
    parent_descriptor, leaf = _open_contained_parent_descriptor(canonical_root, path)
    try:
        return os.open(leaf, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def iter_tree_no_follow(root: Path) -> Iterator[Path]:
    """Yield descendants deterministically without entering directory symlinks."""

    try:
        root_kind = entry_kind(root)
    except OSError as exc:
        raise FilesystemError(f"Cannot inspect source tree {root}: {exc}") from exc
    if root_kind != "directory":
        return
    pending = [root]
    entries = []
    while pending:
        directory = pending.pop()
        try:
            descriptor, before = _open_observed(directory, directory=True)
            try:
                with os.scandir(directory) as stream:
                    children = sorted(stream, key=lambda child: child.name, reverse=True)
                if not _finish_observed_atime(directory, descriptor, before):
                    raise ConcurrentSourceMutation(
                        f"Source directory changed while it was inspected: {directory}"
                    )
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise FilesystemError(f"Cannot inspect source directory {directory}: {exc}") from exc
        for child in children:
            path = Path(child.path)
            entries.append(path)
            try:
                if child.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError as exc:
                raise FilesystemError(f"Cannot inspect source entry {path}: {exc}") from exc
    yield from sorted(entries)


def _capture_directory_tree_metadata(root: Path) -> Dict[Path, os.stat_result]:
    """Capture directory metadata before traversal can advance access times."""

    captured: Dict[Path, os.stat_result] = {}
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            descriptor, before = _open_observed(directory, directory=True)
            try:
                with os.scandir(directory) as stream:
                    children = list(stream)
                if not _finish_observed_atime(directory, descriptor, before):
                    raise ConcurrentSourceMutation(
                        f"Source directory changed while it was inspected: {directory}"
                    )
            finally:
                os.close(descriptor)
            captured[directory] = before
            for child in children:
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
    except OSError as exc:
        raise FilesystemError(f"Cannot inspect source tree {root}: {exc}") from exc
    return captured


def _apply_entry_stat(path: Path, metadata: os.stat_result) -> None:
    """Apply the supported exact mode and timestamps without following links."""

    os.chmod(path, stat.S_IMODE(metadata.st_mode), follow_symlinks=False)
    os.utime(
        path,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
        follow_symlinks=False,
    )


def copy_entry_no_follow(source: Path, destination: Path) -> None:
    """Copy one entry recursively while preserving symlinks as symlinks."""

    kind = entry_kind(source)
    if kind is None:
        raise FilesystemError(f"Cannot preserve missing source entry: {source}")
    if lexists(destination):
        remove_entry_no_follow(destination)
    if kind == "symlink":
        _copy_symlink(source, destination)
        return
    if kind == "file":
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_file_preserve_stat(source, destination)
        return
    if kind == "directory":
        source_directories = _capture_directory_tree_metadata(source)
        copied_directories: list[tuple[Path, Path]] = []
        try:
            destination.mkdir(parents=True, exist_ok=True)
            copied_directories.append((source, destination))
            for child in iter_tree_no_follow(source):
                relative = child.relative_to(source)
                child_kind = entry_kind(child)
                target = destination / relative
                if child_kind == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    copied_directories.append((child, target))
                elif child_kind == "file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_file_preserve_stat(child, target)
                elif child_kind == "symlink":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_symlink(child, target)
                else:
                    raise FilesystemError(
                        f"Cannot preserve unsupported source entry type: {child}"
                    )
            for source_directory, target_directory in reversed(copied_directories):
                _apply_entry_stat(target_directory, source_directories[source_directory])
        finally:
            for source_directory, metadata in reversed(tuple(source_directories.items())):
                if not _restore_observed_path_atime(source_directory, metadata):
                    raise ConcurrentSourceMutation(
                        f"Source directory changed while it was copied: {source_directory}"
                    )
        return
    raise FilesystemError(f"Cannot preserve unsupported source entry type: {source}")


def copy_tree_contents_no_follow(source: Path, destination: Path) -> None:
    """Preserve a user-owned source root, including when the root is a symlink."""

    kind = entry_kind(source)
    if kind is None:
        return
    if kind == "symlink":
        copy_entry_no_follow(source, destination)
        return
    if kind != "directory":
        raise FilesystemError(f"User source root is not a directory or symlink: {source}")
    source_directories = _capture_directory_tree_metadata(source)
    copied_directories: list[tuple[Path, Path]] = []
    try:
        destination.mkdir(parents=True, exist_ok=True)
        copied_directories.append((source, destination))
        for child in iter_tree_no_follow(source):
            relative = child.relative_to(source)
            target = destination / relative
            child_kind = entry_kind(child)
            if child_kind == "directory":
                target.mkdir(parents=True, exist_ok=True)
                copied_directories.append((child, target))
            elif child_kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_preserve_stat(child, target)
            elif child_kind == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                if lexists(target):
                    remove_entry_no_follow(target)
                _copy_symlink(child, target)
            else:
                raise FilesystemError(
                    f"Cannot preserve unsupported source entry type: {child}"
                )
        for source_directory, target_directory in reversed(copied_directories):
            _apply_entry_stat(target_directory, source_directories[source_directory])
    finally:
        for source_directory, metadata in reversed(tuple(source_directories.items())):
            if not _restore_observed_path_atime(source_directory, metadata):
                raise ConcurrentSourceMutation(
                    f"Source directory changed while it was copied: {source_directory}"
                )


def remove_entry_no_follow(path: Path) -> None:
    """Remove the named entry without following a symlink."""

    kind = entry_kind(path)
    if kind is None:
        return
    if kind == "directory":
        shutil.rmtree(path)
    else:
        path.unlink()


def hash_entry_no_follow(path: Path) -> Optional[str]:
    """Hash content, modes, entry kinds, and exact symlink target text."""

    kind = entry_kind(path)
    if kind is None:
        return None
    digest = hashlib.sha256()
    _update_entry_hash(digest, path, Path("."))
    if kind == "directory":
        for child in iter_tree_no_follow(path):
            _update_entry_hash(digest, child, child.relative_to(path))
    return digest.hexdigest()


SourceTreeInventory = Dict[str, Tuple[str, int, int, Optional[str]]]
ProtectedDirectoryMetadata = Dict[str, Tuple[int, int, int]]

_MAX_PERSISTED_TIMESTAMP_NS = (1 << 63) - 1


def validate_persisted_relative_path(
    value: str,
    *,
    allow_root: bool = False,
) -> PurePosixPath:
    """Validate an untrusted, persisted project-relative POSIX path."""

    if not isinstance(value, str):
        raise FilesystemError("Persisted recovery path must be a string")
    if allow_root and value == ".":
        return PurePosixPath(".")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or windows.drive
        or windows.root
        or posix.is_absolute()
        or not posix.parts
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise FilesystemError(
            f"Persisted recovery path must be canonical and relative: {value!r}"
        )
    return posix


def _validate_no_follow_path(
    root: Path,
    relative: str,
    *,
    allow_root: bool = False,
    allow_missing_ancestors: bool = False,
    allowed_final_kinds: set[str | None] | None = None,
) -> Path:
    """Resolve a validated relative path without accepting symlink ancestors."""

    parsed = validate_persisted_relative_path(relative, allow_root=allow_root)
    candidate = root if relative == "." else root.joinpath(*parsed.parts)
    current = root
    parts = () if relative == "." else parsed.parts
    missing = False
    for index, part in enumerate(parts):
        current = current / part
        kind = entry_kind(current)
        final = index == len(parts) - 1
        if final:
            if allowed_final_kinds is not None and kind not in allowed_final_kinds:
                raise FilesystemError(
                    f"Persisted recovery path has an unsafe final entry: {relative!r}"
                )
            continue
        if kind == "symlink":
            raise FilesystemError(
                f"Persisted recovery path has a symbolic-link ancestor: {relative!r}"
            )
        if kind is None:
            if not allow_missing_ancestors:
                raise FilesystemError(
                    f"Persisted recovery path has a missing ancestor: {relative!r}"
                )
            missing = True
            continue
        if missing or kind != "directory":
            raise FilesystemError(
                f"Persisted recovery path has a non-directory ancestor: {relative!r}"
            )
    return candidate


def validate_protected_directory_metadata(
    root: Path,
    metadata: ProtectedDirectoryMetadata,
    *,
    allow_missing: bool = False,
) -> ProtectedDirectoryMetadata:
    """Fully validate persisted directory metadata before any chmod/utime."""

    root = root.resolve(strict=True)
    validated: ProtectedDirectoryMetadata = {}
    for relative, raw in metadata.items():
        if (
            not isinstance(relative, str)
            or not isinstance(raw, tuple)
            or len(raw) != 3
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in raw
            )
        ):
            raise FilesystemError("Protected directory metadata is invalid")
        mode, atime_ns, mtime_ns = raw
        if (
            mode < 0
            or mode > 0o7777
            or atime_ns < 0
            or atime_ns > _MAX_PERSISTED_TIMESTAMP_NS
            or mtime_ns < 0
            or mtime_ns > _MAX_PERSISTED_TIMESTAMP_NS
        ):
            raise FilesystemError("Protected directory metadata is out of range")
        directory_kind: str | None
        if relative == ".":
            directory_kind = "directory"
        else:
            path = root.joinpath(
                *validate_persisted_relative_path(relative).parts
            )
            directory_kind = contained_entry_kind_no_follow(root, path)
        if directory_kind != "directory" and not (
            allow_missing and directory_kind is None
        ):
            raise FilesystemError(
                f"Protected directory metadata has an unsafe path: {relative!r}"
            )
        validated[relative] = (mode, atime_ns, mtime_ns)
    return validated


def source_tree_inventory(root: Path) -> SourceTreeInventory:
    """Inventory committed state while excluding only canonical build/cache roots.

    Directory names such as ``build`` and ``.gradle`` remain valid below user
    source roots.  Exclusions are therefore based on their complete managed
    project location, never on an arbitrary path component.
    """

    root = root.resolve(strict=True)
    inventory: SourceTreeInventory = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            descriptor, before = _open_observed(directory, directory=True)
            try:
                with os.scandir(directory) as stream:
                    children = sorted(stream, key=lambda child: child.name, reverse=True)
                if not _finish_observed_atime(directory, descriptor, before):
                    raise FilesystemError(
                        f"Source directory changed while it was inventoried: {directory}"
                    )
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise FilesystemError(f"Cannot inventory source directory {directory}: {exc}") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if _is_build_or_cache_path(relative):
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
                mode = stat.S_IMODE(metadata.st_mode)
                modified_ns = metadata.st_mtime_ns
                if child.is_symlink():
                    inventory[relative] = (
                        "symlink",
                        mode,
                        modified_ns,
                        os.readlink(path),
                    )
                elif child.is_dir(follow_symlinks=False):
                    inventory[relative] = ("directory", mode, 0, None)
                    pending.append(path)
                elif child.is_file(follow_symlinks=False):
                    inventory[relative] = (
                        "file",
                        mode,
                        modified_ns,
                        _file_sha256(path),
                    )
                else:
                    inventory[relative] = ("other", mode, modified_ns, None)
            except OSError as exc:
                raise FilesystemError(f"Cannot inventory source entry {path}: {exc}") from exc
    return dict(sorted(inventory.items()))


def source_tree_changes(
    before: SourceTreeInventory,
    after: SourceTreeInventory,
) -> Tuple[str, ...]:
    """Return stable added, removed, and modified source paths."""

    changed = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changed.append(f"created:{path}")
        elif path not in after:
            changed.append(f"deleted:{path}")
        elif before[path] != after[path]:
            changed.append(f"modified:{path}")
    return tuple(changed)


def protected_source_snapshot_roots(root: Path) -> Tuple[Path, ...]:
    """Return non-overlapping roots covering exactly the inventoried state.

    The returned roots deliberately split ``android`` and ``local_modules`` at
    canonical build/cache boundaries.  A transaction can snapshot these roots
    before invoking KSP or Gradle and therefore restore every entry observed by
    :func:`source_tree_inventory`, including newly-created entries below an
    existing protected directory.
    """

    root = root.resolve(strict=True)
    protected: list[Path] = []

    def collect(directory: Path) -> None:
        try:
            descriptor, before = _open_observed(directory, directory=True)
            try:
                with os.scandir(directory) as stream:
                    children = sorted(stream, key=lambda child: child.name)
                if not _finish_observed_atime(directory, descriptor, before):
                    raise FilesystemError(
                        f"Protected source directory changed while it was inspected: {directory}"
                    )
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise FilesystemError(
                f"Cannot inspect protected source directory {directory}: {exc}"
            ) from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if _is_build_or_cache_path(relative):
                continue
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise FilesystemError(
                    f"Cannot inspect protected source entry {path}: {exc}"
                ) from exc
            if is_directory and _may_contain_canonical_exclusion(relative):
                collect(path)
            else:
                protected.append(path)

    collect(root)
    return tuple(protected)


def protected_directory_metadata(root: Path) -> ProtectedDirectoryMetadata:
    """Capture root/ancestor directory metadata changed by atomic restoration."""

    root = root.resolve(strict=True)
    directories = {root}
    for protected in protected_source_snapshot_roots(root):
        current = protected if entry_kind(protected) == "directory" else protected.parent
        while True:
            if entry_kind(current) == "directory":
                directories.add(current)
            if current == root:
                break
            current = current.parent
    metadata: ProtectedDirectoryMetadata = {}
    for directory in sorted(directories):
        value = directory.lstat()
        relative = "." if directory == root else directory.relative_to(root).as_posix()
        metadata[relative] = (
            stat.S_IMODE(value.st_mode),
            value.st_atime_ns,
            value.st_mtime_ns,
        )
    return metadata


def restore_protected_directory_metadata(
    root: Path,
    metadata: ProtectedDirectoryMetadata,
) -> Tuple[str, ...]:
    """Restore captured directory modes/times and return any remaining drift."""

    root = root.resolve(strict=True)
    metadata = validate_protected_directory_metadata(root, metadata)
    failures: list[str] = []
    for relative, (mode, atime_ns, mtime_ns) in sorted(
        metadata.items(),
        key=lambda item: len(PurePosixPath(item[0]).parts),
        reverse=True,
    ):
        try:
            _apply_contained_directory_metadata(
                root, relative, mode, atime_ns, mtime_ns
            )
        except OSError:
            failures.append(f"modified:{relative}")
    for relative, (mode, atime_ns, mtime_ns) in metadata.items():
        try:
            value = _stat_contained_directory(root, relative)
        except OSError:
            marker = f"deleted:{relative}"
            if marker not in failures:
                failures.append(marker)
            continue
        if (
            stat.S_IMODE(value.st_mode) != mode
            or value.st_atime_ns != atime_ns
            or value.st_mtime_ns != mtime_ns
        ):
            marker = f"modified:{relative}"
            if marker not in failures:
                failures.append(marker)
    if not failures:
        # Verification of a descendant can advance ancestor atime on some
        # filesystems. Reapply deepest-to-shallow once more and perform no
        # further nested reads so the observable final metadata is exact.
        for relative, (mode, atime_ns, mtime_ns) in sorted(
            metadata.items(),
            key=lambda item: len(PurePosixPath(item[0]).parts),
            reverse=True,
        ):
            try:
                _apply_contained_directory_metadata(
                    root, relative, mode, atime_ns, mtime_ns
                )
            except OSError:
                failures.append(f"modified:{relative}")
    return tuple(sorted(failures))


def _apply_contained_directory_metadata(
    root: Path,
    relative: str,
    mode: int,
    atime_ns: int,
    mtime_ns: int,
) -> None:
    if _descriptor_relative_io_supported():
        descriptor = _open_contained_directory_descriptor(root, relative)
        try:
            os.fchmod(descriptor, mode)
            os.utime(descriptor, ns=(atime_ns, mtime_ns))
        finally:
            os.close(descriptor)
        return
    directory = root if relative == "." else root.joinpath(
        *validate_persisted_relative_path(relative).parts
    )
    validate_contained_path_no_follow(
        root, directory, allowed_final_kinds={"directory"}
    )
    os.chmod(directory, mode, follow_symlinks=False)
    os.utime(directory, ns=(atime_ns, mtime_ns), follow_symlinks=False)


def _stat_contained_directory(root: Path, relative: str) -> os.stat_result:
    if _descriptor_relative_io_supported():
        descriptor = _open_contained_directory_descriptor(root, relative)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)
    directory = root if relative == "." else root.joinpath(
        *validate_persisted_relative_path(relative).parts
    )
    validate_contained_path_no_follow(
        root, directory, allowed_final_kinds={"directory"}
    )
    return directory.lstat()


class ProtectedSourceGuard:
    """Out-of-tree backup/verification for read-only frontend invocations."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.before = source_tree_inventory(self.root)
        self.directory_metadata = protected_directory_metadata(self.root)
        self._observed_mutations: Tuple[str, ...] = ()
        self._temporary = Path(
            tempfile.mkdtemp(prefix="supernote-v4-source-guard-")
        )
        self._entries: list[tuple[Path, Path]] = []
        try:
            recovery_entries = []
            for index, source in enumerate(protected_source_snapshot_roots(self.root)):
                backup = self._temporary / "entries" / str(index)
                copy_entry_no_follow(source, backup)
                self._entries.append((source, backup))
                source_stat = source.lstat()
                recovery_entries.append(
                    {
                        "index": index,
                        "backup": f"entries/{index}",
                        "destination": source.relative_to(self.root).as_posix(),
                        "kind": entry_kind(source),
                        "mode": stat.S_IMODE(source_stat.st_mode),
                        "sha256": hash_entry_no_follow(source),
                    }
                )
            self._write_recovery_manifest(recovery_entries)
        except BaseException:
            try:
                self._remove_temporary()
            except BaseException:
                pass
            raise

    def finish(self) -> Tuple[str, ...]:
        """Return mutations and restore the exact protected baseline if needed."""

        changes = source_tree_changes(self.before, source_tree_inventory(self.root))
        if changes:
            self._observed_mutations = tuple(
                dict.fromkeys((*self._observed_mutations, *changes))
            )
        if not changes:
            directory_failures = restore_protected_directory_metadata(
                self.root, self.directory_metadata
            )
            if directory_failures:
                raise ProtectedSourceRestoreError(
                    "Could not restore protected directory metadata; the protected "
                    f"backup was retained at {self._temporary}",
                    mutations=(),
                    remaining=directory_failures,
                    recovery_path=self._temporary,
                )
            self._remove_temporary()
            return self._observed_mutations
        recovery_remaining: Tuple[str, ...] = ()
        try:
            for destination, backup in self._entries:
                self._restore_entry(destination, backup)
            # Remove new top-level or split-boundary entries that were not
            # contained by an existing protected snapshot root.
            for item in changes:
                action, separator, relative = item.partition(":")
                if not separator or action != "created":
                    continue
                destination = self.root.joinpath(*PurePosixPath(relative).parts)
                if any(
                    _path_is_within(destination, protected)
                    for protected, _ in self._entries
                ):
                    continue
                if lexists(destination):
                    remove_entry_no_follow(destination)
            remaining = source_tree_changes(
                self.before, source_tree_inventory(self.root)
            )
            if remaining:
                raise FilesystemError(
                    "Could not restore frontend source mutation: "
                    + ", ".join(remaining[:8])
                )
            directory_failures = restore_protected_directory_metadata(
                self.root, self.directory_metadata
            )
            if directory_failures:
                recovery_remaining = directory_failures
                raise FilesystemError(
                    "Could not restore protected directory metadata: "
                    + ", ".join(directory_failures[:8])
                )
        except BaseException as exc:
            diagnostics: Tuple[str, ...] = ()
            remaining_verified = True
            try:
                remaining = source_tree_changes(
                    self.before, source_tree_inventory(self.root)
                )
            except BaseException as inventory_exc:
                remaining = ()
                diagnostics = (f"inventory_failed:{inventory_exc}",)
                remaining_verified = False
            remaining = tuple(dict.fromkeys((*remaining, *recovery_remaining)))
            raise ProtectedSourceRestoreError(
                "Could not restore frontend source mutation; the protected "
                f"backup was retained at {self._temporary}: {exc}",
                mutations=self._observed_mutations,
                remaining=remaining,
                recovery_path=self._temporary,
                interrupted=isinstance(exc, KeyboardInterrupt),
                diagnostics=diagnostics,
                remaining_verified=remaining_verified,
            ) from exc
        self._remove_temporary()
        return self._observed_mutations

    @property
    def recovery_path(self) -> Path:
        """Return the retained out-of-tree recovery bundle path."""

        return self._temporary

    @property
    def observed_mutations(self) -> Tuple[str, ...]:
        """Return cumulative mutation evidence retained across finalization retries."""

        return self._observed_mutations

    def remaining_changes(self) -> Tuple[str, ...]:
        """Inventory exact protected residue without discarding the backup."""

        return source_tree_changes(self.before, source_tree_inventory(self.root))

    def _remove_temporary(self) -> None:
        if not lexists(self._temporary):
            return
        # Preserved read-only directory modes must not prevent deletion of the
        # guard's private copy. Never chmod symlinks or their targets.
        directories = [self._temporary]
        directories.extend(
            path
            for path in iter_tree_no_follow(self._temporary)
            if entry_kind(path) == "directory"
        )
        for directory in directories:
            directory.chmod(0o700)
        shutil.rmtree(self._temporary, ignore_errors=False)

    def _write_recovery_manifest(self, entries: list[dict[str, object]]) -> None:
        manifest = {
            "schema_version": 1,
            "plugin_root": str(self.root),
            "entries": entries,
            "directories": [
                {
                    "destination": relative,
                    "mode": mode,
                    "atime_ns": atime_ns,
                    "mtime_ns": mtime_ns,
                }
                for relative, (mode, atime_ns, mtime_ns) in sorted(
                    self.directory_metadata.items()
                )
            ],
        }
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        path = self._temporary / "recovery-manifest.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)

    def _restore_entry(self, destination: Path, backup: Path) -> None:
        """Stage a baseline copy before atomically replacing the live entry."""

        token = uuid.uuid4().hex
        staged = destination.with_name(
            f".{destination.name}.supernote-v4-restore-{token}"
        )
        displaced = destination.with_name(
            f".{destination.name}.supernote-v4-displaced-{token}"
        )
        copy_entry_no_follow(backup, staged)
        had_destination = lexists(destination)
        try:
            if had_destination:
                os.replace(destination, displaced)
            os.replace(staged, destination)
            if hash_entry_no_follow(destination) != hash_entry_no_follow(backup):
                raise FilesystemError(
                    f"Restored source does not match its backup: {destination}"
                )
        except BaseException:
            if lexists(staged):
                remove_entry_no_follow(staged)
            if lexists(displaced):
                if lexists(destination):
                    remove_entry_no_follow(destination)
                os.replace(displaced, destination)
            raise
        if lexists(displaced):
            remove_entry_no_follow(displaced)


def finish_protected_source_guard(
    guard: ProtectedSourceGuard,
    *,
    context_label: str,
) -> Tuple[Tuple[str, ...], bool]:
    """Finish a guard once, then complete recovery after one cancellation.

    Canonical observed mutations, verified current residue, and internal
    finalization diagnostics remain distinct across both attempts.
    """

    observed = guard.observed_mutations
    diagnostics: Tuple[str, ...] = ()
    try:
        return guard.finish(), False
    except ProtectedSourceRestoreError as first_exc:
        if not first_exc.interrupted:
            raise
        observed = tuple(
            dict.fromkeys(
                (*observed, *first_exc.mutations, *guard.observed_mutations)
            )
        )
        diagnostics = first_exc.diagnostics
    except KeyboardInterrupt:
        observed = tuple(
            dict.fromkeys((*observed, *guard.observed_mutations))
        )
    try:
        retry_mutations = guard.finish()
        return tuple(
            dict.fromkeys(
                (*observed, *retry_mutations, *guard.observed_mutations)
            )
        ), True
    except ProtectedSourceRestoreError as exc:
        exc.mutations = tuple(
            dict.fromkeys(
                (*observed, *exc.mutations, *guard.observed_mutations)
            )
        )
        exc.diagnostics = tuple(
            dict.fromkeys((*diagnostics, *exc.diagnostics))
        )
        exc.interrupted = True
        raise
    except BaseException as exc:
        final_diagnostics = [*diagnostics, f"finalization_failed:{exc}"]
        try:
            remaining = guard.remaining_changes()
            remaining_verified = True
        except BaseException as inventory_exc:
            remaining = ()
            remaining_verified = False
            final_diagnostics.append(f"inventory_failed:{inventory_exc}")
        mutations = tuple(
            dict.fromkeys(
                (*observed, *guard.observed_mutations, *remaining)
            )
        )
        raise ProtectedSourceRestoreError(
            f"{context_label} finalization was interrupted and exact "
            "protected-source restoration could not be verified; the backup "
            f"was retained at {guard.recovery_path}: {exc}",
            mutations=mutations,
            remaining=remaining,
            recovery_path=guard.recovery_path,
            interrupted=True,
            diagnostics=tuple(dict.fromkeys(final_diagnostics)),
            remaining_verified=remaining_verified,
        ) from exc


def restore_protected_source_backup(
    recovery_path: Path,
    plugin_root: Path,
) -> Tuple[str, ...]:
    """Restore a retained guard backup using only its durable manifest."""

    if entry_kind(recovery_path) != "directory":
        raise FilesystemError("Protected recovery root is missing or unsafe")
    recovery_root = recovery_path.resolve(strict=True)
    root = plugin_root.resolve(strict=True)
    manifest_path = _validate_no_follow_path(
        recovery_root,
        "recovery-manifest.json",
        allowed_final_kinds={"file"},
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FilesystemError(f"Protected recovery manifest is invalid: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("plugin_root") != str(root)
    ):
        raise FilesystemError("Protected recovery manifest targets another plugin")
    entries = manifest.get("entries")
    directories = manifest.get("directories")
    if not isinstance(entries, list) or not isinstance(directories, list):
        raise FilesystemError("Protected recovery manifest is incomplete")
    validated_entries: list[tuple[dict[str, object], Path, Path]] = []
    destinations: set[str] = set()
    backups: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise FilesystemError("Protected recovery entry is invalid")
        relative = raw.get("destination")
        backup_relative = raw.get("backup")
        if not isinstance(relative, str) or not isinstance(backup_relative, str):
            raise FilesystemError("Protected recovery entry paths are invalid")
        if relative in destinations or backup_relative in backups:
            raise FilesystemError("Protected recovery entry paths must be unique")
        destinations.add(relative)
        backups.add(backup_relative)
        expected_kind = raw.get("kind")
        if expected_kind not in {"file", "directory", "symlink"}:
            raise FilesystemError("Protected recovery entry kind is invalid")
        expected_mode = raw.get("mode")
        if (
            not isinstance(expected_mode, int)
            or isinstance(expected_mode, bool)
            or expected_mode < 0
            or expected_mode > 0o7777
        ):
            raise FilesystemError("Protected recovery entry mode is invalid")
        source = _validate_no_follow_path(
            recovery_root,
            backup_relative,
            allowed_final_kinds={str(expected_kind)},
        )
        destination = _validate_no_follow_path(
            root,
            relative,
            allow_missing_ancestors=True,
            allowed_final_kinds={None, "file", "directory", "symlink"},
        )
        expected = raw.get("sha256")
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
            or hash_entry_no_follow(source) != expected
        ):
            raise FilesystemError(
                f"Protected recovery backup hash mismatch: {relative}"
            )
        validated_entries.append((raw, source, destination))
    metadata: ProtectedDirectoryMetadata = {}
    for raw in directories:
        if not isinstance(raw, dict):
            raise FilesystemError("Protected recovery directory metadata is invalid")
        relative = raw.get("destination")
        values = (raw.get("mode"), raw.get("atime_ns"), raw.get("mtime_ns"))
        if not isinstance(relative, str) or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in values
        ):
            raise FilesystemError("Protected recovery directory metadata is invalid")
        mode, atime_ns, mtime_ns = values
        assert isinstance(mode, int)
        assert isinstance(atime_ns, int)
        assert isinstance(mtime_ns, int)
        validate_persisted_relative_path(relative, allow_root=True)
        metadata[relative] = (mode, atime_ns, mtime_ns)
    validate_protected_directory_metadata(root, metadata, allow_missing=True)

    for index, left in enumerate(sorted(destinations)):
        left_parts = PurePosixPath(left).parts
        for right in sorted(destinations)[index + 1 :]:
            right_parts = PurePosixPath(right).parts
            shorter = min(len(left_parts), len(right_parts))
            if left_parts[:shorter] == right_parts[:shorter]:
                raise FilesystemError(
                    "Protected recovery destinations must not overlap"
                )

    # The complete manifest and every source/destination are validated before
    # recovery creates an ancestor or replaces a live entry.
    for raw, source, destination in validated_entries:
        relative = str(raw["destination"])
        _ensure_recovery_parent_no_follow(root, relative)
        _restore_backup_entry_atomic(source, destination)
        expected = str(raw["sha256"])
        if hash_entry_no_follow(destination) != expected:
            raise FilesystemError(f"Protected recovery hash mismatch: {relative}")
    return restore_protected_directory_metadata(root, metadata)


def _ensure_recovery_parent_no_follow(root: Path, relative: str) -> None:
    parsed = validate_persisted_relative_path(relative)
    current = root
    for part in parsed.parts[:-1]:
        current = current / part
        kind = entry_kind(current)
        if kind is None:
            current.mkdir(mode=0o700)
            continue
        if kind != "directory":
            raise FilesystemError(
                f"Protected recovery destination has an unsafe ancestor: {relative!r}"
            )


def _restore_backup_entry_atomic(source: Path, destination: Path) -> None:
    token = uuid.uuid4().hex
    staged = destination.with_name(
        f".{destination.name}.supernote-v4-recover-{token}"
    )
    displaced = destination.with_name(
        f".{destination.name}.supernote-v4-displaced-{token}"
    )
    copy_entry_no_follow(source, staged)
    had_destination = lexists(destination)
    try:
        if had_destination:
            os.replace(destination, displaced)
        os.replace(staged, destination)
        if hash_entry_no_follow(destination) != hash_entry_no_follow(source):
            raise FilesystemError(
                f"Protected recovery activation mismatch: {destination}"
            )
    except BaseException:
        if lexists(staged):
            remove_entry_no_follow(staged)
        if lexists(displaced):
            if lexists(destination):
                remove_entry_no_follow(destination)
            os.replace(displaced, destination)
        raise
    if lexists(displaced):
        remove_entry_no_follow(displaced)


def retain_directory_metadata_recovery(
    plugin_root: Path,
    metadata: ProtectedDirectoryMetadata,
    *,
    transaction_id: str,
    outcome: str,
) -> tuple[Path, str, str]:
    """Persist an out-of-tree, fresh-process recovery bundle for metadata."""

    root = plugin_root.resolve(strict=True)
    metadata = validate_protected_directory_metadata(
        root,
        metadata,
        allow_missing=True,
    )
    if (
        len(transaction_id) != 32
        or any(character not in "0123456789abcdef" for character in transaction_id)
        or outcome not in {"rollback", "commit", "abandon"}
    ):
        raise FilesystemError("Transaction metadata recovery binding is invalid")
    bundle_id = uuid.uuid4().hex
    recovery = Path(tempfile.mkdtemp(prefix="supernote-v4-metadata-recovery-")).resolve(
        strict=True
    )
    manifest = {
        "schema_version": 2,
        "recovery_kind": "transaction-directory-metadata",
        "plugin_root": str(root),
        "transaction_id": transaction_id,
        "outcome": outcome,
        "bundle_id": bundle_id,
        "entries": [],
        "directories": [
            {
                "destination": relative,
                "mode": mode,
                "atime_ns": atime_ns,
                "mtime_ns": mtime_ns,
            }
            for relative, (mode, atime_ns, mtime_ns) in sorted(metadata.items())
        ],
    }
    path = recovery / "recovery-manifest.json"
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return recovery, bundle_id, hashlib.sha256(payload).hexdigest()


def validate_transaction_metadata_recovery(
    recovery_path: Path,
    plugin_root: Path,
    *,
    transaction_id: str,
    outcome: str,
    bundle_id: str,
    manifest_sha256: str,
) -> ProtectedDirectoryMetadata:
    """Validate a transaction-owned metadata-only recovery bundle without mutation."""

    root = plugin_root.resolve(strict=True)
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    path = Path(recovery_path)
    if (
        not path.is_absolute()
        or path.parent != temporary_root
        or not path.name.startswith("supernote-v4-metadata-recovery-")
        or entry_kind(path) != "directory"
        or path.resolve(strict=True) != path
    ):
        raise FilesystemError("Transaction metadata recovery ancestry is unsafe")
    manifest_path = _validate_no_follow_path(
        path,
        "recovery-manifest.json",
        allowed_final_kinds={"file"},
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(manifest_path, flags)
    except OSError as exc:
        raise FilesystemError("Transaction metadata recovery manifest is unavailable") from exc
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise FilesystemError(
                "Transaction metadata recovery manifest is not a regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(payload).hexdigest() != manifest_sha256:
        raise FilesystemError("Transaction metadata recovery manifest was modified")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FilesystemError("Transaction metadata recovery manifest is invalid") from exc
    expected_fields = {
        "schema_version",
        "recovery_kind",
        "plugin_root",
        "transaction_id",
        "outcome",
        "bundle_id",
        "entries",
        "directories",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_fields
        or manifest.get("schema_version") != 2
        or manifest.get("recovery_kind") != "transaction-directory-metadata"
        or manifest.get("plugin_root") != str(root)
        or manifest.get("transaction_id") != transaction_id
        or manifest.get("outcome") != outcome
        or manifest.get("bundle_id") != bundle_id
        or manifest.get("entries") != []
        or not isinstance(manifest.get("directories"), list)
    ):
        raise FilesystemError("Transaction metadata recovery binding is invalid")
    metadata: ProtectedDirectoryMetadata = {}
    for raw in manifest["directories"]:
        if not isinstance(raw, dict) or set(raw) != {
            "destination",
            "mode",
            "atime_ns",
            "mtime_ns",
        }:
            raise FilesystemError("Transaction metadata recovery entry is invalid")
        relative = raw.get("destination")
        values = (raw.get("mode"), raw.get("atime_ns"), raw.get("mtime_ns"))
        if not isinstance(relative, str) or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in values
        ):
            raise FilesystemError("Transaction metadata recovery entry is invalid")
        if relative in metadata:
            raise FilesystemError("Transaction metadata recovery entries are duplicated")
        mode, atime_ns, mtime_ns = values
        assert isinstance(mode, int)
        assert isinstance(atime_ns, int)
        assert isinstance(mtime_ns, int)
        metadata[relative] = (mode, atime_ns, mtime_ns)
    return validate_protected_directory_metadata(root, metadata, allow_missing=True)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_source_symlink_support(
    roots: Iterable[Path],
    *,
    platform_name: str = os.name,
) -> None:
    """Fail before project mutation when Windows cannot recreate source links."""

    links = tuple(_source_symlinks(roots))
    if not links or platform_name != "nt":
        return
    try:
        _probe_windows_symlink_support()
    except OSError as exc:
        raise SymlinkPreservationError(
            "Windows cannot preserve user-owned source symlinks in this environment. "
            "Enable Developer Mode or grant the Create symbolic links privilege, then "
            "retry. V4 will not dereference symlinks as a fallback."
        ) from exc


def _source_symlinks(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        kind = entry_kind(root)
        if kind == "symlink":
            yield root
            continue
        if kind != "directory":
            continue
        for path in iter_tree_no_follow(root):
            if entry_kind(path) == "symlink":
                yield path


def _copy_symlink(source: Path, destination: Path) -> None:
    target = os.readlink(source)
    source_metadata = source.lstat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(
            target,
            destination,
            target_is_directory=_symlink_is_directory(source),
        )
        try:
            os.utime(
                destination,
                ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
                follow_symlinks=False,
            )
        except (NotImplementedError, OSError):
            # Exact target text and link identity are mandatory everywhere.
            # Link timestamps are preserved on platforms that expose the
            # no-follow operation.
            pass
    except OSError as exc:
        raise SymlinkPreservationError(
            f"Could not preserve symbolic link {source} -> {target!r}. "
            "V4 will not dereference symlinks as a fallback: {exc}"
        ) from exc


def _copy_file_preserve_stat(source: Path, destination: Path) -> None:
    """Copy a regular file without letting the read change captured atime."""

    if lexists(destination):
        remove_entry_no_follow(destination)
    source_descriptor, metadata = _open_observed(source)
    destination_descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        destination_descriptor = os.open(
            destination, flags, stat.S_IMODE(metadata.st_mode)
        )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        os.fsync(destination_descriptor)
        if not _finish_observed_atime(source, source_descriptor, metadata):
            raise ConcurrentSourceMutation(
                f"Source file changed while it was copied: {source}"
            )
        _apply_entry_stat(destination, metadata)
    except BaseException:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if lexists(destination):
            remove_entry_no_follow(destination)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _symlink_is_directory(path: Path) -> bool:
    if os.name != "nt":
        return False
    metadata = path.lstat()
    directory_attribute = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0)
    if directory_attribute and hasattr(metadata, "st_file_attributes"):
        return bool(getattr(metadata, "st_file_attributes") & directory_attribute)
    if path.is_dir():
        return True
    if not path.exists():
        raise SymlinkPreservationError(
            f"Windows could not determine whether broken symlink {path} targets a "
            "file or directory. The link was not dereferenced."
        )
    return False


def _update_entry_hash(digest: _Digest, path: Path, relative: Path) -> None:
    metadata = path.lstat()
    kind = entry_kind(path)
    digest.update(relative.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(kind).encode("ascii"))
    digest.update(b"\0")
    digest.update(f"{stat.S_IMODE(metadata.st_mode):o}".encode("ascii"))
    digest.update(b"\0")
    if kind == "symlink":
        digest.update(os.fsencode(os.readlink(path)))
    elif kind == "file":
        _update_digest_from_file(digest, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _update_digest_from_file(digest: _Digest, path: Path) -> None:
    """Hash a file while restoring any access-time side effect of reading it."""

    descriptor, before = _open_observed(path)
    try:
        if not stat.S_ISREG(before.st_mode):
            raise FilesystemError(f"Hash source is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if not _finish_observed_atime(path, descriptor, before):
            raise ConcurrentSourceMutation(
                f"Source file changed while it was hashed: {path}"
            )
    finally:
        os.close(descriptor)


def _is_build_or_cache_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    first = parts[0]
    if first in {
        ".git",
        "node_modules",
        ".idea",
        "build",
        ".gradle",
        ".cxx",
        ".kotlin",
    }:
        return True
    if first == ".supernote-module-transaction.json":
        return True
    if first.startswith((".supernote-module-transaction-", ".v4-plan-")):
        return True
    if parts[:2] in {
        ("android", "build"),
        ("android", ".gradle"),
        ("android", ".cxx"),
        ("android", ".kotlin"),
    }:
        return True
    if parts[:3] == ("android", "app", "build"):
        return True
    if parts[:4] == (
        "android",
        ".supernote-module",
        "v4-runtime",
        "build",
    ):
        return True
    if (
        parts[:3] == ("android", ".supernote-module", "v4-runtime")
        and len(parts) >= 5
        and parts[3] in {"annotations", "processor"}
        and parts[4] == "build"
    ):
        return True
    feature_offset = _feature_component_count(parts)
    return feature_offset is not None and parts[
        feature_offset : feature_offset + 2
    ] in {
        ("android", "build"),
        ("android", ".gradle"),
        ("android", ".cxx"),
        ("android", ".kotlin"),
    }


def _feature_component_count(parts: Tuple[str, ...]) -> Optional[int]:
    if not parts or parts[0] != "local_modules":
        return None
    if len(parts) >= 3 and parts[1].startswith("@"):
        return 3
    if len(parts) >= 2:
        return 2
    return None


def _may_contain_canonical_exclusion(relative: str) -> bool:
    """Whether an included directory must be split around a nested exclusion."""

    parts = PurePosixPath(relative).parts
    if parts == ("android",):
        return True
    if parts in {
        ("android", "app"),
        ("android", ".supernote-module"),
        ("android", ".supernote-module", "v4-runtime"),
        ("android", ".supernote-module", "v4-runtime", "annotations"),
        ("android", ".supernote-module", "v4-runtime", "processor"),
        ("local_modules",),
    }:
        return True
    feature_offset = _feature_component_count(parts)
    if feature_offset is None:
        return False
    if len(parts) <= feature_offset:
        return True
    return parts[feature_offset:] == ("android",)


def _probe_windows_symlink_support() -> None:
    with tempfile.TemporaryDirectory(prefix="supernote-v4-symlink-probe-") as raw:
        root = Path(raw)
        file_target = root / "file-target"
        directory_target = root / "directory-target"
        file_target.write_bytes(b"")
        directory_target.mkdir()
        os.symlink("file-target", root / "file-link", target_is_directory=False)
        os.symlink(
            "directory-target",
            root / "directory-link",
            target_is_directory=True,
        )
