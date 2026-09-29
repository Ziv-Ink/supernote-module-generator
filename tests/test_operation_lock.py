from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.feature_cli_operations import FeatureCliOperationService
from supernote_module_generator.operation_lock import (
    PluginBusyError,
    _windows_mutex_name,
    plugin_operation_lock,
)
from supernote_module_generator.platform_tools import gradle_wrapper_path
from supernote_module_generator.cli import JOURNAL_NAME


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (tmp_path / "android/app/build.gradle").write_text(
        "plugins {}\n", encoding="utf-8"
    )
    gradle = gradle_wrapper_path(tmp_path)
    if os.name == "nt":
        gradle.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        gradle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gradle.chmod(0o755)
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=root)
    return code, stdout.getvalue(), stderr.getvalue()


def _start_lock_holder(root: Path) -> subprocess.Popen[str]:
    source = Path(__file__).parents[1] / "src"
    script = (
        "import pathlib, sys, time\n"
        "from supernote_module_generator.operation_lock import plugin_operation_lock\n"
        "with plugin_operation_lock(pathlib.Path(sys.argv[1])):\n"
        "    print('LOCKED', flush=True)\n"
        "    time.sleep(60)\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "LOCKED"
    return process


def _stop_lock_holder(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)


def _directory_alias(root: Path, alias: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            pytest.skip(f"Windows junction creation unavailable: {result.stderr}")
        return
    alias.symlink_to(root, target_is_directory=True)


def test_plugin_directory_lock_is_nonblocking_and_leaves_no_artifact(tmp_path: Path):
    root = plugin(tmp_path)

    with plugin_operation_lock(root):
        with pytest.raises(PluginBusyError, match="already running"):
            with plugin_operation_lock(root):
                pass

    assert not list(root.glob("*lock*"))


def test_same_root_alias_contends_across_processes(tmp_path: Path):
    root = plugin(tmp_path / "plugin")
    alias = tmp_path / "plugin-alias"
    _directory_alias(root, alias)
    holder = _start_lock_holder(root)
    try:
        with pytest.raises(PluginBusyError, match="already running"):
            with plugin_operation_lock(alias):
                pass
    finally:
        _stop_lock_holder(holder)


def test_owner_death_releases_cross_process_lock(tmp_path: Path):
    root = plugin(tmp_path / "plugin")
    holder = _start_lock_holder(root)
    _stop_lock_holder(holder)

    started = time.monotonic()
    with plugin_operation_lock(root):
        pass
    assert time.monotonic() - started < 5


def test_windows_mutex_identity_is_stable_and_contains_no_plugin_path(tmp_path: Path):
    identity = str(tmp_path.resolve())

    first = _windows_mutex_name(identity)
    second = _windows_mutex_name(identity)

    assert first == second
    assert first.startswith("Local\\SupernoteModuleGenerator-")
    assert identity not in first


def test_operation_lock_module_import_does_not_require_fcntl(tmp_path: Path):
    source = Path(__file__).parents[1] / "src"
    script = (
        "import builtins\n"
        "original = builtins.__import__\n"
        "def guarded(name, *args, **kwargs):\n"
        "    if name == 'fcntl':\n"
        "        raise ModuleNotFoundError('simulated Windows host')\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = guarded\n"
        "import supernote_module_generator.operation_lock\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_overlapping_cli_command_fails_cleanly_before_mutation(tmp_path: Path):
    root = plugin(tmp_path)
    before = (root / "package.json").read_bytes()

    with plugin_operation_lock(root):
        code, _, stderr = invoke(
            root,
            ["add", "blocked", "--starter", "cpp", "--yes"],
        )

    assert code == 2
    assert "Another sn-module-gen command is already running" in stderr
    assert (root / "package.json").read_bytes() == before
    assert not (root / "local_modules/blocked").exists()
    assert not (root / JOURNAL_NAME).exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_busy_json_preserves_semantic_error_kind_and_phase(tmp_path: Path):
    root = plugin(tmp_path)

    with plugin_operation_lock(root):
        code, stdout, stderr = invoke(root, ["--json", "validate"])

    payload = json.loads(stdout)
    assert code == 2
    assert stderr == ""
    assert payload["error"]["kind"] == "plugin_busy"
    assert payload["error"]["phase"] == "preflight"
    assert payload["exit_code"] == 2
    assert payload["next_action"] == (
        "Correct the reported problem and rerun Validate."
    )


def test_two_overlapping_add_commands_have_one_clean_winner(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = FeatureCliOperationService._validate_add

    def blocking_preflight(self, decisions):
        original(self, decisions)
        if decisions.package_name == "first":
            entered.set()
            assert release.wait(timeout=30)

    monkeypatch.setattr(FeatureCliOperationService, "_validate_add", blocking_preflight)
    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(
            invoke(
                root,
                ["add", "first", "--starter", "cpp", "--yes"],
            )
        )
    )
    first.start()
    assert entered.wait(timeout=30)
    try:
        second = invoke(
            root,
            ["add", "second", "--starter", "cpp", "--yes"],
        )
    finally:
        release.set()
        first.join(timeout=30)

    assert not first.is_alive()
    assert first_result and first_result[0][0] == 0
    assert second[0] == 2
    assert "Another sn-module-gen command is already running" in second[2]
    assert (root / "local_modules/first").is_dir()
    assert not (root / "local_modules/second").exists()
    assert not (root / JOURNAL_NAME).exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
