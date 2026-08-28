"""Pure C++ member and constructor suffix decisions."""
from __future__ import annotations

from .cpp_lexer import _Token


class CppMemberDecisionError(ValueError):
    """A source-line decision failure awaiting module/export context."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


def parse_member_qualifiers(
    tokens: list[_Token],
    *,
    allow_const: bool,
    allow_default: bool,
) -> tuple[bool, bool]:
    """Return const/noexcept flags after validating one member suffix."""

    is_const = False
    is_noexcept = False
    cursor = 0
    while cursor < len(tokens):
        value = tokens[cursor].value
        if value == "const" and allow_const and not is_const:
            is_const = True
            cursor += 1
            continue
        if value == "noexcept" and not is_noexcept:
            is_noexcept = True
            cursor += 1
            if cursor < len(tokens) and tokens[cursor].value == "(":
                raise CppMemberDecisionError(
                    tokens[cursor].line,
                    "only bare 'noexcept' is supported on exported objects",
                )
            continue
        if (
            allow_default
            and cursor + 1 == len(tokens) - 1
            and value == "="
            and tokens[cursor + 1].value == "default"
        ):
            cursor += 2
            continue
        raise CppMemberDecisionError(
            tokens[cursor].line,
            f"unsupported trailing object member token {value!r}; only "
            "const and bare noexcept are supported",
        )
    return is_const, is_noexcept


def constructor_suffix(tokens: list[_Token]) -> tuple[bool, bool]:
    """Return noexcept/deleted state for one canonical constructor suffix."""

    is_noexcept = False
    cursor = 0
    if cursor < len(tokens) and tokens[cursor].value == "noexcept":
        is_noexcept = True
        cursor += 1
        if cursor < len(tokens) and tokens[cursor].value == "(":
            raise CppMemberDecisionError(
                tokens[cursor].line,
                "only bare noexcept is supported on a constructor",
            )
    deleted = False
    if cursor < len(tokens):
        if _is_default_or_deleted_tail(tokens, cursor):
            deleted = tokens[cursor + 1].value == "delete"
            cursor += 2
        elif tokens[cursor].value == ":" and cursor + 1 < len(tokens):
            # The C++ compiler owns initializer-expression semantics.  The
            # binding parser only needs to recognize the complete initializer
            # tail so a header-only constructor reaches the same semantic
            # model as an out-of-line constructor declaration.
            cursor = len(tokens)
        else:
            raise CppMemberDecisionError(
                tokens[cursor].line,
                "unsupported constructor suffix; only bare noexcept, "
                "a member initializer list, = default, or = delete is supported",
            )
    return is_noexcept, deleted


def _is_default_or_deleted_tail(tokens: list[_Token], cursor: int) -> bool:
    return (
        cursor + 2 == len(tokens)
        and tokens[cursor].value == "="
        and tokens[cursor + 1].value in {"default", "delete"}
    )


def is_copy_or_move_constructor(
    groups: list[list[_Token]], cpp_name: str
) -> bool:
    """Classify the canonical unqualified copy/move constructor spellings."""

    if len(groups) != 1:
        return False
    values = [token.value for token in groups[0]]
    if values and groups[0][-1].kind == "identifier":
        values = values[:-1]
    return values in (
        [cpp_name, "&"],
        ["const", cpp_name, "&"],
        [cpp_name, "&", "&"],
    )
