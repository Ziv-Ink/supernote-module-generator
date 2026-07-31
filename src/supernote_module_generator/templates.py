from __future__ import annotations

from pathlib import Path
import re

from .errors import TemplateError


def render(name: str, values: dict[str, str]) -> str:
    try:
        source = (
            Path(__file__).resolve().parent / "templates" / name
        ).read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"Unable to read template {name}") from exc
    try:
        rendered = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)", lambda match: values.get(match.group(1) or match.group(2), match.group(0)), source)
    except (KeyError, ValueError) as exc:
        raise TemplateError(f"Unable to render template {name}: {exc}") from exc
    return rendered.replace("\r\n", "\n").rstrip() + "\n"
