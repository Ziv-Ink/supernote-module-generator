"""Executable Doctor probes for module-generation and generated-build inputs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence, Tuple

from .models import (
    Change,
    CommandResult,
    DoctorCheckResult,
    DoctorResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
)
from .errors import FilesystemError
from .filesystem import (
    ProtectedSourceGuard,
    ProtectedSourceRestoreError,
    finish_protected_source_guard,
    read_regular_bytes_no_follow,
)
from .platform_tools import (
    gradle_wrapper_command,
    gradle_wrapper_path,
    ndk_compiler_path,
)
from .project import manager_evidence, resolve_plugin_root
from .rendering import ProgressReporter, Renderer
from .subprocesses import run_process


_GRADLE_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:ext\.)?"
    r"(?P<name>ndkVersion|compileSdkVersion|buildToolsVersion)|"
    r"extra\[\s*['\"](?P<extra_name>ndkVersion|compileSdkVersion|buildToolsVersion)"
    r"['\"]\s*\])\s*=\s*"
    r"(?P<value>\"[^\"]+\"|'[^']+'|\d+)\s*;?\s*$"
)
ANDROID_CMAKE_VERSION = "3.22.1"


@dataclass(frozen=True)
class GradleToolchainSelection:
    """Literal Android toolchain selections declared by the plugin root."""

    values: Dict[str, str] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> Optional[str]:
        return self.values.get(name)


def _capability_metadata(**values: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "configured": False,
        "found": False,
        "selected": False,
        "executable_probed": False,
        "compiler_probed": False,
        "project_built": False,
        "device_tested": False,
    }
    metadata.update(values)
    return metadata


def _android_sdk_selection() -> Tuple[Optional[Path], Optional[str], Dict[str, str]]:
    configured = {
        name: value
        for name, value in (
            ("ANDROID_HOME", os.environ.get("ANDROID_HOME")),
            ("ANDROID_SDK_ROOT", os.environ.get("ANDROID_SDK_ROOT")),
        )
        if value
    }
    try:
        resolved = {
            name: Path(value).expanduser().resolve()
            for name, value in configured.items()
        }
    except OSError as exc:
        return None, f"Android SDK selection could not be resolved: {exc}", configured
    if len(set(resolved.values())) > 1:
        rendered = ", ".join(
            f"{name}={configured[name]}" for name in sorted(configured)
        )
        return (
            None,
            f"Android SDK environment selections conflict: {rendered}.",
            configured,
        )
    selected = next(iter(resolved.values()), None)
    if selected is None:
        return (
            None,
            "ANDROID_HOME or ANDROID_SDK_ROOT must select an Android SDK.",
            configured,
        )
    return selected, None, configured


def _project_toolchain_selection(root: Path) -> GradleToolchainSelection:
    """Read the official template's literal root selections without Gradle.

    V4 intentionally does not guess through arbitrary Gradle expressions. A
    missing, dynamic, or conflicting selection is reported as uninspectable so
    Doctor cannot claim that an unrelated installed tool is project-selected.
    """

    candidates: Dict[str, List[Tuple[str, str]]] = {
        "ndkVersion": [],
        "compileSdkVersion": [],
        "buildToolsVersion": [],
    }
    for relative in ("android/build.gradle", "android/build.gradle.kts"):
        path = root / relative
        if not path.exists():
            continue
        try:
            content, _metadata = read_regular_bytes_no_follow(path)
            text = content.decode("utf-8-sig")
            text = re.sub(
                r"/\*.*?\*/",
                lambda match: "".join(
                    "\n" if character == "\n" else " "
                    for character in match.group(0)
                ),
                text,
                flags=re.DOTALL,
            )
            lines = text.splitlines()
        except (FilesystemError, UnicodeError) as exc:
            return GradleToolchainSelection(
                errors={name: f"Could not read {relative}: {exc}" for name in candidates}
            )
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.split("//", 1)[0]
            match = _GRADLE_ASSIGNMENT.match(line)
            if match is None:
                continue
            value = match.group("value")
            if value[:1] in {"\"", "'"}:
                value = value[1:-1]
            name = match.group("name") or match.group("extra_name")
            assert name is not None
            candidates[name].append(
                (value, f"{relative}:{line_number}")
            )

    properties = root / "android/gradle.properties"
    if properties.exists():
        try:
            content, _metadata = read_regular_bytes_no_follow(properties)
            lines = content.decode("utf-8-sig").splitlines()
        except (FilesystemError, UnicodeError) as exc:
            return GradleToolchainSelection(
                errors={
                    name: f"Could not read android/gradle.properties: {exc}"
                    for name in candidates
                }
            )
        property_names = {
            "ndkVersion": "ndkVersion",
            "android.ndkVersion": "ndkVersion",
            "compileSdkVersion": "compileSdkVersion",
            "android.compileSdk": "compileSdkVersion",
            "buildToolsVersion": "buildToolsVersion",
            "android.buildToolsVersion": "buildToolsVersion",
        }
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", "!")) or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            canonical = property_names.get(key)
            if canonical is not None and value:
                candidates[canonical].append(
                    (value, f"android/gradle.properties:{line_number}")
                )

    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for name, found in candidates.items():
        distinct = {value for value, _ in found}
        if not found:
            errors[name] = (
                f"No literal {name} selection was found in the root Android "
                "Gradle configuration."
            )
        elif len(distinct) != 1:
            rendered = ", ".join(f"{value} at {source}" for value, source in found)
            errors[name] = f"Conflicting {name} selections were found: {rendered}."
        else:
            values[name] = found[0][0]
            sources[name] = found[0][1]
    return GradleToolchainSelection(values, sources, errors)


def _version_tuple(value: Optional[str]) -> Tuple[int, ...]:
    if not value:
        return ()
    match = re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)+|\d+)(?![A-Za-z])", value)
    if not match:
        return ()
    parts = tuple(int(part) for part in match.group(1).split("."))
    if len(parts) > 1 and parts[0] == 1:
        return (parts[1], *parts[2:])
    return parts


def _gradle_version(output: str) -> Optional[str]:
    match = re.search(r"^Gradle\s+([^\s]+)", output, flags=re.MULTILINE)
    return match.group(1) if match else None


def _gradle_jvm_lines(output: str) -> Tuple[Optional[str], Optional[str]]:
    """Return an effective version and daemon Java home when Gradle reports them."""
    legacy = re.search(r"^JVM:\s*([^\s]+)", output, flags=re.MULTILINE)
    launcher = re.search(r"^Launcher JVM:\s*([^\s]+)", output, flags=re.MULTILINE)
    daemon = re.search(r"^Daemon JVM:\s*(.+)$", output, flags=re.MULTILINE)
    daemon_home = None
    if daemon:
        value = daemon.group(1).strip()
        value = re.sub(r"\s+\((?:from|no JDK specified).*$", "", value)
        if value and (
            value.startswith(("/", "~", "."))
            or re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            daemon_home = value
    return (
        legacy.group(1) if legacy else launcher.group(1) if launcher else None,
        daemon_home,
    )


class DoctorService:
    def __init__(
        self,
        cwd: Path,
        renderer: Renderer,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        platform_name: Optional[str] = None,
    ) -> None:
        self.cwd = cwd.expanduser().resolve()
        self.renderer = renderer
        self.progress = ProgressReporter(renderer)
        self.run = run
        self.platform_name = os.name if platform_name is None else platform_name

    def _phase(self, active: str, completed: str) -> ContextManager[object]:
        # Plain output is commonly redirected or read linearly. The final Doctor
        # table already reports every check, so static progress lines only repeat
        # the same information and make the report harder to scan.
        if self.renderer.plain:
            return nullcontext()
        return self.progress.phase(active, completed)

    def _verbose_stream(self, destination: str, content: str) -> None:
        target = self.renderer.stdout if destination == "stdout" else self.renderer.stderr
        target.write(content)
        target.flush()

    def _is_executable(self, path: Path) -> bool:
        return path.is_file() and (
            self.platform_name == "nt" or os.access(path, os.X_OK)
        )

    def execute(self, scope: str, *, build: bool = False) -> CommandResult:
        checks: List[DoctorCheckResult] = []
        build_result: Optional[CommandResult] = None
        with self._phase("Checking project", "Checked project"):
            try:
                root = resolve_plugin_root(self.cwd)
            except Exception:
                root = self.cwd
                checks.append(
                    DoctorCheckResult(
                        "project",
                        "Project",
                        "required",
                        "failed",
                        None,
                        str(self.cwd),
                        "The current directory is not a Supernote plugin.",
                        _capability_metadata(),
                    )
                )
                valid_root = False
            else:
                checks.append(
                    DoctorCheckResult(
                        "project",
                        "Project",
                        "required",
                        "passed",
                        None,
                        str(root),
                        "Plugin root and package metadata are available.",
                        _capability_metadata(
                            configured=True,
                            found=True,
                            selected=True,
                        ),
                    )
                )
                valid_root = True

        selection = (
            _project_toolchain_selection(root)
            if valid_root
            else GradleToolchainSelection(
                errors={
                    name: "Project selection is unavailable outside a plugin root."
                    for name in (
                        "ndkVersion",
                        "compileSdkVersion",
                        "buildToolsVersion",
                    )
                }
            )
        )

        guard = ProtectedSourceGuard(root) if valid_root else None
        try:
            with self._phase("Checking JavaScript tools", "Checked JavaScript tools"):
                checks.extend(self._javascript_checks(root, valid_root))
            with self._phase("Checking Android tools", "Checked Android tools"):
                checks.extend(self._android_checks(root, valid_root, selection))
            if scope == "plugin":
                with self._phase("Checking native tools", "Checked native tools"):
                    checks.extend(self._native_checks(selection))
            if scope == "plugin":
                with self._phase("Checking JSI runtime", "Checked JSI runtime"):
                    checks.extend(self._jsi_runtime_checks())
            if build:
                with self._phase("Building project", "Built project"):
                    build_result = self._project_build_result(root, valid_root)
                    checks.append(
                        self._project_build_check(root, valid_root, build_result)
                    )
        except BaseException as exc:
            if guard is None:
                raise
            stage_result = (
                self._stage_failure_result(scope, checks, exc, build_result)
                if isinstance(exc, Exception)
                else None
            )
            try:
                mutations, finish_interrupted = self._finish_guard(guard)
            except ProtectedSourceRestoreError as restore_exc:
                return self._guard_failure_result(
                    scope,
                    checks,
                    restore_exc,
                    interrupted=(
                        isinstance(exc, KeyboardInterrupt)
                        or restore_exc.interrupted
                    ),
                    build_result=stage_result or build_result,
                )
            if isinstance(exc, KeyboardInterrupt):
                return self._cancelled_result(
                    scope,
                    checks,
                    mutations,
                    build_result=build_result,
                )
            if stage_result is not None and finish_interrupted:
                return self._cancelled_result(
                    scope,
                    checks,
                    mutations,
                    build_result=stage_result,
                )
            if mutations:
                checks.append(self._source_mutation_check(mutations, restored=True))
                return self._final_result(
                    scope,
                    checks,
                    changes=self._mutation_changes(mutations),
                    rollback=RollbackResult(True, "completed", []),
                    build_result=stage_result or build_result,
                )
            if stage_result is not None:
                return stage_result
            raise
        if guard is not None:
            try:
                mutations, finish_interrupted = self._finish_guard(guard)
            except ProtectedSourceRestoreError as exc:
                return self._guard_failure_result(
                    scope,
                    checks,
                    exc,
                    interrupted=exc.interrupted,
                    build_result=build_result,
                )
            if finish_interrupted:
                return self._cancelled_result(
                    scope,
                    checks,
                    mutations,
                    build_result=build_result,
                )
            if mutations:
                checks.append(self._source_mutation_check(mutations, restored=True))
                return self._final_result(
                    scope,
                    checks,
                    changes=self._mutation_changes(mutations),
                    rollback=RollbackResult(True, "completed", []),
                    build_result=build_result,
                )
        return self._final_result(scope, checks, build_result=build_result)

    def _finish_guard(
        self,
        guard: ProtectedSourceGuard,
    ) -> Tuple[Tuple[str, ...], bool]:
        return finish_protected_source_guard(
            guard, context_label="Doctor"
        )

    def _cancelled_result(
        self,
        scope: str,
        checks: List[DoctorCheckResult],
        mutations: Tuple[str, ...],
        *,
        build_result: Optional[CommandResult],
    ) -> CommandResult:
        if mutations and not any(
            check.id == "doctor_source_integrity" for check in checks
        ):
            checks.append(self._source_mutation_check(mutations, restored=True))
        metadata = self._composed_result_metadata(build_result)
        nested_partial = bool(
            build_result is not None
            and (
                build_result.status == "partial"
                or build_result.rollback.status == "partial"
                or build_result.metadata.get("cancellation_status") == "partial"
            )
        )
        metadata.update(
            {
                "phase_label": "Doctor",
                "cancellation_requested": True,
                "cancellation_status": "partial" if nested_partial else "completed",
                "cancellation_message": (
                    build_result.metadata.get("cancellation_message")
                    if nested_partial and build_result is not None
                    else "Doctor was interrupted. Protected source state was restored."
                ),
            }
        )
        nested_outcome = bool(
            build_result is not None
            and build_result.status in {"failure", "partial"}
        )
        nested_changes = list(build_result.changes) if nested_outcome else []
        outer_changes = self._mutation_changes(mutations)
        changes = [*nested_changes, *outer_changes]
        rollback = (
            build_result.rollback
            if build_result is not None
            and build_result.rollback.status == "partial"
            else RollbackResult(True, "completed", [])
        )
        return CommandResult(
            "doctor",
            status=(build_result.status if nested_outcome else "cancelled"),
            exit_code=(build_result.exit_code if nested_outcome else 130),
            doctor=self._doctor_result(scope, checks),
            changes=changes,
            validation=(build_result.validation if build_result is not None else None),
            diagnostics=(list(build_result.diagnostics) if build_result else []),
            rollback=rollback,
            recovery=(build_result.recovery if nested_outcome else None),
            error=(build_result.error if nested_outcome else None),
            requested_targets=(
                list(build_result.requested_targets) if build_result is not None else []
            ),
            affected_targets=(
                list(build_result.affected_targets) if build_result is not None else []
            ),
            next_action=(build_result.next_action if nested_outcome else None),
            metadata=metadata,
        )

    def _build_result_metadata(
        self,
        build_result: Optional[CommandResult],
    ) -> Dict[str, Any]:
        if build_result is None:
            return {}
        metadata = dict(build_result.metadata)
        metadata["authoritative_build_result"] = {
            "command": build_result.command,
            "status": build_result.status,
            "exit_code": build_result.exit_code,
            "rollback": build_result.rollback.to_dict(),
            "error": (
                build_result.error.to_dict()
                if build_result.error is not None
                else None
            ),
            "next_action": build_result.next_action,
            "recovery": (
                build_result.recovery.to_dict()
                if build_result.recovery is not None
                else None
            ),
        }
        return metadata

    def _composed_result_metadata(
        self,
        result: Optional[CommandResult],
    ) -> Dict[str, Any]:
        if result is None:
            return {}
        if result.metadata.get("authority_kind") == "doctor_stage":
            return dict(result.metadata)
        return self._build_result_metadata(result)

    def _stage_failure_result(
        self,
        scope: str,
        checks: List[DoctorCheckResult],
        exc: Exception,
        build_result: Optional[CommandResult],
    ) -> CommandResult:
        """Preserve a Doctor probe failure as authority during guard cleanup."""

        error = ErrorInfo("doctor_stage_failed", "doctor", str(exc))
        next_action = "Correct the Doctor probe failure and rerun Doctor."
        if not any(check.id == "doctor_probe_execution" for check in checks):
            checks.append(
                DoctorCheckResult(
                    "doctor_probe_execution",
                    "Doctor probe execution",
                    "required",
                    "failed",
                    None,
                    str(self.cwd),
                    f"Doctor could not complete a required probe: {exc}",
                    _capability_metadata(
                        phase="doctor",
                        exception_type=type(exc).__name__,
                    ),
                )
            )
        metadata = self._build_result_metadata(build_result)
        metadata.update(
            {
                "phase_label": "Doctor",
                "next_action": next_action,
                "authority_kind": "doctor_stage",
                "authoritative_stage_result": {
                    "status": "failure",
                    "exit_code": 1,
                    "error": error.to_dict(),
                    "next_action": next_action,
                    "requested_targets": (
                        list(build_result.requested_targets)
                        if build_result is not None
                        else []
                    ),
                    "affected_targets": (
                        list(build_result.affected_targets)
                        if build_result is not None
                        else []
                    ),
                    "validation": (
                        build_result.validation.to_dict()
                        if build_result is not None
                        and build_result.validation is not None
                        else None
                    ),
                    "diagnostics": (
                        list(build_result.diagnostics)
                        if build_result is not None
                        else []
                    ),
                },
            }
        )
        return CommandResult(
            "doctor",
            status="failure",
            exit_code=1,
            doctor=self._doctor_result(scope, checks),
            changes=(list(build_result.changes) if build_result is not None else []),
            validation=(build_result.validation if build_result is not None else None),
            rollback=(
                build_result.rollback
                if build_result is not None
                else RollbackResult()
            ),
            recovery=(build_result.recovery if build_result is not None else None),
            error=error,
            requested_targets=(
                list(build_result.requested_targets) if build_result is not None else []
            ),
            affected_targets=(
                list(build_result.affected_targets) if build_result is not None else []
            ),
            diagnostics=(
                list(build_result.diagnostics) if build_result is not None else []
            ),
            next_action=next_action,
            metadata=metadata,
        )

    def _doctor_result(
        self,
        scope: str,
        checks: List[DoctorCheckResult],
    ) -> DoctorResult:
        required_failed = [
            check
            for check in checks
            if check.requirement == "required" and check.status == "failed"
        ]
        advisories = [
            check
            for check in checks
            if check.requirement == "advisory" and check.status == "warning"
        ]
        return DoctorResult(
            scope,
            not required_failed,
            len(required_failed),
            len(advisories),
            checks,
        )

    def _final_result(
        self,
        scope: str,
        checks: List[DoctorCheckResult],
        *,
        changes: Optional[List[Change]] = None,
        rollback: Optional[RollbackResult] = None,
        build_result: Optional[CommandResult] = None,
    ) -> CommandResult:
        doctor = self._doctor_result(scope, checks)
        required_failed = [
            check
            for check in checks
            if check.requirement == "required" and check.status == "failed"
        ]
        check_diagnostics = [
            str(path)
            for check in checks
            for path in check.metadata.get("diagnostics", [])
        ]
        diagnostics = list(
            dict.fromkeys(
                [
                    *(build_result.diagnostics if build_result is not None else []),
                    *check_diagnostics,
                ]
            )
        )
        if build_result is not None and (
            build_result.status != "success" or build_result.exit_code != 0
        ):
            metadata = self._composed_result_metadata(build_result)
            metadata["phase_label"] = "Doctor"
            is_failure = build_result.status == "failure"
            next_action = build_result.next_action
            error = build_result.error
            if is_failure:
                next_action = next_action or self._required_issue_next_action(
                    required_failed
                )
                error = error or ErrorInfo(
                    "doctor_build_failed",
                    "build",
                    "Authoritative V4 state validation or the Android build failed.",
                )
            return CommandResult(
                "doctor",
                status=build_result.status,
                exit_code=build_result.exit_code,
                doctor=doctor,
                changes=list(changes or build_result.changes),
                validation=build_result.validation,
                rollback=rollback or build_result.rollback,
                recovery=build_result.recovery,
                error=error,
                requested_targets=list(build_result.requested_targets),
                affected_targets=list(build_result.affected_targets),
                diagnostics=diagnostics,
                next_action=next_action,
                metadata=metadata,
            )
        if required_failed:
            count = len(required_failed)
            next_action = self._required_issue_next_action(required_failed)
            metadata = self._composed_result_metadata(build_result)
            metadata.update(
                {
                    "phase_label": "Doctor",
                    "next_action": next_action,
                }
            )
            return CommandResult(
                "doctor",
                status="failure",
                exit_code=1,
                doctor=doctor,
                changes=list(
                    changes
                    or (build_result.changes if build_result is not None else ())
                ),
                validation=(
                    build_result.validation if build_result is not None else None
                ),
                rollback=(
                    rollback
                    or (
                        build_result.rollback
                        if build_result is not None
                        else RollbackResult()
                    )
                ),
                recovery=(
                    build_result.recovery if build_result is not None else None
                ),
                requested_targets=(
                    list(build_result.requested_targets)
                    if build_result is not None
                    else []
                ),
                affected_targets=(
                    list(build_result.affected_targets)
                    if build_result is not None
                    else []
                ),
                diagnostics=diagnostics,
                next_action=next_action,
                error=ErrorInfo(
                    "doctor_failed",
                    "doctor",
                    f"Doctor found {count} required issue{'s' if count != 1 else ''}.",
                ),
                metadata=metadata,
            )
        return CommandResult(
            "doctor",
            doctor=doctor,
            changes=list(changes or ()),
            validation=(build_result.validation if build_result is not None else None),
            rollback=rollback or RollbackResult(),
            diagnostics=diagnostics,
            next_action=(build_result.next_action if build_result is not None else None),
            recovery=(build_result.recovery if build_result is not None else None),
            error=(build_result.error if build_result is not None else None),
            requested_targets=(
                list(build_result.requested_targets) if build_result is not None else []
            ),
            affected_targets=(
                list(build_result.affected_targets) if build_result is not None else []
            ),
            metadata=self._composed_result_metadata(build_result),
        )

    def _source_mutation_check(
        self,
        mutations: Tuple[str, ...],
        *,
        restored: bool,
    ) -> DoctorCheckResult:
        return DoctorCheckResult(
            "doctor_source_integrity",
            "Doctor source integrity",
            "required",
            "failed",
            None,
            str(self.cwd),
            "A Doctor probe changed protected source state; the original state "
            + ("was restored." if restored else "could not be restored exactly."),
            _capability_metadata(
                configured=True,
                found=True,
                selected=True,
                mutations=list(mutations),
                restored=restored,
            ),
        )

    def _mutation_changes(self, mutations: Tuple[str, ...]) -> List[Change]:
        action_names = {
            "created": "create",
            "modified": "update",
            "deleted": "delete",
        }
        changes: List[Change] = []
        for mutation in mutations:
            action, separator, relative = mutation.partition(":")
            if separator and action in action_names:
                changes.append(Change(relative, action_names[action], "user source"))
        return changes

    def _guard_failure_result(
        self,
        scope: str,
        checks: List[DoctorCheckResult],
        exc: ProtectedSourceRestoreError,
        *,
        interrupted: bool = False,
        build_result: Optional[CommandResult] = None,
    ) -> CommandResult:
        if exc.mutations:
            checks.append(
                self._source_mutation_check(exc.mutations, restored=False)
            )
        residue = (
            self._mutation_changes(exc.remaining)
            if exc.remaining_verified
            else []
        )
        nested_changes = list(build_result.changes) if build_result is not None else []
        changes = [*nested_changes, *residue]
        if not exc.remaining_verified:
            reason = (
                "Doctor could not inventory current protected-source residue; "
                "exact restoration is unverified."
            )
        elif exc.remaining:
            reason = (
                "Doctor was interrupted and exact protected-source restoration "
                "could not be verified."
                if interrupted
                else "Doctor could not restore protected source state exactly."
            )
        else:
            reason = (
                "Protected source matches the pre-command baseline, but Doctor "
                "guard cleanup did not complete."
            )
        recovery_summary = f"Protected source backup retained at {exc.recovery_path}."
        next_action = (
            f"Preserve the recovery backup at {exc.recovery_path}, restore the "
            "listed residue, then rerun Doctor."
            if exc.remaining_verified and exc.remaining
            else f"Preserve the recovery backup at {exc.recovery_path}, then "
            "rerun Doctor; current residue could not be inventoried."
            if not exc.remaining_verified
            else f"Preserve the recovery backup at {exc.recovery_path}, then "
            "rerun Doctor to complete guard cleanup."
        )
        if build_result is not None and build_result.recovery is not None:
            recovery_summary += (
                " Authoritative build recovery: "
                + build_result.recovery.summary
            )
        if build_result is not None and build_result.next_action:
            next_action += " Then: " + build_result.next_action
        metadata = self._composed_result_metadata(build_result)
        metadata.update(
            {
                "phase_label": "Doctor",
                "recovery_path": str(exc.recovery_path),
                "cancellation_requested": interrupted,
                "cancellation_status": "partial" if interrupted else "not_requested",
                "cancellation_message": reason if interrupted else None,
                "residue_verified": exc.remaining_verified,
                "restore_diagnostics": list(exc.diagnostics),
            }
        )
        return CommandResult(
            "doctor",
            status="partial",
            exit_code=3,
            doctor=self._doctor_result(scope, checks),
            changes=changes,
            validation=(build_result.validation if build_result is not None else None),
            rollback=RollbackResult(True, "partial", []),
            recovery=RecoveryAction(
                recovery_summary,
                ["supernote-module", "doctor"],
            ),
            error=ErrorInfo(
                (
                    "doctor_source_restore_unverified"
                    if not exc.remaining_verified
                    else "doctor_source_cleanup_failed"
                    if not exc.remaining
                    else "doctor_source_restore_partial"
                ),
                "doctor",
                reason,
            ),
            next_action=next_action,
            diagnostics=(list(build_result.diagnostics) if build_result else []),
            requested_targets=(
                list(build_result.requested_targets) if build_result is not None else []
            ),
            affected_targets=(
                list(build_result.affected_targets) if build_result is not None else []
            ),
            metadata=metadata,
        )

    def _required_issue_next_action(
        self,
        failed: Sequence[DoctorCheckResult],
    ) -> str:
        failed_ids = {check.id for check in failed}
        if "android_project_build" in failed_ids:
            return (
                "Review the Doctor build diagnostics, correct the first integrity or "
                "compiler failure, then rerun `supernote-module doctor --build`."
            )
        if failed_ids in ({"gradle_wrapper"}, {"gradle_wrapper", "gradle_jvm"}):
            wrapper = next(check for check in failed if check.id == "gradle_wrapper")
            relative = (
                "android/gradlew.bat"
                if self.platform_name == "nt"
                else "android/gradlew"
            )
            if wrapper.path is None:
                if self.platform_name == "nt":
                    return (
                        f"Restore `{relative}`, then rerun "
                        "`supernote-module doctor`."
                    )
                return (
                    f"Restore `{relative}`, make it executable, then rerun "
                    "`supernote-module doctor`."
                )
            return (
                f"Fix `{relative}` so it executes successfully, then rerun "
                "`supernote-module doctor`."
            )
        return (
            "Resolve the required checks listed above, then rerun "
            "`supernote-module doctor`."
        )

    def _probe(self, command: Sequence[str], timeout: int = 10) -> Tuple[bool, Optional[str], str]:
        try:
            if self.run is subprocess.run:
                result = run_process(
                    command,
                    cwd=self.cwd,
                    timeout=timeout,
                    stream=(
                        self._verbose_stream
                        if self.renderer.mode == "verbose"
                        else None
                    ),
                )
            else:
                result = self.run(
                    list(command),
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, None, str(exc)
        output = (result.stdout or result.stderr).strip()
        first = output.splitlines()[0] if output else ""
        return result.returncode == 0, first or None, output

    def _tool_check(
        self,
        identifier: str,
        label: str,
        command: str,
        *,
        required: bool = True,
    ) -> DoctorCheckResult:
        path = shutil.which(command)
        requirement = "required" if required else "advisory"
        if path is None:
            return DoctorCheckResult(
                identifier,
                label,
                requirement,
                "failed" if required else "warning",
                None,
                None,
                f"{label} was not found.",
                _capability_metadata(command=[command, "--version"]),
            )
        passed, version, _ = self._probe([path, "--version"])
        return DoctorCheckResult(
            identifier,
            label,
            requirement,
            "passed" if passed else ("failed" if required else "warning"),
            version,
            path,
            f"{label} is available." if passed else f"{label} returned a nonzero status.",
            _capability_metadata(
                found=True,
                selected=True,
                executable_probed=passed,
                command=[path, "--version"],
            ),
        )

    def _javascript_checks(self, root: Path, valid_root: bool) -> List[DoctorCheckResult]:
        checks = [self._tool_check("node", "Node.js", "node")]
        if not valid_root:
            checks.append(
                DoctorCheckResult(
                    "package_manager",
                    "npm or Yarn",
                    "required",
                    "failed",
                    None,
                    None,
                    "Package-manager selection is unavailable outside a plugin root.",
                )
            )
            return checks
        evidence = manager_evidence(root)
        if evidence.conflicting:
            npm = self._tool_check("npm", "npm", "npm", required=False)
            yarn = self._tool_check("yarn", "Yarn", "yarn", required=False)
            checks.extend([npm, yarn])
            healthy = any(check.status == "passed" for check in (npm, yarn))
            checks.append(
                DoctorCheckResult(
                    "package_manager_health",
                    "Package manager",
                    "required",
                    "passed" if healthy else "failed",
                    None,
                    None,
                    "At least one package manager is healthy."
                    if healthy
                    else "Neither npm nor Yarn is healthy.",
                )
            )
            checks.append(
                DoctorCheckResult(
                    "package_manager",
                    "Package manager",
                    "advisory",
                    "warning",
                    None,
                    None,
                    "Both package-lock.json and yarn.lock were found; lifecycle commands require an explicit manager."
                    if healthy
                    else "Both lockfiles exist and neither package manager is healthy.",
                )
            )
            return checks
        manager = evidence.sole or "npm"
        checks.append(self._tool_check("package_manager", "Yarn" if manager == "yarn" else "npm", manager))
        return checks

    def _java_check(self) -> DoctorCheckResult:
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            executable = Path(java_home).expanduser() / "bin" / (
                "java.exe" if self.platform_name == "nt" else "java"
            )
            path = str(executable)
            found = self._is_executable(executable)
        else:
            discovered = shutil.which("java")
            path = discovered
            found = discovered is not None
        command = [path, "--version"] if path is not None else ["java", "--version"]
        if not found or path is None:
            return DoctorCheckResult(
                "java",
                "Java",
                "required",
                "failed",
                None,
                path,
                "JAVA_HOME does not contain bin/java."
                if java_home
                else "Java was not found on PATH.",
                _capability_metadata(
                    configured=bool(java_home),
                    selected=bool(java_home),
                    configured_path=java_home,
                    command=command,
                ),
            )
        passed, version, _ = self._probe(command)
        return DoctorCheckResult(
            "java",
            "Java",
            "required",
            "passed" if passed else "failed",
            version,
            path,
            "The selected Java executable is available."
            if passed
            else "The selected Java executable returned a nonzero status.",
            _capability_metadata(
                configured=bool(java_home),
                found=True,
                selected=True,
                executable_probed=passed,
                configured_path=java_home,
                command=command,
            ),
        )

    def _selected_sdk_component_check(
        self,
        sdk: Optional[Path],
        selection: GradleToolchainSelection,
        *,
        selection_name: str,
        identifier: str,
        label: str,
        relative: Callable[[str], Path],
        sdk_error: Optional[str],
    ) -> DoctorCheckResult:
        selected = selection.value(selection_name)
        source = selection.sources.get(selection_name)
        error = selection.errors.get(selection_name)
        path = sdk / relative(selected) if sdk is not None and selected else None
        required_paths: List[Path] = []
        if identifier == "android_platform":
            found = bool(path and path.is_file())
            if path is not None:
                required_paths.append(path)
        elif identifier == "android_build_tools":
            suffix = ".exe" if self.platform_name == "nt" else ""
            signer = "apksigner.bat" if self.platform_name == "nt" else "apksigner"
            required_paths = (
                [
                    path / f"aapt2{suffix}",
                    path / f"zipalign{suffix}",
                    path / signer,
                ]
                if path is not None
                else []
            )
            found = bool(
                path
                and path.is_dir()
                and all(self._is_executable(required) for required in required_paths)
            )
        else:
            found = bool(path and path.exists())
        if sdk_error:
            message = sdk_error
        elif error:
            message = error
        elif not found:
            message = f"Project-selected {label.lower()} {selected} was not found."
        else:
            message = f"Project-selected {label.lower()} {selected} is available."
        return DoctorCheckResult(
            identifier,
            label,
            "required",
            "passed" if found and error is None and sdk_error is None else "failed",
            selected,
            str(path) if path is not None else None,
            message,
            _capability_metadata(
                configured=selected is not None,
                found=found,
                selected=(
                    selected is not None and error is None and sdk_error is None
                ),
                selection_source=source,
                selection_error=sdk_error or error,
                required_paths=[str(required) for required in required_paths],
            ),
        )

    def _adb_check(self, sdk: Optional[Path]) -> DoctorCheckResult:
        configured = os.environ.get("ADB_BIN")
        default_name = "adb.exe" if self.platform_name == "nt" else "adb"
        if configured:
            candidate = Path(configured).expanduser()
            selected_by = "ADB_BIN"
        elif sdk is not None:
            candidate = sdk / "platform-tools" / default_name
            selected_by = "selected Android SDK"
        else:
            found = shutil.which("adb")
            candidate = Path(found) if found else None
            selected_by = "PATH"
        found = bool(candidate and self._is_executable(candidate))
        command = [str(candidate), "version"] if candidate else [default_name, "version"]
        passed = False
        version = None
        if found and candidate is not None:
            passed, version, _ = self._probe(command)
        return DoctorCheckResult(
            "adb",
            "ADB",
            "advisory",
            "passed" if passed else "warning",
            version,
            str(candidate) if candidate is not None else None,
            f"ADB selected by {selected_by} executed successfully."
            if passed
            else f"ADB selected by {selected_by} was not found or could not execute.",
            _capability_metadata(
                configured=bool(configured),
                found=found,
                selected=candidate is not None,
                executable_probed=passed,
                configured_path=configured,
                selected_by=selected_by,
                command=command,
            ),
        )

    def _android_checks(
        self,
        root: Path,
        valid_root: bool,
        selection: GradleToolchainSelection,
    ) -> List[DoctorCheckResult]:
        java_check = self._java_check()
        if java_check.status == "passed" and _version_tuple(java_check.detected_version) < (17,):
            java_check = DoctorCheckResult(
                "java",
                "Java",
                "required",
                "failed",
                java_check.detected_version,
                java_check.path,
                "Java 17 or newer is required.",
                java_check.metadata,
            )
        sdk, sdk_error, sdk_environment = _android_sdk_selection()
        sdk_ok = bool(
            sdk
            and sdk_error is None
            and sdk.is_dir()
            and (sdk / "platforms").is_dir()
            and (sdk / "build-tools").is_dir()
            and (sdk / "platform-tools").is_dir()
        )
        sdk_check = DoctorCheckResult(
            "android_sdk",
            "Android SDK",
            "required",
            "passed" if sdk_ok else "failed",
            None,
            str(sdk) if sdk else None,
            "Android SDK base layout is available."
            if sdk_ok
            else sdk_error
            or "The selected Android SDK does not contain platforms, build-tools, and platform-tools.",
            _capability_metadata(
                configured=bool(sdk_environment),
                found=sdk_ok,
                selected=sdk is not None and sdk_error is None,
                configured_paths=sdk_environment,
                selection_error=sdk_error,
            ),
        )
        platform_check = self._selected_sdk_component_check(
            sdk,
            selection,
            selection_name="compileSdkVersion",
            identifier="android_platform",
            label="Android platform",
            relative=lambda value: Path("platforms") / f"android-{value}" / "android.jar",
            sdk_error=sdk_error,
        )
        build_tools_check = self._selected_sdk_component_check(
            sdk,
            selection,
            selection_name="buildToolsVersion",
            identifier="android_build_tools",
            label="Android build tools",
            relative=lambda value: Path("build-tools") / value,
            sdk_error=sdk_error,
        )
        adb_check = self._adb_check(sdk)
        if valid_root:
            gradle = gradle_wrapper_path(root, platform_name=self.platform_name)
            if gradle.is_file():
                command = gradle_wrapper_command(
                    gradle,
                    ["--version"],
                    platform_name=self.platform_name,
                )
                passed, _, gradle_output = self._probe(command, timeout=120)
                version = _gradle_version(gradle_output)
            else:
                passed, version, gradle_output = False, None, ""
            gradle_exists = gradle.is_file()
            gradle_check = DoctorCheckResult(
                "gradle_wrapper",
                "Gradle wrapper",
                "required",
                "passed" if passed else "failed",
                version,
                str(gradle) if gradle_exists else None,
                (
                    "Gradle wrapper executed successfully."
                    if passed
                    else "The project Gradle wrapper could not be executed."
                    if gradle_exists
                    else "The project Gradle wrapper is missing."
                ),
                _capability_metadata(
                    configured=True,
                    found=gradle_exists,
                    selected=True,
                    executable_probed=passed,
                    command=command if gradle_exists else None,
                ),
            )
            gradle_jvm_check = self._gradle_jvm_check(
                gradle_output,
                wrapper_passed=passed,
                shell_java=java_check,
            )
        else:
            gradle_check = DoctorCheckResult(
                "gradle_wrapper",
                "Gradle wrapper",
                "required",
                "failed",
                None,
                None,
                "The project Gradle wrapper is unavailable outside a plugin root.",
                _capability_metadata(),
            )
            gradle_jvm_check = DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                None,
                "The Gradle JVM is unavailable outside a plugin root.",
                _capability_metadata(),
            )
        return [
            java_check,
            sdk_check,
            platform_check,
            build_tools_check,
            adb_check,
            gradle_check,
            gradle_jvm_check,
        ]

    def _gradle_jvm_check(
        self,
        output: str,
        *,
        wrapper_passed: bool,
        shell_java: DoctorCheckResult,
    ) -> DoctorCheckResult:
        if not wrapper_passed:
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                None,
                "The Gradle JVM could not be inspected because the wrapper failed.",
                _capability_metadata(),
            )
        reported_version, daemon_home = _gradle_jvm_lines(output)
        detected = reported_version
        path = daemon_home
        if daemon_home:
            executable = Path(daemon_home).expanduser() / "bin" / (
                "java.exe" if self.platform_name == "nt" else "java"
            )
            command = [str(executable), "--version"]
            if not self._is_executable(executable):
                return DoctorCheckResult(
                    "gradle_jvm",
                    "Gradle JVM",
                    "required",
                    "failed",
                    reported_version,
                    str(executable),
                    "Gradle reported a daemon JVM without an executable bin/java.",
                    _capability_metadata(
                        configured=True,
                        found=False,
                        selected=True,
                        command=command,
                    ),
                )
            passed, detected, _ = self._probe(command)
            if not passed:
                return DoctorCheckResult(
                    "gradle_jvm",
                    "Gradle JVM",
                    "required",
                    "failed",
                    detected,
                    str(executable),
                    "Gradle reported a daemon JVM that could not be executed.",
                    _capability_metadata(
                        configured=True,
                        found=executable.is_file(),
                        selected=True,
                        command=command,
                    ),
                )
            path = str(executable)
        if not detected:
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                shell_java.path,
                "Gradle did not report the JVM that will run the Android build.",
                _capability_metadata(
                    found=wrapper_passed,
                    selected=False,
                ),
            )
        if daemon_home is None:
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                detected,
                None,
                "Gradle reported a JVM version but not the exact daemon Java home; "
                "the effective executable could not be inspected.",
                _capability_metadata(
                    found=True,
                    selected=False,
                ),
            )
        gradle_java = _version_tuple(detected)
        if gradle_java < (17,) or gradle_java >= (24,):
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                detected,
                path,
                "The effective Gradle JVM is outside the Java 17 through 23 "
                "range supported by the generated Gradle 8.13 build; check "
                "JAVA_HOME and org.gradle.java.home. Java 17 is recommended.",
                _capability_metadata(
                    configured=daemon_home is not None,
                    found=True,
                    selected=True,
                    executable_probed=daemon_home is not None,
                    command=[path, "--version"] if path is not None else None,
                ),
            )
        return DoctorCheckResult(
            "gradle_jvm",
            "Gradle JVM",
            "required",
            "passed",
            detected,
            path,
            "The effective Gradle JVM is supported (Java 17 through 23).",
            _capability_metadata(
                configured=daemon_home is not None,
                found=True,
                selected=True,
                executable_probed=daemon_home is not None,
                command=[path, "--version"] if path is not None else None,
            ),
        )

    def _native_checks(
        self,
        selection: GradleToolchainSelection,
    ) -> List[DoctorCheckResult]:
        sdk, sdk_error, sdk_environment = _android_sdk_selection()
        cmake = self._selected_cmake_check(sdk, sdk_error)
        ndk_env = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT")
        selected_version = selection.value("ndkVersion")
        selection_error = selection.errors.get("ndkVersion")
        installed: List[Tuple[str, Path]] = []
        installed_error: Optional[str] = None
        if sdk is not None and sdk_error is None:
            ndk_root = sdk / "ndk"
            if ndk_root.is_dir():
                try:
                    ndk_directories = tuple(ndk_root.iterdir())
                except OSError as exc:
                    ndk_directories = ()
                    installed_error = f"Could not inventory installed NDKs: {exc}"
                for path in ndk_directories:
                    if not path.is_dir():
                        continue
                    installed.append(
                        (self._ndk_revision(path) or path.name, path.resolve())
                    )
        installed.sort(key=lambda item: _version_tuple(item[0]), reverse=True)
        installed_check = DoctorCheckResult(
            "android_ndk_installed",
            "Installed Android NDKs",
            "advisory",
            "passed" if installed else "warning",
            ", ".join(version for version, _ in installed) or None,
            str(sdk / "ndk") if sdk is not None else None,
            "Installed NDKs were inventoried separately from project selection."
            if installed
            else installed_error or "No side-by-side Android NDK installations were found.",
            _capability_metadata(
                found=bool(installed),
                installed_versions=[version for version, _ in installed],
                installed_paths=[str(path) for _, path in installed],
                inventory_error=installed_error,
            ),
        )

        ndk: Optional[Path] = None
        selection_source = selection.sources.get("ndkVersion")
        env_revision = None
        if selected_version:
            ndk = next(
                (path for version, path in installed if version == selected_version),
                None,
            )
            if ndk is None and sdk is not None and sdk_error is None:
                exact = sdk / "ndk" / selected_version
                if exact.is_dir():
                    ndk = exact.resolve()
        if ndk_env:
            env_candidate = Path(ndk_env).expanduser()
            env_revision = self._ndk_revision(env_candidate)
            if (
                ndk is None
                and sdk_error is None
                and selected_version
                and env_revision == selected_version
            ):
                ndk = env_candidate.resolve()
        clang = None
        clangxx = None
        detected_version = None
        ndk_healthy = False
        if ndk is not None:
            detected_version = self._ndk_revision(ndk)
            prebuilt = ndk / "toolchains/llvm/prebuilt"
            clang = ndk_compiler_path(
                prebuilt,
                "clang",
                platform_name=self.platform_name,
            )
            if clang is not None:
                clangxx = ndk_compiler_path(
                    prebuilt,
                    "clang++",
                    platform_name=self.platform_name,
                )
                clang_ok, _, _ = self._probe([str(clang), "--version"])
                c23_ok, _, _ = self._probe(
                    [
                        str(clang),
                        "--target=aarch64-linux-android27",
                        "-std=c23",
                        "-fsyntax-only",
                        "-x",
                        "c",
                        os.devnull,
                    ]
                )
                cpp23_ok = False
                if clangxx is not None and clangxx.is_file():
                    cpp23_ok, _, _ = self._probe(
                        [
                            str(clangxx),
                            "--target=aarch64-linux-android27",
                            "-std=c++23",
                            "-fsyntax-only",
                            "-x",
                            "c++",
                            os.devnull,
                        ]
                    )
                ndk_healthy = (
                    clang_ok
                    and c23_ok
                    and cpp23_ok
                    and detected_version == selected_version
                    and selection_error is None
                )
        if sdk_error:
            ndk_message = sdk_error
        elif selection_error:
            ndk_message = selection_error
        elif ndk is None:
            mismatch = (
                f" ANDROID_NDK_HOME selects {env_revision}."
                if ndk_env and env_revision != selected_version
                else ""
            )
            ndk_message = (
                f"Project-selected Android NDK {selected_version} was not found."
                f"{mismatch}"
            )
        elif not ndk_healthy:
            ndk_message = (
                f"Project-selected Android NDK {selected_version} was found, but its "
                "C23/C++23 compiler probes failed."
            )
        else:
            ndk_message = (
                f"Project-selected Android NDK {selected_version} passed C23 and C++23 compiler probes."
            )
        ndk_check = DoctorCheckResult(
            "android_ndk",
            "Android NDK",
            "required",
            "passed" if ndk_healthy else "failed",
            detected_version,
            str(ndk) if ndk else None,
            ndk_message,
            _capability_metadata(
                configured=selected_version is not None,
                found=ndk is not None,
                selected=(
                    selected_version is not None
                    and selection_error is None
                    and sdk_error is None
                ),
                executable_probed=ndk_healthy,
                compiler_probed=ndk_healthy,
                selection_source=selection_source,
                selection_error=selection_error,
                sdk_selection_error=sdk_error,
                sdk_environment=sdk_environment,
                configured_ndk_path=ndk_env,
                configured_ndk_revision=env_revision,
                commands=(
                    [
                        [str(clang), "--version"],
                        [
                            str(clang),
                            "--target=aarch64-linux-android27",
                            "-std=c23",
                            "-fsyntax-only",
                            "-x",
                            "c",
                            os.devnull,
                        ],
                        [
                            str(clangxx),
                            "--target=aarch64-linux-android27",
                            "-std=c++23",
                            "-fsyntax-only",
                            "-x",
                            "c++",
                            os.devnull,
                        ],
                    ]
                    if clang is not None and clangxx is not None
                    else []
                ),
            ),
        )
        return [cmake, installed_check, ndk_check]

    def _selected_cmake_check(
        self,
        sdk: Optional[Path],
        sdk_error: Optional[str],
    ) -> DoctorCheckResult:
        executable_name = "cmake.exe" if self.platform_name == "nt" else "cmake"
        executable = (
            sdk / "cmake" / ANDROID_CMAKE_VERSION / "bin" / executable_name
            if sdk is not None
            else None
        )
        command = [str(executable), "--version"] if executable else []
        found = bool(executable and self._is_executable(executable))
        passed = False
        detected = None
        if found:
            passed, detected, _ = self._probe(command)
            passed = passed and _version_tuple(detected) == _version_tuple(
                ANDROID_CMAKE_VERSION
            )
        if sdk_error:
            message = sdk_error
        elif not found:
            message = (
                f"Project-selected Android SDK CMake {ANDROID_CMAKE_VERSION} was not found."
            )
        elif not passed:
            message = (
                f"Project-selected Android SDK CMake {ANDROID_CMAKE_VERSION} could not be probed."
            )
        else:
            message = (
                f"Project-selected Android SDK CMake {ANDROID_CMAKE_VERSION} executed successfully."
            )
        return DoctorCheckResult(
            "cmake",
            "CMake",
            "required",
            "passed" if passed and sdk_error is None else "failed",
            detected,
            str(executable) if executable is not None else None,
            message,
            _capability_metadata(
                configured=True,
                found=found,
                selected=executable is not None and sdk_error is None,
                executable_probed=passed,
                selection_source="generated Android externalNativeBuild configuration",
                selected_version=ANDROID_CMAKE_VERSION,
                sdk_selection_error=sdk_error,
                command=command,
            ),
        )

    def _ndk_revision(self, ndk: Path) -> Optional[str]:
        properties = ndk / "source.properties"
        if not properties.exists():
            return None
        try:
            raw, _metadata = read_regular_bytes_no_follow(properties)
            content = raw.decode("utf-8", errors="replace")
        except FilesystemError:
            return None
        match = re.search(
            r"^Pkg\.Revision\s*=\s*(.+)$",
            content,
            flags=re.MULTILINE,
        )
        return match.group(1).strip() if match else None

    def _jsi_runtime_checks(self) -> List[DoctorCheckResult]:
        return [
            DoctorCheckResult(
                "selinux_policy",
                "JSI execution policy",
                "advisory",
                "warning",
                None,
                None,
                "Target PluginHost and SELinux execution policy were not inspected; generated JSI files do not prove runtime execution.",
                _capability_metadata(),
            )
        ]

    def _project_build_result(
        self,
        root: Path,
        valid_root: bool,
    ) -> Optional[CommandResult]:
        if not valid_root:
            return None
        from .v4_cli_operations import V4CliOperationService

        return V4CliOperationService(root).check(build=True)

    def _project_build_check(
        self,
        root: Path,
        valid_root: bool,
        result: Optional[CommandResult],
    ) -> DoctorCheckResult:
        command = ["supernote-module", "check", "--build"]
        if not valid_root:
            return DoctorCheckResult(
                "android_project_build",
                "Android project build",
                "required",
                "failed",
                None,
                str(root),
                "A full project build is unavailable outside a plugin root.",
                _capability_metadata(command=command),
            )
        if result is None:
            raise AssertionError("valid project build check requires a command result")
        validation = result.validation
        passed = bool(
            result.status == "success"
            and result.exit_code == 0
            and validation is not None
            and validation.structural == "passed"
            and validation.integration == "passed"
            and validation.dependency_link == "passed"
            and validation.build == "passed"
        )
        return DoctorCheckResult(
            "android_project_build",
            "Android project build",
            "required",
            "passed" if passed else "failed",
            None,
            str(root / "android"),
            "Authoritative V4 state validation and the full Android build passed."
            if passed
            else "Authoritative V4 state validation or the full Android build failed.",
            _capability_metadata(
                configured=True,
                found=True,
                selected=True,
                executable_probed=True,
                compiler_probed=passed,
                project_built=passed,
                device_tested=False,
                command=command,
                validation=validation.to_dict() if validation is not None else None,
                issues=list(validation.issues) if validation is not None else [],
                diagnostics=list(result.diagnostics),
            ),
        )
