from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.rendering import Renderer, TerminalCapabilities


HOST_GRADLE = "gradlew.bat" if os.name == "nt" else "gradlew"
HOST_CLANG = "clang.exe" if os.name == "nt" else "clang"
HOST_CLANGXX = "clang++.exe" if os.name == "nt" else "clang++"


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
    (tmp_path / "android" / HOST_GRADLE).write_text(
        "@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
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
    (compiler / HOST_CLANG).write_text("", encoding="utf-8")
    (compiler / HOST_CLANGXX).write_text("", encoding="utf-8")
    (ndk / "source.properties").write_text(
        "Pkg.Revision = 27.1.0\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    return sdk


def successful_run(command, **kwargs):
    executable = Path(command[0]).name
    if executable == "sh" and len(command) > 1:
        executable = Path(command[1]).name
    output = {
        "node": "v20.0.0\n",
        "npm": "10.0.0\n",
        "yarn": "1.22.0\n",
        "java": "openjdk 17.0.12\n",
        "gradlew": "Gradle 8.13\nJVM: 17.0.12\n",
        "gradlew.bat": "Gradle 8.13\nJVM: 17.0.12\n",
        "cmake": "cmake version 3.22.1\n",
        "clang": "clang version 18.0.0\n",
        "clang++": "clang version 18.0.0\n",
        "clang.exe": "clang version 18.0.0\n",
        "clang++.exe": "clang version 18.0.0\n",
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

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    assert result.doctor.required_passed
    assert any(check.id == "selinux_policy" for check in result.doctor.checks)
    assert not any(check.id in {"adb", "adb_device"} for check in result.doctor.checks)
    assert result.doctor.advisory_count >= 1


def test_windows_doctor_uses_batch_wrapper_and_exe_ndk_compilers(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    for wrapper in (root / "android/gradlew", root / "android/gradlew.bat"):
        wrapper.unlink(missing_ok=True)
    (root / "android/gradlew.bat").write_text("@echo off\r\n", encoding="utf-8")
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    compiler = sdk / "ndk/27.1.0/toolchains/llvm/prebuilt/test/bin"
    (compiler / "clang").unlink(missing_ok=True)
    (compiler / "clang++").unlink(missing_ok=True)
    (compiler / "clang.exe").write_bytes(b"")
    (compiler / "clang++.exe").write_bytes(b"")
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"C:/tools/{name}",
    )
    commands = []

    def run(command, **kwargs):
        commands.append(list(command))
        return successful_run(command, **kwargs)

    result = DoctorService(
        root,
        renderer(),
        run=run,
        platform_name="nt",
    ).execute("plugin")

    assert result.exit_code == 0
    assert any(Path(command[0]).name == "gradlew.bat" for command in commands)
    assert not any(command[0] == "sh" for command in commands)
    assert any(Path(command[0]).name == "clang.exe" for command in commands)
    assert any(Path(command[0]).name == "clang++.exe" for command in commands)


def test_plugin_doctor_reports_jsi_policy_without_probing_deployment(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.doctor is not None
    assert any(check.id == "selinux_policy" for check in result.doctor.checks)
    assert not any(check.id in {"adb", "adb_device"} for check in result.doctor.checks)


def test_both_lockfiles_pass_when_one_manager_is_healthy(tmp_path: Path, monkeypatch):
    root = plugin(tmp_path, both_locks=True)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: None if name == "yarn" else f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

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

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    cmake = next(check for check in result.doctor.checks if check.id == "cmake")
    assert cmake.status == "failed"


def test_doctor_fails_when_gradle_uses_java_older_than_path_java(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\nJVM: 11.0.31\n",
                "",
            )
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    shell_java = next(check for check in result.doctor.checks if check.id == "java")
    gradle_java = next(
        check for check in result.doctor.checks if check.id == "gradle_jvm"
    )
    assert shell_java.status == "passed"
    assert shell_java.detected_version == "openjdk 17.0.12"
    assert gradle_java.status == "failed"
    assert gradle_java.detected_version == "11.0.31"
    assert "JAVA_HOME" in gradle_java.message


def test_doctor_rejects_gradle_jvm_newer_than_generated_gradle_support(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\nJVM: 25.0.3\n",
                "",
            )
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    gradle_java = next(
        check for check in result.doctor.checks if check.id == "gradle_jvm"
    )
    assert gradle_java.status == "failed"
    assert gradle_java.detected_version == "25.0.3"
    assert "Java 17 through 23" in gradle_java.message
    assert "Java 17 is recommended" in gradle_java.message


def test_doctor_probes_the_daemon_java_home_reported_by_new_gradle(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    daemon_home = tmp_path / "jdk-11"
    daemon_java = daemon_home / "bin" / (
        "java.exe" if os.name == "nt" else "java"
    )
    daemon_java.parent.mkdir(parents=True)
    daemon_java.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\n"
                "Launcher JVM: 25.0.3\n"
                f"Daemon JVM: {daemon_home} (from org.gradle.java.home)\n",
                "",
            )
        if Path(command[0]) == daemon_java:
            return subprocess.CompletedProcess(
                command, 0, "openjdk 11.0.31\n", ""
            )
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    gradle_java = next(
        check for check in result.doctor.checks if check.id == "gradle_jvm"
    )
    assert gradle_java.detected_version == "openjdk 11.0.31"
    assert gradle_java.path == str(daemon_java)
    assert gradle_java.status == "failed"


def test_plain_doctor_emits_one_final_report_without_progress_noise(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    plain_renderer = Renderer(
        "human",
        TerminalCapabilities(False, False, False, False, 80, 24),
        stdout=stdout,
        stderr=stderr,
        plain=True,
    )

    result = DoctorService(root, plain_renderer, run=successful_run).execute("plugin")
    plain_renderer.render(result)

    assert "Doctor - Plugin" in stdout.getvalue()
    assert "... Checking" not in stderr.getvalue()
    assert "Checked project" not in stderr.getvalue()


def test_missing_gradle_wrapper_has_specific_diagnosis_and_recovery(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    (root / "android" / HOST_GRADLE).unlink()
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    gradle = next(
        check for check in result.doctor.checks if check.id == "gradle_wrapper"
    )
    assert gradle.message == "The project Gradle wrapper is missing."
    expected = (
        "Restore `android/gradlew.bat`, then rerun `supernote-module doctor`."
        if os.name == "nt"
        else "Restore `android/gradlew`, make it executable, then rerun "
        "`supernote-module doctor`."
    )
    assert result.metadata["next_action"] == expected
