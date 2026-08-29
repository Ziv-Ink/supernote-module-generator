#!/usr/bin/env python3
"""Generate JNI or JSI bindings from annotated C++ APIs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys

if __package__:
    from .cpp_class_syntax import (
        CppClassSyntaxError,
        class_definition_extent as _decide_class_definition_extent,
    )
    from .cpp_declarations import (
        ClassStackKind,
        CppDeclarationError,
        MarkerStack as _MarkerStack,
        class_stack_route as _declaration_class_stack_route,
        first_unconsumed_marker as _declaration_first_unconsumed_marker,
        intent_from_stack as _declaration_intent_from_stack,
        member_marker_bindings as _declaration_member_marker_bindings,
        marker_entries as _declaration_marker_entries,
        marker_stacks as _marker_stacks,
        namespace_at as _namespace_at,
        unmarked_class_owner_offsets as _declaration_owner_offsets,
        validate_marker_stack_location as _declaration_validate_marker_location,
    )
    from .cpp_function_syntax import (
        CppFunctionSyntaxError,
        function_declaration_boundary as _decide_function_declaration_boundary,
        function_head as _decide_function_head,
        function_parameters as _decide_function_parameters,
        function_tail_start as _decide_function_tail_start,
        validate_function_body_opening as _validate_function_body_opening,
    )
    from .cpp_global_functions import (
        global_function_shape as _decide_global_function_shape,
    )
    from .cpp_lexer import _LexedSource, _LineComment, _Token, _lex_source
    from .cpp_source_routing import (
        CPP_HEADER_SUFFIXES,
        CPP_IMPLEMENTATION_SUFFIXES as CPP_SUFFIXES,
        first_owned_jni_bootstrap as _first_owned_jni_bootstrap,
        forbidden_marker_message as _forbidden_marker_message,
        source_route as _cpp_source_route,
    )
    from .jsi_binding_decisions import (
        JsiBindingDecisionError,
        JsiBindingMode,
        JsiRegistrationKind,
        async_helpers_required as _jsi_async_helpers_required,
        binding_mode as _jsi_binding_mode,
        registration_kind as _jsi_registration_kind,
    )
    from .cpp_members import (
        member_declarations as _member_declarations,
        parameter_groups as _parameter_groups,
    )
    from .cpp_member_semantics import (
        CppMemberDecisionError,
        constructor_suffix as _decide_constructor_suffix,
        parse_member_qualifiers as _decide_member_qualifiers,
    )
    from .cpp_member_shapes import (
        CppCallableHead,
        CppCallableKind,
        CppConstructorRoute,
        CppMemberShapeError,
        CppStoredMemberKind,
        callable_head as _decide_callable_head,
        constructor_route as _decide_constructor_route,
        field_shape as _decide_field_shape,
        method_shape as _decide_method_shape,
        stored_member_kind as _decide_stored_member_kind,
    )
    from .cpp_type_syntax import (
        CppTypeSyntaxError,
        parameter_list_syntax as _decide_parameter_list_syntax,
    )
    from .cpp_object_binding_codegen import render_cpp_object_bindings
    from .cpp_routes import CppRouteError, plan_cpp_routes
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
        CppEnumSource,
        CppFunctionSource,
        CppFieldSource,
        CppMethodSource,
        CppParameterSource,
        DeclarationTarget,
        SourceIntent,
        SourceModelError,
        SupernoteMarker,
    )
else:
    from supernote_codegen.cpp_class_syntax import (  # type: ignore[no-redef]
        CppClassSyntaxError,
        class_definition_extent as _decide_class_definition_extent,
    )
    from supernote_codegen.cpp_declarations import (  # type: ignore[no-redef]
        ClassStackKind,
        CppDeclarationError,
        MarkerStack as _MarkerStack,
        class_stack_route as _declaration_class_stack_route,
        first_unconsumed_marker as _declaration_first_unconsumed_marker,
        intent_from_stack as _declaration_intent_from_stack,
        member_marker_bindings as _declaration_member_marker_bindings,
        marker_entries as _declaration_marker_entries,
        marker_stacks as _marker_stacks,
        namespace_at as _namespace_at,
        unmarked_class_owner_offsets as _declaration_owner_offsets,
        validate_marker_stack_location as _declaration_validate_marker_location,
    )
    from supernote_codegen.cpp_function_syntax import (  # type: ignore[no-redef]
        CppFunctionSyntaxError,
        function_declaration_boundary as _decide_function_declaration_boundary,
        function_head as _decide_function_head,
        function_parameters as _decide_function_parameters,
        function_tail_start as _decide_function_tail_start,
        validate_function_body_opening as _validate_function_body_opening,
    )
    from supernote_codegen.cpp_global_functions import (  # type: ignore[no-redef]
        global_function_shape as _decide_global_function_shape,
    )
    from supernote_codegen.cpp_lexer import (  # type: ignore[no-redef]
        _LexedSource,
        _LineComment,
        _Token,
        _lex_source,
    )
    from supernote_codegen.cpp_source_routing import (  # type: ignore[no-redef]
        CPP_HEADER_SUFFIXES,
        CPP_IMPLEMENTATION_SUFFIXES as CPP_SUFFIXES,
        first_owned_jni_bootstrap as _first_owned_jni_bootstrap,
        forbidden_marker_message as _forbidden_marker_message,
        source_route as _cpp_source_route,
    )
    from supernote_codegen.jsi_binding_decisions import (  # type: ignore[no-redef]
        JsiBindingDecisionError,
        JsiBindingMode,
        JsiRegistrationKind,
        async_helpers_required as _jsi_async_helpers_required,
        binding_mode as _jsi_binding_mode,
        registration_kind as _jsi_registration_kind,
    )
    from supernote_codegen.cpp_members import (  # type: ignore[no-redef]
        member_declarations as _member_declarations,
        parameter_groups as _parameter_groups,
    )
    from supernote_codegen.cpp_member_semantics import (  # type: ignore[no-redef]
        CppMemberDecisionError,
        constructor_suffix as _decide_constructor_suffix,
        parse_member_qualifiers as _decide_member_qualifiers,
    )
    from supernote_codegen.cpp_member_shapes import (  # type: ignore[no-redef]
        CppCallableHead,
        CppCallableKind,
        CppConstructorRoute,
        CppMemberShapeError,
        CppStoredMemberKind,
        callable_head as _decide_callable_head,
        constructor_route as _decide_constructor_route,
        field_shape as _decide_field_shape,
        method_shape as _decide_method_shape,
        stored_member_kind as _decide_stored_member_kind,
    )
    from supernote_codegen.cpp_type_syntax import (  # type: ignore[no-redef]
        CppTypeSyntaxError,
        parameter_list_syntax as _decide_parameter_list_syntax,
    )
    from supernote_codegen.cpp_object_binding_codegen import (  # type: ignore[no-redef]
        render_cpp_object_bindings,
    )
    from supernote_codegen.cpp_routes import (  # type: ignore[no-redef]
        CppRouteError,
        plan_cpp_routes,
    )
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
        CppEnumSource,
        CppFunctionSource,
        CppFieldSource,
        CppMethodSource,
        CppParameterSource,
        DeclarationTarget,
        SourceIntent,
        SourceModelError,
        SupernoteMarker,
    )


def _iter_source_tree_no_follow(root: Path):
    """Yield source entries without entering directory symlinks."""

    if __package__:
        from .filesystem import FilesystemError, entry_kind, iter_tree_no_follow

        if entry_kind(root) != "directory":
            return
        try:
            yield from iter_tree_no_follow(root)
        except FilesystemError as exc:
            raise CodegenError(
                f"could not inspect C/C++ source directory {root}: {exc}"
            ) from exc
        return
    if root.is_symlink():
        return
    pending = [root]
    entries = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as stream:
                children = sorted(stream, key=lambda child: child.name, reverse=True)
        except OSError as exc:
            raise CodegenError(
                f"could not inspect C/C++ source directory {directory}: {exc}"
            ) from exc
        for child in children:
            path = Path(child.path)
            entries.append(path)
            try:
                if child.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError as exc:
                raise CodegenError(
                    f"could not inspect C/C++ source entry {path}: {exc}"
                ) from exc
    yield from sorted(entries)


OBJECT_ANNOTATION = re.compile(
    r"@SupernoteExportObject"
    r"(?:\(\s*name\s*=\s*\"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\"\s*\))?"
)
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
    async_: bool = False

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
    async_: bool = False

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


@dataclass(frozen=True)
class _CppClassParseContext:
    namespace: tuple[str, ...]
    intent: SourceIntent
    following: tuple[_Token, ...]
    diagnostic_line: int


def _error(path: Path, line: int, message: str) -> CodegenError:
    return CodegenError(f"{path}:{line}: {message}")


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

def _object_marker_name(comment: _LineComment) -> tuple[bool, str | None]:
    value = comment.text.strip()
    match = OBJECT_ANNOTATION.fullmatch(value)
    if match:
        return True, match.group("name")
    return bool(re.match(r"@SupernoteExportObject(?:\b|\()", value)), None


def _tokens_text(tokens: list[_Token]) -> str:
    return " ".join(token.value for token in tokens)


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
    namespace, namespace_depth = _namespace_at(lexed, marker.start)
    _validate_marker_stack_location(
        module_root,
        path,
        module_name,
        stack,
        brace_depth=namespace_depth,
        description="free-function",
        export_name=marker_export,
        brace_message=(
            "a free-function marker must be at namespace scope, not inside "
            "a class or function"
        ),
    )
    intent = _intent_from_stack(
        module_root,
        path,
        module_name,
        stack,
        DeclarationTarget.FUNCTION,
        marker_export,
    )

    try:
        boundary = _decide_function_declaration_boundary(
            text,
            lexed.tokens,
            lexed.directives,
            marker_start=marker.start,
            marker_line=marker.line,
            marker_end=stack.last.end,
            next_marker_start=next_marker_start,
            pending_export=marker_export,
        )
    except CppFunctionSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    following = list(boundary.following)

    try:
        function_head = _decide_function_head(
            following,
            pending_export=marker_export,
            forbidden_prefixes=FORBIDDEN_DECLARATION_PREFIXES,
            keywords=CPP23_KEYWORDS,
        )
    except CppFunctionSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    return_type = function_head.return_type_spelling
    cpp_name = function_head.cpp_name
    js_name = cpp_name
    try:
        function_parameters = _decide_function_parameters(
            following,
            opening_index=function_head.parameter_opening_index,
            marker_line=marker.line,
            export_name=js_name,
        )
    except CppFunctionSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    groups = [list(group) for group in function_parameters.groups]
    close_index = function_parameters.closing_index

    parameters = _parse_parameters(
        groups,
        module_root=module_root,
        path=path,
        marker_line=marker.line,
        module_name=module_name,
        export_name=js_name,
    )

    try:
        function_tail = _decide_function_tail_start(
            following,
            close_index=close_index,
            marker_line=marker.line,
            export_name=js_name,
        )
    except CppFunctionSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    opening = following[function_tail.body_opening_index]
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
    try:
        _validate_function_body_opening(opening, export_name=js_name)
    except CppFunctionSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc

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
        noexcept=function_tail.noexcept,
        definition_offset=function_head.definition_offset,
        namespace=namespace,
    )


def _parse_parameters(
    groups: list[list[_Token]],
    *,
    module_root: Path,
    path: Path,
    marker_line: int,
    module_name: str,
    export_name: str,
) -> tuple[Parameter, ...]:
    try:
        parameters = _decide_parameter_list_syntax(
            groups,
            marker_line=marker_line,
            keywords=CPP23_KEYWORDS,
        )
    except CppTypeSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            export_name,
            exc.message,
        ) from exc
    return tuple(Parameter(item.cpp_type, item.name) for item in parameters)


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
    try:
        return _decide_member_qualifiers(
            tokens,
            allow_const=allow_const,
            allow_default=allow_default,
        )
    except CppMemberDecisionError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            export_name,
            exc.message,
        ) from exc


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
            if routed is None:
                continue
            _, namespace_depth = _namespace_at(lexed, token.start)
            if token.brace_depth != namespace_depth:
                continue
            if (source, token.start) in tagged_locations:
                continue
            shape = _decide_global_function_shape(
                tokens,
                name_index=index,
                namespace_depth=namespace_depth,
            )
            if shape is None:
                continue
            raise _source_error(
                module_root,
                path,
                token.line,
                module_name,
                routed.cpp_name,
                f"untagged global {shape.kind} for routable C++ name "
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
    try:
        return _declaration_marker_entries(lexed)
    except CppDeclarationError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc


def _intent_from_stack(
    module_root: Path,
    path: Path,
    module_name: str,
    stack: _MarkerStack,
    target: DeclarationTarget,
    export_name: str | None,
) -> SourceIntent:
    try:
        return _declaration_intent_from_stack(stack, target, export_name)
    except CppDeclarationError as exc:
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from cause


def _validate_marker_stack_location(
    module_root: Path,
    path: Path,
    module_name: str,
    stack: _MarkerStack,
    *,
    brace_depth: int,
    description: str,
    export_name: str | None = None,
    brace_message: str | None = None,
) -> None:
    try:
        _declaration_validate_marker_location(
            stack,
            brace_depth=brace_depth,
            description=description,
            export_name=export_name,
            brace_message=brace_message,
        )
    except CppDeclarationError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc


def _class_stack_route(
    module_root: Path,
    path: Path,
    module_name: str,
    lexed: _LexedSource,
    stack: _MarkerStack,
):
    try:
        return _declaration_class_stack_route(lexed, stack)
    except CppDeclarationError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc


def _constructor_suffix(
    tokens: list[_Token],
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    class_name: str,
) -> tuple[bool, bool]:
    try:
        return _decide_constructor_suffix(tokens)
    except CppMemberDecisionError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            f"{class_name}.create",
            exc.message,
        ) from exc


def _stored_member_kind(
    declaration: list[_Token],
    *,
    marked: bool,
    value_class: bool,
    module_root: Path,
    path: Path,
    module_name: str,
    class_name: str,
) -> CppStoredMemberKind:
    try:
        return _decide_stored_member_kind(
            declaration,
            marked=marked,
            value_class=value_class,
        )
    except CppMemberShapeError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line if exc.line is not None else declaration[0].line,
            module_name,
            class_name,
            exc.message,
        ) from exc


def _parse_v4_field_source(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    relative: str,
    class_name: str,
    access: str,
    declaration: list[_Token],
    stack: _MarkerStack,
) -> CppFieldSource:
    intent = _intent_from_stack(
        module_root,
        path,
        module_name,
        stack,
        DeclarationTarget.FIELD,
        class_name,
    )
    if access != "public":
        raise _source_error(
            module_root,
            path,
            stack.first.line,
            module_name,
            class_name,
            "a generated field must be public in C++",
        )
    try:
        field = _decide_field_shape(declaration)
    except CppMemberShapeError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line if exc.line is not None else stack.first.line,
            module_name,
            class_name,
            exc.message,
        ) from exc
    return CppFieldSource(
        SourceProvenance(
            f"cpp:{relative}:{class_name}.{field.name}#field",
            "cpp",
            relative,
            stack.first.line,
        ),
        field.name,
        field.type_spelling,
        intent,
        access,
        field.mutable,
        field.static,
    )


def _constructor_is_lowerable(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    class_name: str,
    declaration: list[_Token],
    groups: list[list[_Token]],
    callable_head: CppCallableHead,
    stack: _MarkerStack | None,
) -> bool:
    route = _decide_constructor_route(
        callable_head,
        groups,
        cpp_name=class_name,
        marked=stack is not None,
    )
    if route is CppConstructorRoute.REJECT_PREFIX:
        raise _source_error(
            module_root,
            path,
            declaration[0].line,
            module_name,
            f"{class_name}.create",
            "a generated constructor may use only the optional explicit "
            "modifier before its class name",
        )
    if route is CppConstructorRoute.REJECT_COPY_OR_MOVE:
        assert stack is not None
        raise _source_error(
            module_root,
            path,
            stack.first.line,
            module_name,
            class_name,
            "copy and move constructors cannot be generated creation paths",
        )
    return route is CppConstructorRoute.LOWER


def _parse_v4_constructor_source(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    relative: str,
    class_name: str,
    access: str,
    declaration: list[_Token],
    groups: list[list[_Token]],
    suffix: list[_Token],
    callable_head: CppCallableHead,
    stack: _MarkerStack | None,
    constructor_ids: set[str],
) -> CppConstructorSource | None:
    if not _constructor_is_lowerable(
        module_root=module_root,
        path=path,
        module_name=module_name,
        class_name=class_name,
        declaration=declaration,
        groups=groups,
        callable_head=callable_head,
        stack=stack,
    ):
        return None
    intent = (
        _intent_from_stack(
            module_root,
            path,
            module_name,
            stack,
            DeclarationTarget.CONSTRUCTOR,
            f"{class_name}.create",
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
            f"{class_name}.create",
            "SupernoteConstructor must mark a public constructor",
        )
    try:
        parsed_parameters = _parse_parameters(
            groups,
            module_root=module_root,
            path=path,
            marker_line=(stack.first.line if stack else declaration[0].line),
            module_name=module_name,
            export_name=f"{class_name}.create",
        )
    except CodegenError:
        if stack is not None:
            raise
        return None
    try:
        is_noexcept, deleted = _constructor_suffix(
            suffix,
            module_root=module_root,
            path=path,
            module_name=module_name,
            class_name=class_name,
        )
    except CodegenError:
        if stack is not None:
            raise
        return None
    if stack is not None and deleted:
        raise _source_error(
            module_root,
            path,
            stack.first.line,
            module_name,
            f"{class_name}.create",
            "SupernoteConstructor cannot select a deleted constructor",
        )
    signature = ",".join(parameter.cpp_type for parameter in parsed_parameters)
    declaration_id = f"cpp:{relative}:constructor:{class_name}({signature})"
    if declaration_id in constructor_ids:
        raise _source_error(
            module_root,
            path,
            declaration[0].line,
            module_name,
            f"{class_name}.create",
            "duplicate constructor signature",
        )
    constructor_ids.add(declaration_id)
    return CppConstructorSource(
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
        explicit=callable_head.explicit,
        noexcept=is_noexcept,
    )


def _parse_v4_method_source(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    relative: str,
    class_name: str,
    access: str,
    declaration: list[_Token],
    opening_index: int,
    groups: list[list[_Token]],
    suffix: list[_Token],
    callable_head: CppCallableHead,
    stack: _MarkerStack,
    method_names: dict[str, int],
) -> CppMethodSource:
    if callable_head.kind is CppCallableKind.DESTRUCTOR:
        raise _source_error(
            module_root,
            path,
            stack.first.line,
            module_name,
            class_name,
            "destructors cannot be generated members",
        )
    intent = _intent_from_stack(
        module_root,
        path,
        module_name,
        stack,
        DeclarationTarget.METHOD,
        class_name,
    )
    if access != "public":
        raise _source_error(
            module_root,
            path,
            stack.first.line,
            module_name,
            class_name,
            "a generated method must be public in C++",
        )
    try:
        method = _decide_method_shape(
            declaration,
            opening_index,
            keywords=CPP23_KEYWORDS,
        )
    except CppMemberShapeError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line if exc.line is not None else declaration[0].line,
            module_name,
            class_name,
            exc.message,
        ) from exc
    method_name = method.name
    if method_name in method_names:
        raise _source_error(
            module_root,
            path,
            method.name_line,
            module_name,
            class_name,
            f"duplicate generated method name {method_name!r}; first "
            f"marked at line {method_names[method_name]}",
        )
    parameters = _parse_parameters(
        groups,
        module_root=module_root,
        path=path,
        marker_line=stack.first.line,
        module_name=module_name,
        export_name=f"{class_name}.{method_name}",
    )
    is_const, is_noexcept = _parse_member_qualifiers(
        suffix,
        module_root=module_root,
        path=path,
        module_name=module_name,
        export_name=f"{class_name}.{method_name}",
        allow_const=True,
        allow_default=False,
    )
    signature = ",".join(parameter.cpp_type for parameter in parameters)
    method_names[method_name] = method.name_line
    return CppMethodSource(
        provenance=SourceProvenance(
            declaration_id=(
                f"cpp:{relative}:{class_name}.{method_name}({signature})"
            ),
            language="cpp",
            path=relative,
            line=stack.first.line,
        ),
        cpp_name=method_name,
        return_type_spelling=method.return_type_spelling,
        parameters=tuple(
            CppParameterSource(parameter.cpp_type, parameter.name)
            for parameter in parameters
        ),
        intent=intent,
        access=access,
        const=is_const,
        noexcept=is_noexcept,
        static=method.static,
    )


def _class_member_callable_parts(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    class_name: str,
    declaration: list[_Token],
    stack: _MarkerStack | None,
) -> tuple[list[list[_Token]], list[_Token], CppCallableHead] | None:
    values = [token.value for token in declaration]
    opening_index = values.index("(")
    try:
        groups, closing_index = _parameter_groups(declaration, opening_index)
    except ValueError:
        if stack is None:
            return None
        raise _source_error(
            module_root,
            path,
            declaration[opening_index].line,
            module_name,
            class_name,
            "missing ')' in marked member declaration",
        ) from None
    return (
        groups,
        declaration[closing_index + 1:],
        _decide_callable_head(declaration[:opening_index], class_name),
    )


def _parse_v4_class_members(
    *,
    module_root: Path,
    path: Path,
    module_name: str,
    relative: str,
    class_name: str,
    declarations: list[tuple[str, list[_Token]]],
    stack_by_declaration: dict[int, _MarkerStack],
    value_class: bool,
) -> tuple[
    list[CppConstructorSource],
    list[CppMethodSource],
    list[CppFieldSource],
    bool,
]:
    constructors: list[CppConstructorSource] = []
    methods: list[CppMethodSource] = []
    fields: list[CppFieldSource] = []
    method_names: dict[str, int] = {}
    constructor_ids: set[str] = set()
    has_user_constructor = False
    for access, declaration in declarations:
        if not declaration:
            continue
        stack = stack_by_declaration.get(declaration[0].start)
        stored_kind = _stored_member_kind(
            declaration,
            marked=stack is not None,
            value_class=value_class,
            module_root=module_root,
            path=path,
            module_name=module_name,
            class_name=class_name,
        )
        if stored_kind is CppStoredMemberKind.IGNORE:
            continue
        if stored_kind is CppStoredMemberKind.FIELD:
            assert stack is not None
            fields.append(
                _parse_v4_field_source(
                    module_root=module_root,
                    path=path,
                    module_name=module_name,
                    relative=relative,
                    class_name=class_name,
                    access=access,
                    declaration=declaration,
                    stack=stack,
                )
            )
            continue
        callable_parts = _class_member_callable_parts(
            module_root=module_root,
            path=path,
            module_name=module_name,
            class_name=class_name,
            declaration=declaration,
            stack=stack,
        )
        if callable_parts is None:
            continue
        groups, suffix, callable_head = callable_parts
        opening_index = next(
            index for index, token in enumerate(declaration) if token.value == "("
        )
        if callable_head.kind is CppCallableKind.CONSTRUCTOR:
            has_user_constructor = True
            constructor = _parse_v4_constructor_source(
                module_root=module_root,
                path=path,
                module_name=module_name,
                relative=relative,
                class_name=class_name,
                access=access,
                declaration=declaration,
                groups=groups,
                suffix=suffix,
                callable_head=callable_head,
                stack=stack,
                constructor_ids=constructor_ids,
            )
            if constructor is not None:
                constructors.append(constructor)
            continue
        if stack is None:
            continue
        methods.append(
            _parse_v4_method_source(
                module_root=module_root,
                path=path,
                module_name=module_name,
                relative=relative,
                class_name=class_name,
                access=access,
                declaration=declaration,
                opening_index=opening_index,
                groups=groups,
                suffix=suffix,
                callable_head=callable_head,
                stack=stack,
                method_names=method_names,
            )
        )
    return constructors, methods, fields, has_user_constructor


def _append_implicit_constructor(
    constructors: list[CppConstructorSource],
    *,
    has_user_constructor: bool,
    relative: str,
    class_name: str,
    diagnostic_line: int,
) -> None:
    if has_user_constructor:
        return
    constructors.append(
        CppConstructorSource(
            provenance=SourceProvenance(
                declaration_id=(
                    f"cpp:{relative}:constructor:{class_name}()#implicit"
                ),
                language="cpp",
                path=relative,
                line=diagnostic_line,
            ),
            parameters=(),
            access="public",
            intent=SourceIntent(DeclarationTarget.CONSTRUCTOR),
            implicit=True,
        )
    )


def _class_parse_context(
    *,
    module_root: Path,
    path: Path,
    lexed: _LexedSource,
    active_tokens: list[_Token],
    class_stack: _MarkerStack | None,
    class_token: _Token | None,
    module_name: str,
) -> _CppClassParseContext:
    if class_stack is not None:
        class_offset = class_stack.first.start
        namespace, namespace_depth = _namespace_at(lexed, class_offset)
        _validate_marker_stack_location(
            module_root,
            path,
            module_name,
            class_stack,
            brace_depth=namespace_depth,
            description="class",
        )
        intent = _intent_from_stack(
            module_root,
            path,
            module_name,
            class_stack,
            DeclarationTarget.CLASS,
            None,
        )
        preceding = [token for token in active_tokens if token.end <= class_offset]
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
                f"{_tokens_text(prefix)!r}; templates and declaration "
                "modifiers are not supported",
            )
        return _CppClassParseContext(
            namespace=namespace,
            intent=intent,
            following=tuple(
                token
                for token in active_tokens
                if token.start >= class_stack.last.end
            ),
            diagnostic_line=class_stack.first.line,
        )
    if class_token is None:
        raise ValueError("an unmarked class parse requires its class token")
    class_offset = class_token.start
    namespace, namespace_depth = _namespace_at(lexed, class_offset)
    if class_token.brace_depth != namespace_depth:
        raise _source_error(
            module_root,
            path,
            class_token.line,
            module_name,
            None,
            "generated members require a top-level or namespace-level "
            "implementation owner class",
        )
    return _CppClassParseContext(
        namespace=namespace,
        intent=SourceIntent(DeclarationTarget.CLASS),
        following=tuple(
            token for token in active_tokens if token.start >= class_token.start
        ),
        diagnostic_line=class_token.line,
    )


def _parse_v4_class_source(
    *,
    module_root: Path,
    source_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    class_stack: _MarkerStack | None,
    class_token: _Token | None,
    stacks: list[_MarkerStack],
    module_name: str,
) -> tuple[CppClassSource, set[int]]:
    active_tokens = [
        token for token in lexed.tokens if token.conditional_depth == 0
    ]
    context = _class_parse_context(
        module_root=module_root,
        path=path,
        lexed=lexed,
        active_tokens=active_tokens,
        class_stack=class_stack,
        class_token=class_token,
        module_name=module_name,
    )
    namespace = context.namespace
    class_intent = context.intent
    following = list(context.following)
    diagnostic_line = context.diagnostic_line
    if not following:
        try:
            _decide_class_definition_extent(
                following,
                diagnostic_line=diagnostic_line,
            )
        except CppClassSyntaxError as exc:
            raise _source_error(
                module_root,
                path,
                exc.line,
                module_name,
                exc.export_name,
                exc.message,
            ) from exc
        raise AssertionError("an empty class token sequence cannot be valid")
    first = following[0]
    if (
        class_stack is not None
        and text[class_stack.last.end:first.start].strip()
    ):
        raise _source_error(
            module_root,
            path,
            diagnostic_line,
            module_name,
            None,
            "only whitespace may appear between the final class marker and "
            "the class or struct definition",
        )
    try:
        class_extent = _decide_class_definition_extent(
            following,
            diagnostic_line=diagnostic_line,
        )
    except CppClassSyntaxError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    cpp_name = class_extent.cpp_name
    opening = class_extent.opening_index
    closing = class_extent.closing_index

    opening_token = following[opening]
    closing_token = following[closing]
    member_depth = opening_token.brace_depth + 1
    body = following[opening + 1:closing]
    declarations = _member_declarations(
        body,
        default_access="private" if first.value == "class" else "public",
    )
    try:
        member_bindings = _declaration_member_marker_bindings(
            text,
            declarations,
            stacks,
            class_stack=class_stack,
            opening_end=opening_token.end,
            closing_start=closing_token.start,
            member_depth=member_depth,
            class_name=cpp_name,
        )
    except CppDeclarationError as exc:
        raise _source_error(
            module_root,
            path,
            exc.line,
            module_name,
            exc.export_name,
            exc.message,
        ) from exc
    stack_by_declaration = dict(member_bindings.stacks_by_declaration)
    consumed = set(member_bindings.consumed_comment_offsets)

    relative = str(path.relative_to(module_root))
    constructors, methods, fields, has_user_constructor = _parse_v4_class_members(
        module_root=module_root,
        path=path,
        module_name=module_name,
        relative=relative,
        class_name=cpp_name,
        declarations=declarations,
        stack_by_declaration=stack_by_declaration,
        value_class=class_intent.declares_value,
    )
    _append_implicit_constructor(
        constructors,
        has_user_constructor=has_user_constructor,
        relative=relative,
        class_name=cpp_name,
        diagnostic_line=diagnostic_line,
    )
    class_source = CppClassSource(
        provenance=SourceProvenance(
            declaration_id=f"cpp:{relative}:class:{cpp_name}",
            language="cpp",
            path=relative,
            line=diagnostic_line,
        ),
        cpp_name=cpp_name,
        include=path.relative_to(source_root).as_posix(),
        intent=class_intent,
        constructors=tuple(constructors),
        methods=tuple(methods),
        declaration_kind=class_extent.declaration_kind,
        fields=tuple(fields),
        namespace=namespace,
    )
    return class_source, consumed


def _top_level_class_extents(
    lexed: _LexedSource,
) -> list[tuple[_Token, _Token, _Token]]:
    tokens = [item for item in lexed.tokens if item.conditional_depth == 0]
    result: list[tuple[_Token, _Token, _Token]] = []
    for index, token in enumerate(tokens):
        if token.value not in {"class", "struct"}:
            continue
        if index and tokens[index - 1].value == "enum":
            continue
        _, namespace_depth = _namespace_at(lexed, token.start)
        if token.brace_depth != namespace_depth:
            continue
        opening = next(
            (
                item for item in tokens[index + 1:]
                if item.value in {"{", ";"}
            ),
            None,
        )
        if opening is None or opening.value != "{":
            continue
        closing = next(
            (
                item for item in tokens
                if item.start > opening.start
                and item.value == "}"
                and item.brace_depth == opening.brace_depth + 1
            ),
            None,
        )
        if closing is not None:
            result.append((token, opening, closing))
    return result


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
    for path in _iter_source_tree_no_follow(source_root):
        if not path.is_file() or path.suffix.lower() not in CPP_HEADER_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        entries = _marker_entries(module_root, path, lexed, module_name)
        stacks = _marker_stacks(text, entries)
        consumed: set[int] = set()
        parsed_class_offsets: set[int] = set()
        for stack in stacks:
            route = _class_stack_route(
                module_root, path, module_name, lexed, stack
            )
            if route.kind is ClassStackKind.IGNORE:
                continue
            if route.kind is ClassStackKind.ENUM:
                consumed.update(comment.start for comment in stack.comments)
                continue
            item, item_consumed = _parse_v4_class_source(
                module_root=module_root,
                source_root=source_root,
                path=path,
                text=text,
                lexed=lexed,
                class_stack=stack,
                class_token=None,
                stacks=stacks,
                module_name=module_name,
            )
            classes.append(item)
            consumed.update(item_consumed)
            if route.following is not None:
                parsed_class_offsets.add(route.following.start)

        extents = _top_level_class_extents(lexed)
        owner_offsets = _declaration_owner_offsets(
            stacks, consumed, extents, parsed_class_offsets
        )
        for owner_offset in owner_offsets:
            class_token = next(
                token for token, _, _ in extents if token.start == owner_offset
            )
            item, item_consumed = _parse_v4_class_source(
                module_root=module_root,
                source_root=source_root,
                path=path,
                text=text,
                lexed=lexed,
                class_stack=None,
                class_token=class_token,
                stacks=stacks,
                module_name=module_name,
            )
            classes.append(item)
            consumed.update(item_consumed)
        unconsumed = _declaration_first_unconsumed_marker(entries, consumed)
        if unconsumed is not None:
            raise _source_error(
                module_root,
                path,
                unconsumed.line,
                module_name,
                None,
                "a marked C++ member requires a top-level or namespace-level "
                "implementation owner class",
            )
    classes.sort(
        key=lambda item: (
            item.provenance.path,
            item.provenance.line,
            item.cpp_name,
        )
    )
    return classes


def _parse_cpp_enum_source(
    *,
    module_root: Path,
    source_root: Path,
    path: Path,
    text: str,
    lexed: _LexedSource,
    stack: _MarkerStack,
    module_name: str,
) -> tuple[CppEnumSource, set[int]]:
    namespace, namespace_depth = _namespace_at(lexed, stack.first.start)
    _validate_marker_stack_location(
        module_root, path, module_name, stack,
        brace_depth=namespace_depth, description="enum",
    )
    intent = _intent_from_stack(
        module_root, path, module_name, stack, DeclarationTarget.ENUM, None
    )
    following = [
        item for item in lexed.tokens
        if item.conditional_depth == 0 and item.start >= stack.last.end
    ]
    if len(following) < 5 or following[0].value != "enum" or following[1].value != "class":
        raise _source_error(
            module_root, path, stack.first.line, module_name, None,
            "SupernotePluginValue on an enum requires a complete enum class definition",
        )
    if text[stack.last.end:following[0].start].strip():
        raise _source_error(
            module_root, path, stack.first.line, module_name, None,
            "only whitespace may appear between SupernotePluginValue and enum class",
        )
    name_token = following[2]
    if name_token.kind != "identifier" or following[3].value != "{":
        raise _source_error(
            module_root, path, name_token.line, module_name, None,
            "a marked enum class requires an ordinary name and no base type",
        )
    opening = following[3]
    closing_index = next(
        (
            index for index, item in enumerate(following[4:], start=4)
            if item.value == "}" and item.brace_depth == opening.brace_depth + 1
        ),
        None,
    )
    if closing_index is None or closing_index + 1 >= len(following) or following[closing_index + 1].value != ";":
        raise _source_error(
            module_root, path, name_token.line, module_name, name_token.value,
            "marked enum class must be a complete definition ending in '};'",
        )
    body = following[4:closing_index]
    constants: list[str] = []
    expect_constant = True
    for token in body:
        if expect_constant and token.kind == "identifier":
            constants.append(token.value)
            expect_constant = False
        elif not expect_constant and token.value == ",":
            expect_constant = True
        else:
            raise _source_error(
                module_root, path, token.line, module_name, name_token.value,
                "string enums allow only comma-separated source constant names; "
                "explicit values, aliases, and attributes are unsupported",
            )
    if not constants or (expect_constant and body and body[-1].value != ","):
        raise _source_error(
            module_root, path, name_token.line, module_name, name_token.value,
            "a marked enum class requires at least one valid constant",
        )
    relative = str(path.relative_to(module_root))
    return (
        CppEnumSource(
            SourceProvenance(
                f"cpp:{relative}:enum:{'::'.join((*namespace, name_token.value))}",
                "cpp", relative, stack.first.line,
            ),
            name_token.value,
            path.relative_to(source_root).as_posix(),
            intent,
            tuple(constants),
            namespace,
        ),
        {item.start for item in stack.comments},
    )


def scan_cpp_enum_source_model(
    module_root: Path,
    *,
    module_name: str | None = None,
) -> list[CppEnumSource]:
    source_root = module_root / "android/src/main/cpp"
    _, resolved_name = _scan_context(module_root, None, module_name)
    result: list[CppEnumSource] = []
    for path in _iter_source_tree_no_follow(source_root):
        if not path.is_file() or path.suffix.lower() not in CPP_HEADER_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        stacks = _marker_stacks(
            text, _marker_entries(module_root, path, lexed, resolved_name)
        )
        for stack in stacks:
            following = next(
                (
                    item for item in lexed.tokens
                    if item.conditional_depth == 0 and item.start >= stack.last.end
                ),
                None,
            )
            if following is not None and following.value == "enum":
                item, _ = _parse_cpp_enum_source(
                    module_root=module_root, source_root=source_root, path=path,
                    text=text, lexed=lexed, stack=stack, module_name=resolved_name,
                )
                result.append(item)
    return sorted(result, key=lambda item: (item.provenance.path, item.provenance.line))


def _inspect_cpp_source_files(
    module_root: Path,
    paths: list[Path],
    module_name: str,
) -> dict[Path, _LexedSource]:
    lexed_sources: dict[Path, _LexedSource] = {}
    for path in paths:
        route = _cpp_source_route(path.suffix)
        if not route.inspect:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = _lex_source(text)
        lexed_sources[path] = lexed
        active_tokens = tuple(
            token for token in lexed.tokens if token.conditional_depth == 0
        )
        bootstrap = _first_owned_jni_bootstrap(active_tokens)
        if bootstrap is not None:
            raise _source_error(
                module_root,
                path,
                bootstrap.line,
                module_name,
                "JNI_OnLoad",
                "user sources must not declare or define JNI_OnLoad because "
                "the generated binding layer owns that bootstrap symbol",
            )
        marker_error = _forbidden_marker_message(route)
        if marker_error is None:
            continue
        entries = _marker_entries(module_root, path, lexed, module_name)
        if entries:
            raise _source_error(
                module_root,
                path,
                entries[0][0].line,
                module_name,
                None,
                marker_error,
            )
    return lexed_sources


def _reject_source_only_type_markers(
    module_root: Path,
    path: Path,
    entries: list[tuple[_LineComment, SupernoteMarker]],
    module_name: str,
) -> None:
    for comment, marker in entries:
        if marker not in {SupernoteMarker.OBJECT, SupernoteMarker.VALUE}:
            continue
        raise _source_error(
            module_root,
            path,
            comment.line,
            module_name,
            None,
            "bindable C++ classes, structs, values, and enums must have "
            "their complete structural definition in a header; a marked "
            "type defined only in a .cc, .cpp, or .cxx source file is not "
            "supported. Move the complete marked definition to a header; "
            "constructors and member functions may remain implemented "
            "out-of-line in this source file",
        )


def _parse_cpp_function_sources(
    module_root: Path,
    paths: list[Path],
    lexed_sources: dict[Path, _LexedSource],
    module_name: str,
) -> list[CppFunctionSource]:
    sources: list[CppFunctionSource] = []
    for path in paths:
        if not _cpp_source_route(path.suffix).parses_functions:
            continue
        text = path.read_text(encoding="utf-8")
        lexed = lexed_sources[path]
        entries = _marker_entries(module_root, path, lexed, module_name)
        _reject_source_only_type_markers(
            module_root,
            path,
            entries,
            module_name,
        )
        stacks = _marker_stacks(text, entries)
        for index, stack in enumerate(stacks):
            next_marker = (
                stacks[index + 1].first.start
                if index + 1 < len(stacks)
                else None
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
    return sources


def _reject_duplicate_cpp_function_names(
    sources: list[CppFunctionSource],
    module_name: str,
) -> None:
    native_names: dict[str, CppFunctionSource] = {}
    for source in sources:
        if source.cpp_name in native_names:
            first = native_names[source.cpp_name]
            raise CodegenError(
                f"{source.provenance.path}:{source.provenance.line}: module "
                f"{module_name!r}, export {source.cpp_name!r}: overloaded or "
                f"duplicate routable C++ function {source.cpp_name!r}; first "
                f"marked at {first.provenance.path}:{first.provenance.line}. "
                "Rename one C++ function; overloads are not supported"
            )
        native_names[source.cpp_name] = source


def scan_cpp_source_model(
    module_root: Path,
    *,
    module_name: str | None = None,
) -> list[CppFunctionSource]:
    source_root = module_root / "android/src/main/cpp"
    if not source_root.is_dir():
        raise CodegenError(f"missing C/C++ source directory: {source_root}")

    _, module_name = _scan_context(module_root, None, module_name)
    all_sources = [
        path for path in _iter_source_tree_no_follow(source_root) if path.is_file()
    ]
    lexed_sources = _inspect_cpp_source_files(
        module_root,
        all_sources,
        module_name,
    )
    sources = _parse_cpp_function_sources(
        module_root,
        all_sources,
        lexed_sources,
        module_name,
    )
    sources.sort(
        key=lambda item: (
            item.provenance.path,
            item.provenance.line,
            item.cpp_name,
        )
    )
    _reject_duplicate_cpp_function_names(sources, module_name)
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
        feature_id = "supernote:feature:legacy"
        metadata_path = module_root / ".supernote-module.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            candidate = metadata.get("feature_id")
            if isinstance(candidate, str):
                feature_id = candidate
        return project_cpp_api(
            scan_cpp_source_model(module_root, module_name=module_name),
            scan_cpp_class_source_model(module_root, module_name=module_name),
            scan_cpp_enum_source_model(module_root, module_name=module_name),
            feature_id=feature_id,
        )
    except (CppProjectionError, SourceModelError, ValueError) as exc:
        raise CodegenError(str(exc)) from exc


def _lower_sync_export(
    module_root: Path,
    source: CppFunctionSource,
    *,
    backend: str,
    module_name: str,
    allow_async: bool = False,
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
            "SupernotePluginInternal was recognized, but its generated C++ caller "
            "route is not implemented yet",
        )
    if source.intent.execution is ExecutionMode.ASYNC and not allow_async:
        raise _source_error(
            module_root,
            path,
            source.provenance.line,
            module_name,
            name,
            "SupernotePluginAsync was recognized, but async lowering is not "
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
        async_=source.intent.execution is ExecutionMode.ASYNC,
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
    allow_async: bool = False,
    allow_internal_members: bool = False,
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
            "SupernotePluginInternal class semantics were recognized, but the "
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
            if allow_internal_members:
                # Internal object methods remain in the common semantic model
                # but never become JSI properties. Their receiver-aware call
                # paths are emitted separately from this public surface.
                continue
            raise _source_error(
                module_root,
                path,
                method.provenance.line,
                module_name,
                f"{source.cpp_name}.{method.cpp_name}",
                "SupernotePluginInternal object methods were recognized, but their "
                "receiver-aware internal route is not implemented yet",
            )
        if method.intent.execution is ExecutionMode.ASYNC and not allow_async:
            raise _source_error(
                module_root,
                path,
                method.provenance.line,
                module_name,
                f"{source.cpp_name}.{method.cpp_name}",
                "SupernotePluginAsync object methods were recognized, but async "
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
                async_=method.intent.execution is ExecutionMode.ASYNC,
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
    for path in _iter_source_tree_no_follow(source_root):
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
                "SupernoteExportObject is removed in V4; mark the class with "
                "SupernotePluginExport and mark each generated method explicitly",
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


def scan_v4_bindings(
    module_root: Path,
    *,
    module_name: str,
) -> ScannedBindings:
    """Lower source declarations into the active V4 binding model."""

    function_sources = scan_cpp_source_model(
        module_root, module_name=module_name
    )
    exports = [
        _lower_sync_export(
            module_root,
            source,
            backend="jsi",
            module_name=module_name,
            allow_async=True,
        )
        for source in function_sources
        if source.intent.role is DeclarationRole.EXPORTED
    ]
    class_sources = scan_cpp_class_source_model(
        module_root, module_name=module_name
    )
    objects = [
        _lower_sync_object(
            module_root,
            source,
            module_name=module_name,
            allow_async=True,
            allow_internal_members=True,
        )
        for source in class_sources
        if source.intent.role is DeclarationRole.EXPORTED
    ]
    free_names = {export.js_name: export for export in exports}
    for item in objects:
        if item.js_name in free_names:
            first = free_names[item.js_name]
            raise CodegenError(
                f"{item.source}:{item.line}: module {module_name!r}, export "
                f"{item.js_name!r}: JavaScript name collides with free-function "
                f"export at {first.source}:{first.line}; rename the function "
                "or class in source"
            )
    _validate_typescript_names(objects, module_name)
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
        return f"supernote_copy_uint8_array(runtime, supernote_snapshot_{number})"
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
    path = f"{diagnostic_name}.argument[{number}]({parameter.name})"
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
            f"{indent}      runtime, {json.dumps(prefix + 'must be a signed 32-bit integer')},",
            f"{indent}      \"OUT_OF_RANGE\", {json.dumps(path)}, \"int32\",",
            f"{indent}      supernote_describe_value(runtime, arguments[{number}]));",
            f"{indent}}}",
        ]
    if parameter.cpp_type in {"int64_t", "std::int64_t"}:
        return [
            f"{indent}if (!arguments[{number}].getBigInt(runtime).isInt64(runtime)) {{",
            f"{indent}  supernote_throw_range_error(",
            f"{indent}      runtime, {json.dumps(prefix + 'must fit in a signed 64-bit integer')},",
            f"{indent}      \"OUT_OF_RANGE\", {json.dumps(path)}, \"int64 bigint\", \"bigint\");",
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
            f"{indent}      runtime, {json.dumps(prefix + 'must fit in a 32-bit float')},",
            f"{indent}      \"OUT_OF_RANGE\", {json.dumps(path)}, \"float32\", \"number\");",
            f"{indent}}}",
        ]
    if parameter.cpp_type == "std::vector<std::byte>":
        snapshot_name = f"supernote_snapshot_{number}"
        return [
            f"{indent}auto {snapshot_name} = supernote_snapshot_uint8_array(",
            f"{indent}    runtime, arguments[{number}]);",
            f"{indent}supernote_check_uint8_array_snapshot_limit(",
            f"{indent}    runtime, {snapshot_name}, {json.dumps(path)});",
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
    return r'''std::string supernote_describe_value(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value) {
  if (value.isUndefined()) return "undefined";
  if (value.isNull()) return "null";
  if (value.isBool()) return "boolean";
  if (value.isNumber()) return "number";
  if (value.isBigInt()) return "bigint";
  if (value.isString()) return "string";
  if (value.isSymbol()) return "symbol";
  if (!value.isObject()) return "unknown";
  auto object = value.getObject(runtime);
  if (object.isArray(runtime)) return "Array";
  if (object.isFunction(runtime)) return "function";
  return "object";
}

facebook::jsi::Value supernote_make_builtin_error(
    facebook::jsi::Runtime &runtime,
    const char *constructor_name,
    const std::string &message,
    const std::string &reason,
    const std::string &path,
    const std::string &expected,
    const std::string &actual) {
  auto constructor =
      runtime.global().getPropertyAsFunction(runtime, constructor_name);
  const facebook::jsi::Value argument(
      facebook::jsi::String::createFromUtf8(runtime, message));
  auto error_value = constructor.callAsConstructor(
      runtime, &argument, static_cast<std::size_t>(1));
  auto error = error_value.getObject(runtime);
  error.setProperty(
      runtime, "reason",
      facebook::jsi::String::createFromAscii(runtime, reason));
  error.setProperty(
      runtime, "path",
      facebook::jsi::String::createFromUtf8(runtime, path));
  error.setProperty(
      runtime, "expected",
      facebook::jsi::String::createFromUtf8(runtime, expected));
  error.setProperty(
      runtime, "actual",
      facebook::jsi::String::createFromUtf8(runtime, actual));
  return facebook::jsi::Value(std::move(error));
}

[[noreturn]] void supernote_throw_builtin_error(
    facebook::jsi::Runtime &runtime,
    const char *constructor_name,
    const std::string &message,
    const std::string &reason = "TYPE_MISMATCH",
    const std::string &path = "",
    const std::string &expected = "",
    const std::string &actual = "unknown") {
  auto error = supernote_make_builtin_error(
      runtime, constructor_name, message, reason, path, expected, actual);
  throw facebook::jsi::JSError(runtime, std::move(error));
}

[[noreturn]] void supernote_throw_type_error(
    facebook::jsi::Runtime &runtime,
    const std::string &message,
    const std::string &reason = "TYPE_MISMATCH",
    const std::string &path = "",
    const std::string &expected = "",
    const std::string &actual = "unknown") {
  supernote_throw_builtin_error(
      runtime, "TypeError", message, reason, path, expected, actual);
}

[[noreturn]] void supernote_throw_range_error(
    facebook::jsi::Runtime &runtime,
    const std::string &message,
    const std::string &reason = "OUT_OF_RANGE",
    const std::string &path = "",
    const std::string &expected = "",
    const std::string &actual = "unknown") {
  supernote_throw_builtin_error(
      runtime, "RangeError", message, reason, path, expected, actual);
}

facebook::jsi::Value supernote_validation_success(
    facebook::jsi::Runtime &runtime) {
  facebook::jsi::Object result(runtime);
  result.setProperty(runtime, "ok", true);
  return facebook::jsi::Value(std::move(result));
}

facebook::jsi::Value supernote_validation_failure(
    facebook::jsi::Runtime &runtime,
    facebook::jsi::Value error) {
  facebook::jsi::Object result(runtime);
  result.setProperty(runtime, "ok", false);
  result.setProperty(runtime, "error", std::move(error));
  return facebook::jsi::Value(std::move(result));
}

facebook::jsi::Function supernote_attach_preflight(
    facebook::jsi::Runtime &runtime,
    facebook::jsi::Function function,
    facebook::jsi::Function accepts,
    facebook::jsi::Function check_arguments) {
  function.setProperty(runtime, "accepts", std::move(accepts));
  function.setProperty(
      runtime, "checkArguments", std::move(check_arguments));
  return function;
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

bool supernote_array_has_own_index(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Array &array,
    std::size_t index) {
  auto object_constructor =
      runtime.global().getPropertyAsObject(runtime, "Object");
  auto prototype =
      object_constructor.getPropertyAsObject(runtime, "prototype");
  auto has_own =
      prototype.getPropertyAsFunction(runtime, "hasOwnProperty");
  auto key = facebook::jsi::String::createFromUtf8(
      runtime, std::to_string(index));
  auto result = has_own.callWithThis(runtime, array, std::move(key));
  return result.isBool() && result.getBool();
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

struct SupernoteUint8ArraySnapshot {
  facebook::jsi::ArrayBuffer buffer;
  std::size_t offset;
  std::size_t length;
};

SupernoteUint8ArraySnapshot supernote_snapshot_uint8_array(
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
  return {std::move(buffer), offset, length};
}

void supernote_check_uint8_array_snapshot_limit(
    facebook::jsi::Runtime &runtime,
    const SupernoteUint8ArraySnapshot &snapshot,
    const std::string &path) {
  constexpr std::size_t kMaxByteBufferBytes = 32ULL * 1024ULL * 1024ULL;
  if (snapshot.length > kMaxByteBufferBytes) {
    supernote_throw_range_error(
        runtime,
        "Uint8Array byteLength exceeds the generated conversion limit",
        "LIMIT_EXCEEDED",
        path,
        "at most 33554432 bytes",
        std::to_string(snapshot.length) + " bytes");
  }
}

std::vector<std::byte> supernote_copy_uint8_array(
    facebook::jsi::Runtime &runtime,
    SupernoteUint8ArraySnapshot &snapshot) {
  supernote_check_uint8_array_snapshot_limit(
      runtime, snapshot, "Uint8Array.byteLength");
  std::vector<std::byte> result(snapshot.length);
  if (snapshot.length != 0) {
    std::memcpy(
        result.data(),
        snapshot.buffer.data(runtime) + snapshot.offset,
        snapshot.length);
  }
  return result;
}

std::vector<std::byte> supernote_copy_uint8_array(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value) {
  auto snapshot = supernote_snapshot_uint8_array(runtime, value);
  return supernote_copy_uint8_array(runtime, snapshot);
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


def _jsi_async_helpers() -> str:
    return r'''constexpr char kPromiseContinuationsGlobal[] =
    "__supernoteV4PromiseContinuations_a7db36cf3b5e";

facebook::jsi::Object supernote_error_object(
    facebook::jsi::Runtime &runtime,
    const char *code,
    const std::string &message) {
  auto registry = runtime.global().getPropertyAsObject(
      runtime, kFeatureRegistryGlobal);
  auto exports = registry.getPropertyAsObject(runtime, kFeatureId);
  auto constructor = exports.getPropertyAsFunction(
      runtime, "__supernoteErrorConstructor");
  const facebook::jsi::Value arguments[] = {
      facebook::jsi::String::createFromAscii(runtime, code),
      facebook::jsi::String::createFromUtf8(runtime, message),
  };
  auto error = constructor.callAsConstructor(
      runtime, arguments, static_cast<std::size_t>(2));
  return error.getObject(runtime);
}

facebook::jsi::Object supernote_promise_continuations(
    facebook::jsi::Runtime &runtime) {
  auto value = runtime.global().getProperty(
      runtime, kPromiseContinuationsGlobal);
  if (value.isObject()) return value.getObject(runtime);
  auto map = runtime.global().getPropertyAsFunction(runtime, "Map");
  auto continuations = map.callAsConstructor(runtime).getObject(runtime);
  runtime.global().setProperty(
      runtime, kPromiseContinuationsGlobal, continuations);
  return continuations;
}

void supernote_register_continuation(
    facebook::jsi::Runtime &runtime,
    std::uint64_t operation_id,
    const facebook::jsi::Value &resolve,
    const facebook::jsi::Value &reject) {
  facebook::jsi::Object continuation(runtime);
  continuation.setProperty(runtime, "resolve", resolve);
  continuation.setProperty(runtime, "reject", reject);
  auto continuations = supernote_promise_continuations(runtime);
  const auto key = std::to_string(operation_id);
  auto set = continuations.getPropertyAsFunction(runtime, "set");
  set.callWithThis(
      runtime, continuations,
      facebook::jsi::String::createFromAscii(runtime, key),
      std::move(continuation));
}

facebook::jsi::Object supernote_take_continuation(
    facebook::jsi::Runtime &runtime,
    std::uint64_t operation_id) {
  auto continuations = supernote_promise_continuations(runtime);
  const auto key = std::to_string(operation_id);
  auto key_value = facebook::jsi::String::createFromAscii(runtime, key);
  auto get = continuations.getPropertyAsFunction(runtime, "get");
  auto value = get.callWithThis(runtime, continuations, key_value);
  if (!value.isObject()) {
    throw facebook::jsi::JSError(
        runtime, "Supernote async continuation is unavailable");
  }
  auto continuation = value.getObject(runtime);
  auto remove = continuations.getPropertyAsFunction(runtime, "delete");
  auto removed = remove.callWithThis(runtime, continuations, key_value);
  if (!removed.isBool() || !removed.getBool()) {
    throw facebook::jsi::JSError(
        runtime, "Supernote async continuation cannot be removed");
  }
  return continuation;
}

void supernote_resolve_operation(
    facebook::jsi::Runtime &runtime,
    std::uint64_t operation_id,
    facebook::jsi::Value value) {
  auto continuation = supernote_take_continuation(runtime, operation_id);
  auto resolve = continuation.getPropertyAsFunction(runtime, "resolve");
  resolve.call(runtime, std::move(value));
}

void supernote_reject_operation(
    facebook::jsi::Runtime &runtime,
    std::uint64_t operation_id,
    const char *code,
    const std::string &message) {
  auto continuation = supernote_take_continuation(runtime, operation_id);
  auto reject = continuation.getPropertyAsFunction(runtime, "reject");
  reject.call(runtime, supernote_error_object(runtime, code, message));
}

void supernote_reject_new_promise(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &reject_value,
    const char *code,
    const std::string &message) {
  auto reject = reject_value.getObject(runtime).asFunction(runtime);
  reject.call(runtime, supernote_error_object(runtime, code, message));
}'''


def _jsi_async_host_function(
    *,
    js_name: str,
    diagnostic: str,
    parameters: tuple[Parameter, ...],
    return_type: str,
    call: str,
    outer_captures: tuple[str, ...] = ("feature_session",),
    prelude: str = "",
    executor_captures: tuple[str, ...] = (),
    worker_captures_extra: tuple[str, ...] = (),
    worker_prelude: str = "",
    release_feature_before_execution: bool = True,
    implementation_name: str = "C++",
    implementation_exception_type: str | None = None,
) -> str:
    expected_parameters = ", ".join(
        _jsi_expected_type(parameter.cpp_type) + f" {parameter.name}"
        for parameter in parameters
    )
    expected_description = (
        f"{len(parameters)} argument"
        f"{'' if len(parameters) == 1 else 's'}"
        f" ({expected_parameters})"
    )
    validations = [
        f"          if (argument_count != {len(parameters)}) {{",
        "            supernote_throw_type_error(",
        f"                runtime, std::string({json.dumps(diagnostic + ': expected ' + expected_description + '; received ')}) +",
        "                std::to_string(argument_count));",
        "          }",
    ]
    inputs = []
    captures = ["feature_session", *executor_captures]
    worker_captures = [
        "operation",
        "operation_id",
        "weak_feature",
        *worker_captures_extra,
    ]
    calls = []
    for index, parameter in enumerate(parameters):
        validations.extend(
            [
                f"          if ({_jsi_type_check(parameter, index)}) {{",
                "            supernote_throw_type_error(",
                f"                runtime, {json.dumps(diagnostic + ': argument ' + str(index + 1) + ' (' + parameter.name + ') has the wrong JavaScript type')});",
                "          }",
            ]
        )
        validations.extend(
            _jsi_range_validation(
                parameter,
                index,
                diagnostic_name=diagnostic,
                indent="          ",
            )
        )
        name = f"supernote_input_{index}"
        inputs.append(f"          auto {name} = {_jsi_argument(parameter, index)};")
        captures.append(f"{name} = std::move({name})")
        worker_captures.append(f"{name} = std::move({name})")
        calls.append(name)
    call_expression = call.replace(
        "__SUPERNOTE_ARGUMENTS__", ", ".join(calls)
    )
    if return_type == "void":
        state = (
            "          struct AsyncState {\n"
            "            bool success{false};\n"
            "            std::string error;\n"
            "          };"
        )
        execution = (
            f"              {call_expression};\n"
            "              state->success = true;"
        )
        resolution = (
            "                supernote_resolve_operation(\n"
            "                    runtime, operation_id, Value::undefined());"
        )
    else:
        state = (
            "          struct AsyncState {\n"
            "            bool success{false};\n"
            f"            std::optional<{return_type}> value;\n"
            "            std::string error;\n"
            "          };"
        )
        execution = (
            f"              state->value = {call_expression};\n"
            "              state->success = true;"
        )
        value = _jsi_async_result_value(return_type)
        resolution = (
            f"                auto value = {value};\n"
            "                supernote_resolve_operation(\n"
            "                    runtime, operation_id, std::move(value));"
        )
    prelude_block = f"{prelude}\n" if prelude else ""
    worker_prelude_block = (
        f"{worker_prelude}\n" if worker_prelude else ""
    )
    feature_release = (
        "                      implementation_feature.reset();\n"
        if release_feature_before_execution
        else ""
    )
    implementation_exception_catch = (
        f"                      }} catch (const {implementation_exception_type} &error) {{\n"
        "                        state->error = error.what();\n"
        if implementation_exception_type
        else ""
    )
    return f'''Function::createFromHostFunction(
        runtime,
        PropNameID::forAscii(runtime, {json.dumps(js_name)}),
        {len(parameters)},
        [{', '.join(outer_captures)}](facebook::jsi::Runtime &runtime,
           const Value &,
           const Value *arguments,
           std::size_t argument_count) -> Value {{
{prelude_block}{chr(10).join(validations)}
{chr(10).join(inputs)}
{state}
          auto state = std::make_shared<AsyncState>();
          auto executor = Function::createFromHostFunction(
              runtime,
              PropNameID::forAscii(runtime, "SupernoteAsyncExecutor"),
              2,
              [{', '.join(captures)}, state](
                  facebook::jsi::Runtime &runtime,
                  const Value &,
                  const Value *continuation_arguments,
                  std::size_t continuation_count) mutable -> Value {{
                if (continuation_count != 2 ||
                    !continuation_arguments[0].isObject() ||
                    !continuation_arguments[1].isObject()) {{
                  throw facebook::jsi::JSError(
                      runtime, "Promise supplied invalid continuation functions");
                }}
                auto operation = feature_session
                    ? feature_session->accept_factory(
                          [](supernote::runtime::SessionId operation_id) {{
                            return [operation_id](void *runtime_pointer) {{
                              auto &runtime = *static_cast<facebook::jsi::Runtime *>(
                                  runtime_pointer);
                              supernote_reject_operation(
                                  runtime, operation_id, "FEATURE_CLOSED",
                                  "feature closed before async completion");
                            }};
                          }})
                    : nullptr;
                if (!operation) {{
                  supernote_reject_new_promise(
                      runtime, continuation_arguments[1], "FEATURE_CLOSED",
                      "feature is closed");
                  return Value::undefined();
                }}
                const auto operation_id = operation->id();
                supernote_register_continuation(
                    runtime, operation_id, continuation_arguments[0],
                    continuation_arguments[1]);
                std::weak_ptr<supernote::runtime::FeatureSession> weak_feature =
                    feature_session;
                auto work = supernote::runtime::process_services().workers().submit(
                    [{', '.join(worker_captures)}, state](
                        supernote::runtime::CancellationToken executor_cancel) mutable {{
                      if (executor_cancel.is_cancelled() ||
                          operation->cancellation_token().is_cancelled()) return;
                      auto implementation_feature = weak_feature.lock();
                      if (!implementation_feature) return;
                      supernote::runtime::FeatureCallScope feature_call_scope(
                          implementation_feature);
{worker_prelude_block}
{feature_release}
                      try {{
{execution}
{implementation_exception_catch}                      }} catch (const std::exception &error) {{
                        state->error = error.what();
                      }} catch (...) {{
                        state->error = {json.dumps("unknown " + implementation_name + " implementation failure")};
                      }}
                      if (executor_cancel.is_cancelled() ||
                          operation->cancellation_token().is_cancelled()) return;
                      auto feature = weak_feature.lock();
                      if (!feature) return;
                      feature->schedule_completion(
                          operation,
                          [state, operation_id](void *runtime_pointer) {{
                            auto &runtime = *static_cast<facebook::jsi::Runtime *>(
                                runtime_pointer);
                            if (!state->success) {{
                              supernote_reject_operation(
                                  runtime, operation_id, "IMPLEMENTATION_ERROR",
                                  state->error.empty()
                                      ? {json.dumps(implementation_name + " implementation failed")}
                                      : state->error);
                              return;
                            }}
                            try {{
{resolution}
                            }} catch (const std::exception &error) {{
                              supernote_reject_operation(
                                  runtime, operation_id, "INTERNAL", error.what());
                            }}
                          }});
                    }});
                operation->set_work(work);
                if (!work.accepted()) {{
                  feature_session->schedule_completion(
                      operation,
                      [operation_id](void *runtime_pointer) {{
                        auto &runtime = *static_cast<facebook::jsi::Runtime *>(
                            runtime_pointer);
                        supernote_reject_operation(
                            runtime, operation_id, "RESOURCE_EXHAUSTED",
                            "Supernote worker queue is full");
                      }});
                }}
                return Value::undefined();
              }});
          auto promise = runtime.global().getPropertyAsFunction(runtime, "Promise");
          const Value executor_argument(std::move(executor));
          return promise.callAsConstructor(
              runtime, &executor_argument, static_cast<std::size_t>(1));
        }})'''


def _jsi_async_registration(
    module_name: str,
    export: Export,
) -> str:
    function = _jsi_async_host_function(
        js_name=export.js_name,
        diagnostic=f"{module_name}.{export.js_name}",
        parameters=export.parameters,
        return_type=export.return_type,
        call=f"{export.cpp_name}(__SUPERNOTE_ARGUMENTS__)",
    )
    return (
        "  {\n"
        f"    auto function = {function};\n"
        f"    exports.setProperty(runtime, {json.dumps(export.js_name)}, "
        "std::move(function));\n"
        "  }"
    )


def _jsi_async_result_value(return_type: str) -> str:
    if return_type == "std::string":
        return "Value(String::createFromUtf8(runtime, *state->value))"
    if return_type in {"int64_t", "std::int64_t"}:
        return (
            "Value(facebook::jsi::BigInt::fromInt64("
            "runtime, static_cast<std::int64_t>(*state->value)))"
        )
    if return_type == "std::vector<std::byte>":
        return "supernote_make_uint8_array(runtime, *state->value)"
    if return_type == "bool":
        return "Value(*state->value)"
    return "Value(static_cast<double>(*state->value))"


def _jsi_object_callable_body(
    *,
    parameters: tuple[Parameter, ...],
    diagnostic_name: str,
    call: str,
    return_type: str,
    indent: str,
    pre_call: tuple[str, ...] = (),
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
    lines.extend(f"{indent}{line}" for line in pre_call)
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
    *,
    session_aware: bool,
) -> str:
    class_name = f"GeneratedObject{index}HostObject"
    receiver_type = (
        f"supernote::runtime::ManagedRef<{item.cpp_name}>"
        if session_aware
        else f"std::shared_ptr<{item.cpp_name}>"
    )
    method_branches: list[str] = []
    for method in item.methods:
        if method.async_:
            function = _jsi_async_host_function(
                js_name=method.js_name,
                diagnostic=(
                    f"{module_name}.{item.js_name}.{method.js_name}"
                ),
                parameters=method.parameters,
                return_type=method.return_type,
                call=(
                    "operation_receiver->"
                    f"{method.cpp_name}(__SUPERNOTE_ARGUMENTS__)"
                ),
                outer_captures=("native_instance", "weak_feature"),
                prelude=(
                    "          auto feature_session = weak_feature.lock();\n"
                    "          auto operation_receiver = native_instance;"
                ),
                executor_captures=(
                    "operation_receiver = std::move(operation_receiver)",
                ),
                worker_captures_extra=(
                    "operation_receiver = std::move(operation_receiver)",
                ),
            )
            method_branches.append(
                f"    if (property_name == {json.dumps(method.js_name)}) {{\n"
                f"      {receiver_type} native_instance = instance_;\n"
                "      std::weak_ptr<supernote::runtime::FeatureSession> "
                "weak_feature = feature_session_;\n"
                f"      return {function};\n"
                "    }"
            )
            continue
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
            pre_call=(
                "if (!feature_session ||",
                "    feature_session->state() != supernote::runtime::FeatureState::ACTIVE) {",
                "  supernote_throw_error(",
                '      runtime, "FEATURE_CLOSED", "feature is closed");',
                "}",
                "supernote::runtime::FeatureCallScope feature_call_scope("
                "    feature_session);",
            )
            if session_aware
            else (),
        )
        arguments_parameter = (
            "const Value *arguments"
            if method.parameters
            else "const Value *"
        )
        feature_setup = (
            "      auto feature_session = feature_session_.lock();\n"
            if session_aware
            else ""
        )
        feature_capture = ", feature_session" if session_aware else ""
        method_branches.append(
            f"    if (property_name == {json.dumps(method.js_name)}) {{\n"
            f"      {receiver_type} native_instance = instance_;\n"
            f"{feature_setup}"
            "      return Function::createFromHostFunction(\n"
            "          runtime,\n"
            f"          PropNameID::forAscii(runtime, "
            f"{json.dumps(method.js_name)}),\n"
            f"          {len(method.parameters)},\n"
            "          [native_instance = std::move(native_instance)"
            f"{feature_capture}](\n"
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
    if session_aware:
        constructor = f"""  explicit {class_name}(
      supernote::runtime::ManagedRef<{item.cpp_name}> instance,
      std::weak_ptr<supernote::runtime::FeatureSession> feature_session)
      : instance_(std::move(instance)),
        feature_session_(std::move(feature_session)) {{}}"""
        feature_member = (
            "\n  std::weak_ptr<supernote::runtime::FeatureSession> "
            "feature_session_;"
        )
    else:
        constructor = f"""  explicit {class_name}(
      std::shared_ptr<{item.cpp_name}> instance)
      : instance_(std::move(instance)) {{}}"""
        feature_member = ""
    return f"""class {class_name} final : public facebook::jsi::HostObject {{
 public:
{constructor}

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
  {receiver_type} instance_;{feature_member}
}};"""


def _jsi_object_registration(
    module_name: str,
    item: ObjectExport,
    index: int,
    *,
    session_aware: bool,
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
        pre_call=(
            (
                "auto feature_session = weak_feature.lock();",
                "if (!feature_session ||",
                "    feature_session->state() != supernote::runtime::FeatureState::ACTIVE) {",
                "  supernote_throw_error(",
                '      runtime, "FEATURE_CLOSED", "feature is closed");',
                "}",
                "supernote::runtime::FeatureCallScope feature_call_scope("
                "    feature_session);",
            )
            if session_aware
            else ()
        ),
    )
    arguments_parameter = (
        "const Value *arguments"
        if item.constructor.parameters
        else "const Value *"
    )
    factory_result = f"return Value({native_call});"
    managed_receiver = (
        "            auto managed_instance = "
        f"supernote::runtime::ManagedRef<{item.cpp_name}>(\n"
        "                std::move(native_instance),\n"
        "                supernote::runtime::process_services().cleanup());\n"
        if session_aware
        else ""
    )
    receiver_argument = "managed_instance" if session_aware else "native_instance"
    callable_body = callable_body.replace(
        factory_result,
        f"auto native_instance = {native_call};\n"
        f"{managed_receiver}"
        "            return Value(Object::createFromHostObject(\n"
        "                runtime,\n"
        f"                std::make_shared<GeneratedObject{index}HostObject>(\n"
        f"                    std::move({receiver_argument})"
        + (", feature_session" if session_aware else "")
        + ")));",
    )
    factory_capture = (
        "[weak_feature = std::weak_ptr<supernote::runtime::FeatureSession>(\n"
        "             feature_session)]"
        if session_aware
        else "[]"
    )
    return (
        "  {\n"
        "    Object object_type(runtime);\n"
        "    auto create = Function::createFromHostFunction(\n"
        "        runtime,\n"
        "        PropNameID::forAscii(runtime, \"create\"),\n"
        f"        {len(item.constructor.parameters)},\n"
        f"        {factory_capture}(facebook::jsi::Runtime &runtime,\n"
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


def _jsi_sync_registration(
    module_name: str,
    export: Export,
    *,
    feature_scoped: bool,
) -> str:
    expected_parameters = ", ".join(
        _jsi_expected_type(parameter.cpp_type) + f" {parameter.name}"
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
    sync_capture = "[feature_session]" if feature_scoped else "[]"
    sync_scope = (
        "          supernote::runtime::FeatureCallScope feature_call_scope(\n"
        "              feature_session);\n"
        if feature_scoped
        else ""
    )
    return (
        "  {\n"
        "    auto function = Function::createFromHostFunction(\n"
        "        runtime,\n"
        f"        PropNameID::forAscii(runtime, {json.dumps(export.js_name)}),\n"
        f"        {len(export.parameters)},\n"
        f"        {sync_capture}(facebook::jsi::Runtime &runtime,\n"
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
        f"{sync_scope}"
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


def _jsi_export_registrations(
    module_name: str,
    exports: list[Export],
    mode: JsiBindingMode,
) -> list[str]:
    registrations: list[str] = []
    for export in exports:
        try:
            kind = _jsi_registration_kind(
                async_export=export.async_,
                mode=mode,
            )
        except JsiBindingDecisionError as exc:
            raise CodegenError(str(exc)) from exc
        if kind is JsiRegistrationKind.ASYNC:
            registrations.append(_jsi_async_registration(module_name, export))
        else:
            registrations.append(
                _jsi_sync_registration(
                    module_name,
                    export,
                    feature_scoped=mode.feature_scoped,
                )
            )
    return registrations


def _jsi_binding(
    config: dict[str, object],
    exports: list[Export],
    objects: list[ObjectExport],
    *,
    feature_id: str | None = None,
    extra_includes: tuple[str, ...] = (),
    extra_declarations: tuple[str, ...] = (),
    extra_wrappers: tuple[str, ...] = (),
    extra_registrations: tuple[str, ...] = (),
    extra_uses_async: bool = False,
) -> str:
    namespace = str(config["android_namespace"])
    module_name = str(config["module_name"])
    class_prefix = str(config["class_prefix"])
    installer = f"{namespace}.generated.{class_prefix}JsiModule"
    global_name = str(config["jsi_global_name"])
    try:
        mode = _jsi_binding_mode(feature_id)
    except JsiBindingDecisionError as exc:
        raise CodegenError(str(exc)) from exc
    feature_suffix = mode.feature_suffix
    declarations = _cpp_declarations(exports)
    object_includes = "\n".join(
        f'#include "{include}"'
        for include in dict.fromkeys(
            [item.include for item in objects] + list(extra_includes)
        )
    )
    object_include_block = f"{object_includes}\n\n" if object_includes else ""
    object_wrappers = "\n\n".join(
        _jsi_object_wrapper(
            module_name,
            item,
            index,
            session_aware=mode.feature_scoped,
        )
        for index, item in enumerate(objects)
    )
    if extra_wrappers:
        object_wrappers = "\n\n".join(
            filter(None, (object_wrappers, *extra_wrappers))
        )
    object_wrapper_block = f"{object_wrappers}\n\n" if object_wrappers else ""
    registrations = _jsi_export_registrations(module_name, exports, mode)
    registrations.extend(
        _jsi_object_registration(
            module_name,
            item,
            index,
            session_aware=mode.feature_scoped,
        )
        for index, item in enumerate(objects)
    )
    registrations.extend(extra_registrations)
    has_async_methods = any(
        method.async_ for item in objects for method in item.methods
    )
    async_helper_block = (
        _jsi_async_helpers() + "\n\n"
        if _jsi_async_helpers_required(
            mode,
            has_async_export=any(item.async_ for item in exports),
            has_async_method=has_async_methods,
            extra_uses_async=extra_uses_async,
        )
        else ""
    )
    namespace_open = (
        f"namespace supernote::generated::feature_{feature_suffix} {{"
        if feature_id is not None
        else "namespace {"
    )
    bootstrap_constants = ""
    if feature_id is None:
        bootstrap_constants = f"""constexpr char kInstallerClassName[] = {json.dumps(installer)};
