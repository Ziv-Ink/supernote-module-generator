"""Segment C++ parameter lists and class-scope member declarations."""
from __future__ import annotations

from dataclasses import dataclass, field

from .cpp_lexer import _Token


MemberDeclaration = tuple[str, list[_Token]]


def parameter_groups(
    tokens: list[_Token], opening: int
) -> tuple[list[list[_Token]], int]:
    """Split a parenthesized parameter list at its top-level commas."""

    groups: list[list[_Token]] = []
    group_start = opening + 1
    depth = 0
    cursor = opening + 1
    while cursor < len(tokens):
        value = tokens[cursor].value
        if value == "(":
            depth += 1
        elif value == ")":
            if depth == 0:
                groups.append(tokens[group_start:cursor])
                if len(groups) == 1 and not groups[0]:
                    groups = []
                return groups, cursor
            depth -= 1
        elif value == "," and depth == 0:
            groups.append(tokens[group_start:cursor])
            group_start = cursor + 1
        cursor += 1
    raise ValueError("missing closing parenthesis")


def member_declarations(
    body: list[_Token], *, default_access: str
) -> list[MemberDeclaration]:
    """Return class-scope declarations without inspecting nested bodies."""

    return _MemberScanner(body, default_access).scan()


@dataclass
class _MemberScanner:
    body: list[_Token]
    access: str
    cursor: int = 0
    declarations: list[MemberDeclaration] = field(default_factory=list)

    def scan(self) -> list[MemberDeclaration]:
        while self.cursor < len(self.body):
            if self._consume_access_label():
                continue
            start = self.cursor
            declaration = self._consume_declaration(start)
            if declaration is not None:
                self.declarations.append((self.access, declaration))
        return self.declarations

    def _consume_access_label(self) -> bool:
        if self.cursor + 1 >= len(self.body):
            return False
        access = self.body[self.cursor].value
        if access not in {"public", "private", "protected"}:
            return False
        if self.body[self.cursor + 1].value != ":":
            return False
        self.access = access
        self.cursor += 2
        return True

    def _consume_declaration(self, start: int) -> list[_Token] | None:
        paren_depth = 0
        bracket_depth = 0
        while self.cursor < len(self.body):
            value = self.body[self.cursor].value
            paren_depth, bracket_depth = _nested_delimiter_depths(
                value, paren_depth, bracket_depth
            )
            if value == ";" and paren_depth == 0 and bracket_depth == 0:
                return self._finish_semicolon_declaration(start)
            if value == "{" and paren_depth == 0 and bracket_depth == 0:
                return self._finish_body_declaration(start)
            self.cursor += 1
        tail = self.body[start:]
        return tail if tail else None

    def _finish_semicolon_declaration(self, start: int) -> list[_Token]:
        declaration = self.body[start : self.cursor]
        self.cursor += 1
        return declaration

    def _finish_body_declaration(self, start: int) -> list[_Token] | None:
        signature = self.body[start : self.cursor]
        self._skip_braced_body()
        if self.cursor < len(self.body) and self.body[self.cursor].value == ";":
            self.cursor += 1
        if any(token.value == "(" for token in signature):
            return signature
        return None

    def _skip_braced_body(self) -> None:
        depth = 1
        self.cursor += 1
        while self.cursor < len(self.body) and depth:
            value = self.body[self.cursor].value
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
            self.cursor += 1


def _nested_delimiter_depths(
    value: str, paren_depth: int, bracket_depth: int
) -> tuple[int, int]:
    if value == "(":
        paren_depth += 1
    elif value == ")":
        paren_depth = max(0, paren_depth - 1)
    elif value in {"[", "[["}:
        bracket_depth += 1
    elif value in {"]", "]]"}:
        bracket_depth = max(0, bracket_depth - 1)
    return paren_depth, bracket_depth
