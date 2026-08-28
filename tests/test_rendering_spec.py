from __future__ import annotations

import io
import time

import pytest

from supernote_module_generator.models import (
    Change,
    CommandResult,
    DoctorCheckResult,
    DoctorResult,
    ErrorInfo,
    RollbackResult,
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
            "code": "SNV4_PARENT_DEPENDENCY",
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
