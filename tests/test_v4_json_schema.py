from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.feature_cli_operations import FeatureCliOperationService
from supernote_module_generator.filesystem import (
    ProtectedSourceGuard,
    ProtectedSourceRestoreError,
    lexists,
    protected_directory_metadata,
    remove_entry_no_follow,
    restore_protected_source_backup,
    source_tree_inventory,
)
from supernote_module_generator.models import (
    Change,
    CommandResult,
    DoctorCheckResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
    SubprocessError,
    ValidationResult,
)
from supernote_module_generator.cli_operations import CliOperationService
from supernote_module_generator.v4_validation import (
    V4ValidationResult,
    V4Validator,
    ValidationIssue,
)


SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "src/supernote_module_generator/schemas/command-result.schema.json"
)


def plugin(root: Path, *, npm_lock: bool = False) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n")
    (root / "package.json").write_text(
        json.dumps({"name": "fixture", "dependencies": {}}) + "\n"
    )
    (root / "android/settings.gradle").write_text("include ':app'\n")
    (root / "android/app/build.gradle").write_text("plugins {}\n")
    wrapper = root / "android/gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    if npm_lock:
        (root / "package-lock.json").write_text("{}\n")
    return root


def invoke(root: Path, arguments: list[str]) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    code = main(
        ["--json", *arguments],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        cwd=root,
    )
    return code, json.loads(stdout.getvalue())


def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def assert_valid(value: dict[str, object]) -> None:
    validator().validate(value)


def test_actual_public_command_families_conform_to_published_schema(tmp_path: Path):
    root = plugin(tmp_path)
    envelopes = []

    code, added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    envelopes.append(added)

    for arguments in (
        ["update", "alpha", "--dry-run", "--diff"],
        ["check"],
        ["validate", "--all"],
        ["repair", "--diff"],
        ["doctor"],
    ):
        _, envelope = invoke(root, arguments)
        envelopes.append(envelope)

    code, removed = invoke(root, ["remove", "alpha", "--skip-install", "--yes"])
    assert code == 0
    envelopes.append(removed)

    assert {str(item["command"]) for item in envelopes} >= {
        "add",
        "update",
        "check",
        "validate",
        "repair",
        "doctor",
        "remove",
    }
    for envelope in envelopes:
        assert_valid(envelope)


def test_staged_repair_validation_failure_conforms_to_schema(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime\n", "")
    )
    issue = ValidationIssue(
        "SNMG_STAGED_REPAIR_REJECTED",
        "error",
        "plugin",
        "staged repair sentinel",
        path="android/settings.gradle",
        suggested_command="Fix the staged repair sentinel and rerun repair.",
    )
    monkeypatch.setattr(
        V4Validator,
        "validate",
        lambda self, **kwargs: V4ValidationResult(
            "failure",
            "1" * 64,
            (issue,),
            diagnostics=("/tmp/staged-repair-diagnostics.log",),
        ),
    )

    code, envelope = invoke(root, ["repair", "--yes"])

    assert code == 1
    assert_valid(envelope)
    assert envelope["error"]["kind"] == "repair_validation_failed"
    assert envelope["error"]["phase"] == "precommit"
    assert envelope["issues"] == [issue.manifest()]
    assert envelope["actual_changes"] == []
    assert envelope["rollback"]["status"] == "completed"


