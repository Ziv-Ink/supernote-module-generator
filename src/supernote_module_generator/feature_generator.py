"""Transactional scaffolding for one language-neutral logical feature."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from . import __version__
from .filesystem import copy_tree_contents_no_follow, entry_kind
from .feature_identity import FeatureIdentity
from .feature_model import FeatureManifest, StarterFamily
from .readme_codegen import render_feature_readme
from .semantic import SemanticApi


def _javascript_string(value: str) -> str:
    """Render a deterministic single-quoted JavaScript string literal."""

    body = json.dumps(value, ensure_ascii=False)[1:-1]
    return "'" + body.replace(r'\"', '"').replace("'", r"\'") + "'"


@dataclass(frozen=True)
class FeatureConfig:
    output: Path
    npm_name: str
    package_version: str
    android_namespace: str
    public_name: str
    description: str = "Local Supernote feature"
    starters: tuple[StarterFamily, ...] = (StarterFamily.NATIVE,)

    def __post_init__(self) -> None:
        FeatureIdentity.create(
            npm_name=self.npm_name,
            android_namespace=self.android_namespace,
            package_version=self.package_version,
        )
        if not self.starters:
            raise ValueError("at least one starter family is required")
        if len(set(self.starters)) != len(self.starters):
            raise ValueError("starter families cannot be duplicated")


def stage_feature(
    config: FeatureConfig,
    *,
    preserve_sources_from: Path | None = None,
) -> Path:
    destination = config.output.resolve()
    parent = destination.parent
    staging_parent = parent
    while not staging_parent.exists() and staging_parent != staging_parent.parent:
        staging_parent = staging_parent.parent
    if not staging_parent.is_dir():
        raise ValueError(f"output parent does not exist: {parent}")
    temporary = Path(
        tempfile.mkdtemp(prefix=".sn-module-gen-feature-stage-", dir=staging_parent)
    )
    try:
        starter_files = []
        native_starter = "android/src/main/cpp/feature.cpp"
        if (
            StarterFamily.NATIVE in config.starters
            and (
                preserve_sources_from is None
                or entry_kind(preserve_sources_from / native_starter) == "file"
            )
        ):
            relative = native_starter
            starter_files.append(relative)
            _write(
                temporary,
                relative,
                "#include <string>\n\n"
                f"namespace supernote_feature_{config.public_name} {{\n\n"
                "// @SupernotePluginExport\n"
                "std::string greet(std::string name) {\n"
                '  return "Hello, " + name;\n'
                "}\n\n"
                f"}}  // namespace supernote_feature_{config.public_name}\n",
            )
        namespace_path = config.android_namespace.replace(".", "/")
        jvm_starter = f"android/src/main/java/{namespace_path}/FeatureApi.kt"
        if (
            StarterFamily.JVM in config.starters
            and (
                preserve_sources_from is None
                or entry_kind(preserve_sources_from / jvm_starter) == "file"
            )
        ):
            relative = jvm_starter
            starter_files.append(relative)
            _write(
                temporary,
                relative,
                f"package {config.android_namespace}\n\n"
                "import supernote.generated.annotations.SupernotePluginExport\n\n"
                "@SupernotePluginExport\n"
                "fun greetFromJvm(name: String): String = \"Hello, $name\"\n",
            )
        if preserve_sources_from is not None:
            for root in (
                "android/src/main/cpp",
                "android/src/main/java",
            ):
                _copy_user_tree(preserve_sources_from / root, temporary / root)
        feature = FeatureManifest.create(
            npm_name=config.npm_name,
            public_name=config.public_name,
            android_namespace=config.android_namespace,
            starter_files=starter_files,
        )
        package = {
            "name": config.npm_name,
            "version": config.package_version,
            "main": "index.js",
            "types": "index.d.ts",
            "files": ["android/src/main", "index.js", "index.d.ts", "README.md"],
        }
        if config.description:
            package["description"] = config.description
        _write(
            temporary,
            "package.json",
            json.dumps(package, indent=2, ensure_ascii=False) + "\n",
        )
        global_name = "__supernoteModule"
        _write(
            temporary,
            "index.js",
            "/* global globalThis */\n"
            "export class SupernoteError extends Error {\n"
            "  constructor(code, message) {\n"
            "    super(message);\n"
            "    this.name = 'SupernoteError';\n"
            "    Object.defineProperty(this, 'code', {value: code, enumerable: true});\n"
            "  }\n"
            "}\n\n"
            "const ERROR_CONSTRUCTOR_PROPERTY = '__supernoteErrorConstructor';\n"
            "const CPP_OBJECT_INFO_PROPERTY = '__supernoteCppObjectInfo';\n"
            "const JVM_OBJECT_INFO_PROPERTY = '__supernoteJvmObjectInfo';\n"
            f"const INSTALL_ERROR = {_javascript_string(config.public_name + ' is not installed in the Supernote generated runtime')};\n\n"
            "const VALIDATION_REASONS = new Set([\n"
            "  'ARITY_MISMATCH',\n"
            "  'TYPE_MISMATCH',\n"
            "  'NOMINAL_MISMATCH',\n"
            "  'MISSING_FIELD',\n"
            "  'INVALID_ENUM',\n"
            "  'OUT_OF_RANGE',\n"
            "  'LIMIT_EXCEEDED',\n"
            "]);\n\n"
            "function currentFeature() {\n"
            "  const runtime = globalThis."
            + global_name
            + ";\n"
            "  if (!runtime || typeof runtime.feature !== 'function') {\n"
            "    return {status: 'runtime-unavailable'};\n"
            "  }\n"
            "  try {\n"
            f"    const value = runtime.feature({_javascript_string(feature.feature_id)});\n"
            "    if (!value || (typeof value !== 'object' && typeof value !== 'function')) {\n"
            "      return {status: 'feature-unavailable'};\n"
            "    }\n"
            "    return {status: 'available', value};\n"
            "  } catch (_error) {\n"
            "    return {status: 'feature-unavailable'};\n"
            "  }\n"
            "}\n\n"
            "export function getFeatureStatus() {\n"
            "  return currentFeature().status;\n"
            "}\n\n"
            "export function isFeatureAvailable() {\n"
            "  return getFeatureStatus() === 'available';\n"
            "}\n\n"
            "export function nativeObjectInfo(value) {\n"
            "  const current = currentFeature();\n"
            "  if (current.status !== 'available') {\n"
            "    return undefined;\n"
            "  }\n"
            "  for (const property of [CPP_OBJECT_INFO_PROPERTY, JVM_OBJECT_INFO_PROPERTY]) {\n"
            "    const inspect = current.value[property];\n"
            "    if (typeof inspect !== 'function') {\n"
            "      continue;\n"
            "    }\n"
            "    const info = inspect(value);\n"
            "    if (info !== undefined) {\n"
            "      return info;\n"
            "    }\n"
            "  }\n"
            "  return undefined;\n"
            "}\n\n"
            "function hasValidationDetails(value) {\n"
            "  return Boolean(\n"
            "    value &&\n"
            "      VALIDATION_REASONS.has(value.reason) &&\n"
            "      typeof value.path === 'string' &&\n"
            "      typeof value.expected === 'string' &&\n"
            "      typeof value.actual === 'string',\n"
            "  );\n"
            "}\n\n"
            "export function isSupernoteTypeError(value) {\n"
            "  return value instanceof TypeError && hasValidationDetails(value);\n"
            "}\n\n"
            "export function isSupernoteRangeError(value) {\n"
            "  return value instanceof RangeError && hasValidationDetails(value);\n"
            "}\n\n"
            "function requireFeature() {\n"
            "  const runtime = globalThis."
            + global_name
            + ";\n"
            "  if (!runtime || typeof runtime.feature !== 'function') {\n"
            "    throw new Error(INSTALL_ERROR);\n"
            "  }\n"
            f"  const value = runtime.feature({_javascript_string(feature.feature_id)});\n"
            "  if (!value || (typeof value !== 'object' && typeof value !== 'function')) {\n"
            "    throw new Error(INSTALL_ERROR);\n"
            "  }\n"
            "  if (value[ERROR_CONSTRUCTOR_PROPERTY] !== SupernoteError) {\n"
            "    Object.defineProperty(value, ERROR_CONSTRUCTOR_PROPERTY, {\n"
            "      configurable: true,\n"
            "      enumerable: false,\n"
            "      value: SupernoteError,\n"
            "      writable: false,\n"
            "    });\n"
            "  }\n"
            "  return value;\n"
            "}\n\n"
            "const feature = new Proxy(\n"
            "  {},\n"
            "  {\n"
            "    get(_target, property) {\n"
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY ||\n"
            "          property === CPP_OBJECT_INFO_PROPERTY ||\n"
            "          property === JVM_OBJECT_INFO_PROPERTY) {\n"
            "        return undefined;\n"
            "      }\n"
            "      return requireFeature()[property];\n"
            "    },\n"
            "    has(_target, property) {\n"
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY ||\n"
            "          property === CPP_OBJECT_INFO_PROPERTY ||\n"
            "          property === JVM_OBJECT_INFO_PROPERTY) {\n"
            "        return false;\n"
            "      }\n"
            "      return property in requireFeature();\n"
            "    },\n"
            "    ownKeys() {\n"
            "      return Reflect.ownKeys(requireFeature()).filter(\n"
            "        property => property !== ERROR_CONSTRUCTOR_PROPERTY &&\n"
            "          property !== CPP_OBJECT_INFO_PROPERTY &&\n"
            "          property !== JVM_OBJECT_INFO_PROPERTY,\n"
            "      );\n"
            "    },\n"
            "    getOwnPropertyDescriptor(_target, property) {\n"
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY ||\n"
            "          property === CPP_OBJECT_INFO_PROPERTY ||\n"
            "          property === JVM_OBJECT_INFO_PROPERTY) {\n"
            "        return undefined;\n"
            "      }\n"
            "      const descriptor = Object.getOwnPropertyDescriptor(\n"
            "        requireFeature(),\n"
            "        property,\n"
            "      );\n"
            "      return descriptor ? {...descriptor, configurable: true} : undefined;\n"
            "    },\n"
            "  },\n"
            ");\n\n"
            "export default feature;\n",
        )
        _write(
            temporary,
            "index.d.ts",
            "/* Generated by supernote_module_generator. Do not edit. */\n"
            "export type SupernoteFeatureStatus = 'available' | 'runtime-unavailable' | 'feature-unavailable';\n"
            "export function isFeatureAvailable(): boolean;\n"
            "export function getFeatureStatus(): SupernoteFeatureStatus;\n"
            "export function nativeObjectInfo(value: unknown): {readonly type: string; readonly originFamily: 'cpp' | 'jvm'} | undefined;\n"
            "export function isSupernoteTypeError(value: unknown): value is TypeError;\n"
            "export function isSupernoteRangeError(value: unknown): value is RangeError;\n"
            f"export interface {config.public_name}Feature {{}}\n"
            f"declare const feature: {config.public_name}Feature;\n"
            "export default feature;\n",
        )
        if preserve_sources_from is not None:
            previous_types = preserve_sources_from / "index.d.ts"
            if previous_types.is_file():
                shutil.copy2(previous_types, temporary / "index.d.ts")
        implementation_roots = []
        if (temporary / "android/src/main/cpp").is_dir():
            implementation_roots.append(("C/C++", "android/src/main/cpp/"))
        if (temporary / "android/src/main/java").is_dir():
            implementation_roots.append(("Kotlin/Java", "android/src/main/java/"))
        _write(
            temporary,
            "README.md",
            render_feature_readme(
                npm_name=config.npm_name,
                public_name=config.public_name,
                description=config.description,
                generator_version=__version__,
                implementation_roots=tuple(implementation_roots),
                api=SemanticApi(),
            ),
        )
        metadata = {
            **feature.manifest(),
            "package_version": config.package_version,
            "description": config.description,
            "generator_version": __version__,
            "generated_files": [
                ".supernote-module.json",
                "index.d.ts",
                "index.js",
                "package.json",
                "README.md",
            ],
        }
        _write(
            temporary,
            ".supernote-module.json",
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        )
        return temporary
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def activate_feature(staged: Path, destination: Path) -> Path | None:
    backup = None
    if destination.exists():
        backup = destination.parent / (
            f".sn-module-gen-feature-backup-{uuid.uuid4().hex}"
        )
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
        return backup
    except Exception:
        if backup is not None and backup.exists():
            os.replace(backup, destination)
        raise


def generate_feature(config: FeatureConfig) -> Path:
    staged = stage_feature(config)
    backup = activate_feature(staged, config.output.resolve())
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return config.output.resolve()


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _copy_user_tree(source: Path, target: Path) -> None:
    copy_tree_contents_no_follow(source, target)
