"""Persistent same-filesystem transaction journal and recovery engine."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .errors import FilesystemError, PartialFailure
from .models import RollbackResult, WarningInfo

JOURNAL_NAME = ".supernote-module-transaction.json"
STATE_PREFIX = ".supernote-module-transaction-"


def _hash_path(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class RecoveryOutcome:
    rollback: RollbackResult
    warning: Optional[WarningInfo]
    recovery_command: Optional[List[str]] = None


class Transaction:
    """Journal filesystem mutations before making them visible."""

    def __init__(self, root: Path, command: str, modules: Iterable[str]) -> None:
        self.root = root.resolve()
        self.journal_path = self.root / JOURNAL_NAME
        self.identifier = uuid.uuid4().hex
        self.state_dir = self.root / f"{STATE_PREFIX}{self.identifier}"
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
        }
        self._persist()

    @property
    def mutated(self) -> bool:
        return bool(self.data["mutated"])

    def _persist(self) -> None:
        _write_json_atomic(self.journal_path, self.data)

    def set_phase(self, phase: str) -> None:
        self.data["phase"] = phase
        self._persist()

    def _entries(self) -> List[Dict[str, object]]:
        value = self.data["entries"]
        assert isinstance(value, list)
        return value  # type: ignore[return-value]

    def snapshot(self, paths: Iterable[Path]) -> None:
        """Snapshot every parent file that an operation may alter."""
        restore_root = self.state_dir / "restore"
        restore_root.mkdir(exist_ok=True)
        seen = {str(entry["path"]) for entry in self._entries()}
        for path in paths:
            canonical = path.resolve(strict=False)
            if not _inside(self.root, canonical):
                raise FilesystemError(f"Transaction target escapes the plugin root: {canonical}")
            if str(canonical) in seen:
                continue
            index = len(self._entries())
            restore = restore_root / str(index)
            existed = canonical.exists()
            if existed:
                if canonical.is_dir():
                    shutil.copytree(canonical, restore, symlinks=True)
                else:
                    restore.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(canonical, restore)
            self._entries().append(
                {
                    "path": str(canonical),
                    "restore": str(restore),
                    "existed": existed,
                    "kind": "snapshot",
                    "hash": _hash_path(canonical),
                }
            )
        self._persist()

    def activate(self, staged: Path, destination: Path) -> None:
        destination = destination.resolve(strict=False)
        staged = staged.resolve()
        if not _inside(self.root, destination) or not _inside(self.root, staged):
            raise FilesystemError("Staged and destination paths must remain in the plugin root")
        restore = self.state_dir / "modules" / str(len(self._entries()))
        restore.parent.mkdir(parents=True, exist_ok=True)
        existed = destination.exists()
        entry = {
            "path": str(destination),
            "restore": str(restore),
            "existed": existed,
            "kind": "replace",
            "hash": _hash_path(destination) if existed else None,
        }
        self._entries().append(entry)
        self.data["mutated"] = True
        self._persist()
        if existed:
            os.replace(destination, restore)
        try:
            os.replace(staged, destination)
        except BaseException:
            if existed and restore.exists() and not destination.exists():
                os.replace(restore, destination)
            raise

    def track_created(self, path: Path) -> None:
        """Record an internal staged path so interruption recovery removes it."""
        canonical = path.resolve(strict=False)
        if not _inside(self.root, canonical):
            raise FilesystemError(f"Staged path escapes the plugin root: {canonical}")
        self._entries().append(
            {
                "path": str(canonical),
                "restore": str(self.state_dir / "unused" / str(len(self._entries()))),
                "existed": False,
                "kind": "created",
                "hash": None,
            }
        )
        self._persist()

    def track_created_directory(self, path: Path) -> None:
        """Remove a generator-created parent on rollback only when it is empty."""
        canonical = path.resolve(strict=False)
        if not _inside(self.root, canonical):
            raise FilesystemError(
                f"Generated directory escapes the plugin root: {canonical}"
            )
        if canonical.exists():
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
                "hash": None,
            }
        )
        self._persist()

    def detach(self, destination: Path) -> None:
        destination = destination.resolve()
        if not _inside(self.root, destination):
            raise FilesystemError(f"Removal target escapes the plugin root: {destination}")
        if not destination.exists():
            raise FilesystemError(f"Removal target does not exist: {destination}")
        restore = self.state_dir / "modules" / str(len(self._entries()))
        restore.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": str(destination),
            "restore": str(restore),
            "existed": True,
            "kind": "detach",
            "hash": _hash_path(destination),
        }
        self._entries().append(entry)
        self.data["mutated"] = True
        self._persist()
        os.replace(destination, restore)

    def mark_write(self) -> None:
        self.data["mutated"] = True
        self._persist()

    def mark_external(self, command: List[str]) -> None:
        self.data["external_command"] = list(command)
        self.data["external_started"] = True
        self.data["mutated"] = True
        self._persist()

    def commit(self) -> None:
        self.data["phase"] = "commit"
        self._persist()
        shutil.rmtree(self.state_dir, ignore_errors=False)
        self.journal_path.unlink()

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


def recover_pending(
    root: Path,
    *,
    reconcile: Optional[Callable[[List[str]], bool]] = None,
) -> RecoveryOutcome:
    root = root.resolve()
    journal = root / JOURNAL_NAME
    if not journal.is_file():
        return RecoveryOutcome(RollbackResult(), None, None)
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("root") != str(root):
            raise ValueError("journal root does not match the current plugin")
        command = str(data.get("command", "operation"))
        modules = data.get("modules", [])
        module = modules[0] if isinstance(modules, list) and modules else None
        if data.get("phase") == "commit":
            state_dir = root / f"{STATE_PREFIX}{data.get('id', '')}"
            shutil.rmtree(state_dir, ignore_errors=True)
            journal.unlink()
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
    restored: List[str] = []
    failures: List[str] = []
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("transaction entries are invalid")
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
            if raw.get("kind") == "created_directory":
                if path.is_dir() and not path.is_symlink():
                    try:
                        path.rmdir()
                    except OSError:
                        # Never remove user content that appeared concurrently.
                        pass
                raw["restored"] = True
                restored.append(str(path))
                continue
            existed = bool(raw.get("existed"))
            expected = raw.get("hash")
            if existed and not restore.exists():
                if path.exists() and expected is not None and _hash_path(path) == expected:
                    raw["restored"] = True
                    restored.append(str(path))
                    continue
                raise FilesystemError(f"Restore data is unavailable: {restore}")
            if path.exists():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(restore, path)
                if expected is not None and _hash_path(path) != expected:
                    raise FilesystemError(f"Restored content hash mismatch: {path}")
            raw["restored"] = True
            restored.append(str(path))
        except Exception:
            failures.append(str(path))

    external = data.get("external_command")
    if bool(data.get("external_started")) and isinstance(external, list) and external:
        if reconcile is None or not reconcile([str(item) for item in external]):
            failures.append("dependency reconciliation")

    state_dir = root / f"{STATE_PREFIX}{data.get('id', '')}"
    if failures:
        data["phase"] = "rollback_partial"
        data["rollback_failures"] = failures
        _write_json_atomic(journal, data)
        return RollbackResult(True, "partial", restored)
    shutil.rmtree(state_dir, ignore_errors=True)
    journal.unlink(missing_ok=True)
    return RollbackResult(True, "completed", restored)
