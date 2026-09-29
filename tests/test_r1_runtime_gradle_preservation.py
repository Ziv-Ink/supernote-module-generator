from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "npm/supernote-runtime"


def _inventory(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted([*names, *files]):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            row: dict[str, object] = {
                "mode": stat.S_IMODE(metadata.st_mode),
            }
            if path.is_symlink():
                row.update(kind="symlink", target=os.readlink(path))
            elif path.is_dir():
                row["kind"] = "directory"
            elif path.is_file():
                row.update(
                    kind="file",
                    size=metadata.st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            else:
                row["kind"] = "other"
            result[relative] = row
    return result


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    records: list[dict[str, object]],
    label: str,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    records.append(
        {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "exit": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def _clean_fixture(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name
            in {
                ".git",
                ".gradle",
                "build",
                "node_modules",
                "local.properties",
                "package-lock.json",
            }
        }

    shutil.copytree(source, target, ignore=ignore)


def _clone_installed_consumer(source: Path, target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {
            name
            for name in names
            if name in {".git", ".gradle", "local.properties"}
        }
        parent = Path(directory)
        for name in names:
            candidate = parent / name
            relative = candidate.relative_to(source).as_posix()
            if name == "build" and (
                relative == "build"
                or relative in {"android/build", "android/app/build"}
                or (
                    relative.startswith("node_modules/")
                    and relative.endswith("/android/build")
                )
            ):
                ignored.add(name)
        return ignored

    shutil.copytree(
        source,
        target,
        ignore=ignore,
        copy_function=os.link,
        symlinks=True,
    )


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def _gradle_command(consumer: Path) -> list[str]:
    wrapper = consumer / "android/gradlew"
    if os.name == "nt":
        return [str(wrapper.with_suffix(".bat"))]
    return ["bash", str(wrapper)]


def _build_directory(properties: str) -> Path:
    for line in properties.splitlines():
        if line.startswith("buildDir: "):
            return Path(line.removeprefix("buildDir: ")).resolve()
    raise AssertionError("Gradle properties did not report buildDir")


def _compile(
    consumer: Path,
    environment: dict[str, str],
    records: list[dict[str, object]],
    label: str,
) -> Path:
    gradle = [
        *_gradle_command(consumer),
        "--no-daemon",
        "--console=plain",
        "-p",
        "android",
    ]
    properties = _run(
        [*gradle, ":supernote_runtime:properties"],
        cwd=consumer,
        environment=environment,
        records=records,
        label=f"{label}-properties",
    )
    build_directory = _build_directory(properties.stdout)
    expected_root = (consumer / "android/build").resolve()
    assert build_directory.is_relative_to(expected_root)
    _run(
        [
            *gradle,
            ":supernote_runtime:compileDebugJavaWithJavac",
            ":app:compileDebugJavaWithJavac",
        ],
        cwd=consumer,
        environment=environment,
        records=records,
        label=f"{label}-compile",
    )
    assert build_directory.is_dir()
    assert any(path.is_file() for path in build_directory.rglob("*"))
    return build_directory


@pytest.mark.skipif(
    os.environ.get("SNMG_RUN_R1_GRADLE_PRESERVATION") != "1",
    reason="set SNMG_RUN_R1_GRADLE_PRESERVATION=1 for the real Gradle control",
)
def test_runtime_gradle_preserves_installed_and_shared_linked_packages(
    tmp_path: Path,
) -> None:
    fixture_value = os.environ.get("SNMG_R1_ANDROID_FIXTURE")
    generator_value = os.environ.get("SUPERNOTE_MODULE_COMMAND")
    if fixture_value is None or generator_value is None:
        pytest.fail(
            "SNMG_R1_ANDROID_FIXTURE and absolute SUPERNOTE_MODULE_COMMAND are required"
        )
    fixture = Path(fixture_value).resolve()
    generator = Path(generator_value).resolve()
    assert fixture.is_dir()
    assert generator.is_absolute() and generator.is_file()
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        pytest.fail("Node.js and npm are required")

    records: list[dict[str, object]] = []
    environment = dict(os.environ)
    environment["SUPERNOTE_MODULE_COMMAND"] = str(generator)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")
    packed = tmp_path / "packed"
    packed.mkdir()
    pack = _run(
        [
            npm,
            "pack",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            str(packed),
        ],
        cwd=RUNTIME,
        environment=environment,
        records=records,
        label="pack-runtime",
    )
    tarball = packed / json.loads(pack.stdout)[0]["filename"]

    consumer = tmp_path / "installed-consumer"
    _clean_fixture(fixture, consumer)
    vendor_runtime = consumer / "vendor" / tarball.name
    vendor_runtime.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tarball, vendor_runtime)
    _run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=consumer,
        environment=environment,
        records=records,
        label="install-consumer",
    )
    runtime = consumer / "node_modules/@supernote/runtime"
    before_installed = _inventory(runtime)
    config = _run(
        [node, "node_modules/react-native/cli.js", "config"],
        cwd=consumer,
        environment=environment,
        records=records,
        label="react-native-config",
    )
    runtime_config = json.loads(config.stdout)["dependencies"]["@supernote/runtime"]
    assert runtime_config["platforms"]["android"]["packageInstance"] == (
        "new SupernoteModulePackage()"
    )
    installed_build = _compile(consumer, environment, records, "installed")
    after_installed = _inventory(runtime)
    assert after_installed == before_installed

    shared_host = tmp_path / "shared-runtime-host"
    shared_host.mkdir()
    _run(
        [
            npm,
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--prefix",
            str(shared_host),
            str(tarball),
        ],
        cwd=tmp_path,
        environment=environment,
        records=records,
        label="install-shared-runtime",
    )
    shared_runtime = shared_host / "node_modules/@supernote/runtime"
    before_shared = _inventory(shared_runtime)
    linked_builds: list[Path] = []
    linked_consumers: list[Path] = []
    for name in ("linked-consumer-a", "linked-consumer-b"):
        linked_consumer = tmp_path / name
        _clone_installed_consumer(consumer, linked_consumer)
        linked_runtime = linked_consumer / "node_modules/@supernote/runtime"
        shutil.rmtree(linked_runtime)
        _link_directory(linked_runtime, shared_runtime)
        assert linked_runtime.resolve() == shared_runtime.resolve()
        linked_consumers.append(linked_consumer)
        linked_builds.append(
            _compile(linked_consumer, environment, records, name)
        )

    after_shared = _inventory(shared_runtime)
    assert after_shared == before_shared
    assert linked_builds[0] != linked_builds[1]
    assert len(linked_consumers) == len(linked_builds)
    for linked_consumer, build_directory in zip(linked_consumers, linked_builds):
        assert build_directory.is_relative_to(
            (linked_consumer / "android/build").resolve()
        )
    generated_sets = [
        {path.resolve() for path in build.rglob("*") if path.is_file()}
        for build in linked_builds
    ]
    assert generated_sets[0]
    assert generated_sets[1]
    assert generated_sets[0].isdisjoint(generated_sets[1])

    report = {
        "status": "passed",
        "runtime_tarball": str(tarball),
        "runtime_tarball_sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
        "installed_runtime": str(runtime),
        "installed_inventory_before": before_installed,
        "installed_inventory_after": after_installed,
        "installed_build_directory": str(installed_build),
        "shared_runtime": str(shared_runtime),
        "shared_inventory_before": before_shared,
        "shared_inventory_after": after_shared,
        "linked_build_directories": [str(path) for path in linked_builds],
        "linked_generated_file_counts": [len(paths) for paths in generated_sets],
        "commands": records,
    }
    output_value = os.environ.get("SNMG_R1_PRESERVATION_OUTPUT")
    if output_value is not None:
        output = Path(output_value).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, sort_keys=True))
