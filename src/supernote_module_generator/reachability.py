"""Compute the JavaScript-public V3 type graph from common semantics."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, Tuple

from .semantic import (
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticDeclaration,
    SemanticEnumDeclaration,
    SemanticModelError,
    SemanticObjectDeclaration,
    SemanticValueDeclaration,
)
from .semantic_types import SemanticType, SemanticTypeKind


class PublicReachabilityError(SemanticModelError):
    """Raised when explicit public intent cannot form one valid JS surface."""


@dataclass(frozen=True)
class PublicApi:
    """Closed public view consumed by TypeScript and later runtime lowerings."""

    functions: Tuple[SemanticBinding, ...]
    declarations: Tuple[SemanticDeclaration, ...]
    object_namespaces: frozenset[str]
    object_instances: frozenset[str]

    def declaration(self, type_id: str) -> SemanticDeclaration:
        for item in self.declarations:
            if item.type_id == type_id:
                return item
        raise KeyError(type_id)


def compute_public_api(
    api: SemanticApi,
    *,
    feature_name: str | None = None,
) -> PublicApi:
    """Return the deterministic transitive public surface for ``api``.

    Static object methods and marked constructors are roots. Instance members
    become graph edges only after an ObjectRef (or constructor) makes their
    receiver type reachable.
    """

    declarations: Dict[str, SemanticDeclaration] = {
        item.type_id: item for item in api.declarations
    }
    public_functions = tuple(
        sorted(
            (
                item
                for item in api.functions
                if item.capabilities.javascript_public
            ),
            key=lambda item: (item.name, item.binding_id),
        )
    )
    reachable: set[str] = set()
    object_namespaces: set[str] = set()
    object_instances: set[str] = set()
    expanded_values: set[str] = set()
    expanded_objects: set[str] = set()
    pending: Deque[SemanticType] = deque()

    for binding in public_functions:
        _queue_binding(pending, binding)

    for declaration in api.declarations:
        if not isinstance(declaration, SemanticObjectDeclaration):
            continue
        static_methods = tuple(
            method
            for method in declaration.methods
            if method.capabilities.javascript_public
            and method.member_scope is MemberScope.STATIC
        )
        if declaration.constructor is not None or static_methods:
            object_namespaces.add(declaration.type_id)
            reachable.add(declaration.type_id)
        if declaration.constructor is not None:
            object_instances.add(declaration.type_id)
            pending.append(SemanticType.object_ref(declaration.type_id))
            pending.extend(parameter.type for parameter in declaration.constructor.parameters)
        for method in static_methods:
            _queue_binding(pending, method)

    while pending:
        semantic_type = pending.popleft()
        if semantic_type.kind in {SemanticTypeKind.ARRAY, SemanticTypeKind.NULLABLE}:
            assert semantic_type.element is not None
            pending.append(semantic_type.element)
            continue
        if semantic_type.type_id is None:
            continue
        declaration = declarations[semantic_type.type_id]
        reachable.add(declaration.type_id)
        if isinstance(declaration, SemanticValueDeclaration):
            if declaration.type_id in expanded_values:
                continue
            expanded_values.add(declaration.type_id)
            pending.extend(field.type for field in declaration.fields)
        elif isinstance(declaration, SemanticObjectDeclaration):
            object_instances.add(declaration.type_id)

        # Expanding object members is delayed until an ObjectRef or constructor
        # reaches the instance. A namespace-only static API is insufficient.
        while True:
            unexpanded = sorted(object_instances - expanded_objects)
            if not unexpanded:
                break
            type_id = unexpanded[0]
            expanded_objects.add(type_id)
            declaration = declarations[type_id]
            assert isinstance(declaration, SemanticObjectDeclaration)
            for method in declaration.methods:
                if (
                    method.capabilities.javascript_public
                    and method.member_scope is MemberScope.INSTANCE
                ):
                    _queue_binding(pending, method)
            pending.extend(field.type for field in declaration.fields)

    _reject_unreachable_members(api, reachable, object_instances)
    public_declarations = tuple(
        sorted(
            (item for item in api.declarations if item.type_id in reachable),
            key=lambda item: (item.name, item.type_id),
        )
    )
    public = PublicApi(
        functions=public_functions,
        declarations=public_declarations,
        object_namespaces=frozenset(object_namespaces),
        object_instances=frozenset(object_instances),
    )
    _validate_public_names(public, feature_name=feature_name)
    return public


def _queue_binding(pending: Deque[SemanticType], binding: SemanticBinding) -> None:
    pending.extend(parameter.type for parameter in binding.parameters)
    pending.append(binding.result)


def _reject_unreachable_members(
    api: SemanticApi,
    reachable: set[str],
    object_instances: set[str],
) -> None:
    for declaration in api.declarations:
        if isinstance(declaration, SemanticValueDeclaration):
            if declaration.type_id not in reachable and declaration.fields:
                field = declaration.fields[0]
                raise PublicReachabilityError(
                    f"{field.source.location}: exported value field "
                    f"{declaration.name}.{field.name} is unreachable from every "
                    "public function, method, or constructor"
                )
            continue
        if not isinstance(declaration, SemanticObjectDeclaration):
            continue
        if declaration.type_id in object_instances:
            continue
        instance_methods = tuple(
            method
            for method in declaration.methods
            if method.capabilities.javascript_public
            and method.member_scope is MemberScope.INSTANCE
        )
        if instance_methods:
            method = instance_methods[0]
            raise PublicReachabilityError(
                f"{method.source.location}: exported instance method "
                f"{declaration.name}.{method.name} is unreachable because no public "
                f"root produces or constructs {declaration.name}"
            )
        if declaration.fields:
            field = declaration.fields[0]
            raise PublicReachabilityError(
                f"{field.source.location}: exported object field "
                f"{declaration.name}.{field.name} is unreachable because no public "
                f"root produces or constructs {declaration.name}"
            )


def _validate_public_names(public: PublicApi, *, feature_name: str | None) -> None:
    declarations = {item.type_id: item for item in public.declarations}
    feature_names: Dict[str, str] = {}
    function_names: Dict[str, str] = {}
    for binding in public.functions:
        _claim(feature_names, binding.name, binding.source.location, "feature root")
        function_names[binding.name] = binding.source.location
    # Every reachable named type owns a runtime companion on the feature root.
    # This includes returned-only objects and copied value/enum declarations,
    # not only object types with constructors or static methods.
    for declaration in public.declarations:
        _claim(
            feature_names,
            declaration.name,
            _declaration_location(declaration),
            "feature root",
        )

    generated_type_names = {
        "SupernoteError": "generated TypeScript error class",
        "SupernoteErrorCode": "generated TypeScript error-code type",
        "SupernoteValidationReason": "generated validation-reason type",
        "SupernoteValidationDetails": "generated validation-details interface",
        "SupernoteTypeError": "generated validation error type",
        "SupernoteRangeError": "generated validation error type",
        "SupernoteValidationResult": "generated validation-result type",
        "SupernoteCallable": "generated callable interface",
        "SupernoteTypeCompanion": "generated type-companion interface",
        "SupernoteFeatureStatus": "generated feature-status type",
        "SupernoteNativeObjectInfo": "generated native-object information interface",
    }
    if feature_name is not None:
        generated_type_names[f"{feature_name}Feature"] = (
            "generated TypeScript feature interface"
        )
    for declaration in public.declarations:
        root_location = function_names.get(declaration.name)
        if root_location is not None:
            raise PublicReachabilityError(
                f"{_declaration_location(declaration)}: reachable type name "
                f"{declaration.name!r} collides with a feature-root property "
                f"declared at {root_location}"
            )
        previous = generated_type_names.get(declaration.name)
        if previous is not None:
            raise PublicReachabilityError(
                f"{_declaration_location(declaration)}: reachable type name "
                f"{declaration.name!r} collides with {previous}"
            )
        generated_type_names[declaration.name] = _declaration_location(declaration)

    for type_id in sorted(public.object_namespaces):
        declaration = declarations[type_id]
        assert isinstance(declaration, SemanticObjectDeclaration)
        namespace: Dict[str, str] = {}
        _claim(
            namespace,
            "is",
            _declaration_location(declaration),
            f"{declaration.name} type namespace",
        )
        _claim(
            namespace,
            "check",
            _declaration_location(declaration),
            f"{declaration.name} type namespace",
        )
        if declaration.constructor is not None:
            _claim(
                namespace,
                "create",
                declaration.constructor.source.location,
                f"{declaration.name} type namespace",
            )
        for method in declaration.methods:
            if (
                method.capabilities.javascript_public
                and method.member_scope is MemberScope.STATIC
            ):
                _claim(
                    namespace,
                    method.name,
                    method.source.location,
                    f"{declaration.name} type namespace",
                )


def _claim(claims: Dict[str, str], name: str, location: str, namespace: str) -> None:
    previous = claims.get(name)
    if previous is not None:
        raise PublicReachabilityError(
            f"{location}: public name {name!r} collides in the {namespace}; "
            f"first declared at {previous}"
        )
    claims[name] = location


def _declaration_location(declaration: SemanticDeclaration) -> str:
    if isinstance(declaration, SemanticObjectDeclaration):
        return declaration.projection.source.location
    return declaration.projections[0].source.location
