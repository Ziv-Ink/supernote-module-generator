from __future__ import annotations

import ast
import configparser
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.9 and 3.10 CI
    import tomli as tomllib

import pytest

from ci.complexity_ratchet import findings as complexity_findings


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/supernote_module_generator"

TYPED_V4_BOUNDARIES = {
    "src/supernote_module_generator/arguments.py",
    "src/supernote_module_generator/cpp_class_syntax.py",
    "src/supernote_module_generator/cpp_declarations.py",
    "src/supernote_module_generator/cpp_function_syntax.py",
    "src/supernote_module_generator/cpp_global_functions.py",
    "src/supernote_module_generator/cpp_lexer.py",
    "src/supernote_module_generator/cpp_member_semantics.py",
    "src/supernote_module_generator/cpp_member_shapes.py",
    "src/supernote_module_generator/cpp_members.py",
    "src/supernote_module_generator/cpp_source_routing.py",
    "src/supernote_module_generator/cpp_type_syntax.py",
    "src/supernote_module_generator/feature_identity.py",
    "src/supernote_module_generator/filesystem.py",
    "src/supernote_module_generator/filesystem_inventory.py",
    "src/supernote_module_generator/windows_authority.py",
    "src/supernote_module_generator/semantic_ir.py",
    "src/supernote_module_generator/generation_plan.py",
    "src/supernote_module_generator/generation_execution.py",
    "src/supernote_module_generator/generation_service.py",
    "src/supernote_module_generator/integrity_manifest.py",
    "src/supernote_module_generator/jsi_binding_decisions.py",
    "src/supernote_module_generator/models.py",
    "src/supernote_module_generator/semantic_types.py",
    "src/supernote_module_generator/template_contract.py",
    "src/supernote_module_generator/transaction.py",
    "src/supernote_module_generator/transaction_registry.py",
}

LOW_LEVEL_CONTRACT_MODULES = {
    "arguments.py",
    "cpp_class_syntax.py",
    "cpp_declarations.py",
    "cpp_function_syntax.py",
    "cpp_global_functions.py",
    "cpp_lexer.py",
    "cpp_member_semantics.py",
    "cpp_member_shapes.py",
    "cpp_members.py",
    "cpp_source_routing.py",
    "cpp_type_syntax.py",
    "feature_identity.py",
    "filesystem.py",
    "filesystem_inventory.py",
    "windows_authority.py",
    "semantic_ir.py",
    "generation_plan.py",
    "generation_execution.py",
    "integrity_manifest.py",
    "jsi_binding_decisions.py",
    "models.py",
    "semantic_types.py",
    "template_contract.py",
    "transaction.py",
    "transaction_registry.py",
}

FORBIDDEN_HIGH_LEVEL_IMPORTS = {
    "cli",
    "doctor",
    "feature_cli_operations",
    "feature_operations",
    "generation_service",
    "interaction",
    "rendering",
    "subprocesses",
    "v4_cli_operations",
    "v4_validation",
}


PACKAGE_NAME = "supernote_module_generator"


