"""Idempotent parent wiring for the one plugin-level generated runtime."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import uuid
from dataclasses import dataclass

from .errors import ConfigurationError, FilesystemError
from .filesystem import (
    contained_entry_kind_no_follow,
    contained_tree_entries_no_follow,
    read_contained_regular_bytes_no_follow,
    validate_contained_path_no_follow,
)


PROJECT_NAME = "supernote-runtime"
ANNOTATIONS_PROJECT = "supernote-module-annotations"
PROCESSOR_PROJECT = "supernote-module-processor"
START = "// sn-module-gen-runtime"
END = "// end sn-module-gen-runtime"
PACKAGE_START = "// supernote-module-package"
PACKAGE_END = "// end supernote-module-package"
LEGACY_MARKERS = tuple(
    f"// supernote-module-v{version}-{kind}"
    for version in (1, 2, 3, 4)
    for kind in ("runtime", "package")
)


@dataclass(frozen=True)
class CanonicalWiringBlock:
    path: Path
    marker: str
    content: str


@dataclass(frozen=True)
class CanonicalWiringFile:
    path: Path
    marker: str
    content: bytes
    previous: bytes
    previous_mode: int


@dataclass(frozen=True)
class WiringSourceFile:
    path: Path
    content: bytes
    mode: int


@dataclass(frozen=True)
class RuntimeWiringSnapshot:
    settings: WiringSourceFile
    app_build: WiringSourceFile
    application: WiringSourceFile | None

    @property
    def files(self) -> tuple[WiringSourceFile, ...]:
        return (
            (self.settings, self.app_build, self.application)
            if self.application is not None
            else (self.settings, self.app_build)
        )


def capture_runtime_wiring_files(plugin_root: Path) -> RuntimeWiringSnapshot:
    """Capture every integration file once without following any component."""

    root = plugin_root.resolve()
    settings, app_build = integration_files(root)
    application = _application_file(root)

    def capture(path: Path) -> WiringSourceFile:
        try:
            content, metadata = read_contained_regular_bytes_no_follow(root, path)
        except FilesystemError as exc:
            raise ConfigurationError(
                f"Android integration file is unsafe or unreadable: {path}: {exc}"
            ) from exc
        return WiringSourceFile(path, content, stat.S_IMODE(metadata.st_mode))

    return RuntimeWiringSnapshot(
        capture(settings),
        capture(app_build),
        capture(application) if application is not None else None,
    )


def desired_runtime_wiring_files(
    plugin_root: Path,
    *,
    enabled: bool,
    snapshot: RuntimeWiringSnapshot | None = None,
) -> tuple[CanonicalWiringFile, ...]:
    """Render complete canonical parent files without mutating the project."""

    snapshot = snapshot or capture_runtime_wiring_files(plugin_root)
    settings = snapshot.settings.path
    app_build = snapshot.app_build.path
    rows = []
    for source in snapshot.files:
        path = source.path
        content = source.content
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationError(f"Android integration file must be UTF-8: {path}") from exc
        if any(marker in text for marker in LEGACY_MARKERS):
            raise ConfigurationError(
                f"{path} contains unsupported legacy runtime wiring"
            )
        if path == settings:
            canonical = _settings_block(path.suffix == ".kts")
            if (
                _has_owned_wiring_fragment(text, START, END, canonical)
                and not _owned_block_is_canonical(
                    text, START, END, canonical, allow_indent=False
                )
            ):
                text = _remove_owned_wiring_fragments(
                    text, start=START, end=END, canonical=canonical
                )
            desired = _replace_block(
                text,
                canonical if enabled else None,
            )
            marker = "sn-module-gen-runtime"
        elif path == app_build:
            canonical = _dependency_block(path.suffix == ".kts")
            if (
                _has_owned_wiring_fragment(text, START, END, canonical)
                and not _owned_block_is_canonical(
                    text, START, END, canonical, allow_indent=False
                )
            ):
                text = _remove_owned_wiring_fragments(
                    text, start=START, end=END, canonical=canonical
                )
            desired = _replace_block(
                text,
                canonical if enabled else None,
            )
            marker = "sn-module-gen-runtime"
        else:
            canonical = _package_block(path.suffix == ".kt")
            if (
                _has_owned_wiring_fragment(
                    text, PACKAGE_START, PACKAGE_END, canonical
                )
                and not _owned_block_is_canonical(
                    text,
                    PACKAGE_START,
                    PACKAGE_END,
                    canonical,
                    allow_indent=True,
                )
            ):
                text = _remove_owned_wiring_fragments(
                    text,
                    start=PACKAGE_START,
                    end=PACKAGE_END,
                    canonical=canonical,
                )
            desired = _replace_package_registration(
                text,
                enabled=enabled,
                kotlin=path.suffix == ".kt",
            )
            marker = "supernote-module-package"
        rows.append(
            CanonicalWiringFile(
                path,
                marker,
                desired.encode("utf-8"),
                content,
                source.mode,
            )
        )
    return tuple(rows)


def expected_runtime_wiring_blocks(
    plugin_root: Path,
    *,
    enabled: bool,
    snapshot: RuntimeWiringSnapshot | None = None,
) -> tuple[CanonicalWiringBlock, ...]:
    """Return canonical owned blocks without changing their user-owned files."""

    if not enabled:
        return ()
    snapshot = snapshot or capture_runtime_wiring_files(plugin_root)
    settings = snapshot.settings.path
    app_build = snapshot.app_build.path
    blocks = [
        CanonicalWiringBlock(
            settings,
            "sn-module-gen-runtime",
            _settings_block(settings.suffix == ".kts"),
        ),
        CanonicalWiringBlock(
            app_build,
            "sn-module-gen-runtime",
            _dependency_block(app_build.suffix == ".kts"),
        ),
    ]
    application = snapshot.application
    if application is not None:
        try:
            content = application.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationError(
                f"Android integration file must be UTF-8: {application.path}"
            ) from exc
        canonical = _package_block(application.path.suffix == ".kt")
        if (
            _has_owned_wiring_fragment(
                content, PACKAGE_START, PACKAGE_END, canonical
            )
            and not _owned_block_is_canonical(
                content,
                PACKAGE_START,
                PACKAGE_END,
                canonical,
                allow_indent=True,
            )
        ):
            content = _remove_owned_wiring_fragments(
                content,
                start=PACKAGE_START,
                end=PACKAGE_END,
                canonical=canonical,
            )
        expected = _replace_package_registration(
            content,
            enabled=True,
            kotlin=application.path.suffix == ".kt",
        )
        blocks.append(
            CanonicalWiringBlock(
                application.path,
                "supernote-module-package",
                _extract_marker_block(expected, PACKAGE_START, PACKAGE_END),
            )
        )
    return tuple(blocks)


def inspect_runtime_wiring_blocks(
    plugin_root: Path,
    *,
    enabled: bool,
    snapshot: RuntimeWiringSnapshot | None = None,
) -> tuple[str, ...]:
    """Structurally compare all actual owned blocks with canonical payloads."""

    snapshot = snapshot or capture_runtime_wiring_files(plugin_root)
    expected = expected_runtime_wiring_blocks(
        plugin_root, enabled=enabled, snapshot=snapshot
    )
    expected_by_path = {item.path: item for item in expected}
    issues = []
    for source in snapshot.files:
        path = source.path
        try:
            content = source.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationError(
                f"Android integration file must be UTF-8: {path}"
            ) from exc
        item = expected_by_path.get(path)
        start, end = (
            (PACKAGE_START, PACKAGE_END)
            if item is not None and item.marker.endswith("package")
            else (START, END)
        )
        start_count = content.count(start)
        end_count = content.count(end)
        wanted = 1 if item is not None else 0
        if start_count != wanted or end_count != wanted:
            issues.append(
                f"{path}: malformed runtime marker block; expected {wanted} "
                f"start/end pair, found {start_count}/{end_count}"
            )
            continue
        if item is None:
            continue
        try:
            actual = _extract_marker_block(content, start, end)
        except ConfigurationError as exc:
            issues.append(f"{path}: {exc}")
            continue
        if actual != item.content:
            issues.append(f"{path}: runtime marker payload is not canonical")
    return tuple(issues)


def _extract_marker_block(content: str, start: str, end: str) -> str:
    pattern = re.compile(
        r"^[ \t]*" + re.escape(start) + r"[ \t]*\r?$\n.*?^[ \t]*"
        + re.escape(end) + r"[ \t]*\r?$",
        re.DOTALL | re.MULTILINE,
    )
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        raise ConfigurationError("runtime marker block is structurally invalid")
    return matches[0].group(0)


def _owned_block_is_canonical(
    content: str,
    start: str,
    end: str,
    canonical: str,
    *,
    allow_indent: bool,
) -> bool:
    try:
        actual = _extract_marker_block(content, start, end)
    except ConfigurationError:
        return False
    if "\r" in actual:
        return False
    if not allow_indent:
        return actual == canonical
    actual_lines = actual.split("\n")
    canonical_lines = canonical.split("\n")
    if len(actual_lines) != len(canonical_lines):
        return False
    indent = actual_lines[0][:-len(actual_lines[0].lstrip(" \t"))]
    return actual_lines == [indent + line for line in canonical_lines]


def _has_owned_wiring_fragment(
    content: str, start: str, end: str, canonical: str
) -> bool:
    if start in content or end in content:
        return True
    owned_lines = {
        line.strip()
        for line in canonical.splitlines()
        if line.strip() and line.strip() not in {start, end, "dependencies {", "}"}
    }
    return any(line.strip() in owned_lines for line in content.splitlines())


def integration_files(plugin_root: Path) -> tuple[Path, Path]:
    android = plugin_root.resolve() / "android"
    settings = _select_integration_file(
        plugin_root,
        tuple(android / name for name in ("settings.gradle", "settings.gradle.kts")),
    )
    app_build = _select_integration_file(
        plugin_root,
        tuple(
            android / "app" / name
            for name in ("build.gradle", "build.gradle.kts")
        ),
    )
    if settings is None:
        raise ConfigurationError("Supernote plugin is missing Android settings")
    if app_build is None:
        raise ConfigurationError("Supernote plugin is missing the app Gradle build file")
    return settings, app_build


def _select_integration_file(
    plugin_root: Path,
    candidates: tuple[Path, ...],
) -> Path | None:
    selected = None
    for candidate in candidates:
        try:
            kind = contained_entry_kind_no_follow(plugin_root, candidate)
        except FilesystemError as exc:
            raise ConfigurationError(
                f"Android integration path is unsafe: {candidate}: {exc}"
            ) from exc
        if kind is None:
            continue
        try:
            validate_contained_path_no_follow(
                plugin_root,
                candidate,
                allowed_final_kinds={"file"},
            )
        except FilesystemError as exc:
            raise ConfigurationError(
                f"Android integration path is unsafe: {candidate}: {exc}"
            ) from exc
        if selected is None:
            selected = candidate
    return selected


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

    snapshot = capture_runtime_wiring_files(plugin_root)
    settings = snapshot.settings.path
    app_build = snapshot.app_build.path
    application = snapshot.application.path if snapshot.application else None
    try:
        originals = {
            source.path: source.content.decode("utf-8")
            for source in snapshot.files
        }
    except UnicodeDecodeError as exc:
        raise ConfigurationError("Android integration files must be UTF-8") from exc
    _reject_legacy_wiring(originals)
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
    if application is not None:
        desired[application] = _replace_package_registration(
            originals[application], enabled=enabled, kotlin=application.suffix == ".kt"
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
) -> None:
    snapshot = capture_runtime_wiring_files(plugin_root)
    settings = snapshot.settings.path
    app_build = snapshot.app_build.path
    application = snapshot.application.path if snapshot.application else None
    try:
        inspected = {
            source.path: source.content.decode("utf-8")
            for source in snapshot.files
        }
    except UnicodeDecodeError as exc:
        raise ConfigurationError("Android integration files must be UTF-8") from exc
    _reject_legacy_wiring(inspected)
    for path in (settings, app_build):
        count = inspected[path].count(START)
        expected = 1 if enabled else 0
        if count != expected:
            raise ConfigurationError(
                f"{path} contains {count} generated runtime blocks; expected {expected}"
            )
    if application is not None:
        count = inspected[application].count(PACKAGE_START)
        expected = 1 if enabled else 0
        if count != expected and not (
            enabled and allow_missing_package and count == 0
        ):
            raise ConfigurationError(
                f"{application} contains {count} generated package blocks; "
                f"expected {expected}"
            )


def _settings_block(kotlin: bool) -> str:
    projects = (
        (PROJECT_NAME, ".supernote-module/runtime"),
        (ANNOTATIONS_PROJECT, ".supernote-module/runtime/annotations"),
        (PROCESSOR_PROJECT, ".supernote-module/runtime/processor"),
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


def _package_block(kotlin: bool) -> str:
    registration = (
        "add(supernote.generated.runtime.SupernoteModulePackage())"
        if kotlin
        else "packages.add(new supernote.generated.runtime.SupernoteModulePackage());"
    )
    return f"{PACKAGE_START}\n{registration}\n{PACKAGE_END}"


def _remove_owned_wiring_fragments(
    content: str,
    *,
    start: str,
    end: str,
    canonical: str,
) -> str:
    """Remove only text whose generated ownership is unambiguous.

    Complete marker blocks are wholly generator-owned. For incomplete marker
    structures, exact marker lines and unique generated payload statements are owned,
    while generic Gradle braces and all other user text are preserved.
    """

    complete = re.compile(
        r"(?:\n)?^[ \t]*"
        + re.escape(start)
        + r"[ \t]*\r?$\n.*?^[ \t]*"
        + re.escape(end)
        + r"[ \t]*\r?$(?:\n)?",
        re.DOTALL | re.MULTILINE,
    )
    cleaned = complete.sub("\n", content)
    marker_line = re.compile(
        r"^[ \t]*(?:"
        + re.escape(start)
        + "|"
        + re.escape(end)
        + r")[ \t]*\r?(?:\n|$)",
        re.MULTILINE,
    )
    cleaned = marker_line.sub("", cleaned)
    for raw_line in canonical.splitlines():
        line = raw_line.strip()
        if not line or line in {start, end, "dependencies {", "}"}:
            continue
        owned_line = re.compile(
            r"^[ \t]*"
            + re.escape(line)
            + r"[ \t]*\r?(?:\n|$)",
            re.MULTILINE,
        )
        cleaned = owned_line.sub("", cleaned)
    return cleaned


def _replace_block(content: str, replacement: str | None) -> str:
    pattern = re.compile(
        r"(?:\n)?" + re.escape(START) + r"\n.*?\n" + re.escape(END) + r"(?:\n)?",
        re.DOTALL,
    )
    matches = list(pattern.finditer(content))
    if len(matches) > 1:
        raise ConfigurationError(
            "Android build contains duplicate generated runtime blocks"
        )
    if replacement is None:
        updated = pattern.sub("\n", content)
        return updated.rstrip() + "\n"
    if matches:
        updated = pattern.sub("\n" + replacement + "\n", content)
        return updated.rstrip() + "\n"
    return content.rstrip() + "\n\n" + replacement + "\n"


def _application_file(plugin_root: Path) -> Path | None:
    source = plugin_root.resolve() / "android/app/src/main/java"
    try:
        source_kind = contained_entry_kind_no_follow(plugin_root, source)
    except FilesystemError as exc:
        raise ConfigurationError(
            f"Android application source path is unsafe: {source}: {exc}"
        ) from exc
    if source_kind is None:
        return None
    try:
        validate_contained_path_no_follow(
            plugin_root,
            source,
            allowed_final_kinds={"directory"},
        )
    except FilesystemError as exc:
        raise ConfigurationError(
            f"Android application source path is unsafe: {source}: {exc}"
        ) from exc
    candidates = []
    for path, kind in contained_tree_entries_no_follow(plugin_root, source):
        if path.name not in {"MainApplication.kt", "MainApplication.java"}:
            continue
        if kind != "file":
            raise ConfigurationError(
                f"Android application integration path is unsafe: {path}"
            )
        candidates.append(path)
    for path in candidates:
        try:
            validate_contained_path_no_follow(
                plugin_root,
                path,
                allowed_final_kinds={"file"},
            )
        except FilesystemError as exc:
            raise ConfigurationError(
                f"Android application integration path is unsafe: {path}: {exc}"
            ) from exc
    matches = sorted(
        candidates
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
        raise ConfigurationError(
            "MainApplication has duplicate generated package blocks"
        )
    cleaned = pattern.sub("", content)
    if not enabled:
        return cleaned
    if kotlin:
        anchor = re.search(
            r"(?m)^(?P<indent>[ \t]*).*PackageList\(this\)\.packages\.apply"
            r"\s*\{[ \t]*\r?$",
            cleaned,
        )
        if anchor is None:
            raise ConfigurationError(
                "cannot locate PackageList(this).packages.apply in MainApplication.kt"
            )
        indent = anchor.group("indent") + "  "
        block = (
            f"\n{indent}{PACKAGE_START}\n"
            f"{indent}add(supernote.generated.runtime.SupernoteModulePackage())\n"
            f"{indent}{PACKAGE_END}"
        )
        return cleaned[: anchor.end()] + block + cleaned[anchor.end() :]
    anchor = re.search(
        r"(?m)^(?P<indent>[ \t]*).*new PackageList\(this\)\.getPackages\(\);"
        r"[ \t]*\r?$",
        cleaned,
    )
    if anchor is None:
        raise ConfigurationError(
            "cannot locate new PackageList(this).getPackages() in MainApplication.java"
        )
    indent = anchor.group("indent")
    block = (
        f"\n{indent}{PACKAGE_START}\n"
        f"{indent}packages.add(new supernote.generated.runtime.SupernoteModulePackage());\n"
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


def _reject_legacy_wiring(files: dict[Path, str]) -> None:
    for path, content in files.items():
        if any(marker in content for marker in LEGACY_MARKERS):
            raise ConfigurationError(
                f"{path} contains unsupported legacy runtime wiring; "
                "sn-module-gen does not read or convert V1-V4 generated state"
            )