def test_staged_repair_partial_rollback_separates_plan_from_actual_residue(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    settings = root / "android/settings.gradle"
    settings.write_text(
        settings.read_text().replace("// end supernote-module-v4-runtime\n", "")
    )
    preview_code, preview = invoke(root, ["repair", "--diff"])
    assert preview_code == 0
    issue = ValidationIssue(
        "SNMG_STAGED_REPAIR_REJECTED",
        "error",
        "plugin",
        "staged repair sentinel",
        path="android/settings.gradle",
    )
    monkeypatch.setattr(
        V4Validator,
        "validate",
        lambda self, **kwargs: V4ValidationResult(
            "failure", "1" * 64, (issue,)
        ),
    )
    original = CliOperationService._rollback_with_verification
    residue = Change("android/settings.gradle", "update", "rollback_residue")

    def partial_rollback(self, transaction, baseline, directory_metadata):
        rollback, _actual = original(
            self, transaction, baseline, directory_metadata
        )
        return RollbackResult(True, "partial", rollback.restored), [residue]

    monkeypatch.setattr(
        CliOperationService,
        "_rollback_with_verification",
        partial_rollback,
    )

    code, envelope = invoke(root, ["repair", "--yes"])

    assert code == 3
    assert_valid(envelope)
    assert envelope["changes"] == preview["changes"]
    assert envelope["actual_changes"] == [residue.to_dict()]
    assert envelope["rollback"]["status"] == "partial"


def test_doctor_capability_states_are_required_by_the_published_schema(
    tmp_path: Path,
):
    root = plugin(tmp_path)
    _, envelope = invoke(root, ["doctor"])
    assert_valid(envelope)
    doctor = envelope["doctor"]
    assert isinstance(doctor, dict)
    checks = doctor["checks"]
    assert isinstance(checks, list) and checks
    metadata = checks[0]["metadata"]
    assert isinstance(metadata, dict)
    metadata.pop("device_tested")

    with pytest.raises(JsonSchemaValidationError):
        assert_valid(envelope)


@pytest.mark.parametrize(
    ("status", "exit_code", "cancellation_status"),
    (
        ("success", 0, "not_requested"),
        ("failure", 1, "not_requested"),
        ("cancelled", 130, "completed"),
        ("partial", 3, "partial"),
    ),
)
def test_doctor_build_cli_preserves_authoritative_check_contract(
    tmp_path: Path,
    monkeypatch,
    status: str,
    exit_code: int,
    cancellation_status: str,
):
    root = plugin(tmp_path)
    validation = ValidationResult(
        structural="passed",
        integration="passed",
        dependency_link="passed",
        build="passed" if status == "success" else "failed",
        issues=(
            []
            if status == "success"
            else [
                {
                    "code": f"SNMG_{status.upper()}",
                    "severity": "error",
                    "scope": "toolchain",
                    "message": f"{status} sentinel",
                }
            ]
        ),
    )
    authoritative_validation = None if status == "cancelled" else validation
    nested_error = (
        None
        if status in {"success", "cancelled"}
        else ErrorInfo(f"nested_{status}", "build", f"{status} root cause")
    )
    nested_recovery = (
        None
        if status == "cancelled"
        else RecoveryAction(
            f"{status} recovery sentinel",
            ["sn-module-gen", "check", "--build"],
        )
    )
    nested_next_action = (
        None if status == "cancelled" else f"{status} next action sentinel"
    )
    nested = CommandResult(
        "check",
        status=status,
        exit_code=exit_code,
        validation=authoritative_validation,
        diagnostics=([] if status == "cancelled" else [f"/tmp/{status}-diagnostic.log"]),
        next_action=nested_next_action,
        error=nested_error,
        recovery=nested_recovery,
        rollback=RollbackResult(
            status in {"cancelled", "partial"},
            "completed" if status == "cancelled" else "partial"
            if status == "partial"
            else "not_needed",
            [f"{status}-restored"] if status == "cancelled" else [],
        ),
        requested_targets=[f"{status}-requested"],
        affected_targets=[f"{status}-affected"],
        metadata={
            "cancellation_requested": status in {"cancelled", "partial"},
            **(
                {"cancellation_status": cancellation_status}
                if status != "cancelled"
                else {}
            ),
            "cancellation_message": (
                f"{status} cancellation sentinel"
                if status in {"cancelled", "partial"}
                else None
            ),
        },
    )

    monkeypatch.setattr(
        DoctorService,
        "_javascript_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_android_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_native_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_jsi_runtime_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "supernote_module_generator.cli_operations.CliOperationService.check",
        lambda *args, **kwargs: nested,
    )

    code, envelope = invoke(root, ["doctor", "--build"])

    assert code == exit_code
    assert envelope["status"] == status
    assert envelope["validation"] == (
        authoritative_validation.to_dict()
        if authoritative_validation is not None
        else None
    )
    assert envelope["diagnostics"] == (
        [] if status == "cancelled" else [f"/tmp/{status}-diagnostic.log"]
    )
    assert envelope["next_action"] == nested_next_action
    assert envelope["recovery"] == (
        nested_recovery.to_dict() if nested_recovery is not None else None
    )
    assert envelope["requested_targets"] == [f"{status}-requested"]
    assert envelope["affected_targets"] == [f"{status}-affected"]
    assert envelope["error"] == (
        nested_error.to_dict() if nested_error is not None else None
    )
    assert envelope["cancellation"]["status"] == cancellation_status
    assert_valid(envelope)


def test_doctor_build_keeps_successful_nested_evidence_when_another_check_fails(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)
    validation = ValidationResult(
        structural="passed",
        integration="passed",
        dependency_link="passed",
        build="passed",
    )
    nested = CommandResult(
        "check",
        validation=validation,
        diagnostics=["/tmp/nested-success.log"],
        requested_targets=["nested-requested"],
        affected_targets=["nested-affected"],
        metadata={
            "generation_id": "nested-generation",
            "build_duration_ms": 1234,
            "built": True,
            "nested_sentinel": "preserved",
        },
    )
    unrelated_failure = DoctorCheckResult(
        "node",
        "Node.js",
        "required",
        "failed",
        None,
        None,
        "Node.js failed independently.",
    )

    monkeypatch.setattr(
        DoctorService,
        "_javascript_checks",
        lambda *args, **kwargs: [unrelated_failure],
    )
    monkeypatch.setattr(
        DoctorService,
        "_android_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_native_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_jsi_runtime_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "supernote_module_generator.cli_operations.CliOperationService.check",
        lambda *args, **kwargs: nested,
    )

    code, envelope = invoke(root, ["doctor", "--build"])

    assert code == 1
    assert envelope["status"] == "failure"
    assert envelope["validation"] == validation.to_dict()
    assert envelope["diagnostics"] == ["/tmp/nested-success.log"]
    assert envelope["requested_targets"] == ["nested-requested"]
    assert envelope["affected_targets"] == ["nested-affected"]
    assert envelope["metadata"]["generation_id"] == "nested-generation"
    assert envelope["metadata"]["build_duration_ms"] == 1234
    assert envelope["metadata"]["built"] is True
    assert envelope["metadata"]["nested_sentinel"] == "preserved"
    assert envelope["metadata"]["phase_label"] == "Doctor"
    assert envelope["error"]["kind"] == "doctor_failed"
    assert_valid(envelope)


def _isolate_doctor_build(monkeypatch, nested: CommandResult) -> None:
    monkeypatch.setattr(
        DoctorService,
        "_javascript_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_android_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_native_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        DoctorService,
        "_jsi_runtime_checks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "supernote_module_generator.cli_operations.CliOperationService.check",
        lambda *args, **kwargs: nested,
    )


def _nested_guard_result(status: str) -> CommandResult:
    is_success = status == "success"
    is_partial = status == "partial"
    return CommandResult(
        "check",
        status=status,
        exit_code=0 if is_success else 3 if is_partial else 1,
        validation=ValidationResult(
            structural="passed",
            integration="passed",
            dependency_link="passed",
            build="passed" if is_success else "failed",
            issues=(
                []
                if is_success
                else [
                    {
                        "code": f"SNMG_GUARD_{status.upper()}",
                        "severity": "error",
                        "scope": "toolchain",
                        "message": f"nested {status}",
                    }
                ]
            ),
        ),
        rollback=RollbackResult(
            is_partial,
            "partial" if is_partial else "not_needed",
            [],
        ),
        recovery=(
            RecoveryAction(
                f"nested {status} recovery",
                ["sn-module-gen", "check", "--build"],
            )
            if not is_success
            else None
        ),
        error=(
            ErrorInfo(f"nested_{status}", "build", f"nested {status} error")
            if not is_success
            else None
        ),
        requested_targets=[f"{status}-requested"],
        affected_targets=[f"{status}-affected"],
        diagnostics=[f"/tmp/{status}-guard.log"],
        next_action=(f"nested {status} action" if not is_success else None),
        metadata={
            "generation_id": f"{status}-generation",
            "nested_sentinel": status,
            **(
                {
                    "cancellation_requested": True,
                    "cancellation_status": "partial",
                    "cancellation_message": "nested cancellation partial",
                }
                if is_partial
                else {}
            ),
        },
    )


@pytest.mark.parametrize(
    ("nested_status", "expected_status", "expected_cancellation"),
    (
        ("success", "cancelled", "completed"),
        ("failure", "failure", "completed"),
        ("partial", "partial", "partial"),
    ),
)
def test_doctor_outer_completed_cancellation_composes_nested_build_outcome(
    tmp_path: Path,
    monkeypatch,
    nested_status: str,
    expected_status: str,
    expected_cancellation: str,
):
    root = plugin(tmp_path)
    nested = _nested_guard_result(nested_status)
    _isolate_doctor_build(monkeypatch, nested)
    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0

    def interrupt_once_then_finish(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise KeyboardInterrupt
        return original_finish(self)

    monkeypatch.setattr(ProtectedSourceGuard, "finish", interrupt_once_then_finish)

    code, envelope = invoke(root, ["doctor", "--build"])

    assert finish_calls == 2
    assert code == (130 if nested_status == "success" else nested.exit_code)
    assert envelope["status"] == expected_status
    assert envelope["validation"] == nested.validation.to_dict()
    assert envelope["diagnostics"] == nested.diagnostics
    assert envelope["requested_targets"] == nested.requested_targets
    assert envelope["affected_targets"] == nested.affected_targets
    assert envelope["metadata"]["nested_sentinel"] == nested_status
    assert envelope["metadata"]["authoritative_build_result"]["status"] == nested_status
    assert envelope["cancellation"]["requested"] is True
    assert envelope["cancellation"]["status"] == expected_cancellation
    if nested_status == "success":
        assert envelope["error"] is None
        assert envelope["next_action"] is None
        assert envelope["recovery"] is None
        assert envelope["rollback"]["status"] == "completed"
    else:
        assert envelope["error"] == nested.error.to_dict()
        assert envelope["next_action"] == nested.next_action
        assert envelope["recovery"] == nested.recovery.to_dict()
        assert envelope["rollback"]["status"] == (
            "partial" if nested_status == "partial" else "completed"
        )
    assert_valid(envelope)


@pytest.mark.parametrize("nested_status", ("success", "failure", "partial"))
def test_doctor_outer_partial_restoration_retains_nested_build_authority(
    tmp_path: Path,
    monkeypatch,
    nested_status: str,
):
    root = plugin(tmp_path)
    nested = _nested_guard_result(nested_status)
    _isolate_doctor_build(monkeypatch, nested)
    finish_calls = 0
    recovery_paths: list[Path] = []

    def interrupt_then_fail(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise KeyboardInterrupt
        recovery_paths.append(self.recovery_path)
        raise ProtectedSourceRestoreError(
            "forced outer restoration failure",
            mutations=("modified:android/app/build.gradle",),
            remaining=("modified:android/app/build.gradle",),
            recovery_path=self.recovery_path,
            interrupted=True,
        )

    monkeypatch.setattr(ProtectedSourceGuard, "finish", interrupt_then_fail)

    code, envelope = invoke(root, ["doctor", "--build"])

    assert finish_calls == 2
    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["rollback"]["status"] == "partial"
    assert envelope["error"]["kind"] == "doctor_source_restore_partial"
    assert envelope["validation"] == nested.validation.to_dict()
    assert envelope["diagnostics"] == nested.diagnostics
    assert envelope["requested_targets"] == nested.requested_targets
    assert envelope["affected_targets"] == nested.affected_targets
    authoritative = envelope["metadata"]["authoritative_build_result"]
    assert authoritative["status"] == nested_status
    assert authoritative["error"] == (
        nested.error.to_dict() if nested.error is not None else None
    )
    assert authoritative["recovery"] == (
        nested.recovery.to_dict() if nested.recovery is not None else None
    )
    assert envelope["cancellation"] == {
        "requested": True,
        "status": "partial",
        "reason": (
            "Doctor was interrupted and exact protected-source restoration "
            "could not be verified."
        ),
    }
    if nested.recovery is not None:
        assert nested.recovery.summary in envelope["recovery"]["summary"]
        assert nested.next_action in envelope["next_action"]
    assert_valid(envelope)
    assert recovery_paths and lexists(recovery_paths[0])
    remove_entry_no_follow(recovery_paths[0])


def test_usage_integrity_and_build_failures_conform_to_schema(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path)
    _, usage = invoke(root, ["update", "missing", "--dry-run"])
    assert usage["status"] == "failure"
    assert_valid(usage)

    assert invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )[0] == 0
    (root / "local_modules/alpha/index.js").write_text("const = ;\n")
    _, integrity = invoke(root, ["check"])
    assert integrity["error"]["kind"] == "integrity_failed"  # type: ignore[index]
    assert_valid(integrity)

    # Restore canonical output, then exercise the compiler failure envelope.
    assert invoke(root, ["update", "--all", "--yes"])[0] == 0

    def failed_build(command, *, cwd, timeout, env):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "src/main/Foo.kt:3: error: unresolved reference: Missing\n",
        )

    monkeypatch.setattr(
        "supernote_module_generator.v4_validation.run_process", failed_build
    )
    _, build = invoke(root, ["check", "--build"])
    assert build["error"]["kind"] == "build_failed"  # type: ignore[index]
    assert build["diagnostics"]
    assert_valid(build)


@pytest.mark.parametrize("outcome", ("success", "failure", "cancelled"))
def test_build_guard_partial_restoration_preserves_authoritative_contract(
    tmp_path: Path,
    monkeypatch,
    outcome: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    build_error = SubprocessError(
        ["./gradlew", "assembleDebug"],
        7,
        ["compiler root cause sentinel"],
    )

    def validation_result(self, **kwargs):
        if outcome == "cancelled":
            raise KeyboardInterrupt
        if outcome == "failure":
            return V4ValidationResult(
                status="failure",
                generation_id="generation-failure-sentinel",
                issues=(
                    ValidationIssue(
                        "SNMG_BUILD_FAILED",
                        "error",
                        "toolchain",
                        "compiler root cause sentinel",
                    ),
                ),
                build="failed",
                build_error=build_error,
                diagnostics=("/tmp/build-failure-diagnostic.log",),
                build_duration_ms=321,
            )
        return V4ValidationResult(
            status="success",
            generation_id="generation-success-sentinel",
            issues=(),
            build="passed",
            diagnostics=("/tmp/build-success-diagnostic.log",),
            build_duration_ms=123,
        )

    monkeypatch.setattr(V4Validator, "validate", validation_result)
    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0
    recovery_paths: list[Path] = []

    def partial_build_restore(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            return original_finish(self)
        source.write_text(source.read_text() + "// retained build mutation\n")
        recovery_paths.append(self.recovery_path)
        raise ProtectedSourceRestoreError(
            "forced partial build restoration",
            mutations=("modified:local_modules/alpha/index.js",),
            remaining=("modified:local_modules/alpha/index.js",),
            recovery_path=self.recovery_path,
            interrupted=outcome == "cancelled",
        )

    monkeypatch.setattr(ProtectedSourceGuard, "finish", partial_build_restore)

    code, envelope = invoke(root, ["check", "--build"])

    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["rollback"]["status"] == "partial"
    assert envelope["recovery"] is not None
    assert recovery_paths and lexists(recovery_paths[0])
    assert any(
        issue["code"] == "SNMG_BUILD_MUTATED_SOURCE"
        for issue in envelope["issues"]
    )
    assert envelope["affected_targets"] == ["alpha"]
    if outcome == "cancelled":
        assert envelope["cancellation"]["status"] == "partial"
        assert envelope["validation"]["build"] == "not_requested"
        assert envelope["diagnostics"] == []
    else:
        expected_build = "passed" if outcome == "success" else "failed"
        assert envelope["validation"]["build"] == expected_build
        assert envelope["diagnostics"] == [
            f"/tmp/build-{outcome}-diagnostic.log"
        ]
        assert envelope["metadata"]["generation_id"] == (
            f"generation-{outcome}-sentinel"
        )
        assert envelope["metadata"]["build_duration_ms"] == (
            123 if outcome == "success" else 321
        )
        assert envelope["error"]["kind"] == "build_mutated_source"
        assert envelope["error"]["phase"] == "build"
        if outcome == "failure":
            assert envelope["metadata"]["build_error"] == build_error.to_dict()
            assert envelope["metadata"]["authoritative_error"]["kind"] == (
                "build_failed"
            )
            assert "Android build failure" in envelope["metadata"][
                "authoritative_next_action"
            ]
    assert_valid(envelope)
    remove_entry_no_follow(recovery_paths[0])


@pytest.mark.parametrize(
    ("remaining", "scope", "feature_id", "affected"),
    (
        ("modified:local_modules/alpha", "feature", "alpha", ["alpha"]),
        (
            "modified:android/.supernote-module/v4-runtime",
            "runtime",
            None,
            ["shared runtime"],
        ),
        ("modified:android/app", "plugin", None, ["plugin wiring"]),
    ),
)
def test_metadata_only_partial_build_restore_uses_exact_residue_scope(
    tmp_path: Path,
    monkeypatch,
    remaining: str,
    scope: str,
    feature_id: str | None,
    affected: list[str],
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0

    monkeypatch.setattr(
        V4Validator,
        "validate",
        lambda self, **kwargs: V4ValidationResult(
            "success",
            "metadata-generation-sentinel",
            (),
            build="passed",
            diagnostics=("/tmp/metadata-only-build.log",),
            build_duration_ms=11,
        ),
    )
    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0
    recovery_paths: list[Path] = []

    def metadata_only_failure(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            return original_finish(self)
        recovery_paths.append(self.recovery_path)
        raise ProtectedSourceRestoreError(
            "forced metadata-only restoration failure",
            mutations=(),
            remaining=(remaining,),
            recovery_path=self.recovery_path,
        )

    monkeypatch.setattr(ProtectedSourceGuard, "finish", metadata_only_failure)

    code, envelope = invoke(root, ["check", "--build"])

    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["rollback"]["status"] == "partial"
    relative = remaining.partition(":")[2]
    assert envelope["actual_changes"][0]["path"] == str(root / relative)
    issue = next(
        item
        for item in envelope["issues"]
        if item["code"] == "SNMG_BUILD_MUTATED_SOURCE"
    )
    assert issue["scope"] == scope
    assert issue["path"] == relative
    assert issue["actual"] == remaining
    if feature_id is None:
        assert issue.get("feature_id") is None
    else:
        assert isinstance(issue.get("feature_id"), str)
        assert issue["feature_id"]
    assert envelope["affected_targets"] == affected
    assert envelope["validation"]["build"] == "passed"
    assert envelope["diagnostics"] == ["/tmp/metadata-only-build.log"]
    assert_valid(envelope)
    assert recovery_paths and lexists(recovery_paths[0])
    remove_entry_no_follow(recovery_paths[0])


@pytest.mark.parametrize("stage", ("frontend", "validator"))
@pytest.mark.parametrize("build", (False, True))
@pytest.mark.parametrize("retry", ("success", "failure"))
@pytest.mark.parametrize("interruption", ("raw", "wrapped"))
def test_stage_failure_survives_independent_finalization_cancellation(
    tmp_path: Path,
    monkeypatch,
    stage: str,
    build: bool,
    retry: str,
    interruption: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    if stage == "frontend":
        def failing_frontend(self):
            source.write_text(source.read_text() + "// frontend stage mutation\n")
            raise RuntimeError("frontend root-cause sentinel")

        monkeypatch.setattr(
            "supernote_module_generator.cli_operations."
            "CliOperationService._jvm_frontend_manifests",
            failing_frontend,
        )
    else:
        def failing_validator(self, **kwargs):
            source.write_text(source.read_text() + "// validator stage mutation\n")
            raise RuntimeError("validator root-cause sentinel")

        monkeypatch.setattr(V4Validator, "validate", failing_validator)

    recovery_paths: list[Path] = []
    original_remove = ProtectedSourceGuard._remove_temporary
    remove_calls = 0
    trigger = 1 if stage == "frontend" else 2

    if interruption == "raw":
        def interrupt_cleanup(self):
            nonlocal remove_calls
            remove_calls += 1
            if remove_calls == trigger:
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt
            if remove_calls == trigger + 1 and retry == "failure":
                raise RuntimeError("guard retry cleanup sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", interrupt_cleanup
        )
    else:
        original_restore = ProtectedSourceGuard._restore_entry
        restore_interrupted = False

        def interrupt_restore(self, destination, backup):
            nonlocal restore_interrupted
            original_restore(self, destination, backup)
            if not restore_interrupted:
                restore_interrupted = True
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt

        def fail_retry_cleanup(self):
            if retry == "failure" and restore_interrupted:
                raise RuntimeError("guard retry cleanup sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_restore_entry", interrupt_restore
        )
        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", fail_retry_cleanup
        )

    arguments = ["check", *(["--build"] if build else [])]
    code, envelope = invoke(root, arguments)

    expected_error_kind = (
        "jvm_frontend_failed" if stage == "frontend" else "validation_failed"
    )
    expected_issue = (
        "SNMG_FRONTEND_MUTATED_SOURCE"
        if stage == "frontend"
        else "SNMG_BUILD_MUTATED_SOURCE"
        if build
        else "SNMG_VALIDATION_MUTATED_SOURCE"
    )
    assert any(issue["code"] == expected_issue for issue in envelope["issues"])
    assert envelope["affected_targets"] == ["alpha"]
    authoritative = envelope["metadata"]["authoritative_stage_result"]
    assert authoritative["status"] == "failure"
    assert authoritative["error"]["kind"] == expected_error_kind
    assert f"{stage} root-cause sentinel" in authoritative["error"]["message"]
    assert authoritative["requested_targets"] == []
    assert authoritative["validation"]["build"] == (
        "not_run" if build else "not_requested"
    )
    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["error"]["kind"] == (
        "frontend_mutated_source"
        if stage == "frontend"
        else "build_mutated_source"
        if build
        else "validation_mutated_source"
    )
    assert envelope["error"]["phase"] == (
        "frontend" if stage == "frontend" else "build" if build else "check"
    )
    assert envelope["cancellation"]["status"] == "not_requested"
    assert envelope["rollback"]["status"] == "partial"
    assert source_tree_inventory(root) != before
    recovery = Path(envelope["metadata"]["recovery_path"])
    assert lexists(recovery)
    assert restore_protected_source_backup(recovery, root) == ()
    assert source_tree_inventory(root) == before
    assert protected_directory_metadata(root) == before_directories
    remove_entry_no_follow(recovery)
    assert_valid(envelope)


@pytest.mark.parametrize(
    "arguments",
    (
        ["update", "alpha", "--dry-run", "--diff"],
        ["repair", "--diff"],
    ),
    ids=("update-dry-run", "repair-preview"),
)
@pytest.mark.parametrize("retry", ("success", "failure"))
@pytest.mark.parametrize("interruption", ("raw", "wrapped"))
def test_preview_stage_failure_survives_independent_finalization_cancellation(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
    retry: str,
    interruption: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    def failing_frontend(self):
        source.write_text(source.read_text() + "// preview stage mutation\n")
        raise RuntimeError("preview frontend root-cause sentinel")

    monkeypatch.setattr(
        "supernote_module_generator.cli_operations."
        "CliOperationService._jvm_frontend_manifests",
        failing_frontend,
    )
    recovery_paths: list[Path] = []
    original_remove = ProtectedSourceGuard._remove_temporary

    if interruption == "raw":
        cleanup_calls = 0

        def interrupt_cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt
            if cleanup_calls == 2 and retry == "failure":
                raise RuntimeError("preview guard retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", interrupt_cleanup
        )
    else:
        original_restore = ProtectedSourceGuard._restore_entry
        restore_interrupted = False

        def interrupt_restore(self, destination, backup):
            nonlocal restore_interrupted
            original_restore(self, destination, backup)
            if not restore_interrupted:
                restore_interrupted = True
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt

        def fail_retry_cleanup(self):
            if retry == "failure" and restore_interrupted:
                raise RuntimeError("preview guard retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_restore_entry", interrupt_restore
        )
        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", fail_retry_cleanup
        )

    code, envelope = invoke(root, arguments)

    authoritative = envelope["metadata"]["authoritative_stage_result"]
    assert authoritative["status"] == "failure"
    assert authoritative["error"]["kind"] == "jvm_frontend_failed"
    assert "preview frontend root-cause sentinel" in authoritative["error"]["message"]
    assert any(
        issue["code"] == "SNMG_FRONTEND_MUTATED_SOURCE"
        for issue in envelope["issues"]
    )
    assert envelope["affected_targets"] == ["alpha"]
    assert source_tree_inventory(root) != before
    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["error"]["kind"] == "frontend_mutated_source"
    assert envelope["next_action"]
    assert envelope["cancellation"]["status"] == "not_requested"
    assert envelope["rollback"]["status"] == "partial"
    recovery = Path(envelope["metadata"]["recovery_path"])
    assert lexists(recovery)
    assert restore_protected_source_backup(recovery, root) == ()
    assert source_tree_inventory(root) == before
    assert protected_directory_metadata(root) == before_directories
    remove_entry_no_follow(recovery)
    assert_valid(envelope)


@pytest.mark.parametrize("retry", ("success", "failure"))
@pytest.mark.parametrize("interruption", ("raw", "wrapped"))
def test_doctor_stage_failure_survives_independent_finalization_cancellation(
    tmp_path: Path,
    monkeypatch,
    retry: str,
    interruption: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    def failing_doctor_stage(self, *args, **kwargs):
        source.write_text(source.read_text() + "// doctor stage mutation\n")
        raise RuntimeError("doctor stage root-cause sentinel")

    monkeypatch.setattr(DoctorService, "_javascript_checks", failing_doctor_stage)
    recovery_paths: list[Path] = []
    original_remove = ProtectedSourceGuard._remove_temporary

    if interruption == "raw":
        cleanup_calls = 0

        def interrupt_cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt
            if cleanup_calls == 2 and retry == "failure":
                raise RuntimeError("doctor guard retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", interrupt_cleanup
        )
    else:
        original_restore = ProtectedSourceGuard._restore_entry
        restore_interrupted = False

        def interrupt_restore(self, destination, backup):
            nonlocal restore_interrupted
            original_restore(self, destination, backup)
            if not restore_interrupted:
                restore_interrupted = True
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt

        def fail_retry_cleanup(self):
            if retry == "failure" and restore_interrupted:
                raise RuntimeError("doctor guard retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_restore_entry", interrupt_restore
        )
        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", fail_retry_cleanup
        )

    code, envelope = invoke(root, ["doctor"])

    authoritative = envelope["metadata"]["authoritative_stage_result"]
    assert authoritative["status"] == "failure"
    assert authoritative["error"]["kind"] == "doctor_stage_failed"
    assert "doctor stage root-cause sentinel" in authoritative["error"]["message"]
    assert "authoritative_build_result" not in envelope["metadata"]
    assert envelope["doctor"]["required_passed"] is False
    assert envelope["doctor"]["required_issue_count"] == 2
    probe = next(
        check
        for check in envelope["doctor"]["checks"]
        if check["id"] == "doctor_probe_execution"
    )
    assert probe["requirement"] == "required"
    assert probe["status"] == "failed"
    assert probe["metadata"]["phase"] == "doctor"
    assert probe["metadata"]["exception_type"] == "RuntimeError"
    integrity = next(
        check
        for check in envelope["doctor"]["checks"]
        if check["id"] == "doctor_source_integrity"
    )
    assert integrity["metadata"]["mutations"] == [
        "modified:local_modules/alpha/index.js"
    ]
    assert source_tree_inventory(root) != before
    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["error"]["kind"] == "doctor_source_restore_partial"
    assert envelope["cancellation"]["status"] == "not_requested"
    assert envelope["rollback"]["status"] == "partial"
    recovery = Path(envelope["metadata"]["recovery_path"])
    assert lexists(recovery)
    assert restore_protected_source_backup(recovery, root) == ()
    assert source_tree_inventory(root) == before
    assert protected_directory_metadata(root) == before_directories
    remove_entry_no_follow(recovery)
    assert_valid(envelope)


@pytest.mark.parametrize("finalization", ("ordinary", "retry-success", "retry-failure"))
def test_doctor_probe_failure_has_truthful_required_summary_without_source_mutation(
    tmp_path: Path,
    monkeypatch,
    finalization: str,
):
    root = plugin(tmp_path)

    def failing_doctor_stage(self, *args, **kwargs):
        raise RuntimeError("doctor no-mutation root-cause sentinel")

    monkeypatch.setattr(DoctorService, "_javascript_checks", failing_doctor_stage)
    recovery_paths: list[Path] = []
    original_remove = ProtectedSourceGuard._remove_temporary
    cleanup_calls = 0

    if finalization != "ordinary":
        def interrupt_cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt
            if cleanup_calls == 2 and finalization == "retry-failure":
                raise RuntimeError("doctor no-mutation retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard, "_remove_temporary", interrupt_cleanup
        )

    code, envelope = invoke(root, ["doctor"])

    assert envelope["doctor"]["required_passed"] is False
    assert envelope["doctor"]["required_issue_count"] == 1
    probe = next(
        check
        for check in envelope["doctor"]["checks"]
        if check["id"] == "doctor_probe_execution"
    )
    assert probe["status"] == "failed"
    assert "doctor no-mutation root-cause sentinel" in probe["message"]
    assert probe["metadata"]["configured"] is False
    assert probe["metadata"]["found"] is False
    assert probe["metadata"]["selected"] is False
    assert probe["metadata"]["executable_probed"] is False
    assert probe["metadata"]["compiler_probed"] is False
    assert probe["metadata"]["project_built"] is False
    assert probe["metadata"]["device_tested"] is False
    assert "authoritative_build_result" not in envelope["metadata"]
    authoritative = envelope["metadata"]["authoritative_stage_result"]
    assert authoritative["error"]["kind"] == "doctor_stage_failed"
    assert "doctor no-mutation root-cause sentinel" in authoritative["error"]["message"]
    if finalization == "retry-failure":
        assert code == 3
        assert envelope["status"] == "partial"
        assert envelope["error"]["kind"] == "doctor_source_cleanup_failed"
        assert envelope["cancellation"]["status"] == "partial"
        assert envelope["rollback"]["status"] == "partial"
        assert envelope["next_action"]
        recovery = Path(envelope["metadata"]["recovery_path"])
        assert recovery_paths and recovery == recovery_paths[0]
        assert lexists(recovery)
        assert restore_protected_source_backup(recovery, root) == ()
        remove_entry_no_follow(recovery)
    else:
        assert code == 1
        assert envelope["status"] == "failure"
        assert envelope["error"]["kind"] == "doctor_stage_failed"
        assert envelope["next_action"] == (
            "Correct the Doctor probe failure and rerun Doctor."
        )
        assert envelope["rollback"]["status"] == (
            "completed" if finalization == "retry-success" else "not_needed"
        )
        assert envelope["cancellation"]["status"] == (
            "completed" if finalization == "retry-success" else "not_requested"
        )
        if recovery_paths:
            assert not lexists(recovery_paths[0])
    assert_valid(envelope)


def test_human_doctor_probe_failure_reports_one_issue_and_next_action(
    tmp_path: Path,
    monkeypatch,
):
    root = plugin(tmp_path)

    def failing_doctor_stage(self, *args, **kwargs):
        raise RuntimeError("human doctor root-cause sentinel")

    monkeypatch.setattr(DoctorService, "_javascript_checks", failing_doctor_stage)
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        ["doctor"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    rendered = stdout.getvalue() + stderr.getvalue()
    assert code == 1
    assert "Doctor found 1 required issue" in rendered
    assert "Doctor" in rendered
    assert "human doctor root-cause sentinel" in rendered
    assert "Correct the Doctor probe failure and rerun Doctor." in rendered
    assert "Doctor found 0 required issues" not in rendered


@pytest.mark.parametrize("residue", ("verified-empty", "live"))
def test_human_doctor_partial_recovery_prioritizes_retained_backup_action(
    tmp_path: Path,
    monkeypatch,
    residue: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    before = source.read_bytes()
    recovery_paths: list[Path] = []

    def failing_doctor_stage(self, *args, **kwargs):
        if residue == "live":
            source.write_bytes(before + b"// live doctor residue\n")
        raise RuntimeError("human partial doctor root-cause sentinel")

    monkeypatch.setattr(DoctorService, "_javascript_checks", failing_doctor_stage)
    if residue == "verified-empty":
        original_remove = ProtectedSourceGuard._remove_temporary
        cleanup_calls = 0

        def interrupt_then_fail_cleanup(self):
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                recovery_paths.append(self.recovery_path)
                raise KeyboardInterrupt
            if cleanup_calls == 2:
                raise RuntimeError("human cleanup retry sentinel")
            return original_remove(self)

        monkeypatch.setattr(
            ProtectedSourceGuard,
            "_remove_temporary",
            interrupt_then_fail_cleanup,
        )
    else:
        original_init = ProtectedSourceGuard.__init__

        def capture_recovery_path(self, guarded_root):
            original_init(self, guarded_root)
            recovery_paths.append(self.recovery_path)

        monkeypatch.setattr(
            ProtectedSourceGuard,
            "__init__",
            capture_recovery_path,
        )

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["doctor"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=root,
    )

    rendered = stdout.getvalue() + stderr.getvalue()
    assert code == 3
    assert recovery_paths
    recovery_path = recovery_paths[0]
    assert str(recovery_path) in rendered
    assert "Preserve the recovery backup" in rendered
    assert "Correct the Doctor probe failure and rerun Doctor." in rendered
    next_line = next(line for line in rendered.splitlines() if "Next:" in line)
    assert "Preserve the recovery backup" in next_line
    assert "Correct the Doctor probe failure" in next_line
    if residue == "verified-empty":
        assert "complete guard cleanup" in rendered
        assert "Doctor found 1 required issue" in rendered
        assert source.read_bytes() == before
    else:
        assert "restore the listed residue" in rendered
        assert "Doctor found 2 required issues" in rendered
        assert source.read_bytes() != before
    assert lexists(recovery_path)
    assert restore_protected_source_backup(recovery_path, root) == ()
    assert source.read_bytes() == before
    remove_entry_no_follow(recovery_path)


@pytest.mark.parametrize("stage", ("frontend", "validator"))
@pytest.mark.parametrize("build", (False, True))
def test_stage_failure_without_mutation_keeps_cleanup_diagnostics_out_of_residue(
    tmp_path: Path,
    monkeypatch,
    stage: str,
    build: bool,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    before = source_tree_inventory(root)
    before_directories = protected_directory_metadata(root)

    if stage == "frontend":
        def failing_frontend(self):
            raise RuntimeError("frontend root-cause sentinel")

        monkeypatch.setattr(
            "supernote_module_generator.cli_operations."
            "CliOperationService._jvm_frontend_manifests",
            failing_frontend,
        )
    else:
        def failing_validator(self, **kwargs):
            raise RuntimeError("validator root-cause sentinel")

        monkeypatch.setattr(V4Validator, "validate", failing_validator)

    original_remove = ProtectedSourceGuard._remove_temporary
    remove_calls = 0
    trigger = 1 if stage == "frontend" else 2
    recovery_paths: list[Path] = []

    def interrupt_then_fail_cleanup(self):
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == trigger:
            recovery_paths.append(self.recovery_path)
            raise KeyboardInterrupt
        if remove_calls == trigger + 1:
            raise RuntimeError("guard retry cleanup sentinel")
        return original_remove(self)

    monkeypatch.setattr(
        ProtectedSourceGuard, "_remove_temporary", interrupt_then_fail_cleanup
    )

    code, envelope = invoke(
        root, ["check", *(["--build"] if build else [])]
    )

    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["rollback"]["status"] == "partial"
    assert envelope["cancellation"]["status"] == "partial"
    assert envelope["changes"] == []
    assert envelope["actual_changes"] == []
    assert envelope["affected_targets"] == []
    assert not any(
        issue["code"].endswith("_MUTATED_SOURCE")
        for issue in envelope["issues"]
    )
    assert envelope["error"]["kind"] == "protected_source_cleanup_failed"
    authoritative = envelope["metadata"]["authoritative_stage_result"]
    assert authoritative["status"] == "failure"
    assert authoritative["error"]["kind"] == (
        "jvm_frontend_failed" if stage == "frontend" else "validation_failed"
    )
    assert f"{stage} root-cause sentinel" in authoritative["error"]["message"]
    assert envelope["metadata"]["restore_diagnostics"] == [
        "finalization_failed:guard retry cleanup sentinel"
    ]
    recovery = Path(envelope["metadata"]["recovery_path"])
    assert recovery_paths and recovery == recovery_paths[0]
    assert lexists(recovery)
    assert source_tree_inventory(root) == before
    assert protected_directory_metadata(root) == before_directories
    assert restore_protected_source_backup(recovery, root) == ()
    remove_entry_no_follow(recovery)
    assert_valid(envelope)


@pytest.mark.parametrize(
    ("stage", "build"), (("frontend", False), ("validator", True))
)
@pytest.mark.parametrize("source_state", ("live", "restored"))
def test_uninventoried_guard_residue_is_never_treated_as_verified_empty(
    tmp_path: Path,
    monkeypatch,
    stage: str,
    build: bool,
    source_state: str,
):
    root = plugin(tmp_path)
    code, _added = invoke(
        root,
        ["add", "alpha", "--starter", "cpp", "--skip-install", "--yes"],
    )
    assert code == 0
    source = root / "local_modules/alpha/index.js"
    before = source_tree_inventory(root)

    if stage == "frontend":
        def failing_frontend(self):
            source.write_text(source.read_text() + "// live mutation\n")
            raise RuntimeError("frontend root-cause sentinel")

        monkeypatch.setattr(
            "supernote_module_generator.cli_operations."
            "CliOperationService._jvm_frontend_manifests",
            failing_frontend,
        )
    else:
        def failing_validator(self, **kwargs):
            source.write_text(source.read_text() + "// live mutation\n")
            raise RuntimeError("validator root-cause sentinel")

        monkeypatch.setattr(V4Validator, "validate", failing_validator)

    original_finish = ProtectedSourceGuard.finish
    finish_calls = 0
    trigger = 1 if stage == "frontend" else 2
    recovery_paths: list[Path] = []

    def interrupt_then_fail(self):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls < trigger:
            return original_finish(self)
        if finish_calls == trigger:
            recovery_paths.append(self.recovery_path)
            self._observed_mutations = (
                "modified:local_modules/alpha/index.js",
            )
            if source_state == "restored":
                assert restore_protected_source_backup(self.recovery_path, root) == ()
            raise KeyboardInterrupt
        if finish_calls == trigger + 1:
            raise RuntimeError("guard retry sentinel")
        raise AssertionError("unexpected guard finish call")

    def inventory_unavailable(self):
        raise OSError("inventory unavailable")

    monkeypatch.setattr(ProtectedSourceGuard, "finish", interrupt_then_fail)
    monkeypatch.setattr(
        ProtectedSourceGuard, "remaining_changes", inventory_unavailable
    )

    code, envelope = invoke(root, ["check", *(["--build"] if build else [])])

    assert code == 3
    assert envelope["status"] == "partial"
    assert envelope["rollback"]["status"] == "partial"
    assert envelope["error"]["kind"] == "protected_source_restore_unverified"
    assert "matches the pre-command baseline" not in envelope["error"]["message"]
    assert envelope["changes"] == []
    assert envelope["actual_changes"] == []
    assert envelope["metadata"]["residue_verified"] is False
    assert envelope["metadata"]["restore_diagnostics"] == [
        "finalization_failed:guard retry sentinel",
        "inventory_failed:inventory unavailable",
    ]
    issue = next(
        item
        for item in envelope["issues"]
        if item["code"].endswith("_MUTATED_SOURCE")
    )
    assert issue["scope"] == "feature"
    assert issue["path"] == "local_modules/alpha/index.js"
    assert envelope["affected_targets"] == ["alpha"]
    assert recovery_paths
    recovery = Path(envelope["metadata"]["recovery_path"])
    assert recovery == recovery_paths[0]
    assert lexists(recovery)
    assert (source_tree_inventory(root) == before) is (source_state == "restored")
    assert restore_protected_source_backup(recovery, root) == ()
    assert source_tree_inventory(root) == before
    remove_entry_no_follow(recovery)
    assert_valid(envelope)


def test_partial_cancellation_cli_envelope_conforms_to_schema(
    tmp_path: Path, monkeypatch
):
    root = plugin(tmp_path, npm_lock=True)
    monkeypatch.setattr(
        FeatureCliOperationService, "_health_check_manager", lambda *args: None
    )

    def interrupt(self, invocation, *, phase):
        raise KeyboardInterrupt

    monkeypatch.setattr(FeatureCliOperationService, "_run", interrupt)
    monkeypatch.setattr(
        FeatureCliOperationService, "_reconcile", lambda *args: False
    )

    code, partial = invoke(
        root,
        [
            "add",
            "cancelled",
            "--starter",
            "cpp",
            "--package-manager",
            "npm",
            "--yes",
        ],
    )

    assert code == 3
    assert partial["status"] == "partial"
    assert partial["cancellation"] == {
        "requested": True,
        "status": "partial",
        "reason": "Operation cancelled, but exact restoration could not be verified.",
    }
    assert partial["error"]["kind"] == "cancellation_rollback_partial"  # type: ignore[index]
    assert partial["next_action"]
    assert_valid(partial)
