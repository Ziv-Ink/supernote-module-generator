from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from supernote_module_generator import __version__
from ci.materialize_readme_examples import materialize, read_examples
from ci.run_wiki_acceptance import (
    END,
    START,
    audit_commands,
    read_commands,
    scan_documented_commands,
)
from ci.scaffold_release_fixture import TEMPLATE_DOTFILES, scaffold
from ci.template_launch_contract import UNVERIFIED, prepare_stub, verify_output, verify_template
from ci.verify_command_result import verify
from supernote_module_generator.binding_codegen import scan_cpp_semantic_model
from supernote_module_generator.arguments import parse_arguments
from supernote_module_generator.feature_generator import FeatureConfig, stage_feature
from supernote_module_generator.feature_model import StarterFamily


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
    compiler = shutil.which("c++")
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


def test_official_template_scaffold_activates_only_declared_dotfiles(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template"
    (template / "android").mkdir(parents=True)
    (template / "scripts").mkdir()
    (template / "android/gradlew").write_text("wrapper\n", encoding="utf-8")
    (template / "scripts/verifyPluginPackage.sh").write_text(
        "verify\n", encoding="utf-8"
    )
    for source_name in TEMPLATE_DOTFILES:
        (template / source_name).write_text(source_name, encoding="utf-8")
    (template / "package.json").write_text("{}\n", encoding="utf-8")

    destination = tmp_path / "plugin"
    scaffold(template, destination)

    assert (destination / "package.json").read_text(encoding="utf-8") == "{}\n"
    for source_name, destination_name in TEMPLATE_DOTFILES.items():
        assert not (destination / source_name).exists()
        assert (destination / destination_name).read_text(encoding="utf-8") == source_name


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
    assert "coverage[toml]>=7.6,<8" in setup
    assert "relative_files = True" in setup
    assert "fail_under = 82.03" in setup
    assert "precision = 2" in setup
    assert "tests/test_regression_harness.py" in quality
    assert "Parse the Bash launch boundary" in quality
    assert "Parse the PowerShell launch boundary" in quality
    assert "npm run run" in quality
    assert "template_launch_contract.py output" in quality
    assert "'setuptools>=58'" in quality
    assert "'wheel>=0.41,<1'" in quality
    assert "--no-build-isolation" in quality
    assert "test_windows_junction_is_never_traversed_or_observed" in platform_paths
    assert (
        "test_windows_copy_preserves_exact_supported_file_and_directory_metadata"
        in platform_paths
    )
    assert "test_windows_contained_classifier_retains_ancestor_against_junction_swap" in platform_paths
    assert "test_windows_symlink_target_read_retains_identity_across_aba_attempt" in platform_paths
    assert "test_windows_atime_neutralization_preserves_concurrent_mtime" in platform_paths


def test_release_gate_pins_and_executes_wiki_and_real_project_contracts() -> None:
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    assert "af3f36f6d6f61d9dbd153b0ebb444a3d3621d25f" in quality
    assert "d20c7714e66b7dd2a4a7fbee7997d1b6e0e94320" in quality
    assert "supernote-module-generator-wiki.bundle" in quality
    assert "file_reader_test-9f626ed.bundle" in quality
    assert "9f626ed39be82b43ff74eb735d10b7de61f51508" in quality
    assert "Verify pinned V4 Wiki commit" in quality
    assert "run_wiki_acceptance.py" in quality
    wiki_runner = quality.index("generator/ci/run_wiki_acceptance.py")
    assert '"$RUNNER_TEMP/generator-venv/bin/python"' in quality[
        wiki_runner - 100 : wiki_runner
    ]
    assert "Audit pinned Wiki commands and run the public CLI scenario" in quality
    assert "Prove the Wiki project is canonical and source-read-only" in quality
    assert "Build and verify the Wiki acceptance package" in quality
    assert "run_file_reader_acceptance.py" in quality
    project_runner = quality.index("generator/ci/run_file_reader_acceptance.py")
    assert '"$RUNNER_TEMP/generator-venv/bin/python"' in quality[
        project_runner - 100 : project_runner
    ]
    assert '"pluginID": "file_reader_test"' in quality
    assert "v4-bounded-note-doc-2026-08-27" in quality
    assert "7.11-bounded-note-doc-device-evidence" in (
        ROOT / "ci/run_file_reader_acceptance.py"
    ).read_text(encoding="utf-8")
    for scenario in ("7.1-7.7", "7.9-7.10"):
        assert scenario in quality


def test_release_gate_builds_the_bounded_note_doc_acceptance_pack() -> None:
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "ci/device_acceptance/cases.json").read_text(encoding="utf-8")
    )

    assert len(manifest["checks"]) == 15
    assert set(manifest["hosts"]) == {"note", "doc"}
    assert "Clone dedicated bounded NOTE and DOC acceptance fixtures" in quality
    assert "ci/device_acceptance/materialize.py" in quality
    assert "bounded-note" in quality and "bounded-doc" in quality
    assert "--identity-suffix Release" in quality
    assert "Compile and package both bounded host contexts" in quality
    assert "for project in bounded-note bounded-doc" in quality
    assert "bounded-note-doc-${{ github.sha }}" in quality


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
    ).strip() == "d20c7714e66b7dd2a4a7fbee7997d1b6e0e94320"
    assert subprocess.check_output(
        ("git", "-C", str(tmp_path / "project"), "rev-parse", "HEAD"), text=True
    ).strip() == "9f626ed39be82b43ff74eb735d10b7de61f51508"


