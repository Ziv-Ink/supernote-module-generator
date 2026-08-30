"""Stage and activate one already-authorized generation plan."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Callable, Iterable

from .errors import ConcurrentSourceMutation
from .generation_plan import ArtifactChange, GenerationPlan, PlanConflictError
from .integrity_manifest import INTEGRITY_MANIFEST_PATH
from .transaction import Transaction


class GenerationPlanExecutor:
    def __init__(
        self,
        root: Path,
        *,
        validate_preconditions: Callable[[GenerationPlan], None],
        validate_path_precondition: Callable[[GenerationPlan, str], None],
    ) -> None:
        self.root = root
        self.validate_preconditions = validate_preconditions
        self.validate_path_precondition = validate_path_precondition

    def execute(
        self,
        plan: GenerationPlan,
        transaction: Transaction,
        *,
        commit: bool,
    ) -> None:
        staging = Path(tempfile.mkdtemp(prefix=".sn-module-gen-plan-", dir=self.root))
        transaction.track_created(staging)
        self._stage_plan(plan, staging)
        transaction.checkpoint("after_staging")
        self.validate_preconditions(plan)
        ordered, manifest_change = self._ordered_changes(plan)
        replaced = self._detach_planned_paths(plan, transaction, ordered)
        conditional, ordinary = self._prepare_replacements(
            plan,
            transaction,
            staging,
            ordered,
            manifest_change,
        )
        self._activate_replacements(
            plan,
            transaction,
            conditional,
            ordinary,
            replaced=replaced,
        )
        self._checkpoint_completed_edits(
            plan,
            transaction,
            manifest_change=manifest_change,
        )
        shutil.rmtree(staging)
        if commit:
            transaction.commit()

    def _stage_plan(self, plan: GenerationPlan, staging: Path) -> None:
        for artifact in plan.artifacts:
            self._stage_bytes(
                staging,
                artifact.path,
                artifact.content,
                mode=artifact.expected_mode,
                description="artifact",
            )
        for dependency_action in plan.dependency_actions:
            self._stage_bytes(
                staging,
                dependency_action.path,
                dependency_action.content,
                mode=dependency_action.previous_mode,
                description="dependency",
            )
        for wiring_action in plan.wiring_actions:
            self._stage_bytes(
                staging,
                wiring_action.path,
                wiring_action.content,
                mode=wiring_action.previous_mode,
                description="wiring",
            )

    @staticmethod
    def _stage_bytes(
        staging: Path,
        relative: str,
        content: bytes,
        *,
        mode: int | None,
        description: str,
    ) -> None:
        target = staging.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if mode is not None:
            target.chmod(mode)
        if target.read_bytes() != content:
            raise RuntimeError(f"staged {description} verification failed: {relative}")

    @staticmethod
    def _ordered_changes(
        plan: GenerationPlan,
    ) -> tuple[list[ArtifactChange], ArtifactChange | None]:
        ordered = sorted(
            plan.changes,
            key=lambda change: (
                change.path == INTEGRITY_MANIFEST_PATH,
                change.path,
            ),
        )
        manifest_change = next(
            (
                change
                for change in ordered
                if change.path == INTEGRITY_MANIFEST_PATH
            ),
            None,
        )
        return ordered, manifest_change

    def _detach_planned_paths(
        self,
        plan: GenerationPlan,
        transaction: Transaction,
        ordered: Iterable[ArtifactChange],
    ) -> bool:
        replaced = False
        for change in ordered:
            if change.path == INTEGRITY_MANIFEST_PATH:
                continue
            self.validate_path_precondition(plan, change.path)
            if change.action.value == "delete":
                transaction.detach(
                    self.root.joinpath(*PurePosixPath(change.path).parts)
                )
                replaced = self._checkpoint_first_replacement(transaction, replaced)
        for action in plan.tree_removals:
            self.validate_path_precondition(plan, action.path)
            transaction.detach(
                self.root.joinpath(*PurePosixPath(action.path).parts)
            )
            replaced = self._checkpoint_first_replacement(transaction, replaced)
        return replaced

    @staticmethod
    def _checkpoint_first_replacement(
        transaction: Transaction,
        replaced: bool,
    ) -> bool:
        if not replaced:
            transaction.checkpoint("after_first_file_replacement")
        return True

    def _prepare_replacements(
        self,
        plan: GenerationPlan,
        transaction: Transaction,
        staging: Path,
        ordered: Iterable[ArtifactChange],
        manifest_change: ArtifactChange | None,
    ) -> tuple[list[tuple[Path, Path, str, int]], list[tuple[str, Path, Path]]]:
        replacement_paths = [
            change.path
            for change in ordered
            if change.path != INTEGRITY_MANIFEST_PATH
            and change.action.value != "delete"
        ]
        replacement_paths.extend(action.path for action in plan.dependency_actions)
        replacement_paths.extend(action.path for action in plan.wiring_actions)
        if manifest_change is not None:
            replacement_paths.append(INTEGRITY_MANIFEST_PATH)
        conditional_root = transaction.state_dir / "template"
        conditional_root.mkdir(parents=True, exist_ok=True)
        conditional: list[tuple[Path, Path, str, int]] = []
        ordinary: list[tuple[str, Path, Path]] = []
        preconditions = {item.path: item for item in plan.preconditions}
        for index, relative in enumerate(replacement_paths):
            self.validate_path_precondition(plan, relative)
            source = staging.joinpath(*PurePosixPath(relative).parts)
            destination = self.root.joinpath(*PurePosixPath(relative).parts)
            precondition = preconditions[relative]
            if (
                precondition.kind == "file"
                and precondition.content_sha256 is not None
                and precondition.mode is not None
            ):
                candidate = conditional_root / str(index)
                shutil.copy2(source, candidate)
                conditional.append(
                    (
                        candidate,
                        destination,
                        precondition.content_sha256,
                        precondition.mode,
                    )
                )
            else:
                ordinary.append((relative, source, destination))
        return conditional, ordinary

    def _activate_replacements(
        self,
        plan: GenerationPlan,
        transaction: Transaction,
        conditional: list[tuple[Path, Path, str, int]],
        ordinary: list[tuple[str, Path, Path]],
        *,
        replaced: bool,
    ) -> None:
        if conditional:
            try:
                transaction.replace_regular_batch_if_matches(conditional)
            except ConcurrentSourceMutation as exc:
                raise PlanConflictError(str(exc)) from exc
            replaced = self._checkpoint_first_replacement(transaction, replaced)
        for relative, source, destination in ordinary:
            self.validate_path_precondition(plan, relative)
            transaction.replace(source, destination)
            replaced = self._checkpoint_first_replacement(transaction, replaced)

    @staticmethod
    def _checkpoint_completed_edits(
        plan: GenerationPlan,
        transaction: Transaction,
        *,
        manifest_change: ArtifactChange | None,
    ) -> None:
        if plan.dependency_actions:
            transaction.checkpoint("after_dependency_edit")
        if plan.wiring_actions:
            transaction.checkpoint("after_wiring")
        if manifest_change is not None:
            transaction.checkpoint("before_manifest_write")
            transaction.checkpoint("after_manifest_write")
