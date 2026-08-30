from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import supernote_module_generator.filesystem as filesystem
from supernote_module_generator.windows_authority import (
    _GenerationTransition,
    GenerationAuthority,
    RawCloseOutcome,
    RawCloseState,
    RawGenerationReference,
)


def _authority(
    ancestors: tuple[int, ...] = (),
) -> GenerationAuthority:
    return GenerationAuthority(_references(ancestors))


def _references(
    handles: tuple[int, ...],
) -> tuple[RawGenerationReference, ...]:
    return tuple(
        RawGenerationReference(
            handle,
            GenerationAuthority(()),
        )
        for handle in handles
    )


def _reference_handles(
    references: tuple[RawGenerationReference, ...],
) -> tuple[int, ...]:
    return tuple(reference.handle for reference in references)


def _retired_handles(
    retired: dict[
        object,
        tuple[RawGenerationReference, ...],
    ],
) -> tuple[tuple[int, ...], ...]:
    return tuple(_reference_handles(references) for references in retired.values())


class _Function:
    def __init__(self, handler):
        self.handler = handler
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.handler(*args)


class _Kernel32:
    def __init__(self) -> None:
        self.next_handle = 100
        self.paths: dict[int, str] = {}
        self.create_calls: list[tuple[str, int, int]] = []
        self.attributes: dict[str, int] = {}
        self.closed: list[int] = []
        self.basic_updates: list[SimpleNamespace] = []
        self.rename_updates: list[bytes] = []
        self.reparse_payload = b""
        self.final_paths: dict[int, str] = {}
        self.directory_page: bytes | None = None
        self.directory_sent = False
        self.last_error = 0
        self.fail_close = False
        self.fail_set = False
        self.fail_device = False
        self.after_basic_read = None
        self.CreateFileW = _Function(self._create_file)
        self.GetFileInformationByHandleEx = _Function(self._get_info)
        self.GetFinalPathNameByHandleW = _Function(self._get_final_path)
        self.CloseHandle = _Function(self._close)
        self.SetFileInformationByHandle = _Function(self._set_info)
        self.DeviceIoControl = _Function(self._device_io)

    @staticmethod
    def _number(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    def _create_file(self, path, desired_access, share_mode, *_args):
        self.create_calls.append(
            (str(path), self._number(desired_access), self._number(share_mode))
        )
        handle = self.next_handle
        self.next_handle += 1
        self.paths[handle] = str(path)
        return handle

    def _get_info(self, handle, info_class, pointer, _size):
        raw = self._number(handle)
        if info_class == 9:
            pointer._obj.FileAttributes = self.attributes.get(self.paths[raw], 0)
            pointer._obj.ReparseTag = 0
            return 1
        if info_class == 0:
            pointer._obj.CreationTime = 0
            pointer._obj.LastAccessTime = 0
            pointer._obj.LastWriteTime = 0
            pointer._obj.ChangeTime = 0
            pointer._obj.FileAttributes = self.attributes.get(self.paths.get(raw, ""), 0)
            if self.after_basic_read is not None:
                self.after_basic_read()
            return 1
        if info_class in {10, 11}:
            if self.directory_page is not None and not self.directory_sent:
                ctypes.memmove(pointer, self.directory_page, len(self.directory_page))
                self.directory_sent = True
                return 1
            self.last_error = 18
            return 0
        raise AssertionError(f"unexpected information class {info_class}")

    def _get_final_path(self, handle, buffer, _size, _flags):
        raw = self._number(handle)
        value = self.final_paths.get(raw, self.paths[raw])
        if buffer is None:
            return len(value) + 1
        buffer.value = value
        return len(value)

    def _close(self, handle):
        self.closed.append(self._number(handle))
        if self.fail_close:
            self.last_error = 5
            return 0
        return 1

    def _set_info(self, _handle, info_class, pointer, size):
        if self.fail_set:
            self.last_error = 5
            return 0
        if info_class == 3:
            self.rename_updates.append(ctypes.string_at(pointer, size))
            return 1
        row = pointer._obj
        self.basic_updates.append(
            SimpleNamespace(
                LastAccessTime=int(row.LastAccessTime),
                LastWriteTime=int(row.LastWriteTime),
                FileAttributes=int(row.FileAttributes),
            )
        )
        return 1

    def _device_io(
        self,
        _handle,
        _control,
        _input,
        _input_size,
        output,
        _output_size,
        returned,
        _overlapped,
    ):
        if self.fail_device:
            self.last_error = 5
            return 0
        ctypes.memmove(output, self.reparse_payload, len(self.reparse_payload))
        returned._obj.value = len(self.reparse_payload)
        return 1


def test_windows_conditional_open_denies_write_and_delete_sharing(monkeypatch):
    observed: dict[str, object] = {}

    def open_handle(path, **kwargs):
        observed["path"] = path
        observed.update(kwargs)
        return 91

    monkeypatch.setattr(filesystem, "_windows_open_no_follow_handle", open_handle)
    monkeypatch.setattr(
        filesystem, "_windows_handle_to_descriptor", lambda handle, flags: 73
    )

    assert filesystem._windows_open_conditional_regular_descriptor(
        Path("C:/plugin/value.js")
    ) == 73
    assert observed["desired_access"] == 0x80000000 | 0x10000 | 0x100 | 0x80
    assert observed["share_mode"] == 0x1


def test_windows_descriptor_rename_uses_no_replace_file_rename_info(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    destination = Path("C:/plugin/long 名/value.js")
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(filesystem, "_windows_descriptor_handle", lambda _fd: 91)
    rename_path = r"\??\C:\plugin\long 名\value.js"
    monkeypatch.setattr(filesystem, "_windows_rename_path", lambda _path: rename_path)

    filesystem._windows_rename_descriptor_no_replace(73, destination)

    assert len(windows_api.rename_updates) == 1
    raw = windows_api.rename_updates[0]
    encoded = rename_path.encode("utf-16-le")
    assert raw[:4] == b"\0\0\0\0"
    assert int.from_bytes(raw[16:20], "little") == len(encoded)
    assert raw.endswith(encoded)


def test_windows_descriptor_rename_surfaces_native_failure(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    windows_api.fail_set = True
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(filesystem, "_windows_descriptor_handle", lambda _fd: 91)
    monkeypatch.setattr(
        filesystem,
        "_windows_rename_path",
        lambda _path: r"\??\C:\plugin\value.js",
    )

    with pytest.raises(OSError, match="fake Windows error"):
        filesystem._windows_rename_descriptor_no_replace(
            73, Path("C:/plugin/value.js")
        )


@pytest.mark.parametrize(
    ("extended", "expected"),
    [
        (r"\\?\C:\plugin\value.js", r"\??\C:\plugin\value.js"),
        (r"\\?\UNC\server\share\value.js", r"\??\UNC\server\share\value.js"),
    ],
)
def test_windows_rename_path_uses_nt_namespace(
    extended: str,
    expected: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(filesystem, "_windows_api_path", lambda _path: extended)

    assert filesystem._windows_rename_path(Path("value.js")) == expected


def test_windows_conditional_open_closes_registered_handle_on_transfer_failure(
    monkeypatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(
        filesystem, "_windows_open_no_follow_handle", lambda *_args, **_kwargs: 91
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_handle_to_descriptor",
        lambda *_args: (_ for _ in ()).throw(OSError("transfer failed")),
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handle_generation",
        lambda _handle: GenerationAuthority(()),
    )
    monkeypatch.setattr(filesystem, "_windows_close_raw_handle", closed.append)

    with pytest.raises(OSError, match="transfer failed"):
        filesystem._windows_open_conditional_regular_descriptor(Path("value.js"))

    assert closed == [91]


def test_windows_conditional_descriptor_read_restores_observed_atime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "value.js"
    path.write_bytes(b"content")
    descriptor = os.open(path, os.O_RDONLY)
    actual = os.fstat(descriptor)
    before = SimpleNamespace(
        st_mode=actual.st_mode,
        st_dev=actual.st_dev,
        st_ino=actual.st_ino,
        st_mtime_ns=actual.st_mtime_ns,
        st_atime_ns=1_000,
        st_size=actual.st_size,
    )
    after = SimpleNamespace(**{**before.__dict__, "st_atime_ns": 1_200})
    observations = iter((before, after))
    applied: list[dict[str, object]] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(filesystem.os, "fstat", lambda _fd: next(observations))
    monkeypatch.setattr(filesystem, "_windows_descriptor_handle", lambda _fd: 91)
    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata_values",
        lambda _handle, **kwargs: applied.append(kwargs),
    )
    try:
        content, metadata = filesystem._read_windows_conditional_regular_descriptor(
            descriptor
        )
    finally:
        os.close(descriptor)

    assert content == b"content"
    assert metadata is before
    assert applied == [
        {
            "mode": None,
            "regular": True,
            "atime_ns": 1_000,
            "mtime_ns": None,
        }
    ]


@pytest.mark.parametrize(
    ("directory", "write_metadata", "override", "expected"),
    [
        (False, False, None, 0x80000000 | 0x80),
        (True, False, None, 0x1 | 0x80),
        (True, True, None, 0x100 | 0x80),
        (False, True, 0x10000, 0x10000 | 0x100),
    ],
)
def test_windows_desired_access_is_scoped_to_operation(
    directory: bool,
    write_metadata: bool,
    override: int | None,
    expected: int,
) -> None:
    assert filesystem._windows_desired_access(
        directory=directory,
        write_metadata=write_metadata,
        override=override,
    ) == expected

@pytest.fixture
def windows_api(monkeypatch):
    kernel = _Kernel32()
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(filesystem, "_windows_kernel32", lambda: kernel)
    monkeypatch.setattr(filesystem, "_windows_api_path", lambda path: str(path))
    monkeypatch.setattr(filesystem, "_windows_path_key", lambda path: str(path))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.last_error, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda code=None: OSError(code or kernel.last_error, "fake Windows error"),
        raising=False,
    )
    return kernel


def test_windows_open_retains_ancestors_and_classifies_leaf(
    tmp_path: Path,
    windows_api: _Kernel32,
) -> None:
    target = tmp_path / "root/child/value.txt"
    for parent in (tmp_path, tmp_path / "root", tmp_path / "root/child"):
        windows_api.attributes[str(parent)] = 0x10
    windows_api.attributes[str(target)] = 0

    handle = filesystem._windows_open_no_follow_handle(
        target,
        directory=False,
        write_metadata=True,
    )
    try:
        assert filesystem._windows_handle_entry_kind(handle) == "file"
    finally:
        filesystem._windows_close_handle(handle)

    assert len(windows_api.closed) >= 4
    assert len(windows_api.closed) == len(set(windows_api.closed))
    assert not set(windows_api.closed).intersection(
        filesystem._WINDOWS_AUTHORITY.handles
    )
    ancestor_calls = windows_api.create_calls[:-1]
    assert ancestor_calls
    assert windows_api.create_calls[-1][1] & 0x100
    assert all(access & 0x1 for _path, access, _share in ancestor_calls)
    assert all(share == 0x1 for _path, _access, share in ancestor_calls)


def test_windows_descriptor_owns_retained_ancestors_until_close(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    target = tmp_path / "root/child/value.txt"
    for parent in (tmp_path, tmp_path / "root", tmp_path / "root/child"):
        windows_api.attributes[str(parent)] = 0x10
    windows_api.attributes[str(target)] = 0
    descriptor = 73
    closed_descriptors: list[int] = []
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
    )
    monkeypatch.setattr(os, "close", closed_descriptors.append)

    handle = filesystem._windows_open_no_follow_handle(target, directory=False)
    retained = filesystem._WINDOWS_AUTHORITY.handles[handle]
    transferred = filesystem._windows_handle_to_descriptor(handle, os.O_RDONLY)

    assert transferred == descriptor
    assert handle not in filesystem._WINDOWS_AUTHORITY.handles
    descriptor_generation = (
        filesystem._WINDOWS_AUTHORITY.descriptors[descriptor]
    )
    assert descriptor_generation is not retained
    assert descriptor_generation.ancestors == retained.ancestors
    assert not set(retained.ancestors).intersection(windows_api.closed)

    filesystem._close_descriptor(descriptor)

    assert closed_descriptors == [descriptor]
    assert set(retained.ancestors).issubset(windows_api.closed)
    assert descriptor not in filesystem._WINDOWS_AUTHORITY.descriptors


def test_windows_unregistered_raw_handle_transfers_empty_descriptor_authority(
    monkeypatch,
) -> None:
    descriptor = 73
    raw_authority: dict[int, GenerationAuthority] = {}
    descriptor_authority: dict[int, GenerationAuthority] = {}
    closed_descriptors: list[int] = []
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "descriptors",
        descriptor_authority,
    )
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
    )
    monkeypatch.setattr(os, "close", closed_descriptors.append)

    assert filesystem._windows_handle_to_descriptor(91, os.O_RDONLY) == descriptor
    assert raw_authority == {}
    assert descriptor_authority[descriptor].ancestors == ()

    filesystem._close_descriptor(descriptor)

    assert closed_descriptors == [descriptor]
    assert descriptor_authority == {}


def test_windows_handle_registration_interruption_closes_authority_once(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    target = tmp_path / "value.txt"
    leaf_handle = 91
    ancestors = (81, 82)

    class InterruptAfterRegistration(
        dict[int, GenerationAuthority]
    ):
        def __setitem__(
            self,
            key: int,
            value: GenerationAuthority,
        ) -> None:
            super().__setitem__(key, value)
            raise KeyboardInterrupt("after handle registration")

    authority = InterruptAfterRegistration()
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        authority,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_retain_non_reparse_ancestors",
        lambda _path: list(ancestors),
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_create_raw_handle",
        lambda _path, _access: leaf_handle,
    )
    monkeypatch.setattr(filesystem, "_windows_handle_attributes", lambda _handle: 0)
    monkeypatch.setattr(
        filesystem,
        "_windows_handle_final_path",
        lambda _handle: target,
    )

    with pytest.raises(KeyboardInterrupt, match="after handle registration"):
        filesystem._windows_open_no_follow_handle(target, directory=False)

    assert windows_api.closed == [leaf_handle, *reversed(ancestors)]
    assert authority == {}


def test_windows_descriptor_transfer_interruption_closes_descriptor_once(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    leaf_handle = 91
    descriptor = 73
    ancestors = (81, 82)
    raw_generation = _authority(ancestors)
    raw_authority = {leaf_handle: raw_generation}

    class InterruptAfterTransfer(
        dict[int, GenerationAuthority]
    ):
        armed = True

        def get(
            self,
            key: int,
            default: GenerationAuthority | None = None,
        ) -> GenerationAuthority | None:
            if self.armed:
                self.armed = False
                raise KeyboardInterrupt("after descriptor transfer")
            return super().get(key, default)

    descriptor_authority = InterruptAfterTransfer()
    transferred: list[tuple[int, int]] = []
    closed_descriptors: list[int] = []
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "descriptors",
        descriptor_authority,
    )
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            open_osfhandle=lambda handle, flags: (
                transferred.append((handle, flags)),
                descriptor,
            )[1]
        ),
    )
    monkeypatch.setattr(os, "close", closed_descriptors.append)

    with pytest.raises(KeyboardInterrupt, match="after descriptor transfer"):
        filesystem._windows_handle_to_descriptor(leaf_handle, os.O_RDONLY)

    assert transferred == [(leaf_handle, os.O_RDONLY)]
    assert closed_descriptors == [descriptor]
    assert windows_api.closed == [*reversed(ancestors)]
    assert raw_authority == {}
    assert descriptor_authority == {}


