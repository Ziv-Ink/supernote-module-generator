"""Safe add/remove wiring for local packages in Supernote plugin projects."""
from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from .config import METADATA_FILES, gradle_project_name, normalize_backend
from .errors import ConfigurationError, FilesystemError, GeneratorError
from .subprocesses import run_process

LOCAL_MODULES_DIR = "local_modules"
LEGACY_MODULES_DIRS = ("local-modules", "modules")


def plugin_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not (root / "PluginConfig.json").is_file() or not (root / "package.json").is_file():
        raise ConfigurationError("Run this command from a Supernote plugin root (PluginConfig.json and package.json are required)")
    if not (root / "android").is_dir():
        raise ConfigurationError("Supernote plugin is missing its android directory")
    return root


def settings_file(root: Path) -> Path:
    for name in ("settings.gradle", "settings.gradle.kts"):
        path = root / "android" / name
        if path.is_file():
            return path
    raise ConfigurationError("Supernote plugin is missing android/settings.gradle")


def _project_name(module: str) -> str:
    return gradle_project_name(module)


def marker(module: str) -> str:
    return f"// local-native-module: {module}"


def settings_block(module: str, kotlin_dsl: bool) -> str:
    name = _project_name(module)
    lines = [marker(module)]
    for suffix in ("native-annotation", "native-processor"):
        project = f":{name}:{suffix}"
        directory = f"../{LOCAL_MODULES_DIR}/{module}/android/.native-module/{suffix.removeprefix('native-')}"
        if kotlin_dsl:
            lines += [f'include("{project}")', f'project("{project}").projectDir = file("{directory}")']
        else:
            lines += [f"include '{project}'", f"project('{project}').projectDir = file('{directory}')"]
    lines.append(f"// end local-native-module: {module}")
    return "\n".join(lines) + "\n"


def _read_package(root: Path) -> tuple[Path, dict[str, object]]:
    path = root / "package.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("package.json must contain an object")
    return path, value


