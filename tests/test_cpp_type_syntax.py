from __future__ import annotations

import pytest

from supernote_module_generator.cpp_lexer import _Token, _lex_source
from supernote_module_generator.cpp_type_syntax import (
    CppParameterSyntax,
    CppTypeSyntaxError,
    cpp_type_spelling,
    parameter_list_syntax,
    parameter_syntax,
)


def _tokens(source: str) -> list[_Token]:
    return list(_lex_source(source).tokens)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Widget", "Widget"),
        ("sample :: Widget", "sample::Widget"),
        ("std :: vector < std :: byte >", "std::vector<std::byte>"),
        ("const Widget &", "const Widget&"),
        ("", None),
        ("Widget *", None),
        ("Widget <", None),
        ("Widget >", None),
        ("Widget & const", None),
    ],
)
def test_cpp_type_spelling_preserves_the_existing_structural_normalization(
    source: str, expected: str | None
):
    assert cpp_type_spelling(_tokens(source)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("double value", CppParameterSyntax("double", "value")),
        ("std :: int32_t page", CppParameterSyntax("std::int32_t", "page")),
        (
            "std :: vector < std :: byte > bytes",
            CppParameterSyntax("std::vector<std::byte>", "bytes"),
        ),
        ("const Widget & value", CppParameterSyntax("const Widget&", "value")),
    ],
)
def test_parameter_syntax_returns_one_named_type_decision(
    source: str, expected: CppParameterSyntax
):
    assert parameter_syntax(
        _tokens(source),
        argument_index=2,
        marker_line=91,
        keywords={"nullptr"},
    ) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "unsupported parameter; argument 3 must use"),
        ("double * value", "raw pointers are not supported"),
        ("double value = 1", "default arguments are not supported"),
        ("double ... values", "variadic arguments are not supported"),
        ("double values [ 2 ]", "array parameters are not supported"),
        ("[[ maybe_unused ]] double value", "attributes are not supported"),
        ("double ( value )", "function or grouped parameter types are not supported"),
        ("double", "unsupported parameter 'double'"),
        ("double * value = 1", "raw pointers are not supported"),
    ],
)
def test_parameter_syntax_retains_failure_precedence_and_source_lines(
    source: str, message: str
):
    tokens = _tokens("\n" + source) if source else []
    with pytest.raises(CppTypeSyntaxError, match=message) as raised:
        parameter_syntax(
            tokens,
            argument_index=3,
            marker_line=77,
            keywords={"nullptr"},
        )

    assert raised.value.line == (2 if tokens else 77)


def test_parameter_syntax_rejects_c23_nullptr_as_a_parameter_name():
    with pytest.raises(
        CppTypeSyntaxError,
        match="argument 1 name 'nullptr' is a C\\+\\+23 keyword",
    ) as raised:
        parameter_syntax(
            _tokens("\n\ndouble nullptr"),
            argument_index=1,
            marker_line=1,
            keywords={"nullptr"},
        )

    assert raised.value.line == 3


def test_parameter_list_syntax_preserves_order_and_argument_indices():
    assert parameter_list_syntax(
        [_tokens("double left"), _tokens("std :: string right")],
        marker_line=91,
        keywords={"nullptr"},
    ) == (
        CppParameterSyntax("double", "left"),
        CppParameterSyntax("std::string", "right"),
    )


def test_parameter_list_syntax_rejects_duplicate_name_at_later_source_line():
    with pytest.raises(CppTypeSyntaxError, match="duplicate parameter name 'value'") as raised:
        parameter_list_syntax(
            [_tokens("double value"), _tokens("\nstd :: int32_t value")],
            marker_line=91,
            keywords={"nullptr"},
        )

    assert raised.value.line == 2
    assert "argument 2" in raised.value.message


def test_parameter_list_syntax_keeps_declaration_error_before_duplicate_policy():
    with pytest.raises(CppTypeSyntaxError, match="raw pointers are not supported"):
        parameter_list_syntax(
            [_tokens("double value"), _tokens("double * value")],
            marker_line=91,
            keywords={"nullptr"},
        )


def test_parameter_list_syntax_accepts_empty_parameter_list():
    assert parameter_list_syntax(
        [], marker_line=91, keywords={"nullptr"}
    ) == ()