def test_windows_descriptor_transfer_preopen_interruption_keeps_cleanup_owned(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    leaf_handle = 91
    ancestors = (81, 82)
    raw_authority = {leaf_handle: _authority(ancestors)}
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            open_osfhandle=lambda _handle, _flags: (_ for _ in ()).throw(
                KeyboardInterrupt("before descriptor transfer")
            )
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="before descriptor transfer"):
        filesystem._windows_handle_to_descriptor(leaf_handle, os.O_RDONLY)

    assert windows_api.closed == [leaf_handle, *reversed(ancestors)]
    assert raw_authority == {}


def test_windows_descriptor_close_preserves_original_authority_across_numeric_reuse(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    descriptor = 73
    original_ancestors = (81,)
    reused_ancestors = (91,)
    original_generation = _authority(original_ancestors)
    reused_generation = _authority(reused_ancestors)
    monkeypatch.setitem(
        filesystem._WINDOWS_AUTHORITY.descriptors,
        descriptor,
        original_generation,
    )

    def close_and_reuse(value: int) -> None:
        assert value == descriptor
        filesystem._WINDOWS_AUTHORITY.descriptors[value] = (
            reused_generation
        )

    monkeypatch.setattr(os, "close", close_and_reuse)

    filesystem._close_descriptor(descriptor)

    assert windows_api.closed == [*reversed(original_ancestors)]
    assert (
        filesystem._WINDOWS_AUTHORITY.descriptors[descriptor]
        is reused_generation
    )


def test_windows_raw_close_setup_failure_keeps_registered_authority(
    monkeypatch,
) -> None:
    leaf_handle = 91
    ancestors = (81, 82)
    generation = _authority(ancestors)
    raw_authority = {leaf_handle: generation}
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_kernel32",
        lambda: (_ for _ in ()).throw(OSError("kernel setup failed")),
    )

    with pytest.raises(OSError, match="kernel setup failed"):
        filesystem._windows_close_raw_handle(leaf_handle)

    assert raw_authority == {leaf_handle: generation}


