from __future__ import annotations

import json
from pathlib import Path

import pytest

from supernote_module_generator.errors import GeneratorError
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_plan import GenerationPlanError
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.integrity_manifest import INTEGRITY_MANIFEST_PATH
from supernote_module_generator.project_model import ProjectModel


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n'
    )
    return tmp_path


def add_native(root: Path, name: str = "alpha") -> Path:
    return FeatureOperationService(root).add(
        FeatureConfig(
            root / f"local_modules/{name}",
            name,
            "4.0.0-dev.0",
            f"com.example.{name}",
            name.title(),
            starters=(StarterFamily.NATIVE,),
        )
    )


def update(root: Path, *requested: str) -> None:
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=requested,
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan)


def test_plan_contains_separate_feature_output_runtime_and_manifest(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    add_native(root)

    plan = GenerationService(root).plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )

    paths = {item.path for item in plan.artifacts}
    assert "local_modules/alpha/.supernote-generated/index.js" in paths
    assert "android/.supernote-module/runtime/feature-registry.json" in paths
    assert INTEGRITY_MANIFEST_PATH in paths
    assert plan.requested_targets == ("alpha",)
    assert set(plan.affected_targets) >= {
        "alpha",
        "shared runtime",
        "integrity manifest",
    }
    assert plan.dependency_actions == ()
    assert plan.wiring_actions == ()
    assert plan.tree_removals == ()


def test_equal_replan_still_carries_complete_output_set_for_rewrite(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    add_native(root)
    update(root, "alpha")

    second = GenerationService(root).plan(
        operation="update", requested_targets=("alpha",)
    )

    assert second.is_noop
    assert second.artifacts


def test_current_generated_project_uses_public_schema_versions(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    add_native(root)
    update(root, "alpha")

    root_manifest = json.loads((root / INTEGRITY_MANIFEST_PATH).read_text())
    feature_ownership = json.loads(
        (root / "local_modules/alpha/.supernote-generated/ownership.json").read_text()
    )
    runtime_ownership = json.loads(
        (root / "android/.supernote-module/runtime/ownership.json").read_text()
    )
    assert root_manifest["schema_version"] == "2.0"
    assert feature_ownership["schema_version"] == "1.0"
    assert runtime_ownership["schema_version"] == "1.0"
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_bytes().lower()
        assert b"__supernotev4" not in content
        assert b"snv4" not in content


def test_existing_manifest_is_valid_authority_for_second_add(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    add_native(root)
    update(root, "alpha")
    add_native(root, "beta")

    service = GenerationService(root)
    plan = service.plan(
        operation="add",
        requested_targets=("beta",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan)

    discovered = ProjectModel.discover(root)
    assert {feature.identity.npm_name for feature in discovered.features} == {
        "alpha",
        "beta",
    }


def test_unowned_runtime_leaf_survives_generated_reconciliation(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    add_native(root)
    runtime = root / "android/.supernote-module/runtime"
    runtime.mkdir(parents=True)
    sentinel = runtime / "user-sentinel.txt"
    sentinel.write_text("unowned bytes\n")

    update(root, "alpha")

    assert sentinel.read_text() == "unowned bytes\n"


def test_parent_package_and_build_files_are_never_planned_for_edit(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    add_native(root)
    baselines = {
        path: path.read_bytes()
        for path in (
            root / "package.json",
            root / "android/settings.gradle",
            root / "android/app/build.gradle",
        )
    }

    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    assert plan.dependency_actions == ()
    assert plan.wiring_actions == ()
    service.execute(plan)

    assert {path: path.read_bytes() for path in baselines} == baselines


@pytest.mark.parametrize(
    "corrupt",
    (
        lambda value: value.pop("generator_version"),
        lambda value: value.__setitem__("generator_version", "version four"),
        lambda value: value["plugin"].__setitem__("id", "another-plugin"),
        lambda value: value["features"].append(dict(value["features"][0])),
        lambda value: value["artifacts"].append(dict(value["artifacts"][0])),
        lambda value: value["features"][0].__setitem__("semantic_hash", "bad"),
    ),
)
def test_malformed_manifest_never_authorizes_generated_mutation(
    tmp_path: Path,
    corrupt,
) -> None:
    root = plugin(tmp_path)
    feature = add_native(root)
    update(root, "alpha")
    source = feature / "android/src/main/cpp/feature.cpp"
    source_before = source.read_bytes()
    manifest_path = root / INTEGRITY_MANIFEST_PATH
    value = json.loads(manifest_path.read_text())
    corrupt(value)
    manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    corrupted_manifest = manifest_path.read_bytes()

    with pytest.raises((GeneratorError, GenerationPlanError)):
        GenerationService(root).plan(
            operation="update",
            requested_targets=("alpha",),
        )

    assert source.read_bytes() == source_before
    assert manifest_path.read_bytes() == corrupted_manifest


def test_edited_runtime_ownership_is_rewritten_without_reaching_author_source(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    feature = add_native(root)
    update(root, "alpha")
    source = feature / "android/src/main/cpp/feature.cpp"
    source_before = source.read_bytes()
    ownership_path = root / "android/.supernote-module/runtime/ownership.json"
    ownership = json.loads(ownership_path.read_text())
    ownership["generated_files"].append(
        r"..\..\..\local_modules\alpha\android\src\main\cpp\feature.cpp"
    )
    ownership_path.write_text(json.dumps(ownership) + "\n")

    update(root, "alpha")

    assert source.read_bytes() == source_before
    restored = json.loads(ownership_path.read_text())
    assert not any("\\" in item for item in restored["generated_files"])


@pytest.mark.parametrize("container", ("root", "plugin", "feature", "artifact"))
def test_duplicate_json_keys_reject_prior_ownership(
    tmp_path: Path,
    container: str,
) -> None:
    root = plugin(tmp_path)
    add_native(root)
    update(root, "alpha")
    manifest = root / INTEGRITY_MANIFEST_PATH
    text = manifest.read_text()
    if container == "root":
        text = text.replace("{\n", '{\n  "schema_version": "9.0",\n', 1)
    elif container == "plugin":
        text = text.replace(
            '"plugin": {\n    "id": "fixture"',
            '"plugin": {\n    "id": "wrong",\n    "id": "fixture"',
            1,
        )
    elif container == "feature":
        text = text.replace(
            '"package_name": "alpha"',
            '"package_name": "wrong",\n      "package_name": "alpha"',
            1,
        )
    else:
        text = text.replace(
            '"committed_source": true,',
            '"committed_source": false,\n      "committed_source": true,',
            1,
        )
    manifest.write_text(text)

    with pytest.raises((GeneratorError, GenerationPlanError), match="duplicate JSON key"):
        GenerationService(root).plan(
            operation="update",
            requested_targets=("alpha",),
        )
