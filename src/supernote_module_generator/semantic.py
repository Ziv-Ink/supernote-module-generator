"""Backend-neutral Supernote API semantics.

The records in this module answer what a Supernote API means.  They contain no
JNI descriptors, C++ include paths, adapter symbols, or generated source text.
Language frontends retain those facts in source models and lowering plans refer
back to both layers by stable identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, Iterable, Optional, Tuple, Union

from .schemas import (
    SEMANTIC_MANIFEST_KIND,
    SEMANTIC_MANIFEST_SCHEMA_VERSION,
)
from .semantic_types import (
    SemanticType,
    SemanticTypeError,
    SemanticTypeKind,
    semantic_type_from_manifest,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SemanticModelError(ValueError):
    """Raised when a backend-neutral API invariant is violated."""


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


class BackendFamily(str, Enum):
    CPP = "cpp"
    JVM = "jvm"


class MemberScope(str, Enum):
    TOP_LEVEL = "top_level"
    INSTANCE = "instance"
    STATIC = "static"


class SemanticDeclarationKind(str, Enum):
    ENUM = "enum"
    VALUE = "value"
    OBJECT = "object"


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

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


def semantic_type_id(feature_id: str, public_name: str) -> str:
    """Create the stable identity shared by every backend projection."""

    if not feature_id:
        raise SemanticModelError("feature identity cannot be empty")
    _validate_identifier(public_name, "public semantic type")
    return f"{feature_id}:type:{public_name}"


@dataclass(frozen=True)
class SemanticProjection:
    """One implementation-family capability for a logical declaration."""

    backend: BackendFamily
    source: SourceProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.backend, BackendFamily):
            raise SemanticModelError(f"unknown backend family {self.backend!r}")
        expected = (
            {"cpp"}
            if self.backend is BackendFamily.CPP
            else {"kotlin", "java"}
        )
        if self.source.language not in expected:
            raise SemanticModelError(
                f"{self.source.location}: {self.source.language!r} source cannot "
                f"provide a {self.backend.value!r} projection"
            )

    def manifest(self) -> Dict[str, object]:
        return {"backend": self.backend.value, "source": self.source.manifest()}


@dataclass(frozen=True)
class SemanticField:
    field_id: str
    owner_id: str
    name: str
    type: SemanticType
    source: SourceProvenance
    mutable: bool
    capabilities: BindingCapabilities = field(
        default_factory=lambda: BindingCapabilities.for_role(
            DeclarationRole.EXPORTED
        )
    )
    scope: MemberScope = MemberScope.INSTANCE
    required: bool = True

    def __post_init__(self) -> None:
        if not self.field_id or not self.owner_id:
            raise SemanticModelError("field and owner identities cannot be empty")
        _validate_identifier(self.name, "semantic field")
        if not isinstance(self.type, SemanticType):
            raise SemanticModelError("semantic field type must be SemanticType")
        if self.type.is_void:
            raise SemanticModelError("void is invalid as a field type")
        if not self.capabilities.javascript_public:
            raise SemanticModelError("semantic fields must be explicitly exported")
        if self.scope is not MemberScope.INSTANCE:
            raise SemanticModelError("static bridge fields are not supported")
        if not self.required:
            raise SemanticModelError("optional/missing fields are not supported")

    def manifest(self) -> Dict[str, object]:
        return {
            "field_id": self.field_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "type": self.type.manifest(),
            "mutable": self.mutable,
            "required": self.required,
            "routable": self.capabilities.routable,
            "javascript_public": self.capabilities.javascript_public,
            "scope": self.scope.value,
            "source": self.source.manifest(),
        }


@dataclass(frozen=True)
class SemanticParameter:
    name: str
    type: SemanticType

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "semantic parameter")
        if not isinstance(self.type, SemanticType):
            raise SemanticModelError("semantic parameter type must be SemanticType")
        if self.type.is_void:
            raise SemanticModelError("void is valid only as a result type")

    def manifest(self) -> Dict[str, object]:
        return {"name": self.name, "type": self.type.manifest()}


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
    member_scope: Optional[MemberScope] = None

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise SemanticModelError("semantic binding identity cannot be empty")
        if not self.capabilities.routable:
            raise SemanticModelError(
                "ordinary declarations do not become semantic bindings"
            )
        if not isinstance(self.result, SemanticType):
            raise SemanticModelError("semantic result type must be SemanticType")
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
        inferred_scope = (
            MemberScope.TOP_LEVEL
            if self.kind is BindingKind.FUNCTION
            else MemberScope.INSTANCE
        )
        if self.member_scope is None:
            object.__setattr__(self, "member_scope", inferred_scope)
        elif self.kind is BindingKind.FUNCTION:
            if self.member_scope is not MemberScope.TOP_LEVEL:
                raise SemanticModelError("top-level functions cannot be instance/static")
        elif self.member_scope is MemberScope.TOP_LEVEL:
            raise SemanticModelError("methods cannot have top-level scope")

    def manifest(self) -> Dict[str, object]:
        member_scope = self.member_scope
        assert member_scope is not None
        value: Dict[str, object] = {
            "binding_id": self.binding_id,
            "source_declaration_id": self.source.declaration_id,
            "kind": self.kind.value,
            "name": self.name,
            "routable": self.capabilities.routable,
            "javascript_public": self.capabilities.javascript_public,
            "execution": self.execution.value,
            "parameters": [parameter.manifest() for parameter in self.parameters],
            "result": self.result.manifest(),
            "member_scope": member_scope.value,
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
class SemanticEnumDeclaration:
    feature_id: str
    type_id: str
    name: str
    constants: Tuple[str, ...]
    projections: Tuple[SemanticProjection, ...]

    kind = SemanticDeclarationKind.ENUM

    def __post_init__(self) -> None:
        _validate_declaration_header(self.feature_id, self.type_id, self.name)
        if not self.constants:
            raise SemanticModelError(f"enum {self.name!r} must declare a constant")
        for constant in self.constants:
            _validate_identifier(constant, f"enum {self.name!r} constant")
        _reject_duplicates(self.constants, f"enum constant on {self.name!r}")
        _validate_projections(self.name, self.projections)

    def manifest(self) -> Dict[str, object]:
        return {
            "kind": self.kind.value,
            "feature_id": self.feature_id,
            "type_id": self.type_id,
            "name": self.name,
            "constants": list(self.constants),
            "projections": _projection_manifests(self.projections),
        }


@dataclass(frozen=True)
class SemanticValueDeclaration:
    feature_id: str
    type_id: str
    name: str
    fields: Tuple[SemanticField, ...]
    projections: Tuple[SemanticProjection, ...]

    kind = SemanticDeclarationKind.VALUE

    def __post_init__(self) -> None:
        _validate_declaration_header(self.feature_id, self.type_id, self.name)
        if not self.fields:
            raise SemanticModelError(f"value {self.name!r} must declare a field")
        _validate_fields(self.type_id, self.name, self.fields)
        _validate_projections(self.name, self.projections)

    def manifest(self) -> Dict[str, object]:
        return {
            "kind": self.kind.value,
            "feature_id": self.feature_id,
            "type_id": self.type_id,
            "name": self.name,
            "fields": [item.manifest() for item in self.fields],
            "projections": _projection_manifests(self.projections),
        }


@dataclass(frozen=True)
class SemanticObjectDeclaration:
    feature_id: str
    type_id: str
    name: str
    projection: SemanticProjection
    constructor: Optional[SemanticConstructor] = None
    methods: Tuple[SemanticBinding, ...] = field(default_factory=tuple)
    fields: Tuple[SemanticField, ...] = field(default_factory=tuple)

    kind = SemanticDeclarationKind.OBJECT

    def __post_init__(self) -> None:
        _validate_declaration_header(self.feature_id, self.type_id, self.name)
        _validate_fields(self.type_id, self.name, self.fields)
        owned_sources = [item.source for item in self.fields]
        if self.constructor is not None:
            owned_sources.append(self.constructor.source)
        binding_ids = []
        source_ids = []
        static_names = []
        instance_names = []
        for method in self.methods:
            if method.kind is not BindingKind.OBJECT_METHOD:
                raise SemanticModelError(
                    f"object member {method.name!r} must have object_method kind"
                )
            if method.owner_id != self.type_id or method.owner_name != self.name:
                raise SemanticModelError(
                    f"method {method.name!r} does not belong to object {self.name!r}"
                )
            binding_ids.append(method.binding_id)
            source_ids.append(method.source.declaration_id)
            if method.member_scope is MemberScope.STATIC:
                static_names.append(method.name)
            else:
                instance_names.append(method.name)
            owned_sources.append(method.source)
        for source in owned_sources:
            if _backend_for_language(source.language) is not self.projection.backend:
                raise SemanticModelError(
                    f"{source.location}: object member source backend disagrees with "
                    f"the {self.projection.backend.value} object projection"
                )
        _reject_duplicates(binding_ids, f"method binding identity on {self.name!r}")
        _reject_duplicates(source_ids, f"method source identity on {self.name!r}")
        _reject_duplicates(static_names, f"static method name on {self.name!r}")
        _reject_duplicates(
            instance_names + [item.name for item in self.fields],
            f"instance member name on {self.name!r}",
        )

    @property
    def projections(self) -> Tuple[SemanticProjection, ...]:
        return (self.projection,)

    def manifest(self) -> Dict[str, object]:
        return {
            "kind": self.kind.value,
            "feature_id": self.feature_id,
            "type_id": self.type_id,
            "name": self.name,
            "projection": self.projection.manifest(),
            "constructor": (
                self.constructor.manifest() if self.constructor is not None else None
            ),
            "methods": [
                item.manifest()
                for item in sorted(self.methods, key=lambda value: value.binding_id)
            ],
            "fields": [item.manifest() for item in self.fields],
        }


SemanticDeclaration = Union[
    SemanticEnumDeclaration,
    SemanticValueDeclaration,
    SemanticObjectDeclaration,
]


@dataclass(frozen=True)
class SemanticApi:
    functions: Tuple[SemanticBinding, ...] = field(default_factory=tuple)
    classes: Tuple[SemanticClass, ...] = field(default_factory=tuple)
    declarations: Tuple[SemanticDeclaration, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for binding in self.functions:
            if binding.kind is not BindingKind.FUNCTION:
                raise SemanticModelError(
                    "top-level semantic API bindings must have function kind"
                )

        all_bindings = list(self.functions)
        for legacy_class in self.classes:
            all_bindings.extend(legacy_class.methods)
        for declaration in self.declarations:
            if isinstance(declaration, SemanticObjectDeclaration):
                all_bindings.extend(declaration.methods)

        semantic_ids = [binding.binding_id for binding in all_bindings]
        semantic_ids.extend(item.class_id for item in self.classes)
        semantic_ids.extend(item.type_id for item in self.declarations)
        semantic_ids.extend(
            field.field_id
            for item in self.declarations
            if isinstance(item, (SemanticValueDeclaration, SemanticObjectDeclaration))
            for field in item.fields
        )
        _reject_duplicates(semantic_ids, "semantic identity")

        source_ids = [binding.source.declaration_id for binding in all_bindings]
        source_ids.extend(item.source.declaration_id for item in self.classes)
        source_ids.extend(
            item.constructor.source.declaration_id for item in self.classes
        )
        for declaration in self.declarations:
            source_ids.extend(
                projection.source.declaration_id
                for projection in declaration.projections
            )
            if isinstance(declaration, SemanticObjectDeclaration):
                if declaration.constructor is not None:
                    source_ids.append(declaration.constructor.source.declaration_id)
                source_ids.extend(
                    field.source.declaration_id for field in declaration.fields
                )
            elif isinstance(declaration, SemanticValueDeclaration):
                source_ids.extend(
                    field.source.declaration_id for field in declaration.fields
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
        _validate_named_references(self)
        _validate_value_cycles(self)

    def manifest(self) -> Dict[str, object]:
        return {
            "schema_version": SEMANTIC_MANIFEST_SCHEMA_VERSION,
            "kind": SEMANTIC_MANIFEST_KIND,
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
            "types": [
                item.manifest()
                for item in sorted(self.declarations, key=lambda value: value.type_id)
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
    _manifest_keys(
        value,
        {"schema_version", "kind", "functions", "classes", "types"},
        "semantic manifest",
    )
    raw_schema = value["schema_version"]
    if not isinstance(raw_schema, str) or not raw_schema:
        raise SemanticModelError(
            f"incompatible semantic manifest schema {raw_schema!r}; "
            f"expected {SEMANTIC_MANIFEST_SCHEMA_VERSION}"
        )
    schema = raw_schema
    if schema != SEMANTIC_MANIFEST_SCHEMA_VERSION:
        raise SemanticModelError(
            f"incompatible semantic manifest schema {schema}; "
            f"expected {SEMANTIC_MANIFEST_SCHEMA_VERSION}"
        )
    if _manifest_string(value["kind"], "kind") != SEMANTIC_MANIFEST_KIND:
        raise SemanticModelError("semantic manifest kind is invalid")
    functions = tuple(
        _binding_from_manifest(item, f"functions[{index}]")
        for index, item in enumerate(_manifest_list(value["functions"], "functions"))
    )
    classes = tuple(
        _class_from_manifest(item, f"classes[{index}]")
        for index, item in enumerate(_manifest_list(value["classes"], "classes"))
    )
    declarations = tuple(
        _declaration_from_manifest(item, f"types[{index}]")
        for index, item in enumerate(_manifest_list(value["types"], "types"))
    )
    return SemanticApi(functions, classes, declarations)


def merge_semantic_apis(*apis: SemanticApi) -> SemanticApi:
    """Merge language frontends and re-run all common identity/name checks."""

    declarations: Dict[str, SemanticDeclaration] = {}
    for declaration in (
        item for api in apis for item in api.declarations
    ):
        previous = declarations.get(declaration.type_id)
        if previous is None:
            declarations[declaration.type_id] = declaration
            continue
        declarations[declaration.type_id] = _merge_declarations(
            previous, declaration
        )
    return SemanticApi(
        tuple(binding for api in apis for binding in api.functions),
        tuple(item for api in apis for item in api.classes),
        tuple(declarations.values()),
    )


def _binding_from_manifest(raw: object, label: str) -> SemanticBinding:
    value = _manifest_object(raw, label)
    required = {
        "binding_id", "source_declaration_id", "kind", "name", "routable",
        "javascript_public", "execution", "parameters", "result", "source",
        "member_scope",
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
            result=_semantic_type_from_manifest(value["result"], f"{label}.result"),
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
            member_scope=MemberScope(
                _manifest_string(value["member_scope"], f"{label}.member_scope")
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
            _semantic_type_from_manifest(value["type"], f"{label}.type"),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _declaration_from_manifest(raw: object, label: str) -> SemanticDeclaration:
    value = _manifest_object(raw, label)
    kind_text = _manifest_string(value.get("kind"), f"{label}.kind")
    try:
        kind = SemanticDeclarationKind(kind_text)
    except ValueError as exc:
        raise SemanticModelError(f"{label}.kind is invalid: {kind_text!r}") from exc
    common = {"kind", "feature_id", "type_id", "name"}
    feature_id = _manifest_string(value.get("feature_id"), f"{label}.feature_id")
    type_id = _manifest_string(value.get("type_id"), f"{label}.type_id")
    name = _manifest_string(value.get("name"), f"{label}.name")
    if kind is SemanticDeclarationKind.ENUM:
        _manifest_keys(value, common | {"constants", "projections"}, label)
        return SemanticEnumDeclaration(
            feature_id,
            type_id,
            name,
            tuple(
                _manifest_string(item, f"{label}.constants[{index}]")
                for index, item in enumerate(
                    _manifest_list(value["constants"], f"{label}.constants")
                )
            ),
            _projections_from_manifest(value["projections"], f"{label}.projections"),
        )
    if kind is SemanticDeclarationKind.VALUE:
        _manifest_keys(value, common | {"fields", "projections"}, label)
        return SemanticValueDeclaration(
            feature_id,
            type_id,
            name,
            tuple(
                _field_from_manifest(item, f"{label}.fields[{index}]")
                for index, item in enumerate(
                    _manifest_list(value["fields"], f"{label}.fields")
                )
            ),
            _projections_from_manifest(value["projections"], f"{label}.projections"),
        )
    _manifest_keys(
        value,
        common | {"projection", "constructor", "methods", "fields"},
        label,
    )
    constructor_raw = value["constructor"]
    return SemanticObjectDeclaration(
        feature_id,
        type_id,
        name,
        _projection_from_manifest(value["projection"], f"{label}.projection"),
        (
            None
            if constructor_raw is None
            else _constructor_from_manifest(constructor_raw, f"{label}.constructor")
        ),
        tuple(
            _binding_from_manifest(item, f"{label}.methods[{index}]")
            for index, item in enumerate(
                _manifest_list(value["methods"], f"{label}.methods")
            )
        ),
        tuple(
            _field_from_manifest(item, f"{label}.fields[{index}]")
            for index, item in enumerate(
                _manifest_list(value["fields"], f"{label}.fields")
            )
        ),
    )


def _constructor_from_manifest(raw: object, label: str) -> SemanticConstructor:
    value = _manifest_object(raw, label)
    _manifest_keys(value, {"source_declaration_id", "parameters", "source"}, label)
    source = _source_from_manifest(value["source"], f"{label}.source")
    if _manifest_string(
        value["source_declaration_id"], f"{label}.source_declaration_id"
    ) != source.declaration_id:
        raise SemanticModelError(f"{label} source declaration identity disagrees")
    return SemanticConstructor(
        source,
        tuple(
            _parameter_from_manifest(item, f"{label}.parameters[{index}]")
            for index, item in enumerate(
                _manifest_list(value["parameters"], f"{label}.parameters")
            )
        ),
    )


def _projection_from_manifest(raw: object, label: str) -> SemanticProjection:
    value = _manifest_object(raw, label)
    _manifest_keys(value, {"backend", "source"}, label)
    try:
        return SemanticProjection(
            BackendFamily(_manifest_string(value["backend"], f"{label}.backend")),
            _source_from_manifest(value["source"], f"{label}.source"),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _projections_from_manifest(raw: object, label: str) -> Tuple[SemanticProjection, ...]:
    return tuple(
        _projection_from_manifest(item, f"{label}[{index}]")
        for index, item in enumerate(_manifest_list(raw, label))
    )


def _field_from_manifest(raw: object, label: str) -> SemanticField:
    value = _manifest_object(raw, label)
    _manifest_keys(
        value,
        {
            "field_id", "owner_id", "name", "type", "mutable", "required",
            "routable", "javascript_public", "scope", "source",
        },
        label,
    )
    try:
        return SemanticField(
            _manifest_string(value["field_id"], f"{label}.field_id"),
            _manifest_string(value["owner_id"], f"{label}.owner_id"),
            _manifest_string(value["name"], f"{label}.name"),
            _semantic_type_from_manifest(value["type"], f"{label}.type"),
            _source_from_manifest(value["source"], f"{label}.source"),
            _manifest_bool(value["mutable"], f"{label}.mutable"),
            BindingCapabilities(
                _manifest_bool(value["routable"], f"{label}.routable"),
                _manifest_bool(
                    value["javascript_public"], f"{label}.javascript_public"
                ),
            ),
            MemberScope(_manifest_string(value["scope"], f"{label}.scope")),
            _manifest_bool(value["required"], f"{label}.required"),
        )
    except ValueError as exc:
        raise SemanticModelError(f"{label}: {exc}") from exc


def _semantic_type_from_manifest(raw: object, label: str) -> SemanticType:
    try:
        return semantic_type_from_manifest(raw, label)
    except SemanticTypeError as exc:
        raise SemanticModelError(str(exc)) from exc


def _validate_declaration_header(feature_id: str, type_id: str, name: str) -> None:
    if not feature_id:
        raise SemanticModelError("feature identity cannot be empty")
    _validate_identifier(name, "semantic declaration")
    expected = semantic_type_id(feature_id, name)
    if type_id != expected:
        raise SemanticModelError(
            f"semantic type {name!r} must use stable identity {expected!r}, "
            f"not {type_id!r}"
        )


def _backend_for_language(language: str) -> BackendFamily:
    if language == "cpp":
        return BackendFamily.CPP
    if language in {"kotlin", "java"}:
        return BackendFamily.JVM
    raise SemanticModelError(f"unsupported semantic source language {language!r}")


def _validate_projections(
    name: str, projections: Tuple[SemanticProjection, ...]
) -> None:
    if not projections:
        raise SemanticModelError(f"semantic type {name!r} needs a backend projection")
    _reject_duplicates(
        (item.backend.value for item in projections),
        f"backend projection on {name!r}",
    )


def _projection_manifests(
    projections: Tuple[SemanticProjection, ...]
) -> list[Dict[str, object]]:
    return [
        item.manifest()
        for item in sorted(projections, key=lambda value: value.backend.value)
    ]


def _validate_fields(
    owner_id: str, owner_name: str, fields: Tuple[SemanticField, ...]
) -> None:
    for item in fields:
        if item.owner_id != owner_id:
            raise SemanticModelError(
                f"field {item.name!r} does not belong to {owner_name!r}"
            )
    _reject_duplicates(
        (item.field_id for item in fields), f"field identity on {owner_name!r}"
    )
    _reject_duplicates(
        (item.name for item in fields), f"field name on {owner_name!r}"
    )


def _walk_type(value: SemanticType) -> Iterable[SemanticType]:
    yield value
    if value.element is not None:
        yield from _walk_type(value.element)


def _binding_types(binding: SemanticBinding) -> Iterable[SemanticType]:
    for parameter in binding.parameters:
        yield parameter.type
    yield binding.result


def _all_api_types(api: SemanticApi) -> Iterable[SemanticType]:
    for binding in api.functions:
        yield from _binding_types(binding)
    for legacy in api.classes:
        for parameter in legacy.constructor.parameters:
            yield parameter.type
        for method in legacy.methods:
            yield from _binding_types(method)
    for declaration in api.declarations:
        if isinstance(declaration, (SemanticValueDeclaration, SemanticObjectDeclaration)):
            for item in declaration.fields:
                yield item.type
        if isinstance(declaration, SemanticObjectDeclaration):
            if declaration.constructor is not None:
                for parameter in declaration.constructor.parameters:
                    yield parameter.type
            for method in declaration.methods:
                yield from _binding_types(method)


def _validate_named_references(api: SemanticApi) -> None:
    declarations = {item.type_id: item for item in api.declarations}
    expected = {
        SemanticTypeKind.ENUM_REF: SemanticEnumDeclaration,
        SemanticTypeKind.VALUE_REF: SemanticValueDeclaration,
        SemanticTypeKind.OBJECT_REF: SemanticObjectDeclaration,
    }
    for root in _all_api_types(api):
        for item in _walk_type(root):
            declaration_class = expected.get(item.kind)
            if declaration_class is None:
                continue
            type_id = item.type_id
            assert type_id is not None
            declaration = declarations.get(type_id)
            if declaration is None:
                raise SemanticModelError(
                    f"unknown semantic {item.kind.value} type ID {item.type_id!r}"
                )
            if not isinstance(declaration, declaration_class):
                raise SemanticModelError(
                    f"nominal reference {item.type_id!r} has kind "
                    f"{item.kind.value!r}, but the declaration is "
                    f"{declaration.kind.value!r}"
                )


def _value_dependencies(declaration: SemanticValueDeclaration) -> set[str]:
    return {
        item.type_id
        for field in declaration.fields
        for item in _walk_type(field.type)
        if item.kind is SemanticTypeKind.VALUE_REF and item.type_id is not None
    }


def _validate_value_cycles(api: SemanticApi) -> None:
    values = {
        item.type_id: item
        for item in api.declarations
        if isinstance(item, SemanticValueDeclaration)
    }
    visiting: list[str] = []
    complete: set[str] = set()

    def visit(type_id: str) -> None:
        if type_id in complete:
            return
        if type_id in visiting:
            start = visiting.index(type_id)
            cycle = visiting[start:] + [type_id]
            raise SemanticModelError(
                "recursive value declaration cycle: " + " -> ".join(cycle)
            )
        visiting.append(type_id)
        for dependency in sorted(_value_dependencies(values[type_id])):
            visit(dependency)
        visiting.pop()
        complete.add(type_id)

    for type_id in sorted(values):
        visit(type_id)


def _declaration_schema(declaration: SemanticDeclaration) -> object:
    if isinstance(declaration, SemanticEnumDeclaration):
        return (declaration.kind, declaration.name, declaration.constants)
    if isinstance(declaration, SemanticValueDeclaration):
        return (
            declaration.kind,
            declaration.name,
            tuple(
                (field.field_id, field.name, field.type, field.required)
                for field in declaration.fields
            ),
        )
    return (declaration.kind, declaration.name)


def _merge_declarations(
    first: SemanticDeclaration, second: SemanticDeclaration
) -> SemanticDeclaration:
    first_source = first.projections[0].source
    second_source = second.projections[0].source
    locations = f"{first_source.location} and {second_source.location}"
    if isinstance(first, SemanticObjectDeclaration) or isinstance(
        second, SemanticObjectDeclaration
    ):
        raise SemanticModelError(
            f"native object {first.type_id!r} has duplicate declarations at {locations}; "
            "object projections do not merge across backend families"
        )
    if type(first) is not type(second) or _declaration_schema(first) != _declaration_schema(second):
        raise SemanticModelError(
            f"logical type {first.type_id!r} has mismatched projections at {locations}"
        )
    existing = {item.backend: item for item in first.projections}
    for projection in second.projections:
        duplicate = existing.get(projection.backend)
        if duplicate is not None:
            raise SemanticModelError(
                f"logical type {first.type_id!r} has duplicate "
                f"{projection.backend.value} projections at "
                f"{duplicate.source.location} and {projection.source.location}"
            )
        existing[projection.backend] = projection
    projections = tuple(
        existing[key] for key in sorted(existing, key=lambda item: item.value)
    )
    if isinstance(first, SemanticEnumDeclaration):
        return SemanticEnumDeclaration(
            first.feature_id, first.type_id, first.name, first.constants, projections
        )
    assert isinstance(first, SemanticValueDeclaration)
    return SemanticValueDeclaration(
        first.feature_id, first.type_id, first.name, first.fields, projections
    )


def validate_semantic_route(
    api: SemanticApi,
    value_type: SemanticType,
    source_backend: BackendFamily,
    target_backend: BackendFamily,
    source: SourceProvenance,
    target: SourceProvenance,
) -> None:
    """Validate backend capabilities for one semantic value on a route."""

    declarations = {item.type_id: item for item in api.declarations}
    checked_values: set[tuple[str, BackendFamily, BackendFamily]] = set()

    def fail(
        message: str,
        *,
        declaration: SemanticDeclaration | None = None,
        position: str = "value",
    ) -> None:
        declared_at = ""
        if declaration is not None:
            locations = ", ".join(
                projection.source.location for projection in declaration.projections
            )
            declared_at = f"; logical type is declared at {locations}"
        raise SemanticModelError(
            f"{message} at {position}{declared_at}; route endpoints are "
            f"{source.location} and {target.location}"
        )

    def check(item: SemanticType, position: str) -> None:
        if item.kind is SemanticTypeKind.ARRAY:
            assert item.element is not None
            check(item.element, position + "[]")
            return
        if item.kind is SemanticTypeKind.NULLABLE:
            assert item.element is not None
            check(item.element, position + "?")
            return
        if item.type_id is None:
            return
        declaration = declarations[item.type_id]
        available = {projection.backend for projection in declaration.projections}
        if item.kind is SemanticTypeKind.OBJECT_REF:
            if source_backend is not target_backend:
                fail(
                    f"native object {declaration.name!r} cannot cross "
                    f"{source_backend.value}->{target_backend.value}; cross-family "
                    "object proxies are deferred in the current generator",
                    declaration=declaration,
                    position=position,
                )
            if source_backend not in available:
                fail(
                    f"native object {declaration.name!r} has no "
                    f"{source_backend.value} projection",
                    declaration=declaration,
                    position=position,
                )
            return
        required = {source_backend, target_backend}
        missing = required - available
        if missing:
            fail(
                f"copied type {declaration.name!r} is missing "
                + ", ".join(sorted(item.value for item in missing))
                + " projection capability",
                declaration=declaration,
                position=position,
            )
        if isinstance(declaration, SemanticValueDeclaration):
            key = (declaration.type_id, source_backend, target_backend)
            if key in checked_values:
                return
            checked_values.add(key)
            for field in declaration.fields:
                check(field.type, position + "." + field.name)

    check(value_type, "value")


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
