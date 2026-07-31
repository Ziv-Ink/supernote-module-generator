"""Lifecycle command state machines and transactional application services."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import (
    ProjectConfig,
    backend_for_type,
    gradle_project_name,
    jsi_global_name,
    native_library_name,
)
from .errors import (
    ConfigurationError,
    GeneratorError,
    PartialFailure,
    SubprocessFailure,
    ValidationError,
)
from .generator import stage
from .integration import add_dependency, remove_dependency, unwire, wire
from .models import (
    Change,
    CommandResult,
    DependencyResult,
    ErrorInfo,
    ModuleInfo,
    RecoveryAction,
    RollbackResult,
    SubprocessError,
    ValidationResult,
    WarningInfo,
)
from .naming import (
    normalize_description,
    validate_android_namespace,
    validate_generated_paths,
    validate_javascript_name,
    validate_package_name,
    validate_package_version,
)
from .project import (
    ManagedModule,
    android_settings,
    dependency_link_path,
    dependency_value,
    ensure_within_plugin,
    find_module,
    git_status,
    managed_modules,
    manager_evidence,
    module_path,
    parent_mutation_targets,
    read_managed_module,
    read_parent_package,
)
from .rendering import ProgressReporter, Renderer
from .subprocesses import run_process
from .transaction import Transaction
from .verification import build_android, inspect_module


@dataclass(frozen=True)
class AddDecisions:
    package_name: str
    type: str
    description: str
    javascript_name: str
    android_namespace: str
    package_version: str
    install: bool
    package_manager: Optional[str]
    build: bool


@dataclass(frozen=True)
class UpdateDecisions:
    package_name: str
    package_manager: Optional[str]
    skip_install: bool
    build: bool


@dataclass(frozen=True)
class RemoveDecisions:
    package_names: List[str]
    all: bool
    package_manager: Optional[str]
    skip_install: bool


@dataclass(frozen=True)
class ValidateDecisions:
    package_names: List[str]
    all: bool
    build: bool


def _stream(renderer: Renderer, destination: str, content: str) -> None:
    target = renderer.stdout if destination == "stdout" else renderer.stderr
    target.write(content)
    target.flush()


def _relevant_lines(output: str) -> List[str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    preferred = [
        line
        for line in lines
        if any(word in line.lower() for word in ("error", "fatal", "failed", "exception", "eresolve"))
    ]
    unique: List[str] = []
    for line in preferred or lines[-8:]:
        if line not in unique:
            unique.append(line)
    return unique[:9]


class OperationService:
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

    def _run(
        self,
        command: List[str],
        *,
        cwd: Optional[Path] = None,
        timeout: int = 600,
        phase: str,
    ) -> Tuple[subprocess.CompletedProcess[str], int]:
        started = time.monotonic()
        try:
            if self.renderer.mode == "verbose" and self.run is subprocess.run:
                result = run_process(
                    command,
                    cwd=cwd or self.root,
                    timeout=timeout,
                    stream=lambda destination, content: _stream(
                        self.renderer,
                        destination,
                        content,
                    ),
                )
            else:
                result = self.run(
                    command,
                    cwd=cwd or self.root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SubprocessFailure(
                f"{command[0]} could not run.",
                kind=f"{phase}_failed",
                phase=phase,
                subprocess={
                    "command": command,
                    "exit_code": 1,
                    "relevant_lines": [str(exc)],
                },
            ) from exc
        duration = round((time.monotonic() - started) * 1000)
        if self.renderer.mode == "verbose" and self.run is not subprocess.run:
            if result.stdout:
                _stream(self.renderer, "stdout", result.stdout)
            if result.stderr:
                _stream(self.renderer, "stderr", result.stderr)
        if result.returncode:
            raise SubprocessFailure(
                f"{command[0]} could not complete the requested operation.",
                kind=f"{phase}_failed",
                phase=phase,
                subprocess={
                    "command": command,
                    "exit_code": result.returncode,
                    "relevant_lines": _relevant_lines(result.stdout + "\n" + result.stderr),
                },
            )
        return result, duration

    def _health_check_manager(self, manager: str) -> None:
        if shutil.which("node") is None:
            raise ConfigurationError("node is not available")
        if shutil.which(manager) is None:
            raise ConfigurationError(f"{manager} is not available")
        self._run(["node", "--version"], timeout=10, phase="preflight")
        self._run([manager, "--version"], timeout=10, phase="preflight")

    def _health_check_build(self) -> None:
        gradle = self.root / "android" / "gradlew"
        if not gradle.is_file():
            raise ConfigurationError("Android Gradle wrapper is not available")
        command = [str(gradle), "--version"] if os.access(gradle, os.X_OK) else ["sh", str(gradle), "--version"]
        self._run(command, cwd=self.root / "android", timeout=120, phase="preflight")

    def _reconcile(self, command: List[str]) -> bool:
        try:
            self._run(command, phase="dependency_recovery")
            return True
        except GeneratorError:
            return False

    def _manager_command(self, manager: str) -> List[str]:
        return [manager, "install"]

    def _run_dependency(self, manager: str, transaction: Transaction, phase: str) -> Tuple[List[str], int]:
        command = self._manager_command(manager)
        transaction.mark_external(command)
        _, duration = self._run(command, phase=phase)
        return command, duration

    def _build(self) -> None:
        success, error, _ = build_android(
            self.root,
            verbose=self.renderer.mode == "verbose",
            stream=lambda destination, content: _stream(self.renderer, destination, content),
        )
        if not success:
            assert error is not None
            raise SubprocessFailure(
                "Gradle could not build the requested module.",
                kind="build_failed",
                phase="build",
                subprocess=error.to_dict(),
            )

    def _module_config(self, decisions: AddDecisions) -> ProjectConfig:
        backend = backend_for_type(decisions.type)
        output = module_path(self.root, decisions.package_name)
        return ProjectConfig(
            output=output,
            npm_name=decisions.package_name,
            package_version=decisions.package_version,
            android_namespace=decisions.android_namespace,
            module_name=decisions.javascript_name,
            backend=backend,
            native_library_name=(
                native_library_name(decisions.package_name)
                if decisions.type in {"jni", "jsi"}
                else None
            ),
            jsi_global_name=(
                jsi_global_name(decisions.package_name)
                if decisions.type == "jsi"
                else None
            ),
            description=decisions.description,
            force=False,
            toolchain_versions=None,
        )

    def _require_writable(self, paths: Iterable[Path]) -> None:
        for path in paths:
            parent = path.parent
            while not parent.exists() and parent != self.root:
                parent = parent.parent
            if not os.access(parent, os.W_OK):
                raise GeneratorError(
                    f"The destination is not writable:\n{path}",
                    kind="filesystem_failed",
                    phase="prepare",
                )

    def _validate_add(self, decisions: AddDecisions) -> ProjectConfig:
        parent_targets = parent_mutation_targets(self.root)
        validate_package_name(decisions.package_name)
        validate_javascript_name(decisions.javascript_name)
        validate_android_namespace(decisions.android_namespace)
        validate_package_version(decisions.package_version)
        normalize_description(decisions.description)
        validate_generated_paths(self.root, decisions.package_name, decisions.android_namespace)
        destination = module_path(self.root, decisions.package_name)
        ensure_within_plugin(self.root, destination)
        if destination.exists():
            if (destination / ".supernote-module.json").is_file():
                raise ConfigurationError(f'module "{decisions.package_name}" already exists')
            raise ConfigurationError(
                f'"{destination}" exists but is not managed by Supernote Module Generator'
            )
        _, parent = read_parent_package(self.root)
        for section in ("dependencies", "devDependencies"):
            dependencies = parent.get(section, {})
            if isinstance(dependencies, dict) and decisions.package_name in dependencies:
                raise ConfigurationError(
                    f'dependency "{decisions.package_name}" already points to a different location'
                )
        wanted_library = (
            native_library_name(decisions.package_name)
            if decisions.type in {"jni", "jsi"}
            else None
        )
        wanted_jsi_global = (
            jsi_global_name(decisions.package_name)
            if decisions.type == "jsi"
            else None
        )
        wanted_gradle_project = gradle_project_name(decisions.package_name)
        settings_content = android_settings(self.root).read_text(encoding="utf-8")
        if (
            f":{wanted_gradle_project}:" in settings_content
            and f"// local-native-module: {decisions.package_name}" not in settings_content
        ):
            raise ConfigurationError(
                "Generated Gradle registration name collides with existing parent integration"
            )
        for module in managed_modules(self.root):
            if module.config.module_name == decisions.javascript_name:
                raise ConfigurationError(
                    f'JavaScript name "{decisions.javascript_name}" is already used by "{module.config.npm_name}"'
                )
            if module.config.android_namespace == decisions.android_namespace:
                raise ConfigurationError(
                    f'Android namespace "{decisions.android_namespace}" is already used by "{module.config.npm_name}"'
                )
            if wanted_library and module.config.native_library_name == wanted_library:
                raise ConfigurationError("Native library name collides with another managed module")
            if wanted_jsi_global and module.config.jsi_global_name == wanted_jsi_global:
                raise ConfigurationError("JSI registration name collides with another managed module")
            if gradle_project_name(module.config.npm_name) == wanted_gradle_project:
                raise ConfigurationError("Generated Gradle registration name collides with another managed module")
        self._require_writable([destination, *parent_targets])
        if decisions.install:
            if decisions.package_manager is None:
                raise ConfigurationError("package manager is ambiguous")
            self._health_check_manager(decisions.package_manager)
        if decisions.build:
            self._health_check_build()
        return self._module_config(decisions)

    def add(self, decisions: AddDecisions) -> CommandResult:
        config = self._validate_add(decisions)
        destination = config.output.resolve(strict=False)
        transaction = Transaction(self.root, "add", [config.npm_name])
        dependency: Optional[DependencyResult] = None
        try:
            if not destination.parent.exists():
                transaction.snapshot([destination.parent])
                destination.parent.mkdir(parents=True)
                transaction.mark_write()
            parent_paths = parent_mutation_targets(self.root)
            transaction.snapshot(parent_paths)
            transaction.set_phase("stage")
            with self.progress.phase("Preparing module", "Prepared module"):
                staged = stage(config)
                transaction.track_created(staged)
            transaction.set_phase("apply")
            with self.progress.phase("Generating module", "Generated module"):
                transaction.activate(staged, destination)
            with self.progress.phase("Updating plugin", "Updated plugin"):
                add_dependency(self.root, config.npm_name)
                if decisions.type == "native":
                    wire(self.root, config.npm_name)
                transaction.mark_write()
            if decisions.install:
                assert decisions.package_manager is not None
                with self.progress.phase("Installing dependency", "Installed dependency"):
                    command, duration = self._run_dependency(
                        decisions.package_manager, transaction, "install_dependency"
                    )
                dependency = DependencyResult(
                    True,
                    decisions.package_manager,
                    "installed",
                    False,
                    command,
                    duration,
                )
            else:
                dependency = DependencyResult(False, decisions.package_manager, "skipped", False, [], 0)
            transaction.set_phase("verify")
            with self.progress.phase("Verifying module", "Verified module"):
                module = read_managed_module(destination)
                validation = inspect_module(
                    self.root,
                    module,
                    dependency_requested=decisions.install,
                )
                if validation.structural == "failed" or validation.integration == "failed" or validation.dependency_link == "failed":
                    raise GeneratorError(
                        "The generated module did not satisfy its structural postconditions.",
                        kind="verification_failed",
                        phase="verify",
                    )
                if dependency is not None and decisions.install:
                    dependency = replace(dependency, verified=True)
            if decisions.build:
                transaction.set_phase("build")
                with self.progress.phase("Building Android", "Built Android"):
                    self._build()
                validation = replace(validation, build="passed")
            transaction.commit()
            changes = [
                Change(str(destination), "created", "module_generated"),
                Change(str(self.root / "package.json"), "updated", "parent"),
            ]
            if decisions.type == "native":
                changes.append(Change(str(android_settings(self.root)), "updated", "parent"))
            result = CommandResult(
                "add",
                module=module.info(),
                changes=changes,
                dependency=dependency,
                validation=validation,
                metadata={"built": decisions.build},
            )
            if not decisions.install:
                result.metadata["next_action"] = _next_install(self.root, decisions.package_manager)
            return result
        except KeyboardInterrupt:
            return self._cancel_mutation("add", transaction)
        except Exception as exc:
            return self._mutation_failure("add", transaction, exc)

    def _update_preflight(self, decisions: UpdateDecisions) -> Tuple[ManagedModule, bool]:
        parent_targets = parent_mutation_targets(self.root)
        module = find_module(self.root, decisions.package_name)
        self._require_writable([module.path, *parent_targets])
        for other in managed_modules(self.root):
            if other.config.npm_name == module.config.npm_name:
                continue
            if other.config.module_name == module.config.module_name:
                raise ConfigurationError(
                    f'JavaScript name "{module.config.module_name}" is already used by "{other.config.npm_name}"'
                )
            if other.config.android_namespace == module.config.android_namespace:
                raise ConfigurationError(
                    f'Android namespace "{module.config.android_namespace}" is already used by "{other.config.npm_name}"'
                )
            if (
                module.config.native_library_name
                and other.config.native_library_name == module.config.native_library_name
            ):
                raise ConfigurationError(
                    "Native library name collides with another managed module"
                )
            if (
                module.config.jsi_global_name
                and other.config.jsi_global_name == module.config.jsi_global_name
            ):
                raise ConfigurationError(
                    "JSI registration name collides with another managed module"
                )
            if gradle_project_name(other.config.npm_name) == gradle_project_name(module.config.npm_name):
                raise ConfigurationError(
                    "Generated Gradle registration name collides with another managed module"
                )
        _, parent = read_parent_package(self.root)
        dependencies = parent.get("dependencies", {})
        parent_link = dependencies.get(module.config.npm_name) if isinstance(dependencies, dict) else None
        link = dependency_link_path(self.root, module.config.npm_name)
        try:
            linked = link.exists() and link.resolve() == module.path.resolve()
        except OSError:
            linked = False
        refresh_required = parent_link != dependency_value(module.config.npm_name) or not linked
        if refresh_required and not decisions.skip_install:
            if decisions.package_manager is None:
                raise ConfigurationError("package manager is ambiguous")
            self._health_check_manager(decisions.package_manager)
        if decisions.build:
            self._health_check_build()
        return module, refresh_required

    def update(self, decisions: UpdateDecisions) -> CommandResult:
        module, refresh_required = self._update_preflight(decisions)
        transaction = Transaction(self.root, "update", [module.config.npm_name])
        dependency: DependencyResult
        git = git_status(self.root)
        warnings = _git_warning(git)
        try:
            transaction.snapshot(parent_mutation_targets(self.root))
            with self.progress.phase("Preparing update", "Prepared update"):
                config = replace(module.config, output=module.path, force=True)
            with self.progress.phase("Staging generated changes", "Staged generated changes"):
                staged = stage(config, preserve_api_from=module.path)
                transaction.track_created(staged)
            transaction.set_phase("apply")
            transaction.activate(staged, module.path)
            with self.progress.phase("Updating plugin", "Updated plugin"):
                add_dependency(self.root, module.config.npm_name)
                if module.type == "native":
                    wire(self.root, module.config.npm_name)
                transaction.mark_write()
            if refresh_required and not decisions.skip_install:
                assert decisions.package_manager is not None
                with self.progress.phase("Refreshing dependencies", "Refreshed dependencies"):
                    command, duration = self._run_dependency(
                        decisions.package_manager, transaction, "refresh_dependency"
                    )
                dependency = DependencyResult(True, decisions.package_manager, "refreshed", False, command, duration)
            elif refresh_required:
                dependency = DependencyResult(False, decisions.package_manager, "skipped", False, [], 0)
            else:
                dependency = DependencyResult(False, decisions.package_manager, "not_needed", True, [], 0)
            transaction.set_phase("verify")
            with self.progress.phase("Verifying module", "Verified module"):
                updated = read_managed_module(module.path)
                validation = inspect_module(
                    self.root,
                    updated,
                    dependency_requested=not decisions.skip_install,
                )
                if validation.structural == "failed" or validation.integration == "failed" or validation.dependency_link == "failed":
                    raise GeneratorError(
                        "The updated module did not satisfy its structural postconditions.",
                        kind="verification_failed",
                        phase="verify",
                    )
                if dependency.status == "refreshed":
                    dependency = replace(dependency, verified=True)
            if decisions.build:
                transaction.set_phase("build")
                with self.progress.phase("Building Android", "Built Android"):
                    self._build()
                validation = replace(validation, build="passed")
            transaction.commit()
            implementation = Path(updated.info().implementation_path)
            result = CommandResult(
                "update",
                module=updated.info(),
                changes=[
                    Change(str(updated.path), "updated", "module_generated"),
                    Change(str(implementation), "preserved", "module_implementation"),
                    Change(str(self.root / "package.json"), "updated", "parent"),
                ],
                dependency=dependency,
                validation=validation,
                warnings=warnings,
                metadata={"built": decisions.build, "git": git},
            )
            if dependency.status == "skipped":
                result.metadata["next_action"] = _next_install(self.root, decisions.package_manager)
            return result
        except KeyboardInterrupt:
            result = self._cancel_mutation("update", transaction)
            result.warnings.extend(warnings)
            return result
        except Exception as exc:
            result = self._mutation_failure("update", transaction, exc)
            result.warnings.extend(warnings)
            return result

    def remove(self, decisions: RemoveDecisions) -> CommandResult:
        parent_targets = parent_mutation_targets(self.root)
        modules = [find_module(self.root, name) for name in decisions.package_names]
        if not modules:
            return CommandResult("remove", metadata={"empty": True, "removed_count": 0})
        self._require_writable(
            [*(module.path for module in modules), *parent_targets]
        )
        if not decisions.skip_install:
            if decisions.package_manager is None:
                raise ConfigurationError("package manager is ambiguous")
            self._health_check_manager(decisions.package_manager)
        _, parent = read_parent_package(self.root)
        dependencies = parent.get("dependencies", {})
        settings_content = android_settings(self.root).read_text(encoding="utf-8")
        for module in modules:
            if not isinstance(dependencies, dict) or dependencies.get(module.config.npm_name) != dependency_value(module.config.npm_name):
                raise ConfigurationError(
                    f'package.json does not link "{module.config.npm_name}" to its generated module path'
                )
            if module.type == "native" and settings_content.count(f"// local-native-module: {module.config.npm_name}") != 1:
                raise ConfigurationError(
                    f'Android settings do not contain exactly one managed entry for "{module.config.npm_name}"'
                )
        git = git_status(self.root)
        warnings = _git_warning(git)
        transaction = Transaction(self.root, "remove", [module.config.npm_name for module in modules])
        try:
            transaction.snapshot(parent_mutation_targets(self.root))
            with self.progress.phase("Preparing removal", "Prepared removal"):
                pass
            transaction.set_phase("apply")
            with self.progress.phase("Detaching module", "Detached module"):
                for module in modules:
                    transaction.detach(module.path)
                    remove_dependency(self.root, module.config.npm_name)
                    if module.type == "native":
                        unwire(self.root, module.config.npm_name)
                transaction.mark_write()
            if not decisions.skip_install:
                assert decisions.package_manager is not None
                with self.progress.phase("Refreshing dependencies", "Refreshed dependencies"):
                    command, duration = self._run_dependency(
                        decisions.package_manager, transaction, "refresh_dependency"
                    )
                dependency = DependencyResult(True, decisions.package_manager, "refreshed", True, command, duration)
            else:
                dependency = DependencyResult(False, decisions.package_manager, "skipped", False, [], 0)
            transaction.set_phase("verify")
            with self.progress.phase("Verifying plugin", "Verified plugin"):
                _, parent = read_parent_package(self.root)
                dependencies = parent.get("dependencies", {})
                settings = android_settings(self.root).read_text(encoding="utf-8")
                for module in modules:
                    if isinstance(dependencies, dict) and module.config.npm_name in dependencies:
                        raise GeneratorError("Parent dependency removal could not be verified.", phase="verify")
                    if module.type == "native" and module.config.npm_name in settings:
                        raise GeneratorError("Gradle integration removal could not be verified.", phase="verify")
                    if module.path.exists():
                        raise GeneratorError("The module was not detached safely.", phase="verify")
            with self.progress.phase("Deleting module", "Deleted module"):
                transaction.commit()
            infos = [module.info() for module in modules]
            result = CommandResult(
                "remove",
                module=infos[0] if len(infos) == 1 else None,
                modules=infos if len(infos) > 1 else [],
                changes=[
                    *[Change(info.path, "removed", "module_implementation") for info in infos],
                    Change(str(self.root / "package.json"), "updated", "parent"),
                ],
                dependency=dependency,
                warnings=warnings,
                metadata={"removed_count": len(infos), "git": git},
            )
            if decisions.skip_install:
                result.metadata["next_action"] = _next_install(self.root, decisions.package_manager)
            return result
        except KeyboardInterrupt:
            result = self._cancel_mutation("remove", transaction)
            result.warnings.extend(warnings)
            return result
        except Exception as exc:
            result = self._mutation_failure("remove", transaction, exc)
            result.warnings.extend(warnings)
            return result

    def validate(self, decisions: ValidateDecisions) -> CommandResult:
        modules = [find_module(self.root, name) for name in decisions.package_names]
        if not modules:
            return CommandResult("validate", modules=[] if decisions.all else [], metadata={"empty": True})
        if decisions.build:
            self._health_check_build()
        results: List[Tuple[ManagedModule, ValidationResult]] = []
        phase = "Checking modules" if decisions.all else "Checking module"
        with self.progress.phase(phase, "Checked modules" if decisions.all else "Checked module"):
            for module in modules:
                results.append((module, inspect_module(self.root, module, dependency_requested=True)))
        build_error: Optional[SubprocessError] = None
        build_passed = False
        if decisions.build:
            with self.progress.phase(
                "Building Android",
                "Built Android",
                "Building Android failed",
            ) as build_phase:
                success, build_error, _ = build_android(
                    self.root,
                    verbose=self.renderer.mode == "verbose",
                    stream=lambda destination, content: _stream(self.renderer, destination, content),
                )
                build_passed = success
                if not success:
                    build_phase.fail()
            results = [
                (module, replace(result, build="passed" if build_passed else "failed"))
                for module, result in results
            ]
        failed = [
            (module, validation)
            for module, validation in results
            if "failed" in {
                validation.structural,
                validation.integration,
                validation.dependency_link,
                validation.build,
            }
        ]
        if failed:
            issues = [issue for _, validation in results for issue in validation.issues]
            if build_error is not None:
                issues.append({"kind": "build", "message": "Android build failed"})
            summary = ValidationResult(
                structural="failed" if any(item.structural == "failed" for _, item in results) else "passed",
                integration="failed" if any(item.integration == "failed" for _, item in results) else "passed",
                dependency_link="failed" if any(item.dependency_link == "failed" for _, item in results) else "passed",
                build=("failed" if decisions.build and not build_passed else "passed" if decisions.build else "not_requested"),
                issues=issues,
            )
            error = ErrorInfo(
                "validation_failed",
                "check",
                (
                    f"Validation failed for {len(failed)} of {len(results)} modules."
                    if decisions.all
                    else str(issues[0].get("message", "The module is invalid."))
                ),
                build_error,
            )
            return CommandResult(
                "validate",
                status="failure",
                exit_code=1,
                module=failed[0][0].info() if not decisions.all else None,
                modules=[replace(module.info(), validation=validation) for module, validation in results] if decisions.all else [],
                validation=summary,
                error=error,
                metadata={"phase_label": "Validation" if decisions.all else "Checking module", "next_action": _validation_next(failed[0][0], decisions.build)},
            )
        summary = ValidationResult(
            structural="passed",
            integration="passed",
            dependency_link="passed",
            build="passed" if decisions.build else "not_requested",
            issues=[],
        )
        return CommandResult(
            "validate",
            module=results[0][0].info() if not decisions.all else None,
            modules=[replace(module.info(), validation=validation) for module, validation in results] if decisions.all else [],
            validation=summary,
            metadata={"built": decisions.build},
        )

    def _cancel_mutation(self, command: str, transaction: Transaction) -> CommandResult:
        if not transaction.mutated:
            transaction.commit()
            return CommandResult(
                command,
                status="cancelled",
                exit_code=130,
                metadata={"cancellation_message": "Operation cancelled."},
            )
        rollback = transaction.rollback(reconcile=self._reconcile)
        if rollback.status == "completed":
            return CommandResult(
                command,
                status="cancelled",
                exit_code=130,
                rollback=rollback,
                metadata={"cancellation_message": "Operation cancelled. Previous state restored."},
            )
        recovery = _dependency_recovery(self.root, _external_manager(transaction))
        return CommandResult(
            command,
            status="partial",
            exit_code=3,
            rollback=rollback,
            recovery=recovery,
            error=ErrorInfo("cancellation_recovery_failed", "rollback", "Cancellation left a partial state."),
            metadata={"phase_label": "Rollback"},
        )

    def _mutation_failure(self, command: str, transaction: Transaction, exc: Exception) -> CommandResult:
        rollback = transaction.rollback(reconcile=self._reconcile)
        if isinstance(exc, GeneratorError):
            error = _error_info(
                exc,
                self.renderer.debug,
                transaction_id=transaction.identifier,
            )
            exit_code = 1 if exc.exit_code == 2 and transaction.mutated else exc.exit_code
        else:
            error = ErrorInfo(
                "internal",
                str(transaction.data.get("phase", "internal")),
                f"Supernote Module Generator could not complete {transaction.data.get('phase', 'the operation')}.",
                internal=(
                    {
                        "traceback": traceback.format_exc(),
                        "transaction_id": transaction.identifier,
                    }
                    if self.renderer.debug
                    else None
                ),
            )
            exit_code = 1
        if rollback.status != "completed":
            return CommandResult(
                command,
                status="partial",
                exit_code=3,
                rollback=rollback,
                recovery=_dependency_recovery(
                    self.root,
                    _external_manager(transaction),
                ),
                error=error,
                metadata={"phase_label": "Rollback"},
            )
        return CommandResult(
            command,
            status="failure",
            exit_code=exit_code,
            rollback=rollback,
            error=error,
            metadata={
                "phase_label": _phase_label(error.phase),
                "next_action": _failure_next(command, error),
            },
        )


def _error_info(
    exc: GeneratorError,
    debug: bool,
    *,
    transaction_id: Optional[str] = None,
) -> ErrorInfo:
    subprocess_error = None
    if exc.subprocess:
        subprocess_error = SubprocessError(
            [str(item) for item in exc.subprocess.get("command", [])],
            int(exc.subprocess.get("exit_code", 1)),
            [str(item) for item in exc.subprocess.get("relevant_lines", [])],
        )
    internal = None
    if debug and exc.kind == "internal":
        internal = {"traceback": traceback.format_exc()}
        if transaction_id is not None:
            internal["transaction_id"] = transaction_id
    return ErrorInfo(exc.kind, exc.phase, exc.message, subprocess_error, internal)


def _phase_label(phase: str) -> str:
    return {
        "prepare": "Preparing module",
        "stage": "Generating module",
        "apply": "Updating plugin",
        "install_dependency": "Installing dependency",
        "refresh_dependency": "Refreshing dependencies",
        "verify": "Verifying module",
        "build": "Building Android",
        "rollback": "Rollback",
    }.get(phase, phase.replace("_", " ").capitalize())


def _failure_next(command: str, error: ErrorInfo) -> str:
    if error.phase in {"install_dependency", "refresh_dependency"}:
        return f"Resolve the dependency error, then rerun {command.capitalize()}."
    if error.phase == "build":
        return "Fix the reported source error, then rerun with --build."
    if error.phase == "verify":
        return "Rerun with --verbose to inspect the failed postcondition."
    return f"Correct the reported problem and rerun {command.capitalize()}."


def _external_manager(transaction: Transaction) -> Optional[str]:
    command = transaction.data.get("external_command")
    if isinstance(command, list) and command:
        manager = str(command[0])
        return manager if manager in {"npm", "yarn"} else None
    return None


def _dependency_recovery(
    root: Path,
    manager: Optional[str] = None,
) -> RecoveryAction:
    evidence = manager_evidence(root)
    selected = manager or evidence.sole or "npm"
    return RecoveryAction(
        "Plugin files were restored, but dependencies may be inconsistent.",
        [selected, "install"],
    )


def _next_install(root: Path, explicit: Optional[str]) -> str:
    manager = explicit or manager_evidence(root).sole
    return f"Run {manager} install" if manager else "Choose npm or Yarn, then install dependencies"


def _git_warning(status: str) -> List[WarningInfo]:
    if "uncommitted changes" not in status:
        return []
    count = status.split(" ", 1)[0]
    return [
        WarningInfo(
            "git_dirty",
            f"The plugin has {count} uncommitted changes.",
            "preflight",
            "The operation can continue, but those changes may affect manual recovery.",
        )
    ]


def _validation_next(module: ManagedModule, build: bool) -> str:
    return (
        f"Run supernote-module validate {module.config.npm_name} --build --verbose"
        if build
        else f"Run supernote-module update {module.config.npm_name}"
    )
