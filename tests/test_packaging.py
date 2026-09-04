from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
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
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        (str(executable), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(next(dist.glob("*.whl")))),
        check=True,
        capture_output=True,
        text=True,
    )
    script_directory = Path(
        subprocess.check_output(
            (
                str(executable),
                "-c",
                "import sysconfig; print(sysconfig.get_path('scripts'))",
            ),
            text=True,
        ).strip()
    )
    scoped_path = str(script_directory)
    public_launcher = shutil.which("sn-module-gen", path=scoped_path)
    assert public_launcher is not None
    assert shutil.which("supernote-module", path=scoped_path) is None
    assert not (script_directory / "supernote-module.exe").exists()
    assert any(
        path.name == "sn-module-gen" or path.name.startswith("sn-module-gen.")
        for path in script_directory.iterdir()
    )
    assert not any(
        path.name == "supernote-module" or path.name.startswith("supernote-module.")
        for path in script_directory.iterdir()
    )
    version = subprocess.run(
        (public_launcher, "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout == "sn-module-gen 0.1.2\n"


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


def test_package_contains_only_the_active_workflow_and_runtime_templates():
    package = ROOT / "src/supernote_module_generator"
    for obsolete in (
        "config.py",
        "generator.py",
        "operations.py",
            "workflows.py",
    ):
        assert not (package / obsolete).exists()
    assert not (package / "v4_validation.py").exists()
    assert (package / "validation.py").is_file()

    templates = {path.name for path in (package / "templates").iterdir()}
    assert templates == {
        "runtime.SupernoteConstructor.java.tmpl",
        "runtime.SupernoteCoroutineBridge.kt.tmpl",
        "runtime.SupernotePluginAsync.java.tmpl",
        "runtime.SupernotePluginExport.java.tmpl",
        "runtime.SupernotePluginInternal.java.tmpl",
        "runtime.SupernoteModule.kt.tmpl",
        "runtime.SupernoteModuleProcessor.kt.tmpl",
        "runtime.processor.provider.tmpl",
        "runtime.SupernotePluginObject.java.tmpl",
        "runtime.SupernotePluginValue.java.tmpl",
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
    release_requirements = (ROOT / "ci/release-requirements.txt").read_text(
        encoding="utf-8"
    )

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "permissions: {}" in workflow
    assert "Require the exact stable release tag" in workflow
    assert 'test "$RELEASE_TAG" = "v0.1.2"' in workflow
    assert 'test "$RELEASE_PRERELEASE" = "false"' in workflow
    assert "name: pypi" in workflow
    assert "url: https://pypi.org/project/sn-module-gen/" in workflow
    assert "https://pypi.org/project/supernote-module-generator/" not in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@v1.14.2" in workflow
    assert "password:" not in workflow
    assert "PYPI_TOKEN" not in workflow
    assert "uses: ./.github/workflows/quality.yml" in workflow
    assert "release_tag: ${{ github.event.release.tag_name }}" in workflow
    assert "needs: qualify" in workflow
    assert "python-package-distributions-${{ github.sha }}" in workflow
    assert "python-package-provenance-${{ github.sha }}" in workflow
    assert "release_provenance.py verify" in workflow
    assert "Refuse to replace an existing release asset" in workflow
    assert "gh release view" in workflow
    assert "--json assets" in workflow
    assert "release_asset_preflight.py" in workflow
    assert "gh release upload" in workflow
    assert workflow.index("release_asset_preflight.py") < workflow.index("gh release upload")
    for forbidden in (
        "--clobber",
        "--skip-existing",
        "gh release delete-asset",
        "gh api",
        "actions/github-script",
        "--method DELETE",
        "-X DELETE",
    ):
        assert forbidden not in workflow
    assert "provenance/SHA256SUMS" in workflow
    assert "provenance/release-provenance.json" in workflow
    assert "Build wheel and source distribution" not in workflow
    assert "uses: ./.github/workflows/quality.yml" in ci
    assert "ref: ${{ github.sha }}" in quality
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in quality
    assert "Release tag ${RELEASE_TAG} does not match package version" in quality
    assert "EXPECTED_RELEASE_TAG: v0.1.2" in quality
    assert "release_provenance.py record" in quality
    assert "reproducible_release_build.py" in quality
    assert "Build byte-reproducible wheel and source distribution twice" in quality
    assert "pip install -r ci/release-requirements.txt" in quality
    assert release_requirements.splitlines() == [
        "build==1.4.4",
        "packaging==26.3",
        "pyproject-hooks==1.2.0",
        "setuptools==84.0.0",
        "twine==6.2.0",
        "wheel==0.48.0",
    ]
    assert "python -m build\n" not in quality
    assert "ci/release_provenance.py" in quality
    assert "python-package-provenance-${{ github.sha }}" in quality
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


def _release_asset_preflight(
    tmp_path: Path, assets: list[dict[str, object]]
) -> subprocess.CompletedProcess[str]:
    dist = tmp_path / "dist"
    provenance = tmp_path / "provenance"
    dist.mkdir()
    provenance.mkdir()
    (dist / "sn_module_gen-0.1.2-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "sn_module_gen-0.1.2.tar.gz").write_bytes(b"sdist")
    (provenance / "SHA256SUMS").write_text("checksums\n", encoding="utf-8")
    (provenance / "release-provenance.json").write_text("{}\n", encoding="utf-8")
    inventory = tmp_path / "release.json"
    inventory.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return subprocess.run(
        (
            sys.executable,
            str(ROOT / "ci/release_asset_preflight.py"),
            str(inventory),
            str(dist),
            str(provenance),
        ),
        capture_output=True,
        text=True,
    )


def test_release_asset_preflight_allows_a_release_without_target_assets(
    tmp_path: Path,
) -> None:
    result = _release_asset_preflight(
        tmp_path,
        [{"name": "unrelated.txt", "digest": "sha256:unrelated"}],
    )

    assert result.returncode == 0, result.stderr


def test_release_asset_preflight_rejects_one_existing_mismatched_asset(
    tmp_path: Path,
) -> None:
    result = _release_asset_preflight(
        tmp_path,
        [
            {
                "name": "sn_module_gen-0.1.2-py3-none-any.whl",
                "digest": "sha256:different-build-bytes",
            }
        ],
    )

    assert result.returncode != 0
    assert "release already contains target assets" in result.stderr
    assert "refusing to replace or publish" in result.stderr


def test_release_provenance_records_and_reverifies_built_distributions(
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
        (sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist)),
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    provenance = tmp_path / "provenance"
    commit = "a" * 40
    command = (
        sys.executable,
        str(source / "ci/release_provenance.py"),
        "record",
        str(dist),
        str(provenance),
        "--repository",
        "Ziv-Ink/supernote-module-generator",
        "--commit",
        commit,
    )
    subprocess.run(command, cwd=source, check=True)

    manifest = json.loads(
        (provenance / "release-provenance.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "1.0"
    assert manifest["repository"] == "Ziv-Ink/supernote-module-generator"
    assert manifest["source_commit"] == commit
    assert manifest["distribution"] == "sn-module-gen"
    assert manifest["version"] == "0.1.2"
    assert manifest["release_tag"] == "v0.1.2"
    assert {artifact["filename"] for artifact in manifest["artifacts"]} == {
        "sn_module_gen-0.1.2-py3-none-any.whl",
        "sn_module_gen-0.1.2.tar.gz",
    }
    for artifact in manifest["artifacts"]:
        path = dist / artifact["filename"]
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert artifact["size"] == path.stat().st_size

    subprocess.run(
        (
            sys.executable,
            str(source / "ci/release_provenance.py"),
            "verify",
            str(dist),
            str(provenance),
            "--repository",
            "Ziv-Ink/supernote-module-generator",
            "--commit",
            commit,
        ),
        cwd=source,
        check=True,
    )
    (provenance / "SHA256SUMS").write_text("tampered\n", encoding="utf-8")
    failed = subprocess.run(
        (
            sys.executable,
            str(source / "ci/release_provenance.py"),
            "verify",
            str(dist),
            str(provenance),
            "--repository",
            "Ziv-Ink/supernote-module-generator",
            "--commit",
            commit,
        ),
        cwd=source,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "checksum manifest does not match" in failed.stderr


def test_release_build_is_byte_reproducible_across_isolated_wall_clock_builds(
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
    subprocess.run(("git", "init", "--quiet", str(source)), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.name", "Release Test"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "release-test@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    commit_environment = os.environ.copy()
    commit_environment["GIT_AUTHOR_DATE"] = "2026-08-31T00:00:00Z"
    commit_environment["GIT_COMMITTER_DATE"] = "2026-08-31T00:00:00Z"
    subprocess.run(
        ("git", "-C", str(source), "commit", "--quiet", "-m", "release source"),
        check=True,
        env=commit_environment,
    )
    commit = subprocess.check_output(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        text=True,
    ).strip()
    dist = tmp_path / "dist"
    started = time.monotonic()
    result = subprocess.run(
        (
            sys.executable,
            str(source / "ci/reproducible_release_build.py"),
            "--source",
            str(source),
            "--output",
            str(dist),
            "--commit",
            commit,
            "--separation-seconds",
            "2.0",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert time.monotonic() - started >= 2.0
    evidence = json.loads(result.stdout)
    assert evidence["source_commit"] == commit
    assert evidence["source_date_epoch"] == 1788134400
    assert evidence["builds"] == 2
    assert evidence["separation_seconds"] == 2.0
    assert {artifact["filename"] for artifact in evidence["artifacts"]} == {
        "sn_module_gen-0.1.2-py3-none-any.whl",
        "sn_module_gen-0.1.2.tar.gz",
    }
    for artifact in evidence["artifacts"]:
        package = dist / artifact["filename"]
        assert hashlib.sha256(package.read_bytes()).hexdigest() == artifact["sha256"]
        assert package.stat().st_size == artifact["size"]

    epoch = evidence["source_date_epoch"]
    expected_zip_time = datetime.fromtimestamp(epoch, timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(next(dist.glob("*.whl"))) as wheel_archive:
        assert {entry.date_time for entry in wheel_archive.infolist()} == {
            expected_zip_time
        }
    with tarfile.open(next(dist.glob("*.tar.gz")), "r:gz") as sdist_archive:
        members = sdist_archive.getmembers()
        assert {member.mtime for member in members} == {epoch}
        assert {member.uid for member in members} == {0}
        assert {member.gid for member in members} == {0}
        assert {member.uname for member in members} == {""}
        assert {member.gname for member in members} == {""}

    subprocess.run(
        (sys.executable, "-m", "twine", "check", *map(str, sorted(dist.iterdir()))),
        check=True,
    )
    provenance = tmp_path / "provenance"
    provenance_command = (
        sys.executable,
        str(source / "ci/release_provenance.py"),
        "record",
        str(dist),
        str(provenance),
        "--repository",
        "Ziv-Ink/supernote-module-generator",
        "--commit",
        commit,
    )
    subprocess.run(provenance_command, cwd=source, check=True)
    subprocess.run(
        (
            sys.executable,
            str(source / "ci/release_provenance.py"),
            "verify",
            str(dist),
            str(provenance),
            "--repository",
            "Ziv-Ink/supernote-module-generator",
            "--commit",
            commit,
        ),
        cwd=source,
        check=True,
    )

    environment = tmp_path / "environment"
    subprocess.run((sys.executable, "-m", "venv", str(environment)), check=True)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    launcher = scripts / ("sn-module-gen.exe" if os.name == "nt" else "sn-module-gen")
    wheel = next(dist.glob("*.whl"))
    subprocess.run(
        (str(python), "-m", "pip", "install", "--no-deps", str(wheel)),
        check=True,
    )
    version = subprocess.run(
        (str(launcher), "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout == "sn-module-gen 0.1.2\n"
    subprocess.run((str(launcher), "--help"), check=True, capture_output=True, text=True)

    (source / "untracked-release-input").write_text("dirty\n", encoding="utf-8")
    dirty_output = tmp_path / "dirty-output"
    dirty = subprocess.run(
        (
            sys.executable,
            str(source / "ci/reproducible_release_build.py"),
            "--source",
            str(source),
            "--output",
            str(dirty_output),
            "--commit",
            commit,
        ),
        capture_output=True,
        text=True,
    )
    assert dirty.returncode != 0
    assert "release source checkout is not clean" in dirty.stderr
    assert not dirty_output.exists()


def test_patch_release_notes_and_old_distribution_retirement_are_bounded():
    notes = (ROOT / "maintainers/release-notes-v0.1.2.md").read_text(
        encoding="utf-8"
    )
    previous_notes = (ROOT / "maintainers/release-notes-v0.1.1.md").read_text(
        encoding="utf-8"
    )
    initial_notes = (ROOT / "maintainers/release-notes-v0.1.0.md").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "maintainers/releasing.md").read_text(encoding="utf-8")
    normalized_notes = " ".join(notes.split())

    assert "filesystem" in notes
    assert "false concurrent-modification" in normalized_notes
    assert "Files, directories, and symlinks" in normalized_notes
    assert "NAME_MAX=143" in notes
    assert "atomic source replacement" in normalized_notes
    assert "APFS" in notes
    assert "ext4" in notes
    assert "eCryptFS" in notes
    assert "SNMG_NATIVE_RESULT:Hello, SNMG_NATIVE_260905_04301" in notes
    assert "Native Windows" in notes
    assert "same-plugin reload" in notes
    assert "fresh-process" in notes
    assert "without changing the generated JavaScript" in normalized_notes
    assert "remains `0.1.0`" in previous_notes
    assert "first public release" in initial_notes
    assert "does not provide migration or compatibility" in " ".join(
        initial_notes.split()
    )
    assert "--notes-file maintainers/release-notes-v0.1.2.md" in guide
    assert 'git tag --annotate v0.1.2 "$RELEASE_SHA"' in guide
    assert "gh release create v0.1.2 --verify-tag" in guide
    assert "complete APFS/ext4 installed-CLI workflows" in " ".join(
        guide.split()
    )
    assert "only after `sn-module-gen==0.1.0` installs" in guide
    assert "Pre-public development package; replaced by sn-module-gen" in guide
    assert "Do not delete the project" in guide
    assert "do not upload a redirect package" in guide
    for topic in (
        "sn-module-gen",
        "supernote",
        "code-generator",
        "python",
        "android",
        "cpp",
        "kotlin",
        "jni",
        "jsi",
        "react-native",
        "pypi",
    ):
        assert f"`{topic}`" in guide


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
        "maintainers/device-evidence/README.md",
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
