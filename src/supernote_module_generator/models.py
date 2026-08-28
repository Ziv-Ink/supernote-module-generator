"""Semantic command and result models shared by every output renderer."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional

from . import __version__


def _serialized_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    value = dict(issue)
    kind = str(value.get("kind", "validation_failed"))
    if "code" not in value:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", kind).strip("_").upper()
        value["code"] = f"SNV4_{normalized or 'VALIDATION_FAILED'}"
    value.setdefault("severity", "error")
    value.setdefault(
        "scope",
        "toolchain"
        if kind == "build"
        else "plugin"
        if kind in {"parent_dependency", "gradle_integration", "parent_integration"}
        else "feature",
    )
    value.setdefault("message", "Validation failed.")
    return value


@dataclass(frozen=True)
class ModuleInfo:
    package_name: str
    javascript_name: str
    type: str
    type_label: str
    path: str
    implementation_path: str
    android_namespace: str
    package_version: str
    validation: Optional["ValidationResult"] = None

    def to_dict(self, *, include_validation: bool = False) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "package_name": self.package_name,
            "javascript_name": self.javascript_name,
            "type": self.type,
            "type_label": self.type_label,
            "path": self.path,
            "implementation_path": self.implementation_path,
            "android_namespace": self.android_namespace,
            "package_version": self.package_version,
        }
        if include_validation:
            value["validation"] = (
                self.validation.to_dict() if self.validation is not None else None
            )
        return value


@dataclass(frozen=True)
class Change:
    path: str
    action: str
    ownership: str

    def to_dict(self) -> Dict[str, str]:
        action = {
            "created": "create",
            "updated": "update",
            "removed": "delete",
            "preserved": "preserve",
        }.get(self.action, self.action)
        return {
            "path": self.path,
            "action": action,
            "ownership": self.ownership,
        }


@dataclass(frozen=True)
class DependencyResult:
    requested: bool
    manager: Optional[str]
    status: str
    verified: bool
    command: List[str]
    duration_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "manager": self.manager,
            "status": self.status,
            "verified": self.verified,
            "command": list(self.command),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class ValidationResult:
    structural: str = "not_requested"
    integration: str = "not_requested"
    dependency_link: str = "not_requested"
    build: str = "not_requested"
    issues: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structural": self.structural,
            "integration": self.integration,
            "dependency_link": self.dependency_link,
            "build": self.build,
            "issues": [_serialized_issue(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class DoctorCheckResult:
    id: str
    label: str
    requirement: str
    status: str
    detected_version: Optional[str]
    path: Optional[str]
    message: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "configured": False,
            "found": False,
            "selected": False,
            "executable_probed": False,
            "compiler_probed": False,
            "project_built": False,
            "device_tested": False,
        }
        metadata.update(self.metadata)
        return {
            "id": self.id,
            "label": self.label,
            "requirement": self.requirement,
            "status": self.status,
            "detected_version": self.detected_version,
            "path": self.path,
            "message": self.message,
            "metadata": metadata,
        }


@dataclass(frozen=True)
class DoctorResult:
    scope: str
    required_passed: bool
    required_issue_count: int
    advisory_count: int
    checks: List[DoctorCheckResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "required_passed": self.required_passed,
            "required_issue_count": self.required_issue_count,
            "advisory_count": self.advisory_count,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class RollbackResult:
    attempted: bool = False
    status: str = "not_needed"
    restored: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "status": self.status,
            "restored": list(self.restored),
        }


@dataclass(frozen=True)
class WarningInfo:
    kind: str
    message: str
    phase: str
    recovery: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "phase": self.phase,
            "recovery": self.recovery,
        }


@dataclass(frozen=True)
class RecoveryAction:
    summary: str
    command: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary, "command": list(self.command)}


@dataclass(frozen=True)
class SubprocessError:
    command: List[str]
    exit_code: int
    relevant_lines: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "relevant_lines": list(self.relevant_lines),
        }


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    phase: str
    message: str
    subprocess: Optional[SubprocessError] = None
    internal: Optional[Dict[str, Any]] = None

    def to_dict(self, *, include_internal: bool = False) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "subprocess": (
                self.subprocess.to_dict() if self.subprocess is not None else None
            ),
        }
        if include_internal and self.internal is not None:
            value["internal"] = dict(self.internal)
        return value


@dataclass
class CommandResult:
    command: str
    status: str = "success"
    exit_code: int = 0
    duration_ms: int = 0
    module: Optional[ModuleInfo] = None
    modules: List[ModuleInfo] = field(default_factory=list)
    changes: List[Change] = field(default_factory=list)
    actual_changes: Optional[List[Change]] = None
    dependency: Optional[DependencyResult] = None
    validation: Optional[ValidationResult] = None
    doctor: Optional[DoctorResult] = None
    rollback: RollbackResult = field(default_factory=RollbackResult)
    warnings: List[WarningInfo] = field(default_factory=list)
    recovery: Optional[RecoveryAction] = None
    error: Optional[ErrorInfo] = None
    requested_targets: List[str] = field(default_factory=list)
    affected_targets: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    next_action: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self, *, debug: bool = False) -> Dict[str, Any]:
        requested_targets = self.requested_targets or list(
            self.metadata.get("requested_targets", [])
        )
        affected_targets = self.affected_targets or list(
            self.metadata.get("affected_targets", [])
        )
        diagnostics = self.diagnostics or list(
            self.metadata.get("diagnostics", [])
        )
        next_action = self.next_action or self.metadata.get("next_action")
        issues = (
            self.validation.to_dict()["issues"]
            if self.validation is not None
            else []
        )
        cancellation_requested = bool(
            self.metadata.get("cancellation_requested")
            or self.status == "cancelled"
            or (self.error is not None and self.error.kind.startswith("cancellation"))
        )
        cancellation_status = self.metadata.get("cancellation_status") or (
            "completed"
            if self.status == "cancelled"
            else "partial"
            if cancellation_requested
            else "not_requested"
        )
        cancellation_reason = (
            self.metadata.get("cancellation_message")
            if cancellation_requested
            else None
        )
        serialized_changes = [change.to_dict() for change in self.changes]
        actual_source = (
            self.actual_changes
            if self.actual_changes is not None
            else self.changes
        )
        actual_changes = [
            change
            for change in (item.to_dict() for item in actual_source)
            if change["action"] != "preserve"
        ]
        if (
            self.metadata.get("dry_run")
            or self.metadata.get("no_op")
            or self.rollback.status == "completed"
            or self.status == "cancelled"
        ):
            actual_changes = []
        return {
            "schema_version": "4.0",
            "tool_version": __version__,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "requested_targets": requested_targets,
            "affected_targets": affected_targets,
            "module": self.module.to_dict() if self.module is not None else None,
            "modules": [
                module.to_dict(include_validation=True) for module in self.modules
            ],
            "changes": serialized_changes,
            "actual_changes": actual_changes,
            "issues": [dict(issue) for issue in issues],
            "dependency": (
                self.dependency.to_dict() if self.dependency is not None else None
            ),
            "validation": (
                self.validation.to_dict() if self.validation is not None else None
            ),
            "doctor": self.doctor.to_dict() if self.doctor is not None else None,
            "rollback": self.rollback.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "cancellation": {
                "requested": cancellation_requested,
                "status": cancellation_status,
                "reason": cancellation_reason,
            },
            "diagnostics": diagnostics,
            "next_action": next_action,
            "recovery": self.recovery.to_dict() if self.recovery is not None else None,
            "error": (
                self.error.to_dict(include_internal=debug)
                if self.error is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }
