"""Pure structural decisions for generated C++ free functions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet

from .cpp_lexer import _Directive, _Token
from .cpp_type_syntax import cpp_type_spelling


class CppFunctionSyntaxError(ValueError):
    """A free-function syntax failure awaiting source and module context."""

    def __init__(self, line: int, export_name: str, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.export_name = export_name
        self.message = message


@dataclass(frozen=True)
class CppFunctionHead:
    cpp_name: str
    return_type_spelling: str
    parameter_opening_index: int
    definition_offset: int


@dataclass(frozen=True)
class CppFunctionTail:
    noexcept: bool
    body_opening_index: int


@dataclass(frozen=True)
class CppFunctionParameters:
    groups: tuple[tuple[_Token, ...], ...]
    closing_index: int


@dataclass(frozen=True)
class CppFunctionBoundary:
    following: tuple[_Token, ...]


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)


def function_declaration_boundary(
    text: str,
    tokens: tuple[_Token, ...],
    directives: tuple[_Directive, ...],
    *,
    marker_start: int,
    marker_line: int,
    marker_end: int,
    next_marker_start: int | None,
    pending_export: str,
) -> CppFunctionBoundary:
    """Validate the boundary from a marker stack to its declaration tokens."""

    active_tokens = [token for token in tokens if token.conditional_depth == 0]
    preceding = [token for token in active_tokens if token.end <= marker_start]
    prefix: list[_Token] = []
    for token in reversed(preceding):
        if token.value in {";", "{", "}"}:
            break
        prefix.append(token)
    prefix.reverse()
    if prefix:
        raise CppFunctionSyntaxError(
            prefix[0].line,
            pending_export,
            "unsupported declaration prefix before the marker "
            f"{_tokens_text(prefix)!r}; place the Supernote marker stack "
            "immediately before an unmodified function return type and remove "
            "static, template, extern \"C\", attributes, or macros",
        )

    following = [token for token in active_tokens if token.start >= marker_end]
    if not following or (
        next_marker_start is not None
        and following[0].start >= next_marker_start
    ):
        raise CppFunctionSyntaxError(
            marker_line,
            pending_export,
            "the Supernote marker stack must be followed by a supported "
            "top-level function definition; expected "
            "'std::int32_t name(std::int32_t value) {'",
        )
    first = following[0]
    intervening_directive = next(
        (
            directive
            for directive in directives
            if marker_end <= directive.start < first.start
        ),
        None,
    )
    if intervening_directive is not None:
        raise CppFunctionSyntaxError(
            intervening_directive.line,
            pending_export,
            "a preprocessor directive cannot occur between a Supernote marker "
            "stack and its function definition",
        )
    if text[marker_end:first.start].strip():
        raise CppFunctionSyntaxError(
            marker_line,
            pending_export,
            "only whitespace may appear between the final Supernote marker "
            "and the function return type",
        )
    return CppFunctionBoundary(tuple(following))


def _return_type_boundary(following: list[_Token]) -> tuple[int, str | None]:
    opening = next(
        (index for index, token in enumerate(following) if token.value == "("),
        None,
    )
    if opening is None or opening < 2:
        return 0, None
    consumed = opening - 1
    return consumed, cpp_type_spelling(following[:consumed])


def _validate_owned_return(
    tokens: list[_Token],
    *,
    line: int,
    export_name: str,
) -> None:
    if any(token.value == "*" for token in tokens):
        raise CppFunctionSyntaxError(
            line,
            export_name,
            "raw pointers are not supported as marked C++ results; "
            "return one canonical owned V4 type",
        )
    if any(token.value in {"&", "&&"} for token in tokens):
        raise CppFunctionSyntaxError(
            line,
            export_name,
            "references are not supported as marked C++ results; return "
            "one canonical owned V4 type",
        )


def function_head(
    following: list[_Token],
    *,
    pending_export: str,
    forbidden_prefixes: AbstractSet[str],
    keywords: AbstractSet[str],
) -> CppFunctionHead:
    """Validate the result/name boundary through the parameter-list opening."""

    first = following[0]
    if first.value in forbidden_prefixes or first.value == "[[":
        raise CppFunctionSyntaxError(
            first.line,
            pending_export,
            "not a supported top-level function definition: declaration "
            f"modifier {first.value!r} is forbidden; routable functions must "
            "have ordinary external C++ linkage with no modifiers",
        )

    consumed, return_type = _return_type_boundary(following)
    return_tokens = following[:consumed]
    _validate_owned_return(
        return_tokens,
        line=first.line,
        export_name=pending_export,
    )
    if return_type is None:
        description = (
            "unsupported declaration prefix or macro"
            if first.kind == "identifier"
            else "unsupported return declaration"
        )
        raise CppFunctionSyntaxError(
            first.line,
            pending_export,
            "not a supported top-level function definition: "
            f"{description} {first.value!r}; expected one canonical V4 return "
            "type followed by a function name",
        )

    if consumed >= len(following) or following[consumed].kind != "identifier":
        line = following[min(consumed, len(following) - 1)].line
        raise CppFunctionSyntaxError(
            line,
            pending_export,
            "not a supported top-level function definition: expected a C++ "
            "function name after the return type",
        )
    function_token = following[consumed]
    cpp_name = function_token.value
    if cpp_name in keywords:
        raise CppFunctionSyntaxError(
            function_token.line,
            cpp_name,
            f"C++ function name {cpp_name!r} is a C++23 keyword; rename the "
            "C++ function",
        )

    opening = consumed + 1
    if opening >= len(following) or following[opening].value != "(":
        line = following[min(opening, len(following) - 1)].line
        raise CppFunctionSyntaxError(
            line,
            cpp_name,
            "not a supported top-level function definition: unsupported "
            "modifier, attribute, or macro between the function name and '('",
        )
    return CppFunctionHead(
        cpp_name=cpp_name,
        return_type_spelling=return_type,
        parameter_opening_index=opening,
        definition_offset=function_token.start,
    )


def function_parameters(
    following: list[_Token],
    *,
    opening_index: int,
    marker_line: int,
    export_name: str,
) -> CppFunctionParameters:
    """Split a free-function parameter list at its active parenthesis depth."""

    groups: list[tuple[_Token, ...]] = []
    group_start = opening_index + 1
    nesting = 0
    cursor = group_start
    while cursor < len(following):
        value = following[cursor].value
        if value == "(":
            nesting += 1
        elif value == ")":
            if nesting == 0:
                groups.append(tuple(following[group_start:cursor]))
                if len(groups) == 1 and not groups[0]:
                    groups = []
                return CppFunctionParameters(tuple(groups), cursor)
            nesting -= 1
        elif value == "," and nesting == 0:
            groups.append(tuple(following[group_start:cursor]))
            group_start = cursor + 1
        elif value in {"{", ";"} and nesting == 0:
            break
        cursor += 1
    raise CppFunctionSyntaxError(
        marker_line,
        export_name,
        "not a supported top-level function definition: missing ')' in the "
        "tagged signature",
    )


def function_tail_start(
    following: list[_Token],
    *,
    close_index: int,
    marker_line: int,
    export_name: str,
) -> CppFunctionTail:
    """Validate the suffix after a closed parameter list through its body token."""

    cursor = close_index + 1
    is_noexcept = False
    if cursor < len(following) and following[cursor].value == "noexcept":
        is_noexcept = True
        cursor += 1
        if cursor < len(following) and following[cursor].value == "(":
            raise CppFunctionSyntaxError(
                following[cursor].line,
                export_name,
                "only bare 'noexcept' is supported; remove the noexcept "
                "expression",
            )
    if cursor >= len(following):
        raise CppFunctionSyntaxError(
            marker_line,
            export_name,
            "tagged declaration has no function body; add '{ ... }'",
        )
    return CppFunctionTail(is_noexcept, cursor)


def validate_function_body_opening(opening: _Token, *, export_name: str) -> None:
    """Validate the body token after directive inspection by the frontend."""

    if opening.value == ";":
        raise CppFunctionSyntaxError(
            opening.line,
            export_name,
            "tagged declarations are not exported; provide the complete "
            "function definition with a '{ ... }' body",
        )
    if opening.value != "{":
        raise CppFunctionSyntaxError(
            opening.line,
            export_name,
            "unsupported tokens after the parameter list; only bare noexcept "
            "followed by the function body is allowed (no attributes, macros, "
            "qualifiers, or trailing return types)",
        )
