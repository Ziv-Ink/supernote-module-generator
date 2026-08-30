from __future__ import annotations

import json
from pathlib import Path

import pytest

from supernote_module_generator.feature_identity import FeatureIdentity
from supernote_module_generator.generation_plan import (
    ArtifactAction,
    DependencyAction,
    GenerationPlan,
    GenerationPlanError,
    OwnedArtifact,
)
from supernote_module_generator.integrity_manifest import (
    IntegrityManifest,
    ManifestFeature,
)
from supernote_module_generator.semantic import SemanticApi
from supernote_module_generator.semantic_ir import FeatureSemanticIR, SemanticIR


def identity(name: str) -> FeatureIdentity:
    return FeatureIdentity.create(
        npm_name=name,
        android_namespace=f"com.example.{name}",
        package_version="4.0.0-dev.0",
    )


def test_semantic_ir_round_trips_deterministically_and_has_stable_generation_id():
    first = FeatureSemanticIR.create(identity("alpha"), cpp=SemanticApi())
    second = FeatureSemanticIR.create(identity("beta"), jvm=SemanticApi())
    ir = SemanticIR.create("fixture", (second, first))

    raw = ir.manifest()
    loaded = SemanticIR.from_manifest(json.loads(json.dumps(raw)))

    assert [item.identity.npm_name for item in loaded.features] == ["alpha", "beta"]
    assert loaded.manifest() == raw
    assert loaded.generation_id == ir.generation_id


def test_generation_plan_requires_one_generation_and_detects_true_noop(tmp_path: Path):
    existing = tmp_path / "local_modules/alpha/index.js"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"canonical\n")
    artifact = OwnedArtifact(
        "local_modules/alpha/index.js",
        "feature:alpha",
        "javascript-wrapper",
        b"canonical\n",
        "generation",
    )
    plan = GenerationPlan.compare(
        tmp_path,
        operation="update",
        requested_targets=("alpha",),
        affected_targets=("alpha", "shared runtime", "plugin wiring"),
        generation_id="generation",
        artifacts=(artifact,),
    )

    assert plan.is_noop
    assert plan.changes == ()
    with pytest.raises(GenerationPlanError, match="generation identity"):
        GenerationPlan.compare(
            tmp_path,
            operation="update",
            requested_targets=("alpha",),
            affected_targets=("alpha",),
            generation_id="other",
            artifacts=(artifact,),
        )


def test_generation_plan_reports_complete_changes_and_unified_diff(tmp_path: Path):
    path = tmp_path / "local_modules/alpha/index.js"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")
    stale = tmp_path / "android/generated/stale.cpp"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")
    artifact = OwnedArtifact(
        "local_modules/alpha/index.js",
        "feature:alpha",
        "javascript-wrapper",
        b"new\n",
        "generation",
    )

    plan = GenerationPlan.compare(
        tmp_path,
        operation="update",
        requested_targets=("alpha",),
        affected_targets=("alpha", "shared runtime"),
        generation_id="generation",
        artifacts=(artifact,),
        deletes=("android/generated/stale.cpp",),
    )

    assert [change.action for change in plan.changes] == [
        ArtifactAction.UPDATE,
        ArtifactAction.DELETE,
    ]
    assert "-old" in plan.unified_diff()
    assert "+new" in plan.unified_diff()
    assert plan.manifest()["requested_targets"] == ["alpha"]


def test_generation_plan_classifies_absence_as_create(tmp_path: Path):
    artifact = OwnedArtifact(
        "local_modules/alpha/index.js",
        "feature:alpha",
        "javascript-wrapper",
        b"generated\n",
        "generation",
    )

    plan = GenerationPlan.compare(
        tmp_path,
        operation="check",
        requested_targets=("alpha",),
        affected_targets=("alpha",),
        generation_id="generation",
        artifacts=(artifact,),
    )

    assert len(plan.changes) == 1
    assert plan.changes[0].action is ArtifactAction.CREATE


@pytest.mark.parametrize(
    "deleted,protected",
    [
        ("local_modules/alpha", "local_modules/alpha/android/src/main/cpp"),
        (
            "local_modules/alpha/android/src/main",
            "local_modules/alpha/android/src/main/cpp",
        ),
        (
            "local_modules/alpha/android/src/main/cpp/user.cpp",
            "local_modules/alpha/android/src/main/cpp",
        ),
    ],
)
def test_generation_plan_rejects_delete_overlap_with_user_source(
    tmp_path: Path, deleted: str, protected: str
):
    with pytest.raises(GenerationPlanError, match="overlaps"):
        GenerationPlan.compare(
            tmp_path,
            operation="repair",
            requested_targets=("alpha",),
            affected_targets=("alpha",),
            generation_id="generation",
            artifacts=(),
            deletes=(deleted,),
            preserved_user_paths=(protected,),
        )