def test_windows_raw_numeric_reuse_reconciles_post_close_failure_authority(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    leaf_handle = 55
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    old_generation = _authority(old_ancestors)
    raw_authority = {leaf_handle: old_generation}
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )

    def close_then_raise(handle) -> int:
        windows_api.closed.append(windows_api._number(handle))
        raise KeyboardInterrupt("after raw close")

    windows_api.CloseHandle.handler = close_then_raise
    with pytest.raises(KeyboardInterrupt, match="after raw close"):
        filesystem._windows_close_raw_handle(leaf_handle)
    assert raw_authority == {leaf_handle: old_generation}

    target = tmp_path / "value.txt"
    windows_api.CloseHandle.handler = windows_api._close
    monkeypatch.setattr(
        filesystem,
        "_windows_retain_non_reparse_ancestors",
        lambda _path: list(new_ancestors),
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_create_raw_handle",
        lambda _path, _access: leaf_handle,
    )
    monkeypatch.setattr(filesystem, "_windows_handle_attributes", lambda _handle: 0)
    monkeypatch.setattr(
        filesystem,
        "_windows_handle_final_path",
        lambda _handle: target,
    )

    assert filesystem._windows_open_no_follow_handle(target, directory=False) == (
        leaf_handle
    )
    new_generation = raw_authority[leaf_handle]
    assert new_generation is not old_generation
    assert new_generation.ancestors == new_ancestors

    filesystem._windows_close_raw_handle(leaf_handle)

    assert raw_authority == {}
    assert windows_api.closed == [
        leaf_handle,
        *reversed(old_ancestors),
        leaf_handle,
        *reversed(new_ancestors),
    ]


def test_windows_descriptor_numeric_reuse_reconciles_each_generation(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    raw_handle = 55
    descriptor = 73
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    raw_generation = _authority(new_ancestors)
    old_descriptor_generation = _authority(old_ancestors)
    raw_authority = {raw_handle: raw_generation}
    descriptor_authority = {descriptor: old_descriptor_generation}
    closed_descriptors: list[int] = []
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "descriptors",
        descriptor_authority,
    )

    def close_then_raise(value: int) -> None:
        closed_descriptors.append(value)
        raise KeyboardInterrupt("after descriptor close")

    monkeypatch.setattr(os, "close", close_then_raise)
    with pytest.raises(KeyboardInterrupt, match="after descriptor close"):
        filesystem._close_descriptor(descriptor)
    assert descriptor_authority == {descriptor: old_descriptor_generation}

    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
    )
    monkeypatch.setattr(os, "close", closed_descriptors.append)

    assert filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY) == (
        descriptor
    )
    assert raw_authority == {}
    new_descriptor_generation = descriptor_authority[descriptor]
    assert new_descriptor_generation is not old_descriptor_generation
    assert new_descriptor_generation.ancestors == new_ancestors

    filesystem._close_descriptor(descriptor)

    assert descriptor_authority == {}
    assert closed_descriptors == [descriptor, descriptor]
    assert windows_api.closed == [
        *reversed(old_ancestors),
        *reversed(new_ancestors),
    ]


def test_windows_raw_reuse_retains_stale_authority_when_cleanup_fails(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    leaf_handle = 55
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    raw_authority = {leaf_handle: _authority(old_ancestors)}
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    real_close_attempt = filesystem._windows_attempt_close_raw_handle
    fail_next = True

    def fail_first_cleanup(
        handle: int,
    ) -> RawCloseOutcome:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            return RawCloseOutcome(
                RawCloseState.RETRYABLE,
                OSError("stale cleanup failed"),
            )
        return real_close_attempt(handle)

    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        fail_first_cleanup,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_retain_non_reparse_ancestors",
        lambda _path: list(new_ancestors),
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_create_raw_handle",
        lambda _path, _access: leaf_handle,
    )
    monkeypatch.setattr(filesystem, "_windows_handle_attributes", lambda _handle: 0)
    target = tmp_path / "value.txt"
    monkeypatch.setattr(
        filesystem,
        "_windows_handle_final_path",
        lambda _handle: target,
    )

    with pytest.raises(OSError, match="stale cleanup failed"):
        filesystem._windows_open_no_follow_handle(target, directory=False)

    assert raw_authority == {}
    assert _retired_handles(retired) == (old_ancestors,)
    assert windows_api.closed == [leaf_handle, *reversed(new_ancestors)]

    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert retired == {}
    assert windows_api.closed == [
        leaf_handle,
        *reversed(new_ancestors),
        *reversed(old_ancestors),
    ]


def test_windows_descriptor_reuse_retains_stale_authority_when_cleanup_fails(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    raw_handle = 55
    descriptor = 73
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    raw_authority = {raw_handle: _authority(new_ancestors)}
    descriptor_authority = {descriptor: _authority(old_ancestors)}
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    closed_descriptors: list[int] = []
    real_close_attempt = filesystem._windows_attempt_close_raw_handle
    fail_next = True

    def fail_first_cleanup(
        handle: int,
    ) -> RawCloseOutcome:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            return RawCloseOutcome(
                RawCloseState.RETRYABLE,
                OSError("stale cleanup failed"),
            )
        return real_close_attempt(handle)

    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "descriptors",
        descriptor_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        fail_first_cleanup,
    )
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
    )
    monkeypatch.setattr(os, "close", closed_descriptors.append)

    with pytest.raises(OSError, match="stale cleanup failed"):
        filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY)

    assert raw_authority == {}
    assert descriptor_authority == {}
    assert _retired_handles(retired) == (old_ancestors,)
    assert closed_descriptors == [descriptor]
    assert windows_api.closed == [*reversed(new_ancestors)]

    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert retired == {}
    assert windows_api.closed == [
        *reversed(new_ancestors),
        *reversed(old_ancestors),
    ]


