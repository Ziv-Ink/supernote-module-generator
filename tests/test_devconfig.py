from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.devconfig import configured_developer_environment
from supernote_module_generator.models import CommandResult
from supernote_module_generator.platform_tools import gradle_wrapper_path
from supernote_module_generator.transaction import JOURNAL_NAME, Transaction


def _plugin(tmp_path: Path) -> Path:
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


def _tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    java_home = tmp_path / "tool paths/jdk 17"
    (java_home / "bin").mkdir(parents=True)
    java = java_home / "bin" / ("java.exe" if os.name == "nt" else "java")
    java.write_text("", encoding="utf-8")
    java.chmod(0o755)
    android_sdk = tmp_path / "tool paths/android sdk"
    (android_sdk / "platforms/android-35").mkdir(parents=True)
    (android_sdk / "platforms/android-35/android.jar").write_bytes(b"")
    build_tools = android_sdk / "build-tools/35.0.0"
    build_tools.mkdir(parents=True)
    for name in (
        "aapt2.exe" if os.name == "nt" else "aapt2",
        "zipalign.exe" if os.name == "nt" else "zipalign",
        "apksigner.bat" if os.name == "nt" else "apksigner",
    ):
        path = build_tools / name
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    adb = android_sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    adb.parent.mkdir(parents=True)
    adb.write_text("", encoding="utf-8")
    adb.chmod(0o755)
    return java_home, android_sdk, adb


