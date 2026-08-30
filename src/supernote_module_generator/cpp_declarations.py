"""Parse C++ Supernote marker stacks into declaration intent."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from .cpp_lexer import _LexedSource, _LineComment, _Token
from .cpp_members import MemberDeclaration
from .source_models import (
    DeclarationTarget,
    MarkerOccurrence,
    SourceIntent,
    SourceModelError,
    SupernoteMarker,
)


_SOURCE_MARKERS = {marker.value: marker for marker in SupernoteMarker}
_SOURCE_MARKER = re.compile(r"@(?P<name>Supernote[A-Za-z][A-Za-z0-9_]*)")


class CppDeclarationError(ValueError):
    """A source-located declaration-intent error awaiting path context."""

    def __init__(self, line: int, export_name: str | None, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.export_name = export_name
        self.message = message


@dataclass(frozen=True)
class MarkerStack:
    comments: tuple[_LineComment, ...]
    markers: tuple[SupernoteMarker, ...]

    @property
    def first(self) -> _LineComment:
        return self.comments[0]

    @property
    def last(self) -> _LineComment:
        return self.comments[-1]


@dataclass(frozen=True)
class MemberMarkerBindings:
    """Marker stacks routed to their following class-member declarations."""

    stacks_by_declaration: tuple[tuple[int, MarkerStack], ...]
    consumed_comment_offsets: frozenset[int]


class ClassStackKind(str, Enum):
    """How one marker stack participates in top-level class discovery."""

    CLASS = "class"
    ENUM = "enum"
    IGNORE = "ignore"


@dataclass(frozen=True)
class ClassStackRoute:
    """Pure class-discovery route for one marker stack."""

    kind: ClassStackKind
    following: _Token | None


def source_marker(comment: _LineComment) -> tuple[bool, SupernoteMarker | None]:
    """Return whether a line comment is a marker candidate and its marker."""

    value = comment.text.strip()
    if re.match(r"@SupernoteExportObject(?:\b|\()", value):
        return False, None
    match = _SOURCE_MARKER.fullmatch(value)
    if match:
        return True, _SOURCE_MARKERS.get(match.group("name"))
    return bool(re.match(r"@Supernote(?:[A-Za-z0-9_]|\()", value)), None


def marker_entries(
    lexed: _LexedSource,
) -> list[tuple[_LineComment, SupernoteMarker]]:
    """Recognize every marker comment and reject malformed candidates."""

    entries: list[tuple[_LineComment, SupernoteMarker]] = []
    for comment in lexed.comments:
        is_candidate, marker = source_marker(comment)
        if not is_candidate:
            continue
        if marker is None:
            raise _invalid_marker(comment)
        entries.append((comment, marker))
    return entries


def _invalid_marker(comment: _LineComment) -> CppDeclarationError:
    value = comment.text.strip()
    match = _SOURCE_MARKER.fullmatch(value)
    if match and match.group("name") not in _SOURCE_MARKERS:
        message = (
            f"unknown Supernote marker {match.group('name')!r}; supported "
            "markers are SupernotePluginObject, SupernotePluginValue, "
            "SupernotePluginExport, SupernotePluginInternal, "
            "SupernotePluginAsync, and SupernoteConstructor"
        )
    else:
        message = (
            "malformed Supernote marker; generated markers take no "
            "arguments and must be written exactly, for example "
            "// @SupernotePluginExport"
        )
    return CppDeclarationError(comment.line, None, message)


def marker_stacks(
    text: str,
    entries: list[tuple[_LineComment, SupernoteMarker]],
) -> list[MarkerStack]:
    """Group adjacent marker comments into declaration-intent stacks."""

    stacks: list[MarkerStack] = []
    comments: list[_LineComment] = []
    markers: list[SupernoteMarker] = []
    for comment, marker in entries:
        if comments and text[comments[-1].end : comment.start].strip():
            stacks.append(MarkerStack(tuple(comments), tuple(markers)))
            comments = []
            markers = []
        comments.append(comment)
        markers.append(marker)
    if comments:
        stacks.append(MarkerStack(tuple(comments), tuple(markers)))
    return stacks


def intent_from_stack(
    stack: MarkerStack,
    target: DeclarationTarget,
    export_name: str | None,
) -> SourceIntent:
    """Validate a marker stack and lower it into immutable source intent."""

    occurrences = tuple(
        MarkerOccurrence(marker, comment.line)
        for marker, comment in zip(stack.markers, stack.comments)
    )
    try:
        return SourceIntent(target, occurrences)
    except SourceModelError as exc:
        diagnostic = _intent_diagnostic(stack)
        raise CppDeclarationError(
            diagnostic.line, export_name, str(exc)
        ) from exc


def _intent_diagnostic(stack: MarkerStack) -> _LineComment:
    if len(stack.markers) != len(set(stack.markers)):
        seen: set[SupernoteMarker] = set()
        for marker, comment in zip(stack.markers, stack.comments):
            if marker in seen:
                return comment
            seen.add(marker)
    if SupernoteMarker.ASYNC in stack.markers:
        return stack.comments[stack.markers.index(SupernoteMarker.ASYNC)]
    if SupernoteMarker.CONSTRUCTOR in stack.markers:
        return stack.comments[stack.markers.index(SupernoteMarker.CONSTRUCTOR)]
    return stack.last


def validate_marker_stack_location(
    stack: MarkerStack,
    *,
    brace_depth: int,
    description: str,
    export_name: str | None = None,
    brace_message: str | None = None,
) -> None:
    """Enforce line, conditional, and declaration-scope marker policy."""

    for comment in stack.comments:
        _validate_marker_location(
            comment,
            brace_depth=brace_depth,
            description=description,
            export_name=export_name,
            brace_message=brace_message,
        )


def member_marker_bindings(
    text: str,
    declarations: list[MemberDeclaration],
    stacks: list[MarkerStack],
    *,
    class_stack: MarkerStack | None,
    opening_end: int,
    closing_start: int,
    member_depth: int,
    class_name: str,
) -> MemberMarkerBindings:
    """Validate and route every marker stack within one class definition."""

    member_stacks = [
        stack
        for stack in stacks
        if opening_end <= stack.first.start < closing_start
        and stack is not class_stack
    ]
    consumed = (
        {comment.start for comment in class_stack.comments}
        if class_stack is not None
        else set()
    )
    stack_by_declaration: dict[int, MarkerStack] = {}
    for stack in member_stacks:
        validate_marker_stack_location(
            stack,
            brace_depth=member_depth,
            description="class member",
        )
        declaration = next(
            (
                item
                for item in declarations
                if item[1] and item[1][0].start >= stack.last.end
            ),
            None,
        )
        if declaration is None:
            raise CppDeclarationError(
                stack.first.line,
                class_name,
                "a member marker stack must be followed by a member declaration",
            )
        _access, tokens = declaration
        if text[stack.last.end : tokens[0].start].strip():
            raise CppDeclarationError(
                stack.first.line,
                class_name,
                "only whitespace may appear between the final member marker "
                "and its declaration",
            )
        if tokens[0].start in stack_by_declaration:
            raise CppDeclarationError(
                stack.first.line,
                class_name,
                "separate marker stacks cannot target the same member",
            )
        stack_by_declaration[tokens[0].start] = stack
        consumed.update(comment.start for comment in stack.comments)
    return MemberMarkerBindings(
        tuple(stack_by_declaration.items()),
        frozenset(consumed),
    )


def _validate_marker_location(
    comment: _LineComment,
    *,
    brace_depth: int,
    description: str,
    export_name: str | None,
    brace_message: str | None,
) -> None:
    if not comment.line_only:
        raise CppDeclarationError(
            comment.line,
            export_name,
            "a Supernote marker must be a // comment on its own line",
        )
    if comment.conditional_depth:
        raise CppDeclarationError(
            comment.line,
            export_name,
            "Supernote markers are not allowed inside a preprocessor "
            "conditional (#if, #ifdef, or #ifndef block)",
        )
    if comment.brace_depth != brace_depth:
        message = brace_message or (
            f"a {description} marker must be at brace depth {brace_depth}"
        )
        raise CppDeclarationError(comment.line, export_name, message)


def namespace_at(lexed: _LexedSource, offset: int) -> tuple[tuple[str, ...], int]:
    """Return the named namespace path and namespace brace depth at offset."""

    tokens = [item for item in lexed.tokens if item.conditional_depth == 0]
    ranges: list[tuple[int, int, tuple[str, ...]]] = []
    for index, token in enumerate(tokens):
        namespace_range = _namespace_range(tokens, index, offset)
        if namespace_range is not None:
            ranges.append(namespace_range)
    ranges.sort(key=lambda item: item[0])
    namespace = tuple(name for _, _, names in ranges for name in names)
    return namespace, len(ranges)


def class_stack_route(
    lexed: _LexedSource,
    stack: MarkerStack,
) -> ClassStackRoute:
    """Classify one marker stack before class-source parsing."""

    _, namespace_depth = namespace_at(lexed, stack.first.start)
    if stack.first.brace_depth != namespace_depth:
        if any(
            marker in {SupernoteMarker.OBJECT, SupernoteMarker.VALUE}
            for marker in stack.markers
        ):
            raise CppDeclarationError(
                stack.first.line,
                None,
                "marked C++ types must be at global or named-namespace "
                "brace depth; anonymous namespaces and nested types "
                "are unsupported",
            )
        return ClassStackRoute(ClassStackKind.IGNORE, None)
    following = next(
        (
            token
            for token in lexed.tokens
            if token.conditional_depth == 0 and token.start >= stack.last.end
        ),
        None,
    )
    kind = (
        ClassStackKind.ENUM
        if following is not None and following.value == "enum"
        else ClassStackKind.CLASS
    )
    return ClassStackRoute(kind, following)


def unmarked_class_owner_offsets(
    stacks: list[MarkerStack],
    consumed_comment_offsets: set[int],
    extents: list[tuple[_Token, _Token, _Token]],
    parsed_class_offsets: set[int],
) -> tuple[int, ...]:
    """Return unmarked owner classes required by remaining member markers."""

    owner_offsets: set[int] = set()
    for stack in stacks:
        if all(
            comment.start in consumed_comment_offsets for comment in stack.comments
        ):
            continue
        owner = next(
            (
                extent
                for extent in extents
                if extent[1].end <= stack.first.start < extent[2].start
            ),
            None,
        )
        if owner is not None and owner[0].start not in parsed_class_offsets:
            owner_offsets.add(owner[0].start)
    return tuple(sorted(owner_offsets))


def first_unconsumed_marker(
    entries: list[tuple[_LineComment, SupernoteMarker]],
    consumed_comment_offsets: set[int],
) -> _LineComment | None:
    """Return the first marker comment not claimed by a class parse."""

    return next(
        (
            comment
            for comment, _ in entries
            if comment.start not in consumed_comment_offsets
        ),
        None,
    )


def _namespace_range(
    tokens: list[_Token], index: int, offset: int
) -> tuple[int, int, tuple[str, ...]] | None:
    token = tokens[index]
    if token.value != "namespace" or token.start >= offset:
        return None
    names, opening_index = _namespace_opening(tokens, index + 1)
    if not names or opening_index is None:
        return None
    opening = tokens[opening_index]
    closing = next(
        (
            item
            for item in tokens[opening_index + 1 :]
            if item.value == "}" and item.brace_depth == opening.brace_depth + 1
        ),
        None,
    )
    if closing is None or not opening.end <= offset < closing.start:
        return None
    return opening.start, closing.start, names


def _namespace_opening(
    tokens: list[_Token], cursor: int
) -> tuple[tuple[str, ...], int | None]:
    names: list[str] = []
    expect_name = True
    while cursor < len(tokens) and tokens[cursor].value != "{":
        current = tokens[cursor]
        if expect_name and current.kind == "identifier":
            names.append(current.value)
            expect_name = False
        elif not expect_name and current.value == "::":
            expect_name = True
        else:
            return (), None
        cursor += 1
    if cursor >= len(tokens) or not names or expect_name:
        return (), None
    return tuple(names), cursor
