"""Apply machine-local Supernote plugin tool paths to generator commands."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Iterator

from .errors import FilesystemError
from .filesystem import lexists, read_regular_bytes_no_follow


@dataclass(frozen=True)
class DevConfigApplication:
    """The settings applied from one plugin's optional devconfig.json."""

    path: Path
    applied: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


def _executable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _java_executable(java_home: Path) -> Path:
    name = "java.exe" if os.name == "nt" else "java"
    return java_home / "bin" / name


def _valid_android_sdk(android_sdk: Path) -> tuple[bool, str]:
    if not android_sdk.is_dir():
        return False, "is not a directory"
    required_directories = (
        android_sdk / "platform-tools",
        android_sdk / "platforms",
        android_sdk / "build-tools",
    )
    missing = [path.name for path in required_directories if not path.is_dir()]
    if missing:
        return False, "is missing " + ", ".join(missing)
    try:
        platforms = tuple(
            path
            for path in (android_sdk / "platforms").iterdir()
            if path.is_dir() and (path / "android.jar").is_file()
        )
    except OSError as exc:
        return False, f"could not inspect Android platforms: {exc}"
    if not platforms:
        return False, "has no installed Android platform with android.jar"
    executable_suffix = ".exe" if os.name == "nt" else ""
    signer = "apksigner.bat" if os.name == "nt" else "apksigner"
    try:
        build_tools = tuple(
            path
            for path in (android_sdk / "build-tools").iterdir()
            if path.is_dir()
            and _executable(path / f"aapt2{executable_suffix}")
            and _executable(path / f"zipalign{executable_suffix}")
            and _executable(path / signer)
        )
    except OSError as exc:
        return False, f"could not inspect Android build tools: {exc}"
    if not build_tools:
        return False, "has no complete installed Android build-tools version"
    return True, ""


def _configured_path(
    value: object,
    *,
    field: str,
    root: Path,
    issues: list[str],
) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        issues.append(
            f"devconfig.json field {field!r} must be a path string or null; "
            "the existing environment will be used."
        )
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _read_config(path: Path, root: Path) -> tuple[dict[str, Path | None], list[str]]:
    issues: list[str] = []
    if not lexists(path):
        return {}, issues
    try:
        content, _metadata = read_regular_bytes_no_follow(path)
        value = json.loads(content.decode("utf-8-sig"))
    except (FilesystemError, OSError, UnicodeError, ValueError) as exc:
        issues.append(
            f"Could not read {path.name}; the existing environment will be used: {exc}"
        )
        return {}, issues
    if not isinstance(value, dict):
        issues.append(
            "devconfig.json must contain a JSON object; the existing environment "
            "will be used."
        )
        return {}, issues
    return (
        {
            field: _configured_path(
                value.get(field), field=field, root=root, issues=issues
            )
            for field in ("javaHome", "androidSdk", "adb")
        },
        issues,
    )


@contextmanager
def configured_developer_environment(root: Path) -> Iterator[DevConfigApplication]:
    """Temporarily apply a plugin's devconfig.json to this process and children.

    The official plugin scripts use only existing Java and Android SDK directories
    and fall back to the launching environment for absent or unusable values. The
    generator follows the same selection rules here, restores its environment after
    the command, and gives every subprocess and Doctor lookup one configuration.
    The generator never rewrites ``android/local.properties`` while applying this environment.
    """

    plugin_root = root.expanduser().resolve()
    path = plugin_root / "devconfig.json"
    configured, issues = _read_config(path, plugin_root)
    updates: dict[str, str] = {}
    applied: list[str] = []

    java_home = configured.get("javaHome")
    if java_home is not None:
        java = _java_executable(java_home)
        if java_home.is_dir() and _executable(java):
            updates["JAVA_HOME"] = str(java_home)
            java_bin = str(java_home / "bin")
            current_path = os.environ.get("PATH", "")
            updates["PATH"] = (
                java_bin + os.pathsep + current_path if current_path else java_bin
            )
            applied.append("javaHome")
        else:
            issues.append(
                f"devconfig.json javaHome does not contain an executable "
                f"{java.relative_to(java_home)}: {java_home}; "
                "the existing JAVA_HOME and PATH will be used."
            )

    android_sdk = configured.get("androidSdk")
    if android_sdk is not None:
        sdk_valid, sdk_reason = _valid_android_sdk(android_sdk)
        if sdk_valid:
            updates["ANDROID_HOME"] = str(android_sdk)
            updates["ANDROID_SDK_ROOT"] = str(android_sdk)
            applied.append("androidSdk")
        else:
            issues.append(
                f"devconfig.json androidSdk {sdk_reason}: {android_sdk}; "
                "the existing Android SDK environment will be used."
            )

    adb = configured.get("adb")
    if adb is not None:
        if _executable(adb):
            updates["ADB_BIN"] = str(adb)
            applied.append("adb")
        else:
            issues.append(
                f"devconfig.json adb is not an executable file: {adb}; "
                "the existing ADB_BIN will be used."
            )

    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield DevConfigApplication(path, tuple(applied), tuple(issues))
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
