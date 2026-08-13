"""Transactional public CLI operations for V2 logical features."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Callable, Iterable, Optional

from .errors import ConfigurationError, GeneratorError, SubprocessFailure
from .feature_generator import FeatureConfig
from .feature_operations import FeatureOperationService, FeatureRecord
from .feature_workflows import (
    FeatureAddDecisions,
    FeatureRemoveDecisions,
    FeatureUpdateDecisions,
    FeatureValidateDecisions,
)
from .integration import add_dependency, remove_dependency
from .models import (
    Change,
    CommandResult,
    DependencyResult,
    ErrorInfo,
    RollbackResult,
    SubprocessError,
    ValidationResult,
)
from .naming import (
    normalize_description,
    validate_android_namespace,
    validate_javascript_name,
    validate_package_name,
    validate_package_version,
)
from .plugin_build_integration import integration_files
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
        destination = self.root / "local_modules" / decisions.package_name
        ensure_within_plugin(self.root, destination)
        config = FeatureConfig(
            output=destination,
            npm_name=decisions.package_name,
            package_version=decisions.package_version,
            android_namespace=decisions.android_namespace,
            public_name=decisions.public_name,
            description=decisions.description,
            starters=decisions.starters,
        )
        transaction = Transaction(self.root, "add", [decisions.package_name])
        dependency: DependencyResult | None = None
        try:
            self._snapshot_operation(transaction, [destination])
            transaction.set_phase("apply")
            with self.progress.phase("Generating feature", "Generated feature"):
                created = self.features.add(config)
                add_dependency(self.root, decisions.package_name)
                transaction.mark_write()
            dependency = self._dependency_result(
                transaction,
                requested=decisions.install,
                manager=decisions.package_manager,
                action="install_dependency",
            )
            validation = self._validate_records(
                [self.features.find_record(decisions.package_name)],
                dependency_requested=decisions.install,
            )
            if _failed(validation):
                raise GeneratorError(
                    "The generated feature did not satisfy its structural postconditions.",
                    kind="verification_failed",
                    phase="verify",
                )
            if decisions.build:
                self._build()
                validation = replace(validation, build="passed")
            transaction.commit()
            record = self.features.find_record(decisions.package_name)
            result = CommandResult(
                "add",
                module=record.info(),
                changes=[
                    Change(str(created), "created", "feature_generated"),
                    Change(str(self.root / RUNTIME_RELATIVE_ROOT), "updated", "plugin_runtime"),
                    Change(str(self.root / "package.json"), "updated", "parent"),
                ],
                dependency=dependency,
                validation=validation,
                metadata={"built": decisions.build},
            )
            if not decisions.install:
                result.metadata["next_action"] = self._next_install(decisions.package_manager)
            return result
        except KeyboardInterrupt:
            return self._cancel("add", transaction)
        except Exception as exc:
            return self._failure("add", transaction, exc)

    def update(self, decisions: FeatureUpdateDecisions) -> CommandResult:
        record = self.features.find_record(decisions.package_name)
        refresh = self._refresh_required(record)
        if refresh and not decisions.skip_install:
            self._health_check_manager(decisions.package_manager)
        if decisions.build:
            self._health_check_build()
        transaction = Transaction(self.root, "update", [decisions.package_name])
        try:
            self._snapshot_operation(transaction, [record.path])
            transaction.set_phase("apply")
            with self.progress.phase("Updating feature", "Updated feature"):
                self.features.update(decisions.package_name)
                add_dependency(self.root, decisions.package_name)
                transaction.mark_write()
            dependency = self._dependency_result(
                transaction,
                requested=refresh and not decisions.skip_install,
                manager=decisions.package_manager,
                action="refresh_dependency",
                skipped_status="skipped" if refresh else "not_needed",
            )
            updated = self.features.find_record(decisions.package_name)
            validation = self._validate_records(
                [updated], dependency_requested=not decisions.skip_install
            )
            if _failed(validation):
                raise GeneratorError(
                    "The updated feature did not satisfy its structural postconditions.",
                    kind="verification_failed",
                    phase="verify",
                )
            if decisions.build:
                self._build()
                validation = replace(validation, build="passed")
            transaction.commit()
            result = CommandResult(
                "update",
                module=updated.info(),
                changes=[
                    Change(str(updated.path), "updated", "feature_generated"),
                    Change(str(updated.path / "android/src/main"), "preserved", "feature_implementation"),
                    Change(str(self.root / RUNTIME_RELATIVE_ROOT), "updated", "plugin_runtime"),
                ],
                dependency=dependency,
                validation=validation,
                metadata={"built": decisions.build},
            )
            if dependency.status == "skipped":
                result.metadata["next_action"] = self._next_install(decisions.package_manager)
            return result
        except KeyboardInterrupt:
            return self._cancel("update", transaction)
        except Exception as exc:
            return self._failure("update", transaction, exc)

    def remove(self, decisions: FeatureRemoveDecisions) -> CommandResult:
        records = [self.features.find_record(name) for name in decisions.package_names]
        if not records:
            return CommandResult("remove", metadata={"empty": True, "removed_count": 0})
        if not decisions.skip_install:
            self._health_check_manager(decisions.package_manager)
        transaction = Transaction(
            self.root, "remove", [record.manifest.npm_name for record in records]
        )
        try:
            self._snapshot_operation(transaction, [record.path for record in records])
            transaction.set_phase("apply")
            with self.progress.phase("Removing features", "Removed features"):
                for record in records:
                    self.features.remove(record.manifest.npm_name)
                    remove_dependency(self.root, record.manifest.npm_name)
                transaction.mark_write()
            dependency = self._dependency_result(
                transaction,
                requested=not decisions.skip_install,
                manager=decisions.package_manager,
                action="refresh_dependency",
            )
            issues = self.features.verify_generated_state()
            if issues:
                raise GeneratorError(
                    issues[0], kind="verification_failed", phase="verify"
                )
            removed_build_paths: list[Path] = []
            if decisions.delete_build_files:
                for path in self._build_output_paths():
                    ensure_within_plugin(self.root, path)
                    if path.exists():
                        transaction.detach(path)
                        removed_build_paths.append(path)
            transaction.commit()
            infos = [record.info() for record in records]
            result = CommandResult(
                "remove",
                module=infos[0] if len(infos) == 1 else None,
                modules=infos if len(infos) > 1 else [],
                changes=[
                    *[
                        Change(info.path, "removed", "feature_implementation")
                        for info in infos
                    ],
                    Change(str(self.root / RUNTIME_RELATIVE_ROOT), "updated", "plugin_runtime"),
                    Change(str(self.root / "package.json"), "updated", "parent"),
                    *[
                        Change(str(path), "removed", "generated_build_output")
                        for path in removed_build_paths
                    ],
                ],
                dependency=dependency,
                metadata={
                    "removed_count": len(infos),
                    "build_files_deleted": decisions.delete_build_files,
                },
            )
            if decisions.skip_install:
                result.metadata["next_action"] = self._next_install(decisions.package_manager)
            return result
        except KeyboardInterrupt:
            return self._cancel("remove", transaction)
        except Exception as exc:
            return self._failure("remove", transaction, exc)

    def _build_output_paths(self) -> tuple[Path, ...]:
        return (
            self.root / "build",
            self.root / "android/build",
            self.root / "android/app/build",
        )

    def validate(self, decisions: FeatureValidateDecisions) -> CommandResult:
        records = [self.features.find_record(name) for name in decisions.package_names]
        if not records:
            return CommandResult("validate", metadata={"empty": True})
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
        validate_package_name(decisions.package_name)
        validate_javascript_name(decisions.public_name)
        validate_android_namespace(decisions.android_namespace)
        validate_package_version(decisions.package_version)
        normalize_description(decisions.description)
        destination = self.root / "local_modules" / decisions.package_name
        if destination.exists():
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
        if decisions.build:
            self._health_check_build()

    def _snapshot_operation(
        self, transaction: Transaction, feature_paths: Iterable[Path]
    ) -> None:
        settings, app_build = integration_files(self.root)
        paths = [
            *parent_mutation_targets(self.root),
            settings,
            app_build,
            self.root / RUNTIME_RELATIVE_ROOT,
            *feature_paths,
        ]
        transaction.snapshot(paths)

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
            if self.renderer.mode == "verbose" and self.run is subprocess.run:
                result = run_process(
                    command, cwd=self.root, timeout=600, stream=self._stream
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
        gradle = self.root / "android/gradlew"
        if not gradle.is_file():
            raise ConfigurationError("Android Gradle wrapper is not available")

    def _build(self) -> None:
        success, error, _ = build_android(
            self.root,
            verbose=self.renderer.mode == "verbose",
            stream=self._stream,
        )
        if not success:
            assert error is not None
            raise SubprocessFailure(
                "Gradle could not build the V2 plugin runtime.",
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

    def _cancel(self, command: str, transaction: Transaction) -> CommandResult:
        rollback = transaction.rollback(reconcile=self._reconcile)
        return CommandResult(
            command,
            status="cancelled" if rollback.status == "completed" else "partial",
            exit_code=130 if rollback.status == "completed" else 3,
            rollback=rollback,
            metadata={"cancellation_message": "Operation cancelled. Previous state restored."},
        )

    def _failure(
        self, command: str, transaction: Transaction, exc: Exception
    ) -> CommandResult:
        rollback = transaction.rollback(reconcile=self._reconcile)
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
                rollback=rollback,
                error=ErrorInfo(
                    exc.kind, exc.phase, exc.message, subprocess_error
                ),
            )
        return CommandResult(
            command,
            status="failure" if rollback.status == "completed" else "partial",
            exit_code=1 if rollback.status == "completed" else 3,
            rollback=rollback,
            error=ErrorInfo("internal", "internal", str(exc)),
        )

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
