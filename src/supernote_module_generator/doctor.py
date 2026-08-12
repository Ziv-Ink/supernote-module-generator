"""Executable Doctor probes for module-generation and generated-build inputs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .models import CommandResult, DoctorCheckResult, DoctorResult, ErrorInfo
from .project import manager_evidence, resolve_plugin_root
from .rendering import ProgressReporter, Renderer
from .subprocesses import run_process


def _version_tuple(value: Optional[str]) -> Tuple[int, ...]:
    if not value:
        return ()
    match = re.search(r"(?<![A-Za-z])(\d+(?:\.\d+)+|\d+)(?![A-Za-z])", value)
    if not match:
        return ()
    parts = tuple(int(part) for part in match.group(1).split("."))
    if len(parts) > 1 and parts[0] == 1:
        return (parts[1], *parts[2:])
    return parts


class DoctorService:
    def __init__(
        self,
        cwd: Path,
        renderer: Renderer,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.cwd = cwd.expanduser().resolve()
        self.renderer = renderer
        self.progress = ProgressReporter(renderer)
        self.run = run

    def _verbose_stream(self, destination: str, content: str) -> None:
        target = self.renderer.stdout if destination == "stdout" else self.renderer.stderr
        target.write(content)
        target.flush()

    def execute(self, scope: str) -> CommandResult:
        checks: List[DoctorCheckResult] = []
        with self.progress.phase("Checking project", "Checked project"):
            try:
                root = resolve_plugin_root(self.cwd)
            except Exception:
                root = self.cwd
                checks.append(
                    DoctorCheckResult(
                        "project",
                        "Project",
                        "required",
                        "failed",
                        None,
                        str(self.cwd),
                        "The current directory is not a Supernote plugin.",
                    )
                )
                valid_root = False
            else:
                checks.append(
                    DoctorCheckResult(
                        "project",
                        "Project",
                        "required",
                        "passed",
                        None,
                        str(root),
                        "Plugin root and package metadata are available.",
                    )
                )
                valid_root = True

        with self.progress.phase("Checking JavaScript tools", "Checked JavaScript tools"):
            checks.extend(self._javascript_checks(root, valid_root))
        with self.progress.phase("Checking Android tools", "Checked Android tools"):
            checks.extend(self._android_checks(root, valid_root))
        if scope == "plugin":
            with self.progress.phase("Checking native tools", "Checked native tools"):
                checks.extend(self._native_checks())
        if scope == "plugin":
            with self.progress.phase("Checking JSI runtime", "Checked JSI runtime"):
                checks.extend(self._jsi_runtime_checks())

        required_failed = [
            check
            for check in checks
            if check.requirement == "required" and check.status == "failed"
        ]
        advisories = [
            check
            for check in checks
            if check.requirement == "advisory" and check.status == "warning"
        ]
        doctor = DoctorResult(
            scope,
            not required_failed,
            len(required_failed),
            len(advisories),
            checks,
        )
        if required_failed:
            count = len(required_failed)
            return CommandResult(
                "doctor",
                status="failure",
                exit_code=1,
                doctor=doctor,
                error=ErrorInfo(
                    "doctor_failed",
                    "doctor",
                    f"Doctor found {count} required issue{'s' if count != 1 else ''}.",
                ),
                metadata={
                    "phase_label": "Doctor",
                    "next_action": "Install the missing tools, then rerun `supernote-module doctor`.",
                },
            )
        return CommandResult("doctor", doctor=doctor)

    def _probe(self, command: Sequence[str], timeout: int = 10) -> Tuple[bool, Optional[str], str]:
        try:
            if self.renderer.mode == "verbose" and self.run is subprocess.run:
                result = run_process(
                    command,
                    cwd=self.cwd,
                    timeout=timeout,
                    stream=self._verbose_stream,
                )
            else:
                result = self.run(
                    list(command),
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, None, str(exc)
        output = (result.stdout or result.stderr).strip()
        first = output.splitlines()[0] if output else ""
        return result.returncode == 0, first or None, output

    def _tool_check(
        self,
        identifier: str,
        label: str,
        command: str,
        *,
        required: bool = True,
    ) -> DoctorCheckResult:
        path = shutil.which(command)
        requirement = "required" if required else "advisory"
        if path is None:
            return DoctorCheckResult(
                identifier,
                label,
                requirement,
                "failed" if required else "warning",
                None,
                None,
                f"{label} was not found.",
            )
        passed, version, _ = self._probe([path, "--version"])
        return DoctorCheckResult(
            identifier,
            label,
            requirement,
            "passed" if passed else ("failed" if required else "warning"),
            version,
            path,
            f"{label} is available." if passed else f"{label} returned a nonzero status.",
        )

    def _javascript_checks(self, root: Path, valid_root: bool) -> List[DoctorCheckResult]:
        checks = [self._tool_check("node", "Node.js", "node")]
        if not valid_root:
            checks.append(
                DoctorCheckResult(
                    "package_manager",
                    "npm or Yarn",
                    "required",
                    "failed",
                    None,
                    None,
                    "Package-manager selection is unavailable outside a plugin root.",
                )
            )
            return checks
        evidence = manager_evidence(root)
        if evidence.conflicting:
            npm = self._tool_check("npm", "npm", "npm", required=False)
            yarn = self._tool_check("yarn", "Yarn", "yarn", required=False)
            checks.extend([npm, yarn])
            healthy = any(check.status == "passed" for check in (npm, yarn))
            checks.append(
                DoctorCheckResult(
                    "package_manager_health",
                    "Package manager",
                    "required",
                    "passed" if healthy else "failed",
                    None,
                    None,
                    "At least one package manager is healthy."
                    if healthy
                    else "Neither npm nor Yarn is healthy.",
                )
            )
            checks.append(
                DoctorCheckResult(
                    "package_manager",
                    "Package manager",
                    "advisory",
                    "warning",
                    None,
                    None,
                    "Both package-lock.json and yarn.lock were found; lifecycle commands require an explicit manager."
                    if healthy
                    else "Both lockfiles exist and neither package manager is healthy.",
                )
            )
            return checks
        manager = evidence.sole or "npm"
        checks.append(self._tool_check("package_manager", "Yarn" if manager == "yarn" else "npm", manager))
        return checks

    def _android_checks(self, root: Path, valid_root: bool) -> List[DoctorCheckResult]:
        java_check = self._tool_check("java", "Java", "java")
        if java_check.status == "passed" and _version_tuple(java_check.detected_version) < (17,):
            java_check = DoctorCheckResult(
                "java",
                "Java",
                "required",
                "failed",
                java_check.detected_version,
                java_check.path,
                "Java 17 or newer is required.",
            )
        sdk_value = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        sdk = Path(sdk_value).expanduser().resolve() if sdk_value else None
        sdk_platform = sdk / "platforms/android-35/android.jar" if sdk else None
        sdk_ok = bool(sdk_platform and sdk_platform.is_file())
        sdk_check = DoctorCheckResult(
            "android_sdk",
            "Android SDK",
            "required",
            "passed" if sdk_ok else "failed",
            None,
            str(sdk) if sdk else None,
            "Android SDK platform 35 is available."
            if sdk_ok
            else "ANDROID_HOME or ANDROID_SDK_ROOT does not identify an SDK with platform 35.",
        )
        if valid_root:
            gradle = root / "android" / ("gradlew.bat" if os.name == "nt" else "gradlew")
            if gradle.is_file():
                command = [str(gradle), "--version"] if os.access(gradle, os.X_OK) else ["sh", str(gradle), "--version"]
                passed, version, _ = self._probe(command, timeout=120)
            else:
                passed, version = False, None
            gradle_check = DoctorCheckResult(
                "gradle_wrapper",
                "Gradle wrapper",
                "required",
                "passed" if passed else "failed",
                version,
                str(gradle) if gradle.is_file() else None,
                "Gradle wrapper executed successfully." if passed else "The project Gradle wrapper could not be executed.",
            )
        else:
            gradle_check = DoctorCheckResult(
                "gradle_wrapper",
                "Gradle wrapper",
                "required",
                "failed",
                None,
                None,
                "The project Gradle wrapper is unavailable outside a plugin root.",
            )
        return [java_check, sdk_check, gradle_check]

    def _native_checks(self) -> List[DoctorCheckResult]:
        cmake = self._tool_check("cmake", "CMake", "cmake")
        if cmake.status == "passed" and _version_tuple(cmake.detected_version) < (3, 22, 1):
            cmake = DoctorCheckResult(
                "cmake",
                "CMake",
                "required",
                "failed",
                cmake.detected_version,
                cmake.path,
                "CMake 3.22.1 or newer is required.",
            )
        sdk_value = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        ndk_env = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT")
        candidates: List[Path] = []
        if ndk_env:
            candidates.append(Path(ndk_env).expanduser())
        if sdk_value:
            ndk_root = Path(sdk_value).expanduser() / "ndk"
            if ndk_root.is_dir():
                candidates.extend(path for path in ndk_root.iterdir() if path.is_dir())
        ndk = next(
            (
                path.resolve()
                for path in sorted(
                    candidates,
                    key=lambda item: _version_tuple(item.name),
                    reverse=True,
                )
                if path.is_dir()
            ),
            None,
        )
        clang = None
        detected_version = None
        ndk_healthy = False
        if ndk is not None:
            properties = ndk / "source.properties"
            if properties.is_file():
                match = re.search(
                    r"^Pkg\.Revision\s*=\s*(.+)$",
                    properties.read_text(encoding="utf-8", errors="replace"),
                    flags=re.MULTILINE,
                )
                detected_version = match.group(1).strip() if match else ndk.name
            prebuilt = ndk / "toolchains/llvm/prebuilt"
            clang = next(iter(sorted(prebuilt.glob("*/bin/clang"))), None) if prebuilt.is_dir() else None
            if clang is not None:
                clangxx = clang.with_name("clang++")
                clang_ok, _, _ = self._probe([str(clang), "--version"])
                c23_ok, _, _ = self._probe(
                    [
                        str(clang),
                        "--target=aarch64-linux-android27",
                        "-std=c23",
                        "-fsyntax-only",
                        "-x",
                        "c",
                        os.devnull,
                    ]
                )
                cpp23_ok = False
                if clangxx.is_file():
                    cpp23_ok, _, _ = self._probe(
                        [
                            str(clangxx),
                            "--target=aarch64-linux-android27",
                            "-std=c++23",
                            "-fsyntax-only",
                            "-x",
                            "c++",
                            os.devnull,
                        ]
                    )
                ndk_healthy = clang_ok and c23_ok and cpp23_ok
        ndk_check = DoctorCheckResult(
            "android_ndk",
            "Android NDK",
            "required",
            "passed" if ndk_healthy else "failed",
            detected_version,
            str(ndk) if ndk else None,
            "Android NDK compiler probes passed for C23 and C++23."
            if ndk_healthy
            else "The required Android NDK was not found or its compiler probe failed.",
        )
        return [cmake, ndk_check]

    def _jsi_runtime_checks(self) -> List[DoctorCheckResult]:
        return [
            DoctorCheckResult(
                "selinux_policy",
                "JSI execution policy",
                "advisory",
                "warning",
                None,
                None,
                "Target PluginHost and SELinux execution policy were not inspected; generated JSI files do not prove runtime execution.",
            )
        ]
