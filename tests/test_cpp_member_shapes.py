from __future__ import annotations

import pytest

from supernote_module_generator.cpp_lexer import _Token, _lex_source
from supernote_module_generator.cpp_member_shapes import (
    CppCallableHead,
    CppCallableKind,
    CppConstructorRoute,
    CppFieldShape,
    CppMemberShapeError,
    CppMethodShape,
    CppStoredMemberKind,
    callable_head,
    constructor_route,
    field_shape,
    method_shape,
    stored_member_kind,
)


def _tokens(source: str) -> list[_Token]:
    return list(_lex_source(source).tokens)


def _method(source: str) -> tuple[list[_Token], int]:
    tokens = _tokens(source)
    opening = next(index for index, token in enumerate(tokens) if token.value == "(")
    return tokens, opening


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Page",
            CppCallableHead(
                CppCallableKind.CONSTRUCTOR,
                constructor_prefix_supported=True,
            ),
        ),
        (
            "explicit Page",
            CppCallableHead(
                CppCallableKind.CONSTRUCTOR,
                constructor_prefix_supported=True,
                explicit=True,
            ),
        ),
        (
            "const Page",
            CppCallableHead(CppCallableKind.CONSTRUCTOR),
        ),
        ("~ Page", CppCallableHead(CppCallableKind.DESTRUCTOR)),
        ("double read", CppCallableHead(CppCallableKind.METHOD)),
        ("", CppCallableHead(CppCallableKind.METHOD)),
        (
            "virtual ~ Page",
            CppCallableHead(CppCallableKind.CONSTRUCTOR),
        ),
    ],
)
def test_callable_head_preserves_constructor_destructor_and_method_classification(
    source: str, expected: CppCallableHead
):
    assert callable_head(_tokens(source), "Page") == expected


@pytest.mark.parametrize(
    ("head_source", "parameters", "marked", "expected"),
    [
        ("Page", "double value", False, CppConstructorRoute.LOWER),
        ("explicit Page", "", True, CppConstructorRoute.LOWER),
        ("const Page", "", False, CppConstructorRoute.IGNORE),
        ("const Page", "", True, CppConstructorRoute.REJECT_PREFIX),
        ("Page", "const Page & other", False, CppConstructorRoute.IGNORE),
        (
            "Page",
            "Page & & other",
            True,
            CppConstructorRoute.REJECT_COPY_OR_MOVE,
        ),
    ],
)
def test_constructor_route_preserves_marked_and_unmarked_decisions(
    head_source: str,
    parameters: str,
    marked: bool,
    expected: CppConstructorRoute,
):
    head = callable_head(_tokens(head_source), "Page")
    groups = [_tokens(parameters)] if parameters else []
    assert constructor_route(
        head,
        groups,
        cpp_name="Page",
        marked=marked,
    ) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("double value", CppFieldShape("value", "double", True, False)),
        (
            "const std :: string name",
            CppFieldShape("name", "std::string", False, False),
        ),
        (
            "static std :: int64_t count",
            CppFieldShape("count", "std::int64_t", True, True),
        ),
        (
            "static const Widget & current",
            CppFieldShape("current", "Widget&", False, True),
        ),
    ],
)
def test_field_shape_returns_structural_type_and_ownership_flags(
    source: str, expected: CppFieldShape
):
    assert field_shape(_tokens(source)) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("double value = 1", "without initializer, pointer, attribute"),
        ("double first , second", "without initializer, pointer, attribute"),
        ("double * value", "without initializer, pointer, attribute"),
        ("[[ maybe_unused ]] double value", "without initializer, pointer, attribute"),
        ("double", "requires a canonical type and ordinary name"),
        ("double &", "requires a canonical type and ordinary name"),
        ("Widget & & value", "unsupported generated field type spelling"),
    ],
)
def test_field_shape_retains_failure_precedence_and_marker_relative_line(
    source: str, message: str
):
    with pytest.raises(CppMemberShapeError, match=message) as raised:
        field_shape(_tokens(source))

    assert raised.value.line is None


@pytest.mark.parametrize(
    ("source", "marked", "value_class", "expected"),
    [
        ("double value()", False, True, CppStoredMemberKind.CALLABLE),
        ("double value", True, False, CppStoredMemberKind.FIELD),
        ("double ordinary", False, False, CppStoredMemberKind.IGNORE),
        ("static double count", False, True, CppStoredMemberKind.IGNORE),
    ],
)
def test_stored_member_kind_routes_before_field_shape_lowering(
    source: str,
    marked: bool,
    value_class: bool,
    expected: CppStoredMemberKind,
):
    assert stored_member_kind(
        _tokens(source), marked=marked, value_class=value_class
    ) is expected


def test_stored_member_kind_requires_markers_for_value_storage():
    with pytest.raises(CppMemberShapeError) as raised:
        stored_member_kind(
            _tokens("\ndouble value"), marked=False, value_class=True
        )

    assert raised.value.line == 2
    assert raised.value.message == (
        "every non-static stored value member requires SupernotePluginExport"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "double read()",
            CppMethodShape("read", 1, "double", False),
        ),
        (
            "static std :: int32_t count()",
            CppMethodShape("count", 1, "std::int32_t", True),
        ),
        (
            "const Widget & current()",
            CppMethodShape("current", 1, "const Widget&", False),
        ),
    ],
)
def test_method_shape_returns_name_result_and_static_state(
    source: str, expected: CppMethodShape
):
    declaration, opening = _method(source)
    assert method_shape(
        declaration,
        opening,
        keywords={"nullptr"},
    ) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("operator double read()", "operators are deferred"),
        ("virtual double read()", "virtual methods are deferred"),
        ("virtual operator double read()", "operators are deferred"),
        ("read()", "must use one canonical V4 result type"),
        ("double * read()", "must use one canonical V4 result type"),
        ("double nullptr()", "method name 'nullptr' is a C\\+\\+23 keyword"),
    ],
)
def test_method_shape_retains_forbidden_and_keyword_failure_precedence(
    source: str, message: str
):
    declaration, opening = _method("\n" + source)
    with pytest.raises(CppMemberShapeError, match=message) as raised:
        method_shape(declaration, opening, keywords={"nullptr"})

    assert raised.value.line == 2
