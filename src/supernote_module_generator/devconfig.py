"""Apply machine-local Supernote plugin tool paths to generator commands."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Iterator
import uuid


@dataclass(frozen=True)
class DevConfigApplication:
    """The settings applied from one plugin's optional devconfig.json."""

    path: Path
    applied: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


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
    if not path.is_file():
        return {}, issues
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
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


def _write_local_properties(path: Path, android_sdk: Path) -> None:
    sdk_line = f"sdk.dir={str(android_sdk).replace(chr(92), '/')}"
    if path.is_file():
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines()
        updated: list[str] = []
        found = False
        for line in lines:
            if line.startswith("sdk.dir="):
                updated.append(sdk_line)
                found = True
            else:
                updated.append(line)
        if not found:
            updated.append(sdk_line)
        content = "\n".join(updated) + "\n"
    else:
        content = sdk_line + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def configured_developer_environment(root: Path) -> Iterator[DevConfigApplication]:
    """Temporarily apply a plugin's devconfig.json to this process and children.

    The official plugin scripts use only existing Java and Android SDK directories
    and fall back to the launching environment for absent or unusable values. The
    generator follows the same selection rules here, restores its environment after
    the command, and gives every subprocess and Doctor lookup one configuration.
    """

    plugin_root = root.expanduser().resolve()
    path = plugin_root / "devconfig.json"
    configured, issues = _read_config(path, plugin_root)
    updates: dict[str, str] = {}
    applied: list[str] = []

    java_home = configured.get("javaHome")
    if java_home is not None:
        if java_home.is_dir():
            updates["JAVA_HOME"] = str(java_home)
            java_bin = str(java_home / "bin")
            current_path = os.environ.get("PATH", "")
            updates["PATH"] = (
                java_bin + os.pathsep + current_path if current_path else java_bin
            )
            applied.append("javaHome")
        else:
            issues.append(
                f"devconfig.json javaHome is not a directory: {java_home}; "
                "the existing JAVA_HOME and PATH will be used."
            )

    android_sdk = configured.get("androidSdk")
    if android_sdk is not None:
        if android_sdk.is_dir():
            updates["ANDROID_HOME"] = str(android_sdk)
            updates["ANDROID_SDK_ROOT"] = str(android_sdk)
            applied.append("androidSdk")
            try:
                _write_local_properties(
                    plugin_root / "android/local.properties", android_sdk
                )
            except (OSError, UnicodeError) as exc:
                issues.append(
                    "Could not synchronize android/local.properties with "
                    f"devconfig.json: {exc}"
                )
        else:
            issues.append(
                f"devconfig.json androidSdk is not a directory: {android_sdk}; "
                "the existing Android SDK environment will be used."
            )

    adb = configured.get("adb")
    if adb is not None:
        updates["ADB_BIN"] = str(adb)
        applied.append("adb")
        if not adb.is_file():
            issues.append(
                f"devconfig.json adb is not a file: {adb}; ADB_BIN will still use "
                "this configured path, so child commands that use ADB may fail."
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
