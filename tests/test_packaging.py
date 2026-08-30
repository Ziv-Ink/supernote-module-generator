from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
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
    assert "name = sn-module-gen" in setup
    assert "author = Ziv-Ink" in setup
    assert "Generate typed C/C++ and Kotlin/Java features for existing Supernote plugins" in setup
    assert "url = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "PyPI = https://pypi.org/project/sn-module-gen/" in setup
    assert "Source = https://github.com/Ziv-Ink/supernote-module-generator" in setup
    assert "sn-module-gen = supernote_module_generator.cli:main" in setup
    assert "supernote-module =" not in setup


def test_clean_wheel_installs_only_the_public_console_script(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", "__pycache__", "*.pyc", "build", "dist", "*.egg-info"),
    )
    dist = tmp_path / "dist"
    subprocess.run(
        (sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)),
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = tmp_path / "environment"
    subprocess.run((sys.executable, "-m", "venv", str(environment)), check=True)
    executable = environment / "bin" / "python"
    subprocess.run(
        (str(executable), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(next(dist.glob("*.whl")))),
        check=True,
        capture_output=True,
        text=True,
    )
    script_directory = environment / "bin"
    assert (script_directory / "sn-module-gen").is_file()
    assert not (script_directory / "supernote-module").exists()
    version = subprocess.run(
        (str(script_directory / "sn-module-gen"), "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout == "sn-module-gen 0.1.0\n"


def test_release_license_and_manifest_are_present():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "include LICENSE" in manifest
    assert "include README.md" in manifest
    assert "include CHANGELOG.md" in manifest
    assert "include CONTRIBUTING.md" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "recursive-include .github *.yml" in manifest
    assert "recursive-include maintainers *" in manifest
    assert "recursive-include architecture *.md" in manifest
    assert "recursive-include ci *" in manifest
    assert "recursive-include tests *" in manifest
    assert "recursive-include src/supernote_module_generator/templates *" in manifest
    assert "recursive-include src/supernote_module_generator/schemas *.json" in manifest
    assert "schemas/*.json" in (ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert "include_package_data = False" in (ROOT / "setup.cfg").read_text(
        encoding="utf-8"
    )


def test_package_contains_only_the_active_v4_workflow_and_runtime_templates():
    package = ROOT / "src/supernote_module_generator"
    for obsolete in (
        "config.py",
        "generator.py",
        "operations.py",
        "validation.py",
        "workflows.py",
    ):
        assert not (package / obsolete).exists()

    templates = {path.name for path in (package / "templates").iterdir()}
    assert templates == {
        "v4.SupernoteConstructor.java.tmpl",
        "v4.SupernoteCoroutineBridge.kt.tmpl",
        "v4.SupernotePluginAsync.java.tmpl",
        "v4.SupernotePluginExport.java.tmpl",
        "v4.SupernotePluginInternal.java.tmpl",
        "v4.SupernoteV4Module.kt.tmpl",
        "v4.SupernoteV4Processor.kt.tmpl",
        "v4.processor.provider.tmpl",
        "v4.SupernotePluginObject.java.tmpl",
        "v4.SupernotePluginValue.java.tmpl",
    }


def test_root_readme_is_the_self_contained_pypi_description():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    assert "pip install sn-module-gen" in readme
    assert "sn-module-gen doctor" in readme
    assert "## License" in readme
    assert "github.com/Ziv-Ink/supernote-module-generator" in readme
    assert "long_description = file: README.md" in setup
    assert not (ROOT / "PYPI_README.md").exists()
    assert not re.search(
        r"(?<!!)\[[^\]]+\]\((?!https?://|mailto:)[^)]+\)", readme
    )


def test_pypi_release_uses_scoped_trusted_publishing():
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@v1.14.2" in workflow
    assert "password:" not in workflow
    assert "PYPI_TOKEN" not in workflow
    assert "uses: ./.github/workflows/quality.yml" in workflow
    assert "release_tag: ${{ github.event.release.tag_name }}" in workflow
    assert "needs: qualify" in workflow
    assert "python-package-distributions-${{ github.sha }}" in workflow
    assert "Build wheel and source distribution" not in workflow
    assert "uses: ./.github/workflows/quality.yml" in ci
    assert "ref: ${{ github.sha }}" in quality
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in quality
    assert "Release tag ${RELEASE_TAG} does not match package version" in quality
    assert "Install wheel in a clean environment" in quality
    assert "Install and smoke the source distribution" in quality
    assert "pip install --no-deps --no-build-isolation" in quality
    assert 'schemas/command-result.schema.json' in quality
    assert "Generated Android plugin" in quality
    assert "supernote-plugin-template" in quality
    assert "af3f36f6d6f61d9dbd153b0ebb444a3d3621d25f" in quality
    assert "template_launch_contract.py sync" in quality
    assert "template_launch_contract.py verify" in quality
    assert "update-no-op" in quality
    assert "check-build" in quality
    assert "npm run build" in quality
    assert "npm run verify" in quality
    assert "Stable 2.0.0 is blocked" not in workflow


def test_unpacked_sdist_contains_and_executes_release_qualification_inputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            "build",
            "dist",
            "*.egg-info",
        ),
    )
    dist = tmp_path / "dist"
    subprocess.run(
        (
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(dist),
        ),
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    archive = next(dist.glob("*.tar.gz"))
    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(unpacked)
    root = next(unpacked.iterdir())
    required = (
        ".github/workflows/quality.yml",
        "ci/fixtures/supernote-module-generator-wiki.bundle",
        "ci/fixtures/file_reader_test-9f626ed.bundle",
        "ci/device_acceptance/cases.json",
        "ci/device_acceptance/App.tsx.tmpl",
        "ci/device_acceptance/DeviceCounter.hpp",
        "ci/device_acceptance/FeatureApi.kt",
        "ci/device_acceptance/device_probe.cpp",
        "maintainers/device-evidence/v4-bounded-note-doc-2026-08-27/note-reactnative.log",
        "maintainers/device-evidence/v4-bounded-note-doc-2026-08-27/doc-evidence.json",
        "maintainers/device-evidence/v4-bounded-note-doc-2026-08-27/doc-allow-dialog.png",
    )
    assert all((root / relative).is_file() for relative in required)
    subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_device_acceptance_pack.py",
            "tests/test_release_qualification.py",
        ),
        cwd=root,
        check=True,
    )
