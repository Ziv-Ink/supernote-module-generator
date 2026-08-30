"""Canonical feature identity, package path, and managed destination rules."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath

from .errors import ConfigurationError
from .naming import (
    validate_android_namespace,
    validate_generated_paths,
    validate_package_name,
    validate_package_version,
)
from .project import ensure_within_plugin


def canonical_feature_id(npm_name: str) -> str:
    digest = hashlib.sha256(npm_name.encode("utf-8")).hexdigest()[:16]
    return f"supernote:feature:{digest}"


@dataclass(frozen=True)
class FeatureIdentity:
    """One validated identity used by every feature operation."""

    npm_name: str
    android_namespace: str
    package_version: str
    feature_id: str

    @classmethod
    def create(
        cls,
        *,
        npm_name: str,
        android_namespace: str,
        package_version: str,
        feature_id: str | None = None,
    ) -> "FeatureIdentity":
        validate_package_name(npm_name)
        validate_android_namespace(android_namespace)
        validate_package_version(package_version)
        expected = canonical_feature_id(npm_name)
        actual = expected if feature_id is None else feature_id
        if actual != expected:
            raise ConfigurationError(
                f'feature identity mismatch for "{npm_name}": '
                f"expected {expected!r}, got {actual!r}",
                kind="invalid_metadata",
                phase="preflight",
            )
        return cls(npm_name, android_namespace, package_version, actual)

    @property
    def package_parts(self) -> tuple[str, ...]:
        if self.npm_name.startswith("@"):
            scope, name = self.npm_name.split("/", 1)
            return scope, name
        return (self.npm_name,)

    @property
    def relative_root(self) -> PurePosixPath:
        return PurePosixPath("local_modules", *self.package_parts)

    def destination(self, plugin_root: Path) -> Path:
        validate_generated_paths(
            plugin_root,
            self.npm_name,
            self.android_namespace,
        )
        destination = plugin_root.joinpath(*self.relative_root.parts)
        ensure_within_plugin(plugin_root, destination)
        return destination

    def validate_directory(self, plugin_root: Path, feature_root: Path) -> None:
        expected = self.destination(plugin_root)
        lexical = Path(feature_root.absolute())
        if lexical != expected:
            raise ConfigurationError(
                f'feature metadata for "{self.npm_name}" is stored in a '
                f"noncanonical directory: expected {expected}, got {lexical}",
                kind="invalid_metadata",
                phase="preflight",
            )
