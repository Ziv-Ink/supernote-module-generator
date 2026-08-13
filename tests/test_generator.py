import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from supernote_module_generator.config import ProjectConfig, native_class_prefix
from supernote_module_generator.generator import generate


def config(tmp_path: Path, **changes):
    values = dict(output=tmp_path / "react-native-math", npm_name="react-native-math", package_version="0.1.0", android_namespace="com.example.math", module_name="Math", description="A \"quoted\" example")
    values.update(changes)
    return ProjectConfig(**values)


def test_android_class_prefix_is_clean_pascal_case():
    assert native_class_prefix("local_math") == "LocalMath"
    assert native_class_prefix("@team/react-native-math-plugin") == "Math"


def test_kotlin_only_project_is_complete(tmp_path):
    path = generate(config(tmp_path))
    package = json.loads((path / "package.json").read_text())
    assert package["name"] == "react-native-math"
    index = (path / "index.js").read_text()
    readme = (path / "README.md").read_text()
    assert "const Math =" in index
    assert "export default Math;" in index
    assert "import Math from 'react-native-math';" in readme
    assert "NativeCodeModule" not in readme
    assert "externalNativeBuild" not in (path / "android/build.gradle.kts").read_text()
    example = path / "android/src/main/java/com/example/math/Example.kt"
    assert example.is_file()
    assert "package com.example.math" in example.read_text()
    assert "fun add(left: Double, right: Double): Double" in example.read_text()
    assert (path / "android/.native-module/processor/build.gradle.kts").is_file()
    assert (path / "android/autolink/MathNativeModulePackage.kt").is_file()
    assert "com.example.math.nativemodule.annotation.SupernotePluginExport" in readme
    assert (path / "android/.native-module/annotation/src/main/java/com/example/math/nativemodule/annotation/SupernotePluginExport.java").is_file()
    assert not (path / "android/.native-module/annotation/src/main/java/com/example/math/nativemodule/annotation/ReactNativeExport.java").exists()
    assert not (path / "android/src/main/kotlin").exists()
    assert "GeneratedNativeModule" in (path / "android/.native-module/processor/src/main/kotlin/localmodule/processor/SupernoteExportProcessor.kt").read_text()
    assert (path / ".supernote-module.json").is_file()
    assert not (path / "example").exists()
    assert (path / "index.d.ts").is_file()


def test_deterministic_rendering(tmp_path):
    first = generate(config(tmp_path, output=tmp_path / "one"))
    second = generate(config(tmp_path, output=tmp_path / "two"))
    assert (first / "android/build.gradle.kts").read_text() == (second / "android/build.gradle.kts").read_text()


def test_annotation_is_unique_to_each_android_namespace(tmp_path):
    first = generate(config(tmp_path, output=tmp_path / "one", npm_name="local-one", android_namespace="com.example.one", module_name="One"))
    second = generate(config(tmp_path, output=tmp_path / "two", npm_name="local-two", android_namespace="com.example.two", module_name="Two"))
    assert (first / "android/.native-module/annotation/src/main/java/com/example/one/nativemodule/annotation/SupernotePluginExport.java").is_file()
    assert (second / "android/.native-module/annotation/src/main/java/com/example/two/nativemodule/annotation/SupernotePluginExport.java").is_file()
    assert "compileOnly(annotationProject)" in (first / "android/build.gradle.kts").read_text()


def test_force_replaces_only_generated_project(tmp_path):
    target = tmp_path / "x"
    generate(config(tmp_path, output=target))
    (target / "old").write_text("old")
    generate(config(tmp_path, output=target, force=True))
    assert not (target / "old").exists()


def test_native_codegen_is_self_contained_and_checkable(tmp_path):
    module = generate(
        config(
            tmp_path,
            output=tmp_path / "local-jsi",
            npm_name="local-jsi",
            module_name="LocalJsi",
            backend="jsi",
            native_library_name="sn_local_jsi_test",
            jsi_global_name="__supernoteLocalJsiTest",
        )
    )
    codegen_root = module / "android/.supernote-module"
    support_root = codegen_root / "supernote_codegen"
    expected_support = {
        "__init__.py",
        "cpp_projection.py",
        "lowering.py",
        "semantic.py",
        "source_models.py",
    }
    assert {path.name for path in support_root.iterdir()} == expected_support

    metadata = json.loads((module / ".supernote-module.json").read_text())
    for name in expected_support:
        assert (
            f"android/.supernote-module/supernote_codegen/{name}"
            in metadata["generated_files"]
        )

    build_script = (module / "android/build.gradle.kts").read_text()
    assert 'fileTree(".supernote-module/supernote_codegen")' in build_script
    assert '"-B"' in build_script
    starter = (module / "android/src/main/cpp/text.cpp").read_text()
    assert "// @SupernotePluginExport\nstd::string greet(" in starter
    assert "SupernotePluginExport(" not in starter
    index = (module / "index.js").read_text()
    assert "export class SupernoteError extends Error" in index
    assert "this.name = 'SupernoteError'" in index
    assert "this.code = code" in index
    assert "Object.defineProperty(value, ERROR_CONSTRUCTOR_PROPERTY" in index
    assert "property === ERROR_CONSTRUCTOR_PROPERTY" in index

    shutil.rmtree(module / "android/build/generated/supernote")
    (module / "index.d.ts").unlink()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        "-B",
        str(codegen_root / "codegen.py"),
        "--module-root",
        str(module),
    ]
    generated = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    checked = subprocess.run(
        [*command, "--check"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
