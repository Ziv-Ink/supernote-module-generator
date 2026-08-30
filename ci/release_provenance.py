#!/usr/bin/env python3
"""Record and verify the exact public release distributions."""

from __future__ import annotations

import argparse
from email import policy
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import tarfile
import zipfile

DISTRIBUTION = "sn-module-gen"
NORMALIZED_DISTRIBUTION = "sn_module_gen"
REPOSITORY = "Ziv-Ink/supernote-module-generator"
ENTRY_POINT = "sn-module-gen = supernote_module_generator.cli:main"


def _version() -> str:
    source = (
        Path(__file__).resolve().parents[1]
        / "src/supernote_module_generator/__init__.py"
    ).read_text(encoding="utf-8")
    matches = re.findall(
        r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$',
        source,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("package version source is not canonical")
    return matches[0]


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _metadata(text: str, *, source: str) -> None:
    version = _version()
    message = Parser(policy=policy.default).parsestr(text)
    if message["Name"] != DISTRIBUTION:
        raise ValueError(f"{source} distribution name is not {DISTRIBUTION}")
    if message["Version"] != version:
        raise ValueError(f"{source} version is not {version}")
    project_urls = set(message.get_all("Project-URL", []))
    expected_url = f"PyPI, https://pypi.org/project/{DISTRIBUTION}/"
    if expected_url not in project_urls:
        raise ValueError(f"{source} does not contain the canonical PyPI project URL")


def _inspect_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise ValueError("wheel must contain one metadata file and one entry-point file")
        _metadata(
            archive.read(metadata_names[0]).decode("utf-8"),
            source=path.name,
        )
        entry_points = archive.read(entry_names[0]).decode("utf-8")
    if ENTRY_POINT not in entry_points:
        raise ValueError("wheel does not expose the public sn-module-gen entry point")
    if re.search(r"^supernote-module\s*=", entry_points, flags=re.MULTILINE):
        raise ValueError("wheel exposes the forbidden pre-public console entry point")


def _inspect_sdist(path: Path) -> None:
    expected_root = f"{NORMALIZED_DISTRIBUTION}-{_version()}/"
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        if not names or any(
            name != expected_root.rstrip("/") and not name.startswith(expected_root)
            for name in names
        ):
            raise ValueError("source distribution has an unexpected archive root")
        metadata_names = [name for name in names if name == f"{expected_root}PKG-INFO"]
        entry_names = [name for name in names if name.endswith(".egg-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise ValueError("source distribution must contain package metadata and entry points")
        metadata_stream = archive.extractfile(metadata_names[0])
        entry_stream = archive.extractfile(entry_names[0])
        if metadata_stream is None or entry_stream is None:
            raise ValueError("source distribution metadata could not be read")
        _metadata(metadata_stream.read().decode("utf-8"), source=path.name)
        entry_points = entry_stream.read().decode("utf-8")
    if ENTRY_POINT not in entry_points:
        raise ValueError("source distribution does not expose sn-module-gen")
    if re.search(r"^supernote-module\s*=", entry_points, flags=re.MULTILINE):
        raise ValueError("source distribution exposes the forbidden pre-public entry point")


def inspect_distributions(directory: Path) -> tuple[dict[str, object], ...]:
    version = _version()
    wheel = directory / f"{NORMALIZED_DISTRIBUTION}-{version}-py3-none-any.whl"
    sdist = directory / f"{NORMALIZED_DISTRIBUTION}-{version}.tar.gz"
    actual = sorted(path.name for path in directory.iterdir() if path.is_file())
    expected = sorted((wheel.name, sdist.name))
    if actual != expected:
        raise ValueError(f"release directory must contain exactly {expected}; found {actual}")
    _inspect_wheel(wheel)
    _inspect_sdist(sdist)
    return tuple(
        {
            "filename": path.name,
            "sha256": _digest(path),
            "size": path.stat().st_size,
        }
        for path in (wheel, sdist)
    )


def _validate_identity(repository: str, commit: str) -> None:
    if repository != REPOSITORY:
        raise ValueError(f"release repository must be {REPOSITORY}")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("release source commit must be a full lowercase Git SHA")


def record(directory: Path, output: Path, *, repository: str, commit: str) -> None:
    _validate_identity(repository, commit)
    artifacts = inspect_distributions(directory)
    output.mkdir(parents=True, exist_ok=False)
    provenance = {
        "schema_version": "1.0",
        "repository": repository,
        "source_commit": commit,
        "distribution": DISTRIBUTION,
        "version": _version(),
        "release_tag": f"v{_version()}",
        "artifacts": artifacts,
    }
    (output / "release-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{artifact['sha256']}  {artifact['filename']}\n" for artifact in artifacts),
        encoding="utf-8",
    )


def verify(directory: Path, provenance_dir: Path, *, repository: str, commit: str) -> None:
    _validate_identity(repository, commit)
    artifacts = inspect_distributions(directory)
    expected = {
        "schema_version": "1.0",
        "repository": repository,
        "source_commit": commit,
        "distribution": DISTRIBUTION,
        "version": _version(),
        "release_tag": f"v{_version()}",
        "artifacts": list(artifacts),
    }
    provenance = json.loads(
        (provenance_dir / "release-provenance.json").read_text(encoding="utf-8")
    )
    if provenance != expected:
        raise ValueError("release provenance does not match the qualified distributions")
    checksums = "".join(
        f"{artifact['sha256']}  {artifact['filename']}\n" for artifact in artifacts
    )
    if (provenance_dir / "SHA256SUMS").read_text(encoding="utf-8") != checksums:
        raise ValueError("release checksum manifest does not match the qualified distributions")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "verify"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
        command.add_argument("provenance", type=Path)
        command.add_argument("--repository", required=True)
        command.add_argument("--commit", required=True)
    arguments = parser.parse_args()
    if arguments.command == "record":
        record(
            arguments.directory,
            arguments.provenance,
            repository=arguments.repository,
            commit=arguments.commit,
        )
    else:
        verify(
            arguments.directory,
            arguments.provenance,
            repository=arguments.repository,
            commit=arguments.commit,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
