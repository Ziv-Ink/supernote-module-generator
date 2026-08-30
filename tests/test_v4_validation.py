from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.diagnostics import relevant_diagnostic_lines
from supernote_module_generator.diagnostics import write_process_diagnostics
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    protected_directory_metadata,
    source_tree_inventory,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.transaction import Transaction
from supernote_module_generator.v4_validation import V4Validator


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
    service.execute(plan, Transaction(tmp_path, "update", ("alpha",)))
    return tmp_path


def add_gradle_wrapper(root: Path) -> Path:
    wrapper = root / "android/gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    return wrapper


def test_gradle_diagnostics_prioritize_actionable_task_cause():
    lines = relevant_diagnostic_lines(
        "FAILURE: Build failed with an exception.\n"
        "* What went wrong:\n"
        "Execution failed for task ':runtime:compileDebugKotlin'.\n"
    )

    assert lines[0] == "Execution failed for task ':runtime:compileDebugKotlin'."


def test_authoritative_validation_accepts_one_canonical_generation(tmp_path: Path):
    root = canonical_plugin(tmp_path)

    result = V4Validator(root).validate()

    assert result.status == "success"
    assert result.issues == ()


def test_corrupt_javascript_fails_before_build_with_feature_scope(
    tmp_path: Path, monkeypatch
):
    root = canonical_plugin(tmp_path)
    (root / "local_modules/alpha/index.js").write_text("const = ;\n")
    invoked = False

    def unexpected_build(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("build must not run before integrity succeeds")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        unexpected_build,
    )

    result = V4Validator(root).validate(build=True)

    assert result.status == "failure"
    assert result.build == "not_run"
    assert invoked is False
    assert {issue.code for issue in result.issues} >= {
        "SNV4_ARTIFACT_MODIFIED",
        "SNV4_JAVASCRIPT_INVALID",
    }
    artifact = next(
        issue for issue in result.issues if issue.code == "SNV4_ARTIFACT_MODIFIED"
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
        "supernote_module_generator.v4_validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.subprocess.run",
        check,
    )

    result = V4Validator(root).validate()

    assert result.status == "success"
    assert observed["command"] == [
        "node-test",
        "--input-type=module",
        "--check",
        "-",
    ]
    assert observed["input"] == (root / "local_modules/alpha/index.js").read_bytes()
    assert observed["capture_output"] is True
    assert observed["check"] is False
    assert "cwd" not in observed


def test_javascript_validation_reports_node_launch_failure(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.shutil.which",
        lambda _name: "node-test",
    )
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("Node launch denied")
        ),
    )

    result = V4Validator(root).validate()

    assert result.status == "failure"
    issue = next(
        item
        for item in result.issues
        if item.code == "SNV4_JAVASCRIPT_CHECK_FAILED"
    )
    assert issue.path == "local_modules/alpha/index.js"
    assert issue.message == (
        "Node.js syntax validation could not run: Node launch denied"
    )


def test_javascript_validation_reports_syntax_error_not_node_version(
    tmp_path: Path, monkeypatch
) -> None:
    root = canonical_plugin(tmp_path)
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.shutil.which",
        lambda _name: "node-test",
    )
    stderr = (
        b"[stdin]:1\nconst = ;\n^^^^^\n\n"
        b"SyntaxError: Unexpected token '='\n\nNode.js v22.23.2\n"
    )
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, b"", stderr
        ),
    )

    result = V4Validator(root).validate()

    issue = next(
        item for item in result.issues if item.code == "SNV4_JAVASCRIPT_INVALID"
    )
    assert issue.message == "SyntaxError: Unexpected token '='"


