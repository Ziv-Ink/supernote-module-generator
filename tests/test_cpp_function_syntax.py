from __future__ import annotations

import pytest

from supernote_module_generator.cpp_function_syntax import (
    CppFunctionBoundary,
    CppFunctionHead,
    CppFunctionParameters,
    CppFunctionSyntaxError,
    CppFunctionTail,
    function_declaration_boundary,
    function_head,
    function_parameters,
    function_tail_start,
    validate_function_body_opening,
)
from supernote_module_generator.cpp_lexer import _Token, _lex_source


FORBIDDEN_PREFIXES = {"static", "template", "extern"}
KEYWORDS = {"nullptr"}


def _tokens(source: str) -> list[_Token]:
    return list(_lex_source(source).tokens)


def _head(source: str) -> CppFunctionHead:
    return function_head(
        _tokens(source),
        pending_export="<pending>",
        forbidden_prefixes=FORBIDDEN_PREFIXES,
        keywords=KEYWORDS,
    )


def _boundary(source: str, *, next_marker_start: int | None = None):
    lexed = _lex_source(source)
    marker = lexed.comments[0]
    return function_declaration_boundary(
        source,
        lexed.tokens,
        lexed.directives,
        marker_start=marker.start,
        marker_line=marker.line,
        marker_end=marker.end,
        next_marker_start=next_marker_start,
        pending_export="<pending>",
    )


def test_function_declaration_boundary_returns_active_following_tokens():
    result = _boundary(
        "double previous;\n"
        "// @SupernotePluginExport\n"
        "double add(double value) { return value; }\n"
    )

    assert isinstance(result, CppFunctionBoundary)
    assert tuple(token.value for token in result.following[:4]) == (
        "double",
        "add",
        "(",
        "double",
    )


@pytest.mark.parametrize(
    ("source", "line", "message"),
    [
        (
            "static\n// @SupernotePluginExport\n",
            1,
            "unsupported declaration prefix before the marker 'static'",
        ),
        (
            "// @SupernotePluginExport\n",
            1,
            "must be followed by a supported top-level function definition",
        ),
        (
            "// @SupernotePluginExport\n#define VALUE double\ndouble add() {}\n",
            2,
            "preprocessor directive cannot occur between",
        ),
        (
            "// @SupernotePluginExport\n/* gap */ double add() {}\n",
            1,
            "only whitespace may appear between",
        ),
    ],
)
def test_function_declaration_boundary_retains_failure_order_and_context(
    source: str, line: int, message: str
):
    with pytest.raises(CppFunctionSyntaxError, match=message) as raised:
        _boundary(source)

    assert raised.value.line == line
    assert raised.value.export_name == "<pending>"


def test_function_declaration_boundary_stops_before_the_next_marker():
    source = (
        "// @SupernotePluginExport\n"
        "// @SupernotePluginExport\n"
        "double add() {}\n"
    )
    lexed = _lex_source(source)
    with pytest.raises(CppFunctionSyntaxError, match="must be followed"):
        function_declaration_boundary(
            source,
            lexed.tokens,
            lexed.directives,
            marker_start=lexed.comments[0].start,
            marker_line=lexed.comments[0].line,
            marker_end=lexed.comments[0].end,
            next_marker_start=lexed.comments[1].start,
            pending_export="<pending>",
        )


@pytest.mark.parametrize(
    ("source", "name", "result", "opening"),
    [
        ("double add(", "add", "double", 2),
        ("std :: int32_t pageCount(", "pageCount", "std::int32_t", 4),
        ("std :: string name(", "name", "std::string", 4),
    ],
)
def test_function_head_returns_canonical_result_name_and_parameter_boundary(
    source: str,
    name: str,
    result: str,
    opening: int,
):
    assert _head(source) == CppFunctionHead(
        cpp_name=name,
        return_type_spelling=result,
        parameter_opening_index=opening,
        definition_offset=source.index(name),
    )


