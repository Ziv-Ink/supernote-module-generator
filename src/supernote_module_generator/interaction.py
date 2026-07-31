"""Accessible line and capable-terminal interaction with explicit events."""
from __future__ import annotations

import os
import select
import shutil
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, IO, Iterator, List, Optional, Sequence

from .rendering import Renderer


class BackRequested(Exception):
    pass


class CancelRequested(Exception):
    pass


class InterruptRequested(Exception):
    pass


class InputClosed(Exception):
    pass


@dataclass(frozen=True)
class MenuItem:
    value: str
    label: str
    description: str = ""
    search_terms: Sequence[str] = field(default_factory=tuple)
    separator_before: bool = False
    completed_label: Optional[str] = None


class KeyReader:
    """Read normalized key events and restore terminal state on every path."""

    def __init__(
        self,
        stream: IO[str],
        terminal: IO[str],
        source: Optional[Callable[[], str]] = None,
    ) -> None:
        self.stream = stream
        self.terminal = terminal
        self.source = source

    @contextmanager
    def raw(self) -> Iterator[None]:
        if self.source is not None or os.name == "nt":
            yield
            return
        import termios
        import tty

        descriptor = self.stream.fileno()
        previous = termios.tcgetattr(descriptor)
        self.terminal.write("\033[?2004h")
        self.terminal.flush()
        try:
            tty.setraw(descriptor)
            yield
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
            self.terminal.write("\033[?2004l\033[?25h\033[0m")
            self.terminal.flush()

    def read(self) -> str:
        if self.source is not None:
            return self.source()
        if os.name == "nt":
            import msvcrt

            first = msvcrt.getwch()
            if first in {"\x00", "\xe0"}:
                return {"H": "up", "P": "down", "K": "left", "M": "right", "S": "delete", "G": "home", "O": "end"}.get(msvcrt.getwch(), "ignore")
            return _single_event(first)
        descriptor = self.stream.fileno()
        first = os.read(descriptor, 1).decode("utf-8", errors="ignore")
        if not first:
            return "eof"
        if first != "\x1b":
            return _single_event(first)
        sequence = self._escape_sequence(descriptor)
        if sequence == "[200~":
            return "paste:" + self._paste(descriptor)
        return {
            "": "escape",
            "[A": "up",
            "[B": "down",
            "[C": "right",
            "[D": "left",
            "[H": "home",
            "[F": "end",
            "[3~": "delete",
        }.get(sequence, "ignore")

    def _escape_sequence(self, descriptor: int) -> str:
        data = ""
        for _ in range(5):
            ready, _, _ = select.select([descriptor], [], [], 0.03)
            if not ready:
                break
            character = os.read(descriptor, 1).decode("utf-8", errors="ignore")
            data += character
            if character.isalpha() or character == "~":
                break
        return data

    def _paste(self, descriptor: int) -> str:
        marker = b"\x1b[201~"
        data = bytearray()
        while not data.endswith(marker):
            chunk = os.read(descriptor, 1)
            if not chunk:
                break
            data.extend(chunk)
        if data.endswith(marker):
            del data[-len(marker) :]
        return data.decode("utf-8", errors="replace")


def _single_event(character: str) -> str:
    return {
        "\r": "enter",
        "\n": "enter",
        "\x03": "cancel",
        "\x04": "eof",
        "\x1b": "escape",
        "\x7f": "backspace",
        "\b": "backspace",
        "\x15": "clear",
        "\x17": "word_backspace",
    }.get(character, f"char:{character}" if character.isprintable() else "ignore")


