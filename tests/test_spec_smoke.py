from __future__ import annotations

import io
import json
from pathlib import Path

from supernote_module_generator.cli import main


def plugin(tmp_path: Path) -> Path:
    android = tmp_path / "android"
    android.mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (android / "settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=root)
    return code, stdout.getvalue(), stderr.getvalue()


def test_add_validate_remove_smoke(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        ["add", "local-math", "--type", "native", "--skip-install", "--yes"],
    )
    assert code == 0, stderr
    assert stdout.startswith('✓ Added module "local-math"\n')
    module = root / "local_modules/local-math"
    assert (module / ".supernote-module.json").is_file()
    assert json.loads((module / "package.json").read_text())["name"] == "local-math"
    assert "description" not in json.loads((module / "package.json").read_text())

    link = root / "node_modules/local-math"
    link.parent.mkdir()
    link.symlink_to(module, target_is_directory=True)
    code, stdout, stderr = invoke(root, ["validate", "local-math"])
    assert code == 0, stderr
    assert stdout == '✓ Module "local-math" is valid\n'

    code, stdout, stderr = invoke(
        root, ["remove", "local-math", "--skip-install", "--yes"]
    )
    assert code == 0, stderr
    assert stdout.startswith('✓ Removed module "local-math"\n')
    assert not module.exists()


def test_json_add_has_stable_envelope_and_empty_stderr(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        [
            "--json",
            "add",
            "local-json",
            "--type",
            "jni",
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
    assert value["module"]["type"] == "jni"
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
    assert "--type <native|jni|jsi>" in stderr
    assert '--description <TEXT> or --description ""' in stderr
    assert "--javascript-name <NAME>" in stderr
    assert "--android-namespace <NAMESPACE>" in stderr
    assert "--package-version <VERSION>" in stderr
    assert "--package-manager <npm|yarn>" in stderr
