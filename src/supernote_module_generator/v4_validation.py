"""Authoritative expected-plan versus actual-state V4 validation."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Dict, Iterable, Mapping, Tuple

from .diagnostics import relevant_diagnostic_lines, write_process_diagnostics
from .filesystem import (
    protected_directory_metadata,
    restore_protected_directory_metadata,
    source_tree_changes,
    source_tree_inventory,
)
from .generation_plan import ArtifactAction
from .generation_service import GenerationService
from .jvm_manifest import JvmSourceManifest
from .models import SubprocessError
from .platform_tools import gradle_wrapper_command, gradle_wrapper_path
from .project_model import ProjectModel
from .project import dependency_link_path, dependency_value, read_parent_package
from .semantic import SemanticApi
from .subprocesses import run_process


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
class V4ValidationResult:
    status: str
    generation_id: str | None
    issues: Tuple[ValidationIssue, ...]
    build: str = "not_run"
    build_error: SubprocessError | None = None
    diagnostics: Tuple[str, ...] = ()
    build_duration_ms: int = 0


@dataclass(frozen=True)
class _BuildResult:
    status: str
    issues: Tuple[ValidationIssue, ...]
    error: SubprocessError | None
    diagnostics: Tuple[str, ...]
    duration_ms: int


class V4Validator:
    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.resolve()

    def validate(
        self,
        *,
        jvm_apis: Mapping[str, SemanticApi] | None = None,
        jvm_manifests: Mapping[str, JvmSourceManifest] | None = None,
        build: bool = False,
        validate_dependencies: bool = False,
        parent_transaction_id: str | None = None,
    ) -> V4ValidationResult:
        try:
            project = ProjectModel.discover(self.root)
            plan = GenerationService(self.root).plan(
                operation="check",
                requested_targets=(
                    feature.identity.npm_name for feature in project.features
                ),
                jvm_apis=jvm_apis,
                jvm_manifests=jvm_manifests,
            )
        except Exception as exc:
            issue = ValidationIssue(
                "SNV4_INPUT_INVALID",
                "error",
                "user source",
                str(exc),
                suggested_command="Fix the reported source/configuration issue and rerun supernote-module check.",
            )
            return V4ValidationResult("failure", None, (issue,))
        features_by_path = {
            feature.root.relative_to(self.root).as_posix(): feature
            for feature in project.features
        }
        issues = []
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
                ArtifactAction.CREATE: "SNV4_ARTIFACT_MISSING",
                ArtifactAction.UPDATE: "SNV4_ARTIFACT_MODIFIED",
                ArtifactAction.DELETE: "SNV4_ARTIFACT_STALE",
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
                    suggested_command="supernote-module update --all",
                )
            )
        for action in plan.wiring_actions:
            if any(action.path in message for message in plan.wiring_issues):
                continue
            issues.append(
                ValidationIssue(
                    "SNV4_WIRING_INVALID",
                    "error",
                    "runtime",
                    f"runtime wiring is not canonical: {action.path}",
                    path=action.path,
                    expected="canonical marker block",
                    actual="different",
                    suggested_command="supernote-module repair --dry-run",
                )
            )
        for message in plan.wiring_issues:
            issues.append(
                ValidationIssue(
                    "SNV4_WIRING_INVALID",
                    "error",
                    "runtime",
                    message,
                    suggested_command="Fix the malformed marker structure, then run supernote-module repair --dry-run.",
                )
            )
        issues.extend(self._javascript_issues(project))
        if validate_dependencies:
            issues.extend(self._dependency_issues(project))
        deduplicated = _deduplicate(issues)
        if deduplicated:
            return V4ValidationResult(
                "failure", plan.generation_id, deduplicated
            )
        build_status = "not_run"
        build_diagnostics: Tuple[str, ...] = ()
        build_duration_ms = 0
        if build:
            # Compilation is deliberately additive and begins only after the
            # authoritative integrity/syntax stages have succeeded.
            build_result = self._build(
                plan.generation_id,
                parent_transaction_id=parent_transaction_id,
            )
            if build_result.status != "passed":
                return V4ValidationResult(
                    "failure",
                    plan.generation_id,
                    build_result.issues,
                    "failed",
                    build_result.error,
                    build_result.diagnostics,
                    build_result.duration_ms,
                )
            build_status = "passed"
            build_diagnostics = build_result.diagnostics
            build_duration_ms = build_result.duration_ms
        return V4ValidationResult(
            "success",
            plan.generation_id,
            (),
            build_status,
            diagnostics=build_diagnostics,
            build_duration_ms=build_duration_ms,
        )

    def _dependency_issues(self, project: ProjectModel) -> list[ValidationIssue]:
        _, package = read_parent_package(self.root)
        dependencies = package.get("dependencies", {})
        issues = []
        for feature in project.features:
            npm_name = feature.identity.npm_name
            expected = dependency_value(npm_name)
            actual = (
                dependencies.get(npm_name)
                if isinstance(dependencies, dict)
                else None
            )
            if actual != expected:
                issues.append(
                    ValidationIssue(
                        "SNV4_DEPENDENCY_INVALID",
                        "error",
                        "feature",
                        f"{npm_name} is not linked from package.json",
                        feature_id=feature.identity.feature_id,
                        path="package.json",
                        expected=expected,
                        actual=str(actual) if actual is not None else "missing",
                        suggested_command="supernote-module update --all",
                    )
                )
            link = dependency_link_path(self.root, npm_name)
            try:
                linked = link.exists() and link.resolve() == feature.root.resolve()
            except OSError:
                linked = False
            if not linked:
                issues.append(
                    ValidationIssue(
                        "SNV4_DEPENDENCY_LINK_MISSING",
                        "error",
                        "feature",
                        f"{npm_name} is not installed in node_modules",
                        feature_id=feature.identity.feature_id,
                        path=link.relative_to(self.root).as_posix(),
                        expected=feature.root.relative_to(self.root).as_posix(),
                        actual="missing or incorrect link",
                        suggested_command="npm install",
                    )
                )
        return issues

    def _build(
        self,
        generation_id: str,
        *,
        parent_transaction_id: str | None,
    ) -> _BuildResult:
        try:
            before_directories = protected_directory_metadata(self.root)
            before = source_tree_inventory(self.root)
            wrapper = gradle_wrapper_path(self.root)
            command = gradle_wrapper_command(wrapper, [":app:assembleDebug"])
        except Exception as exc:
            issue = ValidationIssue(
                "SNV4_BUILD_PREFLIGHT_FAILED",
                "error",
                "toolchain",
                f"Android build preflight failed: {exc}",
                suggested_command="Restore the Android Gradle wrapper and rerun supernote-module check --build.",
            )
            return _BuildResult("failed", (issue,), None, (), 0)

        started = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code = 1
        try:
            environment = os.environ.copy()
            environment["SUPERNOTE_MODULE_PARENT_GENERATION_ID"] = generation_id
            if parent_transaction_id is not None:
                environment["SUPERNOTE_MODULE_PARENT_TRANSACTION_ID"] = (
                    parent_transaction_id
                )
            else:
                environment.pop("SUPERNOTE_MODULE_PARENT_TRANSACTION_ID", None)
            result = run_process(
                command,
                cwd=self.root / "android",
                timeout=1200,
                env=environment,
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _process_text(exc.stdout)
            stderr = _process_text(exc.stderr)
            exit_code = 124
            stderr = (stderr + "\nAndroid build timed out after 1200 seconds.").strip()
        except OSError as exc:
            stderr = str(exc)
        duration_ms = round((time.monotonic() - started) * 1000)
        diagnostic_path = write_process_diagnostics(
            self.root,
            name="v4-check-build",
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        diagnostics = (diagnostic_path,) if diagnostic_path is not None else ()
        try:
            directory_mutations = restore_protected_directory_metadata(
                self.root, before_directories
            )
        except Exception as exc:
            directory_mutations = (f"restore_failed:{exc}",)
        try:
            mutations = source_tree_changes(before, source_tree_inventory(self.root))
        except Exception as exc:
            mutations = (f"inventory_failed:{exc}",)
        mutations = (*mutations, *(
            f"directory_metadata:{item}" for item in directory_mutations
        ))

        issues = []
        combined = stdout + "\n" + stderr
        relevant = relevant_diagnostic_lines(combined)
        if exit_code != 0:
            message = (
                relevant[0]
                if relevant
                else f"Android build failed with exit code {exit_code}."
            )
            issues.append(
                ValidationIssue(
                    "SNV4_BUILD_FAILED",
                    "error",
                    "toolchain",
                    message,
                    expected="exit code 0",
                    actual=f"exit code {exit_code}",
                    suggested_command="Review the diagnostics log, correct the build error, and rerun supernote-module check --build.",
                )
            )
        if mutations:
            issues.append(
                ValidationIssue(
                    "SNV4_BUILD_MUTATED_SOURCE",
                    "error",
                    "plugin",
                    "Android build changed source-tree state: "
                    + ", ".join(mutations[:8]),
                    expected="source tree unchanged",
                    actual="; ".join(mutations),
                    suggested_command="Remove source-writing build hooks and run supernote-module update --all before rebuilding.",
                )
            )
        if not issues:
            return _BuildResult("passed", (), None, diagnostics, duration_ms)
        subprocess_error = SubprocessError(
            list(command),
            exit_code if exit_code != 0 else 1,
            list(relevant or mutations[:12]),
        )
        return _BuildResult(
            "failed",
            tuple(issues),
            subprocess_error,
            diagnostics,
            duration_ms,
        )

    def _javascript_issues(
        self, project: ProjectModel
    ) -> Iterable[ValidationIssue]:
        node = shutil.which("node")
        if node is None:
            return ()
        issues = []
        for feature in project.features:
            path = feature.root / "index.js"
            if not path.is_file():
                continue
            result = subprocess.run(
                [node, "--check", path.relative_to(self.root).as_posix()],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                diagnostic = (result.stderr or result.stdout).strip().splitlines()
                issues.append(
                    ValidationIssue(
                        "SNV4_JAVASCRIPT_INVALID",
                        "error",
                        "generated artifact",
                        diagnostic[-1] if diagnostic else "generated JavaScript is invalid",
                        feature_id=feature.identity.feature_id,
                        path=path.relative_to(self.root).as_posix(),
                        suggested_command="supernote-module update --all",
                    )
                )
        return tuple(issues)


def _feature_for_path(features: Mapping[str, object], path: str):
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
