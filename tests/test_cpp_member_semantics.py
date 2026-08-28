from __future__ import annotations

import pytest

from supernote_module_generator.cpp_lexer import _Token, _lex_source
from supernote_module_generator.cpp_member_semantics import (
    CppMemberDecisionError,
    constructor_suffix,
    is_copy_or_move_constructor,
    parse_member_qualifiers,
)
from supernote_module_generator.cpp_members import parameter_groups


def _tokens(source: str) -> list[_Token]:
    return list(_lex_source(source).tokens)


def _groups(source: str) -> list[list[_Token]]:
    tokens = _tokens(source)
    opening = next(index for index, token in enumerate(tokens) if token.value == "(")
    groups, _ = parameter_groups(tokens, opening)
    return groups


@pytest.mark.parametrize(
    ("source", "allow_const", "allow_default", "expected"),
    [
        ("", True, False, (False, False)),
        ("const", True, False, (True, False)),
        ("noexcept", True, False, (False, True)),
        ("const noexcept", True, False, (True, True)),
        ("noexcept const", True, False, (True, True)),
        ("= default", False, True, (False, False)),
        ("noexcept = default", False, True, (False, True)),
    ],
)
def test_member_qualifier_decisions_preserve_accepted_orderings(
    source: str,
    allow_const: bool,
    allow_default: bool,
    expected: tuple[bool, bool],
):
    assert parse_member_qualifiers(
        _tokens(source),
        allow_const=allow_const,
        allow_default=allow_default,
    ) == expected


@pytest.mark.parametrize(
    ("source", "allow_const", "allow_default", "message"),
    [
        ("const", False, False, "unsupported trailing object member token 'const'"),
        ("const const", True, False, "unsupported trailing object member token 'const'"),
        (
            "noexcept noexcept",
            True,
            False,
            "unsupported trailing object member token 'noexcept'",
        ),
        ("noexcept(true)", True, False, "only bare 'noexcept' is supported"),
        ("= default extra", False, True, "unsupported trailing object member token '='"),
    ],
)
def test_member_qualifier_decisions_retain_exact_first_failure(
    source: str,
    allow_const: bool,
    allow_default: bool,
    message: str,
):
    with pytest.raises(CppMemberDecisionError, match=message) as raised:
        parse_member_qualifiers(
            _tokens("\n" + source),
            allow_const=allow_const,
            allow_default=allow_default,
        )

    assert raised.value.line == 2


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", (False, False)),
        ("noexcept", (True, False)),
        ("= default", (False, False)),
        ("= delete", (False, True)),
        ("noexcept = default", (True, False)),
        ("noexcept = delete", (True, True)),
        (": value_(value)", (False, False)),
        ("noexcept : value_(value), ready_(true)", (True, False)),
    ],
)
def test_constructor_suffix_decisions_cover_canonical_forms(
    source: str, expected: tuple[bool, bool]
):
    assert constructor_suffix(_tokens(source)) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("noexcept(true)", "only bare noexcept is supported on a constructor"),
        ("const", "unsupported constructor suffix"),
        ("= other", "unsupported constructor suffix"),
        ("= default extra", "unsupported constructor suffix"),
        ("noexcept noexcept", "unsupported constructor suffix"),
        (":", "unsupported constructor suffix"),
    ],
)
def test_constructor_suffix_decisions_retain_source_line(
    source: str, message: str
):
    with pytest.raises(CppMemberDecisionError, match=message) as raised:
        constructor_suffix(_tokens("\n\n" + source))

    assert raised.value.line == 3


@pytest.mark.parametrize(
    "signature",
    [
        "Example(Example&)",
        "Example(Example& other)",
        "Example(const Example&)",
        "Example(const Example& other)",
        "Example(Example&&)",
        "Example(Example&& other)",
    ],
)
def test_copy_and_move_constructor_classification_accepts_canonical_forms(
    signature: str,
):
    assert is_copy_or_move_constructor(_groups(signature), "Example")


@pytest.mark.parametrize(
    "signature",
    [
        "Example()",
        "Example(Other&)",
        "Example(Example)",
        "Example(volatile Example&)",
        "Example(ns::Example&)",
        "Example(Example&, int)",
    ],
)
def test_copy_and_move_constructor_classification_rejects_other_forms(
    signature: str,
):
    assert not is_copy_or_move_constructor(_groups(signature), "Example")
