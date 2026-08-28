from __future__ import annotations

import pytest

from supernote_module_generator.cpp_global_functions import (
    CppGlobalFunctionShape,
    global_function_shape,
)
from supernote_module_generator.cpp_lexer import _Token, _lex_source


def _tokens(source: str) -> list[_Token]:
    return [token for token in _lex_source(source).tokens if token.conditional_depth == 0]


def _shape(source: str, name: str = "add") -> CppGlobalFunctionShape | None:
    tokens = _tokens(source)
    name_index = next(index for index, token in enumerate(tokens) if token.value == name)
    namespace_depth = tokens[name_index].brace_depth
    return global_function_shape(
        tokens,
        name_index=name_index,
        namespace_depth=namespace_depth,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("double add(double value) { return value; }", CppGlobalFunctionShape("definition", 6)),
        ("double add(double value);", CppGlobalFunctionShape("declaration", 6)),
        ("double add(double value) noexcept { return value; }", CppGlobalFunctionShape("definition", 7)),
        ("double add(double value) noexcept(true);", CppGlobalFunctionShape("declaration", 10)),
        ("auto add(double value) -> double { return value; }", CppGlobalFunctionShape("definition", 8)),
    ],
)
def test_global_function_shape_recognizes_supported_untagged_boundaries(
    source: str, expected: CppGlobalFunctionShape
):
    assert _shape(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "add(double value);",
        "double add(double value",
        "double add;",
        "value = add(double value);",
        "object.add(double value);",
        "sample::add(double value);",
        "call(add(double value));",
        "values[add(double value)];",
        "call(first, add(double value));",
    ],
)
def test_global_function_shape_ignores_calls_assignments_and_incomplete_forms(
    source: str,
):
    assert _shape(source) is None


def test_global_function_shape_requires_terminator_at_namespace_depth():
    tokens = _tokens("double add(double value) { return value; }")
    name_index = next(index for index, token in enumerate(tokens) if token.value == "add")

    assert global_function_shape(
        tokens,
        name_index=name_index,
        namespace_depth=7,
    ) is None
