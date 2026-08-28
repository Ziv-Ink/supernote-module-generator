from __future__ import annotations

import json
from pathlib import Path

import pytest

from supernote_module_generator.errors import ConfigurationError, ValidationError
from supernote_module_generator.feature_identity import (
    FeatureIdentity,
    canonical_feature_id,
)


def test_unscoped_and_scoped_identity_derive_exact_canonical_paths(tmp_path: Path):
    unscoped = FeatureIdentity.create(
        npm_name="drawing",
        android_namespace="com.example.drawing",
        package_version="4.0.0",
    )
    scoped = FeatureIdentity.create(
        npm_name="@ziv/drawing",
        android_namespace="com.example.scoped_drawing",
        package_version="4.0.0-rc.1",
    )

    assert unscoped.feature_id == canonical_feature_id("drawing")
    assert unscoped.relative_root.as_posix() == "local_modules/drawing"
    assert scoped.relative_root.as_posix() == "local_modules/@ziv/drawing"
    assert scoped.destination(tmp_path) == tmp_path / "local_modules/@ziv/drawing"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"npm_name": "../escape"}, "package name"),
        ({"npm_name": "@scope/name/extra"}, "package name"),
        ({"android_namespace": "com.class.feature"}, "namespace"),
        ({"package_version": "4.0"}, "version"),
        ({"feature_id": "supernote:feature:wrong"}, "identity mismatch"),
    ],
)
def test_identity_rejects_every_invalid_field(
    change: dict[str, str],
    expected: str,
):
    values = {
        "npm_name": "drawing",
        "android_namespace": "com.example.drawing",
        "package_version": "4.0.0",
        "feature_id": canonical_feature_id("drawing"),
    }
    values.update(change)

    with pytest.raises((ConfigurationError, ValidationError), match=expected):
        FeatureIdentity.create(**values)


def test_identity_rejects_noncanonical_manifest_directory(tmp_path: Path):
    identity = FeatureIdentity.create(
        npm_name="drawing",
        android_namespace="com.example.drawing",
        package_version="4.0.0",
    )
    wrong = tmp_path / "local_modules/vendor/drawing"

    with pytest.raises(ConfigurationError, match="noncanonical directory"):
        identity.validate_directory(tmp_path, wrong)


def test_identity_activates_generated_path_length_guard(tmp_path: Path):
    identity = FeatureIdentity.create(
        npm_name="local-" + "a" * 114,
        android_namespace="com.example." + "b" * 60,
        package_version="4.0.0",
    )

    with pytest.raises(ValidationError, match="180-character"):
        identity.destination(tmp_path)


def test_identity_json_fields_are_deterministic():
    identity = FeatureIdentity.create(
        npm_name="@ziv/drawing",
        android_namespace="com.example.drawing",
        package_version="4.0.0",
    )

    encoded = json.dumps(identity.__dict__, sort_keys=True, separators=(",", ":"))
    assert encoded == json.dumps(identity.__dict__, sort_keys=True, separators=(",", ":"))
