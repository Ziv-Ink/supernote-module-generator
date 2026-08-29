"""Typed recovery-registry identity and pointer-schema decisions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Mapping

from .errors import FilesystemError
from .filesystem import entry_kind


RECOVERY_POINTER_SCHEMA = 1
_HEX = frozenset("0123456789abcdef")
_ENTRY_FIELDS = {
    "path",
    "restore",
    "existed",
    "kind",
    "entry_type",
    "hash",
    "restored",
    "capture",
    "published_sha256",
    "published_mode",
    "published_dev",
    "published_ino",
    "result_kind",
    "result_hash",
}
_CONDITIONAL_FIELDS = {
    "capture",
    "published_sha256",
    "published_mode",
    "published_dev",
    "published_ino",
}


@dataclass(frozen=True)
class RecoveryPointer:
    transaction_id: str
    outcome: str
    recovery_path: Path
    bundle_id: str
    manifest_sha256: str


def validated_transaction_identifier(data: Mapping[str, object]) -> str:
    identifier = data.get("id")
    if not _is_hex(identifier, 32):
        raise FilesystemError("Transaction identity is invalid")
    assert isinstance(identifier, str)
    return identifier


def entry_digest(entries: object) -> str:
    encoded = json.dumps(
        entries,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def entry_kind_fields_are_valid(raw: Mapping[str, object]) -> bool:
    common = _validated_common_entry_fields(raw)
    if common is None:
        return False
    existed, entry_type, digest, valid_digest = common
    kind = raw.get("kind")
    if kind == "conditional_replace":
        return _conditional_entry_fields_are_valid(raw, existed, entry_type, valid_digest)
    if set(raw) & _CONDITIONAL_FIELDS:
        return False
    if kind == "created":
        return existed is False and entry_type is not None and digest is None
    if kind == "created_directory":
        return existed is False and entry_type is None and digest is None
    if kind == "preserved_external":
        return (entry_type is not None and valid_digest) if existed else (
            entry_type is None and digest is None
        )
    if kind in {"detach", "covered_detach"}:
        return existed is True and entry_type is not None and valid_digest
    if existed:
        return entry_type is not None and valid_digest
    return entry_type is None and digest is None


def _validated_common_entry_fields(
    raw: Mapping[str, object],
) -> tuple[bool, object, object, bool] | None:
    existed = raw.get("existed")
    entry_type = raw.get("entry_type")
    digest = raw.get("hash")
    if (
        set(raw) - _ENTRY_FIELDS
        or not isinstance(existed, bool)
        or (
            raw.get("restored") is not None
            and not isinstance(raw.get("restored"), bool)
        )
        or entry_type not in {None, "file", "directory", "symlink", "other"}
        or not _result_fields_are_valid(raw)
    ):
        return None
    return existed, entry_type, digest, _is_hex(digest, 64)


def _result_fields_are_valid(raw: Mapping[str, object]) -> bool:
    if "result_kind" not in raw and "result_hash" not in raw:
        return True
    result_kind = raw.get("result_kind")
    result_hash = raw.get("result_hash")
    return result_kind in {None, "file", "directory", "symlink", "other"} and (
        (result_kind is None and result_hash is None)
        or (result_kind is not None and _is_hex(result_hash, 64))
    )


def _conditional_entry_fields_are_valid(
    raw: Mapping[str, object],
    existed: object,
    entry_type: object,
    valid_digest: bool,
) -> bool:
    published_mode = raw.get("published_mode")
    published_dev = raw.get("published_dev")
    published_ino = raw.get("published_ino")
    return (
        existed is True
        and entry_type == "file"
        and valid_digest
        and isinstance(raw.get("capture"), str)
        and _is_hex(raw.get("published_sha256"), 64)
        and isinstance(published_mode, int)
        and not isinstance(published_mode, bool)
        and 0 <= published_mode <= 0o7777
        and isinstance(published_dev, int)
        and not isinstance(published_dev, bool)
        and published_dev >= 0
        and isinstance(published_ino, int)
        and not isinstance(published_ino, bool)
        and published_ino >= 0
    )


def private_recovery_registry(
    temporary_root: Path,
    *,
    identity: str,
    effective_uid: int | None,
    windows: bool,
) -> Path:
    registry = temporary_root / f"supernote-module-v4-recovery-v2-{identity}"
    kind = entry_kind(registry)
    if kind is None:
        try:
            os.mkdir(registry, 0o700)
        except FileExistsError:
            pass
    elif kind != "directory":
        raise FilesystemError("Transaction recovery registry is unsafe")
    metadata = registry.lstat()
    private_owner = effective_uid is None or getattr(metadata, "st_uid", -1) == effective_uid
    private_mode = windows or not stat.S_IMODE(metadata.st_mode) & 0o077
    canonical = (
        entry_kind(registry) == "directory"
        and registry.resolve(strict=True)
        == registry.parent.resolve(strict=True) / registry.name
    )
    if not canonical or not private_owner or not private_mode:
        raise FilesystemError(
            "Transaction recovery registry is not private to the current user"
        )
    return registry


def recovery_pointer_path(root: Path, registry: Path) -> Path:
    key = hashlib.sha256(str(root.resolve(strict=True)).encode("utf-8")).hexdigest()
    return registry / f"{key}.json"


def recovery_pointer_payload(
    root: Path,
    data: Mapping[str, object],
    recovery_path: Path,
    outcome: str,
    *,
    bundle_id: str,
    manifest_sha256: str,
) -> dict[str, object]:
    identifier = validated_transaction_identifier(data)
    if outcome not in {"rollback", "commit", "abandon"}:
        raise FilesystemError("Transaction recovery outcome is invalid")
    if entry_kind(recovery_path) != "directory":
        raise FilesystemError("Transaction recovery bundle is unavailable")
    modules_value = data.get("modules", [])
    modules = list(modules_value) if isinstance(modules_value, list) else []
    return {
        "schema_version": RECOVERY_POINTER_SCHEMA,
        "plugin_root": str(root.resolve(strict=True)),
        "transaction_id": identifier,
        "outcome": outcome,
        "recovery_path": str(recovery_path.resolve(strict=True)),
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "command": str(data.get("command", "operation")),
        "modules": modules,
    }


def parse_recovery_pointer(raw: object, root: Path) -> RecoveryPointer:
    if not isinstance(raw, dict):
        raise FilesystemError("Transaction recovery pointer is invalid")
    expected_fields = {
        "schema_version",
        "plugin_root",
        "transaction_id",
        "outcome",
        "recovery_path",
        "bundle_id",
        "manifest_sha256",
        "command",
        "modules",
    }
    identifier = raw.get("transaction_id")
    outcome = raw.get("outcome")
    recovery_path = raw.get("recovery_path")
    bundle_id = raw.get("bundle_id")
    manifest_sha256 = raw.get("manifest_sha256")
    if (
        set(raw) != expected_fields
        or raw.get("schema_version") != RECOVERY_POINTER_SCHEMA
        or raw.get("plugin_root") != str(root.resolve(strict=True))
        or not _is_hex(identifier, 32)
        or outcome not in {"rollback", "commit", "abandon"}
        or not isinstance(recovery_path, str)
        or not _is_hex(bundle_id, 32)
        or not _is_hex(manifest_sha256, 64)
    ):
        raise FilesystemError("Transaction recovery pointer is invalid")
    assert isinstance(identifier, str)
    assert isinstance(outcome, str)
    assert isinstance(bundle_id, str)
    assert isinstance(manifest_sha256, str)
    return RecoveryPointer(
        transaction_id=identifier,
        outcome=outcome,
        recovery_path=Path(recovery_path),
        bundle_id=bundle_id,
        manifest_sha256=manifest_sha256,
    )


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in _HEX for character in value)
    )
