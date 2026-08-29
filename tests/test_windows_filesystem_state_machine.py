from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import supernote_module_generator.filesystem as filesystem


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
        self.attributes: dict[str, int] = {}
        self.closed: list[int] = []
        self.basic_updates: list[SimpleNamespace] = []
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

    def _create_file(self, path, *_args):
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

    def _set_info(self, _handle, _info_class, pointer, _size):
        if self.fail_set:
            self.last_error = 5
            return 0
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
    restored: list[tuple[int, int | None]] = []
    closed: list[int] = []
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: 77,
    )

    def enumerate_and_advance_atime(_handle, _path, _metadata):
        os.utime(directory, ns=(before.st_atime_ns + 1_000_000_000, before.st_mtime_ns))
        return ()

    def restore_atime(_handle, **values):
        restored.append((values["atime_ns"], values["mtime_ns"]))
        os.utime(directory, ns=(values["atime_ns"], directory.lstat().st_mtime_ns))

    import os

    monkeypatch.setattr(
        filesystem,
        "_windows_list_directory_entries",
        enumerate_and_advance_atime,
    )
    monkeypatch.setattr(
        filesystem,
        "_windows_apply_handle_metadata_values",
        restore_atime,
    )
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)

    children, observed = filesystem._observed_directory_entries(directory)

    assert children == []
    assert observed.st_ino == before.st_ino
    assert restored == [(before.st_atime_ns, None)]
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
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: raw,
    )
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
    monkeypatch.setattr(filesystem, "_windows_host", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "_windows_open_no_follow_handle",
        lambda *_args, **_kwargs: other_fd,
    )
    monkeypatch.setattr(filesystem, "_windows_close_handle", closed.append)
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
        lambda *_args, **_kwargs: 88,
    )
    with pytest.raises(OSError, match="reparse point"):
        filesystem._open_observed(link)
    assert closed == [88]


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
