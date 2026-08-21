"""Source-backed C++ routes for the V3 semantic object model.

The semantic API deliberately forgets source-language ownership spellings.  This
module joins those public semantics back to the exact declarations found by the
C++ frontend.  Renderers consume this plan instead of guessing from a public
type name or rescanning one class at a time.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Optional, Tuple

from .semantic import (
    BackendFamily,
    BindingKind,
    ExecutionMode,
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticConstructor,
    SemanticField,
    SemanticObjectDeclaration,
    SemanticValueDeclaration,
    SemanticModelError,
    validate_semantic_route,
)
from .semantic_types import SemanticType, SemanticTypeKind
from .source_models import (
    CppClassSource,
    CppConstructorSource,
    CppEnumSource,
    CppFieldSource,
    CppFunctionSource,
    CppMethodSource,
    CppParameterSource,
)


class CppRouteError(ValueError):
    """Raised when projected semantics and the source model no longer agree."""


class CppCallableKind(str, Enum):
    FUNCTION = "function"
    CONSTRUCTOR = "constructor"
    INSTANCE_METHOD = "instance_method"
    STATIC_METHOD = "static_method"


class CppObjectPassing(str, Enum):
    """The four accepted direct C++ object parameter forms."""

    BORROWED_MUTABLE = "borrowed_mutable"
    BORROWED_CONST = "borrowed_const"
    SHARED_VALUE = "shared_value"
    SHARED_CONST_REF = "shared_const_ref"


@dataclass(frozen=True)
class CppNamedTypeRoute:
    type_id: str
    public_name: str
    cpp_type: str
    include: str
    kind: SemanticTypeKind
    source_declaration_id: str


@dataclass(frozen=True)
class CppParameterRoute:
    name: str
    cpp_spelling: str
    semantic_type: SemanticType
    object_passing: Optional[CppObjectPassing] = None

    def __post_init__(self) -> None:
        is_direct_object = self.semantic_type.kind is SemanticTypeKind.OBJECT_REF
        if is_direct_object != (self.object_passing is not None):
            raise CppRouteError(
                "only direct object parameters carry a C++ object passing form"
            )


@dataclass(frozen=True)
class CppCallableRoute:
    source_declaration_id: str
    kind: CppCallableKind
    public_name: str
    cpp_name: str
    owner_cpp_type: Optional[str]
    parameters: Tuple[CppParameterRoute, ...]
    result: SemanticType
    result_cpp_spelling: str
    execution: ExecutionMode
    noexcept: bool
    javascript_public: bool
    cpp_namespace: Tuple[str, ...]
    const: bool = False


@dataclass(frozen=True)
class CppFieldRoute:
    source_declaration_id: str
    field_id: str
    public_name: str
    cpp_name: str
    cpp_spelling: str
    semantic_type: SemanticType
    mutable: bool


@dataclass(frozen=True)
class CppObjectRoute:
    named_type: CppNamedTypeRoute
    constructor: Optional[CppCallableRoute]
    methods: Tuple[CppCallableRoute, ...]
    fields: Tuple[CppFieldRoute, ...]


@dataclass(frozen=True)
class CppValueRoute:
    named_type: CppNamedTypeRoute
    fields: Tuple[CppFieldRoute, ...]


@dataclass(frozen=True)
class CppEnumRoute:
    named_type: CppNamedTypeRoute
    constants: Tuple[str, ...]


@dataclass(frozen=True)
class CppRoutePlan:
    functions: Tuple[CppCallableRoute, ...]
    objects: Tuple[CppObjectRoute, ...]
    values: Tuple[CppValueRoute, ...]
    enums: Tuple[CppEnumRoute, ...]
    named_types: Tuple[CppNamedTypeRoute, ...]

    @property
    def named_types_by_id(self) -> dict[str, CppNamedTypeRoute]:
        return {item.type_id: item for item in self.named_types}


def _normalized_cpp_type(spelling: str) -> str:
    value = spelling.strip()
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s*<\s*", "<", value)
    value = re.sub(r"\s*>\s*", ">", value)
    value = re.sub(r"\s*&\s*", "&", value)
    return re.sub(r"\s+", " ", value)


def _qualified_cpp_type(source: CppClassSource | CppEnumSource) -> str:
    return "::" + source.qualified_name.removeprefix("::")


def _object_passing(spelling: str) -> CppObjectPassing:
    canonical = _normalized_cpp_type(spelling)
    if canonical.startswith("const std::shared_ptr<") and canonical.endswith(">&"):
        return CppObjectPassing.SHARED_CONST_REF
    if canonical.startswith("std::shared_ptr<") and canonical.endswith(">"):
        return CppObjectPassing.SHARED_VALUE
    if canonical.startswith("const ") and canonical.endswith("&"):
        return CppObjectPassing.BORROWED_CONST
    if canonical.endswith("&"):
        return CppObjectPassing.BORROWED_MUTABLE
    raise CppRouteError(
        f"unsupported direct C++ object parameter spelling {spelling!r}"
    )


def _parameters(
    semantic_parameters: tuple,
    source_parameters: Tuple[CppParameterSource, ...],
) -> Tuple[CppParameterRoute, ...]:
    if len(semantic_parameters) != len(source_parameters):
        raise CppRouteError("semantic and C++ parameter counts disagree")
    routes = []
    for semantic, source in zip(semantic_parameters, source_parameters):
        if semantic.name != source.name:
            raise CppRouteError("semantic and C++ parameter names disagree")
        passing = (
            _object_passing(source.type_spelling)
            if semantic.type.kind is SemanticTypeKind.OBJECT_REF
            else None
        )
        routes.append(
            CppParameterRoute(
                semantic.name,
                source.type_spelling,
                semantic.type,
                passing,
            )
        )
    return tuple(routes)


def _function_route(
    binding: SemanticBinding,
    source: CppFunctionSource,
) -> CppCallableRoute:
    _require_source(binding.source.declaration_id, source.provenance.declaration_id)
    return CppCallableRoute(
        source.provenance.declaration_id,
        CppCallableKind.FUNCTION,
        binding.name,
        "::" + "::".join((*source.namespace, source.cpp_name)).removeprefix("::"),
        None,
        _parameters(binding.parameters, source.parameters),
        binding.result,
        source.return_type_spelling,
        binding.execution,
        source.noexcept,
        binding.capabilities.javascript_public,
        source.namespace,
    )


def _constructor_route(
    semantic: SemanticConstructor,
    source: CppConstructorSource,
    owner: CppClassSource,
    type_id: str,
) -> CppCallableRoute:
    _require_source(semantic.source.declaration_id, source.provenance.declaration_id)
    cpp_type = _qualified_cpp_type(owner)
    return CppCallableRoute(
        source.provenance.declaration_id,
        CppCallableKind.CONSTRUCTOR,
        "create",
        cpp_type,
        cpp_type,
        _parameters(semantic.parameters, source.parameters),
        SemanticType.object_ref(type_id),
        f"std::shared_ptr<{cpp_type}>",
        ExecutionMode.SYNC,
        source.noexcept,
        True,
        owner.namespace,
    )


def _method_route(
    binding: SemanticBinding,
    source: CppMethodSource,
    owner: CppClassSource,
) -> CppCallableRoute:
    _require_source(binding.source.declaration_id, source.provenance.declaration_id)
    expected_kind = (
        CppCallableKind.STATIC_METHOD
        if binding.member_scope is MemberScope.STATIC
        else CppCallableKind.INSTANCE_METHOD
    )
    if source.static != (expected_kind is CppCallableKind.STATIC_METHOD):
        raise CppRouteError("semantic and C++ method scopes disagree")
    return CppCallableRoute(
        source.provenance.declaration_id,
        expected_kind,
        binding.name,
        source.cpp_name,
        _qualified_cpp_type(owner),
        _parameters(binding.parameters, source.parameters),
        binding.result,
        source.return_type_spelling,
        binding.execution,
        source.noexcept,
        binding.capabilities.javascript_public,
        owner.namespace,
        source.const,
    )


def _field_route(
    semantic: SemanticField,
    source: CppFieldSource,
    *,
    copied_projection: bool = False,
) -> CppFieldRoute:
    if copied_projection:
        if semantic.name != source.cpp_name:
            raise CppRouteError("semantic and C++ copied field names disagree")
    else:
        _require_source(semantic.source.declaration_id, source.provenance.declaration_id)
    if not copied_projection and semantic.mutable != source.mutable:
        raise CppRouteError("semantic and C++ field mutability disagree")
    return CppFieldRoute(
        source.provenance.declaration_id,
        semantic.field_id,
        semantic.name,
        source.cpp_name,
        source.type_spelling,
        semantic.type,
        source.mutable,
    )


def _require_source(expected: str, actual: str) -> None:
    if expected != actual:
        raise CppRouteError(
            f"semantic source {expected!r} does not match C++ source {actual!r}"
        )


def plan_cpp_routes(
    api: SemanticApi,
    functions: Iterable[CppFunctionSource],
    classes: Iterable[CppClassSource],
    enums: Iterable[CppEnumSource] = (),
) -> CppRoutePlan:
    """Join one projected C++ API to its exact source declarations."""

    function_sources = {
        item.provenance.declaration_id: item for item in functions
    }
    class_sources = {
        item.provenance.declaration_id: item for item in classes
    }
    implementation_method_ids = {
        method.provenance.declaration_id
        for owner in class_sources.values()
        if not owner.intent.declares_object
        for method in owner.methods
    }
    enum_sources = {
        item.provenance.declaration_id: item for item in enums
    }

    def validate_type(value: SemanticType, source) -> None:
        try:
            validate_semantic_route(
                api,
                value,
                BackendFamily.CPP,
                BackendFamily.CPP,
                source,
                source,
            )
        except SemanticModelError as exc:
            raise CppRouteError(str(exc)) from exc

    def validate_binding(binding: SemanticBinding) -> None:
        for parameter in binding.parameters:
            validate_type(parameter.type, binding.source)
        validate_type(binding.result, binding.source)

    function_routes = []
    for binding in api.functions:
        if binding.source.language != "cpp":
            continue
        validate_binding(binding)
        if binding.kind is not BindingKind.FUNCTION:
            raise CppRouteError("top-level C++ binding is not a function")
        try:
            source = function_sources[binding.source.declaration_id]
        except KeyError as exc:
            if binding.source.declaration_id in implementation_method_ids:
                # Unmarked implementation-owner methods are lowered by the
                # internal facade, which owns service construction/receiver
                # lookup. They are not direct C++ function routes.
                continue
            raise CppRouteError(
                f"missing C++ function source {binding.source.declaration_id!r}"
            ) from exc
        function_routes.append(_function_route(binding, source))

    named_routes = []
    object_routes = []
    value_routes = []
    enum_routes = []
    for declaration in api.declarations:
        cpp_projections = [
            item for item in declaration.projections
            if item.backend is BackendFamily.CPP
        ]
        if not cpp_projections:
            continue
        if len(cpp_projections) != 1:
            raise CppRouteError(
                f"type {declaration.name!r} has multiple C++ projections"
            )
        projection = cpp_projections[0]
        if declaration.kind.value == "enum":
            try:
                source = enum_sources[projection.source.declaration_id]
            except KeyError as exc:
                raise CppRouteError(
                    f"missing C++ enum source {projection.source.declaration_id!r}"
                ) from exc
            named = CppNamedTypeRoute(
                    declaration.type_id,
                    declaration.name,
                    _qualified_cpp_type(source),
                    source.include,
                    SemanticTypeKind.ENUM_REF,
                    source.provenance.declaration_id,
                )
            named_routes.append(named)
            if tuple(declaration.constants) != tuple(source.constants):
                raise CppRouteError(
                    f"semantic and C++ enum constants disagree for {declaration.name!r}"
                )
            enum_routes.append(CppEnumRoute(named, tuple(source.constants)))
            continue
        try:
            owner = class_sources[projection.source.declaration_id]
        except KeyError as exc:
            raise CppRouteError(
                f"missing C++ class source {projection.source.declaration_id!r}"
            ) from exc
        kind = (
            SemanticTypeKind.OBJECT_REF
            if isinstance(declaration, SemanticObjectDeclaration)
            else SemanticTypeKind.VALUE_REF
        )
        named = CppNamedTypeRoute(
            declaration.type_id,
            declaration.name,
            _qualified_cpp_type(owner),
            owner.include,
            kind,
            owner.provenance.declaration_id,
        )
        named_routes.append(named)
        if isinstance(declaration, SemanticValueDeclaration):
            field_sources = {item.cpp_name: item for item in owner.fields}
            fields = []
            for field in declaration.fields:
                try:
                    field_source = field_sources[field.name]
                except KeyError as exc:
                    raise CppRouteError(
                        f"missing C++ value field source for {declaration.name!r}"
                    ) from exc
                fields.append(
                    _field_route(field, field_source, copied_projection=True)
                )
            value_routes.append(CppValueRoute(named, tuple(fields)))
            continue
        if not isinstance(declaration, SemanticObjectDeclaration):
            continue
        constructor = None
        if declaration.constructor is not None:
            for parameter in declaration.constructor.parameters:
                validate_type(parameter.type, declaration.constructor.source)
            by_id = {
                item.provenance.declaration_id: item for item in owner.constructors
            }
            try:
                constructor_source = by_id[
                    declaration.constructor.source.declaration_id
                ]
            except KeyError as exc:
                raise CppRouteError(
                    f"missing C++ constructor source for {declaration.name!r}"
                ) from exc
            constructor = _constructor_route(
                declaration.constructor,
                constructor_source,
                owner,
                declaration.type_id,
            )
        method_sources = {
            item.provenance.declaration_id: item for item in owner.methods
        }
        methods = []
        for binding in declaration.methods:
            validate_binding(binding)
            try:
                method_source = method_sources[binding.source.declaration_id]
            except KeyError as exc:
                raise CppRouteError(
                    f"missing C++ method source {binding.source.declaration_id!r}"
                ) from exc
            methods.append(_method_route(binding, method_source, owner))
        field_sources = {
            item.provenance.declaration_id: item for item in owner.fields
        }
        fields = []
        for field in declaration.fields:
            validate_type(field.type, field.source)
            try:
                field_source = field_sources[field.source.declaration_id]
            except KeyError as exc:
                raise CppRouteError(
                    f"missing C++ field source {field.source.declaration_id!r}"
                ) from exc
            fields.append(_field_route(field, field_source))
        object_routes.append(
            CppObjectRoute(named, constructor, tuple(methods), tuple(fields))
        )

    return CppRoutePlan(
        tuple(function_routes),
        tuple(object_routes),
        tuple(value_routes),
        tuple(enum_routes),
        tuple(named_routes),
    )
