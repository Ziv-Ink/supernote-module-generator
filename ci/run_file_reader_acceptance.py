#!/usr/bin/env python3
"""Run non-migration V4 scenarios against the pinned file_reader_test clone."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Sequence

from device_acceptance.evidence import validate_evidence
from supernote_module_generator.filesystem import source_tree_inventory


PINNED_REVISION = "9f626ed39be82b43ff74eb735d10b7de61f51508"


def _run(
    project: Path,
    command: Sequence[str],
    *,
    expect: int = 0,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=project,
        check=False,
        text=True,
        capture_output=capture,
    )
    if result.returncode != expect:
        raise RuntimeError(
            f"expected exit {expect}, got {result.returncode}: {' '.join(command)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _generator(
    project: Path,
    executable: str,
    arguments: Sequence[str],
    *,
    expect: int = 0,
) -> dict[str, object]:
    result = _run(
        project,
        (executable, "--json", *arguments),
        expect=expect,
        capture=True,
    )
    return json.loads(result.stdout)


def _add(project: Path, executable: str, name: str) -> None:
    _generator(
        project,
        executable,
        ("add", name, "--starter", "cpp", "--skip-install", "--yes"),
    )


def _write_split_object(project: Path) -> None:
    native = project / "local_modules/header-split/android/src/main/cpp"
    (native / "Counter.hpp").write_text(
        """#pragma once
// @SupernotePluginObject
class Counter {
public:
  // @SupernoteConstructor
  explicit Counter(double initial);
  // @SupernotePluginExport
  double value() const;
private:
  double value_;
};
""",
        encoding="utf-8",
    )
    (native / "feature.cpp").write_text(
        """#include "Counter.hpp"
Counter::Counter(double initial) : value_(initial) {}
double Counter::value() const { return value_; }
""",
        encoding="utf-8",
    )


def _write_header_only_object(project: Path) -> None:
    native = project / "local_modules/header-only/android/src/main/cpp"
    (native / "Counter.hpp").write_text(
        """#pragma once
