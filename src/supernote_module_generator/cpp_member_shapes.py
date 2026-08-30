"""Pure structural decisions for generated C++ class members."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AbstractSet

from .cpp_lexer import _Token
from .cpp_member_semantics import is_copy_or_move_constructor
from .cpp_type_syntax import cpp_type_spelling


class CppMemberShapeError(ValueError):
    """A member-shape failure awaiting marker and binding context."""

    def __init__(self, line: int | None, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


class CppCallableKind(str, Enum):
    CONSTRUCTOR = "constructor"
    DESTRUCTOR = "destructor"
    METHOD = "method"


class CppStoredMemberKind(str, Enum):
    """How one non-empty class declaration enters stored-member lowering."""

    CALLABLE = "callable"
    FIELD = "field"
    IGNORE = "ignore"


class CppConstructorRoute(str, Enum):
    """How one structurally identified constructor enters lowering."""

    LOWER = "lower"
    IGNORE = "ignore"
    REJECT_PREFIX = "reject_prefix"
    REJECT_COPY_OR_MOVE = "reject_copy_or_move"


@dataclass(frozen=True)
class CppCallableHead:
    kind: CppCallableKind
    constructor_prefix_supported: bool = False
    explicit: bool = False


@dataclass(frozen=True)
class CppFieldShape:
    name: str
    type_spelling: str
    mutable: bool
    static: bool


@dataclass(frozen=True)
class CppMethodShape:
    name: str
    name_line: int
    return_type_spelling: str
    static: bool


def stored_member_kind(
    declaration: list[_Token],
    *,
    marked: bool,
    value_class: bool,
) -> CppStoredMemberKind:
    """Route one declaration while enforcing value-field marker coverage."""

    values = [token.value for token in declaration]
    if "(" in values:
        return CppStoredMemberKind.CALLABLE
    if marked:
        return CppStoredMemberKind.FIELD
    if value_class and "static" not in values:
        raise CppMemberShapeError(
            declaration[0].line,
            "every non-static stored value member requires "
            "SupernotePluginExport",
        )
    return CppStoredMemberKind.IGNORE


def callable_head(tokens: list[_Token], cpp_name: str) -> CppCallableHead:
    """Classify one callable declaration prefix for a containing class."""

    values = [token.value for token in tokens]
    if values == ["~", cpp_name]:
        return CppCallableHead(CppCallableKind.DESTRUCTOR)
    if values and values[-1] == cpp_name:
        supported = values in ([cpp_name], ["explicit", cpp_name])
        return CppCallableHead(
            CppCallableKind.CONSTRUCTOR,
            constructor_prefix_supported=supported,
            explicit=supported and values[0] == "explicit",
        )
    return CppCallableHead(CppCallableKind.METHOD)


def constructor_route(
    head: CppCallableHead,
    groups: list[list[_Token]],
    *,
    cpp_name: str,
    marked: bool,
) -> CppConstructorRoute:
    """Route a constructor after parameter grouping and head classification."""

    if not head.constructor_prefix_supported:
        return (
            CppConstructorRoute.REJECT_PREFIX
            if marked
            else CppConstructorRoute.IGNORE
        )
    if is_copy_or_move_constructor(groups, cpp_name):
        return (
            CppConstructorRoute.REJECT_COPY_OR_MOVE
            if marked
            else CppConstructorRoute.IGNORE
        )
    return CppConstructorRoute.LOWER


def field_shape(declaration: list[_Token]) -> CppFieldShape:
    """Return the structural shape of one generated stored-value field."""

    values = [token.value for token in declaration]
    if any(value in values for value in {"=", ",", "*", "[["}):
        raise CppMemberShapeError(
            None,
            "a generated field must be one directly declared named canonical "
            "generated field without initializer, pointer, attribute, or multiple "
            "declarator",
        )
    is_static = values[0] == "static"
    field_tokens = declaration[1:] if is_static else declaration
    is_const = bool(field_tokens and field_tokens[0].value == "const")
    if is_const:
        field_tokens = field_tokens[1:]
    if len(field_tokens) < 2 or field_tokens[-1].kind != "identifier":
        raise CppMemberShapeError(
            None,
            "a generated field requires a canonical type and ordinary name",
        )
    field_type = cpp_type_spelling(field_tokens[:-1])
    if field_type is None:
        raise CppMemberShapeError(None, "unsupported generated field type spelling")
    return CppFieldShape(
        name=field_tokens[-1].value,
        type_spelling=field_type,
        mutable=not is_const,
        static=is_static,
    )


def method_shape(
    declaration: list[_Token],
    opening_index: int,
    *,
    keywords: AbstractSet[str],
) -> CppMethodShape:
    """Return the structural shape of one generated method declaration."""

    prefix_values = [token.value for token in declaration[:opening_index]]
    for forbidden, description in (
        ("operator", "operators"),
        ("virtual", "virtual methods"),
    ):
        if forbidden in prefix_values:
            raise CppMemberShapeError(
                declaration[prefix_values.index(forbidden)].line,
                f"{description} are deferred generated-member forms",
            )

    method_prefix = declaration[:opening_index]
    is_static = bool(method_prefix and method_prefix[0].value == "static")
    if is_static:
        method_prefix = method_prefix[1:]
    return_type = cpp_type_spelling(method_prefix[:-1])
    method_token = method_prefix[-1] if method_prefix else declaration[0]
    if return_type is None or not method_prefix or method_token.kind != "identifier":
        raise CppMemberShapeError(
            declaration[0].line,
            "a marked method must use one canonical generated result type followed "
            "by an ordinary method name",
        )
    method_name = method_token.value
    if method_name in keywords:
        raise CppMemberShapeError(
            method_token.line,
            f"method name {method_name!r} is a C++23 keyword",
        )
    return CppMethodShape(
        name=method_name,
        name_line=method_token.line,
        return_type_spelling=return_type,
        static=is_static,
    )
