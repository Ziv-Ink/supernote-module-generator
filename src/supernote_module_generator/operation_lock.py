"""Non-blocking plugin-root command serialization."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading
from typing import Iterator

from .errors import ConfigurationError, FilesystemError


class PluginBusyError(ConfigurationError):
    """Another generator process currently owns this plugin."""

    kind = "plugin_busy"
    phase = "preflight"


_PROCESS_LOCKS: set[str] = set()
_PROCESS_LOCKS_GUARD = threading.Lock()


def _lock_identity(root: Path) -> str:
    value = str(root)
    if os.name == "nt":
        value = os.path.normcase(value)
    return value


def _busy() -> PluginBusyError:
    return PluginBusyError(
        "Another supernote-module command is already running for this plugin. "
        "Wait for it to finish and try again."
    )


@contextmanager
def _process_claim(identity: str) -> Iterator[None]:
    with _PROCESS_LOCKS_GUARD:
        if identity in _PROCESS_LOCKS:
            raise _busy()
        _PROCESS_LOCKS.add(identity)
    try:
        yield
    finally:
        with _PROCESS_LOCKS_GUARD:
            _PROCESS_LOCKS.discard(identity)


@contextmanager
def _posix_directory_lock(root: Path) -> Iterator[None]:
    import fcntl

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
            raise _busy() from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _windows_mutex_name(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"Local\\SupernoteModuleGenerator-{digest}"


@contextmanager
def _windows_named_mutex(identity: str) -> Iterator[None]:
    # Imports stay inside the Windows-only path so this module remains
    # importable on every supported host.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = [wintypes.HANDLE]
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_mutex(None, False, _windows_mutex_name(identity))
    if not handle:
        error = ctypes.get_last_error()
        raise FilesystemError(
            f"Could not create the Windows plugin-operation mutex (error {error})."
        )

    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    acquired = False
    try:
        result = wait_for_single_object(handle, 0)
        if result in (wait_object_0, wait_abandoned):
            acquired = True
        elif result == wait_timeout:
            raise _busy()
        elif result == wait_failed:
            error = ctypes.get_last_error()
            raise FilesystemError(
                f"Could not wait for the Windows plugin-operation mutex (error {error})."
            )
        else:
            raise FilesystemError(
                f"Windows returned an unexpected plugin-operation mutex status: {result}."
            )
        yield
    finally:
        if acquired:
            release_mutex(handle)
        close_handle(handle)


@contextmanager
def plugin_operation_lock(plugin_root: Path) -> Iterator[None]:
    """Lock the plugin directory without adding a user-visible lock file."""

    root = plugin_root.resolve()
    identity = _lock_identity(root)
    with _process_claim(identity):
        if os.name == "nt":
            with _windows_named_mutex(identity):
                yield
            return
        if os.name == "posix":
            with _posix_directory_lock(root):
                yield
            return
        raise FilesystemError(f"Unsupported host platform for plugin locking: {os.name}")
