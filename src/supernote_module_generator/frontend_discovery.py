"""Language frontends lowering into one complete project SemanticIR."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

from . import binding_codegen
from .jvm_manifest import read_jvm_manifest
from .jvm_projection import project_jvm_owners
from .project_model import ProjectModel
from .filesystem import iter_tree_no_follow
from .semantic import SemanticApi
from .semantic_ir import FeatureSemanticIR, SemanticIR, SemanticIRError


def load_jvm_frontend_output(
    project: ProjectModel,
    manifest_root: Path,
) -> Dict[str, SemanticApi]:
    """Load exact KSP build output without permitting it to write source output."""

    manifests = load_jvm_frontend_manifests(project, manifest_root)
    return {
        feature_id: project_jvm_owners(raw.owners, feature_id=feature_id)
        for feature_id, raw in manifests.items()
    }


def load_jvm_frontend_manifests(
    project: ProjectModel,
    manifest_root: Path,
):
    known = {feature.identity.feature_id: feature for feature in project.features}
    found = {}
    if manifest_root.is_dir():
        for path in sorted(manifest_root.glob("*.json")):
            raw = read_jvm_manifest(path)
            feature_id = raw.feature_id
            if feature_id not in known:
                raise SemanticIRError(
                    f"{path}: KSP emitted semantics for unknown feature {feature_id!r}"
                )
            if feature_id in found:
                raise SemanticIRError(
                    f"multiple JVM semantic manifests were emitted for {feature_id}"
                )
            found[feature_id] = raw
    for feature_id, feature in known.items():
        has_jvm_source = feature.jvm_root.is_dir() and any(
            path.is_file() and path.suffix.lower() in {".kt", ".java"}
            for path in iter_tree_no_follow(feature.jvm_root)
        )
        if has_jvm_source and feature_id not in found:
            raise SemanticIRError(
                f"{feature.identity.npm_name}: JVM source exists but KSP emitted "
                "no semantic manifest; generation cannot commit an incomplete model"
            )
    return found


def discover_semantic_ir(
    project: ProjectModel,
    *,
    jvm_apis: Mapping[str, SemanticApi] | None = None,
) -> SemanticIR:
    jvm_apis = jvm_apis or {}
    known = {feature.identity.feature_id for feature in project.features}
    unknown = sorted(set(jvm_apis) - known)
    if unknown:
        raise SemanticIRError(
            "JVM semantic inputs contain unknown feature identities: "
            + ", ".join(unknown)
        )
    features = []
    for feature in project.features:
        try:
            cpp = (
                binding_codegen.scan_cpp_semantic_model(
                    feature.root, module_name=feature.public_name
                )
                if feature.native_root.is_dir()
                else SemanticApi()
            )
        except binding_codegen.CodegenError as exc:
            raise SemanticIRError(str(exc)) from exc
        has_jvm_source = feature.jvm_root.is_dir() and any(
            path.is_file() and path.suffix.lower() in {".kt", ".java"}
            for path in iter_tree_no_follow(feature.jvm_root)
        )
        if has_jvm_source and feature.identity.feature_id not in jvm_apis:
            raise SemanticIRError(
                f"{feature.identity.npm_name}: complete generation requires "
                "the JVM/KSP semantic frontend output"
            )
        features.append(
            FeatureSemanticIR.create(
                feature.identity,
                public_name=feature.public_name,
                description=feature.description,
                cpp=cpp,
                jvm=jvm_apis.get(feature.identity.feature_id, SemanticApi()),
            )
        )
    return SemanticIR.create(project.plugin_id, features)
