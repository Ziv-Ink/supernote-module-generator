from __future__ import annotations

import io

from supernote_module_generator.models import (
    CommandResult,
    DoctorCheckResult,
    DoctorResult,
    ErrorInfo,
)
from supernote_module_generator.rendering import Renderer, TerminalCapabilities


def capabilities() -> TerminalCapabilities:
    return TerminalCapabilities(False, False, False, False, 80, 24)


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

    assert "Doctor — All" in stdout.getvalue()
    assert "Node.js was not found." in stdout.getvalue()
    assert "Doctor found 1 required issue" in stderr.getvalue()


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

    assert "Doctor — Native Module" in stdout.getvalue()
    assert "Doctor found no required issues" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_quiet_doctor_keeps_advisories_but_suppresses_report_detail():
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = Renderer("quiet", capabilities(), stdout=stdout, stderr=stderr)
    advisory = DoctorCheckResult(
        "adb_device",
        "Connected device",
        "advisory",
        "warning",
        None,
        None,
        "No authorized device is connected.",
    )

    renderer.render(
        CommandResult(
            "doctor",
            doctor=DoctorResult("all", True, 0, 1, [advisory]),
        )
    )

    assert stdout.getvalue() == "Doctor found no required issues\n"
    assert "Connected device: No authorized device is connected." in stderr.getvalue()
    assert "Doctor — All" not in stdout.getvalue()


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