@pytest.mark.parametrize(
    "path",
    [
        r"android\.supernote-module\runtime\..\..\user.cpp",
        r"C:\plugin\generated.cpp",
        r"\\server\share\generated.cpp",
    ],
)
def test_generation_plan_rejects_windows_path_spellings(path: str, tmp_path: Path):
    with pytest.raises(GenerationPlanError, match="canonical and relative"):
        GenerationPlan.compare(
            tmp_path,
            operation="repair",
            requested_targets=(),
            affected_targets=(),
            generation_id="generation",
            artifacts=(),
            deletes=(path,),
        )


def test_dependency_action_cannot_overlap_preserved_user_source(tmp_path: Path):
    source = tmp_path / "local_modules/alpha/android/src/main/cpp"
    source.mkdir(parents=True)
    sentinel = source / "sentinel.cpp"
    sentinel.write_text("// user source\n")

    with pytest.raises(GenerationPlanError, match="canonical parent package.json"):
        GenerationPlan.compare(
            tmp_path,
            operation="update",
            requested_targets=("alpha",),
            affected_targets=("alpha",),
            generation_id="generation",
            artifacts=(),
            preserved_user_paths=(
                "local_modules/alpha/android/src/main/cpp",
            ),
            dependency_actions=(
                DependencyAction(
                    "local_modules/alpha/android/src/main/cpp",
                    ("alpha",),
                    b"destructive\n",
                    b"",
                ),
            ),
        )

    assert sentinel.read_text() == "// user source\n"


def test_tree_removal_rejects_unrelated_or_preserved_project_state(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}\n")
    source = tmp_path / "local_modules/alpha/android/src/main/cpp"
    source.mkdir(parents=True)
    sentinel = source / "sentinel.cpp"
    sentinel.write_text("int sentinel;\n")

    with pytest.raises(GenerationPlanError, match="noncanonical"):
        GenerationPlan.compare(
            tmp_path,
            operation="remove",
            requested_targets=("alpha",),
            affected_targets=("alpha",),
            generation_id="generation",
            artifacts=(),
            tree_removals=(("local_modules/alpha/android", "feature:alpha"),),
        )

    with pytest.raises(GenerationPlanError, match="overlaps"):
        GenerationPlan.compare(
            tmp_path,
            operation="remove",
            requested_targets=("alpha",),
            affected_targets=("alpha",),
            generation_id="generation",
            artifacts=(),
            preserved_user_paths=("local_modules/alpha/android/src/main/cpp",),
            tree_removals=(("local_modules/alpha", "feature:alpha"),),
            authorized_tree_removals=("local_modules/alpha",),
        )

    assert sentinel.read_text() == "int sentinel;\n"


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_dependency_action_requires_regular_parent_package(
    tmp_path: Path, kind: str
):
    package = tmp_path / "package.json"
    if kind == "directory":
        package.mkdir()
    else:
        target = tmp_path / "outside-package.json"
        target.write_text("{}\n")
        package.symlink_to(target)
    with pytest.raises(GenerationPlanError, match="regular file"):
        GenerationPlan.compare(
            tmp_path,
            operation="update",
            requested_targets=(),
            affected_targets=(),
            generation_id="generation",
            artifacts=(),
            dependency_actions=(
                DependencyAction("package.json", (), b"{}\n", b"{}\n"),
            ),
        )


def test_integrity_manifest_is_timestamp_free_and_records_every_owned_hash():
    feature_ir = FeatureSemanticIR.create(identity("alpha"))
    ir = SemanticIR.create("fixture", (feature_ir,))
    artifact = OwnedArtifact(
        "local_modules/alpha/index.js",
        "feature:alpha",
        "javascript-wrapper",
        b"generated\n",
        ir.generation_id,
    )
    manifest = IntegrityManifest.create(
        generator_version="4.0.0",
        generation_id=ir.generation_id,
        plugin_id="fixture",
        features=(
            ManifestFeature(
                feature_ir.identity.feature_id,
                "alpha",
                "local_modules/alpha",
                feature_ir.semantic_hash,
            ),
        ),
        artifacts=(artifact,),
    )
    value = json.loads(manifest.render())

    assert value["schema_version"] == "1.0"
    assert "timestamp" not in value
    assert value["artifacts"][0]["sha256"] == artifact.sha256
    assert value["artifacts"][0]["generation_id"] == ir.generation_id
