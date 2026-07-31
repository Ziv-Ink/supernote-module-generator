"""Dependency discovery without writing machine-local paths into projects."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import ConfigurationError


@dataclass(frozen=True)
class Toolchain:
    jdk17: Path | None
    android_sdk: Path | None
    node: Path | None
    npm: Path | None

    def versions(self) -> dict[str, str]:
        return {name: _version(path) if path else "unavailable" for name, path in {
            "jdk17": self.jdk17 / "bin" / ("java.exe" if os.name == "nt" else "java") if self.jdk17 else None,
            "node": self.node, "npm": self.npm,
        }.items()} | {
            "android_sdk": "detected" if self.android_sdk else "unavailable",
        }


@dataclass(frozen=True)
class DoctorCheck:
    label: str
    value: str
    available: bool
    required: bool = False


def _version(command: Path) -> str:
    try:
        run = subprocess.run([str(command), "--version"], capture_output=True, text=True, timeout=5, check=False)
        return ((run.stdout or run.stderr).splitlines() or ["unknown"])[0]
    except OSError:
        return "unavailable"


def _existing(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    for path in paths:
        try: resolved = path.expanduser().resolve()
        except OSError: continue
        if resolved.exists() and resolved not in unique: unique.append(resolved)
    return unique


def _java_major(home: Path) -> int | None:
    java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not java.is_file(): return None
    try:
        run = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=5, check=False)
    except OSError: return None
    match = re.search(r'(?:version )?"(\d+)', run.stderr + run.stdout)
    return int(match.group(1)) if match else None


def _jdk_candidates() -> list[Path]:
    candidates = [Path(value) for name in ("JAVA17_HOME", "JAVA_HOME") if (value := os.environ.get(name))]
    if platform.system() == "Linux": candidates += list(Path("/usr/lib/jvm").glob("*"))
    elif platform.system() == "Darwin": candidates += list(Path("/Library/Java/JavaVirtualMachines").glob("*/Contents/Home"))
    elif os.environ.get("ProgramFiles"): candidates += list(Path(os.environ["ProgramFiles"]).glob("Java/*"))
    return [path for path in _existing(candidates) if _java_major(path) == 17]


def _sdk_candidates(override: str | None) -> list[Path]:
    values = [override] + [os.environ.get(name) for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")]
    if platform.system() == "Darwin": values.append("~/Library/Android/sdk")
    elif platform.system() == "Linux": values.append("~/Android/Sdk")
    elif os.environ.get("LOCALAPPDATA"): values.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk"))
    return _existing([Path(value) for value in values if value])


def _choose(
    label: str,
    candidates: list[Path],
    supplied: str | None,
    interactive: bool,
    ask: Callable[[str], str],
    preferred_name: str | None = None,
    select: Callable[[str, list[Path], Path], Path] | None = None,
) -> Path | None:
    if supplied:
        path = Path(supplied).expanduser()
        if not path.exists(): raise ConfigurationError(f"{label} path does not exist: {path}")
        return path.resolve()
    if len(candidates) == 1: return candidates[0]
    if not interactive:
        return next((candidate for candidate in candidates if candidate.name == preferred_name), candidates[0] if candidates else None)
    if candidates:
        preferred_index = next((index for index, candidate in enumerate(candidates, 1) if candidate.name == preferred_name), 1)
        preferred = candidates[preferred_index - 1]
        if select is not None:
            return select(label, candidates, preferred)
        print(f"\nSeveral {label} installations were found. Choose the one used when building Android:")
        for index, candidate in enumerate(candidates, 1):
            suffix = " (recommended)" if index == preferred_index else ""
            print(f"  {index}) {candidate}{suffix}")
        answer = ask(f"Choose {label} [{preferred_index}]: ").strip()
        if not answer: return candidates[preferred_index - 1]
    else:
        print(f"\nNo {label} was found automatically. Generation can continue, but Android builds may fail.")
        answer = ask(f"{label} path [press Enter to skip]: ").strip()
    if not answer: return None
    if answer.isdigit() and 1 <= int(answer) <= len(candidates): return candidates[int(answer) - 1]
    path = Path(answer).expanduser()
    if not path.exists(): raise ConfigurationError(f"{label} path does not exist: {path}")
    return path.resolve()


def discover(
    *,
    interactive: bool,
    ask: Callable[[str], str] = input,
    select: Callable[[str, list[Path], Path], Path] | None = None,
    jdk17: str | None = None,
    android_sdk: str | None = None,
    node: str | None = None,
    npm: str | None = None,
) -> Toolchain:
    sdk = _choose("Android SDK", _sdk_candidates(android_sdk), android_sdk, interactive, ask, select=select)
    def executable(value: str | None, command: str) -> list[Path]:
        return _existing([Path(value)]) if value else _existing([Path(found) for found in [shutil.which(command)] if found])
    return Toolchain(
        jdk17=_choose("JDK 17", _jdk_candidates(), jdk17, interactive, ask, select=select),
        android_sdk=sdk,
        node=_choose("Node.js", executable(node, "node"), node, interactive, ask, select=select),
        npm=_choose("npm", executable(npm, "npm"), npm, interactive, ask, select=select),
    )


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path.resolve() for path in paths if path.is_file()), None)


def _ndk_root(android_sdk: Path | None) -> Path | None:
    if android_sdk is None:
        return None
    candidates: list[Path] = []
    ndk_bundle = android_sdk / "ndk-bundle"
    if ndk_bundle.is_dir():
        candidates.append(ndk_bundle)
    ndk = android_sdk / "ndk"
    if ndk.is_dir():
        candidates.extend(path for path in ndk.iterdir() if path.is_dir())
    return sorted(candidates, key=lambda path: _version_tuple(path.name), reverse=True)[0] if candidates else None


def _host_tag() -> str:
    machine = platform.machine().lower()
    if platform.system() == "Darwin":
        return "darwin-arm64" if machine in {"arm64", "aarch64"} else "darwin-x86_64"
    if platform.system() == "Windows":
        return "windows-x86_64"
    return "linux-x86_64"


def _clang_paths(ndk: Path | None) -> tuple[Path | None, Path | None]:
    if ndk is None:
        return None, None
    suffix = ".exe" if os.name == "nt" else ""
    prebuilt = ndk / "toolchains" / "llvm" / "prebuilt"
    preferred = prebuilt / _host_tag()
    hosts = [preferred]
    if prebuilt.is_dir():
        hosts.extend(path for path in sorted(prebuilt.iterdir()) if path != preferred)
    bin_dirs = [host / "bin" for host in hosts if host.is_dir()]
    return (
        _first_existing([directory / f"clang{suffix}" for directory in bin_dirs]),
        _first_existing([directory / f"clang++{suffix}" for directory in bin_dirs]),
    )


def _cmake_path(android_sdk: Path | None) -> Path | None:
    suffix = ".exe" if os.name == "nt" else ""
    candidates: list[Path] = []
    if android_sdk is not None and (android_sdk / "cmake").is_dir():
        candidates.extend(
            version / "bin" / f"cmake{suffix}"
            for version in (android_sdk / "cmake").iterdir()
            if version.is_dir()
        )
    found = shutil.which("cmake")
    if found:
        candidates.append(Path(found))
    existing = [path.resolve() for path in candidates if path.is_file()]
    return sorted(
        existing,
        key=lambda path: _cmake_version(path),
        reverse=True,
    )[0] if existing else None


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", value)
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def _cmake_version(path: Path) -> tuple[int, ...]:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return ()
    return _version_tuple(result.stdout or result.stderr)


def _supports_standard(compiler: Path | None, language: str, standard: str) -> bool:
    if compiler is None:
        return False
    source = "int main(void) { int *value = nullptr; return value != nullptr; }\n"
    if language == "c++":
        source = "int main() { return 0; }\n"
    try:
        result = subprocess.run(
            [
                str(compiler),
                "-fsyntax-only",
                "-x",
                language,
                f"-std={standard}",
                "-target",
                "aarch64-linux-android27",
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _react_native_version(root: Path) -> str:
    installed = root / "node_modules" / "react-native" / "package.json"
    try:
        value = json.loads(installed.read_text(encoding="utf-8"))
        version = value.get("version")
        if isinstance(version, str):
            return version
    except (OSError, ValueError):
        pass
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        requested = {
            **package.get("devDependencies", {}),
            **package.get("dependencies", {}),
        }.get("react-native")
        if isinstance(requested, str):
            return f"not installed (package.json requests {requested})"
    except (OSError, ValueError, TypeError):
        pass
    return "not installed"


def _adb_status(adb: Path | None) -> str:
    if adb is None:
        return "unavailable (needed only for deployment)"
    try:
        result = subprocess.run(
            [str(adb), "devices"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        connected = sum(
            1
            for line in result.stdout.splitlines()[1:]
            if line.strip().endswith("\tdevice")
        )
    except OSError:
        return f"{adb} (device status unavailable; deployment only)"
    suffix = (
        f"{connected} device{'s' if connected != 1 else ''} connected"
        if connected
        else "no device connected; deployment only"
    )
    return f"{adb} ({suffix})"


def doctor_checks(
    root: Path,
    backend: str | None,
    *,
    toolchain: Toolchain | None = None,
) -> list[DoctorCheck]:
    """Inspect build prerequisites without changing the host or project."""
    selected = backend is not None
    native = backend in {"jni", "jsi"}
    tools = toolchain or discover(interactive=False)
    sdk = tools.android_sdk
    ndk = _ndk_root(sdk)
    clang, clangxx = _clang_paths(ndk)
    cmake = _cmake_path(sdk)
    cmake_version = _cmake_version(cmake) if cmake else ()
    npm = tools.npm or (Path(found).resolve() if (found := shutil.which("npm")) else None)
    yarn = Path(found).resolve() if (found := shutil.which("yarn")) else None
    adb = Path(found).resolve() if (found := shutil.which("adb")) else None
    gradle = _first_existing([
        root / "android" / ("gradlew.bat" if os.name == "nt" else "gradlew"),
        root / ("gradlew.bat" if os.name == "nt" else "gradlew"),
    ])
    platform35 = sdk / "platforms" / "android-35" if sdk else None
    c23 = _supports_standard(clang, "c", "c23")
    cpp23 = _supports_standard(clangxx, "c++", "c++23")
    react_native = _react_native_version(root)

    checks = [
        DoctorCheck(
            "Python 3.9+",
            platform.python_version(),
            sys.version_info >= (3, 9),
            selected,
        ),
        DoctorCheck("JDK 17", str(tools.jdk17 or "unavailable"), tools.jdk17 is not None, selected),
        DoctorCheck("Android SDK", str(sdk or "unavailable"), sdk is not None, selected),
        DoctorCheck(
            "Android API 35",
            str(platform35) if platform35 and platform35.is_dir() else "unavailable",
            bool(platform35 and platform35.is_dir()),
            selected,
        ),
        DoctorCheck("Node.js", str(tools.node or "unavailable"), tools.node is not None, selected),
        DoctorCheck(
            "npm or Yarn",
            ", ".join(str(path) for path in (npm, yarn) if path) or "unavailable",
            npm is not None or yarn is not None,
            selected,
        ),
        DoctorCheck(
            "Parent Gradle wrapper",
            str(gradle or "unavailable"),
            gradle is not None,
            selected,
        ),
        DoctorCheck("Android NDK", str(ndk or "unavailable"), ndk is not None, native),
        DoctorCheck(
            "NDK Clang",
            _version(clang) if clang else "unavailable",
            clang is not None and clangxx is not None,
            native,
        ),
        DoctorCheck("C23", "supported" if c23 else "unavailable", c23, native),
        DoctorCheck("C++23", "supported" if cpp23 else "unavailable", cpp23, native),
        DoctorCheck(
            "CMake 3.22.1+",
            ".".join(str(part) for part in cmake_version) if cmake_version else "unavailable",
            cmake_version >= (3, 22, 1),
            native,
        ),
        DoctorCheck(
            "arm64-v8a target",
            "supported" if c23 and cpp23 else "unavailable",
            c23 and cpp23,
            native,
        ),
        DoctorCheck(
            "React Native",
            react_native,
            not react_native.startswith("not installed"),
            selected,
        ),
        DoctorCheck("ADB/device", _adb_status(adb), adb is not None, False),
    ]
    return checks
