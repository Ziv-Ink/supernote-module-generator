from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

import ci.verify_command_result as qualification_gate
import ci.run_wiki_acceptance as wiki_acceptance
from supernote_module_generator import __version__
from ci.materialize_readme_examples import materialize, read_examples
from ci.run_wiki_acceptance import (
    END,
    START,
    audit_commands,
    read_commands,
    scan_documented_commands,
)
from ci.verify_command_result import verify
from supernote_module_generator.binding_codegen import scan_cpp_semantic_model
from supernote_module_generator.feature_generator import FeatureConfig, stage_feature
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.errors import ConfigurationError
from supernote_module_generator.helptext import COMMAND_HELP


ROOT = Path(__file__).resolve().parents[1]


def _stage_release_feature(
    plugin_root: Path,
    name: str,
    namespace: str,
    starter: StarterFamily,
) -> Path:
    staged = stage_feature(
        FeatureConfig(
            output=plugin_root / "local_modules" / name,
            npm_name=name,
            package_version="0.1.0",
            android_namespace=namespace,
            public_name="ReadmeCpp" if name == "readme-cpp" else "ReadmeJvm",
            starters=(starter,),
        )
    )
    destination = plugin_root / "local_modules" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged, destination)
    return destination


def test_release_readme_examples_are_complete_generated_sources(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    cpp = _stage_release_feature(
        plugin, "readme-cpp", "com.example.readme_cpp", StarterFamily.NATIVE
    )
    jvm = _stage_release_feature(
        plugin, "readme-jvm", "com.example.readme_jvm", StarterFamily.JVM
    )

    written = materialize(ROOT / "README.md", plugin)
    examples = read_examples(ROOT / "README.md")

    assert len(examples) == 3
    assert {example.language for example in examples} == {"cpp", "kotlin"}
    assert {path.relative_to(plugin).as_posix() for path in written} == {
        "local_modules/readme-cpp/android/src/main/cpp/feature.cpp",
        "local_modules/readme-cpp/android/src/main/cpp/FeatureTypes.hpp",
        (
            "local_modules/readme-jvm/android/src/main/java/"
            "com/example/readme_jvm/FeatureApi.kt"
        ),
    }
    assert "fun pageCount(): Int = 42" in (
        jvm / "android/src/main/java/com/example/readme_jvm/FeatureApi.kt"
    ).read_text(encoding="utf-8")

    semantic = scan_cpp_semantic_model(cpp)
    assert {declaration.name for declaration in semantic.declarations} == {
        "Point",
        "Stroke",
    }
    assert {function.name for function in semantic.functions} == {
        "loadPage",
        "pageCount",
        "rebuildIndex",
    }


def test_release_readme_cpp_examples_pass_host_syntax_check(tmp_path: Path) -> None:
    compiler = (
        shutil.which("c++")
        or shutil.which("clang++")
        or shutil.which("g++")
    )
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    plugin = tmp_path / "plugin"
    cpp = _stage_release_feature(
        plugin, "readme-cpp", "com.example.readme_cpp", StarterFamily.NATIVE
    )
    _stage_release_feature(
        plugin, "readme-jvm", "com.example.readme_jvm", StarterFamily.JVM
    )
    materialize(ROOT / "README.md", plugin)
    check = cpp / "android/src/main/cpp/readme_header_check.cpp"
    check.write_text('#include "FeatureTypes.hpp"\n', encoding="utf-8")

    subprocess.run(
        [
            compiler,
            "-std=c++23",
            "-fsyntax-only",
            str(cpp / "android/src/main/cpp/feature.cpp"),
            str(check),
        ],
        check=True,
        cwd=cpp,
    )


@pytest.mark.parametrize(
    ("expectation", "value"),
    [
        (
            "update-no-op",
            {
                "schema_version": "1.0",
                "status": "success",
                "exit_code": 0,
                "metadata": {"no_op": True},
                "changes": [],
                "actual_changes": [],
            },
        ),
        (
            "check-build",
            {
                "schema_version": "1.0",
                "status": "success",
                "exit_code": 0,
                "validation": {"build": "passed"},
                "issues": [],
            },
        ),
    ],
)
def test_release_result_verifier_accepts_only_green_contracts(
    tmp_path: Path, expectation: str, value: dict[str, object]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(value), encoding="utf-8")
    verify(result, expectation)

    value["status"] = "failure"
    result.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="did not succeed"):
        verify(result, expectation)


