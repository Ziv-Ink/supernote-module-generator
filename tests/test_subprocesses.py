from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import supernote_module_generator.subprocesses as subprocess_module
from supernote_module_generator.subprocesses import run_process


def test_run_process_uses_the_resolved_host_command(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "supernote_module_generator.subprocesses.host_command",
        lambda command: sys.executable if command == "python-shim" else command,
    )

    result = run_process(
        ["python-shim", "-c", "print('resolved')"],
        cwd=tmp_path,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "resolved\n"
    assert result.args[0] == sys.executable


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if os.name == "posix":
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return bool(state) and not state.startswith("Z")
    return True


def _wait_until_stopped(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    return not _is_running(pid)


def _write_process_tree(tmp_path: Path) -> tuple[Path, Path]:
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, signal, time\n"
        f"open({str(grandchild_pid)!r}, 'w').write(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild)!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return child, grandchild_pid


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_timeout_stops_the_complete_child_process_group(tmp_path: Path):
    child, pid_path = _write_process_tree(tmp_path)

    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, str(child)], cwd=tmp_path, timeout=1)

    grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
    assert _wait_until_stopped(grandchild_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal-forwarding regression")
def test_sigterm_stops_dependency_descendants(tmp_path: Path):
    child, pid_path = _write_process_tree(tmp_path)
    source_root = Path(__file__).parents[1] / "src"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "from supernote_module_generator.subprocesses import run_process\n"
        f"run_process([sys.executable, {str(child)!r}], "
        f"cwd=Path({str(tmp_path)!r}), timeout=60)\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    wrapper_process = subprocess.Popen(
        [sys.executable, str(wrapper)],
        cwd=tmp_path,
        env=environment,
    )
    deadline = time.monotonic() + 3
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_path.is_file()
    grandchild_pid = int(pid_path.read_text(encoding="utf-8"))

    wrapper_process.send_signal(signal.SIGTERM)
    assert wrapper_process.wait(timeout=3) == -signal.SIGTERM
    assert _wait_until_stopped(grandchild_pid)


def test_non_utf8_output_is_replaced_without_losing_the_exit_status(tmp_path: Path):
    result = run_process(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'valid\\n\\xff\\n'); "
            "os.write(2, b'failure\\n\\xfe\\n'); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        timeout=5,
    )

    assert result.returncode == 7
    assert "valid" in result.stdout
    assert "failure" in result.stderr
    assert "\ufffd" in result.stdout
    assert "\ufffd" in result.stderr


def test_streaming_returns_complete_output_and_channel_order(tmp_path: Path):
    streamed: list[tuple[str, str]] = []

    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out-one'); print('err-one', file=sys.stderr)",
        ],
        cwd=tmp_path,
        timeout=5,
        stream=lambda channel, chunk: streamed.append((channel, chunk)),
    )

    assert result.returncode == 0
    assert result.stdout == "out-one\n"
    assert result.stderr == "err-one\n"
    assert ("stdout", "out-one\n") in streamed
    assert ("stderr", "err-one\n") in streamed


def test_streaming_keeps_output_when_the_consumer_closes(tmp_path: Path):
    def closed_consumer(channel: str, chunk: str) -> None:
        assert channel in {"stdout", "stderr"}
        assert chunk
        raise BrokenPipeError

    result = run_process(
        [sys.executable, "-c", "print('retained')"],
        cwd=tmp_path,
        timeout=5,
        stream=closed_consumer,
    )

    assert result.returncode == 0
    assert result.stdout == "retained\n"


def test_graceful_stop_escalates_a_stubborn_process(monkeypatch):
    class StubbornProcess:
        pid = 1234

        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits < 3:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    signals: list[bool] = []
    monkeypatch.setattr(
        subprocess_module,
        "_signal_tree",
        lambda process, *, force: signals.append(force),
    )
    process = StubbornProcess()

    subprocess_module._stop_tree(process, graceful=True)

    assert signals == [False, True, True]
    assert process.waits == 3


def test_windows_tree_signal_falls_back_to_direct_kill(monkeypatch):
    class Process:
        pid = 4321

        def __init__(self) -> None:
            self.killed = False

        def poll(self):
            return None

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(subprocess_module, "os", type("Os", (), {"name": "nt"})())
    monkeypatch.setattr(
        subprocess_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("taskkill unavailable")),
    )
    process = Process()

    subprocess_module._signal_tree(process, force=True)

    assert process.killed


@pytest.mark.parametrize("force", [False, True])
def test_unknown_host_signal_uses_the_direct_process_handle(monkeypatch, force: bool):
    class Process:
        pid = 5678

        def __init__(self) -> None:
            self.action = ""

        def poll(self):
            return None

        def kill(self) -> None:
            self.action = "kill"

        def terminate(self) -> None:
            self.action = "terminate"

    monkeypatch.setattr(subprocess_module, "os", type("Os", (), {"name": "other"})())
    process = Process()

    subprocess_module._signal_tree(process, force=force)

    assert process.action == ("kill" if force else "terminate")


def test_windows_processes_start_in_a_new_process_group(monkeypatch):
    monkeypatch.setattr(subprocess_module, "os", type("Os", (), {"name": "nt"})())

    assert subprocess_module._popen_options() == {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    }


def test_unknown_hosts_do_not_receive_platform_specific_popen_options(monkeypatch):
    monkeypatch.setattr(subprocess_module, "os", type("Os", (), {"name": "other"})())

    assert subprocess_module._popen_options() == {}


def test_sigterm_forwarding_is_not_installed_from_a_worker_thread(monkeypatch):
    worker = object()
    main = object()
    monkeypatch.setattr(subprocess_module.threading, "current_thread", lambda: worker)
    monkeypatch.setattr(subprocess_module.threading, "main_thread", lambda: main)
    entered = False

    with subprocess_module._forward_sigterm(object()):
        entered = True

    assert entered
