#!/usr/bin/env python3
"""Fail a release gate unless its public or installed result is complete."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Optional, Sequence
import xml.etree.ElementTree as ElementTree


REQUIRED_INSTALLED_TESTS = {
    "test_frozen_wheel_is_the_external_installed_generator",
    "test_external_installed_cli_runs_cpp_kotlin_java_and_mixed_author_lifecycle",
    "test_installed_publisher_packages_execute_combined_native_jvm_and_runtime",
}
REQUIRED_INSTALLED_TOOLS = ("cmake", "npm", "node", "java", "javac", "gradle")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class QualificationError(RuntimeError):
    """The required installed-generator qualification is incomplete."""


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


def _resolve_command(explicit: Optional[str], candidates: Iterable[str]) -> Path:
    choices = (explicit,) if explicit else tuple(candidates)
    for choice in choices:
        if not choice:
            continue
        resolved = shutil.which(choice)
        if resolved:
            return _absolute_command_path(Path(resolved))
        path = Path(choice)
        if path.is_file():
            return _absolute_command_path(path)
    requested = explicit or ", ".join(candidates)
    raise QualificationError(f"required command is unavailable: {requested}")


def _absolute_command_path(path: Path) -> Path:
    """Canonicalize the containing directory without changing driver argv[0]."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise QualificationError(
            f"required command directory is unavailable: {candidate.parent}"
        ) from error
    selected = parent / candidate.name
    if not selected.is_file():
        raise QualificationError(f"required command is not a file: {selected}")
    return selected


