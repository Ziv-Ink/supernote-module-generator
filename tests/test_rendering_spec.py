from __future__ import annotations

import io
import time

import pytest

from supernote_module_generator.models import (
    Change,
    CommandResult,
    DependencyResult,
    DoctorCheckResult,
    DoctorResult,
    ErrorInfo,
    ModuleInfo,
    RecoveryAction,
    RollbackResult,
    SubprocessError,
    ValidationResult,
    WarningInfo,
)
from supernote_module_generator.rendering import (
    ProgressReporter,
    Renderer,
    TerminalCapabilities,
)


def capabilities() -> TerminalCapabilities:
    return TerminalCapabilities(False, False, False, False, 80, 24)


def cursor_capabilities() -> TerminalCapabilities:
    return TerminalCapabilities(True, True, False, True, 80, 24)


def test_doctor_report_goes_to_stdout_and_failure_summary_to_stderr():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("human", capabilities(), stdout=stdout, stderr=stderr)
    check = DoctorCheckResult(
        "node",
        "JavaScript",
        "required",
        "failed",
        None,
        None,
        "Node.js was not found.",
    )
    result = CommandResult(
        "doctor",
        status="failure",
        exit_code=1,
        doctor=DoctorResult("all", False, 1, 0, [check]),
        error=ErrorInfo("doctor_failed", "doctor", "Doctor found 1 required issue."),
    )

    renderer.render(result)

    assert "Doctor - All" in stdout.getvalue()
    assert "Node.js was not found." in stdout.getvalue()
    assert "Doctor found 1 required issue" in stderr.getvalue()


def test_doctor_report_separates_long_labels_from_messages():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("human", capabilities(), stdout=stdout, stderr=stderr)
    check = DoctorCheckResult(
        "gradle_wrapper",
        "Gradle wrapper",
        "required",
        "failed",
        None,
        None,
        "The project Gradle wrapper is missing.",
    )

    renderer.render(
        CommandResult(
            "doctor",
            status="failure",
            exit_code=1,
            doctor=DoctorResult("plugin", False, 1, 0, [check]),
            error=ErrorInfo("doctor_failed", "doctor", "Doctor found 1 required issue."),
        )
    )

    combined = stdout.getvalue() + stderr.getvalue()
    assert combined.count("The project Gradle wrapper is missing.") == 1
    assert "wrapperThe" not in combined


def test_successful_doctor_report_and_final_result_are_stdout_only():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("human", capabilities(), stdout=stdout, stderr=stderr)
    check = DoctorCheckResult(
        "project",
        "Project",
        "required",
        "passed",
        None,
        "/plugin",
        "Plugin root and package metadata are available.",
    )

    renderer.render(
        CommandResult(
            "doctor",
            doctor=DoctorResult("native", True, 0, 0, [check]),
        )
    )

    assert "Doctor - Native Module" in stdout.getvalue()
    assert "Doctor found no required issues" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_quiet_doctor_keeps_advisories_but_suppresses_report_detail():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("quiet", capabilities(), stdout=stdout, stderr=stderr)
    advisory = DoctorCheckResult(
        "selinux_policy",
        "JSI execution policy",
        "advisory",
        "warning",
        None,
        None,
        "Target execution policy was not inspected.",
    )

    renderer.render(
        CommandResult(
            "doctor",
            doctor=DoctorResult("all", True, 0, 1, [advisory]),
        )
    )

    assert stdout.getvalue() == "Doctor found no required issues\n"
    assert "JSI execution policy: Target execution policy was not inspected." in stderr.getvalue()
    assert "Doctor - All" not in stdout.getvalue()


