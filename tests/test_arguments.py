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


def test_repeated_identical_single_value_option_is_idempotent():
    parsed = parse_arguments(
        ["add", "local-math", "--description=same", "--description", "same"]
    )

    assert parsed.value("description") == "same"


def test_conflicting_single_value_option_is_rejected():
    with pytest.raises(ConfigurationError, match="conflicting values"):
        parse_arguments(
            ["add", "local-math", "--description=one", "--description=two"]
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["add", "local-math", "--description"], "--description requires a value"),
        (
            ["add", "local-math", "--description", "--yes"],
            "--description requires a value",
        ),
        (["add", "local-math", "--starter=rust"], 'invalid starter family "rust"'),
        (["update", "--package-manager=pnpm"], "--package-manager was removed"),
        (["help", "unknown"], 'unknown command "unknown"'),
        (["check", "local-math"], "check was removed"),
        (["repair", "local-math"], "repair was removed"),
    ],
)
def test_parser_phase_policy_errors_remain_exact(arguments: list[str], message: str):
    with pytest.raises(ConfigurationError, match=message):
        parse_arguments(arguments)


@pytest.mark.parametrize("legacy", ["native", "jni", "jsi"])
def test_v1_type_option_is_rejected(legacy: str):
    with pytest.raises(ConfigurationError, match='unknown option "--type"'):
        parse_arguments(["add", "local-math", "--type", legacy])


@pytest.mark.parametrize("legacy", ["--add", "-add", "--remove", "--no-prompt"])
def test_legacy_command_forms_and_options_are_rejected(legacy: str):
    with pytest.raises(ConfigurationError):
        parse_arguments([legacy])


def test_double_dash_ends_option_parsing_for_command_positionals():
    parsed = parse_arguments(["add", "--", "--help"])

    assert parsed.command == "add"
    assert parsed.positional == "--help"
    assert not parsed.show_help


def test_double_dash_can_precede_the_command():
    parsed = parse_arguments(["--", "help", "add"])

    assert parsed.command == "help"
    assert parsed.positional == "add"


def test_options_after_double_dash_are_not_applied():
    parsed = parse_arguments(["add", "--", "--all"])

    assert parsed.positional == "--all"
    assert not parsed.has("all")


def test_double_dash_positional_still_passes_normal_package_validation(tmp_path: Path):
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n', encoding="utf-8"
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (tmp_path / "android/app/build.gradle").write_text(
        "plugins {}\n", encoding="utf-8"
    )

    code, _, stderr = invoke(
        ["add", "--yes", "--", "--help"], tmp_path
    )

    assert code == 2
    assert 'invalid package name "--help"' in stderr
    assert not (tmp_path / "local_modules").exists()


def test_selective_validate_syntax_is_retired():
    with pytest.raises(ConfigurationError, match="--all was removed"):
        parse_arguments(["validate", "local-math", "--all"])


@pytest.mark.parametrize("command", ["check", "repair", "remove", "template"])
def test_retired_commands_name_the_replacement_workflow(command: str):
    with pytest.raises(ConfigurationError, match=f"{command}.*removed"):
        parse_arguments([command])


def test_output_modes_are_mutually_exclusive():
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        parse_arguments(["doctor", "--quiet", "--json"])


@pytest.mark.parametrize(
    "option",
    [
        "--all",
        "--build",
        "--delete-build-files",
        "--diff",
        "--dry-run",
        "--package-manager",
        "--skip-install",
    ],
)
def test_retired_options_are_precise(option: str):
    arguments = ["update", option]
    if option == "--package-manager":
        arguments.append("npm")
    with pytest.raises(ConfigurationError, match=f"{option} was removed"):
        parse_arguments(arguments)


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


@pytest.mark.parametrize("command", ["add", "update", "validate", "doctor"])
def test_two_command_help_routes_are_byte_identical(tmp_path: Path, command: str):
    first = invoke([command, "--help"], tmp_path)
    second = invoke(["help", command], tmp_path)
    assert first == second == (0, COMMAND_HELP[command], "")


def test_add_help_preserves_multiline_example(tmp_path: Path):
    _, stdout, _ = invoke(["help", "add"], tmp_path)
    assert (
        "  sn-module-gen add @acme/stylus --starter kotlin \\\n"
        "    --javascript-name Stylus \\\n"
        "    --android-namespace com.acme.stylus --yes\n"
    ) in stdout


def test_help_describes_defaults_as_overridable_and_versions_as_separate():
    add = COMMAND_HELP["add"]
    normalized = " ".join(add.split())

    assert "omitted choices use documented" in normalized
    assert "Add does not invoke npm, Yarn, an app build" in normalized
    assert "bounded standalone Gradle/KSP analysis" in normalized
    assert "Authored source, metadata, package.json, and CMakeLists.txt" in normalized
    assert "Local feature package version" in normalized
    assert "omitted choices use documented defaults" in normalized


def test_help_explains_devconfig_and_generated_documentation_refresh():
    root = " ".join(ROOT_HELP.split())
    add = " ".join(COMMAND_HELP["add"].split())
    doctor = " ".join(COMMAND_HELP["doctor"].split())

    assert "Authors own source, package.json, lockfiles, dependencies, and builds" in root
    assert "bounded compiler/KSP analysis" in root
    assert "Generated files live in dedicated generated locations" in add
    assert "CMake 3.24 or newer" in doctor


def test_help_matches_update_and_doctor_behavior():
    update = " ".join(COMMAND_HELP["update"].split())
    doctor = " ".join(COMMAND_HELP["doctor"].split())

    assert "writes all generator-owned output even when bytes are unchanged" in update
    assert "never installs dependencies" in update
    assert "never installs tools, runs an app build" in doctor
    assert "PluginHost/SELinux" in doctor


def test_doctor_rejects_retired_build_probe():
    with pytest.raises(ConfigurationError, match="--build was removed"):
        parse_arguments(["doctor", "--build", "--json"])


def test_version_is_exact_and_works_outside_plugin(tmp_path: Path):
    assert invoke(["--version"], tmp_path) == (
        0,
        f"sn-module-gen {__version__}\n",
        "",
    )


def test_no_command_in_non_tty_is_usage_error(tmp_path: Path):
    code, stdout, stderr = invoke([], tmp_path)
    assert code == 2
    assert stdout == ""
    assert stderr == (
        "error: no command was provided\n\n"
        "Run `sn-module-gen --help` for usage.\n"
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
    assert "sn-module-gen add --help" in stderr


def test_unknown_command_recovery_suggests_a_close_command(tmp_path: Path):
    code, _, stderr = invoke(["ad"], tmp_path)

    assert code == 2
    assert 'unknown command "ad"' in stderr
    assert "Did you mean `sn-module-gen add`?" in stderr
    assert "sn-module-gen --help" in stderr
