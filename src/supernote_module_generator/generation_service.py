"""Assemble and atomically execute one complete GenerationPlan."""
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
from .errors import FilesystemError
from .conversion import plan_api_conversion
from .cross_family_codegen import build_cross_family_renderer
from .feature_generator import (
    GENERATED_FILES,
    GENERATED_PAYLOAD_FILES,
    GENERATED_ROOT,
    FeatureConfig,
    stage_feature,
)
from .feature_model import FeatureRegistryEntry, PluginRuntimeRegistry, StarterFamily
from .feature_operations import (
    FeatureOperationService,
    FeatureRecord,
    FeatureSourceError,
)
from .frontend_discovery import discover_semantic_ir
from .internal_codegen import render_cpp_internal_facade
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
    iter_tree_no_follow,
    read_regular_bytes_no_follow,
    source_tree_inventory,
)
from .integrity_manifest import (
    INTEGRITY_MANIFEST_PATH,
    IntegrityManifest,
    IntegrityManifestError,
    LoadedIntegrityManifest,
    ManifestFeature,
    load_integrity_manifest,
)
from .plugin_runtime_codegen import RUNTIME_RELATIVE_ROOT, generated_runtime_files
from .npm_distribution import (
    PACKAGE_CMAKE_PATH,
    PACKAGE_MANIFEST_PATH,
    PACKAGE_NATIVE_REGISTRATION_HEADER_PATH,
    PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH,
    jvm_adapter_path,
    render_package_manifest,
    render_registration,
)
from .package_integration_codegen import (
    render_jvm_package_registration,
    render_package_cmake,
    render_package_registration,
)
from .project_model import ProjectModel
from .readme_codegen import render_feature_readme
from .semantic import SemanticApi
from .semantic_ir import FeatureSemanticIR, SemanticIR, SemanticIRError
from .typescript_codegen import render_typescript


