from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from typing import Mapping, Sequence

import pytest


REQUIRED_ENVIRONMENT = (
    "SUPERNOTE_Q5_MODULE_COMMAND",
    "SUPERNOTE_Q5_PYTHON",
    "SUPERNOTE_Q5_WHEEL",
    "SUPERNOTE_Q5_WHEEL_SHA256",
    "SUPERNOTE_Q5_SDIST",
    "SUPERNOTE_Q5_SDIST_SHA256",
    "SUPERNOTE_Q5_RUNTIME_ROOT",
    "SUPERNOTE_GRADLE_COMMAND",
)
pytestmark = pytest.mark.skipif(
    any(os.environ.get(name) is None for name in REQUIRED_ENVIRONMENT),
    reason="set the frozen Q5 installed-artifact and host-tool environment",
)

TRACE: list[dict[str, object]] = []

EXPECTED_RUNTIME_PAYLOAD = {
    "android/build.gradle",
    "android/src/main/AndroidManifest.xml",
    "android/src/main/java/supernote/generated/runtime/SupernoteModule.kt",
    "cmake/CMakeLists.txt",
    "cmake/SupernoteModules.cmake",
    "compose-packages.js",
    "consumer-rules.pro",
    "gradle/supernote-modules.gradle",
    "jvm/supernote/generated/annotations/SupernoteConstructor.java",
    "jvm/supernote/generated/annotations/SupernotePluginAsync.java",
    "jvm/supernote/generated/annotations/SupernotePluginExport.java",
    "jvm/supernote/generated/annotations/SupernotePluginInternal.java",
    "jvm/supernote/generated/annotations/SupernotePluginObject.java",
    "jvm/supernote/generated/annotations/SupernotePluginValue.java",
    "jvm/supernote/generated/runtime/SupernoteConversionBudget.kt",
    "jvm/supernote/generated/runtime/SupernoteCoroutineBridge.kt",
    "native/include/supernote/conversion.hpp",
    "native/include/supernote/cpp_objects.hpp",
    "native/include/supernote/runtime.hpp",
    "native/src/runtime_bootstrap.cpp",
    "native/src/runtime_registration_bridge.c",
    "native/src/runtime_services.cpp",
    "native/src/runtime_services.hpp",
    "react-native.config.js",
    "resolve-packages.js",
}


