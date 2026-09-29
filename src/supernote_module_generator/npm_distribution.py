"""Versioned, package-local npm distribution metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping

from .feature_operations import FeatureRecord
from .package_integration_codegen import (
    NATIVE_RUNTIME_ABI,
    feature_cmake_target,
    package_jvm_installer_symbol,
    package_native_installer_symbol,
)
from .schemas import (
    NPM_DISTRIBUTION_KIND,
    NPM_DISTRIBUTION_SCHEMA_VERSION,
    NPM_MODULE_PROTOCOL_VERSION,
)


PACKAGE_MANIFEST_PATH = ".supernote-generated/package-manifest.json"
PACKAGE_REGISTRATION_PATH = ".supernote-generated/android/registration.json"
PACKAGE_CMAKE_PATH = ".supernote-generated/android/CMakeLists.txt"
PACKAGE_JVM_REGISTRATION_PATH = (
    ".supernote-generated/android/jvm/JvmRegistration.java"
)
PACKAGE_JVM_ADAPTER_PATH = ".supernote-generated/android/jvm/KspAdapters.kt"
PACKAGE_NATIVE_REGISTRATION_HEADER_PATH = (
    ".supernote-generated/android/package_registration.hpp"
)
PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH = (
    ".supernote-generated/android/package_registration.cpp"
)


def jvm_adapter_path(feature_id: str) -> str:
    """Return the deterministic package path for one KSP adapter source."""

    return PACKAGE_JVM_ADAPTER_PATH


def ksp_adapter_output_name(feature_id: str) -> str:
    """Return the filename emitted by the publisher-side KSP processor."""

    digest = hashlib.sha256(feature_id.encode("utf-8")).hexdigest()[:20]
    return f"FeatureAdapters_{digest}.kt"


def render_registration(
    record: FeatureRecord,
    *,
    has_native_sources: bool,
    has_jvm_sources: bool,
    has_jvm_bindings: bool,
    has_jvm_adapter: bool,
) -> bytes:
    """Describe already-generated package inputs without reconstructing semantics."""

    bindings = [
        ".supernote-generated/android/feature.cpp",
        ".supernote-generated/android/internal.cpp",
        PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH,
    ]
    if has_jvm_bindings:
        bindings.append(".supernote-generated/android/jvm_feature.cpp")
    value = {
        "schemaVersion": NPM_DISTRIBUTION_SCHEMA_VERSION,
        "kind": "supernote_module_registration",
        "protocol": NPM_MODULE_PROTOCOL_VERSION,
        "featureId": record.manifest.feature_id,
        "packageName": record.manifest.npm_name,
        "publicName": record.manifest.public_name,
        "androidNamespace": record.manifest.android_namespace,
        "nativeAbi": NATIVE_RUNTIME_ABI,
        "installers": {
            "native": {
                "source": PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH,
                "symbol": package_native_installer_symbol(
                    record.manifest.feature_id
                ),
            },
            "jvm": (
                {
                    "source": PACKAGE_NATIVE_REGISTRATION_SOURCE_PATH,
                    "symbol": package_jvm_installer_symbol(
                        record.manifest.feature_id
                    ),
                }
                if has_jvm_bindings
                else None
            ),
        },
        "cmake": PACKAGE_CMAKE_PATH,
        "cmakeTarget": feature_cmake_target(record.manifest.feature_id),
        "nativeSourceDirs": (
            [record.manifest.roots.native] if has_native_sources else []
        ),
        "jvmSourceDirs": [record.manifest.roots.jvm] if has_jvm_sources else [],
        "bindingSources": bindings,
        "bindingHeaders": [
            ".supernote-generated/android/internal.hpp",
            PACKAGE_NATIVE_REGISTRATION_HEADER_PATH,
        ],
        "jvmRegistrationSources": [
            PACKAGE_JVM_REGISTRATION_PATH,
            *(
                [jvm_adapter_path(record.manifest.feature_id)]
                if has_jvm_adapter
                else []
            ),
        ],
        "semantic": ".supernote-generated/android/semantic.json",
        "conversion": ".supernote-generated/android/conversion.json",
    }
    return _json_bytes(value)


def render_package_manifest(
    record: FeatureRecord,
    payload: Mapping[str, bytes],
) -> bytes:
    """Bind package identity to every publisher-supplied consumer input byte."""

    rows = []
    for path, content in sorted(payload.items()):
        normalized = PurePosixPath(path)
        if (
            not path
            or normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in {"", ".", ".."} for part in normalized.parts)
            or path == PACKAGE_MANIFEST_PATH
        ):
            raise ValueError(f"invalid npm distribution payload path: {path!r}")
        rows.append(
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if not rows:
        raise ValueError("npm distribution payload cannot be empty")
    value = {
        "schemaVersion": NPM_DISTRIBUTION_SCHEMA_VERSION,
        "kind": NPM_DISTRIBUTION_KIND,
        "protocol": NPM_MODULE_PROTOCOL_VERSION,
        "featureId": record.manifest.feature_id,
        "packageName": record.manifest.npm_name,
        "packageVersion": record.package_version,
        "publicName": record.manifest.public_name,
        "androidNamespace": record.manifest.android_namespace,
        "registration": PACKAGE_REGISTRATION_PATH,
        "payload": rows,
    }
    return _json_bytes(value)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
