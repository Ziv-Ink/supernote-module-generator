from __future__ import annotations

import hashlib
from pathlib import Path
import os
import stat

import pytest

import supernote_module_generator.transaction_registry as registry_module
from supernote_module_generator.transaction_registry import (
    entry_digest,
    entry_kind_fields_are_valid,
    parse_recovery_pointer,
    private_recovery_registry,
    recovery_pointer_path,
    recovery_pointer_payload,
    validated_transaction_identifier,
)
from supernote_module_generator.errors import FilesystemError


_DIGEST = "a" * 64


@pytest.mark.parametrize(
    "entry",
    [
        {
            "path": "/plugin/new",
            "restore": "/state/unused/0",
            "kind": "created",
            "existed": False,
            "entry_type": "file",
            "hash": None,
        },
        {
            "path": "/plugin/old",
            "restore": "/state/modules/0",
            "kind": "detach",
            "existed": True,
            "entry_type": "file",
            "hash": _DIGEST,
        },
        {
            "path": "/plugin/script",
            "restore": "/state/modules/0",
            "capture": "/state/captures/0",
            "kind": "conditional_replace",
            "existed": True,
            "entry_type": "file",
            "hash": _DIGEST,
            "published_sha256": "b" * 64,
            "published_mode": 0o755,
            "published_dev": 1,
            "published_ino": 2,
        },
    ],
)
def test_transaction_entry_schema_accepts_canonical_authority(entry: dict[str, object]):
    assert entry_kind_fields_are_valid(entry)


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "created", "existed": "no"},
        {
            "kind": "detach",
            "existed": False,
            "entry_type": "file",
            "hash": _DIGEST,
        },
        {
            "kind": "created",
            "existed": False,
            "entry_type": "file",
            "hash": None,
            "capture": "/state/captures/0",
        },
        {
            "kind": "conditional_replace",
            "existed": True,
            "entry_type": "file",
            "hash": _DIGEST,
            "capture": "/state/captures/0",
            "published_sha256": "not-a-digest",
            "published_mode": 0o755,
            "published_dev": 1,
            "published_ino": 2,
        },
    ],
)
def test_transaction_entry_schema_rejects_ambiguous_authority(entry: dict[str, object]):
    assert not entry_kind_fields_are_valid(entry)


