import json
from pathlib import Path

from supernote_module_generator.config import ProjectConfig, normalize_backend
from supernote_module_generator.integration import read_metadata


def test_cpp_is_a_legacy_alias_for_jni():
    assert normalize_backend("cpp") == "jni"
    assert normalize_backend("jni") == "jni"


def test_project_config_writes_canonical_jni_metadata(tmp_path: Path):
    config = ProjectConfig(
        output=tmp_path / "local-jni",
        npm_name="local-jni",
        package_version="0.1.0",
        android_namespace="com.example.localjni",
        module_name="LocalJni",
        backend="cpp",
        native_library_name="sn_local_jni",
    )

    assert config.backend == "jni"
    assert config.metadata()["backend"] == "jni"
    assert "output" not in config.metadata()
    assert "force" not in config.metadata()


def test_read_metadata_normalizes_legacy_cpp_without_rewriting_file(
    tmp_path: Path,
):
    module = tmp_path / "local-jni"
    module.mkdir()
    metadata = module / ".supernote-module.json"
    metadata.write_text(
        json.dumps({"npm_name": "local-jni", "backend": "cpp"}),
        encoding="utf-8",
    )

    assert read_metadata(module)["backend"] == "jni"
    assert json.loads(metadata.read_text(encoding="utf-8"))["backend"] == "cpp"