@pytest.mark.parametrize(
    ("source", "line", "export_name", "message"),
    [
        ("\nstatic double add(", 2, "<pending>", "modifier 'static' is forbidden"),
        ("\ndouble * add(", 2, "<pending>", "raw pointers are not supported"),
        ("\ndouble & add(", 2, "<pending>", "references are not supported"),
        ("\ndouble(", 2, "<pending>", "unsupported declaration prefix or macro"),
        ("\n+ add(", 2, "<pending>", "unsupported return declaration"),
        ("\ndouble :: (", 2, "<pending>", r"expected a C\+\+ function name"),
        ("\ndouble nullptr(", 2, "nullptr", r"is a C\+\+23 keyword"),
    ],
)
def test_function_head_retains_failure_precedence_and_export_context(
    source: str,
    line: int,
    export_name: str,
    message: str,
):
    with pytest.raises(CppFunctionSyntaxError, match=message) as raised:
        _head(source)

    assert raised.value.line == line
    assert raised.value.export_name == export_name


@pytest.mark.parametrize(
    ("source", "opening", "groups", "closing"),
    [
        ("double add() {", 2, (), 3),
        (
            "double add(double left, double right) {",
            2,
            (("double", "left"), ("double", "right")),
            8,
        ),
        (
            "double add(void (* callback)(double, double), double value) {",
            2,
            (
                (
                    "void",
                    "(",
                    "*",
                    "callback",
                    ")",
                    "(",
                    "double",
                    ",",
                    "double",
                    ")",
                ),
                ("double", "value"),
            ),
            16,
        ),
        ("double add(, double value) {", 2, ((), ("double", "value")), 6),
        ("double add(double value,) {", 2, (("double", "value"), ()), 6),
    ],
)
def test_function_parameters_split_only_at_the_active_parenthesis_depth(
    source: str,
    opening: int,
    groups: tuple[tuple[str, ...], ...],
    closing: int,
):
    result = function_parameters(
        _tokens(source),
        opening_index=opening,
        marker_line=41,
        export_name="add",
    )

    assert isinstance(result, CppFunctionParameters)
    assert tuple(
        tuple(token.value for token in group) for group in result.groups
    ) == groups
    assert result.closing_index == closing


@pytest.mark.parametrize(
    "source",
    [
        "double add(double value {",
        "double add(double value ;",
        "double add(double value",
    ],
)
def test_function_parameters_missing_close_uses_marker_context(source: str):
    with pytest.raises(CppFunctionSyntaxError, match="missing '\\)'") as raised:
        function_parameters(
            _tokens(source),
            opening_index=2,
            marker_line=41,
            export_name="add",
        )

    assert raised.value.line == 41
    assert raised.value.export_name == "add"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("double add() {", CppFunctionTail(False, 4)),
        ("double add() noexcept {", CppFunctionTail(True, 5)),
        ("double add() ;", CppFunctionTail(False, 4)),
        ("double add() const", CppFunctionTail(False, 4)),
    ],
)
def test_function_tail_start_preserves_bare_noexcept_and_body_boundary(
    source: str,
    expected: CppFunctionTail,
):
    assert function_tail_start(
        _tokens(source),
        close_index=3,
        marker_line=41,
        export_name="add",
    ) == expected


def test_function_tail_rejects_noexcept_expression_before_missing_body_policy():
    with pytest.raises(CppFunctionSyntaxError, match="only bare 'noexcept'") as raised:
        function_tail_start(
            _tokens("\ndouble add() noexcept(true)"),
            close_index=3,
            marker_line=41,
            export_name="add",
        )

    assert raised.value.line == 2
    assert raised.value.export_name == "add"


def test_function_tail_missing_body_uses_marker_line():
    with pytest.raises(CppFunctionSyntaxError, match="has no function body") as raised:
        function_tail_start(
            _tokens("double add()"),
            close_index=3,
            marker_line=41,
            export_name="add",
        )

    assert raised.value.line == 41


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (";", "tagged declarations are not exported"),
        ("const", "unsupported tokens after the parameter list"),
        ("->", "unsupported tokens after the parameter list"),
    ],
)
def test_function_body_opening_retains_declaration_and_trailing_token_policy(
    token: str,
    message: str,
):
    opening = _tokens("\n" + token)[0]
    with pytest.raises(CppFunctionSyntaxError, match=message) as raised:
        validate_function_body_opening(opening, export_name="add")

    assert raised.value.line == 2
    assert raised.value.export_name == "add"


def test_function_body_opening_accepts_definition_brace():
    validate_function_body_opening(_tokens("{")[0], export_name="add")
