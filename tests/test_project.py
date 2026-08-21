from __future__ import annotations

from pathlib import Path

import pytest

from supernote_module_generator.errors import ConfigurationError
from supernote_module_generator.project import resolve_plugin_root


def _write_project_structure(root: Path) -> None:
    (root / "android").mkdir(parents=True)
    (root / "package.json").write_text(
        '{"name":"fixture","dependencies":{"sn-plugin-lib":"^0.1.19"}}\n',
        encoding="utf-8",
    )
    (root / "android/settings.gradle").write_text(
        "include ':app'\n",
        encoding="utf-8",
    )


def test_resolves_built_plugin_with_manifest(tmp_path: Path) -> None:
    _write_project_structure(tmp_path)
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")

    assert resolve_plugin_root(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("script_name", ["buildPlugin.sh", "buildPlugin.ps1"])
def test_resolves_fresh_official_template_before_manifest_is_generated(
    tmp_path: Path,
    script_name: str,
) -> None:
    _write_project_structure(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / script_name).write_text("# template build script\n", encoding="utf-8")

    assert not (tmp_path / "PluginConfig.json").exists()
    assert resolve_plugin_root(tmp_path) == tmp_path.resolve()


def test_rejects_generic_react_native_project_without_plugin_identity(
    tmp_path: Path,
) -> None:
    _write_project_structure(tmp_path)

    with pytest.raises(ConfigurationError, match="not a Supernote plugin"):
        resolve_plugin_root(tmp_path)


def test_rejects_prebuild_marker_symlink_that_escapes_plugin(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_project_structure(project)
    external = tmp_path / "external-buildPlugin.sh"
    external.write_text("# external\n", encoding="utf-8")
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "buildPlugin.sh").symlink_to(external)

    with pytest.raises(ConfigurationError, match="target resolves outside"):
        resolve_plugin_root(project)
