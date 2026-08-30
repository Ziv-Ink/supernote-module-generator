#!/usr/bin/env python3
"""Qualify the official template's cross-platform launch-result contract."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Sequence

from supernote_module_generator.template_contract import (
    TEMPLATE_SCRIPT_PATHS,
    UNVERIFIED_LAUNCH,
    synchronized_script,
)


UNVERIFIED = UNVERIFIED_LAUNCH


def verify_template(template_root: Path) -> None:
    package = json.loads((template_root / "package.json").read_text(encoding="utf-8"))
    if "scripts/runPlugin.sh" not in package["scripts"]["run"]:
        raise ValueError("npm run run does not route POSIX hosts to runPlugin.sh")
    if "scripts/runPlugin.ps1" not in package["scripts"]["run"]:
        raise ValueError("npm run run does not route Windows to runPlugin.ps1")
    for name in ("runPlugin.sh", "runPlugin.ps1"):
        source = (template_root / "scripts" / name).read_text(encoding="utf-8")
        if UNVERIFIED not in source:
            raise ValueError(f"{name} does not publish the unverified launch outcome")
        if "assuming success after the tap" in source:
            raise ValueError(f"{name} still claims tap-only success")
        if name.endswith(".ps1") and source.count(
            "$nodes = @(Get-NodesMatching $Attribute $Value)"
        ) != 2:
            raise ValueError("runPlugin.ps1 does not preserve one-node arrays")


def sync_template(template_root: Path) -> None:
    """Apply the packaged capability to one disposable template checkout."""

    for relative in TEMPLATE_SCRIPT_PATHS:
        path = template_root / relative
        source = path.read_bytes()
        updated = synchronized_script(relative, source)
        if updated != source:
            path.write_bytes(updated)
    verify_template(template_root)


def prepare_stub(output: Path, plugin_root: Path) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    stub = output / "adb_stub.py"
    stub.write_text(
        """from __future__ import annotations
import sys

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-s":
    args = args[2:]
if args == ["get-state"]:
    print("device")
elif args[:3] == ["shell", "dumpsys", "window"]:
    print("mCurrentFocus=Window{test com.ratta.supernote.note/.view.NoteInsidePagesActivity}")
elif args[:3] == ["shell", "uiautomator", "dump"]:
    print("UI hierarchy dumped to: /sdcard/supernote-deploy-window.xml")
elif args[:2] == ["exec-out", "cat"]:
    print('<hierarchy><node content-desc="plugins" bounds="[0,0][10,10]" />'
          '<node text="HelloWorld" bounds="[10,10][20,20]" /></hierarchy>')
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    posix = output / "adb"
    posix.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n',
        encoding="utf-8",
    )
    posix.chmod(posix.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    windows = output / "adb.cmd"
    windows.write_text(
        f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n',
        encoding="utf-8",
    )
    (plugin_root / "PluginConfig.json").write_text(
        json.dumps({"name": "HelloWorld"}, indent=2) + "\n",
        encoding="utf-8",
    )
    return windows if os.name == "nt" else posix


def verify_output(log: Path) -> None:
    output = log.read_text(encoding="utf-8")
    if UNVERIFIED not in output:
        raise ValueError("npm run run did not emit the explicit unverified outcome")
    if "assuming success" in output:
        raise ValueError("npm run run emitted the forbidden tap-only success claim")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("template", type=Path)
    sync = subparsers.add_parser("sync")
    sync.add_argument("template", type=Path)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("output", type=Path)
    prepare.add_argument("plugin_root", type=Path)
    output = subparsers.add_parser("output")
    output.add_argument("log", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.action == "verify":
        verify_template(arguments.template)
    elif arguments.action == "sync":
        sync_template(arguments.template)
    elif arguments.action == "prepare":
        print(prepare_stub(arguments.output, arguments.plugin_root))
    else:
        verify_output(arguments.log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
