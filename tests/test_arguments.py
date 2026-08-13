from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from supernote_module_generator.arguments import parse_arguments
from supernote_module_generator.cli import main
from supernote_module_generator.errors import ConfigurationError
from supernote_module_generator.helptext import COMMAND_HELP, ROOT_HELP
from supernote_module_generator import __version__


def invoke(arguments: list[str], cwd: Path):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=cwd)
    return code, stdout.getvalue(), stderr.getvalue()


def test_global_options_are_accepted_before_and_after_subcommand():
    before = parse_arguments(["--json", "add", "local-math", "--yes"])
    after = parse_arguments(["add", "local-math", "--yes", "--json"])
    assert before == after
    assert before.output_mode == "json"


def test_repeatable_starter_values_allow_both_families():
    parsed = parse_arguments(
        ["add", "local-math", "--starter", "cpp", "--starter=kotlin"]
    )
    assert parsed.values_for("starter") == ("cpp", "kotlin")


def test_duplicate_starter_values_are_rejected():
    with pytest.raises(ConfigurationError, match="provided more than once"):
        parse_arguments(["add", "local-math", "--starter", "cpp", "--starter", "cpp"])


@pytest.mark.parametrize("legacy", ["native", "jni", "jsi"])
def test_v1_type_option_is_rejected(legacy: str):
    with pytest.raises(ConfigurationError, match='unknown option "--type"'):
        parse_arguments(["add", "local-math", "--type", legacy])


@pytest.mark.parametrize("legacy", ["--add", "-add", "--remove", "--no-prompt"])
def test_legacy_command_forms_and_options_are_rejected(legacy: str):
    with pytest.raises(ConfigurationError):
        parse_arguments([legacy])


def test_all_and_module_are_mutually_exclusive():
    with pytest.raises(ConfigurationError, match="--all cannot"):
        parse_arguments(["validate", "local-math", "--all"])


def test_remove_build_cleanup_requires_its_explicit_option():
    default = parse_arguments(["remove", "local-math", "--yes"])
    cleanup = parse_arguments(
        ["remove", "local-math", "--delete-build-files", "--yes"]
    )

    assert not default.has("delete_build_files")
    assert cleanup.has("delete_build_files")


def test_output_modes_are_mutually_exclusive():
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        parse_arguments(["doctor", "--quiet", "--json"])


def test_plain_and_no_color_are_valid_json_noops():
    parsed = parse_arguments(["doctor", "--json", "--plain", "--no-color"])
    assert parsed.output_mode == "json"
    assert parsed.plain
    assert parsed.no_color


def test_root_help_is_exact_and_works_outside_plugin(tmp_path: Path):
    code, stdout, stderr = invoke(["--help"], tmp_path)
    assert code == 0
    assert stdout == ROOT_HELP
    assert stderr == ""


@pytest.mark.parametrize("command", ["add", "update", "validate", "remove", "doctor"])
def test_two_command_help_routes_are_byte_identical(tmp_path: Path, command: str):
    first = invoke([command, "--help"], tmp_path)
    second = invoke(["help", command], tmp_path)
    assert first == second == (0, COMMAND_HELP[command], "")


def test_add_help_preserves_multiline_example(tmp_path: Path):
    _, stdout, _ = invoke(["help", "add"], tmp_path)
    assert (
        "  supernote-module add @acme/stylus --starter kotlin \\\n"
        "    --javascript-name Stylus \\\n"
        "    --android-namespace com.acme.stylus \\\n"
        "    --package-manager yarn --yes\n"
    ) in stdout


def test_version_is_exact_and_works_outside_plugin(tmp_path: Path):
    assert invoke(["--version"], tmp_path) == (
        0,
        f"supernote-module {__version__}\n",
        "",
    )


def test_no_command_in_non_tty_is_usage_error(tmp_path: Path):
    code, stdout, stderr = invoke([], tmp_path)
    assert code == 2
    assert stdout == ""
    assert stderr == (
        "error: no command was provided\n\n"
        "Run `supernote-module --help` for usage.\n"
    )


def test_parse_error_in_json_is_one_document_and_empty_stderr(tmp_path: Path):
    code, stdout, stderr = invoke(["--json", "add", "x", "--type", "native"], tmp_path)
    assert code == 2
    assert stderr == ""
    result = json.loads(stdout)
    assert result["status"] == "failure"
    assert result["exit_code"] == 2
    assert result["error"]["kind"] == "usage"


def test_unknown_option_recovery_names_command(tmp_path: Path):
    code, _, stderr = invoke(["add", "--force"], tmp_path)
    assert code == 2
    assert "unknown option \"--force\"" in stderr
    assert "supernote-module add --help" in stderr


def test_unknown_command_recovery_suggests_a_close_command(tmp_path: Path):
    code, _, stderr = invoke(["ad"], tmp_path)

    assert code == 2
    assert 'unknown command "ad"' in stderr
    assert "Did you mean `supernote-module add`?" in stderr
    assert "supernote-module --help" in stderr
