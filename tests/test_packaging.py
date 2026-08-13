from __future__ import annotations

import re
from pathlib import Path

from supernote_module_generator import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_setup_cfg_is_the_single_metadata_source():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    assert not (ROOT / "setup.py").exists()
    assert not (ROOT / "__init__.py").exists()
    assert "setuptools.build_meta" in pyproject
    assert "[project]" not in pyproject
    assert "version = attr: supernote_module_generator.__version__" in setup
    assert f'__version__ = "{__version__}"' in (
        ROOT / "src/supernote_module_generator/__init__.py"
    ).read_text(encoding="utf-8")
    assert "name = supernote-module-generator" in setup
    assert "Generate typed C/C++ and Kotlin/Java features for existing Supernote plugins" in setup
    assert "url = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "PyPI = https://pypi.org/project/supernote-module-generator/" in setup
    assert "Source = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "supernote-module = supernote_module_generator.cli:main" in setup


def test_release_license_and_manifest_are_present():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "include LICENSE" in manifest
    assert "include README.md" in manifest
    assert "include CHANGELOG.md" in manifest
    assert "include CONTRIBUTING.md" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "recursive-include maintainers *.md" in manifest
    assert "recursive-include architecture *.md" in manifest
    assert "recursive-include tests" in manifest


def test_root_readme_is_the_self_contained_pypi_description():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    assert "pip install supernote-module-generator" in readme
    assert "supernote-module doctor" in readme
    assert "## License" in readme
    assert "github.com/Ziv-Ink/supernote-module-generator" in readme
    assert "long_description = file: README.md" in setup
    assert not (ROOT / "PYPI_README.md").exists()
    assert not re.search(
        r"(?<!!)\[[^\]]+\]\((?!https?://|mailto:)[^)]+\)", readme
    )


def test_pypi_release_uses_scoped_trusted_publishing():
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@v1.14.2" in workflow
    assert "password:" not in workflow
    assert "PYPI_TOKEN" not in workflow
    assert '"${GITHUB_REF_NAME}" != "v${version}"' in workflow
    assert "Release tag ${GITHUB_REF_NAME} does not match package version" in workflow
    assert "Stable 2.0.0 is blocked" not in workflow