def test_windows_retired_cleanup_records_only_unproven_residual_handles(
    monkeypatch,
) -> None:
    token = object()
    retired = {token: _references((81, 82, 83))}
    close_calls: list[int] = []
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )

    def close_with_middle_failure(
        handle: int,
    ) -> RawCloseOutcome:
        close_calls.append(handle)
        if handle == 82:
            return RawCloseOutcome(
                RawCloseState.RETRYABLE,
                OSError("middle close failed"),
            )
        return RawCloseOutcome(
            RawCloseState.CLOSED
        )

    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        close_with_middle_failure,
    )

    with pytest.raises(OSError, match="middle close failed"):
        filesystem._WINDOWS_AUTHORITY.reconcile()

    assert close_calls == [83, 82, 81]
    assert _retired_handles(retired) == ((82,),)

    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        lambda handle: (
            close_calls.append(handle),
            RawCloseOutcome(
                RawCloseState.CLOSED
            ),
        )[1],
    )
    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert close_calls == [83, 82, 81, 82]
    assert retired == {}


@pytest.mark.parametrize("generation", ["raw", "descriptor"])
def test_windows_reuse_never_retries_ambiguous_retired_handle(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
    generation: str,
) -> None:
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    ambiguous: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    ambiguous_handles: dict[
        int, tuple[GenerationAuthority, ...]
    ] = {}
    events: list[str] = []
    live_generation = "retired"

    def close_then_reuse(handle) -> int:
        nonlocal live_generation
        raw = windows_api._number(handle)
        if raw != old_ancestors[0]:
            return windows_api._close(handle)
        if live_generation == "retired":
            events.append("closed-retired")
            live_generation = "unrelated-reuse"
            raise KeyboardInterrupt("after retired close")
        events.append("closed-unrelated-reuse")
        return 1

    windows_api.CloseHandle.handler = close_then_reuse
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous_retired",
        ambiguous,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous",
        ambiguous_handles,
    )

    if generation == "raw":
        leaf_handle = 55
        raw_authority = {leaf_handle: _authority(old_ancestors)}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_retain_non_reparse_ancestors",
            lambda _path: list(new_ancestors),
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_create_raw_handle",
            lambda _path, _access: leaf_handle,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_attributes",
            lambda _handle: 0,
        )
        target = tmp_path / "value.txt"
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_final_path",
            lambda _handle: target,
        )

        with pytest.raises(KeyboardInterrupt, match="after retired close"):
            filesystem._windows_open_no_follow_handle(target, directory=False)

        assert raw_authority == {}
        assert windows_api.closed == [leaf_handle, *reversed(new_ancestors)]
    else:
        raw_handle = 55
        descriptor = 73
        raw_authority = {raw_handle: _authority(new_ancestors)}
        descriptor_authority = {descriptor: _authority(old_ancestors)}
        closed_descriptors: list[int] = []
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
        )
        monkeypatch.setattr(os, "close", closed_descriptors.append)

        with pytest.raises(KeyboardInterrupt, match="after retired close"):
            filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY)

        assert raw_authority == {}
        assert descriptor_authority == {}
        assert closed_descriptors == [descriptor]
        assert windows_api.closed == [*reversed(new_ancestors)]

    assert retired == {}
    assert _retired_handles(ambiguous) == (old_ancestors,)
    assert set(ambiguous_handles) == {old_ancestors[0]}
    assert events == ["closed-retired"]

    filesystem._WINDOWS_AUTHORITY.reconcile()
    with pytest.raises(OSError, match="ambiguous; refusing to retry"):
        filesystem._windows_close_raw_handle(old_ancestors[0])

    assert events == ["closed-retired"]
    assert _retired_handles(ambiguous) == (old_ancestors,)


@pytest.mark.parametrize("owner", ["raw", "descriptor"])
def test_windows_current_close_attempts_every_ancestor_after_ambiguity(
    windows_api: _Kernel32,
    monkeypatch,
    owner: str,
) -> None:
    ancestors = (81, 82, 83)
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    ambiguous: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    ambiguous_handles: dict[
        int, tuple[GenerationAuthority, ...]
    ] = {}
    close_attempts: list[int] = []

    def close_with_ambiguity(
        handle: int,
    ) -> RawCloseOutcome:
        close_attempts.append(handle)
        if handle == 82:
            return RawCloseOutcome(
                RawCloseState.AMBIGUOUS,
                OSError("current ancestor close is ambiguous"),
            )
        return RawCloseOutcome(
            RawCloseState.CLOSED
        )

    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous_retired",
        ambiguous,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous",
        ambiguous_handles,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        close_with_ambiguity,
    )

    if owner == "raw":
        leaf_handle = 55
        authority = {leaf_handle: _authority(ancestors)}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            authority,
        )

        with pytest.raises(OSError, match="current ancestor close is ambiguous"):
            filesystem._windows_close_raw_handle(leaf_handle)

        assert close_attempts == [leaf_handle, 83, 82, 81]
    else:
        descriptor = 73
        authority = {descriptor: _authority(ancestors)}
        closed_descriptors: list[int] = []
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            authority,
        )
        monkeypatch.setattr(os, "close", closed_descriptors.append)

        with pytest.raises(OSError, match="current ancestor close is ambiguous"):
            filesystem._close_descriptor(descriptor)

        assert closed_descriptors == [descriptor]
        assert close_attempts == [83, 82, 81]

    assert authority == {}
    assert retired == {}
    assert _retired_handles(ambiguous) == ((82,),)
    assert set(ambiguous_handles) == {82}

    filesystem._WINDOWS_AUTHORITY.reconcile()
    assert close_attempts == (
        [55, 83, 82, 81] if owner == "raw" else [83, 82, 81]
    )


@pytest.mark.parametrize("owner", ["raw", "descriptor"])
def test_windows_current_close_retains_only_retryable_ancestor(
    monkeypatch,
    owner: str,
) -> None:
    ancestors = (81, 82, 83)
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    close_attempts: list[int] = []

    def close_with_retryable_failure(
        handle: int,
    ) -> RawCloseOutcome:
        close_attempts.append(handle)
        if handle == 82:
            return RawCloseOutcome(
                RawCloseState.RETRYABLE,
                OSError("current ancestor remains open"),
            )
        return RawCloseOutcome(
            RawCloseState.CLOSED
        )

    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        close_with_retryable_failure,
    )

    if owner == "raw":
        numeric_value = 55
        authority = {numeric_value: _authority(ancestors)}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            authority,
        )
        operation = lambda: filesystem._windows_close_raw_handle(numeric_value)
        expected_attempts = [numeric_value, 83, 82, 81]
    else:
        numeric_value = 73
        authority = {numeric_value: _authority(ancestors)}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            authority,
        )
        monkeypatch.setattr(os, "close", lambda _descriptor: None)
        operation = lambda: filesystem._close_descriptor(numeric_value)
        expected_attempts = [83, 82, 81]

    with pytest.raises(OSError, match="current ancestor remains open"):
        operation()

    assert authority == {}
    assert _retired_handles(retired) == ((82,),)
    assert close_attempts == expected_attempts

    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        lambda handle: (
            close_attempts.append(handle),
            RawCloseOutcome(
                RawCloseState.CLOSED
            ),
        )[1],
    )
    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert retired == {}
    assert close_attempts == [*expected_attempts, 82]


