from __future__ import annotations

import pytest

from supernote_module_generator.cpp_class_syntax import (
    CppClassExtent,
    CppClassSyntaxError,
    class_definition_extent,
)
from supernote_module_generator.cpp_lexer import _Token, _lex_source


def _tokens(source: str) -> list[_Token]:
    return list(_lex_source(source).tokens)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("class Page {};", CppClassExtent("class", "Page", 2, 3)),
        ("struct Page {};", CppClassExtent("struct", "Page", 2, 3)),
        (
            "class Page { void draw() { if (ready) {} } }; trailing",
            CppClassExtent("class", "Page", 2, 15),
        ),
    ],
)
def test_class_definition_extent_returns_complete_balanced_envelope(
    source: str, expected: CppClassExtent
):
    assert class_definition_extent(_tokens(source), diagnostic_line=41) == expected


@pytest.mark.parametrize(
    ("source", "line", "export_name", "message"),
    [
        ("double value;", 1, None, "must be followed by a class or struct"),
        ("class { };", 1, None, "must have an ordinary identifier name"),
        ("class Page;", 1, "Page", "complete definition, not a declaration"),
        ("class Page final;", 1, "Page", "complete definition, not a declaration"),
        ("\nclass Page : Base {};", 2, "Page", "inheritance is not supported"),
        ("\nclass Page final {};", 2, "Page", "unsupported tokens before"),
        ("\nclass Page {", 2, "Page", "missing its closing"),
        ("\nclass Page {}", 2, "Page", "must end with"),
    ],
)
def test_class_definition_extent_retains_failure_precedence_and_context(
    source: str,
    line: int,
    export_name: str | None,
    message: str,
):
    with pytest.raises(CppClassSyntaxError, match=message) as raised:
        class_definition_extent(_tokens(source), diagnostic_line=41)

    assert raised.value.line == line
    assert raised.value.export_name == export_name


def test_empty_class_definition_uses_marker_diagnostic_line():
    with pytest.raises(CppClassSyntaxError, match="complete class or struct") as raised:
        class_definition_extent([], diagnostic_line=41)

    assert raised.value.line == 41
    assert raised.value.export_name is None
