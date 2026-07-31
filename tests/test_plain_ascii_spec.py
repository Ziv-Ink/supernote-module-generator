from __future__ import annotations

import io
import json
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
from supernote_module_generator.rendering import (
    ProgressReporter,
    Renderer,
    TerminalCapabilities,
)
from supernote_module_generator.terminal_text import ascii_presentation
from supernote_module_generator.workflows import TYPE_ITEMS


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android").mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n",
        encoding="utf-8",
    )
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
                "--type",
                "native",
                "--description",
                "café — 模块 🙂",
                "--skip-install",
                "--yes",
            ],
        ),
        run_plain(root, ["update", "local-unicode", "--skip-install", "--yes"]),
        run_plain(root, ["validate", "local-unicode"]),
        run_plain(root, ["doctor", "--type", "native"]),
        run_plain(root, ["validate", "missing-module"]),
        run_plain(root, ["remove", "local-unicode", "--skip-install", "--yes"]),
    ]

    expected_codes = [0, 0, 0, None, 2, 0]
    for expected_code, (code, stdout, stderr) in zip(expected_codes, runs):
        if expected_code is not None:
            assert code == expected_code
        assert_ascii(stdout, stderr)


def test_plain_interactive_routes_and_dynamic_values_are_ascii(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = run_plain(
        root,
        ["add", "--skip-install"],
        "3\nlocal-jsi\ncafé — 模块 🙂\n\n\n\n",
    )

    assert code == 0
    assert "Module type:  JSI Module - C/C++" in stderr
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


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("1", "Native Module - Kotlin/Java"),
        ("2", "Native JNI Module - C/C++ via JNI"),
        ("3", "JSI Module - C/C++"),
    ],
)
def test_plain_module_type_labels_use_the_required_ascii_forms(
    choice: str,
    expected: str,
):
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

    selected = Interaction(renderer, line_source=lambda: choice).menu(
        "Module type",
        TYPE_ITEMS,
        default="native",
        collapse_label="Module type",
    )

    assert selected == TYPE_ITEMS[int(choice) - 1].value
    assert f"  {choice}. {expected}\n" in stderr.getvalue()
    assert stderr.getvalue().endswith(f"Module type:  {expected}\n\n")
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