def test_windows_close_handle_batch_preserves_ambiguous_current_generation(
    monkeypatch,
) -> None:
    handles = (81, 82, 83)
    authority = {handle: _authority() for handle in handles}
    ambiguous_handles: dict[
        int, tuple[GenerationAuthority, ...]
    ] = {}
    close_attempts: list[int] = []

    def close_with_ambiguity(
        handle: int,
    ) -> RawCloseOutcome:
        close_attempts.append(handle)
        if handle == 82:
            return RawCloseOutcome(
                RawCloseState.AMBIGUOUS,
                OSError("batch close is ambiguous"),
            )
        return RawCloseOutcome(
            RawCloseState.CLOSED
        )

    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous",
        ambiguous_handles,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_attempt_close_raw_handle",
        close_with_ambiguity,
    )

    with pytest.raises(OSError, match="batch close is ambiguous"):
        filesystem._windows_close_handles(handles)

    assert close_attempts == [83, 82, 81]
    assert set(authority) == {82}
    assert authority[82].ancestors == ()
    assert set(ambiguous_handles) == {82}


def test_windows_ancestor_allocation_claims_reused_ambiguous_number(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    reused_handle = 100
    raw_authority: dict[int, GenerationAuthority] = {}
    old_generation = _authority()
    ambiguous_handles = {reused_handle: (old_generation,)}
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous",
        ambiguous_handles,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(os.path, "abspath", lambda _path: "/root/value.txt")

    retained = filesystem._windows_retain_non_reparse_ancestors(
        Path("/root/value.txt")
    )

    assert retained == [reused_handle]
    assert not filesystem._WINDOWS_AUTHORITY._generation_is_ambiguous(
        reused_handle,
        raw_authority[reused_handle],
    )
    assert raw_authority[reused_handle].ancestors == ()

    filesystem._windows_close_handles(retained)

    assert windows_api.closed == [reused_handle]
    assert raw_authority == {}
    assert retired == {}


def test_windows_leaf_allocation_claims_reused_number_before_classification(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    leaf_handle = 55
    ancestors = (91, 92)
    old_generation = _authority()
    raw_authority = {leaf_handle: old_generation}
    ambiguous_handles = {leaf_handle: (old_generation,)}
    target = tmp_path / "value.txt"
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        raw_authority,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "ambiguous",
        ambiguous_handles,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_retain_non_reparse_ancestors",
        lambda _path: list(ancestors),
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_create_raw_handle",
        lambda _path, _access: leaf_handle,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_handle_attributes",
        lambda _handle: (_ for _ in ()).throw(OSError("classification failed")),
    )

    with pytest.raises(OSError, match="classification failed"):
        filesystem._windows_open_no_follow_handle(target, directory=False)

    assert raw_authority == {}
    assert leaf_handle in ambiguous_handles
    assert windows_api.closed == [leaf_handle, *reversed(ancestors)]


@pytest.mark.parametrize("owner", ["raw", "descriptor"])
@pytest.mark.parametrize("boundary", ["post_current", "pre_retired"])
def test_windows_interrupted_generation_transition_retains_both_generations(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
    owner: str,
    boundary: str,
) -> None:
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}
    pending: dict[object, _GenerationTransition] = {}
    raw_authority: dict[int, GenerationAuthority]
    descriptor_authority: dict[int, GenerationAuthority]
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "pending",
        pending,
    )

    armed = True
    if boundary == "post_current":
        real_complete = filesystem._WINDOWS_AUTHORITY._complete_transition

        def interrupt_completion(token: object) -> None:
            nonlocal armed
            if armed:
                armed = False
                raise KeyboardInterrupt("after current generation publication")
            real_complete(token)

        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "_complete_transition",
            interrupt_completion,
        )
        expected = "after current generation publication"
    else:
        real_retained = filesystem._WINDOWS_AUTHORITY._transition_retired_references

        def interrupt_retired_publication(
            transition: _GenerationTransition,
            token: object,
        ) -> tuple[RawGenerationReference, ...]:
            nonlocal armed
            if armed:
                armed = False
                raise KeyboardInterrupt("before retired generation publication")
            return real_retained(transition, token)

        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "_transition_retired_references",
            interrupt_retired_publication,
        )
        expected = "before retired generation publication"

    if owner == "raw":
        leaf_handle = 55
        raw_authority = {leaf_handle: _authority(old_ancestors)}
        descriptor_authority = {}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_retain_non_reparse_ancestors",
            lambda _path: list(new_ancestors),
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_create_raw_handle",
            lambda _path, _access: leaf_handle,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_attributes",
            lambda _handle: 0,
        )
        target = tmp_path / "value.txt"
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_final_path",
            lambda _handle: target,
        )

        with pytest.raises(KeyboardInterrupt, match=expected):
            filesystem._windows_open_no_follow_handle(target, directory=False)

        assert windows_api.closed == [leaf_handle, *reversed(new_ancestors)]
    else:
        raw_handle = 55
        descriptor = 73
        raw_authority = {raw_handle: _authority(new_ancestors)}
        descriptor_authority = {descriptor: _authority(old_ancestors)}
        closed_descriptors: list[int] = []
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
        )
        monkeypatch.setattr(os, "close", closed_descriptors.append)

        with pytest.raises(KeyboardInterrupt, match=expected):
            filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY)

        assert closed_descriptors == [descriptor]
        assert windows_api.closed == [*reversed(new_ancestors)]

    assert pending
    assert retired == {}
    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert pending == {}
    assert retired == {}
    assert raw_authority == {}
    assert descriptor_authority == {}
    assert windows_api.closed == (
        [55, 92, 91, 81] if owner == "raw" else [92, 91, 81]
    )


@pytest.mark.parametrize("owner", ["raw", "descriptor"])
@pytest.mark.parametrize("boundary", ["pre_pending_discovery", "pending_store"])
def test_windows_failed_pre_pending_registration_retains_both_generations(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
    owner: str,
    boundary: str,
) -> None:
    old_ancestors = (81,)
    new_ancestors = (91, 92)
    retired: dict[
        object, tuple[RawGenerationReference, ...]
    ] = {}

    class FailFirstPendingStore(
        dict[object, _GenerationTransition]
    ):
        armed = True

        def __setitem__(
            self,
            key: object,
            value: _GenerationTransition,
        ) -> None:
            if self.armed:
                self.armed = False
                raise KeyboardInterrupt("before pending transition storage")
            super().__setitem__(key, value)

    pending: dict[object, _GenerationTransition]
    pending = FailFirstPendingStore() if boundary == "pending_store" else {}
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "pending",
        pending,
    )
    if boundary == "pre_pending_discovery":
        real_unique = filesystem._WINDOWS_AUTHORITY._unique_generations
        armed = True

        def interrupt_discovery(
            generations,
        ) -> tuple[GenerationAuthority, ...]:
            nonlocal armed
            if armed:
                armed = False
                raise KeyboardInterrupt("during generation discovery")
            return real_unique(generations)

        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "_unique_generations",
            interrupt_discovery,
        )
        expected = "during generation discovery"
    else:
        expected = "before pending transition storage"

    if owner == "raw":
        leaf_handle = 55
        old_generation = _authority(old_ancestors)
        raw_authority = {leaf_handle: old_generation}
        descriptor_authority: dict[
            int, GenerationAuthority
        ] = {}
        retry_token = object()
        retired[retry_token] = (
            RawGenerationReference(
                leaf_handle,
                old_generation,
            ),
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_retain_non_reparse_ancestors",
            lambda _path: list(new_ancestors),
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_create_raw_handle",
            lambda _path, _access: leaf_handle,
        )
        target = tmp_path / "value.txt"

        with pytest.raises(KeyboardInterrupt, match=expected):
            filesystem._windows_open_no_follow_handle(target, directory=False)

        assert windows_api.closed == [leaf_handle, *reversed(new_ancestors)]
    else:
        raw_handle = 55
        descriptor = 73
        raw_authority = {raw_handle: _authority(new_ancestors)}
        descriptor_authority = {descriptor: _authority(old_ancestors)}
        closed_descriptors: list[int] = []
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
        )
        monkeypatch.setattr(os, "close", closed_descriptors.append)

        with pytest.raises(KeyboardInterrupt, match=expected):
            filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY)

        assert closed_descriptors == [descriptor]
        assert windows_api.closed == [*reversed(new_ancestors)]

    assert pending == {}
    assert retired
    assert not any(
        reference.handle == 55
        for references in retired.values()
        for reference in references
    )
    if owner == "raw":
        real_attempt = filesystem._windows_attempt_close_raw_handle

        def preserve_reused_sentinel(
            handle: int,
        ) -> RawCloseOutcome:
            if handle == 55:
                raise AssertionError("reconciliation closed the reused sentinel")
            return real_attempt(handle)

        monkeypatch.setattr(
            filesystem,
            "_windows_attempt_close_raw_handle",
            preserve_reused_sentinel,
        )
    filesystem._WINDOWS_AUTHORITY.reconcile()

    assert pending == {}
    assert retired == {}
    assert raw_authority == {}
    assert descriptor_authority == {}
    assert windows_api.closed == (
        [55, 92, 91, 81] if owner == "raw" else [92, 91, 81]
    )


