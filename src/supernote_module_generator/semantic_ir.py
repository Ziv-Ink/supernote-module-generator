"""Versioned project-level semantic input for generated output."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Dict, Iterable, Tuple

from .feature_identity import FeatureIdentity
from .semantic import SemanticApi, merge_semantic_apis, semantic_api_from_manifest


SEMANTIC_IR_SCHEMA_VERSION = "1.0"
CPP_FRONTEND_VERSION = 1
JVM_FRONTEND_VERSION = 1


class SemanticIRError(ValueError):
    """A serialized or merged semantic generation is invalid."""


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class FeatureSemanticIR:
    identity: FeatureIdentity
    public_name: str
    description: str
    cpp: SemanticApi
    jvm: SemanticApi
    merged: SemanticApi

    @classmethod
    def create(
        cls,
        identity: FeatureIdentity,
        *,
        public_name: str | None = None,
        description: str = "",
        cpp: SemanticApi | None = None,
        jvm: SemanticApi | None = None,
    ) -> "FeatureSemanticIR":
        cpp_api = cpp or SemanticApi()
        jvm_api = jvm or SemanticApi()
        try:
            merged = merge_semantic_apis(cpp_api, jvm_api)
        except ValueError as exc:
            raise SemanticIRError(
                f"{identity.npm_name}: C/C++ and JVM semantic inputs conflict: {exc}"
            ) from exc
        return cls(
            identity,
            public_name or identity.npm_name.rsplit("/", 1)[-1],
            description,
            cpp_api,
            jvm_api,
            merged,
        )

    @property
    def semantic_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.merged.manifest())).hexdigest()

    def manifest(self) -> Dict[str, object]:
        return {
            "feature_id": self.identity.feature_id,
            "npm_name": self.identity.npm_name,
            "package_version": self.identity.package_version,
            "android_namespace": self.identity.android_namespace,
            "public_name": self.public_name,
            "description": self.description,
            "semantic_hash": self.semantic_hash,
            "frontends": {
                "cpp": self.cpp.manifest(),
                "jvm": self.jvm.manifest(),
            },
            "semantic_api": self.merged.manifest(),
        }


@dataclass(frozen=True)
class SemanticIR:
    plugin_id: str
    features: Tuple[FeatureSemanticIR, ...]
    schema_version: str = SEMANTIC_IR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SEMANTIC_IR_SCHEMA_VERSION:
            raise SemanticIRError(
                f"unsupported SemanticIR schema {self.schema_version}; "
                f"expected {SEMANTIC_IR_SCHEMA_VERSION}"
            )
        if not self.plugin_id:
            raise SemanticIRError("plugin identity cannot be empty")
        npm_names = [feature.identity.npm_name for feature in self.features]
        feature_ids = [feature.identity.feature_id for feature in self.features]
        if len(set(npm_names)) != len(npm_names):
            raise SemanticIRError("duplicate npm package identity in SemanticIR")
        if len(set(feature_ids)) != len(feature_ids):
            raise SemanticIRError("duplicate feature identity in SemanticIR")

    @classmethod
    def create(
        cls,
        plugin_id: str,
        features: Iterable[FeatureSemanticIR],
    ) -> "SemanticIR":
        ordered = tuple(
            sorted(features, key=lambda feature: feature.identity.npm_name)
        )
        return cls(plugin_id, ordered)

    @property
    def generation_id(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.manifest())).hexdigest()

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "supernote_module_semantic_ir",
            "plugin_id": self.plugin_id,
            "frontend_versions": {
                "cpp": CPP_FRONTEND_VERSION,
                "jvm": JVM_FRONTEND_VERSION,
            },
            "features": [feature.manifest() for feature in self.features],
        }

    @classmethod
    def from_manifest(cls, raw: object) -> "SemanticIR":
        if not isinstance(raw, dict):
            raise SemanticIRError("SemanticIR must be a JSON object")
        expected = {
            "schema_version",
            "kind",
            "plugin_id",
            "frontend_versions",
            "features",
        }
        if set(raw) != expected:
            raise SemanticIRError("SemanticIR fields are invalid")
        if raw.get("schema_version") != SEMANTIC_IR_SCHEMA_VERSION:
            raise SemanticIRError("SemanticIR schema is incompatible")
        if raw.get("kind") != "supernote_module_semantic_ir":
            raise SemanticIRError("SemanticIR kind is invalid")
        versions = raw.get("frontend_versions")
        if versions != {"cpp": CPP_FRONTEND_VERSION, "jvm": JVM_FRONTEND_VERSION}:
            raise SemanticIRError("SemanticIR frontend versions are incompatible")
        plugin_id = raw.get("plugin_id")
        features = raw.get("features")
        if not isinstance(plugin_id, str) or not isinstance(features, list):
            raise SemanticIRError("SemanticIR plugin/features fields are invalid")
        parsed = []
        for index, item in enumerate(features):
            if not isinstance(item, dict):
                raise SemanticIRError(f"features[{index}] must be an object")
            try:
                identity = FeatureIdentity.create(
                    npm_name=str(item["npm_name"]),
                    android_namespace=str(item["android_namespace"]),
                    package_version=str(item["package_version"]),
                    feature_id=str(item["feature_id"]),
                )
                frontends = item["frontends"]
                if not isinstance(frontends, dict) or set(frontends) != {"cpp", "jvm"}:
                    raise SemanticIRError("frontends must contain cpp and jvm")
                feature = FeatureSemanticIR.create(
                    identity,
                    public_name=str(item["public_name"]),
                    description=str(item["description"]),
                    cpp=semantic_api_from_manifest(frontends["cpp"]),
                    jvm=semantic_api_from_manifest(frontends["jvm"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SemanticIRError(f"features[{index}] is invalid: {exc}") from exc
            if item.get("semantic_api") != feature.merged.manifest():
                raise SemanticIRError(
                    f"features[{index}] merged semantic API is not canonical"
                )
            if item.get("semantic_hash") != feature.semantic_hash:
                raise SemanticIRError(
                    f"features[{index}] semantic hash is not canonical"
                )
            parsed.append(feature)
        result = cls.create(plugin_id, parsed)
        if result.manifest() != raw:
            raise SemanticIRError("SemanticIR feature ordering is not canonical")
        return result