def _write_config(
    root: Path, java_home: Path | None, android_sdk: Path | None, adb: Path | None
) -> None:
    (root / "devconfig.json").write_text(
        json.dumps(
            {
                "javaHome": str(java_home) if java_home else None,
                "androidSdk": str(android_sdk) if android_sdk else None,
                "adb": str(adb) if adb else None,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _invoke(root: Path, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        arguments,
        cwd=root,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_devconfig_temporarily_applies_all_tools_without_rewriting_local_properties(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    java_home, android_sdk, adb = _tools(tmp_path)
    _write_config(root, java_home, android_sdk, adb)
    local_properties = root / "android/local.properties"
    local_properties.write_text(
        "keep=this\nsdk.dir=/old/sdk\nlast=value\n", encoding="utf-8"
    )
    monkeypatch.setenv("JAVA_HOME", "/fallback/jdk")
    monkeypatch.setenv("ANDROID_HOME", "/fallback/sdk")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "/fallback/sdk-root")
    monkeypatch.setenv("ADB_BIN", "/fallback/adb")
    original_path = os.environ.get("PATH", "")

    with configured_developer_environment(root) as application:
        assert application.applied == ("javaHome", "androidSdk", "adb")
        assert application.issues == ()
        assert os.environ["JAVA_HOME"] == str(java_home)
        assert os.environ["ANDROID_HOME"] == str(android_sdk)
        assert os.environ["ANDROID_SDK_ROOT"] == str(android_sdk)
        assert os.environ["ADB_BIN"] == str(adb)
        assert os.environ["PATH"].split(os.pathsep)[0] == str(java_home / "bin")

    assert os.environ["JAVA_HOME"] == "/fallback/jdk"
    assert os.environ["ANDROID_HOME"] == "/fallback/sdk"
    assert os.environ["ANDROID_SDK_ROOT"] == "/fallback/sdk-root"
    assert os.environ["ADB_BIN"] == "/fallback/adb"
    assert os.environ.get("PATH", "") == original_path
    assert local_properties.read_text(encoding="utf-8") == (
        "keep=this\nsdk.dir=/old/sdk\nlast=value\n"
    )


def test_absent_or_null_devconfig_values_preserve_the_launch_environment(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    monkeypatch.setenv("JAVA_HOME", "/existing/jdk")
    monkeypatch.setenv("ANDROID_HOME", "/existing/sdk")
    _write_config(root, None, None, None)

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert application.issues == ()
        assert os.environ["JAVA_HOME"] == "/existing/jdk"
        assert os.environ["ANDROID_HOME"] == "/existing/sdk"

    (root / "devconfig.json").unlink()
    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert application.issues == ()


def test_devconfig_read_preserves_atime_and_refuses_a_symlink_without_following(
    tmp_path: Path,
    monkeypatch,
):
    root = _plugin(tmp_path)
    config = root / "devconfig.json"
    config.write_text("{}\n", encoding="utf-8")
    metadata = config.stat()
    old_atime = 946684800_000_000_000
    os.utime(config, ns=(old_atime, metadata.st_mtime_ns))

    with configured_developer_environment(root) as application:
        assert application.issues == ()
    assert config.stat().st_atime_ns == old_atime

    external = tmp_path.parent / f"{tmp_path.name}-external-devconfig.json"
    external.write_text('{"javaHome":"/must-not-follow"}\n', encoding="utf-8")
    before = external.read_bytes()
    config.unlink()
    try:
        config.symlink_to(external)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable on this host: {exc}")
    expected_link_target = os.readlink(config)
    monkeypatch.setenv("JAVA_HOME", "/existing/jdk")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert len(application.issues) == 1
        assert "Cannot read regular source entry" in application.issues[0]
        assert os.environ["JAVA_HOME"] == "/existing/jdk"
    assert os.readlink(config) == expected_link_target
    assert external.read_bytes() == before


def test_bad_devconfig_fields_warn_and_fall_back_without_leaking_environment(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    missing_java = root / "missing-jdk"
    (root / "devconfig.json").write_text(
        json.dumps(
            {
                "javaHome": str(missing_java),
                "androidSdk": ["not", "a", "path"],
                "adb": 42,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JAVA_HOME", "/existing/jdk")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert len(application.issues) == 3
        assert any("javaHome does not contain" in item for item in application.issues)
        assert any("'androidSdk'" in item for item in application.issues)
        assert any("'adb'" in item for item in application.issues)
        assert os.environ["JAVA_HOME"] == "/existing/jdk"

    assert os.environ["JAVA_HOME"] == "/existing/jdk"


def test_missing_configured_adb_warns_and_preserves_existing_selection(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    missing_adb = root / "tools/adb-that-is-not-installed"
    _write_config(root, None, None, missing_adb)
    monkeypatch.setenv("ADB_BIN", "/existing/adb")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert os.environ["ADB_BIN"] == "/existing/adb"
        assert len(application.issues) == 1
        assert "not an executable file" in application.issues[0]

    assert os.environ["ADB_BIN"] == "/existing/adb"


def test_empty_tool_directories_are_rejected_without_rewriting_local_properties(
    tmp_path: Path,
    monkeypatch,
):
    root = _plugin(tmp_path)
    empty_java = tmp_path / "empty-jdk"
    empty_java.mkdir()
    empty_sdk = tmp_path / "empty-sdk"
    empty_sdk.mkdir()
    missing_adb = tmp_path / "missing-adb"
    _write_config(root, empty_java, empty_sdk, missing_adb)
    local_properties = root / "android/local.properties"
    local_properties.write_text("sdk.dir=/preserved/sdk\n", encoding="utf-8")
    before = local_properties.read_bytes()
    monkeypatch.setenv("JAVA_HOME", "/existing/jdk")
    monkeypatch.setenv("ANDROID_HOME", "/existing/sdk")
    monkeypatch.setenv("ADB_BIN", "/existing/adb")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert len(application.issues) == 3
        assert os.environ["JAVA_HOME"] == "/existing/jdk"
        assert os.environ["ANDROID_HOME"] == "/existing/sdk"
        assert os.environ["ADB_BIN"] == "/existing/adb"

    assert local_properties.read_bytes() == before


def test_incomplete_sdk_version_directories_are_rejected_before_environment_change(
    tmp_path: Path,
    monkeypatch,
):
    root = _plugin(tmp_path)
    sdk = tmp_path / "incomplete-sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    (sdk / "platforms/android-35").mkdir(parents=True)
    (sdk / "build-tools/35.0.0").mkdir(parents=True)
    _write_config(root, None, sdk, None)
    monkeypatch.setenv("ANDROID_HOME", "/existing/sdk")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "/existing/sdk")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert len(application.issues) == 1
        assert "android.jar" in application.issues[0]
        assert os.environ["ANDROID_HOME"] == "/existing/sdk"
        assert os.environ["ANDROID_SDK_ROOT"] == "/existing/sdk"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not use POSIX execute bits")
@pytest.mark.parametrize("tool_name", ("aapt2", "zipalign", "apksigner"))
def test_devconfig_rejects_each_nonexecutable_android_build_tool(
    tmp_path: Path,
    monkeypatch,
    tool_name: str,
):
    root = _plugin(tmp_path)
    _java_home, android_sdk, _adb = _tools(tmp_path)
    (android_sdk / "build-tools/35.0.0" / tool_name).chmod(0o644)
    _write_config(root, None, android_sdk, None)
    monkeypatch.setenv("ANDROID_HOME", "/existing/sdk")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "/existing/sdk-root")

    with configured_developer_environment(root) as application:
        assert application.applied == ()
        assert len(application.issues) == 1
        assert "complete installed Android build-tools" in application.issues[0]
        assert os.environ["ANDROID_HOME"] == "/existing/sdk"
        assert os.environ["ANDROID_SDK_ROOT"] == "/existing/sdk-root"


def test_malformed_devconfig_is_reported_in_json_without_blocking_safe_commands(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    (root / "devconfig.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        "supernote_module_generator.cli.DoctorService.execute",
        lambda self, scope, build=False: CommandResult("doctor"),
    )

    code, stdout, stderr = _invoke(root, ["--json", "doctor"])

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["warnings"][0]["kind"] == "devconfig"
    assert "Could not read devconfig.json" in payload["warnings"][0]["message"]


def test_add_update_build_and_doctor_share_the_devconfig_environment(
    tmp_path: Path, monkeypatch, make_directory_symlink
):
    root = _plugin(tmp_path)
    java_home, android_sdk, adb = _tools(tmp_path)
    _write_config(root, java_home, android_sdk, adb)
    monkeypatch.setenv("JAVA_HOME", "/wrong/jdk")
    monkeypatch.setenv("ANDROID_HOME", "/wrong/sdk")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "/wrong/sdk-root")
    monkeypatch.setenv("ADB_BIN", "/wrong/adb")
    observed: list[tuple[str, str, str, str, str]] = []

    def record(command, *, cwd, timeout, stream=None, env=None):
        observed.append(
            (
                str(command[-1]),
                os.environ["JAVA_HOME"],
                os.environ["ANDROID_HOME"],
                os.environ["ANDROID_SDK_ROOT"],
                os.environ["ADB_BIN"],
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        "supernote_module_generator.feature_cli_operations.run_process", record
    )
    monkeypatch.setattr("supernote_module_generator.v4_validation.run_process", record)

    add_code, _, add_error = _invoke(
        root,
        ["add", "document", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert add_code == 0, add_error
    feature = root / "local_modules/document"
    update_code, _, update_error = _invoke(
        root, ["update", "document", "--skip-install", "--yes"]
    )
    assert update_code == 0, update_error
    link = root / "node_modules/document"
    link.parent.mkdir()
    make_directory_symlink(link, feature)
    validate_code, _, validate_error = _invoke(
        root, ["validate", "document", "--build"]
    )
    assert validate_code == 0, validate_error

    doctor_environment: dict[str, str] = {}

    def doctor(self, scope, *, build=False):
        doctor_environment.update(
            {
                name: os.environ[name]
                for name in (
                    "JAVA_HOME",
                    "ANDROID_HOME",
                    "ANDROID_SDK_ROOT",
                    "ADB_BIN",
                )
            }
        )
        return CommandResult("doctor")

    monkeypatch.setattr(
        "supernote_module_generator.cli.DoctorService.execute", doctor
    )
    assert _invoke(root, ["--json", "doctor"])[0] == 0

    expected = (str(java_home), str(android_sdk), str(android_sdk), str(adb))
    assert [entry[0] for entry in observed] == [":app:assembleDebug"]
    assert all(entry[1:] == expected for entry in observed)
    assert tuple(doctor_environment.values()) == expected
    assert os.environ["JAVA_HOME"] == "/wrong/jdk"
    assert os.environ["ANDROID_HOME"] == "/wrong/sdk"
    assert os.environ["ANDROID_SDK_ROOT"] == "/wrong/sdk-root"
    assert os.environ["ADB_BIN"] == "/wrong/adb"


def test_startup_recovery_reconciles_with_the_devconfig_environment(
    tmp_path: Path, monkeypatch
):
    root = _plugin(tmp_path)
    java_home, android_sdk, adb = _tools(tmp_path)
    _write_config(root, java_home, android_sdk, adb)
    monkeypatch.setenv("JAVA_HOME", "/wrong/jdk")
    monkeypatch.setenv("ANDROID_HOME", "/wrong/sdk")
    marker = root / "recovery-environment.txt"
    script = (
        "import os, pathlib, sys; "
        f"expected={(str(java_home), str(android_sdk), str(adb))!r}; "
        "actual=(os.environ.get('JAVA_HOME'), os.environ.get('ANDROID_HOME'), "
        "os.environ.get('ADB_BIN')); "
        f"pathlib.Path({str(marker)!r}).write_text('|'.join(actual)); "
        "sys.exit(0 if actual == expected else 9)"
    )
    package = root / "package.json"
    transaction = Transaction(root, "update", ["document"])
    transaction.snapshot([package])
    package.write_text('{"name":"changed"}\n', encoding="utf-8")
    transaction.mark_external([sys.executable, "-c", script])
    assert transaction.rollback(reconcile=lambda command: False).status == "partial"
    assert (root / JOURNAL_NAME).is_file()

    code, _, stderr = _invoke(root, ["validate", "--all"])

    assert code == 0, stderr
    assert marker.read_text(encoding="utf-8") == "|".join(
        (str(java_home), str(android_sdk), str(adb))
    )
    assert not (root / JOURNAL_NAME).exists()
    assert os.environ["JAVA_HOME"] == "/wrong/jdk"
    assert os.environ["ANDROID_HOME"] == "/wrong/sdk"
