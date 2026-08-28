"""Versioned official-template capability comparison and explicit sync."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Dict, Iterable, Tuple

from .errors import ConcurrentSourceMutation, GeneratorError, TemplateStateError
from .filesystem import (
    contained_entry_kind_no_follow,
    protected_directory_metadata,
    read_contained_regular_bytes_no_follow,
)
from .generation_plan import PlanConflictError
from .integrity_manifest import INTEGRITY_MANIFEST_PATH, load_integrity_manifest
from .models import (
    Change,
    CommandResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
    ValidationResult,
)
from .transaction import Transaction, TransactionCleanupError


UNVERIFIED_LAUNCH = "Launch attempted but runtime success was not verified."
FORBIDDEN_LAUNCH = "assuming success after the tap"
TEMPLATE_SCRIPT_PATHS = (
    "scripts/runPlugin.sh",
    "scripts/runPlugin.ps1",
)


@dataclass(frozen=True)
class TemplateFileState:
    path: str
    state: str
    sha256: str | None
    detail: str

    def manifest(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "state": self.state,
            "sha256": self.sha256,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TemplateCapabilityState:
    required_capability: str
    state: str
    files: Tuple[TemplateFileState, ...]

    def manifest(self) -> Dict[str, object]:
        return {
            "required_capability": self.required_capability,
            "state": self.state,
            "files": [item.manifest() for item in self.files],
        }


@dataclass(frozen=True)
class _CapturedTemplateEntry:
    path: str
    content: bytes
    mode: int
    sha256: str


@dataclass(frozen=True)
class _TemplateSnapshot:
    state: TemplateCapabilityState
    entries: Tuple[_CapturedTemplateEntry, ...]

    def entry(self, path: str) -> _CapturedTemplateEntry | None:
        return next((item for item in self.entries if item.path == path), None)


@dataclass(frozen=True)
class _TemplateCandidate:
    baseline: _CapturedTemplateEntry
    content: bytes


@dataclass(frozen=True)
class _TemplateSyncPlan:
    snapshot: _TemplateSnapshot
    candidates: Tuple[_TemplateCandidate, ...]


def _script_is_current(relative: str, content: bytes) -> tuple[bool, str]:
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError:
        return False, "script is not UTF-8"
    if FORBIDDEN_LAUNCH in source:
        return False, "script still claims tap-only success"
    if relative.endswith(".sh"):
        verified = (
            "wait_for_new_log_occurrence" in source
            and source.count("wait_for_new_log_occurrence \"$") >= 2
            and "PluginHost PID $current_pid" in source
        )
    else:
        verified = (
            "Wait-ForNewLogOccurrence" in source
            and source.count("Wait-ForNewLogOccurrence $eventPattern") == 1
            and source.count("Wait-ForNewLogOccurrence $runningPattern") == 1
            and "PluginHost PID $currentPid" in source
        )
    if UNVERIFIED_LAUNCH not in source and not verified:
        return False, "neither a correlated runtime result nor the explicit unverified outcome is present"
    if relative.endswith(".ps1") and source.count(
        "$nodes = @(Get-NodesMatching $Attribute $Value)"
    ) != 2:
        return False, "PowerShell one-node array handling is stale"
    return True, "capability is current"


def _capture_existing_entry(
    root: Path, relative: str
) -> _CapturedTemplateEntry | None:
    path = root / relative
    if contained_entry_kind_no_follow(root, path) != "file":
        return None
    content, metadata = read_contained_regular_bytes_no_follow(root, path)
    return _CapturedTemplateEntry(
        relative,
        content,
        stat.S_IMODE(metadata.st_mode),
        hashlib.sha256(content).hexdigest(),
    )


def _capture_template_snapshot(root: Path) -> _TemplateSnapshot:
    """Capture every template authority entry through no-follow reads."""

    root = root.resolve()
    manifest = load_integrity_manifest(root)
    entries: list[_CapturedTemplateEntry] = []
    manifest_entry = _capture_existing_entry(root, INTEGRITY_MANIFEST_PATH)
    if manifest_entry is None:
        raise TemplateStateError("V4 integrity manifest is unavailable")
    entries.append(manifest_entry)
    records: list[TemplateFileState] = []
    for relative in TEMPLATE_SCRIPT_PATHS:
        path = root / relative
        kind = contained_entry_kind_no_follow(root, path)
        if kind is None:
            records.append(TemplateFileState(relative, "missing", None, "file is missing"))
            continue
        if kind != "file":
            records.append(
                TemplateFileState(relative, "drifted", None, f"unsafe entry kind: {kind}")
            )
            continue
        captured = _capture_existing_entry(root, relative)
        if captured is None:
            raise TemplateStateError(f"{relative} changed while it was inspected")
        entries.append(captured)
        current, detail = _script_is_current(relative, captured.content)
        records.append(
            TemplateFileState(
                relative,
                "current" if current else "drifted",
                captured.sha256,
                detail,
            )
        )

    package_path = root / "package.json"
    package_kind = contained_entry_kind_no_follow(root, package_path)
    if package_kind != "file":
        records.append(
            TemplateFileState(
                "package.json",
                "missing" if package_kind is None else "drifted",
                None,
                "template run command is unavailable",
            )
        )
    else:
        package_entry = _capture_existing_entry(root, "package.json")
        if package_entry is None:
            raise TemplateStateError("package.json changed while it was inspected")
        entries.append(package_entry)
        try:
            package = json.loads(package_entry.content.decode("utf-8"))
            run = package["scripts"]["run"]
            routed = isinstance(run, str) and all(
                relative in run for relative in TEMPLATE_SCRIPT_PATHS
            )
        except (KeyError, TypeError, UnicodeDecodeError, ValueError):
            routed = False
        records.append(
            TemplateFileState(
                "package.json",
                "current" if routed else "drifted",
                package_entry.sha256,
                "npm run run routes both launch scripts"
                if routed
                else "npm run run does not route both launch scripts",
            )
        )

    states = {item.state for item in records}
    overall = "missing" if "missing" in states else "drifted" if "drifted" in states else "current"
    return _TemplateSnapshot(
        TemplateCapabilityState(manifest.template_capability, overall, tuple(records)),
        tuple(entries),
    )


def inspect_template_capability(root: Path) -> TemplateCapabilityState:
    """Inspect template scripts without following any project symlink."""

    return _capture_template_snapshot(root).state


def synchronized_script(relative: str, content: bytes) -> bytes:
    current, _detail = _script_is_current(relative, content)
    if current:
        return content
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TemplateStateError(f"{relative} is not UTF-8") from exc
    if relative.endswith(".sh"):
        old = '    log "Pressed $LAUNCH_LABEL through NOTE; assuming success after the tap."'
        new = (
            '    log "Pressed $LAUNCH_LABEL through NOTE."\n'
            f'    log "{UNVERIFIED_LAUNCH}"\n'
            '    log "Confirm the plugin UI or a plugin-specific runtime marker before claiming success."'
        )
        if source.count(old) != 1:
            raise TemplateStateError(
                "runPlugin.sh drift cannot be synchronized automatically"
            )
        source = source.replace(old, new, 1)
    else:
        old = '    Write-Log "Pressed $LaunchLabel through NOTE; assuming success after the tap."'
        new = (
            '    Write-Log "Pressed $LaunchLabel through NOTE."\n'
            f'    Write-Log "{UNVERIFIED_LAUNCH}"\n'
            '    Write-Log "Confirm the plugin UI or a plugin-specific runtime marker before claiming success."'
        )
        plain = "$nodes = Get-NodesMatching $Attribute $Value"
        wrapped = "$nodes = @(Get-NodesMatching $Attribute $Value)"
        old_count = source.count(old)
        plain_count = source.count(plain)
        if old_count not in {0, 1} or plain_count not in {0, 2}:
            raise TemplateStateError(
                "runPlugin.ps1 drift cannot be synchronized automatically"
            )
        if old_count == 0 and plain_count == 0:
            raise TemplateStateError(
                "runPlugin.ps1 drift cannot be synchronized automatically"
            )
        if plain_count:
            source = source.replace(plain, wrapped)
        if old_count:
            source = source.replace(old, new, 1)
    updated = source.encode("utf-8")
    synchronized, detail = _script_is_current(relative, updated)
    if not synchronized:
        raise TemplateStateError(
            f"{relative} sync did not reach current state: {detail}"
        )
    return updated


def _sync_plan(root: Path) -> _TemplateSyncPlan:
    snapshot = _capture_template_snapshot(root)
    package = next(item for item in snapshot.state.files if item.path == "package.json")
    if package.state != "current":
        raise TemplateStateError(
            "package.json does not route both template launch scripts; restore the pinned official template baseline"
        )
    candidates: list[_TemplateCandidate] = []
    for relative in TEMPLATE_SCRIPT_PATHS:
        baseline = snapshot.entry(relative)
        if baseline is None:
            state = next(item for item in snapshot.state.files if item.path == relative)
            raise TemplateStateError(
                f"{relative} is {state.state}; restore a regular file from the pinned official template baseline"
            )
        updated = synchronized_script(relative, baseline.content)
        if updated != baseline.content:
            candidates.append(_TemplateCandidate(baseline, updated))
    return _TemplateSyncPlan(snapshot, tuple(candidates))


def _changes(root: Path, candidates: Iterable[_TemplateCandidate]) -> list[Change]:
    return [
        Change(
            str(root / candidate.baseline.path),
            "update",
            "template_capability",
        )
        for candidate in candidates
    ]


def _validate_captured_entry(root: Path, baseline: _CapturedTemplateEntry) -> None:
    path = root / baseline.path
    if contained_entry_kind_no_follow(root, path) != "file":
        raise PlanConflictError(
            f"Template authority changed after planning: {baseline.path}"
        )
    try:
        content, metadata = read_contained_regular_bytes_no_follow(root, path)
    except Exception as exc:
        raise PlanConflictError(
            f"Template authority changed after planning: {baseline.path}"
        ) from exc
    if (
        hashlib.sha256(content).hexdigest() != baseline.sha256
        or stat.S_IMODE(metadata.st_mode) != baseline.mode
    ):
        raise PlanConflictError(
            f"Template authority changed after planning: {baseline.path}"
        )


def _validate_sync_preconditions(root: Path, plan: _TemplateSyncPlan) -> None:
    for baseline in plan.snapshot.entries:
        _validate_captured_entry(root, baseline)


def _stage_candidate(
    transaction: Transaction, candidate: _TemplateCandidate, index: int
) -> Path:
    staged = transaction.state_dir / "template" / str(index)
    staged.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        staged,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        candidate.baseline.mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(candidate.content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(staged, candidate.baseline.mode, follow_symlinks=False)
    return staged


def _activate_candidates(
    root: Path,
    transaction: Transaction,
    staged_candidates: Tuple[Tuple[_TemplateCandidate, Path], ...],
) -> None:
    """Retain every live baseline before atomically activating staged bytes."""

    try:
        transaction.replace_regular_batch_if_matches(
            (
                (
                    staged,
                    root / candidate.baseline.path,
                    candidate.baseline.sha256,
                    candidate.baseline.mode,
                )
                for candidate, staged in staged_candidates
            )
        )
    except ConcurrentSourceMutation as exc:
        raise PlanConflictError(
            "Template authority changed after planning during conditional activation"
        ) from exc


def _template_residue(root: Path, plan: _TemplateSyncPlan) -> list[Change]:
    residue: list[Change] = []
    for candidate in plan.candidates:
        baseline = candidate.baseline
        path = root / baseline.path
        try:
            kind = contained_entry_kind_no_follow(root, path)
            if kind == "file":
                content, metadata = read_contained_regular_bytes_no_follow(root, path)
                exact = (
                    hashlib.sha256(content).hexdigest() == baseline.sha256
                    and stat.S_IMODE(metadata.st_mode) == baseline.mode
                )
            else:
                exact = False
        except Exception:
            exact = False
        if not exact:
            residue.append(Change(str(path), "update", "rollback_residue"))
    return residue


def _template_error(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, GeneratorError):
        return ErrorInfo(exc.kind, exc.phase, exc.message)
    return ErrorInfo("template_sync_failed", "template", str(exc))


def _recovery_action(
    transaction: Transaction, summary: str
) -> tuple[RecoveryAction, str | None]:
    authority = transaction.recovery_authority_path()
    recovery_path = str(authority) if authority is not None else None
    if recovery_path is not None:
        summary = f"{summary} Recovery authority remains at {recovery_path}."
    return RecoveryAction(summary, ["supernote-module", "doctor"]), recovery_path


def _abandon_result(
    transaction: Transaction,
    plan: _TemplateSyncPlan,
    planned: list[Change],
    exc: BaseException,
) -> CommandResult:
    interrupted = isinstance(exc, KeyboardInterrupt)
    try:
        transaction.abandon_unmutated()
    except BaseException as cleanup_exc:
        recovery, recovery_path = _recovery_action(
            transaction,
            "Precommit template cleanup is incomplete; preserve the durable authority and run Doctor.",
        )
        return CommandResult(
            "template",
            status="partial",
            exit_code=3,
            changes=planned,
            actual_changes=[],
            affected_targets=["template launch scripts"],
            rollback=RollbackResult(False, "not_needed", []),
            recovery=recovery,
            error=ErrorInfo(
                "template_sync_cleanup_failed", "precommit", str(cleanup_exc)
            ),
            diagnostics=[str(exc), str(cleanup_exc)],
            next_action=recovery.summary,
            metadata={
                "template": plan.snapshot.state.manifest(),
                "recovery_path": recovery_path,
                "cancellation_requested": interrupted,
                "cancellation_status": "partial" if interrupted else "not_requested",
            },
        )
    if isinstance(exc, PlanConflictError):
        return CommandResult(
            "template",
            status="failure",
            exit_code=1,
            changes=planned,
            actual_changes=[],
            affected_targets=["template launch scripts"],
            error=ErrorInfo("plan_conflict", "precommit", str(exc)),
            next_action="The template authority changed during planning. Preserve the external edit, review it, and rerun template sync.",
            metadata={"template": plan.snapshot.state.manifest()},
        )
    if interrupted:
        return CommandResult(
            "template",
            status="cancelled",
            exit_code=130,
            changes=planned,
            actual_changes=[],
            affected_targets=["template launch scripts"],
            rollback=RollbackResult(False, "not_needed", []),
            metadata={
                "template": plan.snapshot.state.manifest(),
                "cancellation_requested": True,
                "cancellation_status": "completed",
                "cancellation_message": "Template sync cancelled before mutation; staged state was discarded.",
            },
        )
    return CommandResult(
        "template",
        status="failure",
        exit_code=exc.exit_code if isinstance(exc, GeneratorError) else 1,
        changes=planned,
        actual_changes=[],
        affected_targets=["template launch scripts"],
        error=_template_error(exc),
        diagnostics=[str(exc)],
        next_action="Correct the template precommit failure and rerun template sync.",
        metadata={"template": plan.snapshot.state.manifest()},
    )


def _rollback_result(
    root: Path,
    transaction: Transaction,
    plan: _TemplateSyncPlan,
    planned: list[Change],
    exc: BaseException,
) -> CommandResult:
    interrupted = isinstance(exc, KeyboardInterrupt)
    try:
        rollback = transaction.rollback()
    except BaseException as rollback_exc:
        rollback = RollbackResult(True, "partial", [])
        diagnostics = [str(exc), str(rollback_exc)]
    else:
        diagnostics = [str(exc)]
    residue = _template_residue(root, plan)
    if rollback.status == "completed":
        if interrupted:
            return CommandResult(
                "template",
                status="cancelled",
                exit_code=130,
                changes=planned,
                actual_changes=[],
                affected_targets=["template launch scripts"],
                rollback=rollback,
                diagnostics=diagnostics,
                metadata={
                    "template": plan.snapshot.state.manifest(),
                    "cancellation_requested": True,
                    "cancellation_status": "completed",
                    "cancellation_message": "Template sync cancelled. Previous state restored.",
                },
            )
        return CommandResult(
            "template",
            status="failure",
            exit_code=exc.exit_code if isinstance(exc, GeneratorError) else 1,
            changes=planned,
            actual_changes=[],
            affected_targets=["template launch scripts"],
            rollback=rollback,
            error=_template_error(exc),
            diagnostics=diagnostics,
            next_action="The previous template state was restored. Correct the failure and rerun template sync.",
            metadata={"template": plan.snapshot.state.manifest()},
        )
    recovery, recovery_path = _recovery_action(
        transaction,
        "Template rollback is incomplete; preserve the durable authority, restore the reported residue, and run Doctor.",
    )
    return CommandResult(
        "template",
        status="partial",
        exit_code=3,
        changes=planned,
        actual_changes=residue,
        affected_targets=["template launch scripts"],
        rollback=rollback,
        recovery=recovery,
        error=ErrorInfo(
            "cancellation_rollback_partial"
            if interrupted
            else "template_sync_rollback_partial",
            "rollback",
            str(exc),
        ),
        diagnostics=diagnostics,
        next_action=recovery.summary,
        metadata={
            "template": plan.snapshot.state.manifest(),
            "recovery_path": recovery_path,
            "cancellation_requested": interrupted,
            "cancellation_status": "partial" if interrupted else "not_requested",
            "cancellation_message": (
                "Template sync was interrupted and exact restoration could not be verified."
                if interrupted
                else None
            ),
        },
    )


def _retained_conflict_result(
    root: Path,
    transaction: Transaction,
    plan: _TemplateSyncPlan,
    planned: list[Change],
    exc: BaseException,
) -> CommandResult:
    interrupted = isinstance(exc, KeyboardInterrupt) or (
        isinstance(exc, TransactionCleanupError) and exc.interrupted
    )
    recovery, recovery_path = _recovery_action(
        transaction,
        "Template retention recovery is ambiguous. Preserve the transaction "
        "journal/state, restore scripts/runPlugin.sh and scripts/runPlugin.ps1 "
        "to the retained pre-sync bytes and metadata, then run Doctor.",
    )
    residue = (
        []
        if transaction.conditional_conflict_is_durable()
        and not transaction.conditional_destination_parents_match()
        else _template_residue(root, plan)
    )
    return CommandResult(
        "template",
        status="partial",
        exit_code=3,
        changes=planned,
        actual_changes=residue,
        affected_targets=["template launch scripts"],
        rollback=RollbackResult(True, "partial", []),
        recovery=recovery,
        error=ErrorInfo(
            "cancellation_rollback_partial"
            if interrupted
            else "template_sync_rollback_partial",
            "rollback",
            str(exc),
        ),
        diagnostics=[str(exc)],
        next_action=recovery.summary,
        metadata={
            "template": plan.snapshot.state.manifest(),
            "recovery_path": recovery_path,
            "cancellation_requested": interrupted,
            "cancellation_status": "partial" if interrupted else "not_requested",
            "cancellation_message": (
                "Template sync was interrupted and exact restoration could not be verified."
                if interrupted
                else None
            ),
        },
    )


def _durable_result(
    transaction: Transaction,
    plan: _TemplateSyncPlan,
    planned: list[Change],
    state: TemplateCapabilityState,
    exc: BaseException,
) -> CommandResult:
    interrupted = isinstance(exc, KeyboardInterrupt)
    try:
        transaction.finish_commit()
    except BaseException as cleanup_exc:
        recovery, recovery_path = _recovery_action(
            transaction,
            "Template changes are durably committed but transaction cleanup is incomplete; preserve the authority and run Doctor.",
        )
        return CommandResult(
            "template",
            status="partial",
            exit_code=3,
            changes=planned,
            actual_changes=planned,
            affected_targets=["template launch scripts"],
            rollback=RollbackResult(False, "not_needed", []),
            recovery=recovery,
            error=ErrorInfo(
                "template_commit_cleanup_failed", "commit", str(cleanup_exc)
            ),
            diagnostics=[str(exc), str(cleanup_exc)],
            next_action=recovery.summary,
            metadata={
                "template": state.manifest(),
                "commit_durable": True,
                "recovery_path": recovery_path,
                "cancellation_requested": interrupted,
                "cancellation_status": "partial" if interrupted else "not_requested",
            },
        )
    return CommandResult(
        "template",
        changes=planned,
        actual_changes=planned,
        affected_targets=["template launch scripts"],
        rollback=RollbackResult(False, "not_needed", []),
        metadata={
            "template": state.manifest(),
            "no_op": False,
            "commit_durable": True,
            "success_message": "Template capability synchronized",
            "cancellation_requested": interrupted,
            "cancellation_status": "completed" if interrupted else "not_requested",
            "cancellation_message": (
                "Cancellation arrived after the template changes were durably committed."
                if interrupted
                else None
            ),
        },
    )


class TemplateContractService:
    """Expose the template capability as a read-only status or explicit sync."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def status(self) -> CommandResult:
        state = inspect_template_capability(self.root)
        metadata = {"template": state.manifest()}
        if state.state == "current":
            return CommandResult(
                "template",
                affected_targets=["template launch scripts"],
                metadata={
                    **metadata,
                    "success_message": "Template capability is current",
                },
            )
        message = f"Template capability is {state.state}."
        missing = state.state == "missing"
        return CommandResult(
            "template",
            status="failure",
            exit_code=1,
            validation=ValidationResult(
                structural="failed",
                issues=[
                    {
                        "kind": "template_drift",
                        "code": (
                            "SNV4_TEMPLATE_MISSING"
                            if missing
                            else "SNV4_TEMPLATE_DRIFT"
                        ),
                        "severity": "error",
                        "scope": "plugin",
                        "message": message,
                    }
                ],
            ),
            affected_targets=["template launch scripts"],
            next_action=(
                "Restore missing official-template files, then run `supernote-module template sync --yes`."
                if missing
                else "Run `supernote-module template sync --dry-run`, then sync explicitly with --yes."
            ),
            error=ErrorInfo(
                "template_state_failed" if missing else "template_drift",
                "template_preflight" if missing else "template",
                message,
            ),
            metadata=metadata,
        )

    def sync(self, *, dry_run: bool) -> CommandResult:
        plan = _sync_plan(self.root)
        planned = _changes(self.root, plan.candidates)
        if dry_run or not plan.candidates:
            try:
                _validate_sync_preconditions(self.root, plan)
            except PlanConflictError as exc:
                return CommandResult(
                    "template",
                    status="failure",
                    exit_code=1,
                    changes=planned,
                    actual_changes=[],
                    affected_targets=["template launch scripts"],
                    error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                    next_action="The template authority changed during planning. Preserve the external edit, review it, and rerun template sync.",
                    metadata={
                        "dry_run": dry_run,
                        "template": plan.snapshot.state.manifest(),
                    },
                )
            return CommandResult(
                "template",
                changes=planned,
                affected_targets=["template launch scripts"],
                metadata={
                    "dry_run": dry_run,
                    "no_op": not plan.candidates,
                    "template": plan.snapshot.state.manifest(),
                    "success_message": (
                        "Template capability is already current"
                        if not plan.candidates
                        else "Template sync previewed; no files were changed"
                    ),
                },
            )

        directory_metadata = protected_directory_metadata(self.root)
        transaction = Transaction(self.root, "template", TEMPLATE_SCRIPT_PATHS)
        transaction.record_directory_metadata(directory_metadata)
        state = plan.snapshot.state
        try:
            staged_candidates = tuple(
                (candidate, _stage_candidate(transaction, candidate, index))
                for index, candidate in enumerate(plan.candidates)
            )
            _validate_sync_preconditions(self.root, plan)
            _activate_candidates(self.root, transaction, staged_candidates)
            state = inspect_template_capability(self.root)
            if state.state != "current":
                raise TemplateStateError(
                    "template sync did not produce current capability state"
                )
            transaction.commit()
        except BaseException as exc:
            if transaction.commit_is_durable():
                return _durable_result(transaction, plan, planned, state, exc)
            if transaction.conflict_is_durable():
                return _retained_conflict_result(
                    self.root, transaction, plan, planned, exc
                )
            if not transaction.mutated:
                return _abandon_result(transaction, plan, planned, exc)
            return _rollback_result(self.root, transaction, plan, planned, exc)

        return CommandResult(
            "template",
            changes=planned,
            actual_changes=planned,
            affected_targets=["template launch scripts"],
            rollback=RollbackResult(False, "not_needed", []),
            metadata={
                "template": state.manifest(),
                "no_op": False,
                "success_message": "Template capability synchronized",
            },
        )
