from __future__ import annotations

import pytest

from supernote_module_generator.cpp_declarations import (
    ClassStackKind,
    CppDeclarationError,
    class_stack_route,
    first_unconsumed_marker,
    intent_from_stack,
    member_marker_bindings,
    marker_entries,
    marker_stacks,
    namespace_at,
    source_marker,
    unmarked_class_owner_offsets,
    validate_marker_stack_location,
)
from supernote_module_generator.cpp_lexer import _lex_source
from supernote_module_generator.cpp_members import member_declarations
from supernote_module_generator.source_models import (
    DeclarationTarget,
    SupernoteMarker,
)


def _stacks(source: str):
    lexed = _lex_source(source)
    return lexed, marker_stacks(source, marker_entries(lexed))


def test_marker_recognition_excludes_old_object_annotation_and_plain_prose():
    lexed = _lex_source(
        "// @SupernoteExportObject\n"
        "// prose mentions @SupernotePluginExport here\n"
        "// @SupernotePluginExport\n"
    )

    assert [source_marker(comment) for comment in lexed.comments] == [
        (False, None),
        (False, None),
        (True, SupernoteMarker.EXPORT),
    ]
    assert marker_entries(lexed) == [
        (lexed.comments[2], SupernoteMarker.EXPORT)
    ]


@pytest.mark.parametrize(
    ("source", "line", "message"),
    [
        (
            "// @SupernotePluginUnknown\n",
            1,
            "unknown Supernote marker 'SupernotePluginUnknown'",
        ),
        (
            "\n// @SupernotePluginExport(name = \"renamed\")\n",
            2,
            "malformed Supernote marker",
        ),
    ],
)
def test_marker_entries_report_source_line_and_policy(
    source: str, line: int, message: str
):
    with pytest.raises(CppDeclarationError, match=message) as raised:
        marker_entries(_lex_source(source))

    assert raised.value.line == line
    assert raised.value.export_name is None


def test_marker_stacks_split_only_on_non_whitespace_source():
    source = (
        "// @SupernotePluginExport\n"
        "\n"
        "// @SupernotePluginAsync\n"
        "int separator;\n"
        "// @SupernotePluginInternal\n"
    )

    _, stacks = _stacks(source)

    assert [stack.markers for stack in stacks] == [
        (SupernoteMarker.EXPORT, SupernoteMarker.ASYNC),
        (SupernoteMarker.INTERNAL,),
    ]
    assert stacks[0].first.line == 1
    assert stacks[0].last.line == 3
    assert stacks[1].first.line == 5


def test_intent_validation_retains_exact_diagnostic_marker_and_export_name():
    _, stacks = _stacks(
        "// @SupernotePluginExport\n"
        "// @SupernotePluginAsync\n"
        "// @SupernotePluginExport\n"
    )

    with pytest.raises(CppDeclarationError) as raised:
        intent_from_stack(stacks[0], DeclarationTarget.FUNCTION, "pending")

    assert raised.value.line == 3
    assert raised.value.export_name == "pending"
    assert raised.value.message == "duplicate SupernotePluginExport marker"


def test_location_validation_is_source_located_for_each_policy():
    trailing_source = "int value; // @SupernotePluginExport\n"
    _, trailing = _stacks(trailing_source)
    with pytest.raises(CppDeclarationError) as trailing_error:
        validate_marker_stack_location(
            trailing[0], brace_depth=0, description="function"
        )
    assert trailing_error.value.message == (
        "a Supernote marker must be a // comment on its own line"
    )

    conditional_source = "#if FLAG\n// @SupernotePluginExport\n#endif\n"
    _, conditional = _stacks(conditional_source)
    with pytest.raises(CppDeclarationError) as conditional_error:
        validate_marker_stack_location(
            conditional[0], brace_depth=0, description="function"
        )
    assert "preprocessor conditional" in conditional_error.value.message

    nested_source = "class Example {\n// @SupernotePluginExport\n};\n"
    _, nested = _stacks(nested_source)
    with pytest.raises(CppDeclarationError) as nested_error:
        validate_marker_stack_location(
            nested[0],
            brace_depth=0,
            description="free-function",
            export_name="pending",
            brace_message="free functions require namespace scope",
        )
    assert nested_error.value.export_name == "pending"
    assert nested_error.value.message == "free functions require namespace scope"


def test_namespace_discovery_preserves_nested_and_compound_namespaces():
    source = (
        "namespace outer {\n"
        "namespace middle::inner {\n"
        "// @SupernotePluginExport\n"
        "double value() { return 1.0; }\n"
        "}\n"
        "}\n"
    )
    lexed = _lex_source(source)
    marker = lexed.comments[0]

    assert namespace_at(lexed, marker.start) == (
        ("outer", "middle", "inner"),
        2,
    )


