#!/usr/bin/env python3
"""Audit every public CLI example and execute the bounded Wiki scenario."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Iterable, Sequence

from supernote_module_generator.arguments import parse_arguments


START = "<!-- snv4-release-commands:start -->"
END = "<!-- snv4-release-commands:end -->"
SHELL_FENCE = re.compile(r"^```(?:bash|sh|shell|powershell|pwsh)?\s*$", re.I)
PLACEHOLDER = re.compile(r"(?:<[^>]+>|\[[^]]+\]|\{\{[^}]+\}\})")


@dataclass(frozen=True)
class DocumentedCommand:
    source: str
    line: int
    argv: tuple[str, ...]
    classification: str
    reason: str

    def manifest(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line": self.line,
            "argv": list(self.argv),
            "classification": self.classification,
            "reason": self.reason,
        }


def _logical_commands(lines: Sequence[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    commands: list[tuple[int, str]] = []
    pending = ""
    pending_line = 0
    for line_number, raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            pending_line = line_number
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\") or pending.endswith("`"):
            pending = pending[:-1].rstrip()
            continue
        commands.append((pending_line, pending))
        pending = ""
    if pending:
        commands.append((pending_line, pending))
    return tuple(commands)


def _fenced_shell_lines(page: Path) -> tuple[tuple[int, str], ...]:
    selected: list[tuple[int, str]] = []
    in_fence = False
    active = False
    for line_number, raw in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                active = False
            else:
                in_fence = True
                active = SHELL_FENCE.fullmatch(stripped) is not None
            continue
        if active:
            selected.append((line_number, raw))
    return tuple(selected)


def _classification(source: str, argv: tuple[str, ...]) -> tuple[str, str]:
    joined = " ".join(argv)
    if PLACEHOLDER.search(joined):
        return "placeholder", "contains a documented value placeholder"
    if len(argv) == 1:
        return "interactive", "opens the guided interface"
    if argv[1] in {"--version", "--help", "help"} or "--help" in argv[2:]:
        return "smoke", "safe exact command executed by the documentation gate"
    if "--build" in argv:
        return "android", "executed by the generated and real-project Android gates"
    if argv[1] == "doctor":
        return "environment", "host-dependent capability probe"
    if argv[1] in {"add", "update", "remove"} and not any(
        flag in argv for flag in ("--yes", "-y", "--dry-run")
    ):
        return "interactive", "requires an explicit decision in documentation use"
    if source.endswith("Getting-Started.md") and "wiki-feature" in argv:
        return "scenario", "executed in order by the bounded Wiki acceptance scenario"
    return "project", "grammar-checked here and exercised by disposable project gates"


def scan_documented_commands(paths: Iterable[Path]) -> tuple[DocumentedCommand, ...]:
    commands: list[DocumentedCommand] = []
    for page in sorted(paths):
        for line, source in _logical_commands(_fenced_shell_lines(page)):
            stripped = source.removeprefix("$ ").strip()
            if not stripped.startswith("supernote-module"):
                continue
            argv = tuple(shlex.split(stripped, posix=True))
            classification, reason = _classification(page.name, argv)
            commands.append(
                DocumentedCommand(page.name, line, argv, classification, reason)
            )
    return tuple(commands)


def _grammar_arguments(command: DocumentedCommand) -> list[str]:
    values = {
        "[PACKAGE]": "example",
        "[FEATURE]": "example",
        "[MODULE]": "example",
        "<module-name>": "example",
    }
    arguments = [values.get(value, value) for value in command.argv[1:]]
    return [value for value in arguments if value != "[options]"]


def audit_commands(
    wiki_root: Path,
    readme: Path,
    generator_command: str,
    output: Path,
) -> tuple[DocumentedCommand, ...]:
    commands = scan_documented_commands([readme, *wiki_root.glob("*.md")])
    if not commands:
        raise ValueError("No public supernote-module examples were found")
    for command in commands:
        parse_arguments(_grammar_arguments(command))
        if command.classification == "smoke":
            subprocess.run(
                (generator_command, *command.argv[1:]),
                check=True,
                stdout=subprocess.DEVNULL,
            )
    classifications: dict[str, int] = {}
    for command in commands:
        classifications[command.classification] = (
            classifications.get(command.classification, 0) + 1
        )
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "command_count": len(commands),
                "classifications": classifications,
                "commands": [command.manifest() for command in commands],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return commands


def read_commands(page: Path) -> tuple[tuple[str, ...], ...]:
    text = page.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("Wiki release-command markers must each occur exactly once")
    marked = text.split(START, 1)[1].split(END, 1)[0]
    fences = marked.split("```bash")
    if len(fences) != 2 or "```" not in fences[1]:
        raise ValueError("Wiki release commands must contain one bash fence")
    source = fences[1].split("```", 1)[0]
    commands = tuple(
        tuple(shlex.split(line))
        for _line, line in _logical_commands(
            tuple(enumerate(source.splitlines(), 1))
        )
    )
    if not commands:
        raise ValueError("Wiki release command block is empty")
    for command in commands:
        if not command or command[0] != "supernote-module":
            raise ValueError("Wiki release commands may invoke only supernote-module")
    return commands


def run_commands(page: Path, plugin_root: Path, generator_command: str) -> None:
    for command in read_commands(page):
        subprocess.run((generator_command, *command[1:]), check=True, cwd=plugin_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wiki_root", type=Path)
    parser.add_argument("readme", type=Path)
    parser.add_argument("plugin_root", type=Path)
    parser.add_argument("generator_command")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args(argv)
    audit_commands(
        arguments.wiki_root,
        arguments.readme,
        arguments.generator_command,
        arguments.output,
    )
    run_commands(
        arguments.wiki_root / "Getting-Started.md",
        arguments.plugin_root,
        arguments.generator_command,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
