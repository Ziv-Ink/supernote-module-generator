from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

from supernote_module_generator.cli import main
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.jvm_manifest import (
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_owner_identity,
)
from supernote_module_generator.semantic import SourceProvenance
from supernote_module_generator.source_models import (
    DeclarationTarget,
    JvmDeclarationSource,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    SourceIntent,
    SupernoteMarker,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGE = Path(
    os.environ.get("SUPERNOTE_Q5_RUNTIME_ROOT", ROOT / "npm/supernote-runtime")
).resolve()
RESOLVER = ROOT / "src/supernote_module_generator/node/resolve-packages.js"
RUNTIME_RESOLVER = RUNTIME_PACKAGE / "resolve-packages.js"


def _public_cli(arguments: list[str], *, cwd: Path) -> int:
    installed = os.environ.get("SUPERNOTE_Q5_MODULE_COMMAND")
    if installed is None:
        return main(arguments, cwd=cwd)
    executable = Path(installed)
    assert executable.is_absolute()
    assert executable.is_file()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(executable), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if result.returncode not in {0, 1}:
        print(result.stdout)
        print(result.stderr)
    return result.returncode


def _plugin(root: Path) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "publisher", "dependencies": {}}) + "\n",
        encoding="utf-8",
    )
    (root / "android/settings.gradle").write_text("include ':app'\n")
    (root / "android/app/build.gradle").write_text("plugins {}\n")
    gradle = root / "android/gradlew"
    gradle.write_text("#!/bin/sh\nexit 0\n")
    gradle.chmod(0o755)
    return root


def _add_cpp(
    root: Path,
    name: str = "@fixture/alpha",
    *,
    public_name: str = "Alpha",
    namespace: str = "com.fixture.alpha",
) -> Path:
    code = _public_cli(
        [
            "add",
            name,
            "--starter",
            "cpp",
            "--javascript-name",
            public_name,
            "--android-namespace",
            namespace,
            "--yes",
        ],
        cwd=root,
    )
    assert code == 0
    scope, package = name.split("/", 1)
    return root / "local_modules" / scope / package


def _add_generated_feature(
    root: Path,
    name: str,
    *,
    public_name: str,
    namespace: str,
    starters: tuple[StarterFamily, ...],
) -> Path:
    service = FeatureOperationService(root)
    feature_root = service.add(
        FeatureConfig(
            root / "local_modules" / Path(*name.split("/")),
            name,
            "1.2.3",
            namespace,
            public_name,
            starters=starters,
        )
    )
    record = service.find_record(name)
    manifests: dict[str, JvmSourceManifest] = {}
    if StarterFamily.JVM in starters:
        owner_class = f"{namespace}.FeatureApiKt"
        owner_id = jvm_owner_identity(owner_class)
        declaration_id = jvm_declaration_identity(
            owner_class,
            "greetFromJvm",
            "(Ljava/lang/String;)Ljava/lang/String;",
        )
        declaration = JvmDeclarationSource(
            SourceProvenance(declaration_id, "kotlin", "FeatureApi.kt", 6, 1),
            owner_id,
            owner_class,
            "greetFromJvm",
            "(Ljava/lang/String;)Ljava/lang/String;",
            (JvmParameterSource("kotlin.String", "name"),),
            "kotlin.String",
            False,
            SourceIntent.from_markers(
                DeclarationTarget.FUNCTION,
                (SupernoteMarker.EXPORT,),
            ),
            "public",
            jvm_adapter_identity(declaration_id),
            JvmLanguage.KOTLIN,
            False,
            True,
        )
        owner = JvmOwnerSource(
            SourceProvenance(owner_id, "kotlin", "FeatureApi.kt", 1, 1),
            JvmLanguage.KOTLIN,
            owner_class,
            "FeatureApiKt",
            JvmOwnerForm.KOTLIN_TOP_LEVEL,
            SourceIntent.from_markers(DeclarationTarget.CLASS, ()),
            (),
            (declaration,),
        )
        manifests[record.manifest.feature_id] = JvmSourceManifest(
            record.manifest.feature_id,
            "test-ksp",
            (owner,),
        )
    generator = GenerationService(root)
    plan = generator.plan(
        operation="update",
        requested_targets=(name,),
        jvm_manifests=manifests,
        jvm_adapter_sources={
            feature_id: (
                b"package supernote.generated.adapters\n\n"
                b"object FixtureAdapter\n"
            )
            for feature_id in manifests
        },
        allow_unmanifested_bootstrap=True,
    )
    generator.execute(plan)
    return feature_root


def _write_runtime(root: Path, *, protocol: str = "2.0", name: str = "@supernote/runtime") -> None:
    shutil.copytree(RUNTIME_PACKAGE, root)
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    package["name"] = name
    package["supernoteNativeRuntime"]["protocol"] = protocol
    (root / "package.json").write_text(json.dumps(package) + "\n", encoding="utf-8")


