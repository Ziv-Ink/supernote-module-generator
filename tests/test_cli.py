from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile

import pytest

from supernote_module_generator.cli import (
    _exception_result,
    _guess_command,
    _raw_has,
    _usage_recovery,
    main,
)
from supernote_module_generator.errors import (
    ConfigurationError,
    GeneratorError,
    InternalFailure,
    PartialFailure,
    UnsupportedLegacyProject,
    UnmanifestedGeneratedProject,
)
from supernote_module_generator.filesystem import (
    hash_entry_no_follow,
    restore_protected_source_backup,
)


def invoke(root: Path, arguments: list[str]):
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        arguments,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def isolate_source_guard_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Give one test ownership of only its source-guard backup namespace."""

    private_root = tmp_path.parent / f"{tmp_path.name}-guard-temporary"
    private_root.mkdir()
    original_mkdtemp = tempfile.mkdtemp

    def isolated_mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == "sn-module-gen-source-guard-":
            kwargs["dir"] = private_root
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", isolated_mkdtemp)
    return private_root


@pytest.mark.parametrize(
    "field,unsafe",
    [
        ("destination", "../outside/sentinel.txt"),
        ("destination", "/tmp/outside/sentinel.txt"),
        ("destination", "C:/outside/sentinel.txt"),
        ("destination", "//server/share/sentinel.txt"),
        ("destination", r"..\outside\sentinel.txt"),
        ("backup", "../outside/sentinel.txt"),
        ("backup", "C:/outside/sentinel.txt"),
        ("backup", r"..\outside\sentinel.txt"),
    ],
)
def test_retained_backup_rejects_unsafe_paths_before_mutation(
    tmp_path: Path,
    field: str,
    unsafe: str,
):
    root = tmp_path / "plugin"
    root.mkdir()
    recovery = tmp_path / "recovery"
    backup = recovery / "entries/0"
    backup.parent.mkdir(parents=True)
    backup.write_text("baseline\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n")
    entry = {
        "backup": "entries/0",
        "destination": "local_modules/alpha/sentinel.txt",
        "kind": "file",
        "mode": 0o644,
        "sha256": hash_entry_no_follow(backup),
    }
    entry[field] = unsafe
    (recovery / "recovery-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin_root": str(root.resolve()),
                "entries": [entry],
                "directories": [],
            }
        )
    )

    with pytest.raises(Exception, match="recovery|path|canonical"):
        restore_protected_source_backup(recovery, root)

    assert sentinel.read_text() == "outside\n"
    assert not (root / "local_modules").exists()


@pytest.mark.parametrize(
    ("command", "message", "expected"),
    [
        ("udpate", 'unknown command "udpate"', "Did you mean `sn-module-gen update`?"),
        ("mystery", 'unknown command "mystery"', "sn-module-gen --help"),
        ("add", 'unknown option "--old"', "sn-module-gen add --help"),
        ("mystery", 'unknown option "--old"', "sn-module-gen --help"),
        (
            "add",
            "--starter is required without --yes in non-interactive mode",
            "Provide --starter cpp",
        ),
        ("add", "--quiet, --verbose, and --json cannot be combined", "Choose one output mode"),
        ("add", "invalid starter family old", "Choose cpp or kotlin"),
        ("add", "invalid package name Bad", "valid npm package name"),
        ("add", "invalid JavaScript name 1bad", "Provide it with --javascript-name"),
        ("add", "invalid Android namespace bad", "com.example.local_math"),
        ("add", "invalid package version latest", "valid semantic version"),
        ("add", "could not derive a valid JavaScript name", "--javascript-name"),
        ("add", "could not derive a valid Android namespace", "--android-namespace"),
        ("add", "package name is required", "sn-module-gen add <PACKAGE>"),
        ("add", "node is not available", "Install Node.js"),
        ("add", "not a Supernote plugin: /tmp/x", "Expected package.json"),
        ("add", "non-interactive Add is missing required decisions", ""),
        ("update", "update requires --yes", "Provide --yes"),
        ("add", "module alpha already exists", "Choose another package name"),
        ("add", '"x" exists but is not managed', "remove it manually"),
        ("add", "JavaScript name Alpha already used", "--javascript-name"),
        ("add", "Android namespace com.x already used", "--android-namespace"),
        ("add", "dependency alpha has a different location", "Review package.json"),
        ("update", "module metadata for alpha is missing", "Restore the metadata"),
        ("update", "package.json could not be read", "Fix the file"),
        ("update", "target resolves outside root", "escaping symlink"),
        ("update", "another validation failure", "Correct the input"),
    ],
)
def test_generator_only_usage_failures_have_actionable_recovery(
    command: str, message: str, expected: str
) -> None:
    recovery = _usage_recovery(command, message)
    assert expected in recovery


def test_command_guess_and_raw_option_scan_are_deterministic() -> None:
    assert _guess_command(["--json", "validate", "--plain"]) == "validate"
    assert _guess_command(["--json", "--plain"]) == "unknown"
    assert _raw_has(["--plain", "validate"], "--plain")
    assert not _raw_has(["validate"], "--plain")


@pytest.mark.parametrize(
    ("error", "status", "next_action"),
    [
        (
            GeneratorError("permissions", kind="filesystem_failed", phase="prepare"),
            "failure",
            "Correct the directory permissions",
        ),
        (
            InternalFailure("unexpected"),
            "failure",
            "Rerun with --debug",
        ),
        (
            UnsupportedLegacyProject("legacy"),
            "failure",
            "does not migrate V1-V4",
        ),
        (
            UnmanifestedGeneratedProject("unowned"),
            "failure",
            "Preserve the unmanifested files",
        ),
        (
            PartialFailure("partial", phase="apply"),
            "partial",
            "Correct the reported problem",
        ),
    ],
)
def test_generator_failures_map_to_stable_status_and_next_action(
    error: GeneratorError, status: str, next_action: str
) -> None:
    result = _exception_result("update", error, debug=True)

    assert result.status == status
    assert next_action in result.metadata["next_action"]


def test_generator_failure_keeps_subprocess_and_recovery_evidence() -> None:
    error = GeneratorError(
        "compiler failed",
        kind="subprocess_failed",
        phase="api_generation",
        recovery=["sn-module-gen", "update"],
        subprocess={
            "command": ["cmake", "--build", "out"],
            "exit_code": 7,
            "relevant_lines": ["compile error"],
        },
    )

    result = _exception_result("update", error, debug=False)

    assert result.error is not None
    assert result.error.subprocess is not None
    assert result.error.subprocess.command == ["cmake", "--build", "out"]
    assert result.error.subprocess.exit_code == 7
    assert result.recovery is not None
    assert result.recovery.command == ["sn-module-gen", "update"]
    assert result.metadata["phase_label"] == "Refreshing JavaScript API"


def test_usage_and_unexpected_exceptions_keep_their_distinct_exit_contracts() -> None:
    usage = _exception_result("add", ConfigurationError("package name is required"), False)
    internal = _exception_result("update", RuntimeError("boom"), True)

    assert usage.exit_code == 2
    assert usage.metadata["recovery_text"].startswith("Provide it")
    assert internal.exit_code == 1
    assert internal.error is not None and internal.error.kind == "internal"
    assert internal.error.internal is not None


@pytest.mark.parametrize("symlink_side", ["source", "destination"])
def test_retained_backup_rejects_symlink_ancestors(
    tmp_path: Path,
    symlink_side: str,
):
    root = tmp_path / "plugin"
    root.mkdir()
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside\n")
    if symlink_side == "source":
        source = outside / "source.txt"
        source.write_text("baseline\n")
        (recovery / "entries").symlink_to(outside, target_is_directory=True)
        backup_relative = "entries/source.txt"
    else:
        entries = recovery / "entries"
        entries.mkdir()
        source = entries / "source.txt"
        source.write_text("baseline\n")
        (root / "local_modules").symlink_to(outside, target_is_directory=True)
        backup_relative = "entries/source.txt"
    (recovery / "recovery-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin_root": str(root.resolve()),
                "entries": [
                    {
                        "backup": backup_relative,
                        "destination": "local_modules/alpha/sentinel.txt",
                        "kind": "file",
                        "mode": 0o644,
                        "sha256": hash_entry_no_follow(source),
                    }
                ],
                "directories": [],
            }
        )
    )

    with pytest.raises(Exception, match="symbolic-link|unsafe"):
        restore_protected_source_backup(recovery, root)

    assert sentinel.read_text() == "outside\n"
