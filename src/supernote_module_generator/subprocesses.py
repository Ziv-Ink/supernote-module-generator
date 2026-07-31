"""Subprocess execution with optional real-time, stream-preserving output."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    stream: Optional[Callable[[str, str], None]] = None,
) -> subprocess.CompletedProcess[str]:
    if stream is None:
        return subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
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
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    return subprocess.CompletedProcess(
        list(command),
        return_code,
        "".join(stdout_parts),
        "".join(stderr_parts),
    )
