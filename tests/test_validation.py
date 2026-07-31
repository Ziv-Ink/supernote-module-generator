from pathlib import Path

import pytest

from supernote_module_generator.config import (
    ProjectConfig,
    native_class_prefix,
    normalize_backend,
)
from supernote_module_generator.errors import DestinationConflict, ValidationError
from supernote_module_generator.validation import package_path, validate_config, validate_npm_name, validate_semver


@pytest.mark.parametrize("name", ["react-native-x", "@scope/react-native-x", "a1"])
def test_valid_npm_names(name): validate_npm_name(name)


@pytest.mark.parametrize("name", ["React Native", "@scope/", "../x", "x/"])
def test_invalid_npm_names(name):
    with pytest.raises(ValidationError): validate_npm_name(name)


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3-alpha.1", "1.2.3+build.4"])
def test_valid_semver(version): validate_semver(version)


@pytest.mark.parametrize("version", ["1", "1.2", "01.2.3", "v1.2.3"])
def test_invalid_semver(version):
    with pytest.raises(ValidationError): validate_semver(version)


def test_package_path(): assert package_path("com.example.localmath") == "com/example/localmath"


def test_legacy_cpp_backend_normalizes_to_jni():
    assert normalize_backend("cpp") == "jni"
    config = ProjectConfig(
        Path("local_modules/local-jni"),
        "local-jni",
        "0.1.0",
        "com.example.localjni",
        "LocalJni",
        backend="cpp",
        native_library_name="sn_local_jni",
    )
    assert config.backend == "jni"
    assert config.metadata()["backend"] == "jni"


@pytest.mark.parametrize("name", ["class", "await", "default"])
def test_javascript_reserved_module_names_are_rejected(name):
    config = ProjectConfig(
        Path(f"local_modules/{name}"),
        f"local-{name}",
        "0.1.0",
        "com.example.safe",
        name,
    )
    with pytest.raises(ValidationError, match="non-reserved"):
        validate_config(config)


@pytest.mark.parametrize(
    "namespace",
    [
        "com.native.module",
        "com.synchronized.module",
        "com.boolean.module",
        "com.throws.module",
        "com.when.module",
        "com.typealias.module",
    ],
)
def test_java_and_kotlin_namespace_keywords_are_rejected(
    tmp_path: Path, namespace: str
):
    config = ProjectConfig(
        tmp_path / "module",
        "local-module",
        "0.1.0",
        namespace,
        "LocalModule",
    )
    with pytest.raises(ValidationError, match="namespace"):
        validate_config(config)


def test_generated_class_prefix_never_starts_with_digit():
    assert native_class_prefix("123-native") == "Module123Native"


def test_refuses_unrelated_existing_directory(tmp_path: Path):
    target = tmp_path / "project"; target.mkdir(); (target / "mine").write_text("x")
    config = ProjectConfig(target, "react-native-x", "0.1.0", "com.example.x", "LocalX", description="x")
    with pytest.raises(DestinationConflict): validate_config(config)
