"""Projection of C++ frontend records into common Supernote semantics."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, Iterable, Optional, Protocol, Tuple

from .semantic import (
    BindingCapabilities,
    BindingKind,
    BackendFamily,
    DeclarationRole,
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticConstructor,
    SemanticEnumDeclaration,
    SemanticField,
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticProjection,
    SemanticType,
    SemanticValueDeclaration,
    SourceProvenance,
    semantic_type_id,
)
from .source_models import (
    CppClassSource,
    CppConstructorSource,
    CppEnumSource,
    CppFieldSource,
    CppFunctionSource,
    CppMethodSource,
)


class CppProjectionError(ValueError):
    """A source-located C++ to semantic projection diagnostic."""


class _CppDeclaration(Protocol):
    provenance: SourceProvenance


_CPP_TYPES = {
    "void": SemanticType.VOID,
    "bool": SemanticType.BOOL,
    "int32_t": SemanticType.INT32,
    "std::int32_t": SemanticType.INT32,
    "int64_t": SemanticType.INT64,
    "std::int64_t": SemanticType.INT64,
    "float": SemanticType.FLOAT32,
    "double": SemanticType.FLOAT64,
    "std::string": SemanticType.STRING,
    "std::vector<std::byte>": SemanticType.BYTES,
}


@dataclass(frozen=True)
class _CppNamedType:
    qualified_name: str
    public_name: str
    kind: str
    type_id: str


class _CppTypeRegistry:
    def __init__(
        self,
        feature_id: str,
        classes: Iterable[CppClassSource],
        enums: Iterable[CppEnumSource],
    ) -> None:
        self.feature_id = feature_id
        self.by_qualified: dict[str, _CppNamedType] = {}
        self.by_final: dict[str, list[_CppNamedType]] = {}
        for item in classes:
            kind = "object" if item.intent.declares_object else (
                "value" if item.intent.declares_value else "owner"
            )
            if kind == "owner":
                continue
            self._add(item.qualified_name, item.cpp_name, kind)
        for item in enums:
            self._add(item.qualified_name, item.cpp_name, "enum")

    def _add(self, qualified_name: str, public_name: str, kind: str) -> None:
        value = _CppNamedType(
            qualified_name,
            public_name,
            kind,
            semantic_type_id(self.feature_id, public_name),
        )
        if qualified_name in self.by_qualified:
            raise CppProjectionError(
                f"duplicate marked C++ type definition {qualified_name!r}"
            )
        self.by_qualified[qualified_name] = value
        self.by_final.setdefault(public_name, []).append(value)

    def resolve(
        self,
        spelling: str,
        namespace: Tuple[str, ...],
        source: _CppDeclaration,
    ) -> _CppNamedType:
        name = spelling.removeprefix("::")
        if "::" in name:
            candidate = self.by_qualified.get(name)
        else:
            candidate = None
            for count in range(len(namespace), -1, -1):
                qualified = "::".join((*namespace[:count], name))
                candidate = self.by_qualified.get(qualified)
                if candidate is not None:
                    break
            if candidate is None:
                matches = self.by_final.get(name, [])
                if len(matches) == 1:
                    candidate = matches[0]
                elif len(matches) > 1:
                    raise _error(
                        source,
                        f"ambiguous unqualified marked C++ type {spelling!r}; "
                        "use its exact namespace-qualified name",
                    )
        if candidate is None:
            raise _error(source, f"unknown marked C++ type {spelling!r}")
        return candidate


def _normalized_cpp_type(spelling: str) -> str:
    value = spelling.strip()
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s*<\s*", "<", value)
    value = re.sub(r"\s*>\s*", ">", value)
    value = re.sub(r"\s*&\s*", "&", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _template_inner(value: str, template: str) -> Optional[str]:
    prefix = template + "<"
    if not value.startswith(prefix) or not value.endswith(">"):
        return None
    inner = value[len(prefix):-1]
    depth = 0
    for character in inner:
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
        elif character == "," and depth == 0:
            return None
    return inner if inner and depth == 0 else None


def canonical_cpp_type(
    spelling: str,
    *,
    result: bool,
    source: _CppDeclaration,
    registry: Optional[_CppTypeRegistry] = None,
    namespace: Tuple[str, ...] = (),
    position: str = "direct",
) -> SemanticType:
    """Map one exact DR-027 C++ value spelling to a semantic type."""

    canonical = _normalized_cpp_type(spelling)
    semantic = _CPP_TYPES.get(canonical)
    if semantic is not None:
        if semantic is SemanticType.VOID and not result:
            raise _error(source, "void is valid only as a marked function result")
        return semantic
    if registry is not None:
        vector_inner = _template_inner(canonical, "std::vector")
        if vector_inner is not None:
            return SemanticType.array(
                canonical_cpp_type(
                    vector_inner,
                    result=False,
                    source=source,
                    registry=registry,
                    namespace=namespace,
                    position="contained",
                )
            )
        optional_inner = _template_inner(canonical, "std::optional")
        if optional_inner is not None:
            return SemanticType.nullable(
                canonical_cpp_type(
                    optional_inner,
                    result=False,
                    source=source,
                    registry=registry,
                    namespace=namespace,
                    position="contained",
                )
            )
        reference = canonical.endswith("&")
        core = canonical[:-1] if reference else canonical
        core_const = core.startswith("const ")
        if core_const:
            core = core[len("const "):]
        shared_inner = _template_inner(core, "std::shared_ptr")
        borrowed = reference and shared_inner is None
        named_spelling = shared_inner or core
        try:
            named = registry.resolve(named_spelling, namespace, source)
        except CppProjectionError as exc:
            if "ambiguous unqualified" in str(exc):
                raise
            named = None
        if named is not None:
            if named.kind == "object":
                valid_object_form = shared_inner is not None or (
                    borrowed and position == "parameter"
                )
                if result or position in {"field", "contained"}:
                    valid_object_form = (
                        shared_inner is not None and not reference and not core_const
                    )
                elif position == "parameter" and shared_inner is not None:
                    valid_object_form = (
                        (not reference and not core_const)
                        or (reference and core_const)
                    )
                if not valid_object_form:
                    expected = (
                        "std::shared_ptr<T>"
                        if result or position in {"field", "contained"}
                        else "T&, const T&, std::shared_ptr<T>, or const std::shared_ptr<T>&"
                    )
                    raise _error(
                        source,
                        f"native object {named.public_name!r} requires {expected} "
                        f"in this C++ position",
                    )
                return SemanticType.object_ref(named.type_id)
            if shared_inner is not None:
                raise _error(
                    source,
                    f"std::shared_ptr is valid only for native object types, not "
                    f"{named.kind} {named.public_name!r}",
                )
            if reference or core_const:
                raise _error(
                    source,
                    f"{named.kind} {named.public_name!r} must use its exact "
                    "owned value spelling in this C++ position",
                )
            if named.kind == "value":
                return SemanticType.value_ref(named.type_id)
            if named.kind == "enum":
                return SemanticType.enum_ref(named.type_id)
    if semantic is None:
        accepted = (
            "void, bool, int32_t/std::int32_t, int64_t/std::int64_t, "
            "float, double, std::string, or std::vector<std::byte>"
        )
        raise _error(
            source,
            f"unsupported marked C++ type {spelling!r}; use a canonical "
            f"owned V3 type ({accepted})",
        )
    raise AssertionError("unreachable")


def semantic_binding_id(source: CppFunctionSource) -> str:
    """Derive a semantic identity without using a JavaScript/public alias."""

    return f"supernote:binding:{source.provenance.declaration_id}"


def semantic_class_id(source: CppClassSource) -> str:
    return f"supernote:class:{source.provenance.declaration_id}"


def project_cpp_function(
    source: CppFunctionSource,
    *,
    registry: Optional[_CppTypeRegistry] = None,
) -> Optional[SemanticBinding]:
    """Project a marked C++ definition; ordinary code returns ``None``."""

    if source.intent.role is DeclarationRole.ORDINARY:
        return None
    if source.provenance.language == "c" or source.provenance.path.endswith(".c"):
        raise _error(
            source,
            "direct marked C bindings are unsupported in initial V3; place the "
            "marker on a canonical C++ boundary that calls ordinary C23 code",
        )
    if source.provenance.language != "cpp":
        raise _error(
            source,
            f"invalid C++ frontend language {source.provenance.language!r}",
        )

    parameters = tuple(
        SemanticParameter(
            parameter.name,
            canonical_cpp_type(
                parameter.type_spelling,
                result=False,
                source=source,
                registry=registry,
                namespace=source.namespace,
                position="parameter",
            ),
        )
        for parameter in source.parameters
    )
    result = canonical_cpp_type(
        source.return_type_spelling,
        result=True,
        source=source,
        registry=registry,
        namespace=source.namespace,
        position="result",
    )
    return SemanticBinding(
        binding_id=semantic_binding_id(source),
        kind=BindingKind.FUNCTION,
        name=source.cpp_name,
        capabilities=BindingCapabilities.for_role(source.intent.role),
        execution=source.intent.execution,
        parameters=parameters,
        result=result,
        source=source.provenance,
    )


def project_cpp_functions(sources: Iterable[CppFunctionSource]) -> SemanticApi:
    bindings = []
    for source in sources:
        binding = project_cpp_function(source)
        if binding is not None:
            bindings.append(binding)
    return SemanticApi(functions=tuple(bindings))


def _project_method(
    source: CppMethodSource,
    *,
    owner: CppClassSource,
    owner_id: str,
    class_kind: SemanticClassKind,
    registry: Optional[_CppTypeRegistry] = None,
) -> SemanticBinding:
    if source.intent.role is DeclarationRole.ORDINARY:
        raise _error(source, "ordinary methods do not become semantic bindings")
    if (
        class_kind is SemanticClassKind.INTERNAL_SERVICE
        and source.intent.role is not DeclarationRole.INTERNAL
    ):
        raise _error(
            source,
            "a SupernotePluginInternal class may contain only SupernotePluginInternal "
            "generated methods",
        )
    parameters = tuple(
        SemanticParameter(
            parameter.name,
            canonical_cpp_type(
                parameter.type_spelling,
                result=False,
                source=source,
                registry=registry,
                namespace=owner.namespace,
                position="parameter",
            ),
        )
        for parameter in source.parameters
    )
    result = canonical_cpp_type(
        source.return_type_spelling,
        result=True,
        source=source,
        registry=registry,
        namespace=owner.namespace,
        position="result",
    )
    kind = (
        BindingKind.OBJECT_METHOD
        if class_kind is SemanticClassKind.JS_OBJECT
        else BindingKind.SERVICE_METHOD
    )
    return SemanticBinding(
        binding_id=f"supernote:binding:{source.provenance.declaration_id}",
        kind=kind,
        name=source.cpp_name,
        capabilities=BindingCapabilities.for_role(source.intent.role),
        execution=source.intent.execution,
        parameters=parameters,
        result=result,
        source=source.provenance,
        owner_id=owner_id,
        owner_name=owner.cpp_name,
        member_scope=(MemberScope.STATIC if source.static else MemberScope.INSTANCE),
    )


def _constructor_parameters(
    source: CppConstructorSource,
    *,
    registry: Optional[_CppTypeRegistry] = None,
    namespace: Tuple[str, ...] = (),
) -> Optional[Tuple[SemanticParameter, ...]]:
    try:
        return tuple(
            SemanticParameter(
                parameter.name,
                canonical_cpp_type(
                    parameter.type_spelling,
                    result=False,
                    source=source,
                    registry=registry,
                    namespace=namespace,
                    position="parameter",
                ),
            )
            for parameter in source.parameters
        )
    except (CppProjectionError, ValueError):
        if source.selected:
            raise
        return None


def _select_js_constructor(
    source: CppClassSource,
    registry: Optional[_CppTypeRegistry] = None,
) -> tuple[CppConstructorSource, Tuple[SemanticParameter, ...]]:
    eligible = []
    for constructor in source.constructors:
        parameters = _constructor_parameters(
            constructor, registry=registry, namespace=source.namespace
        )
        if (
            constructor.access == "public"
            and not constructor.deleted
            and parameters is not None
        ):
            eligible.append((constructor, parameters))
        elif constructor.selected:
            raise _error(
                constructor,
                "SupernoteConstructor must select an eligible public, "
                "non-deleted constructor using canonical V3 value types",
            )
    if not eligible:
        raise _error(
            source,
            "a SupernotePluginExport class requires at least one eligible public "
            "constructor; returned-only objects are deferred",
        )
    selected = [item for item in eligible if item[0].selected]
    if len(eligible) == 1:
        if len(selected) > 1:  # pragma: no cover - defensive
            raise _error(source, "multiple constructors were selected")
        return selected[0] if selected else eligible[0]
    if len(selected) != 1:
        raise _error(
            source,
            "multiple eligible constructors require exactly one "
            "SupernoteConstructor selection",
        )
    return selected[0]


def _select_service_constructor(
    source: CppClassSource,
) -> CppConstructorSource:
    if any(constructor.selected for constructor in source.constructors):
        raise _error(
            source,
            "SupernoteConstructor does not apply to a SupernotePluginInternal "
            "feature service",
        )
    eligible = [
        constructor
        for constructor in source.constructors
        if constructor.access == "public"
        and not constructor.deleted
        and not constructor.parameters
    ]
    if len(eligible) != 1:
        raise _error(
            source,
            "a SupernotePluginInternal C++ class requires one unambiguous public "
            "zero-argument construction path",
        )
    return eligible[0]


def project_cpp_class(source: CppClassSource) -> Optional[SemanticClass]:
    role = source.intent.role
    if role is DeclarationRole.ORDINARY:
        return None
    if source.provenance.language != "cpp":
        raise _error(
            source,
            f"invalid C++ frontend language {source.provenance.language!r}",
        )
    class_kind = (
        SemanticClassKind.JS_OBJECT
        if role is DeclarationRole.EXPORTED
        else SemanticClassKind.INTERNAL_SERVICE
    )
    class_id = semantic_class_id(source)
    if class_kind is SemanticClassKind.JS_OBJECT:
        constructor_source, constructor_parameters = _select_js_constructor(source)
    else:
        constructor_source = _select_service_constructor(source)
        constructor_parameters = ()
    methods = tuple(
        _project_method(
            method,
            owner=source,
            owner_id=class_id,
            class_kind=class_kind,
        )
        for method in source.methods
        if method.intent.role is not DeclarationRole.ORDINARY
    )
    return SemanticClass(
        class_id=class_id,
        kind=class_kind,
        name=source.cpp_name,
        capabilities=BindingCapabilities.for_role(role),
        source=source.provenance,
        constructor=SemanticConstructor(
            constructor_source.provenance,
            constructor_parameters,
        ),
        methods=methods,
    )


def project_cpp_api(
    functions: Iterable[CppFunctionSource],
    classes: Iterable[CppClassSource],
    enums: Iterable[CppEnumSource] = (),
    *,
    feature_id: str = "supernote:feature:legacy",
) -> SemanticApi:
    class_sources = tuple(classes)
    enum_sources = tuple(enums)
    registry = _CppTypeRegistry(feature_id, class_sources, enum_sources)
    bindings: list[SemanticBinding] = []
    for source in functions:
        binding = project_cpp_function(source, registry=registry)
        if binding is not None:
            bindings.append(binding)
    legacy_classes: list[SemanticClass] = []
    declarations = []
    for source in class_sources:
        if source.intent.declares_object:
            declarations.append(_project_cpp_object(source, registry, feature_id))
        elif source.intent.declares_value:
            declarations.append(_project_cpp_value(source, registry, feature_id))
        elif any(
            method.intent.role is not DeclarationRole.ORDINARY
            for method in source.methods
        ):
            generated_methods = tuple(
                method for method in source.methods
                if method.intent.role is not DeclarationRole.ORDINARY
            )
            if any(not method.static for method in generated_methods):
                eligible = [
                    constructor for constructor in source.constructors
                    if constructor.access == "public"
                    and not constructor.deleted
                    and not constructor.parameters
                ]
                if len(eligible) != 1:
                    raise _error(
                        source,
                        "an unmarked C++ implementation owner with instance "
                        "methods requires one unambiguous public zero-argument "
                        "construction path",
                    )
            if any(constructor.selected for constructor in source.constructors):
                raise _error(
                    source,
                    "SupernoteConstructor is valid only on a "
                    "SupernotePluginObject class",
                )
            bindings.extend(
                _project_implementation_method(source, method, registry)
                for method in generated_methods
            )
        else:
            semantic_class = project_cpp_class(source)
            if semantic_class is not None:
                legacy_classes.append(semantic_class)
    declarations.extend(
        _project_cpp_enum(source, feature_id) for source in enum_sources
    )
    return SemanticApi(tuple(bindings), tuple(legacy_classes), tuple(declarations))


def _field_type(
    source: CppFieldSource,
    owner: CppClassSource,
    registry: _CppTypeRegistry,
) -> SemanticType:
    return canonical_cpp_type(
        source.type_spelling,
        result=False,
        source=source,
        registry=registry,
        namespace=owner.namespace,
        position="field",
    )


def _semantic_field(
    source: CppFieldSource,
    owner: CppClassSource,
    owner_id: str,
    registry: _CppTypeRegistry,
) -> SemanticField:
    if source.access != "public":
        raise _error(source, "a generated C++ field must be public")
    if source.static:
        raise _error(source, "static generated fields are unsupported")
    if source.intent.role is not DeclarationRole.EXPORTED:
        raise _error(source, "generated fields require SupernotePluginExport")
    return SemanticField(
        field_id=f"{owner_id}:field:{source.cpp_name}",
        owner_id=owner_id,
        name=source.cpp_name,
        type=_field_type(source, owner, registry),
        source=source.provenance,
        mutable=source.mutable,
    )


def _selected_object_constructor(
    source: CppClassSource,
    registry: _CppTypeRegistry,
) -> Optional[SemanticConstructor]:
    selected = [item for item in source.constructors if item.selected]
    if len(selected) > 1:
        raise _error(source, "an object may select at most one SupernoteConstructor")
    if not selected:
        return None
    constructor = selected[0]
    if constructor.access != "public" or constructor.deleted:
        raise _error(
            constructor,
            "SupernoteConstructor must select a public non-deleted constructor",
        )
    parameters = _constructor_parameters(
        constructor, registry=registry, namespace=source.namespace
    )
    assert parameters is not None
    return SemanticConstructor(constructor.provenance, parameters)


def _project_cpp_object(
    source: CppClassSource,
    registry: _CppTypeRegistry,
    feature_id: str,
) -> SemanticObjectDeclaration:
    type_id = semantic_type_id(feature_id, source.cpp_name)
    methods = tuple(
        _project_method(
            method,
            owner=source,
            owner_id=type_id,
            class_kind=SemanticClassKind.JS_OBJECT,
            registry=registry,
        )
        for method in source.methods
        if method.intent.role is not DeclarationRole.ORDINARY
    )
    fields = tuple(
        _semantic_field(item, source, type_id, registry) for item in source.fields
    )
    return SemanticObjectDeclaration(
        feature_id,
        type_id,
        source.cpp_name,
        SemanticProjection(BackendFamily.CPP, source.provenance),
        _selected_object_constructor(source, registry),
        methods,
        fields,
    )


def _project_cpp_value(
    source: CppClassSource,
    registry: _CppTypeRegistry,
    feature_id: str,
) -> SemanticValueDeclaration:
    if any(item.selected for item in source.constructors):
        raise _error(source, "SupernoteConstructor cannot mark a value type")
    if any(
        item.intent.role is not DeclarationRole.ORDINARY for item in source.methods
    ):
        raise _error(source, "value types expose fields, not generated methods")
    type_id = semantic_type_id(feature_id, source.cpp_name)
    fields = tuple(
        _semantic_field(item, source, type_id, registry) for item in source.fields
    )
    return SemanticValueDeclaration(
        feature_id,
        type_id,
        source.cpp_name,
        fields,
        (SemanticProjection(BackendFamily.CPP, source.provenance),),
    )


def _project_cpp_enum(
    source: CppEnumSource,
    feature_id: str,
) -> SemanticEnumDeclaration:
    return SemanticEnumDeclaration(
        feature_id,
        semantic_type_id(feature_id, source.cpp_name),
        source.cpp_name,
        source.constants,
        (SemanticProjection(BackendFamily.CPP, source.provenance),),
    )


def _project_implementation_method(
    owner: CppClassSource,
    source: CppMethodSource,
    registry: _CppTypeRegistry,
) -> SemanticBinding:
    return SemanticBinding(
        binding_id=f"supernote:binding:{source.provenance.declaration_id}",
        kind=BindingKind.FUNCTION,
        name=source.cpp_name,
        capabilities=BindingCapabilities.for_role(source.intent.role),
        execution=source.intent.execution,
        parameters=tuple(
            SemanticParameter(
                item.name,
                canonical_cpp_type(
                    item.type_spelling,
                    result=False,
                    source=source,
                    registry=registry,
                    namespace=owner.namespace,
                    position="parameter",
                ),
            )
            for item in source.parameters
        ),
        result=canonical_cpp_type(
            source.return_type_spelling,
            result=True,
            source=source,
            registry=registry,
            namespace=owner.namespace,
            position="result",
        ),
        source=source.provenance,
    )


def cpp_type_table() -> Dict[str, SemanticType]:
    """Return a copy for diagnostics/tests without exposing mutable state."""

    return dict(_CPP_TYPES)


def _error(source: _CppDeclaration, message: str) -> CppProjectionError:
    provenance = source.provenance
    return CppProjectionError(
        f"{provenance.path}:{provenance.line}:{provenance.column}: {message}"
    )
