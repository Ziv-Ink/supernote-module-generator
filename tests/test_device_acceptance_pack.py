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

    assert manifest["schema_version"] == 1
    assert manifest["pinned_file_reader_revision"] == materialize.PINNED_REVISION
    assert manifest["pinned_sn_plugin_lib"] == "0.1.63"
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
    assert "SNV4_PERMISSION_REQUEST" in application
    assert "SNV4_TEST_RESULT" in application
    assert "PluginNoteAPI.saveCurrentNote" in application
    assert "PluginDocAPI.getCurrentTotalPages" in application
    assert "DeviceProbe.jvmEcho(DeviceProbe.nativeEcho('mixed'))" in application
    assert native.count("@SupernotePluginExport") == 4
    assert "@SupernotePluginAsync" in native
    assert "@SupernotePluginObject" in header
    assert "@SupernoteConstructor" in header
    assert "Build.MODEL" in jvm and "Build.VERSION.SDK_INT" in jvm
    assert "suspend fun jvmAsyncEcho" in jvm


def test_doc_fixture_pdf_is_deterministic_and_self_contained() -> None:
    first = build_fixture_pdf()
    second = build_fixture_pdf()

    assert first == second
    assert first.startswith(b"%PDF-1.4\n")
    assert b"SNV4 Bounded DOC Acceptance" in first
    assert first.endswith(b"%%EOF\n")
    assert b"/Count 1" in first


@pytest.mark.parametrize(
    ("host", "expected_file", "requested"),
    (
        (
            "note",
            "/storage/emulated/0/Note/SNV4_Bounded_Acceptance/SNV4_Bounded_NOTE.note",
            0,
        ),
        (
            "doc",
            "/storage/emulated/0/Document/SNV4_Bounded_Acceptance.pdf",
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
            "prefix SNV4_TEST_EVENT "
            + json.dumps({"schema": 1, "host": host, "id": item["id"]})
        )
    request = {
        "schema": 1,
        "host": host,
        "permission": cases["hosts"][host]["permission"],
        "action": cases["hosts"][host]["permission_action"],
    }
    terminal = {
        "schema": 1,
        "suite": cases["suite"],
        "host": host,
        "pluginName": f"snv4-{host}-acceptance-unit",
        "status": "pass",
        "checks": checks,
    }
    log = "\n".join(
        [
            *events,
            "prefix SNV4_PERMISSION_REQUEST " + json.dumps(request),
            "prefix SNV4_TEST_RESULT " + json.dumps(terminal),
        ]
    )

    normalized = validate_evidence(log, cases, host, expected_file)

    assert normalized["status"] == "pass"
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
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"sn-plugin-lib": "^0.1.19"}}) + "\n",
        encoding="utf-8",
    )
    strings = tmp_path / "android/app/src/main/res/values/strings.xml"
    strings.parent.mkdir(parents=True)
    strings.write_text(
        '<resources><string name="app_name">file_reader_test</string></resources>\n',
        encoding="utf-8",
    )
    (tmp_path / "app.json").write_text(
        json.dumps({"name": "file_reader_test", "displayName": "file_reader_test"})
        + "\n",
        encoding="utf-8",
    )
    activity = tmp_path / "android/app/src/main/java/com/file_reader_test/MainActivity.kt"
    activity.parent.mkdir(parents=True)
    activity.write_text(
        'class MainActivity { fun getMainComponentName(): String = "file_reader_test" }\n',
        encoding="utf-8",
    )
    (tmp_path / "index.js").write_text(
        "PluginManager.registerButton(1, ['NOTE', 'DOC'], { name: 'file_reader_test' });\n",
        encoding="utf-8",
    )

    def fake_run(root: Path, command: tuple[str, ...], *, capture: bool = False) -> str:
        assert root == tmp_path
        assert command == ("git", "rev-parse", "HEAD")
        assert capture is True
        return materialize.PINNED_REVISION + "\n"

    def fake_generator(
        root: Path, executable: str, *arguments: str
    ) -> dict[str, object]:
        assert root == tmp_path
        assert executable == "generator"
        if arguments[0] == "add":
            (root / "local_modules/device-probe").mkdir(parents=True)
        return {"status": "success"}

    monkeypatch.setattr(materialize, "_run", fake_run)
    monkeypatch.setattr(materialize, "_generator", fake_generator)

    result = materialize.materialize(
        tmp_path, "generator", host, "Unit", PACK
    )

    config = json.loads((tmp_path / "PluginConfig.json").read_text(encoding="utf-8"))
    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    application = (tmp_path / "App.tsx").read_text(encoding="utf-8")
    assert result["permission"] == permission
    assert result["permission_action"] == action
    assert config["uses-permissions"] == [permission]
    assert package["name"] == config["name"]
    assert result["launch_label"] == config["name"]
    assert (tmp_path / ".supernote-launch-label").read_text(encoding="utf-8") == (
        config["name"] + "\n"
    )
    assert f'<string name="app_name">{config["name"]}</string>' in (
        strings.read_text(encoding="utf-8")
    )
    app = json.loads((tmp_path / "app.json").read_text(encoding="utf-8"))
    assert app == {
        "name": config["name"],
        "displayName": config["name"],
    }
    assert result["react_component"] == config["name"]
    assert f'getMainComponentName(): String = "{config["name"]}"' in (
        activity.read_text(encoding="utf-8")
    )
    assert f"name: '{config['name']}'" in (
        (tmp_path / "index.js").read_text(encoding="utf-8")
    )
    assert config["pluginID"] == result["plugin_id"]
    assert len(config["pluginID"]) == 16
    assert package["dependencies"]["sn-plugin-lib"] == materialize.SN_PLUGIN_LIB_VERSION
    assert materialize.SN_PLUGIN_LIB_VERSION == "0.1.65"
    assert "__HOST__" not in application
    assert f"const HOST = '{host}'" in application
