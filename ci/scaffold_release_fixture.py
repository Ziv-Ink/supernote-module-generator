#!/usr/bin/env python3
"""Create the exact official-template project shape used by release CI."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


TEMPLATE_DOTFILES = {
    "_eslintrc.js": ".eslintrc.js",
    "_gitignore": ".gitignore",
    "_prettierrc.js": ".prettierrc.js",
    "_watchmanconfig": ".watchmanconfig",
}


def scaffold(template: Path, destination: Path) -> None:
    template = template.resolve()
    destination = destination.resolve()
    if not (template / "android/gradlew").is_file():
        raise ValueError(f"official template Gradle wrapper is missing: {template}")
    if not (template / "scripts/verifyPluginPackage.sh").is_file():
        raise ValueError(f"official template verification script is missing: {template}")
    if destination.exists():
        raise ValueError(f"release fixture destination already exists: {destination}")

    shutil.copytree(template, destination, symlinks=True)
    for source_name, destination_name in TEMPLATE_DOTFILES.items():
        source = destination / source_name
        if not source.is_file():
            raise ValueError(f"official template dotfile is missing: {source_name}")
        source.rename(destination / destination_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    scaffold(arguments.template, arguments.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
