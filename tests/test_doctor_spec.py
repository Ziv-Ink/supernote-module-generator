from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    lexists,
    protected_directory_metadata,
    remove_entry_no_follow,
    restore_protected_source_backup,
    source_tree_inventory,
)
from supernote_module_generator.rendering import Renderer, TerminalCapabilities
from supernote_module_generator.models import CommandResult, ValidationResult


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
    (tmp_path / "android/build.gradle").write_text(
        "buildscript {\n"
        "    ext {\n"
        "        buildToolsVersion = \"35.0.0\"\n"
        "        compileSdkVersion = 35\n"
        "        ndkVersion = \"27.1.0\"\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "android" / HOST_GRADLE).write_text(
        "@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    gradle_bin = tmp_path / ".doctor-tools/jdk-17/bin"
    gradle_bin.mkdir(parents=True)
    for java_name in ("java", "java.exe"):
        gradle_java = gradle_bin / java_name
        gradle_java.write_text("", encoding="utf-8")
        gradle_java.chmod(0o755)
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
    build_tools = sdk / "build-tools/35.0.0"
    build_tools.mkdir(parents=True)
    for name in (
        "aapt2",
        "zipalign",
        "apksigner",
        "aapt2.exe",
        "zipalign.exe",
        "apksigner.bat",
    ):
        tool = build_tools / name
        tool.write_text("", encoding="utf-8")
        tool.chmod(0o755)
    adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    adb.parent.mkdir(parents=True)
    adb.write_text("", encoding="utf-8")
    adb.chmod(0o755)
    ndk = sdk / "ndk/27.1.0"
    compiler = ndk / "toolchains/llvm/prebuilt/test/bin"
    compiler.mkdir(parents=True)
    (compiler / HOST_CLANG).write_text("", encoding="utf-8")
    (compiler / HOST_CLANGXX).write_text("", encoding="utf-8")
    (ndk / "source.properties").write_text(
        "Pkg.Revision = 27.1.0\n", encoding="utf-8"
    )
    cmake = sdk / "cmake/3.22.1/bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    cmake.parent.mkdir(parents=True)
    cmake.write_text("", encoding="utf-8")
    cmake.chmod(0o755)
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk))
    monkeypatch.setenv("ANDROID_NDK_HOME", str(ndk))
    monkeypatch.setenv("ANDROID_NDK_ROOT", str(ndk))
    return sdk


def install_fake_ndk(sdk: Path, version: str) -> Path:
    ndk = sdk / "ndk" / version
    compiler = ndk / "toolchains/llvm/prebuilt/test/bin"
    compiler.mkdir(parents=True)
    (compiler / HOST_CLANG).write_text("", encoding="utf-8")
    (compiler / HOST_CLANGXX).write_text("", encoding="utf-8")
    (ndk / "source.properties").write_text(
        f"Pkg.Revision = {version}\n", encoding="utf-8"
    )
    return ndk


def select_ndk(root: Path, version: str) -> None:
    path = root / "android/build.gradle"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'ndkVersion = "27.1.0"', f'ndkVersion = "{version}"'
        ),
        encoding="utf-8",
    )


