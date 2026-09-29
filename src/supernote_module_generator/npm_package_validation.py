"""Read-only validation of author-project npm package activation."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from .npm_distribution import PACKAGE_MANIFEST_PATH
from .project_model import ProjectModel
from .validation import ValidationIssue


def validate_author_package_inputs(
    root: Path,
    project: ProjectModel,
) -> tuple[ValidationIssue, ...]:
    """Validate local packages against their explicitly installed direct views."""

    if not project.features:
        return ()
    dependencies = dict(project.dependencies)
    declared_local = [
        feature.identity.npm_name
        for feature in project.features
        if feature.identity.npm_name in dependencies
    ]
    if not declared_local:
        return ()

    node = shutil.which("node")
    if node is None:
        return (
            ValidationIssue(
                "SNMG_NODE_UNAVAILABLE",
                "error",
                "dependency",
                "Node.js is required to validate installed Supernote packages",
            ),
        )
    helper = Path(__file__).with_name("node") / "resolve-packages.js"
    result = subprocess.run(
        [node, str(helper), str(root)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        code = _diagnostic_code(message)
        return (
            ValidationIssue(
                code,
                "error",
                "dependency",
                message or "Supernote package discovery failed",
                suggested_command="Refresh declared dependencies with npm or Yarn, then rerun sn-module-gen validate.",
            ),
        )
    try:
        discovered = json.loads(result.stdout)
        installed = {
            item["name"]: item for item in discovered.get("modules", [])
        }
    except (KeyError, TypeError, ValueError) as exc:
        return (
            ValidationIssue(
                "SNMG_PACKAGE_DISCOVERY_INVALID",
                "error",
                "dependency",
                f"Supernote package discovery returned invalid output: {exc}",
            ),
        )

    issues = []
    for feature in project.features:
        name = feature.identity.npm_name
        if name not in declared_local:
            continue
        resolved = installed.get(name)
        if resolved is None:
            issues.append(
                ValidationIssue(
                    "SNMG_MODULE_DEPENDENCY_MISSING",
                    "error",
                    "dependency",
                    f"{name} is declared but its installed package is not an active Supernote module",
                    suggested_command="Refresh declared dependencies with npm or Yarn.",
                )
            )
            continue
        installed_root = Path(resolved["root"])
        try:
            same_physical_root = installed_root.resolve() == feature.root.resolve()
        except OSError:
            same_physical_root = False
        if not same_physical_root and not _declares_local_relationship(
            self_root=root,
            feature_root=feature.root,
            specification=dependencies[name],
        ):
            issues.append(
                ValidationIssue(
                    "SNMG_MODULE_LOCAL_RELATIONSHIP_INVALID",
                    "error",
                    "dependency",
                    f"{name} is copied from local_modules but its dependency specification does not identify that local package",
                    suggested_command=f"Install ./local_modules/{name} explicitly with npm or Yarn.",
                )
            )
            continue
        local_manifest = feature.root / PACKAGE_MANIFEST_PATH
        installed_manifest = Path(resolved["manifest"])
        try:
            local_bytes = local_manifest.read_bytes()
            installed_bytes = installed_manifest.read_bytes()
        except OSError as exc:
            issues.append(
                ValidationIssue(
                    "SNMG_MODULE_PAYLOAD_MISSING",
                    "error",
                    "dependency",
                    f"{name} distribution manifest cannot be compared: {exc}",
                    suggested_command="Run sn-module-gen update, then refresh the dependency.",
                )
            )
            continue
        if local_bytes != installed_bytes:
            issues.append(
                ValidationIssue(
                    "SNMG_MODULE_INSTALL_STALE",
                    "error",
                    "dependency",
                    f"{name} installed package is stale relative to local_modules/{name}",
                    suggested_command=f"Refresh {name} with npm or Yarn after sn-module-gen update.",
                )
            )
    return tuple(issues)


def package_issue_warnings(issues: Iterable[ValidationIssue]) -> tuple[str, ...]:
    return tuple(issue.message for issue in issues)


def _diagnostic_code(message: str) -> str:
    marker = "Supernote package discovery ["
    if marker in message:
        tail = message.split(marker, 1)[1]
        code = tail.split("]", 1)[0]
        if code:
            return code
    return "SNMG_PACKAGE_DISCOVERY_FAILED"


def _declares_local_relationship(
    *,
    self_root: Path,
    feature_root: Path,
    specification: str,
) -> bool:
    if specification.startswith("workspace:"):
        return True
    for prefix in ("file:", "link:"):
        if not specification.startswith(prefix):
            continue
        raw = specification.removeprefix(prefix)
        target = Path(raw)
        if target.is_absolute() or raw.lower().endswith((".tgz", ".tar.gz")):
            return False
        try:
            return (self_root / target).resolve() == feature_root.resolve()
        except OSError:
            return False
    return False
