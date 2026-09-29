from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.platform_tools import gradle_wrapper_path


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
        root, ["add", "document", *arguments, "--yes", "--plain"]
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
    assert (feature / ".supernote-generated/ownership.json").is_file()
    assert (root / "android/settings.gradle").read_text() == "include ':app'\n"


def test_add_and_update_render_semantic_api_without_gradle_source_writes(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    settings_before = (root / "android/settings.gradle").read_bytes()
    build_before = (root / "android/app/build.gradle").read_bytes()

    assert invoke(
        root,
        ["add", "document", "--starter", "cpp", "--yes", "--plain"],
    )[0] == 0
    assert invoke(root, ["update", "--yes", "--plain"])[0] == 0

    assert (root / "android/settings.gradle").read_bytes() == settings_before
    assert (root / "android/app/build.gradle").read_bytes() == build_before
    assert (root / ".supernote-module/manifest.json").is_file()
    assert (
        root / "local_modules/document/.supernote-generated/ownership.json"
    ).is_file()


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
            "--yes",
            "--plain",
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
            "--yes",
            "--plain",
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
            "--yes",
            "--plain",
        ],
    )[0] == 0
    feature = root / "local_modules/document"
    native = feature / "android/src/main/cpp/custom.cpp"
    native.write_text("int custom() { return 7; }\n")
    starter = feature / "android/src/main/cpp/feature.cpp"
    starter.unlink()
    java = feature / "android/src/main/java/com/example/document/Custom.java"
    java.write_text("package com.example.document; class Custom {}\n")

    code, _, stderr = invoke(root, ["update", "--yes", "--plain"])

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
        ["add", "links", "--starter", "cpp", "--yes", "--plain"],
    )[0] == 0
    feature = root / "local_modules/links"
    expected_links = _source_symlink_matrix(root, feature)

    for iteration in range(2):
        code, _, stderr = invoke(root, ["update", "--yes", "--plain"])
        assert code == 0, stderr
        source = feature / "android/src/main/cpp"
        assert {
            name: os.readlink(source / name) for name in expected_links
        } == expected_links
        assert all((source / name).is_symlink() for name in expected_links)


def test_quiet_success_is_exactly_one_line(tmp_path: Path):
    root = plugin(tmp_path)
    code, stdout, stderr = invoke(
        root,
        [
            "add",
            "quiet",
            "--starter",
            "cpp",
            "--yes",
            "--quiet",
        ],
    )
    assert code == 0, stderr
    assert stdout == 'Added feature "quiet"\n'


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
        ["add", "escape", "--starter", "cpp", "--yes", "--plain"],
    )

    assert code == 2
    assert "target resolves outside the Supernote plugin" in stderr
    assert not (outside / "escape").exists()
