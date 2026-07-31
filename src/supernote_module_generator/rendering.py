"""Thin human, quiet, JSON, and progress presentation layers."""
from __future__ import annotations

import json
import locale
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Callable, Iterator, Optional

from .models import CommandResult, ErrorInfo, WarningInfo


@dataclass(frozen=True)
class TerminalCapabilities:
    interactive: bool
    cursor: bool
    color: bool
    unicode: bool
    columns: int
    lines: int

    @classmethod
    def detect(
        cls,
        *,
        stdin: IO[str],
        stdout: IO[str],
        stderr: IO[str],
        plain: bool,
        no_color: bool,
    ) -> "TerminalCapabilities":
        interactive = bool(
            getattr(stdin, "isatty", lambda: False)()
            and getattr(stdout, "isatty", lambda: False)()
        )
        term = os.environ.get("TERM", "")
        encoding = (
            getattr(stderr, "encoding", None)
            or locale.getpreferredencoding(False)
            or ""
        )
        unicode_safe = "utf" in encoding.lower()
        size = shutil.get_terminal_size((80, 24))
        raw_input_safe = False
        if interactive:
            try:
                raw_input_safe = os.isatty(stdin.fileno())
            except (AttributeError, OSError, ValueError):
                raw_input_safe = False
        cursor = bool(
            interactive
            and raw_input_safe
            and getattr(stderr, "isatty", lambda: False)()
            and not plain
            and term.lower() != "dumb"
        )
        return cls(
            interactive=interactive,
            cursor=cursor,
            color=(
                cursor
                and not no_color
                and "NO_COLOR" not in os.environ
                and getattr(stderr, "isatty", lambda: False)()
            ),
            unicode=unicode_safe and not plain,
            columns=size.columns,
            lines=size.lines,
        )


