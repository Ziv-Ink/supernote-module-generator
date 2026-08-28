from __future__ import annotations

from pathlib import Path

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.frontend_discovery import discover_semantic_ir
from supernote_module_generator.project_model import ExistingGeneration, ProjectModel
from supernote_module_generator.semantic_ir import SemanticIRError
from v4_project_inventory import inventory_project


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n'
    )
    return tmp_path


def test_project_discovery_is_read_only_and_canonical(tmp_path: Path):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    service.add(
        FeatureConfig(
            root / "local_modules/alpha",
            "alpha",
            "4.0.0-dev.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    before = inventory_project(root)

    model = ProjectModel.discover(root, allow_unmanifested_bootstrap=True)
    ir = discover_semantic_ir(model)

    assert model.existing_generation is ExistingGeneration.UNMANIFESTED_V4
    assert [feature.identity.npm_name for feature in model.features] == ["alpha"]
    assert ir.plugin_id == "fixture"
    assert inventory_project(root) == before


def test_complete_ir_rejects_jvm_source_without_ksp_frontend_output(tmp_path: Path):
    root = plugin(tmp_path)
    service = FeatureOperationService(root)
    service.add(
        FeatureConfig(
            root / "local_modules/jvm",
            "jvm",
            "4.0.0-dev.0",
            "com.example.jvm",
            "Jvm",
            starters=(StarterFamily.JVM,),
        )
    )

    with pytest.raises(SemanticIRError, match="requires the JVM/KSP"):
        discover_semantic_ir(
            ProjectModel.discover(root, allow_unmanifested_bootstrap=True)
        )
