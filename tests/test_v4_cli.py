from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.feature_generator import FeatureConfig, stage_feature
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.generation_plan import PlanConflictError
from supernote_module_generator.models import Change, RollbackResult
from supernote_module_generator.v4_cli_operations import V4CliOperationService
from supernote_module_generator.v4_validation import (
    V4ValidationResult,
    V4Validator,
    ValidationIssue,
)
from supernote_module_generator.transaction import Transaction, recover_pending
from supernote_module_generator.filesystem import (
    ProtectedSourceGuard,
    ProtectedSourceRestoreError,
    hash_entry_no_follow,
    protected_directory_metadata,
    remove_entry_no_follow,
    restore_protected_directory_metadata,
    restore_protected_source_backup,
    source_tree_inventory,
)
from v4_project_inventory import inventory_project


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n")
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
        operation="bootstrap",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(tmp_path, "bootstrap", ("alpha",)))
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        arguments,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_update_all_dry_run_json_exposes_complete_plan_without_writes(tmp_path: Path):
    root = plugin(tmp_path)
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    source.write_text(source.read_text().replace("greet(", "greetAgain("))
    before = inventory_project(root)

    code, stdout, stderr = invoke(
        root, ["--json", "update", "--all", "--dry-run", "--diff"]
    )
    value = json.loads(stdout)

    assert code == 0
    assert stderr == ""
    assert value["schema_version"] == "4.0"
    assert value["metadata"]["requested_targets"] == ["alpha"]
    assert set(value["metadata"]["affected_targets"]) >= {
        "alpha",
        "shared runtime",
        "plugin wiring",
    }
    assert value["metadata"]["dry_run"] is True
    assert "diff" in value["metadata"]
    assert value["requested_targets"] == ["alpha"]
    assert set(value["affected_targets"]) >= {
        "alpha",
        "shared runtime",
        "plugin wiring",
    }
    assert value["changes"]
    assert value["actual_changes"] == []
    assert inventory_project(root) == before


def test_update_all_commits_manifest_and_second_execution_is_noop(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    before = inventory_project(root)
    before_mtimes = {".": root.lstat().st_mtime_ns, **{
        path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
        for path in root.rglob("*")
    }}

    code, stdout, stderr = invoke(
        root, ["--json", "update", "--all", "--yes"]
    )
    value = json.loads(stdout)

    assert code == 0
    assert stderr == ""
    assert value["metadata"]["no_op"] is True
    assert value["changes"] == []
    assert value["actual_changes"] == []
    assert inventory_project(root) == before
    assert {".": root.lstat().st_mtime_ns, **{
        path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
        for path in root.rglob("*")
    }} == before_mtimes
    assert invoke(root, ["--json", "check"])[0] == 0


def test_human_dry_run_separates_requested_and_also_affected(tmp_path: Path):
    root = plugin(tmp_path)

    code, stdout, stderr = invoke(
        root, ["update", "alpha", "--dry-run", "--diff"]
    )

    assert code == 0, stderr
    assert "Requested:\n  alpha" in stdout
    assert "Also affected:" in stdout
    assert "shared runtime" in stdout
    assert "Changes:" in stdout


def test_repair_without_yes_is_a_dry_run(tmp_path: Path):
    root = plugin(tmp_path)
    before = inventory_project(root)

    code, stdout, stderr = invoke(root, ["--json", "repair", "--diff"])
    value = json.loads(stdout)

    assert code == 0
    assert stderr == ""
    assert value["metadata"]["dry_run"] is True
    assert inventory_project(root) == before


@pytest.mark.parametrize("damage", ["missing_end", "duplicate", "payload"])
def test_repair_canonicalizes_malformed_v4_wiring_without_touching_user_text(
    tmp_path: Path,
    damage: str,
):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    canonical = settings.read_text()
    user_line = "include ':user-library'\n"
    if damage == "missing_end":
        damaged = canonical.replace("// end supernote-module-v4-runtime\n", "")
    elif damage == "duplicate":
        start = canonical.index("// supernote-module-v4-runtime")
        block = canonical[start:]
        damaged = canonical + block
    else:
        damaged = canonical.replace(
            "include ':supernote-v4-runtime'",
            "include ':corrupted-generator-payload'",
        )
    settings.write_text(damaged + user_line)
    before = inventory_project(root)

    check_code, check_stdout, check_stderr = invoke(root, ["--json", "check"])
    check = json.loads(check_stdout)

    assert check_code == 1, check_stderr
    assert {issue["code"] for issue in check["issues"]} == {
        "SNV4_WIRING_INVALID"
    }
    assert inventory_project(root) == before

    preview_code, preview_stdout, preview_stderr = invoke(
        root, ["--json", "repair", "--diff"]
    )
    preview = json.loads(preview_stdout)

    assert preview_code == 0, preview_stderr
    assert preview["metadata"]["dry_run"] is True
    assert any(
        change["path"].endswith("/android/settings.gradle")
        for change in preview["changes"]
    ), json.dumps(preview, indent=2, sort_keys=True)
    assert inventory_project(root) == before

    repair_code, repair_stdout, repair_stderr = invoke(
        root, ["--json", "repair", "--yes"]
    )
    repaired = json.loads(repair_stdout)

    assert repair_code == 0, (repair_stderr, repaired)
    assert repaired["rollback"]["status"] == "not_needed"
    assert any(
        change["path"].endswith("/android/settings.gradle")
        for change in repaired["actual_changes"]
    )
    text = settings.read_text()
    assert text.count("// supernote-module-v4-runtime") == 1
    assert text.count("// end supernote-module-v4-runtime") == 1
    assert text.count(user_line.strip()) == 1
    assert invoke(root, ["--json", "check"])[0] == 0


def test_repair_restores_every_malformed_wiring_family_atomically(tmp_path: Path):
    root = plugin(tmp_path)
    application = root / "android/app/src/main/java/com/example/MainApplication.kt"
    application.parent.mkdir(parents=True)
    application.write_text(
        "fun getPackages() =\n"
        "    PackageList(this).packages.apply {\n"
        "      add(UserPackage())\n"
        "    }\n"
    )
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    settings = root / "android/settings.gradle"
    app_build = root / "android/app/build.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime\n", "")
        + "include ':user-library'\n"
    )
    app_build.write_text(
        app_build.read_text().replace("// end supernote-module-v4-runtime\n", "")
        + "dependencies { implementation project(':user-library') }\n"
    )
    application.write_text(
        application.read_text().replace("// end supernote-module-v4-package\n", "")
    )
    before = inventory_project(root)

    code, stdout, stderr = invoke(root, ["--json", "repair", "--yes"])
    payload = json.loads(stdout)

    assert code == 0, (stderr, payload)
    assert {
        Path(change["path"]).relative_to(root).as_posix()
        for change in payload["actual_changes"]
        if change["ownership"] == "plugin_wiring"
    } == {
        "android/settings.gradle",
        "android/app/build.gradle",
        "android/app/src/main/java/com/example/MainApplication.kt",
    }
    assert inventory_project(root) != before
    assert "include ':user-library'" in settings.read_text()
    assert "implementation project(':user-library')" in app_build.read_text()
    assert "add(UserPackage())" in application.read_text()
    assert settings.read_text().count("// end supernote-module-v4-runtime") == 1
    assert app_build.read_text().count("// end supernote-module-v4-runtime") == 1
    assert application.read_text().count("// end supernote-module-v4-package") == 1
    assert invoke(root, ["--json", "check"])[0] == 0


