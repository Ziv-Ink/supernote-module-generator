"""Projection of C++ frontend records into common Supernote semantics."""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Protocol, Tuple

from .semantic import (
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticConstructor,
    SemanticParameter,
    SemanticType,
    SourceProvenance,
)
from .source_models import (
    CppClassSource,
    CppConstructorSource,
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


def canonical_cpp_type(
    spelling: str,
    *,
    result: bool,
    source: _CppDeclaration,
) -> SemanticType:
    """Map one exact DR-027 C++ value spelling to a semantic type."""

    canonical = spelling.strip()
    canonical = re.sub(r"\s*::\s*", "::", canonical)
    canonical = re.sub(r"\s*<\s*", "<", canonical)
    canonical = re.sub(r"\s*>\s*", ">", canonical)
    semantic = _CPP_TYPES.get(canonical)
    if semantic is None:
        accepted = (
            "void, bool, int32_t/std::int32_t, int64_t/std::int64_t, "
            "float, double, std::string, or std::vector<std::byte>"
        )
        raise _error(
            source,
            f"unsupported marked C++ type {spelling!r}; use a canonical "
            f"owned V2 type ({accepted})",
        )
    if semantic is SemanticType.VOID and not result:
        raise _error(source, "void is valid only as a marked function result")
    return semantic


def semantic_binding_id(source: CppFunctionSource) -> str:
    """Derive a semantic identity without using a JavaScript/public alias."""

    return f"supernote:binding:{source.provenance.declaration_id}"


def semantic_class_id(source: CppClassSource) -> str:
    return f"supernote:class:{source.provenance.declaration_id}"


def project_cpp_function(
    source: CppFunctionSource,
) -> Optional[SemanticBinding]:
    """Project a marked C++ definition; ordinary code returns ``None``."""

    if source.intent.role is DeclarationRole.ORDINARY:
        return None
    if source.provenance.language == "c" or source.provenance.path.endswith(".c"):
        raise _error(
            source,
            "direct marked C bindings are unsupported in initial V2; place the "
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
            ),
        )
        for parameter in source.parameters
    )
    result = canonical_cpp_type(
        source.return_type_spelling,
        result=True,
        source=source,
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
) -> SemanticBinding:
    if source.intent.role is DeclarationRole.ORDINARY:
        raise _error(source, "ordinary methods do not become semantic bindings")
    if (
        class_kind is SemanticClassKind.INTERNAL_SERVICE
        and source.intent.role is not DeclarationRole.INTERNAL
    ):
        raise _error(
            source,
            "a SupernoteInternal class may contain only SupernoteInternal "
            "generated methods",
        )
    parameters = tuple(
        SemanticParameter(
            parameter.name,
            canonical_cpp_type(
                parameter.type_spelling,
                result=False,
                source=source,
            ),
        )
        for parameter in source.parameters
    )
    result = canonical_cpp_type(
        source.return_type_spelling,
        result=True,
        source=source,
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
    )


def _constructor_parameters(
    source: CppConstructorSource,
) -> Optional[Tuple[SemanticParameter, ...]]:
    try:
        return tuple(
            SemanticParameter(
                parameter.name,
                canonical_cpp_type(
                    parameter.type_spelling,
                    result=False,
                    source=source,
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
) -> tuple[CppConstructorSource, Tuple[SemanticParameter, ...]]:
    eligible = []
    for constructor in source.constructors:
        parameters = _constructor_parameters(constructor)
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
                "non-deleted constructor using canonical V2 value types",
            )
    if not eligible:
        raise _error(
            source,
            "a SupernoteExport class requires at least one eligible public "
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
            "SupernoteConstructor does not apply to a SupernoteInternal "
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
            "a SupernoteInternal C++ class requires one unambiguous public "
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
) -> SemanticApi:
    bindings = []
    for source in functions:
        binding = project_cpp_function(source)
        if binding is not None:
            bindings.append(binding)
    semantic_classes = []
    for source in classes:
        semantic_class = project_cpp_class(source)
        if semantic_class is not None:
            semantic_classes.append(semantic_class)
    return SemanticApi(tuple(bindings), tuple(semantic_classes))


def cpp_type_table() -> Dict[str, SemanticType]:
    """Return a copy for diagnostics/tests without exposing mutable state."""

    return dict(_CPP_TYPES)


def _error(source: _CppDeclaration, message: str) -> CppProjectionError:
    provenance = source.provenance
    return CppProjectionError(
        f"{provenance.path}:{provenance.line}:{provenance.column}: {message}"
    )
