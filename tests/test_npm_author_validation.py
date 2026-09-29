"""Author activation boundaries; resolver faults are not compiler evidence."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from supernote_module_generator import npm_package_validation as validation
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.npm_distribution import (
    PACKAGE_MANIFEST_PATH,
    render_package_manifest,
)
from supernote_module_generator.package_integration_codegen import feature_cmake_target
from supernote_module_generator.project_model import ProjectModel

from test_npm_distribution import (
    RUNTIME_PACKAGE,
    _add_cpp,
    _dependency_root,
    _file_inventory,
    _plugin,
)


@pytest.fixture
def author(tmp_path: Path) -> tuple[Path, Path]:
    root = _plugin(tmp_path / "publisher")
    feature = _add_cpp(root)
    package = json.loads((root / "package.json").read_text())
    package["dependencies"] = {
        "@fixture/alpha": "file:local_modules/@fixture/alpha",
        "@supernote/runtime": "file:runtime-package",
    }
    (root / "package.json").write_text(json.dumps(package) + "\n")
    return root, feature


@pytest.fixture
def installed_author(author: tuple[Path, Path]) -> tuple[Path, Path]:
    root, feature = author
    for name, source in (("@fixture/alpha", feature), ("@supernote/runtime", RUNTIME_PACKAGE)):
        destination = _dependency_root(root, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    return root, feature


def _validate_unchanged(root: Path):
    project = ProjectModel.discover(root)
    before = _file_inventory(root)
    issues = validation.validate_author_package_inputs(root, project)
    assert _file_inventory(root) == before
    return issues


def _resolver_reply(
    monkeypatch: pytest.MonkeyPatch, *, output: str, error: str = "", code: int = 0
):
    """Fault only the child-process boundary; keep model and filesystem real."""
    monkeypatch.setattr(validation.shutil, "which", lambda _name: "/test/node")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, code, output, error),
    )


def test_author_validation_reports_missing_node_without_writing(author, monkeypatch):
    root, _feature = author
    monkeypatch.setattr(validation.shutil, "which", lambda _name: None)
    issues = _validate_unchanged(root)
    assert [(issue.code, issue.severity, issue.scope) for issue in issues] == [
        ("SNMG_NODE_UNAVAILABLE", "error", "dependency")
    ]


@pytest.mark.parametrize(
    ("output", "error", "expected_code", "expected_message"),
    [
        (
            "",
            "Supernote package discovery [SNMG_DEPENDENCY_MISSING]: absent",
            "SNMG_DEPENDENCY_MISSING",
            "absent",
        ),
        ("stdout failure", "", "SNMG_PACKAGE_DISCOVERY_FAILED", "stdout failure"),
        ("", "", "SNMG_PACKAGE_DISCOVERY_FAILED", "Supernote package discovery failed"),
        (
            "",
            "Supernote package discovery []: malformed",
            "SNMG_PACKAGE_DISCOVERY_FAILED",
            "malformed",
        ),
    ],
)
def test_author_validation_preserves_resolver_failure_diagnostics(
    author,
    monkeypatch,
    output,
    error,
    expected_code,
    expected_message,
):
    root, _feature = author
    _resolver_reply(monkeypatch, output=output, error=error, code=1)
    issues = _validate_unchanged(root)
    assert len(issues) == 1
    assert issues[0].code == expected_code
    assert expected_message in issues[0].message
    assert "npm or Yarn" in issues[0].suggested_command
    assert validation.package_issue_warnings(issues) == (issues[0].message,)


@pytest.mark.parametrize("output", ["not json", '{"modules":null}', '{"modules":[{}]}'])
def test_author_validation_rejects_malformed_resolver_output(author, monkeypatch, output):
    root, _feature = author
    _resolver_reply(monkeypatch, output=output)
    issues = _validate_unchanged(root)
    assert [issue.code for issue in issues] == ["SNMG_PACKAGE_DISCOVERY_INVALID"]


def test_author_validation_reports_declared_nonmodule_from_real_resolver(author):
    root, _feature = author
    if shutil.which("node") is None:
        pytest.skip("Node.js is required")
    for name in ("@fixture/alpha", "@supernote/runtime"):
        destination = _dependency_root(root, name)
        destination.mkdir(parents=True)
        (destination / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
    issues = _validate_unchanged(root)
    assert [issue.code for issue in issues] == ["SNMG_MODULE_DEPENDENCY_MISSING"]


@pytest.mark.parametrize(
    ("specification", "accepted"),
    [
        ("file:local_modules/@fixture/alpha", True),
        ("link:local_modules/@fixture/alpha", True),
        ("workspace:*", True),
        ("file:local_modules/@fixture/other", False),
        ("file:alpha.tgz", False),
        ("file:alpha.TAR.GZ", False),
        ("1.0.0", False),
        ("ABSOLUTE", False),
    ],
)
def test_copied_author_package_requires_explicit_local_relationship(
    installed_author,
    specification,
    accepted,
):
    root, feature = installed_author
    if shutil.which("node") is None:
        pytest.skip("Node.js is required")
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"]["@fixture/alpha"] = (
        "file:" + str(feature) if specification == "ABSOLUTE" else specification
    )
    package_path.write_text(json.dumps(package) + "\n")
    issues = _validate_unchanged(root)
    assert [issue.code for issue in issues] == (
        [] if accepted else ["SNMG_MODULE_LOCAL_RELATIONSHIP_INVALID"]
    )


def test_undeclared_author_feature_does_not_require_installation(installed_author):
    root, _feature = installed_author
    if shutil.which("node") is None:
        pytest.skip("Node.js is required")
    _add_cpp(root, "@fixture/beta", public_name="Beta", namespace="com.fixture.beta")
    assert _validate_unchanged(root) == ()


@pytest.mark.parametrize("missing_side", ["author", "installed"])
def test_author_manifest_disappearing_after_discovery_is_reported(
    installed_author,
    monkeypatch,
    missing_side,
):
    root, feature = installed_author
    installed = _dependency_root(root, "@fixture/alpha")
    _resolver_reply(
        monkeypatch,
        output=json.dumps(
            {
                "modules": [
                    {
                        "name": "@fixture/alpha",
                        "root": str(installed),
                        "manifest": str(installed / PACKAGE_MANIFEST_PATH),
                    }
                ]
            }
        ),
    )
    target = feature if missing_side == "author" else installed
    (target / PACKAGE_MANIFEST_PATH).unlink()
    issues = _validate_unchanged(root)
    assert [issue.code for issue in issues] == ["SNMG_MODULE_PAYLOAD_MISSING"]
    assert "refresh the dependency" in issues[0].suggested_command


@pytest.mark.parametrize("unresolvable", ["installed", "author"])
def test_author_validation_handles_package_root_resolution_failure(
    installed_author,
    monkeypatch,
    unresolvable,
):
    root, feature = installed_author
    installed = _dependency_root(root, "@fixture/alpha")
    project = ProjectModel.discover(root)
    before = _file_inventory(root)
    _resolver_reply(
        monkeypatch,
        output=json.dumps(
            {
                "modules": [
                    {
                        "name": "@fixture/alpha",
                        "root": str(installed),
                        "manifest": str(installed / PACKAGE_MANIFEST_PATH),
                    }
                ]
            }
        ),
    )
    target = installed if unresolvable == "installed" else feature
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == target:
            raise OSError("controlled package-root resolution failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    issues = validation.validate_author_package_inputs(root, project)
    assert [issue.code for issue in issues] == (
        [] if unresolvable == "installed" else ["SNMG_MODULE_LOCAL_RELATIONSHIP_INVALID"]
    )
    assert _file_inventory(root) == before


@pytest.mark.parametrize(
    "path", ["", "/absolute", "a/../b", "a//b", "./a", "a/", PACKAGE_MANIFEST_PATH]
)
def test_distribution_manifest_rejects_noncanonical_or_recursive_payload(author, path):
    root, _feature = author
    record = FeatureOperationService(root).records()[0]
    with pytest.raises(ValueError, match="invalid npm distribution payload path"):
        render_package_manifest(record, {path: b"payload"})


def test_distribution_manifest_rejects_empty_payload(author):
    root, _feature = author
    with pytest.raises(ValueError, match="cannot be empty"):
        render_package_manifest(FeatureOperationService(root).records()[0], {})


def test_distribution_manifest_sorts_paths_and_binds_exact_payload_bytes(author):
    root, _feature = author
    record = FeatureOperationService(root).records()[0]
    output = render_package_manifest(record, {"z.txt": b"", "a.txt": b"abc"})
    assert output == render_package_manifest(record, {"a.txt": b"abc", "z.txt": b""})
    manifest = json.loads(output)
    assert manifest["packageName"] == "@fixture/alpha"
    assert manifest["publicName"] == "Alpha"
    assert manifest["androidNamespace"] == "com.fixture.alpha"
    assert manifest["payload"] == [
        {
            "path": "a.txt",
            "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        },
        {
            "path": "z.txt",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
    ]


@pytest.mark.parametrize(
    "identity",
    ["feature:0123456789abcdef", "supernote:feature:", "supernote:feature:0123456789abcdeg"],
)
def test_package_cmake_target_rejects_invalid_feature_identity(identity):
    with pytest.raises(ValueError, match="invalid Supernote feature identity"):
        feature_cmake_target(identity)
