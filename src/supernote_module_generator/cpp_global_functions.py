"""Pure structural decisions for untagged global C++ functions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cpp_lexer import _Token


@dataclass(frozen=True)
class CppGlobalFunctionShape:
    kind: Literal["definition", "declaration"]
    terminator_index: int


def _matching_parenthesis(tokens: list[_Token], opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index].value == "(":
            depth += 1
        elif tokens[index].value == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _function_terminator(
    tokens: list[_Token],
    name_index: int,
    namespace_depth: int,
) -> int | None:
    if name_index + 1 >= len(tokens) or tokens[name_index + 1].value != "(":
        return None
    closing = _matching_parenthesis(tokens, name_index + 1)
    if closing is None:
        return None
    cursor = closing + 1
    if cursor < len(tokens) and tokens[cursor].value == "noexcept":
        cursor += 1
        if cursor < len(tokens) and tokens[cursor].value == "(":
            noexcept_close = _matching_parenthesis(tokens, cursor)
            if noexcept_close is None:
                return None
            cursor = noexcept_close + 1
    if cursor < len(tokens) and tokens[cursor].value == "->":
        cursor += 1
        while cursor < len(tokens) and tokens[cursor].value not in {"{", ";"}:
            cursor += 1
    if (
        cursor < len(tokens)
        and tokens[cursor].brace_depth == namespace_depth
        and tokens[cursor].value in {"{", ";"}
    ):
        return cursor
    return None


def global_function_shape(
    tokens: list[_Token],
    *,
    name_index: int,
    namespace_depth: int,
) -> CppGlobalFunctionShape | None:
    """Return an ordinary namespace-level function declaration or definition."""

    terminator = _function_terminator(tokens, name_index, namespace_depth)
    if terminator is None:
        return None

    prefix: list[_Token] = []
    for previous in reversed(tokens[:name_index]):
        if previous.value in {";", "{", "}"}:
            break
        prefix.append(previous)
    prefix.reverse()
    if not prefix or any(item.value == "=" for item in prefix):
        return None
    if prefix[-1].value in {".", "->", "::", "(", "[", ","}:
        return None

    kind: Literal["definition", "declaration"] = (
        "definition" if tokens[terminator].value == "{" else "declaration"
    )
    return CppGlobalFunctionShape(kind, terminator)
