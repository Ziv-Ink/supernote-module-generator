"""Ratchet transaction debt and keep extracted orchestration boundaries clean."""
from __future__ import annotations

from pathlib import Path

from complexity_ratchet import check_expected

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/supernote_module_generator"
EXPECTED = (
    (
        "transaction.py",
        (
            ("_complete_recovery_pointer", 11),
            ("_validate_transaction_entries", 23),
            ("_validate_restore_payloads", 11),
            ("replace_snapshot_file_baseline", 13),
            ("_adopt_snapshot_change", 17),
            ("preserve_external_source_changes", 17),
            ("replace_regular_batch_if_matches", 17),
            ("_retain_matching_regular_file", 25),
            ("_resolve_regular_publication_conflict", 11),
            ("recover_pending", 18),
            ("_rollback_data", 26),
            ("_finish_durable_data", 13),
        ),
    ),
    ("transaction_registry.py", ()),
    ("generation_execution.py", ()),
    ("generation_service.py", ()),
)


def main() -> int:
    return check_expected(ROOT, PACKAGE, EXPECTED)


if __name__ == "__main__":
    raise SystemExit(main())