def test_every_readme_and_wiki_cli_example_is_source_classified(tmp_path: Path) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)
    commands = scan_documented_commands([ROOT / "README.md", *wiki.glob("*.md")])

    assert len(commands) >= 100
    classifications = {command.classification for command in commands}
    assert {"android", "project", "legacy-documentation-deferred"} <= classifications
    assert all(command.reason for command in commands)
    for command in commands:
        if command.classification not in {
            "placeholder",
            "legacy-documentation-deferred",
        }:
            parse_arguments(list(command.argv[1:]))


def test_pinned_wiki_audit_preserves_legacy_source_argv(tmp_path: Path) -> None:
    bundle = ROOT / "ci/fixtures/supernote-module-generator-wiki.bundle"
    wiki = tmp_path / "wiki"
    subprocess.run(("git", "clone", str(bundle), str(wiki)), check=True)

    commands = scan_documented_commands([wiki / "Getting-Started.md"])
    legacy = [
        command
        for command in commands
        if command.classification == "legacy-documentation-deferred"
    ]

    assert legacy
    assert all(command.argv[0] == "supernote-module" for command in legacy)
    assert all("Checkpoint 3" in command.reason for command in legacy)
    assert read_commands(wiki / "Getting-Started.md")[0][0] == "supernote-module"

    output = tmp_path / "documented-commands.json"
    audit_commands(wiki, ROOT / "README.md", sys.executable, output)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    deferred = [
        command
        for command in manifest["commands"]
        if command["classification"] == "legacy-documentation-deferred"
    ]
    assert deferred
    assert all(command["argv"][0] == "supernote-module" for command in deferred)


def test_wiki_acceptance_commands_are_bounded_and_source_backed(tmp_path: Path) -> None:
    page = tmp_path / "Getting-Started.md"
    page.write_text(
        f"""# Getting started
{START}
```bash
sn-module-gen add wiki-feature \\
  --starter cpp --starter kotlin --yes
sn-module-gen check
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
        ("sn-module-gen", "check"),
    )

    page.write_text(
        f"{START}\n```bash\nnpm install\n```\n{END}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="documented generator CLI"):
        read_commands(page)


def test_template_launch_contract_and_fake_device_harness(tmp_path: Path) -> None:
    template = tmp_path / "template"
    scripts = template / "scripts"
    scripts.mkdir(parents=True)
    (template / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "run": (
                        "node -e \"process.platform==='win32'?"
                        "scripts/runPlugin.ps1:scripts/runPlugin.sh\""
                    )
                }
            }
        ),
        encoding="utf-8",
    )
    for name in ("runPlugin.sh", "runPlugin.ps1"):
        source = UNVERIFIED + "\n"
        if name.endswith(".ps1"):
            source += (
                "$nodes = @(Get-NodesMatching $Attribute $Value)\n"
                "$nodes = @(Get-NodesMatching $Attribute $Value)\n"
            )
        (scripts / name).write_text(source, encoding="utf-8")

    verify_template(template)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    executable = prepare_stub(tmp_path / "fake-adb", plugin)
    assert executable.is_file()
    assert json.loads((plugin / "PluginConfig.json").read_text())["name"] == "HelloWorld"

    log = tmp_path / "run.log"
    log.write_text(f"[run_plugin] {UNVERIFIED}\n", encoding="utf-8")
    verify_output(log)
