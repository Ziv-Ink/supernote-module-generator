"""Semantic command and result models shared by every output renderer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import __version__


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
        return {
            "path": self.path,
            "action": self.action,
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
            "issues": [dict(issue) for issue in self.issues],
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "requirement": self.requirement,
            "status": self.status,
            "detected_version": self.detected_version,
            "path": self.path,
            "message": self.message,
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
    dependency: Optional[DependencyResult] = None
    validation: Optional[ValidationResult] = None
    doctor: Optional[DoctorResult] = None
    rollback: RollbackResult = field(default_factory=RollbackResult)
    warnings: List[WarningInfo] = field(default_factory=list)
    recovery: Optional[RecoveryAction] = None
    error: Optional[ErrorInfo] = None
    metadata: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self, *, debug: bool = False) -> Dict[str, Any]:
        return {
            "schema_version": "1.0",
            "tool_version": __version__,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "module": self.module.to_dict() if self.module is not None else None,
            "modules": [
                module.to_dict(include_validation=True) for module in self.modules
            ],
            "changes": [change.to_dict() for change in self.changes],
            "dependency": (
                self.dependency.to_dict() if self.dependency is not None else None
            ),
            "validation": (
                self.validation.to_dict() if self.validation is not None else None
            ),
            "doctor": self.doctor.to_dict() if self.doctor is not None else None,
            "rollback": self.rollback.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "recovery": self.recovery.to_dict() if self.recovery is not None else None,
            "error": (
                self.error.to_dict(include_internal=debug)
                if self.error is not None
                else None
            ),
        }