def test_release_result_verifier_rejects_pre_public_schema(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": "4.0",
                "status": "success",
                "exit_code": 0,
                "metadata": {"no_op": True},
                "changes": [],
                "actual_changes": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema version 1.0"):
        verify(result, "update-no-op")


def test_device_canary_evidence_is_scoped_and_complete() -> None:
    evidence = (
        ROOT / "maintainers/device-evidence/v4-device-canary-2026-08-27.md"
    ).read_text(encoding="utf-8")

    for required in (
        "SN100C10004301",
        "eng.supern.20260824.133801",
        "PluginHost version: `1.00.2608211`",
        "Reload cycles: 25 passed",
        "Unique semantic generation IDs: 25",
        "Same-process PluginHost PID for all reloads: `10699`",
        "PluginHost PID after the authorized force-stop/relaunch boundary: `22473`",
        "`SNV4_RESTART_REQUIRED` observations: 0",
        "V4 canary failures, fatal signals, or V4 loader failures: 0",
        "did not clear PluginHost",
        "device evidence, not a general",
        "Post-canary launch-only scope note",
        "corrected harness was immediately rerun",
        "Expanded lifecycle qualification",
        "distinct HelloWorld package updates prepared and exercised: 33",
        "SNV4_PENDING_PROMISE_STARTED generation=1",
        "SNV4_LONG_ASYNC_NATIVE_START generation=1",
        "SNV4_LONG_ASYNC_NATIVE_END",
        "Neither raw log contains `SNV4_STALE_COMPLETION`",
        "count=32, limit=32",
        "expected `SNV4_RESTART_REQUIRED` observations: 1",
        "PluginHost force-stop/relaunch boundaries: exactly 2",
        "PID `25803`",
        "PID `5343`",
        "final recovery canary failures: 0",
        "a1e40a81b7dd59dd9bbf515cd9457eeb0b1e5a535691623a401de705062cbebc",
    ):
        assert required in evidence


def test_release_version_has_a_dated_changelog_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## {__version__} - " in changelog


def test_reusable_release_gate_covers_platforms_compileall_and_coverage() -> None:
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    release_requirements = (ROOT / "ci/release-requirements.txt").read_text(
        encoding="utf-8"
    )
    platform_paths = (ROOT / "tests/test_platform_paths.py").read_text(
        encoding="utf-8"
    )

    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in quality
    assert "tests/test_platform_tools.py" in quality
    assert "tests/test_operation_lock.py" in quality
    assert "tests/test_regression_harness.py" in quality
    assert "tests/test_platform_paths.py" in quality
    assert "python -m compileall -q src tests ci" in quality
    assert "--data-file=.coverage.linux-complete" in quality
    assert "path: .coverage.linux-complete" in quality
    assert "--data-file=.coverage.platform-${{ runner.os }}" in quality
    assert "pattern: coverage-*" in quality
    assert "python -m coverage combine coverage-data" in quality
    assert "python -m coverage report" in quality
    assert "needs: [test, coverage, platform]" in quality
    assert 'python-version: ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]' in quality
    assert "coverage[toml]>=7.6,<8" in setup
    assert "relative_files = True" in setup
    assert "fail_under = 82.03" in setup
    assert "precision = 2" in setup
    assert "tests/test_regression_harness.py" in quality
    assert "pip install -r ci/release-requirements.txt" in quality
    assert "setuptools==84.0.0" in release_requirements
    assert "wheel==0.48.0" in release_requirements
    assert "--no-build-isolation" in quality
    assert "test_windows_junction_is_never_traversed_or_observed" in platform_paths
    assert (
        "test_windows_copy_preserves_exact_supported_file_and_directory_metadata"
        in platform_paths
    )
    assert "test_windows_contained_classifier_retains_ancestor_against_junction_swap" in platform_paths
    assert "test_windows_symlink_target_read_retains_identity_across_aba_attempt" in platform_paths
    assert "test_windows_atime_neutralization_preserves_concurrent_mtime" in platform_paths


def test_release_gate_is_generator_only_and_uses_installed_artifacts() -> None:
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    runtime_codegen = (
        ROOT / "src/supernote_module_generator/plugin_runtime_codegen.py"
    ).read_text(encoding="utf-8")

    assert "SUPERNOTE_MODULE_COMMAND" not in runtime_codegen
    assert '"SUPERNOTE_MODULE_COMMAND": command' in quality
    assert 'cmake: ["3.24.4", current]' in quality
    assert "tests/test_q5_installed_qualification.py" in quality
    assert "tests/test_q5_combined_installed_acceptance.py" in quality
    assert "installed-select-toolchain" in quality
    assert "SUPERNOTE_REQUIRE_INSTALLED_QUALIFICATION" in quality
    assert "verify_command_result.py installed-verify" in quality
    assert "generator-toolchain.json" in quality
    assert "Retain generator-only success or failure evidence" in quality
    assert "if: always()" in quality
    assert "SN_MODULE_GEN_COMMAND" not in quality
    for excluded in (
        "supernote-plugin-template",
        "run_wiki_acceptance.py",
        "run_file_reader_acceptance.py",
        "bounded-note",
        "bounded-doc",
        "npm run build",
        "npm run verify",
        "sdkmanager",
    ):
        assert excluded not in quality


_COMBINED_INSTALLED_TEST = (
    "test_installed_publisher_packages_execute_combined_native_jvm_and_runtime"
)


def _write_installed_junit(path: Path, skipped: bool = False) -> None:
    cases = []
    for name in sorted(qualification_gate.REQUIRED_INSTALLED_TESTS):
        body = (
            '<skipped message="missing compiler" />'
            if skipped and name == _COMBINED_INSTALLED_TEST
            else ""
        )
        cases.append(
            f'<testcase classname="tests.required" name="{name}">{body}</testcase>'
        )
    path.write_text(
        f'<testsuite tests="3">{"".join(cases)}</testsuite>\n', encoding="utf-8"
    )


def _write_complete_installed_evidence(path: Path) -> None:
    digest = "a" * 64
    runtime_payload = {"native/runtime.cpp": digest}
    native = {
        "outputs": [
            "Q5_GENERATED_BINDINGS=236,15",
            "Q5_GENERATED_BINDINGS=236,15",
        ],
        "compiler_inputs": ["feature.cpp"],
        "linked_generated_symbols": ["register_feature"],
    }
    path.write_text(
        json.dumps(
            {
                "commands": [{"arguments": ["cmake", "--build"]}],
                "installed": {
                    "file": "/venv/site-packages/package.py",
                    "version": "0.1.3",
                },
                "wheel": {"sha256": digest},
                "sdist": {"sha256": digest},
                "generated_v1": {
                    name: {"generated.cpp": digest}
                    for name in ("native", "kotlin", "java", "mixed")
                },
                "generated_v2": {"generated.cpp": digest},
                "native_v1": native,
                "native_v2": {
                    **native,
                    "generation_guard_sensitivity": {
                        "authored_method_execution_detected": True,
                        "disabled_guard_sites": 1,
                        "expected_oracle_failure_returncode": 22,
                    },
                },
                "jvm_output": {
                    "host_jvm_execution": "Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M",
                    "android_autolink_sources": [
                        "/consumer/node_modules/@q5/native/.supernote-generated/android/jvm",
                        "/consumer/node_modules/@q5/kotlin/.supernote-generated/android/jvm",
                        "/consumer/node_modules/@q5/java/.supernote-generated/android/jvm",
                        "/consumer/node_modules/@q5/mixed/.supernote-generated/android/jvm",
                        "/consumer/node_modules/@q5/kotlin/android/src/main/java",
                        "/consumer/node_modules/@q5/java/android/src/main/java",
                        "/consumer/node_modules/@q5/mixed/android/src/main/java",
                        "/consumer/node_modules/@supernote/runtime/jvm",
                    ],
                },
                "javascript_output": "Q5_JS_SURFACE_OK=2,12",
                "runtime_output": "Q5_RUNTIME_LIFECYCLE_OK",
                "runtime_payload": runtime_payload,
                "runtime_inventory": {
                    "payload": [
                        {"path": "native/runtime.cpp", "sha256": digest}
                    ]
                },
                "consumer_generator": {
                    "attempts_during_consumption": [],
                    "consumer_subprocesses": 1,
                    "probe_returncode": 97,
                    "probe_attempts": ["q5-probe"],
                },
                "consumer_ksp": "not invoked",
                "tarballs": {
                    f"artifact-{index}.tgz": digest for index in range(6)
                },
                "publisher_unavailable": "/tmp/publisher-unavailable",
            }
        ),
        encoding="utf-8",
    )


def test_required_combined_skip_is_a_hard_gate(tmp_path: Path) -> None:
    junit = tmp_path / "acceptance.xml"
    evidence = tmp_path / "combined.json"
    _write_installed_junit(junit, skipped=True)
    _write_complete_installed_evidence(evidence)

    with pytest.raises(qualification_gate.QualificationError, match="skipped"):
        qualification_gate.verify_installed_qualification(junit, evidence)


def test_required_combined_evidence_must_exist_and_be_complete(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "acceptance.xml"
    evidence = tmp_path / "combined.json"
    _write_installed_junit(junit)

    with pytest.raises(qualification_gate.QualificationError, match="missing"):
        qualification_gate.verify_installed_qualification(junit, evidence)

    _write_complete_installed_evidence(evidence)
    qualification_gate.verify_installed_qualification(junit, evidence)
    value = json.loads(evidence.read_text(encoding="utf-8"))
    value["runtime_output"] = ""
    evidence.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(
        qualification_gate.QualificationError, match="runtime lifecycle"
    ):
        qualification_gate.verify_installed_qualification(junit, evidence)


@pytest.mark.parametrize("mutation", (
    "missing-object", "legacy-marker", "missing-marker", "wrong-marker",
    "missing-sources", "empty-sources", "string-sources", "blank-source",
    "non-string-source", "missing-runtime", "missing-adapter", "duplicate",
    "wrong-source", "different-consumer",
))
def test_required_jvm_evidence_rejects_missing_wrong_or_empty_inputs(
    tmp_path: Path, mutation: str,
) -> None:
    evidence = tmp_path / "combined.json"
    _write_complete_installed_evidence(evidence)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    jvm = payload["jvm_output"]
    sources = jvm["android_autolink_sources"]
    if mutation == "missing-object":
        del payload["jvm_output"]
    elif mutation == "legacy-marker":
        payload["jvm_output"] = "Q5_COMBINED_JVM=Kotlin:K,Java:J,Mixed:M"
    elif mutation == "missing-marker":
        del jvm["host_jvm_execution"]
    elif mutation == "wrong-marker":
        jvm["host_jvm_execution"] = "Q5_HOST_JVM_EXECUTION=wrong"
    elif mutation == "missing-sources":
        del jvm["android_autolink_sources"]
    elif mutation == "empty-sources":
        jvm["android_autolink_sources"] = []
    elif mutation == "string-sources":
        jvm["android_autolink_sources"] = sources[0]
    elif mutation == "blank-source":
        sources[0] = " "
    elif mutation == "non-string-source":
        sources[0] = None
    elif mutation == "missing-runtime":
        sources.pop()
    elif mutation == "missing-adapter":
        sources.pop(0)
    elif mutation == "duplicate":
        sources.append(sources[0])
    elif mutation == "wrong-source":
        sources[0] = "/consumer/node_modules/@wrong/feature/jvm"
    elif mutation == "different-consumer":
        sources[0] = sources[0].replace("/consumer/", "/other/")
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(qualification_gate.QualificationError, match="JVM"):
        qualification_gate.verify_combined_installed_evidence(evidence)


def test_required_jvm_evidence_accepts_native_windows_source_paths(tmp_path: Path) -> None:
    evidence = tmp_path / "combined.json"
    _write_complete_installed_evidence(evidence)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    jvm = payload["jvm_output"]
    jvm["android_autolink_sources"] = [
        ("C:" + path).replace("/", "\\") for path in jvm["android_autolink_sources"]
    ]
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    qualification_gate.verify_combined_installed_evidence(evidence)


def test_required_mode_turns_missing_compiler_control_into_failure(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PATH"] = str(tmp_path)
    environment["SUPERNOTE_REQUIRE_INSTALLED_QUALIFICATION"] = "1"
    environment.pop("CC", None)
    environment.pop("CXX", None)
    for name in (
        "SUPERNOTE_Q5_MODULE_COMMAND",
        "SUPERNOTE_Q5_PYTHON",
        "SUPERNOTE_Q5_WHEEL",
        "SUPERNOTE_Q5_WHEEL_SHA256",
        "SUPERNOTE_Q5_SDIST",
        "SUPERNOTE_Q5_SDIST_SHA256",
        "SUPERNOTE_Q5_RUNTIME_ROOT",
        "SUPERNOTE_GRADLE_COMMAND",
    ):
        environment[name] = str(tmp_path / name.lower())
    node = (
        "tests/test_q5_combined_installed_acceptance.py::"
        "test_installed_publisher_packages_execute_combined_native_jvm_and_runtime"
    )
    result = subprocess.run(
        (sys.executable, "-m", "pytest", "-q", node),
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode != 0
    assert "combined Q5 acceptance requires" in result.stdout
    assert "failed" in result.stdout.lower()


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX compiler symlink")
def test_toolchain_selector_preserves_symlinked_cxx_driver_and_links_standard_library(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang++")
    if compiler is None:
        pytest.skip("clang++ is unavailable")
    driver = tmp_path / "bin/clang++"
    driver.parent.mkdir()
    driver.symlink_to(compiler)

    selected = qualification_gate._resolve_command(str(driver), ())

    assert selected == driver.absolute()
    assert selected.is_symlink()
    probe = tmp_path / "probe"
    probe.mkdir()
    result = qualification_gate._probe_cxx23_driver(selected, probe)
    assert result["output"] == "SNMG_CXX23_LINK_OK"
    assert re.fullmatch(r"[0-9a-f]{64}", result["sha256"])


def test_self_contained_release_inputs_resolve_exact_revisions(tmp_path: Path) -> None:
    wiki = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    project = ROOT / "ci/fixtures/file_reader_test-9f626ed.bundle"
    verifier = tmp_path / "verifier"
    subprocess.run(("git", "init", str(verifier)), check=True, capture_output=True)
    subprocess.run(("git", "bundle", "verify", str(wiki)), cwd=verifier, check=True)
    subprocess.run(
        ("git", "bundle", "verify", str(project)), cwd=verifier, check=True
    )
    subprocess.run(("git", "clone", str(wiki), str(tmp_path / "wiki")), check=True)
    subprocess.run(("git", "clone", str(project), str(tmp_path / "project")), check=True)
    assert subprocess.check_output(
        ("git", "-C", str(tmp_path / "wiki"), "rev-parse", "HEAD"), text=True
    ).strip() == "28cee5a54ea452c91a570dd48cbbadbbfadf30d0"
    assert subprocess.check_output(
        ("git", "-C", str(tmp_path / "project"), "rev-parse", "HEAD"), text=True
    ).strip() == "9f626ed39be82b43ff74eb735d10b7de61f51508"


def test_every_readme_and_wiki_command_output_record_is_source_classified(
    tmp_path: Path,
) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)
    records = scan_documented_commands([ROOT / "README.md", *wiki.glob("*.md")])

    assert len(records) >= 300
    assert {record.classification for record in records} == {
        "android_device",
        "executable",
        "explanatory_output",
        "placeholder",
    }
    assert all(record.reason for record in records)
    assert all(
        record.execution_gate
        for record in records
        if record.classification in {"android_device", "executable"}
    )
    assert all(
        record.execution_gate is None
        for record in records
        if record.classification in {"explanatory_output", "placeholder"}
    )
    grammar = {
        (record.source, record.line, record.text): wiki_acceptance._grammar_disposition(
            record
        )
        for record in records
        if record.argv and record.argv[0] == "sn-module-gen"
    }
    assert all(
        status == "current"
        for (source, _line, _text), (status, _diagnostic) in grammar.items()
        if source == "README.md"
    )
    assert any(
        status == "retired"
        for (source, _line, _text), (status, _diagnostic) in grammar.items()
        if source != "README.md"
    )
    assert all(
        diagnostic
        for (_source, _line, _text), (status, diagnostic) in grammar.items()
        if status == "retired"
    )

    by_source_and_text = {(record.source, record.text): record for record in records}
    expected = {
        ("Getting-Started.md", "python3 -m pip install --upgrade sn-module-gen"): (
            "executable",
            "manual-host-setup",
        ),
        ("Getting-Started.md", "Get-Location"): (
            "executable",
            "manual-host-inspection",
        ),
        ("Using-the-CLI.md", "npm install"): (
            "executable",
            "generated-and-real-project-dependency-gates",
        ),
        ("Troubleshooting.md", "./android/gradlew -version"): (
            "android_device",
            "generated-and-real-project-android-gates",
        ),
        (
            "Troubleshooting.md",
            "adb shell pidof com.ratta.supernote.pluginhost",
        ): ("android_device", "authorized-device-diagnostic"),
        ("Getting-Started.md", "PluginConfig.json"): (
            "explanatory_output",
            None,
        ),
        ("Troubleshooting.md", "# Linux"): (
            "explanatory_output",
            None,
        ),
        ("Using-the-CLI.md", "sn-module-gen add [PACKAGE] [options]"): (
            "placeholder",
            None,
        ),
    }
    for key, classification in expected.items():
        record = by_source_and_text[key]
        assert (record.classification, record.execution_gate) == classification


def test_pinned_wiki_audit_inventories_commands_and_explanatory_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)

    records = scan_documented_commands([wiki / "Getting-Started.md"])
    assert len(records) > 30
    assert any(record.argv[:1] == ("sn-module-gen",) for record in records)
    assert any(record.argv[:1] == ("python3",) for record in records)
    assert any(record.fence == "powershell" for record in records)
    assert any(record.classification == "explanatory_output" for record in records)
    assert read_commands(wiki / "Getting-Started.md")[0][0] == "sn-module-gen"

    output = tmp_path / "documented-commands.json"
    generator = tmp_path / "sn-module-gen"
    generator.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from supernote_module_generator.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    generator.chmod(0o755)
    parsed_arguments: list[tuple[str, ...]] = []
    original_parse_arguments = wiki_acceptance.parse_arguments

    def tracked_parse_arguments(arguments: list[str]):
        parsed_arguments.append(tuple(arguments))
        return original_parse_arguments(arguments)

    monkeypatch.setattr(wiki_acceptance, "parse_arguments", tracked_parse_arguments)
    audited_records = audit_commands(
        wiki,
        ROOT / "README.md",
        (sys.executable, str(generator)),
        output,
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.2"
    assert manifest["record_count"] == len(manifest["records"])
    assert {
        record["classification"] for record in manifest["records"]
    } == {"android_device", "executable", "explanatory_output", "placeholder"}
    assert any(record["argv"][:1] == ["python3"] for record in manifest["records"])
    assert any(
        record["classification"] == "explanatory_output"
        for record in manifest["records"]
    )
    assert {record["grammar_status"] for record in manifest["records"]} == {
        "current",
        "not_applicable",
        "retired",
    }
    assert all(
        record["grammar_diagnostic"]
        for record in manifest["records"]
        if record["grammar_status"] == "retired"
    )
    assert all(
        record["grammar_diagnostic"] is None
        for record in manifest["records"]
        if record["grammar_status"] != "retired"
    )
    placeholders = [
        record
        for record in audited_records
        if record.classification == "placeholder"
        and record.argv[:1] == ("sn-module-gen",)
    ]
    assert {(record.source, record.text) for record in placeholders} == {
        (
            "Error-Handling.md",
            "sn-module-gen validate <module-name> --build --verbose",
        ),
        ("Using-the-CLI.md", "sn-module-gen add [PACKAGE] [options]"),
        ("Using-the-CLI.md", "sn-module-gen update [FEATURE] [options]"),
        ("Using-the-CLI.md", "sn-module-gen validate [FEATURE] [options]"),
        ("Using-the-CLI.md", "sn-module-gen validate --all [options]"),
        ("Using-the-CLI.md", "sn-module-gen remove [FEATURE] [options]"),
        ("Using-the-CLI.md", "sn-module-gen remove --all [options]"),
    }
    assert {
        tuple(wiki_acceptance._grammar_arguments(record)) for record in placeholders
    } <= set(parsed_arguments)


def test_pinned_wiki_doctor_adb_claim_is_explicitly_historical(
    tmp_path: Path,
) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)
    expected = (
        "Doctor selects an ADB executable and runs `adb version` as an advisory "
        "host-tool probe. It does not connect to a device or certify PluginHost or "
        "tablet behavior."
    )
    for name in ("Getting-Started.md", "Using-the-CLI.md", "Troubleshooting.md"):
        normalized = " ".join((wiki / name).read_text(encoding="utf-8").split())
        assert expected in normalized

    doctor_source = (
        ROOT / "src/supernote_module_generator/doctor.py"
    ).read_text(encoding="utf-8")
    assert 'command = [str(candidate), "version"]' in doctor_source
    assert "passed, version, _ = self._probe(command)" in doctor_source
    assert "configured ADB" not in COMMAND_HELP["doctor"]
    assert "device-tested states" not in COMMAND_HELP["doctor"]
    assert "generator-relevant setup" in COMMAND_HELP["doctor"]
    assert "never installs tools" in COMMAND_HELP["doctor"]
    assert "claims device execution" in COMMAND_HELP["doctor"]


def test_contributor_wiki_links_resolve_against_the_pinned_bundle(tmp_path: Path) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)
    pages = {page.stem for page in wiki.glob("*.md")}
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    links = re.findall(
        r"https://github\.com/Ziv-Ink/supernote-module-generator/wiki/([^\s)#]+)",
        contributing,
    )

    assert links
    assert set(links) <= pages
    for stale_name in (
        "CLI and Automation",
        "Requirements and Compatibility",
        "Add a Module",
    ):
        assert stale_name not in contributing


def test_historical_device_evidence_index_maps_pre_public_v4_to_0_1_0() -> None:
    index = (ROOT / "maintainers/device-evidence/README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(index.split())

    assert "V4 was that implementation's internal pre-public qualification codename" in normalized
    assert "released publicly as `sn-module-gen` 0.1.0" in normalized
    assert "Do not rewrite those records" in index
    assert "v4-device-canary-2026-08-27.md" in index
    assert "v4-bounded-note-doc-2026-08-27/" in index


def test_wiki_acceptance_commands_are_bounded_and_source_backed(tmp_path: Path) -> None:
    page = tmp_path / "Getting-Started.md"
    page.write_text(
        f"""# Getting started
{START}
```bash
sn-module-gen add wiki-feature \\
  --starter cpp --starter kotlin --yes
sn-module-gen validate
```
{END}
""",
        encoding="utf-8",
    )

    assert read_commands(page) == (
        (
            "sn-module-gen",
            "add",
            "wiki-feature",
            "--starter",
            "cpp",
            "--starter",
            "kotlin",
            "--yes",
        ),
        ("sn-module-gen", "validate"),
    )

    page.write_text(
        f"{START}\n```bash\nnpm install\n```\n{END}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="documented generator CLI"):
        read_commands(page)

    page.write_text(
        "# Stale command\n\n```bash\nsupernote-module --version\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pre-public CLI command"):
        scan_documented_commands([page])

    page.write_text(
        "# Unknown command\n\n```bash\nmystery-tool audit\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unclassified bash fenced command"):
        scan_documented_commands([page])

    wiki = tmp_path / "invalid-wiki"
    wiki.mkdir()
    readme = tmp_path / "empty-readme.md"
    readme.write_text("# Readme\n", encoding="utf-8")
    invalid = wiki / "Invalid.md"
    invalid.write_text(
        "# Invalid placeholder command\n\n"
        "```bash\n"
        "sn-module-gen validate <module-name> --definitely-invalid\n"
        "```\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match='unknown option "--definitely-invalid"'):
        audit_commands(wiki, readme, "unused-generator", tmp_path / "invalid.json")

    invalid.write_text(
        "# Unsupported placeholder\n\n"
        "```bash\n"
        "sn-module-gen validate <unknown-module>\n"
        "```\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported documented placeholder"):
        audit_commands(wiki, readme, "unused-generator", tmp_path / "invalid.json")
