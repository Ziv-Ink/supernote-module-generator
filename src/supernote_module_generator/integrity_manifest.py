"""Root V4 integrity manifest rendering and strict loading."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Dict, Iterable, Optional, Tuple

from .feature_identity import canonical_feature_id
from .filesystem import read_contained_regular_bytes_no_follow
from .generation_plan import OwnedArtifact
from .naming import NPM_NAME
from .semantic_ir import CPP_FRONTEND_VERSION, JVM_FRONTEND_VERSION
from .v4_schemas import (
    FEATURE_MANIFEST_KIND,
    FEATURE_MANIFEST_SCHEMA_VERSION,
    GENERATED_OWNERSHIP_KIND,
    GENERATED_OWNERSHIP_SCHEMA_VERSION,
)


INTEGRITY_MANIFEST_SCHEMA_VERSION = 4
INTEGRITY_MANIFEST_PATH = ".supernote-module/manifest.json"
V4_RUNTIME_ROOT = "android/.supernote-module/v4-runtime"
TEMPLATE_CAPABILITY_VERSION = "launch-verification-v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_FEATURE_GENERATED_FILES = (
    ".supernote-module.json",
    "index.d.ts",
    "index.js",
    "package.json",
    "README.md",
)


class IntegrityManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestArtifactRecord:
    path: str
    owner: str
    kind: str
    sha256: str
    generation_id: str
    committed_source: bool
    mode: int | None = None

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "path": self.path,
            "owner": self.owner,
            "kind": self.kind,
            "sha256": self.sha256,
            "generation_id": self.generation_id,
            "committed_source": self.committed_source,
        }
        if self.mode is not None:
            value["mode"] = self.mode
        return value


@dataclass(frozen=True)
class LoadedIntegrityManifest:
    generator_version: str
    generation_id: str
    plugin_id: str
    features: Tuple["ManifestFeature", ...]
    artifacts: Tuple[ManifestArtifactRecord, ...]
    wiring: Tuple["WiringRecord", ...]
    template_capability: str
    authority_hashes: Tuple[Tuple[str, str], ...] = ()

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": INTEGRITY_MANIFEST_SCHEMA_VERSION,
            "generator_version": self.generator_version,
            "generation_id": self.generation_id,
            "plugin": {"id": self.plugin_id},
            "features": [item.manifest() for item in self.features],
            "artifacts": [item.manifest() for item in self.artifacts],
            "wiring": [item.manifest() for item in self.wiring],
            "template_capability": self.template_capability,
            "frontend_versions": {
                "cpp": CPP_FRONTEND_VERSION,
                "jvm": JVM_FRONTEND_VERSION,
            },
        }


@dataclass(frozen=True)
class ManifestFeature:
    feature_id: str
    package_name: str
    root: str
    semantic_hash: str

    def manifest(self) -> Dict[str, str]:
        return {
            "id": self.feature_id,
            "package_name": self.package_name,
            "root": self.root,
            "semantic_hash": self.semantic_hash,
        }


@dataclass(frozen=True)
class WiringRecord:
    path: str
    marker: str
    sha256: str

    def manifest(self) -> Dict[str, str]:
        return {"path": self.path, "marker": self.marker, "sha256": self.sha256}


@dataclass(frozen=True)
class IntegrityManifest:
    generator_version: str
    generation_id: str
    plugin_id: str
    features: Tuple[ManifestFeature, ...]
    artifacts: Tuple[OwnedArtifact, ...]
    wiring: Tuple[WiringRecord, ...] = ()
    template_capability: str = TEMPLATE_CAPABILITY_VERSION

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "schema_version": INTEGRITY_MANIFEST_SCHEMA_VERSION,
            "generator_version": self.generator_version,
            "generation_id": self.generation_id,
            "plugin": {"id": self.plugin_id},
            "features": [item.manifest() for item in self.features],
            "artifacts": [item.manifest() for item in self.artifacts],
            "wiring": [item.manifest() for item in self.wiring],
            "template_capability": self.template_capability,
            "frontend_versions": {
                "cpp": CPP_FRONTEND_VERSION,
                "jvm": JVM_FRONTEND_VERSION,
            },
        }
        return value

    def render(self) -> bytes:
        return (
            json.dumps(self.manifest(), indent=2, ensure_ascii=False, sort_keys=True)
            + "\n"
        ).encode("utf-8")

    @classmethod
    def create(
        cls,
        *,
        generator_version: str,
        generation_id: str,
        plugin_id: str,
        features: Iterable[ManifestFeature],
        artifacts: Iterable[OwnedArtifact],
        wiring: Iterable[WiringRecord] = (),
    ) -> "IntegrityManifest":
        ordered_artifacts = tuple(
            sorted(
                (item for item in artifacts if item.committed_source),
                key=lambda item: item.path,
            )
        )
        if any(item.generation_id != generation_id for item in ordered_artifacts):
            raise IntegrityManifestError("artifact generation identities disagree")
        return cls(
            generator_version,
            generation_id,
            plugin_id,
            tuple(sorted(features, key=lambda item: item.package_name)),
            ordered_artifacts,
            tuple(sorted(wiring, key=lambda item: (item.path, item.marker))),
            TEMPLATE_CAPABILITY_VERSION,
        )


def load_integrity_manifest(
    plugin_root: Path,
    *,
    validate_live_ownership: bool = True,
) -> LoadedIntegrityManifest:
    """Load the active manifest without following any managed symlink."""

    root = plugin_root.resolve()
    path = root / INTEGRITY_MANIFEST_PATH
    try:
        content, metadata = read_contained_regular_bytes_no_follow(root, path)
        raw = _decode_unique_json(content, INTEGRITY_MANIFEST_PATH)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, IntegrityManifestError):
            raise
        raise IntegrityManifestError(
            f"{INTEGRITY_MANIFEST_PATH}: cannot load V4 integrity manifest: {exc}"
        ) from exc
    result = parse_integrity_manifest(raw)
    authority_hashes = {
        INTEGRITY_MANIFEST_PATH: _regular_entry_hash(content, metadata),
    }
    if validate_live_ownership:
        authority_hashes.update(
            _validate_live_ownership_inventories(root, result)
        )
    return LoadedIntegrityManifest(
        result.generator_version,
        result.generation_id,
        result.plugin_id,
        result.features,
        result.artifacts,
        result.wiring,
        result.template_capability,
        tuple(sorted(authority_hashes.items())),
    )


def parse_integrity_manifest(raw: object) -> LoadedIntegrityManifest:
    """Parse one complete canonical V4 ownership manifest."""

    manifest = _require_manifest_object(raw)
    generator_version, generation_id, plugin_id = _parse_manifest_header(manifest)
    raw_features, raw_artifacts, raw_wiring = _manifest_record_lists(manifest)
    features = _parse_feature_records(raw_features)
    feature_by_package = {item.package_name: item for item in features}
    artifacts = _parse_artifact_records(
        raw_artifacts, generation_id, feature_by_package
    )
    _require_ownership_anchors(features, artifacts)
    wiring = _parse_wiring_records(raw_wiring)
    result = LoadedIntegrityManifest(
        generator_version,
        generation_id,
        plugin_id,
        features,
        artifacts,
        wiring,
        TEMPLATE_CAPABILITY_VERSION,
    )
    if result.manifest() != manifest:
        raise IntegrityManifestError("integrity manifest is not canonical")
    return result


def _require_manifest_object(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict):
        raise IntegrityManifestError("integrity manifest must be a JSON object")
    return raw


def _parse_manifest_header(raw: Dict[str, object]) -> Tuple[str, str, str]:
    expected_fields = {
        "schema_version",
        "generator_version",
        "generation_id",
        "plugin",
        "features",
        "artifacts",
        "wiring",
        "template_capability",
        "frontend_versions",
    }
    if set(raw) != expected_fields:
        raise IntegrityManifestError("integrity manifest fields are invalid")
    if raw.get("schema_version") != INTEGRITY_MANIFEST_SCHEMA_VERSION:
        raise IntegrityManifestError("integrity manifest schema is incompatible")
    generator_version = raw.get("generator_version")
    generation_id = raw.get("generation_id")
    plugin = raw.get("plugin")
    if (
        not isinstance(generator_version, str)
        or _SEMVER_RE.fullmatch(generator_version) is None
        or not _canonical_semver_prerelease(generator_version)
    ):
        raise IntegrityManifestError("generator_version must be canonical SemVer")
    _require_sha256(generation_id, "generation_id")
    if (
        not isinstance(plugin, dict)
        or set(plugin) != {"id"}
        or not isinstance(plugin.get("id"), str)
        or not plugin["id"]
    ):
        raise IntegrityManifestError("plugin identity is invalid")
    if raw.get("frontend_versions") != {
        "cpp": CPP_FRONTEND_VERSION,
        "jvm": JVM_FRONTEND_VERSION,
    }:
        raise IntegrityManifestError("frontend versions are incompatible")
    if raw.get("template_capability") != TEMPLATE_CAPABILITY_VERSION:
        raise IntegrityManifestError("template capability is incompatible")
    assert isinstance(generation_id, str)
    assert isinstance(plugin, dict)
    plugin_id = plugin["id"]
    assert isinstance(plugin_id, str)
    return generator_version, generation_id, plugin_id


def _manifest_record_lists(
    raw: Dict[str, object],
) -> Tuple[list[object], list[object], list[object]]:
    raw_features = raw.get("features")
    raw_artifacts = raw.get("artifacts")
    raw_wiring = raw.get("wiring")
    if not isinstance(raw_features, list):
        raise IntegrityManifestError("features must be a list")
    if not isinstance(raw_artifacts, list):
        raise IntegrityManifestError("artifacts must be a list")
    if not isinstance(raw_wiring, list):
        raise IntegrityManifestError("wiring must be a list")
    return raw_features, raw_artifacts, raw_wiring


def _parse_feature_records(raw_features: list[object]) -> Tuple[ManifestFeature, ...]:
    features = tuple(
        _parse_feature(item, index) for index, item in enumerate(raw_features)
    )
    package_names = [item.package_name for item in features]
    feature_ids = [item.feature_id for item in features]
    feature_roots = [item.root for item in features]
    if (
        len(package_names) != len(set(package_names))
        or len(feature_ids) != len(set(feature_ids))
        or len(feature_roots) != len(set(feature_roots))
    ):
        raise IntegrityManifestError("feature identities and roots must be unique")
    if features != tuple(sorted(features, key=lambda item: item.package_name)):
        raise IntegrityManifestError("features must be canonically ordered")
    return features


def _parse_artifact_records(
    raw_artifacts: list[object],
    generation_id: str,
    feature_by_package: Dict[str, ManifestFeature],
) -> Tuple[ManifestArtifactRecord, ...]:
    artifacts = tuple(
        _parse_artifact(item, index, generation_id, feature_by_package)
        for index, item in enumerate(raw_artifacts)
    )
    artifact_paths = [item.path for item in artifacts]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise IntegrityManifestError("artifact paths must be unique")
    if artifacts != tuple(sorted(artifacts, key=lambda item: item.path)):
        raise IntegrityManifestError("artifacts must be canonically ordered")
    return artifacts


def _require_ownership_anchors(
    features: Tuple[ManifestFeature, ...],
    artifacts: Tuple[ManifestArtifactRecord, ...],
) -> None:
    for feature in features:
        metadata_path = f"{feature.root}/.supernote-module.json"
        if not any(
            artifact.path == metadata_path
            and artifact.owner == f"feature:{feature.package_name}"
            and artifact.kind == "feature-metadata"
            for artifact in artifacts
        ):
            raise IntegrityManifestError(
                f"feature {feature.package_name!r} lacks canonical metadata ownership"
            )
    if features and not any(
        artifact.path == f"{V4_RUNTIME_ROOT}/ownership.json"
        and artifact.owner == "shared-runtime"
        and artifact.kind == "runtime-metadata"
        for artifact in artifacts
    ):
        raise IntegrityManifestError(
            "active features require canonical shared-runtime ownership metadata"
        )


def _parse_wiring_records(raw_wiring: list[object]) -> Tuple[WiringRecord, ...]:
    wiring = tuple(
        _parse_wiring(item, index) for index, item in enumerate(raw_wiring)
    )
    wiring_keys = [(item.path, item.marker) for item in wiring]
    if len(wiring_keys) != len(set(wiring_keys)):
        raise IntegrityManifestError("wiring records must be unique")
    if len({item.path for item in wiring}) != len(wiring):
        raise IntegrityManifestError("each wiring path may have only one owned block")
    if wiring != tuple(sorted(wiring, key=lambda item: (item.path, item.marker))):
        raise IntegrityManifestError("wiring records must be canonically ordered")
    return wiring


def _decode_unique_json(content: bytes, label: str) -> object:
    """Decode JSON without the standard decoder's last-key-wins ambiguity."""

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise IntegrityManifestError(
                    f"{label}: duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=object_pairs)
    except IntegrityManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityManifestError(f"{label}: invalid JSON: {exc}") from exc


def _validate_live_ownership_inventories(
    root: Path,
    manifest: LoadedIntegrityManifest,
) -> dict[str, str]:
    """Bind destructive authority to the complete canonical ownership lists."""

    artifacts_by_owner = _artifact_paths_by_owner(manifest.artifacts)
    artifact_by_path = {item.path: item for item in manifest.artifacts}
    authority_hashes: dict[str, str] = {}
    for feature in manifest.features:
        relative, digest = _validate_feature_live_ownership(
            root,
            manifest,
            feature,
            artifacts_by_owner,
            artifact_by_path,
        )
        authority_hashes[relative] = digest
    runtime_authority = _validate_runtime_live_ownership(
        root,
        manifest,
        artifacts_by_owner,
        artifact_by_path,
    )
    if runtime_authority is not None:
        relative, digest = runtime_authority
        authority_hashes[relative] = digest
    return authority_hashes


def _artifact_paths_by_owner(
    artifacts: Tuple[ManifestArtifactRecord, ...],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for artifact in artifacts:
        result.setdefault(artifact.owner, set()).add(artifact.path)
    return result


def _validate_feature_live_ownership(
    root: Path,
    manifest: LoadedIntegrityManifest,
    feature: ManifestFeature,
    artifacts_by_owner: dict[str, set[str]],
    artifact_by_path: dict[str, ManifestArtifactRecord],
) -> tuple[str, str]:
    owner = f"feature:{feature.package_name}"
    metadata_relative = f"{feature.root}/.supernote-module.json"
    metadata, metadata_content, metadata_stat = _read_owned_json(
        root, metadata_relative
    )
    _require_feature_metadata_identity(
        metadata, metadata_relative, feature, manifest.generator_version
    )
    assert isinstance(metadata, dict)
    generated = _canonical_generated_files(
        metadata.get("generated_files"), metadata_relative
    )
    unexpected = sorted(set(generated) - set(_FEATURE_GENERATED_FILES))
    if unexpected:
        raise IntegrityManifestError(
            f"{metadata_relative}: unrecognized generated feature artifact "
            f"{unexpected[0]!r}"
        )
    if generated != _FEATURE_GENERATED_FILES:
        raise IntegrityManifestError(
            f"{metadata_relative}: generated_files inventory is incomplete or noncanonical"
        )
    expected = {f"{feature.root}/{relative}" for relative in generated}
    if artifacts_by_owner.get(owner, set()) != expected:
        raise IntegrityManifestError(
            f"{metadata_relative}: manifest feature ownership inventory is incomplete"
        )
    _require_inventory_anchor_hash(
        metadata_relative, metadata_content, artifact_by_path, owner
    )
    return metadata_relative, _regular_entry_hash(metadata_content, metadata_stat)


def _require_feature_metadata_identity(
    metadata: object,
    relative: str,
    feature: ManifestFeature,
    generator_version: str,
) -> None:
    if not isinstance(metadata, dict):
        raise IntegrityManifestError(
            f"{relative}: feature metadata must be a JSON object"
        )
    if (
        metadata.get("schema_version") != FEATURE_MANIFEST_SCHEMA_VERSION
        or metadata.get("kind") != FEATURE_MANIFEST_KIND
        or metadata.get("feature_id") != feature.feature_id
        or metadata.get("npm_name") != feature.package_name
        or metadata.get("generator_version") != generator_version
    ):
        raise IntegrityManifestError(
            f"{relative}: feature identity or generator version disagrees"
        )


def _validate_runtime_live_ownership(
    root: Path,
    manifest: LoadedIntegrityManifest,
    artifacts_by_owner: dict[str, set[str]],
    artifact_by_path: dict[str, ManifestArtifactRecord],
) -> Optional[tuple[str, str]]:
    runtime_paths = artifacts_by_owner.get("shared-runtime", set())
    if manifest.features:
        return _validate_runtime_inventory(
            root,
            manifest.generator_version,
            runtime_paths,
            artifact_by_path,
        )
    if runtime_paths:
        raise IntegrityManifestError(
            "an empty feature set cannot own shared-runtime artifacts"
        )
    return None


def _validate_runtime_inventory(
    root: Path,
    generator_version: str,
    runtime_paths: set[str],
    artifact_by_path: dict[str, ManifestArtifactRecord],
) -> tuple[str, str]:
    relative = f"{V4_RUNTIME_ROOT}/ownership.json"
    ownership, content, metadata = _read_owned_json(root, relative)
    _require_runtime_metadata_identity(ownership, relative, generator_version)
    assert isinstance(ownership, dict)
    generated = _canonical_generated_files(ownership.get("generated_files"), relative)
    if "ownership.json" not in generated:
        raise IntegrityManifestError(
            f"{relative}: ownership.json is absent from generated_files"
        )
    expected = {f"{V4_RUNTIME_ROOT}/{path}" for path in generated}
    if runtime_paths != expected:
        raise IntegrityManifestError(
            f"{relative}: manifest runtime ownership inventory is incomplete"
        )
    _require_inventory_anchor_hash(
        relative, content, artifact_by_path, "shared-runtime"
    )
    return relative, _regular_entry_hash(content, metadata)


def _require_runtime_metadata_identity(
    ownership: object,
    relative: str,
    generator_version: str,
) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "component_name",
        "generator_version",
        "generated_files",
    }
    if not isinstance(ownership, dict) or set(ownership) != expected_fields:
        raise IntegrityManifestError(
            f"{relative}: runtime ownership fields are invalid"
        )
    if (
        ownership.get("schema_version") != GENERATED_OWNERSHIP_SCHEMA_VERSION
        or ownership.get("kind") != GENERATED_OWNERSHIP_KIND
        or ownership.get("generator_version") != generator_version
        or not isinstance(ownership.get("component_name"), str)
        or not ownership.get("component_name")
    ):
        raise IntegrityManifestError(
            f"{relative}: runtime ownership identity disagrees"
        )


def _read_owned_json(
    root: Path,
    relative: str,
) -> tuple[object, bytes, os.stat_result]:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        content, metadata = read_contained_regular_bytes_no_follow(root, path)
    except (OSError, ValueError) as exc:
        raise IntegrityManifestError(
            f"{relative}: cannot read canonical ownership metadata: {exc}"
        ) from exc
    return _decode_unique_json(content, relative), content, metadata


def _regular_entry_hash(content: bytes, metadata: os.stat_result) -> str:
    """Match filesystem.hash_entry_no_follow for one descriptor-read file."""

    digest = hashlib.sha256()
    digest.update(b".\0file\0")
    digest.update(f"{stat.S_IMODE(metadata.st_mode):o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(content)
    return digest.hexdigest()


def _canonical_generated_files(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise IntegrityManifestError(f"{label}: generated_files must be a list")
    rows: list[str] = []
    for index, relative in enumerate(value):
        _require_canonical_relative(relative, f"{label}.generated_files[{index}]")
        assert isinstance(relative, str)
        rows.append(relative)
    if len(rows) != len(set(rows)):
        raise IntegrityManifestError(
            f"{label}: generated_files must be unique"
        )
    return tuple(rows)


def _require_inventory_anchor_hash(
    relative: str,
    content: bytes,
    artifacts: dict[str, ManifestArtifactRecord],
    owner: str,
) -> None:
    record = artifacts.get(relative)
    if record is None or record.owner != owner:
        raise IntegrityManifestError(
            f"{relative}: canonical ownership anchor is absent from the manifest"
        )
    if hashlib.sha256(content).hexdigest() != record.sha256:
        raise IntegrityManifestError(
            f"{relative}: ownership anchor hash disagrees with the manifest"
        )


def _canonical_semver_prerelease(value: str) -> bool:
    core_and_prerelease = value.split("+", 1)[0]
    _separator, _dash, prerelease = core_and_prerelease.partition("-")
    if not prerelease:
        return True
    return all(
        not (identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0")
        for identifier in prerelease.split(".")
    )


def _parse_feature(raw: object, index: int) -> ManifestFeature:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "package_name",
        "root",
        "semantic_hash",
    }:
        raise IntegrityManifestError(f"features[{index}] fields are invalid")
    package_name = raw.get("package_name")
    feature_id = raw.get("id")
    root = raw.get("root")
    semantic_hash = raw.get("semantic_hash")
    if not isinstance(package_name, str) or not package_name:
        raise IntegrityManifestError(f"features[{index}] package name is invalid")
    expected_id = canonical_feature_id(package_name)
    if feature_id != expected_id:
        raise IntegrityManifestError(f"features[{index}] identity is not canonical")
    expected_root = _canonical_feature_root(package_name)
    if root != expected_root:
        raise IntegrityManifestError(f"features[{index}] root is not canonical")
    _require_sha256(semantic_hash, f"features[{index}].semantic_hash")
    assert isinstance(feature_id, str)
    assert isinstance(root, str)
    assert isinstance(semantic_hash, str)
    return ManifestFeature(feature_id, package_name, root, semantic_hash)


def _parse_artifact(
    raw: object,
    index: int,
    generation_id: str,
    features: Dict[str, ManifestFeature],
) -> ManifestArtifactRecord:
    artifact = _artifact_object(raw, index)
    path = artifact.get("path")
    owner = artifact.get("owner")
    kind = artifact.get("kind")
    sha256 = artifact.get("sha256")
    item_generation = artifact.get("generation_id")
    committed_source = artifact.get("committed_source")
    _require_canonical_relative(path, f"artifacts[{index}].path")
    if not isinstance(owner, str) or not owner:
        raise IntegrityManifestError(f"artifacts[{index}] owner is invalid")
    if not isinstance(kind, str) or not kind:
        raise IntegrityManifestError(f"artifacts[{index}] kind is invalid")
    _require_sha256(sha256, f"artifacts[{index}].sha256")
    if item_generation != generation_id:
        raise IntegrityManifestError(
            f"artifacts[{index}] generation identity disagrees with manifest"
        )
    if committed_source is not True:
        raise IntegrityManifestError(
            f"artifacts[{index}] must describe committed source"
        )
    mode = _artifact_mode(artifact, index)
    assert isinstance(path, str)
    if path == INTEGRITY_MANIFEST_PATH:
        raise IntegrityManifestError("the manifest cannot own itself as an artifact")
    _validate_artifact_owner(path, owner, index, features)
    assert isinstance(sha256, str)
    assert isinstance(item_generation, str)
    return ManifestArtifactRecord(
        path,
        owner,
        kind,
        sha256,
        item_generation,
        True,
        mode,
    )


def _artifact_object(raw: object, index: int) -> Dict[str, object]:
    required = {
        "path",
        "owner",
        "kind",
        "sha256",
        "generation_id",
        "committed_source",
    }
    if not isinstance(raw, dict) or not required <= set(raw) or not set(raw) <= (
        required | {"mode"}
    ):
        raise IntegrityManifestError(f"artifacts[{index}] fields are invalid")
    return raw


def _artifact_mode(raw: Dict[str, object], index: int) -> Optional[int]:
    mode = raw.get("mode")
    if "mode" in raw and (
        isinstance(mode, bool)
        or not isinstance(mode, int)
        or not 0 <= mode <= 0o7777
    ):
        raise IntegrityManifestError(f"artifacts[{index}] mode is invalid")
    assert mode is None or isinstance(mode, int)
    return mode if "mode" in raw else None


def _validate_artifact_owner(
    path: str,
    owner: str,
    index: int,
    features: Dict[str, ManifestFeature],
) -> None:
    if owner == "shared-runtime":
        if not _is_descendant(path, V4_RUNTIME_ROOT):
            raise IntegrityManifestError(
                f"artifacts[{index}] shared-runtime path is outside the runtime root"
            )
    elif owner.startswith("feature:"):
        package_name = owner.removeprefix("feature:")
        feature = features.get(package_name)
        if feature is None or not _is_descendant(path, feature.root):
            raise IntegrityManifestError(
                f"artifacts[{index}] feature ownership is inconsistent"
            )
    elif owner == "plugin-global":
        if _is_descendant(path, V4_RUNTIME_ROOT) or any(
            _is_descendant(path, feature.root) for feature in features.values()
        ):
            raise IntegrityManifestError(
                f"artifacts[{index}] plugin-global ownership overlaps another owner"
            )
    else:
        raise IntegrityManifestError(f"artifacts[{index}] owner is unsupported")


def _parse_wiring(raw: object, index: int) -> WiringRecord:
    if not isinstance(raw, dict) or set(raw) != {"path", "marker", "sha256"}:
        raise IntegrityManifestError(f"wiring[{index}] fields are invalid")
    path = raw.get("path")
    marker = raw.get("marker")
    sha256 = raw.get("sha256")
    _require_canonical_relative(path, f"wiring[{index}].path")
    if marker not in {
        "supernote-module-v4-runtime",
        "supernote-module-v4-package",
    }:
        raise IntegrityManifestError(f"wiring[{index}] marker is invalid")
    _require_sha256(sha256, f"wiring[{index}].sha256")
    assert isinstance(path, str)
    assert isinstance(marker, str)
    assert isinstance(sha256, str)
    return WiringRecord(path, marker, sha256)


def _canonical_feature_root(package_name: str) -> str:
    if NPM_NAME.fullmatch(package_name) is None:
        raise IntegrityManifestError("package name is invalid")
    if package_name.startswith("@"):
        parts = package_name.split("/")
        if len(parts) != 2 or not parts[0][1:] or not parts[1]:
            raise IntegrityManifestError("scoped package name is invalid")
        return PurePosixPath("local_modules", *parts).as_posix()
    if "/" in package_name or package_name in {".", ".."}:
        raise IntegrityManifestError("package name is invalid")
    _require_canonical_relative(
        f"local_modules/{package_name}", "feature package root"
    )
    return f"local_modules/{package_name}"


def _require_canonical_relative(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise IntegrityManifestError(f"{label} must be a string")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or posix.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.as_posix() != value
    ):
        raise IntegrityManifestError(f"{label} must be canonical and relative")


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IntegrityManifestError(f"{label} must be a lowercase SHA-256 digest")


def _is_descendant(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts
