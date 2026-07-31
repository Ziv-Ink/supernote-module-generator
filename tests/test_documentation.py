from __future__ import annotations

import re
from pathlib import Path

import pytest

from supernote_module_generator.arguments import (
    COMMAND_BOOLEAN_OPTIONS,
    COMMAND_VALUE_OPTIONS,
    GLOBAL_BOOLEANS,
)
from supernote_module_generator.config import (
    ProjectConfig,
    jsi_global_name,
    native_library_name,
)
from supernote_module_generator.generator import generate
from supernote_module_generator.helptext import COMMAND_HELP, ROOT_HELP


ROOT = Path(__file__).resolve().parents[1]
APPROVED_DESCRIPTIONS = (
    "For coding in Kotlin/Java and/or using Android APIs.",
    "For combining Android APIs with existing or performance-intensive C/C++ code.",
    "For low-latency synchronous calls from JavaScript.",
)


def _specification_help_screens() -> dict[str, str]:
    specification = (ROOT / "UX_REDESIGN_SPECIFICATION.md").read_text(
        encoding="utf-8"
    )
    section = specification.split("## 21. Complete help screens\n", 1)[1]
    section = section.split("\n## 22. ", 1)[0]
    matches = re.findall(
        r"^### 21\.\d+ ([^\n]+)\n\n```text\n(.*?)^```$",
        section,
        flags=re.MULTILINE | re.DOTALL,
    )
    return {title: screen for title, screen in matches}


def _module_config(tmp_path: Path, backend: str) -> ProjectConfig:
    package = f"local-docs-{backend}"
    return ProjectConfig(
        output=tmp_path / package,
        npm_name=package,
        package_version="0.1.0",
        android_namespace=f"com.example.docs_{backend}",
        module_name=f"Docs{backend.title()}",
        description="Documentation fixture",
        backend=backend,
        native_library_name=(
            native_library_name(package) if backend in {"jni", "jsi"} else None
        ),
        jsi_global_name=jsi_global_name(package) if backend == "jsi" else None,
    )


def test_specification_help_screens_match_runtime_help_exactly():
    screens = _specification_help_screens()
    expected = {
        "Root": ROOT_HELP,
        "Add": COMMAND_HELP["add"],
        "Update": COMMAND_HELP["update"],
        "Validate": COMMAND_HELP["validate"],
        "Remove": COMMAND_HELP["remove"],
        "Doctor": COMMAND_HELP["doctor"],
        "Help command": COMMAND_HELP["help"],
    }
    assert screens == expected


def test_parser_options_are_covered_by_help_and_automation_guide():
    automation = (ROOT / "docs/automation.md").read_text(encoding="utf-8")
    for option in GLOBAL_BOOLEANS:
        assert option in ROOT_HELP
        assert option in automation

    for command, options in COMMAND_VALUE_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]
            assert option in automation

    for command, options in COMMAND_BOOLEAN_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]
            assert option in automation


def test_relative_markdown_links_resolve():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for document in documents:
        for raw_target in pattern.findall(document.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            assert resolved.exists(), f"{document}: broken link to {raw_target}"


def test_approved_module_descriptions_are_consistent():
    documents = (
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (ROOT / "docs/choosing-a-module.md").read_text(encoding="utf-8"),
        (ROOT / "UX_REDESIGN_SPECIFICATION.md").read_text(encoding="utf-8"),
        ROOT_HELP,
    )
    for document in documents:
        normalized = " ".join(document.split())
        for description in APPROVED_DESCRIPTIONS:
            assert " ".join(description.split()) in normalized


@pytest.mark.parametrize(
    ("backend", "description", "source", "call"),
    (
        (
            "kotlin",
            APPROVED_DESCRIPTIONS[0],
            "android/src/main/java/",
            "await DocsKotlin.add(20, 22)",
        ),
        (
            "jni",
            APPROVED_DESCRIPTIONS[1],
            "android/src/main/cpp",
            "await DocsJni.add(20, 22)",
        ),
        (
            "jsi",
            APPROVED_DESCRIPTIONS[2],
            "android/src/main/cpp",
            "const total = DocsJsi.add(20, 22)",
        ),
    ),
)
def test_generated_module_readmes_are_complete(
    tmp_path: Path, backend: str, description: str, source: str, call: str
):
    module = generate(_module_config(tmp_path, backend))
    readme = (module / "README.md").read_text(encoding="utf-8")

    assert description in readme
    assert source in readme
    assert call in readme
    assert "## Names" in readme
    assert "## Ownership" in readme
    assert "## Build and troubleshoot" in readme
    assert "bash deploy_plugin.sh" in readme
    assert not re.search(r"\$[A-Z][A-Z0-9_]*", readme)


def test_historical_audit_is_archived_and_marked_superseded():
    assert not (ROOT / "UX_AUDIT_2026-07-31.md").exists()
    audit = ROOT / "docs/history/UX_AUDIT_2026-07-31.md"
    assert audit.is_file()
    assert "Historical document — superseded" in audit.read_text(encoding="utf-8")
