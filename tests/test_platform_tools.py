from __future__ import annotations

from pathlib import Path

from supernote_module_generator.platform_tools import (
    gradle_wrapper_command,
    gradle_wrapper_path,
    ndk_compiler_path,
)


def test_gradle_wrapper_uses_windows_batch_file_without_posix_shell(tmp_path: Path):
    wrapper = gradle_wrapper_path(tmp_path, platform_name="nt")

    assert wrapper == tmp_path / "android/gradlew.bat"
    assert gradle_wrapper_command(
        wrapper,
        ["--version"],
        platform_name="nt",
    ) == [str(wrapper), "--version"]


def test_gradle_wrapper_uses_sh_for_non_executable_posix_script(tmp_path: Path):
    wrapper = gradle_wrapper_path(tmp_path, platform_name="posix")
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

    assert gradle_wrapper_command(
        wrapper,
        [":app:assembleDebug"],
        platform_name="posix",
    ) == ["sh", str(wrapper), ":app:assembleDebug"]


def test_ndk_compiler_resolution_uses_windows_executable_suffix(tmp_path: Path):
    prebuilt = tmp_path / "prebuilt"
    compiler = prebuilt / "windows-x86_64/bin"
    compiler.mkdir(parents=True)
    clang = compiler / "clang.exe"
    clangxx = compiler / "clang++.exe"
    clang.write_bytes(b"")
    clangxx.write_bytes(b"")

    assert ndk_compiler_path(
        prebuilt,
        "clang",
        platform_name="nt",
    ) == clang
    assert ndk_compiler_path(
        prebuilt,
        "clang++",
        platform_name="nt",
    ) == clangxx
    assert ndk_compiler_path(
        prebuilt,
        "clang",
        platform_name="posix",
    ) is None
