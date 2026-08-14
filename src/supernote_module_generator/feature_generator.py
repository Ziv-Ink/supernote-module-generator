"""Transactional scaffolding for one language-neutral V2 logical feature."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from . import __version__
from .feature_model import FeatureManifest, StarterFamily


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
    staging_parent = parent if parent.exists() else parent.parent
    if not staging_parent.exists():
        raise ValueError(f"output parent does not exist: {staging_parent}")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=staging_parent)
    )
    try:
        starter_files = []
        native_starter = "android/src/main/cpp/feature.cpp"
        if (
            StarterFamily.NATIVE in config.starters
            and (
                preserve_sources_from is None
                or (preserve_sources_from / native_starter).is_file()
            )
        ):
            relative = native_starter
            starter_files.append(relative)
            _write(
                temporary,
                relative,
                "#include <string>\n\n"
                "// @SupernotePluginExport\n"
                "std::string greet(std::string name) {\n"
                '  return "Hello, " + name;\n'
                "}\n",
            )
        namespace_path = config.android_namespace.replace(".", "/")
        jvm_starter = f"android/src/main/java/{namespace_path}/FeatureApi.kt"
        if (
            StarterFamily.JVM in config.starters
            and (
                preserve_sources_from is None
                or (preserve_sources_from / jvm_starter).is_file()
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
        global_name = "__supernoteV2"
        _write(
            temporary,
            "index.js",
            "export class SupernoteError extends Error {\n"
            "  constructor(code, message) {\n"
            "    super(message);\n"
            "    this.name = 'SupernoteError';\n"
            "    Object.defineProperty(this, 'code', { value: code, enumerable: true });\n"
            "  }\n"
            "}\n\n"
            "const ERROR_CONSTRUCTOR_PROPERTY = '__supernoteErrorConstructor';\n"
            f"const INSTALL_ERROR = {json.dumps(config.public_name + ' is not installed in the Supernote V2 runtime')};\n\n"
            "function requireFeature() {\n"
            "  const runtime = globalThis."
            + global_name
            + ";\n"
            "  if (!runtime || typeof runtime.feature !== 'function') {\n"
            "    throw new Error(INSTALL_ERROR);\n"
            "  }\n"
            f"  const value = runtime.feature({json.dumps(feature.feature_id)});\n"
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
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY) return undefined;\n"
            "      return requireFeature()[property];\n"
            "    },\n"
            "    has(_target, property) {\n"
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY) return false;\n"
            "      return property in requireFeature();\n"
            "    },\n"
            "    ownKeys() {\n"
            "      return Reflect.ownKeys(requireFeature()).filter(\n"
            "        property => property !== ERROR_CONSTRUCTOR_PROPERTY,\n"
            "      );\n"
            "    },\n"
            "    getOwnPropertyDescriptor(_target, property) {\n"
            "      if (property === ERROR_CONSTRUCTOR_PROPERTY) return undefined;\n"
            "      const descriptor = Object.getOwnPropertyDescriptor(\n"
            "        requireFeature(),\n"
            "        property,\n"
            "      );\n"
            "      return descriptor ? { ...descriptor, configurable: true } : undefined;\n"
            "    },\n"
            "  },\n"
            ");\n\n"
            "export default feature;\n",
        )
        _write(
            temporary,
            "index.d.ts",
            "/* Generated by supernote_module_generator. Do not edit. */\n"
            f"export interface {config.public_name}Feature {{}}\n"
            f"declare const feature: {config.public_name}Feature;\n"
            "export default feature;\n",
        )
        if preserve_sources_from is not None:
            previous_types = preserve_sources_from / "index.d.ts"
            if previous_types.is_file():
                shutil.copy2(previous_types, temporary / "index.d.ts")
        _write(temporary, "README.md", _feature_readme(config))
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


def _feature_readme(config: FeatureConfig) -> str:
    return f"""# {config.npm_name}

Generated Supernote V2 feature package. Its logical feature is language-neutral:
C/C++ and Kotlin/Java source may coexist under `android/src/main/`.

Only declarations with explicit Supernote markers enter generated APIs. Ordinary
public source remains ordinary implementation code. `SupernotePluginExport` publishes a
declaration to JavaScript; `SupernotePluginInternal` generates hidden cross-language
routing; `SupernotePluginAsync` makes an accepted call return a Promise. Marker spelling
is source-language-specific (`// @SupernotePluginExport` in C++ and
`@SupernotePluginExport` on Kotlin/Java declarations).

The generated TypeScript API is `index.d.ts`. Run the plugin's Android/Gradle
generation after changing marked declarations. `supernote-module update
{config.npm_name}` refreshes generator-owned files while preserving user-owned
C/C++ and Kotlin/Java source.

Generated runtime behavior is shared once by the whole plugin. Do not edit this
package's generated JavaScript, TypeScript, metadata, or README directly.
"""


def activate_feature(staged: Path, destination: Path) -> Path | None:
    backup = None
    if destination.exists():
        backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
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
    if not source.is_dir():
        return
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