def read_metadata(path: Path) -> dict[str, object]:
    metadata_path = next((path / name for name in METADATA_FILES if (path / name).is_file()), None)
    if metadata_path is None:
        raise ConfigurationError(f"{path} is not owned by this generator")
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Could not read {metadata_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{metadata_path} must contain an object")
    backend = value.get("backend")
    if isinstance(backend, str):
        value["backend"] = normalize_backend(backend)
    return value


def assert_no_module_collisions(
    root: Path,
    module: str,
    namespace: str,
    *,
    module_name: str | None = None,
    native_library_name: str | None = None,
    jsi_global_name: str | None = None,
    excluding: str | None = None,
) -> None:
    _, package = _read_package(root)
    dependencies = package.get("dependencies", {})
    dev_dependencies = package.get("devDependencies", {})
    for section_name, entries in (
        ("dependencies", dependencies),
        ("devDependencies", dev_dependencies),
    ):
        if (
            isinstance(entries, dict)
            and module in entries
            and module != excluding
        ):
            raise ConfigurationError(
                f"Package name {module!r} already exists in parent "
                f"package.json {section_name}"
            )
    wanted_project = gradle_project_name(module)
    for directory in (LOCAL_MODULES_DIR, *LEGACY_MODULES_DIRS):
        modules_root = root / directory
        if not modules_root.exists():
            continue
        for metadata_name in METADATA_FILES:
            for metadata_path in modules_root.rglob(metadata_name):
                candidate = metadata_path.parent
                data = read_metadata(candidate)
                candidate_name = data.get("npm_name")
                if not isinstance(candidate_name, str) or candidate_name == excluding:
                    continue
                if candidate_name == module:
                    raise ConfigurationError(f"Local native code module already exists: {candidate}. Use update {module!r} instead.")
                if data.get("android_namespace") == namespace:
                    raise ConfigurationError(f"Android namespace {namespace!r} is already used by local module {candidate_name!r}")
                if gradle_project_name(candidate_name) == wanted_project:
                    raise ConfigurationError(f"Gradle project name collision: {module!r} conflicts with {candidate_name!r}")
                if module_name and data.get("module_name") == module_name:
                    raise ConfigurationError(
                        f"JavaScript module name {module_name!r} is already used "
                        f"by local module {candidate_name!r}"
                    )
                if (
                    native_library_name
                    and data.get("native_library_name") == native_library_name
                ):
                    raise ConfigurationError(
                        f"Native library name {native_library_name!r} is already "
                        f"used by local module {candidate_name!r}"
                    )
                if jsi_global_name and data.get("jsi_global_name") == jsi_global_name:
                    raise ConfigurationError(
                        f"JSI global {jsi_global_name!r} is already used by "
                        f"local module {candidate_name!r}"
                    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    _write_text(path, json.dumps(value, indent=2) + "\n")


def _write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def choose_package_manager(root: Path, requested: str | None, *, interactive: bool, choose: callable | None = None) -> str:
    if requested:
        return requested
    found = [manager for manager, lock in (("yarn", "yarn.lock"), ("npm", "package-lock.json")) if (root / lock).is_file()]
    if len(found) == 1:
        return found[0]
    if not interactive:
        raise ConfigurationError("Could not choose a package manager; pass --package-manager yarn or --package-manager npm")
    if choose is None:
        raise ConfigurationError("Interactive package-manager selection is unavailable")
    return choose(found)


def _run_package_manager(command: list[str], root: Path, *, verbose: bool) -> subprocess.CompletedProcess[str]:
    if verbose:
        print("Running: " + shlex.join(command))
    try:
        result = run_process(command, cwd=root, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeneratorError(f"Could not run {command[0]}: {exc}") from exc
    if verbose:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def install_local(root: Path, module: str, manager: str, *, verbose: bool = False) -> None:
    relative = f"file:./{LOCAL_MODULES_DIR}/{module}"
    command = ["yarn", "add", relative, "--exact"] if manager == "yarn" else ["npm", "install", "--save", relative]
    result = _run_package_manager(command, root, verbose=verbose)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GeneratorError(f"{manager} could not install {module} (exit {result.returncode}): {detail}")


def refresh_dependencies(root: Path, manager: str, *, verbose: bool = False) -> None:
    command = ["yarn", "install"] if manager == "yarn" else ["npm", "install"]
    result = _run_package_manager(command, root, verbose=verbose)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GeneratorError(f"{manager} could not refresh dependencies (exit {result.returncode}): {detail}")


def clear_autolinking(root: Path) -> None:
    cache = root / "android" / "build" / "generated" / "autolinking"
    if cache.exists():
        shutil.rmtree(cache)


def wire(root: Path, module: str) -> Path:
    settings = settings_file(root)
    content = settings.read_text(encoding="utf-8")
    for start, end in (("rn-legacy-module", "rn-legacy-module"), ("local-kotlin-module", "local-kotlin-module")):
        legacy = re.compile(r"\n?// " + start + r": " + re.escape(module) + r"\n.*?// end " + end + r": " + re.escape(module) + r"\n?", re.DOTALL)
        content = legacy.sub("\n", content)
    current = re.compile(
        r"\n?// local-native-module: " + re.escape(module)
        + r"\n.*?// end local-native-module: " + re.escape(module) + r"\n?",
        re.DOTALL,
    )
    replacement = "\n" + settings_block(module, settings.suffix == ".kts")
    if current.search(content):
        content = current.sub(replacement, content)
        _write_text(settings, content.rstrip() + "\n")
    else:
        _write_text(settings, content.rstrip() + "\n\n" + replacement.lstrip())
    return settings


def unwire(root: Path, module: str) -> Path:
    settings = settings_file(root)
    content = settings.read_text(encoding="utf-8")
    pattern = re.compile(r"\n?// local-native-module: " + re.escape(module) + r"\n.*?// end local-native-module: " + re.escape(module) + r"\n?", re.DOTALL)
    updated, count = pattern.subn("\n", content)
    if count != 1:
        raise ConfigurationError(f"Could not find one generator-owned Android settings block for {module}")
    _write_text(settings, updated.rstrip() + "\n")
    return settings


def add_dependency(root: Path, module: str) -> None:
    path, package = _read_package(root)
    dependencies = package.setdefault("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ConfigurationError("package.json dependencies must be an object")
    dependencies[module] = f"file:./{LOCAL_MODULES_DIR}/{module}"
    _write_json(path, package)


def remove_dependency(root: Path, module: str) -> None:
    path, package = _read_package(root)
    dependencies = package.get("dependencies", {})
    if not isinstance(dependencies, dict) or module not in dependencies:
        raise ConfigurationError(f"package.json does not contain dependency {module}")
    del dependencies[module]
    _write_json(path, package)


def owned_module(root: Path, module: str) -> Path:
    for directory in (LOCAL_MODULES_DIR, *LEGACY_MODULES_DIRS):
        path = root / directory / module
        if any((path / name).is_file() for name in METADATA_FILES):
            return path
    path = root / LOCAL_MODULES_DIR / module
    raise ConfigurationError(f"Refusing to remove {path}: it is not owned by this generator")


def owned_modules(root: Path) -> list[tuple[str, Path]]:
    """Return generator-owned modules in deterministic package-name order."""
    found: dict[str, Path] = {}
    for directory in (LOCAL_MODULES_DIR, *LEGACY_MODULES_DIRS):
        modules_root = root / directory
        if not modules_root.is_dir():
            continue
        for metadata_name in METADATA_FILES:
            for metadata_path in sorted(modules_root.rglob(metadata_name)):
                module_path = metadata_path.parent
                value = read_metadata(module_path).get("npm_name")
                if not isinstance(value, str):
                    raise ConfigurationError(f"Invalid module name in {metadata_path}")
                existing = found.get(value)
                if existing and existing != module_path:
                    raise ConfigurationError(
                        f"Local module {value!r} exists in both {existing} and {module_path}"
                    )
                found[value] = module_path
    return sorted(found.items())
