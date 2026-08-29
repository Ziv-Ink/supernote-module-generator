from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from supernote_module_generator.arguments import parse_arguments
from supernote_module_generator.cli import main
from supernote_module_generator.errors import SubprocessFailure, SymlinkPreservationError
from supernote_module_generator.feature_cli_operations import FeatureCliOperationService
from supernote_module_generator.feature_generator import FeatureConfig, stage_feature
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.filesystem import (
    entry_kind,
    source_tree_changes,
    source_tree_inventory,
)
from supernote_module_generator.feature_workflows import FeatureDecisionCollector
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.platform_tools import gradle_wrapper_path
from supernote_module_generator.plugin_build_integration import set_runtime_wiring
import supernote_module_generator.transaction as transaction_module
from supernote_module_generator.transaction import Transaction, recover_pending
from v4_project_inventory import inventory_project


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
    gradle = gradle_wrapper_path(tmp_path)
    if os.name == "nt":
        gradle.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        gradle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gradle.chmod(0o755)
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


def tree_mtimes(root: Path) -> dict[str, int]:
    return {".": root.lstat().st_mtime_ns, **{
        path.relative_to(root).as_posix(): path.lstat().st_mtime_ns
        for path in root.rglob("*")
        if not path.is_symlink()
    }}


def main_application(root: Path) -> Path:
    source = (
        root
        / "android/app/src/main/java/com/example/fixture/MainApplication.kt"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "fun getPackages() =\n"
        "    PackageList(this).packages.apply {\n"
        "      add(ExistingPackage())\n"
        "    }\n",
        encoding="utf-8",
    )
    return source


def _source_symlink_matrix(root: Path, feature: Path) -> dict[str, str]:
    source = feature / "android/src/main/cpp"
    targets = source / "link-targets"
    targets.mkdir()
    (targets / "relative.txt").write_text("relative target\n", encoding="utf-8")
    directory = targets / "directory"
    directory.mkdir()
    (directory / "ordinary.txt").write_text("directory target\n", encoding="utf-8")
    outside_file = root.parent / f"{root.name}-outside-source.txt"
    outside_file.write_text("absolute target\n", encoding="utf-8")
    outside_directory = root.parent / f"{root.name}-outside-source-directory"
    outside_directory.mkdir()
    (outside_directory / "ignored.hpp").write_text(
        "// @SupernotePluginObject\nclass MustNotBeDiscovered {};\n",
        encoding="utf-8",
    )
    links = {
        "relative-file-link": ("link-targets/relative.txt", False),
        "absolute-file-link": (str(outside_file), False),
        "relative-directory-link": ("link-targets/directory", True),
        "absolute-directory-link": (str(outside_directory), True),
        "broken-file-link": ("missing-file.txt", False),
        "broken-directory-link": ("missing-directory", True),
    }
    for name, (target, target_is_directory) in links.items():
        try:
            (source / name).symlink_to(
                target,
                target_is_directory=target_is_directory,
            )
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symbolic links are unavailable on this host: {exc}")
    return {name: os.readlink(source / name) for name in links}


@pytest.mark.parametrize(
    ("arguments", "native", "jvm"),
    [
        (["--starter", "cpp"], True, False),
        (["--starter", "kotlin"], False, True),
        (["--starter", "cpp", "--starter", "kotlin"], True, True),
    ],
)
def test_add_scaffolds_selected_families_without_backend_metadata(
    tmp_path: Path,
    arguments: list[str],
    native: bool,
    jvm: bool,
    stub_ksp_frontend,
):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root, ["add", "document", *arguments, "--skip-install", "--yes"]
    )

    assert code == 0, stderr
    assert stdout.splitlines()[0].endswith('Added feature "document"')
    feature = root / "local_modules/document"
    metadata = json.loads((feature / ".supernote-module.json").read_text())
    assert "type" not in metadata
    assert "backend" not in metadata
    assert (feature / "android/src/main/cpp/feature.cpp").is_file() is native
    kotlin = feature / "android/src/main/java/com/example/document/FeatureApi.kt"
    assert kotlin.is_file() is jvm
    assert "supernote-v4-runtime" in (root / "android/settings.gradle").read_text()


