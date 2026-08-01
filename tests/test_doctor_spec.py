from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.rendering import Renderer, TerminalCapabilities


def plugin(tmp_path: Path, *, both_locks: bool = False) -> Path:
    (tmp_path / "android").mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (tmp_path / "android/gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    if both_locks:
        (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    return tmp_path


def renderer() -> Renderer:
    return Renderer(
        "json",
        TerminalCapabilities(False, False, False, False, 80, 24),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )


def install_fake_sdk(tmp_path: Path, monkeypatch) -> Path:
    sdk = tmp_path / "sdk"
    platform = sdk / "platforms/android-35"
    platform.mkdir(parents=True)
    (platform / "android.jar").write_bytes(b"")
    ndk = sdk / "ndk/27.1.0"
    compiler = ndk / "toolchains/llvm/prebuilt/test/bin"
    compiler.mkdir(parents=True)
    (compiler / "clang").write_text("", encoding="utf-8")
    (compiler / "clang++").write_text("", encoding="utf-8")
    (ndk / "source.properties").write_text(
        "Pkg.Revision = 27.1.0\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    return sdk


def successful_run(command, **kwargs):
    executable = Path(command[0]).name
    output = {
        "node": "v20.0.0\n",
        "npm": "10.0.0\n",
        "yarn": "1.22.0\n",
        "java": "openjdk 17.0.12\n",
        "gradlew": "Gradle 8.0\n",
        "cmake": "cmake version 3.22.1\n",
        "clang": "clang version 18.0.0\n",
        "clang++": "clang version 18.0.0\n",
    }.get(executable, "")
    return subprocess.CompletedProcess(command, 0, output, "")


def test_doctor_executes_required_probes_and_keeps_selinux_advisory(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("all")

    assert result.exit_code == 0
    assert result.doctor is not None
    assert result.doctor.required_passed
    assert any(check.id == "selinux_policy" for check in result.doctor.checks)
    assert not any(check.id in {"adb", "adb_device"} for check in result.doctor.checks)
    assert result.doctor.advisory_count >= 1


def test_native_doctor_does_not_probe_deployment_or_jsi_runtime(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("native")

    assert result.doctor is not None
    assert not any(
        check.id in {"adb", "adb_device", "selinux_policy"}
        for check in result.doctor.checks
    )


def test_both_lockfiles_pass_when_one_manager_is_healthy(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path, both_locks=True)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: None if name == "yarn" else f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("native")

    assert result.exit_code == 0
    assert result.doctor is not None
    health = next(
        check for check in result.doctor.checks if check.id == "package_manager_health"
    )
    assert health.status == "passed"
    ambiguity = next(
        check for check in result.doctor.checks if check.id == "package_manager"
    )
    assert ambiguity.requirement == "advisory"
    assert ambiguity.status == "warning"


def test_nonzero_tool_probe_fails_doctor(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run(command, **kwargs):
        if Path(command[0]).name == "cmake":
            return subprocess.CompletedProcess(command, 2, "", "broken\n")
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("jni")

    assert result.exit_code == 1
    assert result.doctor is not None
    cmake = next(check for check in result.doctor.checks if check.id == "cmake")
    assert cmake.status == "failed"
