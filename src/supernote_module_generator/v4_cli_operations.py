"""V4 plan/check/repair command adapter shared by human and JSON output."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable

from .generation_service import GenerationService
from .generation_plan import PlanConflictError
from .frontend_discovery import (
    load_jvm_frontend_manifests,
)
from .project_model import ProjectModel
from .models import (
    Change,
    CommandResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
    ValidationResult,
)
from .transaction import Transaction
from .v4_validation import V4ValidationResult, V4Validator
from .filesystem import (
    ProtectedSourceRestoreError,
    ProtectedSourceGuard,
    finish_protected_source_guard,
    iter_tree_no_follow,
    protected_directory_metadata,
    protected_source_snapshot_roots,
    restore_protected_directory_metadata,
    source_tree_changes,
    source_tree_inventory,
)
from .platform_tools import gradle_wrapper_command, gradle_wrapper_path
from .plugin_runtime_codegen import RUNTIME_RELATIVE_ROOT
from .subprocesses import run_process


class V4CliOperationService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def check(
        self,
        *,
        build: bool = False,
        jvm_manifest_root: Path | None = None,
        command: str = "check",
        requested_targets: Iterable[str] = (),
    ) -> CommandResult:
        requested = tuple(requested_targets)
        try:
            classification_project = ProjectModel.discover(self.root)
        except Exception:
            classification_project = None
        guard = ProtectedSourceGuard(self.root)
        jvm_manifests = None
        try:
            if jvm_manifest_root is not None:
                jvm_manifests = load_jvm_frontend_manifests(
                    ProjectModel.discover(self.root), jvm_manifest_root
                )
            else:
                jvm_manifests = self._jvm_frontend_manifests()
        except BaseException as exc:
            try:
                mutations, finish_interrupted = self._finish_guard(guard)
            except ProtectedSourceRestoreError as restore_exc:
                classified = restore_exc.mutations or restore_exc.remaining
                if isinstance(exc, KeyboardInterrupt):
                    return self._cancellation_result(
                        command,
                        requested,
                        rollback=RollbackResult(True, "partial", []),
                        residue=self._changes_from_mutations(
                            restore_exc.remaining
                        ),
                        mutations=classified,
                        recovery_path=restore_exc.recovery_path,
                        residue_verified=restore_exc.remaining_verified,
                        restore_diagnostics=restore_exc.diagnostics,
                        project=classification_project,
                    )
                if isinstance(exc, Exception):
                    authoritative = self._stage_exception_result(
                        command,
                        requested,
                        exc,
                        error_kind=(
                            "jvm_frontend_invalid"
                            if jvm_manifest_root is not None
                            else "jvm_frontend_failed"
                        ),
                        phase="frontend",
                        next_action=(
                            "Correct the KSP frontend failure and rerun the command."
                        ),
                        options=self._frontend_mutation_options(),
                        project=classification_project,
                        mutations=classified,
                        build=build,
                        finalization_interrupted=False,
                    )
                    return self._stage_restore_failure_result(
                        authoritative,
                        restore_exc,
                        options=self._frontend_mutation_options(),
                        project=classification_project,
                        cancellation_label="Frontend",
                    )
                raise
            if isinstance(exc, KeyboardInterrupt):
                return self._cancellation_result(
                    command,
                    requested,
                    rollback=RollbackResult(True, "completed", []),
                    mutations=mutations,
                    project=classification_project,
                )
            if isinstance(exc, Exception):
                return self._stage_exception_result(
                    command,
                    requested,
                    exc,
                    error_kind=(
                        "jvm_frontend_invalid"
                        if jvm_manifest_root is not None
                        else "jvm_frontend_failed"
                    ),
                    phase="frontend",
                    next_action="Correct the KSP frontend failure and rerun the command.",
                    options=self._frontend_mutation_options(),
                    project=classification_project,
                    mutations=mutations,
                    build=build,
                    finalization_interrupted=finish_interrupted,
                )
            raise

        try:
            mutations, finish_interrupted = self._finish_guard(guard)
        except ProtectedSourceRestoreError as exc:
            classified = exc.mutations or exc.remaining
            if exc.interrupted:
                return self._cancellation_result(
                    command,
                    requested,
                    rollback=RollbackResult(True, "partial", []),
                    residue=self._changes_from_mutations(exc.remaining),
                    mutations=classified,
                    recovery_path=exc.recovery_path,
                    residue_verified=exc.remaining_verified,
                    restore_diagnostics=exc.diagnostics,
                    project=classification_project,
                )
            return self._frontend_mutation_result(
                command,
                requested,
                classified,
                restored=False,
                residue=self._changes_from_mutations(exc.remaining),
                recovery_path=exc.recovery_path,
                residue_verified=exc.remaining_verified,
                restore_diagnostics=exc.diagnostics,
                project=classification_project,
            )
        if finish_interrupted:
            return self._cancellation_result(
                command,
                requested,
                rollback=RollbackResult(True, "completed", []),
                mutations=mutations,
                project=classification_project,
            )
        if mutations:
            return self._frontend_mutation_result(
                command,
                requested,
                mutations,
                restored=True,
                project=classification_project,
            )
        # Frontend and validation are independent untrusted read-only stages.
        # Never reuse a finalized guard: this backup remains authoritative
        # until validation, Gradle (when requested), and diagnostics finish.
        guard = ProtectedSourceGuard(self.root)

        try:
            result = V4Validator(self.root).validate(
                build=build,
                jvm_manifests=jvm_manifests,
                validate_dependencies=command == "validate",
            )
        except BaseException as exc:
            try:
                mutations, finish_interrupted = self._finish_guard(guard)
            except ProtectedSourceRestoreError as restore_exc:
                issue_options = self._validator_mutation_options(build)
                classified = restore_exc.mutations or restore_exc.remaining
                if isinstance(exc, KeyboardInterrupt):
                    return self._cancellation_result(
                        command,
                        requested,
                        rollback=RollbackResult(True, "partial", []),
                        residue=self._changes_from_mutations(
                            restore_exc.remaining
                        ),
                        mutations=classified,
                        recovery_path=restore_exc.recovery_path,
                        residue_verified=restore_exc.remaining_verified,
                        restore_diagnostics=restore_exc.diagnostics,
                        project=classification_project,
                        **issue_options,
                    )
                if isinstance(exc, Exception):
                    authoritative = self._stage_exception_result(
                        command,
                        requested,
                        exc,
                        error_kind="validation_failed",
                        phase="build" if build else "check",
                        next_action=(
                            "Correct the validation/build failure and rerun the command."
                        ),
                        options=issue_options,
                        project=classification_project,
                        mutations=classified,
                        build=build,
                        finalization_interrupted=False,
                    )
                    return self._stage_restore_failure_result(
                        authoritative,
                        restore_exc,
                        options=issue_options,
                        project=classification_project,
                        cancellation_label="Validation",
                    )
                raise
            if isinstance(exc, KeyboardInterrupt):
                return self._cancellation_result(
                    command,
                    requested,
                    rollback=RollbackResult(True, "completed", []),
                    mutations=mutations,
                    project=classification_project,
                    **self._validator_mutation_options(build),
                )
            if isinstance(exc, Exception):
                return self._stage_exception_result(
                    command,
                    requested,
                    exc,
                    error_kind="validation_failed",
                    phase="build" if build else "check",
                    next_action="Correct the validation/build failure and rerun the command.",
                    options=self._validator_mutation_options(build),
                    project=classification_project,
                    mutations=mutations,
                    build=build,
                    finalization_interrupted=finish_interrupted,
                )
            raise

        rollback = RollbackResult()
        try:
            mutations, finish_interrupted = self._finish_guard(guard)
        except ProtectedSourceRestoreError as exc:
            authoritative = self._validation_command_result(
                command,
                requested,
                result,
                build=build,
                rollback=rollback,
                project=classification_project,
            )
            return self._validation_restore_failure_result(
                authoritative,
                exc,
                build=build,
                project=classification_project,
            )
        if finish_interrupted:
            authoritative = self._validation_command_result(
                command,
                requested,
                result,
                build=build,
                rollback=RollbackResult(True, "completed", []),
                project=classification_project,
            )
            return self._validation_finalization_cancelled_result(
                authoritative,
                mutations,
                build=build,
                project=classification_project,
            )
        if mutations:
            authoritative = self._validation_command_result(
                command,
                requested,
                result,
                build=build,
                rollback=RollbackResult(True, "completed", []),
                project=classification_project,
            )
            return self._validation_restored_mutation_result(
                authoritative,
                mutations,
                build=build,
                project=classification_project,
            )
        return self._validation_command_result(
            command,
            requested,
            result,
            build=build,
            rollback=rollback,
            project=classification_project,
        )

    @staticmethod
    def _finish_guard(
        guard: ProtectedSourceGuard,
    ) -> tuple[tuple[str, ...], bool]:
        return finish_protected_source_guard(
            guard, context_label="Validation"
        )

    def _validation_finalization_cancelled_result(
        self,
        authoritative: CommandResult,
        mutations: tuple[str, ...],
        *,
        build: bool,
        project: ProjectModel | None,
    ) -> CommandResult:
        """Compose a completed finalization cancellation over validation authority."""

        if mutations:
            options = self._validator_mutation_options(build)
            issues, affected = self._frontend_mutation_issues(
                mutations,
                project=project,
                issue_code=str(options["issue_code"]),
                stage_label=str(options["stage_label"]),
                suggested=str(options["suggested"]),
            )
            if authoritative.validation is not None:
                combined = list(authoritative.validation.issues)
                seen = {
                    (item.get("code"), item.get("actual"))
                    for item in combined
                }
                combined.extend(
                    issue
                    for issue in issues
                    if (issue.get("code"), issue.get("actual")) not in seen
                )
                authoritative.validation = ValidationResult(
                    structural=authoritative.validation.structural,
                    integration=authoritative.validation.integration,
                    dependency_link=authoritative.validation.dependency_link,
                    build=authoritative.validation.build,
                    issues=combined,
                )
            authoritative.affected_targets = sorted(
                set(authoritative.affected_targets) | set(affected)
            )
        if authoritative.status == "success":
            authoritative.status = "cancelled"
            authoritative.exit_code = 130
            authoritative.error = None
            authoritative.next_action = None
            authoritative.recovery = None
        authoritative.changes = []
        authoritative.rollback = RollbackResult(True, "completed", [])
        authoritative.metadata = {
            **authoritative.metadata,
            "cancellation_requested": True,
            "cancellation_status": "completed",
            "cancellation_message": (
                "Validation finalization was interrupted. Protected source state "
                "was restored."
            ),
        }
        return authoritative

    def _validation_restored_mutation_result(
        self,
        authoritative: CommandResult,
        mutations: tuple[str, ...],
        *,
        build: bool,
        project: ProjectModel | None,
    ) -> CommandResult:
        options = self._validator_mutation_options(build)
        authoritative_error = (
            authoritative.error.to_dict()
            if authoritative.error is not None
            else None
        )
        authoritative_next_action = authoritative.next_action
        issues, affected = self._frontend_mutation_issues(
            mutations,
            project=project,
            issue_code=str(options["issue_code"]),
            stage_label=str(options["stage_label"]),
            suggested=str(options["suggested"]),
        )
        if authoritative.validation is not None:
            combined = list(authoritative.validation.issues)
            seen = {(item.get("code"), item.get("actual")) for item in combined}
            combined.extend(
                issue
                for issue in issues
                if (issue.get("code"), issue.get("actual")) not in seen
            )
            authoritative.validation = ValidationResult(
                structural=authoritative.validation.structural,
                integration=authoritative.validation.integration,
                dependency_link=authoritative.validation.dependency_link,
                build=authoritative.validation.build,
                issues=combined,
            )
        authoritative.status = "failure"
        authoritative.exit_code = 1
        authoritative.changes = []
        authoritative.rollback = RollbackResult(True, "completed", [])
        authoritative.affected_targets = sorted(
            set(authoritative.affected_targets) | set(affected)
        )
        authoritative.next_action = str(options["suggested"])
        authoritative.recovery = None
        authoritative.error = ErrorInfo(
            str(options["error_kind"]),
            str(options["phase"]),
            f"{options['stage_label']} changed protected source state; the "
            "pre-command state was restored.",
        )
        authoritative.metadata = {
            **authoritative.metadata,
            "authoritative_error": authoritative_error,
            "authoritative_next_action": authoritative_next_action,
        }
        return authoritative

    def _validation_command_result(
        self,
        command: str,
        requested: tuple[str, ...],
        result: V4ValidationResult,
        *,
        build: bool,
        rollback: RollbackResult,
        project: ProjectModel | None = None,
    ) -> CommandResult:
        issues = [issue.manifest() for issue in result.issues]
        structural_failed = any(
            issue.code.startswith("SNV4_ARTIFACT")
            or issue.code in {"SNV4_INPUT_INVALID", "SNV4_JAVASCRIPT_INVALID"}
            for issue in result.issues
        )
        integration_failed = any(
            issue.code == "SNV4_WIRING_INVALID" for issue in result.issues
        )
        dependency_failed = any(
            issue.code.startswith("SNV4_DEPENDENCY") for issue in result.issues
        )
        validation = ValidationResult(
            structural="failed" if structural_failed else "passed",
            integration="failed" if integration_failed else "passed",
            dependency_link="failed" if dependency_failed else "passed",
            build=result.build,
            issues=issues,
        )
        affected_targets = self._affected_targets(result.issues, project=project)
        only_dependency_issues = bool(result.issues) and all(
            issue.code.startswith("SNV4_DEPENDENCY")
            for issue in result.issues
        )
        next_action = (
            None
            if result.status == "success"
            else "Review the diagnostics log and correct the Android build failure."
            if result.build == "failed"
            else "Run `npm install` to refresh local dependencies."
            if only_dependency_issues
            else "Run `supernote-module update --all --dry-run --diff` to preview repair."
        )
        metadata = {
            "generation_id": result.generation_id,
            "affected_targets": affected_targets,
            "requested_targets": list(requested),
            "next_action": next_action,
            "build_duration_ms": result.build_duration_ms,
            "build_error": (
                result.build_error.to_dict()
                if result.build_error is not None
                else None
            ),
        }
        if result.status == "success":
            return CommandResult(
                command,
                validation=validation,
                requested_targets=list(requested),
                affected_targets=affected_targets,
                diagnostics=list(result.diagnostics),
                metadata={
                    **metadata,
                    "built": build,
                    "success_message": "Generated state is canonical",
                },
                rollback=rollback,
            )
        build_failed = result.build == "failed"
        return CommandResult(
            command,
            status="failure",
            exit_code=1,
            validation=validation,
            requested_targets=list(requested),
            affected_targets=affected_targets,
            diagnostics=list(result.diagnostics),
            next_action=next_action,
            error=ErrorInfo(
                "build_failed" if build_failed else "integrity_failed",
                "build" if build_failed else "check",
                result.issues[0].message if result.issues else "Generated state is stale.",
                result.build_error,
            ),
            metadata=metadata,
            rollback=rollback,
        )

    def _validation_restore_failure_result(
        self,
        authoritative: CommandResult,
        failure: ProtectedSourceRestoreError,
        *,
        build: bool,
        project: ProjectModel | None,
    ) -> CommandResult:
        return self._stage_restore_failure_result(
            authoritative,
            failure,
            options=self._validator_mutation_options(build),
            project=project,
            cancellation_label="Validation",
        )

    def _stage_restore_failure_result(
        self,
        authoritative: CommandResult,
        failure: ProtectedSourceRestoreError,
        *,
        options: dict[str, str],
        project: ProjectModel | None,
        cancellation_label: str,
    ) -> CommandResult:
        nested_stage_result = authoritative.metadata.get(
            "authoritative_stage_result"
        )
        authoritative_stage_result = (
            dict(nested_stage_result)
            if isinstance(nested_stage_result, dict)
            else {
                "status": authoritative.status,
                "exit_code": authoritative.exit_code,
                "error": (
                    authoritative.error.to_dict()
                    if authoritative.error is not None
                    else None
                ),
                "next_action": authoritative.next_action,
                "requested_targets": list(authoritative.requested_targets),
                "affected_targets": list(authoritative.affected_targets),
                "validation": (
                    authoritative.validation.to_dict()
                    if authoritative.validation is not None
                    else None
                ),
                "diagnostics": list(authoritative.diagnostics),
                "metadata": dict(authoritative.metadata),
            }
        )
        authoritative_error = (
            authoritative.error.to_dict()
            if authoritative.error is not None
            else None
        )
        authoritative_next_action = authoritative.next_action
        classified = failure.mutations or failure.remaining
        issues, affected = self._frontend_mutation_issues(
            classified,
            project=project,
            issue_code=str(options["issue_code"]),
            stage_label=str(options["stage_label"]),
            suggested=str(options["suggested"]),
        )
        if authoritative.validation is not None:
            combined = list(authoritative.validation.issues)
            seen = {
                (item.get("code"), item.get("actual"))
                for item in combined
            }
            combined.extend(
                issue
                for issue in issues
                if (issue.get("code"), issue.get("actual")) not in seen
            )
            authoritative.validation = ValidationResult(
                structural=authoritative.validation.structural,
                integration=authoritative.validation.integration,
                dependency_link=authoritative.validation.dependency_link,
                build=authoritative.validation.build,
                issues=combined,
            )
        authoritative.status = "partial"
        authoritative.exit_code = 3
        authoritative.changes = self._changes_from_mutations(failure.remaining)
        authoritative.rollback = RollbackResult(True, "partial", [])
        authoritative.affected_targets = sorted(
            set(authoritative.affected_targets) | set(affected)
        )
        residue_verified = failure.remaining_verified
        has_live_residue = residue_verified and bool(failure.remaining)
        authoritative.next_action = (
            f"Preserve the recovery backup at {failure.recovery_path}, restore "
            "the listed residue, then run `supernote-module doctor`."
            if has_live_residue
            else f"Preserve the recovery backup at {failure.recovery_path}, then "
            "run `supernote-module doctor`; current project residue could not be "
            "inventoried."
            if not residue_verified
            else f"Preserve the recovery backup at {failure.recovery_path}, then "
            "run `supernote-module doctor` to verify and complete guard cleanup."
        )
        authoritative.recovery = RecoveryAction(
            f"Protected source backup retained at {failure.recovery_path}.",
            ["supernote-module", "doctor"],
        )
        authoritative.error = (
            ErrorInfo(
                str(options["error_kind"]),
                str(options["phase"]),
                f"{options['stage_label']} changed protected source and exact "
                "restoration could not be verified.",
            )
            if has_live_residue
            else ErrorInfo(
                "protected_source_restore_unverified",
                "rollback",
                "Protected source residue could not be inventoried, so exact "
                "restoration is unverified.",
            )
            if not residue_verified
            else ErrorInfo(
                "protected_source_cleanup_failed",
                "rollback",
                "Protected source matches the pre-command baseline, but guard "
                "cleanup did not complete.",
            )
        )
        authoritative.metadata = {
            **authoritative.metadata,
            "authoritative_stage_result": authoritative_stage_result,
            "authoritative_error": authoritative_error,
            "authoritative_next_action": authoritative_next_action,
            "recovery_path": str(failure.recovery_path),
            "restore_diagnostics": list(failure.diagnostics),
            "residue_verified": residue_verified,
            "cancellation_requested": failure.interrupted,
            "cancellation_status": (
                "partial" if failure.interrupted else "not_requested"
            ),
            "cancellation_message": (
                f"{cancellation_label} finalization was interrupted and exact "
                "restoration could not be verified."
                if failure.interrupted
                else None
            ),
        }
        return authoritative

    @staticmethod
    def _frontend_mutation_options() -> dict[str, str]:
        return {
            "issue_code": "SNV4_FRONTEND_MUTATED_SOURCE",
            "error_kind": "frontend_mutated_source",
            "phase": "frontend",
            "stage_label": "The JVM frontend",
            "suggested": (
                "Disable or fix the source-writing KSP/frontend hook, restore "
                "affected source, and rerun the command."
            ),
        }

    def _stage_exception_result(
        self,
        command: str,
        requested: tuple[str, ...],
        exc: Exception,
        *,
        error_kind: str,
        phase: str,
        next_action: str,
        options: dict[str, str],
        project: ProjectModel | None,
        mutations: tuple[str, ...],
        build: bool,
        finalization_interrupted: bool,
    ) -> CommandResult:
        issues, affected = (
            self._frontend_mutation_issues(
                mutations,
                project=project,
                issue_code=options["issue_code"],
                stage_label=options["stage_label"],
                suggested=options["suggested"],
            )
            if mutations
            else ([], [])
        )
        stage_error = ErrorInfo(error_kind, phase, str(exc))
        error = (
            ErrorInfo(
                options["error_kind"],
                options["phase"],
                f"{options['stage_label']} changed protected source; the exact "
                "pre-command state was restored.",
            )
            if mutations
            else stage_error
        )
        public_next_action = options["suggested"] if mutations else next_action
        return CommandResult(
            command,
            status="failure",
            exit_code=1,
            validation=ValidationResult(
                structural="failed",
                integration="not_requested",
                dependency_link="not_requested",
                build="not_run" if build else "not_requested",
                issues=issues,
            ),
            rollback=(
                RollbackResult(True, "completed", [])
                if finalization_interrupted or mutations
                else RollbackResult()
            ),
            requested_targets=list(requested),
            affected_targets=affected,
            diagnostics=[],
            next_action=public_next_action,
            error=error,
            metadata={
                "authoritative_stage_result": {
                    "status": "failure",
                    "exit_code": 1,
                    "error": stage_error.to_dict(),
                    "next_action": next_action,
                    "requested_targets": list(requested),
                    "affected_targets": affected,
                    "validation": {
                        "structural": "failed",
                        "integration": "not_requested",
                        "dependency_link": "not_requested",
                        "build": "not_run" if build else "not_requested",
                        "issues": issues,
                    },
                    "diagnostics": [],
                },
                "cancellation_requested": finalization_interrupted,
                "cancellation_status": (
                    "completed" if finalization_interrupted else "not_requested"
                ),
                "cancellation_message": (
                    f"{options['stage_label']} finalization was interrupted. "
                    "Protected source state was restored."
                    if finalization_interrupted
                    else None
                ),
            },
        )

    @staticmethod
    def _validator_mutation_options(build: bool) -> dict[str, str]:
        if build:
            return {
                "issue_code": "SNV4_BUILD_MUTATED_SOURCE",
                "error_kind": "build_mutated_source",
                "phase": "build",
                "stage_label": "The Android build",
                "suggested": (
                    "Disable or fix the source-writing build hook, restore affected "
                    "source, and rerun the command."
                ),
            }
        return {
            "issue_code": "SNV4_VALIDATION_MUTATED_SOURCE",
            "error_kind": "validation_mutated_source",
            "phase": "check",
            "stage_label": "Validation",
            "suggested": (
                "Disable or fix the source-writing validation hook, restore affected "
                "source, and rerun the command."
            ),
        }

    def update(
        self,
        requested_targets: Iterable[str],
        *,
        dry_run: bool,
        include_diff: bool,
        command: str = "update",
    ) -> CommandResult:
        requested = tuple(requested_targets)
        try:
            classification_project = ProjectModel.discover(self.root)
        except Exception:
            classification_project = None
        transaction: Transaction | None = None
        baseline = None
        guard: ProtectedSourceGuard | None = None
        directory_metadata = protected_directory_metadata(self.root)
        if dry_run:
            guard = ProtectedSourceGuard(self.root)
        else:
            baseline = source_tree_inventory(self.root)
            transaction = Transaction(self.root, command, requested)
            transaction.record_directory_metadata(directory_metadata)
            transaction.snapshot(protected_source_snapshot_roots(self.root))
        try:
            jvm_manifests = self._jvm_frontend_manifests()
            if guard is not None:
                mutations, finish_interrupted = self._finish_guard(guard)
                guard = None
                if finish_interrupted:
                    return self._cancellation_result(
                        command,
                        requested,
                        rollback=RollbackResult(True, "completed", []),
                        mutations=mutations,
                        project=classification_project,
                    )
                if mutations:
                    return self._frontend_mutation_result(
                        command,
                        requested,
                        mutations,
                        restored=True,
                        project=classification_project,
                    )
            elif baseline is not None and transaction is not None:
                mutations = source_tree_changes(
                    baseline, source_tree_inventory(self.root)
                )
                if mutations:
                    rollback, residue = self._rollback_with_verification(
                        transaction, baseline, directory_metadata
                    )
                    return self._frontend_mutation_result(
                        command,
                        requested,
                        mutations,
                        restored=rollback.status == "completed",
                        rollback=rollback,
                        residue=residue,
                        project=classification_project,
                    )
            plan = GenerationService(self.root).plan(
                operation=command,
                requested_targets=requested,
                jvm_manifests=jvm_manifests,
            )
        except BaseException as exc:
            if guard is not None:
                try:
                    mutations, finish_interrupted = self._finish_guard(guard)
                except ProtectedSourceRestoreError as restore_exc:
                    if isinstance(exc, KeyboardInterrupt):
                        return self._cancellation_result(
                            command,
                            requested,
                            rollback=RollbackResult(True, "partial", []),
                            residue=self._changes_from_mutations(
                                restore_exc.remaining
                            ),
                            mutations=restore_exc.mutations,
                            recovery_path=restore_exc.recovery_path,
                            residue_verified=restore_exc.remaining_verified,
                            restore_diagnostics=restore_exc.diagnostics,
                            project=classification_project,
                        )
                    if isinstance(exc, Exception):
                        authoritative = self._stage_exception_result(
                            command,
                            requested,
                            exc,
                            error_kind="jvm_frontend_failed",
                            phase="frontend",
                            next_action=(
                                "Correct the KSP frontend failure and rerun the "
                                "command."
                            ),
                            options=self._frontend_mutation_options(),
                            project=classification_project,
                            mutations=(
                                restore_exc.mutations or restore_exc.remaining
                            ),
                            build=False,
                            finalization_interrupted=False,
                        )
                        return self._stage_restore_failure_result(
                            authoritative,
                            restore_exc,
                            options=self._frontend_mutation_options(),
                            project=classification_project,
                            cancellation_label="Frontend",
                        )
                    raise
                if isinstance(exc, KeyboardInterrupt):
                    return self._cancellation_result(
                        command,
                        requested,
                        rollback=RollbackResult(True, "completed", []),
                        mutations=mutations,
                        project=classification_project,
                    )
                if isinstance(exc, Exception):
                    return self._stage_exception_result(
                        command,
                        requested,
                        exc,
                        error_kind="jvm_frontend_failed",
                        phase="frontend",
                        next_action=(
                            "Correct the KSP frontend failure and rerun the command."
                        ),
                        options=self._frontend_mutation_options(),
                        project=classification_project,
                        mutations=mutations,
                        build=False,
                        finalization_interrupted=finish_interrupted,
                    )
                raise
            rollback = RollbackResult()
            residue: list[Change] = []
            if transaction is not None and baseline is not None:
                rollback, residue = self._rollback_with_verification(
                    transaction, baseline, directory_metadata
                )
            if isinstance(exc, KeyboardInterrupt):
                return self._cancellation_result(
                    command,
                    requested,
                    rollback=rollback,
                    residue=residue,
                    project=classification_project,
                )
            if not isinstance(exc, Exception):
                raise
            return CommandResult(
                command,
                status="failure" if rollback.status != "partial" else "partial",
                exit_code=2 if rollback.status != "partial" else 3,
                changes=residue,
                rollback=rollback,
                error=ErrorInfo("invalid_source", "preflight", str(exc)),
                metadata={"requested_targets": list(requested)},
            )
        metadata = {
            "requested_targets": list(plan.requested_targets),
            "affected_targets": list(plan.affected_targets),
            "generation_id": plan.generation_id,
            "dry_run": dry_run,
            "no_op": plan.is_noop,
            "plan": plan.manifest(),
        }
        if include_diff:
            metadata["diff"] = plan.unified_diff()
        changes = [
            Change(
                str(self.root / change.path),
                change.action.value,
                change.artifact.owner if change.artifact is not None else "generated",
            )
            for change in plan.changes
        ] + [
            Change(
                str(self.root / action.path),
                "update",
                "parent_dependency",
            )
            for action in plan.dependency_actions
        ] + [
            Change(str(self.root / action.path), "update", "plugin_wiring")
            for action in plan.wiring_actions
        ] + [
            Change(
                str(self.root / action.path),
                "delete",
                "plugin_runtime"
                if action.owner == "shared-runtime"
                else "feature_implementation",
            )
            for action in plan.tree_removals
        ]
        if dry_run:
            try:
                GenerationService(self.root).validate_preconditions(plan)
            except PlanConflictError as exc:
                return CommandResult(
                    command,
                    status="failure",
                    exit_code=1,
                    changes=changes,
                    requested_targets=list(plan.requested_targets),
                    affected_targets=list(plan.affected_targets),
                    error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                    next_action="The project changed during planning. Review the external edit and rerun the command.",
                    metadata=metadata,
                )
            return CommandResult(
                command,
                changes=changes,
                metadata={
                    **metadata,
                    "success_message": (
                        "No generated changes are required"
                        if plan.is_noop
                        else "Generation plan previewed; no files were changed"
                    ),
                },
            )
        assert transaction is not None
        assert baseline is not None
        if plan.is_noop:
            try:
                GenerationService(self.root).validate_preconditions(plan)
                transaction.commit()
                directory_failures = restore_protected_directory_metadata(
                    self.root, directory_metadata
                )
                if directory_failures:
                    raise RuntimeError(
                        "could not restore no-op directory metadata: "
                        + ", ".join(directory_failures)
                    )
            except BaseException as exc:
                if transaction.commit_is_durable():
                    return self._committed_result(
                        command,
                        plan,
                        changes,
                        metadata,
                        transaction,
                        directory_metadata,
                        interrupted=isinstance(exc, KeyboardInterrupt),
                    )
                if isinstance(exc, PlanConflictError) and not transaction.mutated:
                    interrupted = False
                    try:
                        transaction.preserve_current_directory_metadata(
                            exc.preserve_directory_paths
                        )
                        transaction.abandon_unmutated()
                        directory_failures = ()
                    except KeyboardInterrupt:
                        interrupted = True
                        try:
                            if transaction.abandon_is_durable():
                                transaction.finish_abandon()
                                directory_failures = ()
                            else:
                                transaction.abandon_unmutated()
                                directory_failures = ()
                        except BaseException as cleanup_exc:
                            return self._conflict_cleanup_failure(
                                command, plan, changes, metadata, cleanup_exc,
                                interrupted=True,
                            )
                    except BaseException as cleanup_exc:
                        return self._conflict_cleanup_failure(
                            command, plan, changes, metadata, cleanup_exc,
                            interrupted=False,
                        )
                    if directory_failures:
                        return self._conflict_cleanup_failure(
                            command,
                            plan,
                            changes,
                            metadata,
                            RuntimeError("; ".join(directory_failures)),
                            interrupted=interrupted,
                        )
                    return CommandResult(
                        command,
                        status="failure",
                        exit_code=1,
                        changes=changes,
                        rollback=RollbackResult(False, "not_needed", []),
                        requested_targets=list(plan.requested_targets),
                        affected_targets=list(plan.affected_targets),
                        next_action="The project changed during planning. Review the external edit and rerun the command.",
                        error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                        metadata={
                            **metadata,
                            "abandon_durable": True,
                            "cancellation_requested": interrupted,
                            "cancellation_status": (
                                "completed" if interrupted else "not_requested"
                            ),
                        },
                    )
                rollback, residue = self._rollback_with_verification(
                    transaction, baseline, directory_metadata
                )
                if isinstance(exc, KeyboardInterrupt):
                    return self._cancellation_result(
                        command,
                        requested,
                        rollback=rollback,
                        residue=residue,
                    )
                if not isinstance(exc, Exception):
                    raise
                return CommandResult(
                    command,
                    status="failure" if rollback.status == "completed" else "partial",
                    exit_code=1 if rollback.status == "completed" else 3,
                    changes=residue,
                    rollback=rollback,
                    error=ErrorInfo("commit_failed", "commit", str(exc)),
                    metadata=metadata,
                )
            return CommandResult(
                command,
                changes=changes,
                requested_targets=list(plan.requested_targets),
                affected_targets=list(plan.affected_targets),
                metadata={
                    **metadata,
                    "success_message": "No generated changes are required",
                },
            )
        try:
            staged_repair = command == "repair"
            GenerationService(self.root).execute(
                plan,
                transaction,
                commit=not staged_repair,
            )
            if staged_repair:
                staged_validation = V4Validator(self.root).validate(
                    jvm_manifests=jvm_manifests,
                    parent_transaction_id=transaction.identifier,
                )
                if staged_validation.issues:
                    rollback, residue = self._rollback_with_verification(
                        transaction, baseline, directory_metadata
                    )
                    return self._staged_repair_validation_failure(
                        plan,
                        staged_validation,
                        planned_changes=changes,
                        rollback=rollback,
                        residue=residue,
                        metadata=metadata,
                    )
                transaction.commit()
        except BaseException as exc:
            if transaction.commit_is_durable():
                return self._committed_result(
                    command,
                    plan,
                    changes,
                    metadata,
                    transaction,
                    directory_metadata,
                    interrupted=isinstance(exc, KeyboardInterrupt),
                )
            if isinstance(exc, PlanConflictError) and not transaction.mutated:
                interrupted = False
                try:
                    transaction.preserve_current_directory_metadata(
                        exc.preserve_directory_paths
                    )
                    if transaction.abandon_is_durable():
                        transaction.finish_abandon()
                    else:
                        transaction.abandon_unmutated()
                    directory_failures = ()
                except KeyboardInterrupt:
                    interrupted = True
                    try:
                        if transaction.abandon_is_durable():
                            transaction.finish_abandon()
                            directory_failures = ()
                        else:
                            transaction.abandon_unmutated()
                            directory_failures = ()
                    except BaseException as cleanup_exc:
                        return self._conflict_cleanup_failure(
                            command, plan, changes, metadata, cleanup_exc,
                            interrupted=True,
                        )
                except BaseException as cleanup_exc:
                    return self._conflict_cleanup_failure(
                        command, plan, changes, metadata, cleanup_exc,
                        interrupted=False,
                    )
                if directory_failures:
                    return CommandResult(
                        command,
                        status="partial",
                        exit_code=3,
                        changes=self._changes_from_mutations(directory_failures),
                        rollback=RollbackResult(True, "partial", []),
                        recovery=RecoveryAction(
                            "Precommit conflict cleanup left directory metadata residue.",
                            ["supernote-module", "doctor"],
                        ),
                        error=ErrorInfo(
                            "plan_conflict_cleanup_failed",
                            "precommit",
                            "; ".join(directory_failures),
                        ),
                        metadata=metadata,
                    )
                return CommandResult(
                    command,
                    status="failure",
                    exit_code=1,
                    changes=changes,
                    rollback=RollbackResult(True, "completed", []),
                    requested_targets=list(plan.requested_targets),
                    affected_targets=list(plan.affected_targets),
                    next_action="The project changed during planning. Review the external edit and rerun the command.",
                    error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                    metadata={
                        **metadata,
                        "abandon_durable": True,
                        "cancellation_requested": interrupted,
                        "cancellation_status": (
                            "completed" if interrupted else "not_requested"
                        ),
                        "cancellation_message": (
                            "Interrupt arrived during conflict cleanup; the external edit was retained."
                            if interrupted else None
                        ),
                    },
                )
            rollback, residue = self._rollback_with_verification(
                transaction, baseline, directory_metadata
            )
            if isinstance(exc, KeyboardInterrupt):
                return self._cancellation_result(
                    command,
                    requested,
                    rollback=rollback,
                    residue=residue,
                )
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, PlanConflictError):
                return CommandResult(
                    command,
                    status="failure" if rollback.status == "completed" else "partial",
                    exit_code=1 if rollback.status == "completed" else 3,
                    changes=[*changes, *residue],
                    rollback=rollback,
                    error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                    next_action=(
                        "The project changed during publication. Preserve the external "
                        "edit, complete any retained recovery action, and rerun the command."
                    ),
                    metadata=metadata,
                )
            return CommandResult(
                command,
                status="failure" if rollback.status == "completed" else "partial",
                exit_code=1 if rollback.status == "completed" else 3,
                changes=[*changes, *residue],
                rollback=rollback,
                error=ErrorInfo("commit_failed", "commit", str(exc)),
                metadata=metadata,
            )
        return CommandResult(
            command,
            changes=changes,
            metadata={**metadata, "success_message": "Generated state updated atomically"},
        )

    def _staged_repair_validation_failure(
        self,
        plan,
        result: V4ValidationResult,
        *,
        planned_changes: list[Change],
        rollback: RollbackResult,
        residue: list[Change],
        metadata: dict[str, object],
    ) -> CommandResult:
        """Preserve the authoritative precommit validation after rollback."""

        authoritative = self._validation_command_result(
            "repair",
            tuple(plan.requested_targets),
            result,
            build=False,
            rollback=rollback,
        )
        first_issue = result.issues[0] if result.issues else None
        next_action = (
            first_issue.suggested_command
            if first_issue is not None and first_issue.suggested_command
            else "Review the staged validation issues, then rerun `supernote-module repair --dry-run`."
        )
        authoritative.status = (
            "failure" if rollback.status == "completed" else "partial"
        )
        authoritative.exit_code = 1 if rollback.status == "completed" else 3
        authoritative.changes = list(planned_changes)
        authoritative.actual_changes = list(residue)
        authoritative.error = ErrorInfo(
            "repair_validation_failed",
            "precommit",
            (
                first_issue.message
                if first_issue is not None
                else "Staged repair validation rejected the planned result."
            ),
            result.build_error,
        )
        authoritative.next_action = next_action
        authoritative.metadata = {
            **metadata,
            **authoritative.metadata,
            "staged_validation": "failed",
            "next_action": next_action,
        }
        if rollback.status != "completed":
            authoritative.recovery = RecoveryAction(
                "Staged repair rollback is incomplete; inspect the listed residue.",
                ["supernote-module", "doctor"],
            )
        return authoritative

    def _jvm_frontend_manifests(
        self, *, allow_unmanifested_bootstrap: bool = False
    ):
        project = ProjectModel.discover(
            self.root,
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        has_jvm = any(
            feature.jvm_root.is_dir()
            and any(
                path.is_file() and path.suffix.lower() in {".kt", ".java"}
                for path in iter_tree_no_follow(feature.jvm_root)
            )
            for feature in project.features
        )
        if not has_jvm:
            return {}
        gradle = gradle_wrapper_path(self.root)
        if not gradle.is_file():
            raise RuntimeError(
                "JVM generation requires the Android Gradle wrapper to run KSP"
            )
        command = gradle_wrapper_command(
            gradle, [":supernote-v4-runtime:kspDebugKotlin"]
        )
        result = run_process(command, cwd=self.root / "android", timeout=1200)
        if result.returncode:
            output = result.stderr or result.stdout
            raise RuntimeError(
                "KSP semantic frontend failed:\n" + "\n".join(output.splitlines()[-12:])
            )
        manifest_root = (
            self.root
            / RUNTIME_RELATIVE_ROOT
            / "build/generated/ksp/debug/resources/supernote/generated/manifests"
        )
        return load_jvm_frontend_manifests(project, manifest_root)

    def _affected_targets(
        self,
        issues,
        *,
        project: ProjectModel | None = None,
    ) -> list[str]:
        if project is None:
            try:
                project = ProjectModel.discover(self.root)
            except Exception:
                return sorted(
                    {issue.feature_id for issue in issues if issue.feature_id is not None}
                )
        names = {
            feature.identity.feature_id: feature.identity.npm_name
            for feature in project.features
        }
        affected = {
            names.get(issue.feature_id, issue.feature_id)
            for issue in issues
            if issue.feature_id is not None
        }
        if any(issue.scope == "runtime" for issue in issues):
            affected.add("shared runtime")
        if any(issue.scope == "plugin" for issue in issues):
            affected.add("plugin wiring")
        return sorted(item for item in affected if item is not None)

    def _frontend_mutation_result(
        self,
        command: str,
        requested: tuple[str, ...],
        mutations: tuple[str, ...],
        *,
        restored: bool,
        rollback: RollbackResult | None = None,
        residue: list[Change] | None = None,
        recovery_path: Path | None = None,
        residue_verified: bool = True,
        restore_diagnostics: tuple[str, ...] = (),
        project: ProjectModel | None = None,
        issue_code: str = "SNV4_FRONTEND_MUTATED_SOURCE",
        error_kind: str = "frontend_mutated_source",
        phase: str = "frontend",
        stage_label: str = "The JVM frontend",
        suggested: str = (
            "Disable or fix the source-writing KSP/frontend hook, restore affected "
            "source, and rerun the command."
        ),
    ) -> CommandResult:
        message = (
            f"{stage_label} changed protected source state: "
            + ", ".join(mutations[:8])
            if mutations
            else f"{stage_label} protected-source finalization did not complete."
        )
        issues, affected_targets = self._frontend_mutation_issues(
            mutations,
            project=project,
            issue_code=issue_code,
            stage_label=stage_label,
            suggested=suggested,
        )
        next_action = suggested
        if recovery_path is not None:
            next_action = (
                f"Preserve the recovery backup at {recovery_path}, restore the listed "
                "residue, then rerun `supernote-module doctor`."
                if residue_verified and residue
                else f"Preserve the recovery backup at {recovery_path}, then rerun "
                "`supernote-module doctor`; current residue could not be inventoried."
                if not residue_verified
                else f"Preserve the recovery backup at {recovery_path}, then rerun "
                "`supernote-module doctor` to complete guard cleanup."
            )
        verified_rollback = rollback or RollbackResult(
            True, "completed" if restored else "partial", []
        )
        return CommandResult(
            command,
            status="failure" if restored else "partial",
            exit_code=1 if restored else 3,
            changes=list(residue or ()),
            validation=ValidationResult(
                structural="failed",
                integration="passed",
                dependency_link="passed",
                issues=issues,
            ),
            rollback=verified_rollback,
            requested_targets=list(requested),
            affected_targets=affected_targets,
            next_action=next_action,
            recovery=(
                RecoveryAction(
                    f"Protected source backup retained at {recovery_path}.",
                    ["supernote-module", "doctor"],
                )
                if recovery_path is not None
                else None
            ),
            error=(
                ErrorInfo(
                    "protected_source_restore_unverified",
                    "rollback",
                    "Protected source residue could not be inventoried, so exact "
                    "restoration is unverified.",
                )
                if not residue_verified
                else ErrorInfo(error_kind, phase, message)
            ),
            metadata={
                **(
                    {"recovery_path": str(recovery_path)}
                    if recovery_path is not None
                    else {}
                ),
                "residue_verified": residue_verified,
                "restore_diagnostics": list(restore_diagnostics),
            },
        )

    def _frontend_mutation_issues(
        self,
        mutations: tuple[str, ...],
        *,
        project: ProjectModel | None = None,
        issue_code: str = "SNV4_FRONTEND_MUTATED_SOURCE",
        stage_label: str = "The JVM frontend",
        suggested: str = (
            "Disable or fix the source-writing KSP/frontend hook, restore affected "
            "source, and rerun the command."
        ),
    ) -> tuple[list[dict[str, object]], list[str]]:
        if project is None:
            try:
                project = ProjectModel.discover(self.root)
            except Exception:
                project = None
        issues: list[dict[str, object]] = []
        affected: set[str] = set()
        for mutation in mutations:
            action, separator, relative = mutation.partition(":")
            if not separator or action not in {"created", "deleted", "modified"}:
                relative = ""
            scope = "plugin"
            feature_id = None
            affected_target = "plugin wiring"
            if relative:
                relative_parts = PurePosixPath(relative).parts
                runtime_parts = PurePosixPath(
                    RUNTIME_RELATIVE_ROOT.as_posix()
                ).parts
                if relative_parts[: len(runtime_parts)] == runtime_parts:
                    scope = "runtime"
                    affected_target = "shared runtime"
                elif project is not None:
                    for feature in project.features:
                        feature_relative = feature.root.relative_to(
                            self.root
                        ).as_posix()
                        feature_parts = PurePosixPath(feature_relative).parts
                        if relative_parts[: len(feature_parts)] == feature_parts:
                            scope = "feature"
                            feature_id = feature.identity.feature_id
                            affected_target = feature.identity.npm_name
                            break
            issue: dict[str, object] = {
                "code": issue_code,
                "severity": "error",
                "scope": scope,
                "message": f"{stage_label} changed protected source state: {mutation}",
                "expected": "source tree unchanged",
                "actual": mutation,
                "suggested_command": suggested,
            }
            if relative:
                issue["path"] = relative
            if feature_id is not None:
                issue["feature_id"] = feature_id
            issues.append(issue)
            affected.add(affected_target)
        return issues, sorted(affected)

    def _cancellation_result(
        self,
        command: str,
        requested: tuple[str, ...],
        *,
        rollback: RollbackResult,
        residue: list[Change] | None = None,
        mutations: tuple[str, ...] = (),
        recovery_path: Path | None = None,
        residue_verified: bool = True,
        restore_diagnostics: tuple[str, ...] = (),
        project: ProjectModel | None = None,
        issue_code: str = "SNV4_FRONTEND_MUTATED_SOURCE",
        error_kind: str = "frontend_mutated_source",
        phase: str = "frontend",
        stage_label: str = "The JVM frontend",
        suggested: str = (
            "Disable or fix the source-writing KSP/frontend hook, restore affected "
            "source, and rerun the command."
        ),
    ) -> CommandResult:
        completed = rollback.status == "completed"
        issues, affected = (
            self._frontend_mutation_issues(
                mutations,
                project=project,
                issue_code=issue_code,
                stage_label=stage_label,
                suggested=suggested,
            )
            if mutations
            else ([], [])
        )
        message = (
            "Operation cancelled. Previous state restored."
            if completed
            else "Operation cancelled, but exact restoration could not be verified."
        )
        next_action = None
        recovery = None
        error = None
        if not completed:
            next_action = (
                f"Preserve the recovery backup at {recovery_path}, restore the listed "
                "residue, then run `supernote-module doctor`."
                if recovery_path is not None and residue_verified and residue
                else f"Preserve the recovery backup at {recovery_path}, then run "
                "`supernote-module doctor`; current residue could not be inventoried."
                if recovery_path is not None and not residue_verified
                else f"Preserve the recovery backup at {recovery_path}, then run "
                "`supernote-module doctor` to complete guard cleanup."
                if recovery_path is not None
                else "Run `supernote-module doctor`, restore the listed residue, then rerun the command."
            )
            recovery = RecoveryAction(
                (
                    f"Protected source backup retained at {recovery_path}."
                    if recovery_path is not None
                    else "Cancellation recovery is incomplete."
                ),
                ["supernote-module", "doctor"],
            )
            error = ErrorInfo(
                (
                    "cancellation_restore_unverified"
                    if not residue_verified
                    else "cancellation_rollback_partial"
                ),
                "rollback",
                (
                    "Operation was cancelled and source residue could not be "
                    "inventoried; exact restoration is unverified."
                    if not residue_verified
                    else "Operation was cancelled, but exact source restoration "
                    "could not be verified."
                ),
            )
        return CommandResult(
            command,
            status="cancelled" if completed else "partial",
            exit_code=130 if completed else 3,
            changes=list(residue or ()),
            validation=(
                ValidationResult(
                    structural="failed",
                    integration="passed",
                    dependency_link="passed",
                    issues=issues,
                )
                if issues
                else None
            ),
            rollback=rollback,
            requested_targets=list(requested),
            affected_targets=affected,
            next_action=next_action,
            recovery=recovery,
            error=error,
            metadata={
                "cancellation_requested": True,
                "cancellation_message": message,
                **(
                    {"recovery_path": str(recovery_path)}
                    if recovery_path is not None
                    else {}
                ),
                "residue_verified": residue_verified,
                "restore_diagnostics": list(restore_diagnostics),
            },
        )

    def _changes_from_mutations(
        self, mutations: tuple[str, ...]
    ) -> list[Change]:
        changes = []
        for item in mutations:
            action, separator, relative = item.partition(":")
            changes.append(
                Change(
                    str(self.root / (relative if separator else item)),
                    {"created": "create", "deleted": "delete"}.get(
                        action, "update"
                    ),
                    "rollback_residue",
                )
            )
        return changes

    def _committed_result(
        self,
        command: str,
        plan,
        changes: list[Change],
        metadata: dict[str, object],
        transaction: Transaction,
        directory_metadata,
        *,
        interrupted: bool,
    ) -> CommandResult:
        """Finish durable commit cleanup without attempting destructive rollback."""

        try:
            transaction.finish_commit()
        except BaseException as exc:
            recovery_path = getattr(exc, "recovery_path", None)
            return CommandResult(
                command,
                status="partial",
                exit_code=3,
                changes=changes,
                rollback=RollbackResult(False, "not_needed", []),
                recovery=RecoveryAction(
                    (
                        "The generation committed, but transaction cleanup is incomplete."
                        + (
                            f" Recovery metadata is retained at {recovery_path}."
                            if recovery_path is not None
                            else " The durable transaction journal remains available for recovery."
                        )
                    ),
                    ["supernote-module", "doctor"],
                ),
                error=ErrorInfo("commit_cleanup_failed", "commit", str(exc)),
                metadata={
                    **metadata,
                    "commit_durable": True,
                    "cancellation_requested": interrupted,
                    "cancellation_message": (
                        "Interrupt arrived after commit; committed state was retained."
                        if interrupted
                        else None
                    ),
                    "cancellation_status": "partial" if interrupted else "not_requested",
                    "recovery_path": (
                        str(recovery_path)
                        if recovery_path is not None
                        else str(transaction.journal_path)
                    ),
                },
            )
        committed_metadata = {
            **metadata,
            "commit_durable": True,
            "commit_cleanup_recovered": True,
            "success_message": "Generated state committed atomically",
        }
        if interrupted:
            committed_metadata.update(
                {
                    "cancellation_requested": True,
                    "cancellation_status": "completed",
                    "cancellation_message": (
                        "Interrupt arrived after commit; committed state was retained."
                    ),
                }
            )
        return CommandResult(
            command,
            changes=changes,
            requested_targets=list(plan.requested_targets),
            affected_targets=list(plan.affected_targets),
            metadata=committed_metadata,
        )

    def _conflict_cleanup_failure(
        self,
        command: str,
        plan,
        changes: list[Change],
        metadata: dict[str, object],
        exc: BaseException,
        *,
        interrupted: bool,
    ) -> CommandResult:
        return CommandResult(
            command,
            status="partial",
            exit_code=3,
            changes=changes,
            rollback=RollbackResult(False, "not_needed", []),
            requested_targets=list(plan.requested_targets),
            affected_targets=list(plan.affected_targets),
            recovery=RecoveryAction(
                "Precommit cleanup is incomplete; startup recovery will finish the durable abandon without restoring stale snapshots.",
                ["supernote-module", "doctor"],
            ),
            error=ErrorInfo(
                "plan_conflict_cleanup_failed", "precommit", str(exc)
            ),
            metadata={
                **metadata,
                "abandon_durable": True,
                "cancellation_requested": interrupted,
                "cancellation_status": "partial" if interrupted else "not_requested",
            },
        )

    def _rollback_with_verification(
        self,
        transaction: Transaction,
        baseline,
        directory_metadata=None,
    ) -> tuple[RollbackResult, list[Change]]:
        try:
            transaction.preserve_rollback_external_changes(baseline)
        except BaseException:
            try:
                transaction.retain_conflict()
            except BaseException:
                pass
            remaining = source_tree_changes(
                baseline, source_tree_inventory(self.root)
            )
            return (
                RollbackResult(True, "partial", []),
                self._changes_from_mutations(remaining),
            )
        rollback = transaction.rollback()
        try:
            remaining = source_tree_changes(
                baseline, source_tree_inventory(self.root)
            )
        except Exception as exc:
            remaining = (f"inventory_failed:{exc}",)
        if directory_metadata is not None:
            remaining = (
                *remaining,
                *restore_protected_directory_metadata(
                    self.root, directory_metadata
                ),
            )
        if not remaining:
            return rollback, []
        residue = self._changes_from_mutations(remaining)
        return RollbackResult(True, "partial", rollback.restored), residue
