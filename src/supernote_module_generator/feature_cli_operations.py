"""Public add, full-update, and project-validation operations."""
from __future__ import annotations

from pathlib import Path

from .errors import ConfigurationError, GeneratorError, PartialFailure
from .feature_generator import FeatureConfig
from .feature_identity import FeatureIdentity
from .feature_operations import FeatureOperationService, FeatureSourceError
from .feature_workflows import (
    FeatureAddDecisions,
    FeatureUpdateDecisions,
    FeatureValidateDecisions,
)
from .filesystem import (
    iter_tree_no_follow,
    lexists,
    source_tree_changes,
    source_tree_inventory,
)
from .generation_plan import GenerationPlan
from .generation_service import GenerationService
from .models import Change, CommandResult, ErrorInfo, ValidationResult
from .models import WarningInfo
from .npm_package_validation import validate_author_package_inputs
from .jvm_frontend_service import JvmFrontendService
from .integrity_manifest import INCOMPLETE_MARKER_PATH
from .naming import normalize_description, validate_javascript_name
from .project import read_parent_package
from .project_model import ProjectModel
from .rendering import ProgressReporter, Renderer
from .validation import GeneratedProjectValidationResult, GeneratedProjectValidator
from .semantic import SemanticApi


class FeatureCliOperationService:
    def __init__(self, root: Path, renderer: Renderer) -> None:
        self.root = root.resolve()
        self.renderer = renderer
        self.progress = ProgressReporter(renderer)
        self.features = FeatureOperationService(self.root)

    def add(self, decisions: FeatureAddDecisions) -> CommandResult:
        self._validate_add(decisions)
        identity = FeatureIdentity.create(
            npm_name=decisions.package_name,
            android_namespace=decisions.android_namespace,
            package_version=decisions.package_version,
        )
        destination = identity.destination(self.root)
        config = FeatureConfig(
            output=destination,
            npm_name=decisions.package_name,
            package_version=decisions.package_version,
            android_namespace=decisions.android_namespace,
            public_name=decisions.public_name,
            description=decisions.description,
            starters=decisions.starters,
        )
        with self.progress.phase("Scaffolding feature", "Scaffolded feature"):
            self.features.add(config)
        try:
            plan, manifests = self._plan(
                "add",
                allow_unmanifested_bootstrap=True,
                requested_targets=(decisions.package_name,),
            )
            with self.progress.phase("Generating project", "Generated project"):
                GenerationService(self.root).execute(plan)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise self._partial_generation_error("add", exc) from exc
        validation = GeneratedProjectValidator(self.root).validate(
            jvm_manifests=manifests,
            include_dependencies=False,
        )
        result = self._result(
            "add",
            plan,
            validation,
            module=self.features.find_record(decisions.package_name).info(),
            extra_changes=[Change(str(destination), "created", "authored_feature")],
            next_action=(
                "Install the authored module separately, for example: "
                f"`npm install ./{identity.relative_root.as_posix()}`."
            ),
        )
        result.warnings.extend(self._package_warnings())
        return result

    def update(self, _decisions: FeatureUpdateDecisions) -> CommandResult:
        try:
            plan, manifests = self._plan(
                "update", allow_unmanifested_bootstrap=True
            )
            with self.progress.phase("Generating project", "Generated project"):
                GenerationService(self.root).execute(plan)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            if isinstance(exc, FeatureSourceError) and not lexists(
                self.root / INCOMPLETE_MARKER_PATH
            ):
                raise
            raise self._partial_generation_error("update", exc) from exc
        validation = GeneratedProjectValidator(self.root).validate(
            jvm_manifests=manifests,
            include_dependencies=False,
        )
        result = self._result("update", plan, validation)
        result.warnings.extend(self._package_warnings())
        return result

    def validate(self, decisions: FeatureValidateDecisions) -> CommandResult:
        before = source_tree_inventory(self.root)
        manifests = JvmFrontendService(self.root).manifests()
        mutations = source_tree_changes(before, source_tree_inventory(self.root))
        if mutations:
            return CommandResult(
                "validate",
                status="failure",
                exit_code=1,
                validation=ValidationResult(
                    structural="failed",
                    integration="not_requested",
                    dependency_link="not_requested",
                    issues=[
                        {
                            "code": "SNMG_FRONTEND_MUTATED_INPUT",
                            "kind": "source",
                            "message": "KSP analysis changed authored input: "
                            + ", ".join(mutations[:8]),
                        }
                    ],
                ),
                error=ErrorInfo(
                    "frontend_mutated_input",
                    "frontend",
                    "KSP analysis changed authored input.",
                ),
            )
        validation = GeneratedProjectValidator(self.root).validate(
            jvm_manifests=manifests,
        )
        records = [self.features.find_record(name) for name in decisions.package_names]
        result = self._validation_command_result(validation)
        result.modules = [record.info() for record in records]
        result.requested_targets = list(decisions.package_names)
        result.affected_targets = [
            *decisions.package_names,
            "integrity manifest",
            "shared runtime",
        ]
        return result

    def _plan(
        self,
        operation: str,
        *,
        allow_unmanifested_bootstrap: bool = False,
        requested_targets: tuple[str, ...] | None = None,
    ) -> tuple[GenerationPlan, object]:
        before = source_tree_inventory(self.root)
        project = ProjectModel.discover(
            self.root,
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        jvm_feature_ids = tuple(
            feature.identity.feature_id
            for feature in project.features
            if feature.jvm_root.is_dir()
            and any(
                path.is_file() and path.suffix.lower() in {".kt", ".java"}
                for path in iter_tree_no_follow(feature.jvm_root)
            )
        )
        if jvm_feature_ids:
            bootstrap = GenerationService(self.root).plan(
                operation=operation,
                requested_targets=(
                    requested_targets
                    or tuple(
                        feature.identity.npm_name for feature in project.features
                    )
                ),
                jvm_apis={feature_id: SemanticApi() for feature_id in jvm_feature_ids},
                allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
            )
            GenerationService(self.root).execute(bootstrap, finalize=False)
        manifests = JvmFrontendService(self.root).manifests(
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        mutations = source_tree_changes(before, source_tree_inventory(self.root))
        if mutations:
            raise GeneratorError(
                "KSP analysis changed authored input: " + ", ".join(mutations[:8]),
                kind="frontend_mutated_input",
                phase="frontend",
            )
        records = self.features.records()
        requested = requested_targets or tuple(
            record.manifest.npm_name for record in records
        )
        plan = GenerationService(self.root).plan(
            operation=operation,
            requested_targets=requested,
            jvm_manifests=manifests,
            jvm_adapter_sources=getattr(manifests, "adapter_sources", {}),
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        return plan, manifests

    def _validate_add(self, decisions: FeatureAddDecisions) -> None:
        identity = FeatureIdentity.create(
            npm_name=decisions.package_name,
            android_namespace=decisions.android_namespace,
            package_version=decisions.package_version,
        )
        validate_javascript_name(decisions.public_name)
        normalize_description(decisions.description)
        destination = identity.destination(self.root)
        resumable = self.features.resumable_incomplete_activation(destination)
        if lexists(destination) and not resumable:
            raise ConfigurationError(f'module "{decisions.package_name}" already exists')
        _, package = read_parent_package(self.root)
        for section in ("dependencies", "devDependencies"):
            values = package.get(section, {})
            if isinstance(values, dict) and decisions.package_name in values:
                raise ConfigurationError(
                    f'dependency "{decisions.package_name}" already exists; '
                    "remove or rename the conflicting dependency before scaffolding"
                )
        for record in self.features.records(
            allowed_incomplete=destination if resumable else None
        ):
            if record.manifest.public_name == decisions.public_name:
                raise ConfigurationError(
                    f'JavaScript name "{decisions.public_name}" is already used by '
                    f'"{record.manifest.npm_name}"'
                )
            if record.manifest.android_namespace == decisions.android_namespace:
                raise ConfigurationError(
                    f'Android namespace "{decisions.android_namespace}" is already used by '
                    f'"{record.manifest.npm_name}"'
                )

    def _result(
        self,
        command: str,
        plan: GenerationPlan,
        validation: GeneratedProjectValidationResult,
        *,
        module=None,
        extra_changes: list[Change] | None = None,
        next_action: str | None = None,
    ) -> CommandResult:
        result = self._validation_command_result(validation, command=command)
        result.module = module
        result.changes = [
            *(extra_changes or []),
            *[
                Change(
                    str(self.root / change.path),
                    change.action.value,
                    change.artifact.owner if change.artifact else "generated",
                )
                for change in plan.changes
            ],
        ]
        result.requested_targets = list(plan.requested_targets)
        result.affected_targets = list(plan.affected_targets)
        result.next_action = next_action if result.exit_code == 0 else result.next_action
        result.metadata.update(
            {
                "generation_id": plan.generation_id,
                "written_artifacts": len(plan.artifacts),
                "removed_stale_artifacts": len(plan.deletes),
            }
        )
        return result

    def _validation_command_result(
        self,
        validation: GeneratedProjectValidationResult,
        *,
        command: str = "validate",
    ) -> CommandResult:
        public = self._validation_result(validation)
        if validation.status == "success":
            return CommandResult(command, validation=public)
        message = (
            validation.issues[0].message
            if validation.issues
            else "Generated project validation failed."
        )
        return CommandResult(
            command,
            status="failure",
            exit_code=1,
            validation=public,
            diagnostics=list(validation.diagnostics),
            next_action="Correct the reported input or run plain `sn-module-gen update`.",
            error=ErrorInfo("validation_failed", "validate", message),
        )

    @staticmethod
    def _validation_result(
        result: GeneratedProjectValidationResult,
    ) -> ValidationResult:
        structural_failed = any(issue.scope != "dependency" for issue in result.issues)
        return ValidationResult(
            structural="failed" if structural_failed else "passed",
            integration="not_requested",
            dependency_link=result.dependency_link,
            build="not_requested",
            issues=[issue.manifest() for issue in result.issues],
        )

    def _package_warnings(self) -> list[WarningInfo]:
        project = ProjectModel.discover(self.root)
        return [
            WarningInfo(
                issue.code,
                issue.message,
                "dependency",
                issue.suggested_command or "Refresh declared dependencies with npm or Yarn.",
            )
            for issue in validate_author_package_inputs(self.root, project)
        ]

    @staticmethod
    def _partial_generation_error(command: str, exc: BaseException) -> PartialFailure:
        detail = exc.message if isinstance(exc, GeneratorError) else str(exc)
        return PartialFailure(
            f"{command} did not complete generated publication: {detail}. "
            "Authored input was not rolled back; correct the cause and rerun plain "
            "`sn-module-gen update`.",
            kind="generated_publication_incomplete",
            phase="apply",
        )
