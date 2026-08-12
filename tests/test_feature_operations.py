import json
from pathlib import Path

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import (
    FeatureOperationError,
    FeatureOperationService,
)


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android").mkdir()
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


def test_add_rolls_back_feature_when_registry_projection_fails(tmp_path: Path):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    service.add(config(root, "good"))
    before = registry(root)
    bad = config(root, "bad", StarterFamily.JVM)
    with pytest.raises(FeatureOperationError, match="KSP JVM manifest"):
        service.add(bad)

    assert not bad.output.exists()
    assert registry(root) == before
