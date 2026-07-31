from __future__ import annotations

import io
from collections.abc import Iterator

import pytest

from supernote_module_generator.interaction import BackRequested, Interaction, MenuItem
from supernote_module_generator.rendering import Renderer, TerminalCapabilities


def renderer(*, cursor: bool) -> tuple[Renderer, io.StringIO, io.StringIO]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    capabilities = TerminalCapabilities(
        interactive=True,
        cursor=cursor,
        color=False,
        unicode=cursor,
        columns=80,
        lines=24,
    )
    return Renderer("human", capabilities, stdout=stdout, stderr=stderr), stdout, stderr


def source(events: list[str]):
    iterator = iter(events)
    return lambda: next(iterator)


def test_cursor_menu_stops_at_boundaries_and_has_no_q_binding():
    render, _, _ = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["up", "char:q", "down", "down", "down", "enter"]),
    )
    assert ui.menu(
        "Module type",
        [MenuItem("a", "A"), MenuItem("b", "B"), MenuItem("c", "C")],
        default="a",
    ) == "c"


def test_filter_starts_only_after_slash_and_esc_clears_before_back():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(
            ["char:m", "char:/", "char:z", "escape", "char:/", "char:b", "enter"]
        ),
    )
    result = ui.menu(
        "Module",
        [MenuItem("alpha", "alpha"), MenuItem("beta", "beta")],
        default="alpha",
        searchable=True,
    )
    assert result == "beta"
    assert "No matching modules." in stderr.getvalue()


@pytest.mark.parametrize("query", ["stylusapi", "local_modules/stylus-jsi"])
def test_module_filter_matches_javascript_name_and_relative_path(query: str):
    render, _, _ = renderer(cursor=True)
    events = ["char:/", *(f"char:{character}" for character in query), "enter"]
    ui = Interaction(render, key_source=source(events))

    result = ui.menu(
        "Module",
        [
            MenuItem(
                "local-math",
                "local-math",
                "Native Module",
                ("Math", "local_modules/local-math"),
            ),
            MenuItem(
                "stylus-jsi",
                "stylus-jsi",
                "JSI Module",
                ("StylusApi", "local_modules/stylus-jsi"),
            ),
        ],
        default="local-math",
        searchable=True,
    )

    assert result == "stylus-jsi"


def test_no_initial_selection_enter_does_nothing_and_warns():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(render, key_source=source(["enter", "down", "enter"]))
    assert ui.menu(
        "Package manager",
        [MenuItem("npm", "npm"), MenuItem("yarn", "Yarn")],
        default=None,
    ) == "npm"
    assert "Select npm or Yarn" in stderr.getvalue()


def test_multiline_bracketed_paste_is_fully_rejected():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["paste:first\nsecond", "char:v", "char:a", "char:l", "char:i", "char:d", "enter"]),
    )
    assert ui.text("Package name") == "valid"
    output = stderr.getvalue()
    assert "This field accepts one line. Paste a single value." in output
    assert "first" not in output
    assert "second" not in output


def test_plain_multiline_input_is_rejected_as_one_value():
    render, _, stderr = renderer(cursor=False)
    values = iter(["first\nsecond", "valid"])
    ui = Interaction(render, line_source=lambda: next(values))
    assert ui.text("Package name") == "valid"
    assert "This field accepts one line. Paste a single value." in stderr.getvalue()


def test_plain_bracketed_single_line_paste_returns_only_payload():
    render, _, _ = renderer(cursor=False)
    ui = Interaction(
        render,
        line_source=lambda: "\x1b[200~local-math\x1b[201~\n",
    )
    assert ui.text("Package name") == "local-math"


def test_plain_typed_confirmation_rejects_multiline_paste_and_stays_on_field():
    render, _, stderr = renderer(cursor=False)
    values = iter(["local-math\nREMOVE ALL", "local-math"])
    ui = Interaction(render, line_source=lambda: next(values))
    assert ui.typed_confirmation('Type "local-math" to continue: ', "local-math")
    assert "This field accepts one line. Paste a single value." in stderr.getvalue()


def test_plain_ordinary_back_and_q_are_field_data():
    render, _, _ = renderer(cursor=False)
    values = iter(["back", "q"])
    ui = Interaction(render, line_source=lambda: next(values))
    assert ui.text("Description", optional=True) == "back"
    assert ui.text("Description", optional=True) == "q"


def test_plain_colon_back_is_control():
    render, _, _ = renderer(cursor=False)
    ui = Interaction(render, line_source=lambda: ":back")
    with pytest.raises(BackRequested):
        ui.text("Package name")
