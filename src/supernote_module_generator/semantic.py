"""Backend-neutral Supernote API semantics for V2.

The records in this module answer what a Supernote API means.  They contain no
JNI descriptors, C++ include paths, adapter symbols, or generated source text.
Language frontends retain those facts in source models and lowering plans refer
back to both layers by stable identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, Iterable, Optional, Tuple


SEMANTIC_MANIFEST_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SemanticModelError(ValueError):
    """Raised when a backend-neutral API invariant is violated."""


class SemanticType(str, Enum):
    VOID = "void"
    BOOL = "bool"
    INT32 = "int32"
    INT64 = "int64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BYTES = "bytes"


class BindingKind(str, Enum):
    FUNCTION = "function"
    OBJECT_METHOD = "object_method"
    SERVICE_METHOD = "service_method"


class SemanticClassKind(str, Enum):
    JS_OBJECT = "js_object"
    INTERNAL_SERVICE = "internal_service"


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class DeclarationRole(str, Enum):
    ORDINARY = "ordinary"
    INTERNAL = "internal"
    EXPORTED = "exported"


@dataclass(frozen=True)
class BindingCapabilities:
    """Independent generated reachability and JavaScript publication flags."""

    routable: bool
    javascript_public: bool

    def __post_init__(self) -> None:
        if self.javascript_public and not self.routable:
            raise SemanticModelError(
                "a JavaScript-public declaration must also be generated/routable"
            )

    @classmethod
    def for_role(cls, role: DeclarationRole) -> "BindingCapabilities":
        if role is DeclarationRole.ORDINARY:
            return cls(False, False)
        if role is DeclarationRole.INTERNAL:
            return cls(True, False)
        if role is DeclarationRole.EXPORTED:
            return cls(True, True)
        raise SemanticModelError(f"unknown declaration role {role!r}")

    @property
    def role(self) -> DeclarationRole:
        if self.javascript_public:
            return DeclarationRole.EXPORTED
        if self.routable:
            return DeclarationRole.INTERNAL
        return DeclarationRole.ORDINARY


@dataclass(frozen=True)
class SourceProvenance:
    """Stable source identity and diagnostic location from a language frontend."""

    declaration_id: str
    language: str
    path: str
    line: int
    column: int = 1

    def __post_init__(self) -> None:
        if not self.declaration_id:
            raise SemanticModelError("source declaration identity cannot be empty")
        if not self.language:
            raise SemanticModelError("source language cannot be empty")
        if not self.path:
            raise SemanticModelError("source path cannot be empty")
        if self.line < 1 or self.column < 1:
            raise SemanticModelError("source line and column must be positive")

    def manifest(self) -> Dict[str, object]:
        return {
            "declaration_id": self.declaration_id,
            "language": self.language,
            "path": self.path,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True)
class SemanticParameter:
    name: str
    type: SemanticType

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "semantic parameter")
        if self.type is SemanticType.VOID:
            raise SemanticModelError("void is valid only as a result type")

    def manifest(self) -> Dict[str, str]:
        return {"name": self.name, "type": self.type.value}


@dataclass(frozen=True)
class SemanticBinding:
    """A generated function or method with common Supernote meaning.

    ``binding_id`` identifies the semantic API record.  It is deliberately
    separate from ``source.declaration_id``, which identifies what a language
    frontend discovered.
    """

    binding_id: str
    kind: BindingKind
    name: str
    capabilities: BindingCapabilities
    execution: ExecutionMode
    parameters: Tuple[SemanticParameter, ...]
    result: SemanticType
    source: SourceProvenance
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise SemanticModelError("semantic binding identity cannot be empty")
        if not self.capabilities.routable:
            raise SemanticModelError(
                "ordinary declarations do not become semantic bindings"
            )
        _validate_identifier(self.name, "semantic binding")
        _reject_duplicates(
            (parameter.name for parameter in self.parameters),
            f"parameter name in binding {self.name!r}",
        )
        is_member = self.kind in {
            BindingKind.OBJECT_METHOD,
            BindingKind.SERVICE_METHOD,
        }
        has_owner = self.owner_id is not None and self.owner_name is not None
        if is_member != has_owner:
            raise SemanticModelError(
                "object/service methods require owner identity and name; "
                "functions forbid them"
            )
        if (self.owner_id is None) != (self.owner_name is None):
            raise SemanticModelError("owner identity and name must be provided together")
        if self.owner_id is not None and not self.owner_id:
            raise SemanticModelError("semantic owner identity cannot be empty")
        if self.owner_name is not None:
            _validate_identifier(self.owner_name, "semantic owner")

    def manifest(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "binding_id": self.binding_id,
            "source_declaration_id": self.source.declaration_id,
            "kind": self.kind.value,
            "name": self.name,
            "routable": self.capabilities.routable,
            "javascript_public": self.capabilities.javascript_public,
            "execution": self.execution.value,
            "parameters": [parameter.manifest() for parameter in self.parameters],
            "result": self.result.value,
            "source": self.source.manifest(),
        }
        if self.owner_id is not None:
            value["owner_id"] = self.owner_id
            value["owner"] = self.owner_name
        return value


@dataclass(frozen=True)
class SemanticConstructor:
    """The selected construction path and its caller-visible value inputs.

    Runtime-injected JVM Context values are source/lowering facts and therefore
    never appear in ``parameters``.
    """

    source: SourceProvenance
    parameters: Tuple[SemanticParameter, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _reject_duplicates(
            (parameter.name for parameter in self.parameters),
            "constructor parameter name",
        )

    def manifest(self) -> Dict[str, object]:
        return {
            "source_declaration_id": self.source.declaration_id,
            "parameters": [parameter.manifest() for parameter in self.parameters],
            "source": self.source.manifest(),
        }


@dataclass(frozen=True)
class SemanticClass:
    """A JS-owned object type or a feature-owned internal service."""

    class_id: str
    kind: SemanticClassKind
    name: str
    capabilities: BindingCapabilities
    source: SourceProvenance
    constructor: SemanticConstructor
    methods: Tuple[SemanticBinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.class_id:
            raise SemanticModelError("semantic class identity cannot be empty")
        _validate_identifier(self.name, "semantic class")
        if not isinstance(self.constructor, SemanticConstructor):
            raise SemanticModelError("a generated class requires one selected constructor")
        if self.kind is SemanticClassKind.JS_OBJECT:
            if self.capabilities.role is not DeclarationRole.EXPORTED:
                raise SemanticModelError(
                    "a JS object class must be JavaScript-public and routable"
                )
            expected_method_kind = BindingKind.OBJECT_METHOD
        elif self.kind is SemanticClassKind.INTERNAL_SERVICE:
            if self.capabilities.role is not DeclarationRole.INTERNAL:
                raise SemanticModelError(
                    "an internal service class must be routable and hidden from JavaScript"
                )
            if self.constructor.parameters:
                raise SemanticModelError(
                    "an internal service constructor has no caller-visible parameters"
                )
            expected_method_kind = BindingKind.SERVICE_METHOD
        else:  # pragma: no cover - defensive against non-enum construction
            raise SemanticModelError(f"unknown semantic class kind {self.kind!r}")

        binding_ids = []
        source_ids = []
        method_names = []
        for method in self.methods:
            if method.kind is not expected_method_kind:
                raise SemanticModelError(
                    f"method {method.name!r} has kind {method.kind.value!r}; "
                    f"expected {expected_method_kind.value!r}"
                )
            if method.owner_id != self.class_id or method.owner_name != self.name:
                raise SemanticModelError(
                    f"method {method.name!r} does not belong to class {self.name!r}"
                )
            if (
                self.kind is SemanticClassKind.INTERNAL_SERVICE
                and method.capabilities.javascript_public
            ):
                raise SemanticModelError(
                    "an internal service method cannot be JavaScript-public"
                )
            binding_ids.append(method.binding_id)
            source_ids.append(method.source.declaration_id)
            method_names.append(method.name)

        _reject_duplicates(binding_ids, f"method binding identity on {self.name!r}")
        _reject_duplicates(source_ids, f"method source identity on {self.name!r}")
        _reject_duplicates(method_names, f"generated method name on {self.name!r}")

    def manifest(self) -> Dict[str, object]:
        return {
            "class_id": self.class_id,
            "source_declaration_id": self.source.declaration_id,
            "kind": self.kind.value,
            "name": self.name,
            "routable": self.capabilities.routable,
            "javascript_public": self.capabilities.javascript_public,
            "source": self.source.manifest(),
            "constructor": self.constructor.manifest(),
            "methods": [
                method.manifest()
                for method in sorted(self.methods, key=lambda item: item.binding_id)
            ],
        }


@dataclass(frozen=True)
class SemanticApi:
    functions: Tuple[SemanticBinding, ...] = field(default_factory=tuple)
    classes: Tuple[SemanticClass, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for binding in self.functions:
            if binding.kind is not BindingKind.FUNCTION:
                raise SemanticModelError(
                    "top-level semantic API bindings must have function kind"
                )

        all_bindings = list(self.functions)
        for item in self.classes:
            all_bindings.extend(item.methods)

        semantic_ids = [binding.binding_id for binding in all_bindings]
        semantic_ids.extend(item.class_id for item in self.classes)
        _reject_duplicates(semantic_ids, "semantic identity")

        source_ids = [binding.source.declaration_id for binding in all_bindings]
        source_ids.extend(item.source.declaration_id for item in self.classes)
        source_ids.extend(
            item.constructor.source.declaration_id for item in self.classes
        )
        _reject_duplicates(source_ids, "source declaration identity")

        public_names = [
            binding.name
            for binding in self.functions
            if binding.capabilities.javascript_public
        ]
        public_names.extend(
            item.name
            for item in self.classes
            if item.capabilities.javascript_public
        )
        _reject_duplicates(public_names, "JavaScript-public top-level name")

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": SEMANTIC_MANIFEST_SCHEMA_VERSION,
            "functions": [
                binding.manifest()
                for binding in sorted(
                    self.functions, key=lambda item: item.binding_id
                )
            ],
            "classes": [
                item.manifest()
                for item in sorted(self.classes, key=lambda value: value.class_id)
            ],
        }


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise SemanticModelError(f"invalid {label} name {value!r}")


def _reject_duplicates(values: Iterable[str], label: str) -> None:
    seen = set()  # type: set[str]
    for value in values:
        if value in seen:
            raise SemanticModelError(f"duplicate {label} {value!r}")
        seen.add(value)


def semantic_api_from_manifest(raw: object) -> SemanticApi:
    """Read the strict backend-neutral manifest used between build stages."""

    value = _manifest_object(raw, "semantic manifest")
    _manifest_keys(value, {"schema_version", "functions", "classes"}, "semantic manifest")
    schema = _manifest_int(value["schema_version"], "schema_version")
    if schema != SEMANTIC_MANIFEST_SCHEMA_VERSION:
        raise SemanticModelError(
            f"incompatible semantic manifest schema {schema}; "
            f"expected {SEMANTIC_MANIFEST_SCHEMA_VERSION}"
        )
    functions = tuple(
        _binding_from_manifest(item, f"functions[{index}]")
        for index, item in enumerate(_manifest_list(value["functions"], "functions"))
    )
    classes = tuple(
        _class_from_manifest(item, f"classes[{index}]")
        for index, item in enumerate(_manifest_list(value["classes"], "classes"))
    )
    return SemanticApi(functions, classes)


def merge_semantic_apis(*apis: SemanticApi) -> SemanticApi:
    """Merge language frontends and re-run all common identity/name checks."""

    return SemanticApi(
        tuple(binding for api in apis for binding in api.functions),
        tuple(item for api in apis for item in api.classes),
    )


def _binding_from_manifest(raw: object, label: str) -> SemanticBinding:
    value = _manifest_object(raw, label)
    required = {
        "binding_id", "source_declaration_id", "kind", "name", "routable",
        "javascript_public", "execution", "parameters", "result", "source",
    }
    optional = {"owner_id", "owner"}
    actual = set(value)
    if not required.issubset(actual) or actual - required - optional:
        _manifest_keys(value, required | (optional & actual), label)
    source = _source_from_manifest(value["source"], f"{label}.source")
    source_id = _manifest_string(
        value["source_declaration_id"], f"{label}.source_declaration_id"
    )
    if source_id != source.declaration_id:
        raise SemanticModelError(f"{label} source declaration identity disagrees")
    has_owner_id = "owner_id" in value
    has_owner = "owner" in value
    if has_owner_id != has_owner:
        raise SemanticModelError(f"{label} owner identity and name must appear together")
    try:
        return SemanticBinding(
            binding_id=_manifest_string(value["binding_id"], f"{label}.binding_id"),
            kind=BindingKind(_manifest_string(value["kind"], f"{label}.kind")),
            name=_manifest_string(value["name"], f"{label}.name"),
            capabilities=BindingCapabilities(
                _manifest_bool(value["routable"], f"{label}.routable"),
                _manifest_bool(
                    value["javascript_public"], f"{label}.javascript_public"
                ),
            ),
            execution=ExecutionMode(
                _manifest_string(value["execution"], f"{label}.execution")
            ),
            parameters=tuple(
                _parameter_from_manifest(item, f"{label}.parameters[{index}]")
                for index, item in enumerate(
                    _manifest_list(value["parameters"], f"{label}.parameters")
                )
            ),
            result=SemanticType(
                _manifest_string(value["result"], f"{label}.result")
            ),
            source=source,
            owner_id=(
                _manifest_string(value["owner_id"], f"{label}.owner_id")
                if has_owner_id
                else None
            ),
            owner_name=(
                _manifest_string(value["owner"], f"{label}.owner")
                if has_owner
                else None
            ),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _class_from_manifest(raw: object, label: str) -> SemanticClass:
    value = _manifest_object(raw, label)
    _manifest_keys(
        value,
        {
            "class_id", "source_declaration_id", "kind", "name", "routable",
            "javascript_public", "source", "constructor", "methods",
        },
        label,
    )
    source = _source_from_manifest(value["source"], f"{label}.source")
    if _manifest_string(
        value["source_declaration_id"], f"{label}.source_declaration_id"
    ) != source.declaration_id:
        raise SemanticModelError(f"{label} source declaration identity disagrees")
    constructor_value = _manifest_object(
        value["constructor"], f"{label}.constructor"
    )
    _manifest_keys(
        constructor_value,
        {"source_declaration_id", "parameters", "source"},
        f"{label}.constructor",
    )
    constructor_source = _source_from_manifest(
        constructor_value["source"], f"{label}.constructor.source"
    )
    if _manifest_string(
        constructor_value["source_declaration_id"],
        f"{label}.constructor.source_declaration_id",
    ) != constructor_source.declaration_id:
        raise SemanticModelError(f"{label} constructor source identity disagrees")
    try:
        return SemanticClass(
            class_id=_manifest_string(value["class_id"], f"{label}.class_id"),
            kind=SemanticClassKind(
                _manifest_string(value["kind"], f"{label}.kind")
            ),
            name=_manifest_string(value["name"], f"{label}.name"),
            capabilities=BindingCapabilities(
                _manifest_bool(value["routable"], f"{label}.routable"),
                _manifest_bool(
                    value["javascript_public"], f"{label}.javascript_public"
                ),
            ),
            source=source,
            constructor=SemanticConstructor(
                constructor_source,
                tuple(
                    _parameter_from_manifest(
                        item, f"{label}.constructor.parameters[{index}]"
                    )
                    for index, item in enumerate(
                        _manifest_list(
                            constructor_value["parameters"],
                            f"{label}.constructor.parameters",
                        )
                    )
                ),
            ),
            methods=tuple(
                _binding_from_manifest(item, f"{label}.methods[{index}]")
                for index, item in enumerate(
                    _manifest_list(value["methods"], f"{label}.methods")
                )
            ),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _parameter_from_manifest(raw: object, label: str) -> SemanticParameter:
    value = _manifest_object(raw, label)
    _manifest_keys(value, {"name", "type"}, label)
    try:
        return SemanticParameter(
            _manifest_string(value["name"], f"{label}.name"),
            SemanticType(_manifest_string(value["type"], f"{label}.type")),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _source_from_manifest(raw: object, label: str) -> SourceProvenance:
    value = _manifest_object(raw, label)
    _manifest_keys(
        value, {"declaration_id", "language", "path", "line", "column"}, label
    )
    try:
        return SourceProvenance(
            _manifest_string(value["declaration_id"], f"{label}.declaration_id"),
            _manifest_string(value["language"], f"{label}.language"),
            _manifest_string(value["path"], f"{label}.path"),
            _manifest_int(value["line"], f"{label}.line"),
            _manifest_int(value["column"], f"{label}.column"),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _manifest_keys(value: Dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise SemanticModelError(f"{label} has invalid fields: {'; '.join(details)}")


def _manifest_object(value: object, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticModelError(f"{label} must be an object")
    return value


def _manifest_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SemanticModelError(f"{label} must be an array")
    return value


def _manifest_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SemanticModelError(f"{label} must be a non-empty string")
    return value


def _manifest_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SemanticModelError(f"{label} must be an integer")
    return value


def _manifest_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise SemanticModelError(f"{label} must be a boolean")
    return value