constexpr char kGlobalName[] = {json.dumps(global_name)};
"""
    else:
        bootstrap_constants = f"""constexpr char kFeatureRegistryGlobal[] =
    "__supernoteV4FeatureRegistry_63f6999c8c67";
constexpr char kFeatureId[] = {json.dumps(feature_id)};
"""
    value_helpers = _jsi_value_helpers()
    if feature_id is not None:
        value_helpers = value_helpers.replace(
            "auto exports = runtime.global().getPropertyAsObject(runtime, kGlobalName);",
            "auto registry = runtime.global().getPropertyAsObject(\n"
            "      runtime, kFeatureRegistryGlobal);\n"
            "  auto exports = registry.getPropertyAsObject(runtime, kFeatureId);",
        )
    install_name = "register_feature" if feature_id is not None else "install_exports"
    install_parameters = (
        "facebook::jsi::Runtime &runtime,\n"
        "    facebook::jsi::Object &feature_registry,\n"
        "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature_session"
        if feature_id is not None
        else "facebook::jsi::Runtime &runtime"
    )
    install_target = (
        f"feature_registry.setProperty(runtime, {json.dumps(feature_id)}, "
        "std::move(exports));"
        if feature_id is not None
        else "runtime.global().setProperty(runtime, kGlobalName, std::move(exports));"
    )
    if feature_id is not None:
        bootstrap_tail = (
            "\n}  // namespace supernote::generated::feature_"
            f"{feature_suffix}\n"
        )
    else:
        bootstrap_tail = """
