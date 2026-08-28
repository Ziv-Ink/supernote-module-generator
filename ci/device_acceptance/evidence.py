#!/usr/bin/env python3
"""Validate and normalize one bounded NOTE or DOC device log."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


RESULT_MARKER = "SNV4_TEST_RESULT "
EVENT_MARKER = "SNV4_TEST_EVENT "
PERMISSION_MARKER = "SNV4_PERMISSION_REQUEST "


def _payloads(log: str, marker: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in log.splitlines():
        if marker not in line:
            continue
        value = json.loads(line.split(marker, 1)[1])
        if not isinstance(value, dict):
            raise ValueError(f"{marker.strip()} payload must be an object")
        payloads.append(value)
    return payloads


def validate_evidence(
    log: str,
    cases: dict[str, Any],
    host: str,
    expected_file: str,
) -> dict[str, Any]:
    if host not in {"note", "doc"}:
        raise ValueError("host must be note or doc")
    expected_checks = [item["id"] for item in cases["checks"]]
    results = _payloads(log, RESULT_MARKER)
    if len(results) != 1:
        raise ValueError(f"expected one terminal result, found {len(results)}")
    result = results[0]
    if result.get("suite") != cases["suite"] or result.get("host") != host:
        raise ValueError("terminal result suite/host mismatch")
    if result.get("status") != "pass":
        raise ValueError("terminal result did not pass")
    checks = result.get("checks")
    if not isinstance(checks, list):
        raise ValueError("terminal checks are missing")
    ids = [item.get("id") for item in checks if isinstance(item, dict)]
    if ids != expected_checks or any(item.get("status") != "pass" for item in checks):
        raise ValueError("terminal checks are incomplete, reordered, or failed")

    events = _payloads(log, EVENT_MARKER)
    event_ids = [item.get("id") for item in events]
    if event_ids != expected_checks:
        raise ValueError("event sequence does not match the source-backed cases")
    requests = _payloads(log, PERMISSION_MARKER)
    expected_host = cases["hosts"][host]
    expected_request = {
        "schema": 1,
        "host": host,
        "permission": expected_host["permission"],
        "action": expected_host["permission_action"],
    }
    if requests != [expected_request]:
        raise ValueError("permission request marker mismatch")

    by_id = {item["id"]: item for item in checks}
    current_file = by_id["current-file"].get("actual")
    if current_file != expected_file:
        raise ValueError(f"current file mismatch: {current_file!r}")
    android = by_id["android-build-info"].get("actual")
    if android != {"model": "Supernote Nomad", "sdk": 30}:
        raise ValueError(f"unexpected Android evidence: {android!r}")
    permission = by_id["permission-status-request-result"].get("actual")
    expected_permission = {
        "before": 0,
        "requested": 0 if host == "note" else 1,
        "after": 0 if host == "note" else 1,
        "permission": expected_host["permission"],
        "action": expected_host["permission_action"],
    }
    if permission != expected_permission:
        raise ValueError(f"permission outcome mismatch: {permission!r}")

    return {
        "schema_version": 1,
        "suite": cases["suite"],
        "host": host,
        "plugin_name": result.get("pluginName"),
        "status": "pass",
        "check_count": len(checks),
        "check_ids": ids,
        "current_file": current_file,
        "permission": permission,
        "android": android,
        "log_sha256": hashlib.sha256(log.encode("utf-8")).hexdigest(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("cases", type=Path)
    parser.add_argument("host", choices=("note", "doc"))
    parser.add_argument("expected_file")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = validate_evidence(
        arguments.log.read_text(encoding="utf-8"),
        json.loads(arguments.cases.read_text(encoding="utf-8")),
        arguments.host,
        arguments.expected_file,
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
