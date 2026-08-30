from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat

from jsonschema import Draft202012Validator
import pytest

import supernote_module_generator.filesystem as filesystem_module
import supernote_module_generator.template_contract as template_contract_module
import supernote_module_generator.transaction as transaction_module
from supernote_module_generator.cli import main
from supernote_module_generator.errors import PartialFailure
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    contained_directory_entries_no_follow,
    read_contained_regular_bytes_no_follow,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.integrity_manifest import TEMPLATE_CAPABILITY_VERSION
from supernote_module_generator.models import RollbackResult
from supernote_module_generator.template_contract import (
    TemplateContractService,
    inspect_template_capability,
)
from supernote_module_generator.transaction import Transaction, recover_pending


OLD_BASH = '''#!/bin/sh
launch_plugin() {
    log "Pressed $LAUNCH_LABEL through NOTE; assuming success after the tap."
}
'''
OLD_POWERSHELL = '''function Get-UniqueBounds([string]$Attribute, [string]$Value) {
    $nodes = Get-NodesMatching $Attribute $Value
}
function Test-HasUniqueNode([string]$Attribute, [string]$Value) {
    $nodes = Get-NodesMatching $Attribute $Value
}
function Invoke-LaunchPlugin {
    Write-Log "Pressed $LaunchLabel through NOTE; assuming success after the tap."
}
'''
VERIFIED_BASH = '''#!/bin/sh
wait_for_new_log_occurrence() { true; }
launch_plugin() {
    wait_for_new_log_occurrence "$event_pattern" "$previous_event_count"
    wait_for_new_log_occurrence "$running_pattern" "$previous_running_count"
    log "Launched $PLUGIN_NAME through NOTE (PluginHost PID $current_pid)."
}
'''
VERIFIED_POWERSHELL = '''function Get-UniqueBounds([string]$Attribute, [string]$Value) {
    $nodes = Get-NodesMatching $Attribute $Value
}
function Test-HasUniqueNode([string]$Attribute, [string]$Value) {
    $nodes = Get-NodesMatching $Attribute $Value
}
function Wait-ForNewLogOccurrence([string]$Needle, [int]$PreviousCount) {
    return $true
}
function Invoke-LaunchPlugin {
    if (-not (Wait-ForNewLogOccurrence $eventPattern $previousEventCount)) { exit 1 }
    if (-not (Wait-ForNewLogOccurrence $runningPattern $previousRunningCount)) { exit 1 }
    Write-Log "Launched $PluginName through NOTE (PluginHost PID $currentPid)."
}
'''
SCHEMA = Draft202012Validator(
    json.loads(
        (
            Path(__file__).parents[1]
            / "src/supernote_module_generator/schemas/command-result.schema.json"
        ).read_text(encoding="utf-8")
    )
)


