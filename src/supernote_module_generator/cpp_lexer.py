"""Minimal deterministic C/C++ lexical source model for declaration discovery."""
from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    line: int
    kind: str
    conditional_depth: int
    brace_depth: int


@dataclass(frozen=True)
class _LineComment:
    text: str
    start: int
    end: int
    line: int
    line_only: bool
    conditional_depth: int
    brace_depth: int


@dataclass(frozen=True)
class _Directive:
    start: int
    end: int
    line: int
    name: str


@dataclass(frozen=True)
class _LexedSource:
    tokens: tuple[_Token, ...]
    comments: tuple[_LineComment, ...]
    directives: tuple[_Directive, ...]


def _consume_newlines(
    text: str,
    start: int,
    end: int,
    line: int,
    line_start: int,
) -> tuple[int, int]:
    for index in range(start, end):
        if text[index] == "\n":
            line += 1
            line_start = index + 1
    return line, line_start


def _raw_string_end(text: str, start: int) -> int | None:
    prefixes = ("u8R\"", "uR\"", "UR\"", "LR\"", "R\"")
    prefix = next(
        (candidate for candidate in prefixes if text.startswith(candidate, start)),
        None,
    )
    if prefix is None:
        return None
    delimiter_start = start + len(prefix)
    opening = text.find("(", delimiter_start, delimiter_start + 17)
    if opening < 0:
        return None
    delimiter = text[delimiter_start:opening]
    if any(char.isspace() or char in {"\\", "(", ")"} for char in delimiter):
        return None
    terminator = ")" + delimiter + '"'
    closing = text.find(terminator, opening + 1)
    return len(text) if closing < 0 else closing + len(terminator)


@dataclass
class _Lexer:
    text: str
    tokens: list[_Token] = field(default_factory=list)
    comments: list[_LineComment] = field(default_factory=list)
    directives: list[_Directive] = field(default_factory=list)
    index: int = 0
    line: int = 1
    line_start: int = 0
    conditional_depth: int = 0
    brace_depth: int = 0

    def lex(self) -> _LexedSource:
        while self.index < len(self.text):
            self._consume_token_or_trivia()
        return _LexedSource(
            tuple(self.tokens), tuple(self.comments), tuple(self.directives)
        )

    def _consume_token_or_trivia(self) -> None:
        char = self.text[self.index]
        next_char = (
            self.text[self.index + 1] if self.index + 1 < len(self.text) else ""
        )
        if char == "\n":
            self.line += 1
            self.line_start = self.index + 1
            self.index += 1
            return
        if char.isspace():
            self.index += 1
            return
        if char == "#" and not self.text[self.line_start : self.index].strip():
            self._consume_directive()
            return
        if char == "/" and next_char == "/":
            self._consume_line_comment()
            return
        if char == "/" and next_char == "*":
            self._consume_block_comment()
            return
        raw_end = _raw_string_end(self.text, self.index)
        if raw_end is not None:
            self._consume_raw_string(raw_end)
            return
        if char in {'"', "'"}:
            self._consume_quoted_string(char)
            return
        if char.isalpha() or char == "_":
            self._consume_identifier()
            return
        self._consume_punctuation(char)

    def _consume_directive(self) -> None:
        start = self.index
        directive_line = self.line
        end = self.index
        while end < len(self.text):
            newline = self.text.find("\n", end)
            if newline < 0:
                end = len(self.text)
                break
            slash_count = 0
            probe = newline - 1
            while probe >= start and self.text[probe] == "\\":
                slash_count += 1
                probe -= 1
            end = newline + 1
            if slash_count % 2 == 0:
                break
        first_line_end = self.text.find("\n", start, end)
        if first_line_end < 0:
            first_line_end = end
        match = re.match(
            r"#[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
            self.text[start:first_line_end],
        )
        name = match.group("name") if match else ""
        self.directives.append(_Directive(start, end, directive_line, name))
        self._update_conditional_depth(name)
        self.line, self.line_start = _consume_newlines(
            self.text, start, end, self.line, self.line_start
        )
        self.index = end

    def _update_conditional_depth(self, name: str) -> None:
        if name in {"if", "ifdef", "ifndef"}:
            self.conditional_depth += 1
        elif name == "endif":
            self.conditional_depth = max(0, self.conditional_depth - 1)

    def _consume_line_comment(self) -> None:
        end = self.text.find("\n", self.index + 2)
        if end < 0:
            end = len(self.text)
        self.comments.append(
            _LineComment(
                text=self.text[self.index + 2 : end],
                start=self.index,
                end=end,
                line=self.line,
                line_only=not self.text[self.line_start : self.index].strip(),
                conditional_depth=self.conditional_depth,
                brace_depth=self.brace_depth,
            )
        )
        self.index = end

    def _consume_block_comment(self) -> None:
        end_marker = self.text.find("*/", self.index + 2)
        end = len(self.text) if end_marker < 0 else end_marker + 2
        self.line, self.line_start = _consume_newlines(
            self.text, self.index, end, self.line, self.line_start
        )
        self.index = end

    def _consume_raw_string(self, end: int) -> None:
        self.tokens.append(
            _Token(
                "<string>",
                self.index,
                end,
                self.line,
                "string",
                self.conditional_depth,
                self.brace_depth,
            )
        )
        self.line, self.line_start = _consume_newlines(
            self.text, self.index, end, self.line, self.line_start
        )
        self.index = end

    def _consume_quoted_string(self, quote: str) -> None:
        start = self.index
        self.index += 1
        while self.index < len(self.text):
            if self.text[self.index] == "\\":
                self.index = min(len(self.text), self.index + 2)
                continue
            if self.text[self.index] == quote:
                self.index += 1
                break
            if self.text[self.index] == "\n":
                self.line += 1
                self.line_start = self.index + 1
            self.index += 1
        self.tokens.append(
            _Token(
                "<string>",
                start,
                self.index,
                self.line,
                "string",
                self.conditional_depth,
                self.brace_depth,
            )
        )

    def _consume_identifier(self) -> None:
        start = self.index
        self.index += 1
        while self.index < len(self.text) and (
            self.text[self.index].isalnum() or self.text[self.index] == "_"
        ):
            self.index += 1
        self.tokens.append(
            _Token(
                self.text[start : self.index],
                start,
                self.index,
                self.line,
                "identifier",
                self.conditional_depth,
                self.brace_depth,
            )
        )

    def _consume_punctuation(self, char: str) -> None:
        multi = next(
            (
                candidate
                for candidate in ("...", "::", "[[", "]]", "->")
                if self.text.startswith(candidate, self.index)
            ),
            None,
        )
        value = multi or char
        end = self.index + len(value)
        self.tokens.append(
            _Token(
                value,
                self.index,
                end,
                self.line,
                "punctuation",
                self.conditional_depth,
                self.brace_depth,
            )
        )
        if self.conditional_depth == 0:
            if value == "{":
                self.brace_depth += 1
            elif value == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
        self.index = end


def _lex_source(text: str) -> _LexedSource:
    """Lex just enough C/C++ to locate real line comments and declarations."""

    return _Lexer(text).lex()
