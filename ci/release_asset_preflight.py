#!/usr/bin/env python3
"""Refuse to upload over any existing target GitHub release asset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROVENANCE_ASSETS = ("SHA256SUMS", "release-provenance.json")


def target_asset_names(distributions: Path, provenance: Path) -> set[str]:
    wheels = sorted(path.name for path in distributions.glob("*.whl") if path.is_file())
    sdists = sorted(path.name for path in distributions.glob("*.tar.gz") if path.is_file())
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("release upload requires exactly one wheel and one source archive")
    missing = [name for name in PROVENANCE_ASSETS if not (provenance / name).is_file()]
    if missing:
        raise ValueError(f"release provenance assets are missing: {missing}")
    return {*wheels, *sdists, *PROVENANCE_ASSETS}


def existing_asset_names(inventory: Path) -> set[str]:
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        raise ValueError("GitHub release inventory must contain an assets list")
    names: set[str] = set()
    for asset in payload["assets"]:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("GitHub release inventory contains an invalid asset")
        name = asset["name"]
        if not name or name in names:
            raise ValueError("GitHub release inventory contains an invalid asset name")
        names.add(name)
    return names


def verify(inventory: Path, distributions: Path, provenance: Path) -> None:
    conflicts = sorted(
        target_asset_names(distributions, provenance) & existing_asset_names(inventory)
    )
    if conflicts:
        raise ValueError(
            "release already contains target assets; refusing to replace or publish: "
            + ", ".join(conflicts)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("distributions", type=Path)
    parser.add_argument("provenance", type=Path)
    arguments = parser.parse_args()
    verify(arguments.inventory, arguments.distributions, arguments.provenance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
