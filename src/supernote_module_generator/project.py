"""Read-only Supernote plugin and managed-module repository."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import ConfigurationError, FilesystemError
from .filesystem import read_regular_bytes_no_follow

LOCAL_MODULES_DIR = "local_modules"
TEMPLATE_BUILD_SCRIPTS = (
    Path("scripts/buildPlugin.sh"),
    Path("scripts/buildPlugin.ps1"),
)


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


def template_build_script(root: Path) -> Optional[Path]:
    """Return the official template's pre-build identity marker, if present."""
    return next(
        (root / relative for relative in TEMPLATE_BUILD_SCRIPTS if (root / relative).is_file()),
        None,
    )


def resolve_plugin_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    manifest = root / "PluginConfig.json"
    prebuild_marker = template_build_script(root)
    markers = (
        manifest.is_file() or prebuild_marker is not None,
        (root / "package.json").is_file(),
        (root / "android").is_dir(),
        any((root / "android" / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")),
    )
    if not all(markers):
        raise ConfigurationError(f"not a Supernote plugin: {root}")
    identity_marker = manifest if manifest.is_file() else prebuild_marker
    assert identity_marker is not None
    for marker in (
        identity_marker,
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
        content, _metadata = read_regular_bytes_no_follow(path)
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, FilesystemError) as exc:
        raise ConfigurationError(
            f"package.json could not be read\n\n{path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigurationError("package.json could not be read\n\npackage.json must contain an object")
    return path, value


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
            encoding="utf-8",
            errors="replace",
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