def plugin(tmp_path: Path) -> Path:
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "PluginConfig.json").write_text("{}\n")
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/runPlugin.sh").write_text(OLD_BASH)
    (tmp_path / "scripts/runPlugin.ps1").write_text(OLD_POWERSHELL)
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "dependencies": {},
                "scripts": {
                    "run": "scripts/runPlugin.sh scripts/runPlugin.ps1"
                },
            }
        )
        + "\n"
    )
    FeatureOperationService(tmp_path).add(
        FeatureConfig(
            tmp_path / "local_modules/alpha",
            "alpha",
            "0.1.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    plan = GenerationService(tmp_path).plan(
        operation="bootstrap",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    GenerationService(tmp_path).execute(
        plan, Transaction(tmp_path, "bootstrap", ("alpha",))
    )
    return tmp_path


def invoke(root: Path, arguments: list[str]) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--json", *arguments],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    assert stderr.getvalue() == ""
    result = json.loads(stdout.getvalue())
    SCHEMA.validate(result)
    return code, result


def invoke_human(root: Path, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--plain", *arguments],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_manifest_records_required_template_capability(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    manifest = json.loads((root / ".supernote-module/manifest.json").read_text())

    assert manifest["template_capability"] == TEMPLATE_CAPABILITY_VERSION


def test_status_reports_drift_and_explicit_sync_is_transactional(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    bash.chmod(0o751)
    before = (bash.read_bytes(), powershell.read_bytes())

    code, status = invoke(root, ["template", "status"])
    assert code == 1
    assert status["metadata"]["template"]["state"] == "drifted"
    assert status["issues"][0]["code"] == "SNMG_TEMPLATE_DRIFT"

    code, preview = invoke(root, ["template", "sync", "--dry-run"])
    assert code == 0
    assert len(preview["changes"]) == 2
    assert (bash.read_bytes(), powershell.read_bytes()) == before

    code, synchronized = invoke(root, ["template", "sync", "--yes"])
    assert code == 0
    assert synchronized["metadata"]["template"]["state"] == "current"
    assert stat.S_IMODE(bash.stat().st_mode) == 0o751
    assert not (root / ".supernote-module-transaction.json").exists()

    code, current = invoke(root, ["template", "status"])
    assert code == 0
    assert current["metadata"]["template"]["state"] == "current"
    assert inspect_template_capability(root).state == "current"


def test_human_template_status_preview_execution_and_state_failure(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)

    code, _stdout, stderr = invoke_human(root, ["template", "status"])
    assert code == 1
    assert "Template capability is drifted" in stderr

    code, stdout, stderr = invoke_human(root, ["template", "sync", "--dry-run"])
    assert (code, stderr) == (0, "")
    assert "Template sync previewed; no files were changed" in stdout

    code, stdout, stderr = invoke_human(root, ["template", "sync", "--yes"])
    assert (code, stderr) == (0, "")
    assert "Template capability synchronized" in stdout

    code, stdout, stderr = invoke_human(root, ["template", "status"])
    assert (code, stderr) == (0, "")
    assert "Template capability is current" in stdout

    (root / "scripts/runPlugin.ps1").write_text("custom user script\n")
    code, _stdout, stderr = invoke_human(root, ["template", "sync", "--yes"])
    assert code == 1
    assert "Template preflight failed" in stderr
    assert "cannot be synchronized automatically" in stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ["template", "status", "--yes"],
        ["template", "status", "--dry-run"],
        ["template", "sync", "--yes", "--dry-run"],
    ),
)
def test_public_template_grammar_errors_are_schema_valid(
    tmp_path: Path, arguments: list[str]
) -> None:
    root = plugin(tmp_path)

    code, result = invoke(root, arguments)

    assert code == 2
    assert result["error"]["kind"] == "usage"
    assert result["error"]["phase"] == "parse"


def test_missing_and_unknown_drift_fail_without_mutation(tmp_path: Path) -> None:
    root = plugin(tmp_path)
    powershell = root / "scripts/runPlugin.ps1"
    powershell.unlink()

    missing = TemplateContractService(root).status()
    assert missing.status == "failure"
    assert missing.metadata["template"]["state"] == "missing"
    assert missing.error is not None
    assert missing.error.kind == "template_state_failed"
    assert missing.error.phase == "template_preflight"

    code, missing_result = invoke(root, ["template", "sync", "--yes"])
    assert code == 1
    assert missing_result["error"]["kind"] == "template_state_failed"
    assert missing_result["error"]["phase"] == "template_preflight"
    assert not (root / ".supernote-module-transaction.json").exists()

    powershell.write_text("Write-Output 'custom user script'\n")
    before = powershell.read_bytes()
    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "template_state_failed"
    assert result["error"]["phase"] == "template_preflight"
    assert powershell.read_bytes() == before
    assert not (root / ".supernote-module-transaction.json").exists()


def test_correlated_runtime_launch_is_current_and_powershell_array_drift_syncs(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    bash.write_text(VERIFIED_BASH)
    powershell.write_text(VERIFIED_POWERSHELL)

    before = inspect_template_capability(root)
    assert before.state == "drifted"
    assert next(item for item in before.files if item.path.endswith(".sh")).state == "current"

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 0
    assert result["metadata"]["template"]["state"] == "current"
    assert bash.read_text() == VERIFIED_BASH
    assert powershell.read_text().count("$nodes = @(Get-NodesMatching") == 2
    assert "Launch attempted but runtime success was not verified." not in powershell.read_text()


@pytest.mark.parametrize("relative", ("scripts/runPlugin.sh", "scripts/runPlugin.ps1"))
def test_sync_rejects_a_concurrent_script_edit_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = plugin(tmp_path)
    target = root / relative
    external = b"external concurrent template edit\n"
    original = template_contract_module._validate_sync_preconditions

    def substitute(root_path, plan) -> None:
        target.write_bytes(external)
        original(root_path, plan)

    monkeypatch.setattr(
        template_contract_module, "_validate_sync_preconditions", substitute
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    assert target.read_bytes() == external
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("relative", ("scripts/runPlugin.sh", "scripts/runPlugin.ps1"))
def test_sync_rejects_an_edit_after_staging_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = plugin(tmp_path)
    target = root / relative
    external = b"external edit after template payload staging\n"
    external_mode = 0o640
    external_atime = 1_700_000_001_000_000_000
    external_mtime = 1_700_000_002_000_000_000
    original = template_contract_module._stage_candidate

    def substitute(transaction, candidate, index):
        staged = original(transaction, candidate, index)
        if candidate.baseline.path == relative:
            target.write_bytes(external)
            target.chmod(external_mode)
            os.utime(
                target,
                ns=(external_atime, external_mtime),
                follow_symlinks=False,
            )
        return staged

    monkeypatch.setattr(template_contract_module, "_stage_candidate", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    content, target_stat = read_contained_regular_bytes_no_follow(root, target)
    assert content == external
    assert stat.S_IMODE(target_stat.st_mode) == external_mode
    assert target_stat.st_atime_ns == external_atime
    assert target_stat.st_mtime_ns == external_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("relative", ("scripts/runPlugin.sh", "scripts/runPlugin.ps1"))
def test_sync_conditionally_rejects_an_edit_at_the_replacement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = plugin(tmp_path)
    target = root / relative
    external = b"external edit at conditional template replacement\n"
    external_mode = 0o604
    external_atime = 1_700_000_003_000_000_000
    external_mtime = 1_700_000_004_000_000_000
    original = Transaction.replace_regular_batch_if_matches

    def substitute(self, replacements) -> None:
        target.write_bytes(external)
        target.chmod(external_mode)
        os.utime(
            target,
            ns=(external_atime, external_mtime),
            follow_symlinks=False,
        )
        original(self, replacements)

    monkeypatch.setattr(Transaction, "replace_regular_batch_if_matches", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    content, target_stat = read_contained_regular_bytes_no_follow(root, target)
    assert content == external
    assert stat.S_IMODE(target_stat.st_mode) == external_mode
    assert target_stat.st_atime_ns == external_atime
    assert target_stat.st_mtime_ns == external_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("relative", ("scripts/runPlugin.sh", "scripts/runPlugin.ps1"))
def test_sync_no_clobber_publication_preserves_a_recreated_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    root = plugin(tmp_path)
    target = root / relative
    external = b"external destination recreated during template publication\n"
    external_mode = 0o640
    external_atime = 1_700_000_005_000_000_000
    external_mtime = 1_700_000_006_000_000_000
    original = os.link

    def substitute(source, destination, **kwargs) -> None:
        if (
            Path(destination).name == target.name
            and kwargs.get("dst_dir_fd") is not None
        ):
            target.write_bytes(external)
            target.chmod(external_mode)
            os.utime(
                target,
                ns=(external_atime, external_mtime),
                follow_symlinks=False,
            )
        original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    content, target_stat = read_contained_regular_bytes_no_follow(root, target)
    assert content == external
    assert stat.S_IMODE(target_stat.st_mode) == external_mode
    assert target_stat.st_atime_ns == external_atime
    assert target_stat.st_mtime_ns == external_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symbolic links")
def test_sync_no_clobber_publication_preserves_a_symlink_and_external_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    target = root / "scripts/runPlugin.sh"
    external = tmp_path / "external-template-sentinel"
    external.write_bytes(b"external target must remain untouched\n")
    external.chmod(0o604)
    external_atime = 1_700_000_007_000_000_000
    external_mtime = 1_700_000_008_000_000_000
    os.utime(
        external,
        ns=(external_atime, external_mtime),
        follow_symlinks=False,
    )
    original = os.link

    def substitute(source, destination, **kwargs) -> None:
        if (
            Path(destination).name == target.name
            and kwargs.get("dst_dir_fd") is not None
        ):
            target.symlink_to(external)
        original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    assert target.is_symlink()
    assert target.readlink() == external
    content, target_stat = read_contained_regular_bytes_no_follow(
        tmp_path, external
    )
    assert content == b"external target must remain untouched\n"
    assert stat.S_IMODE(target_stat.st_mode) == 0o604
    assert target_stat.st_atime_ns == external_atime
    assert target_stat.st_mtime_ns == external_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("replacement_kind", ("file", "symlink"))
def test_later_publication_conflict_preserves_replaced_earlier_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    if replacement_kind == "symlink" and os.name == "nt":
        pytest.skip("requires POSIX symbolic links")
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    bash_external = b"external Bash replacement after publication\n"
    powershell_external = b"later external PowerShell conflict\n"
    bash_mode = 0o604
    bash_atime = 1_700_000_009_000_000_000
    bash_mtime = 1_700_000_010_000_000_000
    sentinel = tmp_path / "external-symlink-sentinel"
    sentinel.write_bytes(b"symlink target remains exact\n")
    sentinel.chmod(0o640)
    sentinel_atime = 1_700_000_011_000_000_000
    sentinel_mtime = 1_700_000_012_000_000_000
    os.utime(
        sentinel,
        ns=(sentinel_atime, sentinel_mtime),
        follow_symlinks=False,
    )
    original = os.link

    def substitute(source, destination, **kwargs) -> None:
        destination_path = Path(destination)
        if destination_path.name == bash.name:
            original(source, destination, **kwargs)
            replacement = bash.with_name(".external-runPlugin.sh")
            if replacement_kind == "file":
                replacement.write_bytes(bash_external)
                replacement.chmod(bash_mode)
                os.utime(
                    replacement,
                    ns=(bash_atime, bash_mtime),
                    follow_symlinks=False,
                )
            else:
                replacement.symlink_to(sentinel)
            os.replace(replacement, bash)
            return
        if destination_path.name == powershell.name:
            powershell.write_bytes(powershell_external)
        original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    assert powershell.read_bytes() == powershell_external
    if replacement_kind == "file":
        content, bash_stat = read_contained_regular_bytes_no_follow(root, bash)
        assert content == bash_external
        assert stat.S_IMODE(bash_stat.st_mode) == bash_mode
        assert bash_stat.st_atime_ns == bash_atime
        assert bash_stat.st_mtime_ns == bash_mtime
    else:
        assert bash.is_symlink()
        assert bash.readlink() == sentinel
        content, sentinel_stat = read_contained_regular_bytes_no_follow(
            tmp_path, sentinel
        )
        assert content == b"symlink target remains exact\n"
        assert stat.S_IMODE(sentinel_stat.st_mode) == 0o640
        assert sentinel_stat.st_atime_ns == sentinel_atime
        assert sentinel_stat.st_mtime_ns == sentinel_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_later_publication_conflict_preserves_in_place_earlier_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    external = b"in-place external Bash edit after publication\n"
    external_mode = 0o604
    external_atime = 1_700_000_013_000_000_000
    external_mtime = 1_700_000_014_000_000_000
    original = os.link

    def substitute(source, destination, **kwargs) -> None:
        destination_path = Path(destination)
        if destination_path.name == powershell.name:
            powershell.write_bytes(b"later PowerShell conflict\n")
            original(source, destination, **kwargs)
        else:
            original(source, destination, **kwargs)
        if destination_path.name == bash.name:
            bash.write_bytes(external)
            bash.chmod(external_mode)
            os.utime(
                bash,
                ns=(external_atime, external_mtime),
                follow_symlinks=False,
            )

    monkeypatch.setattr(os, "link", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    content, bash_stat = read_contained_regular_bytes_no_follow(root, bash)
    assert content == external
    assert stat.S_IMODE(bash_stat.st_mode) == external_mode
    assert bash_stat.st_atime_ns == external_atime
    assert bash_stat.st_mtime_ns == external_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("replacement_kind", ("file", "symlink"))
def test_later_conflict_atomically_retains_cleanup_boundary_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    if replacement_kind == "symlink" and os.name == "nt":
        pytest.skip("requires POSIX symbolic links")
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    external = b"atomic Bash replacement at cleanup retention\n"
    external_mode = 0o640
    external_atime = 1_700_000_015_000_000_000
    external_mtime = 1_700_000_016_000_000_000
    sentinel = tmp_path / "cleanup-boundary-symlink-target"
    sentinel.write_bytes(b"cleanup symlink target remains exact\n")
    sentinel.chmod(0o604)
    sentinel_atime = 1_700_000_017_000_000_000
    sentinel_mtime = 1_700_000_018_000_000_000
    os.utime(
        sentinel,
        ns=(sentinel_atime, sentinel_mtime),
        follow_symlinks=False,
    )
    original_link = os.link
    original_replace = os.replace
    original_rename = os.rename
    conflict_seen = False

    def conflict(source, destination, **kwargs) -> None:
        nonlocal conflict_seen
        destination_path = Path(destination)
        if destination_path.name == powershell.name:
            conflict_seen = True
            powershell.write_bytes(b"later PowerShell conflict\n")
        original_link(source, destination, **kwargs)

    substituted = False

    def substitute(source, destination, **kwargs) -> None:
        nonlocal substituted
        source_path = Path(source)
        if (
            not substituted
            and conflict_seen
            and source_path.name == bash.name
            and kwargs.get("dst_dir_fd") is not None
        ):
            substituted = True
            replacement = bash.with_name(".cleanup-boundary-replacement")
            if replacement_kind == "file":
                replacement.write_bytes(external)
                replacement.chmod(external_mode)
                os.utime(
                    replacement,
                    ns=(external_atime, external_mtime),
                    follow_symlinks=False,
                )
            else:
                replacement.symlink_to(sentinel)
            original_replace(replacement, bash)
        original_rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", conflict)
    monkeypatch.setattr(os, "rename", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert substituted
    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["actual_changes"] == []
    assert result["rollback"]["status"] == "not_needed"
    if replacement_kind == "file":
        content, bash_stat = read_contained_regular_bytes_no_follow(root, bash)
        assert content == external
        assert stat.S_IMODE(bash_stat.st_mode) == external_mode
        assert bash_stat.st_atime_ns == external_atime
        assert bash_stat.st_mtime_ns == external_mtime
    else:
        assert bash.is_symlink()
        assert bash.readlink() == sentinel
        content, sentinel_stat = read_contained_regular_bytes_no_follow(
            tmp_path, sentinel
        )
        assert content == b"cleanup symlink target remains exact\n"
        assert stat.S_IMODE(sentinel_stat.st_mode) == 0o604
        assert sentinel_stat.st_atime_ns == sentinel_atime
        assert sentinel_stat.st_mtime_ns == sentinel_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_later_conflict_retains_both_entries_when_restoration_also_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    captured_external = b"external Bash edit retained for recovery\n"
    newer_external = b"newer Bash edit that wins the restoration race\n"
    original = os.link
    bash_published = False

    def substitute(source, destination, **kwargs) -> None:
        nonlocal bash_published
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.name == powershell.name:
            powershell.write_bytes(b"later PowerShell conflict\n")
            original(source, destination, **kwargs)
            return
        if (
            bash_published
            and source_path.name == "0"
            and destination_path.name == bash.name
            and kwargs.get("src_dir_fd") is not None
        ):
            bash.write_bytes(newer_external)
            original(source, destination, **kwargs)
            return
        original(source, destination, **kwargs)
        if destination_path.name == bash.name:
            bash_published = True
            bash.write_bytes(captured_external)

    monkeypatch.setattr(os, "link", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 3, json.dumps(result, indent=2)
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == [
        {
            "path": str(bash),
            "action": "update",
            "ownership": "rollback_residue",
        },
        {
            "path": str(powershell),
            "action": "update",
            "ownership": "rollback_residue",
        },
    ]
    journal = root / ".supernote-module-transaction.json"
    assert Path(result["metadata"]["recovery_path"]) == journal
    raw = json.loads(journal.read_text(encoding="utf-8"))
    conditional = next(
        entry for entry in raw["entries"] if entry["kind"] == "conditional_replace"
    )
    capture = Path(conditional["capture"])
    assert bash.read_bytes() == newer_external
    assert capture.read_bytes() == captured_external

    monkeypatch.undo()
    bash.unlink()
    recovery = recover_pending(root)
    assert recovery.rollback.status == "completed"
    assert bash.read_bytes() == captured_external
    assert not journal.exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symbolic links")
def test_conditional_capture_descriptor_ignores_substituted_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    bash_before = bash.read_bytes()
    external_directory = tmp_path / "external-capture-directory"
    external_directory.mkdir()
    sentinel = external_directory / "0"
    sentinel.write_bytes(b"external capture sentinel remains exact\n")
    sentinel.chmod(0o604)
    sentinel_atime = 1_700_000_019_000_000_000
    sentinel_mtime = 1_700_000_020_000_000_000
    os.utime(
        sentinel,
        ns=(sentinel_atime, sentinel_mtime),
        follow_symlinks=False,
    )
    original_link = os.link
    original_rename = os.rename
    substituted = False
    conflict_seen = False

    def conflict(source, destination, **kwargs) -> None:
        nonlocal conflict_seen
        if Path(destination).name == powershell.name:
            conflict_seen = True
            powershell.write_bytes(b"later PowerShell conflict\n")
        original_link(source, destination, **kwargs)

    def substitute(source, destination, **kwargs) -> None:
        nonlocal substituted
        if (
            not substituted
            and conflict_seen
            and Path(source).name == bash.name
            and kwargs.get("dst_dir_fd") is not None
        ):
            substituted = True
            state_dir = next(root.glob(".supernote-module-transaction-*"))
            captures = state_dir / "captures"
            detached = state_dir / "detached-captures"
            original_rename(captures, detached)
            captures.symlink_to(external_directory, target_is_directory=True)
        original_rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", conflict)
    monkeypatch.setattr(os, "rename", substitute)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert substituted
    assert code == 1
    assert result["error"]["kind"] == "plan_conflict"
    assert result["rollback"]["status"] == "not_needed"
    assert result["actual_changes"] == []
    assert bash.read_bytes() == bash_before
    sentinel_content, sentinel_stat = read_contained_regular_bytes_no_follow(
        tmp_path, sentinel
    )
    assert sentinel_content == b"external capture sentinel remains exact\n"
    assert stat.S_IMODE(sentinel_stat.st_mode) == 0o604
    assert sentinel_stat.st_atime_ns == sentinel_atime
    assert sentinel_stat.st_mtime_ns == sentinel_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symbolic links")
@pytest.mark.parametrize("boundary", ("retention", "publication"))
def test_conditional_destination_parent_descriptor_blocks_external_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    root = plugin(tmp_path)
    scripts = root / "scripts"
    bash = scripts / "runPlugin.sh"
    powershell = scripts / "runPlugin.ps1"
    before = tuple(
        read_contained_regular_bytes_no_follow(root, path)
        for path in (bash, powershell)
    )
    external = tmp_path / f"external-{boundary}-destination"
    external.mkdir()
    external.chmod(0o705)
    external_atime = 1_700_000_025_000_000_000
    external_mtime = 1_700_000_026_000_000_000
    os.utime(external, ns=(external_atime, external_mtime))
    detached = root / f"detached-scripts-{boundary}"
    original_rename = os.rename
    original_link = os.link
    substituted = False

    def substitute_parent() -> None:
        nonlocal substituted
        substituted = True
        original_rename(scripts, detached)
        scripts.symlink_to(external, target_is_directory=True)

    def rename(source, destination, **kwargs) -> None:
        if (
            boundary == "retention"
            and not substituted
            and Path(source).name == bash.name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            substitute_parent()
        original_rename(source, destination, **kwargs)

    def link(source, destination, **kwargs) -> None:
        if (
            boundary == "publication"
            and not substituted
            and Path(destination).name == bash.name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            substitute_parent()
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "link", link)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert substituted
    assert code == 3, result
    assert result["status"] == "partial"
    # The retained descriptor cleanup has already unwound every generator
    # publication. Only transaction finalization remains blocked by the
    # externally substituted canonical parent.
    assert result["rollback"]["status"] == "not_needed"
    assert contained_directory_entries_no_follow(tmp_path, external) == ()
    external_stat = external.lstat()
    assert stat.S_IMODE(external_stat.st_mode) == 0o705
    assert external_stat.st_atime_ns == external_atime
    assert external_stat.st_mtime_ns == external_mtime
    journal = root / ".supernote-module-transaction.json"
    assert journal.is_file()

    monkeypatch.undo()
    scripts.unlink()
    original_rename(detached, scripts)
    recovery = recover_pending(root)

    assert recovery.rollback.status == "not_needed"
    assert contained_directory_entries_no_follow(tmp_path, external) == ()
    for path, expected in zip((bash, powershell), before):
        current = read_contained_regular_bytes_no_follow(root, path)
        assert current[0] == expected[0]
        assert current[1].st_mode == expected[1].st_mode
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    assert not journal.exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("boundary", ("retention", "publication"))
def test_conditional_destination_parent_regular_replacement_is_unwound_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts = root / "scripts"
    paths = (scripts / "runPlugin.sh", scripts / "runPlugin.ps1")
    before = tuple(
        read_contained_regular_bytes_no_follow(root, path) for path in paths
    )
    scripts_before = scripts.lstat()
    moved = tmp_path / f"moved-scripts-{boundary}"
    original_rename = os.rename
    original_link = os.link
    substituted = False

    def write_visible_replacement() -> None:
        nonlocal substituted
        substituted = True
        original_rename(scripts, moved)
        scripts.mkdir()
        scripts.chmod(stat.S_IMODE(scripts_before.st_mode))
        for path, (content, metadata) in zip(paths, before):
            replacement = scripts / path.name
            replacement.write_bytes(content)
            replacement.chmod(stat.S_IMODE(metadata.st_mode))
            os.utime(
                replacement,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
            )
        os.utime(
            scripts,
            ns=(scripts_before.st_atime_ns, scripts_before.st_mtime_ns),
        )

    def rename(source, destination, **kwargs) -> None:
        if (
            boundary == "retention"
            and not substituted
            and Path(source).name == paths[0].name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            write_visible_replacement()
        original_rename(source, destination, **kwargs)

    def link(source, destination, **kwargs) -> None:
        if (
            boundary == "publication"
            and not substituted
            and Path(destination).name == paths[0].name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            write_visible_replacement()
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "link", link)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert substituted
    assert code == 1, json.dumps(result, indent=2)
    assert result["error"]["kind"] == "plan_conflict"
    assert result["error"]["phase"] == "precommit"
    assert result["rollback"]["status"] == "not_needed"
    assert result["actual_changes"] == []
    for directory in (scripts, moved):
        directory_metadata = directory.lstat()
        assert stat.S_IMODE(directory_metadata.st_mode) == stat.S_IMODE(
            scripts_before.st_mode
        )
        assert directory_metadata.st_atime_ns == scripts_before.st_atime_ns
        assert directory_metadata.st_mtime_ns == scripts_before.st_mtime_ns
        assert contained_directory_entries_no_follow(
            tmp_path, directory
        ) == (("runPlugin.ps1", "file"), ("runPlugin.sh", "file"))
        for path, (content, metadata) in zip(paths, before):
            current = read_contained_regular_bytes_no_follow(
                tmp_path, directory / path.name
            )
            assert current[0] == content
            assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
                metadata.st_mode
            )
            assert current[1].st_atime_ns == metadata.st_atime_ns
            assert current[1].st_mtime_ns == metadata.st_mtime_ns
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


def test_conditional_batch_opens_one_shared_destination_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts = root / "scripts"
    bash = scripts / "runPlugin.sh"
    powershell = scripts / "runPlugin.ps1"
    transaction = Transaction(root, "template sync", ())
    (transaction.state_dir / "template").mkdir()
    original_open = transaction_module._open_contained_parent_descriptor
    calls = 0
    substituted = False

    def open_parent(project_root: Path, destination: Path) -> tuple[int, str]:
        nonlocal calls, substituted
        if destination.parent == scripts:
            calls += 1
            if calls == 2:
                substituted = True
        return original_open(project_root, destination)

    monkeypatch.setattr(
        transaction_module, "_open_contained_parent_descriptor", open_parent
    )
    descriptors = transaction_module._prepare_conditional_state_descriptors(
        root, transaction.state_dir, (bash, powershell)
    )
    try:
        bash_descriptor, _ = descriptors.destination(bash)
        powershell_descriptor, _ = descriptors.destination(powershell)
        assert bash_descriptor == powershell_descriptor
        assert calls == 1
        assert not substituted
    finally:
        descriptors.close()
        transaction.abandon_unmutated()


@pytest.mark.parametrize("target_name", ("runPlugin.sh", "runPlugin.ps1"))
@pytest.mark.parametrize(
    "failure", (RuntimeError("post-retention failure"), KeyboardInterrupt())
)
def test_post_retention_exception_restores_exactly_with_truthful_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    failure: BaseException,
) -> None:
    root = plugin(tmp_path)
    scripts = tuple(
        root / relative
        for relative in ("scripts/runPlugin.sh", "scripts/runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    root_mode = 0o751
    root_times = (1_700_000_039_000_000_000, 1_700_000_040_000_000_000)
    root.chmod(root_mode)
    os.utime(root, ns=root_times)
    original_rename = os.rename
    injected = False

    def interrupt_after_move(source, destination, **kwargs) -> None:
        nonlocal injected
        original_rename(source, destination, **kwargs)
        if (
            not injected
            and Path(source).name == target_name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            injected = True
            raise failure

    monkeypatch.setattr(os, "rename", interrupt_after_move)

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(failure, KeyboardInterrupt)
    assert injected
    assert code == (130 if interrupted else 1), result
    assert result["status"] == ("cancelled" if interrupted else "failure")
    assert result["rollback"]["status"] == "not_needed"
    assert result["actual_changes"] == []
    assert result["recovery"] is None
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "completed" if interrupted else "not_requested"
    )
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    root_metadata = root.lstat()
    assert stat.S_IMODE(root_metadata.st_mode) == root_mode
    assert root_metadata.st_atime_ns == root_times[0]
    assert root_metadata.st_mtime_ns == root_times[1]
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.parametrize("target_name", ("runPlugin.sh", "runPlugin.ps1"))
@pytest.mark.parametrize(
    "failure", (RuntimeError("post-retention parent swap"), KeyboardInterrupt())
)
@pytest.mark.parametrize("replacement_boundary", ("rename", "reconciliation"))
def test_post_retention_parent_replacement_unwinds_through_retained_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    failure: BaseException,
    replacement_boundary: str,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts_directory = root / "scripts"
    scripts = tuple(
        scripts_directory / name for name in ("runPlugin.sh", "runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    directory_before = scripts_directory.lstat()
    root_mode = 0o751
    root_times = (1_700_000_041_000_000_000, 1_700_000_042_000_000_000)
    root.chmod(root_mode)
    os.utime(root, ns=root_times)
    detached = tmp_path / (
        f"detached-{replacement_boundary}-{target_name}-{type(failure).__name__}"
    )
    external_contents = (
        b"external replacement Bash remains exact\n",
        b"external replacement PowerShell remains exact\n",
    )
    external_modes = (0o604, 0o640)
    external_times = (
        (1_700_000_033_000_000_000, 1_700_000_034_000_000_000),
        (1_700_000_035_000_000_000, 1_700_000_036_000_000_000),
    )
    external_directory_times = (
        1_700_000_037_000_000_000,
        1_700_000_038_000_000_000,
    )
    external_root_mode = root_mode
    external_root_times = (
        root_times[0],
        1_700_000_044_000_000_000,
    )
    original_rename = os.rename
    original_matches_exact = transaction_module._relative_regular_matches_exact
    injected = False
    retention_interrupted = False

    def replace_parent() -> None:
        nonlocal injected
        injected = True
        original_rename(scripts_directory, detached)
        scripts_directory.mkdir()
        scripts_directory.chmod(0o705)
        for script, content, mode, times in zip(
            scripts, external_contents, external_modes, external_times
        ):
            script.write_bytes(content)
            script.chmod(mode)
            os.utime(script, ns=times)
        os.utime(scripts_directory, ns=external_directory_times)
        root.chmod(external_root_mode)
        os.utime(root, ns=external_root_times)

    def replace_parent_after_move(source, destination, **kwargs) -> None:
        nonlocal retention_interrupted
        original_rename(source, destination, **kwargs)
        if (
            not retention_interrupted
            and Path(source).name == target_name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            retention_interrupted = True
            if replacement_boundary == "rename":
                replace_parent()
            raise failure

    def replace_parent_after_reconciliation(name, descriptor, content, metadata):
        exact = original_matches_exact(name, descriptor, content, metadata)
        if (
            replacement_boundary == "reconciliation"
            and retention_interrupted
            and not injected
            and name == target_name
            and exact
        ):
            replace_parent()
        return exact

    monkeypatch.setattr(os, "rename", replace_parent_after_move)
    monkeypatch.setattr(
        transaction_module,
        "_relative_regular_matches_exact",
        replace_parent_after_reconciliation,
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(failure, KeyboardInterrupt)
    assert injected
    assert code == 3, result
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == []
    assert result["recovery"]["command"] == ["sn-module-gen", "doctor"]
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    detached_metadata = detached.lstat()
    assert stat.S_IMODE(detached_metadata.st_mode) == stat.S_IMODE(
        directory_before.st_mode
    )
    assert detached_metadata.st_atime_ns == directory_before.st_atime_ns
    assert detached_metadata.st_mtime_ns == directory_before.st_mtime_ns
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(
            tmp_path, detached / script.name
        )
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    visible_metadata = scripts_directory.lstat()
    assert stat.S_IMODE(visible_metadata.st_mode) == 0o705
    assert visible_metadata.st_atime_ns == external_directory_times[0]
    assert visible_metadata.st_mtime_ns == external_directory_times[1]
    for script, content, mode, times in zip(
        scripts, external_contents, external_modes, external_times
    ):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == content
        assert stat.S_IMODE(current[1].st_mode) == mode
        assert current[1].st_atime_ns == times[0]
        assert current[1].st_mtime_ns == times[1]
    root_metadata = root.lstat()
    assert stat.S_IMODE(root_metadata.st_mode) == external_root_mode
    assert root_metadata.st_atime_ns == external_root_times[0]
    assert root_metadata.st_mtime_ns == external_root_times[1]
    journal = root / ".supernote-module-transaction.json"
    assert journal.is_file()
    raw = json.loads(journal.read_text(encoding="utf-8"))
    assert transaction_module._conditional_conflict_is_durable(root, raw)
    state_dir = root / f"{transaction_module.STATE_PREFIX}{raw['id']}"
    for index, entry in enumerate(raw["entries"]):
        live = detached / Path(entry["path"]).name
        authority = state_dir / "modules" / str(index)
        live_metadata = live.lstat()
        authority_metadata = authority.lstat()
        assert (live_metadata.st_dev, live_metadata.st_ino) != (
            authority_metadata.st_dev,
            authority_metadata.st_ino,
        )
    recovery = recover_pending(root)
    assert recovery.rollback.status == "partial"
    assert recovery.recovery_command == ["sn-module-gen", "doctor"]


@pytest.mark.parametrize(
    "failure", (RuntimeError("root handoff failure"), KeyboardInterrupt())
)
def test_newer_root_metadata_after_conflict_publication_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts_directory = root / "scripts"
    scripts = tuple(
        scripts_directory / name for name in ("runPlugin.sh", "runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    root_mode = 0o751
    root_times = (1_700_000_041_000_000_000, 1_700_000_042_000_000_000)
    root.chmod(root_mode)
    os.utime(root, ns=root_times)
    detached = tmp_path / "detached-root-handoff"
    first_root_state = (
        root_mode,
        root_times[0],
        1_700_000_046_000_000_000,
    )
    newer_root_state = (
        root_mode,
        root_times[0],
        1_700_000_048_000_000_000,
    )
    original_rename = os.rename
    original_replace = os.replace
    armed = False
    updated = False
    root_journal_republished = False

    def replace_parent_after_move(source, destination, **kwargs) -> None:
        nonlocal armed
        original_rename(source, destination, **kwargs)
        if (
            not armed
            and Path(source).name == "runPlugin.ps1"
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            armed = True
            original_rename(scripts_directory, detached)
            scripts_directory.mkdir()
            for script, expected in zip(scripts, before):
                script.write_bytes(expected[0])
                script.chmod(stat.S_IMODE(expected[1].st_mode))
                os.utime(
                    script,
                    ns=(expected[1].st_atime_ns, expected[1].st_mtime_ns),
                )
            root.chmod(first_root_state[0])
            os.utime(root, ns=first_root_state[1:])
            raise failure

    def update_root_at_conflict_publication(source, destination, *args, **kwargs):
        nonlocal root_journal_republished, updated
        result = original_replace(source, destination, *args, **kwargs)
        destination_path = Path(destination)
        if armed and destination_path == root / transaction_module.JOURNAL_NAME:
            root_journal_republished = True
        if (
            armed
            and not updated
            and destination_path.name
            == Path(transaction_module.CONDITIONAL_CONFLICT_AUTHORITY_NAME).name
        ):
            updated = True
            root.chmod(newer_root_state[0])
            os.utime(root, ns=newer_root_state[1:])
        return result

    monkeypatch.setattr(os, "rename", replace_parent_after_move)
    monkeypatch.setattr(os, "replace", update_root_at_conflict_publication)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert armed and updated
    assert not root_journal_republished
    interrupted = isinstance(failure, KeyboardInterrupt)
    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == []
    assert result["recovery"]["command"] == ["sn-module-gen", "doctor"]
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    metadata = root.lstat()
    assert stat.S_IMODE(metadata.st_mode) == newer_root_state[0]
    assert metadata.st_atime_ns == newer_root_state[1]
    assert metadata.st_mtime_ns == newer_root_state[2]
    journal = root / ".supernote-module-transaction.json"
    raw = json.loads(journal.read_text(encoding="utf-8"))
    assert transaction_module._conditional_conflict_is_durable(root, raw)
    recovery = recover_pending(root)
    assert recovery.rollback.status == "partial"
    assert recovery.recovery_command == ["sn-module-gen", "doctor"]


def test_doctor_finalizes_resolved_conditional_template_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts_directory = root / "scripts"
    scripts = tuple(
        scripts_directory / name for name in ("runPlugin.sh", "runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    directory_before = scripts_directory.lstat()
    detached = tmp_path / "detached-template-baseline"
    external_tree = tmp_path / "preserved-external-template"
    external_contents = (
        b"external Bash awaiting operator resolution\n",
        b"external PowerShell awaiting operator resolution\n",
    )
    external_modes = (0o604, 0o640)
    external_times = (
        (1_700_000_061_000_000_000, 1_700_000_062_000_000_000),
        (1_700_000_063_000_000_000, 1_700_000_064_000_000_000),
    )
    external_directory_times = (
        1_700_000_065_000_000_000,
        1_700_000_066_000_000_000,
    )
    resolved_root_state = (
        0o705,
        1_700_000_067_000_000_000,
        1_700_000_068_000_000_000,
    )
    original_rename = os.rename
    injected = False

    def replace_parent_after_move(source, destination, **kwargs) -> None:
        nonlocal injected
        original_rename(source, destination, **kwargs)
        if (
            not injected
            and Path(source).name == "runPlugin.ps1"
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            injected = True
            original_rename(scripts_directory, detached)
            scripts_directory.mkdir()
            scripts_directory.chmod(0o711)
            for script, content, mode, times in zip(
                scripts, external_contents, external_modes, external_times
            ):
                script.write_bytes(content)
                script.chmod(mode)
                os.utime(script, ns=times)
            os.utime(scripts_directory, ns=external_directory_times)
            raise RuntimeError("operator resolution required")

    monkeypatch.setattr(os, "rename", replace_parent_after_move)

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert injected
    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert "restore scripts/runPlugin.sh" in result["next_action"]
    journal = root / transaction_module.JOURNAL_NAME
    assert journal.is_file()
    raw_journal = json.loads(journal.read_text(encoding="utf-8"))
    state_dir = root / (
        f"{transaction_module.STATE_PREFIX}{raw_journal['id']}"
    )
    assert (state_dir / "modules/0").is_file()
    assert (state_dir / "modules/1").is_file()
    for index, script in enumerate(scripts):
        live_metadata = (detached / script.name).lstat()
        authority_metadata = (state_dir / "modules" / str(index)).lstat()
        assert (live_metadata.st_dev, live_metadata.st_ino) != (
            authority_metadata.st_dev,
            authority_metadata.st_ino,
        )

    doctor_code, doctor_blocked = invoke(root, ["doctor"])
    assert doctor_code == 3
    assert doctor_blocked["error"]["kind"] == "startup_recovery_failed"
    assert "Restore scripts/runPlugin.sh" in doctor_blocked["next_action"]
    assert journal.is_file()

    original_rename(scripts_directory, external_tree)
    original_rename(detached, scripts_directory)
    root.chmod(resolved_root_state[0])
    os.utime(root, ns=resolved_root_state[1:])

    drifted_bash_times = (
        before[0][1].st_atime_ns + 7_000_000_000,
        before[0][1].st_mtime_ns + 9_000_000_000,
    )
    os.utime(scripts[0], ns=drifted_bash_times)

    drift_code, drift_result = invoke(root, ["doctor"])
    assert drift_code == 3, json.dumps(drift_result, indent=2)
    assert drift_result["error"]["kind"] == "startup_recovery_failed"
    assert journal.is_file()
    drifted_bash = scripts[0].lstat()
    assert drifted_bash.st_atime_ns == drifted_bash_times[0]
    assert drifted_bash.st_mtime_ns == drifted_bash_times[1]

    os.utime(
        scripts[0],
        ns=(before[0][1].st_atime_ns, before[0][1].st_mtime_ns),
    )
    drifted_powershell_times = (
        before[1][1].st_atime_ns + 11_000_000_000,
        before[1][1].st_mtime_ns + 13_000_000_000,
    )
    os.utime(scripts[1], ns=drifted_powershell_times)

    current_drift_code, current_drift_result = invoke(root, ["doctor"])
    assert current_drift_code == 3, json.dumps(current_drift_result, indent=2)
    assert current_drift_result["error"]["kind"] == "startup_recovery_failed"
    assert journal.is_file()
    drifted_powershell = scripts[1].lstat()
    assert drifted_powershell.st_atime_ns == drifted_powershell_times[0]
    assert drifted_powershell.st_mtime_ns == drifted_powershell_times[1]

    os.utime(
        scripts[1],
        ns=(before[1][1].st_atime_ns, before[1][1].st_mtime_ns),
    )
    root.chmod(resolved_root_state[0])
    os.utime(root, ns=resolved_root_state[1:])

    doctor_code, doctor_result = invoke(root, ["doctor"])

    assert doctor_code in {0, 1}
    assert doctor_result["error"] is None or (
        doctor_result["error"]["kind"] != "startup_recovery_failed"
    )
    assert any(
        warning["kind"] == "startup_recovery"
        for warning in doctor_result["warnings"]
    )
    assert not journal.exists()
    root_entries = contained_directory_entries_no_follow(root, root)
    assert not any(
        name.startswith(transaction_module.STATE_PREFIX) for name, _kind in root_entries
    )
    root_metadata = root.lstat()
    assert stat.S_IMODE(root_metadata.st_mode) == resolved_root_state[0]
    assert root_metadata.st_atime_ns == resolved_root_state[1]
    assert root_metadata.st_mtime_ns == resolved_root_state[2]
    restored_directory = scripts_directory.lstat()
    assert stat.S_IMODE(restored_directory.st_mode) == stat.S_IMODE(
        directory_before.st_mode
    )
    assert restored_directory.st_atime_ns == directory_before.st_atime_ns
    assert restored_directory.st_mtime_ns == directory_before.st_mtime_ns
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    external_directory = external_tree.lstat()
    assert stat.S_IMODE(external_directory.st_mode) == 0o711
    assert external_directory.st_atime_ns == external_directory_times[0]
    assert external_directory.st_mtime_ns == external_directory_times[1]
    for name, content, mode, times in zip(
        ("runPlugin.sh", "runPlugin.ps1"),
        external_contents,
        external_modes,
        external_times,
    ):
        external = external_tree / name
        external_content, metadata = read_contained_regular_bytes_no_follow(
            tmp_path, external
        )
        assert external_content == content
        assert stat.S_IMODE(metadata.st_mode) == mode
        assert metadata.st_atime_ns == times[0]
        assert metadata.st_mtime_ns == times[1]

    normal_code, normal_result = invoke(root, ["template", "sync", "--dry-run"])
    assert normal_code == 0
    assert normal_result["status"] == "success"


@pytest.mark.parametrize(
    ("failure_boundary", "boundary_failure"),
    (
        ("post_activation", None),
        ("post_link", OSError("post-link failure")),
        ("post_link", KeyboardInterrupt()),
        ("parent_check", OSError("post-link parent check failed")),
        ("parent_check", KeyboardInterrupt()),
        ("staged_cleanup", OSError("staged cleanup failed")),
        ("staged_cleanup", KeyboardInterrupt()),
    ),
)
def test_post_activation_parent_swap_keeps_monotonic_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
    boundary_failure: BaseException | None,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts_directory = root / "scripts"
    scripts = tuple(
        scripts_directory / name for name in ("runPlugin.sh", "runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    directory_before = scripts_directory.lstat()
    detached = tmp_path / "detached-post-activation-template"
    external_tree = tmp_path / "preserved-post-activation-external"
    external_contents = (
        b"external Bash after conditional activation\n",
        b"external PowerShell after conditional activation\n",
    )
    external_modes = (0o604, 0o640)
    external_times = (
        (1_700_000_081_000_000_000, 1_700_000_082_000_000_000),
        (1_700_000_083_000_000_000, 1_700_000_084_000_000_000),
    )
    external_directory_times = (
        1_700_000_085_000_000_000,
        1_700_000_086_000_000_000,
    )
    resolved_root_state = (
        0o705,
        1_700_000_087_000_000_000,
        1_700_000_088_000_000_000,
    )
    original_batch = Transaction.replace_regular_batch_if_matches
    original_rename = os.rename
    original_link = os.link
    original_unlink = os.unlink
    original_parent_match = (
        transaction_module._ConditionalBatchDescriptors.destination_parents_match
    )
    published: tuple[tuple[bytes, os.stat_result], ...] | None = None
    parent_replaced = False
    published_links = 0

    def replace_parent() -> None:
        nonlocal parent_replaced, published
        assert not parent_replaced
        parent_replaced = True
        published = tuple(
            read_contained_regular_bytes_no_follow(root, script)
            for script in scripts
        )
        original_rename(scripts_directory, detached)
        scripts_directory.mkdir()
        scripts_directory.chmod(0o711)
        for script, content, mode, times in zip(
            scripts, external_contents, external_modes, external_times
        ):
            script.write_bytes(content)
            script.chmod(mode)
            os.utime(script, ns=times)
        os.utime(scripts_directory, ns=external_directory_times)

    def replace_parent_after_activation(
        transaction: Transaction,
        replacements,
    ) -> None:
        original_batch(transaction, replacements)
        if failure_boundary == "post_activation":
            replace_parent()

    def fail_staged_cleanup(path, **kwargs) -> None:
        if (
            failure_boundary == "staged_cleanup"
            and not parent_replaced
            and Path(path).name == "0"
            and kwargs.get("dir_fd") is not None
        ):
            replace_parent()
            assert boundary_failure is not None
            raise boundary_failure
        original_unlink(path, **kwargs)

    def fail_after_final_link(source, destination, **kwargs) -> None:
        nonlocal published_links
        original_link(source, destination, **kwargs)
        if (
            Path(destination).name in {"runPlugin.sh", "runPlugin.ps1"}
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            published_links += 1
        if (
            failure_boundary == "post_link"
            and not parent_replaced
            and Path(destination).name == "runPlugin.ps1"
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            replace_parent()
            assert boundary_failure is not None
            raise boundary_failure

    def fail_post_link_parent_check(descriptors, root_path: Path) -> bool:
        if (
            failure_boundary == "parent_check"
            and published_links == 2
            and not parent_replaced
        ):
            replace_parent()
            assert boundary_failure is not None
            raise boundary_failure
        return original_parent_match(descriptors, root_path)

    monkeypatch.setattr(
        Transaction,
        "replace_regular_batch_if_matches",
        replace_parent_after_activation,
    )
    monkeypatch.setattr(os, "link", fail_after_final_link)
    monkeypatch.setattr(os, "unlink", fail_staged_cleanup)
    monkeypatch.setattr(
        transaction_module._ConditionalBatchDescriptors,
        "destination_parents_match",
        fail_post_link_parent_check,
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert parent_replaced
    assert published is not None
    assert code == 3, json.dumps(result, indent=2)
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == []
    interrupted = isinstance(boundary_failure, KeyboardInterrupt)
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    journal = root / transaction_module.JOURNAL_NAME
    raw = json.loads(journal.read_text(encoding="utf-8"))
    state_dir = root / f"{transaction_module.STATE_PREFIX}{raw['id']}"
    assert (
        state_dir
        / transaction_module.CONDITIONAL_RETENTION_AUTHORITY_NAME
    ).is_file()
    assert transaction_module._conditional_conflict_is_durable(root, raw)
    for script, content, mode, times in zip(
        scripts, external_contents, external_modes, external_times
    ):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == content
        assert stat.S_IMODE(current[1].st_mode) == mode
        assert current[1].st_atime_ns == times[0]
        assert current[1].st_mtime_ns == times[1]
    for script, expected in zip(scripts, published):
        current = read_contained_regular_bytes_no_follow(
            tmp_path, detached / script.name
        )
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns

    blocked_code, blocked_result = invoke(root, ["doctor"])
    assert blocked_code == 3
    assert blocked_result["error"]["kind"] == "startup_recovery_failed"
    assert journal.is_file()

    original_rename(scripts_directory, external_tree)
    original_rename(detached, scripts_directory)
    for script, expected in zip(scripts, before):
        script.write_bytes(expected[0])
        script.chmod(stat.S_IMODE(expected[1].st_mode))
        os.utime(
            script,
            ns=(expected[1].st_atime_ns, expected[1].st_mtime_ns),
        )
    scripts_directory.chmod(stat.S_IMODE(directory_before.st_mode))
    os.utime(
        scripts_directory,
        ns=(directory_before.st_atime_ns, directory_before.st_mtime_ns),
    )
    root.chmod(resolved_root_state[0])
    os.utime(root, ns=resolved_root_state[1:])

    doctor_code, doctor_result = invoke(root, ["doctor"])

    assert doctor_code in {0, 1}
    assert doctor_result["error"] is None or (
        doctor_result["error"]["kind"] != "startup_recovery_failed"
    )
    assert not journal.exists()
    root_metadata = root.lstat()
    assert stat.S_IMODE(root_metadata.st_mode) == resolved_root_state[0]
    assert root_metadata.st_atime_ns == resolved_root_state[1]
    assert root_metadata.st_mtime_ns == resolved_root_state[2]
    for name, content, mode, times in zip(
        ("runPlugin.sh", "runPlugin.ps1"),
        external_contents,
        external_modes,
        external_times,
    ):
        current = read_contained_regular_bytes_no_follow(
            tmp_path, external_tree / name
        )
        assert current[0] == content
        assert stat.S_IMODE(current[1].st_mode) == mode
        assert current[1].st_atime_ns == times[0]
        assert current[1].st_mtime_ns == times[1]
    dry_run_code, dry_run_result = invoke(
        root, ["template", "sync", "--dry-run"]
    )
    assert dry_run_code == 0
    assert dry_run_result["status"] == "success"


@pytest.mark.parametrize("target_name", ("runPlugin.sh", "runPlugin.ps1"))
@pytest.mark.parametrize(
    "failure_boundary",
    ("candidate_cleanup", "verification", "marker_publication"),
)
@pytest.mark.parametrize(
    "boundary_failure",
    (OSError("candidate cleanup failed"), KeyboardInterrupt()),
)
def test_post_publication_failure_retains_conditional_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    failure_boundary: str,
    boundary_failure: BaseException,
) -> None:
    root = plugin(tmp_path / "plugin")
    scripts_directory = root / "scripts"
    scripts = tuple(
        scripts_directory / name for name in ("runPlugin.sh", "runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    directory_before = scripts_directory.lstat()
    detached = tmp_path / f"detached-{failure_boundary}-{target_name}"
    external_tree = tmp_path / f"preserved-{failure_boundary}-{target_name}"
    external_contents = (
        b"external Bash survives post-publication failure\n",
        b"external PowerShell survives post-publication failure\n",
    )
    external_modes = (0o604, 0o640)
    external_times = (
        (1_700_000_071_000_000_000, 1_700_000_072_000_000_000),
        (1_700_000_073_000_000_000, 1_700_000_074_000_000_000),
    )
    external_directory_times = (
        1_700_000_075_000_000_000,
        1_700_000_076_000_000_000,
    )
    original_rename = os.rename
    original_unlink = os.unlink
    original_replace = os.replace
    original_matches_exact = transaction_module._relative_regular_matches_exact
    retention_failed = False
    parent_replaced = False

    def fail_after_retention(source, destination, **kwargs) -> None:
        nonlocal retention_failed
        original_rename(source, destination, **kwargs)
        if (
            not retention_failed
            and Path(source).name == target_name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            retention_failed = True
            raise RuntimeError("restore the current retained member")

    def replace_parent() -> None:
        nonlocal parent_replaced
        parent_replaced = True
        original_rename(scripts_directory, detached)
        scripts_directory.mkdir()
        scripts_directory.chmod(0o711)
        for script, content, mode, times in zip(
            scripts, external_contents, external_modes, external_times
        ):
            script.write_bytes(content)
            script.chmod(mode)
            os.utime(script, ns=times)
        os.utime(scripts_directory, ns=external_directory_times)

    def replace_parent_and_fail() -> None:
        replace_parent()
        raise boundary_failure

    def fail_candidate_cleanup(path, **kwargs) -> None:
        if (
            failure_boundary == "candidate_cleanup"
            and retention_failed
            and not parent_replaced
            and Path(path).name.startswith(".restore-")
            and kwargs.get("dir_fd") is not None
        ):
            replace_parent_and_fail()
        original_unlink(path, **kwargs)

    def fail_destination_verification(name, descriptor, content, metadata):
        if (
            failure_boundary in {"verification", "marker_publication"}
            and retention_failed
            and not parent_replaced
            and name == target_name
        ):
            if failure_boundary == "verification":
                replace_parent_and_fail()
            replace_parent()
        return original_matches_exact(name, descriptor, content, metadata)

    def fail_marker_publication(source, destination, **kwargs) -> None:
        if (
            failure_boundary == "marker_publication"
            and parent_replaced
            and Path(destination).name
            == transaction_module.CONDITIONAL_CONFLICT_AUTHORITY_NAME.split("/")[-1]
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            raise boundary_failure
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", fail_after_retention)
    monkeypatch.setattr(os, "unlink", fail_candidate_cleanup)
    monkeypatch.setattr(os, "replace", fail_marker_publication)
    monkeypatch.setattr(
        transaction_module,
        "_relative_regular_matches_exact",
        fail_destination_verification,
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(boundary_failure, KeyboardInterrupt)
    assert retention_failed
    assert parent_replaced
    assert code == 3, result
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == []
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    journal = root / transaction_module.JOURNAL_NAME
    raw = json.loads(journal.read_text(encoding="utf-8"))
    state_dir = root / f"{transaction_module.STATE_PREFIX}{raw['id']}"
    expected_entry_count = 1 if target_name == "runPlugin.sh" else 2
    assert len(raw["entries"]) == expected_entry_count
    assert transaction_module._conditional_conflict_is_durable(root, raw)
    for index, entry in enumerate(raw["entries"]):
        authority = state_dir / "modules" / str(index)
        live = detached / Path(entry["path"]).name
        authority_metadata = authority.lstat()
        live_metadata = live.lstat()
        assert (authority_metadata.st_dev, authority_metadata.st_ino) != (
            live_metadata.st_dev,
            live_metadata.st_ino,
        )

    detached_metadata = detached.lstat()
    assert stat.S_IMODE(detached_metadata.st_mode) == stat.S_IMODE(
        directory_before.st_mode
    )
    assert detached_metadata.st_atime_ns == directory_before.st_atime_ns
    assert detached_metadata.st_mtime_ns == directory_before.st_mtime_ns
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(
            tmp_path, detached / script.name
        )
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    visible_metadata = scripts_directory.lstat()
    assert stat.S_IMODE(visible_metadata.st_mode) == 0o711
    assert visible_metadata.st_atime_ns == external_directory_times[0]
    assert visible_metadata.st_mtime_ns == external_directory_times[1]
    for script, content, mode, times in zip(
        scripts, external_contents, external_modes, external_times
    ):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == content
        assert stat.S_IMODE(current[1].st_mode) == mode
        assert current[1].st_atime_ns == times[0]
        assert current[1].st_mtime_ns == times[1]

    blocked_code, blocked_result = invoke(root, ["doctor"])
    assert blocked_code == 3
    assert blocked_result["error"]["kind"] == "startup_recovery_failed"

    original_rename(scripts_directory, external_tree)
    original_rename(detached, scripts_directory)
    incomplete_content = (
        f"incomplete operator restoration for {target_name}\n".encode()
    )
    incomplete_mode = 0o600
    incomplete_times = (
        1_700_000_077_000_000_000,
        1_700_000_078_000_000_000,
    )
    incomplete = scripts_directory / target_name
    incomplete.write_bytes(incomplete_content)
    incomplete.chmod(incomplete_mode)
    os.utime(incomplete, ns=incomplete_times)

    incomplete_code, incomplete_result = invoke(root, ["doctor"])

    assert incomplete_code == 3
    assert incomplete_result["error"]["kind"] == "startup_recovery_failed"
    assert journal.is_file()
    incomplete_current = read_contained_regular_bytes_no_follow(root, incomplete)
    assert incomplete_current[0] == incomplete_content
    assert stat.S_IMODE(incomplete_current[1].st_mode) == incomplete_mode
    assert incomplete_current[1].st_atime_ns == incomplete_times[0]
    assert incomplete_current[1].st_mtime_ns == incomplete_times[1]

    target_index = 0 if target_name == "runPlugin.sh" else 1
    incomplete.write_bytes(before[target_index][0])
    incomplete.chmod(stat.S_IMODE(before[target_index][1].st_mode))
    os.utime(
        incomplete,
        ns=(
            before[target_index][1].st_atime_ns,
            before[target_index][1].st_mtime_ns,
        ),
    )
    resolved_root_state = (
        0o705,
        1_700_000_079_000_000_000,
        1_700_000_080_000_000_000,
    )
    root.chmod(resolved_root_state[0])
    os.utime(root, ns=resolved_root_state[1:])
    doctor_code, doctor_result = invoke(root, ["doctor"])

    assert doctor_code in {0, 1}
    assert doctor_result["error"] is None or (
        doctor_result["error"]["kind"] != "startup_recovery_failed"
    )
    assert not journal.exists()
    root_entries = contained_directory_entries_no_follow(root, root)
    assert not any(
        name.startswith(transaction_module.STATE_PREFIX) for name, _kind in root_entries
    )
    resolved_root = root.lstat()
    assert stat.S_IMODE(resolved_root.st_mode) == resolved_root_state[0]
    assert resolved_root.st_atime_ns == resolved_root_state[1]
    assert resolved_root.st_mtime_ns == resolved_root_state[2]
    dry_run_code, dry_run_result = invoke(
        root, ["template", "sync", "--dry-run"]
    )
    assert dry_run_code == 0
    assert dry_run_result["status"] == "success"


@pytest.mark.parametrize("publication_boundary", ("authority", "journal"))
@pytest.mark.parametrize(
    "boundary_failure",
    (RuntimeError("authority publication failure"), KeyboardInterrupt()),
)
def test_failed_revocation_republishes_a_valid_conflict_authority_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_boundary: str,
    boundary_failure: BaseException,
) -> None:
    root = plugin(tmp_path)
    scripts = tuple(
        root / relative
        for relative in ("scripts/runPlugin.sh", "scripts/runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    journal = root / ".supernote-module-transaction.json"
    original_rename = os.rename
    original_authority_write = transaction_module._write_entry_authority
    original_json_write = transaction_module._write_json_atomic
    armed = False
    injected = False

    def interrupt_after_retention(source, destination, **kwargs) -> None:
        nonlocal armed
        original_rename(source, destination, **kwargs)
        if (
            not armed
            and Path(source).name == "runPlugin.sh"
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            armed = True
            raise RuntimeError("post-retention trigger")

    def fail_after_authority_write(
        state_dir, identifier, entries, authority_name
    ) -> None:
        nonlocal injected
        original_authority_write(
            state_dir, identifier, entries, authority_name
        )
        if (
            publication_boundary == "authority"
            and armed
            and not injected
            and entries == []
        ):
            injected = True
            raise boundary_failure

    def fail_after_journal_write(path, data) -> None:
        nonlocal injected
        original_json_write(path, data)
        if (
            publication_boundary == "journal"
            and armed
            and not injected
            and Path(path) == journal
            and isinstance(data, dict)
            and data.get("entries") == []
        ):
            injected = True
            raise boundary_failure

    monkeypatch.setattr(os, "rename", interrupt_after_retention)
    monkeypatch.setattr(
        transaction_module, "_write_entry_authority", fail_after_authority_write
    )
    monkeypatch.setattr(
        transaction_module, "_write_json_atomic", fail_after_journal_write
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(boundary_failure, KeyboardInterrupt)
    assert injected
    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == []
    assert result["recovery"]["command"] == ["sn-module-gen", "doctor"]
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == expected[0]
        assert stat.S_IMODE(current[1].st_mode) == stat.S_IMODE(
            expected[1].st_mode
        )
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns

    persisted = json.loads(journal.read_text(encoding="utf-8"))
    assert persisted["phase"] == "conflict"
    assert len(persisted["entries"]) == 1
    transaction_module._validate_transaction_entries(root, persisted)
    authority = root / f".supernote-module-transaction-{persisted['id']}"
    assert (authority / persisted["entry_authority"]).is_file()
    recovery = recover_pending(root)
    assert recovery.rollback.status == "partial"
    assert recovery.recovery_command == ["sn-module-gen", "doctor"]
    transaction_module._validate_transaction_entries(root, persisted)


@pytest.mark.parametrize(
    "failure", (RuntimeError("ambiguous retention"), KeyboardInterrupt())
)
def test_ambiguous_post_retention_state_keeps_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    before = read_contained_regular_bytes_no_follow(root, bash)
    external = b"external entry recreated after retention\n"
    external_mode = 0o604
    external_atime = 1_700_000_031_000_000_000
    external_mtime = 1_700_000_032_000_000_000
    original_rename = os.rename
    injected = False

    def recreate_after_move(source, destination, **kwargs) -> None:
        nonlocal injected
        original_rename(source, destination, **kwargs)
        if (
            not injected
            and Path(source).name == bash.name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            injected = True
            bash.write_bytes(external)
            bash.chmod(external_mode)
            os.utime(bash, ns=(external_atime, external_mtime))
            raise failure

    monkeypatch.setattr(os, "rename", recreate_after_move)

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(failure, KeyboardInterrupt)
    assert injected
    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["error"]["kind"] == (
        "cancellation_rollback_partial"
        if interrupted
        else "template_sync_rollback_partial"
    )
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    journal = root / ".supernote-module-transaction.json"
    assert Path(result["metadata"]["recovery_path"]) == journal
    assert result["recovery"]["command"] == ["sn-module-gen", "doctor"]
    raw = json.loads(journal.read_text(encoding="utf-8"))
    assert raw["phase"] == "conflict"
    restore = Path(raw["entries"][0]["restore"])
    restored_content, restored_metadata = read_contained_regular_bytes_no_follow(
        root, restore
    )
    assert restored_content == before[0]
    assert stat.S_IMODE(restored_metadata.st_mode) == stat.S_IMODE(
        before[1].st_mode
    )
    content, metadata = read_contained_regular_bytes_no_follow(root, bash)
    assert content == external
    assert stat.S_IMODE(metadata.st_mode) == external_mode
    assert metadata.st_atime_ns == external_atime
    assert metadata.st_mtime_ns == external_mtime


@pytest.mark.skipif(
    not filesystem_module._descriptor_relative_io_supported(),
    reason="requires a host with the complete descriptor primitive set",
)
@pytest.mark.parametrize("missing", (os.stat, os.link))
def test_descriptor_capability_requires_no_follow_keyword_support(
    monkeypatch: pytest.MonkeyPatch,
    missing,
) -> None:
    monkeypatch.setattr(
        os,
        "supports_follow_symlinks",
        set(os.supports_follow_symlinks) - {missing},
    )

    assert not filesystem_module._detect_descriptor_relative_io_support()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symbolic links")
def test_fresh_recovery_rejects_substituted_capture_ancestor_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    original = os.link
    bash_published = False

    def conflict_twice(source, destination, **kwargs) -> None:
        nonlocal bash_published
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.name == powershell.name:
            powershell.write_bytes(b"PowerShell conflict retained on disk\n")
            original(source, destination, **kwargs)
            return
        if (
            bash_published
            and source_path.name == "0"
            and destination_path.name == bash.name
            and kwargs.get("src_dir_fd") is not None
        ):
            bash.write_bytes(b"newer Bash destination remains exact\n")
            original(source, destination, **kwargs)
            return
        original(source, destination, **kwargs)
        if destination_path.name == bash.name:
            bash_published = True
            bash.write_bytes(b"captured Bash entry remains recoverable\n")

    monkeypatch.setattr(os, "link", conflict_twice)
    code, result = invoke(root, ["template", "sync", "--yes"])
    assert code == 3, result
    assert result["rollback"]["status"] == "partial"
    monkeypatch.undo()

    journal = root / ".supernote-module-transaction.json"
    state_dir = next(root.glob(".supernote-module-transaction-*"))
    captures = state_dir / "captures"
    detached = state_dir / "detached-captures"
    os.rename(captures, detached)
    external_directory = tmp_path / "fresh-recovery-external"
    external_directory.mkdir()
    sentinel = external_directory / "0"
    sentinel.write_bytes(b"fresh recovery sentinel remains exact\n")
    sentinel.chmod(0o640)
    sentinel_atime = 1_700_000_021_000_000_000
    sentinel_mtime = 1_700_000_022_000_000_000
    os.utime(
        sentinel,
        ns=(sentinel_atime, sentinel_mtime),
        follow_symlinks=False,
    )
    captures.symlink_to(external_directory, target_is_directory=True)
    bash_before = bash.read_bytes()
    powershell_before = powershell.read_bytes()

    with pytest.raises(PartialFailure, match="Automatic recovery could not complete"):
        recover_pending(root)

    assert bash.read_bytes() == bash_before
    assert powershell.read_bytes() == powershell_before
    assert (detached / "0").read_bytes() == b"captured Bash entry remains recoverable\n"
    sentinel_content, sentinel_stat = read_contained_regular_bytes_no_follow(
        tmp_path, sentinel
    )
    assert sentinel_content == b"fresh recovery sentinel remains exact\n"
    assert stat.S_IMODE(sentinel_stat.st_mode) == 0o640
    assert sentinel_stat.st_atime_ns == sentinel_atime
    assert sentinel_stat.st_mtime_ns == sentinel_mtime
    assert journal.is_file()


def test_template_sync_fails_closed_without_descriptor_relative_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    scripts = tuple(
        root / relative
        for relative in ("scripts/runPlugin.sh", "scripts/runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    external = tmp_path / "unsupported-capability-external"
    external.mkdir()
    sentinel = external / "0"
    sentinel.write_bytes(b"unsupported capability sentinel remains exact\n")
    sentinel.chmod(0o604)
    sentinel_atime = 1_700_000_023_000_000_000
    sentinel_mtime = 1_700_000_024_000_000_000
    os.utime(sentinel, ns=(sentinel_atime, sentinel_mtime))

    monkeypatch.setattr(
        transaction_module, "_descriptor_relative_io_supported", lambda: False
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["status"] == "failure"
    assert result["error"]["kind"] == "filesystem_failed"
    assert result["rollback"]["status"] == "not_needed"
    assert result["actual_changes"] == []
    for script, (content, metadata) in zip(scripts, before):
        current_content, current_metadata = read_contained_regular_bytes_no_follow(
            root, script
        )
        assert current_content == content
        assert stat.S_IMODE(current_metadata.st_mode) == stat.S_IMODE(metadata.st_mode)
        assert current_metadata.st_atime_ns == metadata.st_atime_ns
        assert current_metadata.st_mtime_ns == metadata.st_mtime_ns
    sentinel_content, sentinel_stat = read_contained_regular_bytes_no_follow(
        tmp_path, sentinel
    )
    assert sentinel_content == b"unsupported capability sentinel remains exact\n"
    assert stat.S_IMODE(sentinel_stat.st_mode) == 0o604
    assert sentinel_stat.st_atime_ns == sentinel_atime
    assert sentinel_stat.st_mtime_ns == sentinel_mtime
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name != "nt", reason="native Windows capability boundary")
def test_windows_template_sync_never_selects_pathname_capture(
    tmp_path: Path,
) -> None:
    root = plugin(tmp_path)
    scripts = tuple(
        root / relative
        for relative in ("scripts/runPlugin.sh", "scripts/runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script) for script in scripts
    )
    external = tmp_path / "windows-reparse-external"
    external.mkdir()
    sentinel = external / "0"
    sentinel.write_bytes(b"Windows external sentinel remains exact\n")
    probe = tmp_path / "windows-reparse-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pass

    assert not transaction_module._descriptor_relative_io_supported()
    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 1
    assert result["error"]["kind"] == "filesystem_failed"
    assert result["rollback"]["status"] == "not_needed"
    assert result["actual_changes"] == []
    for script, expected in zip(scripts, before):
        current = read_contained_regular_bytes_no_follow(root, script)
        assert current[0] == expected[0]
        assert current[1].st_mode == expected[1].st_mode
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    assert sentinel.read_bytes() == b"Windows external sentinel remains exact\n"
    assert not (root / ".supernote-module-transaction.json").exists()
    assert not list(root.glob(".supernote-module-transaction-*"))


@pytest.mark.skipif(os.name == "nt", reason="creates a POSIX partial authority")
def test_fresh_recovery_fails_closed_without_descriptor_relative_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    original_link = os.link
    bash_published = False

    def conflict_twice(source, destination, **kwargs) -> None:
        nonlocal bash_published
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.name == powershell.name:
            powershell.write_bytes(b"PowerShell recovery conflict remains exact\n")
            original_link(source, destination, **kwargs)
            return
        if (
            bash_published
            and source_path.name == "0"
            and destination_path.name == bash.name
            and kwargs.get("src_dir_fd") is not None
        ):
            bash.write_bytes(b"newer Bash recovery destination remains exact\n")
            original_link(source, destination, **kwargs)
            return
        original_link(source, destination, **kwargs)
        if destination_path.name == bash.name:
            bash_published = True
            bash.write_bytes(b"captured Bash recovery entry remains exact\n")

    monkeypatch.setattr(os, "link", conflict_twice)
    code, result = invoke(root, ["template", "sync", "--yes"])
    assert code == 3, result
    monkeypatch.undo()

    journal = root / ".supernote-module-transaction.json"
    state_dir = next(root.glob(".supernote-module-transaction-*"))
    capture = state_dir / "captures/0"
    before = (
        read_contained_regular_bytes_no_follow(root, bash),
        read_contained_regular_bytes_no_follow(root, powershell),
        read_contained_regular_bytes_no_follow(root, journal),
        read_contained_regular_bytes_no_follow(root, capture),
    )
    monkeypatch.setattr(
        transaction_module, "_descriptor_relative_io_supported", lambda: False
    )

    with pytest.raises(PartialFailure, match="Automatic recovery could not complete"):
        recover_pending(root)

    for path, expected in zip((bash, powershell), before[:2]):
        current = read_contained_regular_bytes_no_follow(root, path)
        assert current[0] == expected[0]
        assert current[1].st_mode == expected[1].st_mode
        assert current[1].st_atime_ns == expected[1].st_atime_ns
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns
    for path, expected in zip((journal, capture), before[2:]):
        current = read_contained_regular_bytes_no_follow(root, path)
        assert current[0] == expected[0]
        assert current[1].st_mode == expected[1].st_mode
        assert current[1].st_mtime_ns == expected[1].st_mtime_ns


@pytest.mark.parametrize(
    "failure", (RuntimeError("write failed"), KeyboardInterrupt())
)
def test_post_activation_failure_retains_exact_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root = plugin(tmp_path)
    scripts = tuple(
        root / relative
        for relative in ("scripts/runPlugin.sh", "scripts/runPlugin.ps1")
    )
    before = tuple(
        read_contained_regular_bytes_no_follow(root, path) for path in scripts
    )
    original = Transaction.replace_regular_batch_if_matches
    calls = 0

    def fail_after_replace(self: Transaction, replacements) -> None:
        nonlocal calls
        original(self, replacements)
        calls += 1
        if calls == 1:
            raise failure

    monkeypatch.setattr(
        Transaction, "replace_regular_batch_if_matches", fail_after_replace
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    interrupted = isinstance(failure, KeyboardInterrupt)
    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert len(result["actual_changes"]) == 2
    assert result["cancellation"]["requested"] is interrupted
    assert result["cancellation"]["status"] == (
        "partial" if interrupted else "not_requested"
    )
    journal = root / ".supernote-module-transaction.json"
    assert journal.is_file()
    recovery = recover_pending(root)
    assert recovery.rollback.status == "partial"
    for path, expected in zip(scripts, before):
        path.write_bytes(expected[0])
        path.chmod(stat.S_IMODE(expected[1].st_mode))
        os.utime(
            path,
            ns=(expected[1].st_atime_ns, expected[1].st_mtime_ns),
        )
    recovery = recover_pending(root)
    assert recovery.rollback.status == "not_needed"
    assert not journal.exists()


@pytest.mark.parametrize(
    "failure", (RuntimeError("write failed"), KeyboardInterrupt())
)
def test_partial_template_rollback_reports_residue_and_real_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    powershell = root / "scripts/runPlugin.ps1"
    before = tuple(
        read_contained_regular_bytes_no_follow(root, script)
        for script in (bash, powershell)
    )
    original_replace = Transaction.replace_regular_batch_if_matches
    calls = 0

    def fail_after_replace(self: Transaction, replacements) -> None:
        nonlocal calls
        original_replace(self, replacements)
        calls += 1
        if calls == 1:
            raise failure

    monkeypatch.setattr(
        Transaction, "replace_regular_batch_if_matches", fail_after_replace
    )
    monkeypatch.setattr(
        Transaction,
        "rollback",
        lambda self, **_kwargs: RollbackResult(True, "partial", []),
    )

    code, result = invoke(root, ["template", "sync", "--yes"])

    assert code == 3
    assert result["status"] == "partial"
    assert result["rollback"]["status"] == "partial"
    assert result["actual_changes"] == [
        {
            "path": str(bash),
            "action": "update",
            "ownership": "rollback_residue",
        },
        {
            "path": str(powershell),
            "action": "update",
            "ownership": "rollback_residue",
        },
    ]
    recovery_path = Path(result["metadata"]["recovery_path"])
    assert recovery_path == root / ".supernote-module-transaction.json"
    assert recovery_path.is_file()
    assert str(recovery_path) in result["next_action"]
    assert result["cancellation"]["requested"] is isinstance(
        failure, KeyboardInterrupt
    )
    assert result["cancellation"]["status"] == (
        "partial" if isinstance(failure, KeyboardInterrupt) else "not_requested"
    )

    monkeypatch.undo()
    recovery = recover_pending(root)
    assert recovery.rollback.status == "partial"
    assert recovery_path.exists()
    for script, expected in zip((bash, powershell), before):
        script.write_bytes(expected[0])
        script.chmod(stat.S_IMODE(expected[1].st_mode))
        os.utime(
            script,
            ns=(expected[1].st_atime_ns, expected[1].st_mtime_ns),
        )
    recovery = recover_pending(root)
    assert recovery.rollback.status == "not_needed"
    assert bash.read_text() == OLD_BASH
    assert powershell.read_text() == OLD_POWERSHELL
    assert not recovery_path.exists()


def test_human_interruption_after_activation_reports_retained_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = plugin(tmp_path)
    bash = root / "scripts/runPlugin.sh"
    before = bash.read_bytes()
    original = Transaction.replace_regular_batch_if_matches

    def interrupt_after_replace(self: Transaction, replacements) -> None:
        original(self, replacements)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        Transaction, "replace_regular_batch_if_matches", interrupt_after_replace
    )

    code, stdout, stderr = invoke_human(root, ["template", "sync", "--yes"])

    assert code == 3
    assert stdout == ""
    assert "Rollback: Partial" in stderr
    assert "run Doctor" in stderr
    assert str(root / ".supernote-module-transaction.json") in stderr
    assert bash.read_bytes() != before