def _write_module(
    root: Path,
    name: str,
    *,
    feature_id: str,
    public_name: str,
    namespace: str,
) -> None:
    target_suffix = hashlib.sha256(feature_id.encode("utf-8")).hexdigest()[:16]
    cmake_target = f"supernote_feature_{target_suffix}"
    generated = root / ".supernote-generated"
    android = generated / "android"
    android.mkdir(parents=True)
    (android / "jvm").mkdir()
    native = root / "android/src/main/cpp"
    native.mkdir(parents=True)
    (native / "feature.cpp").write_text(f"// source {name}\n")
    package = {
        "name": name,
        "version": "1.2.3",
        "main": ".supernote-generated/index.js",
        "supernoteNativeModule": {
            "kind": "supernote_module_distribution",
            "manifest": ".supernote-generated/package-manifest.json",
            "protocol": "2.0",
            "schemaVersion": "2.0",
        },
    }
    (root / "package.json").write_text(json.dumps(package) + "\n")
    authored = {
        "schema_version": "1.0",
        "kind": "supernote_module_feature",
        "feature_id": feature_id,
        "npm_name": name,
        "public_name": public_name,
        "android_namespace": namespace,
        "package_version": "1.2.3",
    }
    (root / ".supernote-module.json").write_text(json.dumps(authored) + "\n")
    registration = {
        "kind": "supernote_module_registration",
        "nativeAbi": "supernote-jsi-v1",
        "schemaVersion": "2.0",
        "protocol": "2.0",
        "featureId": feature_id,
        "packageName": name,
        "publicName": public_name,
        "androidNamespace": namespace,
        "installers": {
            "native": {
                "source": ".supernote-generated/android/package_registration.cpp",
                "symbol": f"supernote_package_install_native_{target_suffix}",
            },
            "jvm": None,
        },
        "nativeSourceDirs": ["android/src/main/cpp"],
        "jvmSourceDirs": [],
        "bindingSources": [
            ".supernote-generated/android/feature.cpp",
            ".supernote-generated/android/internal.cpp",
            ".supernote-generated/android/package_registration.cpp",
        ],
        "bindingHeaders": [
            ".supernote-generated/android/internal.hpp",
            ".supernote-generated/android/package_registration.hpp",
        ],
        "cmake": ".supernote-generated/android/CMakeLists.txt",
        "cmakeTarget": cmake_target,
        "jvmRegistrationSources": [
            ".supernote-generated/android/jvm/JvmRegistration.java"
        ],
        "semantic": ".supernote-generated/android/semantic.json",
        "conversion": ".supernote-generated/android/conversion.json",
    }
    registration_path = android / "registration.json"
    registration_path.write_text(json.dumps(registration) + "\n")
    binding_path = android / "feature.cpp"
    binding_path.write_text(f"// {name}\n")
    for relative, content in {
        ".supernote-generated/index.js": "export default {};\n",
        ".supernote-generated/index.d.ts": "declare const value: object; export default value;\n",
        ".supernote-generated/android/semantic.json": "{}\n",
        ".supernote-generated/android/conversion.json": "{}\n",
        ".supernote-generated/android/internal.cpp": "// internal\n",
        ".supernote-generated/android/internal.hpp": "// internal\n",
        ".supernote-generated/android/CMakeLists.txt": (
            f"add_library({cmake_target} STATIC package_registration.cpp)\n"
            f"set(SUPERNOTE_PACKAGE_FEATURE_TARGET {cmake_target} PARENT_SCOPE)\n"
        ),
        ".supernote-generated/android/package_registration.cpp": (
            "// package registration\n"
        ),
        ".supernote-generated/android/package_registration.hpp": (
            "// package registration\n"
        ),
        ".supernote-generated/android/jvm/JvmRegistration.java": (
            "// JVM package registration\n"
        ),
    }.items():
        (root / relative).write_text(content)
    payload_paths = [
        "package.json",
        ".supernote-module.json",
        ".supernote-generated/android/registration.json",
        ".supernote-generated/android/feature.cpp",
        ".supernote-generated/index.js",
        ".supernote-generated/index.d.ts",
        ".supernote-generated/android/semantic.json",
        ".supernote-generated/android/conversion.json",
        ".supernote-generated/android/internal.cpp",
        ".supernote-generated/android/internal.hpp",
        ".supernote-generated/android/CMakeLists.txt",
        ".supernote-generated/android/package_registration.cpp",
        ".supernote-generated/android/package_registration.hpp",
        ".supernote-generated/android/jvm/JvmRegistration.java",
        "android/src/main/cpp/feature.cpp",
    ]
    payload = []
    for relative in payload_paths:
        content = (root / relative).read_bytes()
        payload.append(
            {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}
        )
    manifest = {
        "schemaVersion": "2.0",
        "kind": "supernote_module_distribution",
        "protocol": "2.0",
        "featureId": feature_id,
        "packageName": name,
        "packageVersion": "1.2.3",
        "publicName": public_name,
        "androidNamespace": namespace,
        "registration": ".supernote-generated/android/registration.json",
        "payload": payload,
    }
    (generated / "package-manifest.json").write_text(json.dumps(manifest) + "\n")


def _consumer(root: Path, dependencies: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "consumer", "dependencies": dependencies}) + "\n"
    )
    return root


def _dependency_root(consumer: Path, name: str) -> Path:
    parts = name.split("/") if name.startswith("@") else [name]
    return consumer / "node_modules" / Path(*parts)


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
    else:
        link.symlink_to(target, target_is_directory=True)


def _resolve(consumer: Path) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    return subprocess.run(
        [node, str(RESOLVER), str(consumer)],
        capture_output=True,
        text=True,
        check=False,
    )