def successful_run(command, **kwargs):
    executable = Path(command[0]).name
    if executable == "sh" and len(command) > 1:
        executable = Path(command[1]).name
    cwd = Path(kwargs.get("cwd", "."))
    daemon_home = cwd / ".doctor-tools/jdk-17"
    output = {
        "node": "v20.0.0\n",
        "npm": "10.0.0\n",
        "yarn": "1.22.0\n",
        "java": "openjdk 17.0.12\n",
        "java.exe": "openjdk 17.0.12\n",
        "gradlew": (
            "Gradle 8.13\nLauncher JVM: 17.0.12\n"
            f"Daemon JVM: {daemon_home} (from org.gradle.java.home)\n"
        ),
        "gradlew.bat": (
            "Gradle 8.13\nLauncher JVM: 17.0.12\n"
            f"Daemon JVM: {daemon_home} (from org.gradle.java.home)\n"
        ),
        "cmake": "cmake version 3.22.1\n",
        "cmake.exe": "cmake version 3.22.1\n",
        "clang": "clang version 18.0.0\n",
        "clang++": "clang version 18.0.0\n",
        "clang.exe": "clang version 18.0.0\n",
        "clang++.exe": "clang version 18.0.0\n",
        "adb": "Android Debug Bridge version 1.0.41\n",
        "adb.exe": "Android Debug Bridge version 1.0.41\n",
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
    adb = next(check for check in result.doctor.checks if check.id == "adb")
    assert adb.status == "passed"
    assert adb.metadata["executable_probed"] is True
    assert adb.metadata["device_tested"] is False
    assert result.doctor.advisory_count >= 1


def test_doctor_passes_for_plugin_with_typed_cpp_jvm_and_mixed_v4_features(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    (root / "android/app").mkdir()
    (root / "android/app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    features = FeatureOperationService(root)
    for name, starters in (
        ("typed-cpp", (StarterFamily.NATIVE,)),
        ("typed-jvm", (StarterFamily.JVM,)),
        ("typed-mixed", (StarterFamily.NATIVE, StarterFamily.JVM)),
    ):
        created = features.add(
            FeatureConfig(
                output=root / "local_modules" / name,
                npm_name=name,
                package_version="0.1.0",
                android_namespace=f"com.example.{name.replace('-', '_')}",
                public_name="".join(part.title() for part in name.split("-")),
                starters=starters,
            )
        )
        if name == "typed-cpp":
            (created / "android/src/main/cpp/Typed.hpp").write_text(
                "// @SupernotePluginValue\n"
                "struct Point {\n"
                "  // @SupernotePluginExport\n"
                "  double x;\n"
                "};\n",
                encoding="utf-8",
            )

    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    assert result.doctor.required_passed
    assert len(features.records()) == 3


def test_windows_doctor_uses_batch_wrapper_and_exe_ndk_compilers(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    for wrapper in (root / "android/gradlew", root / "android/gradlew.bat"):
        wrapper.unlink(missing_ok=True)
    (root / "android/gradlew.bat").write_text("@echo off\r\n", encoding="utf-8")
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    compiler = sdk / "ndk/27.1.0/toolchains/llvm/prebuilt/test/bin"
    (compiler / "clang").unlink(missing_ok=True)
    (compiler / "clang++").unlink(missing_ok=True)
    (compiler / "clang.exe").write_bytes(b"")
    (compiler / "clang++.exe").write_bytes(b"")
    windows_cmake = sdk / "cmake/3.22.1/bin/cmake.exe"
    windows_cmake.write_bytes(b"")
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"C:/tools/{name}",
    )
    commands = []

    def run(command, **kwargs):
        commands.append(list(command))
        if Path(command[0]).name == "gradlew.bat":
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\nLauncher JVM: 17.0.12\n"
                f"Daemon JVM: {root / '.doctor-tools/jdk-17'} "
                "(from org.gradle.java.home)\n",
                "",
            )
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
    adb = next(check for check in result.doctor.checks if check.id == "adb")
    assert adb.status == "passed"
    assert adb.metadata["device_tested"] is False


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


def test_doctor_fails_for_missing_project_selected_ndk_even_when_others_are_installed(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    install_fake_ndk(sdk, "30.0.0")
    select_ndk(root, "99.0.0")
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    selected = next(check for check in result.doctor.checks if check.id == "android_ndk")
    installed = next(
        check for check in result.doctor.checks if check.id == "android_ndk_installed"
    )
    assert selected.status == "failed"
    assert selected.detected_version is None
    assert "99.0.0" in selected.message
    assert selected.metadata["selected"] is True
    assert selected.metadata["found"] is False
    assert installed.metadata["installed_versions"] == ["30.0.0", "27.1.0"]


def test_doctor_probes_project_selected_ndk_not_newest_or_environment_ndk(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    newest = install_fake_ndk(sdk, "30.0.0")
    monkeypatch.setenv("ANDROID_NDK_HOME", str(newest))
    monkeypatch.setenv("ANDROID_NDK_ROOT", str(newest))
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    selected = next(check for check in result.doctor.checks if check.id == "android_ndk")
    assert selected.detected_version == "27.1.0"
    assert selected.path == str((sdk / "ndk/27.1.0").resolve())
    assert selected.metadata["configured_ndk_revision"] == "30.0.0"
    compiler_commands = [command for command in commands if "clang" in Path(command[0]).name]
    assert compiler_commands
    assert all("27.1.0" in command[0] for command in compiler_commands)


def test_doctor_probes_project_selected_sdk_cmake_not_path_or_newest_installation(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    newest = sdk / "cmake/4.1.2/bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    newest.parent.mkdir(parents=True)
    newest.write_text("", encoding="utf-8")
    newest.chmod(0o755)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/path-tools/{name}",
    )
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    cmake = next(check for check in result.doctor.checks if check.id == "cmake")
    expected = sdk.resolve() / "cmake/3.22.1/bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    assert cmake.path == str(expected)
    assert cmake.metadata["selected_version"] == "3.22.1"
    assert [str(expected), "--version"] in commands
    assert not any("cmake/4.1.2" in command[0] for command in commands)


def test_doctor_reports_exact_selected_sdk_components_and_adb_capabilities(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    monkeypatch.setenv("ADB_BIN", str(adb))
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    checks = {check.id: check for check in result.doctor.checks}
    assert checks["android_platform"].detected_version == "35"
    assert checks["android_platform"].path == str(
        sdk.resolve() / "platforms/android-35/android.jar"
    )
    assert checks["android_build_tools"].detected_version == "35.0.0"
    assert checks["adb"].path == str(adb)
    assert checks["adb"].metadata["configured"] is True
    assert checks["adb"].metadata["executable_probed"] is True
    for check in checks.values():
        assert {
            "configured",
            "found",
            "selected",
            "executable_probed",
            "compiler_probed",
            "project_built",
            "device_tested",
        } <= set(check.metadata) or check.id == "project"
    assert all(check.metadata.get("device_tested") is not True for check in checks.values())
    assert checks["selinux_policy"].metadata["project_built"] is False


def test_doctor_rejects_missing_selected_platform_and_build_tools(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    (sdk / "platforms/android-35/android.jar").unlink()
    for path in (sdk / "build-tools/35.0.0").iterdir():
        path.unlink()
    (sdk / "build-tools/35.0.0").rmdir()
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    checks = {check.id: check for check in result.doctor.checks}
    assert checks["android_platform"].status == "failed"
    assert checks["android_build_tools"].status == "failed"
    assert checks["android_sdk"].status == "passed"


def test_doctor_rejects_selected_build_tools_directory_without_required_tools(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    missing = sdk / "build-tools/35.0.0" / (
        "aapt2.exe" if os.name == "nt" else "aapt2"
    )
    missing.unlink()
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    build_tools = next(
        check for check in result.doctor.checks if check.id == "android_build_tools"
    )
    assert build_tools.status == "failed"
    assert str(missing) in build_tools.metadata["required_paths"]


def test_doctor_rejects_configured_java_home_without_its_platform_executable(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    empty_java = tmp_path / "empty-java"
    empty_java.mkdir()
    monkeypatch.setenv("JAVA_HOME", str(empty_java))
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    java = next(check for check in result.doctor.checks if check.id == "java")
    assert java.status == "failed"
    assert java.path == str(
        empty_java / "bin" / ("java.exe" if os.name == "nt" else "java")
    )
    assert java.metadata["configured"] is True
    assert java.metadata["found"] is False


def test_doctor_rejects_conflicting_android_sdk_environment_selections(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    sdk = install_fake_sdk(tmp_path, monkeypatch)
    other_sdk = tmp_path / "other-sdk"
    (other_sdk / "platforms").mkdir(parents=True)
    (other_sdk / "build-tools").mkdir()
    (other_sdk / "platform-tools").mkdir()
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(other_sdk))
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    checks = {check.id: check for check in result.doctor.checks}
    for identifier in ("android_sdk", "android_platform", "android_build_tools", "android_ndk"):
        assert checks[identifier].status == "failed"
        assert "environment selections conflict" in checks[identifier].message
        assert checks[identifier].metadata["selected"] is False


def test_doctor_toolchain_discovery_preserves_gradle_configuration_atime(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    build_gradle = root / "android/build.gradle"
    old_atime = 946684800_000_000_000
    metadata = build_gradle.stat()
    os.utime(build_gradle, ns=(old_atime, metadata.st_mtime_ns))
    before = build_gradle.stat()
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    after = build_gradle.stat()
    assert result.exit_code == 0
    assert after.st_atime_ns == before.st_atime_ns
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_mode == before.st_mode


def test_doctor_preserves_unattributed_source_written_during_a_tool_probe(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    before = source.read_bytes()
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    mutated = False

    def run(command, **kwargs):
        nonlocal mutated
        if not mutated and any(
            Path(part).name in {"gradlew", "gradlew.bat"} for part in command
        ):
            source.write_text("val sentinel = 2\n", encoding="utf-8")
            mutated = True
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 3
    assert result.status == "partial"
    assert result.rollback.status == "partial"
    assert source.read_bytes() == b"val sentinel = 2\n"
    assert result.doctor is not None
    integrity = next(
        check
        for check in result.doctor.checks
        if check.id == "doctor_source_integrity"
    )
    assert integrity.status == "failed"
    assert integrity.metadata["restored"] is False
    assert result.to_dict()["actual_changes"] == [
        {
            "path": "android/app/src/main/kotlin/App.kt",
            "action": "update",
            "ownership": "user source",
        }
    ]
    recovery = Path(result.metadata["recovery_path"])
    assert restore_protected_source_backup(recovery, root) == ()
    assert source.read_bytes() == before
    remove_entry_no_follow(recovery)


def test_doctor_interrupt_preserves_unattributed_source_and_backup(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    before_inventory = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            source.write_text("val sentinel = 2\n", encoding="utf-8")
            raise KeyboardInterrupt
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 3
    assert result.status == "partial"
    assert result.rollback.status == "partial"
    assert source.read_bytes() == b"val sentinel = 2\n"
    payload = result.to_dict()
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "partial"
    assert payload["actual_changes"] == [
        {
            "path": "android/app/src/main/kotlin/App.kt",
            "action": "update",
            "ownership": "user source",
        }
    ]
    recovery = Path(result.metadata["recovery_path"])
    assert restore_protected_source_backup(recovery, root) == ()
    assert source_tree_inventory(root) == before_inventory
    assert protected_directory_metadata(root) == before_directories
    remove_entry_no_follow(recovery)


def test_doctor_finish_inventory_interrupt_retries_without_losing_live_source(
    tmp_path: Path,
    monkeypatch,
):
    import supernote_module_generator.filesystem as filesystem

    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    before = source.read_bytes()
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    armed = False
    interrupted = False
    original_inventory = filesystem.source_tree_inventory

    def interrupt_once(path):
        nonlocal interrupted
        if armed and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_inventory(path)

    monkeypatch.setattr(filesystem, "source_tree_inventory", interrupt_once)

    def run(command, **kwargs):
        nonlocal armed
        if not armed and any(
            Path(part).name in {"gradlew", "gradlew.bat"} for part in command
        ):
            source.write_text("val sentinel = 2\n", encoding="utf-8")
            armed = True
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert interrupted
    assert result.status == "partial"
    assert result.exit_code == 3
    assert result.rollback.status == "partial"
    assert source.read_bytes() == b"val sentinel = 2\n"
    assert result.to_dict()["cancellation"] == {
        "requested": True,
        "status": "partial",
        "reason": (
            "Doctor was interrupted and exact protected-source restoration "
            "could not be verified."
        ),
    }
    recovery = Path(result.metadata["recovery_path"])
    assert restore_protected_source_backup(recovery, root) == ()
    assert source.read_bytes() == before
    remove_entry_no_follow(recovery)


def test_doctor_interrupted_finalization_failed_retry_retains_actionable_backup(
    tmp_path: Path,
    monkeypatch,
):
    import supernote_module_generator.filesystem as filesystem

    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    before_inventory = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    armed = False
    inventory_attempts = 0
    original_inventory = filesystem.source_tree_inventory

    def fail_inventory(path):
        nonlocal inventory_attempts
        if not armed:
            return original_inventory(path)
        inventory_attempts += 1
        if inventory_attempts == 1:
            raise KeyboardInterrupt
        raise OSError("forced retry inventory failure")

    monkeypatch.setattr(filesystem, "source_tree_inventory", fail_inventory)

    def run(command, **kwargs):
        nonlocal armed
        if not armed and any(
            Path(part).name in {"gradlew", "gradlew.bat"} for part in command
        ):
            source.write_text("val sentinel = 2\n", encoding="utf-8")
            armed = True
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert inventory_attempts >= 2
    assert result.status == "partial"
    assert result.exit_code == 3
    assert result.rollback.status == "partial"
    payload = result.to_dict()
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "partial"
    assert "unverified" in payload["cancellation"]["reason"]
    assert payload["actual_changes"] == []
    assert payload["metadata"]["residue_verified"] is False
    recovery_path = Path(result.metadata["recovery_path"])
    assert lexists(recovery_path / "recovery-manifest.json")
    assert restore_protected_source_backup(recovery_path, root) == ()
    assert source_tree_inventory(root) == before_inventory
    assert protected_directory_metadata(root) == before_directories
    remove_entry_no_follow(recovery_path)
    assert not lexists(recovery_path)


@pytest.mark.parametrize("source_state", ("live", "restored"))
def test_doctor_distinguishes_uninventoried_residue_from_verified_empty(
    tmp_path: Path,
    monkeypatch,
    source_state: str,
):
    import supernote_module_generator.filesystem as filesystem

    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    before_inventory = source_tree_inventory(root)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    armed = False
    finish_calls = 0
    recovery_paths: list[Path] = []
    def interrupt_then_fail(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            recovery_paths.append(self.recovery_path)
            if source_state == "restored":
                assert restore_protected_source_backup(self.recovery_path, root) == ()
            raise KeyboardInterrupt
        if finish_calls == 2:
            raise RuntimeError("guard retry sentinel")
        raise AssertionError("unexpected guard finish call")

    def inventory_unavailable(self):
        raise OSError("inventory unavailable")

    monkeypatch.setattr(
        filesystem.ProtectedSourceGuard, "finish", interrupt_then_fail
    )
    monkeypatch.setattr(
        filesystem.ProtectedSourceGuard,
        "remaining_changes",
        inventory_unavailable,
    )

    def run(command, **kwargs):
        nonlocal armed
        if not armed and any(
            Path(part).name in {"gradlew", "gradlew.bat"} for part in command
        ):
            source.write_text("val sentinel = 2\n", encoding="utf-8")
            armed = True
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")
    payload = result.to_dict()

    assert result.status == "partial"
    assert result.exit_code == 3
    assert result.rollback.status == "partial"
    assert payload["error"]["kind"] == "doctor_source_restore_unverified"
    assert "matches the pre-command baseline" not in payload["error"]["message"]
    assert payload["changes"] == []
    assert payload["actual_changes"] == []
    assert payload["metadata"]["residue_verified"] is False
    assert payload["metadata"]["restore_diagnostics"] == [
        "finalization_failed:guard retry sentinel",
        "inventory_failed:inventory unavailable",
    ]
    assert not any(
        check["id"] == "doctor_source_integrity"
        for check in payload["doctor"]["checks"]
    )
    assert (source_tree_inventory(root) == before_inventory) is (
        source_state == "restored"
    )
    recovery_path = Path(payload["metadata"]["recovery_path"])
    assert recovery_paths and recovery_path == recovery_paths[0]
    assert lexists(recovery_path)
    assert restore_protected_source_backup(recovery_path, root) == ()
    assert source_tree_inventory(root) == before_inventory
    remove_entry_no_follow(recovery_path)


def test_doctor_verified_empty_cleanup_failure_has_no_project_residue(
    tmp_path: Path,
    monkeypatch,
):
    import supernote_module_generator.filesystem as filesystem

    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    before_inventory = source_tree_inventory(root)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    armed = False
    cleanup_calls = 0
    recovery_paths: list[Path] = []
    original_remove = filesystem.ProtectedSourceGuard._remove_temporary

    def interrupt_then_fail_cleanup(self):
        nonlocal cleanup_calls
        if not armed:
            return original_remove(self)
        cleanup_calls += 1
        if cleanup_calls == 1:
            recovery_paths.append(self.recovery_path)
            raise KeyboardInterrupt
        raise RuntimeError("guard retry sentinel")

    monkeypatch.setattr(
        filesystem.ProtectedSourceGuard,
        "_remove_temporary",
        interrupt_then_fail_cleanup,
    )

    def run(command, **kwargs):
        nonlocal armed
        if not armed and any(
            Path(part).name in {"gradlew", "gradlew.bat"} for part in command
        ):
            armed = True
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")
    payload = result.to_dict()

    assert result.status == "partial"
    assert payload["error"]["kind"] == "doctor_source_cleanup_failed"
    assert payload["changes"] == []
    assert payload["actual_changes"] == []
    assert payload["metadata"]["residue_verified"] is True
    assert payload["metadata"]["restore_diagnostics"] == [
        "finalization_failed:guard retry sentinel"
    ]
    assert source_tree_inventory(root) == before_inventory
    assert not any(
        check["id"] == "doctor_source_integrity"
        for check in payload["doctor"]["checks"]
    )
    recovery_path = Path(payload["metadata"]["recovery_path"])
    assert recovery_paths and recovery_path == recovery_paths[0]
    assert lexists(recovery_path)
    assert restore_protected_source_backup(recovery_path, root) == ()
    remove_entry_no_follow(recovery_path)


def test_successful_doctor_is_observationally_read_only_for_protected_state(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    source = root / "android/app/src/main/kotlin/App.kt"
    source.parent.mkdir(parents=True)
    source.write_text("val sentinel = 1\n", encoding="utf-8")
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    before_inventory = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 0
    assert source_tree_inventory(root) == before_inventory
    assert protected_directory_metadata(root) == before_directories


def test_doctor_rejects_dynamic_or_conflicting_project_toolchain_selection(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    (root / "android/build.gradle").write_text(
        "buildscript { ext {\n"
        "  compileSdkVersion = providers.gradleProperty('compileSdk').get()\n"
        "  buildToolsVersion = '35.0.0'\n"
        "  ndkVersion = '27.1.0'\n"
        "  ndkVersion = '30.0.0'\n"
        "} }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    checks = {check.id: check for check in result.doctor.checks}
    assert "No literal compileSdkVersion" in checks["android_platform"].message
    assert "Conflicting ndkVersion" in checks["android_ndk"].message


def test_doctor_ignores_commented_gradle_values_and_supports_literal_kotlin_extra(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    (root / "android/build.gradle").unlink()
    (root / "android/build.gradle.kts").write_text(
        "/*\n"
        "extra[\"ndkVersion\"] = \"99.0.0\"\n"
        "*/\n"
        "extra[\"compileSdkVersion\"] = 35\n"
        "extra[\"buildToolsVersion\"] = \"35.0.0\"\n"
        "extra[\"ndkVersion\"] = \"27.1.0\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    result = DoctorService(root, renderer(), run=successful_run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    ndk = next(check for check in result.doctor.checks if check.id == "android_ndk")
    assert ndk.detected_version == "27.1.0"
    assert ndk.metadata["selection_source"] == "android/build.gradle.kts:6"


def test_doctor_build_reports_only_a_real_authoritative_build_as_project_built(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    observed: list[bool] = []

    def check(self, *, build=False, jvm_manifest_root=None):
        observed.append(build)
        return CommandResult(
            "check",
            validation=ValidationResult(
                structural="passed",
                integration="passed",
                dependency_link="passed",
                build="passed",
            ),
            diagnostics=["/tmp/doctor-build.log"],
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_cli_operations.V4CliOperationService.check",
        check,
    )

    result = DoctorService(root, renderer(), run=successful_run).execute(
        "plugin", build=True
    )

    assert result.exit_code == 0
    assert observed == [True]
    assert result.doctor is not None
    build_check = next(
        check for check in result.doctor.checks if check.id == "android_project_build"
    )
    assert build_check.metadata["project_built"] is True
    assert build_check.metadata["device_tested"] is False
    assert build_check.metadata["diagnostics"] == ["/tmp/doctor-build.log"]
    assert result.diagnostics == ["/tmp/doctor-build.log"]
    assert result.validation is not None
    assert result.validation.build == "passed"


def test_doctor_build_failure_preserves_validation_and_diagnostics(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )

    def check(self, *, build=False, jvm_manifest_root=None):
        return CommandResult(
            "check",
            status="failure",
            exit_code=1,
            validation=ValidationResult(
                structural="passed",
                integration="passed",
                dependency_link="passed",
                build="failed",
                issues=[
                    {
                        "code": "SNV4_BUILD_FAILED",
                        "severity": "error",
                        "scope": "toolchain",
                        "message": "compiler root cause",
                    }
                ],
            ),
            diagnostics=["/tmp/doctor-build-failure.log"],
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_cli_operations.V4CliOperationService.check",
        check,
    )

    result = DoctorService(root, renderer(), run=successful_run).execute(
        "plugin", build=True
    )

    assert result.exit_code == 1
    assert result.diagnostics == ["/tmp/doctor-build-failure.log"]
    assert result.next_action is not None
    assert "diagnostics" in result.next_action
    assert result.doctor is not None
    build_check = next(
        check for check in result.doctor.checks if check.id == "android_project_build"
    )
    assert build_check.metadata["project_built"] is False
    assert build_check.metadata["validation"]["build"] == "failed"
    assert build_check.metadata["issues"][0]["code"] == "SNV4_BUILD_FAILED"


def test_doctor_fails_when_gradle_uses_java_older_than_path_java(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "supernote_module_generator.doctor.shutil.which",
        lambda name: f"/tools/{name}",
    )
    daemon_home = root / ".doctor-tools/jdk-11"
    daemon_java = daemon_home / "bin" / (
        "java.exe" if os.name == "nt" else "java"
    )
    daemon_java.parent.mkdir(parents=True)
    daemon_java.write_text("", encoding="utf-8")
    daemon_java.chmod(0o755)

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\n"
                f"Daemon JVM: {daemon_home} (from org.gradle.java.home)\n",
                "",
            )
        if Path(command[0]) == daemon_java:
            return subprocess.CompletedProcess(command, 0, "openjdk 11.0.31\n", "")
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
    assert gradle_java.detected_version == "openjdk 11.0.31"
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
    daemon_home = root / ".doctor-tools/jdk-25"
    daemon_java = daemon_home / "bin" / (
        "java.exe" if os.name == "nt" else "java"
    )
    daemon_java.parent.mkdir(parents=True)
    daemon_java.write_text("", encoding="utf-8")
    daemon_java.chmod(0o755)

    def run(command, **kwargs):
        if any(Path(part).name in {"gradlew", "gradlew.bat"} for part in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "Gradle 8.13\n"
                f"Daemon JVM: {daemon_home} (from org.gradle.java.home)\n",
                "",
            )
        if Path(command[0]) == daemon_java:
            return subprocess.CompletedProcess(command, 0, "openjdk 25.0.3\n", "")
        return successful_run(command, **kwargs)

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 1
    assert result.doctor is not None
    gradle_java = next(
        check for check in result.doctor.checks if check.id == "gradle_jvm"
    )
    assert gradle_java.status == "failed"
    assert gradle_java.detected_version == "openjdk 25.0.3"
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
    daemon_java.chmod(0o755)
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
    assert gradle_java.metadata["command"] == [str(daemon_java), "--version"]


def test_doctor_passed_gradle_jvm_identifies_exact_executable_and_command(
    tmp_path: Path,
    monkeypatch,
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
    gradle_java = next(
        check for check in result.doctor.checks if check.id == "gradle_jvm"
    )
    executable = root / ".doctor-tools/jdk-17/bin" / (
        "java.exe" if os.name == "nt" else "java"
    )
    assert gradle_java.status == "passed"
    assert gradle_java.path == str(executable)
    assert gradle_java.metadata["command"] == [str(executable), "--version"]
    assert gradle_java.metadata["executable_probed"] is True


def test_doctor_legacy_gradle_jvm_version_is_uninspectable_without_java_home(
    tmp_path: Path,
    monkeypatch,
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
                "Gradle 8.13\nJVM: 17.0.12\n",
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
    assert gradle_java.path is None
    assert gradle_java.metadata["selected"] is False
    assert gradle_java.metadata.get("command") is None
    assert "exact daemon Java home" in gradle_java.message


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
