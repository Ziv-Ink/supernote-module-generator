"""Transactional public CLI operations for logical features."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable, Iterable, Optional

from .errors import ConfigurationError, FilesystemError, GeneratorError, SubprocessFailure
from .feature_generator import FeatureConfig
from .feature_identity import FeatureIdentity
from .feature_operations import (
    FeatureOperationService,
    FeatureRecord,
)
from .feature_workflows import (
    FeatureAddDecisions,
    FeatureRemoveDecisions,
    FeatureUpdateDecisions,
    FeatureValidateDecisions,
)
from .filesystem import (
    SourceTreeInventory,
    entry_kind,
    iter_tree_no_follow,
    lexists,
    protected_source_snapshot_roots,
    protected_directory_metadata,
    read_regular_bytes_no_follow,
    source_tree_changes,
    source_tree_inventory,
    validate_source_symlink_support,
)
from .generation_service import GenerationService
from .generation_plan import PlanConflictError
from .integration import add_dependency
from .models import (
    Change,
    CommandResult,
    DependencyResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
    SubprocessError,
    ValidationResult,
)
from .platform_tools import gradle_wrapper_command, gradle_wrapper_path
from .naming import (
    normalize_description,
    validate_javascript_name,
)
from .plugin_build_integration import integration_mutation_files
from .plugin_runtime_codegen import RUNTIME_RELATIVE_ROOT
from .project import (
    dependency_link_path,
    dependency_value,
    ensure_within_plugin,
    manager_evidence,
    parent_mutation_targets,
    read_parent_package,
)
from .rendering import ProgressReporter, Renderer
from .subprocesses import run_process
from .transaction import Transaction
from .verification import build_android
from .cli_operations import CliOperationService
from .validation import GeneratedProjectValidator


def _json_object_members(text: str, start: int) -> tuple[list[dict[str, object]], int]:
    """Return exact member/value spans for one JSON object."""

    if start >= len(text) or text[start] != "{":
        raise FilesystemError("Parent package metadata must contain JSON objects")
    decoder = json.JSONDecoder()
    members: list[dict[str, object]] = []
    keys: set[str] = set()
    position = start + 1
    previous_comma: int | None = None
    while True:
        member_start = position
        while position < len(text) and text[position].isspace():
            position += 1
        if position < len(text) and text[position] == "}":
            return members, position + 1
        key_start = position
        try:
            key, key_end = decoder.raw_decode(text, position)
        except ValueError as exc:
            raise FilesystemError("Parent package metadata is invalid JSON") from exc
        if not isinstance(key, str):
            raise FilesystemError("Parent package metadata has a non-string key")
        if key in keys:
            raise FilesystemError(
                f"Parent package metadata contains an ambiguous duplicate key: {key}"
            )
        keys.add(key)
        position = key_end
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != ":":
            raise FilesystemError("Parent package metadata is invalid JSON")
        position += 1
        while position < len(text) and text[position].isspace():
            position += 1
        value_start = position
        try:
            value, value_end = decoder.raw_decode(text, position)
        except ValueError as exc:
            raise FilesystemError("Parent package metadata is invalid JSON") from exc
        position = value_end
        while position < len(text) and text[position].isspace():
            position += 1
        comma = position if position < len(text) and text[position] == "," else None
        members.append(
            {
                "key": key,
                "value": value,
                "member_start": member_start,
                "key_start": key_start,
                "value_start": value_start,
                "value_end": value_end,
                "previous_comma": previous_comma,
                "comma": comma,
            }
        )
        if comma is None:
            if position >= len(text) or text[position] != "}":
                raise FilesystemError("Parent package metadata is invalid JSON")
            return members, position + 1
        previous_comma = comma
        position = comma + 1


def _dependency_context(
    text: str, package_name: str
) -> tuple[dict[str, object] | None, list[dict[str, object]], dict[str, object] | None]:
    start = text.find("{")
    if start < 0:
        raise FilesystemError("Parent package metadata is invalid JSON")
    top, _end = _json_object_members(text, start)
    dependencies = next(
        (member for member in top if member["key"] == "dependencies"), None
    )
    if dependencies is None:
        return None, [], None
    value_start = int(dependencies["value_start"])
    if value_start >= len(text) or text[value_start] != "{":
        raise FilesystemError("Parent dependencies changed to a non-object")
    members, _end = _json_object_members(text, value_start)
    return (
        dependencies,
        members,
        next((member for member in members if member["key"] == package_name), None),
    )


def _remove_json_member_exact(text: str, member: dict[str, object]) -> str:
    comma = member["comma"]
    previous_comma = member["previous_comma"]
    if comma is not None:
        remove_start = int(member["member_start"])
        remove_end = int(comma) + 1
    elif previous_comma is not None:
        remove_start = int(previous_comma)
        remove_end = int(member["value_end"])
    else:
        remove_start = int(member["key_start"])
        remove_end = int(member["value_end"])
    return text[:remove_start] + text[remove_end:]


def _reverse_dependency_add_exact(
    original: bytes,
    live: bytes,
    package_name: str,
) -> bytes:
    """Reverse only the generator's dependency member, preserving live bytes."""

    try:
        original_text = original.decode("utf-8")
        live_text = live.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FilesystemError("Parent package metadata must be UTF-8") from exc
    original_dependencies, _original_members, original_member = _dependency_context(
        original_text, package_name
    )
    live_dependencies, live_members, live_member = _dependency_context(
        live_text, package_name
    )
    if live_member is None:
        if original_member is None:
            return live
        raise FilesystemError(
            "The parent dependency changed concurrently and cannot be separated exactly"
        )
    if live_member["value"] != dependency_value(package_name):
        raise FilesystemError(
            "The attempted dependency changed concurrently and cannot be separated exactly"
        )
    if original_member is not None:
        original_value = original_text[
            int(original_member["value_start"]):int(original_member["value_end"])
        ]
        updated = (
            live_text[: int(live_member["value_start"])]
            + original_value
            + live_text[int(live_member["value_end"]):]
        )
        return updated.encode("utf-8")
    if original_dependencies is None and len(live_members) == 1:
        assert live_dependencies is not None
        return _remove_json_member_exact(live_text, live_dependencies).encode("utf-8")
    return _remove_json_member_exact(live_text, live_member).encode("utf-8")


