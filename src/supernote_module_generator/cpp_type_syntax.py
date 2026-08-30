"""Pure C++ type-token and parameter-declaration decisions."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import AbstractSet

from .cpp_lexer import _Token


class CppTypeSyntaxError(ValueError):
    """A source-line type-syntax failure awaiting binding context."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


@dataclass(frozen=True)
class CppParameterSyntax:
    """One validated named C++ parameter before semantic lowering."""

    cpp_type: str
    name: str


def cpp_type_spelling(tokens: list[_Token]) -> str | None:
    """Normalize one structurally valid C++ type-token spelling."""

    if not tokens:
        return None
    allowed_punctuation = {"::", "<", ">", "&"}
    if any(
        token.kind != "identifier" and token.value not in allowed_punctuation
        for token in tokens
    ):
        return None
    if sum(token.value == "<" for token in tokens) != sum(
        token.value == ">" for token in tokens
    ):
        return None
    if any(
        token.value == "&" and index != len(tokens) - 1
        for index, token in enumerate(tokens)
    ):
        return None
    value = " ".join(token.value for token in tokens)
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s*<\s*", "<", value)
    value = re.sub(r"\s*>\s*", ">", value)
    value = re.sub(r"\s*&\s*", "&", value)
    return value


def parameter_syntax(
    tokens: list[_Token],
    *,
    argument_index: int,
    marker_line: int,
    keywords: AbstractSet[str],
) -> CppParameterSyntax:
    """Validate one named canonical generated parameter declaration."""

    line = tokens[0].line if tokens else marker_line
    expected = (
        f"argument {argument_index} must use one named canonical generated value "
        "type, for example 'std::int32_t value'"
    )
    if not tokens:
        raise CppTypeSyntaxError(line, f"unsupported parameter; {expected}")

    values = [token.value for token in tokens]
    forbidden = {
        "*": "raw pointers",
        "=": "default arguments",
        "...": "variadic arguments",
        "[": "array parameters",
        "[[": "attributes",
        "(": "function or grouped parameter types",
    }
    for value, description in forbidden.items():
        if value in values:
            token = tokens[values.index(value)]
            raise CppTypeSyntaxError(
                token.line,
                f"unsupported parameter {_tokens_text(tokens)!r}: "
                f"{description} are not supported; {expected}",
            )

    name = tokens[-1].value if tokens[-1].kind == "identifier" else None
    type_tokens = tokens[:-1]
    cpp_type = cpp_type_spelling(type_tokens) if name is not None else None
    if cpp_type is None or name is None:
        raise CppTypeSyntaxError(
            line,
            f"unsupported parameter {_tokens_text(tokens)!r}; {expected}",
        )
    if name in keywords:
        raise CppTypeSyntaxError(
            tokens[-1].line,
            f"argument {argument_index} name {name!r} is a C++23 keyword; "
            "rename the C++ parameter",
        )
    return CppParameterSyntax(cpp_type, name)


def parameter_list_syntax(
    groups: list[list[_Token]],
    *,
    marker_line: int,
    keywords: AbstractSet[str],
) -> tuple[CppParameterSyntax, ...]:
    """Validate a complete parameter list and reject duplicate names."""

    parameters: list[CppParameterSyntax] = []
    names: set[str] = set()
    for argument_index, group in enumerate(groups, start=1):
        parameter = parameter_syntax(
            group,
            argument_index=argument_index,
            marker_line=marker_line,
            keywords=keywords,
        )
        if parameter.name in names:
            raise CppTypeSyntaxError(
                group[-1].line if group else marker_line,
                f"duplicate parameter name {parameter.name!r} at argument "
                f"{argument_index}; give every argument a unique name",
            )
        names.add(parameter.name)
        parameters.append(parameter)
    return tuple(parameters)


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)
