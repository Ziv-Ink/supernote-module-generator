from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from supernote_module_generator.arguments import parse_arguments
from supernote_module_generator.cli import main
from supernote_module_generator.errors import SubprocessFailure
from supernote_module_generator.feature_cli_operations import FeatureCliOperationService
from supernote_module_generator.feature_workflows import FeatureDecisionCollector


def plugin(tmp_path: Path, *, npm_lock: bool = False, yarn_lock: bool = False) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (tmp_path / "android/app/build.gradle").write_text(
        "plugins {}\n", encoding="utf-8"
    )
    if npm_lock:
        (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    if yarn_lock:
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    return tmp_path


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(arguments, stdin=io.StringIO(), stdout=stdout, stderr=stderr, cwd=root)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("arguments", "native", "jvm"),
    [
        (["--starter", "cpp"], True, False),
        (["--starter", "kotlin"], False, True),
        (["--starter", "cpp", "--starter", "kotlin"], True, True),
    ],
)
def test_add_scaffolds_selected_families_without_backend_metadata(
    tmp_path: Path, arguments: list[str], native: bool, jvm: bool
):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root, ["add", "document", *arguments, "--skip-install", "--yes"]
    )

    assert code == 0, stderr
    assert stdout.startswith('✓ Added feature "document"\n')
    feature = root / "local_modules/document"
    metadata = json.loads((feature / ".supernote-module.json").read_text())
    assert "type" not in metadata
    assert "backend" not in metadata
    assert (feature / "android/src/main/cpp/feature.cpp").is_file() is native
    kotlin = feature / "android/src/main/java/com/example/document/FeatureApi.kt"
    assert kotlin.is_file() is jvm
    assert "supernote-v2-runtime" in (root / "android/settings.gradle").read_text()


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        ("--javascript-name", "Existing", 'JavaScript name "Existing" is already used'),
        (
            "--android-namespace",
            "com.example.existing",
            'Android namespace "com.example.existing" is already used',
        ),
    ],
)
def test_add_rejects_feature_identity_collisions_without_mutation(
    tmp_path: Path, option: str, value: str, expected: str
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        [
            "add",
            "existing",
            "--starter",
            "cpp",
            "--javascript-name",
            "Existing",
            "--android-namespace",
            "com.example.existing",
            "--skip-install",
            "--yes",
        ],
    )[0] == 0
    before = (root / "android/settings.gradle").read_bytes()

    code, _, stderr = invoke(
        root,
        [
            "add",
            "candidate",
            "--starter",
            "kotlin",
            option,
            value,
            "--skip-install",
            "--yes",
        ],
    )

    assert code == 2
    assert expected in stderr
    assert not (root / "local_modules/candidate").exists()
    assert (root / "android/settings.gradle").read_bytes() == before


