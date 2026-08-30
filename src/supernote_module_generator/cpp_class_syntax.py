"""Pure structural decisions for generated C++ class definitions."""
from __future__ import annotations

from dataclasses import dataclass

from .cpp_lexer import _Token


class CppClassSyntaxError(ValueError):
    """A class-definition failure awaiting source and module context."""

    def __init__(self, line: int, export_name: str | None, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.export_name = export_name
        self.message = message


@dataclass(frozen=True)
class CppClassExtent:
    declaration_kind: str
    cpp_name: str
    opening_index: int
    closing_index: int


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)


def _class_identity(
    following: list[_Token],
    *,
    diagnostic_line: int,
) -> tuple[_Token, str]:
    if not following:
        raise CppClassSyntaxError(
            diagnostic_line,
            None,
            "a class marker stack must be followed by a complete class or "
            "struct definition",
        )
    first = following[0]
    if first.value not in {"class", "struct"}:
        raise CppClassSyntaxError(
            first.line,
            None,
            "a class marker stack must be followed by a class or struct "
            "definition",
        )
    if len(following) < 3 or following[1].kind != "identifier":
        raise CppClassSyntaxError(
            first.line,
            None,
            "a marked class or struct must have an ordinary identifier name",
        )
    return first, following[1].value


def _class_body_open(following: list[_Token], first: _Token, cpp_name: str) -> int:
    opening = next(
        (
            index
            for index in range(2, len(following))
            if following[index].value in {"{", ";"}
        ),
        None,
    )
    if opening is None or following[opening].value != "{":
        raise CppClassSyntaxError(
            first.line,
            cpp_name,
            "a marked class requires a complete definition, not a declaration",
        )

    before_body = following[2:opening]
    if any(token.value == ":" for token in before_body):
        raise CppClassSyntaxError(
            before_body[0].line,
            cpp_name,
            "inheritance is not supported for generated classes",
        )
    if before_body:
        raise CppClassSyntaxError(
            before_body[0].line,
            cpp_name,
            "unsupported tokens before marked class body "
            f"{_tokens_text(before_body)!r}",
        )
    return opening


def _class_body_close(
    following: list[_Token],
    first: _Token,
    cpp_name: str,
    opening: int,
) -> int:

    depth = 1
    closing: int | None = None
    cursor = opening + 1
    while cursor < len(following):
        if following[cursor].value == "{":
            depth += 1
        elif following[cursor].value == "}":
            depth -= 1
            if depth == 0:
                closing = cursor
                break
        cursor += 1
    if closing is None:
        raise CppClassSyntaxError(
            first.line,
            cpp_name,
            "marked class definition is missing its closing '}'",
        )
    return closing


def class_definition_extent(
    following: list[_Token],
    *,
    diagnostic_line: int,
) -> CppClassExtent:
    """Validate one complete class/struct envelope and return its extent."""

    first, cpp_name = _class_identity(
        following,
        diagnostic_line=diagnostic_line,
    )
    opening = _class_body_open(following, first, cpp_name)
    closing = _class_body_close(following, first, cpp_name, opening)
    if closing + 1 >= len(following) or following[closing + 1].value != ";":
        raise CppClassSyntaxError(
            following[closing].line,
            cpp_name,
            "marked class definition must end with '};'",
        )
    return CppClassExtent(first.value, cpp_name, opening, closing)
