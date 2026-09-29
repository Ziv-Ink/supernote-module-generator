"""Keep generated-publication orchestration boundaries below the complexity cap."""
from __future__ import annotations

from pathlib import Path

from complexity_ratchet import check_expected

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/supernote_module_generator"
EXPECTED = (
    ("generation_execution.py", ()),
    ("generation_service.py", ()),
)


def main() -> int:
    return check_expected(ROOT, PACKAGE, EXPECTED)


if __name__ == "__main__":
    raise SystemExit(main())
