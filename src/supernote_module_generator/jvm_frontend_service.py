"""Bounded publisher-side Kotlin/Java semantic analysis."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Mapping

from .errors import FilesystemError
from .filesystem import iter_tree_no_follow, read_regular_bytes_no_follow
from .frontend_discovery import load_jvm_frontend_manifests
from .jvm_manifest import JvmSourceManifest
from .npm_distribution import ksp_adapter_output_name
from .plugin_runtime_codegen import RUNTIME_RELATIVE_ROOT
from .project_model import ProjectModel
from .subprocesses import run_process


class JvmFrontendManifests(dict[str, JvmSourceManifest]):
    """KSP manifests plus the exact adapter bytes emitted by the same run."""

    def __init__(
        self,
        manifests: Mapping[str, JvmSourceManifest],
        adapter_sources: Mapping[str, bytes],
    ) -> None:
        super().__init__(manifests)
        self.adapter_sources = dict(adapter_sources)


class JvmFrontendService:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def manifests(self, *, allow_unmanifested_bootstrap: bool = False):
        project = ProjectModel.discover(
            self.root,
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        has_jvm = any(
            feature.jvm_root.is_dir()
            and any(
                path.is_file() and path.suffix.lower() in {".kt", ".java"}
                for path in iter_tree_no_follow(feature.jvm_root)
            )
            for feature in project.features
        )
        if not has_jvm:
            return {}
        configured = os.environ.get("SUPERNOTE_GRADLE_COMMAND")
        gradle = configured or shutil.which("gradle")
        if gradle is None or not Path(gradle).is_absolute() or not Path(gradle).is_file():
            raise RuntimeError(
                "JVM generation requires an absolute standalone Gradle executable; "
                "set SUPERNOTE_GRADLE_COMMAND or put gradle on PATH"
            )
        analysis_root = self.root / RUNTIME_RELATIVE_ROOT / "analysis"
        settings = analysis_root / "settings.gradle"
        subject = analysis_root / "subject/build.gradle"
        if not settings.is_file() or not subject.is_file():
            raise RuntimeError(
                "JVM generation requires a generated analysis bootstrap; "
                "rerun plain `sn-module-gen update`"
            )
        command = [
            str(gradle),
            "--no-daemon",
            "--console=plain",
            "-p",
            str(analysis_root),
            ":subject:kspKotlin",
        ]
        result = run_process(command, cwd=analysis_root, timeout=1200)
        if result.returncode:
            output = result.stderr or result.stdout
            raise RuntimeError(
                "KSP semantic frontend failed:\n"
                + "\n".join(output.splitlines()[-12:])
            )
        manifest_root = (
            self.root
            / RUNTIME_RELATIVE_ROOT
            / "analysis/subject/build/generated/ksp/main/resources/supernote/generated/manifests"
        )
        manifests = load_jvm_frontend_manifests(project, manifest_root)
        adapter_root = (
            self.root
            / RUNTIME_RELATIVE_ROOT
            / "analysis/subject/build/generated/ksp/main/kotlin/"
            "supernote/generated/adapters"
        )
        adapter_sources: dict[str, bytes] = {}
        for feature_id in sorted(manifests):
            output_path = adapter_root / ksp_adapter_output_name(feature_id)
            try:
                content, _metadata = read_regular_bytes_no_follow(output_path)
            except (FilesystemError, OSError) as exc:
                raise RuntimeError(
                    "KSP semantic frontend did not emit the required adapter source "
                    f"for {feature_id}: {output_path}"
                ) from exc
            if b"package supernote.generated.adapters" not in content:
                raise RuntimeError(
                    "KSP semantic frontend emitted an invalid adapter source for "
                    f"{feature_id}: {output_path}"
                )
            adapter_sources[feature_id] = content
        return JvmFrontendManifests(manifests, adapter_sources)
