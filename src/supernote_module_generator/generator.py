from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from . import __version__, binding_codegen
from .config import (
    METADATA_FILE,
    ProjectConfig,
    gradle_project_name,
    native_class_prefix,
    normalize_backend,
    public_type,
)
from .errors import FilesystemError
from .templates import render
from .validation import package_path, validate_config


CODEGEN_SUPPORT_MODULES = (
    "semantic.py",
    "source_models.py",
    "cpp_projection.py",
    "lowering.py",
)


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _copy_user_sources(
    source: Path,
    target: Path,
    *,
    replace_template: bool = False,
    excluded: tuple[Path, ...] = (),
) -> None:
    """Copy editable Java/Kotlin sources without silently replacing a file."""
    if not source.is_dir():
        return
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if any(relative == prefix or prefix in relative.parents for prefix in excluded):
            continue
        destination = target / relative
        if (
            destination.exists()
            and destination.read_bytes() != item.read_bytes()
            and not replace_template
        ):
            raise FilesystemError(
                f"Cannot migrate user source because two files map to {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)


def _uses_legacy_kotlin_export(source: Path, namespace_path: str) -> bool:
    """Return whether preserved user source still references ReactNativeExport."""
    source_roots = (
        source / "android/src/main/java",
        source / "android/src/main/kotlin",
    )
    generated_prefix = Path(namespace_path) / "generated"
    marker = re.compile(
        r"(?:@\s*(?:[A-Za-z_][A-Za-z0-9_.]*\.)?ReactNativeExport\b"
        r"|import\s+[A-Za-z_][A-Za-z0-9_.]*\.ReactNativeExport\b)"
    )
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*")):
            if path.suffix not in {".java", ".kt"} or not path.is_file():
                continue
            relative = path.relative_to(source_root)
            if (
                relative == generated_prefix
                or generated_prefix in relative.parents
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise FilesystemError(
                    f"Cannot inspect preserved user source {path}: {exc}"
                ) from exc
            # Remove block and line comments before checking imports/annotations.
            # This deliberately leaves string contents alone: a Java/Kotlin string
            # cannot match the import form, and an annotation-looking string is an
            # uncommon harmless reason to retain one compatibility type.
            code = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
            code = re.sub(r"//[^\n]*", "", code)
            if marker.search(code):
                return True
    return False


def _values(config: ProjectConfig) -> dict[str, str]:
    kotlin_name = native_class_prefix(config.npm_name)
    return {
        "NPM_NAME": json.dumps(config.npm_name),
        "NPM_NAME_RAW": config.npm_name,
        "VERSION": config.package_version,
        "DESCRIPTION": json.dumps(config.description),
        "NAMESPACE": config.android_namespace,
        "PACKAGE_PATH": package_path(config.android_namespace),
        "ANNOTATION_FQCN": f"{config.android_namespace}.nativemodule.annotation.SupernotePluginExport",
        "LEGACY_ANNOTATION_FQCN": f"{config.android_namespace}.nativemodule.annotation.ReactNativeExport",
        "MODULE_NAME": json.dumps(config.module_name),
        "MODULE_NAME_RAW": config.module_name,
        "BACKEND": config.backend,
        "GENERATOR_VERSION": __version__,
        "CLASS_PREFIX": kotlin_name,
        "GRADLE_MODULE_NAME": gradle_project_name(config.npm_name),
        "MIN_SDK": str(config.min_sdk),
        "NATIVE_LIBRARY_NAME": config.native_library_name or "",
        "JSI_GLOBAL_NAME": config.jsi_global_name or "",
        "LOG_TAG": f"SupernoteJsi{kotlin_name}",
        "JSI_FIND_PACKAGE": (
            "find_package(ReactAndroid REQUIRED CONFIG)"
            if config.backend == "jsi"
            else ""
        ),
        "JSI_LINK_TARGET": (
            "ReactAndroid::jsi"
            if config.backend == "jsi"
            else ""
        ),
        "BACKEND_NOTE": (
            "JSI calls are synchronous and run on the JavaScript thread. "
            "Use the C/C++ JNI backend for blocking file I/O or long work."
            if config.backend == "jsi"
            else
            "Value-returning calls use React Native promises. Native work runs "
            "through the generated Kotlin/JNI module."
        ),
    }


def stage(config: ProjectConfig, *, preserve_api_from: Path | None = None) -> Path:
    canonical_backend = normalize_backend(config.backend)
    if canonical_backend != config.backend:
        config = replace(config, backend=canonical_backend)
    validate_config(config)
    destination = config.output.resolve()
    parent = destination.parent
    staging_parent = parent if parent.exists() else parent.parent
    if not staging_parent.exists():
        raise FilesystemError(f"Output parent does not exist: {staging_parent}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=staging_parent))
    values = _values(config)
    try:
        base_files = _KOTLIN_FILES if config.backend == "kotlin" else _NATIVE_FILES
        for relative, template in base_files.items():
            if preserve_api_from and (
                (
                    config.backend == "kotlin"
                    and relative
                    == "android/src/main/java/$PACKAGE_PATH/Example.kt"
                )
                or (
                    config.backend != "kotlin"
                    and relative.startswith("android/src/main/cpp/")
                )
            ):
                # Starter implementation files are user-owned. An update must
                # not resurrect an example that the user deliberately deleted.
                continue
            if config.backend == "jsi" and template == "cpp.Package.kt.tmpl":
                continue
            if (
                config.backend == "jsi"
                and template == "jni.README.md.tmpl"
            ):
                template = "jsi.README.md.tmpl"
            path = relative.replace("$PACKAGE_PATH", values["PACKAGE_PATH"]).replace("${CLASS_PREFIX}", values["CLASS_PREFIX"])
            _write(temporary, path, render(template, values))
        if (
            config.backend == "kotlin"
            and preserve_api_from
            and _uses_legacy_kotlin_export(
                preserve_api_from, values["PACKAGE_PATH"]
            )
        ):
            _write(
                temporary,
                "android/.native-module/annotation/src/main/java/"
                f"{values['PACKAGE_PATH']}/nativemodule/annotation/"
                "ReactNativeExport.java",
                render("ReactNativeExport.java.tmpl", values),
            )
        if config.backend == "jsi":
            _write(
                temporary,
                "index.js",
                render("jsi.index.js.tmpl", values),
            )
            _write(
                temporary,
                f"android/src/main/java/{values['PACKAGE_PATH']}/generated/"
                f"{values['CLASS_PREFIX']}JsiModule.kt",
                render("jsi.Module.kt.tmpl", values),
            )
        if config.backend in {"cpp", "jni", "jsi"}:
            package_root = Path(binding_codegen.__file__).parent
            codegen_source = Path(binding_codegen.__file__).read_text(encoding="utf-8")
            _write(
                temporary,
                "android/.supernote-module/codegen.py",
                codegen_source,
            )
            _write(
                temporary,
                "android/.supernote-module/supernote_codegen/__init__.py",
                '"""Private support package for generated Supernote codegen."""\n',
            )
            for support_name in CODEGEN_SUPPORT_MODULES:
                _write(
                    temporary,
                    f"android/.supernote-module/supernote_codegen/{support_name}",
                    (package_root / support_name).read_text(encoding="utf-8"),
                )
            codegen_config = {
                "backend": config.backend,
                "npm_name": config.npm_name,
                "module_name": config.module_name,
                "android_namespace": config.android_namespace,
                "class_prefix": values["CLASS_PREFIX"],
                "native_library_name": config.native_library_name,
                "jsi_global_name": config.jsi_global_name,
            }
            _write(
                temporary,
                "android/.supernote-module/codegen-config.json",
                json.dumps(codegen_config, indent=2, sort_keys=True) + "\n",
            )
        if not config.description:
            package_file = temporary / "package.json"
            package = json.loads(package_file.read_text(encoding="utf-8"))
            package.pop("description", None)
            _write(
                temporary,
                "package.json",
                json.dumps(package, indent=2, ensure_ascii=False) + "\n",
            )
        if preserve_api_from:
            if config.backend == "kotlin":
                previous_types = preserve_api_from / "index.d.ts"
                if previous_types.is_file():
                    shutil.copy2(previous_types, temporary / "index.d.ts")
                target = temporary / "android/src/main/java"
                excluded = (
                    Path(values["PACKAGE_PATH"]) / "generated",
                )
                # Android's historical `java` source root supports both .java and
                # .kt files. Preserve the new layout first, then migrate files from
                # the generator's former split Kotlin source root.
                _copy_user_sources(
                    preserve_api_from / "android/src/main/java",
                    target,
                    replace_template=True,
                    excluded=excluded,
                )
                _copy_user_sources(
                    preserve_api_from / "android/src/main/kotlin",
                    target,
                    excluded=excluded,
                )
            else:
                _copy_user_sources(
                    preserve_api_from / "android/src/main/cpp",
                    temporary / "android/src/main/cpp",
                    replace_template=True,
                )
        if config.backend in {"cpp", "jni", "jsi"}:
            binding_codegen.generate(temporary)
        implementation_prefixes = (
            ("android/src/main/java/",)
            if config.backend == "kotlin"
            else ("android/src/main/cpp/",)
        )
        generated_files = []
        for candidate in sorted(temporary.rglob("*")):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(temporary).as_posix()
            if any(relative.startswith(prefix) for prefix in implementation_prefixes):
                if "/generated/" not in relative:
                    continue
            generated_files.append(relative)
        generated_files.append(METADATA_FILE)
        metadata = {
            **config.metadata(),
            "generator_version": __version__,
            "metadata_schema": "1.0",
            "type": public_type(config.backend),
            "generated_files": sorted(set(generated_files)),
            "implementation_roots": list(implementation_prefixes),
        }
        _write(
            temporary,
            METADATA_FILE,
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        )
        return temporary
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def activate(staged: Path, destination: Path) -> Path | None:
    """Install a staged project and return a backup that the caller must finalize or restore."""
    backup: Path | None = None
    if destination.exists():
        backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
        return backup
    except Exception:
        if backup and backup.exists():
            os.replace(backup, destination)
        raise


def finalize(backup: Path | None) -> None:
    if backup:
        shutil.rmtree(backup, ignore_errors=True)


def restore(destination: Path, backup: Path | None) -> None:
    if backup is None:
        shutil.rmtree(destination, ignore_errors=True)
    elif backup.exists():
        failed = destination.parent / f".{destination.name}.failed-{uuid.uuid4().hex}"
        if destination.exists():
            os.replace(destination, failed)
        os.replace(backup, destination)
        shutil.rmtree(failed, ignore_errors=True)


def generate(config: ProjectConfig) -> Path:
    """Compatibility helper for standalone generation tests and callers."""
    staged = stage(config)
    backup = activate(staged, config.output.resolve())
    finalize(backup)
    return config.output.resolve()


_KOTLIN_FILES = {
    "package.json": "package.json.tmpl",
    "index.js": "index.js.tmpl",
    "index.d.ts": "index.d.ts.tmpl",
    "README.md": "README.md.tmpl",
    ".gitignore": "gitignore.tmpl",
    "react-native.config.js": "react-native.config.js.tmpl",
    "android/build.gradle.kts": "build.gradle.kts.tmpl",
    "android/autolink/${CLASS_PREFIX}NativeModulePackage.kt": "autolink.Package.kt.tmpl",
    "android/src/main/AndroidManifest.xml": "AndroidManifest.xml.tmpl",
    "android/src/main/java/$PACKAGE_PATH/Example.kt": "Example.kt.tmpl",
    "android/.native-module/annotation/src/main/java/$PACKAGE_PATH/nativemodule/annotation/SupernotePluginExport.java": "SupernotePluginExport.java.tmpl",
    "android/.native-module/processor/src/main/kotlin/localmodule/processor/SupernoteExportProcessor.kt": "SupernoteExportProcessor.kt.tmpl",
    "android/.native-module/processor/src/main/resources/META-INF/services/com.google.devtools.ksp.processing.SymbolProcessorProvider": "processor.provider.tmpl",
    "android/.native-module/processor/build.gradle.kts": "processor.build.gradle.kts.tmpl",
    "android/.native-module/annotation/build.gradle.kts": "annotation.build.gradle.kts.tmpl",
}

_NATIVE_FILES = {
    "package.json": "cpp.package.json.tmpl",
    "index.js": "cpp.index.js.tmpl",
    "README.md": "jni.README.md.tmpl",
    ".gitignore": "gitignore.tmpl",
    "react-native.config.js": "react-native.config.js.tmpl",
    "android/build.gradle.kts": "cpp.build.gradle.kts.tmpl",
    "android/.supernote-module/CMakeLists.txt": "cpp.CMakeLists.txt.tmpl",
    "android/src/main/AndroidManifest.xml": "AndroidManifest.xml.tmpl",
    "android/src/main/cpp/math.cpp": "cpp.math.cpp.tmpl",
    "android/src/main/cpp/text.cpp": "cpp.text.cpp.tmpl",
    "android/src/main/cpp/helpers.h": "cpp.helpers.h.tmpl",
    "android/src/main/cpp/helpers.c": "cpp.helpers.c.tmpl",
    "android/src/main/java/$PACKAGE_PATH/generated/${CLASS_PREFIX}NativeModulePackage.kt": "cpp.Package.kt.tmpl",
}