jboolean native_install(JNIEnv *, jobject, jlong runtime_pointer) {
  if (runtime_pointer == 0) {
    return JNI_FALSE;
  }
  auto *runtime = reinterpret_cast<facebook::jsi::Runtime *>(
      static_cast<std::uintptr_t>(runtime_pointer));
  try {
    install_exports(*runtime);
    return JNI_TRUE;
  } catch (const std::exception &error) {
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag, "JSI installation failed: %s", error.what());
    return JNI_FALSE;
  } catch (...) {
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag, "JSI installation failed");
    return JNI_FALSE;
  }
}

bool register_installer_natives(JNIEnv *env) {
  jclass thread_class = env->FindClass("java/lang/Thread");
  if (thread_class == nullptr) {
    clear_pending_exception(env, "FindClass(Thread)");
    return false;
  }
  jmethodID current_thread =
      env->GetStaticMethodID(thread_class, "currentThread", "()Ljava/lang/Thread;");
  jmethodID get_loader = env->GetMethodID(
      thread_class, "getContextClassLoader", "()Ljava/lang/ClassLoader;");
  if (current_thread == nullptr || get_loader == nullptr) {
    clear_pending_exception(env, "resolve Thread methods");
    return false;
  }
  jobject thread = env->CallStaticObjectMethod(thread_class, current_thread);
  jobject loader = env->CallObjectMethod(thread, get_loader);
  if (env->ExceptionCheck() || loader == nullptr) {
    clear_pending_exception(env, "get context classloader");
    return false;
  }
  jclass loader_class = env->FindClass("java/lang/ClassLoader");
  jmethodID load_class = loader_class == nullptr
      ? nullptr
      : env->GetMethodID(
            loader_class,
            "loadClass",
            "(Ljava/lang/String;)Ljava/lang/Class;");
  if (load_class == nullptr) {
    clear_pending_exception(env, "resolve ClassLoader.loadClass");
    return false;
  }
  jstring class_name = env->NewStringUTF(kInstallerClassName);
  jobject installer =
      env->CallObjectMethod(loader, load_class, class_name);
  if (env->ExceptionCheck() || installer == nullptr) {
    clear_pending_exception(env, "load plugin installer");
    return false;
  }
  JNINativeMethod methods[] = {
      {const_cast<char *>("nativeInstall"),
        const_cast<char *>("(J)Z"),
        reinterpret_cast<void *>(native_install)},
  };
  return env->RegisterNatives(
             static_cast<jclass>(installer), methods, 1) == JNI_OK;
}

}  // namespace

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *java_vm, void *) {
  JNIEnv *env = nullptr;
  if (java_vm->GetEnv(
          reinterpret_cast<void **>(&env), JNI_VERSION_1_6) != JNI_OK ||
      env == nullptr) {
    return JNI_ERR;
  }
  return register_installer_natives(env) ? JNI_VERSION_1_6 : JNI_ERR;
}
"""
    exception_helper = ""
    if feature_id is None:
        exception_helper = """