def test_staged_repair_validation_rejection_rolls_back_with_authoritative_issues(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime\n", "")
    )
    preview_code, preview_stdout, preview_stderr = invoke(
        root, ["--json", "repair", "--diff"]
    )
    preview = json.loads(preview_stdout)
    assert preview_code == 0, preview_stderr
    assert preview["changes"]
    before = inventory_project(root)
    issue = ValidationIssue(
        "SNV4_STAGED_REPAIR_REJECTED",
        "error",
        "plugin",
        "staged repair sentinel",
        path="android/settings.gradle",
        suggested_command="Fix the staged repair sentinel and rerun repair.",
    )

    monkeypatch.setattr(
        V4Validator,
        "validate",
        lambda self, **kwargs: V4ValidationResult(
            "failure",
            "1" * 64,
            (issue,),
            diagnostics=("/tmp/staged-repair-diagnostics.log",),
        ),
    )

    code, stdout, stderr = invoke(root, ["--json", "repair", "--yes"])
    result = json.loads(stdout)

    assert code == 1, stderr
    assert result["status"] == "failure"
    assert result["error"]["kind"] == "repair_validation_failed"
    assert result["error"]["phase"] == "precommit"
    assert result["issues"] == [issue.manifest()]
    assert result["diagnostics"] == ["/tmp/staged-repair-diagnostics.log"]
    assert result["affected_targets"] == ["plugin wiring"]
    assert result["changes"] == preview["changes"]
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "completed"
    assert result["next_action"] == issue.suggested_command
    assert inventory_project(root) == before
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*.state"))

    human_code, human_stdout, human_stderr = invoke(
        root, ["repair", "--yes"]
    )
    human_output = human_stdout + human_stderr
    assert human_code == 1, human_output
    assert "staged repair sentinel" in human_output
    assert issue.suggested_command in human_output
    assert inventory_project(root) == before


def test_staged_repair_partial_rollback_keeps_plan_and_reports_only_residue(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime\n", "")
    )
    preview_code, preview_stdout, _ = invoke(
        root, ["--json", "repair", "--diff"]
    )
    preview = json.loads(preview_stdout)
    assert preview_code == 0
    issue = ValidationIssue(
        "SNV4_STAGED_REPAIR_REJECTED",
        "error",
        "plugin",
        "staged repair sentinel",
        path="android/settings.gradle",
    )
    monkeypatch.setattr(
        V4Validator,
        "validate",
        lambda self, **kwargs: V4ValidationResult(
            "failure", "1" * 64, (issue,)
        ),
    )
    original = V4CliOperationService._rollback_with_verification
    residue = Change("android/settings.gradle", "update", "rollback_residue")

    def partial_rollback(self, transaction, baseline, directory_metadata):
        rollback, _actual = original(
            self, transaction, baseline, directory_metadata
        )
        return RollbackResult(True, "partial", rollback.restored), [residue]

    monkeypatch.setattr(
        V4CliOperationService,
        "_rollback_with_verification",
        partial_rollback,
    )

    code, stdout, stderr = invoke(root, ["--json", "repair", "--yes"])
    result = json.loads(stdout)

    assert code == 3, stderr
    assert result["changes"] == preview["changes"]
    assert result["actual_changes"] == [residue.to_dict()]
    assert result["rollback"]["status"] == "partial"


@pytest.mark.parametrize(
    ("wiring_family", "application_suffix"),
    (
        ("settings", None),
        ("app_gradle", None),
        ("application", ".kt"),
        ("application", ".java"),
    ),
)
@pytest.mark.parametrize("damage", ("reversed", "marker_whitespace", "crlf"))
def test_repair_structurally_canonicalizes_every_v4_marker_variant(
    tmp_path: Path,
    wiring_family: str,
    application_suffix: str | None,
    damage: str,
):
    root = plugin(tmp_path)
    if application_suffix == ".kt":
        path = root / "android/app/src/main/java/com/example/MainApplication.kt"
        path.parent.mkdir(parents=True)
        path.write_text(
            "fun getPackages() =\n"
            "    PackageList(this).packages.apply {\n"
            "      add(UserPackage())\n"
            "    }\n"
        )
    elif application_suffix == ".java":
        path = root / "android/app/src/main/java/com/example/MainApplication.java"
        path.parent.mkdir(parents=True)
        path.write_text(
            "class MainApplication {\n"
            "  java.util.List getPackages() {\n"
            "    java.util.List packages = new PackageList(this).getPackages();\n"
            "    packages.add(new UserPackage());\n"
            "    return packages;\n"
            "  }\n"
            "}\n"
        )
    elif wiring_family == "settings":
        path = root / "android/settings.gradle"
    else:
        path = root / "android/app/build.gradle"

    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    text = path.read_text()
    package_marker = wiring_family == "application"
    start = (
        "// supernote-module-v4-package"
        if package_marker
        else "// supernote-module-v4-runtime"
    )
    end = (
        "// end supernote-module-v4-package"
        if package_marker
        else "// end supernote-module-v4-runtime"
    )
    if damage == "reversed":
        text = text.replace(start, "// marker-placeholder", 1)
        text = text.replace(end, start, 1)
        text = text.replace("// marker-placeholder", end, 1)
    elif damage == "marker_whitespace":
        text = text.replace(start, "  " + start + "  ", 1)
        text = text.replace(end, "  " + end + "  ", 1)
    else:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    before = inventory_project(root)

    preview_code, preview_stdout, preview_stderr = invoke(
        root, ["--json", "repair", "--diff"]
    )
    preview = json.loads(preview_stdout)
    assert preview_code == 0, (preview_stderr, preview)
    assert preview["metadata"]["dry_run"] is True
    assert any(change["path"] == str(path) for change in preview["changes"])
    assert inventory_project(root) == before

    repair_code, repair_stdout, repair_stderr = invoke(
        root, ["--json", "repair", "--yes"]
    )
    repaired = json.loads(repair_stdout)
    assert repair_code == 0, (repair_stderr, repaired)
    assert repaired["status"] == "success"
    assert any(change["path"] == str(path) for change in repaired["actual_changes"])
    repaired_text = path.read_text()
    assert repaired_text.count(start) == 1
    assert repaired_text.count(end) == 1
    if package_marker:
        assert "UserPackage" in repaired_text
    check_code, check_stdout, check_stderr = invoke(root, ["--json", "check"])
    assert check_code == 0, (check_stderr, json.loads(check_stdout))


