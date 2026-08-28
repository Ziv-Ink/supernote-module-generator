"""Versioned public identifier inference and strict field validation."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .errors import ValidationError

NPM_NAME = re.compile(r"^(?:@[-a-z0-9~][-_a-z0-9.~]*/)?[a-z0-9~][-_a-z0-9.~]*$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODULE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
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

SEPARATORS = re.compile(r"[-_.~]+")
ASCII_WHITESPACE = " \t\r\n\v\f"
BIDI_CLASSES = {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"}


@dataclass(frozen=True)
class Normalized:
    value: str
    changed: bool


def strip_ascii(value: str) -> Normalized:
    stripped = value.strip(ASCII_WHITESPACE)
    return Normalized(stripped, stripped != value)


def source_tokens(package_name: str) -> List[str]:
    leaf = package_name.rsplit("/", 1)[-1]
    if leaf.startswith("react-native-"):
        leaf = leaf[len("react-native-") :]
    if leaf.startswith("local-"):
        leaf = leaf[len("local-") :]
    if leaf.endswith("-plugin"):
        leaf = leaf[: -len("-plugin")]
    return [token for token in SEPARATORS.split(leaf) if token]


def infer_javascript_name(package_name: str) -> str:
    value = "".join(token[:1].upper() + token[1:] for token in source_tokens(package_name))
    if not MODULE.fullmatch(value) or value in JAVASCRIPT_RESERVED:
        raise ValidationError(
            f'Could not derive a valid JavaScript name from "{package_name}".',
            field="javascript_name",
        )
    return value


def infer_android_namespace(package_name: str) -> str:
    leaf = "_".join(source_tokens(package_name)).lower()
    value = f"com.example.{leaf}"
    try:
        validate_android_namespace(value)
    except ValidationError as exc:
        raise ValidationError(
            f'Could not derive a valid Android namespace from "{package_name}".',
            field="android_namespace",
        ) from exc
    return value


def _reject_non_ascii(value: str, label: str) -> None:
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} must contain ASCII characters only.") from exc


def _reject_invisible(value: str, label: str) -> None:
    for character in value:
        category = unicodedata.category(character)
        if (
            category == "Cc"
            or unicodedata.bidirectional(character) in BIDI_CLASSES
            or (category == "Cf" and character not in {"\u200c", "\u200d"})
        ):
            raise ValidationError(f"{label} contains a control or invisible character.")


def validate_package_name(value: str) -> None:
    _reject_non_ascii(value, "Package name")
    _reject_invisible(value, "Package name")
    if len(value) > 214:
        raise ValidationError(
            "Package name is longer than the 214-character limit.",
            field="package_name",
        )
    if (
        not NPM_NAME.fullmatch(value)
        or value.endswith(".")
        or value.endswith("/")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValidationError(
            f'invalid package name "{value}"', field="package_name"
        )
    for component in value.split("/"):
        component = component.removeprefix("@")
        if len(component) > 120:
            raise ValidationError(
                "A generated filename component exceeds the 120-character limit.",
                field="package_name",
            )


def validate_javascript_name(value: str) -> None:
    _reject_non_ascii(value, "JavaScript name")
    _reject_invisible(value, "JavaScript name")
    if not MODULE.fullmatch(value) or value in JAVASCRIPT_RESERVED:
        raise ValidationError(
            f'invalid JavaScript name "{value}"', field="javascript_name"
        )


def validate_android_namespace(value: str) -> None:
    _reject_non_ascii(value, "Android namespace")
    _reject_invisible(value, "Android namespace")
    parts = value.split(".")
    if len(parts) < 2 or any(
        not IDENTIFIER.fullmatch(part) or part in ANDROID_PACKAGE_RESERVED
        for part in parts
    ):
        raise ValidationError(
            f'invalid Android namespace "{value}"', field="android_namespace"
        )


def validate_package_version(value: str) -> None:
    _reject_non_ascii(value, "Package version")
    _reject_invisible(value, "Package version")
    if not SEMVER.fullmatch(value):
        raise ValidationError(
            f'invalid package version "{value}"', field="package_version"
        )


def normalize_description(value: str) -> str:
    result = value.strip()
    if "\n" in result or "\r" in result:
        raise ValidationError("Description must contain one line.", field="description")
    _reject_invisible(result, "Description")
    return result


def validate_generated_paths(root: Path, package_name: str, namespace: str) -> None:
    relative = Path("local_modules") / package_name
    if len(relative.as_posix()) > 180:
        raise ValidationError(
            "Generated relative paths exceed the 180-character limit.",
            field="package_name",
        )
    namespace_path = Path(*namespace.split("."))
    generated = relative / "android" / "src" / "main" / "java" / namespace_path
    if len(generated.as_posix()) > 180:
        raise ValidationError(
            "Generated relative paths exceed the 180-character limit.",
            field="android_namespace",
        )
    absolute = root / generated
    # Android remains the narrowest supported absolute-path budget.
    if len(str(absolute)) > 240:
        raise ValidationError(
            "The generated absolute path exceeds the 240-character target limit.",
            field="android_namespace",
        )
