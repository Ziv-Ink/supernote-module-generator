"""Atomic logical-feature and shared-runtime mutations."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from dataclasses import dataclass

from .errors import ConfigurationError, FilesystemError, GeneratorError
from .feature_generator import (
    FEATURE_STAGE_PREFIX,
    FeatureConfig,
    feature_scaffold_paths,
    stage_feature,
)
from .feature_identity import FeatureIdentity
from .filesystem import (
    FEATURE_ADD_INCOMPLETE_MARKER,
    activate_contained_directory_no_replace,
    contained_directory_entries_no_follow,
    contained_entry_kind_no_follow,
    lexists,
    read_regular_bytes_no_follow,
)
from .feature_model import (
    FEATURE_MANIFEST_KIND,
    FeatureModelError,
    FeatureManifest,
    ImplementationRoots,
)
from .models import ModuleInfo


class FeatureOperationError(ConfigurationError):
    pass


class FeatureMetadataError(GeneratorError):
    """An existing generator-owned feature manifest is corrupt or unsupported."""

    kind = "invalid_metadata"
    phase = "preflight"


class FeatureSourceError(GeneratorError):
    """A marked user declaration cannot be represented by generated bindings."""

    kind = "invalid_source"
    phase = "preflight"


@dataclass(frozen=True)
class FeatureRecord:
    path: Path
    manifest: FeatureManifest
    package_version: str
    description: str

    @property
    def identity(self) -> FeatureIdentity:
        return FeatureIdentity.create(
            npm_name=self.manifest.npm_name,
            android_namespace=self.manifest.android_namespace,
            package_version=self.package_version,
            feature_id=self.manifest.feature_id,
        )

    def info(self) -> ModuleInfo:
        return ModuleInfo(
            package_name=self.manifest.npm_name,
            javascript_name=self.manifest.public_name,
            type="feature",
            type_label="Supernote feature",
            path=str(self.path.resolve()),
            implementation_path=str((self.path / "android/src/main").resolve()),
            android_namespace=self.manifest.android_namespace,
            package_version=self.package_version,
        )


class FeatureOperationService:
    """Mutate one feature and the one shared component in one rollback domain."""

    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.resolve()
        self.features_root = self.root / "local_modules"

    def add(
        self,
        config: FeatureConfig,
    ) -> Path:
        self._reject_incomplete_staging()
        destination = config.output.absolute()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise FeatureOperationError(
                f"feature destination is outside the plugin root: {destination}"
            ) from exc
        if lexists(destination) and not self.resumable_incomplete_activation(
            destination
        ):
            self._reject_incomplete_activation(destination)
            raise FeatureOperationError(f"feature already exists: {config.npm_name}")
        staged_feature = stage_feature(config, staging_parent=self.root)
        try:
            try:
                activate_contained_directory_no_replace(
                    self.root,
                    staged_feature,
                    destination,
                    linux_fallback_files=feature_scaffold_paths(config),
                )
            except FileExistsError as exc:
                raise FeatureOperationError(
                    f"feature already exists: {config.npm_name}"
                ) from exc
            if lexists(staged_feature):
                shutil.rmtree(staged_feature)
            return destination
        except BaseException:
            if lexists(staged_feature):
                shutil.rmtree(staged_feature, ignore_errors=True)
            raise

    def _reject_incomplete_staging(self) -> None:
        residues = tuple(
            self.root / name
            for name, _kind in contained_directory_entries_no_follow(
                self.root, self.root
            )
            if name.startswith(FEATURE_STAGE_PREFIX)
        )
        if not residues:
            return
        paths = "\n".join(str(path) for path in residues)
        raise FeatureMetadataError(
            "incomplete authored feature staging was found:\n\n"
            f"{paths}\n\n"
            "Inspect these run-owned staging paths. Remove only the listed "
            "staging paths, then rerun sn-module-gen add. Existing contents "
            "will not be overwritten automatically."
        )

    def find(self, npm_name: str) -> Path:
        for path in self.feature_paths():
            if read_feature_manifest(path).npm_name == npm_name:
                return path
        raise FeatureOperationError(f"feature not found: {npm_name}")

    def feature_paths(
        self,
        *,
        allowed_incomplete: Path | None = None,
        allow_all_incomplete: bool = False,
    ) -> list[Path]:
        self._reject_incomplete_staging()
        root_kind = contained_entry_kind_no_follow(self.root, self.features_root)
        if root_kind == "symlink":
            self._reject_escaping_feature_links()
        if root_kind != "directory":
            return []
        self._reject_escaping_feature_links()
        result: list[Path] = []
        for metadata in self._canonical_metadata_candidates(
            allowed_incomplete=allowed_incomplete,
            allow_all_incomplete=allow_all_incomplete,
        ):
            self._reject_escaping_managed_path(metadata)
            record = read_feature_record(metadata.parent)
            record.identity.validate_directory(self.root, metadata.parent)
            result.append(metadata.parent)
        return result

    def _canonical_metadata_candidates(
        self,
        *,
        allowed_incomplete: Path | None,
        allow_all_incomplete: bool,
    ) -> list[Path]:
        candidates: list[Path] = []
        try:
            children = contained_directory_entries_no_follow(
                self.root, self.features_root
            )
        except OSError as exc:
            raise FeatureMetadataError(
                f"managed feature directory could not be read:\n\n"
                f"{self.features_root}: {exc}"
            ) from exc
        for name, kind in children:
            child = self.features_root / name
            if name.startswith("."):
                continue
            if name.startswith("@"):
                if kind != "directory":
                    continue
                try:
                    packages = contained_directory_entries_no_follow(
                        self.root, child
                    )
                except OSError as exc:
                    raise FeatureMetadataError(
                        f"managed feature scope could not be read:\n\n{child}: {exc}"
                    ) from exc
                for package_name, package_kind in packages:
                    package = child / package_name
                    if (
                        package_name.startswith(".")
                        or package_kind != "directory"
                    ):
                        continue
                    if self._skip_incomplete_activation(
                        package,
                        allowed_incomplete=allowed_incomplete,
                        allow_all_incomplete=allow_all_incomplete,
                    ):
                        continue
                    metadata = package / ".supernote-module.json"
                    if (
                        contained_entry_kind_no_follow(self.root, metadata)
                        == "file"
                    ):
                        candidates.append(metadata)
                continue
            metadata = child / ".supernote-module.json"
            if kind == "directory":
                if self._skip_incomplete_activation(
                    child,
                    allowed_incomplete=allowed_incomplete,
                    allow_all_incomplete=allow_all_incomplete,
                ):
                    continue
                if contained_entry_kind_no_follow(self.root, metadata) == "file":
                    candidates.append(metadata)
        return candidates

    def _reject_incomplete_activation(self, feature: Path) -> None:
        marker = feature / FEATURE_ADD_INCOMPLETE_MARKER
        if contained_entry_kind_no_follow(self.root, marker) is None:
            return
        raise FeatureMetadataError(
            "incomplete authored feature activation was found:\n\n"
            f"{marker}\n\n"
            "Rerun the same sn-module-gen add command to finish the scaffold. "
            "If its recorded contents were modified, inspect and remove only "
            "this incomplete scaffold before retrying. Update and validate will "
            "not ignore it."
        )

    def _skip_incomplete_activation(
        self,
        feature: Path,
        *,
        allowed_incomplete: Path | None,
        allow_all_incomplete: bool,
    ) -> bool:
        marker = feature / FEATURE_ADD_INCOMPLETE_MARKER
        if contained_entry_kind_no_follow(self.root, marker) is None:
            return False
        if allow_all_incomplete or feature == allowed_incomplete:
            return True
        self._reject_incomplete_activation(feature)
        raise AssertionError("incomplete activation rejection must raise")

    def resumable_incomplete_activation(self, feature: Path) -> bool:
        if contained_entry_kind_no_follow(self.root, feature) != "directory":
            return False
        marker = feature / FEATURE_ADD_INCOMPLETE_MARKER
        return contained_entry_kind_no_follow(self.root, marker) == "file"

    def _reject_escaping_feature_links(self) -> None:
        """Reject managed package-root links without policing user source links."""

        candidates = [self.features_root]
        if not self.features_root.is_symlink():
            try:
                children = contained_directory_entries_no_follow(
                    self.root, self.features_root
                )
            except OSError as exc:
                raise FeatureMetadataError(
                    f"managed feature directory could not be read:\n\n"
                    f"{self.features_root}: {exc}"
                ) from exc
            candidates.extend(
                self.features_root / name
                for name, _kind in children
                if not name.startswith(".")
            )
            for scope_name, scope_kind in children:
                scope = self.features_root / scope_name
                if (
                    scope_name.startswith("@")
                    and scope_kind == "directory"
                    and not scope.is_symlink()
                ):
                    try:
                        candidates.extend(
                            scope / name
                            for name, _kind in contained_directory_entries_no_follow(
                                self.root, scope
                            )
                            if not name.startswith(".")
                        )
                    except OSError as exc:
                        raise FeatureMetadataError(
                            f"managed feature scope could not be read:\n\n{scope}: {exc}"
                        ) from exc
        canonical_root = self.root.resolve()
        for candidate in candidates:
            self._reject_escaping_managed_path(candidate, canonical_root)

    def _reject_escaping_managed_path(
        self,
        candidate: Path,
        canonical_root: Path | None = None,
    ) -> None:
        if not candidate.is_symlink():
            return
        canonical = candidate.resolve(strict=False)
        try:
            canonical.relative_to(canonical_root or self.root.resolve())
        except ValueError as exc:
            raise ConfigurationError(
                "target resolves outside the Supernote plugin:\n\n"
                f"managed feature path {candidate}\n"
                f"resolves to {canonical}"
            ) from exc

    def records(
        self,
        *,
        allowed_incomplete: Path | None = None,
        allow_all_incomplete: bool = False,
    ) -> list[FeatureRecord]:
        return [
            read_feature_record(path)
            for path in self.feature_paths(
                allowed_incomplete=allowed_incomplete,
                allow_all_incomplete=allow_all_incomplete,
            )
        ]

    def find_record(self, npm_name: str) -> FeatureRecord:
        path = self.find(npm_name)
        return read_feature_record(path)

def read_feature_manifest(path: Path) -> FeatureManifest:
    metadata = path / ".supernote-module.json"
    raw = _read_feature_metadata(metadata)
    try:
        if "implementation_roots" not in raw:
            raise TypeError("implementation_roots is required")
        roots = raw["implementation_roots"]
        if not isinstance(roots, dict):
            raise TypeError("implementation_roots must be an object")
        starter_files = raw.get("starter_files", ())
        if not isinstance(starter_files, list):
            raise TypeError("starter_files must be an array")
        return FeatureManifest(
            feature_id=_required_string(raw, "feature_id"),
            npm_name=_required_string(raw, "npm_name"),
            public_name=_required_string(raw, "public_name"),
            android_namespace=_required_string(raw, "android_namespace"),
            roots=ImplementationRoots(
                _required_string(roots, "native"),
                _required_string(roots, "jvm"),
            ),
            starter_files=tuple(
                _array_string(starter_files, index)
                for index in range(len(starter_files))
            ),
            schema_version=_required_string(raw, "schema_version"),
        )
    except FeatureMetadataError:
        raise
    except (FeatureModelError, KeyError, TypeError, ValueError) as exc:
        raise _invalid_feature_metadata(metadata, str(exc)) from exc


def read_feature_record(path: Path) -> FeatureRecord:
    metadata = path / ".supernote-module.json"
    raw = _read_feature_metadata(metadata)
    try:
        package_version = _required_string(raw, "package_version")
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise TypeError("description must be a string")
        record = FeatureRecord(
            path.absolute(),
            read_feature_manifest(path),
            package_version,
            description,
        )
        record.identity
        return record
    except FeatureMetadataError:
        raise
    except (ConfigurationError, KeyError, TypeError, ValueError) as exc:
        raise _invalid_feature_metadata(metadata, str(exc)) from exc


def _read_feature_metadata(metadata: Path) -> dict[str, object]:
    try:
        payload, _metadata = read_regular_bytes_no_follow(metadata)
        value = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        reason = f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        raise _invalid_feature_metadata(metadata, reason) from exc
    except (FilesystemError, OSError, UnicodeDecodeError) as exc:
        raise _invalid_feature_metadata(metadata, f"could not read file: {exc}") from exc
    if not isinstance(value, dict):
        raise _invalid_feature_metadata(metadata, "top-level value must be an object")
    kind = value.get("kind")
    if kind != FEATURE_MANIFEST_KIND:
        raise _invalid_feature_metadata(
            metadata,
            f"kind must be {FEATURE_MANIFEST_KIND!r}, got {kind!r}",
        )
    return value


def _invalid_feature_metadata(metadata: Path, reason: str) -> FeatureMetadataError:
    return FeatureMetadataError(
        f"feature metadata is invalid or unsupported:\n\n{metadata}: {reason}"
    )


def _required_string(value: dict[str, object], name: str) -> str:
    if name not in value:
        raise TypeError(f"{name} is required")
    item = value[name]
    if not isinstance(item, str) or not item:
        raise TypeError(f"{name} must be a non-empty string")
    return item


def _required_integer(value: dict[str, object], name: str) -> int:
    if name not in value:
        raise TypeError(f"{name} is required")
    item = value[name]
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"{name} must be an integer")
    return item


def _array_string(value: list[object], index: int) -> str:
    item = value[index]
    if not isinstance(item, str):
        raise TypeError(f"starter_files[{index}] must be a string")
    return item
