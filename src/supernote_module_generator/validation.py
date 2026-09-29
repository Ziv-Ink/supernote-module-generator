"""Authoritative expected-plan versus actual generated-state validation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Dict, Iterable, Mapping, Tuple

from .errors import FilesystemError
from .filesystem import (
    contained_entry_kind_no_follow,
    read_regular_bytes_no_follow,
)
from .generation_plan import ArtifactAction
from .generation_service import GenerationService
from .integrity_manifest import INCOMPLETE_MARKER_PATH
from .jvm_manifest import JvmSourceManifest
from .models import SubprocessError
from .project_model import ProjectFeature, ProjectModel
from .semantic import SemanticApi


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    scope: str
    message: str
    feature_id: str | None = None
    path: str | None = None
    source_range: Dict[str, int] | None = None
    expected: str | None = None
    actual: str | None = None
    suggested_command: str | None = None

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "scope": self.scope,
            "message": self.message,
        }
        optional = {
            "feature_id": self.feature_id,
            "path": self.path,
            "source_range": self.source_range,
            "expected": self.expected,
            "actual": self.actual,
            "suggested_command": self.suggested_command,
        }
        value.update({key: item for key, item in optional.items() if item is not None})
        return value


@dataclass(frozen=True)
class GeneratedProjectValidationResult:
    status: str
    generation_id: str | None
    issues: Tuple[ValidationIssue, ...]
    build: str = "not_run"
    build_error: SubprocessError | None = None
    diagnostics: Tuple[str, ...] = ()
    build_duration_ms: int = 0
    dependency_link: str = "not_requested"


class GeneratedProjectValidator:
    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.resolve()

    def validate(
        self,
        *,
        jvm_apis: Mapping[str, SemanticApi] | None = None,
        jvm_manifests: Mapping[str, JvmSourceManifest] | None = None,
        jvm_adapter_sources: Mapping[str, bytes] | None = None,
        include_dependencies: bool = True,
    ) -> GeneratedProjectValidationResult:
        incomplete_kind = contained_entry_kind_no_follow(
            self.root, self.root / INCOMPLETE_MARKER_PATH
        )
        incomplete_issue = (
            ValidationIssue(
                "SNMG_GENERATION_INCOMPLETE",
                "error",
                "generated artifact",
                "generated publication is incomplete; rerun plain `sn-module-gen update`",
                path=INCOMPLETE_MARKER_PATH,
                expected="absent",
                actual=incomplete_kind,
                suggested_command="sn-module-gen update",
            )
            if incomplete_kind is not None
            else None
        )
        try:
            project = ProjectModel.discover(self.root)
            plan = GenerationService(self.root).plan(
                operation="validate",
                requested_targets=(
                    feature.identity.npm_name for feature in project.features
                ),
                jvm_apis=jvm_apis,
                jvm_manifests=jvm_manifests,
                jvm_adapter_sources=(
                    jvm_adapter_sources
                    if jvm_adapter_sources is not None
                    else getattr(jvm_manifests, "adapter_sources", {})
                ),
            )
        except Exception as exc:
            issue = ValidationIssue(
                "SNMG_INPUT_INVALID",
                "error",
                "user source",
                str(exc),
                suggested_command=(
                    "Fix the reported source/configuration issue and rerun plain "
                    "`sn-module-gen validate`."
                ),
            )
            return GeneratedProjectValidationResult(
                "failure",
                None,
                tuple(item for item in (incomplete_issue, issue) if item is not None),
            )
        features_by_path = {
            feature.root.relative_to(self.root).as_posix(): feature
            for feature in project.features
        }
        issues = [incomplete_issue] if incomplete_issue is not None else []
        for change in plan.changes:
            artifact = change.artifact
            feature = _feature_for_path(features_by_path, change.path)
            owner = artifact.owner if artifact is not None else None
            scope = (
                "feature"
                if owner is not None and owner.startswith("feature:")
                else "runtime"
                if owner == "shared-runtime" or change.path.startswith("android/.supernote-module")
                else "plugin"
            )
            code = {
                ArtifactAction.CREATE: "SNMG_ARTIFACT_MISSING",
                ArtifactAction.UPDATE: "SNMG_ARTIFACT_MODIFIED",
                ArtifactAction.DELETE: "SNMG_ARTIFACT_STALE",
            }[change.action]
            message = {
                ArtifactAction.CREATE: f"expected generated artifact is missing: {change.path}",
                ArtifactAction.UPDATE: f"generated artifact is not canonical: {change.path}",
                ArtifactAction.DELETE: f"stale generated artifact remains: {change.path}",
            }[change.action]
            issues.append(
                ValidationIssue(
                    code,
                    "error",
                    scope,
                    message,
                    feature_id=(feature.identity.feature_id if feature else None),
                    path=change.path,
                    expected=(artifact.sha256 if artifact is not None else "absent"),
                    actual="missing" if change.action is ArtifactAction.CREATE else "different",
                    suggested_command="sn-module-gen update",
                )
            )
        issues.extend(self._javascript_issues(project))
        dependency_status = "not_requested"
        if include_dependencies:
            from .npm_package_validation import validate_author_package_inputs

            dependency_issues = validate_author_package_inputs(self.root, project)
            issues.extend(dependency_issues)
            dependency_status = "failed" if dependency_issues else "passed"
        deduplicated = _deduplicate(issues)
        if deduplicated:
            return GeneratedProjectValidationResult(
                "failure",
                plan.generation_id,
                deduplicated,
                dependency_link=dependency_status,
            )
        return GeneratedProjectValidationResult(
            "success",
            plan.generation_id,
            (),
            "not_run",
            dependency_link=dependency_status,
        )

    def _javascript_issues(
        self, project: ProjectModel
    ) -> Iterable[ValidationIssue]:
        node = shutil.which("node")
        if node is None:
            return ()
        issues = []
        for feature in project.features:
            path = feature.root / ".supernote-generated/index.js"
            relative = path.relative_to(self.root).as_posix()
            try:
                kind = contained_entry_kind_no_follow(self.root, path)
            except FilesystemError as exc:
                issues.append(
                    self._javascript_access_issue(feature, relative, str(exc))
                )
                continue
            if kind is None:
                continue
            if kind != "file":
                issues.append(
                    self._javascript_access_issue(
                        feature,
                        relative,
                        f"expected a regular file without links, found {kind}",
                    )
                )
                continue
            try:
                content, _metadata = read_regular_bytes_no_follow(path)
            except FilesystemError as exc:
                issues.append(
                    self._javascript_access_issue(feature, relative, str(exc))
                )
                continue
            try:
                result = subprocess.run(
                    [node, "--input-type=module", "--check", "-"],
                    input=content,
                    capture_output=True,
                    check=False,
                )
            except OSError as exc:
                issues.append(
                    ValidationIssue(
                        "SNMG_JAVASCRIPT_CHECK_FAILED",
                        "error",
                        "generated artifact",
                        f"Node.js syntax validation could not run: {exc}",
                        feature_id=feature.identity.feature_id,
                        path=relative,
                        suggested_command=(
                            "Restore a working Node.js installation, then rerun "
                            "sn-module-gen validate."
                        ),
                    )
                )
                continue
            if result.returncode:
                diagnostic = _process_text(
                    result.stderr or result.stdout
                ).strip().splitlines()
                issues.append(
                    ValidationIssue(
                        "SNMG_JAVASCRIPT_INVALID",
                        "error",
                        "generated artifact",
                        _javascript_diagnostic(diagnostic),
                        feature_id=feature.identity.feature_id,
                        path=relative,
                        suggested_command="sn-module-gen update",
                    )
                )
        return tuple(issues)

    @staticmethod
    def _javascript_access_issue(
        feature: ProjectFeature,
        relative: str,
        detail: str,
    ) -> ValidationIssue:
        return ValidationIssue(
            "SNMG_JAVASCRIPT_CHECK_FAILED",
            "error",
            "generated artifact",
            f"Generated JavaScript is unsafe or unreadable: {detail}",
            feature_id=feature.identity.feature_id,
            path=relative,
            suggested_command="sn-module-gen update",
        )


def _feature_for_path(
    features: Mapping[str, ProjectFeature], path: str
) -> ProjectFeature | None:
    for root, feature in features.items():
        if path == root or path.startswith(root + "/"):
            return feature
    return None


def _process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _javascript_diagnostic(lines: Iterable[str]) -> str:
    cleaned = tuple(line.strip() for line in lines if line.strip())
    for line in cleaned:
        if "SyntaxError" in line:
            return line
    for line in cleaned:
        if "Error" in line and not line.startswith("Node.js "):
            return line
    return cleaned[0] if cleaned else "generated JavaScript is invalid"


def _deduplicate(issues: Iterable[ValidationIssue]) -> Tuple[ValidationIssue, ...]:
    result = []
    seen = set()
    for issue in issues:
        key = (issue.code, issue.scope, issue.feature_id, issue.path, issue.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return tuple(result)
