#!/usr/bin/env python3
"""Generate JNI or JSI bindings from annotated C++ APIs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

if __package__:
    from .cpp_projection import (
        CppProjectionError,
        project_cpp_api,
        project_cpp_functions,
    )
    from .semantic import (
        DeclarationRole,
        ExecutionMode,
        SemanticApi,
        SemanticClassKind,
        SourceProvenance,
    )
    from .source_models import (
        CppClassSource,
        CppConstructorSource,
        CppFunctionSource,
        CppMethodSource,
        CppParameterSource,
        DeclarationTarget,
        MarkerOccurrence,
        SourceIntent,
        SourceModelError,
        SupernoteMarker,
    )
else:
    from supernote_codegen.cpp_projection import (  # type: ignore[no-redef]
        CppProjectionError,
        project_cpp_api,
        project_cpp_functions,
    )
    from supernote_codegen.semantic import (  # type: ignore[no-redef]
        DeclarationRole,
        ExecutionMode,
        SemanticApi,
        SemanticClassKind,
        SourceProvenance,
    )
    from supernote_codegen.source_models import (  # type: ignore[no-redef]
        CppClassSource,
        CppConstructorSource,
        CppFunctionSource,
        CppMethodSource,
        CppParameterSource,
        DeclarationTarget,
        MarkerOccurrence,
        SourceIntent,
        SourceModelError,
        SupernoteMarker,
    )


SOURCE_MARKERS = {
    marker.value: marker
    for marker in SupernoteMarker
}
SOURCE_MARKER = re.compile(r"@(?P<name>Supernote[A-Za-z][A-Za-z0-9_]*)")
OBJECT_ANNOTATION = re.compile(
    r"@SupernoteExportObject"
    r"(?:\(\s*name\s*=\s*\"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\"\s*\))?"
)
CPP_SUFFIXES = {".cc", ".cpp", ".cxx"}
CPP_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
FORBIDDEN_TAG_SUFFIXES = {
    ".c",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inl",
    ".inc",
    ".ipp",
    ".tpp",
}
KOTLIN_KEYWORDS = {
    "_", "actual", "abstract", "annotation", "as", "break", "by", "catch",
    "class", "companion", "const", "constructor", "continue", "crossinline",
    "data", "delegate", "do", "dynamic", "else", "enum", "expect", "external",
    "false", "field", "file", "final", "finally", "for", "fun", "get", "if",
    "import", "in", "infix", "init", "inline", "inner", "interface",
    "internal", "is", "it", "lateinit", "noinline", "null", "object", "open",
    "operator", "out", "override", "package", "param", "private", "property",
    "protected", "public", "receiver", "reified", "return", "sealed", "set",
    "setparam", "super", "suspend", "tailrec", "this", "throw", "true", "try",
    "typealias", "typeof", "val", "var", "vararg", "when", "where", "while",
}
JAVA_KEYWORDS = {
    "_", "abstract", "assert", "boolean", "break", "byte", "case", "catch",
    "char", "class", "const", "continue", "default", "do", "double", "else",
    "enum", "exports", "extends", "false", "final", "finally", "float", "for",
    "goto", "if", "implements", "import", "instanceof", "int", "interface",
    "long", "module", "native", "new", "non-sealed", "null", "open", "opens",
    "package", "permits", "private", "protected", "provides", "public",
    "record", "requires", "return", "sealed", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws", "to",
    "transient", "transitive", "true", "try", "uses", "var", "void",
    "volatile", "while", "with", "yield",
}
JNI_RESERVED_IDENTIFIERS = KOTLIN_KEYWORDS | JAVA_KEYWORDS
GENERATED_KOTLIN_METHOD_NAMES = {
    "canOverrideExistingModule",
    "getConstants",
    "getName",
    "hasConstants",
    "initialize",
    "invalidate",
    "onCatalystInstanceDestroy",
}
CPP23_KEYWORDS = {
    "alignas",
    "alignof",
    "and",
    "and_eq",
    "asm",
    "auto",
    "bitand",
    "bitor",
    "bool",
    "break",
    "case",
    "catch",
    "char",
    "char8_t",
    "char16_t",
    "char32_t",
    "class",
    "compl",
    "concept",
    "const",
    "const_cast",
    "consteval",
    "constexpr",
    "constinit",
    "continue",
    "co_await",
    "co_return",
    "co_yield",
    "decltype",
    "default",
    "delete",
    "do",
    "double",
    "dynamic_cast",
    "else",
    "enum",
    "explicit",
    "export",
    "extern",
    "false",
    "float",
    "for",
    "friend",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "mutable",
    "namespace",
    "new",
    "noexcept",
    "not",
    "not_eq",
    "nullptr",
    "operator",
    "or",
    "or_eq",
    "private",
    "protected",
    "public",
    "register",
    "reinterpret_cast",
    "requires",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "static_assert",
    "static_cast",
    "struct",
    "switch",
    "template",
    "this",
    "thread_local",
    "throw",
    "true",
    "try",
    "typedef",
    "typeid",
    "typename",
    "union",
    "unsigned",
    "using",
    "virtual",
    "void",
    "volatile",
    "wchar_t",
    "while",
    "xor",
    "xor_eq",
}
FORBIDDEN_DECLARATION_PREFIXES = {
    "alignas",
    "consteval",
    "constexpr",
    "constinit",
    "extern",
    "friend",
    "inline",
    "static",
    "template",
    "thread_local",
    "typedef",
    "using",
    "virtual",
}
SUPPORTED_PARAMETER_TYPES = {
    "bool",
    "int32_t",
    "std::int32_t",
    "int64_t",
    "std::int64_t",
    "float",
    "double",
    "std::string",
    "std::vector<std::byte>",
}
SUPPORTED_RETURN_TYPES = SUPPORTED_PARAMETER_TYPES | {"void"}
LEGACY_SYNC_LOWERING_TYPES = {"bool", "double", "std::string", "void"}
JSI_SYNC_LOWERING_TYPES = SUPPORTED_RETURN_TYPES


class CodegenError(RuntimeError):
    pass


@dataclass(frozen=True)
class Parameter:
    cpp_type: str
    name: str


@dataclass(frozen=True)
class Export:
    source: str
    line: int
    cpp_name: str
    js_name: str
    return_type: str
    parameters: tuple[Parameter, ...]
    noexcept: bool = False
    definition_offset: int = -1

    def manifest(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line": self.line,
            "cpp_name": self.cpp_name,
            "js_name": self.js_name,
            "return_type": self.return_type,
            "noexcept": self.noexcept,
            "parameters": [
                {"type": parameter.cpp_type, "name": parameter.name}
                for parameter in self.parameters
            ],
        }


@dataclass(frozen=True)
class ObjectConstructor:
    parameters: tuple[Parameter, ...]

    def manifest(self) -> dict[str, object]:
        return {
            "parameters": [
                {"type": parameter.cpp_type, "name": parameter.name}
                for parameter in self.parameters
            ],
        }


@dataclass(frozen=True)
class ObjectMethod:
    line: int
    cpp_name: str
    js_name: str
    return_type: str
    parameters: tuple[Parameter, ...]
    const: bool = False
    noexcept: bool = False

    def manifest(self) -> dict[str, object]:
        return {
            "line": self.line,
            "cpp_name": self.cpp_name,
            "js_name": self.js_name,
            "return_type": self.return_type,
            "parameters": [
                {"type": parameter.cpp_type, "name": parameter.name}
                for parameter in self.parameters
            ],
            "const": self.const,
            "noexcept": self.noexcept,
        }


@dataclass(frozen=True)
class ObjectExport:
    source: str
    include: str
    line: int
    cpp_name: str
    js_name: str
    constructor: ObjectConstructor
    methods: tuple[ObjectMethod, ...]

    def manifest(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line": self.line,
            "cpp_name": self.cpp_name,
            "js_name": self.js_name,
            "constructor": self.constructor.manifest(),
            "methods": [method.manifest() for method in self.methods],
        }


@dataclass(frozen=True)
class ScannedBindings:
    exports: tuple[Export, ...]
    objects: tuple[ObjectExport, ...]


def _error(path: Path, line: int, message: str) -> CodegenError:
    return CodegenError(f"{path}:{line}: {message}")


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


def _lex_source(text: str) -> _LexedSource:
    """Lex just enough C/C++ to locate real line comments and declarations."""
    tokens: list[_Token] = []
    comments: list[_LineComment] = []
    directives: list[_Directive] = []
    index = 0
    line = 1
    line_start = 0
    conditional_depth = 0
    brace_depth = 0
    size = len(text)

    while index < size:
        char = text[index]
        next_char = text[index + 1] if index + 1 < size else ""

        if char == "\n":
            line += 1
            line_start = index + 1
            index += 1
            continue
        if char.isspace():
            index += 1
            continue

        if char == "#" and not text[line_start:index].strip():
            start = index
            directive_line = line
            end = index
            while end < size:
                newline = text.find("\n", end)
                if newline < 0:
                    end = size
                    break
                slash_count = 0
                probe = newline - 1
                while probe >= start and text[probe] == "\\":
                    slash_count += 1
                    probe -= 1
                end = newline + 1
                if slash_count % 2 == 0:
                    break
            first_line_end = text.find("\n", start, end)
            if first_line_end < 0:
                first_line_end = end
            match = re.match(
                r"#[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
                text[start:first_line_end],
            )
            name = match.group("name") if match else ""
            directives.append(_Directive(start, end, directive_line, name))
            if name in {"if", "ifdef", "ifndef"}:
                conditional_depth += 1
            elif name == "endif":
                conditional_depth = max(0, conditional_depth - 1)
            line, line_start = _consume_newlines(
                text, start, end, line, line_start
            )
            index = end
            continue

        if char == "/" and next_char == "/":
            end = text.find("\n", index + 2)
            if end < 0:
                end = size
            comments.append(
                _LineComment(
                    text=text[index + 2:end],
                    start=index,
                    end=end,
                    line=line,
                    line_only=not text[line_start:index].strip(),
                    conditional_depth=conditional_depth,
                    brace_depth=brace_depth,
                )
            )
            index = end
            continue

        if char == "/" and next_char == "*":
            end_marker = text.find("*/", index + 2)
            end = size if end_marker < 0 else end_marker + 2
            line, line_start = _consume_newlines(
                text, index, end, line, line_start
            )
            index = end
            continue

        raw_end = _raw_string_end(text, index)
        if raw_end is not None:
            tokens.append(
                _Token(
                    "<string>",
                    index,
                    raw_end,
                    line,
                    "string",
                    conditional_depth,
                    brace_depth,
                )
            )
            line, line_start = _consume_newlines(
                text, index, raw_end, line, line_start
            )
            index = raw_end
            continue

        if char in {'"', "'"}:
            start = index
            quote = char
            index += 1
            while index < size:
                if text[index] == "\\":
                    index = min(size, index + 2)
                    continue
                if text[index] == quote:
                    index += 1
                    break
                if text[index] == "\n":
                    line += 1
                    line_start = index + 1
                index += 1
            tokens.append(
                _Token(
                    "<string>",
                    start,
                    index,
                    line,
                    "string",
                    conditional_depth,
                    brace_depth,
                )
            )
            continue

        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < size and (
                text[index].isalnum() or text[index] == "_"
            ):
                index += 1
            tokens.append(
                _Token(
                    text[start:index],
                    start,
                    index,
                    line,
                    "identifier",
                    conditional_depth,
                    brace_depth,
                )
            )
            continue

        multi = next(
            (
                candidate
                for candidate in ("...", "::", "[[", "]]", "->")
                if text.startswith(candidate, index)
            ),
            None,
        )
        value = multi or char
        end = index + len(value)
        tokens.append(
            _Token(
                value,
                index,
                end,
                line,
                "punctuation",
                conditional_depth,
                brace_depth,
            )
        )
        if conditional_depth == 0:
            if value == "{":
                brace_depth += 1
            elif value == "}":
                brace_depth = max(0, brace_depth - 1)
        index = end

    return _LexedSource(tuple(tokens), tuple(comments), tuple(directives))


def _normalize_backend(backend: object) -> str:
    value = str(backend)
    return "jni" if value == "cpp" else value


def _scan_context(
    module_root: Path,
    backend: str | None,
    module_name: str | None,
) -> tuple[str, str]:
    if backend is not None and module_name is not None:
        return _normalize_backend(backend), module_name
    config_path = module_root / "android/.supernote-module/codegen-config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    resolved_backend = _normalize_backend(
        backend if backend is not None else config.get("backend", "jni")
    )
    resolved_name = str(
        module_name
        if module_name is not None
        else config.get("module_name", module_root.name)
    )
    return resolved_backend, resolved_name


def _source_error(
    module_root: Path,
    path: Path,
    line: int,
    module_name: str,
    export_name: str | None,
    message: str,
) -> CodegenError:
    try:
        source = path.relative_to(module_root)
    except ValueError:
        source = path
    export = export_name if export_name is not None else "<unknown>"
    return CodegenError(
        f"{source}:{line}: module {module_name!r}, export {export!r}: {message}"
    )


def _source_marker(
    comment: _LineComment,
) -> tuple[bool, SupernoteMarker | None]:
    value = comment.text.strip()
    if re.match(r"@SupernoteExportObject(?:\b|\()", value):
        return False, None
    match = SOURCE_MARKER.fullmatch(value)
    if match:
        marker = SOURCE_MARKERS.get(match.group("name"))
        return True, marker
    return bool(re.match(r"@Supernote(?:[A-Za-z0-9_]|\()", value)), None


@dataclass(frozen=True)
class _MarkerStack:
    comments: tuple[_LineComment, ...]
    markers: tuple[SupernoteMarker, ...]

    @property
    def first(self) -> _LineComment:
        return self.comments[0]

    @property
    def last(self) -> _LineComment:
        return self.comments[-1]


def _object_marker_name(comment: _LineComment) -> tuple[bool, str | None]:
    value = comment.text.strip()
    match = OBJECT_ANNOTATION.fullmatch(value)
    if match:
        return True, match.group("name")
    return bool(re.match(r"@SupernoteExportObject(?:\b|\()", value)), None


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)


_CPP_TYPE_TOKENS = {
    ("void",): "void",
    ("bool",): "bool",
    ("int32_t",): "int32_t",
    ("int64_t",): "int64_t",
    ("float",): "float",
    ("double",): "double",
    ("std", "::", "int32_t"): "std::int32_t",
    ("std", "::", "int64_t"): "std::int64_t",
    ("std", "::", "string"): "std::string",
    (
        "std",
        "::",
        "vector",
        "<",
        "std",
        "::",
        "byte",
        ">",
    ): "std::vector<std::byte>",
}


def _type_prefix(tokens: list[_Token]) -> tuple[str | None, int]:
    values = tuple(token.value for token in tokens)
    for pattern, spelling in sorted(
        _CPP_TYPE_TOKENS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if values[: len(pattern)] == pattern:
            return spelling, len(pattern)
    return None, 0


def _parse_parameter(
    tokens: list[_Token],
    *,
    argument_index: int,
    module_root: Path,
    path: Path,
    marker_line: int,
    module_name: str,
    export_name: str,
) -> Parameter:
    line = tokens[0].line if tokens else marker_line
    expected = (
        f"argument {argument_index} must use one named canonical V2 value "
        "type, for example 'std::int32_t value'"
    )
    if not tokens:
        raise _source_error(
            module_root,
            path,
            line,
            module_name,
            export_name,
            f"unsupported parameter; {expected}",
        )
    values = [token.value for token in tokens]
    forbidden = {
        "&": "references",
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
            raise _source_error(
                module_root,
                path,
                token.line,
                module_name,
                export_name,
                f"unsupported parameter {_tokens_text(tokens)!r}: "
                f"{description} are not supported; {expected}",
            )

    cpp_type, consumed = _type_prefix(tokens)
    name = (
        tokens[consumed].value
        if cpp_type is not None
        and cpp_type != "void"
        and consumed + 1 == len(tokens)
        and tokens[consumed].kind == "identifier"
        else None
    )
    if cpp_type is None or name is None:
        raise _source_error(
            module_root,
            path,
            line,
            module_name,
            export_name,
            f"unsupported parameter {_tokens_text(tokens)!r}; {expected}",
        )
    if name in CPP23_KEYWORDS:
        raise _source_error(
            module_root,
            path,
            tokens[-1].line,
            module_name,
            export_name,
            f"argument {argument_index} name {name!r} is a C++23 keyword; "
            "rename the C++ parameter",
        )
    return Parameter(cpp_type, name)


def _parse_function_source(
    *,
    module_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    stack: _MarkerStack,
    next_marker_start: int | None,
    module_name: str,
) -> CppFunctionSource:
    marker = stack.first
    marker_export = "<pending>"
    for comment in stack.comments:
        if not comment.line_only:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                marker_export,
                "a Supernote marker must be a // comment on its own line",
            )
        if comment.conditional_depth:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                marker_export,
                "Supernote markers are not allowed inside a preprocessor "
                "conditional (#if, #ifdef, or #ifndef block)",
            )
        if comment.brace_depth:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                marker_export,
                "a free-function marker must be at global C++ scope",
            )

    occurrences = tuple(
        MarkerOccurrence(item, comment.line)
        for item, comment in zip(stack.markers, stack.comments)
    )
    try:
        intent = SourceIntent(DeclarationTarget.FUNCTION, occurrences)
    except SourceModelError as exc:
        diagnostic = stack.last
        if len(stack.markers) != len(set(stack.markers)):
            seen: set[SupernoteMarker] = set()
            for item, comment in zip(stack.markers, stack.comments):
                if item in seen:
                    diagnostic = comment
                    break
                seen.add(item)
        elif SupernoteMarker.ASYNC in stack.markers:
            diagnostic = stack.comments[stack.markers.index(SupernoteMarker.ASYNC)]
        elif SupernoteMarker.CONSTRUCTOR in stack.markers:
            diagnostic = stack.comments[
                stack.markers.index(SupernoteMarker.CONSTRUCTOR)
            ]
        raise _source_error(
            module_root,
            path,
            diagnostic.line,
            module_name,
            marker_export,
            str(exc),
        ) from exc

    active_tokens = [
        token for token in lexed.tokens if token.conditional_depth == 0
    ]
    preceding = [token for token in active_tokens if token.end <= marker.start]
    prefix: list[_Token] = []
    for token in reversed(preceding):
        if token.value in {";", "{", "}"}:
            break
        prefix.append(token)
    prefix.reverse()
    if prefix:
        raise _source_error(
            module_root,
            path,
            prefix[0].line,
            module_name,
            marker_export,
            "unsupported declaration prefix before the marker "
            f"{_tokens_text(prefix)!r}; place the Supernote marker stack "
            "immediately before an unmodified function return type and remove "
            "static, template, extern \"C\", attributes, or macros",
        )

    following = [token for token in active_tokens if token.start >= stack.last.end]
    if not following or (
        next_marker_start is not None
        and following[0].start >= next_marker_start
    ):
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "the Supernote marker stack must be followed by a supported "
            "top-level function definition; expected "
            "'std::int32_t name(std::int32_t value) {'",
        )
    first = following[0]
    intervening_directive = next(
        (
            directive
            for directive in lexed.directives
            if stack.last.end <= directive.start < first.start
        ),
        None,
    )
    if intervening_directive is not None:
        raise _source_error(
            module_root,
            path,
            intervening_directive.line,
            module_name,
            marker_export,
            "a preprocessor directive cannot occur between a Supernote marker "
            "stack and its function definition",
        )
    between = text[stack.last.end:first.start]
    if between.strip():
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "only whitespace may appear between the final Supernote marker "
            "and the function return type",
        )

    cursor = 0
    first_value = following[cursor].value
    if first_value in FORBIDDEN_DECLARATION_PREFIXES or first_value == "[[":
        raise _source_error(
            module_root,
            path,
            following[cursor].line,
            module_name,
            marker_export,
            "not a supported top-level function definition: declaration "
            f"modifier {first_value!r} is forbidden; routable functions must "
            "have ordinary external C++ linkage with no modifiers",
        )

    return_type, consumed = _type_prefix(following)
    if return_type is None:
        description = (
            "unsupported declaration prefix or macro"
            if following[cursor].kind == "identifier"
            else "unsupported return declaration"
        )
        raise _source_error(
            module_root,
            path,
            following[cursor].line,
            module_name,
            marker_export,
            "not a supported top-level function definition: "
            f"{description} {first_value!r}; expected one canonical V2 return "
            "type followed by a function name",
        )
    cursor = consumed

    if cursor >= len(following) or following[cursor].kind != "identifier":
        line = following[min(cursor, len(following) - 1)].line
        raise _source_error(
            module_root,
            path,
            line,
            module_name,
            marker_export,
            "not a supported top-level function definition: expected a C++ "
            "function name after the return type",
        )
    function_token = following[cursor]
    cpp_name = function_token.value
    js_name = cpp_name
    if cpp_name in CPP23_KEYWORDS:
        raise _source_error(
            module_root,
            path,
            following[cursor].line,
            module_name,
            js_name,
            f"C++ function name {cpp_name!r} is a C++23 keyword; rename the "
            "C++ function",
        )
    cursor += 1
    if cursor >= len(following) or following[cursor].value != "(":
        line = following[min(cursor, len(following) - 1)].line
        raise _source_error(
            module_root,
            path,
            line,
            module_name,
            js_name,
            "not a supported top-level function definition: unsupported "
            "modifier, attribute, or macro between the function name and '('",
        )
    cursor += 1

    groups: list[list[_Token]] = []
    group_start = cursor
    nesting = 0
    close_index: int | None = None
    while cursor < len(following):
        value = following[cursor].value
        if value == "(":
            nesting += 1
        elif value == ")":
            if nesting == 0:
                groups.append(following[group_start:cursor])
                close_index = cursor
                break
            nesting -= 1
        elif value == "," and nesting == 0:
            groups.append(following[group_start:cursor])
            group_start = cursor + 1
        elif value in {"{", ";"} and nesting == 0:
            break
        cursor += 1
    if close_index is None:
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            js_name,
            "not a supported top-level function definition: missing ')' in "
            "the tagged signature",
        )
    if len(groups) == 1 and not groups[0]:
        groups = []

    parameters: list[Parameter] = []
    names: set[str] = set()
    for argument_index, group in enumerate(groups, start=1):
        parameter = _parse_parameter(
            group,
            argument_index=argument_index,
            module_root=module_root,
            path=path,
            marker_line=marker.line,
            module_name=module_name,
            export_name=js_name,
        )
        if parameter.name in names:
            line = group[-1].line if group else marker.line
            raise _source_error(
                module_root,
                path,
                line,
                module_name,
                js_name,
                f"duplicate parameter name {parameter.name!r} at argument "
                f"{argument_index}; give every argument a unique name",
            )
        names.add(parameter.name)
        parameters.append(parameter)

    cursor = close_index + 1
    is_noexcept = False
    if cursor < len(following) and following[cursor].value == "noexcept":
        is_noexcept = True
        cursor += 1
        if cursor < len(following) and following[cursor].value == "(":
            raise _source_error(
                module_root,
                path,
                following[cursor].line,
                module_name,
                js_name,
                "only bare 'noexcept' is supported; remove the noexcept "
                "expression",
            )
    if cursor >= len(following):
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            js_name,
            "tagged declaration has no function body; add '{ ... }'",
        )
    opening = following[cursor]
    directive = next(
        (
            item
            for item in lexed.directives
            if stack.last.end <= item.start < opening.start
        ),
        None,
    )
    if directive is not None:
        raise _source_error(
            module_root,
            path,
            directive.line,
            module_name,
            js_name,
            "preprocessor directives are not supported inside a marked "
            "signature",
        )
    if opening.value == ";":
        raise _source_error(
            module_root,
            path,
            opening.line,
            module_name,
            js_name,
            "tagged declarations are not exported; provide the complete "
            "function definition with a '{ ... }' body",
        )
    if opening.value != "{":
        raise _source_error(
            module_root,
            path,
            opening.line,
            module_name,
            js_name,
            "unsupported tokens after the parameter list; only bare noexcept "
            "followed by the function body is allowed (no attributes, macros, "
            "qualifiers, or trailing return types)",
        )

    relative = str(path.relative_to(module_root))
    signature = ",".join(parameter.cpp_type for parameter in parameters)
    return CppFunctionSource(
        provenance=SourceProvenance(
            declaration_id=f"cpp:{relative}:{cpp_name}({signature})",
            language="cpp",
            path=relative,
            line=marker.line,
        ),
        cpp_name=cpp_name,
        return_type_spelling=return_type,
        parameters=tuple(
            CppParameterSource(parameter.cpp_type, parameter.name)
            for parameter in parameters
        ),
        intent=intent,
        noexcept=is_noexcept,
        definition_offset=function_token.start,
    )


def _parameter_groups(
    tokens: list[_Token],
    opening: int,
) -> tuple[list[list[_Token]], int]:
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


def _parse_parameters(
    groups: list[list[_Token]],
    *,
    module_root: Path,
    path: Path,
    marker_line: int,
    module_name: str,
    export_name: str,
) -> tuple[Parameter, ...]:
    parameters: list[Parameter] = []
    names: set[str] = set()
    for argument_index, group in enumerate(groups, start=1):
        parameter = _parse_parameter(
            group,
            argument_index=argument_index,
            module_root=module_root,
            path=path,
            marker_line=marker_line,
            module_name=module_name,
            export_name=export_name,
        )
        if parameter.name in names:
            raise _source_error(
                module_root,
                path,
                group[-1].line if group else marker_line,
                module_name,
                export_name,
                f"duplicate parameter name {parameter.name!r} at argument "
                f"{argument_index}; give every argument a unique name",
            )
        names.add(parameter.name)
        parameters.append(parameter)
    return tuple(parameters)


def _member_declarations(
    body: list[_Token],
    *,
    default_access: str,
) -> list[tuple[str, list[_Token]]]:
    """Return class-scope declarations without inspecting nested bodies."""
    declarations: list[tuple[str, list[_Token]]] = []
    access = default_access
    cursor = 0
    while cursor < len(body):
        if (
            cursor + 1 < len(body)
            and body[cursor].value in {"public", "private", "protected"}
            and body[cursor + 1].value == ":"
        ):
            access = body[cursor].value
            cursor += 2
            continue
        start = cursor
        paren_depth = 0
        bracket_depth = 0
        while cursor < len(body):
            value = body[cursor].value
            if value == "(":
                paren_depth += 1
                cursor += 1
            elif value == ")":
                paren_depth = max(0, paren_depth - 1)
                cursor += 1
            elif value in {"[", "[["}:
                bracket_depth += 1
                cursor += 1
            elif value in {"]", "]]"}:
                bracket_depth = max(0, bracket_depth - 1)
                cursor += 1
            elif value == ";" and paren_depth == 0 and bracket_depth == 0:
                declarations.append((access, body[start:cursor]))
                cursor += 1
                break
            elif value == "{" and paren_depth == 0 and bracket_depth == 0:
                signature = body[start:cursor]
                depth = 1
                cursor += 1
                while cursor < len(body) and depth:
                    if body[cursor].value == "{":
                        depth += 1
                    elif body[cursor].value == "}":
                        depth -= 1
                    cursor += 1
                if any(token.value == "(" for token in signature):
                    declarations.append((access, signature))
                if cursor < len(body) and body[cursor].value == ";":
                    cursor += 1
                break
            else:
                cursor += 1
        else:
            if body[start:]:
                declarations.append((access, body[start:]))
    return declarations


def _parse_member_qualifiers(
    tokens: list[_Token],
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    export_name: str,
    allow_const: bool,
    allow_default: bool,
) -> tuple[bool, bool]:
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
                raise _source_error(
                    module_root, path, tokens[cursor].line, module_name,
                    export_name,
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
        raise _source_error(
            module_root,
            path,
            tokens[cursor].line,
            module_name,
            export_name,
            f"unsupported trailing object member token {value!r}; only "
            "const and bare noexcept are supported",
        )
    return is_const, is_noexcept


def _is_copy_or_move_constructor(
    groups: list[list[_Token]],
    cpp_name: str,
) -> bool:
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


def _parse_object_members(
    *,
    module_root: Path,
    path: Path,
    marker_line: int,
    module_name: str,
    cpp_name: str,
    js_name: str,
    kind: str,
    body: list[_Token],
) -> tuple[ObjectConstructor, tuple[ObjectMethod, ...]]:
    constructors: list[ObjectConstructor] = []
    methods: list[ObjectMethod] = []
    method_names: dict[str, int] = {}
    declarations = _member_declarations(
        body,
        default_access="private" if kind == "class" else "public",
    )
    for access, declaration in declarations:
        if not declaration or access != "public":
            continue
        values = [token.value for token in declaration]
        if values[0] in {"class", "struct", "template"}:
            raise _source_error(
                module_root, path, declaration[0].line, module_name, js_name,
                "nested classes and templates are not supported in an "
                "exported object's public API",
            )
        if "(" not in values:
            continue
        opening = values.index("(")
        if "=" in values[:opening]:
            continue
        if opening + 1 < len(values) and values[opening + 1] == "*":
            continue
        if (
            "*" in values[:opening]
            and declaration[opening - 1].kind != "identifier"
        ):
            continue
        if "operator" in values:
            raise _source_error(
                module_root, path, declaration[values.index("operator")].line,
                module_name, js_name,
                "operators are not supported in an exported object's public API",
            )
        if "static" in values[:opening]:
            raise _source_error(
                module_root, path, declaration[values.index("static")].line,
                module_name, js_name,
                "static methods are not supported in an exported object's public API",
            )
        if "virtual" in values[:opening]:
            raise _source_error(
                module_root, path, declaration[values.index("virtual")].line,
                module_name, js_name,
                "virtual methods are not supported in an exported object's public API",
            )
        try:
            groups, closing = _parameter_groups(declaration, opening)
        except ValueError:
            raise _source_error(
                module_root, path, declaration[opening].line, module_name,
                js_name, "missing ')' in exported object member declaration",
            ) from None
        suffix = declaration[closing + 1:]
        prefix = declaration[:opening]
        if prefix[:2] == [declaration[0], declaration[1]] and values[:2] == ["~", cpp_name]:
            continue
        constructor_prefix = values[:opening]
        if constructor_prefix in ([cpp_name], ["explicit", cpp_name]):
            if _is_copy_or_move_constructor(groups, cpp_name):
                continue
            parameters = _parse_parameters(
                groups,
                module_root=module_root,
                path=path,
                marker_line=marker_line,
                module_name=module_name,
                export_name=f"{js_name}.create",
            )
            _parse_member_qualifiers(
                suffix,
                module_root=module_root,
                path=path,
                module_name=module_name,
                export_name=f"{js_name}.create",
                allow_const=False,
                allow_default=True,
            )
            constructors.append(ObjectConstructor(parameters))
            continue
        return_type: str | None = None
        name_index = 0
        if values[0] in {"bool", "double", "void"}:
            return_type = values[0]
            name_index = 1
        elif values[:3] == ["std", "::", "string"]:
            return_type = "std::string"
            name_index = 3
        if (
            return_type is None
            or name_index >= opening
            or declaration[name_index].kind != "identifier"
            or name_index + 1 != opening
        ):
            raise _source_error(
                module_root,
                path,
                declaration[0].line,
                module_name,
                js_name,
                f"unsupported public method declaration "
                f"{_tokens_text(declaration)!r}; methods must use bool, "
                "double, std::string, or void types",
            )
        method_name = declaration[name_index].value
        if method_name in CPP23_KEYWORDS:
            raise _source_error(
                module_root, path, declaration[name_index].line, module_name,
                js_name, f"method name {method_name!r} is a C++23 keyword",
            )
        if method_name in method_names:
            raise _source_error(
                module_root, path, declaration[name_index].line, module_name,
                js_name, f"overloaded or duplicate method {method_name!r}; "
                f"first declared at line {method_names[method_name]}. "
                "Object method overloads are not supported",
            )
        parameters = _parse_parameters(
            groups,
            module_root=module_root,
            path=path,
            marker_line=marker_line,
            module_name=module_name,
            export_name=f"{js_name}.{method_name}",
        )
        is_const, is_noexcept = _parse_member_qualifiers(
            suffix,
            module_root=module_root,
            path=path,
            module_name=module_name,
            export_name=f"{js_name}.{method_name}",
            allow_const=True,
            allow_default=False,
        )
        method_names[method_name] = declaration[name_index].line
        methods.append(
            ObjectMethod(
                line=declaration[name_index].line,
                cpp_name=method_name,
                js_name=method_name,
                return_type=return_type,
                parameters=parameters,
                const=is_const,
                noexcept=is_noexcept,
            )
        )
    if not constructors:
        raise _source_error(
            module_root, path, marker_line, module_name, js_name,
            "exported object must declare exactly one public callable constructor",
        )
    if len(constructors) > 1:
        raise _source_error(
            module_root, path, marker_line, module_name, js_name,
            "exported object has overloaded public constructors; exactly one "
            "callable constructor is supported",
        )
    return constructors[0], tuple(methods)


def _parse_object_export(
    *,
    module_root: Path,
    source_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    marker: _LineComment,
    renamed: str | None,
    module_name: str,
) -> ObjectExport:
    marker_name = renamed or "<pending>"
    if not marker.line_only:
        raise _source_error(
            module_root, path, marker.line, module_name, marker_name,
            "the object export marker must be a // comment on its own line",
        )
    if marker.conditional_depth:
        raise _source_error(
            module_root, path, marker.line, module_name, marker_name,
            "object export markers are not allowed inside preprocessor conditionals",
        )
    if marker.brace_depth:
        raise _source_error(
            module_root, path, marker.line, module_name, marker_name,
            "exported objects must be top-level class or struct definitions; "
            "nested exported classes are not supported",
        )
    active_tokens = [
        token for token in lexed.tokens if token.conditional_depth == 0
    ]
    preceding = [token for token in active_tokens if token.end <= marker.start]
    prefix: list[_Token] = []
    for token in reversed(preceding):
        if token.value in {";", "{", "}"}:
            break
        prefix.append(token)
    prefix.reverse()
    if prefix:
        raise _source_error(
            module_root, path, prefix[0].line, module_name, marker_name,
            "unsupported declaration prefix before the object marker "
            f"{_tokens_text(prefix)!r}; templates and declaration modifiers "
            "are not supported",
        )
    following = [
        token for token in active_tokens if token.start >= marker.end
    ]
    if not following:
        raise _source_error(
            module_root, path, marker.line, module_name, marker_name,
            "object export tag must be followed by a class or struct definition",
        )
    first = following[0]
    if text[marker.end:first.start].strip():
        raise _source_error(
            module_root, path, marker.line, module_name, marker_name,
            "only whitespace may appear between the object marker and class or struct",
        )
    directive = next(
        (item for item in lexed.directives if marker.end <= item.start < first.start),
        None,
    )
    if directive is not None:
        raise _source_error(
            module_root, path, directive.line, module_name, marker_name,
            "a preprocessor directive cannot occur between an object marker "
            "and its class or struct definition",
        )
    if first.value not in {"class", "struct"}:
        raise _source_error(
            module_root, path, first.line, module_name, marker_name,
            "object export tag must be followed by a class or struct definition",
        )
    if len(following) < 3 or following[1].kind != "identifier":
        raise _source_error(
            module_root, path, first.line, module_name, marker_name,
            "exported class or struct must have an ordinary identifier name",
        )
    cpp_name = following[1].value
    js_name = renamed or cpp_name
    opening = next(
        (index for index in range(2, len(following)) if following[index].value in {"{", ";"}),
        None,
    )
    if opening is None or following[opening].value != "{":
        raise _source_error(
            module_root, path, first.line, module_name, js_name,
            "export tag requires a complete class or struct definition, not a declaration",
        )
    prefix = following[2:opening]
    if any(token.value == ":" for token in prefix):
        raise _source_error(
            module_root, path, prefix[0].line, module_name, js_name,
            "inheritance is not supported for exported objects",
        )
    if prefix:
        raise _source_error(
            module_root, path, prefix[0].line, module_name, js_name,
            f"unsupported tokens before exported object body {_tokens_text(prefix)!r}",
        )
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
        raise _source_error(
            module_root, path, first.line, module_name, js_name,
            "exported object definition is missing its closing '}'",
        )
    if closing + 1 >= len(following) or following[closing + 1].value != ";":
        raise _source_error(
            module_root, path, following[closing].line, module_name, js_name,
            "exported object definition must end with '};'",
        )
    constructor, methods = _parse_object_members(
        module_root=module_root,
        path=path,
        marker_line=marker.line,
        module_name=module_name,
        cpp_name=cpp_name,
        js_name=js_name,
        kind=first.value,
        body=following[opening + 1:closing],
    )
    return ObjectExport(
        source=str(path.relative_to(module_root)),
        include=path.relative_to(source_root).as_posix(),
        line=marker.line,
        cpp_name=cpp_name,
        js_name=js_name,
        constructor=constructor,
        methods=methods,
    )


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


def _global_function_terminator(
    tokens: list[_Token],
    name_index: int,
) -> int | None:
    if (
        name_index + 1 >= len(tokens)
        or tokens[name_index + 1].value != "("
    ):
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
        and tokens[cursor].brace_depth == 0
        and tokens[cursor].value in {"{", ";"}
    ):
        return cursor
    return None


def _reject_untagged_global_functions(
    module_root: Path,
    lexed_sources: dict[Path, _LexedSource],
    sources: list[CppFunctionSource],
    module_name: str,
) -> None:
    routable_by_name = {source.cpp_name: source for source in sources}
    tagged_locations = {
        (source.provenance.path, source.definition_offset) for source in sources
    }
    for path, lexed in lexed_sources.items():
        if path.suffix.lower() not in CPP_SUFFIXES:
            continue
        source = str(path.relative_to(module_root))
        tokens = [
            token for token in lexed.tokens if token.conditional_depth == 0
        ]
        for index, token in enumerate(tokens):
            routed = routable_by_name.get(token.value)
            if routed is None or token.brace_depth != 0:
                continue
            if (source, token.start) in tagged_locations:
                continue
            terminator = _global_function_terminator(tokens, index)
            if terminator is None:
                continue

            prefix: list[_Token] = []
            for previous in reversed(tokens[:index]):
                if previous.value in {";", "{", "}"}:
                    break
                prefix.append(previous)
            prefix.reverse()
            if not prefix or any(item.value == "=" for item in prefix):
                continue
            if prefix[-1].value in {".", "->", "::", "(", "[", ","}:
                continue

            kind = (
                "definition"
                if tokens[terminator].value == "{"
                else "declaration"
            )
            raise _source_error(
                module_root,
                path,
                token.line,
                module_name,
                routed.cpp_name,
                f"untagged global {kind} for routable C++ name "
                f"{token.value!r} conflicts with the tagged definition at "
                f"{routed.provenance.path}:{routed.provenance.line}; overloads "
                "and duplicate "
                "declarations are not supported. Keep exactly one global "
                "function with this C++ name",
            )


def _marker_entries(
    module_root: Path,
    path: Path,
    lexed: _LexedSource,
    module_name: str,
) -> list[tuple[_LineComment, SupernoteMarker]]:
    entries: list[tuple[_LineComment, SupernoteMarker]] = []
    for comment in lexed.comments:
        is_candidate, marker = _source_marker(comment)
        if not is_candidate:
            continue
        if marker is None:
            value = comment.text.strip()
            match = SOURCE_MARKER.fullmatch(value)
            if match and match.group("name") not in SOURCE_MARKERS:
                message = (
                    f"unknown Supernote marker {match.group('name')!r}; supported "
                    "markers are SupernoteExport, SupernoteInternal, "
                    "SupernoteAsync, and SupernoteConstructor"
                )
            else:
                message = (
                    "malformed Supernote marker; initial V2 markers take no "
                    "arguments and must be written exactly, for example "
                    "// @SupernoteExport"
                )
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                message,
            )
        entries.append((comment, marker))
    return entries


def _marker_stacks(
    text: str,
    entries: list[tuple[_LineComment, SupernoteMarker]],
) -> list[_MarkerStack]:
    stacks: list[_MarkerStack] = []
    comments: list[_LineComment] = []
    markers: list[SupernoteMarker] = []
    for comment, marker in entries:
        if comments and text[comments[-1].end:comment.start].strip():
            stacks.append(_MarkerStack(tuple(comments), tuple(markers)))
            comments = []
            markers = []
        comments.append(comment)
        markers.append(marker)
    if comments:
        stacks.append(_MarkerStack(tuple(comments), tuple(markers)))
    return stacks


def _intent_from_stack(
    module_root: Path,
    path: Path,
    module_name: str,
    stack: _MarkerStack,
    target: DeclarationTarget,
    export_name: str | None,
) -> SourceIntent:
    occurrences = tuple(
        MarkerOccurrence(marker, comment.line)
        for marker, comment in zip(stack.markers, stack.comments)
    )
    try:
        return SourceIntent(target, occurrences)
    except SourceModelError as exc:
        diagnostic = stack.last
        if len(stack.markers) != len(set(stack.markers)):
            seen: set[SupernoteMarker] = set()
            for marker, comment in zip(stack.markers, stack.comments):
                if marker in seen:
                    diagnostic = comment
                    break
                seen.add(marker)
        elif SupernoteMarker.ASYNC in stack.markers:
            diagnostic = stack.comments[
                stack.markers.index(SupernoteMarker.ASYNC)
            ]
        elif SupernoteMarker.CONSTRUCTOR in stack.markers:
            diagnostic = stack.comments[
                stack.markers.index(SupernoteMarker.CONSTRUCTOR)
            ]
        raise _source_error(
            module_root,
            path,
            diagnostic.line,
            module_name,
            export_name,
            str(exc),
        ) from exc


def _validate_marker_stack_location(
    module_root: Path,
    path: Path,
    module_name: str,
    stack: _MarkerStack,
    *,
    brace_depth: int,
    description: str,
) -> None:
    for comment in stack.comments:
        if not comment.line_only:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                "a Supernote marker must be a // comment on its own line",
            )
        if comment.conditional_depth:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                "Supernote markers are not allowed inside a preprocessor "
                "conditional (#if, #ifdef, or #ifndef block)",
            )
        if comment.brace_depth != brace_depth:
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                f"a {description} marker must be at brace depth {brace_depth}",
            )


def _constructor_suffix(
    tokens: list[_Token],
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    class_name: str,
) -> tuple[bool, bool]:
    is_noexcept = False
    cursor = 0
    if cursor < len(tokens) and tokens[cursor].value == "noexcept":
        is_noexcept = True
        cursor += 1
        if cursor < len(tokens) and tokens[cursor].value == "(":
            raise _source_error(
                module_root,
                path,
                tokens[cursor].line,
                module_name,
                f"{class_name}.create",
                "only bare noexcept is supported on a constructor",
            )
    deleted = False
    if cursor < len(tokens):
        if (
            cursor + 2 == len(tokens)
            and tokens[cursor].value == "="
            and tokens[cursor + 1].value in {"default", "delete"}
        ):
            deleted = tokens[cursor + 1].value == "delete"
            cursor += 2
        else:
            raise _source_error(
                module_root,
                path,
                tokens[cursor].line,
                module_name,
                f"{class_name}.create",
                "unsupported constructor suffix; only bare noexcept, "
                "= default, or = delete is supported",
            )
    return is_noexcept, deleted


def _parse_v2_class_source(
    *,
    module_root: Path,
    source_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    class_stack: _MarkerStack,
    stacks: list[_MarkerStack],
    module_name: str,
) -> tuple[CppClassSource, set[int]]:
    _validate_marker_stack_location(
        module_root,
        path,
        module_name,
        class_stack,
        brace_depth=0,
        description="class",
    )
    class_intent = _intent_from_stack(
        module_root,
        path,
        module_name,
        class_stack,
        DeclarationTarget.CLASS,
        None,
    )
    active_tokens = [
        token for token in lexed.tokens if token.conditional_depth == 0
    ]
    preceding = [
        token for token in active_tokens if token.end <= class_stack.first.start
    ]
    prefix: list[_Token] = []
    for token in reversed(preceding):
        if token.value in {";", "{", "}"}:
            break
        prefix.append(token)
    prefix.reverse()
    if prefix:
        raise _source_error(
            module_root,
            path,
            prefix[0].line,
            module_name,
            None,
            "unsupported declaration prefix before the class marker "
            f"{_tokens_text(prefix)!r}; templates and declaration modifiers "
            "are not supported",
        )
    following = [
        token for token in active_tokens if token.start >= class_stack.last.end
    ]
    if not following:
        raise _source_error(
            module_root,
            path,
            class_stack.first.line,
            module_name,
            None,
            "a class marker stack must be followed by a complete class or "
            "struct definition",
        )
    first = following[0]
    if text[class_stack.last.end:first.start].strip():
        raise _source_error(
            module_root,
            path,
            class_stack.first.line,
            module_name,
            None,
            "only whitespace may appear between the final class marker and "
            "the class or struct definition",
        )
    if first.value not in {"class", "struct"}:
        raise _source_error(
            module_root,
            path,
            first.line,
            module_name,
            None,
            "a class marker stack must be followed by a class or struct "
            "definition",
        )
    if len(following) < 3 or following[1].kind != "identifier":
        raise _source_error(
            module_root,
            path,
            first.line,
            module_name,
            None,
            "a marked class or struct must have an ordinary identifier name",
        )
    cpp_name = following[1].value
    opening = next(
        (
            index
            for index in range(2, len(following))
            if following[index].value in {"{", ";"}
        ),
        None,
    )
    if opening is None or following[opening].value != "{":
        raise _source_error(
            module_root,
            path,
            first.line,
            module_name,
            cpp_name,
            "a marked class requires a complete definition, not a declaration",
        )
    before_body = following[2:opening]
    if any(token.value == ":" for token in before_body):
        raise _source_error(
            module_root,
            path,
            before_body[0].line,
            module_name,
            cpp_name,
            "inheritance is not supported for initial V2 generated classes",
        )
    if before_body:
        raise _source_error(
            module_root,
            path,
            before_body[0].line,
            module_name,
            cpp_name,
            f"unsupported tokens before marked class body "
            f"{_tokens_text(before_body)!r}",
        )
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
        raise _source_error(
            module_root,
            path,
            first.line,
            module_name,
            cpp_name,
            "marked class definition is missing its closing '}'",
        )
    if closing + 1 >= len(following) or following[closing + 1].value != ";":
        raise _source_error(
            module_root,
            path,
            following[closing].line,
            module_name,
            cpp_name,
            "marked class definition must end with '};'",
        )

    opening_token = following[opening]
    closing_token = following[closing]
    member_depth = opening_token.brace_depth + 1
    body = following[opening + 1:closing]
    declarations = _member_declarations(
        body,
        default_access="private" if first.value == "class" else "public",
    )
    member_stacks = [
        stack
        for stack in stacks
        if opening_token.end <= stack.first.start < closing_token.start
        and stack is not class_stack
    ]
    consumed = {comment.start for comment in class_stack.comments}
    stack_by_declaration: dict[int, _MarkerStack] = {}
    for stack in member_stacks:
        _validate_marker_stack_location(
            module_root,
            path,
            module_name,
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
            raise _source_error(
                module_root,
                path,
                stack.first.line,
                module_name,
                cpp_name,
                "a member marker stack must be followed by a member "
                "declaration",
            )
        access, tokens = declaration
        if text[stack.last.end:tokens[0].start].strip():
            raise _source_error(
                module_root,
                path,
                stack.first.line,
                module_name,
                cpp_name,
                "only whitespace may appear between the final member marker "
                "and its declaration",
            )
        if tokens[0].start in stack_by_declaration:
            raise _source_error(
                module_root,
                path,
                stack.first.line,
                module_name,
                cpp_name,
                "separate marker stacks cannot target the same member",
            )
        stack_by_declaration[tokens[0].start] = stack
        consumed.update(comment.start for comment in stack.comments)

    constructors: list[CppConstructorSource] = []
    methods: list[CppMethodSource] = []
    method_names: dict[str, int] = {}
    constructor_ids: set[str] = set()
    has_user_constructor = False
    relative = str(path.relative_to(module_root))
    for access, declaration in declarations:
        if not declaration:
            continue
        stack = stack_by_declaration.get(declaration[0].start)
        values = [token.value for token in declaration]
        if "(" not in values:
            if stack is not None:
                raise _source_error(
                    module_root,
                    path,
                    stack.first.line,
                    module_name,
                    cpp_name,
                    "properties, fields, and other non-method generated "
                    "members are deferred in initial V2",
                )
            continue
        opening_index = values.index("(")
        try:
            groups, closing_index = _parameter_groups(
                declaration,
                opening_index,
            )
        except ValueError:
            if stack is None:
                continue
            raise _source_error(
                module_root,
                path,
                declaration[opening_index].line,
                module_name,
                cpp_name,
                "missing ')' in marked member declaration",
            ) from None
        prefix_values = values[:opening_index]
        suffix = declaration[closing_index + 1:]
        is_destructor = prefix_values == ["~", cpp_name]
        is_constructor = (
            not is_destructor
            and bool(prefix_values)
            and prefix_values[-1] == cpp_name
        )
        if is_constructor:
            has_user_constructor = True
            if prefix_values not in ([cpp_name], ["explicit", cpp_name]):
                if stack is not None:
                    raise _source_error(
                        module_root,
                        path,
                        declaration[0].line,
                        module_name,
                        f"{cpp_name}.create",
                        "a generated constructor may use only the optional "
                        "explicit modifier before its class name",
                    )
                continue
            if _is_copy_or_move_constructor(groups, cpp_name):
                if stack is not None:
                    raise _source_error(
                        module_root,
                        path,
                        stack.first.line,
                        module_name,
                        cpp_name,
                        "copy and move constructors cannot be generated "
                        "creation paths",
                    )
                continue
            intent = (
                _intent_from_stack(
                    module_root,
                    path,
                    module_name,
                    stack,
                    DeclarationTarget.CONSTRUCTOR,
                    f"{cpp_name}.create",
                )
                if stack is not None
                else SourceIntent(DeclarationTarget.CONSTRUCTOR)
            )
            if stack is not None and access != "public":
                raise _source_error(
                    module_root,
                    path,
                    stack.first.line,
                    module_name,
                    f"{cpp_name}.create",
                    "SupernoteConstructor must mark a public constructor",
                )
            try:
                parsed_parameters = _parse_parameters(
                    groups,
                    module_root=module_root,
                    path=path,
                    marker_line=(stack.first.line if stack else declaration[0].line),
                    module_name=module_name,
                    export_name=f"{cpp_name}.create",
                )
            except CodegenError:
                if stack is not None:
                    raise
                continue
            try:
                is_noexcept, deleted = _constructor_suffix(
                    suffix,
                    module_root=module_root,
                    path=path,
                    module_name=module_name,
                    class_name=cpp_name,
                )
            except CodegenError:
                if stack is not None:
                    raise
                continue
            if stack is not None and deleted:
                raise _source_error(
                    module_root,
                    path,
                    stack.first.line,
                    module_name,
                    f"{cpp_name}.create",
                    "SupernoteConstructor cannot select a deleted constructor",
                )
            signature = ",".join(
                parameter.cpp_type for parameter in parsed_parameters
            )
            declaration_id = (
                f"cpp:{relative}:constructor:{cpp_name}({signature})"
            )
            if declaration_id in constructor_ids:
                raise _source_error(
                    module_root,
                    path,
                    declaration[0].line,
                    module_name,
                    f"{cpp_name}.create",
                    "duplicate constructor signature",
                )
            constructor_ids.add(declaration_id)
            constructors.append(
                CppConstructorSource(
                    provenance=SourceProvenance(
                        declaration_id=declaration_id,
                        language="cpp",
                        path=relative,
                        line=declaration[0].line,
                    ),
                    parameters=tuple(
                        CppParameterSource(parameter.cpp_type, parameter.name)
                        for parameter in parsed_parameters
                    ),
                    access=access,
                    intent=intent,
                    deleted=deleted,
                    explicit=prefix_values[0] == "explicit",
                    noexcept=is_noexcept,
                )
            )
            continue
        if stack is None:
            continue
        if is_destructor:
            raise _source_error(
                module_root,
                path,
                stack.first.line,
                module_name,
                cpp_name,
                "destructors cannot be generated members",
            )
        intent = _intent_from_stack(
            module_root,
            path,
            module_name,
            stack,
            DeclarationTarget.METHOD,
            cpp_name,
        )
        if access != "public":
            raise _source_error(
                module_root,
                path,
                stack.first.line,
                module_name,
                cpp_name,
                "a generated method must be public in C++",
            )
        for forbidden, description in (
            ("operator", "operators"),
            ("static", "static methods"),
            ("virtual", "virtual methods"),
        ):
            if forbidden in prefix_values:
                raise _source_error(
                    module_root,
                    path,
                    declaration[prefix_values.index(forbidden)].line,
                    module_name,
                    cpp_name,
                    f"{description} are deferred generated-member forms",
                )
        return_type, consumed_type = _type_prefix(declaration[:opening_index])
        if (
            return_type is None
            or consumed_type + 1 != opening_index
            or declaration[consumed_type].kind != "identifier"
        ):
            raise _source_error(
                module_root,
                path,
                declaration[0].line,
                module_name,
                cpp_name,
                "a marked method must use one canonical V2 result type "
                "followed by an ordinary method name",
            )
        method_name = declaration[consumed_type].value
        if method_name in CPP23_KEYWORDS:
            raise _source_error(
                module_root,
                path,
                declaration[consumed_type].line,
                module_name,
                cpp_name,
                f"method name {method_name!r} is a C++23 keyword",
            )
        if method_name in method_names:
            raise _source_error(
                module_root,
                path,
                declaration[consumed_type].line,
                module_name,
                cpp_name,
                f"duplicate generated method name {method_name!r}; first "
                f"marked at line {method_names[method_name]}",
            )
        parameters = _parse_parameters(
            groups,
            module_root=module_root,
            path=path,
            marker_line=stack.first.line,
            module_name=module_name,
            export_name=f"{cpp_name}.{method_name}",
        )
        is_const, is_noexcept = _parse_member_qualifiers(
            suffix,
            module_root=module_root,
            path=path,
            module_name=module_name,
            export_name=f"{cpp_name}.{method_name}",
            allow_const=True,
            allow_default=False,
        )
        signature = ",".join(parameter.cpp_type for parameter in parameters)
        method_names[method_name] = declaration[consumed_type].line
        methods.append(
            CppMethodSource(
                provenance=SourceProvenance(
                    declaration_id=(
                        f"cpp:{relative}:{cpp_name}.{method_name}({signature})"
                    ),
                    language="cpp",
                    path=relative,
                    line=stack.first.line,
                ),
                cpp_name=method_name,
                return_type_spelling=return_type,
                parameters=tuple(
                    CppParameterSource(parameter.cpp_type, parameter.name)
                    for parameter in parameters
                ),
                intent=intent,
                access=access,
                const=is_const,
                noexcept=is_noexcept,
            )
        )

    if not has_user_constructor:
        constructors.append(
            CppConstructorSource(
                provenance=SourceProvenance(
                    declaration_id=(
                        f"cpp:{relative}:constructor:{cpp_name}()#implicit"
                    ),
                    language="cpp",
                    path=relative,
                    line=class_stack.first.line,
                ),
                parameters=(),
                access="public",
                intent=SourceIntent(DeclarationTarget.CONSTRUCTOR),
                implicit=True,
            )
        )
    class_source = CppClassSource(
        provenance=SourceProvenance(
            declaration_id=f"cpp:{relative}:class:{cpp_name}",
            language="cpp",
            path=relative,
            line=class_stack.first.line,
        ),
        cpp_name=cpp_name,
        include=path.relative_to(source_root).as_posix(),
        intent=class_intent,
        constructors=tuple(constructors),
        methods=tuple(methods),
        declaration_kind=first.value,
    )
    return class_source, consumed


def scan_cpp_class_source_model(
    module_root: Path,
    *,
    module_name: str | None = None,
) -> list[CppClassSource]:
    source_root = module_root / "android/src/main/cpp"
    if not source_root.is_dir():
        raise CodegenError(f"missing C/C++ source directory: {source_root}")
    _, module_name = _scan_context(module_root, None, module_name)
    classes: list[CppClassSource] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CPP_HEADER_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        entries = _marker_entries(module_root, path, lexed, module_name)
        stacks = _marker_stacks(text, entries)
        consumed: set[int] = set()
        for stack in stacks:
            if stack.first.brace_depth != 0:
                continue
            item, item_consumed = _parse_v2_class_source(
                module_root=module_root,
                source_root=source_root,
                path=path,
                text=text,
                lexed=lexed,
                class_stack=stack,
                stacks=stacks,
                module_name=module_name,
            )
            classes.append(item)
            consumed.update(item_consumed)
        for comment, _ in entries:
            if comment.start not in consumed:
                raise _source_error(
                    module_root,
                    path,
                    comment.line,
                    module_name,
                    None,
                    "a marked C++ member requires a marked top-level "
                    "SupernoteExport or SupernoteInternal class",
                )
    classes.sort(
        key=lambda item: (
            item.provenance.path,
            item.provenance.line,
            item.cpp_name,
        )
    )
    return classes


def scan_cpp_source_model(
    module_root: Path,
    *,
    module_name: str | None = None,
) -> list[CppFunctionSource]:
    source_root = module_root / "android/src/main/cpp"
    if not source_root.is_dir():
        raise CodegenError(f"missing C/C++ source directory: {source_root}")

    _, module_name = _scan_context(module_root, None, module_name)
    all_sources = sorted(path for path in source_root.rglob("*") if path.is_file())
    lexed_sources: dict[Path, _LexedSource] = {}
    for path in all_sources:
        suffix = path.suffix.lower()
        if suffix not in CPP_SUFFIXES | FORBIDDEN_TAG_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        lexed_sources[path] = lexed
        active_tokens = [
            token for token in lexed.tokens if token.conditional_depth == 0
        ]
        for index, token in enumerate(active_tokens[:-1]):
            if (
                token.value == "JNI_OnLoad"
                and active_tokens[index + 1].value == "("
            ):
                raise _source_error(
                    module_root,
                    path,
                    token.line,
                    module_name,
                    "JNI_OnLoad",
                    "user sources must not declare or define JNI_OnLoad because "
                    "the generated binding layer owns that bootstrap symbol",
                )
        if suffix not in FORBIDDEN_TAG_SUFFIXES:
            continue
        if suffix in CPP_HEADER_SUFFIXES:
            # Header markers are parsed by scan_cpp_class_source_model().
            continue
        for comment, marker in _marker_entries(
            module_root, path, lexed, module_name
        ):
            if suffix == ".c":
                message = (
                    "direct marked C bindings are unsupported in initial V2; "
                    "use ordinary C23 implementation code behind a canonical "
                    "marked C++ boundary"
                )
            else:
                message = (
                    "free-function Supernote markers are allowed only in .cc, "
                    ".cpp, or .cxx files"
                )
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                message,
            )

    sources: list[CppFunctionSource] = []
    for path in all_sources:
        if path.suffix.lower() not in CPP_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = lexed_sources[path]
        stacks = _marker_stacks(
            text,
            _marker_entries(module_root, path, lexed, module_name),
        )
        for index, stack in enumerate(stacks):
            next_marker = (
                stacks[index + 1].first.start if index + 1 < len(stacks) else None
            )
            sources.append(
                _parse_function_source(
                    module_root=module_root,
                    path=path,
                    text=text,
                    lexed=lexed,
                    stack=stack,
                    next_marker_start=next_marker,
                    module_name=module_name,
                )
            )

    sources.sort(
        key=lambda item: (
            item.provenance.path,
            item.provenance.line,
            item.cpp_name,
        )
    )
    native_names: dict[str, CppFunctionSource] = {}
    for source in sources:
        if source.cpp_name in native_names:
            first = native_names[source.cpp_name]
            raise CodegenError(
                f"{source.provenance.path}:{source.provenance.line}: module "
                f"{module_name!r}, export {source.cpp_name!r}: overloaded or "
                f"duplicate routable C++ function {source.cpp_name!r}; first "
                f"marked at {first.provenance.path}:{first.provenance.line}. "
                "Rename one C++ function; "
                "overloads are not supported"
            )
        native_names[source.cpp_name] = source
    _reject_untagged_global_functions(
        module_root,
        lexed_sources,
        sources,
        module_name,
    )
    return sources


def scan_cpp_semantic_model(
    module_root: Path,
    *,
    module_name: str | None = None,
) -> SemanticApi:
    try:
        return project_cpp_api(
            scan_cpp_source_model(module_root, module_name=module_name),
            scan_cpp_class_source_model(module_root, module_name=module_name),
        )
    except (CppProjectionError, SourceModelError, ValueError) as exc:
        raise CodegenError(str(exc)) from exc


def _lower_sync_export(
    module_root: Path,
    source: CppFunctionSource,
    *,
    backend: str,
    module_name: str,
) -> Export:
    path = module_root / source.provenance.path
    name = source.cpp_name
    if source.intent.role is DeclarationRole.INTERNAL:
        raise _source_error(
            module_root,
            path,
            source.provenance.line,
            module_name,
            name,
            "SupernoteInternal was recognized, but its generated C++ caller "
            "route is not implemented yet",
        )
    if source.intent.execution is ExecutionMode.ASYNC:
        raise _source_error(
            module_root,
            path,
            source.provenance.line,
            module_name,
            name,
            "SupernoteAsync was recognized, but async lowering is not "
            "implemented yet",
        )
    used_types = {source.return_type_spelling}
    used_types.update(parameter.type_spelling for parameter in source.parameters)
    lowering_types = (
        JSI_SYNC_LOWERING_TYPES
        if backend == "jsi"
        else LEGACY_SYNC_LOWERING_TYPES
    )
    unsupported = sorted(used_types - lowering_types)
    if unsupported:
        raise _source_error(
            module_root,
            path,
            source.provenance.line,
            module_name,
            name,
            "semantic type was recognized, but synchronous "
            f"{backend.upper()} conversion is not implemented yet for "
            f"{', '.join(unsupported)}",
        )

    parameters = tuple(
        Parameter(parameter.type_spelling, parameter.name)
        for parameter in source.parameters
    )
    if backend == "jni":
        if name in JNI_RESERVED_IDENTIFIERS:
            raise _source_error(
                module_root,
                path,
                source.provenance.line,
                module_name,
                name,
                f"JavaScript export name {name!r} is reserved by Kotlin/Java; "
                "rename the C++ function",
            )
        if (
            name in GENERATED_KOTLIN_METHOD_NAMES
            or re.fullmatch(r"native[0-9]+", name)
        ):
            raise _source_error(
                module_root,
                path,
                source.provenance.line,
                module_name,
                name,
                f"JavaScript export name {name!r} collides with a generated "
                "Kotlin method; rename the C++ function",
            )
        for argument_index, parameter in enumerate(parameters, start=1):
            if parameter.name in JNI_RESERVED_IDENTIFIERS:
                raise _source_error(
                    module_root,
                    path,
                    source.provenance.line,
                    module_name,
                    name,
                    f"argument {argument_index} name {parameter.name!r} is "
                    "reserved by Kotlin/Java; rename the C++ parameter",
                )
            if (
                source.return_type_spelling != "void"
                and parameter.name == "promise"
            ):
                raise _source_error(
                    module_root,
                    path,
                    source.provenance.line,
                    module_name,
                    name,
                    f"argument {argument_index} name 'promise' collides with "
                    "the generated React Native Promise parameter; rename the "
                    "C++ parameter",
                )
    return Export(
        source=source.provenance.path,
        line=source.provenance.line,
        cpp_name=source.cpp_name,
        js_name=source.cpp_name,
        return_type=source.return_type_spelling,
        parameters=parameters,
        noexcept=source.noexcept,
        definition_offset=source.definition_offset,
    )


def scan_sources(
    module_root: Path,
    *,
    backend: str | None = None,
    module_name: str | None = None,
) -> list[Export]:
    backend, module_name = _scan_context(module_root, backend, module_name)
    sources = scan_cpp_source_model(module_root, module_name=module_name)
    try:
        semantics = project_cpp_functions(sources)
    except (CppProjectionError, SourceModelError, ValueError) as exc:
        raise CodegenError(str(exc)) from exc
    by_source = {
        binding.source.declaration_id: binding
        for binding in semantics.functions
    }
    exports = []
    for source in sources:
        if source.provenance.declaration_id not in by_source:
            continue
        exports.append(
            _lower_sync_export(
                module_root,
                source,
                backend=backend,
                module_name=module_name,
            )
        )
    return exports


def _lower_sync_object(
    module_root: Path,
    source: CppClassSource,
    *,
    module_name: str,
) -> ObjectExport:
    try:
        semantic = project_cpp_api((), (source,)).classes[0]
    except (CppProjectionError, SourceModelError, ValueError) as exc:
        raise CodegenError(str(exc)) from exc
    path = module_root / source.provenance.path
    if semantic.kind is SemanticClassKind.INTERNAL_SERVICE:
        raise _source_error(
            module_root,
            path,
            source.provenance.line,
            module_name,
            source.cpp_name,
            "SupernoteInternal class semantics were recognized, but the "
            "FeatureSession service route is not implemented yet",
        )
    selected = next(
        constructor
        for constructor in source.constructors
        if constructor.provenance.declaration_id
        == semantic.constructor.source.declaration_id
    )
    constructor_types = {
        parameter.type_spelling for parameter in selected.parameters
    }
    unsupported_constructor = sorted(
        constructor_types - (JSI_SYNC_LOWERING_TYPES - {"void"})
    )
    if unsupported_constructor:
        raise _source_error(
            module_root,
            path,
            selected.provenance.line,
            module_name,
            f"{source.cpp_name}.create",
            "constructor semantic types were recognized, but HostObject "
            "conversion is not implemented yet for "
            + ", ".join(unsupported_constructor),
        )
    methods: list[ObjectMethod] = []
    for method in source.methods:
        if method.intent.role is DeclarationRole.INTERNAL:
            raise _source_error(
                module_root,
                path,
                method.provenance.line,
                module_name,
                f"{source.cpp_name}.{method.cpp_name}",
                "SupernoteInternal object methods were recognized, but their "
                "receiver-aware internal route is not implemented yet",
            )
        if method.intent.execution is ExecutionMode.ASYNC:
            raise _source_error(
                module_root,
                path,
                method.provenance.line,
                module_name,
                f"{source.cpp_name}.{method.cpp_name}",
                "SupernoteAsync object methods were recognized, but async "
                "HostObject lowering is not implemented yet",
            )
        used_types = {method.return_type_spelling}
        used_types.update(
            parameter.type_spelling for parameter in method.parameters
        )
        unsupported = sorted(used_types - JSI_SYNC_LOWERING_TYPES)
        if unsupported:
            raise _source_error(
                module_root,
                path,
                method.provenance.line,
                module_name,
                f"{source.cpp_name}.{method.cpp_name}",
                "method semantic types were recognized, but HostObject "
                "conversion is not implemented yet for "
                + ", ".join(unsupported),
            )
        methods.append(
            ObjectMethod(
                line=method.provenance.line,
                cpp_name=method.cpp_name,
                js_name=method.cpp_name,
                return_type=method.return_type_spelling,
                parameters=tuple(
                    Parameter(parameter.type_spelling, parameter.name)
                    for parameter in method.parameters
                ),
                const=method.const,
                noexcept=method.noexcept,
            )
        )
    return ObjectExport(
        source=source.provenance.path,
        include=source.include,
        line=source.provenance.line,
        cpp_name=source.cpp_name,
        js_name=source.cpp_name,
        constructor=ObjectConstructor(
            tuple(
                Parameter(parameter.type_spelling, parameter.name)
                for parameter in selected.parameters
            )
        ),
        methods=tuple(methods),
    )


def scan_objects(
    module_root: Path,
    *,
    backend: str | None = None,
    module_name: str | None = None,
) -> list[ObjectExport]:
    source_root = module_root / "android/src/main/cpp"
    if not source_root.is_dir():
        raise CodegenError(f"missing C/C++ source directory: {source_root}")
    backend, module_name = _scan_context(module_root, backend, module_name)
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        for comment in lexed.comments:
            is_candidate, _ = _object_marker_name(comment)
            if not is_candidate:
                continue
            raise _source_error(
                module_root,
                path,
                comment.line,
                module_name,
                None,
                "SupernoteExportObject is removed in V2; mark the class with "
                "SupernoteExport and mark each generated method explicitly",
            )
    class_sources = scan_cpp_class_source_model(
        module_root,
        module_name=module_name,
    )
    public_classes = [
        source
        for source in class_sources
        if source.intent.role is DeclarationRole.EXPORTED
    ]
    if public_classes and backend != "jsi":
        first = public_classes[0]
        raise _source_error(
            module_root,
            module_root / first.provenance.path,
            first.provenance.line,
            module_name,
            first.cpp_name,
            "JavaScript-public C++ objects require the JSI frontend",
        )
    objects = [
        _lower_sync_object(
            module_root,
            source,
            module_name=module_name,
        )
        for source in class_sources
    ]
    objects.sort(key=lambda item: (item.source, item.line, item.js_name))
    cpp_names: dict[str, ObjectExport] = {}
    js_names: dict[str, ObjectExport] = {}
    for item in objects:
        if item.cpp_name in cpp_names:
            first = cpp_names[item.cpp_name]
            raise CodegenError(
                f"{item.source}:{item.line}: module {module_name!r}, export "
                f"{item.js_name!r}: duplicate exported C++ object name "
                f"{item.cpp_name!r}; first exported at "
                f"{first.source}:{first.line}"
            )
        if item.js_name in js_names:
            first = js_names[item.js_name]
            raise CodegenError(
                f"{item.source}:{item.line}: module {module_name!r}, export "
                f"{item.js_name!r}: duplicate JavaScript object export; "
                f"first exported at {first.source}:{first.line}"
            )
        cpp_names[item.cpp_name] = item
        js_names[item.js_name] = item
    return objects


def _validate_typescript_names(
    objects: list[ObjectExport],
    module_name: str,
) -> None:
    module_interface = f"{module_name}Module"
    generated_names: dict[str, tuple[ObjectExport, str]] = {}
    for item in objects:
        candidates = (
            (item.js_name, "object interface"),
            (f"{item.js_name}Factory", "factory interface"),
        )
        for generated_name, role in candidates:
            if generated_name == module_interface:
                raise CodegenError(
                    f"{item.source}:{item.line}: module {module_name!r}, "
                    f"export {item.js_name!r}: generated TypeScript name "
                    f"{generated_name!r} for the {role} collides with the "
                    f"generated module interface {module_interface!r}; rename "
                    "the source class"
                )
            previous = generated_names.get(generated_name)
            if previous is not None:
                first, first_role = previous
                raise CodegenError(
                    f"{item.source}:{item.line}: module {module_name!r}, "
                    f"export {item.js_name!r}: generated TypeScript name "
                    f"{generated_name!r} for the {role} conflicts with the "
                    f"{first_role} generated for object export "
                    f"{first.js_name!r} at {first.source}:{first.line}; rename "
                    "one source class"
                )
            generated_names[generated_name] = (item, role)


def scan_bindings(
    module_root: Path,
    *,
    backend: str | None = None,
    module_name: str | None = None,
) -> ScannedBindings:
    resolved_backend, resolved_name = _scan_context(
        module_root, backend, module_name
    )
    exports = scan_sources(
        module_root,
        backend=resolved_backend,
        module_name=resolved_name,
    )
    objects = scan_objects(
        module_root,
        backend=resolved_backend,
        module_name=resolved_name,
    )
    free_names = {export.js_name: export for export in exports}
    for item in objects:
        if item.js_name in free_names:
            first = free_names[item.js_name]
            raise CodegenError(
                f"{item.source}:{item.line}: module {resolved_name!r}, export "
                f"{item.js_name!r}: JavaScript name collides with free-function "
                f"export at {first.source}:{first.line}; rename the function "
                "or class in source"
            )
    _validate_typescript_names(objects, resolved_name)
    return ScannedBindings(tuple(exports), tuple(objects))


def _cpp_declarations(exports: list[Export]) -> str:
    declarations = []
    for export in exports:
        parameters = ", ".join(
            f"{parameter.cpp_type} {parameter.name}"
            for parameter in export.parameters
        )
        exception_specification = " noexcept" if export.noexcept else ""
        declarations.append(
            f"{export.return_type} {export.cpp_name}({parameters})"
            f"{exception_specification};"
        )
    return "\n".join(declarations)


def _typescript(
    config: dict[str, object],
    exports: list[Export],
    objects: list[ObjectExport],
) -> str:
    module_name = str(config["module_name"])
    backend = _normalize_backend(config["backend"])
    type_map = {
        "bool": "boolean",
        "int32_t": "number",
        "std::int32_t": "number",
        "int64_t": "bigint",
        "std::int64_t": "bigint",
        "float": "number",
        "double": "number",
        "std::string": "string",
        "std::vector<std::byte>": "Uint8Array",
        "void": "void",
    }
    methods = []
    for export in exports:
        parameters = ", ".join(
            f"{parameter.name}: {type_map[parameter.cpp_type]}"
            for parameter in export.parameters
        )
        result = type_map[export.return_type]
        if backend == "jni" and export.return_type != "void":
            result = f"Promise<{result}>"
        methods.append(f"  {export.js_name}({parameters}): {result};")
    object_interfaces: list[str] = []
    object_properties: list[str] = []
    for item in objects:
        object_methods: list[str] = []
        for method in item.methods:
            parameters = ", ".join(
                f"{parameter.name}: {type_map[parameter.cpp_type]}"
                for parameter in method.parameters
            )
            object_methods.append(
                f"  {method.js_name}({parameters}): "
                f"{type_map[method.return_type]};"
            )
        constructor_parameters = ", ".join(
            f"{parameter.name}: {type_map[parameter.cpp_type]}"
            for parameter in item.constructor.parameters
        )
        object_interfaces.append(
            f"export interface {item.js_name} {{\n"
            f"{chr(10).join(object_methods)}\n"
            "}\n\n"
            f"export interface {item.js_name}Factory {{\n"
            f"  create({constructor_parameters}): {item.js_name};\n"
            "}"
        )
        object_properties.append(
            f"  {item.js_name}: {item.js_name}Factory;"
        )
    body = "\n".join(object_properties + methods)
    interface_prefix = (
        "\n\n".join(object_interfaces) + "\n\n"
        if object_interfaces
        else ""
    )
    return (
        "/* Generated by supernote_module_generator. Do not edit. */\n"
        "export type SupernoteErrorCode =\n"
        "  | \"RESOURCE_EXHAUSTED\"\n"
        "  | \"CANCELLED\"\n"
        "  | \"FEATURE_CLOSED\"\n"
        "  | \"IMPLEMENTATION_ERROR\"\n"
        "  | \"INTERNAL\";\n\n"
        "export class SupernoteError extends Error {\n"
        "  readonly code: SupernoteErrorCode;\n"
        "}\n\n"
        f"{interface_prefix}"
        f"export interface {module_name}Module {{\n{body}\n}}\n\n"
        f"declare const {module_name}: {module_name}Module;\n"
        f"export default {module_name};\n"
    )


def _jni_descriptor(cpp_type: str) -> str:
    return {
        "bool": "Z",
        "double": "D",
        "std::string": "[B",
        "void": "V",
    }[cpp_type]


def _jni_type(cpp_type: str) -> str:
    return {
        "bool": "jboolean",
        "double": "jdouble",
        "std::string": "jbyteArray",
        "void": "void",
    }[cpp_type]


def _kotlin_type(cpp_type: str) -> str:
    return {
        "bool": "Boolean",
        "double": "Double",
        "std::string": "ByteArray",
        "void": "Unit",
    }[cpp_type]


def _kotlin_bridge(config: dict[str, object], exports: list[Export]) -> str:
    namespace = str(config["android_namespace"])
    module_name = str(config["module_name"])
    library = str(config["native_library_name"])
    methods: list[str] = []
    natives: list[str] = []
    for index, export in enumerate(exports):
        native_parameters = ", ".join(
            f"arg{number}: {_kotlin_type(parameter.cpp_type)}"
            for number, parameter in enumerate(export.parameters)
        )
        natives.append(
            f"  private external fun native{index}({native_parameters})"
            f": {_kotlin_type(export.return_type)}"
        )
        public_parameters = ", ".join(
            f"{parameter.name}: "
            + {
                "bool": "Boolean",
                "double": "Double",
                "std::string": "String",
            }[parameter.cpp_type]
            for parameter in export.parameters
        )
        arguments = ", ".join(
            (
                f"{parameter.name}.encodeToByteArray()"
                if parameter.cpp_type == "std::string"
                else parameter.name
            )
            for parameter in export.parameters
        )
        if export.return_type == "void":
            methods.append(
                "  @ReactMethod\n"
                f"  fun {export.js_name}({public_parameters}) {{\n"
                "    try {\n"
                f"      native{index}({arguments})\n"
                "    } catch (failure: Throwable) {\n"
                f'      Log.e(TAG, "{export.js_name} failed", failure)\n'
                "    }\n"
                "  }"
            )
        else:
            separator = ", " if public_parameters else ""
            conversion = (
                ".decodeToString()"
                if export.return_type == "std::string"
                else ""
            )
            methods.append(
                "  @ReactMethod\n"
                f"  fun {export.js_name}({public_parameters}{separator}"
                "promise: Promise) {\n"
                "    try {\n"
                f"      promise.resolve(native{index}({arguments}){conversion})\n"
                "    } catch (failure: Throwable) {\n"
                '      promise.reject("SUPERNOTE_NATIVE_ERROR", '
                "failure.message, failure)\n"
                "    }\n"
                "  }"
            )
    return f"""package {namespace}.generated