void clear_pending_exception(JNIEnv *env, const char *operation) {
  if (!env->ExceptionCheck()) {
    return;
  }
  __android_log_print(
      ANDROID_LOG_ERROR, kLogTag, "%s raised a Java exception", operation);
  env->ExceptionDescribe();
  env->ExceptionClear();
}
"""
    runtime_include = (
        '#include "runtime_services.hpp"\n' if feature_id is not None else ""
    )
    return f"""#include <jni.h>
#include <jsi/jsi.h>
{runtime_include}

#include <android/log.h>

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>
{object_include_block}{declarations}
{chr(10).join(extra_declarations)}

{namespace_open}

constexpr char kLogTag[] = "SupernoteJsi{class_prefix}";
{bootstrap_constants}

{value_helpers}

{async_helper_block}

{object_wrapper_block}{exception_helper}
void {install_name}(
    {install_parameters}) {{
  using facebook::jsi::Function;
  using facebook::jsi::Object;
  using facebook::jsi::PropNameID;
  using facebook::jsi::String;
  using facebook::jsi::Value;

  Object exports(runtime);
{chr(10).join(registrations)}
  {install_target}
}}
{bootstrap_tail}
"""


def render_v4_feature_jsi(
    module_root: Path,
    *,
    module_name: str,
    feature_id: str,
    conversion_digest: str | None = None,
    include_prefix: str | None = None,
) -> str:
    """Render one feature registration unit without owning plugin bootstrap."""

    extra_includes: tuple[str, ...] = ()
    extra_declarations: tuple[str, ...] = ()
    extra_wrappers: tuple[str, ...] = ()
    extra_registrations: tuple[str, ...] = ()
    extra_uses_async = False
    if (module_root / "android/src/main/cpp").is_dir():
        function_sources = scan_cpp_source_model(
            module_root, module_name=module_name
        )
        class_sources = scan_cpp_class_source_model(
            module_root, module_name=module_name
        )
        enum_sources = scan_cpp_enum_source_model(
            module_root, module_name=module_name
        )
        try:
            semantic = project_cpp_api(
                function_sources,
                class_sources,
                enum_sources,
                feature_id=feature_id,
            )
            routes = plan_cpp_routes(
                semantic, function_sources, class_sources, enum_sources
            )
            (
                extra_includes,
                extra_declarations,
                extra_wrappers,
                extra_registrations,
            ) = render_cpp_object_bindings(routes, module_name=module_name)
            if include_prefix is not None:
                extra_includes = tuple(
                    f"{include_prefix.rstrip('/')}/{include}"
                    for include in extra_includes
                )
            extra_uses_async = any(
                route.execution is ExecutionMode.ASYNC
                for route in routes.functions
            ) or any(
                route.execution is ExecutionMode.ASYNC
                for item in routes.objects
                for route in item.methods
            )
        except (CppProjectionError, CppRouteError, SourceModelError, ValueError) as exc:
            raise CodegenError(str(exc)) from exc
        # The V4 route renderer owns every public C++ function, including
        # scalar-only functions.  Keeping the legacy scalar renderer empty is
        # important because it loses namespace ownership and cannot safely
        # distinguish identical starter symbols from separate features.
        bindings = ScannedBindings((), ())
    else:
        bindings = ScannedBindings((), ())
    config: dict[str, object] = {
        "android_namespace": "supernote.generated.v4",
        "module_name": module_name,
        "class_prefix": "V4Feature",
        "jsi_global_name": "__supernoteV4",
    }
    rendered = _jsi_binding(
        config,
        list(bindings.exports),
        list(bindings.objects),
        feature_id=feature_id,
        extra_includes=extra_includes,
        extra_declarations=extra_declarations,
        extra_wrappers=extra_wrappers,
        extra_registrations=extra_registrations,
        extra_uses_async=extra_uses_async,
    )
    if conversion_digest is None:
        return rendered
    if not re.fullmatch(r"[0-9a-f]{64}", conversion_digest):
        raise CodegenError("invalid V4 conversion-plan digest")
    return (
        f"// Supernote V4 conversion plan SHA-256: {conversion_digest}\n"
        "#include <supernote/conversion.hpp>\n"
        "#include <supernote/cpp_objects.hpp>\n"
        + rendered
    )


def render_v4_plugin_jsi(
    feature_ids: list[str],
    *,
    jvm_feature_ids: list[str] | None = None,
) -> str:
    """Render the single plugin registry installed by the runtime bootstrap."""

    validated: list[tuple[str, str]] = []
    for feature_id in feature_ids:
        if not re.fullmatch(r"supernote:feature:[0-9a-f]{16}", feature_id):
            raise CodegenError(f"invalid V4 feature identity {feature_id!r}")
        validated.append(
            (feature_id, feature_id.removeprefix("supernote:feature:"))
        )
    if len({feature_id for feature_id, _ in validated}) != len(validated):
        raise CodegenError("duplicate V4 feature identity in plugin registry")
    jvm_features = set(jvm_feature_ids or ())
    unknown_jvm = jvm_features - {feature_id for feature_id, _ in validated}
    if unknown_jvm:
        raise CodegenError("JVM routes refer to an unknown V4 feature")
    declarations = "\n".join(
        "namespace supernote::generated::feature_"
        f"{suffix} {{\n"
        "void register_feature(facebook::jsi::Runtime &runtime,\n"
        "                      facebook::jsi::Object &feature_registry,\n"
        "                      const std::shared_ptr<\n"
        "                          supernote::runtime::FeatureSession> &feature_session);\n"
        "}"
        for _, suffix in validated
    )
    jvm_declarations = "\n".join(
        "namespace supernote::generated::jvm_feature_"
        f"{suffix} {{\n"
        "void register_jvm_feature(facebook::jsi::Runtime &runtime,\n"
        "                          facebook::jsi::Object &feature_registry,\n"
        "                          const std::shared_ptr<\n"
        "                              supernote::runtime::FeatureSession> &feature_session);\n"
        "}"
        for feature_id, suffix in validated
        if feature_id in jvm_features
    )
    registrations = "\n".join(
        "  {\n"
        "    auto feature_session = supernote::runtime::FeatureSession::create(\n"
        "        runtime_session, supernote::runtime::process_services().cleanup());\n"
        f"    feature_{suffix}::register_feature(\n"
        "        runtime, features, feature_session);\n"
        + (
            f"    jvm_feature_{suffix}::register_jvm_feature(\n"
            "        runtime, features, feature_session);\n"
            if feature_id in jvm_features
            else ""
        )
        + "  }"
        for feature_id, suffix in validated
    )
    return f"""#include <jsi/jsi.h>

