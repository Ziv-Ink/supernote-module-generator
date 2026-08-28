from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.models import (
    CommandResult,
    DoctorCheckResult,
    DoctorResult,
    ErrorInfo,
    WarningInfo,
)
from supernote_module_generator.platform_tools import gradle_wrapper_path
from supernote_module_generator.rendering import (
    ProgressReporter,
    Renderer,
    TerminalCapabilities,
)
from supernote_module_generator.terminal_text import ascii_presentation
from supernote_module_generator.feature_workflows import STARTER_ITEMS


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n",
        encoding="utf-8",
    )
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    gradle = gradle_wrapper_path(tmp_path)
    if os.name == "nt":
        gradle.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        gradle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gradle.chmod(0o755)
    return tmp_path


def run_plain(root: Path, arguments: list[str], stdin: str = "") -> tuple[int, str, str]:
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main(
        ["--plain", *arguments],
        stdin=TtyStringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def assert_ascii(stdout: str, stderr: str) -> None:
    assert stdout.isascii(), repr(stdout)
    assert stderr.isascii(), repr(stderr)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--version"],
        ["add", "--help"],
        ["update", "--help"],
        ["validate", "--help"],
        ["remove", "--help"],
        ["doctor", "--help"],
        ["unknown-command"],
    ],
)
def test_every_plain_cli_entry_screen_is_ascii(
    tmp_path: Path,
    arguments: list[str],
):
    code, stdout, stderr = run_plain(tmp_path, arguments)

    assert code in {0, 2}
    assert_ascii(stdout, stderr)


def test_every_plain_command_route_is_ascii(tmp_path: Path):
    root = plugin(tmp_path)

    runs = [
        run_plain(
            root,
            [
                "add",
                "local-unicode",
                "--starter",
                "cpp",
                "--description",
                "café — 模块 🙂",
                "--skip-install",
                "--yes",
            ],
        ),
        run_plain(root, ["update", "local-unicode", "--skip-install", "--yes"]),
        run_plain(root, ["validate", "local-unicode"], "n\n"),
        run_plain(root, ["doctor"]),
        run_plain(root, ["validate", "missing-module"]),
        run_plain(root, ["remove", "local-unicode", "--skip-install", "--yes"]),
    ]

    expected_codes = [0, 0, 1, None, 2, 0]
    for expected_code, (code, stdout, stderr) in zip(expected_codes, runs):
        if expected_code is not None:
            assert code == expected_code
        assert_ascii(stdout, stderr)


def test_plain_interactive_routes_and_dynamic_values_are_ascii(
    tmp_path: Path, stub_ksp_frontend
):
    root = plugin(tmp_path)
    code, stdout, stderr = run_plain(
        root,
        ["add", "--skip-install"],
        "1,2\nlocal-mixed\ncafé — 模块 🙂\n\n\n\n",
    )

    assert code == 0
    assert "Starter code:  C/C++ (native), Kotlin/Java (JVM)" in stderr
    assert "\\xe9" in stderr
    assert_ascii(stdout, stderr)


def test_plain_combined_with_json_remains_ascii_and_keeps_duration_ms(tmp_path: Path):
    root = plugin(tmp_path)

    code, stdout, stderr = run_plain(root, ["--json", "validate"])

    payload = json.loads(stdout)
    assert code == 0
    assert stderr == ""
    assert isinstance(payload["duration_ms"], int)
    assert_ascii(stdout, stderr)


def test_plain_starter_labels_allow_multiple_language_families():
    stdout = io.StringIO()
    stderr = io.StringIO()
    # The mode-level contract wins even if a caller reports full cursor and
    # Unicode capabilities.
    capabilities = TerminalCapabilities(True, True, True, True, 80, 24)
    renderer = Renderer(
        "human",
        capabilities,
        stdout=stdout,
        stderr=stderr,
        plain=True,
    )

    from supernote_module_generator.interaction import Interaction

    selected = Interaction(renderer, line_source=lambda: "1,2").multi_menu(
        "Starter code",
        STARTER_ITEMS,
        defaults=("cpp",),
        collapse_label="Starter code",
    )

    assert selected == ["cpp", "kotlin"]
    assert "1. [x] C/C++ (native)" in stderr.getvalue()
    assert "2. [ ] Kotlin/Java (JVM)" in stderr.getvalue()
    assert stderr.getvalue().endswith(
        "Starter code:  C/C++ (native), Kotlin/Java (JVM)\n\n"
    )
    assert_ascii(stdout.getvalue(), stderr.getvalue())


def test_plain_ascii_conversion_replaces_em_dash_and_escapes_other_unicode():
    assert ascii_presentation("Café — ↑/↓ 模块 🙂") == (
        "Caf\\xe9 - ^/v \\u6a21\\u5757 \\U0001f642"
    )


def test_plain_renderer_guards_success_failure_warning_doctor_and_progress():
    stdout = io.StringIO()
    stderr = io.StringIO()
    capabilities = TerminalCapabilities(False, False, False, False, 80, 24)
    renderer = Renderer(
        "human",
        capabilities,
        stdout=stdout,
        stderr=stderr,
        plain=True,
    )

    renderer.warning(WarningInfo("warning", "Café — 模块 🙂", "test"))
    with ProgressReporter(renderer).phase("Installing — 模块", "Installed — 模块"):
        pass
    renderer.render(
        CommandResult(
            "doctor",
            doctor=DoctorResult(
                "all",
                True,
                0,
                0,
                [
                    DoctorCheckResult(
                        "project",
                        "Prøject",
                        "required",
                        "passed",
                        None,
                        "/模块",
                        "Café — ready 🙂",
                    )
                ],
            ),
        )
    )
    renderer.render(
        CommandResult(
            "add",
            status="failure",
            exit_code=1,
            error=ErrorInfo("failure", "apply", "Café — failed 🙂"),
        )
    )

    assert "Doctor - All" in stdout.getvalue()
    assert_ascii(stdout.getvalue(), stderr.getvalue())
