from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def make_directory_symlink():
    """Create a directory symlink or skip when the host forbids test symlinks."""

    def make(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory symlinks are unavailable on this host: {exc}")

    return make
