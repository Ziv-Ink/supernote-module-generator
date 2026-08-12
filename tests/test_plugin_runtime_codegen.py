import json
from pathlib import Path

from supernote_module_generator.feature_model import (
    FeatureManifest,
    FeatureRegistryEntry,
    PluginRuntimeRegistry,
)
from supernote_module_generator.plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    activate_plugin_runtime,
    generate_plugin_runtime,
    restore_plugin_runtime,
    stage_plugin_runtime,
)
from supernote_module_generator.semantic import SemanticApi


def entry(name: str) -> FeatureRegistryEntry:
    feature = FeatureManifest.create(
        npm_name=f"@local/{name}",
        public_name=name.title(),
        android_namespace=f"com.example.{name}",
    )
    return FeatureRegistryEntry.create(feature, SemanticApi())


def registry(*names: str) -> PluginRuntimeRegistry:
    return PluginRuntimeRegistry.create(
        plugin_id="com.example.plugin",
        generator_version="2.0.0.dev0",
        features=(entry(name) for name in names),
    )


def test_generates_one_compiled_runtime_component_for_all_features(tmp_path: Path):
    generated = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    cmake = (generated / "CMakeLists.txt").read_text()
    services = (generated / "src/runtime_services.cpp").read_text()
    source = (generated / "src/feature_registry.cpp").read_text()

    assert cmake.count("add_library(") == 1
    assert "runtime_services.cpp" in cmake
    assert "feature_registry.cpp" in cmake
    assert "alpha" not in cmake
    assert "beta" not in cmake
    assert services.count("static RuntimeServices services") == 1
    assert "supernote:feature:" in source
    assert '"Alpha"' in source
    assert '"Beta"' in source


def test_registry_and_ownership_are_deterministic(tmp_path: Path):
    first = generate_plugin_runtime(tmp_path, registry("beta", "alpha"))
    snapshot = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    repeated = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    ownership = json.loads((second / "ownership.json").read_text())

    assert snapshot == repeated
    assert set(ownership["generated_files"]) == set(snapshot)


def test_removing_feature_regenerates_registry_without_replacing_component(tmp_path: Path):
    full = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    component = json.loads((full / "feature-registry.json").read_text())[
        "component_name"
    ]
    reduced = generate_plugin_runtime(tmp_path, registry("beta"))
    value = json.loads((reduced / "feature-registry.json").read_text())

    assert value["component_name"] == component
    assert [item["public_name"] for item in value["features"]] == ["Beta"]
    assert "Alpha" not in (reduced / "src/feature_registry.cpp").read_text()


def test_activation_can_restore_previous_shared_component(tmp_path: Path):
    destination = generate_plugin_runtime(tmp_path, registry("alpha"))
    original = (destination / "feature-registry.json").read_bytes()
    staged = stage_plugin_runtime(tmp_path, registry("beta"))
    backup = activate_plugin_runtime(staged, tmp_path)
    assert backup is not None
    assert b'"Beta"' in (destination / "feature-registry.json").read_bytes()

    restore_plugin_runtime(tmp_path, backup)
    assert (tmp_path / RUNTIME_RELATIVE_ROOT / "feature-registry.json").read_bytes() == original
