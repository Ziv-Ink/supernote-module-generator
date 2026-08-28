from __future__ import annotations

import os
from pathlib import Path

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.transaction import Transaction
from supernote_module_generator.v4_cli_operations import V4CliOperationService


def _plugin(root: Path) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (root / "android/settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    (root / "android/app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (root / "package.json").write_text(
        '{"name":"platform-fixture","dependencies":{}}\n', encoding="utf-8"
    )
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/alpha",
            "alpha",
            "0.1.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    service = GenerationService(root)
    plan = service.plan(
        operation="bootstrap",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "bootstrap", ("alpha",)))
    return root


def _exercise_mutation_validation_and_rollback(root: Path) -> None:
    service = GenerationService(root)
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    generated = root / "local_modules/alpha/index.d.ts"
    baseline_generated = generated.read_bytes()
    source.write_text(
        source.read_text(encoding="utf-8").replace("greet(", "greetPath("),
        encoding="utf-8",
    )
    plan = service.plan(operation="update", requested_targets=("alpha",))
    transaction = Transaction(root, "update", ("alpha",))
    service.execute(plan, transaction, commit=False)
    assert generated.read_bytes() != baseline_generated
    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert generated.read_bytes() == baseline_generated
    assert "greetPath(" in source.read_text(encoding="utf-8")

    committed = service.plan(operation="update", requested_targets=("alpha",))
    service.execute(committed, Transaction(root, "update", ("alpha",)))
    assert V4CliOperationService(root).check().status == "success"


@pytest.mark.parametrize("directory", ("project with spaces", "פרויקט-unicode-文档"))
def test_active_v4_path_supports_spaces_and_unicode(
    tmp_path: Path, directory: str
) -> None:
    root = _plugin(tmp_path / directory)

    _exercise_mutation_validation_and_rollback(root)


@pytest.mark.skipif(os.name != "nt", reason="native Windows long-path contract")
def test_windows_long_path_reaches_active_v4_mutation_validation_and_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path
    component = "long-v4-path-segment-0123456789"
    while len(str(root / component)) < 285:
        root /= component
    root = _plugin(root)

    assert len(str(root)) >= 285
    _exercise_mutation_validation_and_rollback(root)
