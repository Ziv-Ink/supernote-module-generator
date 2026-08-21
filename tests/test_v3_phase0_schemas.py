import json
from dataclasses import replace

import pytest

from supernote_module_generator.feature_model import (
    FeatureManifest,
    FeatureModelError,
    PluginRuntimeRegistry,
)
from supernote_module_generator.jvm_manifest import (
    JvmManifestError,
    JvmSourceManifest,
    read_jvm_manifest,
)
from supernote_module_generator.plugin_runtime_codegen import generated_runtime_files
from supernote_module_generator.semantic import (
    SemanticApi,
    SemanticModelError,
    semantic_api_from_manifest,
)
from supernote_module_generator.v3_schemas import (
    FEATURE_MANIFEST_KIND,
    FEATURE_MANIFEST_SCHEMA_VERSION,
    GENERATED_OWNERSHIP_KIND,
    GENERATED_OWNERSHIP_SCHEMA_VERSION,
    JVM_SOURCE_MANIFEST_KIND,
    JVM_SOURCE_MANIFEST_SCHEMA_VERSION,
    PLUGIN_REGISTRY_KIND,
    PLUGIN_REGISTRY_SCHEMA_VERSION,
    SEMANTIC_MANIFEST_KIND,
    SEMANTIC_MANIFEST_SCHEMA_VERSION,
)


def test_every_phase0_generated_boundary_has_an_explicit_v3_identity():
    assert (
        SEMANTIC_MANIFEST_SCHEMA_VERSION,
        JVM_SOURCE_MANIFEST_SCHEMA_VERSION,
        FEATURE_MANIFEST_SCHEMA_VERSION,
        PLUGIN_REGISTRY_SCHEMA_VERSION,
        GENERATED_OWNERSHIP_SCHEMA_VERSION,
    ) == (3, 3, 3, 2, 2)
    assert {
        SEMANTIC_MANIFEST_KIND,
        JVM_SOURCE_MANIFEST_KIND,
        FEATURE_MANIFEST_KIND,
        PLUGIN_REGISTRY_KIND,
        GENERATED_OWNERSHIP_KIND,
    } == {
        "supernote_v3_semantic_manifest",
        "supernote_v3_jvm_source_manifest",
        "supernote_v3_feature",
        "supernote_v3_plugin_runtime_registry",
        "supernote_v3_plugin_runtime_ownership",
    }


def test_v2_schema_versions_are_rejected_instead_of_converted(tmp_path):
    semantic = SemanticApi().manifest()
    for stale_schema in (1, 2, 99):
        semantic["schema_version"] = stale_schema
        with pytest.raises(SemanticModelError, match="incompatible semantic manifest"):
            semantic_api_from_manifest(semantic)

    jvm = JvmSourceManifest("supernote:feature:phase0", "3.0.0.dev0", ())
    raw = jvm.manifest()
    raw["schema_version"] = 1
    path = tmp_path / "jvm.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(JvmManifestError, match="incompatible JVM manifest"):
        read_jvm_manifest(path)

    feature = FeatureManifest.create(
        npm_name="phase0",
        public_name="Phase0",
        android_namespace="com.example.phase0",
    )
    with pytest.raises(FeatureModelError, match="unsupported feature manifest"):
        replace(feature, schema_version=2)

    registry = PluginRuntimeRegistry.create(
        plugin_id="phase0",
        generator_version="3.0.0.dev0",
        features=(),
    )
    with pytest.raises(FeatureModelError, match="unsupported plugin registry"):
        replace(registry, schema_version=1)


def test_generated_registry_and_ownership_use_only_v3_schemas():
    registry = PluginRuntimeRegistry.create(
        plugin_id="phase0",
        generator_version="3.0.0.dev0",
        features=(),
    )
    files = generated_runtime_files(registry)
    registry_json = json.loads(files["feature-registry.json"])
    ownership_json = json.loads(files["ownership.json"])

    assert registry_json["schema_version"] == PLUGIN_REGISTRY_SCHEMA_VERSION
    assert registry_json["kind"] == PLUGIN_REGISTRY_KIND
    assert ownership_json["schema_version"] == GENERATED_OWNERSHIP_SCHEMA_VERSION
    assert ownership_json["kind"] == GENERATED_OWNERSHIP_KIND
    assert "supernote_v2" not in files["feature-registry.json"]
    assert "supernote_v2" not in files["ownership.json"]
