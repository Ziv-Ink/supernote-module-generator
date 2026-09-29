#!/usr/bin/env python3
"""Replace the R7-F2 fixture payload with revision 2 inside one device run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

from ci.device_acceptance.materialize import _generator
from ci.device_acceptance.q5_runtime.materialize import FEATURE, PROJECT_NAME


def _run(root: Path, command: Sequence[str]) -> None:
    result = subprocess.run(command, cwd=root, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}"
        )


def switch(project: Path, generator: str, fixture_root: Path) -> dict[str, object]:
    project = project.resolve()
    if project.name != PROJECT_NAME:
        raise RuntimeError(f"fixture destination name must be {PROJECT_NAME}")
    feature = project / "local_modules" / FEATURE
    shutil.copyfile(
        fixture_root / "feature_v2.cpp",
        feature / "android/src/main/cpp/feature.cpp",
    )
    for name in ("package.json", ".supernote-module.json"):
        path = feature / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        key = "version" if name == "package.json" else "package_version"
        payload[key] = "0.1.1"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _generator(project, generator, "update", "--yes")
    _run(project, ("npm", "install", f"./local_modules/{FEATURE}", "--force"))
    shutil.copyfile(fixture_root / "App_v2.tsx", project / "App.tsx")
    shutil.copyfile(
        fixture_root / "PluginConfig.v2.json",
        project / "PluginConfig.json",
    )
    _generator(project, generator, "validate")
    _run(project, ("npm", "run", "lint", "--", "--no-cache"))
    _run(project, ("npx", "tsc", "--noEmit"))
    return {
        "schema_version": 1,
        "fixture": "q5-r7-f2-post-old-completion",
        "revision": 2,
        "project": str(project),
        "generator": str(Path(generator).resolve()),
        "feature_version": "0.1.1",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("generator")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = switch(
        arguments.project,
        arguments.generator,
        Path(__file__).resolve().parent,
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