class Renderer:
    def __init__(
        self,
        mode: str,
        capabilities: TerminalCapabilities,
        *,
        stdout: IO[str] = sys.stdout,
        stderr: IO[str] = sys.stderr,
        debug: bool = False,
    ) -> None:
        self.mode = mode
        self.capabilities = capabilities
        self.stdout = stdout
        self.stderr = stderr
        self.debug = debug
        self.pending_warnings: list[WarningInfo] = []
        self.progress_emitted = False

    def style(self, role: str, text: str) -> str:
        if not self.capabilities.color:
            return text
        codes = {
            "heading": "36;1",
            "active": "36",
            "success": "32;1",
            "warning": "33",
            "error": "31;1",
            "bold": "1",
            "dim": "2",
        }
        code = codes.get(role)
        return f"\033[{code}m{text}\033[0m" if code else text

    @property
    def symbols(self) -> dict[str, str]:
        if self.capabilities.unicode:
            return {"success": "✓", "failure": "×", "warning": "!", "active": "›"}
        return {"success": "[OK]", "failure": "[X]", "warning": "[!]", "active": ">"}

    def warning(self, warning: WarningInfo) -> None:
        if self.mode == "json":
            return
        symbol = self.symbols["warning"]
        print(self.style("warning", f"{symbol} {warning.message}"), file=self.stderr)
        if warning.recovery:
            print(f"  {warning.recovery}", file=self.stderr)

    def render(self, result: CommandResult) -> None:
        if self.mode == "json":
            json.dump(result.to_dict(debug=self.debug), self.stdout, indent=2)
            self.stdout.write("\n")
            self.stdout.flush()
            return
        if self.progress_emitted:
            print(file=self.stderr)
            self.progress_emitted = False
        for warning in result.warnings:
            self.warning(warning)
        if result.status == "success":
            self._render_success(result)
        elif result.status == "cancelled":
            print(
                result.metadata.get(
                    "cancellation_message",
                    f"{result.command.capitalize()} cancelled.",
                ),
                file=self.stdout,
            )
        else:
            self._render_failure(result)

    def _success_line(self, result: CommandResult) -> str:
        command = result.command
        module = result.module
        built = bool(result.metadata.get("built"))
        if command == "add" and module:
            return (
                f'Added and built module "{module.package_name}"'
                if built
                else f'Added module "{module.package_name}"'
            )
        if command == "update" and module:
            return (
                f'Updated and built module "{module.package_name}"'
                if built
                else f'Updated module "{module.package_name}"'
            )
        if command == "validate":
            if module:
                return (
                    f'Module "{module.package_name}" is valid and builds successfully'
                    if built
                    else f'Module "{module.package_name}" is valid'
                )
            count = len(result.modules)
            return (
                f"All {count} modules are valid and build successfully"
                if built
                else f"All {count} modules are valid"
            )
        if command == "remove":
            if module:
                return f'Removed module "{module.package_name}"'
            return f"Removed {result.metadata.get('removed_count', len(result.modules))} modules"
        if command == "doctor":
            return "Doctor found no required issues"
        return str(result.metadata.get("success_message", "Success"))

    def _render_success(self, result: CommandResult) -> None:
        if result.command == "doctor" and result.doctor is not None and self.mode != "quiet":
            self._render_doctor_checks(result)
        elif result.command == "doctor" and result.doctor is not None:
            for check in result.doctor.checks:
                if check.requirement == "advisory" and check.status == "warning":
                    print(
                        f"{self.symbols['warning']} {check.label}: {check.message}",
                        file=self.stderr,
                    )
        line = self._success_line(result)
        if self.mode == "quiet":
            print(line, file=self.stdout)
            return
        symbol = self.symbols["success"]
        print(self.style("success", f"{symbol} {line}"), file=self.stdout)
        if result.command == "add" and result.module:
            print(f"  Path: {result.module.path}", file=self.stdout)
        dependency = result.dependency
        if dependency is not None and dependency.status in {"installed", "refreshed"}:
            verb = "installed" if dependency.status == "installed" else "refreshed"
            print(f"  Dependency: {verb} with {dependency.manager}", file=self.stdout)
        next_action = result.metadata.get("next_action")
        if next_action:
            print(f"  Next: {next_action}", file=self.stdout)

    def _render_failure(self, result: CommandResult) -> None:
        error = result.error
        if error is None:
            print("× Internal error", file=self.stderr)
            return
        if result.exit_code == 2:
            print(f"error: {error.message}", file=self.stderr)
            recovery_text = result.metadata.get("recovery_text")
            if recovery_text:
                print(f"\n{recovery_text}", file=self.stderr)
            return
        if result.command == "doctor" and result.doctor is not None:
            self._render_doctor_checks(result)
            count = result.doctor.required_issue_count
            print(
                self.style(
                    "error",
                    f"{self.symbols['failure']} Doctor found {count} required issue{'s' if count != 1 else ''}",
                ),
                file=self.stderr,
            )
            for check in result.doctor.checks:
                if check.requirement == "required" and check.status == "failed":
                    print(f"\n  {check.label:<12}{check.message}", file=self.stderr)
            if result.metadata.get("next_action"):
                print(f"\n  Next: {result.metadata['next_action']}", file=self.stderr)
            return
        if result.command == "validate" and result.validation is not None:
            self._render_validation_failure(result, error)
            return
        if error.kind == "internal":
            print(
                self.style("error", f"{self.symbols['failure']} Internal error"),
                file=self.stderr,
            )
            print(f"\n  {error.message}", file=self.stderr)
            rollback = result.rollback.status.replace("_", " ").title()
            print(f"\n  Rollback: {rollback}", file=self.stderr)
            if result.metadata.get("next_action"):
                print(f"  Next:     {result.metadata['next_action']}", file=self.stderr)
            self._render_debug(error)
            return
        label = str(result.metadata.get("phase_label", error.phase.replace("_", " ").capitalize()))
        print(
            self.style("error", f"{self.symbols['failure']} {label} failed"),
            file=self.stderr,
        )
        print(f"\n  {error.message}", file=self.stderr)
        if error.subprocess is not None:
            for line in error.subprocess.relevant_lines[:8]:
                print(f"\n    {line}", file=self.stderr)
            if len(error.subprocess.relevant_lines) > 8:
                print(
                    "\n  Additional output omitted. Rerun with --verbose for complete subprocess output.",
                    file=self.stderr,
                )
        rollback = result.rollback.status.replace("_", " ").title()
        print(f"\n  Rollback: {rollback}", file=self.stderr)
        if result.recovery is not None:
            command = " ".join(result.recovery.command)
            print(f"  Next:     {command}", file=self.stderr)
        elif result.metadata.get("next_action"):
            print(f"  Next:     {result.metadata['next_action']}", file=self.stderr)
        self._render_debug(error)

    def _render_validation_failure(
        self,
        result: CommandResult,
        error: ErrorInfo,
    ) -> None:
        if result.modules:
            failed = [
                module
                for module in result.modules
                if module.validation is not None
                and "failed"
                in {
                    module.validation.structural,
                    module.validation.integration,
                    module.validation.dependency_link,
                    module.validation.build,
                }
            ]
            print(
                self.style(
                    "error",
                    f"{self.symbols['failure']} Validation failed for {len(failed)} of {len(result.modules)} modules",
                ),
                file=self.stderr,
            )
            width = max((len(module.package_name) for module in failed), default=0)
            for module in failed:
                validation = module.validation
                assert validation is not None
                message = (
                    str(validation.issues[0].get("message"))
                    if validation.issues
                    else "Android build failed"
                )
                print(f"\n  {module.package_name:<{width}}  {message}", file=self.stderr)
        else:
            module_name = result.module.package_name if result.module is not None else "module"
            issues = result.validation.issues
            kinds = {str(issue.get("kind")) for issue in issues}
            missing: list[str] = []
            if result.validation.build == "failed" and not (kinds - {"build"}):
                label = "Building Android"
                message = f'Gradle could not build module "{module_name}".'
            elif kinds and kinds <= {
                "parent_dependency",
                "gradle_integration",
                "parent_integration",
                "dependency_link",
            }:
                label = "Checking integration"
                message = str(issues[0].get("message"))
            else:
                label = "Checking module"
                missing = [
                    str(issue.get("path"))
                    for issue in issues
                    if issue.get("kind") == "missing_generated_file"
                ]
                if missing:
                    count = len(missing)
                    message = (
                        f'Module "{module_name}" is missing {count} generated '
                        f'file{"s" if count != 1 else ""}.'
                    )
                else:
                    message = str(
                        issues[0].get("message")
                        if issues
                        else error.message
                    )
            print(
                self.style("error", f"{self.symbols['failure']} {label} failed"),
                file=self.stderr,
            )
            print(f"\n  {message}", file=self.stderr)
            if missing:
                print("\n  Missing:", file=self.stderr)
                for path in missing:
                    print(f"    {path}", file=self.stderr)
        if error.subprocess is not None:
            for line in error.subprocess.relevant_lines[:8]:
                print(f"\n    {line}", file=self.stderr)
        rollback = result.rollback.status.replace("_", " ").title()
        print(f"\n  Rollback: {rollback}", file=self.stderr)
        if result.metadata.get("next_action"):
            print(f"  Next:     {result.metadata['next_action']}", file=self.stderr)
        self._render_debug(error)

    def _render_debug(self, error: object) -> None:
        if not self.debug or not isinstance(error, ErrorInfo) or error.internal is None:
            return
        transaction_id = error.internal.get("transaction_id")
        if transaction_id:
            print(f"\n  Transaction: {transaction_id}", file=self.stderr)
        traceback_text = error.internal.get("traceback")
        if traceback_text:
            print("\n  Traceback:", file=self.stderr)
            print(str(traceback_text).rstrip(), file=self.stderr)

    def _render_doctor_checks(self, result: CommandResult) -> None:
        assert result.doctor is not None
        if self.mode == "quiet":
            return
        scope = {
            "all": "All",
            "native": "Native Module",
            "jni": "Native JNI Module",
            "jsi": "JSI Module",
        }.get(result.doctor.scope, result.doctor.scope)
        print(f"Supernote Module Generator\n\nDoctor — {scope}\n", file=self.stdout)
        groups = (
            (
                "Project",
                ("project",),
                "Plugin root and package metadata",
            ),
            (
                "JavaScript",
                ("node", "npm", "yarn", "package_manager", "package_manager_health"),
                "Node.js, npm, or Yarn",
            ),
            (
                "Android",
                ("java", "android_sdk", "gradle_wrapper"),
                "Java, Android SDK, Gradle wrapper",
            ),
            (
                "Native",
                ("cmake", "android_ndk"),
                "CMake and Android NDK",
            ),
            (
                "Deployment",
                ("adb", "adb_device", "selinux_policy"),
                "adb, connected device, and SELinux policy",
            ),
        )
        by_id = {check.id: check for check in result.doctor.checks}
        for label, identifiers, success_message in groups:
            checks = [by_id[identifier] for identifier in identifiers if identifier in by_id]
            if not checks:
                continue
            failed_checks = [
                check
                for check in checks
                if check.requirement == "required" and check.status == "failed"
            ]
            warning_checks = [check for check in checks if check.status == "warning"]
            if failed_checks:
                status = "failed"
                message = failed_checks[0].message
            elif warning_checks:
                status = "warning"
                if label == "Deployment":
                    adb = by_id.get("adb")
                    device = by_id.get("adb_device")
                    parts = [
                        "adb found" if adb is not None and adb.status == "passed" else "adb not found",
                        (
                            "device connected"
                            if device is not None and device.status == "passed"
                            else "no device connected"
                        ),
                        "SELinux policy not inspected",
                    ]
                    message = "; ".join(parts)
                else:
                    message = warning_checks[0].message
                status = "warning"
            else:
                status = "passed"
                message = success_message
            if status == "passed":
                symbol = self.symbols["success"]
                role = "success"
            elif status == "failed":
                symbol = self.symbols["failure"]
                role = "error"
            elif status == "warning":
                symbol = self.symbols["warning"]
                role = "warning"
            else:
                symbol = "—" if self.capabilities.unicode else "[-]"
                role = "dim"
            print(self.style(role, f"{symbol} {label:<12}{message}"), file=self.stdout)
            for check in failed_checks:
                if check.path:
                    print(f"  Path: {check.path}", file=self.stdout)
                if check.detected_version:
                    print(f"  Detected: {check.detected_version}", file=self.stdout)
        print(file=self.stdout)


