from __future__ import annotations

from pathlib import Path

import pytest

from supernote_module_generator.filesystem import iter_tree_no_follow
from supernote_module_generator.jvm_manifest import JvmSourceManifest
from supernote_module_generator.project_model import ProjectModel
from supernote_module_generator.cli_operations import CliOperationService


@pytest.fixture
def make_directory_symlink():
    """Create a directory symlink or skip when the host forbids test symlinks."""

    def make(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory symlinks are unavailable on this host: {exc}")

    return make


@pytest.fixture
def stub_ksp_frontend(monkeypatch):
    """Supply deterministic empty KSP IR to CLI tests with stub Gradle."""

    def manifests(
        service: CliOperationService,
        *,
        allow_unmanifested_bootstrap: bool = False,
    ):
        project = ProjectModel.discover(
            service.root,
            allow_unmanifested_bootstrap=allow_unmanifested_bootstrap,
        )
        return {
            feature.identity.feature_id: JvmSourceManifest(
                feature.identity.feature_id,
                "test-frontend",
                (),
            )
            for feature in project.features
            if feature.jvm_root.is_dir()
            and any(
                path.is_file() and path.suffix.lower() in {".kt", ".java"}
                for path in iter_tree_no_follow(feature.jvm_root)
            )
        }

    monkeypatch.setattr(
        CliOperationService,
        "_jvm_frontend_manifests",
        manifests,
    )