def _project_imports(source: str, *, filename: str = "<source>") -> set[str]:
    tree = ast.parse(source, filename=filename)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                prefix, separator, remainder = alias.name.partition(".")
                if prefix == PACKAGE_NAME and separator:
                    imported.add(remainder.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                if module:
                    imported.add(module.split(".", 1)[0])
                else:
                    imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif module == PACKAGE_NAME:
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif module.startswith(PACKAGE_NAME + "."):
                imported.add(module[len(PACKAGE_NAME) + 1 :].split(".", 1)[0])
    return imported


def test_static_correctness_and_gradual_typing_baselines_are_checked_in():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["ruff"]["target-version"] == "py39"
    assert config["tool"]["ruff"]["lint"]["select"] == ["F"]
    assert config["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"] == 10
    assert set(config["tool"]["mypy"]["files"]) == TYPED_V4_BOUNDARIES
    assert config["tool"]["mypy"]["disallow_untyped_defs"] is True
    assert config["tool"]["mypy"]["follow_imports"] == "silent"
    assert config["tool"]["mypy"]["incremental"] is False
    assert config["tool"]["mypy"]["warn_unused_ignores"] is True

    setup = configparser.ConfigParser()
    setup.read(ROOT / "setup.cfg", encoding="utf-8")
    dev = setup["options.extras_require"]["dev"]
    assert "ruff>=0.9,<1" in dev
    assert "mypy>=1.11,<2" in dev

    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    assert "python -m ruff check src tests ci" in workflow
    assert "--select C901" in workflow
    for path in (
        "generation_plan.py",
        "arguments.py",
        "integrity_manifest.py",
        "semantic_types.py",
        "cpp_lexer.py",
        "cpp_declarations.py",
        "cpp_members.py",
        "cpp_member_semantics.py",
        "cpp_member_shapes.py",
        "cpp_class_syntax.py",
        "cpp_function_syntax.py",
        "cpp_global_functions.py",
        "cpp_source_routing.py",
        "jsi_binding_decisions.py",
        "template_contract.py",
        "binding_codegen.py",
        "cpp_type_syntax.py",
    ):
        assert f"src/supernote_module_generator/{path}" in workflow
    assert "python -m mypy" in workflow
    assert "python ci/check_filesystem_complexity.py" in workflow
    assert "python ci/check_transaction_complexity.py" in workflow
    filesystem_ratchet = (ROOT / "ci/check_filesystem_complexity.py").read_text(
        encoding="utf-8"
    )
    assert '"_windows_open_no_follow_handle"' not in filesystem_ratchet
    assert '"_windows_list_directory_entries"' not in filesystem_ratchet
    assert '("filesystem_inventory.py", ())' in filesystem_ratchet
    transaction_ratchet = (ROOT / "ci/check_transaction_complexity.py").read_text(
        encoding="utf-8"
    )
    assert '("transaction_registry.py", ())' in transaction_ratchet
    assert '("generation_service.py", ())' in transaction_ratchet
    assert '("generation_execution.py", ())' in transaction_ratchet


def test_complexity_ratchet_rejects_a_missing_target(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not a regular file"):
        complexity_findings(ROOT, tmp_path / "missing.py")


def test_complexity_ratchet_preserves_duplicate_function_names(
    tmp_path: Path,
) -> None:
    target = tmp_path / "duplicate_names.py"
    conditions = "\n".join(
        f"        if value == {index}:\n            return {index}"
        for index in range(11)
    )
    target.write_text(
        "class First:\n"
        "    def route(self, value):\n"
        f"{conditions}\n"
        "        return -1\n\n"
        "class Second:\n"
        "    def route(self, value):\n"
        f"{conditions}\n"
        "        return -1\n",
        encoding="utf-8",
    )

    assert complexity_findings(ROOT, target) == (("route", 12), ("route", 12))


def test_low_level_v4_contracts_do_not_depend_on_command_or_ui_layers():
    for name in sorted(LOW_LEVEL_CONTRACT_MODULES):
        path = PACKAGE / name
        imports = _project_imports(
            path.read_text(encoding="utf-8"), filename=str(path)
        )
        forbidden = imports & FORBIDDEN_HIGH_LEVEL_IMPORTS
        assert not forbidden, f"{name} imports high-level modules: {sorted(forbidden)}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import supernote_module_generator.cli", "cli"),
        (
            "from supernote_module_generator.doctor import DoctorService",
            "doctor",
        ),
        ("from .rendering import HumanRenderer", "rendering"),
        ("from . import feature_operations", "feature_operations"),
        ("from supernote_module_generator import v4_cli_operations", "v4_cli_operations"),
    ],
)
def test_dependency_guard_normalizes_every_supported_import_form(
    source: str, expected: str
):
    assert _project_imports(source) == {expected}


def test_binding_frontend_has_no_disconnected_legacy_object_parser():
    source = (PACKAGE / "binding_codegen.py").read_text(encoding="utf-8")
    functions = {
        node.name
        for node in ast.walk(ast.parse(source, filename="binding_codegen.py"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_parse_object_members" not in functions
    assert "_parse_object_export" not in functions
    assert "_parse_v4_class_source" in functions
    assert "scan_cpp_class_source_model" in functions
