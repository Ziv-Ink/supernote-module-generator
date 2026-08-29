"""Fail if the filesystem safety boundary gains new McCabe debt."""
from __future__ import annotations

from pathlib import Path

from complexity_ratchet import check_expected

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/supernote_module_generator"
EXPECTED = (
    (
        "filesystem.py",
        (
            ("read_contained_regular_bytes_no_follow", 11),
            ("copy_entry_no_follow", 13),
            ("copy_tree_contents_no_follow", 12),
            ("restore_protected_directory_metadata", 11),
            ("restore_protected_source_backup", 20),
        ),
    ),
    ("filesystem_inventory.py", ()),
)


def main() -> int:
    return check_expected(ROOT, PACKAGE, EXPECTED)


if __name__ == "__main__":
    raise SystemExit(main())
