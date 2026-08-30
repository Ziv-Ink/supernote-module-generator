"""Whole-project filesystem evidence for V4 integration and rollback tests."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Dict, Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class InventoryEntry:
    """One filesystem entry without following symbolic links."""

    path: str
    kind: str
    mode: int
    sha256: Optional[str] = None
    symlink_target: Optional[str] = None
    generator_owner: Optional[str] = None

    def json_value(self) -> Dict[str, object]:
        return asdict(self)


ProjectInventory = Tuple[InventoryEntry, ...]


def inventory_project(
    root: Path,
    *,
    additional_owners: Mapping[str, str] | None = None,
) -> ProjectInventory:
    """Inventory every entry below ``root`` without dereferencing symlinks.

    Generator ownership is reconstructed from the current V4 feature and shared
    runtime ownership files.  ``additional_owners`` lets an individual audit
    fixture identify plugin-global or deliberately corrupted owned paths that
    are not representable by the current manifests.
    """

    root = root.resolve(strict=True)
    owners = _discover_generator_owners(root)
    if additional_owners:
        for relative, owner in additional_owners.items():
            owners[_normalized_relative(relative)] = owner

    entries = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in sorted(children, key=lambda item: item.name, reverse=True):
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                metadata = child.stat(follow_symlinks=False)
                mode = stat.S_IMODE(metadata.st_mode)
                owner = owners.get(relative)
                if child.is_symlink():
                    entries.append(
                        InventoryEntry(
                            path=relative,
                            kind="symlink",
                            mode=mode,
                            symlink_target=os.readlink(path),
                            generator_owner=owner,
                        )
                    )
                elif child.is_dir(follow_symlinks=False):
                    entries.append(
                        InventoryEntry(
                            path=relative,
                            kind="directory",
                            mode=mode,
                            generator_owner=owner,
                        )
                    )
                    pending.append(path)
                elif child.is_file(follow_symlinks=False):
                    entries.append(
                        InventoryEntry(
                            path=relative,
                            kind="file",
                            mode=mode,
                            sha256=_file_sha256(path),
                            generator_owner=owner,
                        )
                    )
                else:
                    entries.append(
                        InventoryEntry(
                            path=relative,
                            kind="other",
                            mode=mode,
                            generator_owner=owner,
                        )
                    )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def inventory_json(inventory: Iterable[InventoryEntry]) -> str:
    """Return stable JSON suitable for raw failure evidence."""

    return json.dumps(
        [entry.json_value() for entry in inventory],
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _discover_generator_owners(root: Path) -> Dict[str, str]:
    owners: Dict[str, str] = {}
    local_modules = root / "local_modules"
    for manifest in _feature_manifest_candidates(local_modules):
        value = _read_json_object(manifest)
        if value is None:
            continue
        npm_name = value.get("npm_name")
        generated_files = value.get("generated_files")
        if not isinstance(npm_name, str) or not isinstance(generated_files, list):
            continue
        feature_root = manifest.parent
        owner = f"feature:{npm_name}"
        for relative in generated_files:
            if not isinstance(relative, str):
                continue
            owned_path = _contained_owned_path(root, feature_root, relative)
            if owned_path is not None:
                owners[owned_path] = owner

    runtime_root = root / "android/.supernote-module/runtime"
    ownership = runtime_root / "ownership.json"
    value = _read_json_object(ownership)
    if value is not None:
        generated_files = value.get("generated_files")
        if isinstance(generated_files, list):
            for relative in generated_files:
                if not isinstance(relative, str):
                    continue
                owned_path = _contained_owned_path(root, runtime_root, relative)
                if owned_path is not None:
                    owners[owned_path] = "shared-runtime"
    return owners


def _feature_manifest_candidates(local_modules: Path) -> Tuple[Path, ...]:
    if not local_modules.is_dir():
        return ()
    manifests = list(local_modules.glob("*/.supernote-module.json"))
    manifests.extend(local_modules.glob("@*/*/.supernote-module.json"))
    return tuple(sorted(manifests))


def _read_json_object(path: Path) -> Optional[Dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _contained_owned_path(root: Path, owner_root: Path, relative: str) -> Optional[str]:
    try:
        normalized = _normalized_relative(relative)
    except ValueError:
        return None
    candidate = owner_root / Path(normalized)
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return None


def _normalized_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"expected a normalized relative path, got {value!r}")
    return path.as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
