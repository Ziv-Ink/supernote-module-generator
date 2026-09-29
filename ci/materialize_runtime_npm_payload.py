"""Materialize and verify the checked-in @supernote/runtime native payload."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "npm/supernote-runtime"
GENERATED_PATHS = {
    "include/supernote/conversion.hpp": "native/include/supernote/conversion.hpp",
    "include/supernote/cpp_objects.hpp": "native/include/supernote/cpp_objects.hpp",
    "include/supernote/runtime.hpp": "native/include/supernote/runtime.hpp",
    "src/runtime_services.hpp": "native/src/runtime_services.hpp",
    "src/runtime_services.cpp": "native/src/runtime_services.cpp",
    "src/runtime_bootstrap.cpp": "native/src/runtime_bootstrap.cpp",
    "src/runtime_registration_bridge.c": "native/src/runtime_registration_bridge.c",
    "src/main/java/supernote/generated/runtime/SupernoteModule.kt": (
        "android/src/main/java/supernote/generated/runtime/SupernoteModule.kt"
    ),
    "src/main/java/supernote/generated/runtime/SupernoteCoroutineBridge.kt": (
        "jvm/supernote/generated/runtime/SupernoteCoroutineBridge.kt"
    ),
    "src/main/java/supernote/generated/runtime/SupernoteConversionBudget.kt": (
        "jvm/supernote/generated/runtime/SupernoteConversionBudget.kt"
    ),
    "consumer-rules.pro": "consumer-rules.pro",
    **{
        (
            "annotations/src/main/java/supernote/generated/annotations/"
            f"{name}.java"
        ): f"jvm/supernote/generated/annotations/{name}.java"
        for name in (
            "SupernoteConstructor",
            "SupernotePluginAsync",
            "SupernotePluginExport",
            "SupernotePluginInternal",
            "SupernotePluginObject",
            "SupernotePluginValue",
        )
    },
}

STATIC_PATHS = (
    "android/build.gradle",
    "android/src/main/AndroidManifest.xml",
    "cmake/CMakeLists.txt",
    "cmake/SupernoteModules.cmake",
    "compose-packages.js",
    "gradle/supernote-modules.gradle",
    "react-native.config.js",
    "resolve-packages.js",
)


def _payload() -> dict[str, bytes]:
    sys.path.insert(0, str(ROOT / "src"))
    from supernote_module_generator.feature_model import PluginRuntimeRegistry
    from supernote_module_generator.plugin_runtime_codegen import generated_runtime_files

    registry = PluginRuntimeRegistry.create(
        plugin_id="npm-runtime-payload",
        generator_version="4.0.0.dev0",
        features=(),
    )
    generated = generated_runtime_files(registry)
    payload = {
        destination: generated[source].encode("utf-8")
        for source, destination in GENERATED_PATHS.items()
    }
    payload.update(
        (relative, (RUNTIME_ROOT / relative).read_bytes())
        for relative in STATIC_PATHS
    )
    return payload


def _manifest(payload: dict[str, bytes]) -> bytes:
    value = {
        "schemaVersion": "2.0",
        "kind": "supernote_runtime_distribution",
        "protocol": "2.0",
        "nativeAbi": "supernote-jsi-v1",
        "payload": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(payload.items())
        ],
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    expected = _payload()
    expected["runtime-manifest.json"] = _manifest(expected)
    failures: list[str] = []
    for relative, content in expected.items():
        destination = RUNTIME_ROOT / relative
        if arguments.write:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        elif not destination.is_file() or destination.read_bytes() != content:
            failures.append(relative)
    if failures:
        print("stale @supernote/runtime payload: " + ", ".join(failures))
        return 1
    print("@supernote/runtime native payload is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