class Interaction:
    def __init__(
        self,
        renderer: Renderer,
        *,
        stdin: IO[str] = sys.stdin,
        key_source: Optional[Callable[[], str]] = None,
        line_source: Optional[Callable[[], str]] = None,
    ) -> None:
        self.renderer = renderer
        self.stdin = stdin
        self.terminal = renderer.stderr
        self.keys = KeyReader(stdin, self.terminal, key_source)
        self.line_source = line_source
        self.plain = not renderer.capabilities.cursor

    def header(self, command: Optional[str] = None) -> None:
        print(self.renderer.style("heading", "Supernote Module Generator"), file=self.terminal)
        if command:
            print(f"\n{self.renderer.style('heading', command)}", file=self.terminal)

    def supporting_answer(self, label: str, value: str) -> None:
        print(self.renderer.style("dim", f"{label}:  {value}"), file=self.terminal)

    def answer(self, label: str, value: str) -> None:
        print(f"{label}:  {value}", file=self.terminal)

    def info(self, text: str, *, dim: bool = False) -> None:
        print(self.renderer.style("dim" if dim else "bold", text), file=self.terminal)

    def warning(self, text: str) -> None:
        print(self.renderer.style("warning", f"{self.renderer.symbols['warning']} {text}"), file=self.terminal)

    def error(self, text: str) -> None:
        print(self.renderer.style("error", text), file=self.terminal)

    def menu(
        self,
        label: str,
        items: Sequence[MenuItem],
        *,
        default: Optional[str],
        searchable: bool = False,
        footer: str = "Esc back",
        collapse_label: Optional[str] = None,
    ) -> str:
        if self.plain:
            return self._plain_menu(label, items, default, searchable, collapse_label)
        return self._cursor_menu(label, items, default, searchable, footer, collapse_label)

    def _plain_menu(
        self,
        label: str,
        items: Sequence[MenuItem],
        default: Optional[str],
        searchable: bool,
        collapse_label: Optional[str],
    ) -> str:
        if label:
            print(f"{label}:", file=self.terminal)
        for index, item in enumerate(items, 1):
            description = f" — {item.description}" if item.description else ""
            print(f"  {index}. {item.label}{description}", file=self.terminal)
        accepts_package_name = searchable and label == "Module"
        prompt = (
            "Choose a number or package name: "
            if accepts_package_name
            else f"Choose [1-{len(items)}]: "
        )
        print('Type ":back" for the previous question or ":cancel" to exit.', file=self.terminal)
        while True:
            answer = self._line(prompt).strip()
            if answer == ":back":
                raise BackRequested
            if answer == ":cancel":
                raise CancelRequested
            selected: Optional[MenuItem] = None
            if not answer and default is not None:
                selected = next((item for item in items if item.value == default), None)
            elif answer.isdigit() and 1 <= int(answer) <= len(items):
                selected = items[int(answer) - 1]
            elif accepts_package_name:
                selected = next((item for item in items if item.value == answer), None)
            if selected is not None:
                if collapse_label or label:
                    self.answer(
                        collapse_label or label,
                        selected.completed_label or selected.label,
                    )
                return selected.value
            self.error(
                "Enter a listed number or exact package name."
                if accepts_package_name
                else "Enter a listed number."
            )

    def _cursor_menu(
        self,
        label: str,
        items: Sequence[MenuItem],
        default: Optional[str],
        searchable: bool,
        footer: str,
        collapse_label: Optional[str],
    ) -> str:
        selected = next((index for index, item in enumerate(items) if item.value == default), None)
        original = selected
        query = ""
        filtering = False
        drawn = 0
        notice: Optional[str] = None
        if label:
            print(f"{label}:", file=self.terminal)

        def matches() -> List[int]:
            if not query:
                return list(range(len(items)))
            lowered = query.casefold()
            return [
                index
                for index, item in enumerate(items)
                if any(
                    lowered in value.casefold()
                    for value in (item.label, item.description, *item.search_terms)
                )
            ]

        def redraw() -> None:
            nonlocal drawn, selected
            visible_indexes = matches()
            if selected not in visible_indexes:
                selected = visible_indexes[0] if visible_indexes and original is not None else None
            terminal_size = shutil.get_terminal_size((80, 24))
            viewport = max(3, terminal_size.lines - 9)
            start = 0
            if selected is not None and selected in visible_indexes:
                position = visible_indexes.index(selected)
                start = max(0, min(position - viewport // 2, len(visible_indexes) - viewport))
            page = visible_indexes[start : start + viewport]
            lines: List[str] = []
            if filtering:
                lines.append(f"Filter: {query}")
            if not page:
                lines.extend(["No matching modules.", "Esc clear filter"])
            if start > 0:
                lines.append("↑ more")
            for index in page:
                item = items[index]
                if item.separator_before:
                    lines.append("")
                marker = self.renderer.symbols["active"] if index == selected else " "
                if terminal_size.columns >= 72 and item.description:
                    row = f"{marker} {item.label:<20}{item.description}"
                else:
                    row = f"{marker} {item.label}"
                lines.append(self.renderer.style("active", row) if index == selected else row)
            if start + len(page) < len(visible_indexes):
                lines.append("↓ more")
            if len(visible_indexes) > viewport:
                lines.append(f"{start + 1}–{start + len(page)} of {len(visible_indexes)}")
            if notice:
                lines.append(self.renderer.style("warning", notice))
            hint = "↑/↓ move  Enter select"
            if searchable:
                hint += "  / filter"
            hint += f"  {footer}"
            lines.append(hint)
            if drawn:
                self.terminal.write(f"\033[{drawn}A")
            total = max(drawn, len(lines))
            for row in range(total):
                line = lines[row] if row < len(lines) else ""
                self.terminal.write("\r\033[2K" + line + "\n")
            self.terminal.flush()
            drawn = total

        with self.keys.raw():
            redraw()
            while True:
                event = self.keys.read()
                visible_indexes = matches()
                if event == "cancel":
                    raise InterruptRequested
                if event == "eof":
                    raise InputClosed
                if event == "enter":
                    if selected is None or selected not in visible_indexes:
                        if default is None:
                            notice = "! Select npm or Yarn."
                            redraw()
                        continue
                    notice = None
                    chosen = items[selected]
                    if drawn:
                        self.terminal.write(f"\033[{drawn}A")
                        for _ in range(drawn):
                            self.terminal.write("\r\033[2K\n")
                        self.terminal.write(f"\033[{drawn}A")
                    self.terminal.flush()
                    if collapse_label or label:
                        self.answer(
                            collapse_label or label,
                            chosen.completed_label or chosen.label,
                        )
                    return chosen.value
                if event in {"up", "down"} and visible_indexes:
                    notice = None
                    if selected not in visible_indexes:
                        selected = visible_indexes[-1] if event == "up" else visible_indexes[0]
                    else:
                        position = visible_indexes.index(selected)
                        position += -1 if event == "up" else 1
                        position = max(0, min(position, len(visible_indexes) - 1))
                        selected = visible_indexes[position]
                    redraw()
                    continue
                if event == "escape":
                    if filtering:
                        filtering = False
                        query = ""
                        selected = original
                        redraw()
                    else:
                        raise BackRequested
                    continue
                if searchable and event == "char:/" and not filtering:
                    filtering = True
                    query = ""
                    redraw()
                    continue
                if filtering and event == "backspace":
                    query = query[:-1]
                    redraw()
                    continue
                if filtering and event == "word_backspace":
                    query = query.rstrip()
                    query = query[: query.rfind(" ") + 1] if " " in query else ""
                    redraw()
                    continue
                if filtering and event.startswith("char:"):
                    query += event[5:]
                    redraw()

    def text(
        self,
        label: str,
        *,
        default: Optional[str] = None,
        guidance: Optional[str] = None,
        validate: Optional[Callable[[str], None]] = None,
        normalize: Optional[Callable[[str], str]] = None,
        optional: bool = False,
        bracket_default: bool = False,
    ) -> str:
        if self.plain:
            return self._plain_text(
                label,
                default,
                guidance,
                validate,
                normalize,
                optional,
                bracket_default,
            )
        return self._cursor_text(
            label,
            default,
            guidance,
            validate,
            normalize,
            optional,
            bracket_default,
        )

    def _plain_text(
        self,
        label: str,
        default: Optional[str],
        guidance: Optional[str],
        validate: Optional[Callable[[str], None]],
        normalize: Optional[Callable[[str], str]],
        optional: bool,
        bracket_default: bool,
    ) -> str:
        while True:
            suffix = f" [{default}]" if default is not None and bracket_default else ""
            print(f"{label}:{suffix}", file=self.terminal)
            if guidance:
                print(f"  {guidance}", file=self.terminal)
            prompt = (
                f"[{default}] "
                if default is not None and not bracket_default
                else ""
            )
            value = self._line(prompt)
            if value == ":back":
                raise BackRequested
            if value == ":cancel":
                raise CancelRequested
            if "\n" in value or "\r" in value:
                self.error("This field accepts one line. Paste a single value.")
                continue
            if not value and default is not None:
                value = default
            try:
                if normalize is not None:
                    value = normalize(value)
            except Exception as exc:
                self.error(str(exc))
                continue
            if not value and not optional:
                self.error("Enter a value.")
                continue
            try:
                if validate:
                    validate(value)
            except Exception as exc:
                self.error(str(exc))
                continue
            self.answer(label.replace(" (optional)", ""), value)
            return value

    def _cursor_text(
        self,
        label: str,
        default: Optional[str],
        guidance: Optional[str],
        validate: Optional[Callable[[str], None]],
        normalize: Optional[Callable[[str], str]],
        optional: bool,
        bracket_default: bool,
    ) -> str:
        while True:
            suffix = f" [{default}]" if default is not None and bracket_default else ""
            print(f"{label}:{suffix}", file=self.terminal)
            if guidance:
                print(f"  {guidance}", file=self.terminal)
            buffer = list(default or "") if not bracket_default else []
            cursor = len(buffer)
            self.terminal.write(self.renderer.symbols["active"] + " ")
            self.terminal.write("".join(buffer))
            self.terminal.flush()
            rejected_paste = False
            with self.keys.raw():
                while True:
                    event = self.keys.read()
                    if event == "cancel":
                        raise InterruptRequested
                    if event == "eof":
                        raise InputClosed
                    if event == "escape":
                        raise BackRequested
                    if event == "enter":
                        break
                    if event.startswith("paste:"):
                        pasted = event[6:]
                        if "\n" in pasted or "\r" in pasted:
                            rejected_paste = True
                            break
                        buffer[cursor:cursor] = list(pasted)
                        cursor += len(pasted)
                    elif event.startswith("char:"):
                        value = event[5:]
                        buffer[cursor:cursor] = [value]
                        cursor += 1
                    elif event == "left":
                        cursor = max(0, cursor - 1)
                    elif event == "right":
                        cursor = min(len(buffer), cursor + 1)
                    elif event == "home":
                        cursor = 0
                    elif event == "end":
                        cursor = len(buffer)
                    elif event == "backspace" and cursor:
                        del buffer[cursor - 1]
                        cursor -= 1
                    elif event == "delete" and cursor < len(buffer):
                        del buffer[cursor]
                    elif event == "clear":
                        buffer = []
                        cursor = 0
                    self.terminal.write("\r\033[2K" + self.renderer.symbols["active"] + " " + "".join(buffer))
                    if len(buffer) > cursor:
                        self.terminal.write(f"\033[{len(buffer) - cursor}D")
                    self.terminal.flush()
            self.terminal.write("\n")
            if rejected_paste:
                self.error("This field accepts one line. Paste a single value.")
                continue
            value = "".join(buffer)
            if not value and default is not None:
                value = default
            try:
                if normalize is not None:
                    value = normalize(value)
            except Exception as exc:
                self.error(str(exc))
                continue
            if not value and not optional:
                self.error("Enter a value.")
                continue
            try:
                if validate:
                    validate(value)
            except Exception as exc:
                self.error(str(exc))
                continue
            self.answer(label.replace(" (optional)", ""), value)
            return value

    def confirm(self, prompt: str, *, default: bool) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            raw_prompt = f"{prompt} [{suffix}]: "
            value = (
                self._line(raw_prompt)
                if self.plain
                else self._raw_line(raw_prompt)
            )
            if "\n" in value or "\r" in value:
                self.error("This field accepts one line. Paste a single value.")
                continue
            value = value.strip().lower()
            if value == ":back":
                raise BackRequested
            if value == ":cancel":
                raise CancelRequested
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            self.error("Enter yes or no.")

    def typed_confirmation(self, prompt: str, expected: str) -> Optional[bool]:
        while True:
            value = (
                self._line(prompt) if self.plain else self._raw_line(prompt)
            )
            if "\n" in value or "\r" in value:
                self.error("This field accepts one line. Paste a single value.")
                continue
            value = value.strip()
            if value == ":back":
                raise BackRequested
            if value == ":cancel":
                raise CancelRequested
            if not value:
                continue
            return value == expected

    def _line(self, prompt: str) -> str:
        self.terminal.write(prompt)
        self.terminal.flush()
        try:
            if self.line_source is not None:
                value = self.line_source()
            else:
                value = self.stdin.readline()
        except EOFError as exc:
            raise InputClosed from exc
        if value == "":
            raise InputClosed
        if value.startswith("\x1b[200~"):
            chunks = [value]
            while not any("\x1b[201~" in chunk for chunk in chunks):
                following = self.stdin.readline()
                if following == "":
                    break
                chunks.append(following)
            pasted = "".join(chunks)[len("\x1b[200~") :]
            ending = pasted.find("\x1b[201~")
            return pasted[:ending] if ending >= 0 else pasted
        result = value[:-1] if value.endswith("\n") else value
        if getattr(self.stdin, "isatty", lambda: False)() and hasattr(self.stdin, "fileno"):
            try:
                descriptor = self.stdin.fileno()
                extra: List[str] = []
                while select.select([descriptor], [], [], 0)[0]:
                    following = self.stdin.readline()
                    if not following:
                        break
                    extra.append(following)
                if extra:
                    result += "\n" + "".join(extra)
            except (OSError, ValueError):
                pass
        return result

    def wait_for_return(self) -> None:
        if self.plain:
            self._line("")
            return
        with self.keys.raw():
            while True:
                event = self.keys.read()
                if event in {"enter", "escape"}:
                    return
                if event == "cancel":
                    raise InterruptRequested
                if event == "eof":
                    raise InputClosed

    def _raw_line(self, prompt: str) -> str:
        buffer: List[str] = []
        self.terminal.write(prompt)
        self.terminal.flush()
        with self.keys.raw():
            while True:
                event = self.keys.read()
                if event == "cancel":
                    raise InterruptRequested
                if event == "eof":
                    raise InputClosed
                if event == "escape":
                    raise BackRequested
                if event == "enter":
                    self.terminal.write("\n")
                    self.terminal.flush()
                    return "".join(buffer)
                if event == "backspace" and buffer:
                    buffer.pop()
                elif event == "clear":
                    buffer.clear()
                elif event.startswith("paste:"):
                    pasted = event[6:]
                    if "\n" in pasted or "\r" in pasted:
                        self.terminal.write("\n")
                        self.error("This field accepts one line. Paste a single value.")
                        self.terminal.write(prompt)
                    else:
                        buffer.extend(pasted)
                elif event.startswith("char:"):
                    buffer.append(event[5:])
                self.terminal.write("\r\033[2K" + prompt + "".join(buffer))
                self.terminal.flush()
