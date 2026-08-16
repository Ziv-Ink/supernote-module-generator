"""Subprocess execution with bounded, process-tree-aware cleanup."""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence

from .platform_tools import host_command


def _popen_options() -> dict[str, object]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {}


def _signal_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    """Signal the complete child tree without ever targeting our own group."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        return
    if os.name == "nt":
        # taskkill is part of Windows and is the only generally available way
        # to include grandchildren that do not share a Python Popen handle.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        except OSError:
            if process.poll() is None:
                process.kill()
        return
    if process.poll() is None:
        (process.kill if force else process.terminate)()


def _stop_tree(process: subprocess.Popen[str], *, graceful: bool) -> None:
    _signal_tree(process, force=not graceful)
    if graceful:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # The direct child may have exited while a descendant ignored SIGTERM.
        # Sending SIGKILL to the now-empty process group is harmless.
        _signal_tree(process, force=True)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _signal_tree(process, force=True)
        process.wait()


@contextmanager
def _forward_sigterm(process: subprocess.Popen[str]) -> Iterator[None]:
    """Make an externally terminated CLI stop its active dependency tree."""
    if (
        os.name != "posix"
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def handle(signum: int, frame: object) -> None:
        _signal_tree(process, force=True)
        signal.signal(signal.SIGTERM, previous)
        if previous == signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)  # type: ignore[arg-type]
            return
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, handle)
    try:
        yield
    finally:
        if signal.getsignal(signal.SIGTERM) is handle:
            signal.signal(signal.SIGTERM, previous)


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    stream: Optional[Callable[[str, str], None]] = None,
) -> subprocess.CompletedProcess[str]:
    resolved_command = list(command)
    if resolved_command:
        resolved_command[0] = host_command(resolved_command[0])
    process = subprocess.Popen(
        resolved_command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_popen_options(),
    )
    if stream is None:
        try:
            with _forward_sigterm(process):
                stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_tree(process, graceful=False)
            stdout, stderr = process.communicate()
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        except KeyboardInterrupt:
            _stop_tree(process, graceful=True)
            raise
        return subprocess.CompletedProcess(
            resolved_command, process.returncode, stdout, stderr
        )

    stdout_parts: List[str] = []
    stderr_parts: List[str] = []

    def pump(name: str, pipe: object, parts: List[str]) -> None:
        assert pipe is not None
        for chunk in iter(pipe.readline, ""):  # type: ignore[attr-defined]
            parts.append(chunk)
            try:
                stream(name, chunk)
            except BrokenPipeError:
                pass
        pipe.close()  # type: ignore[attr-defined]

    threads = [
        threading.Thread(
            target=pump,
            args=("stdout", process.stdout, stdout_parts),
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=("stderr", process.stderr, stderr_parts),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        with _forward_sigterm(process):
            return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_tree(process, graceful=False)
        raise
    except KeyboardInterrupt:
        _stop_tree(process, graceful=True)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    return subprocess.CompletedProcess(
        resolved_command,
        return_code,
        "".join(stdout_parts),
        "".join(stderr_parts),
    )
