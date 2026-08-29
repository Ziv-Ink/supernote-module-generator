"""Atomic V4 logical-feature and shared-runtime mutations."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from . import __version__, binding_codegen
from .errors import ConfigurationError, FilesystemError, GeneratorError
from .feature_generator import FeatureConfig, stage_feature
from .feature_identity import FeatureIdentity
from .filesystem import (
    contained_directory_entries_no_follow,
    contained_entry_kind_no_follow,
    lexists,
    read_regular_bytes_no_follow,
)
from .feature_model import (
    FEATURE_MANIFEST_KIND,
    FeatureModelError,
    FeatureManifest,
    FeatureRegistryEntry,
    ImplementationRoots,
    PluginRuntimeRegistry,
)
from .plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    generated_runtime_files,
    stage_plugin_runtime,
)
from .plugin_build_integration import (
    integration_mutation_files,
    set_runtime_wiring,
    verify_runtime_wiring,
)
from .semantic import SemanticApi
from .models import ModuleInfo
from .transaction import Transaction


class FeatureOperationError(ConfigurationError):
    pass


class FeatureMetadataError(GeneratorError):
    """An existing generator-owned feature manifest is corrupt or unsupported."""

    kind = "invalid_metadata"
    phase = "preflight"


class FeatureSourceError(GeneratorError):
    """A marked user declaration cannot be represented by V4 bindings."""

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
        *,
        transaction: Transaction | None = None,
    ) -> Path:
        had_features = bool(self.feature_paths())
        runtime_root = self.root / RUNTIME_RELATIVE_ROOT
        if (
            not had_features
            and contained_entry_kind_no_follow(self.root, runtime_root) is not None
        ):
            raise FeatureOperationError(
                "shared V4 runtime exists without feature or integrity-manifest "
                f"ownership authority: {runtime_root}"
            )
        verify_runtime_wiring(
            self.root,
            enabled=had_features,
            allow_missing_package=True,
        )
        destination = config.output.resolve()
        if lexists(config.output) or destination.exists():
            raise FeatureOperationError(f"feature already exists: {config.npm_name}")
        journal, owns_journal = self._journal(
            transaction, "add", (config.npm_name,)
        )
        try:
            staged_feature = stage_feature(config)
            journal.track_created(staged_feature)
            future = self._entries(extra=(staged_feature,), excluding=())
            journal.track_created_directory(
                (self.root / RUNTIME_RELATIVE_ROOT).parent
            )
            staged_runtime = stage_plugin_runtime(self.root, self._registry(future))
            journal.track_created(staged_runtime)
            journal.checkpoint("after_staging")
            journal.activate(staged_feature, destination)
            journal.checkpoint("after_first_file_replacement")
            journal.activate(staged_runtime, runtime_root)
            set_runtime_wiring(self.root, enabled=True)
            journal.record_snapshot_results(integration_mutation_files(self.root))
            journal.checkpoint("after_wiring")
            verify_runtime_wiring(self.root, enabled=True)
            if owns_journal:
                journal.commit()
            return destination
        except BaseException:
            if owns_journal:
                journal.rollback()
            raise

    def _journal(
        self,
        transaction: Transaction | None,
        command: str,
        modules: tuple[str, ...],
    ) -> tuple[Transaction, bool]:
        if transaction is not None:
            return transaction, False
        journal = Transaction(self.root, command, modules)
        journal.snapshot(integration_mutation_files(self.root))
        return journal, True

    def find(self, npm_name: str) -> Path:
        for path in self.feature_paths():
            if read_feature_manifest(path).npm_name == npm_name:
                return path
        raise FeatureOperationError(f"feature not found: {npm_name}")

    def feature_paths(self) -> list[Path]:
        root_kind = contained_entry_kind_no_follow(self.root, self.features_root)
        if root_kind == "symlink":
            self._reject_escaping_feature_links()
        if root_kind != "directory":
            return []
        self._reject_escaping_feature_links()
        result: list[Path] = []
        for metadata in self._canonical_metadata_candidates():
            self._reject_escaping_managed_path(metadata)
            record = read_feature_record(metadata.parent)
            record.identity.validate_directory(self.root, metadata.parent)
            result.append(metadata.parent)
        return result

    def _canonical_metadata_candidates(self) -> list[Path]:
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
                    metadata = package / ".supernote-module.json"
                    if (
                        contained_entry_kind_no_follow(self.root, metadata)
                        == "file"
                    ):
                        candidates.append(metadata)
                continue
            metadata = child / ".supernote-module.json"
            if kind == "directory" and (
                contained_entry_kind_no_follow(self.root, metadata) == "file"
            ):
                candidates.append(metadata)
        return candidates

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

    def records(self) -> list[FeatureRecord]:
        return [read_feature_record(path) for path in self.feature_paths()]

    def find_record(self, npm_name: str) -> FeatureRecord:
        path = self.find(npm_name)
        return read_feature_record(path)

    def expected_registry(self) -> PluginRuntimeRegistry:
        return self._registry(self._entries(extra=(), excluding=()))

    def verify_generated_state(self) -> list[str]:
        """Return deterministic structural issues without mutating the plugin."""

        records = self.records()
        issues: list[str] = []
        try:
            verify_runtime_wiring(self.root, enabled=bool(records))
        except Exception as exc:
            issues.append(str(exc))
        runtime = self.root / RUNTIME_RELATIVE_ROOT
        if not records:
            if runtime.exists():
                issues.append("shared V4 runtime exists without any features")
            return issues
        try:
            expected = generated_runtime_files(self.expected_registry())
        except Exception as exc:
            issues.append(str(exc))
            return issues
        for relative, content in expected.items():
            path = runtime / relative
            if not path.is_file():
                issues.append(f"missing generated runtime file: {path}")
            elif path.read_text(encoding="utf-8") != content:
                issues.append(f"stale generated runtime file: {path}")
        for record in records:
            for relative in (
                ".supernote-module.json",
                "package.json",
                "index.js",
                "index.d.ts",
                "README.md",
            ):
                if not (record.path / relative).is_file():
                    issues.append(f"missing generated feature file: {record.path / relative}")
        return issues

    def _entries(
        self,
        *,
        extra: tuple[Path, ...],
        excluding: tuple[Path, ...],
    ) -> tuple[FeatureRegistryEntry, ...]:
        excluded = {path.resolve() for path in excluding}
        paths = [
            path for path in self.feature_paths() if path.resolve() not in excluded
        ]
        paths.extend(extra)
        entries = []
        for path in paths:
            manifest = read_feature_manifest(path)
            native_root = path / manifest.roots.native
            try:
                semantic = (
                    binding_codegen.scan_cpp_semantic_model(
                        path, module_name=manifest.public_name
                    )
                    if native_root.is_dir()
                    else SemanticApi()
                )
            except binding_codegen.CodegenError as exc:
                raise FeatureSourceError(str(exc)) from exc
            entries.append(FeatureRegistryEntry.create(manifest, semantic))
        return tuple(entries)

    def _registry(
        self, entries: tuple[FeatureRegistryEntry, ...]
    ) -> PluginRuntimeRegistry:
        package = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
        plugin_id = str(package.get("name") or self.root.name)
        return PluginRuntimeRegistry.create(
            plugin_id=plugin_id,
            generator_version=__version__,
            features=entries,
        )

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
            schema_version=_required_integer(raw, "schema_version"),
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
