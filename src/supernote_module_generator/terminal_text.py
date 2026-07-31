"""Terminal-cell measurement and ASCII-safe terminal presentation."""
from __future__ import annotations

import unicodedata
from typing import IO, Any


def ascii_presentation(text: str) -> str:
    """Keep plain and Unicode-unsafe output ASCII without silently dropping text."""
    text = text.translate(
        str.maketrans(
            {
                "\u2013": "-",
                "\u2014": "-",
                "\u2191": "^",
                "\u2193": "v",
            }
        )
    )
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


class AsciiTextStream:
    """A transparent text-stream adapter that escapes non-ASCII output."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        self._stream.write(ascii_presentation(text))
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _is_variation_selector(character: str) -> bool:
    codepoint = ord(character)
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_emoji_modifier(character: str) -> bool:
    return 0x1F3FB <= ord(character) <= 0x1F3FF


def _is_zero_width(character: str) -> bool:
    return (
        character == "\u200d"
        or _is_variation_selector(character)
        or _is_emoji_modifier(character)
        or bool(unicodedata.combining(character))
        or unicodedata.category(character) in {"Cc", "Cf", "Mn", "Me"}
    )


def _clusters(text: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    for character in text:
        if not current:
            current = character
            continue
        regional_pair = (
            len(current) == 1
            and _is_regional_indicator(current)
            and _is_regional_indicator(character)
        )
        if (
            current.endswith("\u200d")
            or character == "\u200d"
            or _is_zero_width(character)
            or regional_pair
        ):
            current += character
            continue
        clusters.append(current)
        current = character
    if current:
        clusters.append(current)
    return clusters


def _codepoint_width(character: str) -> int:
    if _is_zero_width(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _cluster_width(cluster: str) -> int:
    characters = list(cluster)
    if len(characters) == 2 and all(_is_regional_indicator(char) for char in characters):
        return 2
    widths = [_codepoint_width(character) for character in characters]
    if "\u200d" in cluster:
        return max(widths, default=0)
    if "\u20e3" in cluster:
        return 2
    return sum(widths)


def terminal_width(text: str) -> int:
    """Return the number of terminal cells used by visible Unicode text."""
    return sum(_cluster_width(cluster) for cluster in _clusters(text))