class FeatureCliOperationService:
    def __init__(
        self,
        root: Path,
        renderer: Renderer,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.root = root.resolve()
        self.renderer = renderer
        self.progress = ProgressReporter(renderer)
        self.run = run
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
        baseline = source_tree_inventory(self.root)
        directory_metadata = protected_directory_metadata(self.root)
        transaction = Transaction(self.root, "add", [decisions.package_name])
        transaction.record_directory_metadata(directory_metadata)
        dependency: DependencyResult | None = None
        plan = None
        jvm_manifests = None
        success_result: CommandResult | None = None
        conflict_baseline: SourceTreeInventory | None = None
        try:
            transaction.snapshot(protected_source_snapshot_roots(self.root))
            self._snapshot_operation(
                transaction,
                [destination, *self._generated_api_outputs()],
            )
            transaction.checkpoint("after_planning")
            transaction.set_phase("apply")
            with self.progress.phase("Generating feature", "Generated feature"):
                created = self.features.add(config, transaction=transaction)
                add_dependency(self.root, decisions.package_name)
                transaction.record_snapshot_results(
                    [
                        self.root / "package.json",
                        *integration_mutation_files(self.root),
                    ]
                )
                transaction.checkpoint("after_dependency_edit")
                jvm_manifests = CliOperationService(
                    self.root
                )._jvm_frontend_manifests(
                    allow_unmanifested_bootstrap=True
                )
                plan = GenerationService(self.root).plan(
                    operation="add",
                    requested_targets=(decisions.package_name,),
                    jvm_manifests=jvm_manifests,
                    allow_unmanifested_bootstrap=True,
                )
                conflict_baseline = source_tree_inventory(self.root)
                GenerationService(self.root).execute(
                    plan, transaction, commit=False
                )
            dependency = self._dependency_result(
                transaction,
                requested=decisions.install,
                manager=decisions.package_manager,
                action="install_dependency",
            )
            integrity = GeneratedProjectValidator(self.root).validate(
                jvm_manifests=jvm_manifests,
                validate_dependencies=decisions.install,
                build=decisions.build,
                parent_transaction_id=transaction.identifier,
            )
            validation = self._validation_result(integrity)
            if _failed(validation):
                assert plan is not None
                return self._validation_failure(
                    "add",
                    transaction,
                    baseline,
                    integrity,
                    validation,
                    plan,
                    dependency=dependency,
                )
            record = self.features.find_record(decisions.package_name)
            assert plan is not None
            planned_changes = [
                Change(
                    str(self.root / change.path),
                    change.action.value,
                    (
                        change.artifact.owner
                        if change.artifact is not None
                        else "generated"
                    ),
                )
                for change in plan.changes
            ]
            success_result = CommandResult(
                "add",
                module=record.info(),
                changes=[
                    Change(str(created), "created", "feature_generated"),
                    Change(str(self.root / "package.json"), "updated", "parent"),
                    *planned_changes,
                ],
                dependency=dependency,
                validation=validation,
                requested_targets=[decisions.package_name],
                affected_targets=list(plan.affected_targets),
                diagnostics=list(integrity.diagnostics),
                metadata={
                    "built": decisions.build,
                    "generation_id": plan.generation_id,
                    "build_duration_ms": integrity.build_duration_ms,
                },
            )
            if not decisions.install:
                success_result.next_action = self._next_install(decisions.package_manager)
            transaction.commit()
            return success_result
        except KeyboardInterrupt:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "add", transaction, plan, success_result, interrupted=True
                )
            return self._cancel("add", transaction, baseline=baseline)
        except Exception as exc:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "add", transaction, plan, success_result, interrupted=False
                )
            if isinstance(exc, PlanConflictError):
                return self._plan_conflict_result(
                    "add",
                    transaction,
                    exc,
                    plan,
                    conflict_baseline=conflict_baseline,
                )
            return self._failure("add", transaction, exc, baseline=baseline)

    def update(self, decisions: FeatureUpdateDecisions) -> CommandResult:
        record = self.features.find_record(decisions.package_name)
        validate_source_symlink_support(
            (
                record.path / record.manifest.roots.native,
                record.path / record.manifest.roots.jvm,
            )
        )
        refresh = self._refresh_required(record)
        if refresh and not decisions.skip_install:
            self._health_check_manager(decisions.package_manager)
        self._health_check_semantics()
        if decisions.build:
            self._health_check_build()
        plan = None
        jvm_manifests = None
        if not self._has_jvm_sources():
            jvm_manifests = {}
            try:
                plan = GenerationService(self.root).plan(
                    operation="update",
                    requested_targets=(decisions.package_name,),
                    jvm_manifests=jvm_manifests,
                )
            except Exception as exc:
                return CommandResult(
                    "update",
                    status="failure",
                    exit_code=(exc.exit_code if isinstance(exc, GeneratorError) else 1),
                    requested_targets=[decisions.package_name],
                    error=ErrorInfo(
                        exc.kind if isinstance(exc, GeneratorError) else "invalid_source",
                        exc.phase if isinstance(exc, GeneratorError) else "preflight",
                        exc.message if isinstance(exc, GeneratorError) else str(exc),
                    ),
                )
            if (
                plan.is_noop
                and not decisions.build
                and (not refresh or decisions.skip_install)
            ):
                try:
                    GenerationService(self.root).validate_preconditions(plan)
                except PlanConflictError as exc:
                    return CommandResult(
                        "update",
                        status="failure",
                        exit_code=1,
                        requested_targets=[decisions.package_name],
                        affected_targets=list(plan.affected_targets),
                        error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                        next_action="The project changed during planning. Review the external edit and rerun the command.",
                    )
                dependency = DependencyResult(
                    False,
                    decisions.package_manager,
                    "skipped" if refresh else "not_needed",
                    False,
                    [],
                    0,
                )
                result = CommandResult(
                    "update",
                    module=record.info(),
                    changes=[],
                    dependency=dependency,
                    requested_targets=[decisions.package_name],
                    affected_targets=list(plan.affected_targets),
                    metadata={
                        "built": False,
                        "generation_id": plan.generation_id,
                        "build_duration_ms": 0,
                        "no_op": True,
                    },
                )
                if refresh and decisions.skip_install:
                    result.next_action = self._next_install(decisions.package_manager)
                return result
        baseline = source_tree_inventory(self.root)
        directory_metadata = protected_directory_metadata(self.root)
        transaction = Transaction(self.root, "update", [decisions.package_name])
        transaction.record_directory_metadata(directory_metadata)
        success_result: CommandResult | None = None
        try:
            transaction.snapshot(protected_source_snapshot_roots(self.root))
            if plan is None:
                jvm_manifests = CliOperationService(
                    self.root
                )._jvm_frontend_manifests()
                frontend_mutations = source_tree_changes(
                    baseline, source_tree_inventory(self.root)
                )
                if frontend_mutations:
                    raise GeneratorError(
                        "The JVM frontend changed protected source state: "
                        + ", ".join(frontend_mutations[:8]),
                        kind="build_mutated_source",
                        phase="frontend",
                    )
                plan = GenerationService(self.root).plan(
                    operation="update",
                    requested_targets=(decisions.package_name,),
                    jvm_manifests=jvm_manifests,
                )
            transaction.checkpoint("after_planning")
            transaction.set_phase("apply")
            with self.progress.phase("Updating feature", "Updated feature"):
                if not plan.is_noop:
                    GenerationService(self.root).execute(
                        plan, transaction, commit=False
                    )
            dependency = self._dependency_result(
                transaction,
                requested=refresh and not decisions.skip_install,
                manager=decisions.package_manager,
                action="refresh_dependency",
                skipped_status="skipped" if refresh else "not_needed",
            )
            updated = self.features.find_record(decisions.package_name)
            integrity = GeneratedProjectValidator(self.root).validate(
                jvm_manifests=jvm_manifests,
                validate_dependencies=not decisions.skip_install,
                build=decisions.build,
                parent_transaction_id=transaction.identifier,
            )
            validation = self._validation_result(integrity)
            if _failed(validation):
                return self._validation_failure(
                    "update",
                    transaction,
                    baseline,
                    integrity,
                    validation,
                    plan,
                    dependency=dependency,
                )
            assert plan is not None
            success_result = CommandResult(
                "update",
                module=updated.info(),
                changes=self._plan_changes(plan),
                dependency=dependency,
                validation=validation,
                requested_targets=[decisions.package_name],
                affected_targets=list(plan.affected_targets),
                diagnostics=list(integrity.diagnostics),
                metadata={
                    "built": decisions.build,
                    "generation_id": plan.generation_id,
                    "build_duration_ms": integrity.build_duration_ms,
                    "no_op": (
                        plan.is_noop
                        and not (refresh and not decisions.skip_install)
                    ),
                },
            )
            if dependency.status == "skipped":
                success_result.next_action = self._next_install(decisions.package_manager)
            transaction.commit()
            return success_result
        except KeyboardInterrupt:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "update", transaction, plan, success_result, interrupted=True
                )
            if transaction.abandon_is_durable():
                return self._interrupted_abandon_result(
                    "update", transaction, plan
                )
            return self._cancel("update", transaction, baseline=baseline)
        except Exception as exc:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "update", transaction, plan, success_result, interrupted=False
                )
            if isinstance(exc, PlanConflictError):
                return self._plan_conflict_result("update", transaction, exc, plan)
            return self._failure("update", transaction, exc, baseline=baseline)

    def remove(self, decisions: FeatureRemoveDecisions) -> CommandResult:
        records = [self.features.find_record(name) for name in decisions.package_names]
        if not records:
            return CommandResult("remove", metadata={"empty": True, "removed_count": 0})
        if not decisions.skip_install:
            self._health_check_manager(decisions.package_manager)
        baseline = source_tree_inventory(self.root)
        directory_metadata = protected_directory_metadata(self.root)
        transaction = Transaction(
            self.root, "remove", [record.manifest.npm_name for record in records]
        )
        transaction.record_directory_metadata(directory_metadata)
        plan = None
        success_result: CommandResult | None = None
        jvm_manifests = None
        try:
            removed_roots = tuple(record.path for record in records)
            if len(records) == len(self.features.records()):
                removed_roots = (
                    *removed_roots,
                    self.root / RUNTIME_RELATIVE_ROOT,
                )
            transaction.snapshot(
                path
                for path in protected_source_snapshot_roots(self.root)
                if not any(
                    path == removed_root or path.is_relative_to(removed_root)
                    for removed_root in removed_roots
                )
            )
            transaction.snapshot(
                [
                    *parent_mutation_targets(self.root),
                    *integration_mutation_files(self.root),
                ]
            )
            jvm_manifests = CliOperationService(
                self.root
            )._jvm_frontend_manifests()
            frontend_mutations = source_tree_changes(
                baseline, source_tree_inventory(self.root)
            )
            if frontend_mutations:
                raise GeneratorError(
                    "The JVM frontend changed protected source state: "
                    + ", ".join(frontend_mutations[:8]),
                    kind="build_mutated_source",
                    phase="frontend",
                )
            plan = GenerationService(self.root).plan(
                operation="remove",
                requested_targets=tuple(decisions.package_names),
                removed_targets=tuple(decisions.package_names),
                jvm_manifests=jvm_manifests,
            )
            transaction.checkpoint("after_planning")
            transaction.set_phase("apply")
            with self.progress.phase("Removing features", "Removed features"):
                GenerationService(self.root).execute(
                    plan, transaction, commit=False
                )
            dependency = self._dependency_result(
                transaction,
                requested=not decisions.skip_install,
                manager=decisions.package_manager,
                action="refresh_dependency",
            )
            integrity = GeneratedProjectValidator(self.root).validate(
                jvm_manifests={
                    feature_id: manifest
                    for feature_id, manifest in jvm_manifests.items()
                    if feature_id
                    not in {record.manifest.feature_id for record in records}
                },
                validate_dependencies=not decisions.skip_install,
                parent_transaction_id=transaction.identifier,
            )
            validation = self._validation_result(integrity)
            if _failed(validation):
                return self._validation_failure(
                    "remove",
                    transaction,
                    baseline,
                    integrity,
                    validation,
                    plan,
                    dependency=dependency,
                )
            removed_build_paths: list[Path] = []
            if decisions.delete_build_files:
                for path in self._build_output_paths():
                    ensure_within_plugin(self.root, path)
                    if path.exists():
                        transaction.detach(path)
                        removed_build_paths.append(path)
            infos = [record.info() for record in records]
            success_result = CommandResult(
                "remove",
                module=infos[0] if len(infos) == 1 else None,
                modules=infos if len(infos) > 1 else [],
                changes=[
                    *self._plan_changes(plan),
                    *[
                        Change(str(path), "removed", "generated_build_output")
                        for path in removed_build_paths
                    ],
                ],
                dependency=dependency,
                validation=validation,
                requested_targets=list(plan.requested_targets),
                affected_targets=list(plan.affected_targets),
                diagnostics=list(integrity.diagnostics),
                metadata={
                    "removed_count": len(infos),
                    "build_files_deleted": decisions.delete_build_files,
                    "generation_id": plan.generation_id,
                    "built": False,
                    "build_duration_ms": integrity.build_duration_ms,
                },
            )
            if decisions.skip_install:
                success_result.next_action = self._next_install(
                    decisions.package_manager
                )
            transaction.commit()
            return success_result
        except KeyboardInterrupt:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "remove", transaction, plan, success_result, interrupted=True
                )
            if transaction.abandon_is_durable():
                return self._interrupted_abandon_result(
                    "remove", transaction, plan
                )
            return self._cancel("remove", transaction, baseline=baseline)
        except Exception as exc:
            if transaction.commit_is_durable():
                return self._durable_commit_result(
                    "remove", transaction, plan, success_result, interrupted=False
                )
            if isinstance(exc, PlanConflictError):
                return self._plan_conflict_result("remove", transaction, exc, plan)
            return self._failure("remove", transaction, exc, baseline=baseline)

    def _build_output_paths(self) -> tuple[Path, ...]:
        return (
            self.root / "build",
            self.root / "android/build",
            self.root / "android/app/build",
        )

    def validate(self, decisions: FeatureValidateDecisions) -> CommandResult:
        records = [self.features.find_record(name) for name in decisions.package_names]
        validation = self._validate_records(records, dependency_requested=True)
        build_error: SubprocessError | None = None
        if decisions.build:
            success, build_error, _ = build_android(
                self.root,
                verbose=self.renderer.mode == "verbose",
                stream=self._stream,
            )
            validation = replace(validation, build="passed" if success else "failed")
        infos = [replace(record.info(), validation=validation) for record in records]
        if _failed(validation):
            result = CommandResult(
                "validate",
                status="failure",
                exit_code=1,
                module=infos[0] if not decisions.all else None,
                modules=infos if decisions.all else [],
                validation=validation,
                error=ErrorInfo(
                    "validation_failed",
                    "check",
                    validation.issues[0]["message"] if validation.issues else "Validation failed.",
                    build_error,
                ),
            )
            issue_kinds = {str(issue.get("kind")) for issue in validation.issues}
            if issue_kinds and issue_kinds <= {"dependency", "dependency_link"}:
                result.metadata["next_action"] = self._next_install(
                    manager_evidence(self.root).sole
                )
            return result
        return CommandResult(
            "validate",
            module=infos[0] if not decisions.all else None,
            modules=infos if decisions.all else [],
            validation=validation,
            metadata={"built": decisions.build},
        )

    def _validate_add(self, decisions: FeatureAddDecisions) -> None:
        identity = FeatureIdentity.create(
            npm_name=decisions.package_name,
            android_namespace=decisions.android_namespace,
            package_version=decisions.package_version,
        )
        validate_javascript_name(decisions.public_name)
        normalize_description(decisions.description)
        destination = identity.destination(self.root)
        if lexists(destination):
            raise ConfigurationError(f'module "{decisions.package_name}" already exists')
        _, package = read_parent_package(self.root)
        for section in ("dependencies", "devDependencies"):
            values = package.get(section, {})
            if isinstance(values, dict) and decisions.package_name in values:
                raise ConfigurationError(
                    f'dependency "{decisions.package_name}" already points to a different location'
                )
        for record in self.features.records():
            if record.manifest.public_name == decisions.public_name:
                raise ConfigurationError(
                    f'JavaScript name "{decisions.public_name}" is already used by "{record.manifest.npm_name}"'
                )
            if record.manifest.android_namespace == decisions.android_namespace:
                raise ConfigurationError(
                    f'Android namespace "{decisions.android_namespace}" is already used by "{record.manifest.npm_name}"'
                )
        if decisions.install:
            self._health_check_manager(decisions.package_manager)
        self._health_check_semantics()
        if decisions.build:
            self._health_check_build()

    def _snapshot_operation(
        self, transaction: Transaction, feature_paths: Iterable[Path]
    ) -> None:
        paths = [
            *parent_mutation_targets(self.root),
            *integration_mutation_files(self.root),
            *feature_paths,
        ]
        transaction.snapshot(paths)

    def _generated_api_outputs(
        self, *, excluding: Path | None = None
    ) -> list[Path]:
        excluded = excluding.resolve() if excluding is not None else None
        return [
            output
            for feature in self.features.feature_paths()
            if excluded is None or feature.resolve() != excluded
            for output in (feature / "index.d.ts", feature / "README.md")
        ]

    def _validate_records(
        self, records: list[FeatureRecord], *, dependency_requested: bool
    ) -> ValidationResult:
        issues = [
            {"kind": "structure", "message": message}
            for message in self.features.verify_generated_state()
        ]
        dependency_failed = False
        _, package = read_parent_package(self.root)
        dependencies = package.get("dependencies", {})
        for record in records:
            expected = dependency_value(record.manifest.npm_name)
            actual = (
                dependencies.get(record.manifest.npm_name)
                if isinstance(dependencies, dict)
                else None
            )
            if actual != expected:
                dependency_failed = True
                issues.append(
                    {
                        "kind": "dependency",
                        "message": f'{record.manifest.npm_name} is not linked from package.json',
                    }
                )
            if dependency_requested:
                link = dependency_link_path(self.root, record.manifest.npm_name)
                try:
                    linked = link.exists() and link.resolve() == record.path.resolve()
                except OSError:
                    linked = False
                if not linked:
                    dependency_failed = True
                    issues.append(
                        {
                            "kind": "dependency_link",
                            "message": f'{record.manifest.npm_name} is not installed in node_modules',
                        }
                    )
        structural_failed = any(issue["kind"] == "structure" for issue in issues)
        return ValidationResult(
            structural="failed" if structural_failed else "passed",
            integration="failed" if structural_failed else "passed",
            dependency_link="failed" if dependency_failed else "passed",
            issues=issues,
        )

    def _validation_result(self, result) -> ValidationResult:
        structural_failed = any(
            issue.code.startswith("SNMG_ARTIFACT")
            or issue.code in {"SNMG_INPUT_INVALID", "SNMG_JAVASCRIPT_INVALID"}
            for issue in result.issues
        )
        integration_failed = any(
            issue.code == "SNMG_WIRING_INVALID" for issue in result.issues
        )
        dependency_failed = any(
            issue.code.startswith("SNMG_DEPENDENCY") for issue in result.issues
        )
        return ValidationResult(
            structural="failed" if structural_failed else "passed",
            integration="failed" if integration_failed else "passed",
            dependency_link="failed" if dependency_failed else "passed",
            build=(
                "passed"
                if result.build == "passed"
                else "failed"
                if result.build == "failed"
                else "not_requested"
            ),
            issues=[issue.manifest() for issue in result.issues],
        )

    def _raise_build_failure(self, result) -> None:
        if result.build != "failed":
            return
        subprocess_error = (
            result.build_error.to_dict()
            if result.build_error is not None
            else None
        )
        message = (
            result.issues[0].message
            if result.issues
            else "Android build validation failed."
        )
        raise SubprocessFailure(
            message,
            kind="build_failed",
            phase="build",
            subprocess=subprocess_error,
        )

    def _plan_changes(self, plan) -> list[Change]:
        return [
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

    def _validation_failure(
        self,
        command: str,
        transaction: Transaction,
        baseline: SourceTreeInventory,
        integrity,
        validation: ValidationResult,
        plan,
        *,
        dependency: DependencyResult | None,
    ) -> CommandResult:
        rollback, residue = self._verified_rollback(transaction, baseline)
        build_failed = integrity.build == "failed"
        message = (
            integrity.issues[0].message
            if integrity.issues
            else "Generated state validation failed."
        )
        next_action = (
            "Disable or fix the source-writing build hook, restore affected source, and rerun the command."
            if any(
                issue.code == "SNMG_BUILD_MUTATED_SOURCE"
                for issue in integrity.issues
            )
            else "Review the diagnostics log and correct the Android build failure."
            if build_failed
            else "Run `sn-module-gen update --all --dry-run --diff` to preview repair."
        )
        return CommandResult(
            command,
            status="failure" if rollback.status == "completed" else "partial",
            exit_code=1 if rollback.status == "completed" else 3,
            changes=[*self._plan_changes(plan), *residue],
            dependency=dependency,
            validation=validation,
            rollback=rollback,
            requested_targets=list(plan.requested_targets),
            affected_targets=list(plan.affected_targets),
            diagnostics=list(integrity.diagnostics),
            next_action=next_action,
            error=ErrorInfo(
                "build_failed" if build_failed else "integrity_failed",
                "build" if build_failed else "verify",
                message,
                integrity.build_error,
            ),
            metadata={
                "generation_id": plan.generation_id,
                "build_duration_ms": integrity.build_duration_ms,
            },
        )

    def _dependency_result(
        self,
        transaction: Transaction,
        *,
        requested: bool,
        manager: Optional[str],
        action: str,
        skipped_status: str = "skipped",
    ) -> DependencyResult:
        if not requested:
            return DependencyResult(False, manager, skipped_status, False, [], 0)
        assert manager is not None
        command = [manager, "install"]
        transaction.mark_external(command)
        started = time.monotonic()
        self._run(command, phase=action)
        return DependencyResult(
            True,
            manager,
            "installed" if action == "install_dependency" else "refreshed",
            True,
            command,
            round((time.monotonic() - started) * 1000),
        )

    def _run(self, command: list[str], *, phase: str) -> None:
        try:
            if self.run is subprocess.run:
                result = run_process(
                    command,
                    cwd=self.root,
                    timeout=600,
                    stream=self._stream if self.renderer.mode == "verbose" else None,
                )
            else:
                result = self.run(
                    command,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SubprocessFailure(
                f"{command[0]} could not run.",
                kind=f"{phase}_failed",
                phase=phase,
            ) from exc
        if result.returncode:
            raise SubprocessFailure(
                f"{command[0]} could not complete the requested operation.",
                kind=f"{phase}_failed",
                phase=phase,
                subprocess={
                    "command": command,
                    "exit_code": result.returncode,
                    "relevant_lines": (result.stderr or result.stdout).splitlines()[-8:],
                },
            )

    def _stream(self, destination: str, content: str) -> None:
        target = self.renderer.stdout if destination == "stdout" else self.renderer.stderr
        target.write(content)
        target.flush()

    def _health_check_manager(self, manager: Optional[str]) -> None:
        if manager is None:
            raise ConfigurationError("package manager is ambiguous")
        if shutil.which("node") is None:
            raise ConfigurationError("node is not available")
        if shutil.which(manager) is None:
            raise ConfigurationError(f"{manager} is not available")

    def _health_check_build(self) -> None:
        gradle = gradle_wrapper_path(self.root)
        if not gradle.is_file():
            raise ConfigurationError("Android Gradle wrapper is not available")

    def _health_check_semantics(self) -> None:
        gradle = gradle_wrapper_path(self.root)
        if not gradle.is_file():
            raise ConfigurationError(
                "Android Gradle wrapper is required to refresh the JavaScript API"
            )

    def _refresh_semantics(self) -> None:
        gradle = gradle_wrapper_path(self.root)
        command = gradle_wrapper_command(
            gradle,
            [":supernote-runtime:generateSupernoteDebugSemantics"],
        )
        try:
            result = run_process(
                command,
                cwd=self.root / "android",
                timeout=1200,
                stream=self._stream if self.renderer.mode == "verbose" else None,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SubprocessFailure(
                "Gradle could not refresh the generated JavaScript API.",
                kind="api_generation_failed",
                phase="api_generation",
                subprocess={
                    "command": command,
                    "exit_code": 1,
                    "relevant_lines": [str(exc)],
                },
            ) from exc
        if result.returncode:
            output = result.stderr or result.stdout
            raise SubprocessFailure(
                "Gradle could not refresh the generated JavaScript API.",
                kind="api_generation_failed",
                phase="api_generation",
                subprocess={
                    "command": command,
                    "exit_code": result.returncode,
                    "relevant_lines": output.splitlines()[-8:],
                },
            )

    def _build(self) -> None:
        success, error, _ = build_android(
            self.root,
            verbose=self.renderer.mode == "verbose",
            stream=self._stream,
        )
        if not success:
            assert error is not None
            raise SubprocessFailure(
                "Gradle could not build the generated plugin runtime.",
                kind="build_failed",
                phase="build",
                subprocess=error.to_dict(),
            )

    def _refresh_required(self, record: FeatureRecord) -> bool:
        _, package = read_parent_package(self.root)
        dependencies = package.get("dependencies", {})
        value = (
            dependencies.get(record.manifest.npm_name)
            if isinstance(dependencies, dict)
            else None
        )
        link = dependency_link_path(self.root, record.manifest.npm_name)
        try:
            linked = link.exists() and link.resolve() == record.path.resolve()
        except OSError:
            linked = False
        return value != dependency_value(record.manifest.npm_name) or not linked

    def _has_jvm_sources(self) -> bool:
        for record in self.features.records():
            root = record.path / record.manifest.roots.jvm
            if not root.is_dir():
                continue
            for path in iter_tree_no_follow(root):
                if path.is_file() and path.suffix.lower() in {".kt", ".java"}:
                    return True
        return False

    def _cancel(
        self,
        command: str,
        transaction: Transaction,
        *,
        baseline: SourceTreeInventory | None = None,
    ) -> CommandResult:
        rollback, residue = self._verified_rollback(
            transaction, baseline, reconcile=True
        )
        completed = rollback.status == "completed"
        cancellation_message = (
            "Operation cancelled. Previous state restored."
            if completed
            else "Operation cancelled, but exact restoration could not be verified."
        )
        return CommandResult(
            command,
            status="cancelled" if completed else "partial",
            exit_code=130 if completed else 3,
            changes=residue,
            rollback=rollback,
            error=(
                None
                if completed
                else ErrorInfo(
                    "cancellation_rollback_partial",
                    "rollback",
                    "Operation was cancelled, but exact source restoration could not be verified.",
                )
            ),
            metadata={
                "cancellation_requested": True,
                "cancellation_message": cancellation_message,
            },
            next_action=(
                None
                if completed
                else "Run `sn-module-gen doctor`, resolve dependency reconciliation, then rerun the original command."
            ),
        )

    def _durable_commit_result(
        self,
        command: str,
        transaction: Transaction,
        plan,
        success_result: CommandResult | None,
        *,
        interrupted: bool,
    ) -> CommandResult:
        """Finish committed cleanup; a durable commit must never be rolled back."""

        base = success_result or CommandResult(
            command,
            changes=self._plan_changes(plan) if plan is not None else [],
            requested_targets=(
                list(plan.requested_targets) if plan is not None else []
            ),
            affected_targets=(
                list(plan.affected_targets) if plan is not None else []
            ),
        )
        try:
            transaction.finish_commit()
        except BaseException as exc:
            recovery_path = getattr(exc, "recovery_path", None)
            return replace(
                base,
                status="partial",
                exit_code=3,
                rollback=RollbackResult(False, "not_needed", []),
                recovery=RecoveryAction(
                    (
                        "Generation committed, but transaction cleanup is incomplete."
                        + (
                            f" Recovery metadata is retained at {recovery_path}."
                            if recovery_path is not None
                            else (
                                " The durable transaction journal remains at "
                                f"{transaction.journal_path}."
                            )
                        )
                    ),
                    ["sn-module-gen", "doctor"],
                ),
                error=ErrorInfo("commit_cleanup_failed", "commit", str(exc)),
                metadata={
                    **base.metadata,
                    "commit_durable": True,
                    "cancellation_requested": interrupted,
                    "cancellation_status": "partial" if interrupted else "not_requested",
                    "cancellation_message": (
                        "Interrupt arrived after commit; committed state was retained."
                        if interrupted
                        else None
                    ),
                    "recovery_path": (
                        str(recovery_path)
                        if recovery_path is not None
                        else str(transaction.journal_path)
                    ),
                },
            )
        metadata = {
            **base.metadata,
            "commit_durable": True,
            "commit_cleanup_recovered": True,
            "success_message": "Generated state committed atomically",
        }
        if interrupted:
            metadata.update(
                {
                    "cancellation_requested": True,
                    "cancellation_status": "completed",
                    "cancellation_message": (
                        "Interrupt arrived after commit; committed state was retained."
                    ),
                }
            )
        return replace(
            base,
            rollback=RollbackResult(False, "not_needed", []),
            metadata=metadata,
        )

    def _plan_conflict_result(
        self,
        command: str,
        transaction: Transaction,
        exc: PlanConflictError,
        plan,
        *,
        conflict_baseline: SourceTreeInventory | None = None,
    ) -> CommandResult:
        """Abandon a stale plan without restoring over the external edit."""

        if transaction.mutated:
            if command == "add":
                return self._rollback_add_plan_conflict(
                    transaction,
                    exc,
                    plan,
                    conflict_baseline,
                )
            try:
                transaction.retain_conflict()
            except BaseException as retain_exc:
                return self._abandon_cleanup_failure(
                    command, retain_exc, plan, interrupted=False
                )
            return CommandResult(
                command,
                status="partial",
                exit_code=3,
                rollback=RollbackResult(True, "partial", []),
                requested_targets=(list(plan.requested_targets) if plan is not None else []),
                affected_targets=(list(plan.affected_targets) if plan is not None else []),
                recovery=RecoveryAction(
                    "The project changed after this operation had staged visible state. Automatic rollback is blocked to preserve the external edit.",
                    ["sn-module-gen", "doctor"],
                ),
                error=ErrorInfo("plan_conflict", "precommit", str(exc)),
                metadata={"conflict_retained": True},
            )
        interrupted = False
        try:
            transaction.preserve_current_directory_metadata(
                exc.preserve_directory_paths
            )
            if transaction.abandon_is_durable():
                transaction.finish_abandon()
            else:
                transaction.abandon_unmutated()
        except KeyboardInterrupt:
            interrupted = True
            try:
                if transaction.abandon_is_durable():
                    transaction.finish_abandon()
                else:
                    transaction.abandon_unmutated()
            except BaseException as cleanup_exc:
                return self._abandon_cleanup_failure(
                    command, cleanup_exc, plan, interrupted=True
                )
        except BaseException as cleanup_exc:
            return self._abandon_cleanup_failure(
                command, cleanup_exc, plan, interrupted=False
            )
        metadata = {
            "abandon_durable": True,
            "cancellation_requested": interrupted,
            "cancellation_status": "completed" if interrupted else "not_requested",
        }
        if interrupted:
            metadata["cancellation_message"] = (
                "Interrupt arrived during conflict cleanup; the external edit was retained."
            )
        return CommandResult(
            command,
            status="failure",
            exit_code=1,
            rollback=RollbackResult(False, "not_needed", []),
            requested_targets=(list(plan.requested_targets) if plan is not None else []),
            affected_targets=(list(plan.affected_targets) if plan is not None else []),
            next_action="The project changed during planning. Review the external edit and rerun the command.",
            error=ErrorInfo("plan_conflict", "precommit", str(exc)),
            metadata=metadata,
        )

    def _rollback_add_plan_conflict(
        self,
        transaction: Transaction,
        exc: PlanConflictError,
        plan,
        conflict_baseline: SourceTreeInventory | None,
    ) -> CommandResult:
        """Remove a partially visible add while retaining unrelated parent edits."""

        interrupted = False
        try:
            if conflict_baseline is None:
                raise FilesystemError(
                    "Fresh-add conflict baseline is unavailable; exact cleanup is unsafe"
                )
            package_path = self.root / "package.json"
            baseline_bytes = transaction.snapshot_file_bytes(package_path)
            requested_roots = []
            for package_name in (
                plan.requested_targets
                if plan is not None
                else transaction.data["modules"]
            ):
                requested_roots.append(
                    self.features.find_record(str(package_name)).path
                )
            external_mutations = transaction.preserve_external_source_changes(
                conflict_baseline,
                excluded_roots=requested_roots,
                passthrough_non_regular_paths=(package_path,),
            )
            package_was_edited = any(
                mutation.endswith(":package.json")
                for mutation in external_mutations
            )
            if package_was_edited:
                if entry_kind(package_path) != "file":
                    rollback = transaction.rollback()
                    return self._completed_add_conflict_result(
                        rollback, exc, plan, interrupted=False
                    )
                live_bytes, package_metadata = read_regular_bytes_no_follow(
                    package_path
                )
                merged = live_bytes
                for package_name in (
                    plan.requested_targets
                    if plan is not None
                    else transaction.data["modules"]
                ):
                    merged = _reverse_dependency_add_exact(
                        baseline_bytes,
                        merged,
                        str(package_name),
                    )
                transaction.replace_snapshot_file_baseline(
                    package_path,
                    merged,
                    mode=package_metadata.st_mode & 0o7777,
                    atime_ns=package_metadata.st_atime_ns,
                    mtime_ns=package_metadata.st_mtime_ns,
                )
            rollback = transaction.rollback()
        except KeyboardInterrupt:
            interrupted = True
            try:
                rollback = transaction.rollback()
            except BaseException as cleanup_exc:
                return self._abandon_cleanup_failure(
                    "add", cleanup_exc, plan, interrupted=True
                )
        except BaseException as cleanup_exc:
            return self._abandon_cleanup_failure(
                "add", cleanup_exc, plan, interrupted=False
            )
        return self._completed_add_conflict_result(
            rollback, exc, plan, interrupted=interrupted
        )

    def _completed_add_conflict_result(
        self,
        rollback: RollbackResult,
        exc: PlanConflictError,
        plan,
        *,
        interrupted: bool,
    ) -> CommandResult:
        if rollback.status != "completed":
            return CommandResult(
                "add",
                status="partial",
                exit_code=3,
                rollback=rollback,
                requested_targets=(
                    list(plan.requested_targets) if plan is not None else []
                ),
                affected_targets=(
                    list(plan.affected_targets) if plan is not None else []
                ),
                recovery=RecoveryAction(
                    "The stale add plan could not be removed exactly.",
                    ["sn-module-gen", "doctor"],
                ),
                error=ErrorInfo("plan_conflict_cleanup_failed", "rollback", str(exc)),
                metadata={
                    "cancellation_requested": interrupted,
                    "cancellation_status": "partial" if interrupted else "not_requested",
                },
            )
        return CommandResult(
            "add",
            status="failure",
            exit_code=1,
            rollback=rollback,
            requested_targets=(list(plan.requested_targets) if plan is not None else []),
            affected_targets=(list(plan.affected_targets) if plan is not None else []),
            next_action=(
                "The project changed during planning. The partial add was removed; "
                "review the external edit and rerun the command."
            ),
            error=ErrorInfo("plan_conflict", "precommit", str(exc)),
            metadata={
                "conflict_reconciled": True,
                "cancellation_requested": interrupted,
                "cancellation_status": "completed" if interrupted else "not_requested",
            },
        )

    def _interrupted_abandon_result(self, command: str, transaction: Transaction, plan) -> CommandResult:
        try:
            transaction.finish_abandon()
        except BaseException as exc:
            return self._abandon_cleanup_failure(command, exc, plan, interrupted=True)
        return CommandResult(
            command,
            status="cancelled",
            exit_code=130,
            rollback=RollbackResult(False, "not_needed", []),
            requested_targets=(list(plan.requested_targets) if plan is not None else []),
            affected_targets=(list(plan.affected_targets) if plan is not None else []),
            metadata={
                "abandon_durable": True,
                "cancellation_requested": True,
                "cancellation_status": "completed",
                "cancellation_message": "Operation cancelled before mutation; staged state was discarded.",
            },
        )

    def _abandon_cleanup_failure(
        self, command: str, exc: BaseException, plan, *, interrupted: bool
    ) -> CommandResult:
        return CommandResult(
            command,
            status="partial",
            exit_code=3,
            rollback=RollbackResult(False, "not_needed", []),
            requested_targets=(list(plan.requested_targets) if plan is not None else []),
            affected_targets=(list(plan.affected_targets) if plan is not None else []),
            recovery=RecoveryAction(
                "Precommit conflict cleanup is incomplete; the durable journal can finish it safely.",
                ["sn-module-gen", "doctor"],
            ),
            error=ErrorInfo("plan_conflict_cleanup_failed", "precommit", str(exc)),
            metadata={
                "abandon_durable": True,
                "cancellation_requested": interrupted,
                "cancellation_status": "partial" if interrupted else "not_requested",
            },
        )
    def _failure(
        self,
        command: str,
        transaction: Transaction,
        exc: Exception,
        *,
        baseline: SourceTreeInventory | None = None,
    ) -> CommandResult:
        rollback, residue = self._verified_rollback(
            transaction, baseline, reconcile=True
        )
        if isinstance(exc, GeneratorError):
            subprocess_error = None
            if exc.subprocess:
                subprocess_error = SubprocessError(
                    [str(item) for item in exc.subprocess.get("command", [])],
                    int(exc.subprocess.get("exit_code", 1)),
                    [
                        str(item)
                        for item in exc.subprocess.get("relevant_lines", [])
                    ],
                )
            return CommandResult(
                command,
                status="failure" if rollback.status == "completed" else "partial",
                exit_code=exc.exit_code if rollback.status == "completed" else 3,
                changes=residue,
                rollback=rollback,
                error=ErrorInfo(
                    exc.kind, exc.phase, exc.message, subprocess_error
                ),
            )
        return CommandResult(
            command,
            status="failure" if rollback.status == "completed" else "partial",
            exit_code=1 if rollback.status == "completed" else 3,
            changes=residue,
            rollback=rollback,
            error=ErrorInfo("internal", "internal", str(exc)),
        )

    def _verified_rollback(
        self,
        transaction: Transaction,
        baseline: SourceTreeInventory | None,
        *,
        reconcile: bool = False,
    ) -> tuple[RollbackResult, list[Change]]:
        if baseline is not None:
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
                residue = [
                    Change(
                        str(self.root / item.partition(":")[2]),
                        "update",
                        "rollback_residue",
                    )
                    for item in remaining
                    if item.partition(":")[1]
                ]
                return RollbackResult(True, "partial", []), residue
        rollback = transaction.rollback(
            reconcile=self._reconcile if reconcile else None
        )
        if baseline is None:
            return rollback, []
        try:
            remaining = source_tree_changes(
                baseline, source_tree_inventory(self.root)
            )
        except Exception as exc:
            remaining = (f"inventory_failed:{exc}",)
        if not remaining:
            return rollback, []
        verified = RollbackResult(True, "partial", rollback.restored)
        residue = []
        for item in remaining:
            action, separator, relative = item.partition(":")
            if not separator:
                relative = item
                action = "update"
            residue.append(
                Change(
                    str(self.root / relative),
                    {"created": "create", "deleted": "delete"}.get(
                        action, "update"
                    ),
                    "rollback_residue",
                )
            )
        return verified, residue

    def _reconcile(self, command: list[str]) -> bool:
        try:
            self._run(command, phase="dependency_recovery")
            return True
        except Exception:
            return False

    def _next_install(self, manager: Optional[str]) -> str:
        return f"Run `{manager or 'npm'} install` to refresh local dependencies."


def _failed(validation: ValidationResult) -> bool:
    return "failed" in {
        validation.structural,
        validation.integration,
        validation.dependency_link,
        validation.build,
    }
