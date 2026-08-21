"""Idempotent parent wiring for the one plugin-level V3 runtime component."""
from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

from .errors import ConfigurationError


PROJECT_NAME = "supernote-v3-runtime"
ANNOTATIONS_PROJECT = "supernote-v3-annotations"
PROCESSOR_PROJECT = "supernote-v3-processor"
START = "// supernote-module-v3-runtime"
END = "// end supernote-module-v3-runtime"
PACKAGE_START = "// supernote-module-v3-package"
PACKAGE_END = "// end supernote-module-v3-package"
LEGACY_MARKERS = (
    "// supernote-module-v2-runtime",
    "// supernote-module-v2-package",
)
LEGACY_BLOCKS = (
    ("// supernote-module-v2-runtime", "// end supernote-module-v2-runtime"),
    ("// supernote-module-v2-package", "// end supernote-module-v2-package"),
)


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


def integration_mutation_files(plugin_root: Path) -> tuple[Path, ...]:
    """Return every user-owned Android file runtime wiring may change."""

    settings, app_build = integration_files(plugin_root)
    application = _application_file(plugin_root)
    return (
        (settings, app_build, application)
        if application is not None
        else (settings, app_build)
    )


def set_runtime_wiring(plugin_root: Path, *, enabled: bool) -> tuple[Path, Path]:
    """Wire or unwire exactly one generated Android library atomically."""

    settings, app_build = integration_files(plugin_root)
    application = _application_file(plugin_root)
    originals = {
        settings: settings.read_text(encoding="utf-8"),
        app_build: app_build.read_text(encoding="utf-8"),
    }
    if application is not None:
        originals[application] = application.read_text(encoding="utf-8")
    cleaned = {
        path: _remove_legacy_v2_blocks(content)
        for path, content in originals.items()
    }
    desired = {
        settings: _replace_block(
            cleaned[settings],
            _settings_block(settings.suffix == ".kts") if enabled else None,
        ),
        app_build: _replace_block(
            cleaned[app_build],
            _dependency_block(app_build.suffix == ".kts") if enabled else None,
        ),
    }
    if application is not None:
        desired[application] = _replace_package_registration(
            cleaned[application], enabled=enabled, kotlin=application.suffix == ".kt"
        )
    changed: list[Path] = []
    try:
        for path in originals:
            if desired[path] == originals[path]:
                continue
            _atomic_write(path, desired[path])
            changed.append(path)
    except Exception:
        for path in reversed(changed):
            _atomic_write(path, originals[path])
        raise
    return settings, app_build


def verify_runtime_wiring(
    plugin_root: Path,
    *,
    enabled: bool,
    allow_missing_package: bool = False,
    allow_legacy_v2: bool = False,
) -> None:
    settings, app_build = integration_files(plugin_root)
    inspected = {
        settings: settings.read_text(encoding="utf-8"),
        app_build: app_build.read_text(encoding="utf-8"),
    }
    application = _application_file(plugin_root)
    if application is not None:
        inspected[application] = application.read_text(encoding="utf-8")
    if not allow_legacy_v2:
        _reject_legacy_v2_wiring(inspected)
    for path in (settings, app_build):
        count = path.read_text(encoding="utf-8").count(START)
        expected = 1 if enabled else 0
        if count != expected:
            raise ConfigurationError(
                f"{path} contains {count} V3 runtime blocks; expected {expected}"
            )
    if application is not None:
        count = application.read_text(encoding="utf-8").count(PACKAGE_START)
        expected = 1 if enabled else 0
        if count != expected and not (
            enabled and allow_missing_package and count == 0
        ):
            raise ConfigurationError(
                f"{application} contains {count} V3 package blocks; "
                f"expected {expected}"
            )


