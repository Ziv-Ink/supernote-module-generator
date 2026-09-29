from __future__ import annotations

import io
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.doctor import DoctorService
from supernote_module_generator.models import (
    CommandResult,
    ErrorInfo,
    RecoveryAction,
    RollbackResult,
    ValidationResult,
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
        ["add", "alpha", "--starter", "cpp", "--yes"],
    )
    assert code == 0
    envelopes.append(added)

    for arguments in (
        ["update", "--yes"],
        ["validate"],
        ["doctor"],
    ):
        _, envelope = invoke(root, arguments)
        envelopes.append(envelope)

    assert {str(item["command"]) for item in envelopes} == {
        "add", "update", "validate", "doctor"
    }
    for envelope in envelopes:
        assert_valid(envelope)


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
