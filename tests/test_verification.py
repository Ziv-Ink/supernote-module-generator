from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import supernote_module_generator.verification as verification


def _configure_gradle(monkeypatch, root: Path) -> list[str]:
    gradle = root / "android/gradlew"
    command = [str(gradle), ":app:assembleDebug"]
    monkeypatch.setattr(verification, "gradle_wrapper_path", lambda _root: gradle)
    monkeypatch.setattr(
        verification,
        "gradle_wrapper_command",
        lambda actual, arguments: [str(actual), *arguments],
    )
    return command


def test_android_build_success_forwards_verbose_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = _configure_gradle(monkeypatch, tmp_path)
    streamed: list[tuple[str, str]] = []
    calls: list[tuple[list[str], Path, int, object]] = []

    def run_process(actual, *, cwd, timeout, stream):
        calls.append((actual, cwd, timeout, stream))
        return subprocess.CompletedProcess(actual, 0, "built\n", "")

    monkeypatch.setattr(verification, "run_process", run_process)

    assert verification.build_android(
        tmp_path,
        verbose=True,
        stream=lambda channel, line: streamed.append((channel, line)),
    ) == (True, None, 0)
    assert calls == [(command, tmp_path / "android", 1200, calls[0][3])]
    assert callable(calls[0][3])
    assert streamed == []


def test_android_build_failure_reports_relevant_unique_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = _configure_gradle(monkeypatch, tmp_path)

    def run_process(actual, **_kwargs):
        return subprocess.CompletedProcess(
            actual,
            7,
            "noise\n> Task :app:compile FAILED\nerror: broken\nerror: broken\n",
            "fatal exception\n",
        )

    monkeypatch.setattr(verification, "run_process", run_process)

    passed, error, duration = verification.build_android(tmp_path, verbose=False)

    assert not passed
    assert duration == 0
    assert error is not None
    assert error.command == command
    assert error.exit_code == 7
    assert error.relevant_lines == [
        "> Task :app:compile FAILED",
        "error: broken",
        "fatal exception",
    ]


def test_android_build_failure_falls_back_to_last_eight_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_gradle(monkeypatch, tmp_path)
    output = "\n".join(f"line {index}" for index in range(12))
    monkeypatch.setattr(
        verification,
        "run_process",
        lambda actual, **_kwargs: subprocess.CompletedProcess(actual, 2, output, ""),
    )

    passed, error, _duration = verification.build_android(tmp_path, verbose=False)

    assert not passed
    assert error is not None
    assert error.relevant_lines == [f"line {index}" for index in range(4, 12)]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("cannot execute Gradle"),
        subprocess.TimeoutExpired(["gradlew"], 1200),
    ],
)
def test_android_build_start_failure_is_structured(
    tmp_path: Path,
    monkeypatch,
    failure: BaseException,
) -> None:
    command = _configure_gradle(monkeypatch, tmp_path)

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(verification, "run_process", fail)

    passed, error, duration = verification.build_android(tmp_path, verbose=False)

    assert not passed
    assert duration == 0
    assert error is not None
    assert error.command == command
    assert error.exit_code == 1
    assert error.relevant_lines == [str(failure)]
