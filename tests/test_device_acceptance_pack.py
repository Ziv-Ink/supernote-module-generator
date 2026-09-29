from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci.device_acceptance import materialize
from ci.device_acceptance.evidence import validate_evidence
from ci.device_acceptance.fixture_pdf import build_fixture_pdf


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ci/device_acceptance"
EVIDENCE = ROOT / "maintainers/device-evidence/v4-bounded-note-doc-2026-08-27"


def test_bounded_pack_has_fifteen_source_backed_checks_and_two_hosts() -> None:
    manifest = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    checks = manifest["checks"]

    assert manifest["schema_version"] == "1.0"
    assert manifest["suite"] == "sn-module-gen-bounded-note-doc"
    assert "pinned_file_reader_revision" not in manifest
    assert "pinned_sn_plugin_lib" not in manifest
    assert set(manifest["hosts"]) == {"note", "doc"}
    assert len(checks) == 15
    assert len({item["id"] for item in checks}) == 15
    assert {item["surface"] for item in checks} >= {
        "generated-cpp-jsi",
        "generated-kotlin-jni-jsi",
        "safe-android-api",
        "PluginManager-permission-flow",
        "PluginNoteAPI-or-PluginDocAPI",
    }
    assert manifest["hosts"]["note"]["permission_action"] == "deny"
    assert manifest["hosts"]["doc"]["permission_action"] == "allow_once"
    assert all(reference.startswith("https://docs.supernote.com/") for reference in manifest["references"])


def test_fixture_sources_cover_generated_cpp_jvm_android_and_terminal_results() -> None:
    application = (PACK / "App.tsx.tmpl").read_text(encoding="utf-8")
    native = (PACK / "device_probe.cpp").read_text(encoding="utf-8")
    header = (PACK / "DeviceCounter.hpp").read_text(encoding="utf-8")
    jvm = (PACK / "FeatureApi.kt").read_text(encoding="utf-8")
    manifest = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))

    for item in manifest["checks"]:
        assert f"'{item['id']}'" in application
    assert "SNMG_PERMISSION_REQUEST" in application
    assert "SNMG_TEST_RESULT" in application
    assert "PluginNoteAPI.saveCurrentNote" in application
    assert "PluginDocAPI.getCurrentTotalPages" in application
    assert "DeviceProbe.jvmEcho(DeviceProbe.nativeEcho('mixed'))" in application
    assert native.count("@SupernotePluginExport") == 4
    assert "@SupernotePluginAsync" in native
    assert "@SupernotePluginObject" in header
    assert "@SupernoteConstructor" in header
    assert "Build.MODEL" in jvm and "Build.VERSION.SDK_INT" in jvm
    assert "suspend fun jvmAsyncEcho" in jvm


def test_materializers_use_fresh_canonical_template_and_current_generator_commands() -> None:
    bounded = (PACK / "materialize.py").read_text(encoding="utf-8")
    runtime = (PACK / "q5_runtime/materialize.py").read_text(encoding="utf-8")
    replacement = (PACK / "q5_runtime/switch_to_v2.py").read_text(encoding="utf-8")

    assert materialize.TEMPLATE_ORIGIN in bounded
    assert "_scaffold(template_root, project" in runtime
    assert '"--skip-install"' not in runtime
    assert '"template", "sync"' not in runtime
    assert '"template", "status"' not in runtime
    assert '"validate"' in runtime
    assert '"update"' in runtime
    assert '"update"' in replacement
    assert '"update", FEATURE' not in bounded
    assert '"update", FEATURE' not in runtime
    assert '"update", FEATURE' not in replacement
    assert "App_v1.tsx" in runtime
    assert "App_v2.tsx" in replacement


