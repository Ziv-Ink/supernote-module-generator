#!/usr/bin/env python3
"""Generate JNI or JSI bindings from // @SupernoteExport C++ functions."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


ANNOTATION = re.compile(
    r"@SupernoteExport"
    r"(?:\(\s*name\s*=\s*\"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\"\s*\))?"
)
CPP_SUFFIXES = {".cc", ".cpp", ".cxx"}
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
SUPPORTED_PARAMETER_TYPES = {"bool", "double", "std::string"}
SUPPORTED_RETURN_TYPES = SUPPORTED_PARAMETER_TYPES | {"void"}


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


def _marker_name(comment: _LineComment) -> tuple[bool, str | None]:
    value = comment.text.strip()
    match = ANNOTATION.fullmatch(value)
    if match:
        return True, match.group("name")
    is_candidate = bool(
        re.match(r"@SupernoteExport(?:\b|\()", value)
    )
    return is_candidate, None


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)


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
        f"argument {argument_index} must be a named bool, double, or "
        "std::string value, for example 'double value'"
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

    cpp_type: str | None = None
    name: str | None = None
    if (
        len(tokens) == 2
        and tokens[0].value in {"bool", "double"}
        and tokens[1].kind == "identifier"
    ):
        cpp_type = tokens[0].value
        name = tokens[1].value
    elif (
        len(tokens) == 4
        and [token.value for token in tokens[:3]] == ["std", "::", "string"]
        and tokens[3].kind == "identifier"
    ):
        cpp_type = "std::string"
        name = tokens[3].value
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


def _parse_export(
    *,
    module_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    marker: _LineComment,
    renamed: str | None,
    next_marker_start: int | None,
    backend: str,
    module_name: str,
) -> Export:
    marker_export = renamed or "<pending>"
    if not marker.line_only:
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "the export marker must be a // comment on its own line; use "
            "// @SupernoteExport",
        )
    if marker.conditional_depth:
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "export markers are not allowed inside #if, #ifdef, or #ifndef "
            "blocks; move the complete exported definition outside the "
            "preprocessor conditional",
        )
    if marker.brace_depth:
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "export must be a global, top-level C++ free function; namespaces, "
            "classes, and function-local exports are not supported",
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
            module_root,
            path,
            prefix[0].line,
            module_name,
            marker_export,
            "unsupported declaration prefix before the marker "
            f"{_tokens_text(prefix)!r}; place // @SupernoteExport immediately "
            "before an unmodified function return type and remove static, "
            "template, extern \"C\", attributes, or macros",
        )

    following = [token for token in active_tokens if token.start >= marker.end]
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
            "tag must be followed by a supported top-level function definition; "
            "expected 'double name(double value) {'",
        )
    first = following[0]
    intervening_directive = next(
        (
            directive
            for directive in lexed.directives
            if marker.end <= directive.start < first.start
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
            "a preprocessor directive cannot occur between an export marker "
            "and its function definition",
        )
    between = text[marker.end:first.start]
    if between.strip():
        raise _source_error(
            module_root,
            path,
            marker.line,
            module_name,
            marker_export,
            "only whitespace may appear between // @SupernoteExport and the "
            "function return type",
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
            f"modifier {first_value!r} is forbidden; exported functions must "
            "have ordinary external C++ linkage with no modifiers",
        )

    return_type: str | None = None
    if first_value in {"bool", "double", "void"}:
        return_type = first_value
        cursor += 1
    elif (
        len(following) >= 3
        and [token.value for token in following[:3]] == ["std", "::", "string"]
    ):
        return_type = "std::string"
        cursor = 3
    else:
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
            f"{description} {first_value!r}; expected a bool, double, "
            "std::string, or void return type followed by a function name",
        )

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
    js_name = renamed or cpp_name
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
            if marker.end <= item.start < opening.start
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
            "preprocessor directives are not supported inside an exported "
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

    if backend == "jni":
        if js_name in JNI_RESERVED_IDENTIFIERS:
            raise _source_error(
                module_root,
                path,
                marker.line,
                module_name,
                js_name,
                f"JavaScript export name {js_name!r} is reserved by "
                "Kotlin/Java; choose a different @SupernoteExport name",
            )
        if (
            js_name in GENERATED_KOTLIN_METHOD_NAMES
            or re.fullmatch(r"native[0-9]+", js_name)
        ):
            raise _source_error(
                module_root,
                path,
                marker.line,
                module_name,
                js_name,
                f"JavaScript export name {js_name!r} collides with a generated "
                "Kotlin method; choose a different @SupernoteExport name",
            )
        for argument_index, parameter in enumerate(parameters, start=1):
            if parameter.name in JNI_RESERVED_IDENTIFIERS:
                token = groups[argument_index - 1][-1]
                raise _source_error(
                    module_root,
                    path,
                    token.line,
                    module_name,
                    js_name,
                    f"argument {argument_index} name {parameter.name!r} is "
                    "reserved by Kotlin/Java; rename the C++ parameter",
                )
            if return_type != "void" and parameter.name == "promise":
                token = groups[argument_index - 1][-1]
                raise _source_error(
                    module_root,
                    path,
                    token.line,
                    module_name,
                    js_name,
                    f"argument {argument_index} name 'promise' collides with "
                    "the generated React Native Promise parameter; rename the "
                    "C++ parameter",
                )

    return Export(
        source=str(path.relative_to(module_root)),
        line=marker.line,
        cpp_name=cpp_name,
        js_name=js_name,
        return_type=return_type,
        parameters=tuple(parameters),
        noexcept=is_noexcept,
        definition_offset=function_token.start,
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
    exports: list[Export],
    module_name: str,
) -> None:
    exported_by_name = {export.cpp_name: export for export in exports}
    tagged_locations = {
        (export.source, export.definition_offset) for export in exports
    }
    for path, lexed in lexed_sources.items():
        if path.suffix.lower() not in CPP_SUFFIXES:
            continue
        source = str(path.relative_to(module_root))
        tokens = [
            token for token in lexed.tokens if token.conditional_depth == 0
        ]
        for index, token in enumerate(tokens):
            exported = exported_by_name.get(token.value)
            if exported is None or token.brace_depth != 0:
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
                exported.js_name,
                f"untagged global {kind} for exported C++ name "
                f"{token.value!r} conflicts with the tagged definition at "
                f"{exported.source}:{exported.line}; overloads and duplicate "
                "declarations are not supported. Keep exactly one global "
                "function with this C++ name",
            )


def scan_sources(
    module_root: Path,
    *,
    backend: str | None = None,
    module_name: str | None = None,
) -> list[Export]:
    source_root = module_root / "android/src/main/cpp"
    if not source_root.is_dir():
        raise CodegenError(f"missing C/C++ source directory: {source_root}")

    backend, module_name = _scan_context(module_root, backend, module_name)
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
        for comment in lexed.comments:
            is_candidate, _ = _marker_name(comment)
            if is_candidate:
                raise _source_error(
                    module_root,
                    path,
                    comment.line,
                    module_name,
                    None,
                    "export tags are allowed only in .cc, .cpp, or .cxx files; "
                    "move the exported definition into a C++ source file",
                )

    exports: list[Export] = []
    for path in all_sources:
        if path.suffix.lower() not in CPP_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = lexed_sources[path]
        markers: list[tuple[_LineComment, str | None]] = []
        for comment in lexed.comments:
            is_candidate, renamed = _marker_name(comment)
            if not is_candidate:
                continue
            if ANNOTATION.fullmatch(comment.text.strip()) is None:
                raise _source_error(
                    module_root,
                    path,
                    comment.line,
                    module_name,
                    None,
                    "malformed export tag; use // @SupernoteExport or "
                    '// @SupernoteExport(name = "javascriptName")',
                )
            markers.append((comment, renamed))
        for index, (marker, renamed) in enumerate(markers):
            next_marker = (
                markers[index + 1][0].start if index + 1 < len(markers) else None
            )
            exports.append(
                _parse_export(
                    module_root=module_root,
                    path=path,
                    text=text,
                    lexed=lexed,
                    marker=marker,
                    renamed=renamed,
                    next_marker_start=next_marker,
                    backend=backend,
                    module_name=module_name,
                )
            )

    exports.sort(key=lambda item: (item.source, item.line, item.js_name))
    native_names: dict[str, Export] = {}
    js_names: dict[str, Export] = {}
    for export in exports:
        if export.cpp_name in native_names:
            first = native_names[export.cpp_name]
            raise CodegenError(
                f"{export.source}:{export.line}: module {module_name!r}, "
                f"export {export.js_name!r}: overloaded or duplicate C++ "
                f"function {export.cpp_name!r}; first exported at "
                f"{first.source}:{first.line}. Rename one C++ function; "
                "overloads are not supported"
            )
        if export.js_name in js_names:
            first = js_names[export.js_name]
            raise CodegenError(
                f"{export.source}:{export.line}: module {module_name!r}, "
                f"export {export.js_name!r}: duplicate JavaScript export "
                f"{export.js_name!r}; first exported at "
                f"{first.source}:{first.line}. Give every export a unique "
                "@SupernoteExport name"
            )
        native_names[export.cpp_name] = export
        js_names[export.js_name] = export
    _reject_untagged_global_functions(
        module_root,
        lexed_sources,
        exports,
        module_name,
    )
    return exports


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


def _typescript(config: dict[str, object], exports: list[Export]) -> str:
    module_name = str(config["module_name"])
    backend = _normalize_backend(config["backend"])
    type_map = {
        "bool": "boolean",
        "double": "number",
        "std::string": "string",
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
    body = "\n".join(methods)
    return (
        "/* Generated by supernote_module_generator. Do not edit. */\n"
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
        error_class, "Native module failed; inspect the PluginHost logcat");
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
    if parameter.cpp_type == "double":
        return f"arguments[{number}].asNumber()"
    return f"arguments[{number}].asString(runtime).utf8(runtime)"


def _jsi_type_check(parameter: Parameter, number: int) -> str:
    method = {
        "bool": "isBool()",
        "double": "isNumber()",
        "std::string": "isString()",
    }[parameter.cpp_type]
    return f"!arguments[{number}].{method}"


def _jsi_binding(config: dict[str, object], exports: list[Export]) -> str:
    namespace = str(config["android_namespace"])
    module_name = str(config["module_name"])
    class_prefix = str(config["class_prefix"])
    installer = f"{namespace}.generated.{class_prefix}JsiModule"
    global_name = str(config["jsi_global_name"])
    declarations = _cpp_declarations(exports)
    registrations: list[str] = []
    for export in exports:
        expected_parameters = ", ".join(
            {
                "bool": "boolean",
                "double": "number",
                "std::string": "string",
            }[parameter.cpp_type]
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
            js_type = {
                "bool": "boolean",
                "double": "number",
                "std::string": "string",
            }[parameter.cpp_type]
            type_error = (
                f"{module_name}.{export.js_name}: argument {number + 1} "
                f"({parameter.name}) must be a {js_type}; expected "
                f"{expected_description}"
            )
            type_checks.append(
                f"          if ({_jsi_type_check(parameter, number)}) {{\n"
                "            throw facebook::jsi::JSError(\n"
                f"                runtime, {json.dumps(type_error)});\n"
                "          }"
            )
        arguments = ", ".join(
            _jsi_argument(parameter, number)
            for number, parameter in enumerate(export.parameters)
        )
        call = f"{export.cpp_name}({arguments})"
        if export.return_type == "void":
            result = f"        {call};\n        return Value::undefined();"
        elif export.return_type == "std::string":
            result = (
                f"        const auto result = {call};\n"
                "        return Value(String::createFromUtf8(runtime, result));"
            )
        else:
            result = f"        return Value({call});"
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
            "            throw facebook::jsi::JSError(\n"
            f"                runtime, std::string({json.dumps(count_prefix)}) + "
            "std::to_string(argument_count));\n"
            "          }\n"
            f"{chr(10).join(type_checks)}"
            f"{chr(10) if type_checks else ''}"
            "          try {\n"
            f"{result}\n"
            "          } catch (const facebook::jsi::JSError &) {\n"
            "            throw;\n"
            "          } catch (const std::exception &error) {\n"
            "            throw facebook::jsi::JSError(\n"
            f"                runtime, std::string({json.dumps(module_name + '.' + export.js_name + ': ')}) + error.what());\n"
            "          } catch (...) {\n"
            "            throw facebook::jsi::JSError(\n"
            f"                runtime, {json.dumps(module_name + '.' + export.js_name + ': unknown C++ exception')});\n"
            "          }\n"
            "        });\n"
            f"    exports.setProperty(runtime, {json.dumps(export.js_name)}, "
            "std::move(function));\n"
            "  }"
        )
    return f"""#include <jni.h>
#include <jsi/jsi.h>

#include <android/log.h>

#include <cstdint>
#include <exception>
#include <string>
#include <utility>

{declarations}

namespace {{

constexpr char kLogTag[] = "SupernoteJsi{class_prefix}";
constexpr char kInstallerClassName[] = {json.dumps(installer)};
constexpr char kGlobalName[] = {json.dumps(global_name)};

void clear_pending_exception(JNIEnv *env, const char *operation) {{
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
) -> dict[Path, str]:
    generated = module_root / "android/build/generated/supernote"
    backend = _normalize_backend(config["backend"])
    manifest = {
        "backend": backend,
        "exports": [export.manifest() for export in exports],
    }
    result = {
        generated / "exports.json":
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        module_root / "index.d.ts": _typescript(config, exports),
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
            config, exports
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
    exports = scan_sources(
        module_root,
        backend=str(config["backend"]),
        module_name=str(config.get("module_name", module_root.name)),
    )
    outputs = _contents(module_root, config, exports)
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
    except (CodegenError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(exports)} Supernote {args.module_root.name} exports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
