#!/usr/bin/env python3
"""Create one bounded NOTE or DOC fixture from the canonical template."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Sequence


TEMPLATE_ORIGIN = "https://github.com/Ziv-Ink/supernote-plugin-template.git"
FEATURE = "device-probe"


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


def _template_identity(template_root: Path) -> tuple[str, str, str]:
    origin = _run(template_root, ("git", "remote", "get-url", "origin"), capture=True).strip()
    revision = _run(template_root, ("git", "rev-parse", "HEAD"), capture=True).strip()
    branch = _run(
        template_root,
        ("git", "branch", "--show-current"),
        capture=True,
    ).strip()
    status = _run(
        template_root,
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        capture=True,
    )
    if origin != TEMPLATE_ORIGIN:
        raise RuntimeError(f"canonical template origin mismatch: {origin}")
    if branch != "main":
        raise RuntimeError(f"canonical template must be on main, not {branch or 'detached HEAD'}")
    if status:
        raise RuntimeError("canonical template must be clean before fixture materialization")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError(f"canonical template revision is invalid: {revision}")
    return origin, branch, revision


def _identity(host: str, suffix: str) -> tuple[str, str, str]:
    safe_suffix = re.sub(r"[^A-Za-z0-9]", "", suffix)
    if not safe_suffix:
        raise ValueError("identity suffix must contain a letter or digit")
    label = f"Snmg{host.title()}Acceptance{safe_suffix}"
    key = f"snmg-{host}-acceptance-{safe_suffix.lower()}"
    plugin_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return label, key, plugin_id


def _scaffold(
    template_root: Path,
    project: Path,
    *,
    install_dependencies: bool,
) -> None:
    if project.exists():
        raise RuntimeError(f"fresh fixture destination already exists: {project}")
    project.parent.mkdir(parents=True, exist_ok=True)
    install_flag = "--install" if install_dependencies else "--skip-install"
    _run(
        project.parent,
        (sys.executable, str(template_root / "setup.py"), project.name, install_flag),
    )
    if not project.is_dir():
        raise RuntimeError("canonical template setup did not create the fixture")


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
    template_root: Path,
    project: Path,
    generator: str,
    host: str,
    suffix: str,
    source_root: Path,
    *,
    install_dependencies: bool = False,
) -> dict[str, object]:
    if host not in {"note", "doc"}:
        raise ValueError("host must be note or doc")
    template_root = template_root.resolve()
    project = project.resolve()
    origin, branch, revision = _template_identity(template_root)
    label, key, plugin_id = _identity(host, suffix)
    if project.name != label:
        raise RuntimeError(f"fixture destination name must be {label}")
    _scaffold(
        template_root,
        project,
        install_dependencies=install_dependencies,
    )

    permission = (
        "plugin.permission.FILE:WRITE"
        if host == "note"
        else "plugin.permission.FILE:READ"
    )
    permission_action = "deny" if host == "note" else "allow_once"
    package = json.loads((project / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != label:
        raise RuntimeError("canonical template setup produced an unexpected package identity")
    (project / ".supernote-launch-label").write_text(label + "\n", encoding="utf-8")
    (project / "PluginConfig.json").write_text(
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
    (project / "app.json").write_text(
        json.dumps({"name": key, "displayName": label}, indent=2) + "\n",
        encoding="utf-8",
    )
    application = (source_root / "App.tsx.tmpl").read_text(encoding="utf-8")
    application = (
        application.replace("__HOST__", host)
        .replace("__PLUGIN_NAME__", label)
        .replace("__PERMISSION__", permission)
        .replace("__PERMISSION_ACTION__", permission_action)
    )
    (project / "App.tsx").write_text(application, encoding="utf-8")

    _generator(
        project,
        generator,
        "add",
        FEATURE,
        "--starter",
        "cpp",
        "--starter",
        "kotlin",
        "--yes",
    )
    _write_sources(project, source_root)
    _generator(project, generator, "update", "--yes")
    _generator(project, generator, "validate")
    if install_dependencies:
        _run(project, ("npm", "install", f"./local_modules/{FEATURE}"))

    return {
        "schema_version": 2,
        "host": host,
        "plugin_name": label,
        "plugin_key": key,
        "plugin_id": plugin_id,
        "launch_label": label,
        "react_component": key,
        "permission": permission,
        "permission_action": permission_action,
        "template_origin": origin,
        "template_branch": branch,
        "template_revision": revision,
        "dependencies_installed": install_dependencies,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("project", type=Path)
    parser.add_argument("generator_command")
    parser.add_argument("host", choices=("note", "doc"))
    parser.add_argument("--identity-suffix", default="FinalA")
    parser.add_argument("--install-dependencies", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parent
    result = materialize(
        arguments.template,
        arguments.project,
        arguments.generator_command,
        arguments.host,
        arguments.identity_suffix,
        source_root,
        install_dependencies=arguments.install_dependencies,
    )
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
