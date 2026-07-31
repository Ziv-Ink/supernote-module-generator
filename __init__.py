"""Source-checkout import shim for direct test and development commands."""
from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).resolve().parent / "src" / "supernote_module_generator"
if str(_SOURCE_PACKAGE) not in __path__:
    __path__.insert(0, str(_SOURCE_PACKAGE))
