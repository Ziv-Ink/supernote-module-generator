from __future__ import annotations

import json
from pathlib import Path

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import (
    FeatureOperationError,
    FeatureOperationService,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.errors import GeneratorError
from supernote_module_generator.integrity_manifest import INTEGRITY_MANIFEST_PATH
from supernote_module_generator.generation_plan import GenerationPlanError
from supernote_module_generator.jvm_manifest import JvmSourceManifest, write_jvm_manifest
from supernote_module_generator.project_model import ProjectModel
from supernote_module_generator.transaction import Transaction
from supernote_module_generator.cli_operations import CliOperationService
from project_inventory import inventory_project


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n'
    )
    return tmp_path


def add_cpp(root: Path) -> None:
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/alpha",
            "alpha",
            "4.0.0-dev.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )


def test_real_generation_plan_contains_feature_runtime_and_manifest(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)

    plan = GenerationService(root).plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )

    paths = {item.path for item in plan.artifacts}
    assert "local_modules/alpha/index.js" in paths
    assert "android/.supernote-module/runtime/feature-registry.json" in paths
    assert INTEGRITY_MANIFEST_PATH in paths
    assert plan.requested_targets == ("alpha",)
    assert set(plan.affected_targets) >= {
        "alpha",
        "shared runtime",
        "plugin wiring",
    }


def test_execute_plan_commits_once_then_replanning_is_a_true_noop(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    transaction = Transaction(root, "update", ("alpha",))

    service.execute(plan, transaction)
    second = service.plan(operation="update", requested_targets=("alpha",))

    assert (root / INTEGRITY_MANIFEST_PATH).is_file()
    assert second.is_noop


def test_current_generated_project_uses_only_public_identity_and_schema(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "update", ("alpha",)))

    forbidden = (b"v4", b"snv4", b"__supernotev4")
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix().lower().encode("utf-8")
        assert not any(token in relative for token in forbidden), relative
        if not path.is_file():
            continue
        content = path.read_bytes().lower()
        assert not any(token in content for token in forbidden), relative
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and "schema_version" in value:
                assert value["schema_version"] == "1.0", relative


