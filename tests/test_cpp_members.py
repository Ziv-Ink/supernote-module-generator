from __future__ import annotations

import pytest

from supernote_module_generator.cpp_lexer import _Token, _lex_source
from supernote_module_generator.cpp_members import (
    member_declarations,
    parameter_groups,
)


def _values(tokens: list[_Token]) -> list[str]:
    return [token.value for token in tokens]


def _body_tokens(source: str) -> list[_Token]:
    tokens = list(_lex_source(source).tokens)
    opening = next(index for index, token in enumerate(tokens) if token.value == "{")
    closing = next(
        index
        for index in range(opening + 1, len(tokens))
        if tokens[index].value == "}" and tokens[index].brace_depth == 1
    )
    return tokens[opening + 1 : closing]


def test_parameter_groups_split_only_at_the_active_parenthesis_depth():
    tokens = list(
        _lex_source(
            "function(first, nested(one, two), factory(three, inner(four, five)))"
        ).tokens
    )
    opening = next(index for index, token in enumerate(tokens) if token.value == "(")

    groups, closing = parameter_groups(tokens, opening)

    assert [_values(group) for group in groups] == [
        ["first"],
        ["nested", "(", "one", ",", "two", ")"],
        [
            "factory",
            "(",
            "three",
            ",",
            "inner",
            "(",
            "four",
            ",",
            "five",
            ")",
            ")",
        ],
    ]
    assert tokens[closing].value == ")"


def test_parameter_groups_preserve_empty_and_unclosed_policy():
    empty = list(_lex_source("function() suffix").tokens)
    opening = next(index for index, token in enumerate(empty) if token.value == "(")
    groups, closing = parameter_groups(empty, opening)
    assert groups == []
    assert empty[closing].value == ")"

    unclosed = list(_lex_source("function(first, nested(second)").tokens)
    opening = next(index for index, token in enumerate(unclosed) if token.value == "(")
    with pytest.raises(ValueError, match="missing closing parenthesis"):
        parameter_groups(unclosed, opening)


def test_member_segmentation_tracks_access_and_skips_nested_type_bodies():
    body = _body_tokens(
        "class Example {\n"
        "  int hidden;\n"
        "public:\n"
        "  double visible;\n"
        "protected:\n"
        "  void hook();\n"
        "public:\n"
        "  double calculate() { if (true) { return 1.0; } return 0.0; }\n"
        "  struct Nested { void ignored(); };\n"
        "  bool ready() const;\n"
        "};"
    )

    declarations = member_declarations(body, default_access="private")

    assert [(access, _values(tokens)) for access, tokens in declarations] == [
        ("private", ["int", "hidden"]),
        ("public", ["double", "visible"]),
        ("protected", ["void", "hook", "(", ")"]),
        ("public", ["double", "calculate", "(", ")"]),
        ("public", ["bool", "ready", "(", ")", "const"]),
    ]


def test_member_segmentation_ignores_delimiters_nested_in_parentheses_and_brackets():
    body = _body_tokens(
        "struct Example {\n"
        "  [[nodiscard]] double evaluate(int value = factory(1, 2));\n"
        "  int field[factory(3, 4)];\n"
        "  void inlineMethod() { auto lambda = [] { return 1; }; }\n"
        "  bool finalMember;\n"
        "};"
    )

    declarations = member_declarations(body, default_access="public")

    assert [(access, _values(tokens)) for access, tokens in declarations] == [
        (
            "public",
            [
                "[[",
                "nodiscard",
                "]]",
                "double",
                "evaluate",
                "(",
                "int",
                "value",
                "=",
                "factory",
                "(",
                "1",
                ",",
                "2",
                ")",
                ")",
            ],
        ),
        (
            "public",
            [
                "int",
                "field",
                "[",
                "factory",
                "(",
                "3",
                ",",
                "4",
                ")",
                "]",
            ],
        ),
        ("public", ["void", "inlineMethod", "(", ")"]),
        ("public", ["bool", "finalMember"]),
    ]


def test_member_segmentation_preserves_an_unterminated_tail():
    tokens = list(_lex_source("public: double unfinished").tokens)

    declarations = member_declarations(tokens, default_access="private")

    assert [(access, _values(items)) for access, items in declarations] == [
        ("public", ["double", "unfinished"])
    ]
