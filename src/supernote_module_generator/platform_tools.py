"""Host-platform paths and commands used by the Android toolchain."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence


def gradle_wrapper_path(root: Path, *, platform_name: Optional[str] = None) -> Path:
    """Return the checked-in Gradle wrapper for the current host."""

    host = os.name if platform_name is None else platform_name
    name = "gradlew.bat" if host == "nt" else "gradlew"
    return root / "android" / name


def gradle_wrapper_command(
    wrapper: Path,
    arguments: Sequence[str],
    *,
    platform_name: Optional[str] = None,
) -> List[str]:
    """Build a command that can execute the host's Gradle wrapper."""

    host = os.name if platform_name is None else platform_name
    if host == "nt" or os.access(wrapper, os.X_OK):
        return [str(wrapper), *arguments]
    return ["sh", str(wrapper), *arguments]


def ndk_compiler_path(
    prebuilt_root: Path,
    compiler: str,
    *,
    platform_name: Optional[str] = None,
) -> Optional[Path]:
    """Find an NDK host compiler, including Windows' required .exe suffix."""

    host = os.name if platform_name is None else platform_name
    executable = f"{compiler}.exe" if host == "nt" else compiler
    if not prebuilt_root.is_dir():
        return None
    return next(iter(sorted(prebuilt_root.glob(f"*/bin/{executable}"))), None)
