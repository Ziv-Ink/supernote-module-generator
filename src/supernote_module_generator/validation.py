from __future__ import annotations

import re
from pathlib import Path

from .config import BACKENDS, METADATA_FILES, ProjectConfig
from .errors import DestinationConflict, ValidationError

NPM_NAME = re.compile(r"^(?:@[-a-z0-9~][-_a-z0-9.~]*/)?[a-z0-9~][-_a-z0-9.~]*$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODULE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RESERVED = {"con", "prn", "aux", "nul", "com1", "lpt1"}
JAVASCRIPT_RESERVED = {
    "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "enum", "export",
    "extends", "false", "finally", "for", "function", "if", "implements",
    "import", "in", "instanceof", "interface", "let", "new", "null",
    "package", "private", "protected", "public", "return", "static",
    "super", "switch", "this", "throw", "true", "try", "typeof", "var",
    "void", "while", "with", "yield",
}
JAVA_RESERVED = {
    "_", "abstract", "assert", "boolean", "break", "byte", "case", "catch",
    "char", "class", "const", "continue", "default", "do", "double", "else",
    "enum", "exports", "extends", "false", "final", "finally", "float", "for",
    "goto", "if", "implements", "import", "instanceof", "int", "interface",
    "long", "module", "native", "new", "non-sealed", "null", "open", "opens",
    "package", "permits", "private", "protected", "provides", "public",
    "record", "requires", "return", "sealed", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws", "to",
    "transient", "transitive", "true", "try", "uses", "var", "void",
    "volatile", "while", "with", "yield",
}
KOTLIN_RESERVED = {
    "_", "actual", "abstract", "annotation", "as", "break", "by", "catch",
    "class", "companion", "const", "constructor", "continue", "crossinline",
    "data", "delegate", "do", "dynamic", "else", "enum", "expect", "external",
    "false", "field", "file", "final", "finally", "for", "fun", "get", "if",
    "import", "in", "infix", "init", "inline", "inner", "interface",
    "internal", "is", "it", "lateinit", "noinline", "null", "object", "open",
    "operator", "out", "override", "package", "param", "private", "property",
    "protected", "public", "receiver", "reified", "return", "sealed", "set",
    "setparam", "super", "suspend", "tailrec", "this", "throw", "true", "try",
    "typealias", "typeof", "val", "var", "vararg", "when", "where", "while",
}
ANDROID_PACKAGE_RESERVED = JAVA_RESERVED | KOTLIN_RESERVED


def validate_npm_name(value: str) -> None:
    if not NPM_NAME.fullmatch(value) or value.endswith(".") or value.endswith("/"):
        raise ValidationError(f"Invalid npm package name: {value!r}")


def validate_semver(value: str) -> None:
    if not SEMVER.fullmatch(value):
        raise ValidationError(f"Invalid semantic version: {value!r}")


def validate_namespace(value: str) -> None:
    parts = value.split(".")
    if len(parts) < 2 or any(
        not IDENTIFIER.fullmatch(part) or part in ANDROID_PACKAGE_RESERVED
        for part in parts
    ):
        raise ValidationError(f"Invalid Android namespace: {value!r}")


def validate_module_name(value: str) -> None:
    if not MODULE.fullmatch(value) or value in JAVASCRIPT_RESERVED:
        raise ValidationError(
            "Module name must be a non-reserved JavaScript identifier "
            "beginning with a letter"
        )


def validate_output(path: Path, force: bool) -> None:
    if not str(path) or ".." in path.parts:
        raise ValidationError("Output path must not contain '..'")
    if path.name.lower() in RESERVED:
        raise ValidationError(f"Output directory uses reserved filename: {path.name}")
    if path.exists() and any(path.iterdir()) and not force:
        raise DestinationConflict(f"Destination is non-empty: {path}. Use --force only for a generated project.")
    if path.exists() and force and not any((path / name).exists() for name in METADATA_FILES):
        raise DestinationConflict("--force refuses to overwrite a directory not created by this generator")


def validate_config(config: ProjectConfig) -> None:
    validate_npm_name(config.npm_name)
    validate_semver(config.package_version)
    validate_namespace(config.android_namespace)
    validate_module_name(config.module_name)
    if config.backend not in BACKENDS:
        raise ValidationError(
            f"Invalid module backend {config.backend!r}; expected one of: "
            + ", ".join(BACKENDS)
        )
    if config.backend in {"jni", "jsi"}:
        if not config.native_library_name or not IDENTIFIER.fullmatch(config.native_library_name):
            raise ValidationError("C/C++ modules require a valid native library name")
    if config.backend == "jsi":
        if not config.jsi_global_name or not IDENTIFIER.fullmatch(config.jsi_global_name):
            raise ValidationError("JSI modules require a valid private global name")
    if "\n" in config.description or "\r" in config.description:
        raise ValidationError("Description must contain one line")
    if not 21 <= config.min_sdk <= 35:
        raise ValidationError("min-sdk must be between 21 and 35")
    validate_output(config.output, config.force)


def package_path(namespace: str) -> str:
    validate_namespace(namespace)
    return namespace.replace(".", "/")
