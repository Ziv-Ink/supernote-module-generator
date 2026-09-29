from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import pytest

from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.integrity_manifest import INCOMPLETE_MARKER_PATH
from supernote_module_generator.validation import GeneratedProjectValidator


def _plugin(root: Path) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (root / "android/settings.gradle").write_text(
        "include ':app'\n", encoding="utf-8"
    )
    (root / "android/app/build.gradle").write_text(
        "plugins {}\n", encoding="utf-8"
    )
    (root / "package.json").write_text(
        '{"name":"q2-native-fixture","dependencies":{}}\n',
        encoding="utf-8",
    )
    return root


def _add(root: Path) -> Path:
    return FeatureOperationService(root).add(
        FeatureConfig(
            output=root / "local_modules/alpha",
            npm_name="alpha",
            package_version="1.0.0",
            android_namespace="com.example.alpha",
            public_name="Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )


def _plan(root: Path):
    return GenerationService(root).plan(
        operation="update",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )


def _update(root: Path) -> None:
    service = GenerationService(root)
    service.execute(
        service.plan(
            operation="update",
            requested_targets=tuple(
                record.manifest.npm_name
                for record in FeatureOperationService(root).records()
            ),
            allow_unmanifested_bootstrap=True,
        )
    )


def _authored_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    generated_parts = {".supernote-generated", ".supernote-module"}
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if generated_parts.intersection(relative.parts):
            continue
        metadata = path.stat()
        result[relative.as_posix()] = (
            path.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
        )
    return result


def test_read_only_authored_source_is_not_modified(tmp_path: Path) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    source = feature / "android/src/main/cpp/feature.cpp"
    _update(root)
    before = _authored_snapshot(root)
    acl_applied = False
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        if not username:
            pytest.skip("Windows username is unavailable for the ACL control")
        result = subprocess.run(
            [
                "icacls",
                str(source),
                "/inheritance:r",
                "/grant:r",
                f"{username}:(R)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            pytest.skip(f"Windows read-only ACL setup unavailable: {result.stderr}")
        acl_applied = True
    else:
        source.chmod(0o444)
        metadata = source.stat()
        before[source.relative_to(root).as_posix()] = (
            source.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
        )
    try:
        _update(root)
        assert GeneratedProjectValidator(root).validate().status == "success"
        assert _authored_snapshot(root) == before
    finally:
        if acl_applied:
            subprocess.run(
                ["icacls", str(source), "/reset"],
                capture_output=True,
                text=True,
                check=False,
            )


def test_same_size_same_mtime_author_edit_before_publication_is_rejected(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    _update(root)
    source = feature / "android/src/main/cpp/feature.cpp"
    service = GenerationService(root)
    plan = _plan(root)
    before = source.stat()
    content = source.read_bytes()
    replacement = bytes([content[0] ^ 1]) + content[1:]
    source.write_bytes(replacement)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(Exception, match="authored project state changed"):
        service.execute(plan)

    assert source.read_bytes() == replacement
    assert not (root / INCOMPLETE_MARKER_PATH).exists()


def test_generated_symlink_redirection_preserves_outside_canary(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Windows reparse redirection has a dedicated junction test")
    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    _update(root)
    outside = tmp_path / "outside.js"
    outside.write_text("outside canary\n", encoding="utf-8")
    generated = feature / ".supernote-generated/index.js"
    generated.unlink()
    generated.symlink_to(outside)

    with pytest.raises(Exception):
        _update(root)

    assert outside.read_text(encoding="utf-8") == "outside canary\n"


def test_generated_hardlink_is_replaced_without_mutating_peer(tmp_path: Path) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    _update(root)
    generated = feature / ".supernote-generated/index.js"
    expected = generated.read_bytes()
    peer = tmp_path / "hardlink-peer.js"
    peer.write_text("outside hardlink canary\n", encoding="utf-8")
    generated.unlink()
    os.link(peer, generated)

    _update(root)

    assert peer.read_text(encoding="utf-8") == "outside hardlink canary\n"
    assert generated.read_bytes() == expected
    assert os.stat(peer).st_ino != os.stat(generated).st_ino


@pytest.mark.parametrize("boundary", (0, 1, 2, -2, -1))
def test_process_death_during_publication_is_rerunnable(
    tmp_path: Path,
    boundary: int,
) -> None:
    root = _plugin(tmp_path / "plugin")
    _add(root)
    _update(root)
    authored = _authored_snapshot(root)
    plan = _plan(root)
    total_publications = 1 + len(plan.artifacts)
    selected = total_publications + 1 + boundary if boundary < 0 else boundary
    script = r'''
import os
from pathlib import Path
import sys
import supernote_module_generator.generation_execution as execution
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService

root = Path(sys.argv[1])
boundary = int(sys.argv[2])
if boundary == 0:
    def stop_before_marker(*_args, **_kwargs):
        os._exit(73)
    execution.GenerationPlanExecutor._publish_incomplete_marker = stop_before_marker
else:
    original = execution.replace_generated_regular_no_follow
    calls = 0
    def stop_after_publication(*args, **kwargs):
        global calls
        original(*args, **kwargs)
        calls += 1
        if calls == boundary:
            os._exit(73)
    execution.replace_generated_regular_no_follow = stop_after_publication
records = FeatureOperationService(root).records()
service = GenerationService(root)
plan = service.plan(
    operation="update",
    requested_targets=tuple(item.manifest.npm_name for item in records),
    allow_unmanifested_bootstrap=True,
)
service.execute(plan)
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(selected)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 73, result.stderr
    assert time.monotonic() - started < 20
    assert _authored_snapshot(root) == authored
    if selected > 0:
        assert (root / INCOMPLETE_MARKER_PATH).is_file()
        assert GeneratedProjectValidator(root).validate().status == "failure"
    _update(root)
    assert _authored_snapshot(root) == authored
    assert not (root / INCOMPLETE_MARKER_PATH).exists()
    assert GeneratedProjectValidator(root).validate().status == "success"


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction boundary")
def test_windows_junction_destination_is_rejected_before_external_write(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "plugin")
    outside = tmp_path / "outside"
    outside.mkdir()
    local_modules = root / "local_modules"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(local_modules), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr}")

    with pytest.raises(Exception):
        _add(root)

    assert not (outside / "alpha").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing boundary")
def test_windows_locked_generated_output_fails_bounded_and_reruns(
    tmp_path: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    _update(root)
    source_before = _authored_snapshot(root)
    generated = feature / ".supernote-generated/index.js"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(generated),
        0x80000000,
        0x1,
        None,
        3,
        0x80,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    started = time.monotonic()
    try:
        with pytest.raises(Exception):
            _update(root)
    finally:
        kernel32.CloseHandle(handle)
    assert time.monotonic() - started < 10
    assert _authored_snapshot(root) == source_before
    assert (root / INCOMPLETE_MARKER_PATH).is_file()

    _update(root)
    assert GeneratedProjectValidator(root).validate().status == "success"


def test_zero_feature_obsolete_cleanup_is_rerunnable_after_process_death(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "plugin")
    feature = _add(root)
    _update(root)
    shutil.rmtree(feature)
    authored = _authored_snapshot(root)
    script = r'''
import os
from pathlib import Path
import sys
import supernote_module_generator.generation_execution as execution
from supernote_module_generator.generation_service import GenerationService

root = Path(sys.argv[1])
original = execution.GenerationPlanExecutor._delete_stale_output
def stop_after_cleanup(self, plan):
    original(self, plan)
    os._exit(74)
execution.GenerationPlanExecutor._delete_stale_output = stop_after_cleanup
service = GenerationService(root)
service.execute(service.plan(operation="update", requested_targets=()))
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 74, result.stderr
    assert _authored_snapshot(root) == authored
    assert (root / INCOMPLETE_MARKER_PATH).is_file()
    _update(root)
    assert _authored_snapshot(root) == authored
    assert GeneratedProjectValidator(root).validate().status == "success"
