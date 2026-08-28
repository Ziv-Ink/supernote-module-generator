from dataclasses import replace

import pytest

from supernote_module_generator.feature_model import (
    FEATURE_MANIFEST_KIND,
    FEATURE_MANIFEST_SCHEMA_VERSION,
    FeatureManifest,
    FeatureModelError,
    FeatureRegistryEntry,
    ImplementationFamily,
    PluginRuntimeRegistry,
    feature_identity,
)
from supernote_module_generator.semantic import (
    BindingCapabilities,
    BindingKind,
    ExecutionMode,
    SemanticApi,
    SemanticBinding,
    SemanticParameter,
    SemanticType,
    SourceProvenance,
)


def binding(name: str, *, language: str, public: bool, async_: bool = False):
    source = SourceProvenance(
        declaration_id=f"{language}:{name}",
        language=language,
        path=f"{name}.{'cpp' if language == 'cpp' else 'kt'}",
        line=1,
    )
    return SemanticBinding(
        binding_id=f"binding:{language}:{name}",
        kind=BindingKind.FUNCTION,
        name=name,
        capabilities=BindingCapabilities(True, public),
        execution=ExecutionMode.ASYNC if async_ else ExecutionMode.SYNC,
        parameters=(SemanticParameter("value", SemanticType.INT32),),
        result=SemanticType.INT32,
        source=source,
    )


def feature(name: str, *starter_files: str) -> FeatureManifest:
    return FeatureManifest.create(
        npm_name=f"@local/{name}",
        public_name=name.title(),
        android_namespace=f"com.example.{name}",
        starter_files=starter_files,
    )


def test_feature_manifest_is_language_neutral_and_starters_are_bookkeeping():
    manifest = feature("document", "android/src/main/cpp/document.cpp")
    value = manifest.manifest()

    assert value["schema_version"] == FEATURE_MANIFEST_SCHEMA_VERSION
    assert value["kind"] == FEATURE_MANIFEST_KIND
    assert value["implementation_roots"] == {
        "native": "android/src/main/cpp",
        "jvm": "android/src/main/java",
    }
    assert value["starter_files"] == ["android/src/main/cpp/document.cpp"]
    assert "backend" not in value
    assert "implementation" not in value
    assert "starter_families" not in value


def test_requirements_come_from_semantics_and_may_mix_languages():
    api = SemanticApi(
        functions=(
            binding("parse", language="cpp", public=False),
            binding("load", language="kotlin", public=True, async_=True),
        )
    )
    entry = FeatureRegistryEntry.create(feature("document"), api)

    assert entry.requirements.families == (
        ImplementationFamily.NATIVE,
        ImplementationFamily.JVM,
    )
    assert entry.requirements.javascript_public
    assert entry.requirements.asynchronous


def test_starter_selection_does_not_change_derived_requirements_or_identity():
    api = SemanticApi(functions=(binding("read", language="cpp", public=True),))
    native = FeatureRegistryEntry.create(
        feature("document", "android/src/main/cpp/document.cpp"), api
    )
    both = FeatureRegistryEntry.create(
        feature(
            "document",
            "android/src/main/cpp/document.cpp",
            "android/src/main/java/Document.kt",
        ),
        api,
    )

    assert native.feature.feature_id == both.feature.feature_id
    assert native.requirements == both.requirements
    assert native.semantic_digest == both.semantic_digest


def test_plugin_registry_has_one_stable_component_and_deterministic_order():
    first = FeatureRegistryEntry.create(
        feature("alpha"),
        SemanticApi(functions=(binding("read", language="cpp", public=True),)),
    )
    second = FeatureRegistryEntry.create(
        feature("beta"),
        SemanticApi(functions=(binding("write", language="java", public=True),)),
    )
    left = PluginRuntimeRegistry.create(
        plugin_id="com.example.plugin",
        generator_version="4.0.0.dev0",
        features=(second, first),
    )
    right = PluginRuntimeRegistry.create(
        plugin_id="com.example.plugin",
        generator_version="4.0.0.dev0",
        features=(first, second),
    )

    assert left == right
    assert left.component_name.startswith("sn_supernote_runtime_")
    assert [item["feature_id"] for item in left.manifest()["features"]] == sorted(
        [feature_identity("@local/alpha"), feature_identity("@local/beta")]
    )
    assert "backend" not in repr(left.manifest())


def test_removing_one_registry_entry_preserves_component_and_other_feature():
    alpha = FeatureRegistryEntry.create(feature("alpha"), SemanticApi())
    beta = FeatureRegistryEntry.create(feature("beta"), SemanticApi())
    full = PluginRuntimeRegistry.create(
        plugin_id="plugin", generator_version="4.0.0.dev0", features=(alpha, beta)
    )
    reduced = PluginRuntimeRegistry.create(
        plugin_id="plugin", generator_version="4.0.0.dev0", features=(beta,)
    )

    assert full.component_name == reduced.component_name
    assert reduced.features == (beta,)


@pytest.mark.parametrize(
    "change, diagnostic",
    (
        ({"schema_version": 1}, "unsupported feature manifest schema"),
        ({"starter_files": ("../escape.cpp",)}, "normalized relative path"),
        ({"starter_files": ("other/file.cpp",)}, "outside the implementation roots"),
    ),
)
def test_feature_manifest_rejects_invalid_ownership(change, diagnostic):
    with pytest.raises(FeatureModelError, match=diagnostic):
        replace(feature("document"), **change)


def test_registry_rejects_duplicate_feature_public_identity():
    alpha = FeatureRegistryEntry.create(feature("alpha"), SemanticApi())
    duplicate = FeatureRegistryEntry.create(
        replace(feature("beta"), public_name=alpha.feature.public_name),
        SemanticApi(),
    )
    with pytest.raises(FeatureModelError, match="duplicate feature public name"):
        PluginRuntimeRegistry.create(
            plugin_id="plugin",
            generator_version="4.0.0.dev0",
            features=(alpha, duplicate),
        )
