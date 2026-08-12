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
    "Add-a-Module",
    "CLI-and-Automation",
    "Choosing-a-Module",
    "Compatibility",
    "Export-Functions",
    "Generated-Files-and-Integration",
    "Home",
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
    roots = [ROOT / "maintainers", ROOT / "architecture", ROOT / "docs"]
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


def test_root_readme_explains_the_v2_public_model():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    opening = "\n".join(readme.splitlines()[:12])
    opening_words = " ".join(opening.split())
    assert "typed C/C++ and Kotlin/Java capabilities" in opening
    assert "existing Supernote plugin" in opening_words
    assert "one user-facing feature" in opening
    assert "## Install" in readme
    assert "pip install supernote-module-generator" in readme
    assert "--starter cpp --starter kotlin" in readme
    assert "--type native" not in readme
    assert "SupernoteInternal" in readme
    assert "SupernoteAsync" in readme
    assert "C23" in readme and "C++23" in readme
    assert "--delete-build-files" in readme
    assert "managed non-JS context" in " ".join(readme.split())
    assert "https://docs.supernote.com/" in readme
    assert len(readme.splitlines()) < 240


@pytest.mark.parametrize(
    ("backend", "source", "call", "call_model", "backend_page", "help_page"),
    (
        (
            "kotlin",
            "android/src/main/java/com/example/docs_kotlin/",
            "await DocsKotlin.add(20, 22)",
            "Promises (`await`)",
            "Kotlin-and-Java-Modules",
            "Managing-Modules",
        ),
        (
            "jni",
            "android/src/main/cpp/",
            "await DocsJni.add(20, 22)",
            "Promises (`await`)",
            "JNI-Modules",
            "Managing-Modules",
        ),
        (
            "jsi",
            "android/src/main/cpp/",
            "const total = DocsJsi.add(20, 22)",
            "synchronous; do not use `await`",
            "JSI-Modules",
            "Troubleshooting",
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
    help_page: str,
):
    module = generate(_module_config(tmp_path, backend))
    readme = (module / "README.md").read_text(encoding="utf-8")
    metadata = json.loads((module / ".supernote-module.json").read_text())

    assert f"Generator `{__version__}`" in readme
    assert source in readme
    assert call in readme
    assert call_model in readme
    assert "Update replaces this README" in readme
    assert "Remove deletes the complete module" in readme
    assert f"{WIKI_ROOT}/{backend_page}" in readme
    assert f"{WIKI_ROOT}/{help_page}" in readme
    assert readme.count(f"{WIKI_ROOT}/") == 2
    assert "Canonical guidance" not in readme
    assert "--build --verbose" not in readme
    maximum_lines = 80 if backend == "jsi" else 45
    assert len(readme.splitlines()) <= maximum_lines
    assert "/blob/" not in readme
    assert "/docs/" not in readme
    assert metadata["generator_version"] == __version__


def test_generated_jsi_readme_documents_native_objects(tmp_path: Path):
    module = generate(_module_config(tmp_path, "jsi"))
    readme = (module / "README.md").read_text(encoding="utf-8")

    assert "## Persistent C++ objects" in readme
    assert readme.count("// @SupernoteExport") >= 3
    assert "SupernoteExportObject" not in readme
    assert "android/src/main/cpp/" in readme
    assert "class Counter" in readme
    assert "DocsJsi.Counter.create(10)" in readme
    assert "persistent native C++ instance" in readme
    assert "Only explicitly marked methods" in readme
    assert "unmarked public methods" in readme
    assert "JSI-only" in readme
    assert "remain synchronous" in readme
    assert f"{WIKI_ROOT}/JSI-Modules" in readme


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


def test_repository_docs_contain_architectural_history_not_migration_tooling():
    history = (ROOT / "docs/V1-TO-V2-ARCHITECTURE.md").read_text()
    assert "architectural history" in history
    assert "not a converter guide" in history
    assert "automatic converter" in history
    assert not (ROOT / "docs/Add-a-Feature.md").exists()
    assert not (ROOT / "UX_REDESIGN_SPECIFICATION.md").exists()
    assert not (ROOT / "PYPI_README.md").exists()
    assert "recursive-include docs *.md" in (ROOT / "MANIFEST.in").read_text(
        encoding="utf-8"
    )


def test_release_guide_uses_the_v2_feature_model():
    guide = (ROOT / "maintainers/releasing.md").read_text(encoding="utf-8")
    assert "C/C++ starter" in guide
    assert "Kotlin/Java starter" in guide
    assert "one plugin runtime component" in guide
    assert "all three module types" not in guide
    assert "Add a Module" not in guide


def test_public_material_does_not_reference_private_deploy_script():
    public_documents = [
        ROOT / "README.md",
        *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl")),
    ]
    for document in public_documents:
        assert "deploy_plugin" not in document.read_text(encoding="utf-8"), document


def test_public_repository_material_does_not_teach_the_plugin_lifecycle():
    public_documents = [
        ROOT / "README.md",
        *sorted((ROOT / "src/supernote_module_generator/templates").glob("*.md.tmpl")),
    ]
    forbidden = (
        "buildPlugin.sh",
        "buildPlugin.ps1",
        "plugin.snplg",
        "adb push",
        "Settings > Apps > Plugins",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in public_documents)
    for phrase in forbidden:
        assert phrase not in combined


def test_cli_identifies_an_existing_plugin_as_the_product_boundary():
    assert "existing Supernote plugin" in ROOT_HELP


def test_jsi_is_supported_without_overstating_runtime_availability():
    current_material = [
        ROOT / "README.md",
        ROOT / "src/supernote_module_generator/cli.py",
        ROOT / "src/supernote_module_generator/helptext.py",
        ROOT / "src/supernote_module_generator/workflows.py",
        ROOT / "src/supernote_module_generator/templates/jsi.README.md.tmpl",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in current_material)
    lower = combined.lower()
    assert "experimental" not in lower
    assert "requires target PluginHost support" in combined
    for runtime_constraint in ("compile", "pluginhost", "selinux"):
        assert runtime_constraint in lower
