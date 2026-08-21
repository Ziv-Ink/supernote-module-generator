"""Recursive, backend-neutral V3 semantic types.

Source spellings and lowering ownership never appear here.  Named references
carry only the stable logical type identity that JavaScript and TypeScript use.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Dict, Optional


class SemanticTypeError(ValueError):
    """Raised when a recursive semantic type is impossible."""


class SemanticTypeKind(str, Enum):
    VOID = "void"
    SCALAR = "scalar"
    ENUM_REF = "enum_ref"
    VALUE_REF = "value_ref"
    OBJECT_REF = "object_ref"
    ARRAY = "array"
    NULLABLE = "nullable"


class ScalarKind(str, Enum):
    BOOL = "bool"
    INT32 = "int32"
    INT64 = "int64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BYTES = "bytes"


@dataclass(frozen=True)
class SemanticType:
    """One node in the immutable D-027 semantic type algebra."""

    kind: SemanticTypeKind
    scalar: Optional[ScalarKind] = None
    type_id: Optional[str] = None
    element: Optional["SemanticType"] = None

    VOID: ClassVar["SemanticType"]
    BOOL: ClassVar["SemanticType"]
    INT32: ClassVar["SemanticType"]
    INT64: ClassVar["SemanticType"]
    FLOAT32: ClassVar["SemanticType"]
    FLOAT64: ClassVar["SemanticType"]
    STRING: ClassVar["SemanticType"]
    BYTES: ClassVar["SemanticType"]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticTypeKind):
            raise SemanticTypeError(f"unknown semantic type kind {self.kind!r}")
        if self.kind is SemanticTypeKind.SCALAR:
            if not isinstance(self.scalar, ScalarKind):
                raise SemanticTypeError("a scalar semantic type requires a scalar kind")
            if self.type_id is not None or self.element is not None:
                raise SemanticTypeError("a scalar semantic type forbids reference payload")
            return
        if self.kind in {
            SemanticTypeKind.ENUM_REF,
            SemanticTypeKind.VALUE_REF,
            SemanticTypeKind.OBJECT_REF,
        }:
            if not isinstance(self.type_id, str) or not self.type_id:
                raise SemanticTypeError("a named semantic reference requires a type ID")
            if self.scalar is not None or self.element is not None:
                raise SemanticTypeError("a named semantic reference has only a type ID")
            return
        if self.kind in {SemanticTypeKind.ARRAY, SemanticTypeKind.NULLABLE}:
            if not isinstance(self.element, SemanticType):
                raise SemanticTypeError("a semantic wrapper requires an element type")
            if self.scalar is not None or self.type_id is not None:
                raise SemanticTypeError("a semantic wrapper has only an element type")
            if self.element.is_void:
                raise SemanticTypeError("void cannot be nested in a semantic type")
            if (
                self.kind is SemanticTypeKind.NULLABLE
                and self.element.kind is SemanticTypeKind.NULLABLE
            ):
                raise SemanticTypeError("nested nullable semantic types are forbidden")
            return
        if self.scalar is not None or self.type_id is not None or self.element is not None:
            raise SemanticTypeError("void has no semantic type payload")

    @classmethod
    def enum_ref(cls, type_id: str) -> "SemanticType":
        return cls(SemanticTypeKind.ENUM_REF, type_id=type_id)

    @classmethod
    def value_ref(cls, type_id: str) -> "SemanticType":
        return cls(SemanticTypeKind.VALUE_REF, type_id=type_id)

    @classmethod
    def object_ref(cls, type_id: str) -> "SemanticType":
        return cls(SemanticTypeKind.OBJECT_REF, type_id=type_id)

    @classmethod
    def array(cls, element: "SemanticType") -> "SemanticType":
        return cls(SemanticTypeKind.ARRAY, element=element)

    @classmethod
    def nullable(cls, inner: "SemanticType") -> "SemanticType":
        return cls(SemanticTypeKind.NULLABLE, element=inner)

    @property
    def is_void(self) -> bool:
        return self.kind is SemanticTypeKind.VOID

    @property
    def value(self) -> str:
        """Stable short label retained for scalar-oriented lowering code."""

        return self.scalar.value if self.scalar is not None else self.kind.value

    def manifest(self) -> Dict[str, object]:
        if self.kind is SemanticTypeKind.VOID:
            return {"kind": self.kind.value}
        if self.kind is SemanticTypeKind.SCALAR:
            assert self.scalar is not None
            return {"kind": self.kind.value, "name": self.scalar.value}
        if self.type_id is not None:
            return {"kind": self.kind.value, "type_id": self.type_id}
        assert self.element is not None
        key = "element" if self.kind is SemanticTypeKind.ARRAY else "inner"
        return {"kind": self.kind.value, key: self.element.manifest()}


SemanticType.VOID = SemanticType(SemanticTypeKind.VOID)
SemanticType.BOOL = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.BOOL)
SemanticType.INT32 = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.INT32)
SemanticType.INT64 = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.INT64)
SemanticType.FLOAT32 = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.FLOAT32)
SemanticType.FLOAT64 = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.FLOAT64)
SemanticType.STRING = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.STRING)
SemanticType.BYTES = SemanticType(SemanticTypeKind.SCALAR, ScalarKind.BYTES)

_SCALAR_TYPES = {
    value.scalar: value
    for value in (
        SemanticType.BOOL,
        SemanticType.INT32,
        SemanticType.INT64,
        SemanticType.FLOAT32,
        SemanticType.FLOAT64,
        SemanticType.STRING,
        SemanticType.BYTES,
    )
}


def semantic_type_from_manifest(raw: object, label: str = "semantic type") -> SemanticType:
    if not isinstance(raw, dict):
        raise SemanticTypeError(f"{label} must be an object")
    kind_value = raw.get("kind")
    if not isinstance(kind_value, str) or not kind_value:
        raise SemanticTypeError(f"{label}.kind must be a non-empty string")
    try:
        kind = SemanticTypeKind(kind_value)
    except ValueError as exc:
        raise SemanticTypeError(f"{label}.kind is invalid: {kind_value!r}") from exc

    expected = {"kind"}
    if kind is SemanticTypeKind.SCALAR:
        expected.add("name")
    elif kind in {
        SemanticTypeKind.ENUM_REF,
        SemanticTypeKind.VALUE_REF,
        SemanticTypeKind.OBJECT_REF,
    }:
        expected.add("type_id")
    elif kind is SemanticTypeKind.ARRAY:
        expected.add("element")
    elif kind is SemanticTypeKind.NULLABLE:
        expected.add("inner")
    _require_keys(raw, expected, label)

    try:
        if kind is SemanticTypeKind.VOID:
            return SemanticType.VOID
        if kind is SemanticTypeKind.SCALAR:
            name = raw["name"]
            if not isinstance(name, str):
                raise SemanticTypeError(f"{label}.name must be a string")
            return _SCALAR_TYPES[ScalarKind(name)]
        if kind in {
            SemanticTypeKind.ENUM_REF,
            SemanticTypeKind.VALUE_REF,
            SemanticTypeKind.OBJECT_REF,
        }:
            type_id = raw["type_id"]
            if not isinstance(type_id, str) or not type_id:
                raise SemanticTypeError(f"{label}.type_id must be a non-empty string")
            return SemanticType(kind, type_id=type_id)
        key = "element" if kind is SemanticTypeKind.ARRAY else "inner"
        return SemanticType(
            kind,
            element=semantic_type_from_manifest(raw[key], f"{label}.{key}"),
        )
    except (KeyError, ValueError) as exc:
        raise SemanticTypeError(f"{label} is invalid: {exc}") from exc


def _require_keys(value: Dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unknown " + ", ".join(extra))
    raise SemanticTypeError(f"{label} has invalid fields: {'; '.join(details)}")
