from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from supernote_module_generator.errors import SymlinkPreservationError
from supernote_module_generator.filesystem import validate_source_symlink_support
from v4_project_inventory import inventory_json, inventory_project


def _write_ownership_fixture(root: Path) -> None:
    feature = root / "local_modules/@scope/drawing"
    feature.mkdir(parents=True)
    (feature / "index.js").write_text("export const value = 42;\n", encoding="utf-8")
    (feature / "source.cpp").write_text("int user_value = 42;\n", encoding="utf-8")
    (feature / ".supernote-module.json").write_text(
        json.dumps(
            {
                "npm_name": "@scope/drawing",
                "generated_files": [".supernote-module.json", "index.js"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = root / "android/.supernote-module/v4-runtime"
    runtime.mkdir(parents=True)
    (runtime / "runtime.cpp").write_text("int runtime = 1;\n", encoding="utf-8")
    (runtime / "ownership.json").write_text(
        json.dumps(
            {
                "generated_files": ["ownership.json", "runtime.cpp"],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_inventory_records_hash_mode_type_link_target_and_generator_owner(
    tmp_path: Path,
):
    root = tmp_path / "plugin"
    root.mkdir()
    _write_ownership_fixture(root)
    executable = root / "scripts/build.sh"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o751)
    link = root / "runtime-link"
    try:
        link.symlink_to("android/.supernote-module/v4-runtime", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable on this host: {exc}")

    inventory = {entry.path: entry for entry in inventory_project(root)}

    generated = inventory["local_modules/@scope/drawing/index.js"]
    assert generated.kind == "file"
    assert generated.sha256 == hashlib.sha256(
        (root / "local_modules/@scope/drawing/index.js").read_bytes()
    ).hexdigest()
    assert generated.generator_owner == "feature:@scope/drawing"
    assert inventory["local_modules/@scope/drawing/source.cpp"].generator_owner is None
    assert inventory["android/.supernote-module/v4-runtime/runtime.cpp"].generator_owner == (
        "shared-runtime"
    )
    assert inventory["scripts/build.sh"].mode == stat.S_IMODE(
        executable.lstat().st_mode
    )
    if os.name != "nt":
        assert inventory["scripts/build.sh"].mode == 0o751
    assert inventory["runtime-link"].kind == "symlink"
    assert inventory["runtime-link"].symlink_target == (
        "android/.supernote-module/v4-runtime"
    )
    assert not any(path.startswith("runtime-link/") for path in inventory)


def test_inventory_is_stable_and_exposes_byte_level_project_changes(tmp_path: Path):
    root = tmp_path / "plugin"
    root.mkdir()
    _write_ownership_fixture(root)

    before = inventory_project(root)
    assert before == inventory_project(root)
    assert inventory_json(before) == inventory_json(inventory_project(root))

    generated = root / "local_modules/@scope/drawing/index.js"
    original_mode = generated.stat().st_mode
    generated.write_text("export const value = 43;\n", encoding="utf-8")
    os.chmod(
        generated,
        stat.S_IREAD if os.name == "nt" else original_mode ^ stat.S_IXUSR,
    )

    after = inventory_project(root)
    assert before != after
    before_entry = next(entry for entry in before if entry.path.endswith("index.js"))
    after_entry = next(entry for entry in after if entry.path.endswith("index.js"))
    assert before_entry.sha256 != after_entry.sha256
    assert before_entry.mode != after_entry.mode


def test_inventory_ignores_unsafe_ownership_paths(tmp_path: Path):
    root = tmp_path / "plugin"
    root.mkdir()
    feature = root / "local_modules/safe"
    feature.mkdir(parents=True)
    outside = root / "outside.txt"
    outside.write_text("user owned\n", encoding="utf-8")
    (feature / ".supernote-module.json").write_text(
        json.dumps(
            {
                "npm_name": "safe",
                "generated_files": [".supernote-module.json", "../../outside.txt"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    inventory = {entry.path: entry for entry in inventory_project(root)}

    assert inventory["outside.txt"].generator_owner is None
    assert inventory["local_modules/safe/.supernote-module.json"].generator_owner == (
        "feature:safe"
    )


def test_windows_without_symlink_capability_fails_without_dereference(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    target = source / "target"
    target.write_text("must not be copied as fallback\n", encoding="utf-8")
    link = source / "link"
    try:
        link.symlink_to("target")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable on this host: {exc}")

    def unsupported() -> None:
        raise OSError("symbolic-link privilege is unavailable")

    monkeypatch.setattr(
        "supernote_module_generator.filesystem._probe_windows_symlink_support",
        unsupported,
    )

    with pytest.raises(SymlinkPreservationError, match="Windows cannot preserve"):
        validate_source_symlink_support((source,), platform_name="nt")

    assert link.is_symlink()
    assert os.readlink(link) == "target"
