from __future__ import annotations

import re
from pathlib import Path


from supernote_module_generator.arguments import (
    COMMAND_BOOLEAN_OPTIONS,
    COMMAND_VALUE_OPTIONS,
    GLOBAL_BOOLEANS,
)
from supernote_module_generator.helptext import COMMAND_HELP, ROOT_HELP


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


def test_root_readme_explains_the_v4_public_model():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    opening = "\n".join(readme.splitlines()[:12])
    opening_words = " ".join(opening.split())
    assert "typed C/C++ and Kotlin/Java capabilities" in opening
    assert "existing Supernote plugin" in opening_words
    assert (
        "A feature can use C++, C helper files, Kotlin, and Java together"
        in opening
    )
    assert "## Install" in readme
    assert "pip install supernote-module-generator" in readme
    assert "--starter cpp --starter kotlin" in readme
    assert "--type native" not in readme
    assert "SupernotePluginInternal" in readme
    assert "SupernotePluginAsync" in readme
    assert "SupernotePluginObject" in readme
    assert "SupernotePluginValue" in readme
    assert "returned-only objects" in readme
    assert "homogeneous array" in readme
    assert "nullable `T`" in readme
    assert "Cross-language native-object proxies are not generated yet" in readme
    assert "no V2 users or migration requirements" not in readme
    assert "development line for first-class native objects" not in readme
    assert "C23" in readme and "C++23" in readme
    assert "--delete-build-files" in readme
    assert "plugin root's optional `devconfig.json`" in readme
    assert "preserves the corresponding launch-environment value" in normalized_readme
    assert "do not change the parent shell" in normalized_readme
    assert "do not change the parent shell or `android/local.properties` on disk" in normalized_readme
    assert (
        "destroys C++ receivers and resources away from the JavaScript thread"
        in " ".join(readme.split())
    )
    assert "https://docs.supernote.com/" in readme
    assert len(readme.splitlines()) < 320


def test_initial_v4_feature_readme_is_package_specific_and_generation_owned(
    tmp_path: Path,
):
    from supernote_module_generator.feature_generator import (
        FeatureConfig,
        stage_feature,
    )
    from supernote_module_generator.feature_model import StarterFamily

    feature = stage_feature(
        FeatureConfig(
            output=tmp_path / "typed-feature",
            npm_name="typed-feature",
            package_version="4.0.0-dev.0",
            public_name="TypedFeature",
            android_namespace="com.example.typed_feature",
            starters=(StarterFamily.NATIVE, StarterFamily.JVM),
        )
    )
    readme = (feature / "README.md").read_text(encoding="utf-8")

    assert "import TypedFeature from 'typed-feature';" in readme
    assert "C/C++: `android/src/main/cpp/`" in readme
    assert "Kotlin/Java: `android/src/main/java/`" in readme
    assert "No JavaScript-public declarations are currently generated" in readme
    assert "supernote-module update typed-feature" in readme
    assert "replace this README and `index.d.ts`" in readme
    assert "Cross-family native-object proxies" not in readme


def test_repository_docs_define_v4_architecture_without_migration_tooling():
    architecture = (ROOT / "docs/V4-ARCHITECTURE.md").read_text()
    assert "V4 architecture" in architecture
    assert "V1, V2, and V3" in architecture
    assert "provides no converter, migrator, compatibility mode, or downgrade" in architecture
    assert "Cross-family object proxies" in architecture
    assert not (ROOT / "docs/V1-TO-V2-ARCHITECTURE.md").exists()
    assert not (ROOT / "docs/Add-a-Feature.md").exists()
    assert not (ROOT / "UX_REDESIGN_SPECIFICATION.md").exists()
    assert not (ROOT / "PYPI_README.md").exists()
    assert "recursive-include docs *.md" in (ROOT / "MANIFEST.in").read_text(
        encoding="utf-8"
    )


def test_accepted_v4_policies_forbid_positive_legacy_migration_claims():
    policies = (
        ROOT / "architecture/decisions/0003-v4-owner-confirmed-policies.md"
    ).read_text(encoding="utf-8")
    transactions = (
        ROOT / "architecture/decisions/0001-generated-ownership-and-transactions.md"
    ).read_text(encoding="utf-8")

    assert "V1, V2, and V3 layouts are unsupported" in policies
    assert "does not provide migration" in policies
    assert "supports previewable transactional migration" not in policies
    assert "migration tests" not in transactions


def test_release_guide_uses_the_language_neutral_feature_model():
    guide = (ROOT / "maintainers/releasing.md").read_text(encoding="utf-8")
    assert "C/C++ starter" in guide
    assert "Kotlin/Java starter" in guide
    assert "one plugin runtime component" in guide
    assert "exact release commit" in guide
    assert "true no-op" in guide
    assert "Gradle, KSP, Kotlin, CMake, JNI, and JSI" in guide
    assert "official plugin build and package-verification scripts" in guide
    assert "never rebuilds an unqualified artifact" in guide
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


def test_update_help_and_ownership_adr_describe_the_active_plan_contract():
    update_help = COMMAND_HELP["update"]
    ownership_adr = (
        ROOT / "architecture/decisions/0001-generated-ownership-and-transactions.md"
    ).read_text(encoding="utf-8")

    assert "one or all managed features" in ROOT_HELP
    assert "--all selects the complete managed project" in update_help
    assert "update --all --dry-run" in update_help
    assert "read-only `--dry-run` plan" in ownership_adr
    assert "does not offer\na dry-run" not in ownership_adr


def test_jsi_is_supported_without_overstating_runtime_availability():
    current_material = [
        ROOT / "README.md",
        ROOT / "src/supernote_module_generator/cli.py",
        ROOT / "src/supernote_module_generator/helptext.py",
        ROOT / "src/supernote_module_generator/feature_workflows.py",
        ROOT / "src/supernote_module_generator/plugin_runtime_codegen.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in current_material)
    lower = combined.lower()
    assert "experimental" not in lower
    assert "not that a particular Supernote firmware" in combined
    for runtime_constraint in ("compile", "pluginhost", "selinux"):
        assert runtime_constraint in lower
