"""Non-blocking plugin-root command serialization."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator

from .errors import ConfigurationError, FilesystemError


class PluginBusyError(ConfigurationError):
    """Another generator process currently owns this plugin."""

    kind = "plugin_busy"
    phase = "preflight"


@contextmanager
def plugin_operation_lock(plugin_root: Path) -> Iterator[None]:
    """Lock the plugin directory without adding a user-visible lock file."""

    root = plugin_root.resolve()
    try:
        descriptor = os.open(root, os.O_RDONLY)
    except OSError as exc:
        raise FilesystemError(f"Could not open the plugin directory: {root}") from exc
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise PluginBusyError(
                "Another supernote-module command is already running for this plugin. "
                "Wait for it to finish and try again."
            ) from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
