from __future__ import annotations

import io
import os
from contextlib import contextmanager

import pytest

from supernote_module_generator.interaction import (
    BackRequested,
    CancelRequested,
    InputClosed,
    Interaction,
    InterruptRequested,
    KeyReader,
    MenuItem,
)
from supernote_module_generator.rendering import Renderer, TerminalCapabilities
from supernote_module_generator.terminal_text import terminal_width


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


def test_cursor_menu_has_no_filter_mode_or_filter_hint():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["char:/", "char:b", "enter"]),
    )

    assert ui.menu(
        "Module",
        [MenuItem("alpha", "alpha"), MenuItem("beta", "beta")],
        default="alpha",
    ) == "alpha"
    assert "/ filter" not in stderr.getvalue()
    assert "Filter:" not in stderr.getvalue()


def test_cursor_menu_restores_terminal_before_collapsing_selected_answer():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(render, key_source=source(["down", "enter"]))
    raw = False

    @contextmanager
    def tracked_raw():
        nonlocal raw
        raw = True
        try:
            yield
        finally:
            raw = False

    ui.keys.raw = tracked_raw  # type: ignore[method-assign]
    original_answer = ui.answer

    def answer(label: str, value: str) -> None:
        assert not raw
        original_answer(label, value)

    ui.answer = answer  # type: ignore[method-assign]

    assert ui.menu(
        "Module type",
        [
            MenuItem("native", "Native Module", completed_label="Native Module — Kotlin/Java"),
            MenuItem("jsi", "JSI Module", completed_label="JSI Module — C/C++ (synchronous)"),
        ],
        default="native",
        collapse_label="Module type",
    ) == "jsi"

    output = stderr.getvalue()
    assert "\033[4A" in output  # label plus two choices and footer
    assert output.endswith("Module type:  JSI Module — C/C++ (synchronous)\n\n")


def test_cursor_text_keeps_the_normal_inline_prompt_and_typed_value():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["char:l", "char:o", "char:c", "char:a", "char:l", "enter"]),
    )

    assert ui.text("Package name", guidance="Used as the local folder.") == "local"

    output = stderr.getvalue()
    assert "\033[2A" not in output
    assert output.endswith("Package name: local\r\n")


def test_plain_text_keeps_input_on_the_prompt_line_without_duplicate_answer():
    render, _, stderr = renderer(cursor=False)
    ui = Interaction(
        render,
        stdin=io.StringIO(),
        line_source=lambda: "local-math",
    )

    assert ui.text("Package name") == "local-math"
    assert stderr.getvalue() == "Package name: local-math\n"


def test_plain_default_stays_dimless_and_inline_when_enter_accepts_it():
    render, _, stderr = renderer(cursor=False)
    ui = Interaction(
        render,
        stdin=io.StringIO(),
        line_source=lambda: "\n",
    )

    assert ui.text("JavaScript name", default="Math", ghost_default=True) == "Math"
    assert stderr.getvalue() == "JavaScript name [Math]: \n"


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX byte reader")
def test_utf8_keyboard_input_is_read_as_one_unicode_scalar():
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.write(write_descriptor, "🙂".encode("utf-8"))
    finally:
        os.close(write_descriptor)

    with os.fdopen(read_descriptor, "r", encoding="utf-8") as stream:
        reader = KeyReader(stream, io.StringIO())
        assert reader.read() == "char:🙂"


def test_cursor_uses_terminal_cells_for_wide_and_combining_text():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["paste:A🙂B", "left", "left", "enter"]),
    )

    assert ui.text("Description", optional=True) == "A🙂B"
    assert "\033[3D" in stderr.getvalue()
    assert terminal_width("A🙂B") == 4
    assert terminal_width("e\u0301") == 1
    assert terminal_width("👩\u200d💻") == 2