def _file_inventory(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mode & 0o7777,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _tree_inventory(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        row: dict[str, object] = {"mode": metadata.st_mode & 0o7777}
        if path.is_symlink():
            row.update(kind="symlink", target=os.readlink(path))
        elif path.is_dir():
            row["kind"] = "directory"
        elif path.is_file():
            row.update(
                kind="file",
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size=metadata.st_size,
            )
        else:
            row["kind"] = "other"
        result[path.relative_to(root).as_posix()] = row
    return result


def _remove_publisher(root: Path) -> None:
    for attempt in range(5):
        try:
            shutil.rmtree(root)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def test_generator_emits_complete_hashed_package_payload_and_real_npm_pack(
    tmp_path: Path,
) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is required")
    feature = _add_cpp(_plugin(tmp_path / "publisher"))
    package = json.loads((feature / "package.json").read_text())
    manifest = json.loads(
        (feature / ".supernote-generated/package-manifest.json").read_text()
    )
    registration = json.loads(
        (feature / ".supernote-generated/android/registration.json").read_text()
    )

    assert package["supernoteNativeModule"]["protocol"] == "2.0"
    assert manifest["featureId"].startswith("supernote:feature:")
    assert registration["featureId"] == manifest["featureId"]
    payload = {entry["path"]: entry["sha256"] for entry in manifest["payload"]}
    for relative in (
        "package.json",
        ".supernote-module.json",
        "CMakeLists.txt",
        "android/src/main/cpp/feature.cpp",
        ".supernote-generated/index.js",
        ".supernote-generated/index.d.ts",
        ".supernote-generated/android/feature.cpp",
        ".supernote-generated/android/internal.cpp",
        ".supernote-generated/android/internal.hpp",
        ".supernote-generated/android/CMakeLists.txt",
        ".supernote-generated/android/package_registration.cpp",
        ".supernote-generated/android/package_registration.hpp",
        ".supernote-generated/android/jvm/JvmRegistration.java",
        ".supernote-generated/android/registration.json",
    ):
        assert payload[relative] == hashlib.sha256((feature / relative).read_bytes()).hexdigest()
    assert all(not Path(item).is_absolute() for item in payload)

    destination = tmp_path / "packed"
    destination.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")
    packed = subprocess.run(
        [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(destination)],
        cwd=feature,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert packed.returncode == 0, packed.stderr
    tarball = destination / json.loads(packed.stdout)[0]["filename"]
    with tarfile.open(tarball, "r:gz") as archive:
        members = {item.name for item in archive.getmembers() if item.isfile()}
    assert "package/.supernote-generated/package-manifest.json" in members
    assert "package/.supernote-generated/android/feature.cpp" in members
    assert "package/.supernote-generated/android/CMakeLists.txt" in members
    assert (
        "package/.supernote-generated/android/jvm/JvmRegistration.java"
        in members
    )
    assert "package/android/src/main/cpp/feature.cpp" in members
    assert "package/CMakeLists.txt" in members


def test_real_generated_language_families_install_and_discover(tmp_path: Path) -> None:
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        pytest.skip("Node.js and npm are required")
    features = {
        "@fixture/native": _add_generated_feature(
            _plugin(tmp_path / "n"),
            "@fixture/native",
            public_name="NativeOnly",
            namespace="com.fixture.native_only",
            starters=(StarterFamily.NATIVE,),
        ),
        "@fixture/jvm": _add_generated_feature(
            _plugin(tmp_path / "j"),
            "@fixture/jvm",
            public_name="JvmOnly",
            namespace="com.fixture.jvm_only",
            starters=(StarterFamily.JVM,),
        ),
        "@fixture/mixed": _add_generated_feature(
            _plugin(tmp_path / "m"),
            "@fixture/mixed",
            public_name="Mixed",
            namespace="com.fixture.mixed",
            starters=(StarterFamily.NATIVE, StarterFamily.JVM),
        ),
    }
    expected_roots = {
        "@fixture/native": (["android/src/main/cpp"], []),
        "@fixture/jvm": ([], ["android/src/main/java"]),
        "@fixture/mixed": (
            ["android/src/main/cpp"],
            ["android/src/main/java"],
        ),
    }
    tarballs = tmp_path / "family-tarballs"
    tarballs.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "family-npm-cache")

    def pack(root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(tarballs)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        return tarballs / json.loads(result.stdout)[0]["filename"]

    packed = {name: pack(root) for name, root in features.items()}
    packed["@supernote/runtime"] = pack(RUNTIME_PACKAGE)
    for publisher in (
        tmp_path / "n",
        tmp_path / "j",
        tmp_path / "m",
    ):
        _remove_publisher(publisher)
    consumer = tmp_path / "family-consumer"
    vendor = consumer / "vendor"
    vendor.mkdir(parents=True)
    dependencies = {}
    for name, tarball in packed.items():
        target = vendor / tarball.name
        shutil.copy2(tarball, target)
        dependencies[name] = f"file:vendor/{target.name}"
    (consumer / "package.json").write_text(
        json.dumps({"name": "family-consumer", "private": True, "dependencies": dependencies})
        + "\n"
    )
    installed = subprocess.run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert installed.returncode == 0, installed.stderr
    installed_roots = {
        name: _dependency_root(consumer, name)
        for name in (*features, "@supernote/runtime")
    }
    before_compose = {
        name: {
            path.relative_to(root).as_posix(): (
                path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in root.rglob("*")
            if path.is_file()
        }
        for name, root in installed_roots.items()
    }
    probe = subprocess.run(
        [
            node,
            "-e",
            "const d=require('@supernote/runtime').discover(process.cwd());process.stdout.write(JSON.stringify(d));",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    discovered = json.loads(probe.stdout)
    assert {item["name"] for item in discovered["modules"]} == set(features)
    assert all(Path(item["root"]).is_relative_to(consumer) for item in discovered["modules"])
    for name, (native, jvm) in expected_roots.items():
        module_root = _dependency_root(consumer, name)
        registration = json.loads(
            (
                module_root / ".supernote-generated/android/registration.json"
            ).read_text()
        )
        assert registration["nativeSourceDirs"] == native
        assert registration["jvmSourceDirs"] == jvm
        if jvm:
            assert registration["jvmRegistrationSources"][-1] == (
                ".supernote-generated/android/jvm/KspAdapters.kt"
            )
            assert (
                '#include "internal.hpp"'
                in (
                    module_root / ".supernote-generated/android/jvm_feature.cpp"
                ).read_text()
            )
        suffix = registration["cmakeTarget"].removeprefix("supernote_feature_")
        assert registration["installers"]["native"]["symbol"] == (
            f"supernote_package_install_native_{suffix}"
        )
        assert (registration["installers"]["jvm"] is not None) == bool(jvm)

    composition = consumer / "android/build/supernote-composition"
    composed = subprocess.run(
        [
            node,
            str(_dependency_root(consumer, "@supernote/runtime") / "compose-packages.js"),
            str(consumer),
            str(composition),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert composed.returncode == 0, composed.stderr
    summary = json.loads(composed.stdout)
    assert summary["moduleCount"] == 3
    assert summary["featureIds"] == [
        item["featureId"] for item in discovered["modules"]
    ]
    bindings = (composition / "plugin_bindings.cpp").read_text()
    registry = (composition / "feature_registry.cpp").read_text()
    for item in discovered["modules"]:
        assert item["featureId"] in registry
        assert item["installers"]["native"]["symbol"] in bindings
        if item["installers"]["jvm"] is not None:
            assert item["installers"]["jvm"]["symbol"] in bindings
    after_compose = {
        name: {
            path.relative_to(root).as_posix(): (
                path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in root.rglob("*")
            if path.is_file()
        }
        for name, root in installed_roots.items()
    }
    assert after_compose == before_compose


def test_resolver_rejects_r4_jvm_package_without_ksp_adapter(tmp_path: Path) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/jvm": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    module = _dependency_root(consumer, "@fixture/jvm")
    generated = _add_generated_feature(
        _plugin(tmp_path / "publisher"),
        "@fixture/jvm",
        public_name="JvmOnly",
        namespace="com.fixture.jvm_only",
        starters=(StarterFamily.JVM,),
    )
    shutil.copytree(generated, module)
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))

    adapter_relative = ".supernote-generated/android/jvm/KspAdapters.kt"
    registration_path = module / ".supernote-generated/android/registration.json"
    registration = json.loads(registration_path.read_text())
    registration["jvmRegistrationSources"] = [
        ".supernote-generated/android/jvm/JvmRegistration.java"
    ]
    registration_path.write_text(json.dumps(registration) + "\n")
    manifest_path = module / ".supernote-generated/package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["payload"] = [
        row for row in manifest["payload"] if row["path"] != adapter_relative
    ]
    for row in manifest["payload"]:
        if row["path"] == ".supernote-generated/android/registration.json":
            row["sha256"] = hashlib.sha256(registration_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest) + "\n")
    (module / adapter_relative).unlink()

    result = _resolve(consumer)
    assert result.returncode != 0
    assert "SNMG_MODULE_REGISTRATION_INVALID" in result.stderr
    assert "required binding inventory disagrees" in result.stderr


def test_public_update_after_last_source_removed_packs_installs_and_discovers(
    tmp_path: Path,
) -> None:
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        pytest.skip("Node.js and npm are required")
    publisher = _plugin(tmp_path / "p")
    feature = _add_cpp(publisher)
    (feature / "android/src/main/cpp/feature.cpp").unlink()
    assert _public_cli(["update"], cwd=publisher) == 0
    registration = json.loads(
        (feature / ".supernote-generated/android/registration.json").read_text()
    )
    assert registration["nativeSourceDirs"] == []
    manifest = json.loads(
        (feature / ".supernote-generated/package-manifest.json").read_text()
    )
    assert all(
        not item["path"].startswith("android/src/main/cpp/")
        for item in manifest["payload"]
    )

    tarballs = tmp_path / "tarballs-empty"
    tarballs.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache-empty")

    def pack(root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(tarballs)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        return tarballs / json.loads(result.stdout)[0]["filename"]

    module_tarball = pack(feature)
    runtime_tarball = pack(RUNTIME_PACKAGE)
    _remove_publisher(publisher)
    consumer = tmp_path / "consumer-empty"
    vendor = consumer / "vendor"
    vendor.mkdir(parents=True)
    for tarball in (module_tarball, runtime_tarball):
        shutil.copy2(tarball, vendor / tarball.name)
    (consumer / "package.json").write_text(
        json.dumps(
            {
                "name": "consumer-empty-family",
                "private": True,
                "dependencies": {
                    "@fixture/alpha": f"file:vendor/{module_tarball.name}",
                    "@supernote/runtime": f"file:vendor/{runtime_tarball.name}",
                },
            }
        )
        + "\n"
    )
    installed = subprocess.run(
        [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert installed.returncode == 0, installed.stderr
    probe = subprocess.run(
        [
            node,
            "-e",
            "const d=require('@supernote/runtime').discover(process.cwd());process.stdout.write(JSON.stringify(d));",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    discovered = json.loads(probe.stdout)
    assert [item["name"] for item in discovered["modules"]] == ["@fixture/alpha"]
    installed_registration = Path(discovered["modules"][0]["registration"])
    assert installed_registration.is_relative_to(consumer)


def test_runtime_resolver_source_is_the_packaged_source() -> None:
    assert RESOLVER.read_bytes() == RUNTIME_RESOLVER.read_bytes()


def test_runtime_native_payload_matches_generated_sources() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "ci/materialize_runtime_npm_payload.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_runtime_readme_documents_normal_npm_and_yarn_workflow() -> None:
    readme = (RUNTIME_PACKAGE / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "npm install @supernote/runtime my-file-module" in readme
    assert "yarn add @supernote/runtime my-file-module" in readme
    assert "`nodeLinker: node-modules`" in readme
    assert "./buildPlugin.sh" in readme
    assert ".\\buildPlugin.ps1" in readme
    assert "Do not apply the runtime Gradle helper manually" in normalized
    assert "do not run `sn-module-gen` in the consumer" in normalized
    assert "Android consumers apply" not in readme
    assert (
        "supernote_link_modules(<runtime-target> <compile-contract-target> "
        "<validated-inventory-file>)"
    ) in readme
    assert "<consumer-root>" not in readme


def test_runtime_npm_tarball_contains_consumer_integration_files(tmp_path: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is required")
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")
    result = subprocess.run(
        [
            npm,
            "pack",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            str(tmp_path),
        ],
        cwd=RUNTIME_PACKAGE,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    tarball = tmp_path / json.loads(result.stdout)[0]["filename"]
    with tarfile.open(tarball, "r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
    expected = {
        "package/android/build.gradle",
        "package/android/src/main/AndroidManifest.xml",
        "package/android/src/main/java/supernote/generated/runtime/SupernoteModule.kt",
        "package/jvm/supernote/generated/runtime/SupernoteCoroutineBridge.kt",
        "package/jvm/supernote/generated/runtime/SupernoteConversionBudget.kt",
        "package/cmake/CMakeLists.txt",
        "package/cmake/SupernoteModules.cmake",
        "package/compose-packages.js",
        "package/consumer-rules.pro",
        "package/gradle/supernote-modules.gradle",
        "package/react-native.config.js",
        "package/runtime-manifest.json",
        "package/native/include/supernote/conversion.hpp",
        "package/native/include/supernote/cpp_objects.hpp",
        "package/native/include/supernote/runtime.hpp",
        "package/native/src/runtime_services.hpp",
        "package/native/src/runtime_services.cpp",
        "package/native/src/runtime_bootstrap.cpp",
        "package/native/src/runtime_registration_bridge.c",
        "package/resolve-packages.js",
    }
    expected.update(
        "package/jvm/supernote/generated/annotations/" + name + ".java"
        for name in (
            "SupernoteConstructor",
            "SupernotePluginAsync",
            "SupernotePluginExport",
            "SupernotePluginInternal",
            "SupernotePluginObject",
            "SupernotePluginValue",
        )
    )
    assert expected <= members


def test_runtime_declares_one_stable_runtime_react_package() -> None:
    package = json.loads((RUNTIME_PACKAGE / "package.json").read_text())
    assert "android" in package["files"]
    assert "react-native.config.js" in package["files"]

    config = (RUNTIME_PACKAGE / "react-native.config.js").read_text()
    assert config.count("new SupernoteModulePackage()") == 1
    assert (
        "import supernote.generated.runtime.SupernoteModulePackage;" in config
    )
    assert "sourceDir: './android'" in config

    entrypoint = (
        RUNTIME_PACKAGE
        / "android/src/main/java/supernote/generated/runtime/SupernoteModule.kt"
    ).read_text()
    assert entrypoint.count("class SupernoteModulePackage") == 1
    assert ": ReactPackage" in entrypoint
    assert "List<ViewManager<*, *>>" in entrypoint
    assert "listOf(SupernoteModule(reactContext))" in entrypoint

    index = (RUNTIME_PACKAGE / "index.js").read_text()
    assert index == "'use strict';\n\nmodule.exports = require('./resolve-packages.js');\n"

    gradle = (RUNTIME_PACKAGE / "android/build.gradle").read_text()
    assert "ignoreExitValue = true" in gradle
    assert "supernoteCompositionExec.standardError.asText.get().trim()" in gradle
    assert "throw new GradleException" in gradle


def test_runtime_resolver_rejects_tampered_native_payload(tmp_path: Path) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@supernote/runtime": "0.1.0"},
    )
    runtime = _dependency_root(consumer, "@supernote/runtime")
    _write_runtime(runtime)
    source = runtime / "native/src/runtime_services.cpp"
    source.write_bytes(source.read_bytes() + b"\n// tampered\n")

    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_RUNTIME_PAYLOAD_STALE" in result.stderr


def test_runtime_resolver_requires_hashed_gradle_helper_without_mutation(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@supernote/runtime": "0.1.0"},
    )
    runtime = _dependency_root(consumer, "@supernote/runtime")
    _write_runtime(runtime)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["payload"] = [
        row
        for row in manifest["payload"]
        if row["path"] != "gradle/supernote-modules.gradle"
    ]
    manifest_path.write_text(json.dumps(manifest) + "\n")
    before = _file_inventory(runtime)

    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_RUNTIME_PAYLOAD_INVALID" in result.stderr
    assert "gradle/supernote-modules.gradle" in result.stderr
    assert _file_inventory(runtime) == before


def test_runtime_resolver_rejects_stale_gradle_helper_without_mutation(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@supernote/runtime": "0.1.0"},
    )
    runtime = _dependency_root(consumer, "@supernote/runtime")
    _write_runtime(runtime)
    helper = runtime / "gradle/supernote-modules.gradle"
    helper.write_bytes(helper.read_bytes() + b"\n// stale\n")
    before = _file_inventory(runtime)

    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_RUNTIME_PAYLOAD_STALE" in result.stderr
    assert "gradle/supernote-modules.gradle" in result.stderr
    assert _file_inventory(runtime) == before


def test_resolver_rejects_unlisted_generated_jvm_sibling_without_mutation(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/jvm": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    generated = _add_generated_feature(
        _plugin(tmp_path / "publisher"),
        "@fixture/jvm",
        public_name="JvmOnly",
        namespace="com.fixture.jvm_only",
        starters=(StarterFamily.JVM,),
    )
    module = _dependency_root(consumer, "@fixture/jvm")
    shutil.copytree(generated, module)
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    extra = module / ".supernote-generated/android/jvm/Unlisted.kt"
    extra.write_text("package supernote.generated.bindings\n\nobject Unlisted\n")
    before = _file_inventory(module)

    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_MODULE_REGISTRATION_INVALID" in result.stderr
    assert "selects unhashed source file" in result.stderr
    assert "Unlisted.kt" in result.stderr
    assert _file_inventory(module) == before


@pytest.mark.parametrize("unlisted_name", ["Unlisted.kt", "Unlisted.java"])
def test_resolver_rejects_unlisted_physical_jvm_sibling_of_contained_symlink(
    tmp_path: Path,
    unlisted_name: str,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/jvm": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    generated = _add_generated_feature(
        _plugin(tmp_path / "publisher"),
        "@fixture/jvm",
        public_name="JvmOnly",
        namespace="com.fixture.jvm_only",
        starters=(StarterFamily.JVM,),
    )
    module = _dependency_root(consumer, "@fixture/jvm")
    shutil.copytree(generated, module)
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))

    registration = module / ".supernote-generated/android/jvm/JvmRegistration.java"
    alternate = module / "alternate/JvmRegistration.java"
    alternate.parent.mkdir()
    alternate.write_bytes(registration.read_bytes())
    registration.unlink()
    try:
        registration.symlink_to(os.path.relpath(alternate, registration.parent))
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    valid_symlink = _resolve(consumer)
    assert valid_symlink.returncode == 0, valid_symlink.stderr

    (alternate.parent / unlisted_name).write_text(
        "package supernote.generated.bindings\n\n"
        + ("object Unlisted\n" if unlisted_name.endswith(".kt") else "final class Unlisted {}\n")
    )
    before = _tree_inventory(consumer)

    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_MODULE_REGISTRATION_INVALID" in result.stderr
    assert "selects unhashed source file" in result.stderr
    assert unlisted_name in result.stderr
    assert _tree_inventory(consumer) == before


@pytest.mark.parametrize("unlisted_name", ["Unlisted.kt", "Unlisted.java"])
def test_resolver_rejects_unlisted_jvm_alias_to_contained_registration_target(
    tmp_path: Path,
    unlisted_name: str,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/jvm": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    generated = _add_generated_feature(
        _plugin(tmp_path / "publisher"),
        "@fixture/jvm",
        public_name="JvmOnly",
        namespace="com.fixture.jvm_only",
        starters=(StarterFamily.JVM,),
    )
    module = _dependency_root(consumer, "@fixture/jvm")
    module.parent.mkdir(parents=True)
    _link_directory(module, generated)
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))

    registration = module / ".supernote-generated/android/jvm/JvmRegistration.java"
    alternate = module / "alternate/JvmRegistration.java"
    alternate.parent.mkdir()
    alternate.write_bytes(registration.read_bytes())
    registration.unlink()
    try:
        registration.symlink_to(os.path.relpath(alternate, registration.parent))
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    valid_linked_symlink = _resolve(consumer)
    assert valid_linked_symlink.returncode == 0, valid_linked_symlink.stderr

    (alternate.parent / unlisted_name).symlink_to(alternate.name)
    before = {
        "consumer": _tree_inventory(consumer),
        "workspace_package": _tree_inventory(generated),
    }
    result = _resolve(consumer)

    assert result.returncode == 1
    assert "SNMG_MODULE_REGISTRATION_INVALID" in result.stderr
    assert "selects unhashed source file" in result.stderr
    assert unlisted_name in result.stderr
    assert {
        "consumer": _tree_inventory(consumer),
        "workspace_package": _tree_inventory(generated),
    } == before


def test_real_npm_tarballs_install_into_unrelated_zero_one_and_two_module_consumers(
    tmp_path: Path,
) -> None:
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        pytest.skip("Node.js and npm are required")
    publisher = _plugin(tmp_path / "publisher")
    alpha = _add_cpp(publisher)
    beta = _add_cpp(
        publisher,
        "@fixture/beta",
        public_name="Beta",
        namespace="com.fixture.beta",
    )
    tarballs = tmp_path / "tarballs"
    tarballs.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")

    def pack(root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(tarballs)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        return tarballs / json.loads(result.stdout)[0]["filename"]

    packed = {
        "@fixture/alpha": pack(alpha),
        "@fixture/beta": pack(beta),
        "@supernote/runtime": pack(RUNTIME_PACKAGE),
    }
    _remove_publisher(publisher)

    for count, selected in enumerate(((), ("@fixture/alpha",), ("@fixture/alpha", "@fixture/beta"))):
        consumer = tmp_path / f"consumer-{count}"
        vendor = consumer / "vendor"
        vendor.mkdir(parents=True)
        dependencies = {}
        if selected:
            runtime_target = vendor / packed["@supernote/runtime"].name
            shutil.copy2(packed["@supernote/runtime"], runtime_target)
            dependencies["@supernote/runtime"] = f"file:vendor/{runtime_target.name}"
        for name in selected:
            target = vendor / packed[name].name
            shutil.copy2(packed[name], target)
            dependencies[name] = f"file:vendor/{target.name}"
        (consumer / "package.json").write_text(
            json.dumps({"name": f"unrelated-{count}", "private": True, "dependencies": dependencies}) + "\n"
        )
        install = subprocess.run(
            [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=consumer,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert install.returncode == 0, install.stderr
        if selected:
            probe = subprocess.run(
                [
                    node,
                    "-e",
                    "const d=require('@supernote/runtime').discover(process.cwd());process.stdout.write(JSON.stringify(d));",
                ],
                cwd=consumer,
                capture_output=True,
                text=True,
                check=False,
            )
            assert probe.returncode == 0, probe.stderr
            discovered = json.loads(probe.stdout)
            assert [item["name"] for item in discovered["modules"]] == list(selected)
            encoded = json.dumps(discovered)
            assert str(tmp_path / "publisher") not in encoded
            assert "sn-module-gen" not in encoded
            assert "build/" not in encoded
        else:
            assert not (consumer / "node_modules/@supernote").exists()


def test_cmake_incrementally_reconfigures_zero_add_remove_reinstall_and_no_change(
    tmp_path: Path,
) -> None:
    cmake = shutil.which("cmake")
    ninja = shutil.which("ninja")
    node = shutil.which("node")
    if cmake is None or ninja is None or node is None:
        pytest.skip("CMake, Ninja, and Node.js are required")

    consumer = _consumer(
        tmp_path / "incremental-consumer",
        {"@supernote/runtime": "0.1.0"},
    )
    runtime = _dependency_root(consumer, "@supernote/runtime")
    _write_runtime(runtime)
    module_source = tmp_path / "module-source"
    feature_id = "supernote:feature:incremental"
    _write_module(
        module_source,
        "@fixture/incremental",
        feature_id=feature_id,
        public_name="Incremental",
        namespace="com.fixture.incremental",
    )
    registration = json.loads(
        (module_source / ".supernote-generated/android/registration.json").read_text()
    )
    symbol = registration["installers"]["native"]["symbol"]
    target = registration["cmakeTarget"]
    registration_source = (
        module_source / ".supernote-generated/android/package_registration.cpp"
    )
    registration_source.write_text(
        f'extern "C" void {symbol}(void *, void *, const void *) {{}}\n',
        encoding="utf-8",
    )
    package_manifest = module_source / ".supernote-generated/package-manifest.json"
    manifest = json.loads(package_manifest.read_text())
    for entry in manifest["payload"]:
        if entry["path"] == ".supernote-generated/android/package_registration.cpp":
            entry["sha256"] = hashlib.sha256(registration_source.read_bytes()).hexdigest()
    package_manifest.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    composition = consumer / "composition"
    inventory = composition / "inventory.json"
    callsite = consumer / "main.cpp"
    build = consumer / "build"
    executable = build / ("graph_probe.exe" if os.name == "nt" else "graph_probe")

    def write_callsite(installed: bool) -> None:
        if installed:
            callsite.write_text(
                f'extern "C" void {symbol}(void *, void *, const void *);\n'
                f"int main() {{ {symbol}(nullptr, nullptr, nullptr); return 0; }}\n",
                encoding="utf-8",
            )
        else:
            callsite.write_text("int main() { return 0; }\n", encoding="utf-8")

    def compose() -> None:
        result = subprocess.run(
            [node, str(runtime / "compose-packages.js"), str(consumer), str(composition)],
            cwd=consumer,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def build_and_run() -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [cmake, "--build", str(build), "--verbose"],
            cwd=consumer,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        executed = subprocess.run(
            [str(executable)],
            cwd=build,
            capture_output=True,
            text=True,
            check=False,
        )
        assert executed.returncode == 0, executed.stderr or executed.stdout
        return result

    write_callsite(False)
    compose()
    (consumer / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.24)\n"
        "cmake_policy(SET CMP0131 NEW)\n"
        "project(supernote_incremental_probe LANGUAGES CXX)\n"
        "add_library(compile_contract INTERFACE)\n"
        "add_executable(graph_probe main.cpp)\n"
        f'include("{(runtime / "cmake/SupernoteModules.cmake").as_posix()}")\n'
        f'supernote_link_modules(graph_probe compile_contract "{inventory.as_posix()}")\n',
        encoding="utf-8",
    )
    configured = subprocess.run(
        [cmake, "-S", str(consumer), "-B", str(build), "-G", "Ninja"],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr or configured.stdout
    build_and_run()
    assert target not in (build / "build.ninja").read_text(encoding="utf-8")

    installed_module = _dependency_root(consumer, "@fixture/incremental")
    package_path = consumer / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"]["@fixture/incremental"] = "1.2.3"
    package_path.write_text(json.dumps(package) + "\n", encoding="utf-8")
    shutil.copytree(module_source, installed_module)
    write_callsite(True)
    compose()
    build_and_run()
    assert target in (build / "build.ninja").read_text(encoding="utf-8")

    package["dependencies"].pop("@fixture/incremental")
    package_path.write_text(json.dumps(package) + "\n", encoding="utf-8")
    shutil.rmtree(installed_module)
    write_callsite(False)
    compose()
    build_and_run()
    assert target not in (build / "build.ninja").read_text(encoding="utf-8")

    package["dependencies"]["@fixture/incremental"] = "1.2.3"
    package_path.write_text(json.dumps(package) + "\n", encoding="utf-8")
    shutil.copytree(module_source, installed_module)
    write_callsite(True)
    compose()
    build_and_run()
    ninja_after_reinstall = (build / "build.ninja").read_bytes()
    executable_after_reinstall = hashlib.sha256(executable.read_bytes()).hexdigest()
    assert target.encode() in ninja_after_reinstall

    compose()
    no_change = build_and_run()
    assert (build / "build.ninja").read_bytes() == ninja_after_reinstall
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == executable_after_reinstall
    assert "no work to do" in (no_change.stdout + no_change.stderr).lower()


def test_direct_discovery_supports_zero_one_two_scoped_hoisted_and_linked(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    consumer = _consumer(
        workspace / "packages/app",
        {
            "@fixture/alpha": "file:../../publishers/alpha",
            "@fixture/beta": "1.2.3",
            "@supernote/runtime": "0.1.0",
            "ordinary-hidden": "1.0.0",
        },
    )
    modules = workspace / "node_modules"
    alpha_source = workspace / "publishers/alpha"
    _write_module(
        alpha_source,
        "@fixture/alpha",
        feature_id="supernote:feature:alpha",
        public_name="Alpha",
        namespace="com.fixture.alpha",
    )
    alpha_link = modules / "@fixture/alpha"
    alpha_link.parent.mkdir(parents=True)
    _link_directory(alpha_link, alpha_source)
    _write_module(
        modules / "@fixture/beta",
        "@fixture/beta",
        feature_id="supernote:feature:beta",
        public_name="Beta",
        namespace="com.fixture.beta",
    )
    beta_root = modules / "@fixture/beta"
    beta_package_path = beta_root / "package.json"
    beta_package = json.loads(beta_package_path.read_text())
    beta_package.pop("main")
    beta_package["exports"] = {".": "./.supernote-generated/index.js"}
    beta_package_path.write_text(json.dumps(beta_package) + "\n")
    beta_distribution_path = beta_root / ".supernote-generated/package-manifest.json"
    beta_distribution = json.loads(beta_distribution_path.read_text())
    for entry in beta_distribution["payload"]:
        if entry["path"] == "package.json":
            entry["sha256"] = hashlib.sha256(beta_package_path.read_bytes()).hexdigest()
    beta_distribution_path.write_text(json.dumps(beta_distribution) + "\n")
    _write_runtime(modules / "@supernote/runtime")
    unrelated = modules / "ordinary-hidden"
    unrelated.mkdir(parents=True)
    (unrelated / "package.json").write_text(
        json.dumps({"name": "ordinary-hidden", "version": "1.0.0", "exports": {".": "./missing.js"}}) + "\n"
    )

    result = _resolve(consumer)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert [item["name"] for item in value["modules"]] == [
        "@fixture/alpha",
        "@fixture/beta",
    ]
    assert value["ignored"] == ["ordinary-hidden"]

    empty = _consumer(tmp_path / "empty", {})
    empty_result = _resolve(empty)
    assert empty_result.returncode == 0, empty_result.stderr
    assert json.loads(empty_result.stdout)["modules"] == []


def test_resolver_ignores_dev_transitive_and_undeclared_leftovers_and_reports_missing(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@supernote/runtime": "0.1.0", "ordinary": "1.0.0"},
    )
    package = json.loads((consumer / "package.json").read_text())
    package["devDependencies"] = {"@fixture/dev-only": "1.2.3"}
    (consumer / "package.json").write_text(json.dumps(package) + "\n")
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    ordinary = _dependency_root(consumer, "ordinary")
    ordinary.mkdir(parents=True)
    (ordinary / "package.json").write_text(
        json.dumps({"name": "ordinary", "version": "1.0.0"}) + "\n"
    )
    _write_module(
        _dependency_root(consumer, "@fixture/dev-only"),
        "@fixture/dev-only",
        feature_id="supernote:feature:dev",
        public_name="DevOnly",
        namespace="com.fixture.dev",
    )
    _write_module(
        ordinary / "node_modules/@fixture/transitive",
        "@fixture/transitive",
        feature_id="supernote:feature:transitive",
        public_name="Transitive",
        namespace="com.fixture.transitive",
    )
    _write_module(
        _dependency_root(consumer, "@fixture/leftover"),
        "@fixture/leftover",
        feature_id="supernote:feature:leftover",
        public_name="Leftover",
        namespace="com.fixture.leftover",
    )

    result = _resolve(consumer)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["modules"] == []
    assert value["ignored"] == ["ordinary"]

    package["dependencies"]["@fixture/missing"] = "1.2.3"
    (consumer / "package.json").write_text(json.dumps(package) + "\n")
    missing = _resolve(consumer)
    assert "SNMG_DEPENDENCY_MISSING" in missing.stderr


def test_real_yarn_classic_installs_copied_tarballs_without_generator(tmp_path: Path) -> None:
    yarn = shutil.which("yarn")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if yarn is None or npm is None or node is None:
        pytest.skip("Yarn classic, npm, and Node.js are required")
    version = subprocess.run(
        [yarn, "--version"], capture_output=True, text=True, check=False
    )
    if not version.stdout.startswith("1."):
        pytest.skip("this control is specifically for Yarn classic")

    publisher = _plugin(tmp_path / "publisher")
    feature = _add_cpp(publisher)
    tarballs = tmp_path / "tarballs"
    tarballs.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")

    def pack(root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(tarballs)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        return tarballs / json.loads(result.stdout)[0]["filename"]

    module_tarball = pack(feature)
    runtime_tarball = pack(RUNTIME_PACKAGE)
    _remove_publisher(publisher)
    consumer = tmp_path / "yarn-consumer"
    vendor = consumer / "vendor"
    vendor.mkdir(parents=True)
    shutil.copy2(module_tarball, vendor / module_tarball.name)
    shutil.copy2(runtime_tarball, vendor / runtime_tarball.name)
    (consumer / "package.json").write_text(
        json.dumps(
            {
                "name": "unrelated-yarn-consumer",
                "private": True,
                "dependencies": {
                    "@fixture/alpha": f"file:vendor/{module_tarball.name}",
                    "@supernote/runtime": f"file:vendor/{runtime_tarball.name}",
                },
            }
        )
        + "\n"
    )
    install = subprocess.run(
        [
            yarn,
            "install",
            "--ignore-scripts",
            "--non-interactive",
            "--cache-folder",
            str(tmp_path / "yarn-cache"),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    probe = subprocess.run(
        [
            node,
            "-e",
            "const d=require('@supernote/runtime').discover(process.cwd());process.stdout.write(JSON.stringify(d));",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert [item["name"] for item in json.loads(probe.stdout)["modules"]] == [
        "@fixture/alpha"
    ]


@pytest.mark.skipif(
    os.environ.get("SNMG_RUN_MODERN_YARN") != "1",
    reason="set SNMG_RUN_MODERN_YARN=1 for the network-backed Yarn 4 host control",
)
def test_real_yarn_four_node_modules_consumer(tmp_path: Path) -> None:
    corepack = shutil.which("corepack")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if corepack is None or npm is None or node is None:
        pytest.skip("Corepack, npm, and Node.js are required")
    publisher = _plugin(tmp_path / "publisher")
    feature = _add_cpp(publisher)
    tarballs = tmp_path / "tarballs"
    tarballs.mkdir()
    environment = dict(os.environ)
    environment["npm_config_cache"] = str(tmp_path / "npm-cache")
    environment["COREPACK_HOME"] = os.environ.get(
        "SNMG_COREPACK_HOME", str(tmp_path / "corepack")
    )

    def pack(root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--ignore-scripts", "--json", "--pack-destination", str(tarballs)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        return tarballs / json.loads(result.stdout)[0]["filename"]

    module_tarball = pack(feature)
    runtime_tarball = pack(RUNTIME_PACKAGE)
    _remove_publisher(publisher)
    consumer = tmp_path / "yarn-four-consumer"
    vendor = consumer / "vendor"
    vendor.mkdir(parents=True)
    shutil.copy2(module_tarball, vendor / module_tarball.name)
    shutil.copy2(runtime_tarball, vendor / runtime_tarball.name)
    (consumer / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    (consumer / "package.json").write_text(
        json.dumps(
            {
                "name": "unrelated-yarn-four-consumer",
                "private": True,
                "packageManager": "yarn@4.9.2",
                "dependencies": {
                    "@fixture/alpha": f"file:vendor/{module_tarball.name}",
                    "@supernote/runtime": f"file:vendor/{runtime_tarball.name}",
                },
            }
        )
        + "\n"
    )
    install = subprocess.run(
        [corepack, "yarn", "install"],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert install.returncode == 0, install.stderr
    assert (consumer / "node_modules/@fixture/alpha").is_dir()
    probe = subprocess.run(
        [
            node,
            "-e",
            "const d=require('@supernote/runtime').discover(process.cwd());process.stdout.write(JSON.stringify(d));",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert [item["name"] for item in json.loads(probe.stdout)["modules"]] == [
        "@fixture/alpha"
    ]


def test_resolver_rejects_payload_realpath_escape_even_for_valid_linked_root(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/alpha": "file:../publisher", "@supernote/runtime": "0.1.0"},
    )
    publisher = tmp_path / "publisher"
    _write_module(
        publisher,
        "@fixture/alpha",
        feature_id="supernote:feature:alpha",
        public_name="Alpha",
        namespace="com.fixture.alpha",
    )
    installed = _dependency_root(consumer, "@fixture/alpha")
    installed.parent.mkdir(parents=True)
    _link_directory(installed, publisher)
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    android = publisher / ".supernote-generated/android"
    outside = tmp_path / "outside-android"
    shutil.copytree(android, outside)
    shutil.rmtree(android)
    _link_directory(android, outside)

    result = _resolve(consumer)
    assert result.returncode == 1
    assert "SNMG_MODULE_PAYLOAD_INVALID" in result.stderr
    assert "resolves outside its package root" in result.stderr


def test_resolver_reports_missing_incompatible_duplicate_and_case_conflicts(
    tmp_path: Path,
) -> None:
    consumer = _consumer(tmp_path / "consumer", {"@fixture/alpha": "1.2.3"})
    _write_module(
        _dependency_root(consumer, "@fixture/alpha"),
        "@fixture/alpha",
        feature_id="supernote:feature:alpha",
        public_name="Alpha",
        namespace="com.fixture.alpha",
    )
    missing_runtime = _resolve(consumer)
    assert "SNMG_RUNTIME_MISSING" in missing_runtime.stderr

    consumer_package = json.loads((consumer / "package.json").read_text())
    consumer_package["dependencies"]["@supernote/runtime"] = "0.1.0"
    (consumer / "package.json").write_text(json.dumps(consumer_package) + "\n")
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"), protocol="1.0")
    incompatible = _resolve(consumer)
    assert "SNMG_RUNTIME_INCOMPATIBLE" in incompatible.stderr

    shutil.rmtree(_dependency_root(consumer, "@supernote/runtime"))
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    consumer_package["dependencies"]["@fixture/beta"] = "1.2.3"
    (consumer / "package.json").write_text(json.dumps(consumer_package) + "\n")
    _write_module(
        _dependency_root(consumer, "@fixture/beta"),
        "@fixture/beta",
        feature_id="supernote:feature:alpha",
        public_name="alpha",
        namespace="COM.FIXTURE.ALPHA",
    )
    duplicate = _resolve(consumer)
    assert "SNMG_MODULE_DUPLICATE_FEATURE" in duplicate.stderr

    beta_manifest = _dependency_root(consumer, "@fixture/beta") / ".supernote-module.json"
    authored = json.loads(beta_manifest.read_text())
    authored["feature_id"] = "supernote:feature:beta"
    beta_manifest.write_text(json.dumps(authored) + "\n")
    distribution_path = _dependency_root(consumer, "@fixture/beta") / ".supernote-generated/package-manifest.json"
    distribution = json.loads(distribution_path.read_text())
    distribution["featureId"] = "supernote:feature:beta"
    registration_path = (
        _dependency_root(consumer, "@fixture/beta")
        / ".supernote-generated/android/registration.json"
    )
    registration = json.loads(registration_path.read_text())
    registration["featureId"] = "supernote:feature:beta"
    registration_path.write_text(json.dumps(registration) + "\n")
    for entry in distribution["payload"]:
        if entry["path"] == ".supernote-module.json":
            entry["sha256"] = hashlib.sha256(beta_manifest.read_bytes()).hexdigest()
        if entry["path"] == ".supernote-generated/android/registration.json":
            entry["sha256"] = hashlib.sha256(registration_path.read_bytes()).hexdigest()
    distribution_path.write_text(json.dumps(distribution) + "\n")
    case_conflict = _resolve(consumer)
    assert "SNMG_MODULE_PUBLIC_NAME_CONFLICT" in case_conflict.stderr


def test_resolver_reports_malformed_module_multiple_runtimes_and_yarn_pnp(
    tmp_path: Path,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"broken": "1.0.0", "@supernote/runtime": "0.1.0"},
    )
    broken = _dependency_root(consumer, "broken")
    broken.mkdir(parents=True)
    (broken / "package.json").write_text(
        json.dumps(
            {
                "name": "broken",
                "version": "1.0.0",
                "supernoteNativeModule": None,
            }
        )
        + "\n"
    )
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    malformed = _resolve(consumer)
    assert "SNMG_MODULE_METADATA_INVALID" in malformed.stderr

    package = json.loads((consumer / "package.json").read_text())
    package["dependencies"].pop("broken")
    package["dependencies"]["alternate-runtime"] = "0.1.0"
    (consumer / "package.json").write_text(json.dumps(package) + "\n")
    _write_runtime(
        _dependency_root(consumer, "alternate-runtime"),
        name="alternate-runtime",
    )
    conflict = _resolve(consumer)
    assert "SNMG_RUNTIME_CONFLICT" in conflict.stderr

    pnp = _consumer(tmp_path / "pnp", {})
    (pnp / ".pnp.cjs").write_text("module.exports = {};\n")
    pnp_result = _resolve(pnp)
    assert "SNMG_YARN_PNP_UNSUPPORTED" in pnp_result.stderr


def test_resolver_reads_immutable_copied_payload_without_mutation(tmp_path: Path) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/alpha": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    module = _dependency_root(consumer, "@fixture/alpha")
    _write_module(
        module,
        "@fixture/alpha",
        feature_id="supernote:feature:alpha",
        public_name="Alpha",
        namespace="com.fixture.alpha",
    )
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    before = {
        path.relative_to(module).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in module.rglob("*")
        if path.is_file()
    }
    if os.name != "nt":
        for path in module.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        module.chmod(0o555)
    result = _resolve(consumer)
    assert result.returncode == 0, result.stderr
    after = {
        path.relative_to(module).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in module.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        ("alternate-unhashed", "SNMG_MODULE_PAYLOAD_INVALID"),
        ("alternate-hashed", "SNMG_MODULE_PAYLOAD_INVALID"),
        ("incomplete-bindings", "SNMG_MODULE_REGISTRATION_INVALID"),
        ("installer-symbol", "SNMG_MODULE_REGISTRATION_INVALID"),
        ("unhashed-source", "SNMG_MODULE_REGISTRATION_INVALID"),
    ],
)
def test_resolver_rejects_unhashed_or_inconsistent_selected_inputs_without_mutation(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    consumer = _consumer(
        tmp_path / "consumer",
        {"@fixture/alpha": "1.2.3", "@supernote/runtime": "0.1.0"},
    )
    module = _dependency_root(consumer, "@fixture/alpha")
    _write_module(
        module,
        "@fixture/alpha",
        feature_id="supernote:feature:alpha",
        public_name="Alpha",
        namespace="com.fixture.alpha",
    )
    _write_runtime(_dependency_root(consumer, "@supernote/runtime"))
    manifest_path = module / ".supernote-generated/package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    registration_path = module / ".supernote-generated/android/registration.json"
    registration = json.loads(registration_path.read_text())
    if mutation.startswith("alternate-"):
        alternate = module / "alternate-registration.json"
        alternate.write_text(json.dumps(registration) + "\n")
        manifest["registration"] = "alternate-registration.json"
        if mutation == "alternate-hashed":
            manifest["payload"].append(
                {
                    "path": "alternate-registration.json",
                    "sha256": hashlib.sha256(alternate.read_bytes()).hexdigest(),
                }
            )
        manifest_path.write_text(json.dumps(manifest) + "\n")
    elif mutation == "incomplete-bindings":
        registration["bindingSources"] = []
        registration_path.write_text(json.dumps(registration) + "\n")
        for entry in manifest["payload"]:
            if entry["path"] == ".supernote-generated/android/registration.json":
                entry["sha256"] = hashlib.sha256(registration_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest) + "\n")
    elif mutation == "installer-symbol":
        registration["installers"]["native"]["symbol"] = "untrusted_installer"
        registration_path.write_text(json.dumps(registration) + "\n")
        for entry in manifest["payload"]:
            if entry["path"] == ".supernote-generated/android/registration.json":
                entry["sha256"] = hashlib.sha256(registration_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest) + "\n")
    else:
        (module / "android/src/main/cpp/unhashed.cpp").write_text("// unhashed\n")

    before = {
        path.relative_to(module).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in module.rglob("*")
        if path.is_file()
    }
    result = _resolve(consumer)
    assert result.returncode == 1
    assert expected_code in result.stderr
    after = {
        path.relative_to(module).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in module.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_author_validate_detects_stale_copied_local_dependency_without_rewriting_it(
    tmp_path: Path,
) -> None:
    root = _plugin(tmp_path / "publisher")
    feature = _add_cpp(root)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text())
    package["dependencies"] = {
        "@fixture/alpha": "file:local_modules/@fixture/alpha",
        "@supernote/runtime": "file:runtime-package",
    }
    package_path.write_text(json.dumps(package) + "\n")
    installed = _dependency_root(root, "@fixture/alpha")
    installed.parent.mkdir(parents=True)
    shutil.copytree(feature, installed)
    runtime = _dependency_root(root, "@supernote/runtime")
    runtime.parent.mkdir(parents=True)
    shutil.copytree(RUNTIME_PACKAGE, runtime)

    assert _public_cli(["validate"], cwd=root) == 0
    package = json.loads(package_path.read_text())
    package["dependencies"]["@fixture/alpha"] = "1.2.3"
    package_path.write_text(json.dumps(package) + "\n")
    assert _public_cli(["validate"], cwd=root) == 1
    package["dependencies"]["@fixture/alpha"] = "file:local_modules/@fixture/alpha"
    package_path.write_text(json.dumps(package) + "\n")
    assert _public_cli(["validate"], cwd=root) == 0
    before = {
        path.relative_to(installed).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in installed.rglob("*")
        if path.is_file()
    }
    source = feature / "android/src/main/cpp/feature.cpp"
    source.write_text(source.read_text() + "// publisher edit\n")
    assert _public_cli(["update"], cwd=root) == 0
    assert _public_cli(["validate"], cwd=root) == 1
    after = {
        path.relative_to(installed).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in installed.rglob("*")
        if path.is_file()
    }
    assert after == before
