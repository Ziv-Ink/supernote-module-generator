from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import test_q5_combined_installed_acceptance as acceptance


ROOT = Path(__file__).resolve().parents[1]
JVM_MARKER = "Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M"


@pytest.mark.parametrize(
    "fault", (None, "missing-execution", "ksp", "generator", "mutation", "wrong-sources")
)
def test_online_jvm_qualification_retains_consumption_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    """Test harness rejection contracts, not generated JVM execution itself."""
    consumer = tmp_path / "consumer"
    modules = []
    for name in ("native", "kotlin", "java", "mixed"):
        root = consumer / "node_modules/@q5" / name
        adapter = root / ".supernote-generated/android/jvm/KspAdapters.kt"
        adapter.parent.mkdir(parents=True)
        adapter.write_text("object Adapter_012345 {\n}\n", encoding="utf-8")
        modules.append({
            "name": "@q5/" + name,
            "root": str(root),
            "jvmSourceDirs": [str(adapter.parent)],
            "jvmRegistrationSources": [],
        })
    runtime = consumer / "node_modules/@supernote/runtime"
    runtime.mkdir(parents=True)
    (runtime / "input.txt").write_text("immutable\n", encoding="utf-8")
    sources = sorted([str(runtime / "jvm"), *(m["jvmSourceDirs"][0] for m in modules)])
    guard = acceptance._ConsumerGeneratorGuard(tmp_path / "guard")
    monkeypatch.setenv("SUPERNOTE_GRADLE_COMMAND", str(tmp_path / "gradle"))
    monkeypatch.setattr(acceptance, "TRACE", [])
    monkeypatch.delenv("SUPERNOTE_Q5_COMBINED_EVIDENCE", raising=False)

    def fake_gradle(argv, **kwargs):
        assert argv[0] == str(tmp_path / "gradle")
        assert "--offline" not in argv
        assert {":supernote-runtime:verifyQ5Autolink", ":host-jvm:classes",
                ":host-jvm:runHarness", ":host-jvm:tasks", "--all"} <= set(argv)
        assert kwargs["env"]["SUPERNOTE_MODULE_COMMAND"] == str(guard.command)
        assert kwargs["env"]["SUPERNOTE_Q5_GENERATOR_TRAP_LOG"] == str(guard.log)
        harness = (consumer / "android/host-jvm/src/main/kotlin/Q5JvmHarness.kt").read_text()
        for expected in ("Kotlin:K", "Java:J", "Mixed:M"):
            assert f'== "{expected}"' in harness
        inventory = consumer / (
            "android/build/react-native-libraries/supernote-runtime/generated/"
            "supernote-composition/inventory.json"
        )
        inventory.parent.mkdir(parents=True)
        inventory.write_text(json.dumps({"runtime": {"root": str(runtime)}, "modules": modules}))
        marker_sources = [] if fault == "wrong-sources" else sources
        output = "Q5_ANDROID_AUTOLINK_JVM_SOURCES=" + json.dumps(marker_sources) + "\n"
        if fault != "missing-execution":
            output += JVM_MARKER + "\n"
        if fault == "ksp":
            output += ":host-jvm:kspKotlin\n"
        if fault == "generator":
            output += "sn-module-gen update\n"
        if fault == "mutation":
            (runtime / "input.txt").write_text("changed\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(acceptance.subprocess, "run", fake_gradle)
    if fault is not None:
        with pytest.raises(AssertionError):
            acceptance._execute_jvm_consumer(consumer, guard)
    else:
        assert acceptance._execute_jvm_consumer(consumer, guard) == {
            "android_autolink_sources": sources,
            "host_jvm_execution": JVM_MARKER,
        }
        assert acceptance.TRACE[-1]["generator_guard"]["attempts_after"] == []


def test_runtime_lf_checkout_preserves_manifest_with_autocrlf(tmp_path: Path) -> None:
    """Exercise Git's conversion on any host; native Windows CI remains required."""
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert [line for line in rules.splitlines() if line and not line.startswith("#")] == [
        "/npm/supernote-runtime/** text=auto eol=lf"
    ]
    git = shutil.which("git")
    assert git is not None, "Git is required for checkout-byte qualification"

    def run(*args: str, cwd: Path) -> None:
        result = subprocess.run(
            [git, "-c", "core.autocrlf=false", "-c", "core.attributesFile=", *args],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(ROOT / "npm/supernote-runtime", source / "npm/supernote-runtime")
    (source / "unrelated.txt").write_bytes(b"outside runtime\n")
    run("init", cwd=source)
    run("add", ".", cwd=source)

    def commit() -> None:
        run("-c", "user.name=Qualification", "-c", "user.email=qualification@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Checkout fixture", cwd=source)

    commit()
    control = tmp_path / "control"
    run("-c", "core.autocrlf=true", "clone", "--no-local", str(source), str(control), cwd=tmp_path)
    relative = "npm/supernote-runtime/android/build.gradle"
    control_bytes = (control / relative).read_bytes()
    assert b"\r\n" in control_bytes
    assert control_bytes != (ROOT / relative).read_bytes()

    (source / ".gitattributes").write_text(rules, encoding="utf-8")
    run("add", ".gitattributes", cwd=source)
    commit()
    fixed = tmp_path / "fixed"
    run("-c", "core.autocrlf=true", "clone", "--no-local", str(source), str(fixed), cwd=tmp_path)
    runtime = fixed / "npm/supernote-runtime"
    manifest = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert manifest["payload"]
    for entry in manifest["payload"]:
        content = (runtime / entry["path"]).read_bytes()
        assert b"\r\n" not in content, entry["path"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"], entry["path"]
    assert (fixed / "unrelated.txt").read_bytes() == b"outside runtime\r\n"
