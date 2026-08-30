"""Pure source-family decisions for the C++ binding frontend."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .cpp_lexer import _Token


CPP_IMPLEMENTATION_SUFFIXES = frozenset({".cc", ".cpp", ".cxx"})
CPP_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
CPP_HELPER_SUFFIXES = frozenset({".inl", ".inc", ".ipp", ".tpp"})


class CppMarkerPolicy(str, Enum):
    """How Supernote markers participate in one source-file family."""

    IGNORE = "ignore"
    PARSE_FUNCTIONS = "parse_functions"
    DEFER_TO_HEADER_FRONTEND = "defer_to_header_frontend"
    REJECT_C = "reject_c"
    REJECT_NON_CPP = "reject_non_cpp"


@dataclass(frozen=True)
class CppSourceRoute:
    """Immutable source-family route used before any declaration lowering."""

    suffix: str
    inspect: bool
    marker_policy: CppMarkerPolicy

    @property
    def parses_functions(self) -> bool:
        return self.marker_policy is CppMarkerPolicy.PARSE_FUNCTIONS


def source_route(suffix: str) -> CppSourceRoute:
    """Classify a case-insensitive suffix without inspecting file contents."""

    normalized = suffix.lower()
    if normalized in CPP_IMPLEMENTATION_SUFFIXES:
        policy = CppMarkerPolicy.PARSE_FUNCTIONS
    elif normalized in CPP_HEADER_SUFFIXES:
        policy = CppMarkerPolicy.DEFER_TO_HEADER_FRONTEND
    elif normalized == ".c":
        policy = CppMarkerPolicy.REJECT_C
    elif normalized in CPP_HELPER_SUFFIXES:
        policy = CppMarkerPolicy.REJECT_NON_CPP
    else:
        return CppSourceRoute(normalized, False, CppMarkerPolicy.IGNORE)
    return CppSourceRoute(normalized, True, policy)


def forbidden_marker_message(route: CppSourceRoute) -> str | None:
    """Return the policy diagnostic for a marker rejected by this route."""

    if route.marker_policy is CppMarkerPolicy.REJECT_C:
        return (
            "direct marked C bindings are unsupported; use "
            "ordinary C23 implementation code behind a canonical marked C++ "
            "boundary"
        )
    if route.marker_policy is CppMarkerPolicy.REJECT_NON_CPP:
        return (
            "free-function Supernote markers are allowed only in .cc, .cpp, "
            "or .cxx files"
        )
    return None


def first_owned_jni_bootstrap(tokens: Sequence[_Token]) -> _Token | None:
    """Return the first generated bootstrap symbol declaration candidate."""

    return next(
        (
            token
            for index, token in enumerate(tokens[:-1])
            if token.value == "JNI_OnLoad" and tokens[index + 1].value == "("
        ),
        None,
    )
