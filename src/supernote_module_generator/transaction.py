"""Persistent same-filesystem transaction journal and recovery engine."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, Iterable, List, Optional, cast

from . import filesystem as filesystem_ops
from .errors import ConcurrentSourceMutation, FilesystemError, PartialFailure
from .filesystem import (
    SourceTreeInventory,
    _descriptor_relative_io_supported,
    _is_build_or_cache_path,
    _observed_directory_entries,
    _open_contained_directory_descriptor,
    _open_contained_parent_descriptor,
    _windows_host,
    _windows_path_key,
    copy_entry_no_follow,
    entry_kind,
    hash_entry_no_follow,
    iter_tree_no_follow,
    lexists,
    read_regular_bytes_no_follow,
    read_contained_regular_bytes_no_follow,
    remove_entry_no_follow,
    retain_directory_metadata_recovery,
    source_tree_changes,
    source_tree_inventory,
    validate_transaction_metadata_recovery,
    validate_persisted_relative_path,
    validate_protected_directory_metadata,
)
from .models import RollbackResult, WarningInfo
from .transaction_registry import (
    entry_kind_fields_are_valid as _entry_kind_fields_are_valid,
    entry_digest as _registry_entry_digest,
    parse_recovery_pointer,
    private_recovery_registry,
    recovery_pointer_path,
    recovery_pointer_payload,
    validated_transaction_identifier,
)

JOURNAL_NAME = ".supernote-module-transaction.json"
STATE_PREFIX = ".supernote-module-transaction-"
ENTRY_AUTHORITY_NAME = "entry-authority.json"
ENTRY_AUTHORITY_PREFIX = "entry-authority-"
CONDITIONAL_CONFLICT_AUTHORITY_NAME = (
    "modules/conditional-conflict-authority.json"
)
CONDITIONAL_RETENTION_AUTHORITY_NAME = (
    "modules/conditional-retention-authority.json"
)


class TransactionCleanupError(FilesystemError):
    """A durable outcome exists but exact metadata cleanup is incomplete."""

    def __init__(
        self,
        message: str,
        *,
        recovery_path: Path | None = None,
        interrupted: bool = False,
    ) -> None:
        if recovery_path is not None:
            message += f" Recovery authority remains at {recovery_path}."
        super().__init__(message)
        self.recovery_path = recovery_path
        self.interrupted = interrupted


def _hash_path(path: Path) -> Optional[str]:
    return hash_entry_no_follow(path)


def _restore_regular_no_clobber(restore: Path, destination: Path) -> None:
    """Restore a retained regular file unless a newer destination already exists."""

    try:
        os.link(restore, destination, follow_symlinks=False)
    except FileExistsError:
        pass
    remove_entry_no_follow(restore)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


@dataclass
class _ConditionalBatchDescriptors:
    capture: int
    modules: int
    staged: int
    destinations: Dict[str, tuple[int, str]]
    parent_metadata: Dict[int, tuple[int, int, int]]

    def destination(self, path: Path) -> tuple[int, str]:
        try:
            return self.destinations[str(path)]
        except KeyError as exc:
            raise FilesystemError(
                f"Conditional destination authority is unavailable: {path}"
            ) from exc

    def destination_parents_match(self, root: Path) -> bool:
        """Return whether every retained parent is still canonically reachable."""

        verified: set[int] = set()
        for path_text, (retained_descriptor, _name) in self.destinations.items():
            if retained_descriptor in verified:
                continue
            current_descriptor: int | None = None
            try:
                current_descriptor, _ = _open_contained_parent_descriptor(
                    root, Path(path_text)
                )
                retained = os.fstat(retained_descriptor)
                current = os.fstat(current_descriptor)
                if (
                    retained.st_dev != current.st_dev
                    or retained.st_ino != current.st_ino
                ):
                    return False
            except (OSError, FilesystemError):
                return False
            finally:
                if current_descriptor is not None:
                    os.close(current_descriptor)
            verified.add(retained_descriptor)
        return True

    def destination_parent_authority(
        self, root: Path
    ) -> list[Dict[str, object]]:
        """Return one canonical identity record for each retained parent."""

        authority: list[Dict[str, object]] = []
        recorded: set[int] = set()
        for path_text, (descriptor, _name) in sorted(self.destinations.items()):
            if descriptor in recorded:
                continue
            metadata = os.fstat(descriptor)
            authority.append(
                {
                    "path": Path(path_text).parent.relative_to(root).as_posix(),
                    "dev": metadata.st_dev,
                    "ino": metadata.st_ino,
                }
            )
            recorded.add(descriptor)
        return authority

    def restore_destination_parent_metadata(self) -> None:
        """Restore metadata changed by child retain/publication operations."""

        for descriptor, (mode, atime_ns, mtime_ns) in self.parent_metadata.items():
            current = os.fstat(descriptor)
            if stat.S_IMODE(current.st_mode) != mode:
                raise FilesystemError(
                    "Conditional destination parent mode changed during operation"
                )
            os.utime(descriptor, ns=(atime_ns, mtime_ns))
            restored = os.fstat(descriptor)
            if (
                restored.st_atime_ns != atime_ns
                or restored.st_mtime_ns != mtime_ns
            ):
                raise FilesystemError(
                    "Conditional destination parent metadata could not be restored"
                )

    def close(self) -> None:
        descriptors = [
            *{
                descriptor
                for descriptor, _name in self.destinations.values()
            },
            self.staged,
            self.modules,
            self.capture,
        ]
        for descriptor in descriptors:
            os.close(descriptor)


def _prepare_conditional_state_descriptors(
    root: Path,
    state_dir: Path,
    destinations: Iterable[Path],
) -> _ConditionalBatchDescriptors:
    """Retain trusted state and live destination-parent descriptors."""

    state_descriptor = _open_contained_directory_descriptor(root, state_dir.name)
    opened: list[int] = []
    try:
        try:
            try:
                os.mkdir("captures", mode=0o700, dir_fd=state_descriptor)
            except FileExistsError:
                pass
            try:
                os.mkdir("modules", mode=0o700, dir_fd=state_descriptor)
            except FileExistsError:
                pass
            capture_descriptor = os.open(
                "captures",
                _directory_open_flags(),
                dir_fd=state_descriptor,
            )
            opened.append(capture_descriptor)
            modules_descriptor = os.open(
                "modules",
                _directory_open_flags(),
                dir_fd=state_descriptor,
            )
            opened.append(modules_descriptor)
            staged_descriptor = os.open(
                "template",
                _directory_open_flags(),
                dir_fd=state_descriptor,
            )
            opened.append(staged_descriptor)
        finally:
            os.close(state_descriptor)
    except BaseException:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise

    destination_descriptors: Dict[str, tuple[int, str]] = {}
    parent_descriptors: Dict[str, int] = {}
    parent_metadata: Dict[int, tuple[int, int, int]] = {}
    try:
        for destination in destinations:
            parent_key = str(destination.parent)
            parent_descriptor = parent_descriptors.get(parent_key)
            if parent_descriptor is None:
                parent_descriptor, name = _open_contained_parent_descriptor(
                    root, destination
                )
                parent_descriptors[parent_key] = parent_descriptor
                metadata = os.fstat(parent_descriptor)
                parent_metadata[parent_descriptor] = (
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns,
                )
            else:
                name = destination.name
            destination_descriptors[str(destination)] = (parent_descriptor, name)
        return _ConditionalBatchDescriptors(
            capture_descriptor,
            modules_descriptor,
            staged_descriptor,
            destination_descriptors,
            parent_metadata,
        )
    except BaseException:
        for descriptor in parent_descriptors.values():
            os.close(descriptor)
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise


def _open_conditional_state_descriptors(raw: Dict[str, object]) -> tuple[int, int]:
    restore = Path(str(raw["restore"]))
    state_dir = restore.parent.parent
    root = state_dir.parent
    state_descriptor = _open_contained_directory_descriptor(root, state_dir.name)
    try:
        capture_descriptor = os.open(
            "captures",
            _directory_open_flags(),
            dir_fd=state_descriptor,
        )
        try:
            modules_descriptor = os.open(
                "modules",
                _directory_open_flags(),
                dir_fd=state_descriptor,
            )
        except BaseException:
            os.close(capture_descriptor)
            raise
        return capture_descriptor, modules_descriptor
    finally:
        os.close(state_descriptor)


def _relative_kind(name: str, descriptor: int) -> str | None:
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    return "other"


def _relative_same_identity(
    left_name: str,
    left_descriptor: int,
    right_name: str,
    right_descriptor: int,
) -> bool:
    try:
        left = os.stat(
            left_name,
            dir_fd=left_descriptor,
            follow_symlinks=False,
        )
        right = os.stat(
            right_name,
            dir_fd=right_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _read_relative_regular_bytes(name: str, descriptor: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(name, flags, dir_fd=descriptor)
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FilesystemError("Conditional capture is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_size != after.st_size
        ):
            raise ConcurrentSourceMutation(
                "Conditional capture changed while it was inspected"
            )
        if before.st_atime_ns != after.st_atime_ns:
            os.utime(
                file_descriptor,
                ns=(before.st_atime_ns, after.st_mtime_ns),
            )
        return b"".join(chunks), before
    finally:
        os.close(file_descriptor)


def _regular_entry_hash(content: bytes, mode: int) -> str:
    digest = hashlib.sha256()
    digest.update(b".\0file\0")
    digest.update(f"{mode:o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def _relative_regular_matches_exact(
    name: str,
    descriptor: int,
    content: bytes,
    metadata: os.stat_result,
) -> bool:
    try:
        current_content, current_metadata = _read_relative_regular_bytes(
            name, descriptor
        )
    except (OSError, FilesystemError):
        return False
    return (
        current_content == content
        and stat.S_IMODE(current_metadata.st_mode)
        == stat.S_IMODE(metadata.st_mode)
        and current_metadata.st_atime_ns == metadata.st_atime_ns
        and current_metadata.st_mtime_ns == metadata.st_mtime_ns
    )


def _relative_link_no_clobber(
    source_name: str,
    source_descriptor: int,
    destination_name: str,
    destination_descriptor: int,
) -> bool:
    try:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=destination_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    return True


def _publish_relative_regular_copy_no_clobber(
    source_name: str,
    source_descriptor: int,
    destination_name: str,
    destination_descriptor: int,
) -> tuple[bool, Optional[BaseException]]:
    """Publish an exact independent copy while retaining immutable authority."""

    content, metadata = _read_relative_regular_bytes(
        source_name, source_descriptor
    )
    candidate_name = f".restore-{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    candidate_descriptor = os.open(
        candidate_name,
        flags,
        0o600,
        dir_fd=source_descriptor,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(candidate_descriptor, view)
            if written <= 0:
                raise FilesystemError(
                    "Conditional restore candidate could not be written"
                )
            view = view[written:]
        mode = stat.S_IMODE(metadata.st_mode)
        os.fchmod(candidate_descriptor, mode)
        os.utime(
            candidate_descriptor,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
        )
        os.fsync(candidate_descriptor)
    finally:
        os.close(candidate_descriptor)
    try:
        if not _relative_regular_matches_exact(
            candidate_name,
            source_descriptor,
            content,
            metadata,
        ):
            raise FilesystemError(
                "Conditional restore candidate did not preserve exact metadata"
            )
        published = _relative_link_no_clobber(
            candidate_name,
            source_descriptor,
            destination_name,
            destination_descriptor,
        )
    except BaseException:
        try:
            os.unlink(candidate_name, dir_fd=source_descriptor)
        except BaseException:
            pass
        raise
    cleanup_error: Optional[BaseException] = None
    try:
        os.unlink(candidate_name, dir_fd=source_descriptor)
    except BaseException as exc:
        cleanup_error = exc
    return published, cleanup_error


def _restore_relative_regular_no_clobber(
    restore_name: str,
    modules_descriptor: int,
    destination_name: str,
    destination_descriptor: int,
) -> None:
    _relative_link_no_clobber(
        restore_name,
        modules_descriptor,
        destination_name,
        destination_descriptor,
    )
    os.unlink(restore_name, dir_fd=modules_descriptor)


def _reconcile_conditional_replacement_descriptor_relative(
    raw: Dict[str, object],
    capture_descriptor: int,
    modules_descriptor: int,
    destination_authority: tuple[int, str] | None = None,
) -> bool:
    destination = Path(str(raw["path"]))
    state_dir = Path(str(raw["restore"])).parent.parent
    root = state_dir.parent
    owns_destination_descriptor = destination_authority is None
    destination_descriptor, destination_name = (
        _open_contained_parent_descriptor(root, destination)
        if destination_authority is None
        else destination_authority
    )
    capture_name = Path(str(raw["capture"])).name
    restore_name = Path(str(raw["restore"])).name
    try:
        if _relative_kind(capture_name, capture_descriptor) is None:
            try:
                os.rename(
                    destination_name,
                    capture_name,
                    src_dir_fd=destination_descriptor,
                    dst_dir_fd=capture_descriptor,
                )
            except FileNotFoundError:
                _relative_link_no_clobber(
                    restore_name,
                    modules_descriptor,
                    destination_name,
                    destination_descriptor,
                )
                return True

        capture_kind = _relative_kind(capture_name, capture_descriptor)
        if capture_kind is None:
            _relative_link_no_clobber(
                restore_name,
                modules_descriptor,
                destination_name,
                destination_descriptor,
            )
            return True

        capture_metadata = os.stat(
            capture_name,
            dir_fd=capture_descriptor,
            follow_symlinks=False,
        )
        published_identity = (
            capture_metadata.st_dev == cast(int, raw["published_dev"])
            and capture_metadata.st_ino == cast(int, raw["published_ino"])
        )
        published_content = False
        if capture_kind == "file":
            content, metadata = _read_relative_regular_bytes(
                capture_name, capture_descriptor
            )
            published_content = (
                hashlib.sha256(content).hexdigest() == raw["published_sha256"]
                and stat.S_IMODE(metadata.st_mode)
                == cast(int, raw["published_mode"])
            )

        if published_identity and published_content:
            os.unlink(capture_name, dir_fd=capture_descriptor)
            _relative_link_no_clobber(
                restore_name,
                modules_descriptor,
                destination_name,
                destination_descriptor,
            )
            return True

        if _relative_kind(destination_name, destination_descriptor) is not None:
            return _relative_same_identity(
                destination_name,
                destination_descriptor,
                capture_name,
                capture_descriptor,
            )
        if not _relative_link_no_clobber(
            capture_name,
            capture_descriptor,
            destination_name,
            destination_descriptor,
        ):
            return _relative_same_identity(
                destination_name,
                destination_descriptor,
                capture_name,
                capture_descriptor,
            )
        return True
    finally:
        if owns_destination_descriptor:
            os.close(destination_descriptor)


def _reconcile_conditional_replacement(
    raw: Dict[str, object],
    descriptors: tuple[int, int] | None = None,
    destination_authority: tuple[int, str] | None = None,
) -> bool:
    """Restore a conditionally published path without discarding a newer entry.

    The journal authorizes ``capture`` before this helper atomically retains the
    current destination.  The retained object, rather than a pathname sample,
    decides whether the generator publication may be discarded.  ``False``
    means that both a captured external entry and a newer destination remain
    available under the durable transaction authority.
    """

    if not _descriptor_relative_io_supported():
        raise FilesystemError(
            "Conditional replacement recovery requires descriptor-relative "
            "no-follow filesystem operations"
        )

    owned_descriptors = descriptors is None
    active_descriptors = (
        _open_conditional_state_descriptors(raw)
        if descriptors is None
        else descriptors
    )
    try:
        return _reconcile_conditional_replacement_descriptor_relative(
            raw,
            active_descriptors[0],
            active_descriptors[1],
            destination_authority,
        )
    finally:
        if owned_descriptors:
            os.close(active_descriptors[0])
            os.close(active_descriptors[1])


@dataclass(frozen=True)
class _PreparedRegularReplacement:
    staged: Path
    destination: Path
    baseline_sha256: str
    baseline_mode: int
    published_sha256: str
    published_entry_hash: str
    published_mode: int
    published_dev: int
    published_ino: int


@dataclass(frozen=True)
class _RetainedRegularReplacement:
    entry: Dict[str, object]
    restore: Path
    prepared: _PreparedRegularReplacement


def _exact_entry_state(path: Path) -> tuple[tuple[object, ...], ...]:
    """Capture exact no-follow content and supported metadata for one entry tree."""

    kind = entry_kind(path)
    if kind is None:
        return ((".", None),)
    paths = [path]
    if kind == "directory":
        paths.extend(iter_tree_no_follow(path))
    state: list[tuple[object, ...]] = []
    for current in paths:
        relative = "." if current == path else current.relative_to(path).as_posix()
        before = current.lstat()
        current_kind = entry_kind(current)
        digest = _hash_path(current) if current_kind != "directory" else None
        after = current.lstat()
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or (
                (
                    stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode)
                    or before.st_mode & stat.S_IWRITE != after.st_mode & stat.S_IWRITE
                )
                if os.name == "nt"
                else before.st_mode != after.st_mode
            )
            or (
                (before.st_mtime_ns // 100 != after.st_mtime_ns // 100)
                if os.name == "nt"
                else before.st_mtime_ns != after.st_mtime_ns
            )
            or before.st_size != after.st_size
        ):
            raise ConcurrentSourceMutation(
                f"Source entry changed while it was captured: {current}"
            )
        mode = (
            (stat.S_IWRITE if after.st_mode & stat.S_IWRITE else 0) | stat.S_IREAD
            if os.name == "nt"
            else stat.S_IMODE(after.st_mode)
        )
        mtime_ns = (
            (after.st_mtime_ns // 100) * 100
            if os.name == "nt"
            else after.st_mtime_ns
        )
        state.append(
            (
                relative,
                current_kind,
                mode,
                mtime_ns,
                after.st_size,
                digest,
            )
        )
    return tuple(state)


def _copy_verified_entry(
    source: Path,
    destination: Path,
    *,
    attempts: int = 3,
) -> tuple[tuple[object, ...], ...]:
    """Copy one stable entry into a fresh recovery slot.

    The source is sampled before and after the copy, and the recovery payload
    must match that exact state. A transient concurrent edit is retried in a
    new slot owned by the caller; no previously-authorized payload is touched.
    """

    if attempts < 1:
        raise ValueError("attempts must be positive")
    last_error: BaseException | None = None
    for _ in range(attempts):
        if lexists(destination):
            raise ConcurrentSourceMutation(
                "Recovery destination was occupied while a stable copy was created: "
                f"{destination}"
            )
        try:
            before = _exact_entry_state(source)
            if before != ((".", None),):
                copy_entry_no_follow(source, destination)
            after = _exact_entry_state(source)
            copied = _exact_entry_state(destination)
            if before != after or copied != after:
                raise ConcurrentSourceMutation(
                    f"Source entry changed while its recovery copy was created: {source}"
                )
            return copied
        except ConcurrentSourceMutation as exc:
            last_error = exc
            if lexists(destination):
                # There is no portable atomic conditional unlink for the live
                # pathname. It may already be a concurrent editor replacement,
                # so retain it as truthful recovery residue instead of deleting.
                raise
        except BaseException:
            raise
    assert last_error is not None
    raise last_error


def _write_json_atomic(path: Path, value: Dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
            closefd=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _managed_entry(root: Path, path: Path) -> Path:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(root)
        candidate.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise FilesystemError(
            f"Transaction target escapes the plugin root: {candidate}"
        ) from exc
    return candidate


def _inside(root: Path, path: Path) -> bool:
    try:
        _managed_entry(root, path)
        return True
    except FilesystemError:
        return False


def _capture_live_tree_metadata(
    root: Path,
) -> Dict[str, tuple[str, int, int, int]]:
    """Capture entry metadata before inventories or copies can advance atime."""

    captured: Dict[str, tuple[str, int, int, int]] = {}
    root_stat = root.lstat()
    captured["."] = (
        "directory",
        stat.S_IMODE(root_stat.st_mode),
        root_stat.st_atime_ns,
        root_stat.st_mtime_ns,
    )
    pending: list[Path] = [root]
    while pending:
        directory = pending.pop()
        try:
            children, _before = _observed_directory_entries(directory)
        except OSError as exc:
            raise FilesystemError(f"Cannot inspect source directory {directory}: {exc}") from exc
        for child in children:
            path = Path(child.path)
            child_relative = path.relative_to(root).as_posix()
            if _is_build_or_cache_path(child_relative):
                continue
            metadata = child.stat(follow_symlinks=False)
            kind = child.kind
            captured[child_relative] = (
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_atime_ns,
                metadata.st_mtime_ns,
            )
            if kind == "directory":
                pending.append(path)
    return captured


def _validated_transaction_identifier(data: Dict[str, object]) -> str:
    return validated_transaction_identifier(data)


def _entry_digest(entries: object) -> str:
    return _registry_entry_digest(entries)


def _recovery_registry() -> Path:
    identity = str(os.getuid()) if hasattr(os, "getuid") else "user"
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    return private_recovery_registry(
        Path(tempfile.gettempdir()),
        identity=identity,
        effective_uid=effective_uid,
        windows=os.name == "nt",
    )


def _recovery_pointer_path(root: Path) -> Path:
    return recovery_pointer_path(root, _recovery_registry())


def _publish_recovery_pointer(
    root: Path,
    data: Dict[str, object],
    recovery_path: Path,
    outcome: str,
    *,
    bundle_id: str,
    manifest_sha256: str,
) -> Path:
    pointer = _recovery_pointer_path(root)
    _write_json_atomic(
        pointer,
        recovery_pointer_payload(
            root,
            data,
            recovery_path,
            outcome,
            bundle_id=bundle_id,
            manifest_sha256=manifest_sha256,
        ),
    )
    return pointer


def _load_recovery_pointer(root: Path) -> tuple[Path, Dict[str, object]] | None:
    pointer = _recovery_pointer_path(root)
    kind = entry_kind(pointer)
    if kind is None:
        return None
    if kind != "file":
        raise FilesystemError("Transaction recovery pointer is unsafe")
    pointer_metadata = pointer.lstat()
    if (
        (
            hasattr(os, "geteuid")
            and getattr(pointer_metadata, "st_uid", -1) != os.geteuid()
        )
        or (os.name != "nt" and stat.S_IMODE(pointer_metadata.st_mode) & 0o077)
    ):
        raise FilesystemError(
            "Transaction recovery pointer is not private to the current user"
        )
    raw = _read_json_regular_no_follow(pointer, "Transaction recovery pointer")
    parsed = parse_recovery_pointer(raw, root)
    validate_transaction_metadata_recovery(
        parsed.recovery_path,
        root,
        transaction_id=parsed.transaction_id,
        outcome=parsed.outcome,
        bundle_id=parsed.bundle_id,
        manifest_sha256=parsed.manifest_sha256,
    )
    assert isinstance(raw, dict)
    return pointer, raw


def _complete_recovery_pointer(
    root: Path,
    *,
    checkpoint: Optional[Callable[[str], None]] = None,
) -> str:
    loaded = _load_recovery_pointer(root)
    if loaded is None:
        raise FilesystemError("Transaction recovery pointer is unavailable")
    pointer, raw = loaded
    identifier = str(raw["transaction_id"])
    recovery_path = Path(str(raw["recovery_path"]))
    metadata = validate_transaction_metadata_recovery(
        recovery_path,
        root,
        transaction_id=identifier,
        outcome=str(raw["outcome"]),
        bundle_id=str(raw["bundle_id"]),
        manifest_sha256=str(raw["manifest_sha256"]),
    )
    journal = root / JOURNAL_NAME
    if entry_kind(journal) is not None:
        if entry_kind(journal) != "file":
            raise FilesystemError("Transaction journal is unsafe")
        journal_data = _read_json_regular_no_follow(journal, "Transaction journal")
        if not isinstance(journal_data, dict) or journal_data.get("id") != identifier:
            raise FilesystemError("Transaction recovery pointer disagrees with journal")
        journal.unlink()
    if checkpoint is not None:
        checkpoint("after_abandon_journal_unlink")
    state_dir = root / f"{STATE_PREFIX}{identifier}"
    state_kind = entry_kind(state_dir)
    if state_kind is not None:
        if state_kind != "directory":
            raise FilesystemError("Transaction state directory is unsafe")
        shutil.rmtree(state_dir, ignore_errors=False)
    if checkpoint is not None:
        checkpoint("after_abandon_state_removal")
    failures = filesystem_ops.restore_protected_directory_metadata(root, metadata)
    if failures:
        raise TransactionCleanupError(
            "Transaction metadata recovery is incomplete: " + ", ".join(failures),
            recovery_path=recovery_path,
        )
    try:
        pointer.unlink()
        shutil.rmtree(recovery_path, ignore_errors=False)
    except BaseException as exc:
        raise TransactionCleanupError(
            f"Durable recovery authority cleanup is incomplete: {exc}",
            recovery_path=recovery_path,
        ) from exc
    return str(raw["outcome"])


def _validate_absolute_entry_path(
    boundary: Path,
    value: object,
    *,
    allow_missing_ancestors: bool,
) -> Path:
    windows = PureWindowsPath(value) if isinstance(value, str) else None
    if (
        not isinstance(value, str)
        or not value
        or (
            os.name != "nt"
            and ("\\" in value or (windows is not None and windows.drive))
        )
    ):
        raise FilesystemError("Transaction entry path is invalid")
    path = Path(value)
    if not path.is_absolute() or path == boundary or not _inside(boundary, path):
        raise FilesystemError(
            f"Transaction entry path escapes its recovery boundary: {value}"
        )
    relative = path.relative_to(boundary)
    validate_persisted_relative_path(relative.as_posix())
    missing = False
    current = boundary
    for part in relative.parts[:-1]:
        current = current / part
        kind = entry_kind(current)
        if kind == "symlink":
            raise FilesystemError(
                f"Transaction entry path has a symbolic-link ancestor: {value}"
            )
        if kind is None:
            if not allow_missing_ancestors:
                raise FilesystemError(
                    f"Transaction entry path has a missing ancestor: {value}"
                )
            missing = True
        elif missing or kind != "directory":
            raise FilesystemError(
                f"Transaction entry path has a non-directory ancestor: {value}"
            )
    return path


def _entry_authority_name(entries_digest: str) -> str:
    return f"{ENTRY_AUTHORITY_PREFIX}{entries_digest}.json"


def _write_entry_authority(
    state_dir: Path,
    identifier: str,
    entries: object,
    authority_name: str,
) -> None:
    if entry_kind(state_dir) != "directory":
        raise FilesystemError("Transaction state authority directory is unavailable")
    _write_json_atomic(
        state_dir / authority_name,
        {
            "schema_version": 1,
            "transaction_id": identifier,
            "entries": entries,
            "sha256": _entry_digest(entries),
        },
    )


def _write_conditional_conflict_authority(
    modules_descriptor: int,
    identifier: str,
    entries: object,
    destination_parents: object,
) -> None:
    """Persist a fail-closed conflict marker without touching the plugin root."""

    value = {
        "schema_version": 1,
        "transaction_id": identifier,
        "entries_sha256": _entry_digest(entries),
        "destination_parents_sha256": _entry_digest(destination_parents),
    }
    name = PurePosixPath(CONDITIONAL_CONFLICT_AUTHORITY_NAME).name
    temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=modules_descriptor)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n", closefd=False) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            name,
            src_dir_fd=modules_descriptor,
            dst_dir_fd=modules_descriptor,
        )
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=modules_descriptor)
        except FileNotFoundError:
            pass


def _write_conditional_retention_authority(
    modules_descriptor: int,
    identifier: str,
    destination_parents: object,
) -> None:
    """Arm fail-closed recovery before the first conditional retention."""

    value = {
        "schema_version": 1,
        "transaction_id": identifier,
        "destination_parents_sha256": _entry_digest(destination_parents),
    }
    name = PurePosixPath(CONDITIONAL_RETENTION_AUTHORITY_NAME).name
    temporary_name = f".{name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=modules_descriptor)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
            closefd=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            name,
            src_dir_fd=modules_descriptor,
            dst_dir_fd=modules_descriptor,
        )
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=modules_descriptor)
        except FileNotFoundError:
            pass


def _conditional_destination_parents_match(
    root: Path,
    data: Dict[str, object],
) -> bool:
    parent_authority = data.get("conditional_destination_parents")
    if not isinstance(parent_authority, list) or not parent_authority:
        raise FilesystemError("Transaction destination-parent authority is invalid")
    seen_parents: set[str] = set()
    for raw_parent in parent_authority:
        if not isinstance(raw_parent, dict) or set(raw_parent) != {
            "path",
            "dev",
            "ino",
        }:
            raise FilesystemError(
                "Transaction destination-parent authority is invalid"
            )
        relative = raw_parent.get("path")
        expected_dev = raw_parent.get("dev")
        expected_ino = raw_parent.get("ino")
        if (
            not isinstance(relative, str)
            or relative in seen_parents
            or not isinstance(expected_dev, int)
            or isinstance(expected_dev, bool)
            or expected_dev < 0
            or not isinstance(expected_ino, int)
            or isinstance(expected_ino, bool)
            or expected_ino < 0
        ):
            raise FilesystemError(
                "Transaction destination-parent authority is invalid"
            )
        parsed = validate_persisted_relative_path(relative)
        try:
            descriptor = _open_contained_directory_descriptor(
                root, parsed.as_posix()
            )
        except (OSError, FilesystemError):
            return False
        try:
            current = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if current.st_dev != expected_dev or current.st_ino != expected_ino:
            return False
        seen_parents.add(relative)
    return True


def _conditional_conflict_is_durable(
    root: Path,
    data: Dict[str, object],
    *,
    allow_parent_fallback: bool = True,
) -> bool:
    authority_name = data.get("conditional_conflict_authority")
    if authority_name != CONDITIONAL_CONFLICT_AUTHORITY_NAME:
        return False
    assert isinstance(authority_name, str)
    identifier = _validated_transaction_identifier(data)
    authority, retention_authority = _read_conditional_authorities(
        root,
        identifier,
        authority_name,
    )
    if authority is None:
        return _markerless_conditional_conflict_is_durable(
            root,
            data,
            identifier,
            retention_authority,
            allow_parent_fallback=allow_parent_fallback,
        )
    _validate_conditional_conflict_authority(authority, data, identifier)
    return True


def _read_conditional_authorities(
    root: Path,
    identifier: str,
    authority_name: str,
) -> tuple[object, object]:
    state_descriptor = _open_contained_directory_descriptor(
        root, f"{STATE_PREFIX}{identifier}"
    )
    try:
        try:
            modules_descriptor = os.open(
                "modules", _directory_open_flags(), dir_fd=state_descriptor
            )
        except FileNotFoundError:
            return None, None
    finally:
        os.close(state_descriptor)
    try:
        marker_name = PurePosixPath(authority_name).name
        authority = _read_optional_relative_json_authority(
            modules_descriptor,
            marker_name,
            "Transaction conflict authority",
        )
        retention_name = PurePosixPath(
            CONDITIONAL_RETENTION_AUTHORITY_NAME
        ).name
        retention_authority = _read_optional_relative_json_authority(
            modules_descriptor,
            retention_name,
            "Transaction retention authority",
        )
    finally:
        os.close(modules_descriptor)
    return authority, retention_authority


def _read_optional_relative_json_authority(
    descriptor: int,
    name: str,
    description: str,
) -> object:
    kind = _relative_kind(name, descriptor)
    if kind is None:
        return None
    if kind != "file":
        raise FilesystemError(f"{description} is unsafe")
    content, _metadata = _read_relative_regular_bytes(name, descriptor)
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FilesystemError(f"{description} is unreadable") from exc


def _markerless_conditional_conflict_is_durable(
    root: Path,
    data: Dict[str, object],
    identifier: str,
    retention_authority: object,
    *,
    allow_parent_fallback: bool,
) -> bool:
    if not allow_parent_fallback:
        return False
    parent_authority = data.get("conditional_destination_parents")
    if (
        data.get("phase") != "stage"
        or not isinstance(parent_authority, list)
        or not parent_authority
        or not isinstance(retention_authority, dict)
        or retention_authority.get("schema_version") != 1
        or retention_authority.get("transaction_id") != identifier
        or retention_authority.get("destination_parents_sha256")
        != _entry_digest(parent_authority)
    ):
        return False
    _validate_transaction_entries(root, data)
    entries = data.get("entries", [])
    return isinstance(entries, list) and any(
        isinstance(raw, dict)
        and raw.get("kind") == "replace"
        and raw.get("existed") is True
        and raw.get("entry_type") == "file"
        and entry_kind(Path(str(raw.get("restore", "")))) == "file"
        for raw in entries
    )


def _validate_conditional_conflict_authority(
    authority: object,
    data: Dict[str, object],
    identifier: str,
) -> None:
    entries = data.get("entries", [])
    destination_parents = data.get("conditional_destination_parents", [])
    if (
        not isinstance(authority, dict)
        or authority.get("schema_version") != 1
        or authority.get("transaction_id") != identifier
        or authority.get("entries_sha256") != _entry_digest(entries)
        or authority.get("destination_parents_sha256")
        != _entry_digest(destination_parents)
    ):
        raise FilesystemError("Transaction conflict authority is invalid")


def _conditional_conflict_is_resolved(
    root: Path,
    data: Dict[str, object],
) -> bool:
    """Return whether every retained destination is again the exact baseline."""

    _validate_transaction_entries(root, data)
    if not _conditional_destination_parents_match(root, data):
        return False
    entries = data.get("entries", [])
    if not isinstance(entries, list) or not entries:
        raise FilesystemError("Transaction conflict entries are invalid")
    for raw in entries:
        if (
            not isinstance(raw, dict)
            or raw.get("kind") != "replace"
            or raw.get("existed") is not True
            or raw.get("entry_type") != "file"
        ):
            raise FilesystemError("Transaction conflict entry is invalid")
        destination = _validate_absolute_entry_path(
            root,
            raw.get("path"),
            allow_missing_ancestors=False,
        )
        restore = Path(str(raw.get("restore", "")))
        if entry_kind(destination) != "file":
            return False
        restore_kind = entry_kind(restore)
        if restore_kind is None:
            raise FilesystemError(
                "Transaction conflict restore payload is unavailable"
            )
        if restore_kind != "file" or _hash_path(restore) != raw.get("hash"):
            raise FilesystemError("Transaction conflict restore payload is invalid")
        live_content, live_metadata = read_contained_regular_bytes_no_follow(
            root, destination
        )
        restore_content, restore_metadata = read_contained_regular_bytes_no_follow(
            root, restore
        )
        if (
            live_content != restore_content
            or (
                (
                    live_metadata.st_mode & stat.S_IWRITE
                    != restore_metadata.st_mode & stat.S_IWRITE
                )
                if _windows_host()
                else stat.S_IMODE(live_metadata.st_mode)
                != stat.S_IMODE(restore_metadata.st_mode)
            )
            or (
                (live_metadata.st_atime_ns // 100 != restore_metadata.st_atime_ns // 100)
                if _windows_host()
                else live_metadata.st_atime_ns != restore_metadata.st_atime_ns
            )
            or (
                (live_metadata.st_mtime_ns // 100 != restore_metadata.st_mtime_ns // 100)
                if _windows_host()
                else live_metadata.st_mtime_ns != restore_metadata.st_mtime_ns
            )
        ):
            return False
    return True


def _finish_conditional_conflict(
    root: Path,
    journal: Path,
    data: Dict[str, object],
) -> None:
    """Remove resolved conflict authority without replaying stale metadata."""

    root_metadata = root.lstat()
    metadata = {
        ".": (
            stat.S_IMODE(root_metadata.st_mode),
            root_metadata.st_atime_ns,
            root_metadata.st_mtime_ns,
        )
    }
    recovery_path, bundle_id, manifest_sha256 = retain_directory_metadata_recovery(
        root,
        metadata,
        transaction_id=_validated_transaction_identifier(data),
        outcome="abandon",
    )
    _publish_recovery_pointer(
        root,
        data,
        recovery_path,
        "abandon",
        bundle_id=bundle_id,
        manifest_sha256=manifest_sha256,
    )
    _complete_recovery_pointer(root)


def _read_json_regular_no_follow(path: Path, description: str) -> object:
    """Read a required regular JSON file without following its final entry."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FilesystemError(f"{description} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FilesystemError(f"{description} is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise FilesystemError(f"{description} is unreadable") from exc
    finally:
        os.close(descriptor)


def _write_transaction_data(
    root: Path,
    journal: Path,
    data: Dict[str, object],
    *,
    authorize_entries: bool = True,
) -> os.stat_result:
    identifier = _validated_transaction_identifier(data)
    entries = data.get("entries", [])
    digest = _entry_digest(entries)
    data["entries_sha256"] = digest
    if authorize_entries:
        authority_name = _entry_authority_name(digest)
        data["entry_authority"] = authority_name
        _write_entry_authority(
            root / f"{STATE_PREFIX}{identifier}",
            identifier,
            entries,
            authority_name,
        )
    elif data.get("entry_authority") != _entry_authority_name(digest):
        raise FilesystemError("Transaction entry authority is not current")
    _write_json_atomic(journal, data)
    # Return the root state observed by the durable publication itself.  A
    # caller must not take its first publication marker after this function
    # returns: an external metadata handoff in that gap would otherwise be
    # mistaken for transaction-owned journal churn.
    return root.lstat()


def _validate_transaction_entries(root: Path, data: Dict[str, object]) -> None:
    """Validate every persisted transaction path before recovery mutates disk."""

    identifier = _validated_transaction_identifier(data)
    state_dir = root / f"{STATE_PREFIX}{identifier}"
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise FilesystemError("Transaction entries are invalid")
    allowed_kinds = {
        "snapshot",
        "replace",
        "detach",
        "covered_replace",
        "covered_detach",
        "created",
        "created_directory",
        "preserved_external",
        "conditional_replace",
    }
    expected_digest = data.get("entries_sha256")
    if not isinstance(expected_digest, str) or expected_digest != _entry_digest(entries):
        raise FilesystemError("Transaction entry authority digest is invalid")
    if entry_kind(state_dir) != "directory":
        raise FilesystemError("Transaction entry authority directory is unavailable")
    authority_name = data.get("entry_authority", ENTRY_AUTHORITY_NAME)
    if not isinstance(authority_name, str) or authority_name not in {
        ENTRY_AUTHORITY_NAME,
        _entry_authority_name(expected_digest),
    }:
        raise FilesystemError("Transaction entry authority name is invalid")
    authority_path = state_dir / authority_name
    if entry_kind(authority_path) != "file":
        raise FilesystemError("Transaction entry authority is unavailable or unsafe")
    authority = _read_json_regular_no_follow(
        authority_path, "Transaction entry authority"
    )
    if (
        not isinstance(authority, dict)
        or authority.get("schema_version") != 1
        or authority.get("transaction_id") != identifier
        or authority.get("entries") != entries
        or authority.get("sha256") != expected_digest
    ):
        raise FilesystemError("Transaction entries disagree with their authority")

    primary_paths: list[tuple[Path, str]] = []
    covered_paths: set[Path] = set()
    covering_entries: list[tuple[Path, Path]] = []
    for index, raw in enumerate(entries):
        if (
            not isinstance(raw, dict)
            or raw.get("kind") not in allowed_kinds
            or not _entry_kind_fields_are_valid(raw)
        ):
            raise FilesystemError("Transaction entry is invalid")
        path = _validate_absolute_entry_path(
            root, raw.get("path"), allow_missing_ancestors=True
        )
        restore = _validate_absolute_entry_path(
            state_dir, raw.get("restore"), allow_missing_ancestors=True
        )
        kind = str(raw["kind"])
        if kind in {"snapshot", "preserved_external"}:
            restore_root = state_dir / "restore"
            canonical_name = str(index)
            version_prefix = f"{index}-adopt-"
            attempt_prefix = f"{index}-attempt-"
            restore_name = restore.name
            version = restore_name[len(version_prefix):]
            attempt = restore_name[len(attempt_prefix):]
            valid_version = (
                restore.parent == restore_root
                and restore_name.startswith(version_prefix)
                and len(version) == 32
                and all(character in "0123456789abcdef" for character in version)
            )
            valid_attempt = (
                restore.parent == restore_root
                and restore_name.startswith(attempt_prefix)
                and len(attempt) == 32
                and all(character in "0123456789abcdef" for character in attempt)
            )
            if not (
                restore == restore_root / canonical_name
                or valid_version
                or valid_attempt
            ):
                raise FilesystemError("Transaction restore slot is noncanonical")
            expected_restore = restore
        elif kind in {"replace", "detach", "conditional_replace"}:
            expected_restore = state_dir / "modules" / str(index)
        elif kind in {"created", "created_directory"}:
            expected_restore = state_dir / "unused" / str(index)
        else:
            covering = [
                (snapshot_path, snapshot_restore)
                for snapshot_path, snapshot_restore in covering_entries
                if path == snapshot_path or snapshot_path in path.parents
            ]
            deepest = max((len(owner.parts) for owner, _ in covering), default=-1)
            canonical_owners = [
                (owner, owner_restore)
                for owner, owner_restore in covering
                if len(owner.parts) == deepest
            ]
            if len(canonical_owners) != 1 or restore != canonical_owners[0][1]:
                raise FilesystemError(
                    "Covered transaction entry lacks one canonical snapshot owner"
                )
            if path in covered_paths:
                raise FilesystemError("Covered transaction entry path is duplicated")
            covered_paths.add(path)
            continue
        if restore != expected_restore:
            raise FilesystemError("Transaction restore slot is noncanonical")
        if kind == "conditional_replace":
            capture = _validate_absolute_entry_path(
                state_dir,
                raw.get("capture"),
                allow_missing_ancestors=True,
            )
            if capture != state_dir / "captures" / str(index):
                raise FilesystemError(
                    "Conditional replacement capture slot is noncanonical"
                )
        if kind == "snapshot":
            if any(
                owned_kind != "created_directory"
                and (path == owned or path in owned.parents or owned in path.parents)
                for owned, owned_kind in primary_paths
            ):
                raise FilesystemError("Transaction snapshot paths overlap")
            covering_entries.append((path, restore))
        elif kind != "created_directory":
            overlaps = [
                (owned, owned_kind)
                for owned, owned_kind in primary_paths
                if owned_kind != "created_directory"
                and (path == owned or path in owned.parents or owned in path.parents)
            ]
            composite_replace = kind in {"replace", "conditional_replace"} and all(
                owned_kind in {"snapshot", "replace"} and (
                    owned == path
                    or owned in path.parents
                    or path in owned.parents
                )
                for owned, owned_kind in overlaps
            )
            if overlaps and not composite_replace:
                detail = ", ".join(
                    f"{owned_kind}:{owned}" for owned, owned_kind in overlaps
                )
                raise FilesystemError(
                    f"Transaction entry paths overlap: {kind}:{path} with {detail}"
                )
            if kind in {"replace", "conditional_replace"}:
                covering_entries.append((path, restore))
        primary_paths.append((path, kind))


def _validate_restore_payloads(root: Path, data: Dict[str, object]) -> None:
    """Validate the complete restore set before rollback changes a live entry."""

    identifier = _validated_transaction_identifier(data)
    state_dir = root / f"{STATE_PREFIX}{identifier}"
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise FilesystemError("Transaction entries are invalid")
    for raw in entries:
        assert isinstance(raw, dict)  # structural validation ran first
        kind = str(raw["kind"])
        if kind in {
            "covered_replace",
            "covered_detach",
            "created",
            "created_directory",
            "preserved_external",
        }:
            continue
        path = Path(str(raw["path"]))
        restore = Path(str(raw["restore"]))
        if not _inside(state_dir, restore):
            raise FilesystemError(f"Restore payload escapes transaction state: {restore}")
        existed = bool(raw["existed"])
        restore_kind = entry_kind(restore)
        if not existed:
            if restore_kind is not None:
                raise FilesystemError(
                    f"Unexpected restore payload exists for a new entry: {restore}"
                )
            continue
        expected_kind = str(raw["entry_type"])
        expected_hash = str(raw["hash"])
        if restore_kind is None:
            # A crash may occur after an atomic restore move but before the
            # journal records completion. Accept only the exact live baseline.
            if (
                entry_kind(path) == expected_kind
                and _hash_path(path) == expected_hash
            ):
                continue
            raise FilesystemError(f"Restore data is unavailable: {restore}")
        if restore_kind != expected_kind:
            raise FilesystemError(f"Restore payload kind mismatch: {restore}")
        if _hash_path(restore) != expected_hash:
            raise FilesystemError(f"Restore payload hash mismatch: {restore}")


@dataclass(frozen=True)
class RecoveryOutcome:
    rollback: RollbackResult
    warning: Optional[WarningInfo]
    recovery_command: Optional[List[str]] = None
    recovery_summary: Optional[str] = None


class Transaction:
    """Journal filesystem mutations before making them visible."""

    def __init__(
        self,
        root: Path,
        command: str,
        modules: Iterable[str],
        *,
        fault_injector: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.root = root.resolve()
        self.journal_path = self.root / JOURNAL_NAME
        self.identifier = uuid.uuid4().hex
        self.state_dir = self.root / f"{STATE_PREFIX}{self.identifier}"
        self._fault_injector = fault_injector
        self._commit_durable = False
        self._abandon_durable = False
        self._conditional_conflict_durable = False
        self._conditional_batch_activated = False
        if self.journal_path.exists():
            raise PartialFailure(
                "An incomplete transaction must be recovered before a new operation begins.",
                phase="startup_recovery",
            )
        self.state_dir.mkdir(mode=0o700)
        self.data: Dict[str, object] = {
            "schema": 1,
            "id": self.identifier,
            "root": str(self.root),
            "command": command,
            "modules": list(modules),
            "phase": "stage",
            "mutated": False,
            "entries": [],
            "external_command": None,
            "external_started": False,
            "conditional_conflict_authority": (
                CONDITIONAL_CONFLICT_AUTHORITY_NAME
            ),
            "conditional_destination_parents": [],
        }
        self._authorize_entries()

    def checkpoint(self, name: str) -> None:
        """Expose deterministic operation boundaries for failure testing."""

        if self._fault_injector is not None:
            self._fault_injector(name)

    @property
    def mutated(self) -> bool:
        return bool(self.data["mutated"])

    def _persist(self) -> None:
        _write_transaction_data(
            self.root,
            self.journal_path,
            self.data,
            authorize_entries=False,
        )

    def _authorize_entries(self) -> os.stat_result:
        return _write_transaction_data(
            self.root,
            self.journal_path,
            self.data,
            authorize_entries=True,
        )

    def _authorize_entries_at_boundary(self, label: str) -> None:
        """Publish state authority and journal with observable durable boundaries."""

        entries = self.data.get("entries", [])
        digest = _entry_digest(entries)
        authority_name = _entry_authority_name(digest)
        self.data["entries_sha256"] = digest
        self.data["entry_authority"] = authority_name
        _write_entry_authority(
            self.state_dir,
            self.identifier,
            entries,
            authority_name,
        )
        self.checkpoint(f"after_{label}_state_authority_write")
        _write_json_atomic(self.journal_path, self.data)
        self.checkpoint(f"after_{label}_journal_write")

    def set_phase(self, phase: str) -> None:
        self.data["phase"] = phase
        self._persist()

    def record_directory_metadata(
        self, metadata: Dict[str, tuple[int, int, int]]
    ) -> None:
        """Persist exact protected directory metadata for fresh-process recovery."""

        self.data["directory_metadata"] = {
            relative: [mode, atime_ns, mtime_ns]
            for relative, (mode, atime_ns, mtime_ns) in metadata.items()
        }
        self._persist()

    def preserve_current_directory_metadata(
        self,
        relative_paths: Iterable[str],
    ) -> None:
        """Merge directory metadata created by an external precommit edit."""

        raw = self.data.get("directory_metadata")
        if not isinstance(raw, dict):
            raise FilesystemError("Transaction directory metadata is invalid")
        for relative in relative_paths:
            parsed = validate_persisted_relative_path(relative, allow_root=True)
            directory = (
                self.root
                if relative == "."
                else self.root.joinpath(*parsed.parts)
            )
            if entry_kind(directory) != "directory":
                continue
            value = directory.lstat()
            raw[relative] = [
                value.st_mode & 0o7777,
                value.st_atime_ns,
                value.st_mtime_ns,
            ]
        self._persist()

    def _entries(self) -> List[Dict[str, object]]:
        value = self.data["entries"]
        assert isinstance(value, list)
        return cast(List[Dict[str, object]], value)

    def _covering_snapshot(self, path: Path) -> Optional[Dict[str, object]]:
        """Return an existing snapshot that already owns rollback for ``path``."""

        for entry in self._entries():
            if entry.get("kind") not in {"snapshot", "replace"}:
                continue
            snapshot_path = Path(str(entry.get("path", "")))
            try:
                path.relative_to(snapshot_path)
            except ValueError:
                continue
            return entry
        return None

    def snapshot(self, paths: Iterable[Path]) -> None:
        """Snapshot every parent file that an operation may alter."""
        restore_root = self.state_dir / "restore"
        restore_root.mkdir(exist_ok=True)
        seen = {str(entry["path"]) for entry in self._entries()}
        for path in paths:
            managed = _managed_entry(self.root, path)
            if str(managed) in seen or self._covering_snapshot(managed) is not None:
                continue
            index = len(self._entries())
            restore = restore_root / str(index)
            copied: tuple[tuple[object, ...], ...] | None = None
            last_error: ConcurrentSourceMutation | None = None
            for _ in range(3):
                candidate = restore_root / f"{index}-attempt-{uuid.uuid4().hex}"
                try:
                    candidate_state = _copy_verified_entry(managed, candidate)
                    if _exact_entry_state(managed) != candidate_state:
                        raise ConcurrentSourceMutation(
                            "Source entry changed before its recovery copy was "
                            f"authorized: {managed}"
                        )
                    copied = candidate_state
                    restore = candidate
                    break
                except ConcurrentSourceMutation as exc:
                    last_error = exc
            if copied is None:
                assert last_error is not None
                raise last_error
            copied_kind = copied[0][1]
            existed = copied_kind is not None
            entry = {
                "path": str(managed),
                "restore": str(restore),
                "existed": existed,
                "kind": "snapshot",
                "entry_type": copied_kind,
                "hash": _hash_path(restore) if existed else None,
            }
            self._entries().append(entry)
            try:
                self._authorize_entries()
            except BaseException:
                self._entries().pop()
                if lexists(restore):
                    remove_entry_no_follow(restore)
                try:
                    self._authorize_entries()
                except BaseException:
                    pass
                raise
            seen.add(str(managed))

    def snapshot_file_bytes(self, path: Path) -> bytes:
        """Read the exact regular-file baseline owned by an exact snapshot."""

        managed = _managed_entry(self.root, path)
        for entry in self._entries():
            if entry.get("kind") != "snapshot" or entry.get("path") != str(managed):
                continue
            restore = Path(str(entry.get("restore", "")))
            if not _inside(self.state_dir, restore) or entry_kind(restore) != "file":
                raise FilesystemError(f"Snapshot baseline is unavailable: {managed}")
            content, _ = read_regular_bytes_no_follow(restore)
            return content
        raise FilesystemError(f"No exact snapshot baseline exists for: {managed}")

    def replace_snapshot_file_baseline(
        self,
        path: Path,
        content: bytes,
        *,
        mode: int,
        atime_ns: int,
        mtime_ns: int,
    ) -> None:
        """Durably merge an external edit into a pending file rollback."""

        if (
            mode < 0
            or mode > 0o7777
            or atime_ns < 0
            or mtime_ns < 0
        ):
            raise FilesystemError("Snapshot baseline mode is invalid")
        managed = _managed_entry(self.root, path)
        for entry in self._entries():
            if entry.get("kind") != "snapshot" or entry.get("path") != str(managed):
                continue
            restore = Path(str(entry.get("restore", "")))
            if not _inside(self.state_dir, restore) or entry_kind(restore) != "file":
                raise FilesystemError(f"Snapshot baseline is unavailable: {managed}")
            entries = self._entries()
            index = entries.index(entry)
            candidate = (
                self.state_dir
                / "restore"
                / f"{index}-adopt-{uuid.uuid4().hex}"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(candidate, flags, mode)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(descriptor)
            os.chmod(candidate, mode, follow_symlinks=False)
            os.utime(
                candidate,
                ns=(atime_ns, mtime_ns),
                follow_symlinks=False,
            )
            copied_content, copied_metadata = read_regular_bytes_no_follow(candidate)
            if (
                copied_content != content
                or (
                    (
                        copied_metadata.st_mode & stat.S_IWRITE
                        != mode & stat.S_IWRITE
                    )
                    if _windows_host()
                    else stat.S_IMODE(copied_metadata.st_mode) != mode
                )
                or (
                    (copied_metadata.st_atime_ns // 100 != atime_ns // 100)
                    if _windows_host()
                    else copied_metadata.st_atime_ns != atime_ns
                )
                or (
                    (copied_metadata.st_mtime_ns // 100 != mtime_ns // 100)
                    if _windows_host()
                    else copied_metadata.st_mtime_ns != mtime_ns
                )
            ):
                raise FilesystemError(
                    f"Merged snapshot baseline could not be verified: {managed}"
                )
            candidate_hash = _hash_path(candidate)
            original_entry = dict(entry)
            covered = [
                (raw, raw.get("restore"))
                for raw in entries
                if raw is not entry
                and raw.get("kind") in {"covered_replace", "covered_detach"}
                and raw.get("restore") == str(restore)
            ]

            def select_candidate() -> None:
                entry["restore"] = str(candidate)
                entry["entry_type"] = "file"
                entry["hash"] = candidate_hash
                entry["existed"] = True
                for raw, _ in covered:
                    raw["restore"] = str(candidate)
                self.data["phase"] = "rollback"

            try:
                self.checkpoint("after_package_baseline_payload_creation")
                select_candidate()
                self.checkpoint("after_package_baseline_entry_update")
                self._authorize_entries_at_boundary("package_baseline")
            except BaseException as exc:
                # A verified candidate is safe to finish authorizing. This
                # completion is not a retry of user work: it guarantees that
                # cancellation can roll back to the external bytes.
                select_candidate()
                try:
                    self._authorize_entries()
                except BaseException:
                    entry.clear()
                    entry.update(original_entry)
                    for raw, previous_restore in covered:
                        raw["restore"] = previous_restore
                    try:
                        self._authorize_entries()
                    except BaseException:
                        pass
                raise exc
            return
        raise FilesystemError(f"No exact snapshot baseline exists for: {managed}")

    def _adopt_snapshot_change(
        self,
        entry: Dict[str, object],
        snapshot_path: Path,
        path: Path,
    ) -> None:
        """Version one external edit into a fresh authorized restore payload."""

        entries = self._entries()
        index = entries.index(entry)
        original_entry = dict(entry)
        original_restore = Path(str(entry["restore"]))
        covered = [
            (raw, raw.get("restore"))
            for raw in entries
            if raw is not entry
            and raw.get("kind") in {"covered_replace", "covered_detach"}
            and raw.get("restore") == str(original_restore)
        ]
        last_error: ConcurrentSourceMutation | None = None
        for _ in range(3):
            candidate = (
                self.state_dir
                / "restore"
                / f"{index}-adopt-{uuid.uuid4().hex}"
            )
            candidate_authorized = False
            try:
                if path != snapshot_path and lexists(original_restore):
                    _copy_verified_entry(original_restore, candidate)
                target = (
                    candidate
                    if path == snapshot_path
                    else candidate / path.relative_to(snapshot_path)
                )
                if path != snapshot_path and lexists(target):
                    # This is an unpublished entry inside the fresh private
                    # candidate tree copied above, not a live project path.
                    remove_entry_no_follow(target)
                adopted_state = _copy_verified_entry(path, target)
                # Catch an edit injected immediately after the copy helper's
                # own source verification, before journal authority changes.
                if _exact_entry_state(path) != adopted_state:
                    raise ConcurrentSourceMutation(
                        f"Source entry changed before its recovery copy was authorized: {path}"
                    )

                candidate_kind = entry_kind(candidate)
                candidate_hash = (
                    _hash_path(candidate) if candidate_kind is not None else None
                )

                def select_candidate() -> None:
                    entry["restore"] = str(candidate)
                    for raw, _ in covered:
                        raw["restore"] = str(candidate)
                    if path == snapshot_path:
                        entry["existed"] = candidate_kind is not None
                        entry["entry_type"] = candidate_kind
                    entry["hash"] = candidate_hash

                try:
                    self.checkpoint("after_snapshot_adoption_payload_creation")
                    select_candidate()
                    self.checkpoint("after_snapshot_adoption_entry_update")
                    self._authorize_entries_at_boundary("snapshot_adoption")
                except BaseException as exc:
                    # Finish authorizing the already-verified external state
                    # before propagating cancellation. This never repeats the
                    # source copy or any user-controlled operation.
                    select_candidate()
                    try:
                        self._authorize_entries()
                        candidate_authorized = True
                    except BaseException:
                        entry.clear()
                        entry.update(original_entry)
                        for raw, previous_restore in covered:
                            raw["restore"] = previous_restore
                        try:
                            self._authorize_entries()
                        except BaseException:
                            pass
                    raise exc
                return
            except ConcurrentSourceMutation as exc:
                last_error = exc
                entry.clear()
                entry.update(original_entry)
                for raw, previous_restore in covered:
                    raw["restore"] = previous_restore
                continue
            except BaseException:
                if not candidate_authorized:
                    entry.clear()
                    entry.update(original_entry)
                    for raw, previous_restore in covered:
                        raw["restore"] = previous_restore
                raise
        assert last_error is not None
        raise last_error

    def preserve_external_source_changes(
        self,
        baseline: SourceTreeInventory,
        *,
        excluded_roots: Iterable[Path] = (),
        passthrough_non_regular_paths: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        """Merge post-plan external edits into the pending rollback baseline.

        This is used only when a fresh add already made visible generator
        changes before its complete V4 plan detected a race.  Every retained
        edit must be covered by a pre-operation snapshot; otherwise the
        transaction stays available for explicit recovery rather than
        guessing which bytes belong to the user.
        """

        pre_read_metadata = _capture_live_tree_metadata(self.root)
        live = source_tree_inventory(self.root)
        mutations = source_tree_changes(baseline, live)
        excluded = tuple(_managed_entry(self.root, path) for path in excluded_roots)
        passthrough = tuple(
            _managed_entry(self.root, path) for path in passthrough_non_regular_paths
        )
        relative_paths: list[PurePosixPath] = []
        all_external_relatives: list[PurePosixPath] = []
        for mutation in mutations:
            action, separator, relative_text = mutation.partition(":")
            if not separator:
                raise FilesystemError(f"External source mutation is invalid: {mutation}")
            parsed = validate_persisted_relative_path(relative_text)
            mutation_path = self.root.joinpath(*parsed.parts)
            if any(
                mutation_path == root or root in mutation_path.parents
                for root in excluded
            ):
                continue
            all_external_relatives.append(parsed)
            if (
                action == "modified"
                and baseline.get(relative_text, (None,))[0] == "directory"
                and live.get(relative_text, (None,))[0] == "directory"
            ):
                # Directory content changes are represented by their child
                # entries. Mode-only changes belong in directory metadata and
                # must not introduce an overlapping whole-parent snapshot.
                continue
            relative_paths.append(parsed)

        # A copied ancestor already preserves all of its descendants.  Keeping
        # only the shallowest changed entries avoids contradictory patch order.
        retained: list[PurePosixPath] = []
        for relative in sorted(relative_paths, key=lambda item: (len(item.parts), item.as_posix())):
            if any(parent == relative or parent in relative.parents for parent in retained):
                continue
            retained.append(relative)

        for retained_relative in retained:
            path = self.root.joinpath(*retained_relative.parts)
            snapshots: list[tuple[int, Dict[str, object], Path]] = []
            for entry in self._entries():
                if entry.get("kind") != "snapshot":
                    continue
                snapshot_path = Path(str(entry.get("path", "")))
                if path == snapshot_path or snapshot_path in path.parents:
                    snapshots.append((len(snapshot_path.parts), entry, snapshot_path))
            if not snapshots:
                # The split pre-operation snapshots intentionally omit
                # cache-bearing ancestors. A concurrent frontier entry outside
                # every snapshot is also outside the generator's rollback
                # authority, so leave it in place instead of recursively
                # copying (and accidentally entering) its excluded caches.
                continue
            _, entry, snapshot_path = max(snapshots, key=lambda item: item[0])
            if path in passthrough and entry_kind(path) != "file":
                if path != snapshot_path:
                    raise FilesystemError(
                        f"Non-regular external entry lacks an exact snapshot: {path}"
                    )
                existed = lexists(path)
                entry["kind"] = "preserved_external"
                entry["existed"] = existed
                entry["entry_type"] = entry_kind(path) if existed else None
                entry["hash"] = _hash_path(path) if existed else None
                continue
            self._adopt_snapshot_change(entry, snapshot_path, path)
        raw_metadata = self.data.get("directory_metadata")
        if not isinstance(raw_metadata, dict):
            raise FilesystemError("Transaction directory metadata is invalid")
        # Inventory traversal itself may advance directory atime. Preserve the
        # complete pre-read directory set so exact conflict cleanup is truly
        # observationally neutral, including newly-created external parents.
        external_relatives = tuple(all_external_relatives)
        for relative_text, (kind, _mode, _atime_ns, _mtime_ns) in pre_read_metadata.items():
            candidate = (
                self.root
                if relative_text == "."
                else self.root.joinpath(*PurePosixPath(relative_text).parts)
            )
            parsed_candidate = PurePosixPath(relative_text)
            belongs_to_external_edit = any(
                parsed_candidate == external
                or parsed_candidate in external.parents
                for external in external_relatives
            )
            if (
                kind == "directory"
                and belongs_to_external_edit
                and not any(
                    candidate == excluded_root or excluded_root in candidate.parents
                    for excluded_root in excluded
                )
                and entry_kind(candidate) == "directory"
            ):
                current = candidate.lstat()
                raw_metadata[relative_text] = [
                    stat.S_IMODE(current.st_mode),
                    current.st_atime_ns,
                    current.st_mtime_ns,
                ]
        self._authorize_entries()
        return mutations

    def preserve_rollback_external_changes(
        self,
        baseline: SourceTreeInventory,
    ) -> tuple[str, ...]:
        """Adopt live states not produced by this transaction before rollback."""

        transaction_owned: list[Path] = []
        for raw in self._entries():
            if raw.get("kind") not in {
                "snapshot",
                "replace",
                "detach",
                "covered_replace",
                "covered_detach",
                "conditional_replace",
            }:
                continue
            if "result_kind" not in raw or "result_hash" not in raw:
                continue
            path = Path(str(raw.get("path", "")))
            expected_kind = raw.get("result_kind")
            expected_hash = raw.get("result_hash")
            live_kind = entry_kind(path)
            live_hash = _hash_path(path) if live_kind is not None else None
            if live_kind == expected_kind and live_hash == expected_hash:
                transaction_owned.append(path)
        return self.preserve_external_source_changes(
            baseline,
            excluded_roots=transaction_owned,
        )

    def activate(self, staged: Path, destination: Path) -> None:
        destination = _managed_entry(self.root, destination)
        staged = _managed_entry(self.root, staged)
        if entry_kind(staged) != "directory":
            raise FilesystemError(f"Staged path is not a directory: {staged}")
        self.replace(staged, destination)

    def replace(self, staged: Path, destination: Path) -> None:
        """Atomically replace one file, directory, or symlink entry."""

        destination = _managed_entry(self.root, destination)
        staged = _managed_entry(self.root, staged)
        if not lexists(staged):
            raise FilesystemError(f"Staged path does not exist: {staged}")
        self.ensure_parent(destination)
        covering = self._covering_snapshot(destination)
        if covering is not None:
            self._entries().append(
                {
                    "path": str(destination),
                    "restore": str(covering["restore"]),
                    "existed": lexists(destination),
                    "kind": "covered_replace",
                    "entry_type": entry_kind(destination),
                    "hash": _hash_path(destination),
                    "result_kind": entry_kind(staged),
                    "result_hash": _hash_path(staged),
                }
            )
            self.data["mutated"] = True
            self._authorize_entries()
            if lexists(destination):
                remove_entry_no_follow(destination)
            os.replace(staged, destination)
            return
        restore = self.state_dir / "modules" / str(len(self._entries()))
        restore.parent.mkdir(parents=True, exist_ok=True)
        existed = lexists(destination)
        entry = {
            "path": str(destination),
            "restore": str(restore),
            "existed": existed,
            "kind": "replace",
            "entry_type": entry_kind(destination),
            "hash": _hash_path(destination) if existed else None,
            "result_kind": entry_kind(staged),
            "result_hash": _hash_path(staged),
        }
        self._entries().append(entry)
        self.data["mutated"] = True
        self._authorize_entries()
        if existed:
            os.replace(destination, restore)
        try:
            os.replace(staged, destination)
        except BaseException:
            if existed and lexists(restore) and not lexists(destination):
                os.replace(restore, destination)
            raise

    def replace_regular_batch_if_matches(
        self,
        replacements: Iterable[tuple[Path, Path, str, int]],
    ) -> None:
        """Retain and verify every live file before activating any replacement."""

        if not _descriptor_relative_io_supported():
            raise FilesystemError(
                "Conditional replacement requires descriptor-relative "
                "no-follow filesystem operations"
            )
        replacement_items = tuple(replacements)
        descriptors = _prepare_conditional_state_descriptors(
            self.root,
            self.state_dir,
            (Path(item[1]) for item in replacement_items),
        )
        self.data["conditional_destination_parents"] = (
            descriptors.destination_parent_authority(self.root)
        )
        self._authorize_entries()
        _write_conditional_retention_authority(
            descriptors.modules,
            self.identifier,
            self.data["conditional_destination_parents"],
        )
        previous_mutated = self.mutated
        entry_count_before = len(self._entries())
        try:
            prepared = self._prepare_regular_replacements(
                replacement_items, descriptors
            )
            if not descriptors.destination_parents_match(self.root):
                raise ConcurrentSourceMutation(
                    "Destination parent changed before conditional replacement"
                )
            retained: list[_RetainedRegularReplacement] = []
            try:
                for replacement in prepared:
                    retained.append(
                        self._retain_matching_regular_file(
                            replacement,
                            previously_mutated=previous_mutated or bool(retained),
                            descriptors=descriptors,
                        )
                    )
                    if not descriptors.destination_parents_match(self.root):
                        raise ConcurrentSourceMutation(
                            "Destination parent changed during conditional retention"
                        )
            except BaseException:
                if self.conflict_is_durable():
                    self._restore_retained_regular_files_for_conflict(
                        retained, descriptors
                    )
                    raise
                if len(self._entries()) == entry_count_before + len(retained):
                    self._restore_retained_regular_files(
                        retained, previous_mutated, descriptors
                    )
                raise

            if not descriptors.destination_parents_match(self.root):
                self._restore_retained_regular_files(
                    retained, previous_mutated, descriptors
                )
                raise ConcurrentSourceMutation(
                    "Destination parent changed before conditional publication"
                )

            activated: list[_PreparedRegularReplacement] = []
            for replacement in prepared:
                destination_descriptor, destination_name = descriptors.destination(
                    replacement.destination
                )
                try:
                    os.link(
                        replacement.staged.name,
                        destination_name,
                        src_dir_fd=descriptors.staged,
                        dst_dir_fd=destination_descriptor,
                        follow_symlinks=False,
                    )
                    # A visible link is the retained-authority boundary.
                    # Set it before append, parent re-observation, or any
                    # other fallible Python-side bookkeeping.
                    self._conditional_batch_activated = True
                except FileExistsError as exc:
                    self._resolve_regular_publication_conflict(
                        retained,
                        activated,
                        previous_mutated,
                        descriptors=descriptors,
                    )
                    raise ConcurrentSourceMutation(
                        "Destination reappeared before conditional publication: "
                        f"{replacement.destination}"
                    ) from exc
                except BaseException:
                    # A syscall wrapper or asynchronous interruption can
                    # report failure after the kernel has already published
                    # the link.  Conservatively enter the fail-closed state
                    # for every ambiguous publication, before any Python-side
                    # append; partial publication is visible publication too.
                    self._conditional_batch_activated = True
                    try:
                        published = _relative_same_identity(
                            replacement.staged.name,
                            descriptors.staged,
                            destination_name,
                            destination_descriptor,
                        )
                    except BaseException:
                        published = False
                    if published:
                        activated.append(replacement)
                    raise
                activated.append(replacement)
                if not descriptors.destination_parents_match(self.root):
                    self._resolve_regular_publication_conflict(
                        retained,
                        activated,
                        previous_mutated,
                        descriptors=descriptors,
                    )
                    raise ConcurrentSourceMutation(
                        "Destination parent changed during conditional publication"
                    )

            for replacement in activated:
                os.unlink(replacement.staged.name, dir_fd=descriptors.staged)
            if not descriptors.destination_parents_match(self.root):
                self._resolve_regular_publication_conflict(
                    retained,
                    activated,
                    previous_mutated,
                    descriptors=descriptors,
                )
                raise ConcurrentSourceMutation(
                    "Destination parent changed before conditional finalization"
                )
            # Keep the fail-closed precursor through all caller-side
            # post-activation validation.  The durable commit transition, or
            # conditional recovery after a validation failure, owns its final
            # removal with the rest of transaction state.  Retiring it here
            # would reopen pathname rollback after our last retained-parent
            # identity check.
        finally:
            descriptors.close()

    def _prepare_regular_replacements(
        self,
        replacements: Iterable[tuple[Path, Path, str, int]],
        descriptors: _ConditionalBatchDescriptors,
    ) -> tuple[_PreparedRegularReplacement, ...]:
        prepared_items: list[_PreparedRegularReplacement] = []
        for staged_path, destination_path, expected_sha256, expected_mode in replacements:
            staged = _managed_entry(self.root, staged_path)
            destination = _managed_entry(self.root, destination_path)
            if (
                staged.parent != self.state_dir / "template"
                or _relative_kind(staged.name, descriptors.staged) != "file"
            ):
                raise FilesystemError(f"Staged path is not a regular file: {staged}")
            destination_descriptor, destination_name = descriptors.destination(
                destination
            )
            if _relative_kind(destination_name, destination_descriptor) != "file":
                raise ConcurrentSourceMutation(
                    f"Destination changed before conditional replacement: {destination}"
                )
            content, metadata = _read_relative_regular_bytes(
                staged.name, descriptors.staged
            )
            prepared_items.append(
                _PreparedRegularReplacement(
                    staged=staged,
                    destination=destination,
                    baseline_sha256=expected_sha256,
                    baseline_mode=expected_mode,
                    published_sha256=hashlib.sha256(content).hexdigest(),
                    published_entry_hash=_regular_entry_hash(
                        content, stat.S_IMODE(metadata.st_mode)
                    ),
                    published_mode=stat.S_IMODE(metadata.st_mode),
                    published_dev=metadata.st_dev,
                    published_ino=metadata.st_ino,
                )
            )
        return tuple(prepared_items)

    def _retain_matching_regular_file(
        self,
        replacement: _PreparedRegularReplacement,
        *,
        previously_mutated: bool,
        descriptors: _ConditionalBatchDescriptors,
    ) -> _RetainedRegularReplacement:
        destination = replacement.destination
        entries = self._entries()
        restore = self.state_dir / "modules" / str(len(entries))
        restore_name = restore.name
        destination_descriptor, destination_name = descriptors.destination(destination)
        live_content, live_metadata = _read_relative_regular_bytes(
            destination_name, destination_descriptor
        )
        live_hash = _regular_entry_hash(
            live_content, stat.S_IMODE(live_metadata.st_mode)
        )
        entry: Dict[str, object] = {
            "path": str(destination),
            "restore": str(restore),
            "existed": True,
            "kind": "replace",
            "entry_type": "file",
            "hash": live_hash,
            "result_kind": "file",
            "result_hash": replacement.published_entry_hash,
        }
        entries.append(entry)
        self.data["mutated"] = True
        try:
            self._authorize_entries()
            os.rename(
                destination_name,
                restore_name,
                src_dir_fd=destination_descriptor,
                dst_dir_fd=descriptors.modules,
            )
            content, metadata = _read_relative_regular_bytes(
                restore_name, descriptors.modules
            )
            retained_hash = _regular_entry_hash(
                content, stat.S_IMODE(metadata.st_mode)
            )
            if retained_hash != live_hash:
                entry["hash"] = retained_hash
                self._authorize_entries()
            if (
                hashlib.sha256(content).hexdigest() != replacement.baseline_sha256
                or stat.S_IMODE(metadata.st_mode) != replacement.baseline_mode
            ):
                raise ConcurrentSourceMutation(
                    f"Destination changed before conditional replacement: {destination}"
                )
        except BaseException as exc:
            propagated_exc = exc
            restore_kind = _relative_kind(restore_name, descriptors.modules)
            destination_kind = _relative_kind(
                destination_name, destination_descriptor
            )
            move_completed = False
            if restore_kind == "file" and destination_kind is None:
                try:
                    published, cleanup_error = (
                        _publish_relative_regular_copy_no_clobber(
                            restore_name,
                            descriptors.modules,
                            destination_name,
                            destination_descriptor,
                        )
                    )
                    if cleanup_error is not None:
                        propagated_exc = cleanup_error
                    move_completed = published
                except BaseException as restoration_exc:
                    propagated_exc = restoration_exc
                if move_completed:
                    try:
                        move_completed = _relative_regular_matches_exact(
                            destination_name,
                            destination_descriptor,
                            live_content,
                            live_metadata,
                        )
                    except BaseException as verification_exc:
                        propagated_exc = verification_exc
                        move_completed = False
            elif restore_kind == "file" and destination_kind == "file":
                try:
                    move_completed = _relative_regular_matches_exact(
                        destination_name,
                        destination_descriptor,
                        live_content,
                        live_metadata,
                    )
                except BaseException as verification_exc:
                    propagated_exc = verification_exc
            move_not_completed = (
                restore_kind is None
                and destination_kind == "file"
                and _relative_regular_matches_exact(
                    destination_name,
                    destination_descriptor,
                    live_content,
                    live_metadata,
                )
            )
            if move_not_completed:
                entries.pop()
                self.data["mutated"] = previously_mutated
                self._authorize_entries()
                raise propagated_exc
            if move_completed:
                if not descriptors.destination_parents_match(self.root):
                    # The live destination was restored through its retained
                    # descriptor, but its canonical parent now names another
                    # directory.  Any root-journal rewrite would itself alter
                    # root mtime and cannot be distinguished atomically from
                    # the external parent handoff.  Publish only a state-dir
                    # conflict marker and forbid pathname rollback.
                    descriptors.restore_destination_parent_metadata()
                    self.retain_conditional_conflict(descriptors.modules)
                    raise TransactionCleanupError(
                        "Conditional retention detected a replaced destination "
                        "parent; preserve the transaction authority",
                        recovery_path=self.journal_path,
                        interrupted=any(
                            isinstance(item, KeyboardInterrupt)
                            for item in (exc, propagated_exc)
                        ),
                    ) from propagated_exc
                # The current entry is already exact through the retained
                # destination descriptor.  Revoke its pathname rollback
                # authority before propagating *every* post-retention
                # exception.  A parent can be replaced after any pathname
                # identity sample, so ordinary rollback must never reopen this
                # destination once the atomic retain has completed.
                authority_removed = False
                removed_directory_metadata: Dict[str, object] = {}
                try:
                    raw_directory_metadata = self.data.get("directory_metadata")
                    if not isinstance(raw_directory_metadata, dict):
                        raise FilesystemError(
                            "Transaction directory metadata is invalid"
                        )
                    relative_parent = destination.parent.relative_to(
                        self.root
                    ).as_posix()
                    if relative_parent in raw_directory_metadata:
                        removed_directory_metadata[relative_parent] = (
                            raw_directory_metadata.pop(relative_parent)
                        )
                    entries.pop()
                    self.data["mutated"] = previously_mutated or bool(entries)
                    root_baseline = raw_directory_metadata.get(".")
                    root_current = self.root.lstat()
                    destination_parent_replaced = (
                        not descriptors.destination_parents_match(self.root)
                    )
                    preserve_live_root = (
                        isinstance(root_baseline, list)
                        and len(root_baseline) == 3
                        and (
                            destination_parent_replaced
                            or stat.S_IMODE(root_current.st_mode)
                            != root_baseline[0]
                            or root_current.st_atime_ns != root_baseline[1]
                        )
                    )
                    for _attempt in range(3):
                        if preserve_live_root:
                            root_current = self.root.lstat()
                            raw_directory_metadata["."] = [
                                stat.S_IMODE(root_current.st_mode),
                                root_current.st_atime_ns,
                                root_current.st_mtime_ns,
                            ]
                        published_root = self._authorize_entries()
                        if not preserve_live_root:
                            break
                        observed_root = self.root.lstat()
                        if (
                            stat.S_IMODE(observed_root.st_mode)
                            == stat.S_IMODE(published_root.st_mode)
                            and observed_root.st_atime_ns
                            == published_root.st_atime_ns
                            and observed_root.st_mtime_ns
                            == published_root.st_mtime_ns
                        ):
                            break
                        root_current = observed_root
                    else:
                        raise FilesystemError(
                            "Plugin root metadata changed during conditional cleanup"
                        )
                    authority_removed = True
                    os.unlink(restore_name, dir_fd=descriptors.modules)
                except BaseException as cleanup_exc:
                    if authority_removed:
                        raise propagated_exc
                    if entry not in entries:
                        entries.append(entry)
                    raw_directory_metadata = self.data.get("directory_metadata")
                    if isinstance(raw_directory_metadata, dict):
                        raw_directory_metadata.update(removed_directory_metadata)
                    self.data["mutated"] = True
                    self.retain_conflict()
                    raise TransactionCleanupError(
                        "Conditional retention could not durably revoke pathname "
                        "rollback authority; preserve the transaction authority",
                        recovery_path=self.journal_path,
                        interrupted=any(
                            isinstance(item, KeyboardInterrupt)
                            for item in (exc, cleanup_exc)
                        ),
                    ) from cleanup_exc
                raise propagated_exc
            if (
                restore_kind == "file"
                and not descriptors.destination_parents_match(self.root)
            ):
                descriptors.restore_destination_parent_metadata()
                self.retain_conditional_conflict(descriptors.modules)
                raise TransactionCleanupError(
                    "Conditional retention detected a replaced destination "
                    "parent before exact restoration; preserve the transaction "
                    "authority",
                    recovery_path=self.journal_path,
                    interrupted=any(
                        isinstance(item, KeyboardInterrupt)
                        for item in (exc, propagated_exc)
                    ),
                ) from propagated_exc
            self.retain_conflict()
            raise TransactionCleanupError(
                "Conditional retention could not determine whether the live "
                "entry moved; preserve the transaction authority",
                recovery_path=self.journal_path,
                interrupted=any(
                    isinstance(item, KeyboardInterrupt)
                    for item in (exc, propagated_exc)
                ),
            ) from propagated_exc
        return _RetainedRegularReplacement(entry, restore, replacement)

    def _restore_retained_regular_files_for_conflict(
        self,
        retained: Iterable[_RetainedRegularReplacement],
        descriptors: _ConditionalBatchDescriptors,
    ) -> None:
        """Restore earlier retained files without reopening project paths."""

        for retained_item in reversed(tuple(retained)):
            destination_descriptor, destination_name = descriptors.destination(
                retained_item.prepared.destination
            )
            _published, cleanup_error = _publish_relative_regular_copy_no_clobber(
                retained_item.restore.name,
                descriptors.modules,
                destination_name,
                destination_descriptor,
            )
            if cleanup_error is not None:
                raise cleanup_error
        descriptors.restore_destination_parent_metadata()

    def _restore_retained_regular_files(
        self,
        retained: Iterable[_RetainedRegularReplacement],
        previous_mutated: bool,
        descriptors: _ConditionalBatchDescriptors,
    ) -> None:
        entries = self._entries()
        cleanup_failed = False
        for retained_item in reversed(tuple(retained)):
            entry = retained_item.entry
            restore = retained_item.restore
            destination = retained_item.prepared.destination
            restore_name = restore.name
            if _relative_kind(restore_name, descriptors.modules) != "file":
                cleanup_failed = True
                continue
            destination_descriptor, destination_name = descriptors.destination(
                destination
            )
            _restore_relative_regular_no_clobber(
                restore_name,
                descriptors.modules,
                destination_name,
                destination_descriptor,
            )
            entries.remove(entry)
        self.data["mutated"] = previous_mutated or bool(entries)
        try:
            self._authorize_entries()
        except BaseException:
            cleanup_failed = True
        try:
            descriptors.restore_destination_parent_metadata()
        except (OSError, FilesystemError):
            cleanup_failed = True
        if cleanup_failed:
            raise FilesystemError(
                "Conditional replacement could not restore retained live entries"
            )

    def _resolve_regular_publication_conflict(
        self,
        retained: Iterable[_RetainedRegularReplacement],
        activated: Iterable[_PreparedRegularReplacement],
        previous_mutated: bool,
        *,
        descriptors: _ConditionalBatchDescriptors,
    ) -> None:
        entries = self._entries()
        retained_items = tuple(retained)
        activated_paths = {
            replacement.destination: replacement for replacement in activated
        }
        for retained_item in retained_items:
            replacement = activated_paths.get(retained_item.prepared.destination)
            if replacement is None:
                continue
            index = entries.index(retained_item.entry)
            retained_item.entry.update(
                {
                    "kind": "conditional_replace",
                    "capture": str(self.state_dir / "captures" / str(index)),
                    "published_sha256": replacement.published_sha256,
                    "published_mode": replacement.published_mode,
                    "published_dev": replacement.published_dev,
                    "published_ino": replacement.published_ino,
                }
            )
        self._authorize_entries()

        cleanup_failed = False
        resolved: list[Dict[str, object]] = []
        for retained_item in reversed(retained_items):
            entry = retained_item.entry
            restore = retained_item.restore
            destination = retained_item.prepared.destination
            try:
                if destination in activated_paths:
                    if not _reconcile_conditional_replacement(
                        entry,
                        (descriptors.capture, descriptors.modules),
                        descriptors.destination(destination),
                    ):
                        cleanup_failed = True
                        continue
                else:
                    destination_descriptor, destination_name = (
                        descriptors.destination(destination)
                    )
                    _restore_relative_regular_no_clobber(
                        restore.name,
                        descriptors.modules,
                        destination_name,
                        destination_descriptor,
                    )
                resolved.append(entry)
            except (OSError, FilesystemError):
                cleanup_failed = True
        for entry in resolved:
            entries.remove(entry)
        self.data["mutated"] = previous_mutated if not cleanup_failed else True
        try:
            self._authorize_entries()
        except BaseException:
            cleanup_failed = True
        try:
            descriptors.restore_destination_parent_metadata()
        except (OSError, FilesystemError):
            cleanup_failed = True
        if cleanup_failed:
            raise FilesystemError(
                "Conditional publication conflict cleanup is incomplete"
            )

    def ensure_parent(self, path: Path) -> None:
        """Create and journal every missing ancestor needed for ``path``."""

        managed = _managed_entry(self.root, path)
        missing: list[Path] = []
        parent = managed.parent
        while parent != self.root and not lexists(parent):
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            self.track_created_directory(directory)
        managed.parent.mkdir(parents=True, exist_ok=True)

    def track_created(self, path: Path) -> None:
        """Record an internal staged path so interruption recovery removes it."""
        canonical = _managed_entry(self.root, path)
        self._entries().append(
            {
                "path": str(canonical),
                "restore": str(self.state_dir / "unused" / str(len(self._entries()))),
                "existed": False,
                "kind": "created",
                "entry_type": entry_kind(canonical),
                "hash": None,
            }
        )
        self._authorize_entries()

    def track_created_directory(self, path: Path) -> None:
        """Remove a generator-created parent on rollback only when it is empty."""
        canonical = _managed_entry(self.root, path)
        if lexists(canonical):
            return
        if any(
            entry.get("kind") == "created_directory"
            and entry.get("path") == str(canonical)
            for entry in self._entries()
        ):
            return
        self._entries().append(
            {
                "path": str(canonical),
                "restore": str(
                    self.state_dir / "unused" / str(len(self._entries()))
                ),
                "existed": False,
                "kind": "created_directory",
                "entry_type": None,
                "hash": None,
            }
        )
        self._authorize_entries()

    def detach(self, destination: Path) -> None:
        destination = _managed_entry(self.root, destination)
        if not lexists(destination):
            raise FilesystemError(f"Removal target does not exist: {destination}")
        covering = self._covering_snapshot(destination)
        if covering is not None:
            self._entries().append(
                {
                    "path": str(destination),
                    "restore": str(covering["restore"]),
                    "existed": True,
                    "kind": "covered_detach",
                    "entry_type": entry_kind(destination),
                    "hash": _hash_path(destination),
                    "result_kind": None,
                    "result_hash": None,
                }
            )
            self.data["mutated"] = True
            self._authorize_entries()
            remove_entry_no_follow(destination)
            return
        restore = self.state_dir / "modules" / str(len(self._entries()))
        restore.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": str(destination),
            "restore": str(restore),
            "existed": True,
            "kind": "detach",
            "entry_type": entry_kind(destination),
            "hash": _hash_path(destination),
            "result_kind": None,
            "result_hash": None,
        }
        self._entries().append(entry)
        self.data["mutated"] = True
        self._authorize_entries()
        os.replace(destination, restore)

    def mark_write(self) -> None:
        self.data["mutated"] = True
        self._persist()

    def record_snapshot_results(self, paths: Iterable[Path]) -> None:
        """Bind direct, transaction-owned writes to their snapshot entries."""

        changed = False
        for path in paths:
            managed = _managed_entry(self.root, path)
            candidates = [
                raw
                for raw in self._entries()
                if raw.get("kind") == "snapshot"
                and (
                    Path(str(raw.get("path", ""))) == managed
                    or Path(str(raw.get("path", ""))) in managed.parents
                )
            ]
            if not candidates:
                raise FilesystemError(
                    f"Direct transaction write lacks a covering snapshot: {managed}"
                )
            entry = max(
                candidates,
                key=lambda raw: len(Path(str(raw.get("path", ""))).parts),
            )
            snapshot_path = Path(str(entry["path"]))
            result_kind = entry_kind(snapshot_path)
            entry["result_kind"] = result_kind
            entry["result_hash"] = (
                _hash_path(snapshot_path) if result_kind is not None else None
            )
            changed = True
        if changed:
            self.data["mutated"] = True
            self._authorize_entries()

    def mark_external(self, command: List[str]) -> None:
        self.data["external_command"] = list(command)
        self.data["external_started"] = True
        self.data["mutated"] = True
        self._persist()

    def retain_conflict(self) -> None:
        """Durably block automatic rollback when it could erase an external edit."""

        self.data["phase"] = "conflict"
        self._authorize_entries()

    def retain_conditional_conflict(self, modules_descriptor: int) -> None:
        """Block pathname recovery without rewriting the plugin-root journal."""

        # The journal and entry authority already durably bind the retained
        # parent identity. Once a live descriptor proves that identity changed,
        # ordinary pathname rollback is forbidden even if the optional marker
        # write is interrupted. Fresh recovery reaches the same conclusion by
        # comparing the durable parent authority with the canonical path.
        self._conditional_conflict_durable = True
        _write_conditional_conflict_authority(
            modules_descriptor,
            self.identifier,
            self.data.get("entries", []),
            self.data.get("conditional_destination_parents", []),
        )

    def conflict_is_durable(self) -> bool:
        """Return whether automatic rollback is blocked by an ambiguous edit."""

        return (
            self.data.get("phase") == "conflict"
            or self._conditional_conflict_durable
            or (
                self._conditional_batch_activated
                and _conditional_conflict_is_durable(
                    self.root,
                    self.data,
                    allow_parent_fallback=True,
                )
            )
            or _conditional_conflict_is_durable(
                self.root,
                self.data,
                allow_parent_fallback=False,
            )
        )

    def conditional_conflict_is_durable(self) -> bool:
        """Return whether descriptor-only cleanup retained external authority."""

        return self._conditional_conflict_durable or (
            self._conditional_batch_activated
            and _conditional_conflict_is_durable(
                self.root,
                self.data,
                allow_parent_fallback=True,
            )
        ) or (
            _conditional_conflict_is_durable(
                self.root,
                self.data,
                allow_parent_fallback=False,
            )
        )

    def conditional_destination_parents_match(self) -> bool:
        """Return whether conditional destinations retain canonical identity."""

        return _conditional_destination_parents_match(self.root, self.data)

    def commit(self) -> None:
        self.data["phase"] = "commit"
        self._persist()
        self._commit_durable = True
        self.finish_commit()

    def commit_is_durable(self) -> bool:
        """Return whether the journal durably crossed the commit boundary."""

        if self._commit_durable:
            return True
        if not self.journal_path.exists():
            return self.data.get("phase") == "commit"
        try:
            persisted = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(persisted, dict)
            and persisted.get("id") == self.identifier
            and persisted.get("phase") == "commit"
        )

    def finish_commit(self) -> None:
        """Idempotently finish cleanup after a durable commit marker."""

        if not self.commit_is_durable():
            raise FilesystemError("Cannot finish a transaction before durable commit")
        if _load_recovery_pointer(self.root) is not None:
            _complete_recovery_pointer(self.root)
            return
        if entry_kind(self.journal_path) is None and entry_kind(self.state_dir) is None:
            return
        _finish_durable_data(
            self.root,
            self.journal_path,
            self.data,
            remove_created=False,
            checkpoint=self.checkpoint,
        )

    def abandon_unmutated(self) -> None:
        """Discard planning/staging state without restoring external edits."""

        if self.mutated:
            raise FilesystemError("Cannot abandon a transaction after visible mutation")
        self.data["phase"] = "abandon"
        self._persist()
        self._abandon_durable = True
        self.checkpoint("after_abandon_persist")
        self.finish_abandon()

    def abandon_is_durable(self) -> bool:
        """Return whether cleanup may finish without restoring stale snapshots."""

        if self._abandon_durable:
            return True
        if not self.journal_path.exists():
            return self.data.get("phase") == "abandon"
        try:
            persisted = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(persisted, dict)
            and persisted.get("id") == self.identifier
            and persisted.get("phase") == "abandon"
        )

    def finish_abandon(self) -> None:
        """Idempotently finish a durable no-write/conflict abandonment."""

        if not self.abandon_is_durable():
            raise FilesystemError("Cannot abandon before the durable abandon marker")
        if _load_recovery_pointer(self.root) is not None:
            _complete_recovery_pointer(self.root, checkpoint=self.checkpoint)
            return
        if entry_kind(self.journal_path) is None and entry_kind(self.state_dir) is None:
            return
        _finish_durable_data(
            self.root,
            self.journal_path,
            self.data,
            remove_created=True,
            checkpoint=self.checkpoint,
        )

    def rollback(
        self,
        *,
        reconcile: Optional[Callable[[List[str]], bool]] = None,
    ) -> RollbackResult:
        return _rollback_data(
            self.root,
            self.journal_path,
            self.data,
            reconcile=reconcile,
        )

    def recovery_authority_path(self) -> Optional[Path]:
        """Return the durable authority a later process can use, when present."""

        if entry_kind(self.journal_path) == "file":
            return self.journal_path
        pending = _load_recovery_pointer(self.root)
        if pending is not None:
            return pending[0]
        if entry_kind(self.state_dir) == "directory":
            return self.state_dir
        return None


def recover_pending(
    root: Path,
    *,
    reconcile: Optional[Callable[[List[str]], bool]] = None,
) -> RecoveryOutcome:
    root = root.resolve()
    try:
        pending_pointer = _load_recovery_pointer(root)
        if pending_pointer is not None:
            _pointer_path, pointer_data = pending_pointer
            outcome = _complete_recovery_pointer(root)
            command = str(pointer_data.get("command", "operation"))
            modules = pointer_data.get("modules", [])
            module = modules[0] if isinstance(modules, list) and modules else None
            if outcome == "rollback":
                message = f"Recovered an interrupted {command.capitalize()}"
                if module:
                    message += f' for "{module}"'
                message += ".\n  Previous state restored."
                return RecoveryOutcome(
                    RollbackResult(True, "completed", []),
                    WarningInfo("startup_recovery", message, "startup_recovery", None),
                    None,
                )
            if outcome == "commit":
                message = f"Recovered an interrupted {command.capitalize()}"
                if module:
                    message += f' for "{module}"'
                message += ".\n  Verified changes were already committed."
                return RecoveryOutcome(
                    RollbackResult(),
                    WarningInfo("startup_recovery", message, "startup_recovery", None),
                    None,
                )
            return RecoveryOutcome(
                RollbackResult(False, "not_needed", []),
                WarningInfo(
                    "startup_recovery",
                    f"Recovered an interrupted {command.capitalize()}.\n  No planned changes were applied.",
                    "startup_recovery",
                    None,
                ),
                None,
            )
    except Exception as exc:
        raise PartialFailure(
            f"Automatic recovery could not complete: {exc}",
            phase="startup_recovery",
            recovery=["supernote-module", "doctor"],
        ) from exc
    journal = root / JOURNAL_NAME
    if not journal.is_file():
        return RecoveryOutcome(RollbackResult(), None, None)
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or (
            (
                _windows_path_key(Path(str(data.get("root"))))
                != _windows_path_key(root)
            )
            if _windows_host()
            else data.get("root") != str(root)
        ):
            raise ValueError("journal root does not match the current plugin")
        command = str(data.get("command", "operation"))
        modules = data.get("modules", [])
        module = modules[0] if isinstance(modules, list) and modules else None
        if _conditional_conflict_is_durable(root, data):
            if _conditional_conflict_is_resolved(root, data):
                _finish_conditional_conflict(root, journal, data)
                return RecoveryOutcome(
                    RollbackResult(False, "not_needed", []),
                    WarningInfo(
                        "startup_recovery",
                        "Recovered the resolved Template retention conflict.\n"
                        "  The canonical launch scripts match the retained pre-sync baseline.",
                        "startup_recovery",
                        None,
                    ),
                    None,
                )
            return RecoveryOutcome(
                RollbackResult(True, "partial", []),
                None,
                ["supernote-module", "doctor"],
                "Restore scripts/runPlugin.sh and scripts/runPlugin.ps1 to the "
                "retained pre-sync bytes and metadata, preserve the transaction "
                "journal/state, then run Doctor.",
            )
        if data.get("phase") == "commit":
            _finish_durable_data(root, journal, data, remove_created=False)
            description = command.capitalize()
            message = f"Recovered an interrupted {description}"
            if module:
                message += f' for "{module}"'
            message += ".\n  Verified changes were already committed."
            return RecoveryOutcome(
                RollbackResult(),
                WarningInfo("startup_recovery", message, "startup_recovery", None),
                None,
            )
        if data.get("phase") == "abandon":
            _finish_durable_data(root, journal, data, remove_created=True)
            description = command.capitalize()
            message = f"Recovered an interrupted {description}.\n  No planned changes were applied."
            return RecoveryOutcome(
                RollbackResult(False, "not_needed", []),
                WarningInfo("startup_recovery", message, "startup_recovery", None),
                None,
            )
        if data.get("phase") == "conflict":
            return RecoveryOutcome(
                RollbackResult(True, "partial", []),
                None,
                ["supernote-module", "doctor"],
            )
        rollback = _rollback_data(root, journal, data, reconcile=reconcile)
    except Exception as exc:
        raise PartialFailure(
            f"Automatic recovery could not complete: {exc}",
            phase="startup_recovery",
            recovery=["supernote-module", "doctor"],
        ) from exc
    if rollback.status == "partial":
        external = data.get("external_command")
        recovery_command = (
            [str(item) for item in external]
            if isinstance(external, list) and external
            else ["supernote-module", "doctor"]
        )
        return RecoveryOutcome(rollback, None, recovery_command)
    description = command.capitalize()
    if module:
        message = f'Recovered an interrupted {description} for "{module}".\n  Previous state restored.'
    else:
        message = f"Recovered an interrupted {description}.\n  Previous state restored."
    return RecoveryOutcome(
        rollback,
        WarningInfo("startup_recovery", message, "startup_recovery", None),
        None,
    )


def _rollback_data(
    root: Path,
    journal: Path,
    data: Dict[str, object],
    *,
    reconcile: Optional[Callable[[List[str]], bool]],
) -> RollbackResult:
    # Journal metadata is untrusted persisted input. Validate it before any
    # restore entry can remove, replace, chmod, or retimestamp project state.
    _validate_transaction_entries(root, data)
    _validate_restore_payloads(root, data)
    directory_metadata = _directory_metadata_from_data(root, data)
    restored: List[str] = []
    failures: List[str] = []
    created_directories: List[tuple[Dict[str, object], Path]] = []
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("transaction entries are invalid")
    if any(
        isinstance(raw, dict) and raw.get("kind") == "conditional_replace"
        for raw in entries
    ) and not _descriptor_relative_io_supported():
        raise FilesystemError(
            "Conditional replacement recovery requires descriptor-relative "
            "no-follow filesystem operations"
        )
    for raw in reversed(entries):
        if not isinstance(raw, dict):
            failures.append("invalid journal entry")
            continue
        path = Path(str(raw.get("path", "")))
        restore = Path(str(raw.get("restore", "")))
        if raw.get("restored") is True:
            restored.append(str(path))
            continue
        if not _inside(root, path) or not _inside(root, restore):
            failures.append(str(path))
            continue
        try:
            if raw.get("kind") == "conditional_replace":
                if not _reconcile_conditional_replacement(raw):
                    failures.append(str(path))
                    continue
                raw["restored"] = True
                restored.append(str(path))
                continue
            if raw.get("kind") in {
                "covered_replace",
                "covered_detach",
                "preserved_external",
            }:
                # A non-overlapping ancestor snapshot is restored later in
                # this reverse walk and owns the exact subtree recovery.
                raw["restored"] = True
                continue
            if raw.get("kind") == "created_directory":
                created_directories.append((raw, path))
                continue
            existed = bool(raw.get("existed"))
            expected = raw.get("hash")
            if existed and not lexists(restore):
                if lexists(path) and expected is not None and _hash_path(path) == expected:
                    raw["restored"] = True
                    restored.append(str(path))
                    continue
                raise FilesystemError(f"Restore data is unavailable: {restore}")
            if lexists(path):
                remove_entry_no_follow(path)
            if existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(restore, path)
                if expected is not None and _hash_path(path) != expected:
                    raise FilesystemError(f"Restored content hash mismatch: {path}")
            raw["restored"] = True
            restored.append(str(path))
        except Exception:
            failures.append(str(path))

    # Parent creation entries can precede snapshots of their future children.
    # Retry them only after every file/tree restoration has completed.
    for raw, path in sorted(
        created_directories,
        key=lambda item: len(item[1].parts),
        reverse=True,
    ):
        try:
            if entry_kind(path) == "directory":
                try:
                    path.rmdir()
                except OSError:
                    # Preserve content that appeared concurrently. Public
                    # operations perform a complete protected-state comparison
                    # and report partial when this is operation residue.
                    pass
            raw["restored"] = True
            restored.append(str(path))
        except Exception:
            failures.append(str(path))

    external = data.get("external_command")
    if bool(data.get("external_started")) and isinstance(external, list) and external:
        if reconcile is None or not reconcile([str(item) for item in external]):
            failures.append("dependency reconciliation")

    if failures:
        data["phase"] = "rollback_partial"
        data["rollback_failures"] = failures
        _write_transaction_data(root, journal, data)
        return RollbackResult(True, "partial", restored)
    created_roots = tuple(
        Path(str(raw.get("path", "")))
        for raw in entries
        if isinstance(raw, dict)
        and raw.get("kind") in {"created", "created_directory"}
    )
    directory_metadata = {
        relative: value
        for relative, value in directory_metadata.items()
        if not (
            entry_kind(
                root
                if relative == "."
                else root.joinpath(*PurePosixPath(relative).parts)
            )
            is None
            and any(
                (
                    root
                    if relative == "."
                    else root.joinpath(*PurePosixPath(relative).parts)
                )
                == created
                or created
                in (
                    root
                    if relative == "."
                    else root.joinpath(*PurePosixPath(relative).parts)
                ).parents
                for created in created_roots
            )
        )
    }
    try:
        recovery_path, bundle_id, manifest_sha256 = retain_directory_metadata_recovery(
            root,
            directory_metadata,
            transaction_id=_validated_transaction_identifier(data),
            outcome="rollback",
        )
        _publish_recovery_pointer(
            root,
            data,
            recovery_path,
            "rollback",
            bundle_id=bundle_id,
            manifest_sha256=manifest_sha256,
        )
    except BaseException as exc:
        data["phase"] = "rollback_partial"
        data["rollback_failures"] = [f"metadata recovery authority: {exc}"]
        _write_transaction_data(root, journal, data)
        return RollbackResult(True, "partial", restored)
    try:
        _complete_recovery_pointer(root)
    except BaseException:
        # The deterministic out-of-tree pointer remains discoverable by the
        # next process; do not recreate an unauthorised in-project journal.
        return RollbackResult(True, "partial", restored)
    return RollbackResult(True, "completed", restored)


def _directory_metadata_from_data(
    root: Path,
    data: Dict[str, object],
) -> Dict[str, tuple[int, int, int]]:
    raw_metadata = data.get("directory_metadata", {})
    if not isinstance(raw_metadata, dict):
        raise FilesystemError("Transaction directory metadata is invalid")
    metadata: Dict[str, tuple[int, int, int]] = {}
    for relative, raw_value in raw_metadata.items():
        if (
            not isinstance(relative, str)
            or not isinstance(raw_value, list)
            or len(raw_value) != 3
            or not all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in raw_value
            )
        ):
            raise FilesystemError("Transaction directory metadata is invalid")
        metadata[relative] = (
            int(raw_value[0]),
            int(raw_value[1]),
            int(raw_value[2]),
        )
    return validate_protected_directory_metadata(
        root,
        metadata,
        allow_missing=True,
    )


def _finish_durable_data(
    root: Path,
    journal: Path,
    data: Dict[str, object],
    *,
    remove_created: bool,
    checkpoint: Optional[Callable[[str], None]] = None,
) -> None:
    """Finish a durable outcome while retaining metadata recovery authority."""

    _validate_transaction_entries(root, data)
    _validate_restore_payloads(root, data)
    metadata = _directory_metadata_from_data(root, data)
    if not remove_created:
        # A committed plan may intentionally remove a previously captured
        # directory. Metadata cleanup applies only to directories that remain
        # part of the committed project state.
        metadata = {
            relative: value
            for relative, value in metadata.items()
            if entry_kind(
                root
                if relative == "."
                else root.joinpath(*PurePosixPath(relative).parts)
            )
            == "directory"
        }
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("transaction entries are invalid")
    try:
        if remove_created:
            for raw in reversed(entries):
                if not isinstance(raw, dict) or raw.get("kind") != "created":
                    continue
                path = Path(str(raw.get("path", "")))
                if not _inside(root, path):
                    raise FilesystemError(
                        f"Transaction cleanup path escapes plugin root: {path}"
                    )
                if lexists(path):
                    remove_entry_no_follow(path)
        if checkpoint is not None:
            checkpoint("after_abandon_staging_removal")
        outcome = "abandon" if remove_created else "commit"
        recovery_path, bundle_id, manifest_sha256 = retain_directory_metadata_recovery(
            root,
            metadata,
            transaction_id=_validated_transaction_identifier(data),
            outcome=outcome,
        )
        _publish_recovery_pointer(
            root,
            data,
            recovery_path,
            outcome,
            bundle_id=bundle_id,
            manifest_sha256=manifest_sha256,
        )
        _complete_recovery_pointer(root, checkpoint=checkpoint)
    except BaseException as exc:
        if isinstance(exc, (TransactionCleanupError, KeyboardInterrupt)):
            raise
        recovery_path = None
        try:
            pending = _load_recovery_pointer(root)
            if pending is not None:
                recovery_path = Path(str(pending[1]["recovery_path"]))
        except BaseException:
            pass
        raise TransactionCleanupError(
            f"Durable transaction cleanup is incomplete: {exc}",
            recovery_path=recovery_path,
        ) from exc