import android.util.Log
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod

class GeneratedNativeModule(
    context: ReactApplicationContext,
) : ReactContextBaseJavaModule(context) {{

  override fun getName(): String = {json.dumps(module_name)}

{chr(10).join(methods)}

{chr(10).join(natives)}

  private companion object {{
    const val TAG = "SupernoteNative{module_name}"
    init {{
      System.loadLibrary({json.dumps(library)})
    }}
  }}
}}
"""


def _jni_conversion(parameter: Parameter, number: int) -> tuple[str, str]:
    if parameter.cpp_type == "std::string":
        local = f"converted{number}"
        return (
            f"    const std::string {local} = read_utf8(env, arg{number});",
            local,
        )
    if parameter.cpp_type == "bool":
        return ("", f"arg{number} == JNI_TRUE")
    return ("", f"static_cast<double>(arg{number})")


def _jni_binding(config: dict[str, object], exports: list[Export]) -> str:
    namespace = str(config["android_namespace"])
    module_name = str(config["module_name"])
    class_path = namespace.replace(".", "/") + "/generated/GeneratedNativeModule"
    if not exports:
        return """#include <jni.h>

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *, void *) {
  return JNI_VERSION_1_6;
}
"""
    declarations = _cpp_declarations(exports)
    functions: list[str] = []
    registrations: list[str] = []
    for index, export in enumerate(exports):
        parameters = "".join(
            f", {_jni_type(parameter.cpp_type)} arg{number}"
            for number, parameter in enumerate(export.parameters)
        )
        conversions: list[str] = []
        arguments: list[str] = []
        for number, parameter in enumerate(export.parameters):
            conversion, argument = _jni_conversion(parameter, number)
            if conversion:
                conversions.append(conversion)
            arguments.append(argument)
        call = f"{export.cpp_name}({', '.join(arguments)})"
        if export.return_type == "std::string":
            success = f"    return write_utf8(env, {call});"
            fallback = "nullptr"
        elif export.return_type == "bool":
            success = f"    return {call} ? JNI_TRUE : JNI_FALSE;"
            fallback = "JNI_FALSE"
        elif export.return_type == "double":
            success = f"    return static_cast<jdouble>({call});"
            fallback = "0.0"
        else:
            success = f"    {call};\n    return;"
            fallback = ""
        fallback_line = f"  return {fallback};" if fallback else ""
        conversion_text = "\n".join(conversions)
        error_prefix = f"{module_name}.{export.js_name}: "
        functions.append(
            f"{_jni_type(export.return_type)} native_{index}("
            f"JNIEnv *env, jobject{parameters}) {{\n"
            "  try {\n"
            f"{conversion_text}{chr(10) if conversion_text else ''}"
            f"{success}\n"
            "  } catch (const std::exception &error) {\n"
            f"    throw_java(env, std::string({json.dumps(error_prefix)}) + "
            "error.what());\n"
            "  } catch (...) {\n"
            f"    throw_java(env, {json.dumps(error_prefix + 'unknown C++ exception')});\n"
            "  }\n"
            f"{fallback_line}\n"
            "}"
        )
        descriptor = (
            "("
            + "".join(_jni_descriptor(parameter.cpp_type) for parameter in export.parameters)
            + ")"
            + _jni_descriptor(export.return_type)
        )
        registrations.append(
            "      {const_cast<char *>(\"native"
            f"{index}\"), const_cast<char *>({json.dumps(descriptor)}), "
            f"reinterpret_cast<void *>(native_{index})}},"
        )
    return f"""#include <jni.h>