def test_back_clears_active_cursor_menu_and_text_blocks():
    menu_render, _, menu_stderr = renderer(cursor=True)
    menu_ui = Interaction(menu_render, key_source=source(["escape"]))
    with pytest.raises(BackRequested):
        menu_ui.menu("Module type", [MenuItem("a", "A")], default="a")
    assert menu_stderr.getvalue().endswith("\033[3A\r")

    text_render, _, text_stderr = renderer(cursor=True)
    text_ui = Interaction(text_render, key_source=source(["escape"]))
    with pytest.raises(BackRequested):
        text_ui.text("Package name", guidance="Used as the local folder.")
    assert text_stderr.getvalue().endswith("\033[2A\r")


def test_cursor_confirmation_keeps_the_normal_inline_default_prompt():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(render, key_source=source(["enter"]))

    assert not ui.confirm("Run an Android build too?", default=False)
    assert stderr.getvalue() == "Run an Android build too? [y/N]: \r\n"


def test_cursor_default_suggestion_is_dim_in_input_and_enter_accepts_it():
    stdout = io.StringIO()
    stderr = io.StringIO()
    capabilities = TerminalCapabilities(True, True, True, True, 80, 24)
    render = Renderer("human", capabilities, stdout=stdout, stderr=stderr)
    ui = Interaction(render, key_source=source(["enter"]))

    assert ui.text(
        "JavaScript name",
        default="Math",
        ghost_default=True,
    ) == "Math"

    assert "JavaScript name: \033[2mMath\033[0m" in stderr.getvalue()


def test_cursor_default_suggestion_disappears_when_typing_and_returns_when_cleared():
    stdout = io.StringIO()
    stderr = io.StringIO()
    capabilities = TerminalCapabilities(True, True, True, True, 80, 24)
    render = Renderer("human", capabilities, stdout=stdout, stderr=stderr)
    ui = Interaction(
        render,
        key_source=source(["char:X", "clear", "char:C", "enter"]),
    )

    assert ui.text(
        "JavaScript name",
        default="Math",
        ghost_default=True,
    ) == "C"

    output = stderr.getvalue()
    assert "JavaScript name: \033[2mMath\033[0m" in output
    assert "\r\033[2KJavaScript name: X" in output
    assert output.count("JavaScript name: \033[2mMath\033[0m") >= 2


def test_menu_explanation_is_dim():
    stdout = io.StringIO()
    stderr = io.StringIO()
    capabilities = TerminalCapabilities(True, True, True, True, 80, 24)
    render = Renderer("human", capabilities, stdout=stdout, stderr=stderr)
    ui = Interaction(render, key_source=source(["down", "enter"]))

    assert ui.menu(
        "Module type",
        [
            MenuItem(
                "native",
                "Native Module",
                explanation=(
                    "For Kotlin/Java code and Android APIs through the React Native "
                    "bridge."
                ),
            ),
            MenuItem(
                "jsi",
                "JSI Module",
                explanation=(
                    "Synchronous C++; requires target PluginHost support."
                ),
            ),
        ],
        default="native",
    ) == "jsi"

    assert "\033[2m    Synchronous C++; requires target PluginHost support.\033[0m" in (
        stderr.getvalue()
    )

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


def test_multiline_paste_discards_only_the_paste_and_preserves_typed_text():
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["char:a", "paste:first\nsecond", "char:d", "enter"]),
    )

    assert ui.text("Description", optional=True) == "ad"
    assert stderr.getvalue().count(
        "This field accepts one line. Paste a single value."
    ) == 1


def test_plain_multiline_input_is_rejected_as_one_value():
    render, _, stderr = renderer(cursor=False)
    values = iter(["first\nsecond", "valid"])
    ui = Interaction(render, line_source=lambda: next(values))
    assert ui.text("Package name") == "valid"
    assert "This field accepts one line. Paste a single value." in stderr.getvalue()


def test_plain_menu_rejects_multiline_input_with_the_canonical_message():
    render, _, stderr = renderer(cursor=False)
    values = iter(["1\n2", "1"])
    ui = Interaction(render, line_source=lambda: next(values))

    assert ui.menu("Module type", [MenuItem("native", "Native Module")], default="native") == "native"
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


