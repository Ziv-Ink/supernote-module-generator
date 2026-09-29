"""Publisher frontend boundary tests, not real KSP/compiler qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from supernote_module_generator import jvm_frontend_service as frontend
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.frontend_discovery import load_jvm_frontend_manifests
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.project_model import ProjectModel
from supernote_module_generator.semantic import SemanticApi
from supernote_module_generator.semantic_ir import SemanticIRError

from test_npm_distribution import _file_inventory, _plugin


@pytest.fixture
def publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _plugin(tmp_path / "publisher")
    FeatureOperationService(root).add(
        FeatureConfig(
            output=root / "local_modules/jvm",
            npm_name="jvm",
            package_version="1.0.0",
            android_namespace="com.fixture.jvm",
            public_name="Jvm",
            starters=(StarterFamily.JVM,),
        )
    )
    project = ProjectModel.discover(root, allow_unmanifested_bootstrap=True)
    feature_id = project.features[0].identity.feature_id
    service = GenerationService(root)
    bootstrap = service.plan(
        operation="add",
        requested_targets=("jvm",),
        jvm_apis={feature_id: SemanticApi()},
        allow_unmanifested_bootstrap=True,
    )
    service.execute(bootstrap, finalize=False)
    # The process is intercepted below. No claim that Python is a Gradle executable.
    monkeypatch.setenv("SUPERNOTE_GRADLE_COMMAND", sys.executable)
    calls = []

    def run(command, *, cwd, timeout):
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(frontend, "run_process", run)
    return root, project, feature_id, calls


def _analysis(root: Path) -> Path:
    return root / "android/.supernote-module/runtime/analysis"


def _manifest(root: Path, feature_id: str, name: str = "feature.json") -> Path:
    directory = (
        _analysis(root) / "subject/build/generated/ksp/main/resources/supernote/generated/manifests"
    )
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    destination.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "supernote_module_jvm_source_manifest",
                "feature_id": feature_id,
                "frontend_version": "fault-fixture",
                "owners": [],
            }
        )
        + "\n"
    )
    return destination


def _adapter(root: Path, feature_id: str, content: bytes) -> Path:
    directory = (
        _analysis(root) / "subject/build/generated/ksp/main/kotlin/supernote/generated/adapters"
    )
    directory.mkdir(parents=True, exist_ok=True)
    suffix = hashlib.sha256(feature_id.encode()).hexdigest()[:20]
    destination = directory / f"FeatureAdapters_{suffix}.kt"
    destination.write_bytes(content)
    return destination


def _expect_failure(root: Path, error_type, message: str):
    before = _file_inventory(root)
    with pytest.raises(error_type, match=message) as caught:
        frontend.JvmFrontendService(root).manifests(allow_unmanifested_bootstrap=True)
    assert _file_inventory(root) == before
    return str(caught.value)


@pytest.mark.parametrize("configuration", ["missing", "relative", "nonexistent", "directory"])
def test_publisher_requires_available_absolute_gradle(publisher, monkeypatch, configuration):
    root, _project, _feature_id, calls = publisher
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: None)
    if configuration == "missing":
        monkeypatch.delenv("SUPERNOTE_GRADLE_COMMAND")
    else:
        monkeypatch.setenv(
            "SUPERNOTE_GRADLE_COMMAND",
            {
                "relative": "gradle",
                "nonexistent": str(root / "missing-gradle"),
                "directory": str(root),
            }[configuration],
        )
    _expect_failure(root, RuntimeError, "absolute standalone Gradle executable")
    assert calls == []


@pytest.mark.parametrize("missing", ["settings.gradle", "subject/build.gradle"])
def test_publisher_rejects_incomplete_analysis_bootstrap(publisher, missing):
    root, _project, _feature_id, calls = publisher
    (_analysis(root) / missing).unlink()
    _expect_failure(root, RuntimeError, "generated analysis bootstrap")
    assert calls == []


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_publisher_ksp_failure_reports_bounded_tail_and_no_result(publisher, monkeypatch, stream):
    root, _project, _feature_id, _calls = publisher
    output = "\n".join(f"compiler line {number:02}" for number in range(20))
    monkeypatch.setattr(
        frontend,
        "run_process",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            output if stream == "stdout" else "ignored stdout",
            output if stream == "stderr" else "",
        ),
    )
    message = _expect_failure(root, RuntimeError, "KSP semantic frontend failed")
    assert message.splitlines() == ["KSP semantic frontend failed:", *output.splitlines()[-12:]]


@pytest.mark.parametrize("condition", ["absent", "unknown", "duplicate"])
def test_publisher_requires_exactly_one_manifest_per_jvm_feature(publisher, condition):
    root, _project, feature_id, calls = publisher
    if condition == "unknown":
        _manifest(root, "supernote:feature:0000000000000000")
    elif condition == "duplicate":
        _manifest(root, feature_id, "first.json")
        _manifest(root, feature_id, "second.json")
    _expect_failure(
        root,
        SemanticIRError,
        {
            "absent": "no semantic manifest",
            "unknown": "unknown feature",
            "duplicate": "multiple JVM semantic manifests",
        }[condition],
    )
    assert len(calls) == 1


@pytest.mark.parametrize("condition", ["missing", "invalid", "directory"])
def test_publisher_rejects_missing_or_invalid_adapter(publisher, condition):
    root, _project, feature_id, calls = publisher
    _manifest(root, feature_id)
    if condition != "missing":
        path = _adapter(root, feature_id, b"package unrelated\n")
        if condition == "directory":
            path.unlink()
            path.mkdir()
    _expect_failure(
        root,
        RuntimeError,
        ("invalid adapter source" if condition == "invalid" else "required adapter source"),
    )
    assert len(calls) == 1


def test_publisher_returns_exact_adapter_bytes_from_same_output_tree(publisher):
    root, _project, feature_id, calls = publisher
    _manifest(root, feature_id)
    content = b"package supernote.generated.adapters\n// transport fixture, not compiler proof\n"
    _adapter(root, feature_id, content)
    before = _file_inventory(root)
    result = frontend.JvmFrontendService(root).manifests(allow_unmanifested_bootstrap=True)
    assert list(result) == [feature_id]
    assert result[feature_id].frontend_version == "fault-fixture"
    assert result.adapter_sources == {feature_id: content}
    assert _file_inventory(root) == before
    command, cwd, timeout = calls[0]
    assert command == [
        sys.executable,
        "--no-daemon",
        "--console=plain",
        "-p",
        str(_analysis(root)),
        ":subject:kspKotlin",
    ]
    assert cwd == _analysis(root)
    assert timeout == 1200
    assert len(calls) == 1


def test_manifest_discovery_allows_feature_without_jvm_source(publisher):
    root, project, _feature_id, calls = publisher
    for source in project.features[0].jvm_root.rglob("*.kt"):
        source.unlink()
    for source in project.features[0].jvm_root.rglob("*.java"):
        source.unlink()
    # No JVM input means neither a manifest nor an analysis subprocess is required.
    assert load_jvm_frontend_manifests(project, root / "missing-manifests") == {}
    assert frontend.JvmFrontendService(root).manifests(allow_unmanifested_bootstrap=True) == {}
    assert calls == []
