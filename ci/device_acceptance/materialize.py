#!/usr/bin/env python3
"""Materialize one bounded NOTE or DOC device fixture in a disposable clone."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Sequence


PINNED_REVISION = "9f626ed39be82b43ff74eb735d10b7de61f51508"
FEATURE = "device-probe"
SN_PLUGIN_LIB_VERSION = "0.1.65"


def _run(root: Path, command: Sequence[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        check=False,
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _generator(root: Path, executable: str, *arguments: str) -> dict[str, object]:
    output = _run(root, (executable, "--json", *arguments), capture=True)
    result = json.loads(output)
    if result.get("status") != "success":
        raise RuntimeError(f"generator command failed: {result}")
    return result


def _identity(host: str, suffix: str) -> tuple[str, str, str]:
    key = f"snmg-{host}-acceptance-{suffix.lower()}"
    label = key
    plugin_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return label, key, plugin_id


def _write_sources(root: Path, source_root: Path) -> None:
    feature = root / "local_modules" / FEATURE
    native = feature / "android/src/main/cpp"
    jvm = feature / "android/src/main/java/com/example/device_probe"
    native.mkdir(parents=True, exist_ok=True)
    jvm.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_root / "device_probe.cpp", native / "feature.cpp")
    shutil.copyfile(source_root / "DeviceCounter.hpp", native / "DeviceCounter.hpp")
    shutil.copyfile(source_root / "FeatureApi.kt", jvm / "FeatureApi.kt")


def materialize(
    root: Path,
    generator: str,
    host: str,
    suffix: str,
    source_root: Path,
) -> dict[str, object]:
    if host not in {"note", "doc"}:
        raise ValueError("host must be note or doc")
    revision = _run(root, ("git", "rev-parse", "HEAD"), capture=True).strip()
    if revision != PINNED_REVISION:
        raise RuntimeError(f"file_reader_test revision is not pinned: {revision}")

    label, key, plugin_id = _identity(host, suffix)
    react_component = label
    permission = (
        "plugin.permission.FILE:WRITE"
        if host == "note"
        else "plugin.permission.FILE:READ"
    )
    permission_action = "deny" if host == "note" else "allow_once"

    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["name"] = label
    package["dependencies"]["sn-plugin-lib"] = SN_PLUGIN_LIB_VERSION
    package_path.write_text(
        json.dumps(package, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (root / ".supernote-launch-label").write_text(label + "\n", encoding="utf-8")
    strings_path = root / "android/app/src/main/res/values/strings.xml"
    strings = strings_path.read_text(encoding="utf-8")
    strings = strings.replace(
        "<string name=\"app_name\">file_reader_test</string>",
        f"<string name=\"app_name\">{label}</string>",
    )
    if f"<string name=\"app_name\">{label}</string>" not in strings:
        raise RuntimeError("file_reader_test Android launch label was not canonical")
    strings_path.write_text(strings, encoding="utf-8")
    app_path = root / "app.json"
    app = json.loads(app_path.read_text(encoding="utf-8"))
    if app != {"name": "file_reader_test", "displayName": "file_reader_test"}:
        raise RuntimeError("file_reader_test React Native identity was not canonical")
    app_path.write_text(
        json.dumps(
            {"name": react_component, "displayName": label},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    activity_path = root / "android/app/src/main/java/com/file_reader_test/MainActivity.kt"
    activity = activity_path.read_text(encoding="utf-8")
    activity = activity.replace(
        'getMainComponentName(): String = "file_reader_test"',
        f'getMainComponentName(): String = "{react_component}"',
    )
    if f'getMainComponentName(): String = "{react_component}"' not in activity:
        raise RuntimeError("file_reader_test MainActivity identity was not canonical")
    activity_path.write_text(activity, encoding="utf-8")
    index_path = root / "index.js"
    index = index_path.read_text(encoding="utf-8")
    index = index.replace("name: 'file_reader_test'", f"name: '{label}'")
    if f"name: '{label}'" not in index:
        raise RuntimeError("file_reader_test plugin button identity was not canonical")
    index_path.write_text(index, encoding="utf-8")
    (root / "PluginConfig.json").write_text(
        json.dumps(
            {
                "name": label,
                "desc": "Bounded public NOTE/DOC final acceptance fixture",
                "iconPath": "",
                "versionName": "1.0.0",
                "versionCode": "1",
                "pluginID": plugin_id,
                "pluginKey": key,
                "jsMainPath": "index",
                "uses-permissions": [permission],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    application = (source_root / "App.tsx.tmpl").read_text(encoding="utf-8")
    application = (
        application.replace("__HOST__", host)
        .replace("__PLUGIN_NAME__", label)
        .replace("__PERMISSION__", permission)
        .replace("__PERMISSION_ACTION__", permission_action)
    )
    (root / "App.tsx").write_text(application, encoding="utf-8")

    _generator(
        root,
        generator,
        "add",
        FEATURE,
        "--starter",
        "cpp",
        "--starter",
        "kotlin",
        "--skip-install",
        "--yes",
    )
    _write_sources(root, source_root)
    _generator(root, generator, "update", FEATURE, "--skip-install", "--yes")
    _generator(root, generator, "template", "sync", "--yes")
    _generator(root, generator, "template", "status")

    return {
        "schema_version": 1,
        "host": host,
        "plugin_name": label,
        "plugin_key": key,
        "plugin_id": plugin_id,
        "launch_label": label,
        "react_component": react_component,
        "permission": permission,
        "permission_action": permission_action,
        "pinned_revision": revision,
        "pinned_sn_plugin_lib": "0.1.63",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("generator_command")
    parser.add_argument("host", choices=("note", "doc"))
    parser.add_argument("--identity-suffix", default="FinalA")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parent
    result = materialize(
        arguments.project.resolve(),
        arguments.generator_command,
        arguments.host,
        arguments.identity_suffix,
        source_root,
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
