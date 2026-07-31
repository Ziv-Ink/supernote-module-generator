import json
from pathlib import Path

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
    assert "com.example.math.nativemodule.annotation.SupernoteExport" in readme
    assert (path / "android/.native-module/annotation/src/main/java/com/example/math/nativemodule/annotation/SupernoteExport.java").is_file()
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
    assert (first / "android/.native-module/annotation/src/main/java/com/example/one/nativemodule/annotation/SupernoteExport.java").is_file()
    assert (second / "android/.native-module/annotation/src/main/java/com/example/two/nativemodule/annotation/SupernoteExport.java").is_file()
    assert "compileOnly(annotationProject)" in (first / "android/build.gradle.kts").read_text()


def test_force_replaces_only_generated_project(tmp_path):
    target = tmp_path / "x"
    generate(config(tmp_path, output=target))
    (target / "old").write_text("old")
    generate(config(tmp_path, output=target, force=True))
    assert not (target / "old").exists()