def test_q5_runtime_fixture_uses_safe_post_completion_and_stale_handle_oracles() -> None:
    fixture = PACK / "q5_runtime"
    first = (fixture / "App_v1.tsx").read_text(encoding="utf-8")
    replacement = (fixture / "App_v2.tsx").read_text(encoding="utf-8")
    first_native = (fixture / "feature_v1.cpp").read_text(encoding="utf-8")
    replacement_native = (fixture / "feature_v2.cpp").read_text(encoding="utf-8")
    barrier = (
        fixture
        / "q5-runtime-autolink/android/src/main/java/com/zivink/q5runtimehost/"
        "Q5RuntimeHostPackage.kt"
    ).read_text(encoding="utf-8")

    assert "receiverHandle(counter)" in first
    assert "writeReceiverHandle(receiverHandle)" in first
    assert "delayedRevision(300000, markerPath)" in first
    assert "Q5_R7_OLD_COMPLETION_MARKER" in first_native
    assert "q5-receiver-handle:" in first_native
    assert "stale-old-generation-handle-rejection" in replacement
    assert "requireCurrentReceiverHandle(oldHandle)" in replacement
    assert "rawJsiObjectTransferred: false" in replacement
    assert "replacement-health-after-observed-old-completion" in replacement
    assert "completionMarker(markerPath)" in replacement
    assert "readOldCallback()" in replacement
    assert "oldCallbackBefore: callbackBefore" in replacement
    assert "oldCallbackAfter: callbackAfter" in replacement
    assert "6500" not in replacement
    assert "STALE_GENERATION_HANDLE" in replacement_native
    assert "class Q5RuntimeBarrierModule" in barrier
    assert 'File(root, "old-native-complete.txt")' in barrier
    assert 'File(root, "old-receiver-handle.txt")' in barrier
    assert 'File(root, "old-callback.txt")' in barrier


def test_doc_fixture_pdf_is_deterministic_and_self_contained() -> None:
    first = build_fixture_pdf()
    second = build_fixture_pdf()

    assert first == second
    assert first.startswith(b"%PDF-1.4\n")
    assert b"SN Module Gen Bounded DOC Acceptance" in first
    assert first.endswith(b"%%EOF\n")
    assert b"/Count 1" in first


@pytest.mark.parametrize(
    ("host", "expected_file", "requested"),
    (
        (
            "note",
            "/storage/emulated/0/Note/SNMG_Bounded_Acceptance/SNMG_Bounded_NOTE.note",
            0,
        ),
        (
            "doc",
            "/storage/emulated/0/Document/SNMG_Bounded_Acceptance.pdf",
            1,
        ),
    ),
)
def test_device_evidence_requires_all_source_backed_checks_and_permission_flow(
    host: str, expected_file: str, requested: int
) -> None:
    cases = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    checks = []
    events = []
    for item in cases["checks"]:
        actual: object = True
        if item["id"] == "current-file":
            actual = expected_file
        elif item["id"] == "android-build-info":
            actual = {"model": "Supernote Nomad", "sdk": 30}
        elif item["id"] == "permission-status-request-result":
            actual = {
                "before": 0,
                "requested": requested,
                "after": requested,
                "permission": cases["hosts"][host]["permission"],
                "action": cases["hosts"][host]["permission_action"],
            }
        checks.append({"id": item["id"], "status": "pass", "actual": actual})
        events.append(
            "prefix SNMG_TEST_EVENT "
            + json.dumps({"schema": "1.0", "host": host, "id": item["id"]})
        )
    request = {
        "schema": "1.0",
        "host": host,
        "permission": cases["hosts"][host]["permission"],
        "action": cases["hosts"][host]["permission_action"],
    }
    terminal = {
        "schema": "1.0",
        "suite": cases["suite"],
        "host": host,
        "pluginName": f"snmg-{host}-acceptance-unit",
        "status": "pass",
        "checks": checks,
    }
    log = "\n".join(
        [
            *events,
            "prefix SNMG_PERMISSION_REQUEST " + json.dumps(request),
            "prefix SNMG_TEST_RESULT " + json.dumps(terminal),
        ]
    )

    normalized = validate_evidence(log, cases, host, expected_file)

    assert normalized["status"] == "pass"
    assert normalized["schema_version"] == "1.0"
    assert normalized["check_count"] == 15
    assert normalized["permission"]["requested"] == requested

    tampered = log.replace('"status": "pass"', '"status": "fail"', 1)
    with pytest.raises(ValueError, match="terminal result did not pass"):
        validate_evidence(tampered, cases, host, expected_file)