FEATURE_GENERATED_FILES = GENERATED_FILES


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
        jvm_adapter_sources: Mapping[str, bytes] | None = None,
        allow_unmanifested_bootstrap: bool = False,
        removed_targets: Iterable[str] = (),
    ) -> GenerationPlan:
        author_inputs_before = _author_input_inventory(self.root)
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
        jvm_manifests = jvm_manifests or {}
        jvm_adapter_sources = jvm_adapter_sources or {}
        remaining_ids = {feature.identity.feature_id for feature in project.features}
        jvm_manifests = {
            feature_id: manifest
            for feature_id, manifest in jvm_manifests.items()
            if feature_id in remaining_ids
        }
        jvm_adapter_sources = {
            feature_id: content
            for feature_id, content in jvm_adapter_sources.items()
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
        try:
            semantic_ir = discover_semantic_ir(project, jvm_apis=jvm_apis)
        except SemanticIRError as exc:
            raise FeatureSourceError(str(exc)) from exc
        artifacts = self._render_owned(
            project,
            semantic_ir,
            jvm_manifests=jvm_manifests,
            jvm_adapter_sources=jvm_adapter_sources,
        )
        author_inputs_after = _author_input_inventory(self.root)
        if author_inputs_after != author_inputs_before:
            raise PlanConflictError(
                "authored project state changed while the generation plan was rendered"
            )
        stale = self._stale_owned_paths(
            {item.path for item in artifacts},
            project=full_project,
            manifest=prior_manifest,
        )
        removal_roots: dict[str, str] = {}
        affected = [feature.identity.npm_name for feature in full_project.features]
        affected.extend(("shared runtime", "integrity manifest"))
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
        }
        immutable_inputs = {
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
            dependency_actions=(),
            wiring_actions=(),
            tree_removals=tuple(removal_roots.items()),
            authorized_tree_removals=authorized_tree_removals,
            wiring_issues=(),
            preserved_user_paths=preserved_paths,
            precondition_paths=semantic_inputs,
            precondition_baselines=authority_baselines,
            discovery_frontier=discovery_frontier,
            immutable_inputs=immutable_inputs,
            author_input_inventory=author_inputs_before,
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
                validate_live_ownership=False,
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
        if operation == "add":
            if manifest_features != expected_prior_features:
                raise GenerationPlanError(
                    f"{INTEGRITY_MANIFEST_PATH}: feature identities do not match the project"
                )
        else:
            for package_name, identity in project_features.items():
                prior = manifest_features.get(package_name)
                if prior is not None and prior != identity:
                    raise GenerationPlanError(
                        f"{INTEGRITY_MANIFEST_PATH}: feature identity changed for "
                        f"{package_name}"
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
        *,
        finalize: bool = True,
    ) -> None:
        """Stage and publish generated files, recording completion last."""

        GenerationPlanExecutor(
            self.root,
            validate_preconditions=self.validate_preconditions,
            validate_path_precondition=self._validate_path_precondition,
            validate_completion_preconditions=self.validate_completion_preconditions,
        ).execute(plan, finalize=finalize)

    def validate_preconditions(self, plan: GenerationPlan) -> None:
        """Reject plan-to-execution races before the first visible write."""

        self._validate_author_inputs(plan, phase="after planning")
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

    def validate_completion_preconditions(self, plan: GenerationPlan) -> None:
        """Recheck authored inputs before recording a generation as complete."""

        self._validate_author_inputs(plan, phase="during publication")
        live_frontier = _feature_discovery_frontier(self.root)
        if live_frontier != plan.discovery_frontier:
            raise PlanConflictError(
                "project feature discovery changed during publication: local_modules",
                preserve_directory_paths=_frontier_changed_directories(
                    plan.discovery_frontier,
                    live_frontier,
                ),
            )
        for relative in plan.immutable_inputs:
            self._validate_path_precondition(plan, relative)

    def _validate_author_inputs(self, plan: GenerationPlan, *, phase: str) -> None:
        live = _author_input_inventory(self.root)
        if live != plan.author_input_inventory:
            raise PlanConflictError(f"authored project state changed {phase}")

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

    def _render_owned(
        self,
        project: ProjectModel,
        semantic_ir: SemanticIR,
        *,
        jvm_manifests: Mapping[str, JvmSourceManifest],
        jvm_adapter_sources: Mapping[str, bytes],
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
                prefix="sn-module-gen-render-"
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
                staged = stage_feature(config)
                contents = {
                    relative: (staged / relative).read_bytes()
                    for relative in GENERATED_PAYLOAD_FILES
                }
            contents[f"{GENERATED_ROOT}/index.d.ts"] = render_typescript(
                record.manifest.public_name, semantic.merged
            ).encode("utf-8")
            authored_package_payload = _authored_package_payload(record)
            has_native_sources = _payload_contains_files_under(
                authored_package_payload,
                record.manifest.roots.native,
            )
            has_jvm_sources = _payload_contains_files_under(
                authored_package_payload,
                record.manifest.roots.jvm,
            )
            implementation_roots = []
            if has_native_sources:
                implementation_roots.append(
                    ("C/C++", record.manifest.roots.native + "/")
                )
            if has_jvm_sources:
                implementation_roots.append(
                    ("Kotlin/Java", record.manifest.roots.jvm + "/")
                )
            contents[f"{GENERATED_ROOT}/README.md"] = render_feature_readme(
                npm_name=record.manifest.npm_name,
                public_name=record.manifest.public_name,
                description=record.description,
                generator_version=__version__,
                implementation_roots=tuple(implementation_roots),
                api=semantic.merged,
            ).encode("utf-8")
            source_manifest = _present_jvm_manifest(
                jvm_manifests.get(record.manifest.feature_id)
            )
            contents.update(
                _render_package_bindings(
                    record,
                    semantic,
                    source_manifest,
                    jvm_adapter_source=jvm_adapter_sources.get(
                        record.manifest.feature_id
                    ),
                    has_native_sources=has_native_sources,
                    has_jvm_sources=has_jvm_sources,
                )
            )
            package_payload = {
                **authored_package_payload,
                **contents,
            }
            contents[PACKAGE_MANIFEST_PATH] = render_package_manifest(
                record, package_payload
            )
            feature_root = record.path.relative_to(self.root).as_posix()
            generated_files = tuple(
                sorted((*contents, f"{GENERATED_ROOT}/ownership.json"))
            )
            contents[f"{GENERATED_ROOT}/ownership.json"] = (
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "kind": "supernote-feature-generated-ownership",
                        "feature_id": record.manifest.feature_id,
                        "package_name": record.manifest.npm_name,
                        "generator_version": __version__,
                        "generation_id": semantic_ir.generation_id,
                        "generated_files": list(generated_files),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
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
        for record, _semantic, source_manifest in runtime_semantics:
            # KSP deliberately emits an authoritative empty manifest when a
            # feature has no remaining JVM declarations.  It must clear stale
            # JVM semantics without creating an otherwise empty JNI route.
            source_manifest = _present_jvm_manifest(source_manifest)
            feature_id = record.manifest.feature_id
            feature_ids.append(feature_id)
            if source_manifest is not None:
                jvm_feature_ids.append(feature_id)
        plugin_jni = "generated/jni/plugin_bindings.cpp"
        runtime_files[plugin_jni] = binding_codegen.render_plugin_jsi(
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
            wiring=(),
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
            feature_roots.update(
                {feature.package_name: feature.root for feature in manifest.features}
            )
            for item in manifest.artifacts:
                _validate_prior_artifact(item.path, item.owner, feature_roots)
                candidates.add(item.path)
        return tuple(sorted(candidates - expected))


def _author_input_inventory(
    root: Path,
) -> tuple[tuple[str, tuple[str, int, int, str | None]], ...]:
    """Capture all non-generated project state before semantic discovery."""

    return tuple(source_tree_inventory(root).items())


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


def _render_package_bindings(
    record: FeatureRecord,
    semantic: FeatureSemanticIR,
    source_manifest: JvmSourceManifest | None,
    *,
    jvm_adapter_source: bytes | None,
    has_native_sources: bool,
    has_jvm_sources: bool,
) -> dict[str, bytes]:
    """Render the complete feature-owned binding set shipped by npm."""

    feature_id = record.manifest.feature_id
    has_jvm_bindings = source_manifest is not None
    if has_jvm_bindings and jvm_adapter_source is None:
        raise GenerationPlanError(
            "JVM bindings require the adapter source emitted by the same KSP run: "
            + record.manifest.npm_name
        )
    if not has_jvm_bindings:
        jvm_adapter_source = None
    conversion = plan_api_conversion(semantic.merged)
    conversion_json = conversion.manifest()
    conversion_encoded = json.dumps(
        conversion_json,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    conversion_digest = hashlib.sha256(conversion_encoded).hexdigest()
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
    header, internal = render_cpp_internal_facade(
        record.path,
        module_name=record.manifest.public_name,
        feature_id=feature_id,
        jvm_manifest=source_manifest,
        jvm_semantic=(semantic.jvm if source_manifest is not None else None),
        cross_family=cross_family,
        include_prefix=record.manifest.roots.native,
        header_include="internal.hpp",
    )
    registration_header, registration_source = render_package_registration(
        feature_id=feature_id,
        package_name=record.manifest.npm_name,
        public_name=record.manifest.public_name,
        has_jvm_installer=has_jvm_bindings,
    )
    jvm_registration_path, jvm_registration = render_jvm_package_registration(
        feature_id=feature_id,
        package_name=record.manifest.npm_name,
        public_name=record.manifest.public_name,
    )
    binding_source_names = ["feature.cpp", "internal.cpp", "package_registration.cpp"]
    if has_jvm_bindings:
        binding_source_names.append("jvm_feature.cpp")
    result = {
        PACKAGE_CMAKE_PATH: render_package_cmake(
            feature_id=feature_id,
            binding_sources=tuple(binding_source_names),
        ).encode("utf-8"),
        f"{GENERATED_ROOT}/android/semantic.json": (
            json.dumps(semantic.merged.manifest(), indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        f"{GENERATED_ROOT}/android/conversion.json": (
            json.dumps(conversion_json, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        f"{GENERATED_ROOT}/android/feature.cpp": binding_codegen.render_feature_jsi(
            record.path,
            module_name=record.manifest.public_name,
            feature_id=feature_id,
            conversion_digest=conversion_digest,
            include_prefix=record.manifest.roots.native,
        ).encode("utf-8"),
        f"{GENERATED_ROOT}/android/internal.cpp": internal.encode("utf-8"),
        f"{GENERATED_ROOT}/android/internal.hpp": header.encode("utf-8"),
        jvm_registration_path: jvm_registration.encode("utf-8"),
        PACKAGE_NATIVE_REGISTRATION_HEADER_PATH: registration_header.encode("utf-8"),
        PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH: registration_source.encode("utf-8"),
        f"{GENERATED_ROOT}/android/registration.json": render_registration(
            record,
            has_native_sources=has_native_sources,
            has_jvm_sources=has_jvm_sources,
            has_jvm_bindings=has_jvm_bindings,
            has_jvm_adapter=has_jvm_bindings,
        ),
    }
    if source_manifest is not None and jvm_adapter_source is not None:
        result[jvm_adapter_path(feature_id)] = jvm_adapter_source
        result[f"{GENERATED_ROOT}/android/jvm_feature.cpp"] = (
            render_jvm_feature_jsi(
                source_manifest,
                semantic.jvm,
                feature_id=feature_id,
                module_name=record.manifest.public_name,
                conversion_digest=conversion_digest,
                cross_family=cross_family,
            ).encode("utf-8")
        )
    return result


def _authored_package_payload(record: FeatureRecord) -> dict[str, bytes]:
    """Read the author-owned bytes that npm consumers compile or interpret."""

    paths = [record.path / "package.json", record.path / ".supernote-module.json"]
    cmake = record.path / "CMakeLists.txt"
    if entry_kind(cmake) == "file":
        paths.append(cmake)
    for source_root in (
        record.path / record.manifest.roots.native,
        record.path / record.manifest.roots.jvm,
    ):
        for path in iter_tree_no_follow(source_root):
            if entry_kind(path) == "file":
                paths.append(path)
    result: dict[str, bytes] = {}
    for path in sorted(set(paths)):
        content, _metadata = read_regular_bytes_no_follow(path)
        result[path.relative_to(record.path).as_posix()] = content
    return result


def _payload_contains_files_under(
    payload: Mapping[str, bytes],
    root: str,
) -> bool:
    """Report whether the exact packable payload contains this source family."""

    prefix = root.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in payload)


def _feature_kind(relative: str) -> str:
    known = {
        f"{GENERATED_ROOT}/ownership.json": "feature-generated-metadata",
        f"{GENERATED_ROOT}/package-manifest.json": "package-distribution-metadata",
        f"{GENERATED_ROOT}/index.d.ts": "typescript-declarations",
        f"{GENERATED_ROOT}/index.js": "javascript-wrapper",
        f"{GENERATED_ROOT}/README.md": "generated-readme",
    }
    if relative in known:
        return known[relative]
    if relative.endswith("CMakeLists.txt"):
        return "package-cmake-integration"
    if relative.endswith(".java"):
        return "package-jvm-registration"
    if relative.endswith(".kt"):
        return "package-jvm-adapter"
    if relative.endswith((".cpp", ".hpp")):
        return "package-native-binding"
    if relative.endswith(".json"):
        return "package-registration-metadata"
    raise GenerationPlanError(f"unknown generated feature artifact: {relative}")


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