// @SupernotePluginObject
class InlineCounter {
public:
  // @SupernoteConstructor
  explicit InlineCounter(double initial) : value_(initial) {}
  // @SupernotePluginExport
  double value() const { return value_; }
private:
  double value_;
};
""",
        encoding="utf-8",
    )
    (native / "feature.cpp").write_text(
        '#include "Counter.hpp"\n', encoding="utf-8"
    )


def _check_corruption(
    project: Path,
    executable: str,
    path: Path,
    content: bytes,
) -> None:
    baseline = path.read_bytes()
    path.write_bytes(content)
    failed = _generator(project, executable, ("check",), expect=1)
    validation = failed.get("validation")
    if isinstance(validation, dict) and validation.get("build") not in {
        None,
        "not_run",
        "not_requested",
    }:
        raise RuntimeError("corruption check reached Gradle")
    path.write_bytes(baseline)
    _generator(project, executable, ("check",))


def run(
    project: Path,
    executable: str,
    output: Path,
    device_evidence: Path,
    bounded_device_evidence: Path,
) -> None:
    revision = _run(
        project, ("git", "rev-parse", "HEAD"), capture=True
    ).stdout.strip()
    if revision != PINNED_REVISION:
        raise RuntimeError(f"file_reader_test revision is not pinned: {revision}")

    completed: list[str] = []

    # 7.1: clean V4 generation from the exact real-project revision.
    _add(project, executable, "auditprobe")
    _generator(project, executable, ("update", "--all", "--skip-install", "--yes"))
    completed.append("7.1-clean-v4-generation")

    # 7.2: both supported native object implementation styles.
    _add(project, executable, "header-split")
    _add(project, executable, "header-only")
    _write_split_object(project)
    _write_header_only_object(project)
    _generator(project, executable, ("update", "--all", "--skip-install", "--yes"))
    completed.append("7.2-header-and-cpp-plus-header-only")

    # 7.3: a complete marked type in .cpp is rejected before commit.
    invalid = project / "local_modules/header-only/android/src/main/cpp/feature.cpp"
    valid = invalid.read_bytes()
    invalid.write_text(
        "// @SupernotePluginObject\nclass InvalidCppOnly { public: double value() const; };\n",
        encoding="utf-8",
    )
    failed = _generator(
        project,
        executable,
        ("update", "--all", "--skip-install", "--yes"),
        expect=2,
    )
    if not failed.get("error") or failed["error"].get("phase") not in {
        "preflight",
        "frontend",
        "plan",
        "verify",
    }:
        raise RuntimeError("cpp-only rejection did not retain a precommit phase")
    invalid.write_bytes(valid)
    _generator(project, executable, ("update", "--all", "--skip-install", "--yes"))
    completed.append("7.3-invalid-cpp-only-rejected")

    # Install all local links once after the complete feature set exists.
    if not os.environ.get("SNMG_ACCEPTANCE_SKIP_NPM_INSTALL"):
        _run(project, ("npm", "install", "--ignore-scripts"))

    # 7.4: targeted update expands to the complete affected closure atomically.
    _add(project, executable, "closure-peer")
    if not os.environ.get("SNMG_ACCEPTANCE_SKIP_NPM_INSTALL"):
        _run(project, ("npm", "install", "--ignore-scripts"))
    audit_source = project / "local_modules/auditprobe/android/src/main/cpp/feature.cpp"
    audit_source.write_text(
        audit_source.read_text(encoding="utf-8").replace("greet(", "greetAgain("),
        encoding="utf-8",
    )
    closure = _generator(
        project,
        executable,
        ("update", "auditprobe", "--skip-install", "--yes"),
    )
    affected = set(closure.get("affected_targets", []))
    if not {"auditprobe", "closure-peer", "shared runtime"}.issubset(affected):
        raise RuntimeError(f"targeted closure is incomplete: {sorted(affected)}")
    _generator(project, executable, ("check",))
    completed.append("7.4-multi-feature-transitive-update")

    # 7.5: dry-run/diff is observationally exact.
    audit_source.write_text(
        audit_source.read_text(encoding="utf-8").replace(
            "greetAgain(", "greetPreview("
        ),
        encoding="utf-8",
    )
    before = source_tree_inventory(project)
    preview = _generator(
        project,
        executable,
        ("update", "auditprobe", "--dry-run", "--diff"),
    )
    if not preview.get("changes") or source_tree_inventory(project) != before:
        raise RuntimeError("dry-run/diff changed the real-project clone")
    completed.append("7.5-dry-run-diff-read-only")

    # 7.6: the second identical update is a true no-op.
    _generator(
        project,
        executable,
        ("update", "auditprobe", "--skip-install", "--yes"),
    )
    no_op = _generator(
        project,
        executable,
        ("update", "auditprobe", "--skip-install", "--yes"),
    )
    if not no_op.get("metadata", {}).get("no_op") or no_op.get("actual_changes"):
        raise RuntimeError("identical real-project update was not a true no-op")
    completed.append("7.6-identical-update-no-op")

    # 7.7: representative owned artifacts, wiring, JNI, and manifest authority.
    _check_corruption(
        project,
        executable,
        project / "local_modules/auditprobe/index.js",
        b"// corrupted JavaScript\n",
    )
    _check_corruption(
        project,
        executable,
        project / "local_modules/auditprobe/index.d.ts",
        b"export type Corrupted = never;\n",
    )
    settings = project / "android/settings.gradle"
    _check_corruption(
        project,
        executable,
        settings,
        settings.read_bytes().replace(
            b"supernote-module-v4-runtime", b"broken-v4-runtime", 1
        ),
    )
    manifest_path = project / ".supernote-module/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    jni_path = next(
        project / row["path"]
        for row in manifest["artifacts"]
        if row["path"].endswith("plugin_bindings.cpp")
    )
    _check_corruption(project, executable, jni_path, b"// corrupted JNI\n")
    tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered["artifacts"][0]["sha256"] = "0" * 64
    _check_corruption(
        project,
        executable,
        manifest_path,
        (json.dumps(tampered, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    completed.append("7.7-corruption-detected-before-gradle")

    evidence = device_evidence.read_bytes()
    for marker in (
        b"Expanded lifecycle qualification",
        b"count=32, limit=32",
        b"PluginHost force-stop/relaunch boundaries: exactly 2",
        b"final recovery canary failures: 0",
    ):
        if marker not in evidence:
            raise RuntimeError(f"device evidence is missing {marker!r}")
    completed.extend(
        ("7.9-approved-device-deployment-evidence", "7.10-same-process-reload-evidence")
    )

    cases_path = Path(__file__).resolve().parent / "device_acceptance/cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    bounded_digest = hashlib.sha256()
    for host, expected_file in (
        (
            "note",
            "/storage/emulated/0/Note/SNMG_Bounded_Acceptance/"
            "SNMG_Bounded_NOTE.note",
        ),
        ("doc", "/storage/emulated/0/Document/SNMG_Bounded_Acceptance.pdf"),
    ):
        log = (bounded_device_evidence / f"{host}-reactnative.log").read_text(
            encoding="utf-8"
        )
        normalized = validate_evidence(log, cases, host, expected_file)
        retained = json.loads(
            (bounded_device_evidence / f"{host}-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        if normalized != retained:
            raise RuntimeError(f"retained {host} device evidence is not canonical")
        bounded_digest.update(host.encode("ascii") + b"\0")
        bounded_digest.update(log.encode("utf-8") + b"\0")
        bounded_digest.update(
            json.dumps(retained, sort_keys=True).encode("utf-8") + b"\0"
        )
    completed.append("7.11-bounded-note-doc-device-evidence")

    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pinned_revision": revision,
                "scenarios": completed,
                "migration": "intentionally_out_of_scope",
                "device_evidence_sha256": hashlib.sha256(evidence).hexdigest(),
                "bounded_device_evidence_sha256": bounded_digest.hexdigest(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("generator_command")
    parser.add_argument("output", type=Path)
    parser.add_argument("device_evidence", type=Path)
    parser.add_argument("bounded_device_evidence", type=Path)
    arguments = parser.parse_args(argv)
    run(
        arguments.project.resolve(),
        arguments.generator_command,
        arguments.output,
        arguments.device_evidence,
        arguments.bounded_device_evidence,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
