from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from supernote_module_generator.doctor import DoctorService, _properties_value
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    protected_directory_metadata,
    source_tree_inventory,
)
from supernote_module_generator.rendering import Renderer, TerminalCapabilities
from supernote_module_generator.models import CommandResult, ValidationResult


HOST_GRADLE = "gradlew.bat" if os.name == "nt" else "gradlew"
HOST_CLANG = "clang.exe" if os.name == "nt" else "clang"
HOST_CLANGXX = "clang++.exe" if os.name == "nt" else "clang++"


def java_properties_escape(value: str) -> str:
    """Encode a path as one Java-properties value without changing its meaning."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


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
    cmake = sdk / "cmake/3.24.4/bin" / (
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
        "cmake": "cmake version 3.24.4\n",
        "cmake.exe": "cmake version 3.24.4\n",
        "clang": "clang version 18.0.0\n",
        "clang++": "clang version 18.0.0\n",
        "clang.exe": "clang version 18.0.0\n",
        "clang++.exe": "clang version 18.0.0\n",
        "adb": "Android Debug Bridge version 1.0.41\n",
        "adb.exe": "Android Debug Bridge version 1.0.41\n",
    }.get(executable, "")
    return subprocess.CompletedProcess(command, 0, output, "")


def test_doctor_executes_required_generator_probes_without_target_policy_advice(
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
    assert not any(check.id == "selinux_policy" for check in result.doctor.checks)
    adb = next(check for check in result.doctor.checks if check.id == "adb")
    assert adb.status == "passed"
    assert adb.metadata["executable_probed"] is True
    assert adb.metadata["device_tested"] is False
    assert result.doctor.advisory_count == 0


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
    windows_cmake = sdk / "cmake/3.24.4/bin/cmake.exe"
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


def test_plugin_doctor_omits_jsi_policy_and_never_probes_deployment(
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
    assert not any(check.id == "selinux_policy" for check in result.doctor.checks)
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
        if Path(command[0]).stem == "cmake":
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
    expected = sdk.resolve() / "cmake/3.24.4/bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    assert cmake.path == str(expected)
    assert cmake.metadata["selected_version"] == "3.24.4"
    assert [str(expected), "--version"] in commands
    assert not any("cmake/4.1.2" in command[0] for command in commands)


def test_doctor_honors_generated_cmake_version_override_and_local_cmake_dir(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    cmake_root = tmp_path / "system-cmake"
    executable = cmake_root / "bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    (root / "android/gradle.properties").write_text(
        "supernoteModuleCmakeVersion=3.28.3\n",
        encoding="utf-8",
    )
    (root / "android/local.properties").write_text(
        f"cmake.dir={java_properties_escape(str(cmake_root))}\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        result = successful_run(command, **kwargs)
        if Path(command[0]) == executable:
            return subprocess.CompletedProcess(
                command, 0, "cmake version 3.28.3\n", ""
            )
        return result

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    cmake = next(check for check in result.doctor.checks if check.id == "cmake")
    assert cmake.path == str(executable)
    assert cmake.detected_version == "cmake version 3.28.3"
    assert cmake.metadata["selected_version"] == "3.28.3"
    assert cmake.metadata["minimum_version"] == "3.24"
    assert cmake.metadata["selection_source"] == "android/local.properties cmake.dir"
    assert [str(executable), "--version"] in commands


def _java_property_value(
    tmp_path: Path,
    properties: Path,
    key: str,
) -> str:
    java = shutil.which("java")
    javac = shutil.which("javac")
    if java is None or javac is None:
        pytest.skip("Java compiler/runtime is required for the Properties oracle")
    source = tmp_path / "PropertiesProbe.java"
    source.write_text(
        """
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