def test_repair_rejects_manifest_claim_of_user_feature_root(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    sentinel = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    before = sentinel.read_bytes()
    manifest_path = root / ".supernote-module/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"].append(
        {
            "path": "local_modules/alpha",
            "owner": "feature:alpha",
            "kind": "feature-metadata",
            "sha256": "0" * 64,
            "generation_id": manifest["generation_id"],
            "committed_source": True,
        }
    )
    manifest_path.write_text(json.dumps(manifest) + "\n")

    code, stdout, _ = invoke(root, ["--json", "repair", "--yes"])
    value = json.loads(stdout)

    assert code != 0
    assert value["error"]["kind"] == "invalid_metadata"
    assert sentinel.read_bytes() == before
    assert (root / "local_modules/alpha").is_dir()


def test_check_and_dry_run_preserve_complete_tree_and_mtimes_with_devconfig(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    java_home = tmp_path / "tools/jdk"
    (java_home / "bin").mkdir(parents=True)
    java = java_home / "bin" / ("java.exe" if os.name == "nt" else "java")
    java.write_text("")
    java.chmod(0o755)
    sdk = tmp_path / "tools/sdk"
    (sdk / "platforms/android-35").mkdir(parents=True)
    (sdk / "platforms/android-35/android.jar").write_bytes(b"")
    build_tools = sdk / "build-tools/35.0.0"
    build_tools.mkdir(parents=True)
    for name in (
        "aapt2.exe" if os.name == "nt" else "aapt2",
        "zipalign.exe" if os.name == "nt" else "zipalign",
        "apksigner.bat" if os.name == "nt" else "apksigner",
    ):
        tool = build_tools / name
        tool.write_text("")
        tool.chmod(0o755)
    (sdk / "platform-tools").mkdir(parents=True)
    adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    adb.write_text("")
    adb.chmod(0o755)
    (root / "devconfig.json").write_text(
        json.dumps(
            {"javaHome": str(java_home), "androidSdk": str(sdk), "adb": str(adb)}
        )
        + "\n"
    )
    local_properties = root / "android/local.properties"
    local_properties.write_text("sdk.dir=/preserved\n")

    before = inventory_project(root)
    mtimes = {
        path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
        for path in root.rglob("*")
    }
    assert invoke(root, ["--json", "check"])[0] == 0
    assert invoke(root, ["--json", "update", "--all", "--dry-run", "--diff"])[0] == 0

    assert inventory_project(root) == before
    assert {
        path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
        for path in root.rglob("*")
    } == mtimes
    assert local_properties.read_text() == "sdk.dir=/preserved\n"
    assert not list((root / "local_modules").glob(".*.tmp-*"))


def test_check_and_dry_run_succeed_with_read_only_source_tree(tmp_path: Path):
    if os.name != "posix":
        pytest.skip("POSIX permission fixture")
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    try:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)

        assert invoke(root, ["--json", "check"])[0] == 0
        assert invoke(root, ["--json", "update", "--all", "--dry-run"])[0] == 0
    finally:
        root.chmod(0o755)
        for path in reversed(paths):
            if path.exists():
                path.chmod(0o755 if path.is_dir() else 0o644)


def test_check_build_json_exposes_additive_build_and_diagnostics(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    wrapper = root / "android/gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)

    def successful_build(command, *, cwd, timeout, env):
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        successful_build,
    )

    code, stdout, stderr = invoke(root, ["--json", "check", "--build"])
    payload = json.loads(stdout)

    assert code == 0
    assert stderr == ""
    assert payload["validation"]["build"] == "passed"
    assert payload["issues"] == []
    assert len(payload["diagnostics"]) == 1
    assert Path(payload["diagnostics"][0]).is_file()
    assert payload["requested_targets"] == []
    assert payload["affected_targets"] == []
    assert payload["next_action"] is None


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_check_build_restores_source_writes_before_returning(
    tmp_path: Path,
    monkeypatch,
    outcome: str,
):
    root = plugin(tmp_path)
    wrapper = root / "android/gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    generated = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)

    def source_writing_build(command, *, cwd, timeout, env):
        generated.write_text(generated.read_text() + "// build mutation\n")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(
            command,
            0 if outcome == "success" else 1,
            "BUILD SUCCESSFUL\n" if outcome == "success" else "",
            "compiler failure\n" if outcome == "failure" else "",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        source_writing_build,
    )

    code, stdout, stderr = invoke(root, ["--json", "check", "--build"])
    payload = json.loads(stdout)

    assert stderr == ""
    assert source_tree_inventory(root) == before
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    if outcome == "interrupt":
        assert code == 130
        assert payload["status"] == "cancelled"
        assert payload["cancellation"]["status"] == "completed"
    else:
        assert code == 1
        assert payload["status"] == "failure"
        assert any(
            issue["code"] == "SNV4_BUILD_MUTATED_SOURCE"
            for issue in payload["issues"]
        )


@pytest.mark.parametrize(
    "arguments",
    (
        ["--json", "check"],
        ["--json", "validate", "--all"],
    ),
)
@pytest.mark.parametrize("outcome", ("return", "error", "interrupt"))
def test_nonbuild_validator_stage_owns_and_restores_its_backup(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
    outcome: str,
):
    root = plugin(tmp_path)
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    def mutating_validation(self, **kwargs):
        source.write_text(source.read_text() + "// validation mutation\n")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "return":
            return V4ValidationResult("success", "generation-sentinel", ())
        raise RuntimeError("validation stage failed after writing source")

    monkeypatch.setattr(V4Validator, "validate", mutating_validation)

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert stderr == ""
    assert source_tree_inventory(root) == before
    assert protected_directory_metadata(root) == before_directories
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert payload["recovery"] is None
    if outcome == "interrupt":
        assert code == 130
        assert payload["status"] == "cancelled"
        assert payload["cancellation"]["status"] == "completed"
    else:
        assert code == 1
        assert payload["status"] == "failure"
        assert payload["error"]["kind"] == "validation_mutated_source"
        assert payload["error"]["phase"] == "check"
        assert any(
            issue["code"] == "SNV4_VALIDATION_MUTATED_SOURCE"
            for issue in payload["issues"]
        )


