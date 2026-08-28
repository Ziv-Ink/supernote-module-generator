from __future__ import annotations

from supernote_module_generator.cpp_lexer import _lex_source


def test_directive_comments_strings_and_conditional_depth_are_exact():
    source = (
        "#if FLAG \\\n"
        "  && MORE\n"
        "namespace demo {\n"
        'const char *ignored = R"tag(// fake\nbrace {)tag";\n'
        "// marker\n"
        "int value; // trailing\n"
        "}\n"
        "#endif\n"
    )

    lexed = _lex_source(source)

    assert [item.name for item in lexed.directives] == ["if", "endif"]
    assert [item.line for item in lexed.directives] == [1, 9]
    assert lexed.directives[0].start == 0
    assert lexed.directives[0].end == source.index("namespace")
    assert lexed.directives[1].start == source.index("#endif")
    assert lexed.directives[1].end == len(source)
    assert [item.text for item in lexed.comments] == [" marker", " trailing"]
    assert [item.line for item in lexed.comments] == [6, 7]
    assert [item.line_only for item in lexed.comments] == [True, False]
    assert all(item.conditional_depth == 1 for item in lexed.comments)
    assert all(item.brace_depth == 0 for item in lexed.comments)
    assert [item.value for item in lexed.tokens] == [
        "namespace",
        "demo",
        "{",
        "const",
        "char",
        "*",
        "ignored",
        "=",
        "<string>",
        ";",
        "int",
        "value",
        ";",
        "}",
    ]
    assert all(item.conditional_depth == 1 for item in lexed.tokens)
    assert all(item.brace_depth == 0 for item in lexed.tokens)


def test_visible_braces_update_following_token_and_comment_depth():
    source = "{ // inside\nidentifier\n}\n"

    lexed = _lex_source(source)

    assert [(item.value, item.brace_depth) for item in lexed.tokens] == [
        ("{", 0),
        ("identifier", 1),
        ("}", 1),
    ]
    assert len(lexed.comments) == 1
    assert lexed.comments[0].text == " inside"
    assert lexed.comments[0].brace_depth == 1
    assert not lexed.comments[0].line_only


def test_raw_and_quoted_multiline_string_line_semantics_remain_exact():
    source = 'R"(raw\nvalue)" "quoted\nvalue" tail'

    lexed = _lex_source(source)

    assert [(item.value, item.kind, item.line) for item in lexed.tokens] == [
        ("<string>", "string", 1),
        ("<string>", "string", 3),
        ("tail", "identifier", 3),
    ]
    assert source[lexed.tokens[0].start : lexed.tokens[0].end] == 'R"(raw\nvalue)"'
    assert source[lexed.tokens[1].start : lexed.tokens[1].end] == '"quoted\nvalue"'


def test_offsets_are_decoded_source_string_indices_after_non_ascii_text():
    source = "é x"

    lexed = _lex_source(source)

    token = next(item for item in lexed.tokens if item.value == "x")
    assert (token.start, token.end) == (2, 3)
    assert source[token.start : token.end] == "x"
    assert len(source[: token.start].encode("utf-8")) == 3
