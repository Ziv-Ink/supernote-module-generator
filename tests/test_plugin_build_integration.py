from pathlib import Path

import pytest

from supernote_module_generator.errors import ConfigurationError
from supernote_module_generator.plugin_build_integration import (
    set_runtime_wiring,
    verify_runtime_wiring,
)


@pytest.mark.parametrize("kotlin", [False, True])
def test_wires_one_plugin_runtime_project_idempotently(tmp_path: Path, kotlin: bool):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    suffix = ".gradle.kts" if kotlin else ".gradle"
    settings = android / f"settings{suffix}"
    app = android / "app" / f"build{suffix}"
    settings.write_text("rootProject.name = 'fixture'\n")
    app.write_text("plugins {}\n")

    set_runtime_wiring(tmp_path, enabled=True)
    first = (settings.read_text(), app.read_text())
    set_runtime_wiring(tmp_path, enabled=True)
    assert (settings.read_text(), app.read_text()) == first
    verify_runtime_wiring(tmp_path, enabled=True)
    assert first[0].count("include") == 3
    assert first[1].count("implementation") == 1

    set_runtime_wiring(tmp_path, enabled=False)
    verify_runtime_wiring(tmp_path, enabled=False)
    assert "supernote-v2-runtime" not in settings.read_text()
    assert "supernote-v2-runtime" not in app.read_text()


def test_duplicate_runtime_blocks_are_rejected(tmp_path: Path):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    block = "// supernote-module-v2-runtime\nx\n// end supernote-module-v2-runtime\n"
    (android / "settings.gradle").write_text(block + block)
    (android / "app/build.gradle").write_text("plugins {}\n")
    with pytest.raises(ConfigurationError, match="duplicate"):
        set_runtime_wiring(tmp_path, enabled=True)
