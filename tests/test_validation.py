from __future__ import annotations

import json
from pathlib import Path
import subprocess

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.diagnostics import relevant_diagnostic_lines
from supernote_module_generator.diagnostics import write_process_diagnostics
from supernote_module_generator.errors import FilesystemError
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    source_tree_inventory,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.validation import GeneratedProjectValidator


def canonical_plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n'
    )
    FeatureOperationService(tmp_path).add(
        FeatureConfig(
            tmp_path / "local_modules/alpha",
            "alpha",
            "4.0.0-dev.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    service = GenerationService(tmp_path)
    plan = service.plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan)
    return tmp_path


def test_gradle_diagnostics_prioritize_actionable_task_cause():
    lines = relevant_diagnostic_lines(
        "FAILURE: Build failed with an exception.\n"
        "* What went wrong:\n"
        "Execution failed for task ':runtime:compileDebugKotlin'.\n"
    )

    assert lines[0] == "Execution failed for task ':runtime:compileDebugKotlin'."


def test_authoritative_validation_accepts_one_canonical_generation(tmp_path: Path):
    root = canonical_plugin(tmp_path)

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "success"
    assert result.issues == ()


def test_corrupt_javascript_fails_with_feature_scope(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    (root / "local_modules/alpha/.supernote-generated/index.js").write_text(
        "const = ;\n"
    )

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "failure"
    assert result.build == "not_run"
    assert {issue.code for issue in result.issues} >= {
        "SNMG_ARTIFACT_MODIFIED",
        "SNMG_JAVASCRIPT_INVALID",
    }
    artifact = next(
        issue for issue in result.issues if issue.code == "SNMG_ARTIFACT_MODIFIED"
    )
    assert artifact.scope == "feature"
    assert artifact.feature_id is not None


def test_javascript_validation_uses_module_stdin_without_a_filename_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    observed: dict[str, object] = {}

    def check(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        "supernote_module_generator.validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.subprocess.run",
        check,
    )

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "success"
    assert observed["command"] == [
        "node-test",
        "--input-type=module",
        "--check",
        "-",
    ]
    assert observed["input"] == (
        root / "local_modules/alpha/.supernote-generated/index.js"
    ).read_bytes()
    assert observed["capture_output"] is True
    assert observed["check"] is False
    assert "cwd" not in observed


def test_javascript_validation_reports_node_launch_failure(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    monkeypatch.setattr(
        "supernote_module_generator.validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("Node launch denied")
        ),
    )

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "failure"
    issue = next(
        item
        for item in result.issues
        if item.code == "SNMG_JAVASCRIPT_CHECK_FAILED"
    )
    assert issue.path == "local_modules/alpha/.supernote-generated/index.js"
    assert issue.message == (
        "Node.js syntax validation could not run: Node launch denied"
    )


def test_javascript_validation_rejects_symlink_without_invoking_node(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    javascript = root / "local_modules/alpha/.supernote-generated/index.js"
    javascript.unlink()
    javascript.symlink_to(root / "package.json")
    monkeypatch.setattr(
        "supernote_module_generator.validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Node must not inspect an unsafe artifact")
        ),
    )

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "failure"
    issue = next(
        item
        for item in result.issues
        if item.code == "SNMG_JAVASCRIPT_CHECK_FAILED"
    )
    assert issue.path == "local_modules/alpha/.supernote-generated/index.js"
    assert issue.message == (
        "Generated JavaScript is unsafe or unreadable: "
        "expected a regular file without links, found symlink"
    )


def test_javascript_validation_reports_unreadable_artifact_without_node(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    monkeypatch.setattr(
        "supernote_module_generator.validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.read_regular_bytes_no_follow",
        lambda _path: (_ for _ in ()).throw(FilesystemError("access denied")),
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Node must not inspect an unreadable artifact")
        ),
    )

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "failure"
    issue = next(
        item
        for item in result.issues
        if item.code == "SNMG_JAVASCRIPT_CHECK_FAILED"
    )
    assert issue.path == "local_modules/alpha/.supernote-generated/index.js"
    assert issue.message == (
        "Generated JavaScript is unsafe or unreadable: access denied"
    )


def test_javascript_validation_reports_syntax_error_not_node_version(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    monkeypatch.setattr(
        "supernote_module_generator.validation.shutil.which",
        lambda _name: "node-test",
    )
    stderr = (
        b"[stdin]:1\nconst = ;\n^^^^^\n\n"
        b"SyntaxError: Unexpected token '='\n\nNode.js v22.23.2\n"
    )
    monkeypatch.setattr(
        "supernote_module_generator.validation.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, b"", stderr
        ),
    )

    result = GeneratedProjectValidator(root).validate()

    issue = next(
        item for item in result.issues if item.code == "SNMG_JAVASCRIPT_INVALID"
    )
    assert issue.message == "SyntaxError: Unexpected token '='"


def test_generated_ownership_edit_does_not_adopt_an_authored_path(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    metadata_path = root / "local_modules/alpha/.supernote-generated/ownership.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["generated_files"].append("android/src/main/cpp/stale_jni.cpp")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    stale = root / "local_modules/alpha/android/src/main/cpp/stale_jni.cpp"
    stale.write_text("// generated stale JNI\n")

    result = GeneratedProjectValidator(root).validate()

    assert result.status == "failure"
    assert {issue.code for issue in result.issues} == {"SNMG_ARTIFACT_MODIFIED"}
    assert stale.read_text() == "// generated stale JNI\n"


def test_missing_owned_artifact_is_classified_as_missing(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    missing = root / "local_modules/alpha/.supernote-generated/index.js"
    missing.unlink()

    result = GeneratedProjectValidator(root).validate()

    issue = next(
        item
        for item in result.issues
        if item.path == "local_modules/alpha/.supernote-generated/index.js"
    )
    assert issue.code == "SNMG_ARTIFACT_MISSING"
    assert issue.actual == "missing"


def test_runtime_frontend_subproject_build_outputs_are_canonical_build_state(
    tmp_path: Path,
):
    root = canonical_plugin(tmp_path)
    runtime = root / "android/.supernote-module/runtime"
    generated_build_files = (
        runtime / "annotations/build/classes/Annotation.class",
        runtime / "processor/build/libs/processor.jar",
    )
    for path in generated_build_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"build output")

    inventory = source_tree_inventory(root)

    assert all(
        path.relative_to(root).as_posix() not in inventory
        for path in generated_build_files
    )


def test_diagnostics_refuse_symlink_ancestor_without_external_write(tmp_path: Path):
    root = canonical_plugin(tmp_path / "plugin")
    external = tmp_path / "external"
    external.mkdir()
    (root / "android/build").symlink_to(external, target_is_directory=True)
    sentinel = external / "sn-module-gen/diagnostics/check-build.log"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("outside\n")

    result = write_process_diagnostics(
        root,
        name="check-build",
        command=("gradle",),
        exit_code=1,
        stdout="",
        stderr="failure",
    )

    assert result is None
    assert sentinel.read_text() == "outside\n"


def test_diagnostics_refuse_symlink_leaf_without_external_write(tmp_path: Path):
    root = canonical_plugin(tmp_path / "plugin")
    diagnostic_root = root / "android/build/sn-module-gen/diagnostics"
    diagnostic_root.mkdir(parents=True)
    external = tmp_path / "outside.log"
    external.write_text("outside\n")
    (diagnostic_root / "check-build.log").symlink_to(external)

    result = write_process_diagnostics(
        root,
        name="check-build",
        command=("gradle",),
        exit_code=1,
        stdout="",
        stderr="failure",
    )

    assert result is None
    assert external.read_text() == "outside\n"
