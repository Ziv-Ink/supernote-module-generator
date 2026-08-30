#!/usr/bin/env python3
"""Fail a release gate unless a public JSON result has the expected outcome."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("command result must be a JSON object")
    if value.get("schema_version") != "1.0":
        raise ValueError("command result does not use schema version 1.0")
    return value


def verify(path: Path, expectation: str) -> None:
    value = _read_result(path)
    if value.get("status") != "success" or value.get("exit_code") != 0:
        raise ValueError(f"command did not succeed: {value.get('error')}")
    if expectation == "update-no-op":
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("no_op") is not True:
            raise ValueError("update did not report a canonical no-op")
        if value.get("changes") != [] or value.get("actual_changes") != []:
            raise ValueError("no-op update reported planned or actual changes")
    elif expectation == "check-build":
        validation = value.get("validation")
        if not isinstance(validation, dict) or validation.get("build") != "passed":
            raise ValueError("check did not report a passed Android build")
        if value.get("issues") != []:
            raise ValueError("check reported public validation issues")
    else:
        raise ValueError(f"unknown command-result expectation: {expectation}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("expectation", choices=("update-no-op", "check-build"))
    parser.add_argument("result", type=Path)
    arguments = parser.parse_args()
    verify(arguments.result, arguments.expectation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
