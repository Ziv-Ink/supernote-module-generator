"""Stable subprocess diagnostics and bounded actionable error extraction."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Iterable, Sequence, Tuple
import uuid


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SOURCE_ERROR = re.compile(
    r"(?:^|\s)(?:[^:\s]+[/\\])?[^:\s]+:\d+(?::\d+)?:\s*"
    r"(?:fatal\s+)?(?:error|warning):",
    re.IGNORECASE,
)
_KOTLIN_ERROR = re.compile(r"^(?:e:|w:)\s+.*?:\s", re.IGNORECASE)
_ROOT_CAUSE = re.compile(
    r"(?:caused by:|exception in thread|fatal error:|\berror:|"
    r"execution failed for task|^[A-Za-z0-9_.$]+(?:Exception|Error):)",
    re.IGNORECASE,
)
_FAILURE_CONTEXT = re.compile(r"what went wrong", re.IGNORECASE)
_GENERIC_FAILURE = re.compile(r"\b(?:error|failed|failure|fatal|exception)\b", re.IGNORECASE)
_TASK_NOISE = re.compile(r"^> Task\s+.*(?:UP-TO-DATE|SKIPPED|NO-SOURCE|FAILED)?$")


def relevant_diagnostic_lines(output: str, *, limit: int = 12) -> Tuple[str, ...]:
    """Prioritize source/compiler causes and keep a bounded useful fallback tail."""

    lines = _clean_lines(output)
    source_causes = [
        line
        for line in lines
        if _SOURCE_ERROR.search(line)
        or _KOTLIN_ERROR.search(line)
    ]
    root_causes = [line for line in lines if _ROOT_CAUSE.search(line)]
    context = [line for line in lines if _FAILURE_CONTEXT.search(line)]
    secondary = [
        line
        for line in lines
        if _GENERIC_FAILURE.search(line) and not _TASK_NOISE.match(line)
    ]
    selected = _unique([*source_causes, *root_causes, *context, *secondary])
    if not selected:
        selected = _unique(
            line for line in lines[-limit:] if not _TASK_NOISE.match(line)
        )
    return tuple(selected[:limit])


def write_process_diagnostics(
    plugin_root: Path,
    *,
    name: str,
    command: Sequence[str],
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str | None:
    """Write complete raw process output under the ordinary Android build tree."""

    root = plugin_root.resolve(strict=True)
    relative_parts = ("android", "build", "sn-module-gen", "diagnostics")
    filename = f"{name}.log"
    path = root.joinpath(*relative_parts, filename)
    directory_fd: int | None = None
    temporary_name: str | None = None
    try:
        content = (
            "Command:\n"
            + " ".join(command)
            + f"\n\nExit code: {exit_code}\n\nSTDOUT:\n"
            + stdout
            + ("\n" if stdout and not stdout.endswith("\n") else "")
            + "\nSTDERR:\n"
            + stderr
            + ("\n" if stderr and not stderr.endswith("\n") else "")
        )
        directory_fd = _open_directory_chain_no_follow(root, relative_parts)
        try:
            leaf = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            leaf = None
        if leaf is not None and stat.S_ISLNK(leaf.st_mode):
            return None
        temporary_name = f".{filename}.tmp-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
    except OSError:
        return None
    finally:
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
    return str(path)


def _open_directory_chain_no_follow(root: Path, parts: Sequence[str]) -> int:
    """Open/create a relative directory chain without traversing symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    current = os.open(root, flags)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=current)
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _clean_lines(output: str) -> Tuple[str, ...]:
    return tuple(
        cleaned
        for raw in output.splitlines()
        if (cleaned := _ANSI.sub("", raw).strip())
    )


def _unique(lines: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result