@pytest.mark.parametrize(
    "arguments",
    (
        ["--json", "check"],
        ["--json", "validate", "--all"],
        ["--json", "check", "--build"],
        ["--json", "validate", "--all", "--build"],
    ),
)
@pytest.mark.parametrize("retry", ("success", "failure"))
def test_validator_guard_finalization_interrupt_is_recovered_or_actionable(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
    retry: str,
):
    root = plugin(tmp_path)
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)
    build = "--build" in arguments

    def mutating_validation(self, **kwargs):
        source.write_text(source.read_text() + "// validator mutation\n")
        return V4ValidationResult(
            "success",
            "generation-sentinel",
            (),
            build="passed" if build else "not_run",
            diagnostics=("/tmp/validator-build.log",) if build else (),
            build_duration_ms=17 if build else 0,
        )

    monkeypatch.setattr(V4Validator, "validate", mutating_validation)
    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0
    recovery_paths: list[Path] = []

    def interrupt_then_finish(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            return original_finish(self)
        if finish_calls == 2:
            recovery_paths.append(self.recovery_path)
            raise KeyboardInterrupt
        if retry == "failure":
            raise RuntimeError("forced guard completion failure")
        return original_finish(self)

    monkeypatch.setattr(ProtectedSourceGuard, "finish", interrupt_then_finish)

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert stderr == ""
    assert finish_calls == 3
    expected_issue = (
        "SNV4_BUILD_MUTATED_SOURCE"
        if build
        else "SNV4_VALIDATION_MUTATED_SOURCE"
    )
    assert any(issue["code"] == expected_issue for issue in payload["issues"])
    if retry == "success":
        assert code == 130
        assert payload["status"] == "cancelled"
        assert payload["rollback"]["status"] == "completed"
        assert payload["cancellation"]["status"] == "completed"
        assert payload["recovery"] is None
        assert source_tree_inventory(root) == before
        assert protected_directory_metadata(root) == before_directories
        assert not any(path.exists() for path in recovery_paths)
    else:
        assert code == 3
        assert payload["status"] == "partial"
        assert payload["rollback"]["status"] == "partial"
        assert payload["cancellation"]["status"] == "partial"
        assert payload["recovery"] is not None
        recovery = Path(payload["metadata"]["recovery_path"])
        assert recovery == recovery_paths[0]
        assert recovery.exists()
        assert restore_protected_source_backup(recovery, root) == ()
        assert source_tree_inventory(root) == before
        assert protected_directory_metadata(root) == before_directories
        remove_entry_no_follow(recovery)


@pytest.mark.parametrize(
    "arguments",
    (
        ["--json", "check"],
        ["--json", "validate", "--all"],
        ["--json", "check", "--build"],
        ["--json", "validate", "--all", "--build"],
    ),
)
@pytest.mark.parametrize("retry", ("success", "failure"))
def test_frontend_guard_finalization_interrupt_is_recovered_or_actionable(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
    retry: str,
):
    root = plugin(tmp_path)
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    def mutating_frontend(self):
        source.write_text(source.read_text() + "// frontend mutation\n")
        return {}

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend
    )
    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0
    recovery_paths: list[Path] = []

    def interrupt_then_finish(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            recovery_paths.append(self.recovery_path)
            raise KeyboardInterrupt
        if retry == "failure":
            raise RuntimeError("forced frontend guard completion failure")
        return original_finish(self)

    monkeypatch.setattr(ProtectedSourceGuard, "finish", interrupt_then_finish)

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert stderr == ""
    assert finish_calls == 2
    assert any(
        issue["code"] == "SNV4_FRONTEND_MUTATED_SOURCE"
        for issue in payload["issues"]
    )
    if retry == "success":
        assert code == 130
        assert payload["status"] == "cancelled"
        assert payload["rollback"]["status"] == "completed"
        assert payload["cancellation"]["status"] == "completed"
        assert payload["recovery"] is None
        assert source_tree_inventory(root) == before
        assert protected_directory_metadata(root) == before_directories
        assert not any(path.exists() for path in recovery_paths)
    else:
        assert code == 3
        assert payload["status"] == "partial"
        assert payload["rollback"]["status"] == "partial"
        assert payload["cancellation"]["status"] == "partial"
        recovery = Path(payload["metadata"]["recovery_path"])
        assert recovery == recovery_paths[0]
        assert recovery.exists()
        assert restore_protected_source_backup(recovery, root) == ()
        assert source_tree_inventory(root) == before
        assert protected_directory_metadata(root) == before_directories
        remove_entry_no_follow(recovery)


def test_validate_uses_authoritative_v4_integrity_before_build(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    (root / "local_modules/alpha/index.js").write_text("const = ;\n")
    invoked = False

    def unexpected_build(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("build must not run after authoritative integrity failure")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        unexpected_build,
    )

    code, stdout, stderr = invoke(
        root, ["--json", "validate", "alpha", "--build"]
    )
    payload = json.loads(stdout)

    assert code == 1
    assert stderr == ""
    assert invoked is False
    assert payload["validation"]["build"] == "not_run"
    assert {issue["code"] for issue in payload["issues"]} >= {
        "SNV4_ARTIFACT_MODIFIED",
        "SNV4_JAVASCRIPT_INVALID",
    }
    assert payload["requested_targets"] == ["alpha"]
    assert payload["affected_targets"] == ["alpha"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--json", "check"],
        ["--json", "check", "--build"],
        ["--json", "repair", "--diff"],
    ],
)
def test_read_only_frontend_mutation_is_rejected_and_restored(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "android/app/src/main/java/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("class App\n")
    before = source.read_bytes()
    def mutating_frontend(self):
        source.write_text("class Mutated\n")
        return {}

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend
    )
    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "BUILD SUCCESSFUL\n", ""
        ),
    )

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert code == 1, (stderr, payload.get("error"), payload.get("metadata"))
    assert payload["error"]["kind"] == "frontend_mutated_source"
    assert payload["issues"][0]["code"] == "SNV4_FRONTEND_MUTATED_SOURCE"
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert source.read_bytes() == before