def _class_member_inputs(source: str):
    lexed, stacks = _stacks(source)
    tokens = [token for token in lexed.tokens if token.conditional_depth == 0]
    opening_index = next(
        index for index, token in enumerate(tokens) if token.value == "{"
    )
    closing_index = next(
        index
        for index, token in enumerate(tokens[opening_index + 1 :], opening_index + 1)
        if token.value == "}" and token.brace_depth == tokens[opening_index].brace_depth + 1
    )
    opening = tokens[opening_index]
    closing = tokens[closing_index]
    declarations = member_declarations(
        tokens[opening_index + 1 : closing_index],
        default_access="private",
    )
    return lexed, stacks, opening, closing, declarations


def test_member_marker_bindings_route_stacks_and_record_consumed_offsets():
    source = (
        "// @SupernotePluginObject\n"
        "class Audit {\n"
        "public:\n"
        "// @SupernotePluginExport\n"
        "double value() const;\n"
        "};\n"
    )
    lexed, stacks, opening, closing, declarations = _class_member_inputs(source)

    result = member_marker_bindings(
        source,
        declarations,
        stacks,
        class_stack=stacks[0],
        opening_end=opening.end,
        closing_start=closing.start,
        member_depth=opening.brace_depth + 1,
        class_name="Audit",
    )

    assert result.stacks_by_declaration == (
        (declarations[0][1][0].start, stacks[1]),
    )
    assert result.consumed_comment_offsets == frozenset(
        comment.start for comment in lexed.comments
    )


@pytest.mark.parametrize(
    ("source", "line", "export_name", "message"),
    [
        (
            "class Audit {\npublic:\n// @SupernotePluginExport\n};\n",
            3,
            "Audit",
            "member marker stack must be followed by a member declaration",
        ),
        (
            "class Audit {\npublic:\n// @SupernotePluginExport\n"
            "#define BETWEEN 1\ndouble value();\n};\n",
            3,
            "Audit",
            "only whitespace may appear between the final member marker",
        ),
        (
            "class Audit {\npublic:\ndouble outer() {\n"
            "// @SupernotePluginExport\nreturn 1.0;\n}\n};\n",
            4,
            None,
            "class member marker must be at brace depth 1",
        ),
    ],
)
def test_member_marker_bindings_preserve_error_precedence_and_context(
    source: str,
    line: int,
    export_name: str | None,
    message: str,
):
    _lexed, stacks, opening, closing, declarations = _class_member_inputs(source)

    with pytest.raises(CppDeclarationError, match=message) as raised:
        member_marker_bindings(
            source,
            declarations,
            stacks,
            class_stack=None,
            opening_end=opening.end,
            closing_start=closing.start,
            member_depth=opening.brace_depth + 1,
            class_name="Audit",
        )

    assert raised.value.line == line
    assert raised.value.export_name == export_name


@pytest.mark.parametrize(
    ("source", "expected_kind", "following"),
    [
        (
            "// @SupernotePluginObject\nclass Audit {};\n",
            ClassStackKind.CLASS,
            "class",
        ),
        (
            "// @SupernotePluginValue\nenum class State {};\n",
            ClassStackKind.ENUM,
            "enum",
        ),
        (
            "class Owner {\n// @SupernotePluginExport\nint value;\n};\n",
            ClassStackKind.IGNORE,
            None,
        ),
    ],
)
def test_class_stack_route_classifies_top_level_and_nested_markers(
    source: str,
    expected_kind: ClassStackKind,
    following: str | None,
):
    lexed, stacks = _stacks(source)

    route = class_stack_route(lexed, stacks[0])

    assert route.kind is expected_kind
    assert (route.following.value if route.following is not None else None) == following


def test_class_stack_route_rejects_nested_marked_type_with_source_context():
    source = (
        "class Owner {\n"
        "// @SupernotePluginObject\n"
        "class Nested {};\n"
        "};\n"
    )
    lexed, stacks = _stacks(source)

    with pytest.raises(CppDeclarationError) as raised:
        class_stack_route(lexed, stacks[0])

    assert raised.value.line == 2
    assert raised.value.export_name is None
    assert raised.value.message == (
        "marked C++ types must be at global or named-namespace brace depth; "
        "anonymous namespaces and nested types are unsupported"
    )


def test_unmarked_owner_selection_and_unconsumed_marker_detection():
    source = (
        "// @SupernotePluginObject\n"
        "class Marked {};\n"
        "class Owner {\n"
        "public:\n"
        "// @SupernotePluginExport\n"
        "double value() const;\n"
        "};\n"
    )
    lexed, stacks = _stacks(source)
    tokens = [token for token in lexed.tokens if token.conditional_depth == 0]
    extents = []
    for index, token in enumerate(tokens):
        if token.value != "class":
            continue
        opening = next(item for item in tokens[index + 1 :] if item.value == "{")
        closing = next(
            item
            for item in tokens[index + 1 :]
            if item.value == "}" and item.brace_depth == opening.brace_depth + 1
        )
        extents.append((token, opening, closing))
    consumed = {comment.start for comment in stacks[0].comments}

    owners = unmarked_class_owner_offsets(
        stacks,
        consumed,
        extents,
        {extents[0][0].start},
    )
    unconsumed = first_unconsumed_marker(marker_entries(lexed), consumed)

    assert owners == (extents[1][0].start,)
    assert unconsumed is stacks[1].first
