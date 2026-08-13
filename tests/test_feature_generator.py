import json
from pathlib import Path

from supernote_module_generator.feature_generator import FeatureConfig, generate_feature, stage_feature
from supernote_module_generator.feature_model import StarterFamily


def config(tmp_path: Path, *starters: StarterFamily) -> FeatureConfig:
    return FeatureConfig(
        output=tmp_path / "document",
        npm_name="@local/document",
        package_version="2.0.0.dev0",
        android_namespace="com.example.document",
        public_name="Document",
        starters=starters or (StarterFamily.NATIVE,),
    )


def test_native_and_jvm_starters_create_user_source_without_backend_metadata(tmp_path: Path):
    feature = generate_feature(
        config(tmp_path, StarterFamily.NATIVE, StarterFamily.JVM)
    )
    metadata = json.loads((feature / ".supernote-module.json").read_text())

    assert (feature / "android/src/main/cpp/feature.cpp").is_file()
    assert (
        feature
        / "android/src/main/java/com/example/document/FeatureApi.kt"
    ).is_file()
    assert metadata["implementation_roots"] == {
        "native": "android/src/main/cpp",
        "jvm": "android/src/main/java",
    }
    assert "backend" not in metadata
    assert "type" not in metadata
    assert "starters" not in metadata
    assert not (feature / "android/build.gradle.kts").exists()
    assert not (feature / "android/CMakeLists.txt").exists()


def test_update_preserves_added_and_deleted_user_starter_files(tmp_path: Path):
    feature = generate_feature(config(tmp_path, StarterFamily.NATIVE))
    starter = feature / "android/src/main/cpp/feature.cpp"
    starter.unlink()
    custom = feature / "android/src/main/java/com/example/document/Custom.java"
    custom.parent.mkdir(parents=True)
    custom.write_text("class Custom {}\n")

    staged = stage_feature(
        config(tmp_path, StarterFamily.NATIVE), preserve_sources_from=feature
    )
    assert not (staged / "android/src/main/cpp/feature.cpp").exists()
    assert (staged / custom.relative_to(feature)).read_text() == "class Custom {}\n"


def test_update_preserves_last_generated_types_until_common_codegen_runs(tmp_path: Path):
    feature = generate_feature(config(tmp_path, StarterFamily.JVM))
    generated_types = "export interface DocumentFeature { pageCount(): number; }\n"
    (feature / "index.d.ts").write_text(generated_types, encoding="utf-8")

    staged = stage_feature(
        config(tmp_path, StarterFamily.JVM), preserve_sources_from=feature
    )

    assert (staged / "index.d.ts").read_text(encoding="utf-8") == generated_types


def test_feature_readme_explains_language_neutral_explicit_intent_model(
    tmp_path: Path,
):
    feature = generate_feature(config(tmp_path))
    readme = (feature / "README.md").read_text(encoding="utf-8")
    package = json.loads((feature / "package.json").read_text())

    assert "C/C++ and Kotlin/Java source may coexist" in readme
    assert "Ordinary" in readme
    assert "SupernotePluginExport" in readme
    assert "SupernotePluginInternal" in readme
    assert "SupernotePluginAsync" in readme
    assert "README.md" in package["files"]


def test_feature_package_uses_shared_runtime_proxy_and_no_native_package(tmp_path: Path):
    feature = generate_feature(config(tmp_path))
    index = (feature / "index.js").read_text()
    package = json.loads((feature / "package.json").read_text())

    assert "globalThis.__supernoteV2" in index
    assert "runtime.feature(" in index
    assert package["main"] == "index.js"
    assert "react-native" not in package