def test_update_all_frontend_mutation_rolls_back_before_noop(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "android/app/src/main/java/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("class App\n")
    before = source.read_bytes()

    def mutating_frontend(self):
        source.write_text("class Mutated\n")
        return {}

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend
    )

    code, stdout, stderr = invoke(
        root, ["--json", "update", "--all", "--yes"]
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "frontend_mutated_source"
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["--json", "check"],
        ["--json", "repair", "--diff"],
        ["--json", "update", "--all", "--yes"],
    ],
)
def test_frontend_interrupt_restores_source_and_cleans_recovery_state(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "android/app/src/main/java/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("class App\n")
    before = source.read_bytes()
    directory_mtimes = {
        ".": root.lstat().st_mtime_ns,
        **{
            path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
            for path in root.rglob("*")
            if path.is_dir()
        },
    }
    temporary_root = Path(tempfile.gettempdir())
    guards_before = set(temporary_root.glob("supernote-v4-source-guard-*"))

    def interrupting_frontend(self):
        source.write_text("class Mutated\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", interrupting_frontend
    )

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert code == 130, stderr
    assert payload["status"] == "cancelled"
    assert payload["cancellation"]["requested"] is True
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert source.read_bytes() == before
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    assert set(temporary_root.glob("supernote-v4-source-guard-*")) == guards_before
    assert {
        ".": root.lstat().st_mtime_ns,
        **{
            path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
            for path in root.rglob("*")
            if path.is_dir()
        },
    } == directory_mtimes


@pytest.mark.parametrize("failure_point", ["before", "during", "after"])
def test_protected_guard_retains_backup_without_losing_live_entry_on_restore_fault(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
):
    root = plugin(tmp_path)
    source = root / "sentinel.txt"
    source.write_text("baseline\n")
    guard = ProtectedSourceGuard(root)
    source.write_text("mutated\n")

    import supernote_module_generator.filesystem as filesystem

    original_copy = filesystem.copy_entry_no_follow
    original_replace = filesystem.os.replace
    original_remove = filesystem.remove_entry_no_follow
    fired = False

    def failing_copy(src, dst):
        nonlocal fired
        if failure_point == "before" and "supernote-v4-restore" in Path(dst).name:
            fired = True
            raise OSError("copy fault")
        return original_copy(src, dst)

    def failing_replace(src, dst):
        nonlocal fired
        if failure_point == "during" and "supernote-v4-restore" in Path(src).name:
            fired = True
            raise OSError("activation fault")
        return original_replace(src, dst)

    def failing_remove(path):
        nonlocal fired
        if failure_point == "after" and "supernote-v4-displaced" in Path(path).name:
            fired = True
            raise OSError("cleanup fault")
        return original_remove(path)

    monkeypatch.setattr(filesystem, "copy_entry_no_follow", failing_copy)
    monkeypatch.setattr(filesystem.os, "replace", failing_replace)
    monkeypatch.setattr(filesystem, "remove_entry_no_follow", failing_remove)

    with pytest.raises(ProtectedSourceRestoreError) as raised:
        guard.finish()

    assert fired is True
    assert source.is_file()
    assert source.read_text() in {"baseline\n", "mutated\n"}
    assert raised.value.recovery_path.is_dir()
    assert any(raised.value.recovery_path.iterdir())

    monkeypatch.setattr(filesystem, "copy_entry_no_follow", original_copy)
    monkeypatch.setattr(filesystem.os, "replace", original_replace)
    monkeypatch.setattr(filesystem, "remove_entry_no_follow", original_remove)
    for displaced in root.glob(".*.supernote-v4-displaced-*"):
        original_remove(displaced)
    shutil.rmtree(raised.value.recovery_path)


def test_guard_restore_failure_reports_residue_and_recovery_backup(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "sentinel.txt"
    source.write_text("baseline\n")
    manifest = root / ".supernote-module/manifest.json"
    manifest_before = manifest.read_bytes()
    temporary_root = Path(tempfile.gettempdir())
    guards_before = set(temporary_root.glob("supernote-v4-source-guard-*"))

    def mutating_frontend(self):
        source.write_text("mutated\n")
        return {}

    import supernote_module_generator.filesystem as filesystem

    original_copy = filesystem.copy_entry_no_follow

    def failing_restore_copy(src, dst):
        if "supernote-v4-restore" in Path(dst).name:
            raise OSError("disk full")
        return original_copy(src, dst)

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend
    )
    monkeypatch.setattr(filesystem, "copy_entry_no_follow", failing_restore_copy)

    code, stdout, stderr = invoke(root, ["--json", "check"])
    payload = json.loads(stdout)
    retained = set(temporary_root.glob("supernote-v4-source-guard-*")) - guards_before

    assert code == 3, stderr
    assert payload["status"] == "partial"
    assert payload["rollback"]["status"] == "partial"
    assert payload["actual_changes"]
    assert payload["recovery"] is not None
    assert len(retained) == 1
    assert source.read_text() == "mutated\n"
    assert manifest.read_bytes() == manifest_before

    recovery = retained.copy().pop()
    recovery_manifest = json.loads(
        (recovery / "recovery-manifest.json").read_text()
    )
    assert recovery_manifest["schema_version"] == 1
    assert recovery_manifest["plugin_root"] == str(root)
    assert recovery_manifest["directories"]
    sentinel_entry = next(
        entry
        for entry in recovery_manifest["entries"]
        if entry["destination"] == "sentinel.txt"
    )
    assert sentinel_entry["kind"] == "file"
    original_copy(
        recovery / sentinel_entry["backup"],
        root / sentinel_entry["destination"],
    )
    assert source.read_text() == "baseline\n"

    shutil.rmtree(recovery)


def test_retained_guard_backup_restores_content_and_directory_metadata_fresh(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    before = source.read_bytes()
    directory_before = protected_directory_metadata(root)
    temporary_root = Path(tempfile.gettempdir())
    guards_before = set(temporary_root.glob("supernote-v4-source-guard-*"))

    def mutating_frontend(self):
        source.write_text("mutated\n")
        os.utime(source.parent, ns=(1_000_000_000, 2_000_000_000))
        return {}

    import supernote_module_generator.filesystem as filesystem
    original_copy = filesystem.copy_entry_no_follow

    def failing_restore_copy(src, dst):
        if "supernote-v4-restore" in Path(dst).name:
            raise OSError("restore unavailable")
        return original_copy(src, dst)

    monkeypatch.setattr(V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend)
    monkeypatch.setattr(filesystem, "copy_entry_no_follow", failing_restore_copy)
    code, stdout, stderr = invoke(root, ["--json", "check"])
    assert code == 3, stderr
    retained = set(temporary_root.glob("supernote-v4-source-guard-*")) - guards_before
    assert len(retained) == 1
    recovery = retained.pop()

    monkeypatch.setattr(filesystem, "copy_entry_no_follow", original_copy)
    assert restore_protected_source_backup(recovery, root) == ()
    assert {
        relative: (
            (root if relative == "." else root / relative).lstat().st_mode & 0o7777,
            (root if relative == "." else root / relative).lstat().st_mtime_ns,
        )
        for relative in directory_before
    } == {
        relative: (mode, mtime_ns)
        for relative, (mode, _atime_ns, mtime_ns) in directory_before.items()
    }
    assert source.read_bytes() == before
    shutil.rmtree(recovery)


@pytest.mark.parametrize(
    "field,unsafe",
    [
        ("destination", "../outside/sentinel.txt"),
        ("destination", "/tmp/outside/sentinel.txt"),
        ("destination", "C:/outside/sentinel.txt"),
        ("destination", "//server/share/sentinel.txt"),
        ("destination", r"..\outside\sentinel.txt"),
        ("backup", "../outside/sentinel.txt"),
        ("backup", "C:/outside/sentinel.txt"),
        ("backup", r"..\outside\sentinel.txt"),
    ],
)
def test_retained_backup_rejects_unsafe_paths_before_mutation(
    tmp_path: Path,
    field: str,
    unsafe: str,
):
    root = tmp_path / "plugin"
    root.mkdir()
    recovery = tmp_path / "recovery"
    backup = recovery / "entries/0"
    backup.parent.mkdir(parents=True)
    backup.write_text("baseline\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n")
    entry = {
        "backup": "entries/0",
        "destination": "local_modules/alpha/sentinel.txt",
        "kind": "file",
        "mode": 0o644,
        "sha256": hash_entry_no_follow(backup),
    }
    entry[field] = unsafe
    (recovery / "recovery-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin_root": str(root.resolve()),
                "entries": [entry],
                "directories": [],
            }
        )
    )

    with pytest.raises(Exception, match="recovery|path|canonical"):
        restore_protected_source_backup(recovery, root)

    assert sentinel.read_text() == "outside\n"
    assert not (root / "local_modules").exists()