def test_untrusted_feature_generated_path_is_rejected_and_preserved(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    metadata_path = root / "local_modules/alpha/.supernote-module.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["generated_files"].append("android/src/main/cpp/stale_jni.cpp")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    stale = root / "local_modules/alpha/android/src/main/cpp/stale_jni.cpp"
    stale.write_text("// generated stale JNI\n")

    result = V4Validator(root).validate()

    assert result.status == "failure"
    assert {issue.code for issue in result.issues} == {"SNV4_INPUT_INVALID"}
    assert "unrecognized generated feature artifact" in result.issues[0].message
    assert stale.read_text() == "// generated stale JNI\n"


def test_missing_owned_artifact_is_classified_as_missing(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    missing = root / "local_modules/alpha/index.js"
    missing.unlink()

    result = V4Validator(root).validate()

    issue = next(item for item in result.issues if item.path == "local_modules/alpha/index.js")
    assert issue.code == "SNV4_ARTIFACT_MISSING"
    assert issue.actual == "missing"


def test_missing_marker_end_is_a_single_runtime_scoped_issue(tmp_path: Path):
    root = canonical_plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime", "")
    )

    result = V4Validator(root).validate()

    wiring = [issue for issue in result.issues if issue.code == "SNV4_WIRING_INVALID"]
    assert len(wiring) == 1
    assert wiring[0].scope == "runtime"


def test_build_is_additive_and_diagnostics_are_outside_source_state(
    tmp_path: Path, monkeypatch
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)
    before = source_tree_inventory(root)
    commands = []

    def successful_build(command, *, cwd, timeout, env):
        commands.append((command, cwd, timeout))
        assert env["SUPERNOTE_MODULE_PARENT_GENERATION_ID"]
        return subprocess.CompletedProcess(
            command,
            0,
            "configuration output\nBUILD SUCCESSFUL\n",
            "",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        successful_build,
    )

    result = V4Validator(root).validate(build=True)

    assert result.status == "success"
    assert result.build == "passed"
    assert result.build_error is None
    assert len(result.diagnostics) == 1
    diagnostics = Path(result.diagnostics[0])
    assert diagnostics.is_file()
    assert "BUILD SUCCESSFUL" in diagnostics.read_text()
    assert source_tree_inventory(root) == before
    assert commands[0][0][-1] == ":app:assembleDebug"
    assert commands[0][1] == root / "android"


def test_build_failure_prioritizes_source_cause_and_preserves_full_log(
    tmp_path: Path, monkeypatch
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)

    def failed_build(command, *, cwd, timeout, env):
        return subprocess.CompletedProcess(
            command,
            1,
            "> Task :app:compileDebugKotlin FAILED\nprogress noise\n",
            "src/main/Foo.kt:42:7: error: unresolved reference: Missing\n"
            "FAILURE: Build failed with an exception.\n",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        failed_build,
    )

    result = V4Validator(root).validate(build=True)

    assert result.status == "failure"
    assert result.build == "failed"
    assert {issue.code for issue in result.issues} == {"SNV4_BUILD_FAILED"}
    assert result.build_error is not None
    assert result.build_error.relevant_lines[0].startswith("src/main/Foo.kt:42:7")
    diagnostics = Path(result.diagnostics[0]).read_text()
    assert "progress noise" in diagnostics
    assert "unresolved reference: Missing" in diagnostics


@pytest.mark.parametrize("exit_code", [0, 1])
def test_first_build_restores_protected_directory_metadata_after_cache_creation(
    tmp_path: Path,
    monkeypatch,
    exit_code: int,
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)
    assert not (root / "android/build").exists()
    assert not (root / "android/app/build").exists()
    before = protected_directory_metadata(root)

    def first_build(command, *, cwd, timeout, env):
        (root / "android/build/generated").mkdir(parents=True)
        (root / "android/app/build/outputs").mkdir(parents=True)
        return subprocess.CompletedProcess(
            command,
            exit_code,
            "BUILD SUCCESSFUL\n" if exit_code == 0 else "",
            "compiler failure\n" if exit_code else "",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", first_build
    )

    result = V4Validator(root).validate(build=True)

    assert result.build == ("passed" if exit_code == 0 else "failed")
    assert protected_directory_metadata(root) == before


def test_successful_gradle_exit_that_mutates_source_fails_build_validation(
    tmp_path: Path, monkeypatch
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)
    generated = root / "local_modules/alpha/index.js"

    def mutating_build(command, *, cwd, timeout, env):
        generated.write_text(generated.read_text() + "// build mutation\n")
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        mutating_build,
    )

    result = V4Validator(root).validate(build=True)

    assert result.status == "failure"
    assert result.build == "failed"
    issue = next(
        item for item in result.issues if item.code == "SNV4_BUILD_MUTATED_SOURCE"
    )
    assert "modified:local_modules/alpha/index.js" in (issue.actual or "")


@pytest.mark.parametrize("directory_name", ["build", ".gradle", ".cxx", ".kotlin"])
def test_build_mutation_detector_includes_cache_named_user_source_directory(
    tmp_path: Path, monkeypatch, directory_name: str
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)
    source = (
        root
        / "local_modules/alpha/android/src/main/cpp"
        / directory_name
        / "sentinel.cpp"
    )
    source.parent.mkdir(parents=True)
    source.write_text("int sentinel = 1;\n")

    def mutating_build(command, *, cwd, timeout, env):
        source.write_text("int sentinel = 2;\n")
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", mutating_build
    )

    result = V4Validator(root).validate(build=True)

    issue = next(
        item for item in result.issues if item.code == "SNV4_BUILD_MUTATED_SOURCE"
    )
    assert f"cpp/{directory_name}/sentinel.cpp" in (issue.actual or "")


def test_runtime_frontend_subproject_build_outputs_are_canonical_build_state(
    tmp_path: Path,
):
    root = canonical_plugin(tmp_path)
    runtime = root / "android/.supernote-module/v4-runtime"
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


def test_build_mutation_detector_rejects_touch_only_source_change(
    tmp_path: Path, monkeypatch
):
    root = canonical_plugin(tmp_path)
    add_gradle_wrapper(root)
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"

    def touching_build(command, *, cwd, timeout, env):
        metadata = source.stat()
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", touching_build
    )

    result = V4Validator(root).validate(build=True)

    assert {item.code for item in result.issues} == {"SNV4_BUILD_MUTATED_SOURCE"}


def test_diagnostics_refuse_symlink_ancestor_without_external_write(tmp_path: Path):
    root = canonical_plugin(tmp_path / "plugin")
    external = tmp_path / "external"
    external.mkdir()
    (root / "android/build").symlink_to(external, target_is_directory=True)
    sentinel = external / "supernote-module/diagnostics/v4-check-build.log"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("outside\n")

    result = write_process_diagnostics(
        root,
        name="v4-check-build",
        command=("gradle",),
        exit_code=1,
        stdout="",
        stderr="failure",
    )

    assert result is None
    assert sentinel.read_text() == "outside\n"


def test_diagnostics_refuse_symlink_leaf_without_external_write(tmp_path: Path):
    root = canonical_plugin(tmp_path / "plugin")
    diagnostic_root = root / "android/build/supernote-module/diagnostics"
    diagnostic_root.mkdir(parents=True)
    external = tmp_path / "outside.log"
    external.write_text("outside\n")
    (diagnostic_root / "v4-check-build.log").symlink_to(external)

    result = write_process_diagnostics(
        root,
        name="v4-check-build",
        command=("gradle",),
        exit_code=1,
        stdout="",
        stderr="failure",
    )

    assert result is None
    assert external.read_text() == "outside\n"
