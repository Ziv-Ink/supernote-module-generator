"""Minimal parent dependency bootstrap used by fresh V4 feature creation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from .errors import ConfigurationError
from .filesystem import entry_kind, read_regular_bytes_no_follow
from .project import dependency_value, read_parent_package


def add_dependency(root: Path, package_name: str) -> None:
    """Add the canonical local dependency without exposing legacy wiring APIs."""

    path, package = read_parent_package(root)
    dependencies = package.setdefault("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ConfigurationError("package.json dependencies must be an object")
    dependencies[package_name] = dependency_value(package_name)
    content = (json.dumps(package, indent=2) + "\n").encode("utf-8")
    _replace_regular_file(path, content)


def _replace_regular_file(path: Path, content: bytes) -> None:
    if entry_kind(path) != "file":
        raise ConfigurationError(f"parent dependency file must be regular: {path}")
    _previous, metadata = read_regular_bytes_no_follow(path)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, path)
    finally:
        if entry_kind(temporary) is not None:
            temporary.unlink()
