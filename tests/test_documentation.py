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
from supernote_module_generator.generator import generate
from supernote_module_generator.helptext import COMMAND_HELP, ROOT_HELP
from supernote_module_generator.verification import TEMPLATE_TOKEN


ROOT = Path(__file__).resolve().parents[1]
WIKI_ROOT = "https://github.com/Ziv-Ink/supernote-module-generator/wiki"
WIKI_PAGES = {
    "CLI-and-Automation",
    "Choosing-a-Module",
    "Compatibility",
    "Exports-and-JavaScript-API",
    "Getting-Started",
    "Home",
    "How-Modules-Work",
    "JNI-Modules",
    "JSI-Modules",
    "Kotlin-and-Java-Modules",
    "Managing-Modules",
    "Troubleshooting",
}


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


def _repository_documents() -> list[Path]:
    roots = [ROOT / "maintainers", ROOT / "architecture"]
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


def test_parser_options_are_covered_by_installed_help():
    for option in GLOBAL_BOOLEANS:
        assert option in ROOT_HELP

    for command, options in COMMAND_VALUE_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]

    for command, options in COMMAND_BOOLEAN_OPTIONS.items():
        for option in options:
            assert option in COMMAND_HELP[command]


def test_relative_repository_markdown_links_resolve():
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for document in _repository_documents():
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


def test_absolute_repository_links_target_current_files():
    documents = [
        *_repository_documents(),
        *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl")),
    ]
    pattern = re.compile(
        r"https://github\.com/Ziv-Ink/supernote-module-generator/blob/main/"
        r"([^\s)#]+)(?:#([^\s)]+))?"
    )
    for document in documents:
        for path_text, anchor in pattern.findall(
            document.read_text(encoding="utf-8")
        ):
            target = ROOT / path_text
            assert target.is_file(), f"{document}: broken repository link to {path_text}"
            if anchor:
                assert anchor in _heading_anchors(target), (
                    f"{document}: broken repository heading link to {path_text}#{anchor}"
                )


def test_wiki_links_use_known_task_pages():
    documents = [
        *_repository_documents(),
        *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl")),
    ]
    pattern = re.compile(
        re.escape(WIKI_ROOT) + r"(?:/([^\s)#]+))?(?:#[^\s)]+)?"
    )
    links = []
    for document in documents:
        links.extend(pattern.findall(document.read_text(encoding="utf-8")))

    assert links
    for page in links:
        if page:
            assert page in WIKI_PAGES


def test_root_readme_is_a_short_product_entry_point():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "## Why use it",
        "## Install",
        "pip install supernote-module-generator",
        "## Quick example",
        "fun add(left: Double, right: Double): Double",
        "await Math.add(20, 22)",
        "## Module types",
        "## Compatibility summary",
        "## Documentation",
        WIKI_ROOT,
        "## Contributing",
        "## License",
    )
    for value in required:
        assert value in readme
    assert len(readme.splitlines()) < 150


@pytest.mark.parametrize(
    ("backend", "source", "call", "call_model", "backend_page"),
    (
        (
            "kotlin",
            "android/src/main/java/com/example/docs_kotlin/",
            "await DocsKotlin.add(20, 22)",
            "JavaScript Promise (`await`)",
            "Kotlin-and-Java-Modules",
        ),
        (
            "jni",
            "android/src/main/cpp/",
            "await DocsJni.add(20, 22)",
            "JavaScript Promise (`await`)",
            "JNI-Modules",
        ),
        (
            "jsi",
            "android/src/main/cpp/",
            "const total = DocsJsi.add(20, 22)",
            "Synchronous; do not use `await`",
            "JSI-Modules",
        ),
    ),
)
def test_generated_readmes_are_package_specific_wiki_supplements(
    tmp_path: Path,
    backend: str,
    source: str,
    call: str,
    call_model: str,
    backend_page: str,
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
    assert f"{WIKI_ROOT}/{backend_page}" in readme
    assert f"{WIKI_ROOT}/Getting-Started" in readme
    assert "/blob/" not in readme
    assert "/docs/" not in readme
    assert metadata["generator_version"] == __version__


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


def test_repository_does_not_duplicate_wiki_user_guides():
    assert not (ROOT / "docs").exists()
    assert not (ROOT / "UX_REDESIGN_SPECIFICATION.md").exists()
    assert not (ROOT / "PYPI_README.md").exists()
    assert "recursive-include docs" not in (ROOT / "MANIFEST.in").read_text(
        encoding="utf-8"
    )


def test_public_material_does_not_reference_private_deploy_script():
    public_documents = [
        ROOT / "README.md",
        *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl")),
    ]
    for document in public_documents:
        assert "deploy_plugin" not in document.read_text(encoding="utf-8"), document


def test_jsi_is_supported_without_overstating_runtime_availability():
    current_material = [
        ROOT / "README.md",
        ROOT / "src/supernote_module_generator/cli.py",
        ROOT / "src/supernote_module_generator/helptext.py",
        ROOT / "src/supernote_module_generator/workflows.py",
        ROOT / "src/supernote_module_generator/templates/jsi.README.md.tmpl",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in current_material)
    assert "experimental" not in combined.lower()
    assert "requires target PluginHost support" in combined
    assert "generation and compilation do not" in combined.lower()
