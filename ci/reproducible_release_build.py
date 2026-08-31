#!/usr/bin/env python3
"""Build byte-reproducible release distributions from one exact Git commit."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from release_provenance import inspect_distributions


def _git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ("git", "-C", str(source), *arguments),
        text=True,
    ).strip()


def _source_timestamp(source: Path, commit: str) -> int:
    if _git(source, "rev-parse", "HEAD") != commit:
        raise ValueError("release source HEAD does not match the requested commit")
    if _git(source, "status", "--porcelain"):
        raise ValueError("release source checkout is not clean")
    timestamp = _git(source, "show", "-s", "--format=%ct", commit)
    if not timestamp.isdecimal() or int(timestamp) <= 0:
        raise ValueError("release source commit timestamp is not canonical")
    return int(timestamp)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _normalize_sdist(path: Path, timestamp: int) -> None:
    normalized = path.with_name(f".{path.name}.normalized")
    with tarfile.open(path, "r:gz") as source:
        members = source.getmembers()
        with normalized.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=timestamp,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target:
                    for original in members:
                        member = copy.copy(original)
                        member.mtime = timestamp
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.pax_headers = {}
                        payload = source.extractfile(original) if original.isfile() else None
                        target.addfile(member, payload)
    os.replace(normalized, path)


def _build_once(source: Path, output: Path, timestamp: int) -> None:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(timestamp)
    subprocess.run(
        (
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
        ),
        cwd=source,
        env=environment,
        check=True,
        stdout=sys.stderr,
    )
    sdists = tuple(output.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise ValueError("release build must produce exactly one source distribution")
    _normalize_sdist(sdists[0], timestamp)
    inspect_distributions(output)


def _clone(source: Path, target: Path, commit: str) -> None:
    subprocess.run(
        ("git", "clone", "--quiet", "--no-hardlinks", str(source), str(target)),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(target), "checkout", "--quiet", "--detach", commit),
        check=True,
    )
    if _git(target, "rev-parse", "HEAD") != commit:
        raise ValueError("isolated release checkout does not match the requested commit")


def _artifact_map(directory: Path) -> dict[str, tuple[str, int]]:
    return {
        path.name: (_digest(path), path.stat().st_size)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def build_reproducible_release(
    source: Path,
    output: Path,
    *,
    commit: str,
    separation_seconds: float,
) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    timestamp = _source_timestamp(source, commit)
    if output.exists():
        raise ValueError("release output directory already exists")
    if separation_seconds < 2.0:
        raise ValueError("isolated builds must be separated by at least two seconds")

    with tempfile.TemporaryDirectory(prefix="sn-module-gen-reproducible-") as temporary:
        root = Path(temporary)
        first_source = root / "source-first"
        second_source = root / "source-second"
        first_dist = root / "dist-first"
        second_dist = root / "dist-second"
        _clone(source, first_source, commit)
        _build_once(first_source, first_dist, timestamp)
        time.sleep(separation_seconds)
        _clone(source, second_source, commit)
        _build_once(second_source, second_dist, timestamp)

        first = _artifact_map(first_dist)
        second = _artifact_map(second_dist)
        if first != second:
            raise ValueError("isolated release builds are not byte-identical")
        shutil.copytree(first_dist, output)

    return {
        "source_commit": commit,
        "source_date_epoch": timestamp,
        "builds": 2,
        "separation_seconds": separation_seconds,
        "artifacts": [
            {"filename": name, "sha256": digest, "size": size}
            for name, (digest, size) in first.items()
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--separation-seconds", type=float, default=2.1)
    arguments = parser.parse_args()
    result = build_reproducible_release(
        arguments.source,
        arguments.output,
        commit=arguments.commit,
        separation_seconds=arguments.separation_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
