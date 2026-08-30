import os
from pathlib import Path
import shutil

import pytest

from supernote_module_generator.errors import ConfigurationError, FilesystemError
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.plugin_build_integration import (
    set_runtime_wiring,
    verify_runtime_wiring,
)


@pytest.mark.parametrize("kotlin", [False, True])
def test_wires_one_plugin_runtime_project_idempotently(tmp_path: Path, kotlin: bool):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    suffix = ".gradle.kts" if kotlin else ".gradle"
    settings = android / f"settings{suffix}"
    app = android / "app" / f"build{suffix}"
    settings.write_text("rootProject.name = 'fixture'\n")
    app.write_text("plugins {}\n")

    set_runtime_wiring(tmp_path, enabled=True)
    first = (settings.read_text(), app.read_text())
    set_runtime_wiring(tmp_path, enabled=True)
    assert (settings.read_text(), app.read_text()) == first
    verify_runtime_wiring(tmp_path, enabled=True)
    assert first[0].count("include") == 3
    assert first[1].count("implementation") == 1

    set_runtime_wiring(tmp_path, enabled=False)
    verify_runtime_wiring(tmp_path, enabled=False)
    assert "supernote-runtime" not in settings.read_text()
    assert "supernote-runtime" not in app.read_text()


def test_duplicate_runtime_blocks_are_rejected(tmp_path: Path):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    block = "// sn-module-gen-runtime\nx\n// end sn-module-gen-runtime\n"
    (android / "settings.gradle").write_text(block + block)
    (android / "app/build.gradle").write_text("plugins {}\n")
    with pytest.raises(ConfigurationError, match="duplicate"):
        set_runtime_wiring(tmp_path, enabled=True)


@pytest.mark.parametrize("language", ["kt", "java"])
def test_registers_generated_react_package_idempotently(
    tmp_path: Path, language: str
):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    (android / "settings.gradle").write_text("rootProject.name = 'fixture'\n")
    (android / "app/build.gradle").write_text("plugins {}\n")
    source = android / "app/src/main/java/com/example" / f"MainApplication.{language}"
    source.parent.mkdir(parents=True)
    if language == "kt":
        source.write_text(
            "fun getPackages() =\n"
            "    PackageList(this).packages.apply {\n"
            "      add(ExistingPackage())\n"
            "    }\n"
        )
    else:
        source.write_text(
            "List<ReactPackage> packages = new PackageList(this).getPackages();\n"
            "return packages;\n"
        )

    set_runtime_wiring(tmp_path, enabled=True)
    first = source.read_text()
    set_runtime_wiring(tmp_path, enabled=True)
    assert source.read_text() == first
    assert first.count("SupernoteModulePackage") == 1
    verify_runtime_wiring(tmp_path, enabled=True)

    set_runtime_wiring(tmp_path, enabled=False)
    assert "SupernoteModulePackage" not in source.read_text()
    verify_runtime_wiring(tmp_path, enabled=False)


@pytest.mark.parametrize("legacy_version", (2, 4))
def test_complete_stale_wiring_is_rejected_without_touching_user_source(
    tmp_path: Path,
    legacy_version: int,
):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    (android / "settings.gradle").write_text(
        "rootProject.name = 'fixture'\n"
        f"// supernote-module-v{legacy_version}-runtime\nlegacy\n"
        f"// end supernote-module-v{legacy_version}-runtime\n"
        "include ':user-library'\n"
    )
    (android / "app/build.gradle").write_text(
        "plugins {}\n"
        f"// supernote-module-v{legacy_version}-runtime\nlegacy dependency\n"
        f"// end supernote-module-v{legacy_version}-runtime\n"
        "dependencies { implementation project(':user-library') }\n"
    )
    application = android / "app/src/main/java/com/example/MainApplication.kt"
    application.parent.mkdir(parents=True)
    application.write_text(
        "fun getPackages() =\n"
        "    PackageList(this).packages.apply {\n"
        f"      // supernote-module-v{legacy_version}-package\n"
        f"      add(supernote.generated.runtime.SupernoteV{legacy_version}Package())\n"
        f"      // end supernote-module-v{legacy_version}-package\n"
        "      add(UserPackage())\n"
        "    }\n"
    )

    before = {
        path: path.read_bytes()
        for path in (
            android / "settings.gradle",
            android / "app/build.gradle",
            application,
        )
    }

    with pytest.raises(ConfigurationError, match="unsupported legacy runtime wiring"):
        set_runtime_wiring(tmp_path, enabled=True)

    assert {path: path.read_bytes() for path in before} == before