def test_plain_multi_menu_validates_defaults_and_all_control_paths():
    render, _, stderr = renderer(cursor=False)
    items = [MenuItem("cpp", "C++"), MenuItem("kotlin", "Kotlin")]

    with pytest.raises(ValueError, match="defaults must be listed"):
        Interaction(render).multi_menu("Starters", items, defaults=["java"])

    values = iter(["bad", "1, 2"])
    assert Interaction(render, line_source=lambda: next(values)).multi_menu(
        "Starters", items, defaults=[]
    ) == ["cpp", "kotlin"]
    assert "Select at least one listed number" in stderr.getvalue()

    assert Interaction(render, line_source=lambda: "\n").multi_menu(
        "Starters", items, defaults=["kotlin"]
    ) == ["kotlin"]
    with pytest.raises(BackRequested):
        Interaction(render, line_source=lambda: ":back").multi_menu(
            "Starters", items, defaults=[]
        )
    with pytest.raises(CancelRequested):
        Interaction(render, line_source=lambda: ":cancel").multi_menu(
            "Starters", items, defaults=[]
        )


def test_cursor_multi_menu_toggles_warns_and_handles_terminal_controls():
    items = [MenuItem("cpp", "C++", "native"), MenuItem("kotlin", "Kotlin")]
    render, _, stderr = renderer(cursor=True)
    ui = Interaction(
        render,
        key_source=source(["enter", "char: ", "down", "char: ", "enter"]),
    )
    assert ui.multi_menu("Starters", items, defaults=[]) == ["cpp", "kotlin"]
    assert "Select at least one starter" in stderr.getvalue()

    for event, exception in (("cancel", InterruptRequested), ("eof", InputClosed), ("escape", BackRequested)):
        render, _, _ = renderer(cursor=True)
        with pytest.raises(exception):
            Interaction(render, key_source=source([event])).multi_menu(
                "Starters", items, defaults=["cpp"]
            )


def test_plain_menu_accepts_package_name_default_and_control_values():
    items = [
        MenuItem("alpha", "Alpha", "first", plain_description="plain first"),
        MenuItem("beta", "Beta", explanation="A long explanatory line."),
    ]
    render, _, stderr = renderer(cursor=False)
    answers = iter(["missing", "beta"])
    assert Interaction(render, line_source=lambda: next(answers)).menu(
        "Module", items, default="alpha", collapse_label="Selected"
    ) == "beta"
    assert "exact package name" in stderr.getvalue()
    assert "plain first" in stderr.getvalue()

    assert Interaction(render, line_source=lambda: "\n").menu(
        "Action", items, default="alpha"
    ) == "alpha"
    with pytest.raises(CancelRequested):
        Interaction(render, line_source=lambda: ":cancel").menu(
            "Action", items, default=None
        )


def test_cursor_menu_scrolls_and_handles_missing_selection_controls(monkeypatch):
    monkeypatch.setattr(
        "supernote_module_generator.interaction.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((60, 12)),
    )
    items = [
        MenuItem(
            str(index),
            f"Item {index}",
            "description",
            explanation="explanation",
            separator_before=index == 4,
        )
        for index in range(10)
    ]
    render, _, stderr = renderer(cursor=True)
    events = ["down"] * 8 + ["enter"]
    assert Interaction(render, key_source=source(events)).menu(
        "Item", items, default="0"
    ) == "8"
    assert "more" in stderr.getvalue()

    render, _, _ = renderer(cursor=True)
    assert Interaction(render, key_source=source(["up", "enter"])).menu(
        "Item", items[:2], default=None
    ) == "1"
    for event, exception in (("cancel", InterruptRequested), ("eof", InputClosed)):
        render, _, _ = renderer(cursor=True)
        with pytest.raises(exception):
            Interaction(render, key_source=source([event])).menu(
                "Item", items[:2], default="0"
            )


def test_plain_text_retries_normalization_validation_and_required_value():
    render, _, stderr = renderer(cursor=False)
    values = iter(["bad", "\n", " value ", "good"])

    def normalize(value: str) -> str:
        if value == "bad":
            raise ValueError("normalize failed")
        return value.strip()

    def validate(value: str) -> None:
        if value != "good":
            raise ValueError("validation failed")

    assert Interaction(render, line_source=lambda: next(values)).text(
        "Value", guidance="Use good.", normalize=normalize, validate=validate
    ) == "good"
    output = stderr.getvalue()
    assert "normalize failed" in output
    assert "Enter a value" in output
    assert "validation failed" in output


