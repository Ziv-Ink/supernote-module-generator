"""Projection of C++ frontend records into common Supernote semantics."""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional

from .semantic import (
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    SemanticApi,
    SemanticBinding,
    SemanticParameter,
    SemanticType,
)
from .source_models import CppFunctionSource


class CppProjectionError(ValueError):
    """A source-located C++ to semantic projection diagnostic."""


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
    source: CppFunctionSource,
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


def cpp_type_table() -> Dict[str, SemanticType]:
    """Return a copy for diagnostics/tests without exposing mutable state."""

    return dict(_CPP_TYPES)


def _error(source: CppFunctionSource, message: str) -> CppProjectionError:
    provenance = source.provenance
    return CppProjectionError(
        f"{provenance.path}:{provenance.line}:{provenance.column}: {message}"
    )