class ProgressReporter:
    """Phase events with delayed animation and deterministic plain output."""

    SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, renderer: Renderer) -> None:
        self.renderer = renderer

    @contextmanager
    def phase(
        self,
        active: str,
        completed: str,
        failed: Optional[str] = None,
    ) -> Iterator["PhaseHandle"]:
        mode = self.renderer.mode
        handle = PhaseHandle()
        if mode in {"quiet", "json"}:
            yield handle
            return
        capable = self.renderer.capabilities.cursor
        plain = not capable
        started = time.monotonic()
        stopped = threading.Event()
        shown = threading.Event()

        if plain:
            self.renderer.progress_emitted = True
            print(f"... {active}", file=self.renderer.stderr, flush=True)
            try:
                yield handle
            except BaseException:
                raise
            else:
                symbol = (
                    self.renderer.symbols["failure"]
                    if handle.failed
                    else self.renderer.symbols["success"]
                )
                print(
                    f"{symbol} {failed if handle.failed and failed else completed}",
                    file=self.renderer.stderr,
                )
            return

        def animate() -> None:
            if stopped.wait(0.25):
                return
            shown.set()
            self.renderer.progress_emitted = True
            index = 0
            frames = (
                self.SPINNER
                if self.renderer.capabilities.unicode
                else ("|", "/", "-", "\\")
            )
            while not stopped.is_set():
                elapsed = time.monotonic() - started
                suffix = f"  {_elapsed(elapsed)}" if elapsed >= 1.0 else ""
                frame = frames[index % len(frames)]
                line = f"{frame} {active}{suffix}"
                print(
                    "\r\033[2K" + self.renderer.style("active", line),
                    end="",
                    file=self.renderer.stderr,
                    flush=True,
                )
                index += 1
                stopped.wait(0.08)

        thread = threading.Thread(target=animate, daemon=True)
        thread.start()
        try:
            yield handle
        finally:
            stopped.set()
            thread.join(timeout=0.5)
        elapsed = time.monotonic() - started
        if shown.is_set():
            print("\r\033[2K", end="", file=self.renderer.stderr)
        else:
            self.renderer.progress_emitted = True
        print(
            self.renderer.style(
                "error" if handle.failed else "success",
                (
                    f"{self.renderer.symbols['failure']} {failed}"
                    if handle.failed and failed
                    else f"{self.renderer.symbols['success']} {completed}"
                ),
            ),
            file=self.renderer.stderr,
        )


@dataclass
class PhaseHandle:
    failed: bool = False

    def fail(self) -> None:
        self.failed = True


def _elapsed(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