def _settings_block(kotlin: bool) -> str:
    projects = (
        (PROJECT_NAME, ".supernote-module/v3-runtime"),
        (ANNOTATIONS_PROJECT, ".supernote-module/v3-runtime/annotations"),
        (PROCESSOR_PROJECT, ".supernote-module/v3-runtime/processor"),
    )
    if kotlin:
        body = "\n".join(
            f'include(":{name}")\nproject(":{name}").projectDir = file("{path}")'
            for name, path in projects
        )
    else:
        body = "\n".join(
            f"include ':{name}'\nproject(':{name}').projectDir = file('{path}')"
            for name, path in projects
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
        raise ConfigurationError("Android build contains duplicate V3 runtime blocks")
    if replacement is None:
        updated = pattern.sub("\n", content)
        return updated.rstrip() + "\n"
    if matches:
        updated = pattern.sub("\n" + replacement + "\n", content)
        return updated.rstrip() + "\n"
    return content.rstrip() + "\n\n" + replacement + "\n"


def _application_file(plugin_root: Path) -> Path | None:
    source = plugin_root.resolve() / "android/app/src/main/java"
    if not source.is_dir():
        return None
    matches = sorted(
        path
        for name in ("MainApplication.kt", "MainApplication.java")
        for path in source.rglob(name)
    )
    if len(matches) > 1:
        raise ConfigurationError("Android app has multiple MainApplication sources")
    return matches[0] if matches else None


def _replace_package_registration(
    content: str, *, enabled: bool, kotlin: bool
) -> str:
    pattern = re.compile(
        r"[ \t]*" + re.escape(PACKAGE_START) + r"\n.*?\n[ \t]*"
        + re.escape(PACKAGE_END) + r"\n?",
        re.DOTALL,
    )
    matches = list(pattern.finditer(content))
    if len(matches) > 1:
        raise ConfigurationError("MainApplication has duplicate V3 package blocks")
    cleaned = pattern.sub("", content)
    if not enabled:
        return cleaned
    if kotlin:
        anchor = re.search(r"(?m)^(?P<indent>[ \t]*).*PackageList\(this\)\.packages\.apply\s*\{[ \t]*$", cleaned)
        if anchor is None:
            raise ConfigurationError(
                "cannot locate PackageList(this).packages.apply in MainApplication.kt"
            )
        indent = anchor.group("indent") + "  "
        block = (
            f"\n{indent}{PACKAGE_START}\n"
            f"{indent}add(supernote.generated.runtime.SupernoteV3Package())\n"
            f"{indent}{PACKAGE_END}"
        )
        return cleaned[: anchor.end()] + block + cleaned[anchor.end() :]
    anchor = re.search(
        r"(?m)^(?P<indent>[ \t]*).*new PackageList\(this\)\.getPackages\(\);[ \t]*$",
        cleaned,
    )
    if anchor is None:
        raise ConfigurationError(
            "cannot locate new PackageList(this).getPackages() in MainApplication.java"
        )
    indent = anchor.group("indent")
    block = (
        f"\n{indent}{PACKAGE_START}\n"
        f"{indent}packages.add(new supernote.generated.runtime.SupernoteV3Package());\n"
        f"{indent}{PACKAGE_END}"
    )
    return cleaned[: anchor.end()] + block + cleaned[anchor.end() :]


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


def _reject_legacy_v2_wiring(files: dict[Path, str]) -> None:
    for path, content in files.items():
        if any(marker in content for marker in LEGACY_MARKERS):
            raise ConfigurationError(
                f"{path} contains stale V2 runtime wiring; V3 does not read or "
                "convert V2 generated state"
            )


def _remove_legacy_v2_blocks(content: str) -> str:
    """Remove only complete generator-owned V2 marker blocks."""

    updated = content
    for start, end in LEGACY_BLOCKS:
        start_count = updated.count(start)
        end_count = updated.count(end)
        if start_count != end_count or start_count > 1:
            raise ConfigurationError(
                "Android source contains malformed or duplicate stale V2 wiring"
            )
        if start_count == 0:
            continue
        pattern = re.compile(
            r"(?:\n)?^[ \t]*"
            + re.escape(start)
            + r"[ \t]*\n.*?^[ \t]*"
            + re.escape(end)
            + r"[ \t]*(?:\n)?",
            re.DOTALL | re.MULTILINE,
        )
        updated, count = pattern.subn("\n", updated)
        if count != 1:
            raise ConfigurationError("Android source contains malformed stale V2 wiring")
    return updated.rstrip() + "\n"