@pytest.mark.parametrize("owner", ["raw", "descriptor"])
def test_windows_empty_generation_reuse_preserves_new_authority(
    windows_api: _Kernel32,
    monkeypatch,
    owner: str,
) -> None:
    numeric_value = 55 if owner == "raw" else 73
    old_generation = _authority()
    new_generation = _authority()

    if owner == "raw":
        authority = {numeric_value: old_generation}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            authority,
        )

        def close_and_reuse(handle) -> int:
            windows_api.closed.append(windows_api._number(handle))
            authority[numeric_value] = new_generation
            return 1

        windows_api.CloseHandle.handler = close_and_reuse
        filesystem._windows_close_raw_handle(numeric_value)

        assert windows_api.closed == [numeric_value]
        monkeypatch.setattr(
            filesystem,
            "_windows_attempt_close_raw_handle",
            lambda _handle: RawCloseOutcome(
                RawCloseState.RETRYABLE,
                OSError("new raw generation remains open"),
            ),
        )
        expected = "new raw generation remains open"
        operation = lambda: filesystem._windows_close_raw_handle(numeric_value)
    else:
        authority = {numeric_value: old_generation}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            authority,
        )

        def close_and_reuse(_descriptor: int) -> None:
            authority[numeric_value] = new_generation

        monkeypatch.setattr(os, "close", close_and_reuse)
        filesystem._close_descriptor(numeric_value)
        monkeypatch.setattr(
            os,
            "close",
            lambda _descriptor: (_ for _ in ()).throw(
                OSError("new descriptor generation remains open")
            ),
        )
        expected = "new descriptor generation remains open"
        operation = lambda: filesystem._close_descriptor(numeric_value)

    assert authority[numeric_value] is new_generation

    with pytest.raises(OSError, match=expected):
        operation()

    assert authority[numeric_value] is new_generation


@pytest.mark.parametrize("generation", ["raw", "descriptor"])
def test_windows_reuse_post_cleanup_interruption_keeps_generations_separate(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
    generation: str,
) -> None:
    old_ancestors = (81,)
    new_ancestors = (91, 92)

    class InterruptAfterCleanup(dict[object, tuple[int, ...]]):
        armed = True

        def pop(self, key: object, default=None):
            value = super().pop(key, default)
            if self.armed:
                self.armed = False
                raise KeyboardInterrupt("after stale cleanup")
            return value

    retired = InterruptAfterCleanup()
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "retired",
        retired,
    )

    if generation == "raw":
        leaf_handle = 55
        raw_authority = {leaf_handle: _authority(old_ancestors)}
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_retain_non_reparse_ancestors",
            lambda _path: list(new_ancestors),
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_create_raw_handle",
            lambda _path, _access: leaf_handle,
        )
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_attributes",
            lambda _handle: 0,
        )
        target = tmp_path / "value.txt"
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_final_path",
            lambda _handle: target,
        )

        with pytest.raises(KeyboardInterrupt, match="after stale cleanup"):
            filesystem._windows_open_no_follow_handle(target, directory=False)

        assert raw_authority == {}
        expected_leaf_closes = [leaf_handle]
    else:
        raw_handle = 55
        descriptor = 73
        raw_authority = {raw_handle: _authority(new_ancestors)}
        descriptor_authority = {descriptor: _authority(old_ancestors)}
        closed_descriptors: list[int] = []
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "handles",
            raw_authority,
        )
        monkeypatch.setattr(
            filesystem._WINDOWS_AUTHORITY,
            "descriptors",
            descriptor_authority,
        )
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(open_osfhandle=lambda _handle, _flags: descriptor),
        )
        monkeypatch.setattr(os, "close", closed_descriptors.append)

        with pytest.raises(KeyboardInterrupt, match="after stale cleanup"):
            filesystem._windows_handle_to_descriptor(raw_handle, os.O_RDONLY)

        assert raw_authority == {}
        assert descriptor_authority == {}
        assert closed_descriptors == [descriptor]
        expected_leaf_closes = []

    assert retired == {}
    assert windows_api.closed == [
        *reversed(old_ancestors),
        *expected_leaf_closes,
        *reversed(new_ancestors),
    ]


def test_windows_raw_close_is_a_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(filesystem, "_windows_host", lambda: False)
    monkeypatch.setattr(
        filesystem,
        "_windows_kernel32",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve kernel32")),
    )

    filesystem._windows_close_raw_handle(91)