def test_internal_error_wording_and_traceback_are_debug_only():
    ordinary_err = io.StringIO()
    debug_err = io.StringIO()
    result = CommandResult(
        "add",
        status="failure",
        exit_code=1,
        error=ErrorInfo(
            "internal",
            "stage",
            "Supernote Module Generator could not complete stage.",
            internal={"traceback": "Traceback text", "transaction_id": "abc123"},
        ),
        metadata={"next_action": "Rerun with --debug and report the resulting traceback."},
    )

    Renderer("human", capabilities(), stderr=ordinary_err).render(result)
    Renderer("human", capabilities(), stderr=debug_err, debug=True).render(result)

    assert ordinary_err.getvalue().startswith("[X] Internal error\n")
    assert "Internal error failed" not in ordinary_err.getvalue()
    assert "Traceback text" not in ordinary_err.getvalue()
    assert "Transaction: abc123" in debug_err.getvalue()
    assert "Traceback text" in debug_err.getvalue()


def test_json_contract_reports_cancellation_and_true_actual_changes():
    cancelled = CommandResult(
        "update",
        status="cancelled",
        exit_code=130,
        changes=[
            Change("generated.txt", "updated", "generated"),
            Change("user.cpp", "preserved", "user"),
        ],
        metadata={"cancellation_message": "Operation cancelled."},
    ).to_dict()

    assert cancelled["cancellation"] == {
        "requested": True,
        "status": "completed",
        "reason": "Operation cancelled.",
    }
    assert cancelled["changes"] == [
        {"path": "generated.txt", "action": "update", "ownership": "generated"},
        {"path": "user.cpp", "action": "preserve", "ownership": "user"},
    ]
    assert cancelled["actual_changes"] == []


def test_json_contract_reports_partial_cancellation_independent_of_status():
    partial = CommandResult(
        "update",
        status="partial",
        exit_code=3,
        rollback=RollbackResult(True, "partial", ["generated.txt"]),
        changes=[Change("residue.txt", "update", "rollback_residue")],
        error=ErrorInfo(
            "cancellation_rollback_partial",
            "rollback",
            "Exact rollback could not be verified.",
        ),
        metadata={
            "cancellation_requested": True,
            "cancellation_message": "Operation cancelled.",
        },
    ).to_dict()

    assert partial["cancellation"] == {
        "requested": True,
        "status": "partial",
        "reason": "Operation cancelled.",
    }
    assert partial["actual_changes"] == [
        {"path": "residue.txt", "action": "update", "ownership": "rollback_residue"}
    ]


def test_json_contract_normalizes_legacy_issues_to_stable_v4_fields():
    payload = CommandResult(
        "validate",
        status="failure",
        exit_code=1,
        validation=ValidationResult(
            structural="failed",
            issues=[{"kind": "parent_dependency", "message": "link is stale"}],
        ),
    ).to_dict()

    assert payload["issues"] == [
        {
            "kind": "parent_dependency",
            "message": "link is stale",
            "code": "SNMG_PARENT_DEPENDENCY",
            "severity": "error",
            "scope": "plugin",
        }
    ]
    assert payload["issues"] == payload["validation"]["issues"]


def test_animated_progress_clears_the_active_line_when_work_raises():
    stderr = io.StringIO()
    renderer = Renderer("human", cursor_capabilities(), stderr=stderr)

    with pytest.raises(RuntimeError, match="boom"):
        with ProgressReporter(renderer).phase("Waiting", "Waited"):
            time.sleep(0.3)
            raise RuntimeError("boom")

    output = stderr.getvalue()
    assert "Waiting" in output
    assert output.endswith("\r\033[2K")


def test_verbose_progress_is_static_while_subprocess_output_is_streamed():
    stderr = io.StringIO()
    renderer = Renderer("verbose", cursor_capabilities(), stderr=stderr)

    with ProgressReporter(renderer).phase("Installing", "Installed"):
        print("dependency output", file=renderer.stderr)

    assert stderr.getvalue() == (
        "... Installing\n"
        "dependency output\n"
        "✓ Installed\n"
    )


def test_plain_mode_overrides_cursor_capability_and_omits_elapsed_time():
    stderr = io.StringIO()
    renderer = Renderer(
        "human",
        cursor_capabilities(),
        stderr=stderr,
        plain=True,
    )

    with ProgressReporter(renderer).phase("Installing dependency", "Installed dependency"):
        pass

    assert stderr.getvalue() == (
        "... Installing dependency\n"
        "[OK] Installed dependency\n"
    )
    assert "\033" not in stderr.getvalue()


