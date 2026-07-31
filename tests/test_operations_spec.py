from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from supernote_module_generator.arguments import parse_arguments
from supernote_module_generator.cli import main
from supernote_module_generator.operations import (
    AddDecisions,
    OperationService,
    RemoveDecisions,
)
from supernote_module_generator.rendering import Renderer, TerminalCapabilities
from supernote_module_generator.workflows import DecisionCollector


def plugin(tmp_path: Path, *, npm_lock: bool = False, yarn_lock: bool = False) -> Path:
    android = tmp_path / "android"
    android.mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (android / "settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    if npm_lock:
        (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    if yarn_lock:
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=root)
    return code, stdout.getvalue(), stderr.getvalue()


def quiet_renderer() -> Renderer:
    return Renderer(
        "quiet",
        TerminalCapabilities(False, False, False, False, 80, 24),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )


@pytest.mark.parametrize("module_type", ["native", "jni", "jsi"])
def test_add_generates_every_public_module_type(tmp_path: Path, module_type: str):
    root = plugin(tmp_path)
    package = {"native": "local-basic", "jni": "local-jni", "jsi": "local-jsi"}[module_type]
    code, stdout, stderr = invoke(
        root,
        ["add", package, "--type", module_type, "--skip-install", "--yes"],
    )
    assert code == 0, stderr
    module = root / f"local_modules/{package}"
    metadata = json.loads((module / ".supernote-module.json").read_text())
    assert metadata["type"] == module_type
    assert metadata["metadata_schema"] == "1.0"
    assert ".supernote-module.json" in metadata["generated_files"]
    if module_type == "native":
        assert next((module / "android/src/main/java").rglob("Example.kt")).is_file()
    else:
        assert (module / "android/src/main/cpp/math.cpp").is_file()


def test_update_preserves_user_owned_native_source(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(root, ["add", "local-preserve", "--type", "native", "--skip-install", "--yes"])[0] == 0
    module = root / "local_modules/local-preserve"
    source = next((module / "android/src/main/java").rglob("Example.kt"))
    source.write_text(source.read_text() + "\n// user change\n", encoding="utf-8")
    link = root / "node_modules/local-preserve"
    link.parent.mkdir()
    link.symlink_to(module, target_is_directory=True)

    code, stdout, stderr = invoke(
        root, ["update", "local-preserve", "--yes"]
    )
    assert code == 0, stderr
    assert "// user change" in source.read_text(encoding="utf-8")
    assert stdout.startswith('✓ Updated module "local-preserve"')


def test_update_preserves_deleted_jni_starter_file(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(root, ["add", "local-jni", "--type", "jni", "--skip-install", "--yes"])[0] == 0
    module = root / "local_modules/local-jni"
    deleted = module / "android/src/main/cpp/text.cpp"
    deleted.unlink()
    link = root / "node_modules/local-jni"
    link.parent.mkdir()
    link.symlink_to(module, target_is_directory=True)
    code, _, stderr = invoke(root, ["update", "local-jni", "--yes"])
    assert code == 0, stderr
    assert not deleted.exists()


@pytest.mark.parametrize(
    "option",
    ["--skip-install", "--package-manager=npm"],
)
def test_update_rejects_dependency_options_when_refresh_is_not_required(
    tmp_path: Path, option: str
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "local-current", "--type", "native", "--skip-install", "--yes"],
    )[0] == 0
    module = root / "local_modules/local-current"
    link = root / "node_modules/local-current"
    link.parent.mkdir()
    link.symlink_to(module, target_is_directory=True)

    code, _, stderr = invoke(root, ["update", "local-current", option, "--yes"])

    assert code == 2
    assert "does not affect this update" in stderr


def test_add_postcondition_failure_rolls_back_all_plugin_files(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path)
    original_package = (root / "package.json").read_bytes()
    original_settings = (root / "android/settings.gradle").read_bytes()

    monkeypatch.setattr("supernote_module_generator.operations.shutil.which", lambda name: f"/fake/{name}")

    def successful_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "1.0\n", "")

    service = OperationService(root, quiet_renderer(), run=successful_run)
    result = service.add(
        AddDecisions(
            "local-unlinked",
            "native",
            "",
            "Unlinked",
            "com.example.unlinked",
            "0.1.0",
            True,
            "npm",
            False,
        )
    )
    assert result.exit_code == 1
    assert result.rollback.status == "completed"
    assert not (root / "local_modules/local-unlinked").exists()
    assert (root / "package.json").read_bytes() == original_package
    assert (root / "android/settings.gradle").read_bytes() == original_settings


def test_remove_dependency_failure_restores_source_and_parent(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path)
    assert invoke(root, ["add", "local-safe", "--type", "native", "--skip-install", "--yes"])[0] == 0
    module = root / "local_modules/local-safe"
    source = next((module / "android/src/main/java").rglob("Example.kt"))
    source.write_text(source.read_text() + "\n// retained\n", encoding="utf-8")
    monkeypatch.setattr("supernote_module_generator.operations.shutil.which", lambda name: f"/fake/{name}")
    installs = 0

    def run(command, **kwargs):
        nonlocal installs
        if command == ["npm", "install"]:
            installs += 1
            return subprocess.CompletedProcess(
                command,
                1 if installs == 1 else 0,
                "",
                "npm error simulated" if installs == 1 else "",
            )
        return subprocess.CompletedProcess(command, 0, "1.0\n", "")

    service = OperationService(root, quiet_renderer(), run=run)
    result = service.remove(RemoveDecisions(["local-safe"], False, "npm", False))
    assert result.exit_code == 1
    assert result.rollback.status == "completed"
    assert source.is_file()
    assert "// retained" in source.read_text()
    package = json.loads((root / "package.json").read_text())
    assert package["dependencies"]["local-safe"] == "file:./local_modules/local-safe"


def test_remove_recovery_failure_is_partial_and_keeps_journal(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path)
    assert invoke(root, ["add", "local-partial", "--type", "native", "--skip-install", "--yes"])[0] == 0
    monkeypatch.setattr("supernote_module_generator.operations.shutil.which", lambda name: f"/fake/{name}")

    def run(command, **kwargs):
        if command == ["npm", "install"]:
            return subprocess.CompletedProcess(command, 1, "", "npm error")
        return subprocess.CompletedProcess(command, 0, "1.0\n", "")

    result = OperationService(root, quiet_renderer(), run=run).remove(
        RemoveDecisions(["local-partial"], False, "npm", False)
    )
    assert result.exit_code == 3
    assert result.status == "partial"
    assert result.rollback.status == "partial"
    assert (root / ".supernote-module-transaction.json").is_file()


def test_package_manager_precedence_for_noninteractive_add(tmp_path: Path):
    root = plugin(tmp_path, yarn_lock=True)
    parsed = parse_arguments(
        [
            "add",
            "local-math",
            "--type",
            "native",
            "--description",
            "",
            "--javascript-name",
            "Math",
            "--android-namespace",
            "com.example.math",
            "--package-version",
            "0.1.0",
        ]
    )
    collector = DecisionCollector(root, parsed, None)
    assert collector.add().package_manager == "yarn"


def test_conflicting_lockfiles_still_require_manager_with_yes(tmp_path: Path):
    root = plugin(tmp_path, npm_lock=True, yarn_lock=True)
    code, _, stderr = invoke(root, ["add", "local-math", "--yes"])
    assert code == 2
    assert "package manager is ambiguous" in stderr


def test_skip_install_bypasses_conflicting_lockfiles(tmp_path: Path):
    root = plugin(tmp_path, npm_lock=True, yarn_lock=True)
    code, stdout, stderr = invoke(root, ["add", "local-math", "--skip-install", "--yes"])
    assert code == 0, stderr
    assert "Choose npm or Yarn, then install dependencies" in stdout


def test_quiet_success_is_exactly_one_line(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root, ["add", "local-quiet", "--type", "native", "--skip-install", "--yes", "--quiet"]
    )
    assert code == 0, stderr
    assert stdout == 'Added module "local-quiet"\n'


def test_build_mode_uses_parent_gradle_wrapper_and_changes_success_copy(tmp_path: Path):
    root = plugin(tmp_path)
    gradle = root / "android/gradlew"
    gradle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gradle.chmod(0o755)
    code, stdout, stderr = invoke(
        root,
        ["add", "local-built", "--type", "native", "--skip-install", "--build", "--yes"],
    )
    assert code == 0, stderr
    assert stdout.startswith('✓ Added and built module "local-built"\n')


def test_add_rejects_local_modules_symlink_that_escapes_plugin_root(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    root = plugin(plugin_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "local_modules").symlink_to(outside, target_is_directory=True)

    code, _, stderr = invoke(
        root,
        ["add", "local-escape", "--type", "native", "--skip-install", "--yes"],
    )

    assert code == 2
    assert "target resolves outside the Supernote plugin" in stderr
    assert not (outside / "local-escape").exists()