public final class PropertiesProbe {
  public static void main(String[] args) throws Exception {
    Properties properties = new Properties();
    try (InputStream input = Files.newInputStream(Path.of(args[0]))) {
      properties.load(input);
    }
    String value = properties.getProperty(args[1]);
    System.out.print(value == null ? "<null>" : value);
  }
}
""".lstrip(),
        encoding="utf-8",
    )
    subprocess.run(
        [javac, str(source)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [java, "-cp", str(tmp_path), "PropertiesProbe", str(properties), key],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_properties_parser_matches_java_for_escaped_windows_path_key_and_continuation(
    tmp_path: Path,
) -> None:
    cases = (
        ("escaped-path", "cmake.dir=" + r"C\:\\Tools\\CMake" + "\n"),
        ("escaped-key", "cmake\\u002edir : " + r"C\:\\Tools\\CMake" + "\n"),
        (
            "continuation",
            "cmake.dir="
            + r"C\:\\Tools"
            + "\\" * 3
            + "\n    CMake\n",
        ),
        (
            "last-value-wins",
            "cmake.dir=ignored\ncmake.dir=" + r"C\:\\Tools\\CMake" + "\n",
        ),
        (
            "comment-trailing-backslash",
            "# CMake parent C:\\\ncmake.dir=/opt/cmake\n",
        ),
        (
            "form-feed-property-whitespace",
            "cmake.dir\f=/opt/cmake\n",
        ),
    )
    for name, content in cases:
        root = tmp_path / name
        (root / "android").mkdir(parents=True)
        properties = root / "android/local.properties"
        properties.write_bytes(content.encode("iso-8859-1"))

        selected, error = _properties_value(
            root,
            "android/local.properties",
            "cmake.dir",
        )

        expected = _java_property_value(root, properties, "cmake.dir")
        assert error is None
        expected_value = (
            "/opt/cmake"
            if name in {"comment-trailing-backslash", "form-feed-property-whitespace"}
            else r"C:\Tools\CMake"
        )
        assert selected == expected == expected_value


def test_properties_parser_rejects_malformed_unicode_escape(tmp_path: Path) -> None:
    root = tmp_path
    (root / "android").mkdir()
    (root / "android/local.properties").write_bytes(b"cmake.dir=C\\u12XZ\n")

    selected, error = _properties_value(
        root,
        "android/local.properties",
        "cmake.dir",
    )

    assert selected is None
    assert error is not None
    assert "malformed Unicode escape" in error


def test_doctor_selects_continued_unicode_key_path_like_java_properties(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = plugin(tmp_path)
    install_fake_sdk(tmp_path, monkeypatch)
    cmake_root = tmp_path / "continued-system-cmake"
    executable = cmake_root / "bin" / (
        "cmake.exe" if os.name == "nt" else "cmake"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    value = java_properties_escape(str(cmake_root))
    split = len(value) - 8
    while value[split - 1] == "\\" or value[split] == "\\":
        split -= 1
    (root / "android/local.properties").write_text(
        "cmake\\u002edir : " + value[:split] + "\\" + "\n  " + value[split:] + "\n",
        encoding="iso-8859-1",
    )
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        result = successful_run(command, **kwargs)
        if Path(command[0]) == executable:
            return subprocess.CompletedProcess(
                command, 0, "cmake version 3.24.4\n", ""
            )
        return result

    result = DoctorService(root, renderer(), run=run).execute("plugin")

    assert result.exit_code == 0
    assert result.doctor is not None
    cmake = next(check for check in result.doctor.checks if check.id == "cmake")
    assert cmake.path == str(executable)
    assert cmake.metadata["configured_directory"] == str(cmake_root.resolve())
    assert [str(executable), "--version"] in commands


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


def test_doctor_toolchain_discovery_never_rewrites_gradle_configuration(
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
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_mode == before.st_mode


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
    after_directories = protected_directory_metadata(root)
    assert {
        relative: (mode, mtime_ns)
        for relative, (mode, _atime_ns, mtime_ns) in after_directories.items()
    } == {
        relative: (mode, mtime_ns)
        for relative, (mode, _atime_ns, mtime_ns) in before_directories.items()
    }


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


@pytest.mark.skip(reason="doctor app-build execution is retired")
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
        "supernote_module_generator.cli_operations.CliOperationService.check",
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
        "Restore `android/gradlew.bat`, then rerun `sn-module-gen doctor`."
        if os.name == "nt"
        else "Restore `android/gradlew`, make it executable, then rerun "
        "`sn-module-gen doctor`."
    )
    assert result.metadata["next_action"] == expected
