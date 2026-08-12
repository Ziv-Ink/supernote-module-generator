"""Atomic V2 logical-feature and shared-runtime mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid

from . import __version__, binding_codegen
from .feature_generator import FeatureConfig, stage_feature
from .feature_model import (
    FeatureManifest,
    FeatureRegistryEntry,
    ImplementationRoots,
    PluginRuntimeRegistry,
    StarterFamily,
)
from .plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    stage_plugin_runtime,
)
from .plugin_build_integration import set_runtime_wiring, verify_runtime_wiring
from .semantic import SemanticApi


class FeatureOperationError(RuntimeError):
    pass


class FeatureOperationService:
    """Mutate one feature and the one shared component in one rollback domain."""

    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.resolve()
        self.features_root = self.root / "local_modules"

    def add(self, config: FeatureConfig) -> Path:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(self.root, enabled=had_features)
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
        try:
            future = self._entries(extra=(staged_feature,), excluding=())
            staged_runtime = stage_plugin_runtime(self.root, self._registry(future))
            feature_backup = self._activate(staged_feature, destination)
            feature_activated = True
            runtime_backup = self._activate(
                staged_runtime, self.root / RUNTIME_RELATIVE_ROOT
            )
            runtime_activated = True
            set_runtime_wiring(self.root, enabled=True)
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=True)
            self._finalize(feature_backup)
            self._finalize(runtime_backup)
            return destination
        except Exception:
            if feature_activated:
                self._restore(destination, feature_backup)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            shutil.rmtree(staged_feature, ignore_errors=True)
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def update(self, npm_name: str) -> Path:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(self.root, enabled=had_features)
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
        try:
            future = self._entries(extra=(staged_feature,), excluding=(current,))
            staged_runtime = stage_plugin_runtime(self.root, self._registry(future))
            feature_backup = self._activate(staged_feature, current)
            feature_activated = True
            runtime_backup = self._activate(
                staged_runtime, self.root / RUNTIME_RELATIVE_ROOT
            )
            runtime_activated = True
            set_runtime_wiring(self.root, enabled=True)
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=True)
            self._finalize(feature_backup)
            self._finalize(runtime_backup)
            return current
        except Exception:
            if feature_activated:
                self._restore(current, feature_backup)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            shutil.rmtree(staged_feature, ignore_errors=True)
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def remove(self, npm_name: str) -> None:
        had_features = bool(self.feature_paths())
        verify_runtime_wiring(self.root, enabled=had_features)
        current = self.find(npm_name)
        staged_runtime = None
        runtime_backup = None
        runtime_activated = False
        integration_mutated = False
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
            set_runtime_wiring(self.root, enabled=bool(future))
            integration_mutated = True
            verify_runtime_wiring(self.root, enabled=bool(future))
            shutil.rmtree(feature_backup)
            self._finalize(runtime_backup)
        except Exception:
            if feature_backup.exists() and not current.exists():
                os.replace(feature_backup, current)
            if runtime_activated:
                self._restore(self.root / RUNTIME_RELATIVE_ROOT, runtime_backup)
            if integration_mutated:
                set_runtime_wiring(self.root, enabled=had_features)
            if staged_runtime is not None:
                shutil.rmtree(staged_runtime, ignore_errors=True)
            raise

    def find(self, npm_name: str) -> Path:
        for path in self.feature_paths():
            if read_feature_manifest(path).npm_name == npm_name:
                return path
        raise FeatureOperationError(f"feature not found: {npm_name}")

    def feature_paths(self) -> list[Path]:
        if not self.features_root.is_dir():
            return []
        result = []
        for metadata in sorted(self.features_root.rglob(".supernote-module.json")):
            relative = metadata.relative_to(self.features_root)
            if any(part.startswith(".") for part in relative.parts[:-1]):
                continue
            value = json.loads(metadata.read_text(encoding="utf-8"))
            if value.get("kind") == "supernote_feature":
                result.append(metadata.parent)
        return result

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
            _reject_unprocessed_jvm_markers(path, manifest.roots)
            native_root = path / manifest.roots.native
            semantic = (
                binding_codegen.scan_cpp_semantic_model(
                    path, module_name=manifest.public_name
                )
                if native_root.is_dir()
                else SemanticApi()
            )
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
    raw = json.loads((path / ".supernote-module.json").read_text(encoding="utf-8"))
    roots = raw["implementation_roots"]
    return FeatureManifest(
        feature_id=str(raw["feature_id"]),
        npm_name=str(raw["npm_name"]),
        public_name=str(raw["public_name"]),
        android_namespace=str(raw["android_namespace"]),
        roots=ImplementationRoots(str(roots["native"]), str(roots["jvm"])),
        starter_files=tuple(str(item) for item in raw.get("starter_files", ())),
        schema_version=int(raw["schema_version"]),
    )


def _starter_families(files: tuple[str, ...]) -> tuple[StarterFamily, ...]:
    values = []
    if any(path.startswith("android/src/main/cpp/") for path in files):
        values.append(StarterFamily.NATIVE)
    if any(path.startswith("android/src/main/java/") for path in files):
        values.append(StarterFamily.JVM)
    return tuple(values)


def _reject_unprocessed_jvm_markers(
    path: Path,
    roots: ImplementationRoots,
) -> None:
    root = path / roots.jvm
    if not root.is_dir():
        return
    for source in root.rglob("*"):
        if source.suffix not in {".kt", ".java"} or not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        if "@Supernote" in text:
            raise FeatureOperationError(
                f"{source}: KSP JVM manifest generation is not implemented yet"
            )
