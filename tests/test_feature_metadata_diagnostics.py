from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.platform_tools import gradle_wrapper_path


def _plugin(tmp_path: Path) -> Path:
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
    return tmp_path


def _invoke(root: Path, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        arguments,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _feature(root: Path) -> Path:
    code, _, stderr = _invoke(
        root,
        ["add", "safe", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0, stderr
    return root / "local_modules/safe"


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ("{", "invalid JSON at line 1"),
        ({"schema_version": 99}, "unsupported feature manifest schema 99"),
        ({"kind": "something_else"}, "kind must be 'supernote_v3_feature'"),
        ({"public_name": None}, "public_name must be a non-empty string"),
    ],
)
def test_invalid_feature_metadata_names_file_and_reports_preflight_error(
    tmp_path: Path,
    replacement: str | dict[str, object],
    expected: str,
):
    root = _plugin(tmp_path)
    feature = _feature(root)
    metadata = feature / ".supernote-module.json"
    if isinstance(replacement, str):
        metadata.write_text(replacement, encoding="utf-8")
    else:
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value.update(replacement)
        metadata.write_text(json.dumps(value) + "\n", encoding="utf-8")

    code, _, stderr = _invoke(root, ["validate", "--all"])

    assert code == 1
    assert str(metadata) in stderr
    assert expected in stderr
    assert "Internal error" not in stderr
    assert "report the resulting traceback" not in stderr


def test_wrong_kind_json_result_is_not_silently_treated_as_no_features(tmp_path: Path):
    root = _plugin(tmp_path)
    metadata = _feature(root) / ".supernote-module.json"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["kind"] = "legacy_module"
    metadata.write_text(json.dumps(value) + "\n", encoding="utf-8")
    stdout = io.StringIO()

    code = main(
        ["--json", "validate", "--all"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        cwd=root,
    )

    result = json.loads(stdout.getvalue())
    assert code == 1
    assert result["error"]["kind"] == "invalid_metadata"
    assert result["error"]["phase"] == "preflight"
    assert str(metadata) in result["error"]["message"]


def test_escaping_managed_feature_symlink_is_rejected_without_following_it(
    tmp_path: Path, make_directory_symlink
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    root = _plugin(plugin_root)
    feature = _feature(root)
    outside = tmp_path / "outside-feature"
    feature.rename(outside)
    make_directory_symlink(feature, outside)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside stays untouched\n", encoding="utf-8")

    code, _, stderr = _invoke(root, ["update", "safe", "--skip-install", "--yes"])

    assert code == 2
    assert "target resolves outside the Supernote plugin" in stderr
    assert f"managed feature path {feature}" in stderr
    assert str(outside) in stderr
    assert sentinel.read_text(encoding="utf-8") == "outside stays untouched\n"


def test_marked_cpp_boundary_error_has_source_preflight_classification(tmp_path: Path):
    root = _plugin(tmp_path)
    feature = _feature(root)
    source = feature / "android/src/main/cpp/Safe.cpp"
    source.write_text(
        "// @SupernotePluginExport\n"
        "std::int32_t * invalid() { return nullptr; }\n",
        encoding="utf-8",
    )
    stdout = io.StringIO()

    code = main(
        ["--json", "update", "safe", "--skip-install", "--yes"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        cwd=root,
    )

    result = json.loads(stdout.getvalue())
    assert code == 1
    assert result["error"]["kind"] == "invalid_source"
    assert result["error"]["phase"] == "preflight"
    source_location = str(Path("android/src/main/cpp/Safe.cpp")) + ":2"
    assert source_location in result["error"]["message"]
    assert "raw pointers are not supported" in result["error"]["message"]
