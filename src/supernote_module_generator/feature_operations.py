"""Atomic V3 logical-feature and shared-runtime mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid
from dataclasses import dataclass

from . import __version__, binding_codegen
from .errors import ConfigurationError, GeneratorError
from .feature_generator import FeatureConfig, stage_feature
from .feature_model import (
    FEATURE_MANIFEST_KIND,
    FeatureModelError,
    FeatureManifest,
    FeatureRegistryEntry,
    ImplementationRoots,
    PluginRuntimeRegistry,
    StarterFamily,
)
from .plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    generated_runtime_files,
    stage_plugin_runtime,
)
from .plugin_build_integration import set_runtime_wiring, verify_runtime_wiring
from .semantic import SemanticApi
from .models import ModuleInfo


class FeatureOperationError(ConfigurationError):
    pass


class FeatureMetadataError(GeneratorError):
    """An existing generator-owned feature manifest is corrupt or unsupported."""

    kind = "invalid_metadata"
    phase = "preflight"


class FeatureSourceError(GeneratorError):
    """A marked user declaration cannot be represented by V3 bindings."""

    kind = "invalid_source"
    phase = "preflight"


LEGACY_RUNTIME_RELATIVE_ROOT = Path("android/.supernote-module/v2-runtime")


@dataclass(frozen=True)
class FeatureRecord:
    path: Path
    manifest: FeatureManifest
    package_version: str
    description: str

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

    def add(self, config: FeatureConfig) -> Path:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(
            self.root,
            enabled=had_features,
            allow_missing_package=True,
            allow_legacy_v2=True,
        )
        destination = config.output.resolve()
        if destination.exists():
            raise FeatureOperationError(f"feature already exists: {config.npm_name}")
        staged_feature = stage_feature(config)
        staged_runtime = None
        feature_backup = None
        runtime_backup = None
        feature_activated = False
        runtime_activated = False
        integration_mutated = False
        legacy_runtime_backup = None
        legacy_runtime_deactivated = False
        try:
            future = self._entries(extra=(staged_feature,), excluding=())
            staged_runtime = stage_plugin_runtime(self.root, self._registry(future))
            feature_backup = self._activate(staged_feature, destination)
            feature_activated = True
            runtime_backup = self._activate(
                staged_runtime, self.root / RUNTIME_RELATIVE_ROOT
            )
            runtime_activated = True
            legacy_runtime_backup = self._deactivate(
                self.root / LEGACY_RUNTIME_RELATIVE_ROOT
            )
            legacy_runtime_deactivated = True
            set_runtime_wiring(self.root, enabled=True)
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=True)
            self._finalize(feature_backup)
            self._finalize(runtime_backup)
            self._finalize(legacy_runtime_backup)
            return destination
        except BaseException:
            if feature_activated:
                self._restore(destination, feature_backup)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            if legacy_runtime_deactivated:
                self._restore(
                    self.root / LEGACY_RUNTIME_RELATIVE_ROOT,
                    legacy_runtime_backup,
                )
            shutil.rmtree(staged_feature, ignore_errors=True)
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def update(self, npm_name: str) -> Path:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(
            self.root,
            enabled=had_features,
            allow_missing_package=True,
            allow_legacy_v2=True,
        )
        current = self.find(npm_name)
        metadata = read_feature_manifest(current)
        raw = json.loads((current / ".supernote-module.json").read_text())
        starters = _starter_families(metadata.starter_files)
        config = FeatureConfig(
            output=current,
            npm_name=metadata.npm_name,
            package_version=str(raw["package_version"]),
            android_namespace=metadata.android_namespace,
            public_name=metadata.public_name,
            description=str(raw.get("description", "")),
            starters=starters or (StarterFamily.NATIVE,),
        )
        staged_feature = stage_feature(config, preserve_sources_from=current)
        staged_runtime = None
        feature_backup = None
        runtime_backup = None
        feature_activated = False
        runtime_activated = False
        integration_mutated = False
        legacy_runtime_backup = None
        legacy_runtime_deactivated = False
        try:
            future = self._entries(extra=(staged_feature,), excluding=(current,))
            staged_runtime = stage_plugin_runtime(self.root, self._registry(future))
            feature_backup = self._activate(staged_feature, current)
            feature_activated = True
            runtime_backup = self._activate(
                staged_runtime, self.root / RUNTIME_RELATIVE_ROOT
            )
            runtime_activated = True
            legacy_runtime_backup = self._deactivate(
                self.root / LEGACY_RUNTIME_RELATIVE_ROOT
            )
            legacy_runtime_deactivated = True
            set_runtime_wiring(self.root, enabled=True)
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=True)
            self._finalize(feature_backup)
            self._finalize(runtime_backup)
            self._finalize(legacy_runtime_backup)
            return current
        except BaseException:
            if feature_activated:
                self._restore(current, feature_backup)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            if legacy_runtime_deactivated:
                self._restore(
                    self.root / LEGACY_RUNTIME_RELATIVE_ROOT,
                    legacy_runtime_backup,
                )
            shutil.rmtree(staged_feature, ignore_errors=True)
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def remove(self, npm_name: str) -> None:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(
            self.root,
            enabled=had_features,
            allow_missing_package=True,
            allow_legacy_v2=True,
        )
        current = self.find(npm_name)
        staged_runtime = None
        runtime_backup = None
        runtime_activated = False
        integration_mutated = False
        legacy_runtime_backup = None
        legacy_runtime_deactivated = False
        feature_backup = current.parent / f".{current.name}.removed-{uuid.uuid4().hex}"
        try:
            future = self._entries(extra=(), excluding=(current,))
            if future:
                staged_runtime = stage_plugin_runtime(
                    self.root, self._registry(future)
                )
            os.replace(current, feature_backup)
            if staged_runtime is not None:
                runtime_backup = self._activate(
                    staged_runtime, self.root / RUNTIME_RELATIVE_ROOT
                )
            else:
                runtime_backup = self._deactivate(
                    self.root / RUNTIME_RELATIVE_ROOT
                )
            runtime_activated = True
            legacy_runtime_backup = self._deactivate(
                self.root / LEGACY_RUNTIME_RELATIVE_ROOT
            )
            legacy_runtime_deactivated = True
            set_runtime_wiring(self.root, enabled=bool(future))
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=bool(future))
            shutil.rmtree(feature_backup)
            self._finalize(runtime_backup)
            self._finalize(legacy_runtime_backup)
        except BaseException:
            if feature_backup.exists() and not current.exists():
                os.replace(feature_backup, current)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            if legacy_runtime_deactivated:
                self._restore(
                    self.root / LEGACY_RUNTIME_RELATIVE_ROOT,
                    legacy_runtime_backup,
                )
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def find(self, npm_name: str) -> Path:
        for path in self.feature_paths():
            if read_feature_manifest(path).npm_name == npm_name:
                return path
        raise FeatureOperationError(f"feature not found: {npm_name}")

    def feature_paths(self) -> list[Path]:
        if self.features_root.is_symlink():
            self._reject_escaping_feature_links()
        if not self.features_root.is_dir():
            return []
        self._reject_escaping_feature_links()
        result = []
        for metadata in sorted(self.features_root.rglob(".supernote-module.json")):
            relative = metadata.relative_to(self.features_root)
            if any(part.startswith(".") for part in relative.parts[:-1]):
                continue
            self._reject_escaping_managed_path(metadata)
            read_feature_manifest(metadata.parent)
            result.append(metadata.parent)
        return result

    def _reject_escaping_feature_links(self) -> None:
        """Reject managed package-root links without policing user source links."""

        candidates = [self.features_root]
        if not self.features_root.is_symlink():
            try:
                children = sorted(self.features_root.iterdir())
            except OSError as exc:
                raise FeatureMetadataError(
                    f"managed feature directory could not be read:\n\n"
                    f"{self.features_root}: {exc}"
                ) from exc
            candidates.extend(
                child for child in children if not child.name.startswith(".")
            )
            for scope in children:
                if (
                    scope.name.startswith("@")
                    and scope.is_dir()
                    and not scope.is_symlink()
                ):
                    try:
                        candidates.extend(
                            child
                            for child in sorted(scope.iterdir())
                            if not child.name.startswith(".")
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
        legacy_runtime = self.root / LEGACY_RUNTIME_RELATIVE_ROOT
        if legacy_runtime.exists():
            issues.append(f"stale generated V2 runtime exists: {legacy_runtime}")
        runtime = self.root / RUNTIME_RELATIVE_ROOT
        if not records:
            if runtime.exists():
                issues.append("shared V3 runtime exists without any features")
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

    @staticmethod
    def _activate(staged: Path, destination: Path) -> Path | None:
        backup = None
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
            return backup
        except Exception:
            if backup is not None and backup.exists():
                os.replace(backup, destination)
            raise

    @staticmethod
    def _restore(destination: Path, backup: Path | None) -> None:
        if backup is None:
            shutil.rmtree(destination, ignore_errors=True)
            return
        if destination.exists():
            shutil.rmtree(destination)
        if backup.exists():
            os.replace(backup, destination)

    @staticmethod
    def _deactivate(destination: Path) -> Path | None:
        if not destination.exists():
            return None
        backup = destination.parent / (
            f".{destination.name}.backup-{uuid.uuid4().hex}"
        )
        os.replace(destination, backup)
        return backup

    @staticmethod
    def _finalize(backup: Path | None) -> None:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


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
        return FeatureRecord(
            path.resolve(),
            read_feature_manifest(path),
            package_version,
            description,
        )
    except FeatureMetadataError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid_feature_metadata(metadata, str(exc)) from exc


def _read_feature_metadata(metadata: Path) -> dict[str, object]:
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        reason = f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        raise _invalid_feature_metadata(metadata, reason) from exc
    except OSError as exc:
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


def _starter_families(files: tuple[str, ...]) -> tuple[StarterFamily, ...]:
    values = []
    if any(path.startswith("android/src/main/cpp/") for path in files):
        values.append(StarterFamily.NATIVE)
    if any(path.startswith("android/src/main/java/") for path in files):
        values.append(StarterFamily.JVM)
    return tuple(values)
