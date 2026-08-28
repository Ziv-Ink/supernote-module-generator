from __future__ import annotations

import pytest

from supernote_module_generator.cpp_lexer import _Token
from supernote_module_generator.cpp_source_routing import (
    CppMarkerPolicy,
    first_owned_jni_bootstrap,
    forbidden_marker_message,
    source_route,
)


@pytest.mark.parametrize(
    ("suffix", "inspect", "policy", "parses_functions", "message"),
    [
        (".cpp", True, CppMarkerPolicy.PARSE_FUNCTIONS, True, None),
        (".CPP", True, CppMarkerPolicy.PARSE_FUNCTIONS, True, None),
        (".cc", True, CppMarkerPolicy.PARSE_FUNCTIONS, True, None),
        (".cxx", True, CppMarkerPolicy.PARSE_FUNCTIONS, True, None),
        (".hpp", True, CppMarkerPolicy.DEFER_TO_HEADER_FRONTEND, False, None),
        (".H", True, CppMarkerPolicy.DEFER_TO_HEADER_FRONTEND, False, None),
        (
            ".c",
            True,
            CppMarkerPolicy.REJECT_C,
            False,
            "direct marked C bindings are unsupported in initial V4; use "
            "ordinary C23 implementation code behind a canonical marked C++ "
            "boundary",
        ),
        (
            ".inl",
            True,
            CppMarkerPolicy.REJECT_NON_CPP,
            False,
            "free-function Supernote markers are allowed only in .cc, .cpp, "
            "or .cxx files",
        ),
        (".txt", False, CppMarkerPolicy.IGNORE, False, None),
        ("", False, CppMarkerPolicy.IGNORE, False, None),
    ],
)
def test_source_route_is_complete_and_case_insensitive(
    suffix: str,
    inspect: bool,
    policy: CppMarkerPolicy,
    parses_functions: bool,
    message: str | None,
):
    route = source_route(suffix)

    assert route.inspect is inspect
    assert route.marker_policy is policy
    assert route.parses_functions is parses_functions
    assert forbidden_marker_message(route) == message


def token(value: str, index: int) -> _Token:
    return _Token(value, index, index + len(value), 1, "identifier", 0, 0)


def test_first_owned_jni_bootstrap_requires_the_call_boundary():
    first = token("JNI_OnLoad", 0)
    second = token("(", 10)
    later = token("JNI_OnLoad", 20)
    tokens = (token("ordinary", -10), first, second, later, token("(", 30))

    assert first_owned_jni_bootstrap(tokens) is first
    assert first_owned_jni_bootstrap((first, token(";", 10))) is None
    assert first_owned_jni_bootstrap((first,)) is None
    assert first_owned_jni_bootstrap(()) is None
