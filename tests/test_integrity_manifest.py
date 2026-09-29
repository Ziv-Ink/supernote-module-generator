from __future__ import annotations

from copy import deepcopy

import pytest

from supernote_module_generator.feature_identity import canonical_feature_id
from supernote_module_generator.integrity_manifest import (
    IntegrityManifestError,
    parse_integrity_manifest,
)
from supernote_module_generator.semantic_ir import (
    CPP_FRONTEND_VERSION,
    JVM_FRONTEND_VERSION,
)


def canonical_manifest() -> dict[str, object]:
    generation_id = "a" * 64
    return {
        "schema_version": "2.0",
        "generator_version": "4.0.0",
        "generation_id": generation_id,
        "plugin": {"id": "fixture"},
        "features": [
            {
                "id": canonical_feature_id("alpha"),
                "package_name": "alpha",
                "root": "local_modules/alpha",
                "semantic_hash": "b" * 64,
            }
        ],
        "artifacts": [
            {
                "path": "android/.supernote-module/runtime/ownership.json",
                "owner": "shared-runtime",
                "kind": "runtime-metadata",
                "sha256": "c" * 64,
                "generation_id": generation_id,
                "committed_source": True,
            },
            {
                "path": "local_modules/alpha/.supernote-generated/ownership.json",
                "owner": "feature:alpha",
                "kind": "feature-generated-metadata",
                "sha256": "d" * 64,
                "generation_id": generation_id,
                "committed_source": True,
            },
        ],
        "wiring": [],
        "frontend_versions": {
            "cpp": CPP_FRONTEND_VERSION,
            "jvm": JVM_FRONTEND_VERSION,
        },
    }


def test_strict_manifest_parser_round_trips_one_canonical_value():
    raw = canonical_manifest()

    assert parse_integrity_manifest(raw).manifest() == raw


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update({"unexpected": True, "schema_version": 3}),
            "integrity manifest fields are invalid",
        ),
        (
            lambda value: value.update(
                {"generator_version": "not-semver", "generation_id": "invalid"}
            ),
            "generator_version must be canonical SemVer",
        ),
        (
            lambda value: value.update({"features": {}, "artifacts": {}}),
            "features must be a list",
        ),
        (
            lambda value: value.update({"wiring": {}, "features": [{}, {}]}),
            "wiring must be a list",
        ),
    ],
)
def test_manifest_phase_validation_order_is_stable(mutate, message: str):
    raw = canonical_manifest()
    mutate(raw)

    with pytest.raises(IntegrityManifestError, match=message):
        parse_integrity_manifest(raw)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"path": "../escaped", "owner": "", "kind": ""},
            r"artifacts\[0\]\.path must be canonical and relative",
        ),
        (
            {"owner": "", "kind": ""},
            r"artifacts\[0\] owner is invalid",
        ),
        (
            {"generation_id": "0" * 64, "committed_source": False},
            r"artifacts\[0\] generation identity disagrees with manifest",
        ),
            (
                {
                    "mode": True,
                },
            r"artifacts\[0\] mode is invalid",
        ),
    ],
)
def test_artifact_field_validation_order_is_stable(
    updates: dict[str, object], message: str
):
    raw = canonical_manifest()
    artifacts = raw["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact.update(deepcopy(updates))

    with pytest.raises(IntegrityManifestError, match=message):
        parse_integrity_manifest(raw)


def _set_nested(value: object, path: tuple[object, ...], replacement: object) -> None:
    current = value
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    current[path[-1]] = replacement  # type: ignore[index]


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        ((), None, "integrity manifest must be a JSON object"),
        (("schema_version",), "1.0", "schema is incompatible"),
        (("generator_version",), 1, "generator_version must be canonical SemVer"),
        (("generator_version",), "1.0.0-01", "generator_version must be canonical SemVer"),
        (("generation_id",), "A" * 64, "generation_id must be a lowercase SHA-256"),
        (("plugin",), [], "plugin identity is invalid"),
        (("plugin",), {"id": ""}, "plugin identity is invalid"),
        (("frontend_versions",), {}, "frontend versions are incompatible"),
        (("artifacts",), {}, "artifacts must be a list"),
        (("features", 0), None, r"features\[0\] fields are invalid"),
        (("features", 0, "package_name"), "", r"features\[0\] package name is invalid"),
        (("features", 0, "id"), "wrong", r"features\[0\] identity is not canonical"),
        (("features", 0, "root"), "local_modules/wrong", r"features\[0\] root is not canonical"),
        (("features", 0, "semantic_hash"), "bad", r"features\[0\]\.semantic_hash"),
        (("artifacts", 0), None, r"artifacts\[0\] fields are invalid"),
        (("artifacts", 0, "path"), 1, r"artifacts\[0\]\.path must be a string"),
        (("artifacts", 0, "owner"), "", r"artifacts\[0\] owner is invalid"),
        (("artifacts", 0, "kind"), "", r"artifacts\[0\] kind is invalid"),
        (("artifacts", 0, "sha256"), "bad", r"artifacts\[0\]\.sha256"),
        (("artifacts", 0, "generation_id"), "0" * 64, "generation identity disagrees"),
        (("artifacts", 0, "committed_source"), False, "must describe committed source"),
        (("artifacts", 0, "mode"), -1, r"artifacts\[0\] mode is invalid"),
        (("artifacts", 0, "mode"), 0o10000, r"artifacts\[0\] mode is invalid"),
        (("artifacts", 0, "path"), ".supernote-module/manifest.json", "cannot own itself"),
        (("artifacts", 0, "path"), "outside.txt", "shared-runtime path is outside"),
        (("artifacts", 1, "owner"), "feature:missing", "feature ownership is inconsistent"),
        (("artifacts", 1, "path"), "outside.txt", "feature ownership is inconsistent"),
        (("artifacts", 0, "owner"), "plugin-global", "plugin-global ownership overlaps"),
        (("artifacts", 0, "owner"), "unsupported", "owner is unsupported"),
    ],
)
def test_manifest_rejects_each_public_identity_and_ownership_boundary(
    path: tuple[object, ...], replacement: object, message: str
) -> None:
    raw: object = canonical_manifest()
    if path:
        _set_nested(raw, path, replacement)
    else:
        raw = replacement

    with pytest.raises(IntegrityManifestError, match=message):
        parse_integrity_manifest(raw)


