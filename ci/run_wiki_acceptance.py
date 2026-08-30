#!/usr/bin/env python3
"""Audit every public command/output record and execute the bounded Wiki scenario."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Iterable, Optional, Sequence

from supernote_module_generator.arguments import parse_arguments


START = "<!-- sn-module-gen-release-commands:start -->"
END = "<!-- sn-module-gen-release-commands:end -->"
FENCE_START = re.compile(r"^```([a-z0-9_+-]*)\s*$", re.I)
# Source-language fences are code examples. Shell and text/console fences are
# the command/output records that this documentation gate inventories.
SHELL_FENCES = {"", "bash", "sh", "shell", "powershell", "pwsh"}
OUTPUT_FENCES = {"text", "console"}
PLACEHOLDER = re.compile(r"(?:<[^>]+>|\[[^]]+\]|\{\{[^}]+\}\})")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")

HOST_COMMAND_GATES = {
    "python": "manual-host-setup",
    "python3": "manual-host-setup",
    "py": "manual-host-setup",
    "pip": "manual-host-setup",
    "pip3": "manual-host-setup",
    "pwd": "manual-host-inspection",
    "ls": "manual-host-inspection",
    "get-location": "manual-host-inspection",
    "get-item": "manual-host-inspection",
    "git": "manual-source-inspection",
    "npm": "generated-and-real-project-dependency-gates",
    "yarn": "generated-and-real-project-dependency-gates",
    "npx": "generated-and-real-project-dependency-gates",
    "node": "generated-and-real-project-javascript-gates",
    "java": "manual-host-toolchain-diagnostics",
    "echo": "manual-host-toolchain-diagnostics",
    "export": "manual-host-toolchain-configuration",
    "find": "manual-generated-source-inspection",
}
ANDROID_DEVICE_COMMANDS = {
    "adb",
    "./android/gradlew",
    ".androidgradlew.bat",
    "bash",
    "powershell",
}


@dataclass(frozen=True)
class DocumentedCommand:
    source: str
    line: int
    fence: str
    text: str
    argv: tuple[str, ...]
    classification: str
    execution_gate: Optional[str]
    reason: str

    def manifest(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line": self.line,
            "fence": self.fence,
            "text": self.text,
            "argv": list(self.argv),
            "classification": self.classification,
            "execution_gate": self.execution_gate,
            "reason": self.reason,
        }


def _logical_commands(lines: Sequence[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    commands: list[tuple[int, str]] = []
    pending = ""
    pending_line = 0
    for line_number, raw in lines:
        line = raw.strip()
        if not line:
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


def _audited_fences(
    page: Path,
) -> tuple[tuple[str, tuple[tuple[int, str], ...]], ...]:
    selected: list[tuple[str, tuple[tuple[int, str], ...]]] = []
    in_fence = False
    active_language = ""
    audited = False
    active_lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if in_fence:
            if stripped == "```":
                if audited:
                    selected.append((active_language, tuple(active_lines)))
                in_fence = False
                active_language = ""
                audited = False
                active_lines = []
            elif audited:
                active_lines.append((line_number, raw))
            continue
        match = FENCE_START.fullmatch(stripped)
        if match:
            in_fence = True
            active_language = match.group(1).lower()
            audited = active_language in SHELL_FENCES | OUTPUT_FENCES
    if in_fence:
        raise ValueError(f"{page.name}: unclosed {active_language or 'shell'} fence")
    return tuple(selected)


def _generator_gate(source: str, argv: tuple[str, ...]) -> str:
    if len(argv) > 1 and (
        argv[1] in {"--version", "--help", "help"} or "--help" in argv[2:]
    ):
        return "documentation-smoke"
    if "--build" in argv:
        return "generated-and-real-project-android-gates"
    if len(argv) > 1 and argv[1] == "doctor":
        return "host-environment-qualification"
    if source.endswith("Getting-Started.md") and "wiki-feature" in argv:
        return "bounded-wiki-scenario"
    if len(argv) == 1 or (
        len(argv) > 1
        and argv[1] in {"add", "update", "remove"}
        and not any(flag in argv for flag in ("--yes", "-y", "--dry-run"))
    ):
        return "manual-interactive-workflow"
    return "disposable-project-gates"


def _documented_record(
    page: Path,
    fence: str,
    line: int,
    source: str,
    argv: tuple[str, ...],
    classification: str,
    execution_gate: Optional[str],
    reason: str,
) -> DocumentedCommand:
    return DocumentedCommand(
        page.name,
        line,
        fence or "shell",
        source,
        argv,
        classification,
        execution_gate,
        reason,
    )


def _generator_record(
    page: Path,
    fence: str,
    line: int,
    source: str,
    argv: tuple[str, ...],
) -> DocumentedCommand:
    if PLACEHOLDER.search(source):
        return _documented_record(
            page,
            fence,
            line,
            source,
            argv,
            "placeholder",
            None,
            "generator example contains a documented value placeholder",
        )
    gate = _generator_gate(page.name, argv)
    classification = "android_device" if "--build" in argv else "executable"
    return _documented_record(
        page,
        fence,
        line,
        source,
        argv,
        classification,
        gate,
        "generator command is grammar-checked and assigned to its qualification gate",
    )


def _adb_record(
    page: Path,
    fence: str,
    line: int,
    source: str,
    argv: tuple[str, ...],
) -> DocumentedCommand:
    reason = "device diagnostic is manual and requires an authorized connected device"
    if PLACEHOLDER.search(source):
        reason += "; the documented command also contains a value placeholder"
    return _documented_record(
        page,
        fence,
        line,
        source,
        argv,
        "android_device",
        "authorized-device-diagnostic",
        reason,
    )


def _host_record(
    page: Path,
    fence: str,
    line: int,
    source: str,
    argv: tuple[str, ...],
) -> DocumentedCommand:
    normalized_head = argv[0].lower()
    gate = (
        "manual-host-toolchain-diagnostics"
        if normalized_head.startswith("$env:")
        else HOST_COMMAND_GATES.get(normalized_head)
    )
    if gate is None:
        raise ValueError(
            f"{page.name}:{line}: unclassified {fence or 'shell'} fenced command: "
            f"{argv[0]}"
        )
    if PLACEHOLDER.search(source):
        return _documented_record(
            page,
            fence,
            line,
            source,
            argv,
            "placeholder",
            None,
            "host command contains a documented value placeholder",
        )
    return _documented_record(
        page,
        fence,
        line,
        source,
        argv,
        "executable",
        gate,
        "allowlisted host command is assigned to an explicit execution gate",
    )


def _shell_record(
    page: Path,
    fence: str,
    line: int,
    source: str,
) -> DocumentedCommand:
    stripped = source.removeprefix("$ ").strip()
    if stripped.startswith("#"):
        return _documented_record(
            page,
            fence,
            line,
            stripped,
            (),
            "explanatory_output",
            None,
            "shell-fence comment is explanatory and not runnable",
        )
    try:
        argv = tuple(shlex.split(stripped, posix=True))
    except ValueError as error:
        raise ValueError(f"{page.name}:{line}: cannot parse fenced record: {error}") from error
    if not argv:
        raise ValueError(f"{page.name}:{line}: empty fenced record is not classified")
    head = argv[0]
    normalized_head = head.lower()
    if head == "supernote-module":
        raise ValueError(f"{page.name}:{line}: pre-public CLI command is not allowed")
    if head == "sn-module-gen":
        return _generator_record(page, fence, line, stripped, argv)
    if normalized_head == "adb":
        return _adb_record(page, fence, line, stripped, argv)
    if normalized_head in ANDROID_DEVICE_COMMANDS:
        return _documented_record(
            page,
            fence,
            line,
            stripped,
            argv,
            "android_device",
            "generated-and-real-project-android-gates",
            "Android build or packaging command is covered by exact-SHA project gates",
        )
    return _host_record(page, fence, line, stripped, argv)


def scan_documented_commands(paths: Iterable[Path]) -> tuple[DocumentedCommand, ...]:
    commands: list[DocumentedCommand] = []
    for page in sorted(paths):
        for fence, lines in _audited_fences(page):
            records = (
                tuple((line, source.strip()) for line, source in lines if source.strip())
                if fence in OUTPUT_FENCES
                else _logical_commands(lines)
            )
            for line, source in records:
                if fence in OUTPUT_FENCES:
                    commands.append(
                        DocumentedCommand(
                            page.name,
                            line,
                            fence,
                            source.strip(),
                            (),
                            "explanatory_output",
                            None,
                            "text fence documents output or structure and is not runnable",
                        )
                    )
                else:
                    commands.append(_shell_record(page, fence, line, source))
    return tuple(commands)


def _heading_anchors(page: Path) -> set[str]:
    anchors: set[str] = set()
    duplicates: dict[str, int] = {}
    for raw in page.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", raw)
        if not match:
            continue
        heading = re.sub(r"[`*_]", "", match.group(1)).lower()
        heading = re.sub(r"[^\w\s-]", "", heading)
        base = re.sub(r"\s+", "-", heading.strip())
        count = duplicates.get(base, 0)
        duplicates[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def validate_wiki_links(wiki_root: Path) -> None:
    pages = {page.stem: page for page in wiki_root.glob("*.md")}
    for source in pages.values():
        for raw_target in MARKDOWN_LINK.findall(source.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            page_name, separator, anchor = target.partition("#")
            target_page = source if not page_name else pages.get(
                page_name.removesuffix(".md")
            )
            if target_page is None:
                raise ValueError(f"{source.name}: broken Wiki link to {raw_target}")
            if separator and anchor not in _heading_anchors(target_page):
                raise ValueError(
                    f"{source.name}: broken Wiki heading link to {raw_target}"
                )


def _grammar_arguments(command: DocumentedCommand) -> list[str]:
    values = {
        "[PACKAGE]": "example",
        "[FEATURE]": "example",
        "[MODULE]": "example",
        "<module-name>": "example",
    }
    arguments: list[str] = []
    for value in command.argv[1:]:
        if value == "[options]":
            continue
        if PLACEHOLDER.search(value):
            replacement = values.get(value)
            if replacement is None:
                raise ValueError(
                    f"{command.source}:{command.line}: unsupported documented "
                    f"placeholder: {value}"
                )
            arguments.append(replacement)
        else:
            arguments.append(value)
    return arguments


def audit_commands(
    wiki_root: Path,
    readme: Path,
    generator_command: str,
    output: Path,
) -> tuple[DocumentedCommand, ...]:
    validate_wiki_links(wiki_root)
    commands = scan_documented_commands([readme, *wiki_root.glob("*.md")])
    if not commands:
        raise ValueError("No public fenced command/output records were found")
    for command in commands:
        if not command.reason or (
            command.classification in {"executable", "android_device"}
            and not command.execution_gate
        ):
            raise ValueError(
                f"{command.source}:{command.line}: classified record lacks gate or reason"
            )
        if not command.argv or command.argv[0] != "sn-module-gen":
            continue
        parse_arguments(_grammar_arguments(command))
        if command.classification == "placeholder":
            continue
        if command.execution_gate == "documentation-smoke":
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
                "schema_version": "1.1",
                "record_count": len(commands),
                "classifications": classifications,
                "records": [command.manifest() for command in commands],
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
        if not command or command[0] != "sn-module-gen":
            raise ValueError("Wiki release commands may invoke only a documented generator CLI")
    return commands


def run_checkpoint_scenario(plugin_root: Path, generator_command: str) -> None:
    commands = (
        (
            "add", "wiki-feature", "--starter", "cpp", "--starter", "kotlin",
            "--javascript-name", "WikiFeature", "--android-namespace",
            "com.example.wiki_feature", "--package-manager", "npm", "--yes",
        ),
        ("update", "wiki-feature", "--dry-run"),
        ("update", "wiki-feature", "--yes"),
        ("check",),
        ("repair", "--dry-run"),
    )
    for command in commands:
        subprocess.run((generator_command, *command), check=True, cwd=plugin_root)


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
    read_commands(arguments.wiki_root / "Getting-Started.md")
    run_checkpoint_scenario(arguments.plugin_root, arguments.generator_command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
