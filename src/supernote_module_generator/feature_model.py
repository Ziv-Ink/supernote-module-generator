"""Language-neutral feature ownership and plugin runtime registry for V4."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import PurePosixPath
import re
from typing import Dict, Iterable, Tuple

from .errors import ValidationError
from .feature_identity import canonical_feature_id
from .naming import validate_android_namespace, validate_package_name

from .semantic import (
    ExecutionMode,
    SemanticApi,
    SemanticObjectDeclaration,
)
from .v4_schemas import (
    FEATURE_MANIFEST_KIND,
    FEATURE_MANIFEST_SCHEMA_VERSION,
    PLUGIN_REGISTRY_KIND,
    PLUGIN_REGISTRY_SCHEMA_VERSION,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FeatureModelError(ValueError):
    """Raised when V4 ownership/build metadata violates its contract."""


class StarterFamily(str, Enum):
    NATIVE = "native"
    JVM = "jvm"


class ImplementationFamily(str, Enum):
    NATIVE = "native"
    JVM = "jvm"


@dataclass(frozen=True)
class ImplementationRoots:
    """User-owned roots available to every feature, regardless of starters."""

    native: str = "android/src/main/cpp"
    jvm: str = "android/src/main/java"

    def __post_init__(self) -> None:
        _validate_relative_path(self.native, "native implementation root")
        _validate_relative_path(self.jvm, "JVM implementation root")
        if self.native == self.jvm:
            raise FeatureModelError("native and JVM implementation roots must differ")

    def manifest(self) -> Dict[str, str]:
        return {"native": self.native, "jvm": self.jvm}


@dataclass(frozen=True)
class FeatureManifest:
    """Portable user-facing feature ownership; never a backend selection."""

    feature_id: str
    npm_name: str
    public_name: str
    android_namespace: str
    roots: ImplementationRoots = field(default_factory=ImplementationRoots)
    starter_files: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = FEATURE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FEATURE_MANIFEST_SCHEMA_VERSION:
            raise FeatureModelError(
                "unsupported feature manifest schema "
                f"{self.schema_version}; expected {FEATURE_MANIFEST_SCHEMA_VERSION}"
            )
        try:
            validate_package_name(self.npm_name)
            validate_android_namespace(self.android_namespace)
        except ValidationError as exc:
            raise FeatureModelError(str(exc)) from exc
        expected_feature_id = canonical_feature_id(self.npm_name)
        if self.feature_id != expected_feature_id:
            raise FeatureModelError(
                f"feature identity mismatch: expected {expected_feature_id!r}, "
                f"got {self.feature_id!r}"
            )
        if not _IDENTIFIER.fullmatch(self.public_name):
            raise FeatureModelError(
                f"invalid feature public name {self.public_name!r}"
            )
        _reject_duplicates(self.starter_files, "starter file")
        for path in self.starter_files:
            _validate_relative_path(path, "starter file")
            if not (
                path == self.roots.native
                or path.startswith(self.roots.native + "/")
                or path == self.roots.jvm
                or path.startswith(self.roots.jvm + "/")
            ):
                raise FeatureModelError(
                    f"starter file {path!r} is outside the implementation roots"
                )

    @classmethod
    def create(
        cls,
        *,
        npm_name: str,
        public_name: str,
        android_namespace: str,
        starter_files: Iterable[str] = (),
        roots: ImplementationRoots | None = None,
    ) -> "FeatureManifest":
        return cls(
            feature_id=feature_identity(npm_name),
            npm_name=npm_name,
            public_name=public_name,
            android_namespace=android_namespace,
            roots=roots or ImplementationRoots(),
            starter_files=tuple(sorted(starter_files)),
        )

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": FEATURE_MANIFEST_KIND,
            "feature_id": self.feature_id,
            "npm_name": self.npm_name,
            "public_name": self.public_name,
            "android_namespace": self.android_namespace,
            "implementation_roots": self.roots.manifest(),
            "starter_files": list(self.starter_files),
        }


@dataclass(frozen=True)
class FeatureRequirements:
    """Build facts derived from semantic declarations, never starter choices."""

    families: Tuple[ImplementationFamily, ...]
    javascript_public: bool
    asynchronous: bool

    @classmethod
    def from_semantic_api(cls, api: SemanticApi) -> "FeatureRequirements":
        bindings = list(api.functions)
        for semantic_class in api.classes:
            bindings.extend(semantic_class.methods)
        for declaration in api.declarations:
            if isinstance(declaration, SemanticObjectDeclaration):
                bindings.extend(declaration.methods)
        languages = {
            source.language
            for source in (
                [binding.source for binding in bindings]
                + [semantic_class.source for semantic_class in api.classes]
                + [
                    projection.source
                    for declaration in api.declarations
                    for projection in declaration.projections
                ]
            )
        }
        unknown = languages - {"cpp", "kotlin", "java"}
        if unknown:
            raise FeatureModelError(
                "unsupported semantic source language(s): "
                + ", ".join(sorted(unknown))
            )
        families = []
        if "cpp" in languages:
            families.append(ImplementationFamily.NATIVE)
        if languages & {"kotlin", "java"}:
            families.append(ImplementationFamily.JVM)
        public = any(
            binding.capabilities.javascript_public for binding in bindings
        ) or any(
            semantic_class.capabilities.javascript_public
            for semantic_class in api.classes
        ) or any(
            isinstance(declaration, SemanticObjectDeclaration)
            and declaration.constructor is not None
            for declaration in api.declarations
        )
        asynchronous = any(
            binding.execution is ExecutionMode.ASYNC for binding in bindings
        )
        return cls(tuple(families), public, asynchronous)

    def manifest(self) -> Dict[str, object]:
        return {
            "implementation_families": [family.value for family in self.families],
            "javascript_public": self.javascript_public,
            "asynchronous": self.asynchronous,
        }


@dataclass(frozen=True)
class FeatureRegistryEntry:
    feature: FeatureManifest
    requirements: FeatureRequirements
    semantic_digest: str
    source_semantic_api: SemanticApi

    @classmethod
    def create(
        cls,
        feature: FeatureManifest,
        semantic_api: SemanticApi,
    ) -> "FeatureRegistryEntry":
        encoded = _canonical_json(semantic_api.manifest()).encode("utf-8")
        return cls(
            feature,
            FeatureRequirements.from_semantic_api(semantic_api),
            hashlib.sha256(encoded).hexdigest(),
            semantic_api,
        )

    def manifest(self) -> Dict[str, object]:
        return {
            "feature_id": self.feature.feature_id,
            "npm_name": self.feature.npm_name,
            "public_name": self.feature.public_name,
            "android_namespace": self.feature.android_namespace,
            "implementation_roots": self.feature.roots.manifest(),
            "semantic_digest": self.semantic_digest,
            "requirements": self.requirements.manifest(),
            "source_semantic_api": self.source_semantic_api.manifest(),
        }


@dataclass(frozen=True)
class PluginRuntimeRegistry:
    """One generator/runtime version and compiled component for all features."""

    plugin_id: str
    component_name: str
    generator_version: str
    features: Tuple[FeatureRegistryEntry, ...]
    schema_version: int = PLUGIN_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLUGIN_REGISTRY_SCHEMA_VERSION:
            raise FeatureModelError(
                "unsupported plugin registry schema "
                f"{self.schema_version}; expected {PLUGIN_REGISTRY_SCHEMA_VERSION}"
            )
        if not self.plugin_id:
            raise FeatureModelError("plugin identity cannot be empty")
        if not self.generator_version:
            raise FeatureModelError("registry generator version cannot be empty")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.component_name):
            raise FeatureModelError("plugin runtime component name is invalid")
        _reject_duplicates(
            (entry.feature.feature_id for entry in self.features),
            "feature identity",
        )
        _reject_duplicates(
            (entry.feature.npm_name for entry in self.features),
            "feature npm name",
        )
        _reject_duplicates(
            (entry.feature.public_name for entry in self.features),
            "feature public name",
        )
        _reject_duplicates(
            (entry.feature.android_namespace for entry in self.features),
            "feature Android namespace",
        )

    @classmethod
    def create(
        cls,
        *,
        plugin_id: str,
        generator_version: str,
        features: Iterable[FeatureRegistryEntry],
    ) -> "PluginRuntimeRegistry":
        return cls(
            plugin_id=plugin_id,
            component_name=plugin_runtime_component_name(plugin_id),
            generator_version=generator_version,
            features=tuple(
                sorted(features, key=lambda entry: entry.feature.feature_id)
            ),
        )

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": PLUGIN_REGISTRY_KIND,
            "plugin_id": self.plugin_id,
            "component_name": self.component_name,
            "generator_version": self.generator_version,
            "features": [entry.manifest() for entry in self.features],
        }


def feature_identity(npm_name: str) -> str:
    return canonical_feature_id(npm_name)


def plugin_runtime_component_name(plugin_id: str) -> str:
    digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:12]
    return f"sn_supernote_runtime_{digest}"


def _validate_relative_path(value: str, label: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or value != path.as_posix()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise FeatureModelError(f"{label} must be a normalized relative path")


def _reject_duplicates(values: Iterable[str], label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise FeatureModelError(f"duplicate {label} {value!r}")
        seen.add(value)


def _canonical_json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