def test_update_preserves_both_source_roots_and_deleted_starter(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(
        root,
        [
            "add",
            "document",
            "--starter",
            "cpp",
            "--starter",
            "kotlin",
            "--skip-install",
            "--yes",
        ],
    )[0] == 0
    feature = root / "local_modules/document"
    native = feature / "android/src/main/cpp/custom.cpp"
    native.write_text("int custom() { return 7; }\n")
    starter = feature / "android/src/main/cpp/feature.cpp"
    starter.unlink()
    java = feature / "android/src/main/java/com/example/document/Custom.java"
    java.write_text("package com.example.document; class Custom {}\n")

    code, _, stderr = invoke(
        root, ["update", "document", "--skip-install", "--yes"]
    )

    assert code == 0, stderr
    assert native.read_text() == "int custom() { return 7; }\n"
    assert java.is_file()
    assert not starter.exists()


@pytest.mark.parametrize("option", ["--skip-install", "--package-manager=npm"])
def test_update_rejects_dependency_options_when_refresh_is_not_required(
    tmp_path: Path, option: str
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "current", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/current"
    link = root / "node_modules/current"
    link.parent.mkdir()
    link.symlink_to(feature, target_is_directory=True)

    code, _, stderr = invoke(root, ["update", "current", option, "--yes"])

    assert code == 2
    assert "does not affect this update" in stderr


def test_add_postcondition_failure_rolls_back_feature_runtime_and_parent(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    originals = {
        path: path.read_bytes()
        for path in (
            root / "package.json",
            root / "android/settings.gradle",
            root / "android/app/build.gradle",
        )
    }
    monkeypatch.setattr(
        "supernote_module_generator.feature_operations.FeatureOperationService.verify_generated_state",
        lambda self: ["forced structural failure"],
    )

    code, _, stderr = invoke(
        root,
        ["add", "broken", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 1
    assert "structural postconditions" in stderr
    assert not (root / "local_modules/broken").exists()
    assert not (root / "android/.supernote-module/v2-runtime").exists()
    for path, content in originals.items():
        assert path.read_bytes() == content


def test_remove_dependency_failure_restores_feature_runtime_and_parent(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path, npm_lock=True)
    assert invoke(
        root,
        ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/safe"
    runtime_before = (root / "android/.supernote-module/v2-runtime/feature-registry.json").read_bytes()

    attempts = 0

    def fail_once(self, command, *, phase):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SubprocessFailure("forced install failure", phase=phase)
        return None

    monkeypatch.setattr(FeatureCliOperationService, "_run", fail_once)
    code, _, stderr = invoke(root, ["remove", "safe", "--yes"])

    assert code == 1
    assert "forced install failure" in stderr
    assert feature.is_dir()
    assert (
        root / "android/.supernote-module/v2-runtime/feature-registry.json"
    ).read_bytes() == runtime_before
    assert json.loads((root / "package.json").read_text())["dependencies"]["safe"]


def test_remove_preserves_build_outputs_unless_cleanup_is_explicit(tmp_path: Path):
    root = plugin(tmp_path)
    for name in ("preserve", "cleanup"):
        assert invoke(
            root,
            ["add", name, "--starter", "cpp", "--skip-install", "--yes"],
        )[0] == 0
    build_paths = (
        root / "build",
        root / "android/build",
        root / "android/app/build",
    )
    for index, path in enumerate(build_paths):
        path.mkdir(parents=True)
        (path / "proof.txt").write_text(str(index), encoding="utf-8")

    assert invoke(
        root, ["remove", "preserve", "--skip-install", "--yes"]
    )[0] == 0
    assert all(path.is_dir() for path in build_paths)

    code, _, stderr = invoke(
        root,
        [
            "remove",
            "cleanup",
            "--delete-build-files",
            "--skip-install",
            "--yes",
        ],
    )

    assert code == 0, stderr
    assert all(not path.exists() for path in build_paths)


def test_remove_all_is_explicit_and_removes_every_feature(tmp_path: Path):
    root = plugin(tmp_path)
    for name in ("one", "two"):
        assert invoke(
            root,
            ["add", name, "--starter", "cpp", "--skip-install", "--yes"],
        )[0] == 0

    code, stdout, stderr = invoke(
        root, ["remove", "--all", "--skip-install", "--yes"]
    )

    assert code == 0, stderr
    assert "2 features" in stdout
    assert not (root / "local_modules/one").exists()
    assert not (root / "local_modules/two").exists()
    assert not (root / "android/.supernote-module/v2-runtime").exists()


def test_package_manager_precedence_for_noninteractive_add(tmp_path: Path):
    root = plugin(tmp_path, yarn_lock=True)
    parsed = parse_arguments(
        [
            "add",
            "math",
            "--starter",
            "cpp",
            "--description",
            "",
            "--javascript-name",
            "Math",
            "--android-namespace",
            "com.example.math",
            "--package-version",
            "0.1.0",
        ]
    )
    decisions = FeatureDecisionCollector(root, parsed, None).add()
    assert decisions.package_manager == "yarn"


def test_conflicting_lockfiles_still_require_manager_with_yes(tmp_path: Path):
    root = plugin(tmp_path, npm_lock=True, yarn_lock=True)
    code, _, stderr = invoke(root, ["add", "math", "--yes"])
    assert code == 2
    assert "package manager is ambiguous" in stderr


def test_skip_install_bypasses_conflicting_lockfiles(tmp_path: Path):
    root = plugin(tmp_path, npm_lock=True, yarn_lock=True)
    code, stdout, stderr = invoke(root, ["add", "math", "--skip-install", "--yes"])
    assert code == 0, stderr
    assert 'Added feature "math"' in stdout


def test_quiet_success_is_exactly_one_line(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        [
            "add",
            "quiet",
            "--starter",
            "cpp",
            "--skip-install",
            "--yes",
            "--quiet",
        ],
    )
    assert code == 0, stderr
    assert stdout == 'Added feature "quiet"\n'


def test_build_flag_routes_to_parent_assemble_task_and_changes_success_copy(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    gradle = root / "android/gradlew"
    gradle.write_text(
        '#!/bin/sh\ncase "$1" in\n  --version|:app:assembleDebug) exit 0 ;;\n  *) exit 1 ;;\nesac\n'
    )
    gradle.chmod(0o755)

    code, stdout, stderr = invoke(
        root,
        ["add", "built", "--starter", "cpp", "--skip-install", "--build", "--yes"],
    )

    assert code == 0, stderr
    assert 'Added and built feature "built"' in stdout


def test_add_rejects_local_modules_symlink_that_escapes_plugin_root(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    root = plugin(plugin_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "local_modules").symlink_to(outside, target_is_directory=True)

    code, _, stderr = invoke(
        root,
        ["add", "escape", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 2
    assert "target resolves outside the Supernote plugin" in stderr
    assert not (outside / "escape").exists()