#include "runtime_services.hpp"

#include <cstddef>
#include <string>
#include <utility>

{declarations}
{jvm_declarations}

namespace supernote::generated {{
namespace {{

constexpr char kFeatureRegistryGlobal[] =
    "__supernoteV4FeatureRegistry_63f6999c8c67";

[[noreturn]] void throw_type_error(
    facebook::jsi::Runtime &runtime, const std::string &message) {{
  auto constructor =
      runtime.global().getPropertyAsFunction(runtime, "TypeError");
  throw facebook::jsi::JSError(
      runtime, constructor.callAsConstructor(runtime, message));
}}

}}  // namespace

void install_plugin_bindings(
    facebook::jsi::Runtime &runtime,
    const std::shared_ptr<supernote::runtime::RuntimeSession> &runtime_session) {{
  using facebook::jsi::Function;
  using facebook::jsi::Object;
  using facebook::jsi::PropNameID;
  using facebook::jsi::Value;

  Object features(runtime);
{registrations}
  runtime.global().setProperty(
      runtime, kFeatureRegistryGlobal, std::move(features));

  Object public_runtime(runtime);
  auto feature = Function::createFromHostFunction(
      runtime,
      PropNameID::forAscii(runtime, "feature"),
      1,
      [](facebook::jsi::Runtime &runtime,
         const Value &,
         const Value *arguments,
         std::size_t argument_count) -> Value {{
        if (argument_count != 1 || !arguments[0].isString()) {{
          throw_type_error(
              runtime,
              "Supernote V4 runtime feature(id) expects exactly one string");
        }}
        const auto feature_id = arguments[0].asString(runtime).utf8(runtime);
        auto registry = runtime.global().getPropertyAsObject(
            runtime, kFeatureRegistryGlobal);
        auto binding = registry.getProperty(runtime, feature_id.c_str());
        if (binding.isUndefined()) {{
          throw_type_error(
              runtime, "unknown Supernote V4 feature: " + feature_id);
        }}
        return binding;
      }});
  public_runtime.setProperty(runtime, "feature", std::move(feature));
  runtime.global().setProperty(
      runtime, "__supernoteV4", std::move(public_runtime));
}}

}}  // namespace supernote::generated
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
