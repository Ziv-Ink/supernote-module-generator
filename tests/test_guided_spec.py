from __future__ import annotations

import io
import json
from pathlib import Path

from supernote_module_generator.cli import main


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class BrokenOutput(io.StringIO):
    def write(self, value: str) -> int:
        raise BrokenPipeError


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android").mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    return tmp_path


def test_plain_guided_add_uses_linear_questions_and_executes_without_review(tmp_path: Path):
    root = plugin(tmp_path)
    stdin = TtyStringIO("1\nlocal-guided\n\n\n\n\n")
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main(
        ["--plain", "add", "--skip-install"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    assert code == 0
    assert stdout.getvalue().startswith('[OK] Added module "local-guided"\n')
    transcript = stderr.getvalue()
    assert "Module type:" in transcript
    assert "Choose [1-3]:" in transcript
    assert "Choose a number or package name:" not in transcript
    assert "Package name:" in transcript
    assert "Description (optional):" in transcript
    assert "Customize names and version?" not in transcript
    assert "JavaScript name [Guided]: " in transcript
    assert "Android namespace [com.example.guided]: " in transcript
    assert "Package version [0.1.0]: " in transcript
    assert "For Kotlin/Java code and Android APIs through the React Native" in transcript
    assert "For C/C++ behind an asynchronous Kotlin/Java React Native bridge." in transcript
    assert "Experimental synchronous C++; requires target PluginHost support." in transcript
    assert "Add module\n\nModule type:" in transcript
    assert "Module type:  Native Module - Kotlin/Java\n\n  Used as" in transcript
    assert "  Used as the local folder" in transcript
    assert "npm or Yarn dependency name.\nPackage name: " in transcript
    assert "\n\n\n" not in transcript
    assert "Add this module?" not in transcript
    assert "0.0s" not in transcript
    assert "0.1s" not in transcript
    assert "Module type:  Native Module - Kotlin/Java" in transcript
    assert transcript.isascii()
    assert stdout.getvalue().isascii()


def test_plain_main_menu_exit_has_no_final_output(tmp_path: Path):
    root = plugin(tmp_path)
    stdin = TtyStringIO("7\n")
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main([], stdin=stdin, stdout=stdout, stderr=stderr, cwd=root)
    assert code == 0
    assert stdout.getvalue() == ""
    assert "Supernote Module Generator" in stderr.getvalue()


def test_guided_add_suggestions_are_editable_without_a_customize_gate(tmp_path: Path):
    root = plugin(tmp_path)
    stdin = TtyStringIO(
        "1\nlocal-custom\n\nCustomBridge\ncom.acme.custom\n2.3.4\n"
    )
    stdout = TtyStringIO()
    stderr = TtyStringIO()

    code = main(
        ["--plain", "add", "--skip-install"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    metadata = json.loads(
        (root / "local_modules/local-custom/.supernote-module.json").read_text(
            encoding="utf-8"
        )
    )
    assert code == 0
    assert metadata["module_name"] == "CustomBridge"
    assert metadata["android_namespace"] == "com.acme.custom"
    assert metadata["package_version"] == "2.3.4"
    assert "Customize names and version?" not in stderr.getvalue()
    assert "JavaScript name [Custom]: " in stderr.getvalue()


def test_invalid_explicit_value_is_rejected_before_wizard_header(tmp_path: Path):
    root = plugin(tmp_path)
    stdin = TtyStringIO()
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main(
        ["--plain", "add", "local-math", "--javascript-name", "1Math"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    assert code == 2
    assert "Supernote Module Generator" not in stderr.getvalue()
    assert 'error: invalid JavaScript name "1Math"' in stderr.getvalue()


def test_invalid_root_menu_exposes_only_doctor_help_exit(tmp_path: Path):
    stdin = TtyStringIO("3\n")
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main([], stdin=stdin, stdout=stdout, stderr=stderr, cwd=tmp_path)
    assert code == 0
    transcript = stderr.getvalue()
    assert "Not a Supernote plugin" in transcript
    assert "Doctor" in transcript
    assert "Help" in transcript
    assert "Add module" not in transcript


def test_direct_interactive_empty_state_prints_once_and_exits(tmp_path: Path):
    root = plugin(tmp_path)
    stdout = TtyStringIO()
    stderr = TtyStringIO()

    code = main(
        ["--plain", "validate"],
        stdin=TtyStringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    assert code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue().count("No modules were found in this plugin.") == 1
    assert "None" not in stderr.getvalue()


def test_help_broken_pipe_exits_without_an_exception(tmp_path: Path):
    assert main(["--help"], stdout=BrokenOutput(), stderr=io.StringIO(), cwd=tmp_path) == 0


def test_root_help_explains_module_execution_models(tmp_path: Path):
    stdout = io.StringIO()

    assert main(["--help"], stdout=stdout, stderr=io.StringIO(), cwd=tmp_path) == 0

    output = stdout.getvalue()
    assert "For Kotlin/Java code and Android APIs through the React Native bridge." in output
    assert "For C/C++ behind an asynchronous Kotlin/Java React Native bridge." in output
    assert "Experimental synchronous C++; requires target PluginHost support." in output


def test_back_reopens_previous_add_answer_for_editing(tmp_path: Path):
    root = plugin(tmp_path)
    stdin = TtyStringIO("1\nlocal-first\n:back\nlocal-second\n\n\n\n\n")
    stdout = TtyStringIO()
    stderr = TtyStringIO()

    code = main(
        ["--plain", "add", "--skip-install"],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    assert code == 0
    assert (root / "local_modules/local-second").is_dir()
    assert not (root / "local_modules/local-first").exists()
    transcript = stderr.getvalue()
    assert "Package name:" in transcript
    assert "Package name [local-first]:" in transcript


def test_remove_yes_never_bypasses_confirmation_without_an_explicit_target(tmp_path: Path):
    root = plugin(tmp_path)
    create_out = TtyStringIO()
    create_err = TtyStringIO()
    assert main(
        ["add", "local-safe", "--type", "native", "--skip-install", "--yes"],
        stdin=io.StringIO(),
        stdout=create_out,
        stderr=create_err,
        cwd=root,
    ) == 0

    stdout = TtyStringIO()
    stderr = TtyStringIO()
    code = main(
        ["--plain", "remove", "--skip-install", "--yes"],
        stdin=TtyStringIO("1\n"),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    assert code == 2
    assert "--yes requires an explicit module or --all" in stderr.getvalue()
    assert (root / "local_modules/local-safe").is_dir()
