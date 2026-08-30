#!/usr/bin/env python3
"""Materialize tagged README snippets into the generated Android fixture."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


MARKER = re.compile(
    r"<!-- sn-module-gen-release-example: (?P<feature>[a-z0-9-]+) "
    r"(?P<path>[^ ]+) -->\n"
    r"```(?P<language>[a-z0-9+.-]+)\n(?P<source>.*?)```",
    re.DOTALL,
)
FEATURES = {"readme-cpp", "readme-jvm"}


@dataclass(frozen=True)
class ReadmeExample:
    feature: str
    path: PurePosixPath
    language: str
    source: str


def read_examples(readme: Path) -> tuple[ReadmeExample, ...]:
    examples = []
    for match in MARKER.finditer(readme.read_text(encoding="utf-8")):
        feature = match.group("feature")
        path = PurePosixPath(match.group("path"))
        if feature not in FEATURES:
            raise ValueError(f"unknown release-example feature: {feature}")
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"unsafe release-example path: {path}")
        examples.append(
            ReadmeExample(
                feature=feature,
                path=path,
                language=match.group("language"),
                source=match.group("source"),
            )
        )
    if not examples:
        raise ValueError("README contains no tagged release examples")
    identities = [(example.feature, example.path.as_posix()) for example in examples]
    if len(identities) != len(set(identities)):
        raise ValueError("README release-example destinations must be unique")
    return tuple(examples)


def materialize(readme: Path, plugin_root: Path) -> tuple[Path, ...]:
    written = []
    for example in read_examples(readme):
        feature_root = plugin_root / "local_modules" / example.feature
        if not (feature_root / ".supernote-module.json").is_file():
            raise ValueError(f"generated release-example feature is missing: {example.feature}")
        destination = feature_root.joinpath(*example.path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(example.source, encoding="utf-8")
        written.append(destination)
    return tuple(written)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("readme", type=Path)
    parser.add_argument("plugin_root", type=Path)
    arguments = parser.parse_args()
    materialize(arguments.readme.resolve(), arguments.plugin_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
