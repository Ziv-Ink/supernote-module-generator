"""Read-only canonical discovery of the V4 project input model."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Tuple

from .errors import (
    GeneratorError,
    UnsupportedLegacyProject,
    UnmanifestedGeneratedProject,
)
from .feature_identity import FeatureIdentity
from .feature_operations import FeatureOperationService
from .filesystem import (
    contained_directory_entries_no_follow,
    contained_entry_kind_no_follow,
    contained_tree_entries_no_follow,
    read_contained_regular_bytes_no_follow,
    validate_contained_path_no_follow,
)
from .integrity_manifest import (
    INTEGRITY_MANIFEST_PATH,
    IntegrityManifestError,
    load_integrity_manifest,
)
from .project import read_parent_package
from .schemas import FEATURE_MANIFEST_KIND, FEATURE_MANIFEST_SCHEMA_VERSION


class ExistingGeneration(str, Enum):
    NONE = "none"
    V3 = "v3"
    V4 = "v4"
    UNMANIFESTED_V4 = "unmanifested_v4"
    UNSUPPORTED_LEGACY = "unsupported_legacy"


@dataclass(frozen=True)
class ProjectFeature:
    identity: FeatureIdentity
    root: Path
    public_name: str
    description: str
    native_root: Path
    jvm_root: Path


@dataclass(frozen=True)
class ProjectModel:
    plugin_root: Path
    plugin_id: str
    features: Tuple[ProjectFeature, ...]
    selected_build_variant: str
    existing_generation: ExistingGeneration
    dependencies: Tuple[Tuple[str, str], ...]
    wiring_paths: Tuple[Path, ...]

    @classmethod
    def discover(
        cls,
        plugin_root: Path,
        *,
        build_variant: str = "debug",
        allow_unmanifested_bootstrap: bool = False,
    ) -> "ProjectModel":
        root = plugin_root.resolve()
        generation = detect_existing_generation(
            root,
            allow_operation_staging=allow_unmanifested_bootstrap,
        )
        if not allow_unmanifested_bootstrap:
            reject_unsupported_project_state(root, generation=generation)
        _, package = read_parent_package(root)
        plugin_id = package.get("name")
        if not isinstance(plugin_id, str) or not plugin_id:
            plugin_id = root.name
        service = FeatureOperationService(root)
        features = []
        for record in service.records():
            features.append(
                ProjectFeature(
                    record.identity,
                    record.path,
                    record.manifest.public_name,
                    record.description,
                    record.path / record.manifest.roots.native,
                    record.path / record.manifest.roots.jvm,
                )
            )
        dependencies = package.get("dependencies", {})
        dependency_rows = (
            tuple(
                sorted(
                    (str(name), str(value))
                    for name, value in dependencies.items()
                )
            )
            if isinstance(dependencies, dict)
            else ()
        )
        return cls(
            root,
            plugin_id,
            tuple(sorted(features, key=lambda item: item.identity.npm_name)),
            build_variant,
            generation,
            dependency_rows,
            (
                root / "android/settings.gradle",
                root / "android/app/build.gradle",
                root / "android/app/src/main/java",
            ),
        )


def detect_existing_generation(
    root: Path,
    *,
    allow_operation_staging: bool = False,
) -> ExistingGeneration:
    """Classify generated project state without following managed symlinks."""

    root = root.resolve()
    manifest = root / INTEGRITY_MANIFEST_PATH
    manifest_kind = contained_entry_kind_no_follow(root, manifest)
    manifest_value: object | None = None
    loaded_manifest = None
    manifest_error: BaseException | None = None
    if manifest_kind is not None:
        if manifest_kind != "file":
            manifest_error = GeneratorError(
                f"V4 integrity manifest must be a regular file: {manifest}",
                kind="invalid_metadata",
                phase="preflight",
            )
        else:
            try:
                content, _metadata = read_contained_regular_bytes_no_follow(
                    root, manifest
                )
                manifest_value = json.loads(content.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                manifest_error = GeneratorError(
                    f"V4 integrity manifest is invalid: {manifest}: {exc}",
                    kind="invalid_metadata",
                    phase="preflight",
                )
            if (
                manifest_error is None
                and isinstance(manifest_value, dict)
                and manifest_value.get("schema_version") == 4
            ):
                try:
                    # First parse only the root ownership declaration. Explicit
                    # legacy metadata must take precedence, and the canonical
                    # feature parser must retain its field/path-specific errors,
                    # before the live ownership inventories are authorized.
                    loaded_manifest = load_integrity_manifest(
                        root,
                        validate_live_ownership=False,
                    )
                except IntegrityManifestError as exc:
                    manifest_error = GeneratorError(
                        f"V4 integrity manifest is invalid: {manifest}: {exc}",
                        kind="invalid_metadata",
                        phase="preflight",
                    )

    active_v4_roots = (
        tuple(item.root for item in loaded_manifest.features)
        if loaded_manifest is not None
        else _claimed_v4_feature_roots(manifest_value)
    )
    # Legacy evidence is authoritative even when a schema-4 file also exists.
    # V4 never migrates, coexists with, or partially reinterprets an older
    # generated layout. Manifest-owned V4 metadata is excluded here so its own
    # parser can report precise corruption diagnostics.
    signals = _legacy_signals(root, active_v4_feature_roots=active_v4_roots)
    if any("v3" in signal.lower() for signal in signals):
        return ExistingGeneration.V3
    if signals:
        return ExistingGeneration.UNSUPPORTED_LEGACY

    if loaded_manifest is not None and not allow_operation_staging:
        # Preserve precise identity/path diagnostics before the strict ownership
        # cross-check. This remains read-only and uses exact-depth no-follow
        # discovery.
        FeatureOperationService(root).records()
        try:
            loaded_manifest = load_integrity_manifest(root)
        except IntegrityManifestError as exc:
            manifest_error = GeneratorError(
                f"V4 integrity manifest is invalid: {manifest}: {exc}",
                kind="invalid_metadata",
                phase="preflight",
            )

    if manifest_kind is not None:
        if manifest_error is not None:
            raise manifest_error
        if (
            isinstance(manifest_value, dict)
            and manifest_value.get("schema_version") in {1, 2, 3}
        ):
            return ExistingGeneration.UNSUPPORTED_LEGACY
        if loaded_manifest is not None:
            return ExistingGeneration.V4
        raise GeneratorError(
            f"V4 integrity manifest has an unsupported schema: {manifest}",
            kind="invalid_metadata",
            phase="preflight",
        )

    if _unmanifested_v4_signals(root):
        # The public boundary rejects this state. It is accepted only inside the
        # already-open first-add transaction while the complete V4 manifest is
        # still being staged.
        return ExistingGeneration.UNMANIFESTED_V4
    return ExistingGeneration.NONE


def reject_unsupported_legacy_project(
    root: Path,
    *,
    generation: ExistingGeneration | None = None,
) -> None:
    """Reject V1/V2/V3 state before any public operation can mutate it."""

    generation = generation or detect_existing_generation(root)
    if generation not in {
        ExistingGeneration.V3,
        ExistingGeneration.UNSUPPORTED_LEGACY,
    }:
        return
    label = "V3" if generation is ExistingGeneration.V3 else "V1/V2/V3"
    signals = _legacy_signals(root)
    details = "\n".join(f"  {item}" for item in signals[:8])
    suffix = f"\n\nDetected legacy state:\n{details}" if details else ""
    raise UnsupportedLegacyProject(
        f"Unsupported legacy Supernote Module Generator project ({label})."
        f"{suffix}\n\n"
        "V4 does not migrate or reinterpret V1, V2, or V3 generated state. "
        "Create a clean V4 plugin and copy only reviewed user-owned source files."
    )


def reject_unsupported_project_state(
    root: Path,
    *,
    generation: ExistingGeneration | None = None,
) -> None:
    """Reject every generated state lacking active V4 manifest authority."""

    generation = generation or detect_existing_generation(root)
    reject_unsupported_legacy_project(root, generation=generation)
    if generation is not ExistingGeneration.UNMANIFESTED_V4:
        return
    signals = _unmanifested_v4_signals(root)
    details = "\n".join(f"  {item}" for item in signals[:8])
    suffix = f"\n\nDetected unmanifested V4 state:\n{details}" if details else ""
    raise UnmanifestedGeneratedProject(
        "V4 generated state exists without a schema-4 integrity manifest."
        f"{suffix}\n\n"
        "The generator cannot prove ownership of this state and will not "
        "reinterpret, repair, replace, or delete it. Create a clean V4 plugin "
        "or restore the exact manifest that owns these generated artifacts."
    )


def assert_public_project(root: Path) -> ExistingGeneration:
    generation = detect_existing_generation(root)
    reject_unsupported_project_state(root, generation=generation)
    return generation


def _unmanifested_v4_signals(root: Path) -> tuple[str, ...]:
    """Find exact current-layout state that requires root-manifest authority."""

    signals: set[str] = set()
    runtime = root / "android/.supernote-module/v4-runtime"
    runtime_kind = contained_entry_kind_no_follow(root, runtime)
    if runtime_kind is not None:
        signals.add(
            f"{runtime.relative_to(root).as_posix()} ({runtime_kind})"
        )

    for record in FeatureOperationService(root).records():
        signals.add(
            f"{record.path.relative_to(root).as_posix()}/.supernote-module.json "
            "(V4 feature metadata)"
        )

    marker_paths = (
        root / "android/settings.gradle",
        root / "android/settings.gradle.kts",
        root / "android/app/build.gradle",
        root / "android/app/build.gradle.kts",
    )
    for path in marker_paths:
        if contained_entry_kind_no_follow(root, path) != "file":
            continue
        content, _metadata = read_contained_regular_bytes_no_follow(root, path)
        if b"supernote-module-v4-runtime" in content:
            signals.add(
                f"{path.relative_to(root).as_posix()} (V4 runtime wiring)"
            )

    application_root = root / "android/app/src/main"
    if contained_entry_kind_no_follow(root, application_root) == "directory":
        validate_contained_path_no_follow(
            root,
            application_root,
            allowed_final_kinds={"directory"},
        )
        for path, kind in contained_tree_entries_no_follow(root, application_root):
            if kind != "file" or path.suffix.lower() not in {".java", ".kt"}:
                continue
            content, _metadata = read_contained_regular_bytes_no_follow(root, path)
            if b"supernote-module-v4-package" in content:
                signals.add(
                    f"{path.relative_to(root).as_posix()} (V4 package wiring)"
                )
    return tuple(sorted(signals))


def _legacy_signals(
    root: Path,
    *,
    active_v4_feature_roots: tuple[str, ...] = (),
) -> tuple[str, ...]:
    signals: set[str] = set()
    active_v4_metadata = {
        f"{feature_root}/.supernote-module.json"
        for feature_root in active_v4_feature_roots
    }
    for version in ("v1", "v2", "v3"):
        relative = f"android/.supernote-module/{version}-runtime"
        if contained_entry_kind_no_follow(root, root / relative) is not None:
            signals.add(relative)

    metadata_names = (
        ".supernote-module.json",
        ".supernote-native-module.json",
        ".rn-legacy-module.json",
    )
    for modules_name in ("local_modules", "local-modules", "modules"):
        modules = root / modules_name
        modules_kind = contained_entry_kind_no_follow(root, modules)
        if modules_kind != "directory":
            continue
        try:
            children = contained_directory_entries_no_follow(root, modules)
        except GeneratorError as exc:
            raise GeneratorError(
                f"legacy project discovery could not read {modules}: {exc}",
                kind="invalid_metadata",
                phase="preflight",
            ) from exc
        for child_name, child_kind in children:
            child = modules / child_name
            if child_name.startswith(".") or child_kind != "directory":
                continue
            package_roots = [child]
            if child_name.startswith("@"):
                try:
                    package_roots = [
                        child / item_name
                        for item_name, item_kind in contained_directory_entries_no_follow(
                            root, child
                        )
                        if not item_name.startswith(".")
                        and item_kind == "directory"
                    ]
                except GeneratorError as exc:
                    raise GeneratorError(
                        f"legacy project discovery could not read {child}: {exc}",
                        kind="invalid_metadata",
                        phase="preflight",
                    ) from exc
            for package_root in package_roots:
                for metadata_name in metadata_names:
                    metadata = package_root / metadata_name
                    metadata_kind = contained_entry_kind_no_follow(root, metadata)
                    if metadata_kind is None:
                        continue
                    label = metadata.relative_to(root).as_posix()
                    value = None
                    if metadata_kind == "file":
                        try:
                            content, _metadata = read_contained_regular_bytes_no_follow(
                                root, metadata
                            )
                            value = json.loads(content.decode("utf-8"))
                        except (OSError, UnicodeDecodeError, ValueError):
                            value = None
                    if isinstance(value, dict) and isinstance(value.get("kind"), str):
                        label += f" ({value['kind']})"
                    if label.split(" (", 1)[0] in active_v4_metadata:
                        if _is_explicit_legacy_feature_metadata(value):
                            signals.add(label)
                        # A claimed V4 metadata path is otherwise left to the
                        # strict V4 manifest/feature parser so malformed V4
                        # input keeps its precise invalid-metadata diagnostic.
                        continue
                    if (
                        modules_name == "local_modules"
                        and metadata_name == ".supernote-module.json"
                        and isinstance(value, dict)
                        and value.get("kind") == "supernote_v4_feature"
                    ):
                        continue
                    signals.add(label)
                for relative in (
                    "android/.native-module",
                    "android/.supernote-module/codegen-config.json",
                    "android/.supernote-module/supernote_codegen",
                ):
                    candidate = package_root / relative
                    if contained_entry_kind_no_follow(root, candidate) is not None:
                        signals.add(
                            f"{candidate.relative_to(root).as_posix()} "
                            "(legacy copied codegen)"
                        )

    marker_paths = (
        root / "android/settings.gradle",
        root / "android/settings.gradle.kts",
        root / "android/app/build.gradle",
        root / "android/app/build.gradle.kts",
    )
    for path in marker_paths:
        if contained_entry_kind_no_follow(root, path) != "file":
            continue
        try:
            content, _metadata = read_contained_regular_bytes_no_follow(root, path)
        except OSError as exc:
            raise GeneratorError(
                f"legacy project discovery could not read {path}: {exc}",
                kind="invalid_metadata",
                phase="preflight",
            ) from exc
        text = content.decode("utf-8", errors="replace")
        for marker in (
            "local-native-module:",
            "rn-legacy-module:",
            "local-kotlin-module:",
        ):
            if marker in text:
                signals.add(
                    f"{path.relative_to(root).as_posix()} ({marker[:-1]} wiring)"
                )
        for version in ("v1", "v2", "v3"):
            if f"supernote-module-{version}" in text or f"supernote-{version}" in text:
                signals.add(f"{path.relative_to(root).as_posix()} ({version} wiring)")
    application_root = root / "android/app/src/main"
    application_kind = contained_entry_kind_no_follow(root, application_root)
    if application_kind is None:
        return tuple(sorted(signals))
    validate_contained_path_no_follow(
        root,
        application_root,
        allowed_final_kinds={"directory"},
    )
    for path, kind in contained_tree_entries_no_follow(root, application_root):
        if kind != "file" or path.suffix.lower() not in {".java", ".kt"}:
            continue
        try:
            content, _metadata = read_contained_regular_bytes_no_follow(root, path)
        except OSError as exc:
            raise GeneratorError(
                f"legacy project discovery could not read {path}: {exc}",
                kind="invalid_metadata",
                phase="preflight",
            ) from exc
        text = content.decode("utf-8", errors="replace")
        for version in ("v1", "v2", "v3"):
            if f"supernote-module-{version}-package" in text:
                signals.add(
                    f"{path.relative_to(root).as_posix()} ({version} package wiring)"
                )
    return tuple(sorted(signals))


def _claimed_v4_feature_roots(manifest: object) -> tuple[str, ...]:
    """Extract non-authoritative roots solely to route preflight diagnostics."""

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 4:
        return ()
    features = manifest.get("features")
    if not isinstance(features, list):
        return ()
    roots: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        value = feature.get("root")
        if (
            isinstance(value, str)
            and value.startswith("local_modules/")
            and "\\" not in value
            and ".." not in Path(value).parts
        ):
            roots.add(value)
    return tuple(sorted(roots))


def _is_explicit_legacy_feature_metadata(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    schema = value.get("schema_version")
    kind = value.get("kind")
    if schema in {1, 2}:
        return True
    if kind in {
        "supernote_v1_feature",
        "supernote_v2_feature",
        "supernote_v3_feature",
    }:
        return True
    return not (
        schema == FEATURE_MANIFEST_SCHEMA_VERSION
        and kind == FEATURE_MANIFEST_KIND
    ) and isinstance(kind, str) and kind.startswith("supernote_v3_")
