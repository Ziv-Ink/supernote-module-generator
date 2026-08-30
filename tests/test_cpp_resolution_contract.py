import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures/cpp_resolution"


def test_d042_resolution_fixture_inventory_is_closed_and_deterministic():
    manifest = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["decision"] == "D-042"
    cases = manifest["cases"]
    assert len({case["file"] for case in cases}) == len(cases)
    assert len({case["rule"] for case in cases}) == len(cases)
    assert {case["outcome"] for case in cases} == {"accept", "reject"}
    assert {path.name for path in FIXTURES.glob("*.hpp")} == {
        case["file"] for case in cases
    }
    for case in cases:
        source = (FIXTURES / case["file"]).read_text(encoding="utf-8")
        assert "// @SupernotePluginObject" in source


def test_d042_rejection_contract_covers_every_deferred_resolution_form():
    manifest = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
    rejected = {
        case["rule"] for case in manifest["cases"] if case["outcome"] == "reject"
    }

    assert rejected == {
        "alias_bridge_visible_spelling",
        "marked_alias",
        "anonymous_namespace_bridge_type",
        "nested_bridge_declaration",
        "ambiguous_unqualified_reference",
        "forward_declaration_only",
        "same_final_public_name_collision",
    }
