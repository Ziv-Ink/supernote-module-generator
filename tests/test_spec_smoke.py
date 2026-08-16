from __future__ import annotations

import io
import json
from pathlib import Path

from supernote_module_generator.cli import main


def plugin(tmp_path: Path) -> Path:
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (android / "settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    (android / "app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=root)
    return code, stdout.getvalue(), stderr.getvalue()


def test_add_validate_remove_smoke(tmp_path: Path, make_directory_symlink):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        ["add", "local-math", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0, stderr
    assert stdout.startswith('✓ Added feature "local-math"\n')
    module = root / "local_modules/local-math"
    assert (module / ".supernote-module.json").is_file()
    assert json.loads((module / "package.json").read_text())["name"] == "local-math"
    assert "description" not in json.loads((module / "package.json").read_text())

    link = root / "node_modules/local-math"
    link.parent.mkdir()
    make_directory_symlink(link, module)
    code, stdout, stderr = invoke(root, ["validate", "local-math"])
    assert code == 0, stderr
    assert stdout == '✓ Feature "local-math" is valid\n'

    code, stdout, stderr = invoke(
        root, ["remove", "local-math", "--skip-install", "--yes"]
    )
    assert code == 0, stderr
    assert stdout.startswith('✓ Removed feature "local-math"\n')
    assert not module.exists()


def test_validate_missing_dependency_link_gives_install_action_without_rollback(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "local-math", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0

    code, _, stderr = invoke(root, ["validate", "local-math"])

    assert code == 1
    assert "local-math is not installed in node_modules" in stderr
    assert "Run `npm install` to refresh local dependencies." in stderr
    assert "Rollback:" not in stderr


def test_validate_all_uses_singular_copy_for_one_feature(
    tmp_path: Path, make_directory_symlink
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "local-math", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/local-math"
    link = root / "node_modules/local-math"
    link.parent.mkdir()
    make_directory_symlink(link, feature)

    code, stdout, stderr = invoke(root, ["validate", "--all"])

    assert code == 0, stderr
    assert stdout == "✓ 1 feature is valid\n"


def test_json_add_has_stable_envelope_and_empty_stderr(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        [
            "--json",
            "add",
            "local-json",
            "--starter",
            "kotlin",
            "--skip-install",
            "--yes",
        ],
    )
    assert code == 0
    assert stderr == ""
    value = json.loads(stdout)
    assert list(value) == [
        "schema_version",
        "tool_version",
        "command",
        "status",
        "exit_code",
        "duration_ms",
        "module",
        "modules",
        "changes",
        "dependency",
        "validation",
        "doctor",
        "rollback",
        "warnings",
        "recovery",
        "error",
    ]
    assert value["module"]["type"] == "feature"
    assert value["rollback"] == {
        "attempted": False,
        "status": "not_needed",
        "restored": [],
    }


def test_noninteractive_add_without_yes_lists_all_missing_decisions(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(root, ["add", "local-math"])
    assert code == 2
    assert stdout == ""
    assert "--starter <cpp|kotlin>" in stderr
    assert '--description <TEXT> or --description ""' in stderr
    assert "--javascript-name <NAME>" in stderr
    assert "--android-namespace <NAMESPACE>" in stderr
    assert "--package-version <VERSION>" in stderr
    assert "--package-manager <npm|yarn>" in stderr
