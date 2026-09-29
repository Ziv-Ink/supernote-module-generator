#!/usr/bin/env python3
"""Materialize the R7-F2 v1 runtime fixture in a fresh template scaffold."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

from ci.device_acceptance.materialize import (
    _generator,
    _scaffold,
    _template_identity,
)


PROJECT_NAME = "SnmgQ5R7Auth"
PLUGIN_KEY = "snmg-q5-r7-auth-device-0909"
FEATURE = "q5-r7-final-runtime-probe"
UNAVAILABLE_FEATURE = "q5-r7-final-unavailable-probe"


def _run(root: Path, command: Sequence[str]) -> None:
    result = subprocess.run(command, cwd=root, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize(
    template_root: Path,
    project: Path,
    generator: str,
    runtime_tarball: Path,
    cmake_dir: Path,
    cmake_version: str,
    fixture_root: Path,
) -> dict[str, object]:
    template_root = template_root.resolve()
    project = project.resolve()
    runtime_tarball = runtime_tarball.resolve()
    cmake_dir = cmake_dir.resolve()
    if project.name != PROJECT_NAME:
        raise RuntimeError(f"fixture destination name must be {PROJECT_NAME}")
    if not runtime_tarball.is_file():
        raise RuntimeError(f"runtime npm artifact does not exist: {runtime_tarball}")
    cmake = cmake_dir / "bin/cmake"
    if not cmake.is_file():
        raise RuntimeError(f"selected CMake executable does not exist: {cmake}")
    origin, branch, revision = _template_identity(template_root)
    _scaffold(template_root, project, install_dependencies=True)

    _generator(
        project,
        generator,
        "add",
        FEATURE,
        "--starter",
        "cpp",
        "--starter",
        "kotlin",
        "--yes",
    )
    feature = project / "local_modules" / FEATURE
    native = feature / "android/src/main/cpp"
    jvm = feature / "android/src/main/java/com/zivink/q5runtimeprobe"
    jvm.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_root / "ProbeTypes.hpp", native / "ProbeTypes.hpp")
    shutil.copyfile(fixture_root / "feature_v1.cpp", native / "feature.cpp")
    shutil.copyfile(fixture_root / "FeatureApi.kt", jvm / "FeatureApi.kt")
    _generator(project, generator, "update", "--yes")

    _generator(
        project,
        generator,
        "add",
        UNAVAILABLE_FEATURE,
        "--starter",
        "cpp",
        "--yes",
    )
    unavailable = project / "local_modules" / UNAVAILABLE_FEATURE
    shutil.copyfile(
        fixture_root / "unavailable_probe.cpp",
        unavailable / "android/src/main/cpp/feature.cpp",
    )
    _generator(project, generator, "update", "--yes")
    fixtures = project / "fixtures"
    fixtures.mkdir()
    shutil.copyfile(
        unavailable / ".supernote-generated/index.js",
        fixtures / "unavailable-feature.js",
    )
    shutil.move(str(unavailable), str(project.parent / f"{UNAVAILABLE_FEATURE}-preserved"))
    _generator(project, generator, "update", "--yes")

    shutil.copyfile(fixture_root / "App_v1.tsx", project / "App.tsx")
    shutil.copyfile(
        fixture_root / "PluginConfig.v1.json",
        project / "PluginConfig.json",
    )
    (project / "app.json").write_text(
        json.dumps({"name": PLUGIN_KEY, "displayName": PROJECT_NAME}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(fixture_root / "settings.gradle", project / "android/settings.gradle")
    autolink = project / "fixture_modules/q5-runtime-autolink"
    shutil.copytree(fixture_root / "q5-runtime-autolink", autolink)
    local_properties = project / "android/local.properties"
    local_properties.write_text(
        local_properties.read_text(encoding="utf-8")
        + f"cmake.dir={cmake_dir}\n",
        encoding="utf-8",
    )
    gradle_properties = project / "android/gradle.properties"
    gradle_properties.write_text(
        gradle_properties.read_text(encoding="utf-8")
        + f"supernoteModuleCmakeVersion={cmake_version}\n",
        encoding="utf-8",
    )
    _run(project, ("npm", "install", str(runtime_tarball)))
    _run(project, ("npm", "install", f"./local_modules/{FEATURE}"))
    _run(project, ("npm", "install", "./fixture_modules/q5-runtime-autolink"))
    _generator(project, generator, "validate")
    _run(project, ("npm", "run", "lint", "--", "--no-cache"))
    _run(project, ("npx", "tsc", "--noEmit"))

    return {
        "schema_version": 1,
        "fixture": "q5-r7-f2-post-old-completion",
        "revision": 1,
        "project": str(project),
        "template_origin": origin,
        "template_branch": branch,
        "template_revision": revision,
        "generator": str(Path(generator).resolve()),
        "runtime_tarball": str(runtime_tarball),
        "runtime_tarball_sha256": _sha256(runtime_tarball),
        "cmake": str(cmake),
        "cmake_version": cmake_version,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("project", type=Path)
    parser.add_argument("generator")
    parser.add_argument("runtime_tarball", type=Path)
    parser.add_argument("--cmake-dir", type=Path, required=True)
    parser.add_argument("--cmake-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = materialize(
        arguments.template,
        arguments.project,
        arguments.generator,
        arguments.runtime_tarball,
        arguments.cmake_dir,
        arguments.cmake_version,
        Path(__file__).resolve().parent,
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
