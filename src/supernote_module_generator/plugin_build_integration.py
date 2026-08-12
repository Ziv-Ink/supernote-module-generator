"""Idempotent parent wiring for the one plugin-level V2 runtime component."""
from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

from .errors import ConfigurationError


PROJECT_NAME = "supernote-v2-runtime"
START = "// supernote-module-v2-runtime"
END = "// end supernote-module-v2-runtime"


def integration_files(plugin_root: Path) -> tuple[Path, Path]:
    android = plugin_root.resolve() / "android"
    settings = next(
        (android / name for name in ("settings.gradle", "settings.gradle.kts")
         if (android / name).is_file()),
        None,
    )
    app_build = next(
        (android / "app" / name for name in ("build.gradle", "build.gradle.kts")
         if (android / "app" / name).is_file()),
        None,
    )
    if settings is None:
        raise ConfigurationError("Supernote plugin is missing Android settings")
    if app_build is None:
        raise ConfigurationError("Supernote plugin is missing the app Gradle build file")
    return settings, app_build


def set_runtime_wiring(plugin_root: Path, *, enabled: bool) -> tuple[Path, Path]:
    """Wire or unwire exactly one generated Android library atomically."""

    settings, app_build = integration_files(plugin_root)
    originals = {
        settings: settings.read_text(encoding="utf-8"),
        app_build: app_build.read_text(encoding="utf-8"),
    }
    desired = {
        settings: _replace_block(
            originals[settings],
            _settings_block(settings.suffix == ".kts") if enabled else None,
        ),
        app_build: _replace_block(
            originals[app_build],
            _dependency_block(app_build.suffix == ".kts") if enabled else None,
        ),
    }
    changed: list[Path] = []
    try:
        for path in (settings, app_build):
            if desired[path] == originals[path]:
                continue
            _atomic_write(path, desired[path])
            changed.append(path)
    except Exception:
        for path in reversed(changed):
            _atomic_write(path, originals[path])
        raise
    return settings, app_build


def verify_runtime_wiring(plugin_root: Path, *, enabled: bool) -> None:
    settings, app_build = integration_files(plugin_root)
    for path in (settings, app_build):
        count = path.read_text(encoding="utf-8").count(START)
        expected = 1 if enabled else 0
        if count != expected:
            raise ConfigurationError(
                f"{path} contains {count} V2 runtime blocks; expected {expected}"
            )


def _settings_block(kotlin: bool) -> str:
    if kotlin:
        body = (
            f'include(":{PROJECT_NAME}")\n'
            f'project(":{PROJECT_NAME}").projectDir = '
            'file(".supernote-module/v2-runtime")'
        )
    else:
        body = (
            f"include ':{PROJECT_NAME}'\n"
            f"project(':{PROJECT_NAME}').projectDir = "
            "file('.supernote-module/v2-runtime')"
        )
    return f"{START}\n{body}\n{END}"


def _dependency_block(kotlin: bool) -> str:
    dependency = (
        f'implementation(project(":{PROJECT_NAME}"))'
        if kotlin
        else f"implementation project(':{PROJECT_NAME}')"
    )
    return f"{START}\ndependencies {{\n    {dependency}\n}}\n{END}"


def _replace_block(content: str, replacement: str | None) -> str:
    pattern = re.compile(
        r"(?:\n)?" + re.escape(START) + r"\n.*?\n" + re.escape(END) + r"(?:\n)?",
        re.DOTALL,
    )
    matches = list(pattern.finditer(content))
    if len(matches) > 1:
        raise ConfigurationError("Android build contains duplicate V2 runtime blocks")
    if replacement is None:
        updated = pattern.sub("\n", content)
        return updated.rstrip() + "\n"
    if matches:
        updated = pattern.sub("\n" + replacement + "\n", content)
        return updated.rstrip() + "\n"
    return content.rstrip() + "\n\n" + replacement + "\n"


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
