from __future__ import annotations

from copy import deepcopy

import pytest

from supernote_module_generator.feature_identity import canonical_feature_id
from supernote_module_generator.integrity_manifest import (
    IntegrityManifestError,
    TEMPLATE_CAPABILITY_VERSION,
    parse_integrity_manifest,
)
from supernote_module_generator.semantic_ir import (
    CPP_FRONTEND_VERSION,
    JVM_FRONTEND_VERSION,
)


def canonical_manifest() -> dict[str, object]:
    generation_id = "a" * 64
    return {
        "schema_version": 4,
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
                "path": "android/.supernote-module/v4-runtime/ownership.json",
                "owner": "shared-runtime",
                "kind": "runtime-metadata",
                "sha256": "c" * 64,
                "generation_id": generation_id,
                "committed_source": True,
            },
            {
                "path": "local_modules/alpha/.supernote-module.json",
                "owner": "feature:alpha",
                "kind": "feature-metadata",
                "sha256": "d" * 64,
                "generation_id": generation_id,
                "committed_source": True,
            },
        ],
        "wiring": [],
        "template_capability": TEMPLATE_CAPABILITY_VERSION,
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
                "path": "local_modules/alpha/runtime.cpp",
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