@pytest.mark.parametrize(
    ("host", "expected_file"),
    (
        (
            "note",
            "/storage/emulated/0/Note/SNV4_Bounded_Acceptance/SNV4_Bounded_NOTE.note",
        ),
        ("doc", "/storage/emulated/0/Document/SNV4_Bounded_Acceptance.pdf"),
    ),
)
def test_retained_device_evidence_matches_the_source_contract(
    host: str, expected_file: str
) -> None:
    cases = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    normalized = validate_evidence(
        (EVIDENCE / f"{host}-reactnative.log").read_text(encoding="utf-8"),
        cases,
        host,
        expected_file,
    )
    retained = json.loads(
        (EVIDENCE / f"{host}-evidence.json").read_text(encoding="utf-8")
    )

    assert normalized == retained
    assert retained["check_count"] == 15


def test_device_evidence_rejects_mixed_marker_families() -> None:
    cases = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    historical = (EVIDENCE / "note-reactnative.log").read_text(encoding="utf-8")
    mixed = historical.replace("SNV4_TEST_RESULT ", "SNMG_TEST_RESULT ")

    with pytest.raises(ValueError, match="mixed current and historical marker families"):
        validate_evidence(
            mixed,
            cases,
            "note",
            "/storage/emulated/0/Note/SNV4_Bounded_Acceptance/SNV4_Bounded_NOTE.note",
        )


@pytest.mark.parametrize(
    ("host", "permission", "action"),
    (
        ("note", "plugin.permission.FILE:WRITE", "deny"),
        ("doc", "plugin.permission.FILE:READ", "allow_once"),
    ),
)
def test_materializer_keeps_each_fixture_scoped_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    permission: str,
    action: str,
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    project = tmp_path / f"Snmg{host.title()}AcceptanceUnit"
    revision = "8" * 40

    def fake_template_identity(root: Path) -> tuple[str, str, str]:
        assert root == template.resolve()
        return materialize.TEMPLATE_ORIGIN, "main", revision

    def fake_scaffold(
        root: Path,
        destination: Path,
        *,
        install_dependencies: bool,
    ) -> None:
        assert root == template.resolve()
        assert destination == project.resolve()
        assert install_dependencies is False
        destination.mkdir()
        (destination / "package.json").write_text(
            json.dumps(
                {
                    "name": destination.name,
                    "dependencies": {"sn-plugin-lib": "^0.1.19"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def fake_generator(
        root: Path, executable: str, *arguments: str
    ) -> dict[str, object]:
        assert root == project.resolve()
        assert executable == "generator"
        if arguments[0] == "add":
            (root / "local_modules/device-probe").mkdir(parents=True)
        return {"status": "success"}

    monkeypatch.setattr(materialize, "_template_identity", fake_template_identity)
    monkeypatch.setattr(materialize, "_scaffold", fake_scaffold)
    monkeypatch.setattr(materialize, "_generator", fake_generator)

    result = materialize.materialize(
        template, project, "generator", host, "Unit", PACK
    )

    config = json.loads((project / "PluginConfig.json").read_text(encoding="utf-8"))
    package = json.loads((project / "package.json").read_text(encoding="utf-8"))
    application = (project / "App.tsx").read_text(encoding="utf-8")
    assert result["permission"] == permission
    assert result["permission_action"] == action
    assert config["uses-permissions"] == [permission]
    assert package["name"] == config["name"]
    assert result["launch_label"] == config["name"]
    assert (project / ".supernote-launch-label").read_text(encoding="utf-8") == (
        config["name"] + "\n"
    )
    app = json.loads((project / "app.json").read_text(encoding="utf-8"))
    assert result["react_component"] == config["pluginKey"]
    assert app == {"name": config["pluginKey"], "displayName": config["name"]}
    assert config["pluginID"] == result["plugin_id"]
    assert len(config["pluginID"]) == 16
    assert package["dependencies"]["sn-plugin-lib"] == "^0.1.19"
    assert result["template_origin"] == materialize.TEMPLATE_ORIGIN
    assert result["template_branch"] == "main"
    assert result["template_revision"] == revision
    assert result["dependencies_installed"] is False
    assert "__HOST__" not in application
    assert f"const HOST = '{host}'" in application