def _run_checked(arguments: Sequence[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(value) for value in arguments],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(str(value) for value in arguments)
        raise QualificationError(
            f"required command failed ({result.returncode}): {rendered}\n{result.stdout}"
        )
    return result


def _probe_cxx23_driver(cxx: Path, output: Path) -> Mapping[str, str]:
    marker = "SNMG_CXX23_LINK_OK"
    source = output / "required-cxx23.cpp"
    executable = output / ("required-cxx23.exe" if os.name == "nt" else "required-cxx23")
    source.write_text(
        "#include <iostream>\n"
        "#include <string>\n"
        "int main() {\n"
        f'  const std::string marker{{"{marker}"}};\n'
        "  std::cout << marker << '\\n';\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    _run_checked((cxx, "-std=c++23", source, "-o", executable), output)
    result = _run_checked((executable,), output)
    if result.stdout.strip() != marker:
        raise QualificationError(
            f"C++23 standard-library probe returned unexpected output: {result.stdout!r}"
        )
    return {
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "output": marker,
    }


def select_and_verify_installed_toolchain(output: Path) -> Mapping[str, object]:
    system = platform.system()
    cc = _resolve_command(os.environ.get("CC"), ("clang", "cc"))
    cxx = _resolve_command(os.environ.get("CXX"), ("clang++", "c++"))
    tools = {
        name: _resolve_command(None, (name,)) for name in REQUIRED_INSTALLED_TOOLS
    }
    ninja: Optional[Path] = None
    if system == "Windows":
        ninja = _resolve_command(None, ("ninja", "ninja.exe"))

    output.mkdir(parents=True, exist_ok=True)
    c_source = output / "required-c23.c"
    c_object = output / "required-c23.o"
    c_source.write_text(
        "#include <stddef.h>\n"
        "int main(void) { void *value = nullptr; return value != nullptr; }\n",
        encoding="utf-8",
    )
    versions = {
        "cc": _run_checked((cc, "--version"), output).stdout.splitlines()[0],
        "cxx": _run_checked((cxx, "--version"), output).stdout.splitlines()[0],
    }
    _run_checked((cc, "-std=c23", "-c", c_source, "-o", c_object), output)
    cxx_probe = _probe_cxx23_driver(cxx, output)
    for name, command in tools.items():
        arguments = (command, "-version") if name == "java" else (command, "--version")
        versions[name] = _run_checked(arguments, output).stdout.splitlines()[0]

    return {
        "status": "passed",
        "platform": system,
        "cc": str(cc),
        "cxx": str(cxx),
        "cmake_generator": "Ninja" if ninja else None,
        "ninja": str(ninja) if ninja else None,
        "tools": {name: str(command) for name, command in tools.items()},
        "versions": versions,
        "probes": {
            "c23_nullptr": hashlib.sha256(c_object.read_bytes()).hexdigest(),
            "cxx23_standard_library": cxx_probe["sha256"],
            "cxx23_output": cxx_probe["output"],
        },
    }


def _write_github_environment(path: Path, toolchain: Mapping[str, object]) -> None:
    values = {"CC": toolchain["cc"], "CXX": toolchain["cxx"]}
    if toolchain["cmake_generator"]:
        values["CMAKE_GENERATOR"] = toolchain["cmake_generator"]
    with path.open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationError(message)


def _require_digest(value: object, label: str) -> None:
    _require(isinstance(value, str) and SHA256.fullmatch(value) is not None, label)


def verify_installed_junit(path: Path) -> None:
    _require(path.is_file(), f"required JUnit evidence is missing: {path}")
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    _require(bool(cases), "JUnit evidence contains no test cases")
    by_name: dict[str, list[ElementTree.Element]] = {}
    for case in cases:
        by_name.setdefault(case.attrib.get("name", ""), []).append(case)
        _require(case.find("skipped") is None, f"required qualification skipped: {case.attrib}")
        _require(case.find("failure") is None, f"required qualification failed: {case.attrib}")
        _require(case.find("error") is None, f"required qualification errored: {case.attrib}")
    for name in REQUIRED_INSTALLED_TESTS:
        _require(
            len(by_name.get(name, [])) == 1,
            f"required test node is missing or duplicated: {name}",
        )


def _verify_installed_jvm_evidence(value: object) -> None:
    _require(isinstance(value, dict), "JVM evidence object is missing")
    _require(
        value.get("host_jvm_execution") == "Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M",
        "JVM output marker is missing",
    )
    sources = value.get("android_autolink_sources")
    _require(
        isinstance(sources, list) and bool(sources)
        and all(isinstance(item, str) and item.strip() == item and bool(item) for item in sources),
        "JVM Android autolink sources are missing or malformed",
    )
    normalized = [item.replace("\\", "/") for item in sources]
    _require(len(set(normalized)) == len(normalized), "JVM Android autolink sources are duplicated")
    parts = [item.rpartition("/node_modules/") for item in normalized]
    _require(
        all(prefix and separator for prefix, separator, _ in parts)
        and len({prefix for prefix, _, _ in parts}) == 1,
        "JVM Android autolink sources do not share one consumer root",
    )
    expected = {
        f"@q5/{name}/.supernote-generated/android/jvm"
        for name in ("native", "kotlin", "java", "mixed")
    } | {
        f"@q5/{name}/android/src/main/java" for name in ("kotlin", "java", "mixed")
    } | {"@supernote/runtime/jvm"}
    _require(
        {suffix for _, _, suffix in parts} == expected,
        "JVM Android autolink source set is incomplete or unexpected",
    )


def verify_combined_installed_evidence(
    path: Path,
    expected_wheel_sha256: Optional[str] = None,
    expected_sdist_sha256: Optional[str] = None,
) -> None:
    _require(path.is_file(), f"required combined evidence is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), "combined evidence is not a JSON object")

    installed = payload.get("installed")
    _require(isinstance(installed, dict), "installed identity marker is missing")
    _require(bool(installed.get("file")), "installed import path marker is missing")
    _require(bool(installed.get("version")), "installed version marker is missing")
    for label, expected in (
        ("wheel", expected_wheel_sha256),
        ("sdist", expected_sdist_sha256),
    ):
        identity = payload.get(label)
        _require(isinstance(identity, dict), f"{label} identity marker is missing")
        digest = identity.get("sha256")
        _require_digest(digest, f"{label} digest marker is invalid")
        if expected:
            _require(digest == expected, f"{label} digest does not match the selected artifact")

    generated_v1 = payload.get("generated_v1")
    _require(isinstance(generated_v1, dict), "generated-v1 markers are missing")
    _require(
        set(generated_v1) == {"native", "kotlin", "java", "mixed"},
        "generated-v1 feature set is incomplete",
    )
    _require(
        all(bool(value) for value in generated_v1.values()),
        "generated-v1 marker is empty",
    )
    _require(bool(payload.get("generated_v2")), "generated-v2 markers are missing")

    for label in ("native_v1", "native_v2"):
        native = payload.get(label)
        _require(isinstance(native, dict), f"{label} execution marker is missing")
        outputs = native.get("outputs")
        _require(
            isinstance(outputs, list)
            and len(outputs) >= 2
            and all(str(value).startswith("Q5_GENERATED_BINDINGS=") for value in outputs),
            f"{label} generated-binding outputs are incomplete",
        )
        _require(bool(native.get("compiler_inputs")), f"{label} compiler inputs are missing")
        _require(
            bool(native.get("linked_generated_symbols")),
            f"{label} linked symbols are missing",
        )
    sensitivity = payload["native_v2"].get("generation_guard_sensitivity")
    _require(isinstance(sensitivity, dict), "native-v2 guard sensitivity is missing")
    _require(
        sensitivity.get("authored_method_execution_detected") is True,
        "guard sensitivity did not detect authored execution",
    )
    _require(
        int(sensitivity.get("disabled_guard_sites", 0)) > 0,
        "guard sensitivity disabled no sites",
    )
    _require(
        int(sensitivity.get("expected_oracle_failure_returncode", 0)) != 0,
        "guard sensitivity control did not fail",
    )

    _verify_installed_jvm_evidence(payload.get("jvm_output"))
    _require(
        payload.get("javascript_output") == "Q5_JS_SURFACE_OK=2,12",
        "JavaScript output marker is missing",
    )
    _require(
        payload.get("runtime_output") == "Q5_RUNTIME_LIFECYCLE_OK",
        "runtime lifecycle marker is missing",
    )
    _require(
        payload.get("consumer_ksp") == "not invoked",
        "consumer KSP isolation marker is missing",
    )

    runtime_payload = payload.get("runtime_payload")
    inventory = payload.get("runtime_inventory")
    _require(
        isinstance(runtime_payload, dict) and bool(runtime_payload),
        "runtime payload markers are missing",
    )
    _require(isinstance(inventory, dict), "runtime inventory marker is missing")
    entries = inventory.get("payload")
    _require(
        isinstance(entries, list) and bool(entries),
        "runtime inventory payload is missing",
    )
    observed = {
        entry.get("path"): entry.get("sha256")
        for entry in entries
        if isinstance(entry, dict)
    }
    _require(
        observed == runtime_payload,
        "runtime inventory does not match the packaged payload",
    )

    guard = payload.get("consumer_generator")
    _require(isinstance(guard, dict), "consumer generator guard marker is missing")
    _require(
        guard.get("attempts_during_consumption") == [],
        "consumer invoked the generator",
    )
    _require(
        int(guard.get("consumer_subprocesses", 0)) > 0,
        "consumer subprocess coverage is missing",
    )
    _require(
        guard.get("probe_returncode") == 97,
        "generator guard sensitivity probe did not fire",
    )
    _require(
        guard.get("probe_attempts") == ["q5-probe"],
        "generator guard probe marker is incomplete",
    )
    _require(
        len(payload.get("tarballs", {})) == 6,
        "publisher/runtime tarball markers are incomplete",
    )
    _require(
        bool(payload.get("publisher_unavailable")),
        "publisher-unavailable marker is missing",
    )
    _require(bool(payload.get("commands")), "combined command trace is missing")


def verify_installed_qualification(junit: Path, evidence: Path) -> None:
    verify_installed_junit(junit)
    verify_combined_installed_evidence(
        evidence,
        os.environ.get("SUPERNOTE_Q5_WHEEL_SHA256"),
        os.environ.get("SUPERNOTE_Q5_SDIST_SHA256"),
    )


def _installed_main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    select = subparsers.add_parser("installed-select-toolchain")
    select.add_argument("--github-env", type=Path, required=True)
    select.add_argument("--evidence", type=Path, required=True)
    installed_verify = subparsers.add_parser("installed-verify")
    installed_verify.add_argument("--junit", type=Path, required=True)
    installed_verify.add_argument("--evidence", type=Path, required=True)
    options = parser.parse_args(arguments)

    try:
        if options.command == "installed-select-toolchain":
            with tempfile.TemporaryDirectory(prefix="snmg-toolchain-") as temporary:
                toolchain = select_and_verify_installed_toolchain(Path(temporary))
            options.evidence.write_text(
                json.dumps(toolchain, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _write_github_environment(options.github_env, toolchain)
        else:
            verify_installed_qualification(options.junit, options.evidence)
    except (
        OSError,
        TypeError,
        ValueError,
        QualificationError,
        ElementTree.ParseError,
    ) as error:
        print(f"installed-generator qualification failed: {error}")
        return 1
    return 0


def main(arguments: Optional[Sequence[str]] = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    if values and values[0].startswith("installed-"):
        return _installed_main(values)
    parser = argparse.ArgumentParser()
    parser.add_argument("expectation", choices=("update-no-op", "check-build"))
    parser.add_argument("result", type=Path)
    options = parser.parse_args(values)
    verify(options.result, options.expectation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
