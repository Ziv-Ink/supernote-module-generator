"""Structural, integration, dependency-link, and build postconditions."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import binding_codegen
from .config import METADATA_FILE, native_class_prefix
from .integration import marker
from .models import SubprocessError, ValidationResult
from .platform_tools import gradle_wrapper_command, gradle_wrapper_path
from .project import (
    ManagedModule,
    android_settings,
    dependency_link_path,
    dependency_value,
    read_parent_package,
)
from .subprocesses import run_process
from .validation import package_path

TEMPLATE_NAMES = (
    "NPM_NAME|NPM_NAME_RAW|VERSION|DESCRIPTION|NAMESPACE|PACKAGE_PATH|"
    "ANNOTATION_FQCN|LEGACY_ANNOTATION_FQCN|MODULE_NAME|MODULE_NAME_RAW|"
    "BACKEND|CLASS_PREFIX|GRADLE_MODULE_NAME|MIN_SDK|NATIVE_LIBRARY_NAME|"
    "JSI_GLOBAL_NAME|LOG_TAG|JSI_FIND_PACKAGE|JSI_LINK_TARGET|BACKEND_NOTE|"
    "GENERATOR_VERSION"
)
TEMPLATE_TOKEN = re.compile(
    rf"(?:\{{\{{[^}}\n]+\}}\}}|\$\{{(?:{TEMPLATE_NAMES})\}}|\$(?:{TEMPLATE_NAMES}))"
)


def expected_generated_files(module: ManagedModule) -> List[str]:
    config = module.config
    namespace = package_path(config.android_namespace)
    prefix = native_class_prefix(config.npm_name)
    common = [
        METADATA_FILE,
        "package.json",
        "index.js",
        "index.d.ts",
        "README.md",
        ".gitignore",
        "react-native.config.js",
        "android/build.gradle.kts",
        "android/src/main/AndroidManifest.xml",
    ]
    if module.type == "native":
        return common + [
            f"android/autolink/{prefix}NativeModulePackage.kt",
            f"android/.native-module/annotation/src/main/java/{namespace}/nativemodule/annotation/SupernotePluginExport.java",
            "android/.native-module/annotation/build.gradle.kts",
            "android/.native-module/processor/build.gradle.kts",
            "android/.native-module/processor/src/main/kotlin/localmodule/processor/SupernoteExportProcessor.kt",
            "android/.native-module/processor/src/main/resources/META-INF/services/com.google.devtools.ksp.processing.SymbolProcessorProvider",
        ]
    generated_package = (
        f"android/src/main/java/{namespace}/generated/{prefix}NativeModulePackage.kt"
        if module.type == "jni"
        else f"android/src/main/java/{namespace}/generated/{prefix}JsiModule.kt"
    )
    return common + [
        "android/.supernote-module/CMakeLists.txt",
        "android/.supernote-module/codegen.py",
        "android/.supernote-module/codegen-config.json",
        "android/.supernote-module/supernote_codegen/__init__.py",
        "android/.supernote-module/supernote_codegen/cpp_projection.py",
        "android/.supernote-module/supernote_codegen/lowering.py",
        "android/.supernote-module/supernote_codegen/semantic.py",
        "android/.supernote-module/supernote_codegen/source_models.py",
        generated_package,
    ]


def _text_files(root: Path) -> List[Path]:
    result: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in {"build", ".cxx"} for part in path.parts):
            continue
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        result.append(path)
    return result


def inspect_module(
    root: Path,
    module: ManagedModule,
    *,
    dependency_requested: bool = True,
    build_state: str = "not_requested",
) -> ValidationResult:
    issues: List[Dict[str, object]] = []
    try:
        ownership = json.loads((module.path / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        ownership = {}
        issues.append(
            {
                "kind": "ownership_metadata",
                "path": METADATA_FILE,
                "message": f"Ownership metadata could not be read: {exc}",
            }
        )
    if isinstance(ownership, dict):
        owned_files = ownership.get("generated_files")
        if not isinstance(owned_files, list) or not all(isinstance(item, str) for item in owned_files):
            issues.append(
                {
                    "kind": "ownership_metadata",
                    "path": METADATA_FILE,
                    "message": "Ownership metadata does not list generated files.",
                }
            )
        else:
            for relative in owned_files:
                if not (module.path / relative).is_file():
                    issues.append(
                        {
                            "kind": "missing_generated_file",
                            "path": relative,
                            "message": f"Missing generated file: {relative}",
                        }
                    )
        if ownership.get("type") != module.type:
            issues.append(
                {
                    "kind": "ownership_metadata",
                    "path": METADATA_FILE,
                    "message": "Module type in ownership metadata is invalid.",
                }
            )
    missing = [
        relative
        for relative in expected_generated_files(module)
        if not (module.path / relative).is_file()
    ]
    for relative in missing:
        issues.append({"kind": "missing_generated_file", "path": relative, "message": f"Missing generated file: {relative}"})
    for path in _text_files(module.path):
        match = TEMPLATE_TOKEN.search(path.read_text(encoding="utf-8"))
        if match:
            relative = path.relative_to(module.path).as_posix()
            issues.append(
                {
                    "kind": "unresolved_template",
                    "path": relative,
                    "message": f"Unresolved template value in {relative}: {match.group(0)}",
                }
            )
    package_ok = True
    try:
        package = json.loads((module.path / "package.json").read_text(encoding="utf-8"))
        package_ok = bool(
            isinstance(package, dict)
            and package.get("name") == module.config.npm_name
            and package.get("version") == module.config.package_version
            and package.get("main") == "index.js"
            and package.get("types") == "index.d.ts"
        )
    except (OSError, ValueError):
        package_ok = False
    if not package_ok:
        issues.append({"kind": "package_metadata", "path": "package.json", "message": "Generated package metadata does not match module metadata."})
    if module.type in {"jni", "jsi"}:
        try:
            binding_codegen.generate(module.path, check=True)
        except binding_codegen.CodegenError as exc:
            issues.append({"kind": "generated_contract", "path": "android/.supernote-module", "message": str(exc)})

    integration_issues: List[Dict[str, object]] = []
    try:
        _, parent = read_parent_package(root)
        dependencies = parent.get("dependencies", {})
        actual = dependencies.get(module.config.npm_name) if isinstance(dependencies, dict) else None
        if actual != dependency_value(module.config.npm_name):
            integration_issues.append(
                {
                    "kind": "parent_dependency",
                    "path": str(root / "package.json"),
                    "message": f'package.json does not link "{module.config.npm_name}" to its generated module path.',
                }
            )
        if module.type == "native":
            settings = android_settings(root).read_text(encoding="utf-8")
            if settings.count(marker(module.config.npm_name)) != 1:
                integration_issues.append(
                    {
                        "kind": "gradle_integration",
                        "path": str(android_settings(root)),
                        "message": "Parent Android settings must contain exactly one generated module entry.",
                    }
                )
    except Exception as exc:
        integration_issues.append({"kind": "parent_integration", "path": str(root), "message": str(exc)})
    issues.extend(integration_issues)

    dependency_state = "skipped"
    if dependency_requested:
        link = dependency_link_path(root, module.config.npm_name)
        try:
            linked = link.exists() and link.resolve() == module.path.resolve()
        except OSError:
            linked = False
        dependency_state = "passed" if linked else "failed"
        if not linked:
            issues.append(
                {
                    "kind": "dependency_link",
                    "path": str(link),
                    "message": f'"{module.config.npm_name}" is not linked from node_modules.',
                }
            )

    structural_issues = [
        issue for issue in issues if issue.get("kind") not in {"parent_dependency", "gradle_integration", "parent_integration", "dependency_link"}
    ]
    return ValidationResult(
        structural="failed" if structural_issues else "passed",
        integration="failed" if integration_issues else "passed",
        dependency_link=dependency_state,
        build=build_state,
        issues=issues,
    )


def build_android(
    root: Path,
    *,
    verbose: bool,
    stream: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, Optional[SubprocessError], int]:
    gradle = gradle_wrapper_path(root)
    command = gradle_wrapper_command(gradle, [":app:assembleDebug"])
    try:
        result = run_process(
            command,
            cwd=root / "android",
            timeout=1200,
            stream=stream if verbose else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, SubprocessError(command, 1, [str(exc)]), 0
    if result.returncode == 0:
        return True, None, 0
    lines = _relevant_lines(result.stdout + "\n" + result.stderr)
    return False, SubprocessError(command, result.returncode, lines), 0


def _relevant_lines(output: str) -> List[str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    preferred = [
        line
        for line in lines
        if any(token in line.lower() for token in ("error", "failed", "fatal", "exception", "> task"))
    ]
    selected = preferred or lines[-8:]
    unique: List[str] = []
    for line in selected:
        if line not in unique:
            unique.append(line)
    return unique[:9]