@pytest.mark.parametrize("symlink_side", ["source", "destination"])
def test_retained_backup_rejects_symlink_ancestors(
    tmp_path: Path,
    symlink_side: str,
):
    root = tmp_path / "plugin"
    root.mkdir()
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n")
    if symlink_side == "source":
        source = outside / "source.txt"
        source.write_text("baseline\n")
        (recovery / "entries").symlink_to(outside, target_is_directory=True)
        backup_relative = "entries/source.txt"
    else:
        entries = recovery / "entries"
        entries.mkdir()
        source = entries / "source.txt"
        source.write_text("baseline\n")
        (root / "local_modules").symlink_to(outside, target_is_directory=True)
        backup_relative = "entries/source.txt"
    (recovery / "recovery-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin_root": str(root.resolve()),
                "entries": [
                    {
                        "backup": backup_relative,
                        "destination": "local_modules/alpha/sentinel.txt",
                        "kind": "file",
                        "mode": 0o644,
                        "sha256": hash_entry_no_follow(source),
                    }
                ],
                "directories": [],
            }
        )
    )

    with pytest.raises(Exception, match="symbolic-link|unsafe"):
        restore_protected_source_backup(recovery, root)

    assert sentinel.read_text() == "outside\n"


@pytest.mark.parametrize(
    "relative,expected_scope,expected_target",
    [
        (
            "local_modules/alpha/android/src/main/cpp/feature.cpp",
            "feature",
            "alpha",
        ),
        (
            "android/.supernote-module/v4-runtime/feature-registry.json",
            "runtime",
            "shared runtime",
        ),
        ("android/app/src/main/java/App.kt", "plugin", "plugin wiring"),
    ],
)
def test_frontend_mutation_scope_and_target_follow_changed_path(
    tmp_path: Path,
    monkeypatch,
    relative: str,
    expected_scope: str,
    expected_target: str,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.write_text("baseline\n")
    before = source.read_bytes()

    def mutating_frontend(self):
        source.write_bytes(before + b"mutated\n")
        return {}

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", mutating_frontend
    )

    code, stdout, stderr = invoke(root, ["--json", "check"])
    payload = json.loads(stdout)
    issue = payload["issues"][0]

    assert code == 1, stderr
    assert issue["scope"] == expected_scope
    assert issue["path"] == relative
    assert payload["affected_targets"] == [expected_target]
    if expected_scope == "feature":
        assert issue["feature_id"]
    else:
        assert "feature_id" not in issue
    assert source.read_bytes() == before


def test_partial_restore_classifies_deleted_feature_from_pre_frontend_model(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    feature = root / "local_modules/alpha"
    temporary_root = Path(tempfile.gettempdir())
    guards_before = set(temporary_root.glob("supernote-v4-source-guard-*"))

    def deleting_frontend(self):
        shutil.rmtree(feature)
        return {}

    import supernote_module_generator.filesystem as filesystem

    original_copy = filesystem.copy_entry_no_follow

    def failing_restore_copy(src, dst):
        if "supernote-v4-restore" in Path(dst).name:
            raise OSError("restore unavailable")
        return original_copy(src, dst)

    monkeypatch.setattr(
        V4CliOperationService, "_jvm_frontend_manifests", deleting_frontend
    )
    monkeypatch.setattr(filesystem, "copy_entry_no_follow", failing_restore_copy)

    code, stdout, stderr = invoke(root, ["--json", "check"])
    payload = json.loads(stdout)
    retained = set(temporary_root.glob("supernote-v4-source-guard-*")) - guards_before

    assert code == 3, stderr
    feature_issues = [
        issue
        for issue in payload["issues"]
        if issue.get("path", "").startswith("local_modules/alpha")
    ]
    assert feature_issues
    assert all(issue["scope"] == "feature" for issue in feature_issues)
    assert all(issue.get("feature_id") for issue in feature_issues)
    assert payload["affected_targets"] == ["alpha"]
    assert len(retained) == 1

    shutil.rmtree(retained.pop())


@pytest.mark.parametrize("has_changes", [False, True])
@pytest.mark.parametrize(
    "interrupt_point",
    [
        "before_commit_persist",
        "after_commit_persist",
        "after_state_removal",
        "after_journal_unlink",
        "after_commit_return",
    ],
)
def test_interrupt_at_commit_boundary_is_recovered_idempotently(
    tmp_path: Path,
    monkeypatch,
    has_changes: bool,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    if not has_changes:
        assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    before = inventory_project(root)
    before_directory_mtimes = {
        ".": root.lstat().st_mtime_ns,
        **{
            path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
            for path in root.rglob("*")
            if path.is_dir()
        },
    }
    fired = False
    original_persist = Transaction._persist
    original_finish = Transaction.finish_commit
    original_commit = Transaction.commit
    original_checkpoint = Transaction.checkpoint

    if interrupt_point in {"before_commit_persist", "after_commit_persist"}:
        def interrupting_persist(self):
            nonlocal fired
            if self.data.get("phase") == "commit" and not fired:
                fired = True
                if interrupt_point == "after_commit_persist":
                    original_persist(self)
                raise KeyboardInterrupt
            return original_persist(self)

        monkeypatch.setattr(Transaction, "_persist", interrupting_persist)
    elif interrupt_point == "after_state_removal":
        def interrupting_checkpoint(self, name):
            nonlocal fired
            original_checkpoint(self, name)
            if name == "after_abandon_state_removal" and not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "checkpoint", interrupting_checkpoint)
    elif interrupt_point == "after_journal_unlink":
        def interrupting_finish(self):
            nonlocal fired
            if not fired:
                original_finish(self)
                fired = True
                raise KeyboardInterrupt
            return original_finish(self)

        monkeypatch.setattr(Transaction, "finish_commit", interrupting_finish)
    else:
        def interrupting_commit(self):
            nonlocal fired
            original_commit(self)
            if not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "commit", interrupting_commit)

    code, stdout, stderr = invoke(
        root, ["--json", "update", "--all", "--yes"]
    )
    payload = json.loads(stdout)

    assert fired is True
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    if interrupt_point == "before_commit_persist":
        assert code == 130, stderr
        assert payload["status"] == "cancelled"
        assert payload["rollback"]["status"] == "completed"
        assert inventory_project(root) == before
        assert {
            ".": root.lstat().st_mtime_ns,
            **{
                path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
                for path in root.rglob("*")
                if path.is_dir()
            },
        } == before_directory_mtimes
    else:
        assert code == 0, stderr
        assert payload["status"] == "success"
        assert payload["metadata"]["commit_durable"] is True
        assert payload["cancellation"]["status"] == "completed"
        assert invoke(root, ["--json", "check"])[0] == 0