def test_plain_text_cancel_optional_and_input_closed_paths():
    render, _, _ = renderer(cursor=False)
    assert Interaction(render, line_source=lambda: "\n").text(
        "Optional", optional=True
    ) == ""
    with pytest.raises(CancelRequested):
        Interaction(render, line_source=lambda: ":cancel").text("Value")
    with pytest.raises(InputClosed):
        Interaction(render, line_source=lambda: "").text("Value")

    def eof() -> str:
        raise EOFError

    with pytest.raises(InputClosed):
        Interaction(render, line_source=eof).text("Value")


def test_cursor_text_editing_and_retry_paths():
    render, _, _ = renderer(cursor=True)
    events = [
        "home",
        "right",
        "end",
        "left",
        "backspace",
        "delete",
        "char:oops",
        "enter",
        "clear",
        "paste:good",
        "enter",
    ]

    def validate(value: str) -> None:
        if value != "good":
            raise ValueError("try again")

    assert Interaction(render, key_source=source(events)).text(
        "Value", default="abc", validate=validate
    ) == "good"


@pytest.mark.parametrize(
    ("event", "exception"),
    [("cancel", InterruptRequested), ("eof", InputClosed), ("escape", BackRequested)],
)
def test_cursor_text_restores_terminal_before_control_exception(event, exception):
    render, _, stderr = renderer(cursor=True)
    with pytest.raises(exception):
        Interaction(render, key_source=source([event])).text(
            "Value", guidance="Guidance"
        )
    assert "\033[" in stderr.getvalue()


def test_confirmation_and_typed_confirmation_cover_all_answers():
    render, _, stderr = renderer(cursor=False)
    answers = iter(["maybe", "yes", "no", "\n"])
    ui = Interaction(render, line_source=lambda: next(answers))
    assert ui.confirm("Continue?", default=False)
    assert not ui.confirm("Continue?", default=True)
    assert ui.confirm("Continue?", default=True)
    assert "Enter yes or no" in stderr.getvalue()

    with pytest.raises(BackRequested):
        Interaction(render, line_source=lambda: ":back").confirm("Continue?", default=False)
    with pytest.raises(CancelRequested):
        Interaction(render, line_source=lambda: ":cancel").confirm("Continue?", default=False)

    typed = iter(["\n", "wrong"])
    assert not Interaction(render, line_source=lambda: next(typed)).typed_confirmation(
        "Type value: ", "value"
    )
    with pytest.raises(BackRequested):
        Interaction(render, line_source=lambda: ":back").typed_confirmation("", "x")
    with pytest.raises(CancelRequested):
        Interaction(render, line_source=lambda: ":cancel").typed_confirmation("", "x")


def test_wait_for_return_handles_plain_cursor_and_terminal_endings():
    render, _, _ = renderer(cursor=False)
    Interaction(render, line_source=lambda: "\n").wait_for_return()

    render, _, _ = renderer(cursor=True)
    Interaction(render, key_source=source(["char:x", "escape"])).wait_for_return()
    with pytest.raises(InterruptRequested):
        Interaction(render, key_source=source(["cancel"])).wait_for_return()
    with pytest.raises(InputClosed):
        Interaction(render, key_source=source(["eof"])).wait_for_return()


def test_cursor_raw_line_edits_rejects_multiline_paste_and_controls():
    render, _, stderr = renderer(cursor=True)
    events = [
        "char:a",
        "backspace",
        "paste:x\ny",
        "paste:ok",
        "clear",
        "char:y",
        "enter",
    ]
    assert Interaction(render, key_source=source(events)).confirm(
        "Continue?", default=False
    )
    assert "accepts one line" in stderr.getvalue()

    for event, exception in (("cancel", InterruptRequested), ("eof", InputClosed), ("escape", BackRequested)):
        render, _, _ = renderer(cursor=True)
        with pytest.raises(exception):
            Interaction(render, key_source=source([event])).confirm(
                "Continue?", default=False
            )
