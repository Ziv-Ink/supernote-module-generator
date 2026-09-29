from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENVIRONMENT = (
    "SUPERNOTE_Q5_MODULE_COMMAND",
    "SUPERNOTE_Q5_PYTHON",
    "SUPERNOTE_Q5_WHEEL",
    "SUPERNOTE_Q5_WHEEL_SHA256",
    "SUPERNOTE_Q5_SDIST",
    "SUPERNOTE_Q5_SDIST_SHA256",
    "SUPERNOTE_Q5_RUNTIME_ROOT",
)
pytestmark = pytest.mark.skipif(
    any(os.environ.get(name) is None for name in REQUIRED_ENVIRONMENT),
    reason="set the frozen Q5 installed-artifact environment",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = Path(os.environ["SUPERNOTE_Q5_MODULE_COMMAND"])
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(command), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_frozen_wheel_is_the_external_installed_generator() -> None:
    command = Path(os.environ["SUPERNOTE_Q5_MODULE_COMMAND"])
    python = Path(os.environ["SUPERNOTE_Q5_PYTHON"])
    wheel = Path(os.environ["SUPERNOTE_Q5_WHEEL"])
    sdist = Path(os.environ["SUPERNOTE_Q5_SDIST"])
    runtime = Path(os.environ["SUPERNOTE_Q5_RUNTIME_ROOT"])
    for path in (command, python, wheel, sdist, runtime):
        assert path.is_absolute()
        assert path.exists()
    assert command.is_file()
    assert python.is_file()
    assert _sha256(wheel) == os.environ["SUPERNOTE_Q5_WHEEL_SHA256"]
    assert _sha256(sdist) == os.environ["SUPERNOTE_Q5_SDIST_SHA256"]

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    version = subprocess.run(
        [str(command), "--version"],
        capture_output=True,
        text=True,
        check=False,
        cwd=wheel.parent,
        env=environment,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout == "sn-module-gen 0.1.3\n"
    help_result = subprocess.run(
        [str(command), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=wheel.parent,
        env=environment,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "sn-module-gen update --yes" in help_result.stdout
    assert "sn-module-gen remove" not in help_result.stdout

    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json,supernote_module_generator as m;"
                "print(json.dumps({'file':m.__file__,'version':m.__version__}))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=wheel.parent,
        env=environment,
    )
    assert probe.returncode == 0, probe.stderr
    identity = json.loads(probe.stdout)
    installed_module = Path(identity["file"]).resolve()
    assert identity["version"] == "0.1.3"
    assert installed_module.is_relative_to(python.parent.parent.resolve())
    assert not installed_module.is_relative_to(ROOT)
    assert (installed_module.parent / "node/resolve-packages.js").is_file()

    package = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "@supernote/runtime"
    assert (runtime / "resolve-packages.js").is_file()
    with tarfile.open(sdist, "r:gz") as archive:
        names = {name.split("/", 1)[-1] for name in archive.getnames()}
    assert "npm/supernote-runtime/resolve-packages.js" in names
    assert "tests/test_q5_installed_qualification.py" in names


def test_external_installed_cli_runs_cpp_kotlin_java_and_mixed_author_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "q5-installed-author"
    if os.name == "nt":
        target_length = 135
        prefix = "q5-installed-author-"
        padding = max(16, target_length - len(str(tmp_path)) - len(prefix) - 1)
        root = tmp_path / (prefix + "x" * padding)
    _write(root / "PluginConfig.json", "{}\n")
    package = '{"name":"q5-installed-author","private":true,"dependencies":{}}\n'
    _write(root / "package.json", package)
    _write(root / "android/settings.gradle", "include ':app'\n")
    _write(root / "android/app/build.gradle", "plugins {}\n")

    additions = (
        (
            "@q5/cpp",
            "Cpp",
            "com.q5.cpp",
            ("cpp",),
        ),
        (
            "@q5/kotlin",
            "KotlinFeature",
            "com.q5.kotlin_feature",
            ("kotlin",),
        ),
        (
            "@q5/java",
            "JavaFeature",
            "com.q5.java_feature",
            ("kotlin",),
        ),
        (
            "@q5/mixed",
            "Mixed",
            "com.q5.mixed",
            ("cpp", "kotlin"),
        ),
    )
    for name, public_name, namespace, starters in additions:
        arguments = [
            "add",
            name,
            "--javascript-name",
            public_name,
            "--android-namespace",
            namespace,
        ]
        for starter in starters:
            arguments.extend(("--starter", starter))
        arguments.append("--yes")
        added = _run_cli(root, *arguments)
        assert added.returncode == 0, added.stderr or added.stdout

    java_root = root / "local_modules/@q5/java/android/src/main/java/com/q5/java_feature"
    kotlin_starter = java_root / "FeatureApi.kt"
    kotlin_starter.unlink()
    java_source = java_root / "FeatureApi.java"
    java_text = """package com.q5.java_feature;
import supernote.generated.annotations.SupernotePluginExport;
public final class FeatureApi {
  @SupernotePluginExport
  public static String greet(String name) { return "Java " + name; }
}
"""
    _write(java_source, java_text)

    updated = _run_cli(root, "update", "--yes")
    assert updated.returncode == 0, updated.stderr or updated.stdout
    validated = _run_cli(root, "validate")
    assert validated.returncode == 0, validated.stderr or validated.stdout
    assert (root / "package.json").read_text(encoding="utf-8") == package
    assert java_source.read_text(encoding="utf-8") == java_text
    assert not kotlin_starter.exists()

    if os.name == "nt":
        manifest_root = (
            root
            / "android/.supernote-module/runtime/analysis/subject/build/generated/"
            "ksp/main/resources/supernote/generated/manifests"
        )
        manifests = sorted(manifest_root.glob("*.json"))
        assert len(str(manifest_root)) < 260
        assert manifests
        assert all(len(str(path)) > 260 for path in manifests)
        assert all(not str(path).startswith("\\\\?\\") for path in manifests)

    for package_name in ("cpp", "kotlin", "java", "mixed"):
        feature = root / f"local_modules/@q5/{package_name}"
        assert (feature / ".supernote-generated/package-manifest.json").is_file()
        assert (feature / ".supernote-generated/android/registration.json").is_file()
    assert not (
        root / "local_modules/@q5/cpp/.supernote-generated/android/jvm_feature.cpp"
    ).exists()
    assert (
        root / "local_modules/@q5/java/.supernote-generated/android/jvm_feature.cpp"
    ).is_file()
    assert (
        root / "local_modules/@q5/mixed/.supernote-generated/android/feature.cpp"
    ).is_file()
    assert (
        root / "local_modules/@q5/mixed/.supernote-generated/android/jvm_feature.cpp"
    ).is_file()