def test_windows_descriptor_close_interruption_keeps_registered_authority(
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    descriptor = 73
    ancestors = (81, 82)
    generation = _authority(ancestors)
    descriptor_authority = {descriptor: generation}
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "descriptors",
        descriptor_authority,
    )
    monkeypatch.setattr(
        os,
        "close",
        lambda _descriptor: (_ for _ in ()).throw(
            KeyboardInterrupt("descriptor close interrupted")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="descriptor close interrupted"):
        filesystem._close_descriptor(descriptor)

    assert descriptor_authority == {descriptor: generation}
    assert windows_api.closed == []


def test_windows_open_directory_and_create_failure_cleanup(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
) -> None:
    target = tmp_path / "directory"
    windows_api.attributes[str(tmp_path)] = 0x10
    windows_api.attributes[str(target)] = 0x10
    handle = filesystem._windows_open_no_follow_handle(target, directory=True)
    assert filesystem._windows_handle_entry_kind(handle) == "directory"
    filesystem._windows_close_handle(handle)

    def fail_create(_path, _access):
        raise OSError("create failed")

    monkeypatch.setattr(filesystem, "_windows_create_raw_handle", fail_create)
    with pytest.raises(OSError, match="create failed"):
        filesystem._windows_open_no_follow_handle(target, directory=False)


def test_windows_open_rejects_reparse_leaf_and_classifies_allowed_leaf(
    tmp_path: Path,
    windows_api: _Kernel32,
) -> None:
    target = tmp_path / "link"
    windows_api.attributes[str(tmp_path)] = 0x10
    windows_api.attributes[str(target)] = 0x400

    with pytest.raises(OSError, match="reparse point"):
        filesystem._windows_open_no_follow_handle(target, directory=False)

    handle = filesystem._windows_open_no_follow_handle(
        target,
        directory=False,
        allow_reparse_leaf=True,
    )
    try:
        assert filesystem._windows_handle_entry_kind(handle) == "symlink"
    finally:
        filesystem._windows_close_handle(handle)


def test_windows_open_rejects_reparse_ancestor_and_final_path_redirection(
    tmp_path: Path,
    windows_api: _Kernel32,
) -> None:
    ancestor = tmp_path / "ancestor"
    target = ancestor / "value.txt"
    windows_api.attributes[str(tmp_path)] = 0x10
    windows_api.attributes[str(ancestor)] = 0x400
    with pytest.raises(OSError, match="contains a Windows reparse point"):
        filesystem._windows_open_no_follow_handle(target, directory=False)

    windows_api.attributes[str(ancestor)] = 0x10
    windows_api.attributes[str(target)] = 0
    next_leaf_handle = windows_api.next_handle + len(Path(target).parts) - 2
    windows_api.final_paths[next_leaf_handle] = str(tmp_path / "redirected.txt")
    with pytest.raises(OSError, match="contains a Windows reparse point"):
        filesystem._windows_open_no_follow_handle(target, directory=False)


def test_windows_handle_atime_update_preserves_mtime_and_readonly_state(
    windows_api: _Kernel32,
) -> None:
    windows_api.paths[7] = "value.txt"
    windows_api.attributes["value.txt"] = 0x1
    windows_api.after_basic_read = lambda: windows_api.attributes.__setitem__(
        "value.txt", 0
    )

    filesystem._windows_apply_handle_metadata_values(
        7,
        mode=None,
        regular=True,
        atime_ns=3_000,
        mtime_ns=None,
    )
    atime_only = windows_api.basic_updates[-1]
    assert atime_only.LastAccessTime == 116444736000000030
    assert atime_only.LastWriteTime == 0
    assert atime_only.FileAttributes == 0
    assert windows_api.attributes["value.txt"] == 0

    windows_api.after_basic_read = None
    filesystem._windows_apply_handle_metadata_values(
        7,
        mode=0o444,
        regular=True,
        atime_ns=None,
        mtime_ns=4_000,
    )
    mtime_only = windows_api.basic_updates[-1]
    assert mtime_only.LastAccessTime == 0
    assert mtime_only.LastWriteTime == 116444736000000040
    assert mtime_only.FileAttributes & 0x1

    windows_api.fail_set = True
    with pytest.raises(OSError, match="fake Windows error"):
        filesystem._windows_apply_handle_metadata_values(
            7,
            mode=0o444,
            regular=False,
            atime_ns=1,
            mtime_ns=2,
        )


def test_windows_handle_close_failure_is_explicit(windows_api: _Kernel32) -> None:
    windows_api.fail_close = True
    with pytest.raises(OSError, match="fake Windows error"):
        filesystem._windows_close_raw_handle(9)


def test_windows_reparse_target_is_decoded_from_retained_handle(
    windows_api: _Kernel32,
) -> None:
    substitute = "\\??\\owned-target".encode("utf-16-le")
    header = (
        (0xA000000C).to_bytes(4, "little")
        + (12 + len(substitute)).to_bytes(2, "little")
        + b"\0\0"
        + (0).to_bytes(2, "little")
        + len(substitute).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + b"\0\0\0\0"
    )
    windows_api.reparse_payload = header + substitute

    assert filesystem._windows_read_symlink_target(9) == "owned-target"

    windows_api.reparse_payload = b"invalid"
    with pytest.raises(OSError, match="not a symbolic link"):
        filesystem._windows_read_symlink_target(9)

    windows_api.fail_device = True
    with pytest.raises(OSError, match="fake Windows error"):
        filesystem._windows_read_symlink_target(9)


def test_windows_directory_enumeration_decodes_retained_page(
    windows_api: _Kernel32,
) -> None:
    class Row(ctypes.Structure):
        _fields_ = (
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_ubyte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        )

    name = "value.txt".encode("utf-16-le")
    page = ctypes.create_string_buffer(Row.FileName.offset + len(name))
    row = Row.from_buffer(page)
    row.NextEntryOffset = 0
    row.LastAccessTime = 116444736000000001
    row.LastWriteTime = 116444736000000002
    row.EndOfFile = 6
    row.FileAttributes = 0
    row.FileNameLength = len(name)
    row.FileId = 19
    ctypes.memmove(ctypes.addressof(page) + Row.FileName.offset, name, len(name))
    windows_api.directory_page = page.raw

    entries = filesystem._windows_list_directory_entries(
        7,
        Path("root"),
        SimpleNamespace(st_dev=5),
    )

    assert [(entry.name, entry.kind, entry.metadata.st_size) for entry in entries] == [
        ("value.txt", "file", 6)
    ]

    windows_api.directory_page = None
    windows_api.directory_sent = False
    assert filesystem._windows_list_directory_entries(
        7,
        Path("root"),
        SimpleNamespace(st_dev=5),
    ) == ()


def test_windows_observed_directory_uses_retained_handle_and_atime_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    before = directory.lstat()
    restored: list[int] = []
    requested_metadata_access: list[bool] = []
    closed: list[int] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)

    def open_read_only(*_args, **kwargs):
        requested_metadata_access.append(kwargs.get("write_metadata", False))
        return 77

    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        open_read_only,
    )

    def enumerate_and_advance_atime(_handle, _path, _metadata):
        os.utime(directory, ns=(before.st_atime_ns + 1_000_000_000, before.st_mtime_ns))
        return ()

    def restore_atime(path, metadata):
        assert path == directory
        restored.append(metadata.st_atime_ns)
        os.utime(
            directory,
            ns=(metadata.st_atime_ns, directory.lstat().st_mtime_ns),
        )
        return True

    import os

    monkeypatch.setattr(
        filesystem,
        "_windows_list_directory_entries",
        enumerate_and_advance_atime,
    )
    monkeypatch.setattr(
        filesystem,
        "_restore_observed_path_atime",
        restore_atime,
    )
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)

    children, observed = filesystem._observed_directory_entries(directory)

    assert children == []
    assert observed.st_ino == before.st_ino
    assert restored == [before.st_atime_ns]
    assert requested_metadata_access == [False]
    assert closed == [77]


def test_windows_observed_directory_rejects_same_handle_mtime_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    before = directory.lstat()
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 78,
    )

    def mutate_mtime(_handle, _path, _metadata):
        os.utime(
            directory,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
        return ()

    import os

    monkeypatch.setattr(filesystem, "_windows_list_directory_entries", mutate_mtime)
    monkeypatch.setattr(filesystem, "_windows_close_handle", lambda _handle: None)

    with pytest.raises(filesystem.ConcurrentSourceMutation, match="changed while"):
        filesystem._observed_directory_entries(directory)


def test_windows_contained_classifier_uses_retained_leaf_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    leaf = root / "leaf"
    closed: list[int] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 91,
    )
    monkeypatch.setattr(filesystem, "_windows_handle_entry_kind", lambda _handle: "file")
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)

    assert filesystem.contained_entry_kind_no_follow(root, leaf) == "file"
    assert closed == [91]

    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert filesystem.contained_entry_kind_no_follow(root, leaf) is None

    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsafe ancestor")),
    )
    with pytest.raises(filesystem.FilesystemError, match="Cannot inspect"):
        filesystem.contained_entry_kind_no_follow(root, leaf)


def test_windows_path_normalization_handles_device_and_unc_spellings(
    monkeypatch,
) -> None:
    monkeypatch.setattr(filesystem.os.path, "abspath", lambda value: value)
    monkeypatch.setattr(filesystem.os.path, "normcase", lambda value: value.lower())
    monkeypatch.setattr(filesystem.os.path, "normpath", lambda value: value)

    assert filesystem._windows_api_path(Path("C:\\root\\file")) == "\\\\?\\C:\\root\\file"
    assert filesystem._windows_api_path(Path("\\\\?\\C:\\root\\file")) == (
        "\\\\?\\C:\\root\\file"
    )
    assert filesystem._windows_api_path(Path("\\\\server\\share")) == (
        "\\\\?\\UNC\\server\\share"
    )
    assert filesystem._windows_path_key(Path("\\\\?\\C:\\ROOT\\file")) == (
        "c:\\root\\file"
    )


