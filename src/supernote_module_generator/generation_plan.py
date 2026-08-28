"""Complete, deterministic V4 artifact planning contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import difflib
import hashlib
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Iterable, Mapping, Tuple

from .filesystem import entry_kind, hash_entry_no_follow, lexists


class GenerationPlanError(ValueError):
    pass


class PlanConflictError(GenerationPlanError):
    """The filesystem changed after a plan captured its preconditions."""

    def __init__(
        self,
        message: str,
        *,
        preserve_directory_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.preserve_directory_paths = tuple(preserve_directory_paths)


class ArtifactAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class OwnedArtifact:
    path: str
    owner: str
    kind: str
    content: bytes
    generation_id: str
    committed_source: bool = True
    expected_mode: int | None = None

    def __post_init__(self) -> None:
        normalized = PurePosixPath(self.path)
        windows = PureWindowsPath(self.path)
        if (
            "\\" in self.path
            or windows.drive
            or windows.root
            or normalized.is_absolute()
            or not normalized.parts
            or any(part in {"", ".", ".."} for part in normalized.parts)
            or normalized.as_posix() != self.path
        ):
            raise GenerationPlanError(
                f"owned artifact path must be canonical and relative: {self.path!r}"
            )
        if not self.owner or not self.kind or not self.generation_id:
            raise GenerationPlanError("owned artifact identity fields cannot be empty")
        if self.expected_mode is not None and not 0 <= self.expected_mode <= 0o7777:
            raise GenerationPlanError("owned artifact mode is invalid")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "path": self.path,
            "owner": self.owner,
            "kind": self.kind,
            "sha256": self.sha256,
            "generation_id": self.generation_id,
            "committed_source": self.committed_source,
        }
        if self.expected_mode is not None:
            value["mode"] = self.expected_mode
        return value


@dataclass(frozen=True)
class ArtifactChange:
    action: ArtifactAction
    artifact: OwnedArtifact | None
    path: str
    previous: bytes | None = field(default=None, repr=False)
    previous_kind: str | None = field(default=None, repr=False)
    previous_hash: str | None = field(default=None, repr=False)

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {"action": self.action.value, "path": self.path}
        if self.artifact is not None:
            value.update(
                {
                    "owner": self.artifact.owner,
                    "kind": self.artifact.kind,
                    "sha256": self.artifact.sha256,
                }
            )
        return value


@dataclass(frozen=True)
class DependencyAction:
    path: str
    package_names: Tuple[str, ...]
    content: bytes = field(repr=False)
    previous: bytes = field(repr=False)
    previous_mode: int = 0o644

    def manifest(self) -> Dict[str, object]:
        return {
            "action": "update",
            "path": self.path,
            "packages": list(self.package_names),
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "mode": self.previous_mode,
        }


@dataclass(frozen=True)
class WiringAction:
    path: str
    marker: str
    content: bytes = field(repr=False)
    previous: bytes = field(repr=False)
    previous_mode: int = 0o644

    def manifest(self) -> Dict[str, object]:
        return {
            "action": "update",
            "path": self.path,
            "marker": self.marker,
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "mode": self.previous_mode,
        }


@dataclass(frozen=True)
class TreeRemovalAction:
    path: str
    owner: str
    previous_hash: str

    def manifest(self) -> Dict[str, object]:
        return {
            "action": "delete",
            "path": self.path,
            "owner": self.owner,
            "kind": "managed-tree",
            "previous_sha256": self.previous_hash,
        }


@dataclass(frozen=True)
class PlanPrecondition:
    """One immutable filesystem assumption captured by a GenerationPlan."""

    path: str
    kind: str | None
    sha256: str | None


@dataclass(frozen=True)
class GenerationPlan:
    operation: str
    requested_targets: Tuple[str, ...]
    affected_targets: Tuple[str, ...]
    generation_id: str
    artifacts: Tuple[OwnedArtifact, ...]
    deletes: Tuple[str, ...]
    changes: Tuple[ArtifactChange, ...]
    preserved_user_paths: Tuple[str, ...] = ()
    dependency_actions: Tuple[DependencyAction, ...] = ()
    wiring_actions: Tuple[WiringAction, ...] = ()
    tree_removals: Tuple[TreeRemovalAction, ...] = ()
    wiring_issues: Tuple[str, ...] = ()
    tool_invocations: Tuple[Tuple[str, ...], ...] = ()
    warnings: Tuple[str, ...] = ()
    preconditions: Tuple[PlanPrecondition, ...] = field(default=(), repr=False)
    discovery_frontier: Tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def compare(
        cls,
        root: Path,
        *,
        operation: str,
        requested_targets: Iterable[str],
        affected_targets: Iterable[str],
        generation_id: str,
        artifacts: Iterable[OwnedArtifact],
        deletes: Iterable[str] = (),
        preserved_user_paths: Iterable[str] = (),
        dependency_actions: Iterable[DependencyAction] = (),
        wiring_actions: Iterable[WiringAction] = (),
        tree_removals: Iterable[tuple[str, str]] = (),
        authorized_tree_removals: Iterable[str] = (),
        wiring_issues: Iterable[str] = (),
        tool_invocations: Iterable[Iterable[str]] = (),
        warnings: Iterable[str] = (),
        precondition_paths: Iterable[str] = (),
        precondition_baselines: Mapping[
            str, tuple[str | None, str | None]
        ] | None = None,
        discovery_frontier: Iterable[str] = (),
    ) -> "GenerationPlan":
        root = root.resolve()
        ordered_artifacts, artifact_paths = _prepare_artifacts(
            artifacts, generation_id
        )
        ordered_deletes = tuple(sorted(set(deletes)))
        if set(artifact_paths) & set(ordered_deletes):
            raise GenerationPlanError("an artifact cannot be rendered and deleted")
        preserved = tuple(sorted(set(preserved_user_paths)))
        for relative in (*artifact_paths, *ordered_deletes, *preserved):
            _validate_relative(relative)
        ordered_dependency_actions, dependency_paths = _prepare_dependency_actions(
            root,
            dependency_actions,
            (*artifact_paths, *ordered_deletes, *preserved),
        )
        ordered_wiring_actions, wiring_paths = _prepare_wiring_actions(
            root,
            wiring_actions,
            (*artifact_paths, *ordered_deletes, *preserved, *dependency_paths),
        )
        removal_rows = _prepare_tree_removals(
            root,
            tree_removals,
            authorized_tree_removals,
            (
                *artifact_paths,
                *ordered_deletes,
                *preserved,
                *dependency_paths,
                *wiring_paths,
            ),
        )
        _validate_stale_deletes(root, ordered_deletes, artifact_paths, preserved)
        changes = _compare_artifact_changes(
            root, ordered_artifacts, ordered_deletes
        )
        preconditions = _capture_preconditions(
            root,
            (
                *artifact_paths,
                *ordered_deletes,
                *preserved,
                *dependency_paths,
                *wiring_paths,
                *(item.path for item in removal_rows),
                *precondition_paths,
            ),
            precondition_baselines,
        )
        return cls(
            operation,
            tuple(requested_targets),
            tuple(sorted(set(affected_targets))),
            generation_id,
            ordered_artifacts,
            ordered_deletes,
            tuple(changes),
            preserved,
            ordered_dependency_actions,
            ordered_wiring_actions,
            tuple(removal_rows),
            tuple(wiring_issues),
            tuple(tuple(item) for item in tool_invocations),
            tuple(warnings),
            tuple(preconditions),
            tuple(discovery_frontier),
        )

    @property
    def is_noop(self) -> bool:
        return (
            not self.changes
            and not self.dependency_actions
            and not self.wiring_actions
            and not self.tree_removals
            and not self.wiring_issues
        )

    def manifest(self) -> Dict[str, object]:
        return {
            "operation": self.operation,
            "requested_targets": list(self.requested_targets),
            "affected_targets": list(self.affected_targets),
            "generation_id": self.generation_id,
            "changes": [change.manifest() for change in self.changes],
            "preserved_user_paths": list(self.preserved_user_paths),
            "dependency_actions": [
                action.manifest() for action in self.dependency_actions
            ],
            "wiring_actions": [action.manifest() for action in self.wiring_actions],
            "tree_removals": [action.manifest() for action in self.tree_removals],
            "wiring_issues": list(self.wiring_issues),
            "tool_invocations": [list(command) for command in self.tool_invocations],
            "warnings": list(self.warnings),
            "discovery_frontier_sha256": hashlib.sha256(
                "\n".join(self.discovery_frontier).encode("utf-8")
            ).hexdigest(),
        }

    def unified_diff(self) -> str:
        sections: list[str] = []
        for change in self.changes:
            if change.artifact is None:
                after: list[str] | None = []
            else:
                after = _text_lines(change.artifact.content)
            before = _text_lines(change.previous or b"")
            if before is None or after is None:
                sections.append(
                    f"Binary {change.action.value}: {change.path}\n"
                )
                continue
            sections.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{change.path}",
                    tofile=f"b/{change.path}",
                )
            )
        for dependency_action in self.dependency_actions:
            before = _text_lines(dependency_action.previous)
            after = _text_lines(dependency_action.content)
            if before is None or after is None:
                sections.append(f"Binary update: {dependency_action.path}\n")
                continue
            sections.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{dependency_action.path}",
                    tofile=f"b/{dependency_action.path}",
                )
            )
        for wiring_action in self.wiring_actions:
            before = _text_lines(wiring_action.previous)
            after = _text_lines(wiring_action.content)
            if before is None or after is None:
                sections.append(f"Binary update: {wiring_action.path}\n")
                continue
            sections.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{wiring_action.path}",
                    tofile=f"b/{wiring_action.path}",
                )
            )
        for tree_removal in self.tree_removals:
            sections.append(f"Delete tree: {tree_removal.path}\n")
        return "".join(sections)


def _prepare_artifacts(
    artifacts: Iterable[OwnedArtifact],
    generation_id: str,
) -> tuple[Tuple[OwnedArtifact, ...], Tuple[str, ...]]:
    ordered = tuple(sorted(artifacts, key=lambda item: item.path))
    paths = tuple(artifact.path for artifact in ordered)
    if len(paths) != len(set(paths)):
        raise GenerationPlanError("owned artifact paths must be unique")
    for artifact in ordered:
        if artifact.generation_id != generation_id:
            raise GenerationPlanError(
                f"{artifact.path}: artifact generation identity disagrees with plan"
            )
    return ordered, paths


def _prepare_dependency_actions(
    root: Path,
    actions: Iterable[DependencyAction],
    protected_paths: Iterable[str],
) -> tuple[Tuple[DependencyAction, ...], Tuple[str, ...]]:
    ordered = tuple(sorted(actions, key=lambda item: item.path))
    paths = tuple(action.path for action in ordered)
    if len(paths) != len(set(paths)):
        raise GenerationPlanError("dependency action paths must be unique")
    protected = tuple(protected_paths)
    for action in ordered:
        _validate_relative(action.path)
        if action.path != "package.json":
            raise GenerationPlanError(
                "dependency actions may update only the canonical parent package.json"
            )
        _validate_managed_destination(root, action.path)
        if any(_paths_overlap(action.path, item) for item in protected):
            raise GenerationPlanError(
                "dependency action overlaps generated or preserved project state: "
                f"{action.path!r}"
            )
        destination = root.joinpath(*PurePosixPath(action.path).parts)
        kind = entry_kind(destination)
        if kind != "file":
            raise GenerationPlanError(
                f"dependency destination must be a regular file: {action.path!r} "
                f"is {kind or 'missing'}"
            )
        if destination.read_bytes() != action.previous:
            raise GenerationPlanError(
                f"dependency action baseline is stale: {action.path!r}"
            )
        if stat.S_IMODE(destination.stat().st_mode) != action.previous_mode:
            raise GenerationPlanError(
                f"dependency action mode baseline is stale: {action.path!r}"
            )
    return ordered, paths


def _prepare_wiring_actions(
    root: Path,
    actions: Iterable[WiringAction],
    protected_paths: Iterable[str],
) -> tuple[Tuple[WiringAction, ...], Tuple[str, ...]]:
    ordered = tuple(sorted(actions, key=lambda item: item.path))
    paths = tuple(action.path for action in ordered)
    if len(paths) != len(set(paths)):
        raise GenerationPlanError("wiring action paths must be unique")
    protected = tuple(protected_paths)
    for action in ordered:
        _validate_relative(action.path)
        _validate_managed_destination(root, action.path)
        destination = root.joinpath(*PurePosixPath(action.path).parts)
        if entry_kind(destination) != "file":
            raise GenerationPlanError(
                f"wiring destination must be a regular file: {action.path!r}"
            )
        if destination.read_bytes() != action.previous:
            raise GenerationPlanError(
                f"wiring action baseline is stale: {action.path!r}"
            )
        if stat.S_IMODE(destination.stat().st_mode) != action.previous_mode:
            raise GenerationPlanError(
                f"wiring action mode baseline is stale: {action.path!r}"
            )
        if any(_paths_overlap(action.path, item) for item in protected):
            raise GenerationPlanError(
                f"wiring action overlaps generated or preserved state: {action.path!r}"
            )
    return ordered, paths


def _canonical_tree_removal_path(owner: str) -> str:
    if owner.startswith("feature:"):
        return f"local_modules/{owner.removeprefix('feature:')}"
    if owner == "shared-runtime":
        return "android/.supernote-module/v4-runtime"
    raise GenerationPlanError(f"managed-tree removal owner is invalid: {owner!r}")


def _prepare_tree_removals(
    root: Path,
    removals: Iterable[tuple[str, str]],
    authorized_tree_removals: Iterable[str],
    protected_paths: Iterable[str],
) -> Tuple[TreeRemovalAction, ...]:
    authorized = set(authorized_tree_removals)
    for relative in authorized:
        _validate_relative(relative)
    protected = tuple(protected_paths)
    rows: list[TreeRemovalAction] = []
    for relative, owner in sorted(removals):
        _validate_relative(relative)
        expected = _canonical_tree_removal_path(owner)
        if relative != expected:
            raise GenerationPlanError(
                f"managed-tree removal is noncanonical: {relative!r} for {owner!r}"
            )
        if relative not in authorized:
            raise GenerationPlanError(
                "managed-tree removal lacks manifest or current-operation "
                f"ownership authority: {relative!r}"
            )
        _validate_managed_destination(root, relative)
        destination = root.joinpath(*PurePosixPath(relative).parts)
        if entry_kind(destination) != "directory":
            raise GenerationPlanError(
                f"feature-tree removal requires a directory: {relative!r}"
            )
        if any(_paths_overlap(relative, item) for item in protected):
            raise GenerationPlanError(
                f"feature-tree removal overlaps another plan action: {relative!r}"
            )
        digest = hash_entry_no_follow(destination)
        assert digest is not None
        rows.append(TreeRemovalAction(relative, owner, digest))
    return tuple(rows)


def _validate_stale_deletes(
    root: Path,
    deletes: Iterable[str],
    artifact_paths: Iterable[str],
    preserved_paths: Iterable[str],
) -> None:
    protected = (*tuple(artifact_paths), *tuple(preserved_paths))
    for relative in deletes:
        if any(_paths_overlap(relative, item) for item in protected):
            raise GenerationPlanError(
                f"refusing to delete {relative!r}: it overlaps an expected artifact "
                "or preserved user source path"
            )
        _validate_managed_destination(root, relative)


def _compare_artifact_changes(
    root: Path,
    artifacts: Iterable[OwnedArtifact],
    deletes: Iterable[str],
) -> Tuple[ArtifactChange, ...]:
    changes = [_compare_owned_artifact(root, artifact) for artifact in artifacts]
    result = [change for change in changes if change is not None]
    result.extend(
        change
        for relative in deletes
        if (change := _compare_stale_artifact(root, relative)) is not None
    )
    return tuple(result)


def _compare_owned_artifact(
    root: Path, artifact: OwnedArtifact
) -> ArtifactChange | None:
    _validate_managed_destination(root, artifact.path)
    destination = root.joinpath(*PurePosixPath(artifact.path).parts)
    kind = entry_kind(destination)
    previous = destination.read_bytes() if kind == "file" else None
    previous_hash = hash_entry_no_follow(destination)
    mode_mismatch = (
        artifact.expected_mode is not None
        and kind == "file"
        and (destination.stat().st_mode & 0o7777) != artifact.expected_mode
    )
    if kind is None:
        return ArtifactChange(
            ArtifactAction.CREATE,
            artifact,
            artifact.path,
            previous_kind=None,
            previous_hash=None,
        )
    if kind != "file" or previous != artifact.content or mode_mismatch:
        return ArtifactChange(
            ArtifactAction.UPDATE,
            artifact,
            artifact.path,
            previous,
            kind,
            previous_hash,
        )
    return None


def _compare_stale_artifact(root: Path, relative: str) -> ArtifactChange | None:
    destination = root.joinpath(*PurePosixPath(relative).parts)
    if not lexists(destination):
        return None
    kind = entry_kind(destination)
    if kind != "file":
        raise GenerationPlanError(
            f"stale owned artifact must be a regular file: {relative!r} "
            f"is {kind or 'missing'}"
        )
    return ArtifactChange(
        ArtifactAction.DELETE,
        None,
        relative,
        destination.read_bytes(),
        kind,
        hash_entry_no_follow(destination),
    )


def _capture_preconditions(
    root: Path,
    paths: Iterable[str],
    baselines: Mapping[str, tuple[str | None, str | None]] | None,
) -> Tuple[PlanPrecondition, ...]:
    baseline_by_path = dict(baselines or {})
    precondition_paths = {*paths, *baseline_by_path}
    rows: list[PlanPrecondition] = []
    for relative in sorted(precondition_paths):
        _validate_relative(relative)
        _validate_managed_destination(root, relative)
        destination = root.joinpath(*PurePosixPath(relative).parts)
        baseline = baseline_by_path.get(relative)
        rows.append(
            PlanPrecondition(
                relative,
                baseline[0] if baseline is not None else entry_kind(destination),
                baseline[1]
                if baseline is not None
                else hash_entry_no_follow(destination),
            )
        )
    return tuple(rows)


def _validate_relative(value: str) -> None:
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if "\\" in value or windows.drive or windows.root or path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise GenerationPlanError(f"path must be canonical and relative: {value!r}")


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _validate_managed_destination(root: Path, relative: str) -> None:
    """Reject existing symlink ancestors before a managed read/write/delete."""

    current = root
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        kind = entry_kind(current)
        if kind == "symlink":
            raise GenerationPlanError(
                f"managed destination has a symbolic-link ancestor: {relative!r}"
            )
        if kind not in {None, "directory"}:
            raise GenerationPlanError(
                f"managed destination has a non-directory ancestor: {relative!r}"
            )


def _text_lines(value: bytes) -> list[str] | None:
    try:
        return value.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