def test_malformed_stale_v2_wiring_is_rejected_without_mutation(tmp_path: Path):
    android = tmp_path / "android"
    (android / "app").mkdir(parents=True)
    settings = android / "settings.gradle"
    settings.write_text("// supernote-module-v2-runtime\nlegacy\n")
    (android / "app/build.gradle").write_text("plugins {}\n")
    before = settings.read_bytes()

    with pytest.raises(ConfigurationError, match="unsupported legacy runtime wiring"):
        set_runtime_wiring(tmp_path, enabled=True)

    assert settings.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink identity fixture")
@pytest.mark.parametrize(
    "unsafe_path",
    (
        "settings_final",
        "settings_ancestor",
        "app_final",
        "app_ancestor",
        "application_final",
        "application_ancestor",
    ),
)
def test_generation_plan_never_follows_unsafe_wiring_paths(
    tmp_path: Path,
    unsafe_path: str,
):
    root = tmp_path / "plugin"
    android = root / "android"
    (android / "app").mkdir(parents=True)
    (android / "settings.gradle").write_text("include ':app'\n")
    (android / "app/build.gradle").write_text("plugins {}\n")
    (root / "package.json").write_text('{"name":"fixture","dependencies":{}}\n')
    application = android / "app/src/main/java/com/example/MainApplication.kt"
    application.parent.mkdir(parents=True)
    application.write_text(
        "fun packages() = PackageList(this).packages.apply {\n}\n"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("outside bytes\n")

    if unsafe_path == "settings_final":
        target = android / "settings.gradle"
        target.unlink()
        target.symlink_to(sentinel)
    elif unsafe_path == "settings_ancestor":
        external_android = outside / "android"
        (external_android / "app").mkdir(parents=True)
        (external_android / "settings.gradle").write_text("include ':app'\n")
        (external_android / "app/build.gradle").write_text("plugins {}\n")
        shutil.rmtree(android)
        android.symlink_to(external_android, target_is_directory=True)
    elif unsafe_path == "app_final":
        target = android / "app/build.gradle"
        target.unlink()
        target.symlink_to(sentinel)
    elif unsafe_path == "app_ancestor":
        external_app = outside / "app"
        external_app.mkdir()
        (external_app / "build.gradle").write_text("plugins {}\n")
        shutil.rmtree(android / "app")
        (android / "app").symlink_to(external_app, target_is_directory=True)
    elif unsafe_path == "application_final":
        application.unlink()
        application.symlink_to(sentinel)
    else:
        java = android / "app/src/main/java"
        external_java = outside / "java"
        external_application = external_java / "com/example/MainApplication.kt"
        external_application.parent.mkdir(parents=True)
        external_application.write_text(
            "fun packages() = PackageList(this).packages.apply {\n}\n"
        )
        shutil.rmtree(java)
        java.symlink_to(external_java, target_is_directory=True)
    sentinel_bytes = sentinel.read_bytes()
    sentinel_metadata = sentinel.lstat()

    with pytest.raises((ConfigurationError, FilesystemError)):
        GenerationService(root).plan(
            operation="bootstrap",
            requested_targets=(),
            allow_unmanifested_bootstrap=True,
        )

    after = sentinel.lstat()
    assert sentinel.read_bytes() == sentinel_bytes
    assert (after.st_mode, after.st_atime_ns, after.st_mtime_ns) == (
        sentinel_metadata.st_mode,
        sentinel_metadata.st_atime_ns,
        sentinel_metadata.st_mtime_ns,
    )