def test_windows_regular_observer_adapter_uses_transferred_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    raw = os.open(source, os.O_RDONLY)
    closed: list[int] = []
    requested_metadata_access: list[bool] = []

    def open_retained(*_args, **kwargs) -> int:
        requested_metadata_access.append(kwargs.get("write_metadata", False))
        return raw

    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        open_retained,
    )
    monkeypatch.setattr(filesystem, "_windows_handle_final_path", lambda _handle: source)
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            open_osfhandle=lambda handle, _flags: os.dup(handle),
            get_osfhandle=lambda descriptor: descriptor,
        ),
    )
    try:
        descriptor, observed = filesystem._open_observed(source)
        try:
            assert os.read(descriptor, 6) == b"value\n"
            assert observed.st_ino == source.lstat().st_ino
        finally:
            os.close(descriptor)
        with pytest.raises(OSError, match="cannot be opened safely"):
            filesystem._open_observed(tmp_path, directory=True)
    finally:
        os.close(raw)

    assert closed == []
    assert requested_metadata_access == [True]


def test_windows_regular_observer_rejects_redirect_and_wrong_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("inside\n", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("outside\n", encoding="utf-8")
    other_fd = os.open(other, os.O_RDONLY)
    closed: list[int] = []
    authority: dict[int, GenerationAuthority] = {}

    def open_handle(handle: int):
        def open_retained(*_args, **_kwargs):
            authority[handle] = _authority()
            return handle

        return open_retained

    def close_handle(handle: int) -> None:
        authority.pop(handle, None)
        closed.append(handle)

    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem._WINDOWS_AUTHORITY,
        "handles",
        authority,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        open_handle(other_fd),
    )
    monkeypatch.setattr(filesystem, "_windows_handle_final_path", lambda _handle: other)
    monkeypatch.setattr(filesystem, "_windows_close_handle", close_handle)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda handle, _flags: os.dup(handle)),
    )
    try:
        with pytest.raises(filesystem.ConcurrentSourceMutation, match="changed while"):
            filesystem._open_observed(source)
    finally:
        os.close(other_fd)

    link = tmp_path / "link"
    link.symlink_to("target")
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        open_handle(88),
    )
    monkeypatch.setattr(filesystem, "_windows_handle_final_path", lambda _handle: link)
    with pytest.raises(OSError, match="reparse point"):
        filesystem._open_observed(link)
    assert closed == [other_fd, 88]


@pytest.mark.parametrize("failure_boundary", ["final_path", "initial_lstat"])
def test_windows_regular_observer_closes_raw_authority_before_transfer_failure(
    tmp_path: Path,
    windows_api: _Kernel32,
    monkeypatch,
    failure_boundary: str,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    leaf_handle = 91
    ancestors = (81, 82)
    monkeypatch.setitem(
        filesystem._WINDOWS_AUTHORITY.handles,
        leaf_handle,
        _authority(ancestors),
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: leaf_handle,
    )
    if failure_boundary == "final_path":
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_final_path",
            lambda _handle: (_ for _ in ()).throw(OSError("final path failed")),
        )
        expected = "final path failed"
    else:
        monkeypatch.setattr(
            filesystem,
            "_windows_handle_final_path",
            lambda _handle: source,
        )
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda _path: (_ for _ in ()).throw(OSError("lstat failed")),
        )
        expected = "lstat failed"

    with pytest.raises(OSError, match=expected):
        filesystem._open_observed(source)

    assert windows_api.closed == [leaf_handle, *reversed(ancestors)]
    assert leaf_handle not in filesystem._WINDOWS_AUTHORITY.handles


def test_windows_descriptor_metadata_adapters_do_not_require_pathnames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    descriptor = os.open(source, os.O_RDWR)
    calls: list[tuple[int | None, int | None]] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(get_osfhandle=lambda value: value),
    )

    def apply_values(_handle, **values):
        calls.append((values["atime_ns"], values["mtime_ns"]))

    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata_values",
        apply_values,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata",
        lambda handle, metadata: calls.append(
            (metadata.st_atime_ns, metadata.st_mtime_ns)
        ),
    )
    try:
        metadata = os.fstat(descriptor)
        filesystem._apply_descriptor_atime_only(descriptor, metadata.st_atime_ns)
        filesystem._apply_descriptor_metadata(descriptor, metadata)
    finally:
        os.close(descriptor)

    assert calls == [
        (metadata.st_atime_ns, None),
        (metadata.st_atime_ns, metadata.st_mtime_ns),
    ]


def test_windows_finish_observed_atime_updates_only_retained_atime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    created = source.lstat()
    os.utime(source, ns=(1_000_000_000, created.st_mtime_ns))
    descriptor = os.open(source, os.O_RDONLY)
    before = os.fstat(descriptor)
    os.read(descriptor, 6)
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(get_osfhandle=lambda value: value),
    )

    def restore_atime(handle, **values):
        current = os.fstat(handle)
        os.utime(handle, ns=(values["atime_ns"], current.st_mtime_ns))

    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata_values",
        restore_atime,
    )
    try:
        assert filesystem._finish_observed_atime(source, descriptor, before)
    finally:
        os.close(descriptor)
    assert source.lstat().st_atime_ns == before.st_atime_ns


def test_windows_directory_atime_restore_fails_before_unsafe_handle_use(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    before = directory.lstat()
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsafe")),
    )

    assert not filesystem._restore_observed_path_atime(directory, before)

    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 95,
    )
    monkeypatch.setattr(filesystem, "_windows_close_handle", lambda _handle: None)
    os.utime(
        directory,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )
    assert not filesystem._restore_observed_path_atime(directory, before)


def test_windows_directory_and_symlink_metadata_authorities_are_handle_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    baseline = directory.lstat()
    os.utime(
        directory,
        ns=(baseline.st_atime_ns + 1_000_000_000, baseline.st_mtime_ns),
    )
    closed: list[int] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 93,
    )
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)

    def restore_directory_atime(_handle, **values):
        os.utime(directory, ns=(values["atime_ns"], directory.lstat().st_mtime_ns))

    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata_values",
        restore_directory_atime,
    )
    assert filesystem._restore_observed_path_atime(directory, baseline)

    link = tmp_path / "link"
    link.symlink_to("target")
    link_metadata = link.lstat()
    assert filesystem._open_symlink_metadata_authority(
        link,
        link_metadata,
    ) == ("windows", 93)
    assert filesystem._symlink_is_directory(
        link,
        SimpleNamespace(st_file_attributes=0x10),
    )
    assert closed == [93]


def test_windows_symlink_directory_classification_fails_closed_without_attributes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    link = tmp_path / "link"
    link.symlink_to("target")
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)

    assert not filesystem._symlink_is_directory(
        link,
        SimpleNamespace(st_file_attributes=0),
    )
    with pytest.raises(filesystem.SymlinkPreservationError, match="could not determine"):
        filesystem._symlink_is_directory(link)


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [(0x400, "symlink"), (0x10, "directory"), (0, "file")],
)
def test_windows_directory_entry_type_and_metadata(
    attributes: int,
    expected: str,
) -> None:
    row = SimpleNamespace(
        FileAttributes=attributes,
        FileId=17,
        EndOfFile=23,
        LastAccessTime=116444736000000001,
        LastWriteTime=116444736000000002,
    )
    parent = SimpleNamespace(st_dev=5)

    observed = filesystem._windows_observed_directory_entry(
        "entry",
        row,
        Path("root"),
        parent,
    )

    assert observed.kind == expected
    assert observed.metadata.st_dev == 5
    assert observed.metadata.st_ino == 17
    assert observed.metadata.st_size == 23
