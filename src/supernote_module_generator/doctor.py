"""Executable Doctor probes for module-generation and generated-build inputs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, ContextManager, List, Optional, Sequence, Tuple

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


def _gradle_version(output: str) -> Optional[str]:
    match = re.search(r"^Gradle\s+([^\s]+)", output, flags=re.MULTILINE)
    return match.group(1) if match else None


def _gradle_jvm_lines(output: str) -> Tuple[Optional[str], Optional[str]]:
    """Return an effective version and daemon Java home when Gradle reports them."""
    legacy = re.search(r"^JVM:\s*([^\s]+)", output, flags=re.MULTILINE)
    launcher = re.search(r"^Launcher JVM:\s*([^\s]+)", output, flags=re.MULTILINE)
    daemon = re.search(r"^Daemon JVM:\s*(.+)$", output, flags=re.MULTILINE)
    daemon_home = None
    if daemon:
        value = daemon.group(1).strip()
        value = re.sub(r"\s+\((?:from|no JDK specified).*$", "", value)
        if value and (
            value.startswith(("/", "~", "."))
            or re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            daemon_home = value
    return (
        legacy.group(1) if legacy else launcher.group(1) if launcher else None,
        daemon_home,
    )


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

    def _phase(self, active: str, completed: str) -> ContextManager[object]:
        # Plain output is commonly redirected or read linearly. The final Doctor
        # table already reports every check, so static progress lines only repeat
        # the same information and make the report harder to scan.
        if self.renderer.plain:
            return nullcontext()
        return self.progress.phase(active, completed)

    def _verbose_stream(self, destination: str, content: str) -> None:
        target = self.renderer.stdout if destination == "stdout" else self.renderer.stderr
        target.write(content)
        target.flush()

    def execute(self, scope: str) -> CommandResult:
        checks: List[DoctorCheckResult] = []
        with self._phase("Checking project", "Checked project"):
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

        with self._phase("Checking JavaScript tools", "Checked JavaScript tools"):
            checks.extend(self._javascript_checks(root, valid_root))
        with self._phase("Checking Android tools", "Checked Android tools"):
            checks.extend(self._android_checks(root, valid_root))
        if scope == "plugin":
            with self._phase("Checking native tools", "Checked native tools"):
                checks.extend(self._native_checks())
        if scope == "plugin":
            with self._phase("Checking JSI runtime", "Checked JSI runtime"):
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
                    "next_action": self._required_issue_next_action(required_failed),
                },
            )
        return CommandResult("doctor", doctor=doctor)

    @staticmethod
    def _required_issue_next_action(
        failed: Sequence[DoctorCheckResult],
    ) -> str:
        failed_ids = {check.id for check in failed}
        if failed_ids in ({"gradle_wrapper"}, {"gradle_wrapper", "gradle_jvm"}):
            wrapper = next(check for check in failed if check.id == "gradle_wrapper")
            if wrapper.path is None:
                return (
                    "Restore `android/gradlew`, make it executable, then rerun "
                    "`supernote-module doctor`."
                )
            return (
                "Fix `android/gradlew` so it executes successfully, then rerun "
                "`supernote-module doctor`."
            )
        return (
            "Resolve the required checks listed above, then rerun "
            "`supernote-module doctor`."
        )

    def _probe(self, command: Sequence[str], timeout: int = 10) -> Tuple[bool, Optional[str], str]:
        try:
            if self.run is subprocess.run:
                result = run_process(
                    command,
                    cwd=self.cwd,
                    timeout=timeout,
                    stream=(
                        self._verbose_stream
                        if self.renderer.mode == "verbose"
                        else None
                    ),
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
                passed, _, gradle_output = self._probe(command, timeout=120)
                version = _gradle_version(gradle_output)
            else:
                passed, version, gradle_output = False, None, ""
            gradle_exists = gradle.is_file()
            gradle_check = DoctorCheckResult(
                "gradle_wrapper",
                "Gradle wrapper",
                "required",
                "passed" if passed else "failed",
                version,
                str(gradle) if gradle_exists else None,
                (
                    "Gradle wrapper executed successfully."
                    if passed
                    else "The project Gradle wrapper could not be executed."
                    if gradle_exists
                    else "The project Gradle wrapper is missing."
                ),
            )
            gradle_jvm_check = self._gradle_jvm_check(
                gradle_output,
                wrapper_passed=passed,
                shell_java=java_check,
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
            gradle_jvm_check = DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                None,
                "The Gradle JVM is unavailable outside a plugin root.",
            )
        return [java_check, sdk_check, gradle_check, gradle_jvm_check]

    def _gradle_jvm_check(
        self,
        output: str,
        *,
        wrapper_passed: bool,
        shell_java: DoctorCheckResult,
    ) -> DoctorCheckResult:
        if not wrapper_passed:
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                None,
                "The Gradle JVM could not be inspected because the wrapper failed.",
            )
        reported_version, daemon_home = _gradle_jvm_lines(output)
        detected = reported_version
        path = daemon_home
        if daemon_home:
            executable = Path(daemon_home).expanduser() / "bin" / (
                "java.exe" if os.name == "nt" else "java"
            )
            passed, detected, _ = self._probe([str(executable), "--version"])
            if not passed:
                return DoctorCheckResult(
                    "gradle_jvm",
                    "Gradle JVM",
                    "required",
                    "failed",
                    detected,
                    str(executable),
                    "Gradle reported a daemon JVM that could not be executed.",
                )
            path = str(executable)
        if not detected:
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                None,
                shell_java.path,
                "Gradle did not report the JVM that will run the Android build.",
            )
        if _version_tuple(detected) < (17,):
            return DoctorCheckResult(
                "gradle_jvm",
                "Gradle JVM",
                "required",
                "failed",
                detected,
                path,
                "The effective Gradle JVM is older than Java 17; check "
                "JAVA_HOME and org.gradle.java.home.",
            )
        return DoctorCheckResult(
            "gradle_jvm",
            "Gradle JVM",
            "required",
            "passed",
            detected,
            path,
            "The effective Gradle JVM is Java 17 or newer.",
        )

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
