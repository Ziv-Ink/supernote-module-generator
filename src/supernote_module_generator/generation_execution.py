"""Rerunnable publication for disposable generated output."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import json
import shutil
import tempfile
from typing import Callable

from .filesystem import (
    _windows_api_path,
    _windows_host,
    entry_kind,
    remove_generated_regular_no_follow,
    replace_generated_regular_no_follow,
)
from .generation_plan import GenerationPlan
from .integrity_manifest import INCOMPLETE_MARKER_PATH, INTEGRITY_MANIFEST_PATH


class GenerationPlanExecutor:
    """Publish generated leaves without retaining or restoring prior output bytes."""

    def __init__(
        self,
        root: Path,
        *,
        validate_preconditions: Callable[[GenerationPlan], None],
        validate_path_precondition: Callable[[GenerationPlan, str], None],
        validate_completion_preconditions: Callable[[GenerationPlan], None],
    ) -> None:
        self.root = root
        self.validate_preconditions = validate_preconditions
        self.validate_path_precondition = validate_path_precondition
        self.validate_completion_preconditions = validate_completion_preconditions

    def execute(self, plan: GenerationPlan, *, finalize: bool = True) -> None:
        staging = Path(tempfile.mkdtemp(prefix=".sn-module-gen-plan-", dir=self.root))
        try:
            self._stage_plan(plan, staging)
            self.validate_preconditions(plan)
            self._publish_incomplete_marker(plan, staging)
            self._delete_stale_output(plan)
            self._publish_output(plan, staging, finalize=finalize)
            if finalize:
                self._clear_incomplete_marker()
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _stage_plan(self, plan: GenerationPlan, staging: Path) -> None:
        for artifact in plan.artifacts:
            target = staging.joinpath(*PurePosixPath(artifact.path).parts)
            writable = (
                Path(_windows_api_path(target)) if _windows_host() else target
            )
            writable.parent.mkdir(parents=True, exist_ok=True)
            writable.write_bytes(artifact.content)
            if artifact.expected_mode is not None:
                writable.chmod(artifact.expected_mode)
            if writable.read_bytes() != artifact.content:
                raise RuntimeError(
                    f"staged generated artifact verification failed: {artifact.path}"
                )

    def _publish_incomplete_marker(
        self, plan: GenerationPlan, staging: Path
    ) -> None:
        relative = INCOMPLETE_MARKER_PATH
        marker = staging.joinpath(*PurePosixPath(relative).parts)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "generation_id": plan.generation_id,
                    "operation": plan.operation,
                    "status": "incomplete",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        replace_generated_regular_no_follow(
            self.root,
            marker,
            self.root.joinpath(*PurePosixPath(relative).parts),
        )

    def _clear_incomplete_marker(self) -> None:
        marker = self.root / INCOMPLETE_MARKER_PATH
        if entry_kind(marker) is not None:
            remove_generated_regular_no_follow(self.root, marker)

    def _delete_stale_output(self, plan: GenerationPlan) -> None:
        for relative in plan.deletes:
            if relative == INTEGRITY_MANIFEST_PATH:
                continue
            destination = self.root.joinpath(*PurePosixPath(relative).parts)
            if entry_kind(destination) is None:
                continue
            self.validate_path_precondition(plan, relative)
            remove_generated_regular_no_follow(self.root, destination)

    def _publish_output(
        self,
        plan: GenerationPlan,
        staging: Path,
        *,
        finalize: bool,
    ) -> None:
        artifacts = sorted(
            plan.artifacts,
            key=lambda artifact: (
                artifact.path == INTEGRITY_MANIFEST_PATH,
                artifact.path,
            ),
        )
        for artifact in artifacts:
            if artifact.path == INTEGRITY_MANIFEST_PATH:
                if not finalize:
                    continue
                self.validate_completion_preconditions(plan)
            else:
                self.validate_path_precondition(plan, artifact.path)
            source = staging.joinpath(*PurePosixPath(artifact.path).parts)
            destination = self.root.joinpath(*PurePosixPath(artifact.path).parts)
            replace_generated_regular_no_follow(self.root, source, destination)