def test_recovery_pointer_schema_round_trips_one_exact_private_bundle(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    data: dict[str, object] = {
        "id": "1" * 32,
        "command": "update",
        "modules": ["alpha"],
    }
    raw = recovery_pointer_payload(
        tmp_path,
        data,
        recovery,
        "rollback",
        bundle_id="2" * 32,
        manifest_sha256="3" * 64,
    )

    parsed = parse_recovery_pointer(raw, tmp_path)

    assert parsed.transaction_id == "1" * 32
    assert parsed.outcome == "rollback"
    assert parsed.recovery_path == recovery
    assert parsed.bundle_id == "2" * 32
    assert parsed.manifest_sha256 == "3" * 64


def test_transaction_identifier_and_digest_are_canonical() -> None:
    assert validated_transaction_identifier({"id": "a" * 32}) == "a" * 32
    assert entry_digest([{"b": 2, "a": 1}]) == entry_digest([{"a": 1, "b": 2}])
    with pytest.raises(FilesystemError, match="identity is invalid"):
        validated_transaction_identifier({"id": "A" * 32})


@pytest.mark.parametrize(
    "entry",
    [
        {
            "kind": "created_directory",
            "existed": False,
            "entry_type": None,
            "hash": None,
        },
        {
            "kind": "preserved_external",
            "existed": False,
            "entry_type": None,
            "hash": None,
        },
        {
            "kind": "preserved_external",
            "existed": True,
            "entry_type": "symlink",
            "hash": _DIGEST,
        },
        {
            "kind": "snapshot",
            "existed": True,
            "entry_type": "directory",
            "hash": _DIGEST,
            "result_kind": "file",
            "result_hash": "b" * 64,
        },
        {
            "kind": "snapshot",
            "existed": False,
            "entry_type": None,
            "hash": None,
            "result_kind": None,
            "result_hash": None,
        },
    ],
)
def test_transaction_entry_schema_covers_every_common_authority_shape(
    entry: dict[str, object],
) -> None:
    assert entry_kind_fields_are_valid(entry)


@pytest.mark.parametrize(
    "entry",
    [
        {
            "kind": "created",
            "existed": False,
            "entry_type": "file",
            "hash": None,
            "unexpected": True,
        },
        {
            "kind": "created",
            "existed": False,
            "entry_type": "unknown",
            "hash": None,
        },
        {
            "kind": "created",
            "existed": False,
            "entry_type": "file",
            "hash": None,
            "restored": "yes",
        },
        {
            "kind": "snapshot",
            "existed": True,
            "entry_type": "file",
            "hash": _DIGEST,
            "result_kind": "file",
            "result_hash": "bad",
        },
        {
            "kind": "conditional_replace",
            "existed": True,
            "entry_type": "file",
            "hash": _DIGEST,
            "capture": "/state/captures/0",
            "published_sha256": "b" * 64,
            "published_mode": True,
            "published_dev": 1,
            "published_ino": 2,
        },
    ],
)
def test_transaction_entry_schema_rejects_each_ambiguous_common_shape(
    entry: dict[str, object],
) -> None:
    assert not entry_kind_fields_are_valid(entry)


def test_private_recovery_registry_create_reuse_and_pointer_identity(
    tmp_path: Path,
) -> None:
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    registry = private_recovery_registry(
        tmp_path,
        identity="fixture",
        effective_uid=effective_uid,
        windows=os.name == "nt",
    )
    assert private_recovery_registry(
        tmp_path,
        identity="fixture",
        effective_uid=effective_uid,
        windows=os.name == "nt",
    ) == registry
    pointer = recovery_pointer_path(tmp_path, registry)
    assert pointer.parent == registry
    assert pointer.suffix == ".json"
    assert len(pointer.stem) == 64


def test_windows_recovery_pointer_identity_uses_normalized_path_key(
    tmp_path: Path, monkeypatch
) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    normalized = r"c:\users\owner\plugin"
    monkeypatch.setattr(registry_module.os, "name", "nt")
    monkeypatch.setattr(
        registry_module,
        "_windows_path_key",
        lambda _path: normalized,
    )

    pointer = recovery_pointer_path(tmp_path, registry)

    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert pointer == registry / f"{expected}.json"


def test_private_recovery_registry_rejects_non_directory(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "supernote-module-recovery-v2-fixture"
    registry.write_bytes(b"not a registry\n")
    with pytest.raises(FilesystemError, match="registry is unsafe"):
        private_recovery_registry(
            tmp_path,
            identity="fixture",
            effective_uid=None,
            windows=False,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-mode contract")
def test_private_recovery_registry_rejects_shared_mode(tmp_path: Path) -> None:
    registry = tmp_path / "supernote-module-recovery-v2-fixture"
    registry.mkdir(mode=0o700)
    registry.chmod(0o755)
    with pytest.raises(FilesystemError, match="not private"):
        private_recovery_registry(
            tmp_path,
            identity="fixture",
            effective_uid=os.geteuid(),
            windows=False,
        )
    assert stat.S_IMODE(registry.stat().st_mode) == 0o755


def test_recovery_pointer_payload_rejects_invalid_outcome_and_bundle(
    tmp_path: Path,
) -> None:
    data = {"id": "1" * 32}
    with pytest.raises(FilesystemError, match="outcome is invalid"):
        recovery_pointer_payload(
            tmp_path,
            data,
            tmp_path / "missing",
            "unknown",
            bundle_id="2" * 32,
            manifest_sha256="3" * 64,
        )
    with pytest.raises(FilesystemError, match="bundle is unavailable"):
        recovery_pointer_payload(
            tmp_path,
            data,
            tmp_path / "missing",
            "rollback",
            bundle_id="2" * 32,
            manifest_sha256="3" * 64,
        )


def test_recovery_pointer_parser_rejects_non_object_and_wrong_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(FilesystemError, match="pointer is invalid"):
        parse_recovery_pointer([], tmp_path)
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    raw = recovery_pointer_payload(
        tmp_path,
        {"id": "1" * 32},
        recovery,
        "rollback",
        bundle_id="2" * 32,
        manifest_sha256="3" * 64,
    )
    raw["plugin_root"] = str(tmp_path / "other")
    with pytest.raises(FilesystemError, match="pointer is invalid"):
        parse_recovery_pointer(raw, tmp_path)
