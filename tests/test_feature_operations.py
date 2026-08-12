import json
from pathlib import Path

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text(
        "rootProject.name = 'fixture'\n"
    )
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text('{"name":"test-plugin"}\n')
    return tmp_path


def config(root: Path, name: str, *starters: StarterFamily) -> FeatureConfig:
    return FeatureConfig(
        output=root / "local_modules" / name,
        npm_name=name,
        package_version="2.0.0.dev0",
        android_namespace=f"com.example.{name}",
        public_name=name.title(),
        starters=starters or (StarterFamily.NATIVE,),
    )


def registry(root: Path) -> dict:
    return json.loads(
        (
            root
            / "android/.supernote-module/v2-runtime/feature-registry.json"
        ).read_text()
    )


def test_add_update_remove_regenerate_one_shared_registry(tmp_path: Path):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    alpha = service.add(config(root, "alpha"))
    beta = service.add(config(root, "beta"))

    assert [item["public_name"] for item in registry(root)["features"]] == [
        "Alpha",
        "Beta",
    ]
    component = registry(root)["component_name"]
    (alpha / "android/src/main/cpp/custom.cpp").write_text(
        "double helper() { return 1; }\n"
    )
    service.update("alpha")
    assert (alpha / "android/src/main/cpp/custom.cpp").is_file()
    assert registry(root)["component_name"] == component

    service.remove("alpha")
    assert not alpha.exists()
    assert beta.exists()
    assert [item["public_name"] for item in registry(root)["features"]] == ["Beta"]
    assert registry(root)["component_name"] == component


def test_jvm_only_feature_is_scaffolded_for_ksp_without_python_source_parsing(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    jvm = config(root, "jvm", StarterFamily.JVM)
    created = service.add(jvm)

    assert created == jvm.output
    assert not (created / "android/src/main/cpp").exists()
    assert (created / "android/src/main/java/com/example/jvm/FeatureApi.kt").is_file()
    gradle = (root / "android/.supernote-module/v2-runtime/build.gradle").read_text()
    assert "local_modules/jvm/android/src/main/java" in gradle
    assert "com.google.devtools.ksp" in gradle


def test_removing_last_feature_removes_shared_component_and_wiring(tmp_path: Path):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    service.add(config(root, "only"))
    assert "supernote-v2-runtime" in (
        root / "android/settings.gradle"
    ).read_text()

    service.remove("only")

    assert not (root / "android/.supernote-module/v2-runtime").exists()
    assert "supernote-v2-runtime" not in (
        root / "android/settings.gradle"
    ).read_text()
    assert "supernote-v2-runtime" not in (
        root / "android/app/build.gradle"
    ).read_text()
