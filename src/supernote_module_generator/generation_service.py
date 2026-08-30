"""Assemble and atomically execute one complete V4 GenerationPlan."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import tempfile
from typing import Iterable, Mapping
import hashlib
import stat

from . import __version__
from . import binding_codegen
from .errors import ConfigurationError, FilesystemError
from .conversion import plan_api_conversion
from .cross_family_codegen import build_cross_family_renderer
from .feature_generator import FeatureConfig, stage_feature
from .feature_model import FeatureRegistryEntry, PluginRuntimeRegistry, StarterFamily
from .feature_operations import FeatureOperationService, FeatureRecord
from .frontend_discovery import discover_semantic_ir
from .internal_codegen import internal_header_path, render_cpp_internal_facade
from .jvm_codegen import render_jvm_feature_jsi
from .jvm_manifest import JvmSourceManifest
from .jvm_projection import project_jvm_owners
from .generation_plan import (
    DependencyAction,
    GenerationPlan,
    GenerationPlanError,
    OwnedArtifact,
    PlanConflictError,
    WiringAction,
)
from .generation_execution import GenerationPlanExecutor
from .filesystem import (
    _windows_host,
    contained_directory_entries_no_follow,
    contained_entry_kind_no_follow,
    entry_kind,
    hash_entry_no_follow,
)
from .integrity_manifest import (
    INTEGRITY_MANIFEST_PATH,
    IntegrityManifest,
    IntegrityManifestError,
    LoadedIntegrityManifest,
    ManifestFeature,
    WiringRecord,
    load_integrity_manifest,
)
from .plugin_build_integration import (
    RuntimeWiringSnapshot,
    capture_runtime_wiring_files,
    desired_runtime_wiring_files,
    expected_runtime_wiring_blocks,
    inspect_runtime_wiring_blocks,
)
from .plugin_runtime_codegen import RUNTIME_RELATIVE_ROOT, generated_runtime_files
from .project_model import ProjectModel
from .project import dependency_value, read_parent_package
from .readme_codegen import render_feature_readme
from .semantic import SemanticApi
from .semantic_ir import FeatureSemanticIR, SemanticIR
from .transaction import Transaction
from .typescript_codegen import render_typescript


FEATURE_GENERATED_FILES = (
    ".supernote-module.json",
    "index.d.ts",
    "index.js",
    "package.json",
    "README.md",
)


def _present_jvm_manifest(
    manifest: JvmSourceManifest | None,
) -> JvmSourceManifest | None:
    """Return only manifests that describe a live JVM declaration owner."""

    if manifest is None or manifest.owners:
        return manifest
    return None


class GenerationService:
    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.resolve()

    def plan(
        self,
        *,
        operation: str,
        requested_targets: Iterable[str],
        jvm_apis: Mapping[str, SemanticApi] | None = None,
        jvm_manifests: Mapping[str, JvmSourceManifest] | None = None,
        allow_unmanifested_bootstrap: bool = False,
        removed_targets: Iterable[str] = (),
    ) -> GenerationPlan:
        requested = tuple(requested_targets)
        full_project = ProjectModel.discover(
            self.root,
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        removed = tuple(sorted(set(removed_targets)))
        by_name = {
            feature.identity.npm_name: feature for feature in full_project.features
        }
        unknown_removed = sorted(set(removed) - set(by_name))
        if unknown_removed:
            raise GenerationPlanError(
                "cannot remove unknown feature(s): " + ", ".join(unknown_removed)
            )
        prior_manifest = self._load_prior_manifest(
            full_project,
            operation=operation,
            requested_targets=requested,
        )
        project = replace(
            full_project,
            features=tuple(
                feature
                for feature in full_project.features
                if feature.identity.npm_name not in removed
            ),
        )
        wiring_snapshot = capture_runtime_wiring_files(self.root)
        jvm_manifests = jvm_manifests or {}
        remaining_ids = {feature.identity.feature_id for feature in project.features}
        jvm_manifests = {
            feature_id: manifest
            for feature_id, manifest in jvm_manifests.items()
            if feature_id in remaining_ids
        }
        if jvm_apis is not None:
            jvm_apis = {
                feature_id: api
                for feature_id, api in jvm_apis.items()
                if feature_id in remaining_ids
            }
        if jvm_manifests:
            projected = {
                feature_id: project_jvm_owners(
                    raw.owners, feature_id=feature_id
                )
                for feature_id, raw in jvm_manifests.items()
            }
            if jvm_apis is not None and any(
                jvm_apis.get(key, SemanticApi()).manifest() != value.manifest()
                for key, value in projected.items()
            ):
                raise ValueError("JVM manifest and projected SemanticIR disagree")
            jvm_apis = projected
        semantic_ir = discover_semantic_ir(project, jvm_apis=jvm_apis)
        artifacts = self._render_owned(
            project,
            semantic_ir,
            jvm_manifests=jvm_manifests,
            wiring_snapshot=wiring_snapshot,
        )
        stale = self._stale_owned_paths(
            {item.path for item in artifacts},
            project=full_project,
            manifest=prior_manifest,
        )
        removal_roots = {
            by_name[name].root.relative_to(self.root).as_posix(): f"feature:{name}"
            for name in removed
        }
        runtime_root = self.root / RUNTIME_RELATIVE_ROOT
        if not project.features and entry_kind(runtime_root) == "directory":
            removal_roots[RUNTIME_RELATIVE_ROOT.as_posix()] = "shared-runtime"
        stale = tuple(
            path
            for path in stale
            if not any(
                path == root or path.startswith(root + "/")
                for root in removal_roots
            )
        )
        affected = [feature.identity.npm_name for feature in full_project.features]
        affected.extend(("shared runtime", "plugin wiring", "integrity manifest"))
        wiring_issues = inspect_runtime_wiring_blocks(
            self.root,
            enabled=bool(project.features),
            snapshot=wiring_snapshot,
        )
        try:
            desired_wiring = desired_runtime_wiring_files(
                self.root,
                enabled=bool(project.features),
                snapshot=wiring_snapshot,
            )
        except ConfigurationError:
            if not wiring_issues:
                raise
            desired_wiring = ()
        if desired_wiring:
            desired_by_path = {row.path: row.content for row in desired_wiring}
            planned_wiring_snapshot = RuntimeWiringSnapshot(
                replace(
                    wiring_snapshot.settings,
                    content=desired_by_path.get(
                        wiring_snapshot.settings.path,
                        wiring_snapshot.settings.content,
                    ),
                ),
                replace(
                    wiring_snapshot.app_build,
                    content=desired_by_path.get(
                        wiring_snapshot.app_build.path,
                        wiring_snapshot.app_build.content,
                    ),
                ),
                (
                    replace(
                        wiring_snapshot.application,
                        content=desired_by_path.get(
                            wiring_snapshot.application.path,
                            wiring_snapshot.application.content,
                        ),
                    )
                    if wiring_snapshot.application is not None
                    else None
                ),
            )
            planned_wiring_issues = inspect_runtime_wiring_blocks(
                self.root,
                enabled=bool(project.features),
                snapshot=planned_wiring_snapshot,
            )
            if planned_wiring_issues:
                raise ConfigurationError(
                    "planned V4 wiring is not canonical: "
                    + "; ".join(planned_wiring_issues)
                )
        wiring_actions = tuple(
            WiringAction(
                row.path.relative_to(self.root).as_posix(),
                row.marker,
                row.content,
                row.previous,
                row.previous_mode,
            )
            for row in desired_wiring
            if row.content != row.previous
        )
        dependency_actions = self._dependency_actions(
            project, removed_targets=removed
        )
        preserved_paths = tuple(
            source_root.relative_to(self.root).as_posix()
            for feature in project.features
            for source_root in (feature.native_root, feature.jvm_root)
        )
        semantic_inputs = {
            "package.json",
            *preserved_paths,
            *(
                (feature.root / ".supernote-module.json")
                .relative_to(self.root)
                .as_posix()
                for feature in project.features
            ),
            *(
                (feature.root / "package.json").relative_to(self.root).as_posix()
                for feature in project.features
            ),
            *(
                path.relative_to(self.root).as_posix()
                for path in project.wiring_paths
            ),
            *(
                block.path.relative_to(self.root).as_posix()
                for block in expected_runtime_wiring_blocks(
                    self.root,
                    enabled=bool(project.features),
                    snapshot=wiring_snapshot,
                )
            ),
        }
        discovery_frontier = _feature_discovery_frontier(self.root)
        authorized_tree_removals = self._authorized_tree_removals(
            removal_roots,
            manifest=prior_manifest,
        )
        authority_baselines = {
            path: ("file", digest)
            for path, digest in (
                prior_manifest.authority_hashes if prior_manifest is not None else ()
            )
        }
        return GenerationPlan.compare(
            self.root,
            operation=operation,
            requested_targets=requested,
            affected_targets=affected,
            generation_id=semantic_ir.generation_id,
            artifacts=artifacts,
            deletes=stale,
            dependency_actions=dependency_actions,
            wiring_actions=wiring_actions,
            tree_removals=tuple(removal_roots.items()),
            authorized_tree_removals=authorized_tree_removals,
            wiring_issues=wiring_issues,
            preserved_user_paths=preserved_paths,
            precondition_paths=semantic_inputs,
            precondition_baselines=authority_baselines,
            discovery_frontier=discovery_frontier,
        )

    def _load_prior_manifest(
        self,
        project: ProjectModel,
        *,
        operation: str,
        requested_targets: tuple[str, ...],
    ) -> LoadedIntegrityManifest | None:
        """Capture the one immutable ownership authority used by this plan."""

        manifest_path = self.root / INTEGRITY_MANIFEST_PATH
        if contained_entry_kind_no_follow(self.root, manifest_path) != "file":
            return None
        try:
            manifest = load_integrity_manifest(
                self.root,
                validate_live_ownership=operation != "add",
            )
        except IntegrityManifestError as exc:
            raise GenerationPlanError(
                f"{INTEGRITY_MANIFEST_PATH}: invalid prior integrity manifest: {exc}"
            ) from exc
        if manifest.plugin_id != project.plugin_id:
            raise GenerationPlanError(
                f"{INTEGRITY_MANIFEST_PATH}: plugin identity does not match the project"
            )
        manifest_features = {
            item.package_name: (item.feature_id, item.root)
            for item in manifest.features
        }
        project_features = {
            feature.identity.npm_name: (
                feature.identity.feature_id,
                feature.root.relative_to(self.root).as_posix(),
            )
            for feature in project.features
        }
        expected_prior_features = dict(project_features)
        if operation == "add":
            for package_name in requested_targets:
                expected_prior_features.pop(package_name, None)
        if manifest_features != expected_prior_features:
            raise GenerationPlanError(
                f"{INTEGRITY_MANIFEST_PATH}: feature identities do not match the project"
            )
        return manifest

    def _authorized_tree_removals(
        self,
        removal_roots: Mapping[str, str],
        *,
        manifest: LoadedIntegrityManifest | None,
    ) -> tuple[str, ...]:
        """Authorize whole-tree deletion only from the active root manifest."""

        if not removal_roots:
            return ()
        if manifest is None:
            return ()
        manifest_features = {
            item.package_name: (item.feature_id, item.root)
            for item in manifest.features
        }
        authorized: set[str] = set()
        for relative, owner in removal_roots.items():
            if owner.startswith("feature:"):
                package_name = owner.removeprefix("feature:")
                if (
                    manifest_features.get(package_name, (None, None))[1] == relative
                    and any(
                        item.path == f"{relative}/.supernote-module.json"
                        and item.owner == owner
                        and item.kind == "feature-metadata"
                        for item in manifest.artifacts
                    )
                ):
                    authorized.add(relative)
                continue
            if owner == "shared-runtime" and any(
                item.owner == "shared-runtime"
                and item.path.startswith(
                    RUNTIME_RELATIVE_ROOT.as_posix() + "/"
                )
                for item in manifest.artifacts
            ):
                authorized.add(relative)
        return tuple(sorted(authorized))

    def execute(
        self,
        plan: GenerationPlan,
        transaction: Transaction,
        *,
        commit: bool = True,
    ) -> None:
        """Stage, verify, and commit all file-level plan changes once."""

        GenerationPlanExecutor(
            self.root,
            validate_preconditions=self.validate_preconditions,
            validate_path_precondition=self._validate_path_precondition,
        ).execute(plan, transaction, commit=commit)

    def validate_preconditions(self, plan: GenerationPlan) -> None:
        """Reject plan-to-execution races before the first visible write."""

        live_frontier = _feature_discovery_frontier(self.root)
        if live_frontier != plan.discovery_frontier:
            raise PlanConflictError(
                "project feature discovery changed after planning: local_modules",
                preserve_directory_paths=_frontier_changed_directories(
                    plan.discovery_frontier,
                    live_frontier,
                ),
            )
        for precondition in plan.preconditions:
            self._validate_path_precondition(plan, precondition.path)
        self._validate_edit_preconditions(
            "dependency",
            plan.dependency_actions,
        )
        self._validate_edit_preconditions("wiring", plan.wiring_actions)

    def _validate_edit_preconditions(
        self,
        label: str,
        actions: Iterable[DependencyAction | WiringAction],
    ) -> None:
        for action in actions:
            destination = self.root.joinpath(*PurePosixPath(action.path).parts)
            if entry_kind(destination) != "file":
                raise PlanConflictError(
                    f"{label} destination changed after planning: {action.path}"
                )
            if destination.read_bytes() != action.previous:
                raise PlanConflictError(
                    f"{label} file changed after planning: {action.path}"
                )
            mode_matches = (
                (destination.stat().st_mode & stat.S_IWRITE)
                == (action.previous_mode & stat.S_IWRITE)
                if _windows_host()
                else stat.S_IMODE(destination.stat().st_mode) == action.previous_mode
            )
            if not mode_matches:
                raise PlanConflictError(
                    f"{label} file mode changed after planning: {action.path}"
                )

    def _validate_path_precondition(
        self,
        plan: GenerationPlan,
        relative: str,
    ) -> None:
        """Revalidate one immutable plan authority at its mutation boundary."""

        precondition = next(
            (item for item in plan.preconditions if item.path == relative),
            None,
        )
        if precondition is None:
            raise PlanConflictError(
                f"project plan lacks mutation authority: {relative}"
            )
        destination = self.root.joinpath(*PurePosixPath(relative).parts)
        live_kind = entry_kind(destination)
        live_hash = hash_entry_no_follow(destination)
        if live_kind != precondition.kind or live_hash != precondition.sha256:
            raise PlanConflictError(
                f"project state changed after planning: {relative}"
            )

    def _dependency_actions(
        self,
        project: ProjectModel,
        *,
        removed_targets: Iterable[str] = (),
    ) -> tuple[DependencyAction, ...]:
        package_path, package = read_parent_package(self.root)
        dependencies = package.get("dependencies")
        if dependencies is None:
            dependencies = {}
            package["dependencies"] = dependencies
        if not isinstance(dependencies, dict):
            raise GenerationPlanError("package.json dependencies must be an object")
        stale = []
        for npm_name in sorted(set(removed_targets)):
            if npm_name in dependencies:
                del dependencies[npm_name]
                stale.append(npm_name)
        for feature in project.features:
            npm_name = feature.identity.npm_name
            expected = dependency_value(npm_name)
            if dependencies.get(npm_name) != expected:
                dependencies[npm_name] = expected
                stale.append(npm_name)
        if not stale:
            return ()
        previous = package_path.read_bytes()
        previous_mode = stat.S_IMODE(package_path.stat().st_mode)
        content = (json.dumps(package, indent=2) + "\n").encode("utf-8")
        return (
            DependencyAction(
                package_path.relative_to(self.root).as_posix(),
                tuple(sorted(stale)),
                content,
                previous,
                previous_mode,
            ),
        )

    def _render_owned(
        self,
        project: ProjectModel,
        semantic_ir: SemanticIR,
        *,
        jvm_manifests: Mapping[str, JvmSourceManifest],
        wiring_snapshot: RuntimeWiringSnapshot,
    ) -> tuple[OwnedArtifact, ...]:
        service = FeatureOperationService(self.root)
        by_id = {
            feature.identity.feature_id: feature for feature in semantic_ir.features
        }
        artifacts: list[OwnedArtifact] = []
        registry_entries = []
        manifest_features = []
        runtime_semantics: list[
            tuple[FeatureRecord, FeatureSemanticIR, JvmSourceManifest | None]
        ] = []
        for project_feature in project.features:
            record = service.find_record(project_feature.identity.npm_name)
            semantic = by_id[project_feature.identity.feature_id]
            with tempfile.TemporaryDirectory(
                prefix="supernote-v4-render-"
            ) as raw_render_root:
                config = FeatureConfig(
                    output=Path(raw_render_root) / "feature",
                    npm_name=record.manifest.npm_name,
                    package_version=record.package_version,
                    android_namespace=record.manifest.android_namespace,
                    public_name=record.manifest.public_name,
                    description=record.description,
                    starters=(StarterFamily.NATIVE, StarterFamily.JVM),
                )
                staged = stage_feature(config, preserve_sources_from=record.path)
                contents = {
                    relative: (staged / relative).read_bytes()
                    for relative in FEATURE_GENERATED_FILES
                }
            contents["index.d.ts"] = render_typescript(
                record.manifest.public_name, semantic.merged
            ).encode("utf-8")
            implementation_roots = []
            if project_feature.native_root.is_dir():
                implementation_roots.append(
                    ("C/C++", record.manifest.roots.native + "/")
                )
            if project_feature.jvm_root.is_dir():
                implementation_roots.append(
                    ("Kotlin/Java", record.manifest.roots.jvm + "/")
                )
            contents["README.md"] = render_feature_readme(
                npm_name=record.manifest.npm_name,
                public_name=record.manifest.public_name,
                description=record.description,
                generator_version=__version__,
                implementation_roots=tuple(implementation_roots),
                api=semantic.merged,
            ).encode("utf-8")
            feature_root = record.path.relative_to(self.root).as_posix()
            for relative, content in sorted(contents.items()):
                artifacts.append(
                    OwnedArtifact(
                        f"{feature_root}/{relative}",
                        f"feature:{record.manifest.npm_name}",
                        _feature_kind(relative),
                        content,
                        semantic_ir.generation_id,
                    )
                )
            registry_entries.append(
                FeatureRegistryEntry.create(record.manifest, semantic.merged)
            )
            runtime_semantics.append(
                (record, semantic, jvm_manifests.get(record.manifest.feature_id))
            )
            manifest_features.append(
                ManifestFeature(
                    record.manifest.feature_id,
                    record.manifest.npm_name,
                    feature_root,
                    semantic.semantic_hash,
                )
            )
        if not project.features:
            integrity = IntegrityManifest.create(
                generator_version=__version__,
                generation_id=semantic_ir.generation_id,
                plugin_id=project.plugin_id,
                features=(),
                artifacts=(),
                wiring=(),
            )
            return (
                OwnedArtifact(
                    INTEGRITY_MANIFEST_PATH,
                    "plugin-global",
                    "integrity-manifest",
                    integrity.render(),
                    semantic_ir.generation_id,
                ),
            )
        registry = PluginRuntimeRegistry.create(
            plugin_id=project.plugin_id,
            generator_version=__version__,
            features=registry_entries,
        )
        runtime_files = generated_runtime_files(registry)
        generated_relative: list[str] = []
        feature_ids = []
        jvm_feature_ids = []
        for raw_record, raw_semantic, source_manifest in runtime_semantics:
            record = raw_record
            semantic = raw_semantic
            # KSP deliberately emits an authoritative empty manifest when a
            # feature has no remaining JVM declarations.  It must clear stale
            # JVM semantics without creating an otherwise empty JNI route.
            source_manifest = _present_jvm_manifest(source_manifest)
            feature_id = record.manifest.feature_id
            feature_ids.append(feature_id)
            suffix = feature_id.removeprefix("supernote:feature:")
            conversion = plan_api_conversion(semantic.merged)
            conversion_json = conversion.manifest()
            conversion_encoded = json.dumps(
                conversion_json,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            conversion_digest = hashlib.sha256(conversion_encoded).hexdigest()
            semantic_path = f"generated/semantics/{suffix}.json"
            conversion_path = f"generated/conversions/{suffix}.json"
            feature_jni = f"generated/jni/feature_{suffix}.cpp"
            internal_jni = f"generated/jni/internal_{suffix}.cpp"
            runtime_files[semantic_path] = (
                json.dumps(semantic.merged.manifest(), indent=2, sort_keys=True)
                + "\n"
            )
            runtime_files[conversion_path] = (
                json.dumps(conversion_json, indent=2, sort_keys=True) + "\n"
            )
            runtime_files[feature_jni] = binding_codegen.render_v4_feature_jsi(
                record.path,
                module_name=record.manifest.public_name,
                feature_id=feature_id,
                conversion_digest=conversion_digest,
                include_prefix=(
                    f"{record.manifest.npm_name}/"
                    f"{record.manifest.roots.native}"
                ),
            )
            cross_family = None
            if source_manifest is not None and (
                record.path / record.manifest.roots.native
            ).is_dir():
                cross_family = build_cross_family_renderer(
                    record.path,
                    semantic.merged,
                    source_manifest,
                    feature_id=feature_id,
                    module_name=record.manifest.public_name,
                )
            header, source = render_cpp_internal_facade(
                record.path,
                module_name=record.manifest.public_name,
                feature_id=feature_id,
                jvm_manifest=source_manifest,
                jvm_semantic=(semantic.jvm if source_manifest is not None else None),
                cross_family=cross_family,
                include_prefix=(
                    f"{record.manifest.npm_name}/"
                    f"{record.manifest.roots.native}"
                ),
            )
            header_path = internal_header_path(feature_id)
            runtime_files[header_path] = header
            runtime_files[internal_jni] = source
            generated_relative.extend(
                (semantic_path, conversion_path, feature_jni, internal_jni, header_path)
            )
            if source_manifest is not None:
                jvm_jni = f"generated/jni/jvm_feature_{suffix}.cpp"
                runtime_files[jvm_jni] = render_jvm_feature_jsi(
                    source_manifest,
                    semantic.jvm,
                    feature_id=feature_id,
                    module_name=record.manifest.public_name,
                    conversion_digest=conversion_digest,
                    cross_family=cross_family,
                )
                generated_relative.append(jvm_jni)
                jvm_feature_ids.append(feature_id)
        plugin_jni = "generated/jni/plugin_bindings.cpp"
        runtime_files[plugin_jni] = binding_codegen.render_v4_plugin_jsi(
            feature_ids, jvm_feature_ids=jvm_feature_ids
        )
        generated_relative.append(plugin_jni)
        ownership = json.loads(runtime_files["ownership.json"])
        ownership["generated_files"] = sorted(
            set(ownership["generated_files"]) | set(generated_relative)
        )
        runtime_files["ownership.json"] = (
            json.dumps(ownership, indent=2, sort_keys=True) + "\n"
        )
        runtime_root = RUNTIME_RELATIVE_ROOT.as_posix()
        for relative, runtime_content in sorted(runtime_files.items()):
            artifacts.append(
                OwnedArtifact(
                    f"{runtime_root}/{relative}",
                    "shared-runtime",
                    _runtime_kind(relative),
                    runtime_content.encode("utf-8"),
                    semantic_ir.generation_id,
                    expected_mode=(0o755 if relative == "common_codegen.py" else None),
                )
            )
        integrity = IntegrityManifest.create(
            generator_version=__version__,
            generation_id=semantic_ir.generation_id,
            plugin_id=project.plugin_id,
            features=manifest_features,
            artifacts=artifacts,
            wiring=(
                WiringRecord(
                    block.path.relative_to(self.root).as_posix(),
                    block.marker,
                    hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
                )
                for block in expected_runtime_wiring_blocks(
                    self.root,
                    enabled=bool(project.features),
                    snapshot=wiring_snapshot,
                )
            ),
        )
        artifacts.append(
            OwnedArtifact(
                INTEGRITY_MANIFEST_PATH,
                "plugin-global",
                "integrity-manifest",
                integrity.render(),
                semantic_ir.generation_id,
            )
        )
        return tuple(artifacts)

    def _stale_owned_paths(
        self,
        expected: set[str],
        *,
        project: ProjectModel,
        manifest: LoadedIntegrityManifest | None,
    ) -> tuple[str, ...]:
        candidates: set[str] = set()
        if manifest is not None:
            feature_roots = {
                feature.identity.npm_name: feature.root.relative_to(self.root).as_posix()
                for feature in project.features
            }
            for item in manifest.artifacts:
                _validate_prior_artifact(item.path, item.owner, feature_roots)
                candidates.add(item.path)
        return tuple(sorted(candidates - expected))


def _feature_discovery_frontier(root: Path) -> tuple[str, ...]:
    """Fingerprint exact supported feature-discovery depths without following links."""

    local_modules = root / "local_modules"
    root_kind = entry_kind(local_modules)
    rows = [f"local_modules|{root_kind or 'missing'}"]
    if root_kind != "directory":
        return tuple(rows)

    def children(directory: Path) -> list[tuple[str, str]]:
        try:
            return list(contained_directory_entries_no_follow(root, directory))
        except (FilesystemError, OSError) as exc:
            raise GenerationPlanError(
                f"cannot inspect feature discovery frontier {directory}: {exc}"
            ) from exc

    def record_candidate(path: Path, relative: str, kind: str) -> None:
        rows.append(f"{relative}|{kind or 'missing'}")
        if kind != "directory":
            return
        manifest = path / ".supernote-module.json"
        rows.append(
            f"{relative}/.supernote-module.json|"
            f"{entry_kind(manifest) or 'missing'}|"
            f"{hash_entry_no_follow(manifest) or '-'}"
        )

    for name, kind in children(local_modules):
        path = local_modules / name
        relative = f"local_modules/{name}"
        if name.startswith("@"):
            rows.append(f"{relative}|{kind or 'missing'}")
            if kind == "directory":
                for package_name, package_kind in children(path):
                    package_path = path / package_name
                    record_candidate(
                        package_path,
                        f"{relative}/{package_name}",
                        package_kind,
                    )
            continue
        record_candidate(path, relative, kind)
    return tuple(rows)


def _frontier_changed_directories(
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> tuple[str, ...]:
    """Return directories whose membership metadata belongs to an external race."""

    before_by_path = {row.split("|", 1)[0]: row for row in before}
    after_by_path = {row.split("|", 1)[0]: row for row in after}
    changed: set[str] = set()
    for path in set(before_by_path) | set(after_by_path):
        if before_by_path.get(path) == after_by_path.get(path):
            continue
        parsed = PurePosixPath(path)
        parent = parsed.parent.as_posix()
        if parent == ".":
            parent = "."
        changed.add(parent)
    return tuple(sorted(changed))


def _feature_kind(relative: str) -> str:
    return {
        ".supernote-module.json": "feature-metadata",
        "index.d.ts": "typescript-declarations",
        "index.js": "javascript-wrapper",
        "package.json": "package-metadata",
        "README.md": "generated-readme",
    }[relative]


def _runtime_kind(relative: str) -> str:
    if relative.endswith((".cpp", ".c", ".hpp", ".h")):
        return "shared-native-runtime"
    if relative.endswith((".gradle", ".kts")):
        return "gradle-integration"
    if relative.endswith("CMakeLists.txt"):
        return "cmake-integration"
    if relative.endswith(".json"):
        return "runtime-metadata"
    return "runtime-support"


def _validate_leaf_relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise GenerationPlanError(f"{label} must be a string")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or windows.drive
        or windows.root
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise GenerationPlanError(f"{label} must be canonical and relative: {value!r}")
    return value


def _validate_prior_artifact(
    path: str, owner: str, feature_roots: Mapping[str, str]
) -> None:
    _validate_leaf_relative(path, "prior artifact path")
    runtime_prefix = RUNTIME_RELATIVE_ROOT.as_posix() + "/"
    if owner == "shared-runtime":
        if not path.startswith(runtime_prefix) or path == runtime_prefix[:-1]:
            raise GenerationPlanError(
                f"prior shared-runtime artifact is outside its managed root: {path!r}"
            )
        return
    if owner.startswith("feature:"):
        npm_name = owner.removeprefix("feature:")
        feature_root = feature_roots.get(npm_name)
        if feature_root is None:
            raise GenerationPlanError(
                f"prior artifact has unknown feature owner {owner!r}"
            )
        allowed = {f"{feature_root}/{relative}" for relative in FEATURE_GENERATED_FILES}
        if path not in allowed:
            raise GenerationPlanError(
                f"prior feature artifact is not a generator-owned leaf: {path!r}"
            )
        return
    if owner == "plugin-global" and path == INTEGRITY_MANIFEST_PATH:
        return
    raise GenerationPlanError(
        f"prior artifact has unsupported ownership {owner!r}: {path!r}"
    )
