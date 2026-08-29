"""Fail-closed Ruff C901 collection shared by checked-in debt ratchets."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


Finding = tuple[str, int]
MESSAGE = re.compile(r"^`(?P<name>[^`]+)` is too complex \((?P<value>\d+) > 10\)$")


def findings(root: Path, path: Path) -> tuple[Finding, ...]:
    """Return every C901 finding, preserving duplicate names and failing closed."""

    if not path.is_file():
        raise SystemExit(f"C901 target is not a regular file: {path}")
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(path),
            "--select",
            "C901",
            "--output-format",
            "json",
        ),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stderr.strip():
        raise SystemExit(f"Unexpected Ruff C901 stderr for {path.name}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid Ruff C901 JSON for {path.name}") from exc
    if not isinstance(payload, list):
        raise SystemExit(f"Unexpected Ruff C901 payload for {path.name}: {payload!r}")

    observed: list[Finding] = []
    for finding in payload:
        if not isinstance(finding, dict) or not isinstance(finding.get("message"), str):
            raise SystemExit(f"Unexpected Ruff C901 output for {path.name}: {finding!r}")
        match = MESSAGE.fullmatch(finding["message"])
        if match is None:
            raise SystemExit(f"Unexpected Ruff C901 output for {path.name}: {finding!r}")
        observed.append((match.group("name"), int(match.group("value"))))

    expected_status = 1 if observed else 0
    if result.returncode != expected_status:
        raise SystemExit(
            f"Unexpected Ruff C901 status for {path.name}: "
            f"{result.returncode}, expected {expected_status}"
        )
    return tuple(sorted(observed))


def check_expected(
    root: Path,
    package: Path,
    expected_by_file: Iterable[tuple[str, tuple[Finding, ...]]],
) -> int:
    for name, expected in expected_by_file:
        observed = findings(root, package / name)
        if observed != tuple(sorted(expected)):
            print(f"Expected {name} C901 debt {expected}, observed {observed}")
            return 1
    return 0
