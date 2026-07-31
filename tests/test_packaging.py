from __future__ import annotations

import re
from pathlib import Path

from supernote_module_generator import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_setup_cfg_is_the_single_metadata_source():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    assert not (ROOT / "setup.py").exists()
    assert "setuptools.build_meta" in pyproject
    assert "[project]" not in pyproject
    assert "version = attr: supernote_module_generator.__version__" in setup
    assert f'__version__ = "{__version__}"' in (
        ROOT / "src/supernote_module_generator/__init__.py"
    ).read_text(encoding="utf-8")
    assert "name = supernote-module-generator" in setup
    assert "url = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "PyPI = https://pypi.org/project/supernote-module-generator/" in setup
    assert "Source = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "supernote-module = supernote_module_generator.cli:main" in setup


def test_release_license_and_manifest_are_present():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "include LICENSE" in manifest
    assert "include PYPI_README.md" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "recursive-include tests" in manifest


def test_pypi_readme_is_self_contained():
    readme = (ROOT / "PYPI_README.md").read_text(encoding="utf-8")

    assert "pip install supernote-module-generator" in readme
    assert "supernote-module doctor" in readme
    assert "MIT License" in readme
    assert "github.com/Ziv-Ink/supernote-module-generator" in readme
    assert not re.search(r"(?<!!)\[[^\]]+\]\((?!https?://|mailto:)[^)]+\)", readme)
