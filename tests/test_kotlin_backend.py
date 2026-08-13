import json
from pathlib import Path

from supernote_module_generator.config import ProjectConfig
from supernote_module_generator.generator import generate, stage


def _config(tmp_path: Path, **changes) -> ProjectConfig:
    values = {
        "output": tmp_path / "local-kotlin",
        "npm_name": "local-kotlin",
        "package_version": "0.2.0",
        "android_namespace": "com.example.kotlin",
        "module_name": "KotlinApi",
        "description": "Kotlin generator fixture",
    }
    values.update(changes)
    return ProjectConfig(**values)


def test_new_kotlin_module_uses_only_canonical_export_and_types(tmp_path):
    module = generate(_config(tmp_path))
    annotation_root = (
        module
        / "android/.native-module/annotation/src/main/java/com/example/kotlin"
        / "nativemodule/annotation"
    )
    assert (annotation_root / "SupernotePluginExport.java").is_file()
    assert not (annotation_root / "ReactNativeExport.java").exists()
    assert "@SupernotePluginExport" in (
        module / "android/src/main/java/com/example/kotlin/Example.kt"
    ).read_text(encoding="utf-8")

    package = json.loads((module / "package.json").read_text(encoding="utf-8"))
    assert package["types"] == "index.d.ts"
    assert "index.d.ts" in package["files"]
    assert package["peerDependencies"]["react-native"] == "*"
    declarations = (module / "index.d.ts").read_text(encoding="utf-8")
    assert "add(left: number, right: number): Promise<number>;" in declarations
    assert "setEnabled(enabled: boolean): void;" in declarations

    provider = (
        module
        / "android/.native-module/processor/src/main/resources/META-INF/services"
        / "com.google.devtools.ksp.processing.SymbolProcessorProvider"
    ).read_text(encoding="utf-8")
    assert provider.strip() == "localmodule.processor.SupernoteExportProcessorProvider"


def test_update_preserves_legacy_source_and_previous_declarations(tmp_path):
    module = generate(_config(tmp_path))
    source = module / "android/src/main/java/com/example/kotlin/Example.kt"
    source.write_text(
        source.read_text(encoding="utf-8")
        .replace("SupernotePluginExport", "ReactNativeExport")
        .replace(
            "com.example.kotlin.nativemodule.annotation.ReactNativeExport",
            "com.example.kotlin.nativemodule.annotation.ReactNativeExport",
        ),
        encoding="utf-8",
    )
    previous_types = "/* previous successful build */\ndeclare const old: true;\n"
    (module / "index.d.ts").write_text(previous_types, encoding="utf-8")

    staged = stage(
        _config(tmp_path, force=True),
        preserve_api_from=module,
    )
    annotation_root = (
        staged
        / "android/.native-module/annotation/src/main/java/com/example/kotlin"
        / "nativemodule/annotation"
    )
    assert (annotation_root / "SupernotePluginExport.java").is_file()
    legacy = annotation_root / "ReactNativeExport.java"
    assert legacy.is_file()
    assert "@Deprecated" in legacy.read_text(encoding="utf-8")
    assert "@ReactNativeExport" in (
        staged / "android/src/main/java/com/example/kotlin/Example.kt"
    ).read_text(encoding="utf-8")
    assert (staged / "index.d.ts").read_text(encoding="utf-8") == previous_types


def test_update_does_not_retain_legacy_annotation_for_comments_only(tmp_path):
    module = generate(_config(tmp_path))
    source = module / "android/src/main/java/com/example/kotlin/Example.kt"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n/* Old docs mentioned @ReactNativeExport here. */\n",
        encoding="utf-8",
    )
    staged = stage(
        _config(tmp_path, force=True),
        preserve_api_from=module,
    )
    legacy = (
        staged
        / "android/.native-module/annotation/src/main/java/com/example/kotlin"
        / "nativemodule/annotation/ReactNativeExport.java"
    )
    assert not legacy.exists()


def test_update_does_not_restore_deleted_starter_source(tmp_path):
    module = generate(_config(tmp_path))
    source = module / "android/src/main/java/com/example/kotlin/Example.kt"
    source.unlink()

    staged = stage(
        _config(tmp_path, force=True),
        preserve_api_from=module,
    )

    assert not (
        staged / "android/src/main/java/com/example/kotlin/Example.kt"
    ).exists()


def test_update_preserves_sources_outside_namespace_but_not_generated_package(
    tmp_path,
):
    module = generate(_config(tmp_path))
    external = module / "android/src/main/java/org/example/device/DeviceApi.kt"
    external.parent.mkdir(parents=True)
    external.write_text(
        "package org.example.device\nclass DeviceApi\n",
        encoding="utf-8",
    )
    legacy_external = (
        module / "android/src/main/kotlin/net/example/legacy/LegacyApi.java"
    )
    legacy_external.parent.mkdir(parents=True)
    legacy_external.write_text(
        "package net.example.legacy;\npublic class LegacyApi {}\n",
        encoding="utf-8",
    )
    generated = (
        module
        / "android/src/main/java/com/example/kotlin/generated/StaleWrapper.kt"
    )
    generated.parent.mkdir(parents=True)
    generated.write_text("generator-owned stale wrapper\n", encoding="utf-8")

    staged = stage(
        _config(tmp_path, force=True),
        preserve_api_from=module,
    )
    assert (
        staged / "android/src/main/java/org/example/device/DeviceApi.kt"
    ).read_text(encoding="utf-8") == external.read_text(encoding="utf-8")
    assert (
        staged / "android/src/main/java/net/example/legacy/LegacyApi.java"
    ).read_text(encoding="utf-8") == legacy_external.read_text(encoding="utf-8")
    assert not (
        staged
        / "android/src/main/java/com/example/kotlin/generated/StaleWrapper.kt"
    ).exists()


def test_processor_contains_context_precedence_types_and_void_logging(tmp_path):
    module = generate(_config(tmp_path))
    processor = (
        module
        / "android/.native-module/processor/src/main/kotlin/localmodule/processor"
        / "SupernoteExportProcessor.kt"
    ).read_text(encoding="utf-8")
    constructor_selection = processor[
        processor.index("return when {", processor.index("private fun selectConstructor"))
        : processor.index("\n  private fun generate(", processor.index("private fun selectConstructor"))
    ]
    assert (
        constructor_selection.index(
            "com.facebook.react.bridge.ReactApplicationContext"
        )
        < constructor_selection.index('"android.content.Context"')
        < constructor_selection.index("publicConstructors.any")
    )
    assert "reactApplicationContext" in processor
    assert "Modifier.INNER in parent.modifiers" in processor
    assert "parent.origin == Origin.JAVA" in processor
    assert "apply exactly one export annotation" in processor
    assert '"getName"' in processor
    assert "conflicts with generated React Native module" in processor
    assert "Dependencies(true, *sourceFiles)" in processor
    assert "kotlinQualifiedIdentifier(export.owner)" in processor
    assert '"`$value`"' in processor
    assert "File(output).writeText(text, Charsets.UTF_8)" in processor
    assert 'Log.e(LOG_TAG, $failureMessage, failure)' in processor

    gradle = (module / "android/build.gradle.kts").read_text(encoding="utf-8")
    assert 'arg("localTypescriptOutput"' in gradle
    assert "outputs.file(supernoteTypescriptDeclarations)" in gradle