def test_add_and_update_render_semantic_api_without_gradle_source_writes(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    calls: list[tuple[list[str], Path]] = []

    def record(command, *, cwd, timeout, stream=None):
        calls.append((list(command), cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        "supernote_module_generator.feature_cli_operations.run_process", record
    )

    assert invoke(
        root,
        ["add", "document", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    assert invoke(
        root, ["update", "document", "--skip-install", "--yes"]
    )[0] == 0

    assert calls == []
    assert (root / ".supernote-module/manifest.json").is_file()


def test_failed_api_documentation_refresh_preserves_a_concurrent_readme_save(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "other", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    assert invoke(
        root,
        ["add", "document", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/document"
    readme = feature / "README.md"
    before = readme.read_bytes()
    other_readme = root / "local_modules/other/README.md"
    source = feature / "android/src/main/cpp/feature.cpp"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n// @SupernotePluginExport\ndouble total(double value) { return value; }\n",
        encoding="utf-8",
    )

    original_execute = GenerationService.execute

    def fail(self, plan, transaction, *, commit=True):
        original_execute(self, plan, transaction, commit=False)
        other_readme.write_text("partially regenerated\n", encoding="utf-8")
        raise RuntimeError("forced semantic failure")

    monkeypatch.setattr(
        GenerationService, "execute", fail
    )
    code, _, stderr = invoke(
        root, ["update", "document", "--skip-install", "--yes"]
    )

    assert code == 3
    assert "forced semantic failure" in stderr
    assert readme.read_bytes() == before
    assert other_readme.read_bytes() == b"partially regenerated\n"
    assert "double total" in source.read_text(encoding="utf-8")


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


def test_update_preserves_both_source_roots_and_deleted_starter(
    tmp_path: Path, stub_ksp_frontend
):
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


def test_update_and_repeated_update_preserve_every_user_source_symlink_kind(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "links", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/links"
    expected_links = _source_symlink_matrix(root, feature)

    for iteration in range(2):
        before_noop = inventory_project(root) if iteration == 1 else None
        code, _, stderr = invoke(
            root,
            ["update", "links", "--skip-install", "--yes"],
        )
        assert code == 0, stderr
        source = feature / "android/src/main/cpp"
        assert {
            name: os.readlink(source / name) for name in expected_links
        } == expected_links
        assert all((source / name).is_symlink() for name in expected_links)
        if before_noop is not None:
            assert inventory_project(root) == before_noop


def test_failed_update_restores_exact_symlinks_and_whole_project_state(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "links", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/links"
    _source_symlink_matrix(root, feature)
    source = feature / "android/src/main/cpp/feature.cpp"
    source.write_text(
        source.read_text()
        + "\n// @SupernotePluginExport\n"
        + "double checkpoint_change(double value) { return value; }\n"
    )
    before = inventory_project(root)
    original_execute = GenerationService.execute

    def fail(self, plan, transaction, *, commit=True):
        original_execute(self, plan, transaction, commit=False)
        raise RuntimeError("forced post-update failure")

    monkeypatch.setattr(GenerationService, "execute", fail)

    code, _, stderr = invoke(
        root,
        ["update", "links", "--skip-install", "--yes"],
    )

    assert code == 1
    assert "forced post-update failure" in stderr
    assert inventory_project(root) == before


def test_ordinary_targeted_update_matches_preview_and_is_true_noop(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    node_modules = root / "node_modules"
    node_modules.mkdir()
    (node_modules / "alpha").symlink_to(root / "local_modules/alpha")
    preview_code, preview_stdout, preview_stderr = invoke(
        root, ["--json", "update", "alpha", "--dry-run"]
    )
    before_entries = inventory_project(root)
    before_mtimes = tree_mtimes(root)

    code, stdout, stderr = invoke(
        root, ["--json", "update", "alpha", "--yes"]
    )
    preview = json.loads(preview_stdout)
    payload = json.loads(stdout)

    assert preview_code == 0, preview_stderr
    assert code == 0, stderr
    assert preview["changes"] == payload["changes"] == []
    assert preview["metadata"]["generation_id"] == payload["metadata"]["generation_id"]
    assert payload["metadata"]["no_op"] is True
    assert payload["actual_changes"] == []
    assert inventory_project(root) == before_entries
    assert tree_mtimes(root) == before_mtimes


def test_stale_parent_dependency_is_previewed_and_executed_by_same_plan(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    node_modules = root / "node_modules"
    node_modules.mkdir()
    (node_modules / "alpha").symlink_to(root / "local_modules/alpha")
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    del package["dependencies"]["alpha"]
    package_path.write_text(json.dumps(package, indent=2) + "\n")

    preview_code, preview_stdout, preview_stderr = invoke(
        root, ["--json", "update", "alpha", "--dry-run", "--diff"]
    )
    code, stdout, stderr = invoke(
        root, ["--json", "update", "alpha", "--skip-install", "--yes"]
    )
    preview = json.loads(preview_stdout)
    payload = json.loads(stdout)

    assert preview_code == 0, preview_stderr
    assert code == 0, stderr
    expected_change = {
        "path": str(package_path),
        "action": "update",
        "ownership": "parent_dependency",
    }
    assert expected_change in preview["changes"]
    assert expected_change in payload["changes"]
    assert expected_change in payload["actual_changes"]
    assert preview["metadata"]["plan"]["dependency_actions"]
    assert preview["metadata"]["no_op"] is False
    assert payload["metadata"]["no_op"] is False
    assert json.loads(package_path.read_text())["dependencies"]["alpha"] == (
        "file:./local_modules/alpha"
    )


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is a POSIX regression fixture")
def test_targeted_update_never_snapshots_ignored_feature_build_cache(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    cache = root / "local_modules/alpha/android/build"
    cache.mkdir(parents=True)
    fifo = cache / "worker.pipe"
    os.mkfifo(fifo)
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    source.write_text(
        source.read_text()
        + "\n// @SupernotePluginExport\n"
        + "double planned_change(double value) { return value; }\n"
    )

    code, _, stderr = invoke(
        root, ["update", "alpha", "--skip-install", "--yes"]
    )

    assert code == 0, stderr
    assert fifo.exists()


@pytest.mark.parametrize("mutation_target", ["other_feature", "application"])
def test_update_build_mutation_preserves_unattributed_live_source(
    tmp_path: Path,
    monkeypatch,
    mutation_target: str,
):
    root = plugin(tmp_path)
    for name in ("alpha", "beta"):
        assert invoke(
            root,
            ["add", name, "--starter", "cpp", "--skip-install", "--yes"],
        )[0] == 0
    alpha = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    alpha.write_text(
        alpha.read_text()
        + "\n// @SupernotePluginExport\n"
        + "double planned_change(double value) { return value; }\n"
    )
    beta = root / "local_modules/beta/android/src/main/cpp/feature.cpp"
    application = main_application(root)
    set_runtime_wiring(root, enabled=True)
    target = beta if mutation_target == "other_feature" else application
    before = source_tree_inventory(root)

    def mutating_build(command, *, cwd, timeout, env):
        target.write_text(target.read_text() + "// build mutation\n")
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", mutating_build
    )

    code, stdout, stderr = invoke(
        root,
        ["--json", "update", "alpha", "--skip-install", "--build", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 3, (stderr, payload)
    assert payload["rollback"]["status"] == "partial"
    assert payload["error"]["kind"] == "build_failed"
    assert payload["issues"][0]["code"] == "SNV4_BUILD_MUTATED_SOURCE"
    assert payload["requested_targets"] == ["alpha"]
    assert payload["affected_targets"]
    assert payload["diagnostics"]
    after = source_tree_inventory(root)
    assert source_tree_changes(before, after) == (
        f"modified:{target.relative_to(root).as_posix()}",
    )
    assert target.read_text().endswith("// build mutation\n")


def test_update_build_failure_preserves_structured_plan_and_diagnostics(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0

    def failed_build(command, *, cwd, timeout, env):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "src/main/Foo.kt:7:3: error: unresolved reference: Missing\n",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", failed_build
    )

    code, stdout, stderr = invoke(
        root,
        ["--json", "update", "alpha", "--skip-install", "--build", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["validation"]["build"] == "failed"
    assert payload["issues"][0]["code"] == "SNV4_BUILD_FAILED"
    assert payload["requested_targets"] == ["alpha"]
    assert payload["affected_targets"]
    assert payload["diagnostics"]
    assert payload["next_action"]


def test_add_build_mutation_preserves_unattributed_application_source(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    application = main_application(root)
    before = source_tree_inventory(root)

    def mutating_build(command, *, cwd, timeout, env):
        application.write_text(application.read_text() + "// build mutation\n")
        return subprocess.CompletedProcess(command, 0, "BUILD SUCCESSFUL\n", "")

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", mutating_build
    )

    code, stdout, stderr = invoke(
        root,
        [
            "--json",
            "add",
            "alpha",
            "--starter",
            "cpp",
            "--skip-install",
            "--build",
            "--yes",
        ],
    )
    payload = json.loads(stdout)

    assert code == 3, stderr
    assert payload["rollback"]["status"] == "partial"
    assert payload["error"]["kind"] == "build_failed"
    assert payload["requested_targets"] == ["alpha"]
    assert payload["diagnostics"]
    after = source_tree_inventory(root)
    assert source_tree_changes(before, after) == (
        f"modified:{application.relative_to(root).as_posix()}",
    )
    assert application.read_text().endswith("// build mutation\n")


def test_unsupported_platform_symlink_capability_fails_before_transaction(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "links", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/links"
    _source_symlink_matrix(root, feature)
    before = inventory_project(root)

    def reject(_roots) -> None:
        raise SymlinkPreservationError("Windows symlink capability is unavailable")

    monkeypatch.setattr(
        "supernote_module_generator.feature_cli_operations.validate_source_symlink_support",
        reject,
    )

    code, _, stderr = invoke(
        root,
        ["update", "links", "--skip-install", "--yes"],
    )

    assert code == 1
    assert "Windows symlink capability is unavailable" in stderr
    assert inventory_project(root) == before
    assert not (root / ".supernote-module-transaction.json").exists()


def test_v4_typed_cpp_jvm_and_mixed_features_complete_public_cli_lifecycle(
    tmp_path: Path, make_directory_symlink, stub_ksp_frontend
):
    root = plugin(tmp_path)
    configurations = {
        "typed-cpp": ("cpp",),
        "typed-jvm": ("kotlin",),
        "typed-mixed": ("cpp", "kotlin"),
    }
    for name, starters in configurations.items():
        arguments = ["add", name]
        for starter in starters:
            arguments.extend(("--starter", starter))
        arguments.extend(("--skip-install", "--yes"))
        code, _, stderr = invoke(root, arguments)
        assert code == 0, stderr

    cpp_source = root / "local_modules/typed-cpp/android/src/main/cpp/Types.hpp"
    cpp_source.write_text(
        "// @SupernotePluginValue\n"
        "struct Point {\n"
        "  // @SupernotePluginExport\n"
        "  double x;\n"
        "};\n"
        "// @SupernotePluginObject\n"
        "class Stroke {\n"
        "public:\n"
            "  // @SupernotePluginExport\n"
            "  double length() const;\n"
            "};\n",
            encoding="utf-8",
        )
    cpp_impl = root / "local_modules/typed-cpp/android/src/main/cpp/Types.cpp"
    cpp_impl.write_text(
        '#include "Types.hpp"\n'
        "#include <memory>\n"
        "// @SupernotePluginExport\n"
        "Point origin() { return Point{0.0}; }\n"
        "// @SupernotePluginExport\n"
        "std::shared_ptr<Stroke> makeStroke() { return std::make_shared<Stroke>(); }\n",
        encoding="utf-8",
    )
    mixed_cpp = root / "local_modules/typed-mixed/android/src/main/cpp/Types.cpp"
    mixed_cpp.write_text(
        "// @SupernotePluginExport\n"
        "double nativeWidth() { return 1.0; }\n",
        encoding="utf-8",
    )
    jvm_source = (
        root
        / "local_modules/typed-jvm/android/src/main/java/com/example/typed_jvm/Types.kt"
    )
    jvm_source.write_text(
        "package com.example.typed_jvm\n\n"
        "import supernote.generated.annotations.SupernotePluginExport\n"
        "import supernote.generated.annotations.SupernotePluginObject\n"
        "import supernote.generated.annotations.SupernotePluginValue\n\n"
        "@SupernotePluginValue\n"
        "data class Point(@field:SupernotePluginExport val x: Double)\n\n"
        "@SupernotePluginObject\n"
        "class Stroke {\n"
        "  @SupernotePluginExport fun length(): Double = 1.0\n"
        "}\n",
        encoding="utf-8",
    )
    mixed_jvm = (
        root
        / "local_modules/typed-mixed/android/src/main/java/com/example/typed_mixed/Types.kt"
    )
    mixed_jvm.write_text(
        "package com.example.typed_mixed\n\n"
        "import supernote.generated.annotations.SupernotePluginExport\n"
        "import supernote.generated.annotations.SupernotePluginValue\n\n"
        "@SupernotePluginValue\n"
        "data class JvmSize(@field:SupernotePluginExport val height: Double)\n",
        encoding="utf-8",
    )
    owned_sources = (cpp_source, cpp_impl, mixed_cpp, jvm_source, mixed_jvm)
    source_bytes = {path: path.read_bytes() for path in owned_sources}

    for name in configurations:
        code, _, stderr = invoke(root, ["update", name, "--skip-install", "--yes"])
        assert code == 0, stderr
    assert {path: path.read_bytes() for path in owned_sources} == source_bytes

    links = root / "node_modules"
    links.mkdir()
    for name in configurations:
        make_directory_symlink(links / name, root / "local_modules" / name)
    code, _, stderr = invoke(root, ["validate", "--all"])
    assert code == 0, stderr

    for command in (None, "add", "update", "validate", "remove", "doctor"):
        arguments = ["--help"] if command is None else ["help", command]
        code, stdout, stderr = invoke(root, arguments)
        assert code == 0, stderr
        assert "Supernote Module Generator" in stdout

    for name in configurations:
        code, _, stderr = invoke(
            root, ["remove", name, "--skip-install", "--yes"]
        )
        assert code == 0, stderr
    assert not (root / "android/.supernote-module/v4-runtime").exists()


def test_update_skip_install_is_idempotent_when_refresh_is_not_required(
    tmp_path: Path, make_directory_symlink
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "current", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/current"
    link = root / "node_modules/current"
    link.parent.mkdir()
    make_directory_symlink(link, feature)

    code, _, stderr = invoke(
        root, ["update", "current", "--skip-install", "--yes"]
    )

    assert code == 0, stderr


def test_update_rejects_package_manager_when_refresh_is_not_required(
    tmp_path: Path, make_directory_symlink
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "current", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/current"
    link = root / "node_modules/current"
    link.parent.mkdir()
    make_directory_symlink(link, feature)

    code, _, stderr = invoke(
        root, ["update", "current", "--package-manager=npm", "--yes"]
    )

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
    original = GenerationService.execute

    def fail_after_v4_staging(self, plan, transaction, *, commit=True):
        original(self, plan, transaction, commit=False)
        raise RuntimeError("forced staged verification failure")

    monkeypatch.setattr(GenerationService, "execute", fail_after_v4_staging)

    code, _, stderr = invoke(
        root,
        ["add", "broken", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 1
    assert "forced staged verification failure" in stderr
    assert not (root / "local_modules/broken").exists()
    assert not (root / "android/.supernote-module/v4-runtime").exists()
    assert not (root / "local_modules").exists()
    assert not (root / "android/.supernote-module").exists()
    for path, content in originals.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("command", ["add", "update", "remove"])
@pytest.mark.parametrize(
    "interrupt_point",
    [
        "before_commit_persist",
        "after_commit_persist",
        "after_state_removal",
        "after_journal_unlink",
        "after_commit_return",
    ],
)
def test_feature_operations_respect_every_durable_commit_boundary(
    tmp_path: Path,
    monkeypatch,
    command: str,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    if command in {"update", "remove"}:
        assert invoke(
            root, ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"]
        )[0] == 0
        if command == "update":
            source = root / "local_modules/safe/android/src/main/cpp/feature.cpp"
            source.write_text(source.read_text().replace("greet(", "greetAgain("))
            arguments = ["--json", "update", "safe", "--skip-install", "--yes"]
        else:
            arguments = ["--json", "remove", "safe", "--skip-install", "--yes"]
    else:
        arguments = [
            "--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"
        ]
    fired = False
    original_persist = Transaction._persist
    original_finish = Transaction.finish_commit
    original_commit = Transaction.commit
    original_checkpoint = Transaction.checkpoint

    if interrupt_point in {"before_commit_persist", "after_commit_persist"}:
        def interrupting_persist(self):
            nonlocal fired
            if self.data.get("phase") == "commit" and not fired:
                fired = True
                if interrupt_point == "after_commit_persist":
                    original_persist(self)
                raise KeyboardInterrupt
            return original_persist(self)

        monkeypatch.setattr(Transaction, "_persist", interrupting_persist)
    elif interrupt_point == "after_state_removal":
        def interrupting_checkpoint(self, name):
            nonlocal fired
            original_checkpoint(self, name)
            if name == "after_abandon_state_removal" and not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "checkpoint", interrupting_checkpoint)
    elif interrupt_point == "after_journal_unlink":
        def interrupting_finish(self):
            nonlocal fired
            if not fired:
                original_finish(self)
                fired = True
                raise KeyboardInterrupt
            return original_finish(self)

        monkeypatch.setattr(Transaction, "finish_commit", interrupting_finish)
    else:
        def interrupting_commit(self):
            nonlocal fired
            original_commit(self)
            if not fired:
                fired = True
                raise KeyboardInterrupt

        monkeypatch.setattr(Transaction, "commit", interrupting_commit)

    code, stdout, stderr = invoke(root, arguments)
    payload = json.loads(stdout)

    assert fired is True
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    if interrupt_point == "before_commit_persist":
        assert code == 130, stderr
        assert payload["status"] == "cancelled"
    else:
        assert code == 0, stderr
        assert payload["metadata"]["commit_durable"] is True
        assert payload["cancellation"]["status"] == "completed"
        assert payload["module"] is not None
        assert payload["dependency"] is not None
        assert payload["validation"] is not None
        assert payload["metadata"]["generation_id"]
        assert "built" in payload["metadata"]
        assert "build_duration_ms" in payload["metadata"]
        assert payload["next_action"] is not None
        assert invoke(root, ["--json", "check"])[0] == 0


def test_fresh_add_plan_conflict_removes_partial_add_and_preserves_parent_edit(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    original_execute = GenerationService.execute

    def concurrent_edit(self, plan, transaction, *, commit=True):
        package = json.loads(package_path.read_text())
        package["concurrent_user_edit"] = "keep"
        package_path.write_text(json.dumps(package, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["metadata"]["conflict_reconciled"] is True
    package = json.loads(package_path.read_text())
    assert package["concurrent_user_edit"] == "keep"
    assert "safe" not in package["dependencies"]
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    assert invoke(root, ["--json", "doctor"])[0] in {0, 1}


@pytest.mark.parametrize("target_kind", ["feature", "application", "plugin"])
def test_fresh_add_conflict_preserves_every_concurrent_source_edit_exactly(
    tmp_path: Path,
    monkeypatch,
    target_kind: str,
):
    root = plugin(tmp_path)
    assert invoke(
        root, ["add", "existing", "--starter", "cpp", "--skip-install", "--yes"]
    )[0] == 0
    if target_kind == "feature":
        target = root / "local_modules/existing/android/src/main/cpp/feature.cpp"
    elif target_kind == "application":
        target = main_application(root)
    else:
        target = root / "PluginConfig.json"
    package_path = root / "package.json"
    original_execute = GenerationService.execute
    expected = f"preserved concurrent {target_kind}\n".encode("utf-8")
    expected_mode = 0o600
    expected_times = (3_000_000_000, 4_000_000_000)

    def concurrent_edit(self, plan, transaction, *, commit=True):
        target.write_bytes(expected)
        target.chmod(expected_mode)
        os.utime(target, ns=expected_times)
        package = json.loads(package_path.read_text())
        package["conflict_trigger"] = target_kind
        package_path.write_text(json.dumps(package, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    metadata = target.lstat()

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["metadata"]["conflict_reconciled"] is True
    assert metadata.st_mode & 0o7777 == expected_mode
    assert metadata.st_atime_ns == expected_times[0]
    assert metadata.st_mtime_ns == expected_times[1]
    assert target.read_bytes() == expected
    package = json.loads(package_path.read_text())
    assert package["conflict_trigger"] == target_kind
    assert "safe" not in package["dependencies"]
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    assert invoke(root, ["--json", "doctor"])[0] in {0, 1}


def test_fresh_add_conflict_preserves_metadata_changed_after_adoption_copy(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root, ["add", "existing", "--starter", "cpp", "--skip-install", "--yes"]
    )[0] == 0
    target = root / "local_modules/existing/android/src/main/cpp/feature.cpp"
    parent = target.parent
    original_execute = GenerationService.execute
    original_copy = transaction_module._copy_verified_entry
    injected = False
    expected_file_times = (81_000_000_000, 82_000_000_000)
    expected_parent_times = (83_000_000_000, 84_000_000_000)

    def concurrent_source_edit(self, plan, transaction, *, commit=True):
        target.write_text("external source remains\n", encoding="utf-8")
        return original_execute(self, plan, transaction, commit=commit)

    def racing_copy(source, destination, *, attempts=3):
        nonlocal injected
        result = original_copy(source, destination, attempts=attempts)
        if Path(source) == target and any(
            "-adopt-" in part for part in Path(destination).parts
        ):
            if not injected:
                injected = True
                target.chmod(0o600)
                os.utime(target, ns=expected_file_times)
                parent.chmod(0o710)
                os.utime(parent, ns=expected_parent_times)
        return result

    monkeypatch.setattr(GenerationService, "execute", concurrent_source_edit)
    monkeypatch.setattr(transaction_module, "_copy_verified_entry", racing_copy)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    target_metadata = target.lstat()
    parent_metadata = parent.lstat()

    assert code == 1, (stderr, payload)
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["metadata"]["conflict_reconciled"] is True
    assert target_metadata.st_mode & 0o7777 == 0o600
    assert (target_metadata.st_atime_ns, target_metadata.st_mtime_ns) == expected_file_times
    assert parent_metadata.st_mode & 0o7777 == 0o710
    assert (parent_metadata.st_atime_ns, parent_metadata.st_mtime_ns) == expected_parent_times
    assert target.read_text(encoding="utf-8") == "external source remains\n"
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("interrupt_point", ["copy", "post_copy_verification"])
def test_single_snapshot_interrupt_is_never_retried_or_swallowed(
    tmp_path: Path,
    monkeypatch,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    call_count = 0
    if interrupt_point == "copy":
        def interrupting_copy(source, destination):
            nonlocal call_count
            call_count += 1
            raise KeyboardInterrupt

        monkeypatch.setattr(
            transaction_module, "copy_entry_no_follow", interrupting_copy
        )
    else:
        original_verified_copy = transaction_module._copy_verified_entry

        def interrupting_verification(source, destination, *, attempts=3):
            nonlocal call_count
            original_verified_copy(source, destination, attempts=attempts)
            call_count += 1
            raise KeyboardInterrupt

        monkeypatch.setattr(
            transaction_module, "_copy_verified_entry", interrupting_verification
        )

    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 130, (stderr, payload)
    assert call_count == 1
    assert payload["status"] == "cancelled"
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "completed"
    assert payload["rollback"]["status"] == "completed"
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize(
    "interrupt_point",
    [
        "after_snapshot_adoption_payload_creation",
        "after_snapshot_adoption_entry_update",
        "after_snapshot_adoption_state_authority_write",
        "after_snapshot_adoption_journal_write",
    ],
)
def test_adoption_boundary_interrupt_is_reported_after_exact_cleanup(
    tmp_path: Path,
    monkeypatch,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    assert invoke(
        root, ["add", "existing", "--starter", "cpp", "--skip-install", "--yes"]
    )[0] == 0
    target = root / "local_modules/existing/android/src/main/cpp/feature.cpp"
    original_execute = GenerationService.execute
    original_checkpoint = Transaction.checkpoint
    expected = b"external interruption state\n"
    expected_times = (93_000_000_000, 94_000_000_000)
    fired = False

    def concurrent_edit(self, plan, transaction, *, commit=True):
        target.write_bytes(expected)
        target.chmod(0o600)
        os.utime(target, ns=expected_times)
        return original_execute(self, plan, transaction, commit=commit)

    def interrupting_checkpoint(self, name):
        nonlocal fired
        original_checkpoint(self, name)
        if name == interrupt_point and not fired:
            fired = True
            raise KeyboardInterrupt

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    monkeypatch.setattr(Transaction, "checkpoint", interrupting_checkpoint)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    metadata = target.lstat()

    assert code == 1, (stderr, payload)
    assert fired is True
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "completed"
    assert payload["rollback"]["status"] == "completed"
    assert metadata.st_mode & 0o7777 == 0o600
    assert (metadata.st_atime_ns, metadata.st_mtime_ns) == expected_times
    assert target.read_bytes() == expected
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize(
    "interrupt_point",
    [
        "after_package_baseline_payload_creation",
        "after_package_baseline_entry_update",
        "after_package_baseline_state_authority_write",
        "after_package_baseline_journal_write",
    ],
)
def test_package_reconciliation_interrupt_preserves_json_and_cancellation(
    tmp_path: Path,
    monkeypatch,
    interrupt_point: str,
):
    root = plugin(tmp_path)
    package = root / "package.json"
    original_execute = GenerationService.execute
    original_checkpoint = Transaction.checkpoint
    live = (
        b'{\n\t"name":"fixture",\n\t"dependencies":'
        b'{"safe":"file:./local_modules/safe"},\n\t"external":true\n}\n'
    )
    expected = (
        b'{\n\t"name":"fixture",\n\t"dependencies":{},'
        b'\n\t"external":true\n}\n'
    )
    expected_times = (95_000_000_000, 96_000_000_000)
    fired = False

    def concurrent_package_edit(self, plan, transaction, *, commit=True):
        package.write_bytes(live)
        package.chmod(0o600)
        os.utime(package, ns=expected_times)
        return original_execute(self, plan, transaction, commit=commit)

    def interrupting_checkpoint(self, name):
        nonlocal fired
        original_checkpoint(self, name)
        if name == interrupt_point and not fired:
            fired = True
            raise KeyboardInterrupt

    monkeypatch.setattr(GenerationService, "execute", concurrent_package_edit)
    monkeypatch.setattr(Transaction, "checkpoint", interrupting_checkpoint)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    metadata = package.lstat()

    assert code == 1, (stderr, payload)
    assert fired is True
    assert payload["error"]["kind"] == "plan_conflict"
    assert payload["cancellation"]["requested"] is True
    assert payload["cancellation"]["status"] == "completed"
    assert payload["rollback"]["status"] == "completed"
    assert metadata.st_mode & 0o7777 == 0o600
    assert (metadata.st_atime_ns, metadata.st_mtime_ns) == expected_times
    assert package.read_bytes() == expected
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_fresh_add_conflict_preserves_custom_package_bytes_and_metadata_exactly(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    original_execute = GenerationService.execute
    live = (
        '{\n\t"name": "fixture",\n\t"dependencies": '
        '{"keep": "workspace:*", "safe": "file:./local_modules/safe"},\n'
        '\t"concurrent": [1, 2, 3]\n}\n'
    ).encode("utf-8")
    expected = (
        '{\n\t"name": "fixture",\n\t"dependencies": '
        '{"keep": "workspace:*"},\n\t"concurrent": [1, 2, 3]\n}\n'
    ).encode("utf-8")
    expected_times = (13_000_000_000, 14_000_000_000)

    def concurrent_edit(self, plan, transaction, *, commit=True):
        package_path.write_bytes(live)
        package_path.chmod(0o600)
        os.utime(package_path, ns=expected_times)
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    metadata = package_path.lstat()

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert package_path.read_bytes() == expected
    assert metadata.st_mode & 0o7777 == 0o600
    assert (metadata.st_atime_ns, metadata.st_mtime_ns) == expected_times
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()


@pytest.mark.parametrize(
    "original",
    [
        b'{"name":"fixture","dependencies":{"keep":"workspace:*"}}\n',
        b'{"name":"fixture","private":true}\n',
    ],
)
def test_source_only_add_conflict_restores_exact_pre_add_package_bytes(
    tmp_path: Path,
    monkeypatch,
    original: bytes,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    package_path.write_bytes(original)
    package_path.chmod(0o600)
    expected_times = (17_000_000_000, 18_000_000_000)
    os.utime(package_path, ns=expected_times)
    original_execute = GenerationService.execute

    def concurrent_source_edit(self, plan, transaction, *, commit=True):
        target = root / "local_modules/beta"
        staged = stage_feature(
            FeatureConfig(
                target,
                "beta",
                "4.0.0",
                "com.example.beta",
                "Beta",
                starters=(StarterFamily.NATIVE,),
            )
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_source_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    current = package_path.lstat()

    assert code == 1, (stderr, payload)
    assert payload["error"]["kind"] == "plan_conflict"
    assert package_path.read_bytes() == original
    assert current.st_mode & 0o7777 == 0o600
    assert (current.st_atime_ns, current.st_mtime_ns) == expected_times
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()


@pytest.mark.parametrize(
    "ambiguous",
    [
        b'{"name":"fixture","dependencies":{"safe":"file:./local_modules/safe"},"dependencies":{}}\n',
        b'{"name":"fixture","dependencies":{"safe":"file:./local_modules/safe","safe":"other"}}\n',
        b'{"name":"fixture","dependencies":{"safe":"other","safe":"file:./local_modules/safe"}}\n',
    ],
)
def test_ambiguous_dependency_conflict_retains_recovery_authority(
    tmp_path: Path,
    monkeypatch,
    ambiguous: bytes,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    original_execute = GenerationService.execute

    def ambiguous_edit(self, plan, transaction, *, commit=True):
        package_path.write_bytes(ambiguous)
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", ambiguous_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 3, stderr
    assert payload["status"] == "partial"
    assert package_path.read_bytes() == ambiguous
    assert (root / ".supernote-module-transaction.json").exists()
    assert (root / "local_modules/safe").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX entry-kind regression fixture")
@pytest.mark.parametrize("replacement_kind", ["missing", "symlink", "directory", "fifo"])
def test_fresh_add_conflict_preserves_non_regular_package_state_without_following(
    tmp_path: Path,
    monkeypatch,
    replacement_kind: str,
):
    root = plugin(tmp_path)
    package_path = root / "package.json"
    outside = tmp_path / "outside-package.json"
    outside.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
    original_execute = GenerationService.execute

    def replace_package(self, plan, transaction, *, commit=True):
        package_path.unlink()
        if replacement_kind == "symlink":
            package_path.symlink_to(outside)
        elif replacement_kind == "directory":
            package_path.mkdir()
            (package_path / "sentinel").write_text("directory remains\n")
        elif replacement_kind == "fifo":
            os.mkfifo(package_path)
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", replace_package)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 1, (stderr, payload)
    assert payload["error"]["kind"] == "plan_conflict"
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))
    if replacement_kind == "missing":
        assert entry_kind(package_path) is None
    elif replacement_kind == "symlink":
        assert entry_kind(package_path) == "symlink"
        assert os.readlink(package_path) == str(outside)
        assert outside.read_text(encoding="utf-8") == '{"sentinel":"unchanged"}\n'
    elif replacement_kind == "directory":
        assert entry_kind(package_path) == "directory"
        assert (package_path / "sentinel").read_text() == "directory remains\n"
    else:
        assert entry_kind(package_path) == "other"


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is a POSIX regression fixture")
@pytest.mark.parametrize("scoped", [False, True])
def test_fresh_add_conflict_does_not_enter_external_feature_build_cache(
    tmp_path: Path,
    monkeypatch,
    scoped: bool,
):
    root = plugin(tmp_path)
    npm_name = "@scope/beta" if scoped else "beta"
    destination = (
        root / "local_modules/@scope/beta"
        if scoped
        else root / "local_modules/beta"
    )
    original_execute = GenerationService.execute

    def concurrent_feature(self, plan, transaction, *, commit=True):
        staged = stage_feature(
            FeatureConfig(
                destination,
                npm_name,
                "4.0.0",
                "com.example.beta",
                "Beta",
                starters=(StarterFamily.NATIVE,),
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        cache = destination / "android/build"
        cache.mkdir(parents=True)
        os.mkfifo(cache / "worker.pipe")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_feature)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert destination.is_dir()
    assert (destination / "android/build/worker.pipe").exists()
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX link identity test")
def test_fresh_add_conflict_preserves_new_tree_link_deletion_and_parent_metadata(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    parent = root / "android/app"
    deleted = root / "PluginConfig.json"
    created = parent / "concurrent/nested"
    link = created / "relative-link"
    package_path = root / "package.json"
    original_execute = GenerationService.execute
    parent_times = (15_000_000_000, 16_000_000_000)
    created_times = (17_000_000_000, 18_000_000_000)

    def concurrent_edit(self, plan, transaction, *, commit=True):
        deleted.unlink()
        created.mkdir(parents=True)
        (created / "value.txt").write_text("external\n", encoding="utf-8")
        os.symlink("value.txt", link)
        created.chmod(0o700)
        os.utime(created, ns=created_times)
        parent.chmod(0o750)
        os.utime(parent, ns=parent_times)
        package = json.loads(package_path.read_text())
        package["concurrent_tree"] = True
        package_path.write_text(json.dumps(package, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    code, stdout, stderr = invoke(
        root,
        ["--json", "add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    payload = json.loads(stdout)
    parent_metadata = parent.lstat()
    created_metadata = created.lstat()

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert not deleted.exists()
    assert (created / "value.txt").read_text(encoding="utf-8") == "external\n"
    assert link.is_symlink()
    assert os.readlink(link) == "value.txt"
    assert parent_metadata.st_mode & 0o7777 == 0o750
    assert (parent_metadata.st_atime_ns, parent_metadata.st_mtime_ns) == parent_times
    assert created_metadata.st_mode & 0o7777 == 0o700
    assert (created_metadata.st_atime_ns, created_metadata.st_mtime_ns) == created_times
    assert not (root / "local_modules/safe").exists()
    assert not (root / ".supernote-module-transaction.json").exists()


def test_targeted_dependency_conflict_preserves_concurrent_parent_edit(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root, ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"]
    )[0] == 0
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"].pop("safe")
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    original_execute = GenerationService.execute

    def concurrent_edit(self, plan, transaction, *, commit=True):
        package = json.loads(package_path.read_text())
        package["concurrent_user_edit"] = "keep"
        package_path.write_text(json.dumps(package, indent=2) + "\n")
        return original_execute(self, plan, transaction, commit=commit)

    monkeypatch.setattr(GenerationService, "execute", concurrent_edit)
    code, stdout, stderr = invoke(
        root, ["--json", "update", "safe", "--skip-install", "--yes"]
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    after = json.loads(package_path.read_text())
    assert after["concurrent_user_edit"] == "keep"
    assert "safe" not in after["dependencies"]
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_targeted_noop_rejects_semantic_source_race(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root, ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"]
    )[0] == 0
    source = root / "local_modules/safe/android/src/main/cpp/feature.cpp"
    original_plan = GenerationService.plan

    def racing_plan(self, **kwargs):
        plan = original_plan(self, **kwargs)
        source.write_text(source.read_text().replace("greet(", "greetAgain("))
        return plan

    monkeypatch.setattr(GenerationService, "plan", racing_plan)
    code, stdout, stderr = invoke(
        root, ["--json", "update", "safe", "--skip-install", "--yes"]
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert "greetAgain(" in source.read_text()


def test_add_rejects_stale_v2_generated_runtime_without_mutation(tmp_path: Path):
    root = plugin(tmp_path)
    legacy_runtime = root / "android/.supernote-module/v2-runtime"
    legacy_runtime.mkdir(parents=True)
    (legacy_runtime / "generated-proof.txt").write_text("v2\n", encoding="utf-8")
    settings = root / "android/settings.gradle"
    app_build = root / "android/app/build.gradle"
    settings.write_text(
        settings.read_text()
        + "// supernote-module-v2-runtime\nlegacy settings\n"
        "// end supernote-module-v2-runtime\n"
        "include ':user-library'\n",
        encoding="utf-8",
    )
    app_build.write_text(
        app_build.read_text()
        + "// supernote-module-v2-runtime\nlegacy dependency\n"
        "// end supernote-module-v2-runtime\n"
        "dependencies { implementation project(':user-library') }\n",
        encoding="utf-8",
    )
    before = inventory_project(root)

    code, _, stderr = invoke(
        root, ["add", "typed", "--starter", "cpp", "--skip-install", "--yes"]
    )

    assert code == 1
    assert "Unsupported legacy" in stderr
    assert "does not migrate" in stderr
    assert inventory_project(root) == before
    assert legacy_runtime.is_dir()
    assert not (root / "android/.supernote-module/v4-runtime").exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fake npm; byte-decoding behavior has a platform-neutral subprocess test",
)
def test_non_utf8_dependency_failure_is_structured_and_restores_exact_parents(
    tmp_path: Path, monkeypatch
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    root = plugin(plugin_root, npm_lock=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    node = tools / "node"
    npm = tools / "npm"
    npm_state = tools / "npm-state"
    node.write_text("#!/bin/sh\necho v20.0.0\n", encoding="utf-8")
    npm.write_text(
        f"#!/bin/sh\nif [ -f {str(npm_state)!r} ]; then exit 0; fi\n"
        f"touch {str(npm_state)!r}\n"
        "printf 'valid diagnostic\\n\\377invalid diagnostic\\n' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    node.chmod(0o755)
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])

    code, stdout, stderr = invoke(
        root,
        [
            "--json",
            "add",
            "broken",
            "--starter",
            "cpp",
            "--package-manager",
            "npm",
            "--yes",
        ],
    )

    payload = json.loads(stdout)
    assert code == 1
    assert stderr == ""
    assert payload["error"]["kind"] == "install_dependency_failed"
    assert payload["error"]["phase"] == "install_dependency"
    assert payload["error"]["subprocess"]["exit_code"] == 1
    assert "valid diagnostic" in payload["error"]["subprocess"]["relevant_lines"]
    assert not (root / "local_modules").exists()
    assert not (root / "android/.supernote-module").exists()


def test_remove_dependency_failure_restores_feature_runtime_and_parent(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path, npm_lock=True)
    assert invoke(
        root,
        ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    feature = root / "local_modules/safe"
    runtime_before = (root / "android/.supernote-module/v4-runtime/feature-registry.json").read_bytes()

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
        root / "android/.supernote-module/v4-runtime/feature-registry.json"
    ).read_bytes() == runtime_before
    assert json.loads((root / "package.json").read_text())["dependencies"]["safe"]


@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_dependency_refresh_exactly_restores_main_application(
    tmp_path: Path, monkeypatch, command: str, interrupted: bool
):
    root = plugin(tmp_path, npm_lock=True)
    application = main_application(root)
    if command == "remove":
        assert invoke(
            root,
            ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
        )[0] == 0
    before = application.read_bytes()

    monkeypatch.setattr(
        FeatureCliOperationService, "_health_check_manager", lambda *args: None
    )

    def fail_dependency(self, invocation, *, phase):
        if interrupted:
            raise KeyboardInterrupt
        raise SubprocessFailure("forced install failure", phase=phase)

    monkeypatch.setattr(FeatureCliOperationService, "_run", fail_dependency)
    monkeypatch.setattr(FeatureCliOperationService, "_reconcile", lambda *args: True)
    arguments = (
        ["add", "safe", "--starter", "cpp", "--package-manager", "npm", "--yes"]
        if command == "add"
        else ["remove", "safe", "--package-manager", "npm", "--yes"]
    )

    code, _, stderr = invoke(root, arguments)

    assert code == (130 if interrupted else 1), stderr
    assert application.read_bytes() == before
    expected_marker_count = 0 if command == "add" else 1
    assert application.read_text().count("supernote-module-v4-package") == (
        expected_marker_count * 2
    )
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_partial_then_startup_recovery_preserves_restored_main_application(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    application = main_application(root)
    before = application.read_bytes()
    service = FeatureCliOperationService(root, renderer=None)  # type: ignore[arg-type]

    transaction = Transaction(root, "add", ["safe"])
    service._snapshot_operation(transaction, [root / "local_modules/safe"])
    set_runtime_wiring(root, enabled=True)
    transaction.mark_external(["npm", "install"])

    first = transaction.rollback(reconcile=lambda _: False)
    assert first.status == "partial"
    assert application.read_bytes() == before
    assert (root / ".supernote-module-transaction.json").is_file()

    outcome = recover_pending(root, reconcile=lambda _: True)

    assert outcome.rollback.status == "completed"
    assert application.read_bytes() == before


def test_empty_validation_rejects_unmanifested_v4_runtime_and_package_wiring(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    application = main_application(root)
    set_runtime_wiring(root, enabled=True)
    before = {
        path: path.read_bytes()
        for path in (
            root / "android/settings.gradle",
            root / "android/app/build.gradle",
            application,
        )
    }

    code, _, stderr = invoke(root, ["validate", "--all"])

    assert code == 1
    assert "without a schema-4 integrity manifest" in stderr
    assert "cannot prove ownership" in stderr
    assert {path: path.read_bytes() for path in before} == before
    assert application.read_text().count("supernote-module-v4-package") == 2


def test_empty_validation_rejects_unmanifested_package_registration_alone(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    application = main_application(root)
    settings = (root / "android/settings.gradle").read_bytes()
    app_build = (root / "android/app/build.gradle").read_bytes()
    set_runtime_wiring(root, enabled=True)
    (root / "android/settings.gradle").write_bytes(settings)
    (root / "android/app/build.gradle").write_bytes(app_build)
    before = application.read_bytes()

    code, _, stderr = invoke(root, ["validate", "--all"])

    assert code == 1
    assert "without a schema-4 integrity manifest" in stderr
    assert "cannot prove ownership" in stderr
    assert application.read_bytes() == before
    assert application.read_text().count("supernote-module-v4-package") == 2


def test_feature_validation_rejects_missing_main_application_registration(
    tmp_path: Path, make_directory_symlink
):
    root = plugin(tmp_path)
    application = main_application(root)
    assert invoke(
        root,
        ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    settings = (root / "android/settings.gradle").read_bytes()
    app_build = (root / "android/app/build.gradle").read_bytes()
    set_runtime_wiring(root, enabled=False)
    (root / "android/settings.gradle").write_bytes(settings)
    (root / "android/app/build.gradle").write_bytes(app_build)
    link = root / "node_modules/safe"
    link.parent.mkdir()
    make_directory_symlink(link, root / "local_modules/safe")

    code, _, stderr = invoke(root, ["validate", "safe"])

    assert code == 1
    assert "SNV4_WIRING_INVALID" in stderr
    assert "expected 1 start/end pair" in stderr
    assert "supernote-module-v4-package" not in application.read_text()


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
    assert not (root / "android/.supernote-module/v4-runtime").exists()


def test_remove_json_exposes_the_complete_v4_plan_and_actual_changes(tmp_path: Path):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0

    code, stdout, stderr = invoke(
        root, ["--json", "remove", "safe", "--skip-install", "--yes"]
    )
    payload = json.loads(stdout)

    assert code == 0, stderr
    assert payload["requested_targets"] == ["safe"]
    assert set(payload["affected_targets"]) >= {
        "safe",
        "shared runtime",
        "plugin wiring",
        "integrity manifest",
    }
    assert payload["validation"]["structural"] == "passed"
    assert payload["metadata"]["generation_id"]
    changes = {
        (
            Path(item["path"]).relative_to(root).as_posix(),
            item["action"],
            item["ownership"],
        )
        for item in payload["changes"]
    }
    assert ("local_modules/safe", "delete", "feature_implementation") in changes
    assert (
        "android/.supernote-module/v4-runtime",
        "delete",
        "plugin_runtime",
    ) in changes
    assert ("package.json", "update", "parent_dependency") in changes
    assert any(scope == "plugin_wiring" for _path, _action, scope in changes)
    assert payload["actual_changes"] == payload["changes"]
    assert not (root / "local_modules/safe").exists()
    assert not (root / "android/.supernote-module/v4-runtime").exists()
    assert invoke(root, ["--json", "check"])[0] == 0


def test_remove_plan_conflict_preserves_external_remaining_feature_edit(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    for name in ("alpha", "beta"):
        assert invoke(
            root,
            ["add", name, "--starter", "cpp", "--skip-install", "--yes"],
        )[0] == 0
    external = root / "local_modules/beta/android/src/main/cpp/feature.cpp"
    original_plan = GenerationService.plan

    def racing_plan(self, **kwargs):
        plan = original_plan(self, **kwargs)
        if kwargs.get("operation") == "remove":
            external.write_text(
                external.read_text().replace("greet(", "concurrentEdit(")
            )
        return plan

    monkeypatch.setattr(GenerationService, "plan", racing_plan)
    code, stdout, stderr = invoke(
        root, ["--json", "remove", "alpha", "--skip-install", "--yes"]
    )
    payload = json.loads(stdout)

    assert code == 1, stderr
    assert payload["error"]["kind"] == "plan_conflict"
    assert "concurrentEdit(" in external.read_text()
    assert (root / "local_modules/alpha").is_dir()
    assert not (root / ".supernote-module-transaction.json").exists()
    assert invoke(root, ["--json", "check"])[0] == 1


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
    gradle = gradle_wrapper_path(root)
    if os.name == "nt":
        gradle.write_text(
            "@echo off\r\n"
            'if "%1"=="--version" exit /b 0\r\n'
            'if "%1"==":supernote-v4-runtime:generateSupernoteDebugSemantics" exit /b 0\r\n'
            'if "%1"==":app:assembleDebug" exit /b 0\r\n'
            "exit /b 1\r\n",
            encoding="utf-8",
        )
    else:
        gradle.write_text(
            '#!/bin/sh\ncase "$1" in\n  --version|:supernote-v4-runtime:generateSupernoteDebugSemantics|:app:assembleDebug) exit 0 ;;\n  *) exit 1 ;;\nesac\n',
            encoding="utf-8",
        )
        gradle.chmod(0o755)

    code, stdout, stderr = invoke(
        root,
        ["add", "built", "--starter", "cpp", "--skip-install", "--build", "--yes"],
    )

    assert code == 0, stderr
    assert 'Added and built feature "built"' in stdout


def test_add_rejects_local_modules_symlink_that_escapes_plugin_root(
    tmp_path: Path, make_directory_symlink
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    root = plugin(plugin_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_symlink(root / "local_modules", outside)

    code, _, stderr = invoke(
        root,
        ["add", "escape", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 2
    assert "target resolves outside the Supernote plugin" in stderr
    assert not (outside / "escape").exists()


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after_planning",
        "after_staging",
        "after_first_file_replacement",
        "after_wiring",
        "after_dependency_edit",
    ],
)
def test_add_fault_injection_restores_exact_project_inventory(
    tmp_path: Path,
    monkeypatch,
    checkpoint: str,
):
    root = plugin(tmp_path)
    before = inventory_project(root)
    original = Transaction.checkpoint

    def inject(transaction: Transaction, name: str) -> None:
        if name == checkpoint:
            raise RuntimeError(f"injected failure at {name}")
        original(transaction, name)

    monkeypatch.setattr(Transaction, "checkpoint", inject)

    code, _, stderr = invoke(
        root,
        ["add", "faulted", "--starter", "cpp", "--skip-install", "--yes"],
    )

    assert code == 1
    assert f"injected failure at {checkpoint}" in stderr
    assert inventory_project(root) == before