class _ConsumerGeneratorGuard:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.log = root / "attempts.log"
        self.bin.mkdir(parents=True)
        if os.name == "nt":
            self.command = self.bin / "sn-module-gen.cmd"
            _write(
                self.command,
                "@echo off\r\necho %*>>\"%SUPERNOTE_Q5_GENERATOR_TRAP_LOG%\"\r\nexit /b 97\r\n",
            )
        else:
            self.command = self.bin / "sn-module-gen"
            _write(
                self.command,
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SUPERNOTE_Q5_GENERATOR_TRAP_LOG\"\nexit 97\n",
            )
            self.command.chmod(0o755)

    def attempts(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        result = dict(os.environ if base is None else base)
        result.pop("PYTHONPATH", None)
        result["PATH"] = str(self.bin) + os.pathsep + result.get("PATH", "")
        result["SUPERNOTE_MODULE_COMMAND"] = str(self.command)
        result["SUPERNOTE_Q5_GENERATOR_TRAP_LOG"] = str(self.log)
        return result


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_inventory(root: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(root).as_posix(): {
            "sha256": _sha256(path),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _flush_trace(extra: Mapping[str, object] | None = None) -> None:
    destination = os.environ.get("SUPERNOTE_Q5_COMBINED_EVIDENCE")
    if destination is None:
        return
    payload: dict[str, object] = {"commands": TRACE}
    if extra:
        payload.update(extra)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(
    arguments: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    consumer_guard: _ConsumerGeneratorGuard | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    argv = [str(argument) for argument in arguments]
    attempts_before = consumer_guard.attempts() if consumer_guard else None
    selected_environment = dict(environment) if environment is not None else None
    if consumer_guard is not None:
        selected_environment = consumer_guard.environment(selected_environment)
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=selected_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    record: dict[str, object] = {
        "argv": argv,
        "cwd": str(cwd.resolve()),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if consumer_guard is not None:
        record["generator_guard"] = {
            "command": str(consumer_guard.command),
            "attempts_before": attempts_before,
            "attempts_after": consumer_guard.attempts(),
        }
    TRACE.append(record)
    _flush_trace()
    return result


def _assert_run(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr or result.stdout


def _installed_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = Path(os.environ["SUPERNOTE_Q5_MODULE_COMMAND"])
    assert command.is_absolute() and command.is_file()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return _run((command, *arguments), cwd=root, environment=environment)


def _add_feature(
    publisher: Path,
    package_name: str,
    public_name: str,
    namespace: str,
    *starters: str,
) -> None:
    arguments = [
        "add",
        package_name,
        "--javascript-name",
        public_name,
        "--android-namespace",
        namespace,
    ]
    for starter in starters:
        arguments.extend(("--starter", starter))
    arguments.append("--yes")
    _assert_run(_installed_cli(publisher, *arguments))


def _pack(package_root: Path, artifacts: Path) -> Path:
    npm = shutil.which("npm")
    assert npm is not None
    result = _run(
        (npm, "pack", "--json", "--pack-destination", artifacts),
        cwd=package_root,
    )
    _assert_run(result)
    return artifacts / json.loads(result.stdout)[0]["filename"]


def _build_prebuilt_libraries(root: Path, package: Path) -> None:
    source = root / "source"
    build = root / "build"
    output = root / "output"
    _write(
        source / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
project(q5_prebuilt LANGUAGES C)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${Q5_OUTPUT_ROOT}/lib")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${Q5_OUTPUT_ROOT}/lib")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${Q5_OUTPUT_ROOT}/bin")
add_library(q5_imported_static STATIC imported_static.c)
add_library(q5_imported_shared SHARED imported_shared.c)
""",
    )
    _write(source / "imported_static.c", "int q5_imported_static(void) { return 50; }\n")
    _write(
        source / "imported_shared.c",
        """#if defined(_WIN32)
#define Q5_EXPORT __declspec(dllexport)
#else
#define Q5_EXPORT
#endif
Q5_EXPORT int q5_imported_shared(void) { return 70; }
""",
    )
    cmake = shutil.which("cmake")
    assert cmake is not None
    configure = [
        cmake,
        "-S",
        str(source),
        "-B",
        str(build),
        f"-DQ5_OUTPUT_ROOT={output}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if shutil.which("ninja") is not None:
        configure.extend(("-G", "Ninja"))
    _assert_run(_run(configure, cwd=root))
    _assert_run(_run((cmake, "--build", build, "--verbose"), cwd=root))

    destination = package / "android/src/main/cpp/prebuilt"
    (destination / "include").mkdir(parents=True)
    _write(
        destination / "include/q5_imported_static.h",
        """#pragma once
#ifdef __cplusplus
extern "C" {
#endif
int q5_imported_static(void);
#ifdef __cplusplus
}
#endif
""",
    )
    _write(
        destination / "include/q5_imported_shared.h",
        """#pragma once
#ifdef __cplusplus
extern "C" {
#endif
int q5_imported_shared(void);
#ifdef __cplusplus
}
#endif
""",
    )
    if os.name == "nt":
        copies = {
            output / "lib/q5_imported_static.lib": destination
            / "lib/q5_imported_static.lib",
            output / "lib/q5_imported_shared.lib": destination
            / "lib/q5_imported_shared.lib",
            output / "bin/q5_imported_shared.dll": destination
            / "bin/q5_imported_shared.dll",
        }
    else:
        shared_suffix = ".dylib" if sys.platform == "darwin" else ".so"
        copies = {
            output / "lib/libq5_imported_static.a": destination
            / "lib/libq5_imported_static.a",
            output / f"lib/libq5_imported_shared{shared_suffix}": destination
            / f"lib/libq5_imported_shared{shared_suffix}",
        }
    for source_path, destination_path in copies.items():
        assert source_path.is_file()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def _write_native_matrix(feature: Path) -> None:
    cpp = feature / "android/src/main/cpp"
    _write(
        cpp / "feature.cpp",
        """#include <chrono>
#include <cstdint>
#include <thread>
#include "Counter.hpp"
#include "c23_helper.h"
#include "file_backend.hpp"
#include "q5_header_only.hpp"
#include "q5_imported_shared.h"
#include "q5_imported_static.h"
#include "q5_source_shared.hpp"

namespace supernote_feature_NativeMatrix {

std::int32_t q5_counter_value_calls = 0;

// @SupernotePluginExport
std::int32_t nativeScore(std::int32_t seed) {
  const unsigned char bytes[] = {1, 2, 3};
  return seed + q5_c23_score(bytes, 3) + file_backend_value() +
      file_backend_support_value() + q5_header_only_value() +
      q5_imported_static() + q5_source_shared() + q5_imported_shared();
}

// @SupernotePluginExport
// @SupernotePluginAsync
std::int32_t asyncNativeScore(std::int32_t seed) {
  return nativeScore(seed);
}

// @SupernotePluginExport
// @SupernotePluginAsync
std::int32_t slowNativeScore(std::int32_t seed) {
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  return nativeScore(seed);
}

}  // namespace supernote_feature_NativeMatrix
""",
    )
    _write(
        cpp / "Counter.hpp",
        """#pragma once
#include <cstdint>
#include <memory>
namespace supernote_feature_NativeMatrix {
extern std::int32_t q5_counter_value_calls;
// @SupernotePluginObject
class Counter {
 public:
  // @SupernoteConstructor
  explicit Counter(std::int32_t value) : value_(value) {}
  // @SupernotePluginExport
  std::int32_t value() const {
    ++q5_counter_value_calls;
    return value_;
  }
  // @SupernotePluginExport
  bool same(const std::shared_ptr<Counter> &other) const {
    return other.get() == this;
  }
 private:
  std::int32_t value_;
};
}  // namespace supernote_feature_NativeMatrix
""",
    )
    _write(
        cpp / "c23_helper.h",
        """#pragma once
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
int q5_c23_score(const unsigned char *bytes, size_t count);
#ifdef __cplusplus
}
#endif
""",
    )
    _write(
        cpp / "c23_helper.c",
        """#include "c23_helper.h"
int q5_c23_score(const unsigned char *bytes, size_t count) {
  int result = (int)count;
  for (size_t index = 0; index < count; ++index) result += bytes[index];
  return result;
}
""",
    )
    _write(
        cpp / "header_only/include/q5_header_only.hpp",
        "#pragma once\ninline int q5_header_only_value() { return 2; }\n",
    )
    _write(
        cpp / "file_backend/CMakeLists.txt",
        """add_subdirectory(support)
add_library(q5_file_backend STATIC file_backend.cpp)
set_target_properties(q5_file_backend PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(q5_file_backend PUBLIC include)
target_link_libraries(q5_file_backend PUBLIC q5_file_backend_support)
""",
    )
    _write(
        cpp / "file_backend/include/file_backend.hpp",
        """#pragma once
#include "file_backend_support.hpp"
int file_backend_value();
""",
    )
    _write(
        cpp / "file_backend/file_backend.cpp",
        '#include "file_backend.hpp"\nint file_backend_value() { return 41; }\n',
    )
    _write(
        cpp / "file_backend/support/CMakeLists.txt",
        """add_library(q5_file_backend_support STATIC support.cpp)
set_target_properties(q5_file_backend_support PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(q5_file_backend_support PUBLIC include)
""",
    )
    _write(
        cpp / "file_backend/support/include/file_backend_support.hpp",
        "#pragma once\nint file_backend_support_value();\n",
    )
    _write(
        cpp / "file_backend/support/support.cpp",
        "int file_backend_support_value() { return 1; }\n",
    )
    _write(
        cpp / "source_shared/CMakeLists.txt",
        """add_library(q5_source_shared SHARED source_shared.cpp)
target_include_directories(q5_source_shared PUBLIC include)
""",
    )
    _write(
        cpp / "source_shared/include/q5_source_shared.hpp",
        """#pragma once
#if defined(_WIN32)
#if defined(q5_source_shared_EXPORTS)
#define Q5_SOURCE_SHARED __declspec(dllexport)
#else
#define Q5_SOURCE_SHARED __declspec(dllimport)
#endif
#else
#define Q5_SOURCE_SHARED
#endif
Q5_SOURCE_SHARED int q5_source_shared();
""",
    )
    _write(
        cpp / "source_shared/source_shared.cpp",
        '#include "q5_source_shared.hpp"\nint q5_source_shared() { return 60; }\n',
    )
    _write(cpp / "vendor/not_selected.cpp", "#error vendor source must not compile\n")
    _write(
        feature / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0131 NEW)
if(NOT DEFINED SUPERNOTE_MODULE_TARGET OR
   NOT TARGET "${SUPERNOTE_MODULE_TARGET}")
  message(FATAL_ERROR "missing generated feature target")
endif()
set(_q5_cpp "${CMAKE_CURRENT_LIST_DIR}/android/src/main/cpp")
target_sources(${SUPERNOTE_MODULE_TARGET} PRIVATE
  "${_q5_cpp}/feature.cpp" "${_q5_cpp}/c23_helper.c")
add_library(q5_header_only INTERFACE)
target_include_directories(q5_header_only INTERFACE
  "${_q5_cpp}/header_only/include")
add_subdirectory("${_q5_cpp}/file_backend"
  "${CMAKE_CURRENT_BINARY_DIR}/file_backend" EXCLUDE_FROM_ALL)
add_subdirectory("${_q5_cpp}/source_shared"
  "${CMAKE_CURRENT_BINARY_DIR}/source_shared" EXCLUDE_FROM_ALL)
add_library(q5_imported_static STATIC IMPORTED GLOBAL)
add_library(q5_imported_shared SHARED IMPORTED GLOBAL)
if(WIN32)
  set_target_properties(q5_imported_static PROPERTIES
    IMPORTED_LOCATION "${_q5_cpp}/prebuilt/lib/q5_imported_static.lib")
  set_target_properties(q5_imported_shared PROPERTIES
    IMPORTED_LOCATION "${_q5_cpp}/prebuilt/bin/q5_imported_shared.dll"
    IMPORTED_IMPLIB "${_q5_cpp}/prebuilt/lib/q5_imported_shared.lib")
else()
  set_target_properties(q5_imported_static PROPERTIES
    IMPORTED_LOCATION "${_q5_cpp}/prebuilt/lib/libq5_imported_static.a")
  set_target_properties(q5_imported_shared PROPERTIES
    IMPORTED_LOCATION "${_q5_cpp}/prebuilt/lib/libq5_imported_shared${CMAKE_SHARED_LIBRARY_SUFFIX}")
endif()
set_target_properties(q5_imported_static PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "${_q5_cpp}/prebuilt/include")
set_target_properties(q5_imported_shared PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "${_q5_cpp}/prebuilt/include")
target_link_libraries(${SUPERNOTE_MODULE_TARGET} PUBLIC
  q5_header_only q5_file_backend q5_file_backend_support
  q5_source_shared q5_imported_static q5_imported_shared)
unset(_q5_cpp)
""",
    )


def _update_native_version(feature: Path) -> None:
    source = feature / "android/src/main/cpp/feature.cpp"
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        "}  // namespace supernote_feature_NativeMatrix\n",
        """// @SupernotePluginExport
std::int32_t nativeRevision() { return 2; }

}  // namespace supernote_feature_NativeMatrix
""",
    )
    _write(source, text)
    for relative in ("package.json", ".supernote-module.json"):
        path = feature / relative
        value = json.loads(path.read_text(encoding="utf-8"))
        if relative == "package.json":
            value["version"] = "0.1.1"
        else:
            value["package_version"] = "0.1.1"
        _write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _feature_identity(feature: Path) -> tuple[str, str, str]:
    registration = json.loads(
        (feature / ".supernote-generated/android/registration.json").read_text(
            encoding="utf-8"
        )
    )
    return (
        registration["featureId"],
        registration["cmakeTarget"],
        "supernote_package_registration_"
        + registration["featureId"].removeprefix("supernote:feature:"),
    )


def _assert_tarball_payload(tarball: Path, generated: Mapping[str, str]) -> None:
    with tarfile.open(tarball, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        for relative, expected_hash in generated.items():
            member = members[f"package/{relative}"]
            stream = archive.extractfile(member)
            assert stream is not None
            assert hashlib.sha256(stream.read()).hexdigest() == expected_hash


def _assert_runtime_tarball_payload(tarball: Path) -> dict[str, str]:
    runtime_root = Path(os.environ["SUPERNOTE_Q5_RUNTIME_ROOT"])
    with tarfile.open(tarball, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        manifest_member = members["package/runtime-manifest.json"]
        manifest_stream = archive.extractfile(manifest_member)
        assert manifest_stream is not None
        manifest = json.loads(manifest_stream.read())
        assert manifest == json.loads(
            (runtime_root / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        payload = manifest["payload"]
        assert len(payload) == len(EXPECTED_RUNTIME_PAYLOAD)
        assert all(set(entry) == {"path", "sha256"} for entry in payload)
        hashes = {entry["path"]: entry["sha256"] for entry in payload}
        assert set(hashes) == EXPECTED_RUNTIME_PAYLOAD
        for relative in sorted(EXPECTED_RUNTIME_PAYLOAD):
            expected_hash = hashlib.sha256((runtime_root / relative).read_bytes()).hexdigest()
            assert hashes[relative] == expected_hash
            member = members[f"package/{relative}"]
            stream = archive.extractfile(member)
            assert stream is not None
            assert hashlib.sha256(stream.read()).hexdigest() == expected_hash
            assert member.mode & 0o777 == 0o644
        return hashes


def _java_home(
    java: str, cwd: Path, guard: _ConsumerGeneratorGuard
) -> Path:
    result = _run(
        (java, "-XshowSettings:properties", "-version"),
        cwd=cwd,
        consumer_guard=guard,
    )
    _assert_run(result)
    line = next(row for row in result.stderr.splitlines() if "java.home =" in row)
    return Path(line.split("=", 1)[1].strip()).resolve()


def _install_consumer(
    root: Path, tarballs: Sequence[Path], guard: _ConsumerGeneratorGuard
) -> None:
    root.mkdir()
    _write(root / "package.json", json.dumps({"name": root.name, "private": True}) + "\n")
    npm = shutil.which("npm")
    assert npm is not None
    result = _run(
        (
            npm,
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            *tarballs,
        ),
        cwd=root,
        consumer_guard=guard,
    )
    _assert_run(result)


def _native_main(
    identities: Mapping[str, tuple[str, str, str]], *, revision: bool
) -> str:
    registrations = "\n".join(
        f'extern "C" const Registration *{symbol}() noexcept;'
        for _feature_id, _target, symbol in identities.values()
    )
    registration_checks = " &&\n      ".join(
        f'std::strcmp({symbol}()->feature_id, "{feature_id}") == 0'
        for feature_id, _target, symbol in identities.values()
    )
    feature_installers = "\n".join(
        "namespace supernote::generated::feature_"
        + feature_id.removeprefix("supernote:feature:")
        + " { void register_feature(facebook::jsi::Runtime &, "
        "facebook::jsi::Object &, const std::shared_ptr<"
        "supernote::runtime::FeatureSession> &); }"
        for feature_id, _target, _symbol in identities.values()
    )
    jvm_installers = "\n".join(
        "namespace supernote::generated::jvm_feature_"
        + identities[name][0].removeprefix("supernote:feature:")
        + " { void register_jvm_feature(facebook::jsi::Runtime &, "
        "facebook::jsi::Object &, const std::shared_ptr<"
        "supernote::runtime::FeatureSession> &); }"
        for name in ("kotlin", "java", "mixed")
    )
    session_rows = "\n".join(
        f"  auto {name}_session = FeatureSession::create(runtime_session, cleanup);"
        for name in identities
    )
    feature_calls = "\n".join(
        "  supernote::generated::feature_"
        + feature_id.removeprefix("supernote:feature:")
        + f"::register_feature(js, feature_registry, {name}_session);"
        for name, (feature_id, _target, _symbol) in identities.items()
    )
    jvm_calls = "\n".join(
        "  supernote::generated::jvm_feature_"
        + identities[name][0].removeprefix("supernote:feature:")
        + f"::register_jvm_feature(js, feature_registry, {name}_session);"
        for name in ("kotlin", "java", "mixed")
    )
    error_rows = "\n".join(
        "  feature_registry.getPropertyAsObject(js, "
        + json.dumps(feature_id)
        + ").setProperty(js, \"__supernoteErrorConstructor\", error_constructor);"
        for feature_id, _target, _symbol in identities.values()
    )
    revision_call = ""
    if revision:
        revision_call = """
  auto revision_function = native_exports.getPropertyAsFunction(js, "nativeRevision");
  if (revision_function.call(js).asNumber() != 2) return 18;
"""
    native_id = json.dumps(identities["native"][0])
    mixed_id = json.dumps(identities["mixed"][0])
    jvm_validation = "\n".join(
        "  { auto exports = feature_registry.getPropertyAsObject(js, "
        + json.dumps(identities[name][0])
        + "); auto function = exports.getPropertyAsFunction(js, "
        + json.dumps(
            {"kotlin": "kotlinGreeting", "java": "javaGreeting", "mixed": "mixedGreeting"}[name]
        )
        + "); auto accepts = function.getPropertyAsFunction(js, \"accepts\"); "
        "Value text(String::createFromUtf8(js, \"ok\")); Value number(1); "
        "if (!accepts.call(js, text).getBool() || accepts.call(js, number).getBool()) return 19; }"
        for name in ("kotlin", "java", "mixed")
    )
    return f"""#include <cstdint>
#include <cstring>
#include <chrono>
#include <iostream>
#include <map>
#include <mutex>
#include <thread>
#include <vector>
#include <jsi/jsi.h>
#include "runtime_services.hpp"
struct Registration {{ const char *feature_id; const char *package_name;
  const char *public_name; const char *cmake_target; }};
{registrations}
{feature_installers}
{jvm_installers}

using namespace facebook::jsi;
using namespace supernote::runtime;

namespace supernote_feature_NativeMatrix {{
extern std::int32_t q5_counter_value_calls;
}}

void install_host_builtins(Runtime &js) {{
  auto error = Function::createFromHostFunction(
      js, PropNameID::forAscii(js, "Error"), 2,
      [](Runtime &runtime, const Value &, const Value *arguments, std::size_t count) {{
        Object result(runtime);
        if (count > 0 && arguments[0].isString())
          result.setProperty(runtime, "code", arguments[0]);
        if (count > 1 && arguments[1].isString())
          result.setProperty(runtime, "message", arguments[1]);
        return Value(std::move(result));
      }});
  js.global().setProperty(js, "TypeError", error);
  js.global().setProperty(js, "RangeError", error);
  auto map_constructor = Function::createFromHostFunction(
      js, PropNameID::forAscii(js, "Map"), 0,
      [](Runtime &runtime, const Value &, const Value *, std::size_t) {{
        Object result(runtime);
        auto entries = std::make_shared<std::map<std::string, Value>>();
        result.setProperty(runtime, "set", Function::createFromHostFunction(
            runtime, PropNameID::forAscii(runtime, "set"), 2,
            [entries](Runtime &, const Value &, const Value *arguments, std::size_t count) {{
              if (count != 2 || !arguments[0].isString()) return Value::undefined();
              (*entries)[arguments[0].string_value()] = arguments[1];
              return Value::undefined();
            }}));
        result.setProperty(runtime, "get", Function::createFromHostFunction(
            runtime, PropNameID::forAscii(runtime, "get"), 1,
            [entries](Runtime &, const Value &, const Value *arguments, std::size_t count) {{
              if (count != 1 || !arguments[0].isString()) return Value::undefined();
              const auto found = entries->find(arguments[0].string_value());
              return found == entries->end() ? Value::undefined() : found->second;
            }}));
        result.setProperty(runtime, "delete", Function::createFromHostFunction(
            runtime, PropNameID::forAscii(runtime, "delete"), 1,
            [entries](Runtime &, const Value &, const Value *arguments, std::size_t count) {{
              return Value(count == 1 && arguments[0].isString() &&
                  entries->erase(arguments[0].string_value()) == 1);
            }}));
        return Value(std::move(result));
      }});
  js.global().setProperty(js, "Map", std::move(map_constructor));
  auto promise_constructor = Function::createFromHostFunction(
      js, PropNameID::forAscii(js, "Promise"), 1,
      [](Runtime &runtime, const Value &, const Value *arguments, std::size_t count) {{
        if (count != 1 || !arguments[0].isObject()) return Value::undefined();
        Object promise(runtime);
        auto resolve = Function::createFromHostFunction(
            runtime, PropNameID::forAscii(runtime, "resolve"), 1,
            [promise](Runtime &runtime, const Value &, const Value *values, std::size_t size) mutable {{
              promise.setProperty(runtime, "settled", String::createFromAscii(runtime, "resolved"));
              if (size == 1) promise.setProperty(runtime, "value", values[0]);
              return Value::undefined();
            }});
        auto reject = Function::createFromHostFunction(
            runtime, PropNameID::forAscii(runtime, "reject"), 1,
            [promise](Runtime &runtime, const Value &, const Value *values, std::size_t size) mutable {{
              promise.setProperty(runtime, "settled", String::createFromAscii(runtime, "rejected"));
              if (size == 1) promise.setProperty(runtime, "error", values[0]);
              return Value::undefined();
            }});
        const Value continuations[] = {{Value(resolve), Value(reject)}};
        Function(arguments[0].getObject(runtime)).call(runtime, continuations, 2);
        return Value(std::move(promise));
      }});
  js.global().setProperty(js, "Promise", std::move(promise_constructor));
}}

int main() {{
  Runtime js;
  install_host_builtins(js);
  std::mutex queue_mutex;
  std::vector<RuntimeSession::JsTask> queue;
  auto runtime_session = RuntimeSession::create([&](RuntimeSession::JsTask task) {{
    std::lock_guard lock(queue_mutex);
    queue.push_back(std::move(task));
  }});
  auto cleanup = process_services().cleanup();
  Object feature_registry(js);
  js.global().setProperty(js, "__supernoteModuleFeatureRegistry_63f6999c8c67", feature_registry);
{session_rows}
{feature_calls}
  auto error_constructor = js.global().getPropertyAsFunction(js, "TypeError");
{error_rows}
{jvm_calls}

  auto native_exports = feature_registry.getPropertyAsObject(js, {native_id});
  auto score = native_exports.getPropertyAsFunction(js, "nativeScore");
  Value seed(3);
  const auto native = score.call(js, seed).asNumber();
  auto mixed_exports = feature_registry.getPropertyAsObject(js, {mixed_id});
  auto mixed_function = mixed_exports.getPropertyAsFunction(js, "mixedNative");
  Value mixed_seed(10);
  const auto mixed = mixed_function.call(js, mixed_seed).asNumber();
  if (mixed != 15) return 9;
  auto accepts = score.getPropertyAsFunction(js, "accepts");
  Value wrong(String::createFromAscii(js, "3"));
  if (!accepts.call(js, seed).getBool() || accepts.call(js, wrong).getBool()) return 10;
  bool rejected_type = false;
  try {{ score.call(js, wrong); }} catch (const JSError &error) {{
    auto detail = error.value().getObject(js);
    rejected_type = detail.getProperty(js, "reason").asString(js).utf8(js) == "TYPE_MISMATCH";
  }}
  if (!rejected_type) return 11;
  auto counter_type = native_exports.getPropertyAsObject(js, "Counter");
  auto create = counter_type.getPropertyAsFunction(js, "create");
  Value initial(9);
  auto counter = create.call(js, initial).getObject(js);
  auto value_method = counter.getPropertyAsFunction(js, "value");
  if (value_method.callWithThis(js, counter).asNumber() != 9) return 12;
  auto same_method = counter.getPropertyAsFunction(js, "same");
  if (!same_method.callWithThis(js, counter, Value(counter)).getBool()) return 13;
  try {{ same_method.callWithThis(js, counter, wrong); return 14; }}
  catch (const JSError &) {{}}
{revision_call}
{jvm_validation}

  auto async_score = native_exports.getPropertyAsFunction(js, "asyncNativeScore");
  auto promise = async_score.call(js, seed).getObject(js);
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (promise.getProperty(js, "settled").isUndefined() &&
         std::chrono::steady_clock::now() < deadline) {{
    std::vector<RuntimeSession::JsTask> ready;
    {{ std::lock_guard lock(queue_mutex); ready.swap(queue); }}
    for (auto &task : ready) task(&js);
    std::this_thread::yield();
  }}
  if (promise.getProperty(js, "settled").asString(js).utf8(js) != "resolved" ||
      promise.getProperty(js, "value").asNumber() != 236) return 15;

  auto slow = native_exports.getPropertyAsFunction(js, "slowNativeScore");
  auto cancelled = slow.call(js, seed).getObject(js);
  native_session->close_feature();
  const auto cancellation_deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (cancelled.getProperty(js, "settled").isUndefined() &&
         std::chrono::steady_clock::now() < cancellation_deadline) {{
    std::vector<RuntimeSession::JsTask> ready;
    {{ std::lock_guard lock(queue_mutex); ready.swap(queue); }}
    for (auto &task : ready) task(&js);
    std::this_thread::yield();
  }}
  if (cancelled.getProperty(js, "settled").asString(js).utf8(js) != "rejected") return 16;

  const auto calls_before_stale_receiver =
      supernote_feature_NativeMatrix::q5_counter_value_calls;
  runtime_session->invalidate();
  auto replacement_runtime = RuntimeSession::create([](RuntimeSession::JsTask) {{}});
  if (replacement_runtime->id() == runtime_session->id()) return 17;
  auto replacement_feature = FeatureSession::create(replacement_runtime, cleanup);
  if (replacement_feature->id() == native_session->id()) return 21;
  bool stale_receiver_rejected = false;
  try {{
    (void)value_method.callWithThis(js, counter);
  }} catch (const JSError &error) {{
    auto detail = error.value().getObject(js);
    stale_receiver_rejected =
        detail.getProperty(js, "code").asString(js).utf8(js) == "FEATURE_CLOSED";
  }}
  if (!stale_receiver_rejected ||
      supernote_feature_NativeMatrix::q5_counter_value_calls !=
          calls_before_stale_receiver) return 22;
  const bool registrations_ok = {registration_checks};
  replacement_runtime->invalidate();
  counter = Object(js);
  cleanup->drain_and_shutdown();
  process_services().shutdown();
  std::cout << "Q5_GENERATED_BINDINGS=" << native << ",15" << std::endl;
  return registrations_ok ? 0 : 20;
}}
"""


def _compile_native_consumer(
    consumer: Path,
    java_home: Path,
    identities: Mapping[str, tuple[str, str, str]],
    guard: _ConsumerGeneratorGuard,
    *,
    revision: bool,
    guard_sensitivity: bool = False,
) -> dict[str, object]:
    installed_packages = {
        name: consumer / "node_modules/@q5" / name for name in identities
    }
    installed_packages["runtime"] = consumer / "node_modules/@supernote/runtime"
    package_inputs_before = {
        name: _tree_inventory(root) for name, root in installed_packages.items()
    }
    node = shutil.which("node")
    assert node is not None
    composition = consumer / "build-supernote-composition"
    composed = _run(
        (
            node,
            consumer / "node_modules/@supernote/runtime/compose-packages.js",
            consumer,
            composition,
        ),
        cwd=consumer,
        consumer_guard=guard,
    )
    _assert_run(composed)
    composition_result = json.loads(composed.stdout)
    inventory = Path(composition_result["inventory"]).resolve()
    assert inventory == (composition / "inventory.json").resolve()
    resolved_inventory = json.loads(inventory.read_text(encoding="utf-8"))
    assert composition_result["moduleCount"] == len(identities)
    assert set(composition_result["featureIds"]) == {
        feature_id for feature_id, _target, _symbol in identities.values()
    }
    assert {module["name"] for module in resolved_inventory["modules"]} == {
        f"@q5/{name}" for name in identities
    }
    for module in resolved_inventory["modules"]:
        installed = consumer / "node_modules" / module["name"]
        assert Path(module["root"]).resolve() == installed.resolve()
        assert Path(module["cmake"]).resolve() == (
            installed / ".supernote-generated/android/CMakeLists.txt"
        ).resolve()
    assert {
        name: _tree_inventory(root) for name, root in installed_packages.items()
    } == package_inputs_before

    stub = consumer / "host-stub"
    _write_jsi_stub(stub)
    _write(
        stub / "android/log.h",
        "#pragma once\n#define ANDROID_LOG_ERROR 6\n"
        "inline int __android_log_print(int, const char *, const char *, ...) "
        "{ return 0; }\n",
    )
    _write(consumer / "main.cpp", _native_main(identities, revision=revision))
    java_platform = {"darwin": "darwin", "linux": "linux"}.get(sys.platform, "win32")
    copy_commands = ""
    if os.name == "nt":
        copy_commands = """add_custom_command(TARGET q5_consumer POST_BUILD
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different
    "$<TARGET_FILE:q5_source_shared>" "$<TARGET_FILE_DIR:q5_consumer>"
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different
    "$<TARGET_FILE:q5_imported_shared>" "$<TARGET_FILE_DIR:q5_consumer>")
"""
    _write(
        consumer / "CMakeLists.txt",
        f"""cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0131 NEW)
project(q5_combined_consumer LANGUAGES C CXX)
find_package(Threads REQUIRED)
add_library(q5_runtime_contract STATIC)
include("${{CMAKE_CURRENT_LIST_DIR}}/node_modules/@supernote/runtime/cmake/SupernoteModules.cmake")
supernote_configure_runtime(q5_runtime_contract)
target_include_directories(q5_runtime_contract PUBLIC
  "{stub.as_posix()}"
  "{java_home.as_posix()}/include"
  "{java_home.as_posix()}/include/{java_platform}")
target_link_libraries(q5_runtime_contract PUBLIC Threads::Threads)
add_executable(q5_consumer main.cpp)
target_link_libraries(q5_consumer PRIVATE q5_runtime_contract)
supernote_link_modules(q5_consumer q5_runtime_contract "{inventory.as_posix()}")
if(WIN32)
  target_link_options(q5_consumer PRIVATE "LINKER:/MAP")
endif()
{copy_commands}""",
    )
    cmake = shutil.which("cmake")
    assert cmake is not None
    compiler_inputs: set[str] = set()
    outputs: list[str] = []
    for configuration in ("Debug", "Release"):
        build = consumer / f"build-{configuration.lower()}"
        configure = [
            cmake,
            "-S",
            str(consumer),
            "-B",
            str(build),
            f"-DCMAKE_BUILD_TYPE={configuration}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        if shutil.which("ninja") is not None:
            configure.extend(("-G", "Ninja"))
        _assert_run(_run(configure, cwd=consumer, consumer_guard=guard))
        built = _run(
            (cmake, "--build", build, "--verbose"),
            cwd=consumer,
            consumer_guard=guard,
        )
        _assert_run(built)
        assert "vendor/not_selected.cpp" not in built.stdout + built.stderr
        executable = build / ("q5_consumer.exe" if os.name == "nt" else "q5_consumer")
        executed = _run((executable,), cwd=build, consumer_guard=guard)
        _assert_run(executed)
        assert "Q5_GENERATED_BINDINGS=236,15" in executed.stdout
        commands = json.loads((build / "compile_commands.json").read_text(encoding="utf-8"))
        compiler_inputs.update(str(Path(row["file"]).resolve()) for row in commands)
        outputs.append(executed.stdout.strip())

    for package_name in identities:
        installed = consumer / "node_modules/@q5" / package_name
        registration = json.loads(
            (installed / ".supernote-generated/android/registration.json").read_text(
                encoding="utf-8"
            )
        )
        for relative in registration["bindingSources"]:
            assert str((installed / relative).resolve()) in compiler_inputs

    guard_sensitivity_result: dict[str, object] | None = None
    if guard_sensitivity:
        guarded_build = consumer / "build-guard-disabled"
        configure = [
            cmake,
            "-S",
            str(consumer),
            "-B",
            str(guarded_build),
            "-DCMAKE_BUILD_TYPE=Debug",
        ]
        if shutil.which("ninja") is not None:
            configure.extend(("-G", "Ninja"))
        _assert_run(_run(configure, cwd=consumer, consumer_guard=guard))

        generated_binding = (
            consumer
            / "node_modules/@q5/native/.supernote-generated/android/feature.cpp"
        )
        original_binding_bytes = generated_binding.read_bytes()
        original_binding = original_binding_bytes.decode("utf-8")
        guard_pattern = re.compile(
            r"if \(!active_feature \|\|\n"
            r"(?P<indent>\s*)active_feature->state\(\) != "
            r"supernote::runtime::FeatureState::ACTIVE\) \{"
        )
        mutated_binding, mutation_count = guard_pattern.subn(
            r"if (false && (!active_feature ||\n"
            r"\g<indent>active_feature->state() != "
            r"supernote::runtime::FeatureState::ACTIVE)) {",
            original_binding,
        )
        assert mutation_count > 0
        generated_binding.write_bytes(mutated_binding.encode("utf-8"))
        try:
            built = _run(
                (cmake, "--build", guarded_build, "--verbose"),
                cwd=consumer,
                consumer_guard=guard,
            )
            _assert_run(built)
            mutated_executable = guarded_build / (
                "q5_consumer.exe" if os.name == "nt" else "q5_consumer"
            )
            executed = _run(
                (mutated_executable,), cwd=guarded_build, consumer_guard=guard
            )
            assert executed.returncode == 22, executed.stdout + executed.stderr
            guard_sensitivity_result = {
                "disabled_guard_sites": mutation_count,
                "expected_oracle_failure_returncode": executed.returncode,
                "authored_method_execution_detected": True,
            }
        finally:
            generated_binding.write_bytes(original_binding_bytes)
        assert generated_binding.read_bytes() == original_binding_bytes
    native_root = consumer / "node_modules/@q5/native/android/src/main/cpp"
    assert str((native_root / "c23_helper.c").resolve()) in compiler_inputs
    assert any("file_backend" in path for path in compiler_inputs)
    assert any("source_shared" in path for path in compiler_inputs)
    if os.name == "nt":
        linker_map = executable.with_suffix(".map")
        assert linker_map.is_file()
        linker_evidence = linker_map.read_text(encoding="utf-8", errors="replace")
        linked_generated_symbols = [
            line.strip()
            for line in linker_evidence.splitlines()
            if "register_feature" in line or "register_jvm_feature" in line
        ]
    else:
        symbol_tool = shutil.which("llvm-nm") or shutil.which("nm")
        assert symbol_tool is not None
        symbols = _run(
            (symbol_tool, "-C", executable), cwd=consumer, consumer_guard=guard
        )
        _assert_run(symbols)
        linked_generated_symbols = [
            line
            for line in symbols.stdout.splitlines()
            if "register_feature" in line or "register_jvm_feature" in line
        ]
    assert any("register_feature" in line for line in linked_generated_symbols)
    assert any("register_jvm_feature" in line for line in linked_generated_symbols)
    return {
        "composition": composition_result,
        "compiler_inputs": sorted(compiler_inputs),
        "outputs": outputs,
        "linked_generated_symbols": linked_generated_symbols,
        "generation_guard_sensitivity": guard_sensitivity_result,
    }


def _adapter_name(feature: Path) -> str:
    source = (
        feature / ".supernote-generated/android/jvm/KspAdapters.kt"
    ).read_text(encoding="utf-8")
    names = re.findall(r"^object (Adapter_[0-9a-f]+) \{", source, re.MULTILINE)
    assert len(names) == 1
    return names[0]


def _execute_jvm_consumer(
    consumer: Path, guard: _ConsumerGeneratorGuard
) -> dict[str, object]:
    installed_packages = {
        name: consumer / "node_modules" / name
        for name in (
            "@q5/native",
            "@q5/kotlin",
            "@q5/java",
            "@q5/mixed",
            "@supernote/runtime",
        )
    }
    package_inputs_before = {
        name: _tree_inventory(root) for name, root in installed_packages.items()
    }
    adapters = {
        name: _adapter_name(consumer / "node_modules" / name)
        for name in ("@q5/kotlin", "@q5/java", "@q5/mixed")
    }
    calls = [
        (adapters["@q5/kotlin"], "K", "Kotlin:K"),
        (adapters["@q5/java"], "J", "Java:J"),
        (adapters["@q5/mixed"], "M", "Mixed:M"),
    ]
    expressions = "\n".join(
        f'  check({adapter}.invoke("{argument}".encodeToByteArray()).decodeToString() == "{expected}")'
        for adapter, argument, expected in calls
    )
    _write(
        consumer / "android/settings.gradle",
        """pluginManagement {
  repositories { google(); mavenCentral(); gradlePluginPortal() }
}
rootProject.name = 'q5-combined-android-consumer'
include ':supernote-runtime', ':host-jvm'
project(':supernote-runtime').projectDir =
    file('../node_modules/@supernote/runtime/android')
project(':host-jvm').projectDir = file('host-jvm')
""",
    )
    _write(
        consumer / "android/build.gradle",
        """import groovy.json.JsonOutput

buildscript {
  ext {
    buildToolsVersion = '35.0.0'
    minSdkVersion = 27
    compileSdkVersion = 35
    targetSdkVersion = 35
    ndkVersion = '27.1.12297006'
    kotlinVersion = '2.0.21'
  }
  repositories { google(); mavenCentral() }
  dependencies {
    classpath 'com.android.tools.build:gradle:8.8.2'
    classpath 'org.jetbrains.kotlin:kotlin-gradle-plugin:2.0.21'
  }
}

allprojects { repositories { google(); mavenCentral() } }

project(':supernote-runtime') {
  afterEvaluate { runtimeProject ->
    tasks.register('verifyQ5Autolink') {
      doLast {
        def adapterSources = runtimeProject.ext.supernoteJvmSourceDirs.collect {
          new File(it).canonicalPath
        }.sort()
        def androidSources = runtimeProject.android.sourceSets.main.java.srcDirs.collect {
          it.canonicalPath
        }.sort()
        if (!androidSources.containsAll(adapterSources)) {
          throw new GradleException('runtime adapter omitted validated JVM sources')
        }
        println 'Q5_ANDROID_AUTOLINK_JVM_SOURCES=' +
            JsonOutput.toJson(adapterSources)
      }
    }
  }
}
""",
    )
    _write(
        consumer / "android/gradle.properties",
        "android.useAndroidX=true\norg.gradle.jvmargs=-Xmx2048m\n",
    )
    _write(
        consumer / "android/host-jvm/build.gradle",
        """apply plugin: 'org.jetbrains.kotlin.jvm'

repositories { mavenCentral() }
evaluationDependsOn(':supernote-runtime')
def runtimeAdapter = project(':supernote-runtime')
def adapterSources = runtimeAdapter.ext.supernoteJvmSourceDirs.collect { file(it) }

sourceSets.main.java.srcDirs(adapterSources)
kotlin {
  sourceSets.main.kotlin.srcDirs(adapterSources)
  jvmToolchain(17)
}
dependencies {
  implementation 'org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1'
}
tasks.register('runHarness', JavaExec) {
  dependsOn classes
  classpath = sourceSets.main.runtimeClasspath
  mainClass = 'q5.consumer.Q5JvmHarnessKt'
}
""",
    )
    _write(
        consumer
        / "android/host-jvm/src/main/kotlin/com/facebook/react/bridge/ReactApplicationContext.kt",
        "package com.facebook.react.bridge\nopen class ReactApplicationContext\n",
    )
    _write(
        consumer / "android/host-jvm/src/main/kotlin/Q5JvmHarness.kt",
        "package q5.consumer\n\n"
        + "import supernote.generated.adapters.*\n\n"
        + "fun main() {\n"
        + expressions
        + '\n  println("Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M")\n}\n',
    )
    gradle = Path(os.environ["SUPERNOTE_GRADLE_COMMAND"])
    result = _run(
        (
            gradle,
            "--no-daemon",
            "--console=plain",
            "--offline",
            "-p",
            consumer / "android",
            "-PsupernoteModuleCmakeVersion=4.1.2",
            ":supernote-runtime:verifyQ5Autolink",
            ":host-jvm:classes",
            ":host-jvm:runHarness",
            ":host-jvm:tasks",
            "--all",
        ),
        cwd=consumer,
        consumer_guard=guard,
    )
    _assert_run(result)
    output = result.stdout + result.stderr
    source_markers = re.findall(
        r"^Q5_ANDROID_AUTOLINK_JVM_SOURCES=(.+)$", output, re.MULTILINE
    )
    assert len(source_markers) == 1
    adapter_sources = json.loads(source_markers[0])
    composition_inventory = (
        consumer
        / "android/build/react-native-libraries/supernote-runtime/generated"
        / "supernote-composition/inventory.json"
    )
    inventory = json.loads(composition_inventory.read_text(encoding="utf-8"))
    assert Path(inventory["runtime"]["root"]).resolve() == installed_packages[
        "@supernote/runtime"
    ].resolve()
    assert {
        module["name"]: Path(module["root"]).resolve()
        for module in inventory["modules"]
    } == {
        name: root.resolve()
        for name, root in installed_packages.items()
        if name.startswith("@q5/")
    }
    expected_sources = {
        str(Path(source).resolve())
        for module in inventory["modules"]
        for source in (
            *module["jvmSourceDirs"],
            *(str(Path(path).parent) for path in module["jvmRegistrationSources"]),
        )
    }
    expected_sources.add(
        str(
            (
                consumer / "node_modules/@supernote/runtime/jvm"
            ).resolve()
        )
    )
    assert adapter_sources == sorted(expected_sources)
    assert "Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M" in output
    assert "kspKotlin" not in output
    assert "sn-module-gen" not in output
    assert {
        name: _tree_inventory(root) for name, root in installed_packages.items()
    } == package_inputs_before
    return {
        "android_autolink_sources": adapter_sources,
        "host_jvm_execution": "Q5_HOST_JVM_EXECUTION=Kotlin:K,Java:J,Mixed:M",
    }


def _execute_javascript_surface(
    consumer: Path, feature_id: str, guard: _ConsumerGeneratorGuard
) -> str:
    index = consumer / "node_modules/@q5/native/.supernote-generated/index.js"
    harness = consumer / "q5-js-surface.mjs"
    _write(
        harness,
        f"""import {{readFile}} from 'node:fs/promises';
let nativeCalls = 0;
let featureLookups = 0;
let generation = 1;
const typeError = Object.assign(new TypeError('wrong'), {{
  reason: 'TYPE_MISMATCH', path: 'arg', expected: 'number', actual: 'string'
}});
const rangeError = Object.assign(new RangeError('large'), {{
  reason: 'LIMIT_EXCEEDED', path: 'bytes', expected: 'at most limit', actual: 'over'
}});
const nativeScore = value => {{ nativeCalls += 1; return value + generation; }};
nativeScore.accepts = value => typeof value === 'number';
nativeScore.checkArguments = value => typeof value === 'number'
  ? {{ok: true}} : {{ok: false, error: typeError}};
const nativeType = {{
  is: value => value?.kind === 'native',
  check: value => value?.kind === 'native'
    ? {{ok: true}} : {{ok: false, error: typeError}},
}};
const feature = {{
  nativeScore,
  delayed: async value => value + generation,
  NativeType: nativeType,
  __supernoteCppObjectInfo: value => value?.kind === 'native'
    ? {{type: 'q5:native', originFamily: 'cpp'}} : undefined,
}};
globalThis.__supernoteModule = {{feature: id => {{
  featureLookups += 1;
  if (id !== {json.dumps(feature_id)}) throw new Error('wrong feature id');
  return feature;
}}}};
const source = await readFile({json.dumps(str(index.resolve()))}, 'utf8');
const module = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
if (!module.isFeatureAvailable() || module.getFeatureStatus() !== 'available') throw new Error('status');
if (!module.default.nativeScore.accepts(4)) throw new Error('accepts');
if (module.default.nativeScore.accepts('4')) throw new Error('accepts negative');
if (!module.default.nativeScore.checkArguments(4).ok) throw new Error('check');
if (module.default.nativeScore.checkArguments('4').ok) throw new Error('check negative');
if (nativeCalls !== 0) throw new Error('validation invoked native');
if (!module.default.NativeType.is({{kind: 'native'}})) throw new Error('type is');
if (module.default.NativeType.check({{kind: 'wrong'}}).ok) throw new Error('type check');
if (nativeCalls !== 0) throw new Error('type inspection invoked native');
if (module.nativeObjectInfo({{kind: 'native'}}).type !== 'q5:native') throw new Error('object info');
if (!module.isSupernoteTypeError(typeError)) throw new Error('type error');
if (!module.isSupernoteRangeError(rangeError)) throw new Error('range error');
const supernoteError = new module.SupernoteError('IMPLEMENTATION_ERROR', 'boom');
if (supernoteError.code !== 'IMPLEMENTATION_ERROR') throw new Error('supernote error');
if (module.default.nativeScore(10) !== 11 || nativeCalls !== 1) throw new Error('call');
generation = 2;
if (module.default.nativeScore(10) !== 12 || nativeCalls !== 2) throw new Error('replacement');
if (await module.default.delayed(5) !== 7) throw new Error('async');
globalThis.__supernoteModule = undefined;
if (module.getFeatureStatus() !== 'runtime-unavailable') throw new Error('unavailable');
console.log(`Q5_JS_SURFACE_OK=${{nativeCalls}},${{featureLookups}}`);
""",
    )
    node = shutil.which("node")
    assert node is not None
    result = _run(
        (node, harness),
        cwd=consumer,
        consumer_guard=guard,
    )
    _assert_run(result)
    assert "Q5_JS_SURFACE_OK=2" in result.stdout
    return result.stdout.strip()


def _write_jsi_stub(root: Path) -> None:
    _write(
        root / "jsi/jsi.h",
        r"""#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
namespace facebook::jsi {
class Runtime;
class Object;
class String;
class Function;
class PropNameID;
struct ObjectState;

class Value {
 public:
  enum class Kind { UNDEFINED, NULL_VALUE, BOOL, NUMBER, STRING, OBJECT };
  Value() = default;
  Value(std::nullptr_t) : kind_(Kind::NULL_VALUE) {}
  explicit Value(bool value) : kind_(Kind::BOOL), boolean_(value) {}
  explicit Value(double value) : kind_(Kind::NUMBER), number_(value) {}
  explicit Value(int value) : Value(static_cast<double>(value)) {}
  Value(Runtime &, const Value &value) : Value(value) {}
  Value(const String &value);
  Value(String &&value);
  Value(const Object &value);
  Value(Object &&value);
  static Value undefined() { return Value(); }
  bool isUndefined() const { return kind_ == Kind::UNDEFINED; }
  bool isNull() const { return kind_ == Kind::NULL_VALUE; }
  bool isBool() const { return kind_ == Kind::BOOL; }
  bool isNumber() const { return kind_ == Kind::NUMBER; }
  bool isBigInt() const { return false; }
  bool isString() const { return kind_ == Kind::STRING; }
  bool isSymbol() const { return false; }
  bool isObject() const { return kind_ == Kind::OBJECT && state_; }
  bool getBool() const { return boolean_; }
  double asNumber() const { return number_; }
  const std::string &string_value() const { return string_; }
  String asString(Runtime &) const;
  Object getObject(Runtime &) const;

 protected:
  Kind kind_{Kind::UNDEFINED};
  bool boolean_{false};
  double number_{0};
  std::string string_;
  std::shared_ptr<ObjectState> state_;
  friend class Object;
  friend class String;
  friend class WeakObject;
};

class PropNameID {
 public:
  static PropNameID forAscii(Runtime &, const char *value) {
    return PropNameID(value);
  }
  std::string utf8(Runtime &) const { return value_; }
 private:
  explicit PropNameID(std::string value) : value_(std::move(value)) {}
  std::string value_;
};

class String : public Value {
 public:
  String() { kind_ = Kind::STRING; }
  explicit String(std::string value) {
    kind_ = Kind::STRING;
    string_ = std::move(value);
  }
  static String createFromAscii(Runtime &, const char *value) {
    return String(value);
  }
  static String createFromAscii(Runtime &, const std::string &value) {
    return String(value);
  }
  static String createFromUtf8(Runtime &, const std::string &value) {
    return String(value);
  }
  static String createFromUtf8(Runtime &, const char *value) {
    return String(value);
  }
  std::string utf8(Runtime &) const { return string_; }
};

inline Value::Value(const String &value) : Value(static_cast<const Value &>(value)) {}
inline Value::Value(String &&value) : Value(static_cast<const Value &>(value)) {}
inline String Value::asString(Runtime &) const {
  if (!isString()) throw std::runtime_error("value is not a string");
  return String(string_);
}

class HostObject {
 public:
  virtual ~HostObject() = default;
  virtual Value get(Runtime &, const PropNameID &) { return {}; }
  virtual std::vector<PropNameID> getPropertyNames(Runtime &) { return {}; }
};

class MutableBuffer {
 public:
  virtual ~MutableBuffer() = default;
  virtual std::size_t size() const = 0;
  virtual std::uint8_t *data() = 0;
};

using HostFunction = std::function<Value(
    Runtime &, const Value &, const Value *, std::size_t)>;
struct ObjectState {
  std::map<std::string, Value> properties;
  std::shared_ptr<HostObject> host;
  std::shared_ptr<MutableBuffer> buffer;
  HostFunction function;
  bool array{false};
};

class Object : public Value {
 public:
  Object() { kind_ = Kind::OBJECT; state_ = std::make_shared<ObjectState>(); }
  explicit Object(Runtime &) : Object() {}
  explicit Object(std::shared_ptr<ObjectState> state) {
    kind_ = Kind::OBJECT;
    state_ = std::move(state);
  }
  static Object createFromHostObject(Runtime &, std::shared_ptr<HostObject> host) {
    Object result;
    result.state_->host = std::move(host);
    return result;
  }
  template <typename T> bool isHostObject(Runtime &) const {
    return state_ && std::dynamic_pointer_cast<T>(state_->host);
  }
  template <typename T> std::shared_ptr<T> getHostObject(Runtime &) const {
    return std::dynamic_pointer_cast<T>(state_->host);
  }
  bool isArray(Runtime &) const { return state_ && state_->array; }
  bool isFunction(Runtime &) const { return state_ && static_cast<bool>(state_->function); }
  bool isArrayBuffer(Runtime &) const { return state_ && static_cast<bool>(state_->buffer); }
  bool instanceOf(Runtime &, const Function &) const { return false; }
  Value getProperty(Runtime &runtime, const char *name) const {
    const auto found = state_->properties.find(name);
    if (found != state_->properties.end()) return found->second;
    if (state_->host) return state_->host->get(runtime, PropNameID::forAscii(runtime, name));
    return {};
  }
  Object getPropertyAsObject(Runtime &runtime, const char *name) const {
    return getProperty(runtime, name).getObject(runtime);
  }
  Function getPropertyAsFunction(Runtime &runtime, const char *name) const;
  Function asFunction(Runtime &) const;
  void setProperty(Runtime &, const char *name, Value value) {
    state_->properties[name] = std::move(value);
  }
  void setProperty(Runtime &runtime, const char *name, bool value) {
    setProperty(runtime, name, Value(value));
  }
  void setProperty(Runtime &runtime, const char *name, double value) {
    setProperty(runtime, name, Value(value));
  }
  template <typename T>
  void setProperty(Runtime &runtime, const char *name, T value) {
    setProperty(runtime, name, Value(std::move(value)));
  }
  class ArrayBuffer getArrayBuffer(Runtime &) const;
  const void *identity() const { return state_.get(); }
};

inline Value::Value(const Object &value) : Value(static_cast<const Value &>(value)) {}
inline Value::Value(Object &&value) : Value(static_cast<const Value &>(value)) {}
inline Object Value::getObject(Runtime &) const { return Object(state_); }

class Function : public Object {
 public:
  Function() = default;
  explicit Function(std::shared_ptr<ObjectState> state) : Object(std::move(state)) {}
  explicit Function(const Object &object) : Object(object) {}
  template <typename Callback>
  static Function createFromHostFunction(
      Runtime &, const PropNameID &, std::size_t, Callback callback) {
    Function result;
    result.state_->function = HostFunction(std::move(callback));
    return result;
  }
  Value call(Runtime &runtime, const Value *arguments, std::size_t count) const {
    if (!state_ || !state_->function) throw std::runtime_error("not callable");
    return state_->function(runtime, Value(), arguments, count);
  }
  Value call(Runtime &runtime) const { return call(runtime, nullptr, 0); }
  template <typename Argument>
  Value call(Runtime &runtime, Argument argument) const {
    const Value value(std::move(argument));
    return call(runtime, &value, 1);
  }
  Value callAsConstructor(
      Runtime &runtime, const Value *arguments, std::size_t count) const {
    if (!state_ || !state_->function) return Value(Object(runtime));
    return state_->function(runtime, Value(), arguments, count);
  }
  Value callAsConstructor(Runtime &runtime) const {
    return callAsConstructor(runtime, nullptr, 0);
  }
  template <typename Argument>
  Value callWithThis(Runtime &runtime, const Object &owner, Argument argument) const {
    const Value value(std::move(argument));
    return state_->function(runtime, owner, &value, 1);
  }
  Value callWithThis(Runtime &runtime, const Object &owner) const {
    return state_->function(runtime, owner, nullptr, 0);
  }
  template <typename First, typename Second>
  Value callWithThis(
      Runtime &runtime, const Object &owner, First first, Second second) const {
    const Value values[] = {Value(std::move(first)), Value(std::move(second))};
    return state_->function(runtime, owner, values, 2);
  }
  Value callWithThis(
      Runtime &runtime, const Object &owner,
      const Value *arguments, std::size_t count) const {
    return state_->function(runtime, owner, arguments, count);
  }
};

inline Function Object::getPropertyAsFunction(Runtime &runtime, const char *name) const {
  return Function(getProperty(runtime, name).state_);
}
inline Function Object::asFunction(Runtime &) const { return Function(*this); }

class Array : public Object {};

class ArrayBuffer : public Object {
 public:
  ArrayBuffer() = default;
  ArrayBuffer(Runtime &, std::shared_ptr<MutableBuffer> buffer) {
    state_->buffer = std::move(buffer);
  }
  explicit ArrayBuffer(std::shared_ptr<ObjectState> state) : Object(std::move(state)) {}
  std::size_t size(Runtime &) const { return state_->buffer->size(); }
  std::uint8_t *data(Runtime &) const { return state_->buffer->data(); }
};

inline ArrayBuffer Object::getArrayBuffer(Runtime &) const { return ArrayBuffer(state_); }

class WeakObject {
 public:
  WeakObject(Runtime &, const Object &object) : state_(object.state_) {}
  WeakObject(WeakObject &&) = default;
  WeakObject &operator=(WeakObject &&) = default;
  Value lock(Runtime &) const { return Value(Object(state_.lock())); }
 private:
  std::weak_ptr<ObjectState> state_;
};

class Runtime {
 public:
  Object &global() { return global_; }
 private:
  Object global_;
};

class JSError : public std::runtime_error {
 public:
  JSError(Runtime &, Value value)
      : std::runtime_error("JavaScript error"), value_(std::move(value)) {}
  JSError(Runtime &, const std::string &message)
      : std::runtime_error(message), value_(Object()) {}
  const Value &value() const { return value_; }
 private:
  Value value_;
};
}  // namespace facebook::jsi
""",
    )


def _execute_generated_runtime(
    runtime_snapshot: Path, root: Path, guard: _ConsumerGeneratorGuard
) -> str:
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    assert compiler is not None
    stub = root / "runtime-stub"
    _write_jsi_stub(stub)
    harness = root / "runtime-lifecycle.cpp"
    _write(
        harness,
        r"""#include "runtime_services.hpp"
#include <supernote/cpp_objects.hpp>
#include <atomic>
#include <chrono>
#include <future>
#include <iostream>
#include <memory>
#include <thread>
#include <vector>
using namespace supernote::runtime;
struct Native { int value = 7; };
class NativeHost final : public CppObjectHandle<Native> {
 public: using CppObjectHandle::CppObjectHandle;
};
struct Tracked {
  explicit Tracked(std::promise<std::thread::id> *done) : done(done) {}
  ~Tracked() { done->set_value(std::this_thread::get_id()); }
  std::promise<std::thread::id> *done;
};
int main() {
  std::vector<RuntimeSession::JsTask> queue;
  auto runtime = RuntimeSession::create([&](RuntimeSession::JsTask task) {
    queue.push_back(std::move(task));
  });
  auto cleanup = std::make_shared<DeferredDestruction>();
  auto feature = FeatureSession::create(runtime, cleanup);
  std::atomic<int> completed{0};
  std::atomic<int> rejected{0};
  auto operation = feature->accept([&](void *) { ++rejected; });
  if (!feature->schedule_completion(operation, [&](void *) { ++completed; })) return 1;
  feature->close_feature();
  int fake_runtime = 1;
  for (auto &task : queue) task(&fake_runtime);
  if (completed != 0 || rejected != 1 ||
      operation->winner() != OperationWinner::CANCELLED_BY_FEATURE) return 2;
  auto replacement = RuntimeSession::create([](RuntimeSession::JsTask) {});
  if (replacement->id() == runtime->id()) return 3;
  auto replacement_feature = FeatureSession::create(replacement, cleanup);
  auto pending = replacement_feature->accept({});
  replacement->invalidate();
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
  while (pending->winner() == OperationWinner::PENDING &&
         std::chrono::steady_clock::now() < deadline) std::this_thread::yield();
  if (pending->winner() != OperationWinner::CANCELLED_BY_RUNTIME) return 4;

  facebook::jsi::Runtime js_runtime;
  auto registry = std::make_shared<CppObjectRegistry>(cleanup);
  auto native = std::make_shared<Native>();
  auto first = registry->wrap(js_runtime, "q5:native", native,
      [](ManagedRef<Native> value) {
        return std::make_shared<NativeHost>("q5:native", std::move(value));
      });
  auto second = registry->wrap(js_runtime, "q5:native", native,
      [](ManagedRef<Native> value) {
        return std::make_shared<NativeHost>("q5:native", std::move(value));
      });
  if (first.identity() != second.identity()) return 5;
  if (!try_extract_cpp_object<Native>(js_runtime, first, "q5:native")) return 6;
  if (try_extract_cpp_object<Native>(js_runtime, first, "q5:wrong")) return 7;

  std::promise<std::thread::id> destroyed;
  auto destroyed_future = destroyed.get_future();
  auto lifetime_feature = FeatureSession::create(
      RuntimeSession::create([](RuntimeSession::JsTask) {}), cleanup);
  auto service = lifetime_feature->service<Tracked>(
      "q5:tracked", [&] { return std::make_shared<Tracked>(&destroyed); });
  service.reset();
  const auto caller = std::this_thread::get_id();
  lifetime_feature->close_feature();
  if (destroyed_future.wait_for(std::chrono::seconds(2)) !=
      std::future_status::ready) return 8;
  if (destroyed_future.get() == caller) return 9;
  cleanup->drain_and_shutdown();
  process_services().shutdown();
  std::cout << "Q5_RUNTIME_LIFECYCLE_OK" << std::endl;
  return 0;
}
""",
    )
    executable = root / ("q5_runtime_lifecycle.exe" if os.name == "nt" else "q5_runtime_lifecycle")
    flags = [] if os.name == "nt" else ["-pthread"]
    result = _run(
        (
            compiler,
            "-std=c++23",
            *flags,
            runtime_snapshot / "src/runtime_services.cpp",
            harness,
            "-I",
            runtime_snapshot / "src",
            "-I",
            runtime_snapshot / "include",
            "-I",
            stub,
            "-o",
            executable,
        ),
        cwd=root,
        consumer_guard=guard,
    )
    _assert_run(result)
    executed = _run(
        (executable,), cwd=root, timeout=60, consumer_guard=guard
    )
    _assert_run(executed)
    assert executed.stdout.strip() == "Q5_RUNTIME_LIFECYCLE_OK"
    return executed.stdout.strip()


def test_installed_publisher_packages_execute_combined_native_jvm_and_runtime(
    tmp_path: Path,
) -> None:
    TRACE.clear()
    required_tools = ("cmake", "npm", "node", "java", "javac")
    missing = [name for name in required_tools if shutil.which(name) is None]
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    if missing or compiler is None:
        message = "combined Q5 acceptance requires CMake, C++, npm, Node, and Java"
        if os.environ.get("SUPERNOTE_REQUIRE_INSTALLED_QUALIFICATION") == "1":
            pytest.fail(message)
        pytest.skip(message)

    command = Path(os.environ["SUPERNOTE_Q5_MODULE_COMMAND"])
    wheel_python = Path(os.environ["SUPERNOTE_Q5_PYTHON"])
    wheel = Path(os.environ["SUPERNOTE_Q5_WHEEL"])
    sdist = Path(os.environ["SUPERNOTE_Q5_SDIST"])
    assert _sha256(wheel) == os.environ["SUPERNOTE_Q5_WHEEL_SHA256"]
    assert _sha256(sdist) == os.environ["SUPERNOTE_Q5_SDIST_SHA256"]
    probe = _run(
        (
            wheel_python,
            "-I",
            "-c",
            "import json,supernote_module_generator as m;"
            "print(json.dumps({'file':m.__file__,'version':m.__version__}))",
        ),
        cwd=tmp_path,
    )
    _assert_run(probe)
    installed_identity = json.loads(probe.stdout)
    assert Path(installed_identity["file"]).resolve().is_relative_to(
        wheel_python.parent.parent.resolve()
    )

    for tool, arguments in (
        (command, ("--version",)),
        (Path(shutil.which("cmake") or "cmake"), ("--version",)),
        (Path(compiler), ("--version",)),
        (Path(shutil.which("node") or "node"), ("--version",)),
        (Path(shutil.which("npm") or "npm"), ("--version",)),
        (Path(shutil.which("java") or "java"), ("-version",)),
    ):
        _assert_run(_run((tool, *arguments), cwd=tmp_path))

    publisher = tmp_path / "publisher"
    _write(publisher / "PluginConfig.json", "{}\n")
    _write(
        publisher / "package.json",
        '{"name":"q5-combined-publisher","private":true,"dependencies":{}}\n',
    )
    _write(publisher / "android/settings.gradle", "include ':app'\n")
    _write(publisher / "android/app/build.gradle", "plugins {}\n")
    _add_feature(
        publisher,
        "@q5/native",
        "NativeMatrix",
        "com.q5.native_matrix",
        "cpp",
    )
    _add_feature(
        publisher,
        "@q5/kotlin",
        "KotlinFeature",
        "com.q5.kotlin_feature",
        "kotlin",
    )
    _add_feature(
        publisher,
        "@q5/java",
        "JavaFeature",
        "com.q5.java_feature",
        "kotlin",
    )
    _add_feature(
        publisher,
        "@q5/mixed",
        "MixedFeature",
        "com.q5.mixed_feature",
        "cpp",
        "kotlin",
    )

    modules = publisher / "local_modules/@q5"
    native = modules / "native"
    _write_native_matrix(native)
    _build_prebuilt_libraries(tmp_path / "prebuilt", native)
    _write(
        modules / "kotlin/android/src/main/java/com/q5/kotlin_feature/FeatureApi.kt",
        """package com.q5.kotlin_feature
import supernote.generated.annotations.SupernotePluginExport
@SupernotePluginExport
fun kotlinGreeting(value: String): String = "Kotlin:$value"
""",
    )
    java_root = modules / "java/android/src/main/java/com/q5/java_feature"
    (java_root / "FeatureApi.kt").unlink()
    _write(
        java_root / "FeatureApi.java",
        """package com.q5.java_feature;
import supernote.generated.annotations.SupernotePluginExport;
public final class FeatureApi {
  @SupernotePluginExport
  public static String javaGreeting(String value) { return "Java:" + value; }
}
""",
    )
    _write(
        modules / "mixed/android/src/main/cpp/feature.cpp",
        """#include <cstdint>
namespace supernote_feature_MixedFeature {
// @SupernotePluginExport
std::int32_t mixedNative(std::int32_t value) { return value + 5; }
}  // namespace supernote_feature_MixedFeature
""",
    )
    _write(
        modules / "mixed/android/src/main/java/com/q5/mixed_feature/FeatureApi.kt",
        """package com.q5.mixed_feature
import supernote.generated.annotations.SupernotePluginExport
@SupernotePluginExport
fun mixedGreeting(value: String): String = "Mixed:$value"
""",
    )
    _assert_run(_installed_cli(publisher, "update", "--yes"))
    _assert_run(_installed_cli(publisher, "validate"))

    generated_v1 = {
        name: {
            relative: _sha256(modules / name / relative)
            for relative in (
                ".supernote-generated/android/feature.cpp",
                ".supernote-generated/android/jvm_feature.cpp",
                ".supernote-generated/android/package_registration.cpp",
            )
            if (modules / name / relative).is_file()
        }
        for name in ("native", "kotlin", "java", "mixed")
    }
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    v1_tarballs = {name: _pack(modules / name, artifacts) for name in generated_v1}
    runtime_tarball = _pack(Path(os.environ["SUPERNOTE_Q5_RUNTIME_ROOT"]), artifacts)
    runtime_payload = _assert_runtime_tarball_payload(runtime_tarball)
    for name, tarball in v1_tarballs.items():
        _assert_tarball_payload(tarball, generated_v1[name])

    _update_native_version(native)
    _assert_run(_installed_cli(publisher, "update", "--yes"))
    _assert_run(_installed_cli(publisher, "validate"))
    v2_generated = {
        relative: _sha256(native / relative)
        for relative in (
            ".supernote-generated/android/feature.cpp",
            ".supernote-generated/android/package_registration.cpp",
        )
    }
    assert v2_generated[".supernote-generated/android/feature.cpp"] != generated_v1[
        "native"
    ][".supernote-generated/android/feature.cpp"]
    v2_native_tarball = _pack(native, artifacts)
    _assert_tarball_payload(v2_native_tarball, v2_generated)

    identities = {
        name: _feature_identity(modules / name)
        for name in ("native", "kotlin", "java", "mixed")
    }
    publisher_unavailable = tmp_path / "publisher-unavailable"
    shutil.move(str(publisher), publisher_unavailable)
    assert not publisher.exists()

    consumer_v1 = tmp_path / "unrelated-consumer-v1"
    consumer_v2 = tmp_path / "unrelated-consumer-v2"
    consumer_guard = _ConsumerGeneratorGuard(tmp_path / "consumer-generator-guard")
    common = [v1_tarballs[name] for name in ("kotlin", "java", "mixed")]
    _install_consumer(
        consumer_v1,
        (v1_tarballs["native"], *common, runtime_tarball),
        consumer_guard,
    )
    _install_consumer(
        consumer_v2,
        (v2_native_tarball, *common, runtime_tarball),
        consumer_guard,
    )
    before_v1 = {
        name: _tree_inventory(consumer_v1 / "node_modules" / name)
        for name in (
            "@q5/native", "@q5/kotlin", "@q5/java", "@q5/mixed",
            "@supernote/runtime",
        )
    }
    before_v2 = {
        name: _tree_inventory(consumer_v2 / "node_modules" / name)
        for name in (
            "@q5/native", "@q5/kotlin", "@q5/java", "@q5/mixed",
            "@supernote/runtime",
        )
    }
    node = shutil.which("node")
    assert node is not None
    runtime_probe = _run(
        (
            node,
            consumer_v2 / "node_modules/@supernote/runtime/resolve-packages.js",
            consumer_v2,
        ),
        cwd=consumer_v2,
        consumer_guard=consumer_guard,
    )
    _assert_run(runtime_probe)
    runtime_inventory = json.loads(runtime_probe.stdout)["runtime"]
    assert {
        entry["path"]: entry["sha256"]
        for entry in runtime_inventory["payload"]
    } == runtime_payload

    java = shutil.which("java")
    assert java is not None
    java_home = _java_home(java, tmp_path, consumer_guard)
    native_v1 = _compile_native_consumer(
        consumer_v1,
        java_home,
        identities,
        consumer_guard,
        revision=False,
    )
    native_v2 = _compile_native_consumer(
        consumer_v2,
        java_home,
        identities,
        consumer_guard,
        revision=True,
        guard_sensitivity=True,
    )
    jvm_output = _execute_jvm_consumer(consumer_v2, consumer_guard)
    js_output = _execute_javascript_surface(
        consumer_v2, identities["native"][0], consumer_guard
    )
    runtime_output = _execute_generated_runtime(
        consumer_v2 / "node_modules/@supernote/runtime/native",
        tmp_path / "runtime-execution",
        consumer_guard,
    )
    assert {
        name: _tree_inventory(consumer_v1 / "node_modules" / name)
        for name in before_v1
    } == before_v1
    assert {
        name: _tree_inventory(consumer_v2 / "node_modules" / name)
        for name in before_v2
    } == before_v2

    tampered = tmp_path / "tampered-consumer"
    tampered.mkdir()
    shutil.copytree(consumer_v2 / "node_modules", tampered / "node_modules")
    shutil.copy2(consumer_v2 / "package.json", tampered / "package.json")
    generated_binding = tampered / "node_modules/@q5/native/.supernote-generated/android/feature.cpp"
    generated_binding.write_text(
        generated_binding.read_text(encoding="utf-8") + "\n// tampered\n",
        encoding="utf-8",
    )
    rejected = _run(
        (
            node,
            tampered
            / "node_modules/@supernote/runtime/resolve-packages.js",
            tampered,
        ),
        cwd=tampered,
        consumer_guard=consumer_guard,
    )
    assert rejected.returncode != 0
    assert "SNMG_MODULE_PAYLOAD_STALE" in rejected.stderr
    assert "digest" in rejected.stderr.lower()

    guarded_commands = [
        record["generator_guard"]
        for record in TRACE
        if "generator_guard" in record
    ]
    assert guarded_commands
    assert all(
        record["attempts_before"] == [] and record["attempts_after"] == []
        for record in guarded_commands
    )
    trap_arguments: tuple[str | Path, ...]
    if os.name == "nt":
        command_interpreter = os.environ.get("COMSPEC", "cmd.exe")
        trap_arguments = (
            command_interpreter, "/d", "/c", consumer_guard.command, "q5-probe"
        )
    else:
        trap_arguments = (consumer_guard.command, "q5-probe")
    trap_probe = _run(
        trap_arguments,
        cwd=tmp_path,
        environment=consumer_guard.environment(),
    )
    assert trap_probe.returncode == 97
    assert consumer_guard.attempts() == ["q5-probe"]

    artifacts_record = {
        path.name: _sha256(path)
        for path in (*v1_tarballs.values(), v2_native_tarball, runtime_tarball)
    }
    _flush_trace(
        {
            "installed": installed_identity,
            "wheel": {"path": str(wheel), "sha256": _sha256(wheel)},
            "sdist": {"path": str(sdist), "sha256": _sha256(sdist)},
            "tarballs": artifacts_record,
            "generated_v1": generated_v1,
            "generated_v2": v2_generated,
            "runtime_payload": runtime_payload,
            "runtime_inventory": runtime_inventory,
            "immutable_before_v1": before_v1,
            "immutable_before_v2": before_v2,
            "native_v1": native_v1,
            "native_v2": native_v2,
            "jvm_output": jvm_output,
            "javascript_output": js_output,
            "runtime_output": runtime_output,
            "publisher_unavailable": str(publisher_unavailable),
            "consumer_generator": {
                "trap": str(consumer_guard.command),
                "consumer_subprocesses": len(guarded_commands),
                "attempts_during_consumption": [],
                "probe_returncode": trap_probe.returncode,
                "probe_attempts": consumer_guard.attempts(),
            },
            "consumer_ksp": "not invoked",
        }
    )
