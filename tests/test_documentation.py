from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from supernote_module_generator import __version__
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
from supernote_module_generator import generator as generator_module
from supernote_module_generator.generator import generate
from supernote_module_generator.helptext import COMMAND_HELP, ROOT_HELP
from supernote_module_generator.verification import TEMPLATE_TOKEN


ROOT = Path(__file__).resolve().parents[1]


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


def _documents() -> list[Path]:
    roots = [ROOT / "docs", ROOT / "maintainers", ROOT / "architecture"]
    return [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        *(path for base in roots for path in sorted(base.rglob("*.md"))),
    ]


def _heading_anchors(document: Path) -> set[str]:
    anchors: set[str] = set()
    for line in document.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        heading = re.sub(r"[`*_]", "", match.group(1)).lower()
        heading = re.sub(r"[^\w\s-]", "", heading)
        anchors.add(re.sub(r"\s+", "-", heading.strip()))
    return anchors


def test_parser_options_are_covered_by_runtime_help_and_cli_reference():
    reference = (ROOT / "docs/reference/cli.md").read_text(encoding="utf-8")
    for option in GLOBAL_BOOLEANS:
        assert option in ROOT_HELP
        assert option in reference

    for command, options in COMMAND_VALUE_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]
            assert option in reference

    for command, options in COMMAND_BOOLEAN_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]
            assert option in reference


def test_relative_markdown_links_resolve():
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for document in _documents():
        for raw_target in pattern.findall(document.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            assert resolved.exists(), f"{document}: broken link to {raw_target}"
            if "#" in raw_target:
                anchor = raw_target.split("#", 1)[1]
                assert anchor in _heading_anchors(resolved), (
                    f"{document}: broken heading link to {raw_target}"
                )


def test_absolute_repository_documentation_links_target_current_files():
    documents = [*_documents(), *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl"))]
    pattern = re.compile(
        r"https://github\.com/Ziv-Ink/supernote-module-generator/blob/"
        r"(?:main|v[^/]+|\$DOCUMENTATION_REF)/([^\s)#]+)(?:#([^\s)]+))?"
    )
    for document in documents:
        for path_text, anchor in pattern.findall(document.read_text(encoding="utf-8")):
            target = ROOT / path_text
            assert target.is_file(), f"{document}: broken repository link to {path_text}"
            if anchor:
                assert anchor in _heading_anchors(target), (
                    f"{document}: broken repository heading link to {path_text}#{anchor}"
                )


def test_root_readme_contains_the_complete_first_success_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "PluginConfig.json",
        "android/settings.gradle",
        "supernote-module",
        "local_modules/local-math/android/src/main/java/com/example/math/Example.kt",
        "fun add(left: Double, right: Double): Double",
        "import Math from 'local-math';",
        "await Math.add(20, 22)",
        "./buildPlugin.sh",
        ".\\buildPlugin.ps1",
        "build/outputs/plugin.snplg",
        "adb push",
        "Settings > Apps > Plugins > Add Plugin",
        "adb logcat",
    )
    for value in required:
        assert value in readme


@pytest.mark.parametrize(
    ("backend", "source", "call", "call_model"),
    (
        (
            "kotlin",
            "android/src/main/java/com/example/docs_kotlin/",
            "await DocsKotlin.add(20, 22)",
            "JavaScript Promise (`await`)",
        ),
        (
            "jni",
            "android/src/main/cpp/",
            "await DocsJni.add(20, 22)",
            "JavaScript Promise (`await`)",
        ),
        (
            "jsi",
            "android/src/main/cpp/",
            "const total = DocsJsi.add(20, 22)",
            "Synchronous; do not use `await`",
        ),
    ),
)
def test_generated_readmes_are_package_specific_supplements(
    tmp_path: Path,
    backend: str,
    source: str,
    call: str,
    call_model: str,
):
    module = generate(_module_config(tmp_path, backend))
    readme = (module / "README.md").read_text(encoding="utf-8")
    metadata = json.loads((module / ".supernote-module.json").read_text())

    assert f"Generator `{__version__}`" in readme
    assert source in readme
    assert call in readme
    assert call_model in readme
    assert "update` replaces this README" in readme
    assert "Remove deletes the entire module" in readme
    assert "docs/getting-started/first-module.md" in readme
    assert metadata["generator_version"] == __version__
    assert "/blob/main/docs/" in readme


def test_released_generated_readme_links_to_its_version_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(generator_module, "__version__", "1.1.0")
    module = generate(_module_config(tmp_path, "jni"))
    readme = (module / "README.md").read_text(encoding="utf-8")

    assert "Generator `1.1.0`" in readme
    assert "/blob/v1.1.0/docs/" in readme


@pytest.mark.parametrize("backend", ["kotlin", "jni", "jsi"])
def test_no_generated_text_file_contains_a_template_value(tmp_path: Path, backend: str):
    module = generate(_module_config(tmp_path, backend))
    for path in module.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        assert TEMPLATE_TOKEN.search(text) is None, path.relative_to(module)


def test_native_initial_declaration_uses_the_configured_interface_name(tmp_path: Path):
    module = generate(_module_config(tmp_path, "kotlin"))
    declarations = (module / "index.d.ts").read_text(encoding="utf-8")
    assert "export interface DocsKotlinModule" in declarations
    assert "declare const DocsKotlin: DocsKotlinModule" in declarations
    assert "$MODULE" not in declarations


def test_user_documentation_has_no_agent_spec_or_release_workflow_in_its_index():
    user_index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert not (ROOT / "UX_REDESIGN_SPECIFICATION.md").exists()
    assert not list((ROOT / "docs/history").glob("*.md"))
    assert "UX specification" not in user_index
    assert "Publishing to PyPI" not in user_index
    assert not (ROOT / "PYPI_README.md").exists()


def test_current_user_documentation_does_not_reference_private_deploy_script():
    user_documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for document in user_documents:
        content = document.read_text(encoding="utf-8")
        assert "deploy_plugin" not in content, document


def test_jsi_docs_separate_generation_compilation_and_runtime_support():
    compatibility = (ROOT / "docs/reference/compatibility.md").read_text(
        encoding="utf-8"
    )
    choosing = (ROOT / "docs/guides/choosing-a-module.md").read_text(
        encoding="utf-8"
    )
    for value in ("Generation", "Compilation", "Execution", "enforcing retail"):
        assert value in compatibility
    normalized = " ".join(choosing.split())
    assert "experimental" in normalized.lower()
    assert "direct C/C++ calls" in normalized
    assert "not supported" in normalized
