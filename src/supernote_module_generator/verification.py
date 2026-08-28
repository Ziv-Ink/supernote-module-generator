"""Android build invocation retained by the active V4 feature workflow."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable, List, Optional, Tuple

from .models import SubprocessError
from .platform_tools import gradle_wrapper_command, gradle_wrapper_path
from .subprocesses import run_process


def build_android(
    root: Path,
    *,
    verbose: bool,
    stream: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, Optional[SubprocessError], int]:
    gradle = gradle_wrapper_path(root)
    command = gradle_wrapper_command(gradle, [":app:assembleDebug"])
    try:
        result = run_process(
            command,
            cwd=root / "android",
            timeout=1200,
            stream=stream if verbose else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, SubprocessError(command, 1, [str(exc)]), 0
    if result.returncode == 0:
        return True, None, 0
    lines = _relevant_lines(result.stdout + "\n" + result.stderr)
    return False, SubprocessError(command, result.returncode, lines), 0


def _relevant_lines(output: str) -> List[str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    preferred = [
        line
        for line in lines
        if any(
            token in line.lower()
            for token in ("error", "failed", "fatal", "exception", "> task")
        )
    ]
    selected = preferred or lines[-8:]
    unique: List[str] = []
    for line in selected:
        if line not in unique:
            unique.append(line)
    return unique[:9]