def test_unicode_capable_doctor_keeps_the_interactive_em_dash():
    stdout = io.StringIO()
    renderer = Renderer("human", cursor_capabilities(), stdout=stdout)

    renderer.render(
        CommandResult(
            "doctor",
            doctor=DoctorResult("all", True, 0, 0, []),
        )
    )

    assert "Doctor — All" in stdout.getvalue()


def test_unicode_unsafe_fallback_guards_dynamic_text_and_navigation_symbols():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer(
        "human",
        capabilities(),
        stdout=stdout,
        stderr=stderr,
    )

    renderer.warning(WarningInfo("warning", "Café — ↑/↓ 🙂", "test"))

    assert stderr.getvalue() == "[!] Caf\\xe9 - ^/v \\U0001f642\n"
    assert stderr.getvalue().isascii()


def _module(name: str = "alpha", *, validation: ValidationResult | None = None) -> ModuleInfo:
    return ModuleInfo(
        name,
        "Alpha",
        "feature",
        "Feature",
        f"/plugin/local_modules/{name}",
        f"/plugin/local_modules/{name}/src",
        f"com.example.{name}",
        "0.1.0",
        validation,
    )


def test_json_warning_and_cancelled_rendering_keep_stream_contracts():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("json", capabilities(), stdout=stdout, stderr=stderr)
    renderer.warning(WarningInfo("config", "hidden", "preflight", "fix it"))
    renderer.render(CommandResult("update", status="cancelled", exit_code=130))
    assert '"status": "cancelled"' in stdout.getvalue()
    assert stderr.getvalue() == ""

    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("human", capabilities(), stdout=stdout, stderr=stderr)
    renderer.progress_emitted = True
    renderer.render(
        CommandResult(
            "update",
            status="cancelled",
            exit_code=130,
            warnings=[WarningInfo("config", "warning", "preflight", "fix it")],
            metadata={"cancellation_message": "Stopped safely."},
        )
    )
    assert stdout.getvalue() == "Stopped safely.\n"
    assert "warning" in stderr.getvalue() and "fix it" in stderr.getvalue()
    assert not renderer.progress_emitted


def test_success_rendering_covers_module_project_plan_dependency_and_next_action():
    stdout = io.StringIO()
    renderer = Renderer("human", capabilities(), stdout=stdout)
    result = CommandResult(
        "add",
        module=_module(),
        changes=[Change("generated/a", "created", "generated")],
        dependency=DependencyResult(True, "npm", "installed", True, ["npm", "install"], 1),
        metadata={
            "built": True,
            "plan": {},
            "requested_targets": ["alpha"],
            "affected_targets": ["alpha", "beta"],
            "diff": "-old\n+new",
            "next_action": "Commit reviewed output.",
        },
    )
    renderer.render(result)
    output = stdout.getvalue()
    assert "Requested:" in output and "Also affected:" in output
    assert "created generated/a" in output and "Diff:" in output
    assert 'Added and built feature "alpha"' in output
    assert "Dependency: installed with npm" in output
    assert "Commit reviewed output" in output

    quiet = io.StringIO()
    Renderer("quiet", capabilities(), stdout=quiet).render(
        CommandResult("update", module=_module())
    )
    assert quiet.getvalue() == 'Updated feature "alpha"\n'


@pytest.mark.parametrize(
    ("modules", "built", "expected"),
    [
        ([_module()], False, "1 feature is valid"),
        ([_module(), _module("beta")], False, "All 2 features are valid"),
        ([_module(), _module("beta")], True, "All 2 features are valid and build successfully"),
    ],
)
def test_validation_success_summarizes_complete_project(
    modules: list[ModuleInfo], built: bool, expected: str
) -> None:
    stdout = io.StringIO()
    Renderer("human", capabilities(), stdout=stdout).render(
        CommandResult("validate", modules=modules, metadata={"built": built})
    )
    assert expected in stdout.getvalue()


