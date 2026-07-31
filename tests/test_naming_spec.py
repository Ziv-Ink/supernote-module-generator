from __future__ import annotations

from pathlib import Path

import pytest

from supernote_module_generator.errors import ValidationError
from supernote_module_generator.naming import (
    infer_android_namespace,
    infer_javascript_name,
    normalize_description,
    source_tokens,
    strip_ascii,
    validate_android_namespace,
    validate_generated_paths,
    validate_javascript_name,
    validate_package_name,
    validate_package_version,
)


@pytest.mark.parametrize(
    ("package", "tokens"),
    [
        ("local-math", ["math"]),
        ("react-native-file-tools", ["file", "tools"]),
        ("@scope/local-jsi-math", ["jsi", "math"]),
        ("network.cache", ["network", "cache"]),
    ],
)
def test_source_token_examples(package: str, tokens: list[str]):
    assert source_tokens(package) == tokens


@pytest.mark.parametrize(
    ("package", "javascript", "namespace"),
    [
        ("local-math", "Math", "com.example.math"),
        ("local-http-client", "HttpClient", "com.example.http_client"),
        ("@scope/local-jsi", "Jsi", "com.example.jsi"),
        ("network.cache", "NetworkCache", "com.example.network_cache"),
    ],
)
def test_stable_inference(package: str, javascript: str, namespace: str):
    assert infer_javascript_name(package) == javascript
    assert infer_android_namespace(package) == namespace


def test_invalid_numeric_inference_names_exact_field():
    with pytest.raises(ValidationError, match="JavaScript name"):
        infer_javascript_name("123-math")
    with pytest.raises(ValidationError, match="Android namespace"):
        infer_android_namespace("123-math")


@pytest.mark.parametrize(
    "value",
    ["Upper", "has space", "../escape", "name/extra/leaf", "a" * 215, "naïve"],
)
def test_invalid_package_names(value: str):
    with pytest.raises(ValidationError):
        validate_package_name(value)


def test_scoped_package_length_and_grammar():
    validate_package_name("@scope/local-math")


@pytest.mark.parametrize("value", ["class", "1Math", "Math-Dash", "Math\u202e"])
def test_invalid_javascript_names(value: str):
    with pytest.raises(ValidationError):
        validate_javascript_name(value)


@pytest.mark.parametrize("value", ["com.class.math", "com.1math", "single", "com.ex-ample.math"])
def test_invalid_android_namespaces(value: str):
    with pytest.raises(ValidationError):
        validate_android_namespace(value)


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "v1.0.0", "1.0.0-"])
def test_invalid_semver(value: str):
    with pytest.raises(ValidationError):
        validate_package_version(value)


def test_description_is_trimmed_and_control_characters_rejected():
    assert normalize_description("  hello  ") == "hello"
    with pytest.raises(ValidationError):
        normalize_description("hello\nworld")
    with pytest.raises(ValidationError):
        normalize_description("hello\u202eworld")


def test_only_surrounding_ascii_whitespace_is_normalized():
    assert strip_ascii("  local-math\t").value == "local-math"
    assert strip_ascii("local math").value == "local math"


def test_generated_path_budgets(tmp_path: Path):
    validate_generated_paths(tmp_path, "local-math", "com.example.math")
    with pytest.raises(ValidationError, match="180-character"):
        validate_generated_paths(tmp_path, "local-" + "a" * 119, "com.example." + "b" * 60)