def _add_beta(raw: dict[str, object]) -> None:
    generation_id = raw["generation_id"]
    features = raw["features"]
    artifacts = raw["artifacts"]
    assert isinstance(generation_id, str)
    assert isinstance(features, list)
    assert isinstance(artifacts, list)
    features.append(
        {
            "id": canonical_feature_id("beta"),
            "package_name": "beta",
            "root": "local_modules/beta",
            "semantic_hash": "e" * 64,
        }
    )
    artifacts.append(
        {
            "path": "local_modules/beta/.supernote-generated/ownership.json",
            "owner": "feature:beta",
            "kind": "feature-generated-metadata",
            "sha256": "f" * 64,
            "generation_id": generation_id,
            "committed_source": True,
        }
    )
    artifacts.sort(key=lambda item: item["path"])


def test_manifest_rejects_duplicate_and_noncanonical_record_order() -> None:
    duplicate_feature = canonical_manifest()
    duplicate_feature["features"].append(deepcopy(duplicate_feature["features"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(IntegrityManifestError, match="identities and roots must be unique"):
        parse_integrity_manifest(duplicate_feature)

    unordered_features = canonical_manifest()
    _add_beta(unordered_features)
    unordered_features["features"].reverse()  # type: ignore[union-attr]
    with pytest.raises(IntegrityManifestError, match="features must be canonically ordered"):
        parse_integrity_manifest(unordered_features)

    duplicate_artifact = canonical_manifest()
    duplicate_artifact["artifacts"].append(  # type: ignore[union-attr]
        deepcopy(duplicate_artifact["artifacts"][0])  # type: ignore[index]
    )
    with pytest.raises(IntegrityManifestError, match="artifact paths must be unique"):
        parse_integrity_manifest(duplicate_artifact)

    unordered_artifacts = canonical_manifest()
    unordered_artifacts["artifacts"].reverse()  # type: ignore[union-attr]
    with pytest.raises(IntegrityManifestError, match="artifacts must be canonically ordered"):
        parse_integrity_manifest(unordered_artifacts)


def test_manifest_requires_feature_and_runtime_ownership_anchors() -> None:
    missing_feature = canonical_manifest()
    missing_feature["artifacts"].pop()  # type: ignore[union-attr]
    with pytest.raises(IntegrityManifestError, match="lacks canonical metadata ownership"):
        parse_integrity_manifest(missing_feature)

    missing_runtime = canonical_manifest()
    missing_runtime["artifacts"].pop(0)  # type: ignore[union-attr]
    with pytest.raises(IntegrityManifestError, match="canonical shared-runtime"):
        parse_integrity_manifest(missing_runtime)


def _wiring(path: str, marker: str = "sn-module-gen-runtime") -> dict[str, str]:
    return {"path": path, "marker": marker, "sha256": "1" * 64}


def test_manifest_rejects_invalid_duplicate_and_unordered_wiring() -> None:
    cases = [
        ([None], r"wiring\[0\] fields are invalid"),
        ([{"path": "x", "marker": "bad", "sha256": "1" * 64}], "marker is invalid"),
        ([_wiring("../x")], r"wiring\[0\]\.path must be canonical"),
        ([_wiring("x") | {"sha256": "bad"}], r"wiring\[0\]\.sha256"),
        ([_wiring("a"), _wiring("a")], "wiring records must be unique"),
        (
            [_wiring("a"), _wiring("a", "supernote-module-package")],
            "each wiring path may have only one owned block",
        ),
        ([_wiring("b"), _wiring("a")], "wiring records must be canonically ordered"),
    ]
    for wiring, message in cases:
        raw = canonical_manifest()
        raw["wiring"] = wiring
        with pytest.raises(IntegrityManifestError, match=message):
            parse_integrity_manifest(raw)


def test_manifest_round_trip_keeps_optional_mode_and_supported_wiring() -> None:
    raw = canonical_manifest()
    raw["artifacts"][0]["mode"] = 0o640  # type: ignore[index]
    raw["wiring"] = [_wiring("android/settings.gradle")]

    assert parse_integrity_manifest(raw).manifest() == raw