def test_empty_generation_plan_and_generic_success_are_explicit():
    stdout = io.StringIO()
    Renderer("human", capabilities(), stdout=stdout).render(
        CommandResult(
            "custom",
            metadata={"plan": {}, "success_message": "Custom complete"},
        )
    )
    output = stdout.getvalue()
    assert "project state check" in output
    assert output.count("(none)") == 2
    assert "Custom complete" in output


def test_usage_missing_error_and_generic_failure_recovery_are_distinct():
    stderr = io.StringIO()
    renderer = Renderer("human", capabilities(), stderr=stderr)
    renderer.render(CommandResult("update", status="failure", exit_code=1))
    renderer.render(
        CommandResult(
            "add",
            status="failure",
            exit_code=2,
            error=ErrorInfo("usage", "parse", "bad option"),
            metadata={"recovery_text": "Run add --help."},
        )
    )
    renderer.render(
        CommandResult(
            "update",
            status="failure",
            exit_code=1,
            error=ErrorInfo(
                "subprocess_failed",
                "generate",
                "compiler failed",
                SubprocessError(["compiler"], 7, [f"line {index}" for index in range(10)]),
            ),
            rollback=RollbackResult(True, "partial", ["a"]),
            recovery=RecoveryAction("Retry", ["sn-module-gen", "update"]),
            diagnostics=["/tmp/build.log"],
            metadata={"phase_label": "Generating feature"},
        )
    )
    output = stderr.getvalue()
    assert "Internal error" in output
    assert "Run add --help" in output
    assert "Additional output omitted" in output
    assert "sn-module-gen update" in output
    assert "Diagnostics: /tmp/build.log" in output


def test_authoritative_validation_failure_renders_codes_paths_and_evidence():
    stderr = io.StringIO()
    Renderer("human", capabilities(), stderr=stderr, debug=True).render(
        CommandResult(
            "validate",
            status="failure",
            exit_code=1,
            validation=ValidationResult(
                structural="failed",
                issues=[
                    {
                        "code": "SNMG_MISSING",
                        "scope": "feature",
                        "feature_id": "feature-alpha",
                        "message": "Generated file is missing.",
                        "path": "generated/a",
                    }
                ],
            ),
            error=ErrorInfo(
                "validation_failed",
                "validate",
                "invalid",
                SubprocessError(["check"], 1, ["compiler detail"]),
                {"transaction_id": "q6", "traceback": "trace"},
            ),
            diagnostics=["/tmp/validation.json"],
            next_action="Run update.",
        )
    )
    output = stderr.getvalue()
    assert "SNMG_MISSING (feature-alpha)" in output
    assert "Path: generated/a" in output
    assert "compiler detail" in output
    assert "Run update" in output and "/tmp/validation.json" in output
    assert "Transaction: q6" in output and "Traceback:" in output


def test_doctor_groups_render_pass_warning_failure_path_version_and_next_action():
    stdout = io.StringIO()
    stderr = io.StringIO()
    checks = [
        DoctorCheckResult("project", "Project", "required", "failed", "1.0", "/plugin", "bad project"),
        DoctorCheckResult("node", "Node", "advisory", "warning", None, None, "node warning"),
        DoctorCheckResult("cmake", "CMake", "required", "passed", "4.4", "/cmake", "ok"),
    ]
    Renderer("human", capabilities(), stdout=stdout, stderr=stderr).render(
        CommandResult(
            "doctor",
            status="failure",
            exit_code=1,
            doctor=DoctorResult("custom", False, 1, 1, checks),
            error=ErrorInfo("doctor_failed", "doctor", "one issue"),
            next_action="Fix the project.",
        )
    )
    output = stdout.getvalue() + stderr.getvalue()
    assert "Doctor - custom" in output
    assert "bad project" in output and "Path: /plugin" in output
    assert "Detected: 1.0" in output
    assert "node warning" in output and "CMake" in output
    assert "Fix the project" in output