def test_existing_manifest_is_valid_prior_authority_for_a_second_add(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    first = service.plan(
        operation="add",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(first, Transaction(root, "add", ("alpha",)))
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/beta",
            "beta",
            "4.0.0-dev.0",
            "com.example.beta",
            "Beta",
            starters=(StarterFamily.NATIVE,),
        )
    )

    second = service.plan(
        operation="add",
        requested_targets=("beta",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(second, Transaction(root, "add", ("beta",)))

    discovered = ProjectModel.discover(root)
    assert {feature.identity.npm_name for feature in discovered.features} == {
        "alpha",
        "beta",
    }
    assert service.plan(operation="check", requested_targets=()).is_noop


def test_remove_plan_is_complete_before_mutation_and_leaves_canonical_empty_runtime(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    bootstrap = service.plan(
        operation="bootstrap",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(bootstrap, Transaction(root, "bootstrap", ("alpha",)))
    before = inventory_project(root)

    plan = service.plan(
        operation="remove",
        requested_targets=("alpha",),
        removed_targets=("alpha",),
    )

    assert inventory_project(root) == before
    assert {(item.path, item.owner) for item in plan.tree_removals} == {
        ("local_modules/alpha", "feature:alpha"),
        ("android/.supernote-module/runtime", "shared-runtime"),
    }
    assert plan.dependency_actions[0].package_names == ("alpha",)
    assert {item.path for item in plan.wiring_actions} == {
        "android/settings.gradle",
        "android/app/build.gradle",
    }
    assert plan.artifacts[-1].path == INTEGRITY_MANIFEST_PATH

    service.execute(plan, Transaction(root, "remove", ("alpha",)))

    assert not (root / "local_modules/alpha").exists()
    assert not (root / "android/.supernote-module/runtime").exists()
    assert service.plan(operation="check", requested_targets=()).is_noop


def test_unmanifested_runtime_tree_cannot_authorize_its_own_deletion(tmp_path: Path):
    root = plugin(tmp_path)
    runtime = root / "android/.supernote-module/runtime"
    runtime.mkdir(parents=True)
    sentinel = runtime / "user-sentinel.txt"
    sentinel.write_text("unmanifested user bytes\n")
    before = inventory_project(root)

    with pytest.raises(GenerationPlanError, match="ownership authority"):
        GenerationService(root).plan(
            operation="repair",
            requested_targets=(),
            allow_unmanifested_bootstrap=True,
        )

    assert inventory_project(root) == before
    assert sentinel.read_text() == "unmanifested user bytes\n"


def test_direct_feature_add_cannot_replace_unmanifested_runtime(tmp_path: Path):
    root = plugin(tmp_path)
    runtime = root / "android/.supernote-module/runtime"
    runtime.mkdir(parents=True)
    sentinel = runtime / "user-sentinel.txt"
    sentinel.write_text("unmanifested user bytes\n")
    before = inventory_project(root)

    with pytest.raises(FeatureOperationError, match="ownership authority"):
        add_cpp(root)

    assert inventory_project(root) == before
    assert sentinel.read_text() == "unmanifested user bytes\n"


def test_plan_execution_fault_before_manifest_restores_exact_inventory(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    add_cpp(root)
    before = inventory_project(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )

    def fail(name: str) -> None:
        if name == "before_manifest_write":
            raise RuntimeError("manifest fault")

    transaction = Transaction(
        root, "update", ("alpha",), fault_injector=fail
    )
    try:
        service.execute(plan, transaction)
    except RuntimeError:
        rollback = transaction.rollback()
    else:  # pragma: no cover
        raise AssertionError("fault was not injected")

    assert rollback.status == "completed"
    assert inventory_project(root) == before


def test_manifest_is_written_after_parent_dependency_action(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    assert plan.dependency_actions

    observed = False

    def observe(name: str) -> None:
        nonlocal observed
        if name != "after_manifest_write":
            return
        observed = True
        package = json.loads((root / "package.json").read_text())
        assert package["dependencies"]["alpha"] == "file:./local_modules/alpha"
        assert (root / INTEGRITY_MANIFEST_PATH).is_file()

    service.execute(
        plan,
        Transaction(root, "update", ("alpha",), fault_injector=observe),
    )

    assert observed is True


def test_prior_manifest_cannot_claim_or_delete_feature_source_root(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "update", ("alpha",)))
    sentinel = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    before = sentinel.read_bytes()
    manifest_path = root / INTEGRITY_MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"].append(
        {
            "path": "local_modules/alpha",
            "owner": "feature:alpha",
            "kind": "javascript-wrapper",
            "sha256": "0" * 64,
            "generation_id": manifest["generation_id"],
            "committed_source": True,
        }
    )
    manifest_path.write_text(json.dumps(manifest) + "\n")

    with pytest.raises(GeneratorError, match="feature ownership is inconsistent"):
        service.plan(operation="repair", requested_targets=("alpha",))

    assert sentinel.read_bytes() == before
    assert (root / "local_modules/alpha").is_dir()


@pytest.mark.parametrize(
    "corruption",
    (
        "missing_generator_version",
        "invalid_generator_version",
        "missing_plugin",
        "plugin_mismatch",
        "frontend_mismatch",
        "duplicate_feature",
        "duplicate_artifact",
        "feature_root_mismatch",
        "feature_identity_mismatch",
        "semantic_hash_invalid",
        "artifact_generation_mismatch",
        "runtime_artifact_owner_mismatch",
        "missing_feature_metadata_authority",
        "missing_runtime_authority",
        "noncanonical_generator_version",
        "truncated_ownership_inventory",
    ),
)
def test_malformed_manifest_never_authorizes_feature_tree_removal(
    tmp_path: Path,
    corruption: str,
):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    bootstrap = service.plan(
        operation="add",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(bootstrap, Transaction(root, "add", ("alpha",)))
    sentinel = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    sentinel_before = sentinel.read_bytes()
    manifest_path = root / INTEGRITY_MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text())
    feature = manifest["features"][0]
    feature_metadata = next(
        item
        for item in manifest["artifacts"]
        if item["path"] == "local_modules/alpha/.supernote-module.json"
    )
    runtime_metadata = next(
        item
        for item in manifest["artifacts"]
        if item["path"]
        == "android/.supernote-module/runtime/ownership.json"
    )

    if corruption == "missing_generator_version":
        del manifest["generator_version"]
    elif corruption == "invalid_generator_version":
        manifest["generator_version"] = "version four"
    elif corruption == "missing_plugin":
        del manifest["plugin"]
    elif corruption == "plugin_mismatch":
        manifest["plugin"]["id"] = "another-plugin"
    elif corruption == "frontend_mismatch":
        manifest["frontend_versions"]["cpp"] += 1
    elif corruption == "duplicate_feature":
        manifest["features"].append(dict(feature))
    elif corruption == "duplicate_artifact":
        manifest["artifacts"].append(dict(feature_metadata))
    elif corruption == "feature_root_mismatch":
        feature["root"] = "local_modules/not-alpha"
    elif corruption == "feature_identity_mismatch":
        feature["id"] = "supernote:feature:0000000000000000"
    elif corruption == "semantic_hash_invalid":
        feature["semantic_hash"] = "not-a-hash"
    elif corruption == "artifact_generation_mismatch":
        feature_metadata["generation_id"] = "0" * 64
    elif corruption == "runtime_artifact_owner_mismatch":
        runtime_metadata["owner"] = "feature:alpha"
    elif corruption == "missing_feature_metadata_authority":
        manifest["artifacts"].remove(feature_metadata)
    elif corruption == "missing_runtime_authority":
        manifest["artifacts"].remove(runtime_metadata)
    elif corruption == "noncanonical_generator_version":
        manifest["generator_version"] = "4.0.0-01"
    elif corruption == "truncated_ownership_inventory":
        manifest["artifacts"] = sorted(
            [feature_metadata, runtime_metadata], key=lambda item: item["path"]
        )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    corrupted = inventory_project(root)

    with pytest.raises((GeneratorError, GenerationPlanError)):
        service.plan(
            operation="remove",
            requested_targets=("alpha",),
            removed_targets=("alpha",),
        )

    assert inventory_project(root) == corrupted
    assert sentinel.read_bytes() == sentinel_before


def test_windows_backslash_runtime_ownership_cannot_reach_user_source(tmp_path: Path):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "update", ("alpha",)))
    sentinel = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    before = sentinel.read_bytes()
    ownership_path = root / "android/.supernote-module/runtime/ownership.json"
    ownership = json.loads(ownership_path.read_text())
    ownership["generated_files"].append(
        r"..\..\..\local_modules\alpha\android\src\main\cpp\feature.cpp"
    )
    ownership_path.write_text(json.dumps(ownership) + "\n")

    with pytest.raises((GeneratorError, GenerationPlanError), match="canonical and relative"):
        service.plan(operation="repair", requested_targets=("alpha",))

    assert sentinel.read_bytes() == before


@pytest.mark.parametrize(
    "duplicate",
    ("top_level", "plugin", "feature", "artifact", "runtime_ownership"),
)
def test_duplicate_json_keys_never_authorize_tree_removal(
    tmp_path: Path,
    duplicate: str,
):
    root = plugin(tmp_path)
    add_cpp(root)
    service = GenerationService(root)
    plan = service.plan(
        operation="add",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "add", ("alpha",)))
    sentinel = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    before = sentinel.read_bytes()
    manifest_path = root / INTEGRITY_MANIFEST_PATH
    text = manifest_path.read_text()
    if duplicate == "top_level":
        text = text.replace("{\n", '{\n  "schema_version": 3,\n', 1)
        manifest_path.write_text(text)
    elif duplicate == "plugin":
        text = text.replace(
            '"plugin": {\n    "id": "fixture"',
            '"plugin": {\n    "id": "wrong",\n    "id": "fixture"',
            1,
        )
        manifest_path.write_text(text)
    elif duplicate == "feature":
        text = text.replace(
            '"package_name": "alpha"',
            '"package_name": "wrong",\n      "package_name": "alpha"',
            1,
        )
        manifest_path.write_text(text)
    elif duplicate == "artifact":
        text = text.replace(
            '"committed_source": true,',
            '"committed_source": false,\n      "committed_source": true,',
            1,
        )
        manifest_path.write_text(text)
    else:
        ownership = root / "android/.supernote-module/runtime/ownership.json"
        ownership.write_text(
            ownership.read_text().replace(
                "{\n", '{\n  "schema_version": 1,\n', 1
            )
        )
    corrupted = inventory_project(root)

    with pytest.raises((GeneratorError, GenerationPlanError), match="duplicate JSON key"):
        service.plan(
            operation="remove",
            requested_targets=("alpha",),
            removed_targets=("alpha",),
        )

    assert inventory_project(root) == corrupted
    assert sentinel.read_bytes() == before


def test_mixed_jvm_build_hook_reuses_raw_manifest_and_is_noop(tmp_path: Path):
    root = plugin(tmp_path)
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/jvm",
            "jvm",
            "4.0.0-dev.0",
            "com.example.jvm",
            "Jvm",
            starters=(StarterFamily.JVM,),
        )
    )
    feature_id = next(
        feature.identity.feature_id
        for feature in ProjectModel.discover(
            root, allow_unmanifested_bootstrap=True
        ).features
        if feature.identity.npm_name == "jvm"
    )
    source_manifest = JvmSourceManifest(feature_id, "4.0.0", ())
    service = GenerationService(root)
    plan = service.plan(
        operation="update",
        requested_targets=("jvm",),
        jvm_manifests={feature_id: source_manifest},
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "update", ("jvm",)))
    manifest_root = tmp_path / "ksp-manifests"
    write_jvm_manifest(manifest_root / "jvm.json", source_manifest)

    result = CliOperationService(root).check(jvm_manifest_root=manifest_root)

    assert result.status == "success"
    assert result.validation is not None
    assert result.validation.issues == []