def test_dependency_edit_after_plan_is_preserved_as_precommit_conflict(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"].pop("alpha")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    original_execute = GenerationService.execute

    def concurrent_edit(self, plan, transaction, *, commit=True):
        concurrent = json.loads(package_path.read_text())
        concurrent["concurrent_user_edit"] = "preserve me"
        package_path.write_text(json.dumps(concurrent, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)

    code, stdout, stderr = invoke(
        root, ["--json", "update", "--all", "--yes"]
    )
    payload = json.loads(stdout)
    after = json.loads(package_path.read_text())

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert after["concurrent_user_edit"] == "preserve me"
    assert "alpha" not in after["dependencies"]
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("authority", ("runtime", "feature"))
@pytest.mark.parametrize("command", ("update", "repair"))
def test_ownership_change_after_authorization_is_a_precommit_conflict(
    tmp_path: Path,
    monkeypatch,
    authority: str,
    command: str,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    runtime = root / "android/.supernote-module/v4-runtime"
    feature = root / "local_modules/alpha"
    if authority == "runtime":
        metadata = runtime / "ownership.json"
        sentinel_relative = "unowned-sentinel.txt"
        sentinel = runtime / sentinel_relative
        referenced = runtime / "src/runtime_services.cpp"
    else:
        metadata = feature / ".supernote-module.json"
        sentinel_relative = "android/src/main/cpp/external-sentinel.cpp"
        sentinel = feature / sentinel_relative
        referenced = feature / "index.js"
    referenced_before = referenced.read_bytes()
    original = GenerationService._load_prior_manifest
    changed_metadata: bytes | None = None

    def changing_authority(self, project, *, operation, requested_targets):
        nonlocal changed_metadata
        manifest = original(
            self,
            project,
            operation=operation,
            requested_targets=requested_targets,
        )
        value = json.loads(metadata.read_text())
        value["generated_files"].append(sentinel_relative)
        changed_metadata = (
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        ).encode()
        metadata.write_bytes(changed_metadata)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("external bytes must survive\n")
        return manifest

    monkeypatch.setattr(
        GenerationService,
        "_load_prior_manifest",
        changing_authority,
    )
    arguments = (
        ["--json", "update", "--all", "--yes"]
        if command == "update"
        else ["--json", "repair", "--yes"]
    )

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert code == 1, (stderr, stdout)
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["rollback"]["status"] == "completed"
    assert payload["actual_changes"] == []
    assert sentinel.read_text() == "external bytes must survive\n"
    assert metadata.read_bytes() == changed_metadata
    assert referenced.read_bytes() == referenced_before
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_semantic_source_race_invalidates_an_update_all_noop(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    original_plan = GenerationService.plan

    def racing_plan(self, **kwargs):
        plan = original_plan(self, **kwargs)
        source.write_text(source.read_text() + "\n// concurrent semantic edit\n")
        return plan

    monkeypatch.setattr(GenerationService, "plan", racing_plan)
    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert "concurrent semantic edit" in source.read_text()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("candidate_kind", ["invalid", "unscoped", "scoped"])
@pytest.mark.parametrize("targeted", [False, True])
def test_feature_discovery_frontier_invalidates_concurrent_new_feature(
    tmp_path: Path,
    monkeypatch,
    candidate_kind: str,
    targeted: bool,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    if targeted:
        gradle = root / "android/gradlew"
        gradle.write_text("#!/bin/sh\nexit 0\n")
        gradle.chmod(0o755)
    original_plan = GenerationService.plan
    npm_name = "@scope/beta" if candidate_kind == "scoped" else "beta"
    destination = (
        root / "local_modules/@scope/beta"
        if candidate_kind == "scoped"
        else root / "local_modules/beta"
    )

    def racing_plan(self, **kwargs):
        plan = original_plan(self, **kwargs)
        if candidate_kind == "invalid":
            destination.mkdir(parents=True)
            (destination / ".supernote-module.json").write_text("{}\n")
        else:
            staged = stage_feature(
                FeatureConfig(
                    destination,
                    npm_name,
                    "4.0.0",
                    "com.example.beta",
                    "Beta",
                    starters=(StarterFamily.NATIVE,),
                )
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
        return plan

    monkeypatch.setattr(GenerationService, "plan", racing_plan)
    arguments = (
        ["--json", "update", "alpha", "--skip-install", "--yes"]
        if targeted
        else ["--json", "update", "--all", "--yes"]
    )
    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert code == 1, (stderr, stdout)
    assert payload["error"]["kind"] == "plan_conflict"
    assert "feature discovery" in payload["error"]["message"]
    assert destination.exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("changing", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_frontier_conflict_preserves_external_directory_metadata_exactly(
    tmp_path: Path,
    monkeypatch,
    targeted: bool,
    changing: bool,
    scoped: bool,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    if changing:
        source.write_text(source.read_text().replace("greet(", "greetAgain("))
    npm_name = "@scope/beta" if scoped else "beta"
    destination = (
        root / "local_modules/@scope/beta"
        if scoped
        else root / "local_modules/beta"
    )
    original_validate = GenerationService.validate_preconditions
    expected: dict[str, tuple[int, int, int]] = {}
    injected = False

    def conflicting_validate(self, plan):
        nonlocal injected
        if not injected:
            injected = True
            staged = stage_feature(
                FeatureConfig(
                    destination,
                    npm_name,
                    "4.0.0",
                    "com.example.beta",
                    "Beta",
                    starters=(StarterFamily.NATIVE,),
                )
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
        try:
            return original_validate(self, plan)
        except PlanConflictError:
            parents = [root / "local_modules"]
            if scoped:
                parents.append(root / "local_modules/@scope")
            for index, parent in enumerate(parents):
                mode = 0o750 - index * 0o20
                atime_ns = 5_000_000_000 + index * 2_000_000_000
                mtime_ns = 6_000_000_000 + index * 2_000_000_000
                parent.chmod(mode)
                os.utime(parent, ns=(atime_ns, mtime_ns))
                expected[parent.relative_to(root).as_posix()] = (
                    mode,
                    atime_ns,
                    mtime_ns,
                )
            raise

    monkeypatch.setattr(
        GenerationService, "validate_preconditions", conflicting_validate
    )
    if targeted:
        gradle = root / "android/gradlew"
        gradle.write_text("#!/bin/sh\nexit 0\n")
        gradle.chmod(0o755)
    arguments = (
        ["--json", "update", "alpha", "--skip-install", "--yes"]
        if targeted
        else ["--json", "update", "--all", "--yes"]
    )
    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert code == 1, (stderr, payload.get("error"), payload.get("metadata"))
    assert payload["error"]["kind"] == "plan_conflict"
    assert destination.is_dir()
    for relative, values in expected.items():
        current = (root / relative).lstat()
        assert current.st_mode & 0o7777 == values[0]
        assert current.st_atime_ns == values[1]
        assert current.st_mtime_ns == values[2]
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_unchanged_artifact_race_invalidates_dependency_only_plan(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"].pop("alpha")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    wrapper = root / "local_modules/alpha/index.js"
    original_execute = GenerationService.execute

    def racing_execute(self, plan, transaction, *, commit=True):
        wrapper.write_text("// concurrent corruption\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", racing_execute)
    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert wrapper.read_text() == "// concurrent corruption\n"
    assert "alpha" not in json.loads(package_path.read_text())["dependencies"]


def test_dependency_update_preserves_mode_and_rejects_mode_race(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    package_path.chmod(0o600)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    assert package_path.stat().st_mode & 0o7777 == 0o600

    package = json.loads(package_path.read_text())
    package["dependencies"].pop("alpha")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    package_path.chmod(0o600)
    original_execute = GenerationService.execute

    def racing_execute(self, plan, transaction, *, commit=True):
        package_path.chmod(0o640)
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", racing_execute)
    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert package_path.stat().st_mode & 0o7777 == 0o640
    assert "alpha" not in json.loads(package_path.read_text())["dependencies"]


@pytest.mark.parametrize(
    "interrupt_point",
    [
        "before_abandon_persist",
        "after_abandon_persist",
        "after_abandon_staging_removal",
        "after_abandon_state_removal",
        "after_abandon_journal_unlink",
        "after_abandon_return",
    ],
)
def test_precommit_conflict_abandon_is_interrupt_durable(
    tmp_path: Path,
    monkeypatch,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"].pop("alpha")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    original_execute = GenerationService.execute
    original_persist = Transaction._persist
    original_abandon = Transaction.abandon_unmutated
    original_checkpoint = Transaction.checkpoint
    fired = False

    def concurrent_edit(self, plan, transaction, *, commit=True):
        current = json.loads(package_path.read_text())
        current["concurrent_user_edit"] = "keep"
        package_path.write_text(json.dumps(current, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    if interrupt_point in {"before_abandon_persist", "after_abandon_persist"}:
        def interrupting_persist(self):
            nonlocal fired
            if self.data.get("phase") == "abandon" and not fired:
                fired = True
                if interrupt_point == "after_abandon_persist":
                    original_persist(self)
                raise KeyboardInterrupt
            return original_persist(self)

        monkeypatch.setattr(Transaction, "_persist", interrupting_persist)
    elif interrupt_point == "after_abandon_return":
        def interrupting_abandon(self):
            nonlocal fired
            original_abandon(self)
            if not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "abandon_unmutated", interrupting_abandon)
    else:
        def interrupting_checkpoint(self, name):
            nonlocal fired
            original_checkpoint(self, name)
            if name == interrupt_point and not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "checkpoint", interrupting_checkpoint)

    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert fired is True
    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "completed"
    assert json.loads(package_path.read_text())["concurrent_user_edit"] == "keep"
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_postcommit_interrupt_with_metadata_failure_retains_recovery_and_cancellation(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    def interrupt_after_commit(self):
        self.data["phase"] = "commit"
        self._persist()
        self._commit_durable = True
        raise KeyboardInterrupt

    monkeypatch.setattr(Transaction, "commit", interrupt_after_commit)
    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        lambda *_args, **_kwargs: ("modified:.",),
    )

    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert code == 3, stderr
    assert payload["status"] == "partial"
    assert payload["metadata"]["commit_durable"] is True
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "partial"
    recovery = Path(payload["metadata"]["recovery_path"])
    manifest = json.loads((recovery / "recovery-manifest.json").read_text())
    assert manifest["entries"] == []
    assert manifest["directories"]
    monkeypatch.setattr(
        "supernote_module_generator.filesystem.restore_protected_directory_metadata",
        restore_protected_directory_metadata,
    )
    recover_pending(root)


def test_postcommit_interrupt_with_recovery_bundle_failure_retains_journal(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(root, ["--json", "update", "--all", "--yes"])[0] == 0
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    source.write_text(source.read_text().replace("greet(", "greetAgain("))

    def interrupt_after_durable_marker(self):
        self.data["phase"] = "commit"
        self._persist()
        self._commit_durable = True
        raise KeyboardInterrupt

    monkeypatch.setattr(Transaction, "commit", interrupt_after_durable_marker)
    monkeypatch.setattr(
        "supernote_module_generator.transaction.retain_directory_metadata_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage full")),
    )

    code, stdout, stderr = invoke(root, ["--json", "update", "--all", "--yes"])
    payload = json.loads(stdout)

    assert code == 3, stderr
    assert payload["status"] == "partial"
    assert payload["metadata"]["commit_durable"] is True
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "partial"
    assert payload["error"]["kind"] == "commit_cleanup_failed"
    assert (root / ".supernote-module-transaction.json").exists()
    assert "journal" in payload["recovery"]["summary"].lower()