#include <cstddef>
#include <cstdint>
#include <exception>
#include <limits>
#include <stdexcept>
#include <string>

{declarations}

namespace {{

std::string read_utf8(JNIEnv *env, jbyteArray value) {{
  if (value == nullptr) {{
    throw std::invalid_argument("string argument is null");
  }}
  const jsize size = env->GetArrayLength(value);
  std::string result(static_cast<std::size_t>(size), '\\0');
  if (size > 0) {{
    env->GetByteArrayRegion(
        value, 0, size, reinterpret_cast<jbyte *>(result.data()));
    if (env->ExceptionCheck()) {{
      throw std::runtime_error("could not read UTF-8 argument");
    }}
  }}
  return result;
}}

jbyteArray write_utf8(JNIEnv *env, const std::string &value) {{
  auto result = env->NewByteArray(static_cast<jsize>(value.size()));
  if (result == nullptr) {{
    throw std::runtime_error("could not allocate UTF-8 result");
  }}
  if (!value.empty()) {{
    env->SetByteArrayRegion(
        result,
        0,
        static_cast<jsize>(value.size()),
        reinterpret_cast<const jbyte *>(value.data()));
    if (env->ExceptionCheck()) {{
      throw std::runtime_error("could not write UTF-8 result");
    }}
  }}
  return result;
}}

void throw_ascii_fallback(JNIEnv *env) noexcept {{
  if (env->ExceptionCheck()) {{
    env->ExceptionClear();
  }}
  jclass error_class = env->FindClass("java/lang/RuntimeException");
  if (error_class != nullptr) {{
    env->ThrowNew(
        error_class, "Generated native module failed; inspect the existing plugin logs");
  }}
}}

void throw_java(JNIEnv *env, const std::string &message) noexcept {{
  if (env->ExceptionCheck()) {{
    env->ExceptionClear();
  }}
  if (message.size() >
      static_cast<std::size_t>(std::numeric_limits<jsize>::max())) {{
    throw_ascii_fallback(env);
    return;
  }}

  jbyteArray message_bytes =
      env->NewByteArray(static_cast<jsize>(message.size()));
  if (message_bytes == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  if (!message.empty()) {{
    env->SetByteArrayRegion(
        message_bytes,
        0,
        static_cast<jsize>(message.size()),
        reinterpret_cast<const jbyte *>(message.data()));
    if (env->ExceptionCheck()) {{
      throw_ascii_fallback(env);
      return;
    }}
  }}

  jclass standards_class =
      env->FindClass("java/nio/charset/StandardCharsets");
  if (standards_class == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  jfieldID utf8_field = env->GetStaticFieldID(
      standards_class, "UTF_8", "Ljava/nio/charset/Charset;");
  if (utf8_field == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  jobject utf8 = env->GetStaticObjectField(standards_class, utf8_field);
  if (utf8 == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}

  jclass string_class = env->FindClass("java/lang/String");
  jmethodID string_constructor = string_class == nullptr
      ? nullptr
      : env->GetMethodID(
            string_class,
            "<init>",
            "([BLjava/nio/charset/Charset;)V");
  if (string_constructor == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  jstring java_message = static_cast<jstring>(
      env->NewObject(string_class, string_constructor, message_bytes, utf8));
  if (java_message == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}

  jclass error_class = env->FindClass("java/lang/RuntimeException");
  jmethodID error_constructor = error_class == nullptr
      ? nullptr
      : env->GetMethodID(
            error_class, "<init>", "(Ljava/lang/String;)V");
  if (error_constructor == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  jthrowable exception = static_cast<jthrowable>(
      env->NewObject(error_class, error_constructor, java_message));
  if (exception == nullptr || env->ExceptionCheck()) {{
    throw_ascii_fallback(env);
    return;
  }}
  if (env->Throw(exception) != JNI_OK) {{
    throw_ascii_fallback(env);
  }}
}}

{chr(10).join(functions)}

}}  // namespace

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *java_vm, void *) {{
  JNIEnv *env = nullptr;
  if (java_vm->GetEnv(
          reinterpret_cast<void **>(&env), JNI_VERSION_1_6) != JNI_OK ||
      env == nullptr) {{
    return JNI_ERR;
  }}
  jclass module_class = env->FindClass({json.dumps(class_path)});
  if (module_class == nullptr) {{
    return JNI_ERR;
  }}
  JNINativeMethod methods[] = {{
{chr(10).join(registrations)}
  }};
  constexpr jint method_count =
      static_cast<jint>(sizeof(methods) / sizeof(methods[0]));
  return env->RegisterNatives(module_class, methods, method_count) == JNI_OK
      ? JNI_VERSION_1_6
      : JNI_ERR;
}}
"""


def _jsi_argument(parameter: Parameter, number: int) -> str:
    if parameter.cpp_type == "bool":
        return f"arguments[{number}].getBool()"
    if parameter.cpp_type in {"int32_t", "std::int32_t"}:
        return (
            f"static_cast<{parameter.cpp_type}>("
            f"arguments[{number}].asNumber())"
        )
    if parameter.cpp_type in {"int64_t", "std::int64_t"}:
        return (
            f"static_cast<{parameter.cpp_type}>(arguments[{number}]"
            ".asBigInt(runtime).asInt64(runtime))"
        )
    if parameter.cpp_type == "float":
        return f"static_cast<float>(arguments[{number}].asNumber())"
    if parameter.cpp_type == "double":
        return f"arguments[{number}].asNumber()"
    if parameter.cpp_type == "std::string":
        return f"arguments[{number}].asString(runtime).utf8(runtime)"
    if parameter.cpp_type == "std::vector<std::byte>":
        return f"supernote_copy_uint8_array(runtime, arguments[{number}])"
    raise AssertionError(f"unsupported JSI parameter type {parameter.cpp_type!r}")


def _jsi_expected_type(cpp_type: str) -> str:
    return {
        "bool": "boolean",
        "int32_t": "number",
        "std::int32_t": "number",
        "int64_t": "bigint",
        "std::int64_t": "bigint",
        "float": "number",
        "double": "number",
        "std::string": "string",
        "std::vector<std::byte>": "Uint8Array",
    }[cpp_type]


def _jsi_type_check(parameter: Parameter, number: int) -> str:
    if parameter.cpp_type == "std::vector<std::byte>":
        return f"!supernote_is_uint8_array(runtime, arguments[{number}])"
    method = {
        "bool": "isBool()",
        "int32_t": "isNumber()",
        "std::int32_t": "isNumber()",
        "int64_t": "isBigInt()",
        "std::int64_t": "isBigInt()",
        "float": "isNumber()",
        "double": "isNumber()",
        "std::string": "isString()",
    }[parameter.cpp_type]
    return f"!arguments[{number}].{method}"


def _jsi_range_validation(
    parameter: Parameter,
    number: int,
    *,
    diagnostic_name: str,
    indent: str,
) -> list[str]:
    argument_name = f"supernote_argument_{number}"
    prefix = (
        f"{diagnostic_name}: argument {number + 1} ({parameter.name}) "
    )
    if parameter.cpp_type in {"int32_t", "std::int32_t"}:
        return [
            f"{indent}const double {argument_name} = arguments[{number}].asNumber();",
            f"{indent}if (!std::isfinite({argument_name}) ||",
            f"{indent}    std::trunc({argument_name}) != {argument_name} ||",
            f"{indent}    {argument_name} < static_cast<double>(",
            f"{indent}        std::numeric_limits<std::int32_t>::min()) ||",
            f"{indent}    {argument_name} > static_cast<double>(",
            f"{indent}        std::numeric_limits<std::int32_t>::max())) {{",
            f"{indent}  supernote_throw_range_error(",
            f"{indent}      runtime, {json.dumps(prefix + 'must be a signed 32-bit integer')});",
            f"{indent}}}",
        ]
    if parameter.cpp_type in {"int64_t", "std::int64_t"}:
        return [
            f"{indent}if (!arguments[{number}].getBigInt(runtime).isInt64(runtime)) {{",
            f"{indent}  supernote_throw_range_error(",
            f"{indent}      runtime, {json.dumps(prefix + 'must fit in a signed 64-bit integer')});",
            f"{indent}}}",
        ]
    if parameter.cpp_type == "float":
        return [
            f"{indent}const double {argument_name} = arguments[{number}].asNumber();",
            f"{indent}if (std::isfinite({argument_name}) &&",
            f"{indent}    ({argument_name} < static_cast<double>(",
            f"{indent}         std::numeric_limits<float>::lowest()) ||",
            f"{indent}     {argument_name} > static_cast<double>(",
            f"{indent}         std::numeric_limits<float>::max()))) {{",
            f"{indent}  supernote_throw_range_error(",
            f"{indent}      runtime, {json.dumps(prefix + 'must fit in a 32-bit float')});",
            f"{indent}}}",
        ]
    return []


def _jsi_result_lines(call: str, return_type: str, indent: str) -> list[str]:
    if return_type == "void":
        return [f"{indent}{call};", f"{indent}return Value::undefined();"]
    if return_type == "std::string":
        return [
            f"{indent}const auto result = {call};",
            f"{indent}return Value(String::createFromUtf8(runtime, result));",
        ]
    if return_type in {"int64_t", "std::int64_t"}:
        return [
            f"{indent}return Value(facebook::jsi::BigInt::fromInt64(",
            f"{indent}    runtime, static_cast<std::int64_t>({call})));",
        ]
    if return_type == "std::vector<std::byte>":
        return [
            f"{indent}return supernote_make_uint8_array(runtime, {call});",
        ]
    if return_type in {
        "int32_t",
        "std::int32_t",
        "float",
        "double",
    }:
        return [f"{indent}return Value(static_cast<double>({call}));"]
    return [f"{indent}return Value({call});"]


def _jsi_value_helpers() -> str:
    return r'''[[noreturn]] void supernote_throw_builtin_error(
    facebook::jsi::Runtime &runtime,
    const char *constructor_name,
    const std::string &message) {
  auto constructor =
      runtime.global().getPropertyAsFunction(runtime, constructor_name);
  const facebook::jsi::Value argument(
      facebook::jsi::String::createFromUtf8(runtime, message));
  auto error = constructor.callAsConstructor(
      runtime, &argument, static_cast<std::size_t>(1));
  throw facebook::jsi::JSError(runtime, std::move(error));
}

[[noreturn]] void supernote_throw_type_error(
    facebook::jsi::Runtime &runtime,
    const std::string &message) {
  supernote_throw_builtin_error(runtime, "TypeError", message);
}

[[noreturn]] void supernote_throw_range_error(
    facebook::jsi::Runtime &runtime,
    const std::string &message) {
  supernote_throw_builtin_error(runtime, "RangeError", message);
}

[[noreturn]] void supernote_throw_error(
    facebook::jsi::Runtime &runtime,
    const char *code,
    const std::string &message) {
  auto exports = runtime.global().getPropertyAsObject(runtime, kGlobalName);
  auto constructor = exports.getPropertyAsFunction(
      runtime, "__supernoteErrorConstructor");
  const facebook::jsi::Value arguments[] = {
      facebook::jsi::Value(
          facebook::jsi::String::createFromAscii(runtime, code)),
      facebook::jsi::Value(
          facebook::jsi::String::createFromUtf8(runtime, message)),
  };
  auto error = constructor.callAsConstructor(
      runtime, arguments, static_cast<std::size_t>(2));
  throw facebook::jsi::JSError(runtime, std::move(error));
}

facebook::jsi::Function supernote_uint8_array_constructor(
    facebook::jsi::Runtime &runtime) {
  return runtime.global().getPropertyAsFunction(runtime, "Uint8Array");
}

bool supernote_is_uint8_array(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value) {
  if (!value.isObject()) {
    return false;
  }
  auto constructor = supernote_uint8_array_constructor(runtime);
  return value.getObject(runtime).instanceOf(runtime, constructor);
}

std::size_t supernote_view_index(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Object &view,
    const char *property) {
  auto value = view.getProperty(runtime, property);
  if (!value.isNumber()) {
    throw facebook::jsi::JSError(
        runtime, std::string("Uint8Array.") + property + " is not numeric");
  }
  const double number = value.asNumber();
  if (!std::isfinite(number) || std::trunc(number) != number || number < 0 ||
      number > static_cast<double>(std::numeric_limits<std::size_t>::max())) {
    throw facebook::jsi::JSError(
        runtime, std::string("Uint8Array.") + property + " is invalid");
  }
  return static_cast<std::size_t>(number);
}

std::vector<std::byte> supernote_copy_uint8_array(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value) {
  auto view = value.getObject(runtime);
  const std::size_t offset =
      supernote_view_index(runtime, view, "byteOffset");
  const std::size_t length =
      supernote_view_index(runtime, view, "byteLength");
  auto buffer_object = view.getPropertyAsObject(runtime, "buffer");
  if (!buffer_object.isArrayBuffer(runtime)) {
    throw facebook::jsi::JSError(
        runtime, "Uint8Array.buffer is not an ArrayBuffer");
  }
  auto buffer = buffer_object.getArrayBuffer(runtime);
  const std::size_t buffer_size = buffer.size(runtime);
  if (offset > buffer_size || length > buffer_size - offset) {
    throw facebook::jsi::JSError(
        runtime, "Uint8Array view exceeds its ArrayBuffer");
  }
  std::vector<std::byte> result(length);
  if (length != 0) {
    std::memcpy(result.data(), buffer.data(runtime) + offset, length);
  }
  return result;
}

class SupernoteOwnedBytesBuffer final : public facebook::jsi::MutableBuffer {
 public:
  explicit SupernoteOwnedBytesBuffer(const std::vector<std::byte> &value)
      : bytes_(value.size()) {
    if (!value.empty()) {
      std::memcpy(bytes_.data(), value.data(), value.size());
    }
  }

  std::size_t size() const override {
    return bytes_.size();
  }

  std::uint8_t *data() override {
    return bytes_.data();
  }

 private:
  std::vector<std::uint8_t> bytes_;
};

facebook::jsi::Value supernote_make_uint8_array(
    facebook::jsi::Runtime &runtime,
    const std::vector<std::byte> &value) {
  auto storage = std::make_shared<SupernoteOwnedBytesBuffer>(value);
  const facebook::jsi::Value argument(
      facebook::jsi::ArrayBuffer(runtime, std::move(storage)));
  auto constructor = supernote_uint8_array_constructor(runtime);
  return constructor.callAsConstructor(
      runtime, &argument, static_cast<std::size_t>(1));
}'''


def _jsi_object_callable_body(
    *,
    parameters: tuple[Parameter, ...],
    diagnostic_name: str,
    call: str,
    return_type: str,
    indent: str,
) -> str:
    expected_parameters = ", ".join(
        _jsi_expected_type(parameter.cpp_type)
        + f" {parameter.name}"
        for parameter in parameters
    )
    expected_description = (
        f"{len(parameters)} argument"
        f"{'' if len(parameters) == 1 else 's'}"
        f" ({expected_parameters})"
    )
    count_prefix = (
        f"{diagnostic_name}: expected {expected_description}; received "
    )
    lines = [
        f"{indent}if (argument_count != {len(parameters)}) {{",
        f"{indent}  supernote_throw_type_error(",
        f"{indent}      runtime, std::string({json.dumps(count_prefix)}) +",
        f"{indent}      std::to_string(argument_count));",
        f"{indent}}}",
    ]
    for number, parameter in enumerate(parameters):
        js_type = _jsi_expected_type(parameter.cpp_type)
        type_error = (
            f"{diagnostic_name}: argument {number + 1} ({parameter.name}) "
            f"must be a {js_type}; expected {expected_description}"
        )
        lines.extend(
            [
                f"{indent}if ({_jsi_type_check(parameter, number)}) {{",
                f"{indent}  supernote_throw_type_error(",
                f"{indent}      runtime, {json.dumps(type_error)});",
                f"{indent}}}",
            ]
        )
        lines.extend(
            _jsi_range_validation(
                parameter,
                number,
                diagnostic_name=diagnostic_name,
                indent=indent,
            )
        )
    result = _jsi_result_lines(call, return_type, f"{indent}  ")
    lines.extend([f"{indent}try {{", *result])
    lines.extend(
        [
            f"{indent}}} catch (const facebook::jsi::JSError &error) {{",
            f"{indent}  __android_log_print(",
            f"{indent}      ANDROID_LOG_ERROR, kLogTag,",
            f"{indent}      {json.dumps(diagnostic_name + ': generated JSI conversion failed: %s')},",
            f"{indent}      error.what());",
            f"{indent}  supernote_throw_error(",
            f"{indent}      runtime, \"INTERNAL\",",
            f"{indent}      {json.dumps(diagnostic_name + ': generated binding conversion failed')});",
            f"{indent}}} catch (const std::exception &error) {{",
            f"{indent}  supernote_throw_error(",
            f"{indent}      runtime, \"IMPLEMENTATION_ERROR\", std::string("
            f"{json.dumps(diagnostic_name + ': ')}) + error.what());",
            f"{indent}}} catch (...) {{",
            f"{indent}  supernote_throw_error(",
            f"{indent}      runtime, \"IMPLEMENTATION_ERROR\", "
            f"{json.dumps(diagnostic_name + ': unknown C++ exception')});",
            f"{indent}}}",
        ]
    )
    return "\n".join(lines)


def _jsi_object_wrapper(
    module_name: str,
    item: ObjectExport,
    index: int,
) -> str:
    class_name = f"GeneratedObject{index}HostObject"
    method_branches: list[str] = []
    for method in item.methods:
        arguments = ", ".join(
            _jsi_argument(parameter, number)
            for number, parameter in enumerate(method.parameters)
        )
        call = f"native_instance->{method.cpp_name}({arguments})"
        body = _jsi_object_callable_body(
            parameters=method.parameters,
            diagnostic_name=f"{module_name}.{item.js_name}.{method.js_name}",
            call=call,
            return_type=method.return_type,
            indent="          ",
        )
        arguments_parameter = (
            "const Value *arguments"
            if method.parameters
            else "const Value *"
        )
        method_branches.append(
            f"    if (property_name == {json.dumps(method.js_name)}) {{\n"
            f"      std::shared_ptr<{item.cpp_name}> native_instance = instance_;\n"
            "      return Function::createFromHostFunction(\n"
            "          runtime,\n"
            f"          PropNameID::forAscii(runtime, "
            f"{json.dumps(method.js_name)}),\n"
            f"          {len(method.parameters)},\n"
            "          [native_instance = std::move(native_instance)](\n"
            "              facebook::jsi::Runtime &runtime,\n"
            "              const Value &,\n"
            f"              {arguments_parameter},\n"
            "              std::size_t argument_count) -> Value {\n"
            f"{body}\n"
            "          });\n"
            "    }"
        )
    property_names = "\n".join(
        "    properties.push_back(facebook::jsi::PropNameID::forAscii("
        f"runtime, {json.dumps(method.js_name)}));"
        for method in item.methods
    )
    if not property_names:
        property_names = "    (void)runtime;"
    return f"""class {class_name} final : public facebook::jsi::HostObject {{
 public:
  explicit {class_name}(std::shared_ptr<{item.cpp_name}> instance)
      : instance_(std::move(instance)) {{}}

  facebook::jsi::Value get(
      facebook::jsi::Runtime &runtime,
      const facebook::jsi::PropNameID &name) override {{
    using facebook::jsi::Function;
    using facebook::jsi::PropNameID;
    using facebook::jsi::String;
    using facebook::jsi::Value;
    const std::string property_name = name.utf8(runtime);
{chr(10).join(method_branches)}
    return Value::undefined();
  }}

  std::vector<facebook::jsi::PropNameID> getPropertyNames(
      facebook::jsi::Runtime &runtime) override {{
    std::vector<facebook::jsi::PropNameID> properties;
    properties.reserve({len(item.methods)});
{property_names}
    return properties;
  }}

 private:
  std::shared_ptr<{item.cpp_name}> instance_;
}};"""


def _jsi_object_registration(
    module_name: str,
    item: ObjectExport,
    index: int,
) -> str:
    arguments = ", ".join(
        _jsi_argument(parameter, number)
        for number, parameter in enumerate(item.constructor.parameters)
    )
    native_call = f"std::make_shared<{item.cpp_name}>({arguments})"
    callable_body = _jsi_object_callable_body(
        parameters=item.constructor.parameters,
        diagnostic_name=f"{module_name}.{item.js_name}.create",
        call=native_call,
        return_type="__object_factory__",
        indent="          ",
    )
    arguments_parameter = (
        "const Value *arguments"
        if item.constructor.parameters
        else "const Value *"
    )
    factory_result = f"return Value({native_call});"
    callable_body = callable_body.replace(
        factory_result,
        f"auto native_instance = {native_call};\n"
        "            return Value(Object::createFromHostObject(\n"
        "                runtime,\n"
        f"                std::make_shared<GeneratedObject{index}HostObject>(\n"
        "                    std::move(native_instance))));",
    )
    return (
        "  {\n"
        "    Object object_type(runtime);\n"
        "    auto create = Function::createFromHostFunction(\n"
        "        runtime,\n"
        "        PropNameID::forAscii(runtime, \"create\"),\n"
        f"        {len(item.constructor.parameters)},\n"
        "        [](facebook::jsi::Runtime &runtime,\n"
        "           const Value &,\n"
        f"           {arguments_parameter},\n"
        "           std::size_t argument_count) -> Value {\n"
        f"{callable_body}\n"
        "        });\n"
        "    object_type.setProperty(runtime, \"create\", std::move(create));\n"
        f"    exports.setProperty(runtime, {json.dumps(item.js_name)}, "
        "std::move(object_type));\n"
        "  }"
    )


def _jsi_binding(
    config: dict[str, object],
    exports: list[Export],
    objects: list[ObjectExport],
) -> str:
    namespace = str(config["android_namespace"])
    module_name = str(config["module_name"])
    class_prefix = str(config["class_prefix"])
    installer = f"{namespace}.generated.{class_prefix}JsiModule"
    global_name = str(config["jsi_global_name"])
    declarations = _cpp_declarations(exports)
    object_includes = "\n".join(
        f'#include "{include}"'
        for include in dict.fromkeys(item.include for item in objects)
    )
    object_include_block = f"{object_includes}\n\n" if object_includes else ""
    object_wrappers = "\n\n".join(
        _jsi_object_wrapper(module_name, item, index)
        for index, item in enumerate(objects)
    )
    object_wrapper_block = f"{object_wrappers}\n\n" if object_wrappers else ""
    registrations: list[str] = []
    for export in exports:
        expected_parameters = ", ".join(
            _jsi_expected_type(parameter.cpp_type)
            + f" {parameter.name}"
            for parameter in export.parameters
        )
        expected_description = (
            f"{len(export.parameters)} argument"
            f"{'' if len(export.parameters) == 1 else 's'}"
            f" ({expected_parameters})"
        )
        count_prefix = (
            f"{module_name}.{export.js_name}: expected "
            f"{expected_description}; received "
        )
        type_checks: list[str] = []
        for number, parameter in enumerate(export.parameters):
            js_type = _jsi_expected_type(parameter.cpp_type)
            type_error = (
                f"{module_name}.{export.js_name}: argument {number + 1} "
                f"({parameter.name}) must be a {js_type}; expected "
                f"{expected_description}"
            )
            type_checks.append(
                f"          if ({_jsi_type_check(parameter, number)}) {{\n"
                "            supernote_throw_type_error(\n"
                f"                runtime, {json.dumps(type_error)});\n"
                "          }"
            )
            type_checks.extend(
                _jsi_range_validation(
                    parameter,
                    number,
                    diagnostic_name=f"{module_name}.{export.js_name}",
                    indent="          ",
                )
            )
        arguments = ", ".join(
            _jsi_argument(parameter, number)
            for number, parameter in enumerate(export.parameters)
        )
        call = f"{export.cpp_name}({arguments})"
        result = "\n".join(_jsi_result_lines(call, export.return_type, "        "))
        registrations.append(
            "  {\n"
            "    auto function = Function::createFromHostFunction(\n"
            "        runtime,\n"
            f"        PropNameID::forAscii(runtime, {json.dumps(export.js_name)}),\n"
            f"        {len(export.parameters)},\n"
            "        [](facebook::jsi::Runtime &runtime,\n"
            "           const Value &,\n"
            "           const Value *arguments,\n"
            "           std::size_t argument_count) -> Value {\n"
            f"          if (argument_count != {len(export.parameters)}) {{\n"
            "            supernote_throw_type_error(\n"
            f"                runtime, std::string({json.dumps(count_prefix)}) + "
            "std::to_string(argument_count));\n"
            "          }\n"
            f"{chr(10).join(type_checks)}"
            f"{chr(10) if type_checks else ''}"
            "          try {\n"
            f"{result}\n"
            "          } catch (const facebook::jsi::JSError &error) {\n"
            "            __android_log_print(\n"
            "                ANDROID_LOG_ERROR, kLogTag,\n"
            f"                {json.dumps(module_name + '.' + export.js_name + ': generated JSI conversion failed: %s')},\n"
            "                error.what());\n"
            "            supernote_throw_error(\n"
            "                runtime, \"INTERNAL\",\n"
            f"                {json.dumps(module_name + '.' + export.js_name + ': generated binding conversion failed')});\n"
            "          } catch (const std::exception &error) {\n"
            "            supernote_throw_error(\n"
            "                runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"                std::string({json.dumps(module_name + '.' + export.js_name + ': ')}) + error.what());\n"
            "          } catch (...) {\n"
            "            supernote_throw_error(\n"
            "                runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"                {json.dumps(module_name + '.' + export.js_name + ': unknown C++ exception')});\n"
            "          }\n"
            "        });\n"
            f"    exports.setProperty(runtime, {json.dumps(export.js_name)}, "
            "std::move(function));\n"
            "  }"
        )
    registrations.extend(
        _jsi_object_registration(module_name, item, index)
        for index, item in enumerate(objects)
    )
    return f"""#include <jni.h>
#include <jsi/jsi.h>

#include <android/log.h>

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>
{object_include_block}{declarations}

namespace {{

constexpr char kLogTag[] = "SupernoteJsi{class_prefix}";
constexpr char kInstallerClassName[] = {json.dumps(installer)};
constexpr char kGlobalName[] = {json.dumps(global_name)};

{_jsi_value_helpers()}

{object_wrapper_block}void clear_pending_exception(JNIEnv *env, const char *operation) {{
  if (!env->ExceptionCheck()) {{
    return;
  }}
  __android_log_print(
      ANDROID_LOG_ERROR, kLogTag, "%s raised a Java exception", operation);
  env->ExceptionDescribe();
  env->ExceptionClear();
}}

void install_exports(facebook::jsi::Runtime &runtime) {{
  using facebook::jsi::Function;
  using facebook::jsi::Object;
  using facebook::jsi::PropNameID;
  using facebook::jsi::String;
  using facebook::jsi::Value;

  Object exports(runtime);
{chr(10).join(registrations)}
  runtime.global().setProperty(runtime, kGlobalName, std::move(exports));
}}

jboolean native_install(JNIEnv *, jobject, jlong runtime_pointer) {{
  if (runtime_pointer == 0) {{
    return JNI_FALSE;
  }}
  auto *runtime = reinterpret_cast<facebook::jsi::Runtime *>(
      static_cast<std::uintptr_t>(runtime_pointer));
  try {{
    install_exports(*runtime);
    return JNI_TRUE;
  }} catch (const std::exception &error) {{
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag, "JSI installation failed: %s", error.what());
    return JNI_FALSE;
  }} catch (...) {{
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag, "JSI installation failed");
    return JNI_FALSE;
  }}
}}

bool register_installer_natives(JNIEnv *env) {{
  jclass thread_class = env->FindClass("java/lang/Thread");
  if (thread_class == nullptr) {{
    clear_pending_exception(env, "FindClass(Thread)");
    return false;
  }}
  jmethodID current_thread =
      env->GetStaticMethodID(thread_class, "currentThread", "()Ljava/lang/Thread;");
  jmethodID get_loader = env->GetMethodID(
      thread_class, "getContextClassLoader", "()Ljava/lang/ClassLoader;");
  if (current_thread == nullptr || get_loader == nullptr) {{
    clear_pending_exception(env, "resolve Thread methods");
    return false;
  }}
  jobject thread = env->CallStaticObjectMethod(thread_class, current_thread);
  jobject loader = env->CallObjectMethod(thread, get_loader);
  if (env->ExceptionCheck() || loader == nullptr) {{
    clear_pending_exception(env, "get context classloader");
    return false;
  }}
  jclass loader_class = env->FindClass("java/lang/ClassLoader");
  jmethodID load_class = loader_class == nullptr
      ? nullptr
      : env->GetMethodID(
            loader_class,
            "loadClass",
            "(Ljava/lang/String;)Ljava/lang/Class;");
  if (load_class == nullptr) {{
    clear_pending_exception(env, "resolve ClassLoader.loadClass");
    return false;
  }}
  jstring class_name = env->NewStringUTF(kInstallerClassName);
  jobject installer =
      env->CallObjectMethod(loader, load_class, class_name);
  if (env->ExceptionCheck() || installer == nullptr) {{
    clear_pending_exception(env, "load plugin installer");
    return false;
  }}
  JNINativeMethod methods[] = {{
      {{const_cast<char *>("nativeInstall"),
        const_cast<char *>("(J)Z"),
        reinterpret_cast<void *>(native_install)}},
  }};
  return env->RegisterNatives(
             static_cast<jclass>(installer), methods, 1) == JNI_OK;
}}

}}  // namespace

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *java_vm, void *) {{
  JNIEnv *env = nullptr;
  if (java_vm->GetEnv(
          reinterpret_cast<void **>(&env), JNI_VERSION_1_6) != JNI_OK ||
      env == nullptr) {{
    return JNI_ERR;
  }}
  return register_installer_natives(env) ? JNI_VERSION_1_6 : JNI_ERR;
}}
"""


def _contents(
    module_root: Path,
    config: dict[str, object],
    exports: list[Export],
    objects: list[ObjectExport],
) -> dict[Path, str]:
    generated = module_root / "android/build/generated/supernote"
    backend = _normalize_backend(config["backend"])
    manifest = {
        "backend": backend,
        "exports": [export.manifest() for export in exports],
        "objects": [item.manifest() for item in objects],
    }
    result = {
        generated / "exports.json":
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        module_root / "index.d.ts": _typescript(config, exports, objects),
    }
    if backend == "jni":
        package_path = str(config["android_namespace"]).replace(".", "/")
        result[
            generated
            / f"java/{package_path}/generated/GeneratedNativeModule.kt"
        ] = _kotlin_bridge(config, exports)
        result[generated / "jni/generated_bindings.cpp"] = _jni_binding(
            config, exports
        )
    elif backend == "jsi":
        result[generated / "jni/generated_bindings.cpp"] = _jsi_binding(
            config, exports, objects
        )
    else:
        raise CodegenError(
            f"binding codegen does not support backend {config['backend']!r}"
        )
    return result


def generate(module_root: Path, *, check: bool = False) -> list[Export]:
    module_root = module_root.expanduser().resolve()
    config_path = module_root / "android/.supernote-module/codegen-config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CodegenError(f"could not read {config_path}: {exc}") from exc
    config = dict(config)
    config["backend"] = _normalize_backend(config.get("backend"))
    bindings = scan_bindings(
        module_root,
        backend=str(config["backend"]),
        module_name=str(config.get("module_name", module_root.name)),
    )
    exports = list(bindings.exports)
    objects = list(bindings.objects)
    outputs = _contents(module_root, config, exports, objects)
    if check:
        stale = [
            str(path)
            for path, expected in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != expected
        ]
        if stale:
            raise CodegenError(
                "generated bindings are stale; rebuild the Android module:\n  "
                + "\n  ".join(stale)
            )
        return exports
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
    return exports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        exports = generate(args.module_root, check=args.check)
        manifest_path = (
            args.module_root.expanduser().resolve()
            / "android/build/generated/supernote/exports.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        object_count = len(manifest["objects"])
    except (CodegenError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Generated {len(exports)} free-function exports and "
        f"{object_count} native-object exports for Supernote "
        f"{args.module_root.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
