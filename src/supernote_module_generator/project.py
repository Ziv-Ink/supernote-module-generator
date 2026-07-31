"""Read-only Supernote plugin and managed-module repository."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import (
    METADATA_FILE,
    ProjectConfig,
    TYPE_LABELS,
    public_type,
)
from .errors import ConfigurationError
from .models import ModuleInfo
from .validation import package_path, validate_config

LOCAL_MODULES_DIR = "local_modules"


def ensure_within_plugin(root: Path, target: Path) -> Path:
    """Return the canonical target or reject a symlink/path escape."""
    canonical_root = root.resolve()
    canonical_target = target.resolve(strict=False)
    try:
        canonical_target.relative_to(canonical_root)
    except ValueError as exc:
        raise ConfigurationError(
            f"target resolves outside the Supernote plugin:\n\n{canonical_target}"
        ) from exc
    return canonical_target


def ensure_tree_within_plugin(root: Path, tree: Path) -> None:
    ensure_within_plugin(root, tree)
    for target in tree.rglob("*"):
        ensure_within_plugin(root, target)


def resolve_plugin_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    markers = (
        (root / "PluginConfig.json").is_file(),
        (root / "package.json").is_file(),
        (root / "android").is_dir(),
        any((root / "android" / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")),
    )
    if not all(markers):
        raise ConfigurationError(f"not a Supernote plugin: {root}")
    for marker in (
        root / "PluginConfig.json",
        root / "package.json",
        root / "android",
        android_settings(root),
    ):
        ensure_within_plugin(root, marker)
    return root


def parent_mutation_targets(root: Path) -> List[Path]:
    targets = [
        root / "package.json",
        root / "package-lock.json",
        root / "yarn.lock",
        android_settings(root),
    ]
    for target in targets:
        ensure_within_plugin(root, target)
    return targets


def android_settings(root: Path) -> Path:
    for name in ("settings.gradle", "settings.gradle.kts"):
        path = root / "android" / name
        if path.is_file():
            return path
    raise ConfigurationError(f"not a Supernote plugin: {root}")


def read_parent_package(root: Path) -> Tuple[Path, Dict[str, object]]:
    path = root / "package.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"package.json could not be read\n\n{path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigurationError("package.json could not be read\n\npackage.json must contain an object")
    return path, value


def _project_config(data: Dict[str, object], output: Path) -> ProjectConfig:
    accepted = {field.name for field in fields(ProjectConfig)}
    values = {key: value for key, value in data.items() if key in accepted}
    values.update({"output": output, "force": True})
    try:
        config = ProjectConfig(**values)  # type: ignore[arg-type]
        validate_config(config)
        return config
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"module metadata for \"{output.name}\" is invalid\n\n{exc}") from exc


@dataclass(frozen=True)
class ManagedModule:
    path: Path
    config: ProjectConfig

    @property
    def type(self) -> str:
        return public_type(self.config.backend)

    def info(self) -> ModuleInfo:
        implementation = (
            self.path
            / "android"
            / "src"
            / "main"
            / ("java" if self.type == "native" else "cpp")
        )
        if self.type == "native":
            implementation /= package_path(self.config.android_namespace)
        return ModuleInfo(
            package_name=self.config.npm_name,
            javascript_name=self.config.module_name,
            type=self.type,
            type_label=TYPE_LABELS[self.type],
            path=str(self.path.resolve()),
            implementation_path=str(implementation.resolve(strict=False)),
            android_namespace=self.config.android_namespace,
            package_version=self.config.package_version,
        )


def read_managed_module(path: Path) -> ManagedModule:
    metadata = path / METADATA_FILE
    if not metadata.is_file():
        raise ConfigurationError(
            f'"{path}" exists but is not managed by Supernote Module Generator'
        )
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f'module metadata for "{path.name}" is invalid\n\n{metadata}: {exc}'
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            f'module metadata for "{path.name}" is invalid\n\nMetadata must contain an object.'
        )
    config = _project_config(data, path.resolve())
    if config.npm_name != path.name and "/" not in config.npm_name:
        raise ConfigurationError(
            f'module metadata for "{path.name}" is invalid\n\nPackage name does not match its module directory.'
        )
    return ManagedModule(path.resolve(), config)


def module_path(root: Path, package_name: str) -> Path:
    return root / LOCAL_MODULES_DIR / package_name


def managed_modules(root: Path) -> List[ManagedModule]:
    modules_root = root / LOCAL_MODULES_DIR
    if not modules_root.is_dir():
        return []
    ensure_within_plugin(root, modules_root)
    found: Dict[str, ManagedModule] = {}
    for metadata in sorted(modules_root.rglob(METADATA_FILE)):
        ensure_within_plugin(root, metadata)
        module = read_managed_module(metadata.parent)
        ensure_tree_within_plugin(root, module.path)
        if module.config.npm_name in found:
            raise ConfigurationError(
                f'module metadata for "{module.config.npm_name}" is invalid\n\nThe package name occurs more than once.'
            )
        found[module.config.npm_name] = module
    return [found[name] for name in sorted(found)]


def find_module(root: Path, package_name: str) -> ManagedModule:
    for module in managed_modules(root):
        if module.config.npm_name == package_name:
            return module
    raise ConfigurationError(f'module "{package_name}" was not found')


@dataclass(frozen=True)
class ManagerEvidence:
    npm_lock: bool
    yarn_lock: bool

    @property
    def sole(self) -> Optional[str]:
        if self.npm_lock and not self.yarn_lock:
            return "npm"
        if self.yarn_lock and not self.npm_lock:
            return "yarn"
        return None

    @property
    def conflicting(self) -> bool:
        return self.npm_lock and self.yarn_lock


def manager_evidence(root: Path) -> ManagerEvidence:
    return ManagerEvidence(
        npm_lock=(root / "package-lock.json").is_file(),
        yarn_lock=(root / "yarn.lock").is_file(),
    )


def git_status(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "status unavailable"
    if result.returncode:
        combined = result.stdout + result.stderr
        return "not a Git repository" if "not a git repository" in combined.lower() else "status unavailable"
    count = len([line for line in result.stdout.splitlines() if line])
    return "clean" if count == 0 else f"{count} uncommitted changes"


def dependency_value(package_name: str) -> str:
    return f"file:./{LOCAL_MODULES_DIR}/{package_name}"


def dependency_link_path(root: Path, package_name: str) -> Path:
    return root / "node_modules" / Path(*package_name.split("/"))
