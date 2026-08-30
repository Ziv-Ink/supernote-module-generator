"""Source-backed JVM routes for the semantic object model.

KSP records exact JVM owners and deterministic adapter identities.  This module
joins those compiler facts back to the backend-neutral semantic API and derives
the descriptors of the generated Kotlin adapter surface.  Code generation must
consume this plan instead of guessing JVM classes from public JavaScript names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from .jvm_manifest import jvm_field_accessor_identity
from .semantic import (
    BackendFamily,
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
from .semantic_types import ScalarKind, SemanticType, SemanticTypeKind
from .source_models import (
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmFieldSource,
    JvmLanguage,
    JvmOwnerSource,
)


class JvmRouteError(ValueError):
    """Raised when projected semantics and KSP source facts disagree."""


@dataclass(frozen=True)
class JvmNamedTypeRoute:
    type_id: str
    public_name: str
    owner_class: str
    language: JvmLanguage
    kind: SemanticTypeKind
    source_declaration_id: str


@dataclass(frozen=True)
class JvmCallableRoute:
    source_declaration_id: str
    public_name: str
    adapter_identity: str
    adapter_descriptor: str
    parameters: Tuple[SemanticType, ...]
    result: SemanticType
    execution: ExecutionMode
    owner_type_id: Optional[str]
    static: bool
    suspend: bool
    javascript_public: bool


@dataclass(frozen=True)
class JvmFieldRoute:
    source_declaration_id: str
    field_id: str
    public_name: str
    accessor_identity: str
    getter_descriptor: str
    setter_descriptor: Optional[str]
    semantic_type: SemanticType
    mutable: bool


@dataclass(frozen=True)
class JvmObjectRoute:
    named_type: JvmNamedTypeRoute
    constructor: Optional[JvmCallableRoute]
    methods: Tuple[JvmCallableRoute, ...]
    fields: Tuple[JvmFieldRoute, ...]


@dataclass(frozen=True)
class JvmValueRoute:
    named_type: JvmNamedTypeRoute
    constructor: JvmCallableRoute
    constructor_fields: Tuple[JvmFieldRoute, ...]
    fields: Tuple[JvmFieldRoute, ...]


@dataclass(frozen=True)
class JvmEnumRoute:
    named_type: JvmNamedTypeRoute
    constants: Tuple[str, ...]


@dataclass(frozen=True)
class JvmRoutePlan:
    functions: Tuple[JvmCallableRoute, ...]
    objects: Tuple[JvmObjectRoute, ...]
    values: Tuple[JvmValueRoute, ...]
    enums: Tuple[JvmEnumRoute, ...]
    named_types: Tuple[JvmNamedTypeRoute, ...]

    @property
    def named_types_by_id(self) -> dict[str, JvmNamedTypeRoute]:
        return {item.type_id: item for item in self.named_types}


_BOXED_DESCRIPTOR = {
    ScalarKind.BOOL: "Ljava/lang/Boolean;",
    ScalarKind.INT32: "Ljava/lang/Integer;",
    ScalarKind.INT64: "Ljava/lang/Long;",
    ScalarKind.FLOAT32: "Ljava/lang/Float;",
    ScalarKind.FLOAT64: "Ljava/lang/Double;",
}


def _adapter_descriptor_for_type(
    semantic: SemanticType,
    named: dict[str, JvmNamedTypeRoute],
) -> str:
    if semantic.kind is SemanticTypeKind.VOID:
        return "V"
    if semantic.kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        child = semantic.element
        if child.kind is SemanticTypeKind.SCALAR and child.scalar in _BOXED_DESCRIPTOR:
            return _BOXED_DESCRIPTOR[child.scalar]
        return _adapter_descriptor_for_type(child, named)
    if semantic.kind is SemanticTypeKind.ARRAY:
        return "Ljava/util/List;"
    if semantic.kind is SemanticTypeKind.SCALAR:
        return {
            ScalarKind.BOOL: "Z",
            ScalarKind.INT32: "I",
            ScalarKind.INT64: "J",
            ScalarKind.FLOAT32: "F",
            ScalarKind.FLOAT64: "D",
            ScalarKind.STRING: "[B",
            ScalarKind.BYTES: "[B",
        }[semantic.scalar]
    assert semantic.type_id is not None
    try:
        route = named[semantic.type_id]
    except KeyError as exc:
        raise JvmRouteError(
            f"missing JVM projection for semantic type {semantic.type_id!r}"
        ) from exc
    return f"L{route.owner_class.replace('.', '/')};"


def _adapter_class(identity: str) -> str:
    return "supernote.generated.adapters.Adapter_" + identity.rsplit(".", 1)[-1]


def _callable(
    semantic: SemanticBinding | SemanticConstructor,
    source: JvmDeclarationSource | JvmConstructorSource,
    *,
    named: dict[str, JvmNamedTypeRoute],
    owner: JvmNamedTypeRoute,
    public_name: str,
    result: SemanticType,
    static: bool,
    suspend: bool,
) -> JvmCallableRoute:
    if semantic.source.declaration_id != source.provenance.declaration_id:
        raise JvmRouteError("semantic and JVM callable source identities disagree")
    parameter_types = tuple(item.type for item in semantic.parameters)
    descriptor_parameters = []
    if not isinstance(source, JvmConstructorSource) and not static:
        descriptor_parameters.append(
            f"L{owner.owner_class.replace('.', '/')};"
        )
    elif isinstance(source, JvmConstructorSource):
        descriptor_parameters.append(
            "Lcom/facebook/react/bridge/ReactApplicationContext;"
        )
    descriptor_parameters.extend(
        _adapter_descriptor_for_type(item, named) for item in parameter_types
    )
    if suspend:
        descriptor_parameters.append("J")
        result_descriptor = "Lkotlinx/coroutines/Job;"
    else:
        result_descriptor = _adapter_descriptor_for_type(result, named)
    return JvmCallableRoute(
        source.provenance.declaration_id,
        public_name,
        source.adapter_identity,
        f"({''.join(descriptor_parameters)}){result_descriptor}",
        parameter_types,
        result,
        (
            semantic.execution
            if isinstance(semantic, SemanticBinding)
            else ExecutionMode.SYNC
        ),
        owner.type_id,
        static,
        suspend,
        (
            semantic.capabilities.javascript_public
            if isinstance(semantic, SemanticBinding)
            else True
        ),
    )


def _field(
    semantic: SemanticField,
    source: JvmFieldSource,
    owner: JvmNamedTypeRoute,
    named: dict[str, JvmNamedTypeRoute],
    *,
    copied_projection: bool = False,
) -> JvmFieldRoute:
    if copied_projection:
        if semantic.name != source.name:
            raise JvmRouteError("semantic and JVM copied field names disagree")
    elif semantic.source.declaration_id != source.provenance.declaration_id:
        raise JvmRouteError("semantic and JVM field source identities disagree")
    if not copied_projection and semantic.mutable != source.mutable:
        raise JvmRouteError("semantic and JVM field mutability disagree")
    expected_accessor = jvm_field_accessor_identity(source.provenance.declaration_id)
    if source.accessor_identity != expected_accessor:
        raise JvmRouteError("JVM field accessor identity is not deterministic")
    owner_descriptor = f"L{owner.owner_class.replace('.', '/')};"
    value_descriptor = _adapter_descriptor_for_type(semantic.type, named)
    return JvmFieldRoute(
        source.provenance.declaration_id,
        semantic.field_id,
        semantic.name,
        source.accessor_identity,
        f"({owner_descriptor}){value_descriptor}",
        f"({owner_descriptor}{value_descriptor})V" if semantic.mutable else None,
        semantic.type,
        source.mutable,
    )


def _value_constructor(
    declaration: SemanticValueDeclaration,
    source: JvmOwnerSource,
    owner: JvmNamedTypeRoute,
    named: dict[str, JvmNamedTypeRoute],
) -> tuple[JvmCallableRoute, tuple[str, ...]]:
    expected = {item.name: item.type for item in declaration.fields}
    eligible = []
    for constructor in source.constructors:
        visible = tuple(item for item in constructor.parameters if item.injected is None)
        if constructor.visibility != "public" or len(visible) != len(expected):
            continue
        if set(item.name for item in visible) != set(expected):
            continue
        eligible.append(constructor)
    if len(eligible) != 1:
        raise JvmRouteError(
            f"JVM value {declaration.name!r} requires one public field-order constructor"
        )
    constructor = eligible[0]
    field_by_name = {item.name: item for item in declaration.fields}
    ordered_fields = tuple(
        field_by_name[item.name]
        for item in constructor.parameters
        if item.injected is None
    )
    parameter_types = tuple(item.type for item in ordered_fields)
    descriptor = (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;"
        + "".join(_adapter_descriptor_for_type(item, named) for item in parameter_types)
        + f")L{owner.owner_class.replace('.', '/')};"
    )
    return JvmCallableRoute(
        constructor.provenance.declaration_id,
        "createValue",
        constructor.adapter_identity,
        descriptor,
        parameter_types,
        SemanticType.value_ref(declaration.type_id),
        ExecutionMode.SYNC,
        owner.type_id,
        True,
        False,
        True,
    ), tuple(item.name for item in ordered_fields)


def plan_jvm_routes(
    api: SemanticApi,
    owners: Iterable[JvmOwnerSource],
) -> JvmRoutePlan:
    """Join marked JVM declarations to their exact generated adapter routes."""

    owner_sources = tuple(owners)
    source_by_id = {
        item.provenance.declaration_id: item for item in owner_sources
    }
    declarations_by_id = {
        declaration.provenance.declaration_id: (owner, declaration)
        for owner in owner_sources
        for declaration in owner.declarations
    }

    def validate_type(value: SemanticType, source) -> None:
        try:
            validate_semantic_route(
                api,
                value,
                BackendFamily.JVM,
                BackendFamily.JVM,
                source,
                source,
            )
        except SemanticModelError as exc:
            raise JvmRouteError(str(exc)) from exc

    def validate_binding(binding: SemanticBinding) -> None:
        for parameter in binding.parameters:
            validate_type(parameter.type, binding.source)
        validate_type(binding.result, binding.source)
    named_routes = []
    declarations_with_sources = []
    for declaration in api.declarations:
        projections = [
            item for item in declaration.projections
            if item.backend is BackendFamily.JVM
        ]
        if not projections:
            continue
        if len(projections) != 1:
            raise JvmRouteError(
                f"type {declaration.name!r} has multiple JVM projections"
            )
        projection = projections[0]
        try:
            source = source_by_id[projection.source.declaration_id]
        except KeyError as exc:
            raise JvmRouteError(
                f"missing JVM owner source {projection.source.declaration_id!r}"
            ) from exc
        kind = {
            "enum": SemanticTypeKind.ENUM_REF,
            "value": SemanticTypeKind.VALUE_REF,
            "object": SemanticTypeKind.OBJECT_REF,
        }[declaration.kind.value]
        named = JvmNamedTypeRoute(
            declaration.type_id,
            declaration.name,
            source.owner_class,
            source.language,
            kind,
            source.provenance.declaration_id,
        )
        named_routes.append(named)
        declarations_with_sources.append((declaration, source, named))

    named = {item.type_id: item for item in named_routes}
    function_routes = []
    for binding in api.functions:
        if binding.source.language not in {"kotlin", "java"}:
            continue
        validate_binding(binding)
        if not binding.capabilities.javascript_public:
            # Hidden JVM routes are emitted through the typed C++ internal
            # facade, never registered as JavaScript functions.
            continue
        try:
            source_owner, source = declarations_by_id[
                binding.source.declaration_id
            ]
        except KeyError as exc:
            raise JvmRouteError(
                f"missing JVM function source {binding.source.declaration_id!r}"
            ) from exc
        if source_owner.form.value == "class" and not source.is_static:
            # Scalar implementation-owner methods remain on the JVM service
            # route. Composite owner setup is added with cross-family copied
            # routes.
            continue
        synthetic_owner = JvmNamedTypeRoute(
            "jvm:implementation-owner:" + source_owner.provenance.declaration_id,
            source_owner.source_name,
            source_owner.owner_class,
            source_owner.language,
            SemanticTypeKind.OBJECT_REF,
            source_owner.provenance.declaration_id,
        )
        function_routes.append(
            _callable(
                binding,
                source,
                named=named,
                owner=synthetic_owner,
                public_name=binding.name,
                result=binding.result,
                static=True,
                suspend=source.is_suspend,
            )
        )
    objects = []
    values = []
    enums = []
    for declaration, source, owner_route in declarations_with_sources:
        if declaration.kind.value == "enum":
            constants = tuple(declaration.constants)
            if constants != tuple(source.enum_constants):
                raise JvmRouteError(
                    f"semantic and JVM enum constants disagree for {declaration.name!r}"
                )
            enums.append(JvmEnumRoute(owner_route, constants))
            continue
        copied_value = isinstance(declaration, SemanticValueDeclaration)
        source_fields = {
            (item.name if copied_value else item.provenance.declaration_id): item
            for item in source.fields
        }
        fields = []
        for field in declaration.fields:
            try:
                source_field = source_fields[
                    field.name if copied_value else field.source.declaration_id
                ]
            except KeyError as exc:
                raise JvmRouteError(
                    f"missing JVM field source {field.source.declaration_id!r}"
                ) from exc
            fields.append(
                _field(
                    field,
                    source_field,
                    owner_route,
                    named,
                    copied_projection=copied_value,
                )
            )
        if isinstance(declaration, SemanticValueDeclaration):
            constructor, constructor_field_names = _value_constructor(
                declaration, source, owner_route, named
            )
            by_name = {item.public_name: item for item in fields}
            values.append(
                JvmValueRoute(
                    owner_route,
                    constructor,
                    tuple(by_name[name] for name in constructor_field_names),
                    tuple(fields),
                )
            )
            continue
        if not isinstance(declaration, SemanticObjectDeclaration):
            continue
        source_constructors = {
            item.provenance.declaration_id: item for item in source.constructors
        }
        constructor_route = None
        if declaration.constructor is not None:
            for parameter in declaration.constructor.parameters:
                validate_type(parameter.type, declaration.constructor.source)
            try:
                constructor_source = source_constructors[
                    declaration.constructor.source.declaration_id
                ]
            except KeyError as exc:
                raise JvmRouteError(
                    f"missing JVM constructor source for {declaration.name!r}"
                ) from exc
            constructor_route = _callable(
                declaration.constructor,
                constructor_source,
                named=named,
                owner=owner_route,
                public_name="create",
                result=SemanticType.object_ref(declaration.type_id),
                static=True,
                suspend=False,
            )
        source_methods = {
            item.provenance.declaration_id: item for item in source.declarations
        }
        methods = []
        for method in declaration.methods:
            validate_binding(method)
            try:
                method_source = source_methods[method.source.declaration_id]
            except KeyError as exc:
                raise JvmRouteError(
                    f"missing JVM method source {method.source.declaration_id!r}"
                ) from exc
            static = method.member_scope is MemberScope.STATIC
            if static != method_source.is_static:
                raise JvmRouteError("semantic and JVM method scopes disagree")
            methods.append(
                _callable(
                    method,
                    method_source,
                    named=named,
                    owner=owner_route,
                    public_name=method.name,
                    result=method.result,
                    static=static,
                    suspend=method_source.is_suspend,
                )
            )
        objects.append(
            JvmObjectRoute(
                owner_route,
                constructor_route,
                tuple(methods),
                tuple(fields),
            )
        )

    return JvmRoutePlan(
        tuple(function_routes),
        tuple(objects),
        tuple(values),
        tuple(enums),
        tuple(named_routes),
    )


__all__ = [
    "JvmCallableRoute",
    "JvmEnumRoute",
    "JvmFieldRoute",
    "JvmNamedTypeRoute",
    "JvmObjectRoute",
    "JvmRouteError",
    "JvmRoutePlan",
    "JvmValueRoute",
    "plan_jvm_routes",
]
