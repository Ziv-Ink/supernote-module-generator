#!/usr/bin/env python3
"""Validate and normalize one bounded NOTE or DOC device log."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


RESULT_MARKER = "SNMG_TEST_RESULT "
EVENT_MARKER = "SNMG_TEST_EVENT "
PERMISSION_MARKER = "SNMG_PERMISSION_REQUEST "
CURRENT_MARKERS = (RESULT_MARKER, EVENT_MARKER, PERMISSION_MARKER)
HISTORICAL_MARKERS = (
    "SNV4_TEST_RESULT ",
    "SNV4_TEST_EVENT ",
    "SNV4_PERMISSION_REQUEST ",
)
HISTORICAL_SUITE = "v4-bounded-note-doc-final"


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


def _marker_family(log: str) -> tuple[tuple[str, str, str], list[dict[str, Any]]]:
    current_results = _payloads(log, CURRENT_MARKERS[0])
    historical_results = _payloads(log, HISTORICAL_MARKERS[0])
    if len(current_results) + len(historical_results) != 1:
        raise ValueError(
            "expected one terminal result across current and historical marker families"
        )
    if current_results:
        selected, rejected, results = CURRENT_MARKERS, HISTORICAL_MARKERS, current_results
    else:
        selected, rejected, results = HISTORICAL_MARKERS, CURRENT_MARKERS, historical_results
    if any(_payloads(log, marker) for marker in rejected):
        raise ValueError("mixed current and historical marker families")
    return selected, results


def validate_evidence(
    log: str,
    cases: dict[str, Any],
    host: str,
    expected_file: str,
) -> dict[str, Any]:
    if host not in {"note", "doc"}:
        raise ValueError("host must be note or doc")
    expected_checks = [item["id"] for item in cases["checks"]]
    markers, results = _marker_family(log)
    current_family = markers == CURRENT_MARKERS
    result = results[0]
    expected_suite = cases["suite"] if current_family else HISTORICAL_SUITE
    expected_schema: object = "1.0" if current_family else 1
    if result.get("schema") != expected_schema:
        raise ValueError("terminal result schema mismatch")
    if result.get("suite") != expected_suite or result.get("host") != host:
        raise ValueError("terminal result suite/host mismatch")
    if result.get("status") != "pass":
        raise ValueError("terminal result did not pass")
    checks = result.get("checks")
    if not isinstance(checks, list):
        raise ValueError("terminal checks are missing")
    ids = [item.get("id") for item in checks if isinstance(item, dict)]
    if ids != expected_checks or any(item.get("status") != "pass" for item in checks):
        raise ValueError("terminal checks are incomplete, reordered, or failed")

    events = _payloads(log, markers[1])
    event_ids = [item.get("id") for item in events]
    if event_ids != expected_checks:
        raise ValueError("event sequence does not match the source-backed cases")
    requests = _payloads(log, markers[2])
    expected_host = cases["hosts"][host]
    expected_request = {
        "schema": expected_schema,
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
        "schema_version": expected_schema,
        "suite": expected_suite,
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
