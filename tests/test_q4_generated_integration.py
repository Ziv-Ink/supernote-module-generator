from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional, Union
import zipfile
import zlib

import pytest

from supernote_module_generator.binding_codegen import scan_cpp_semantic_model
from supernote_module_generator.cli import main
from supernote_module_generator.package_integration_codegen import (
    NATIVE_RUNTIME_ABI,
    feature_cmake_target,
    package_registration_symbol,
    render_jvm_package_registration,
    render_package_cmake,
    render_package_registration,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGE = Path(
    os.environ.get("SUPERNOTE_Q5_RUNTIME_ROOT", ROOT / "npm/supernote-runtime")
).resolve()


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
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
    return result.returncode


def _write(path: Path, content: Union[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _build_prebuilt_libraries(root: Path, cmake: str) -> dict[str, bytes]:
    source = root / "source"
    build = root / "build"
    output = root / "output"
    _write(
        source / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
project(q4_prebuilt LANGUAGES C)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY "${Q4_OUTPUT_ROOT}/lib")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${Q4_OUTPUT_ROOT}/lib")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${Q4_OUTPUT_ROOT}/bin")
add_library(q4_imported_static STATIC imported_static.c)
target_compile_definitions(q4_imported_static PRIVATE Q4_STATIC_VALUE=50)
add_library(q4_imported_shared SHARED imported_shared.c)
target_compile_definitions(q4_imported_shared PRIVATE Q4_SHARED_VALUE=70)
""",
    )
    _write(
        source / "imported_static.c",
        "int q4_imported_static_value(void) { return Q4_STATIC_VALUE; }\n",
    )
    _write(
        source / "imported_shared.c",
        """#if defined(_WIN32)
#define Q4_EXPORT __declspec(dllexport)
#else
#define Q4_EXPORT
#endif
Q4_EXPORT int q4_imported_shared_value(void) { return Q4_SHARED_VALUE; }
""",
    )
    configure = [
        cmake,
        "-S",
        str(source),
        "-B",
        str(build),
        f"-DQ4_OUTPUT_ROOT={output}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if shutil.which("ninja") is not None:
        configure.extend(("-G", "Ninja"))
    configured = subprocess.run(
        configure,
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr or configured.stdout
    built = subprocess.run(
        [cmake, "--build", str(build), "--config", "Release", "--verbose"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    if os.name == "nt":
        artifacts = {
            "prebuilt/lib/q4_imported_static.lib": output
            / "lib/q4_imported_static.lib",
            "prebuilt/lib/q4_imported_shared.lib": output
            / "lib/q4_imported_shared.lib",
            "prebuilt/bin/q4_imported_shared.dll": output
            / "bin/q4_imported_shared.dll",
        }
    else:
        shared_suffix = ".dylib" if sys.platform == "darwin" else ".so"
        artifacts = {
            "prebuilt/lib/libq4_imported_static.a": output
            / "lib/libq4_imported_static.a",
            f"prebuilt/lib/libq4_imported_shared{shared_suffix}": output
            / f"lib/libq4_imported_shared{shared_suffix}",
        }
    assert all(path.is_file() for path in artifacts.values())
    return {relative: path.read_bytes() for relative, path in artifacts.items()}


def _package_fixture(
    root: Path,
    *,
    name: str = "@fixture/alpha",
    feature_id: str = "supernote:feature:0123456789abcdef",
    public_name: str = "Alpha",
    namespace: str = "com.fixture.alpha",
    with_backend: bool = True,
    dependency_matrix: bool = False,
    prebuilt_files: Optional[dict[str, bytes]] = None,
    real_libraries: bool = False,
) -> tuple[str, str]:
    target = feature_cmake_target(feature_id)
    suffix = feature_id.removeprefix("supernote:feature:")
    symbol = package_registration_symbol(feature_id)
    registration_header, registration_source = render_package_registration(
        feature_id=feature_id,
        package_name=name,
        public_name=public_name,
    )
    jvm_path, jvm_source = render_jvm_package_registration(
        feature_id=feature_id,
        package_name=name,
        public_name=public_name,
    )
    author_cmake = """cmake_minimum_required(VERSION 3.24)
if(NOT TARGET "${SUPERNOTE_MODULE_TARGET}")
  message(FATAL_ERROR "missing generated feature target")
endif()
target_sources(${SUPERNOTE_MODULE_TARGET} PRIVATE
  "${CMAKE_CURRENT_LIST_DIR}/android/src/main/cpp/feature.cpp")
target_include_directories(${SUPERNOTE_MODULE_TARGET} PUBLIC
  "${CMAKE_CURRENT_LIST_DIR}/android/src/main/cpp")
"""
    if dependency_matrix:
        assert with_backend
        assert prebuilt_files is not None
        author_cmake += """add_library(q4_header_only INTERFACE)
target_include_directories(q4_header_only INTERFACE
  "${CMAKE_CURRENT_LIST_DIR}/header_only/include")
target_compile_definitions(q4_header_only INTERFACE Q4_HEADER_ONLY_PUBLIC=2)
add_subdirectory(file_backend "${CMAKE_CURRENT_BINARY_DIR}/file_backend"
                 EXCLUDE_FROM_ALL)
add_subdirectory(source_shared "${CMAKE_CURRENT_BINARY_DIR}/source_shared"
                 EXCLUDE_FROM_ALL)
add_library(q4_imported_static STATIC IMPORTED GLOBAL)
add_library(q4_imported_shared SHARED IMPORTED GLOBAL)
if(WIN32)
  set_target_properties(q4_imported_static PROPERTIES
    IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/prebuilt/lib/q4_imported_static.lib")
  set_target_properties(q4_imported_shared PROPERTIES
    IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/prebuilt/bin/q4_imported_shared.dll"
    IMPORTED_IMPLIB "${CMAKE_CURRENT_LIST_DIR}/prebuilt/lib/q4_imported_shared.lib")
else()
  set_target_properties(q4_imported_static PROPERTIES
    IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/prebuilt/lib/libq4_imported_static.a")
  set_target_properties(q4_imported_shared PROPERTIES
    IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/prebuilt/lib/libq4_imported_shared${CMAKE_SHARED_LIBRARY_SUFFIX}")
endif()
set_target_properties(q4_imported_static PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_LIST_DIR}/prebuilt/include"
  INTERFACE_COMPILE_DEFINITIONS Q4_IMPORTED_STATIC_PUBLIC=50)
set_target_properties(q4_imported_shared PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_LIST_DIR}/prebuilt/include"
  INTERFACE_COMPILE_DEFINITIONS Q4_IMPORTED_SHARED_PUBLIC=70)
target_link_libraries(${SUPERNOTE_MODULE_TARGET} PUBLIC
  q4_header_only file_backend q4_source_shared
  q4_imported_static q4_imported_shared)
"""
    elif real_libraries:
        assert not with_backend
        author_cmake += """if(NOT IS_DIRECTORY "${Q4_NLOHMANN_ROOT}/include/nlohmann")
  message(FATAL_ERROR "Q4_NLOHMANN_ROOT must name an extracted nlohmann/json release")
endif()
if(NOT EXISTS "${Q4_ZLIB_ROOT}/CMakeLists.txt")
  message(FATAL_ERROR "Q4_ZLIB_ROOT must name an extracted zlib release")
endif()
add_library(q4_nlohmann INTERFACE)
target_include_directories(q4_nlohmann INTERFACE "${Q4_NLOHMANN_ROOT}/include")
set(ZLIB_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(ZLIB_BUILD_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory("${Q4_ZLIB_ROOT}" "${CMAKE_CURRENT_BINARY_DIR}/zlib"
                 EXCLUDE_FROM_ALL)
if(TARGET zlibstatic)
  set_target_properties(zlibstatic PROPERTIES POSITION_INDEPENDENT_CODE ON)
  set(_q4_zlib_target zlibstatic)
elseif(TARGET ZLIB::ZLIB)
  set(_q4_zlib_target ZLIB::ZLIB)
else()
  message(FATAL_ERROR "zlib did not provide zlibstatic or ZLIB::ZLIB")
endif()
target_link_libraries(${SUPERNOTE_MODULE_TARGET} PUBLIC
  q4_nlohmann ${_q4_zlib_target})
"""
    elif with_backend:
        author_cmake += """add_subdirectory(file_backend "${CMAKE_CURRENT_BINARY_DIR}/file_backend"
                 EXCLUDE_FROM_ALL)
target_link_libraries(${SUPERNOTE_MODULE_TARGET} PUBLIC file_backend)
"""
    if dependency_matrix:
        authored_feature_source = f'''#include "feature.hpp"
#include "file_backend.hpp"
#include "q4_header_only.hpp"
#include "q4_imported_shared.h"
#include "q4_imported_static.h"
#include "q4_source_shared.hpp"
int feature_value_{suffix}() noexcept {{
  return file_backend_value() + q4_header_only_value() +
         q4_imported_static_value() + q4_source_shared_value() +
         q4_imported_shared_value();
}}
'''
        generated_feature_source = f'''#include "feature.hpp"
#include "file_backend_support.hpp"
#include "q4_header_only.hpp"
#include "q4_imported_shared.h"
#include "q4_imported_static.h"
#include "q4_source_shared.hpp"
#if Q4_HEADER_ONLY_PUBLIC != 2
#error missing header-only INTERFACE definition
#endif
#if FILE_BACKEND_PUBLIC != 41
#error missing nested STATIC PUBLIC definition
#endif
#if FILE_BACKEND_SUPPORT_PUBLIC != 1
#error missing transitive STATIC PUBLIC definition
#endif
#if Q4_SOURCE_SHARED_PUBLIC != 60
#error missing source-built SHARED PUBLIC definition
#endif
#if Q4_IMPORTED_STATIC_PUBLIC != 50
#error missing IMPORTED STATIC PUBLIC definition
#endif
#if Q4_IMPORTED_SHARED_PUBLIC != 70
#error missing IMPORTED SHARED PUBLIC definition
#endif
extern "C" int generated_adapter_value_{suffix}() {{
  return feature_value_{suffix}() + file_backend_support_value() +
         q4_header_only_value() + q4_imported_static_value() +
         q4_source_shared_value() + q4_imported_shared_value();
}}
'''
    elif real_libraries:
        authored_feature_source = f'''#include "feature.hpp"
#include <nlohmann/json.hpp>
#include <zlib.h>
int feature_value_{suffix}() noexcept {{
  const auto parsed = nlohmann::json::parse(R"({{"value":37}})");
  const Bytef data[] = "q4-real-feature";
  const auto checksum = crc32(crc32(0L, Z_NULL, 0), data, sizeof(data) - 1);
  return parsed.at("value").get<int>() + static_cast<int>(checksum & 0x7fffUL);
}}
'''
        generated_feature_source = f'''#include "feature.hpp"
#include <nlohmann/json.hpp>
#include <zlib.h>
extern "C" int generated_adapter_value_{suffix}() {{
  const auto parsed = nlohmann::json::parse(R"({{"bonus":5}})");
  const Bytef data[] = "q4-real-adapter";
  const auto checksum = crc32(crc32(0L, Z_NULL, 0), data, sizeof(data) - 1);
  return feature_value_{suffix}() + parsed.at("bonus").get<int>() +
         static_cast<int>(checksum & 0x7fffUL);
}}
'''
    elif with_backend:
        authored_feature_source = (
            '#include "feature.hpp"\n#include "file_backend.hpp"\n'
            f"int feature_value_{suffix}() noexcept "
            "{ return file_backend_value() + 1; }\n"
        )
        generated_feature_source = (
            '#include "feature.hpp"\n'
            "#ifndef FILE_BACKEND_PUBLIC\n#error missing PUBLIC definition\n#endif\n"
            f'extern "C" int generated_adapter_value_{suffix}() '
            f"{{ return feature_value_{suffix}(); }}\n"
        )
    else:
        authored_feature_source = (
            '#include "feature.hpp"\n'
            f"int feature_value_{suffix}() noexcept {{ return 5; }}\n"
        )
        generated_feature_source = (
            '#include "feature.hpp"\n'
            "#ifdef FILE_BACKEND_PUBLIC\n#error leaked into module B\n#endif\n"
            f'extern "C" int generated_adapter_value_{suffix}() '
            f"{{ return feature_value_{suffix}(); }}\n"
        )
    generated_feature_source += f'''\n#include <memory>
namespace facebook::jsi {{ class Object; class Runtime; }}
namespace supernote::runtime {{ class FeatureSession; }}
namespace supernote::generated::feature_{suffix} {{
void register_feature(
    facebook::jsi::Runtime &,
    facebook::jsi::Object &,
    const std::shared_ptr<supernote::runtime::FeatureSession> &) {{}}
}}
'''
    files = {
        "package.json": json.dumps(
            {
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
        )
        + "\n",
        ".supernote-module.json": json.dumps(
            {
                "schema_version": "1.0",
                "kind": "supernote_module_feature",
                "feature_id": feature_id,
                "npm_name": name,
                "public_name": public_name,
                "android_namespace": namespace,
                "package_version": "1.2.3",
            }
        )
        + "\n",
        "CMakeLists.txt": author_cmake,
        "android/src/main/cpp/feature.hpp": (
            f"#pragma once\nint feature_value_{suffix}() noexcept;\n"
        ),
        "android/src/main/cpp/feature.cpp": authored_feature_source,
        "android/src/main/cpp/vendor/not_selected.cpp": "#error vendor compiled\n",
        "android/src/main/cpp/tests/not_selected.cpp": "#error tests compiled\n",
        (
            "android/src/main/java/"
            + namespace.replace(".", "/")
            + "/FeatureApi.java"
        ): (
            f"package {namespace};\n"
            "import supernote.generated.annotations.SupernotePluginExport;\n"
            "public final class FeatureApi {\n"
            "  @SupernotePluginExport\n"
            "  public static String greet(String name) { return name; }\n"
            "}\n"
        ),
        ".supernote-generated/index.js": "export default {};\n",
        ".supernote-generated/index.d.ts": (
            "declare const value: object; export default value;\n"
        ),
        ".supernote-generated/android/semantic.json": "{}\n",
        ".supernote-generated/android/conversion.json": "{}\n",
        ".supernote-generated/android/internal.hpp": "#pragma once\n",
        ".supernote-generated/android/internal.cpp": (
            (
                "#ifndef FILE_BACKEND_PUBLIC\n#error missing PUBLIC definition\n#endif\n"
                f'extern "C" int internal_value_{suffix}() '
                "{ return FILE_BACKEND_PUBLIC; }\n"
            )
            if with_backend
            else (
                "#ifdef FILE_BACKEND_PUBLIC\n#error leaked into module B\n#endif\n"
                f'extern "C" int internal_value_{suffix}() {{ return 6; }}\n'
            )
        ),
        ".supernote-generated/android/feature.cpp": generated_feature_source,
        ".supernote-generated/android/package_registration.hpp": registration_header,
        ".supernote-generated/android/package_registration.cpp": registration_source,
        jvm_path: jvm_source,
        ".supernote-generated/android/CMakeLists.txt": render_package_cmake(
            feature_id=feature_id,
            binding_sources=(
                "feature.cpp",
                "internal.cpp",
                "package_registration.cpp",
            ),
        ),
    }
    registration = {
        "schemaVersion": "2.0",
        "kind": "supernote_module_registration",
        "protocol": "2.0",
        "featureId": feature_id,
        "packageName": name,
        "publicName": public_name,
        "androidNamespace": namespace,
        "nativeAbi": NATIVE_RUNTIME_ABI,
        "installers": {
            "native": {
                "source": ".supernote-generated/android/package_registration.cpp",
                "symbol": f"supernote_package_install_native_{suffix}",
            },
            "jvm": None,
        },
        "cmake": ".supernote-generated/android/CMakeLists.txt",
        "cmakeTarget": target,
        "nativeSourceDirs": ["android/src/main/cpp"],
        "jvmSourceDirs": ["android/src/main/java"],
        "bindingSources": [
            ".supernote-generated/android/feature.cpp",
            ".supernote-generated/android/internal.cpp",
            ".supernote-generated/android/package_registration.cpp",
        ],
        "bindingHeaders": [
            ".supernote-generated/android/internal.hpp",
            ".supernote-generated/android/package_registration.hpp",
        ],
        "jvmRegistrationSources": [jvm_path],
        "semantic": ".supernote-generated/android/semantic.json",
        "conversion": ".supernote-generated/android/conversion.json",
    }
    files[".supernote-generated/android/registration.json"] = (
        json.dumps(registration) + "\n"
    )
    if dependency_matrix:
        files.update(
            {
                "header_only/include/q4_header_only.hpp": (
                    "#pragma once\n"
                    "inline constexpr int q4_header_only_value() noexcept "
                    "{ return Q4_HEADER_ONLY_PUBLIC; }\n"
                ),
                "file_backend/CMakeLists.txt": (
                    "add_subdirectory(support "
                    '"${CMAKE_CURRENT_BINARY_DIR}/support" EXCLUDE_FROM_ALL)\n'
                    "add_library(file_backend STATIC backend.cpp)\n"
                    "set_target_properties(file_backend PROPERTIES "
                    "POSITION_INDEPENDENT_CODE ON)\n"
                    "target_include_directories(file_backend PUBLIC "
                    '"${CMAKE_CURRENT_LIST_DIR}/include")\n'
                    "target_compile_definitions(file_backend PUBLIC "
                    "FILE_BACKEND_PUBLIC=41)\n"
                    "target_link_libraries(file_backend PUBLIC "
                    "file_backend_support)\n"
                ),
                "file_backend/include/file_backend.hpp": (
                    "#pragma once\nint file_backend_value() noexcept;\n"
                ),
                "file_backend/backend.cpp": (
                    '#include "file_backend.hpp"\n'
                    '#include "file_backend_support.hpp"\n'
                    "int file_backend_value() noexcept "
                    "{ return FILE_BACKEND_PUBLIC + "
                    "file_backend_support_value(); }\n"
                ),
                "file_backend/support/CMakeLists.txt": (
                    "add_library(file_backend_support STATIC support.cpp)\n"
                    "set_target_properties(file_backend_support PROPERTIES "
                    "POSITION_INDEPENDENT_CODE ON)\n"
                    "target_include_directories(file_backend_support PUBLIC "
                    '"${CMAKE_CURRENT_LIST_DIR}/include")\n'
                    "target_compile_definitions(file_backend_support PUBLIC "
                    "FILE_BACKEND_SUPPORT_PUBLIC=1)\n"
                ),
                "file_backend/support/include/file_backend_support.hpp": (
                    "#pragma once\nint file_backend_support_value() noexcept;\n"
                ),
                "file_backend/support/support.cpp": (
                    '#include "file_backend_support.hpp"\n'
                    "int file_backend_support_value() noexcept "
                    "{ return FILE_BACKEND_SUPPORT_PUBLIC; }\n"
                ),
                "source_shared/CMakeLists.txt": (
                    "add_library(q4_source_shared SHARED source_shared.cpp)\n"
                    "set_target_properties(q4_source_shared PROPERTIES "
                    "POSITION_INDEPENDENT_CODE ON)\n"
                    "target_include_directories(q4_source_shared PUBLIC "
                    '"${CMAKE_CURRENT_LIST_DIR}/include")\n'
                    "target_compile_definitions(q4_source_shared PUBLIC "
                    "Q4_SOURCE_SHARED_PUBLIC=60)\n"
                ),
                "source_shared/include/q4_source_shared.hpp": (
                    "#pragma once\n"
                    "#if defined(_WIN32)\n"
                    "#if defined(q4_source_shared_EXPORTS)\n"
                    "#define Q4_SOURCE_SHARED_API __declspec(dllexport)\n"
                    "#else\n"
                    "#define Q4_SOURCE_SHARED_API __declspec(dllimport)\n"
                    "#endif\n"
                    "#else\n"
                    "#define Q4_SOURCE_SHARED_API\n"
                    "#endif\n"
                    "Q4_SOURCE_SHARED_API int q4_source_shared_value() noexcept;\n"
                ),
                "source_shared/source_shared.cpp": (
                    '#include "q4_source_shared.hpp"\n'
                    "int q4_source_shared_value() noexcept "
                    "{ return Q4_SOURCE_SHARED_PUBLIC; }\n"
                ),
                "prebuilt/include/q4_imported_static.h": (
                    "#pragma once\n#ifdef __cplusplus\nextern \"C\" {\n#endif\n"
                    "int q4_imported_static_value(void);\n"
                    "#ifdef __cplusplus\n}\n#endif\n"
                ),
                "prebuilt/include/q4_imported_shared.h": (
                    "#pragma once\n"
                    "#if defined(_WIN32)\n#define Q4_SHARED_IMPORT "
                    "__declspec(dllimport)\n#else\n#define Q4_SHARED_IMPORT\n#endif\n"
                    "#ifdef __cplusplus\nextern \"C\" {\n#endif\n"
                    "Q4_SHARED_IMPORT int q4_imported_shared_value(void);\n"
                    "#ifdef __cplusplus\n}\n#endif\n"
                ),
            }
        )
        assert prebuilt_files is not None
        files.update(prebuilt_files)
    elif with_backend:
        files.update(
            {
                "file_backend/CMakeLists.txt": (
                    "add_library(file_backend STATIC backend.cpp)\n"
                    "set_target_properties(file_backend PROPERTIES "
                    "POSITION_INDEPENDENT_CODE ON)\n"
                    "target_include_directories(file_backend PUBLIC "
                    '"${CMAKE_CURRENT_LIST_DIR}/include")\n'
                    "target_compile_definitions(file_backend PUBLIC "
                    "FILE_BACKEND_PUBLIC=41)\n"
                ),
                "file_backend/include/file_backend.hpp": (
                    "#pragma once\nint file_backend_value() noexcept;\n"
                ),
                "file_backend/backend.cpp": (
                    '#include "file_backend.hpp"\n'
                    "int file_backend_value() noexcept "
                    "{ return FILE_BACKEND_PUBLIC; }\n"
                ),
            }
        )
    for relative, content in files.items():
        _write(root / relative, content)
    payload = [
        {
            "path": relative,
            "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
        }
        for relative in sorted(files)
    ]
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
    _write(
        root / ".supernote-generated/package-manifest.json",
        json.dumps(manifest) + "\n",
    )
    return target, symbol


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mode,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_runtime_annotation_sources_match_publisher_bootstrap_templates() -> None:
    template_root = ROOT / "src/supernote_module_generator/templates"
    runtime_root = RUNTIME_PACKAGE / "jvm/supernote/generated/annotations"
    names = (
        "SupernoteConstructor",
        "SupernotePluginAsync",
        "SupernotePluginExport",
        "SupernotePluginInternal",
        "SupernotePluginObject",
        "SupernotePluginValue",
    )
    for name in names:
        assert (runtime_root / f"{name}.java").read_text() == (
            template_root / f"runtime.{name}.java.tmpl"
        ).read_text()


def test_marker_discovery_ignores_unmarked_vendor_and_tests(tmp_path: Path) -> None:
    native = tmp_path / "android/src/main/cpp"
    _write(
        native / "root.cpp",
        "#include <cstdint>\n// @SupernotePluginExport\n"
        "std::int32_t root_value() { return 1; }\n",
    )
    _write(
        native / "nested/api.cpp",
        "#include <cstdint>\n// @SupernotePluginExport\n"
        "std::int32_t nested_value() { return 2; }\n",
    )
    _write(native / "vendor/binary.cpp", b"\xff\xfe\x00not source")
    _write(native / "tests/poison.cpp", "#error must not be parsed or compiled\n")

    semantic = scan_cpp_semantic_model(tmp_path, module_name="Fixture")

    assert [item.name for item in semantic.functions] == [
        "nested_value",
        "root_value",
    ]


def test_real_publisher_package_executes_ksp_adapter_without_consumer_generation(
    tmp_path: Path,
) -> None:
    gradle = os.environ.get("SUPERNOTE_GRADLE_COMMAND")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if gradle is None or not all((npm, node)):
        pytest.skip(
            "Gradle, npm, and Node are required for the real KSP acceptance"
        )
    root = tmp_path / "publisher"
    _write(root / "PluginConfig.json", "{}\n")
    _write(root / "package.json", '{"name":"q4-publisher","dependencies":{}}\n')
    _write(root / "android/settings.gradle", "include ':app'\n")
    _write(root / "android/app/build.gradle", "plugins {}\n")

    code = _public_cli(
        [
            "add",
            "@fixture/jvm",
            "--starter",
            "kotlin",
            "--javascript-name",
            "Jvm",
            "--android-namespace",
            "com.fixture.jvm",
            "--yes",
        ],
        cwd=root,
    )

    assert code == 0
    feature = root / "local_modules/@fixture/jvm"
    generated_adapter = (
        root
        / "android/.supernote-module/runtime/analysis/subject/build/generated/ksp/"
        "main/kotlin/supernote/generated/adapters/"
        "FeatureAdapters_8d2cc7da67e8c7858c62.kt"
    )
    packaged_adapter = feature / ".supernote-generated/android/jvm/KspAdapters.kt"
    bridge = feature / ".supernote-generated/android/jvm_feature.cpp"
    assert packaged_adapter.read_bytes() == generated_adapter.read_bytes()
    assert '#include "internal.hpp"' in bridge.read_text()
    assert (
        "#include <supernote/4f3f29f8d7c2e2fa/internal.hpp>"
        not in bridge.read_text()
    )
    registration = json.loads(
        (feature / ".supernote-generated/android/registration.json").read_text()
    )
    assert registration["jvmSourceDirs"] == ["android/src/main/java"]
    assert ".supernote-generated/android/jvm_feature.cpp" in registration[
        "bindingSources"
    ]
    assert registration["jvmRegistrationSources"] == [
        ".supernote-generated/android/jvm/JvmRegistration.java",
        ".supernote-generated/android/jvm/KspAdapters.kt",
    ]
    distribution = json.loads(
        (feature / ".supernote-generated/package-manifest.json").read_text()
    )
    payload = {row["path"]: row["sha256"] for row in distribution["payload"]}
    assert payload[".supernote-generated/android/jvm/KspAdapters.kt"] == (
        hashlib.sha256(packaged_adapter.read_bytes()).hexdigest()
    )
    assert not (root / "android/gradlew").exists()

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    def pack(package_root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--json", "--pack-destination", str(artifacts)],
            cwd=package_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        return artifacts / json.loads(result.stdout)[0]["filename"]

    module_tarball = pack(feature)
    runtime_tarball = pack(RUNTIME_PACKAGE)
    expected_adapter = packaged_adapter.read_bytes()
    shutil.move(str(root), str(tmp_path / "publisher-unavailable"))

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _write(consumer / "package.json", '{"name":"q4-real-consumer","private":true}\n')
    installed = subprocess.run(
        [
            npm,
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(module_tarball),
            str(runtime_tarball),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    module = consumer / "node_modules/@fixture/jvm"
    consumer_adapter = module / ".supernote-generated/android/jvm/KspAdapters.kt"
    assert consumer_adapter.read_bytes() == expected_adapter
    inventory_result = subprocess.run(
        [
            node,
            str(consumer / "node_modules/@supernote/runtime/resolve-packages.js"),
            str(consumer),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert inventory_result.returncode == 0, inventory_result.stderr
    inventory = json.loads(inventory_result.stdout)
    assert inventory["modules"][0]["jvmRegistrationSources"][-1] == str(
        consumer_adapter.resolve()
    )

    _write(
        consumer / "android/settings.gradle",
        "rootProject.name = 'q4-real-consumer'\n",
    )
    _write(
        consumer / "android/build.gradle",
        """plugins { id 'org.jetbrains.kotlin.jvm' version '2.0.21' }
repositories { mavenCentral() }
apply from: file('../node_modules/@supernote/runtime/gradle/supernote-modules.gradle')
kotlin { jvmToolchain(17) }
tasks.register('runHarness', JavaExec) {
  classpath = sourceSets.main.runtimeClasspath
  mainClass = 'JvmHarnessKt'
}
""",
    )
    _write(
        consumer / "android/src/main/kotlin/com/facebook/react/bridge/ReactApplicationContext.kt",
        "package com.facebook.react.bridge\nopen class ReactApplicationContext\n",
    )
    _write(
        consumer / "android/src/main/kotlin/supernote/generated/runtime/SupernoteCoroutineBridge.kt",
        "package supernote.generated.runtime\nobject SupernoteCoroutineBridge\n",
    )
    _write(
        consumer / "android/src/main/kotlin/JvmHarness.kt",
        """import supernote.generated.adapters.Adapter_8e8e9439b38e96c2c878
fun main() {
  val result = Adapter_8e8e9439b38e96c2c878.invoke("Consumer".encodeToByteArray())
  println("Q4_PRODUCTION_JVM_OK=" + result.decodeToString())
}
""",
    )
    before = _tree_state(module)
    built = subprocess.run(
        [
            gradle,
            "--no-daemon",
            "--console=plain",
            "-p",
            str(consumer / "android"),
            "classes",
            "runHarness",
            "tasks",
            "--all",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    output = built.stdout + built.stderr
    assert "Q4_PRODUCTION_JVM_OK=Hello, Consumer" in output
    assert "kspKotlin" not in output
    assert "sn-module-gen" not in output
    assert _tree_state(module) == before


def test_real_publisher_last_jvm_export_removal_resolves_without_stale_adapter(
    tmp_path: Path,
) -> None:
    gradle = os.environ.get("SUPERNOTE_GRADLE_COMMAND")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if gradle is None or not all((npm, node)):
        pytest.skip(
            "Gradle, npm, and Node are required for the real KSP lifecycle acceptance"
        )
    publisher = tmp_path / "publisher"
    _write(publisher / "PluginConfig.json", "{}\n")
    _write(
        publisher / "package.json",
        '{"name":"q4-empty-jvm-publisher","dependencies":{}}\n',
    )
    _write(publisher / "android/settings.gradle", "include ':app'\n")
    _write(publisher / "android/app/build.gradle", "plugins {}\n")
    assert _public_cli(
        [
            "add",
            "@fixture/jvm",
            "--starter",
            "kotlin",
            "--javascript-name",
            "Jvm",
            "--android-namespace",
            "com.fixture.jvm",
            "--yes",
        ],
        cwd=publisher,
    ) == 0

    feature = publisher / "local_modules/@fixture/jvm"
    source = feature / "android/src/main/java/com/fixture/jvm/FeatureApi.kt"
    unexported_source = "package com.fixture.jvm\nfun helper() = 1\n"
    source.write_text(unexported_source, encoding="utf-8")
    assert _public_cli(["update"], cwd=publisher) == 0

    bridge_relative = ".supernote-generated/android/jvm_feature.cpp"
    adapter_relative = ".supernote-generated/android/jvm/KspAdapters.kt"
    registration_relative = ".supernote-generated/android/jvm/JvmRegistration.java"
    assert source.read_text(encoding="utf-8") == unexported_source
    assert not (feature / bridge_relative).exists()
    assert not (feature / adapter_relative).exists()
    registration = json.loads(
        (feature / ".supernote-generated/android/registration.json").read_text()
    )
    assert bridge_relative not in registration["bindingSources"]
    assert registration["jvmRegistrationSources"] == [registration_relative]
    assert registration["jvmSourceDirs"] == ["android/src/main/java"]
    distribution = json.loads(
        (feature / ".supernote-generated/package-manifest.json").read_text()
    )
    payload_paths = {row["path"] for row in distribution["payload"]}
    assert bridge_relative not in payload_paths
    assert adapter_relative not in payload_paths
    assert registration_relative in payload_paths

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    def pack(package_root: Path) -> Path:
        result = subprocess.run(
            [npm, "pack", "--json", "--pack-destination", str(artifacts)],
            cwd=package_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        return artifacts / json.loads(result.stdout)[0]["filename"]

    module_tarball = pack(feature)
    runtime_tarball = pack(RUNTIME_PACKAGE)
    consumer = tmp_path / "unrelated-consumer"
    consumer.mkdir()
    _write(
        consumer / "package.json",
        '{"name":"q4-empty-jvm-consumer","private":true}\n',
    )
    installed = subprocess.run(
        [
            npm,
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(module_tarball),
            str(runtime_tarball),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    installed_feature = consumer / "node_modules/@fixture/jvm"
    assert (
        installed_feature
        / "android/src/main/java/com/fixture/jvm/FeatureApi.kt"
    ).read_text(encoding="utf-8") == unexported_source
    before = _tree_state(installed_feature)
    resolved = subprocess.run(
        [
            node,
            str(consumer / "node_modules/@supernote/runtime/resolve-packages.js"),
            str(consumer),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    inventory = json.loads(resolved.stdout)
    assert inventory["modules"][0]["jvmRegistrationSources"] == [
        str((installed_feature / registration_relative).resolve())
    ]
    assert not (installed_feature / bridge_relative).exists()
    assert not (installed_feature / adapter_relative).exists()
    assert _tree_state(installed_feature) == before


def test_real_generated_jvm_bridge_compiles_with_package_cmake_when_configured(
    tmp_path: Path,
) -> None:
    gradle = os.environ.get("SUPERNOTE_GRADLE_COMMAND")
    react_aar_value = os.environ.get("SUPERNOTE_Q4_REACT_ANDROID_AAR")
    cmake = shutil.which("cmake")
    cxx = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    javac = shutil.which("javac")
    java = shutil.which("java")
    npm = shutil.which("npm")
    if not all((gradle, react_aar_value, cmake, cxx, javac, java, npm)):
        pytest.skip(
            "Gradle, React Android AAR, CMake, C++, Java, and npm are required"
        )
    react_aar = Path(react_aar_value).resolve()
    assert react_aar.is_file()
    publisher = tmp_path / "publisher"
    _write(publisher / "PluginConfig.json", "{}\n")
    _write(publisher / "package.json", '{"name":"q4-native-publisher"}\n')
    _write(publisher / "android/settings.gradle", "include ':app'\n")
    _write(publisher / "android/app/build.gradle", "plugins {}\n")
    assert _public_cli(
        [
            "add",
            "@fixture/jvm",
            "--starter",
            "kotlin",
            "--javascript-name",
            "Jvm",
            "--android-namespace",
            "com.fixture.jvm",
            "--yes",
        ],
        cwd=publisher,
    ) == 0
    publisher_feature = publisher / "local_modules/@fixture/jvm"
    expected_bridge = (
        publisher_feature / ".supernote-generated/android/jvm_feature.cpp"
    ).read_bytes()
    runtime = tmp_path / "native-runtime-contract"
    shutil.copytree(
        publisher / "android/.supernote-module/runtime/src", runtime / "src"
    )
    shutil.copytree(
        publisher / "android/.supernote-module/runtime/include", runtime / "include"
    )
    artifacts = tmp_path / "native-artifacts"
    artifacts.mkdir()
    packed = subprocess.run(
        [npm, "pack", "--json", "--pack-destination", str(artifacts)],
        cwd=publisher_feature,
        capture_output=True,
        text=True,
        check=False,
    )
    assert packed.returncode == 0, packed.stderr or packed.stdout
    tarball = artifacts / json.loads(packed.stdout)[0]["filename"]
    consumer = tmp_path / "native-consumer"
    consumer.mkdir()
    _write(consumer / "package.json", '{"name":"q4-native-consumer","private":true}\n')
    installed = subprocess.run(
        [
            npm,
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(tarball),
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    feature = consumer / "node_modules/@fixture/jvm"
    bridge = feature / ".supernote-generated/android/jvm_feature.cpp"
    assert bridge.read_bytes() == expected_bridge
    shutil.move(str(publisher), str(tmp_path / "native-publisher-unavailable"))

    react = tmp_path / "react-android"
    with zipfile.ZipFile(react_aar) as archive:
        for member in archive.namelist():
            if member.startswith("prefab/modules/jsi/include/"):
                archive.extract(member, react)
    jsi_include = react / "prefab/modules/jsi/include"
    java_settings = subprocess.run(
        [str(java), "-XshowSettings:properties", "-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    java_home_line = next(
        line for line in java_settings.stderr.splitlines() if "java.home =" in line
    )
    java_home = Path(java_home_line.split("=", 1)[1].strip()).resolve()
    java_platform = {"darwin": "darwin", "linux": "linux"}.get(
        sys.platform, "win32"
    )
    stub = tmp_path / "native-stub"
    _write(
        stub / "android/log.h",
        "#pragma once\n#define ANDROID_LOG_ERROR 6\n"
        "inline int __android_log_print(int, const char *, const char *, ...) "
        "{ return 0; }\n",
    )
    _write(
        tmp_path / "main.cpp",
        """#include <cstring>
#include "package_registration.hpp"
extern "C" const SupernotePackageRegistration *
supernote_package_registration_4f3f29f8d7c2e2fa() noexcept;
int main() {
  const auto *value = supernote_package_registration_4f3f29f8d7c2e2fa();
  return std::strcmp(value->feature_id,
                     "supernote:feature:4f3f29f8d7c2e2fa") == 0 ? 0 : 1;
}
""",
    )
    _write(
        tmp_path / "CMakeLists.txt",
        f"""cmake_minimum_required(VERSION 3.24)
project(q4_production_jvm LANGUAGES C CXX)
add_library(runtime_compile_contract INTERFACE)
target_compile_features(runtime_compile_contract INTERFACE c_std_23 cxx_std_23)
target_include_directories(runtime_compile_contract INTERFACE
  "{stub.as_posix()}"
  "{jsi_include.as_posix()}"
  "{java_home.as_posix()}/include"
  "{java_home.as_posix()}/include/{java_platform}"
  "{(runtime / 'src').as_posix()}"
  "{(runtime / 'include').as_posix()}")
set(SUPERNOTE_RUNTIME_COMPILE_TARGET runtime_compile_contract)
set(SUPERNOTE_RUNTIME_NATIVE_ABI {NATIVE_RUNTIME_ABI})
add_subdirectory("{(feature / '.supernote-generated/android').as_posix()}" feature)
add_executable(q4_production_jvm main.cpp)
target_include_directories(q4_production_jvm PRIVATE
  "{(feature / '.supernote-generated/android').as_posix()}")
target_link_libraries(q4_production_jvm PRIVATE
  supernote_feature_4f3f29f8d7c2e2fa)
""",
    )
    build = tmp_path / "build"
    configured = subprocess.run(
        [
            str(cmake),
            "-S",
            str(tmp_path),
            "-B",
            str(build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr or configured.stdout
    built = subprocess.run(
        [str(cmake), "--build", str(build), "--verbose"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CXX": str(cxx)},
    )
    assert built.returncode == 0, built.stderr or built.stdout
    commands = json.loads((build / "compile_commands.json").read_text())
    assert bridge.resolve() in {Path(row["file"]).resolve() for row in commands}
    executable = build / ("q4_production_jvm.exe" if os.name == "nt" else "q4_production_jvm")
    assert subprocess.run([str(executable)], check=False).returncode == 0


def test_real_gradle_consumer_compiles_registration_without_ksp_when_configured(
    tmp_path: Path,
) -> None:
    gradle = os.environ.get("SUPERNOTE_GRADLE_COMMAND")
    if gradle is None:
        pytest.skip("set SUPERNOTE_GRADLE_COMMAND for Gradle consumer acceptance")
    consumer = tmp_path / "consumer"
    module = consumer / "node_modules/@fixture/alpha"
    runtime = consumer / "node_modules/@supernote/runtime"
    _package_fixture(module)
    shutil.copytree(RUNTIME_PACKAGE, runtime)
    _write(
        consumer / "package.json",
        json.dumps(
            {
                "name": "q4-gradle-consumer",
                "dependencies": {
                    "@fixture/alpha": "1.2.3",
                    "@supernote/runtime": "0.1.0",
                },
            }
        )
        + "\n",
    )
    _write(consumer / "android/settings.gradle", "rootProject.name = 'q4-consumer'\n")
    _write(
        consumer / "android/build.gradle",
        """plugins { id 'java' }
apply from: file('../node_modules/@supernote/runtime/gradle/supernote-modules.gradle')
""",
    )
    _write(
        consumer / "android/src/main/java/JvmConsumer.java",
        """import supernote.generated.packages.feature_0123456789abcdef.JvmRegistration;
public final class JvmConsumer {
  public static String identity() { return JvmRegistration.featureId(); }
}
""",
    )
    before = _tree_state(module)

    built = subprocess.run(
        [
            gradle,
            "--no-daemon",
            "--console=plain",
            "--offline",
            "-p",
            str(consumer / "android"),
            "classes",
            "tasks",
            "--all",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert built.returncode == 0, built.stderr or built.stdout
    assert (consumer / "android/build/classes/java/main/JvmConsumer.class").is_file()
    assert (
        consumer / "android/build/classes/java/main/com/fixture/alpha/FeatureApi.class"
    ).is_file()
    assert "kspKotlin" not in built.stdout
    assert "sn-module-gen" not in built.stdout + built.stderr
    assert _tree_state(module) == before


@pytest.mark.skipif(shutil.which("cmake") is None, reason="CMake is required")
def test_immutable_installed_package_cmake_and_jvm_execute(
    tmp_path: Path,
) -> None:
    cmake = shutil.which("cmake") or "cmake"
    cxx = (
        os.environ.get("CXX")
        or shutil.which("c++")
        or shutil.which("clang++")
    )
    javac = shutil.which("javac")
    java = shutil.which("java")
    node = shutil.which("node")
    if not all((cxx, javac, java, node)):
        pytest.skip("C++, Java, and Node toolchains are required")
    consumer = tmp_path / "consumer"
    module = consumer / "node_modules/@fixture/alpha"
    module_b = consumer / "node_modules/@fixture/beta"
    runtime = consumer / "node_modules/@supernote/runtime"
    prebuilt_files = _build_prebuilt_libraries(
        tmp_path / "prebuilt-toolchain", cmake
    )
    target, symbol = _package_fixture(
        module,
        dependency_matrix=True,
        prebuilt_files=prebuilt_files,
    )
    target_b, symbol_b = _package_fixture(
        module_b,
        name="@fixture/beta",
        feature_id="supernote:feature:fedcba9876543210",
        public_name="Beta",
        namespace="com.fixture.beta",
        with_backend=False,
    )
    shutil.copytree(RUNTIME_PACKAGE, runtime)
    _write(
        consumer / "package.json",
        json.dumps(
            {
                "name": "q4-consumer",
                "dependencies": {
                    "@fixture/alpha": "1.2.3",
                    "@fixture/beta": "1.2.3",
                    "@supernote/runtime": "0.1.0",
                },
            }
        )
        + "\n",
    )
    _write(
        consumer / "main.cpp",
        f"""#include <cstring>
#ifdef FILE_BACKEND_PUBLIC
#error feature usage requirements leaked into runtime
#endif
struct SupernotePackageRegistration {{
  const char *feature_id; const char *package_name;
  const char *public_name; const char *cmake_target;
}};
extern "C" int generated_adapter_value_0123456789abcdef();
extern "C" int internal_value_0123456789abcdef();
extern "C" int generated_adapter_value_fedcba9876543210();
extern "C" int internal_value_fedcba9876543210();
extern "C" const SupernotePackageRegistration *{symbol}() noexcept;
extern "C" const SupernotePackageRegistration *{symbol_b}() noexcept;
int sibling_value();
int main() {{
  const auto *record = {symbol}();
  const auto *record_b = {symbol_b}();
  return generated_adapter_value_0123456789abcdef() == 407 &&
         internal_value_0123456789abcdef() == 41 &&
         generated_adapter_value_fedcba9876543210() == 5 &&
         internal_value_fedcba9876543210() == 6 && sibling_value() == 7 &&
         std::strcmp(record->cmake_target, "{target}") == 0 &&
         std::strcmp(record_b->cmake_target, "{target_b}") == 0
             ? 0 : 1;
}}
""",
    )
    _write(
        consumer / "sibling.cpp",
        "#ifdef FILE_BACKEND_PUBLIC\n#error leaked into sibling\n#endif\n"
        "int sibling_value() { return 7; }\n",
    )
    _write(
        consumer / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0131 NEW)
project(q4_consumer LANGUAGES C CXX)
add_library(runtime_compile_contract INTERFACE)
target_compile_features(runtime_compile_contract INTERFACE c_std_23 cxx_std_23)
add_executable(q4_consumer main.cpp sibling.cpp)
include("${CMAKE_CURRENT_LIST_DIR}/node_modules/@supernote/runtime/cmake/SupernoteModules.cmake")
supernote_link_modules(q4_consumer runtime_compile_contract
  "${CMAKE_CURRENT_LIST_DIR}/android/build/supernote-composition/inventory.json")
if(WIN32)
  add_custom_command(TARGET q4_consumer POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "$<TARGET_FILE:q4_source_shared>" "$<TARGET_FILE_DIR:q4_consumer>"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "$<TARGET_FILE:q4_imported_shared>" "$<TARGET_FILE_DIR:q4_consumer>")
endif()
""",
    )
    _write(
        consumer / "JvmHarness.java",
        """import supernote.generated.packages.feature_0123456789abcdef.JvmRegistration;
public final class JvmHarness {
  public static void main(String[] args) {
    if (!JvmRegistration.featureId().equals("supernote:feature:0123456789abcdef"))
      throw new AssertionError();
  }
}
""",
    )
    composed = subprocess.run(
        [
            str(node),
            str(runtime / "compose-packages.js"),
            str(consumer),
            str(consumer / "android/build/supernote-composition"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert composed.returncode == 0, composed.stderr or composed.stdout
    before = _tree_state(module)
    before_b = _tree_state(module_b)
    if os.name != "nt":
        for immutable in (module, module_b):
            for path in sorted(immutable.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            immutable.chmod(0o555)
        before = _tree_state(module)
        before_b = _tree_state(module_b)
    environment = dict(os.environ)
    environment["CXX"] = str(cxx)
    for configuration in ("Debug", "Release"):
        build = tmp_path / f"build-{configuration.lower()}"
        configure_command = [
            cmake,
            "-S",
            str(consumer),
            "-B",
            str(build),
        ]
        if shutil.which("ninja") is not None:
            configure_command.extend(("-G", "Ninja"))
        configure_command.extend(
            (
                f"-DCMAKE_BUILD_TYPE={configuration}",
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            )
        )
        configured = subprocess.run(
            configure_command,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert configured.returncode == 0, configured.stderr or configured.stdout
        built = subprocess.run(
            [cmake, "--build", str(build), "--verbose"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert built.returncode == 0, built.stderr or built.stdout
        build_output = built.stdout + built.stderr
        for linked_target in (
            "file_backend",
            "file_backend_support",
            "q4_imported_shared",
            "q4_imported_static",
            "q4_source_shared",
        ):
            assert linked_target in build_output
        executable = build / ("q4_consumer.exe" if os.name == "nt" else "q4_consumer")
        executed = subprocess.run([str(executable)], check=False)
        assert executed.returncode == 0
        commands = json.loads((build / "compile_commands.json").read_text())
        command_for = {
            Path(row["file"]).resolve(): row.get(
                "command", " ".join(row.get("arguments", []))
            )
            for row in commands
        }
        generated_feature = (
            module / ".supernote-generated/android/feature.cpp"
        ).resolve()
        authored_feature = (
            module / "android/src/main/cpp/feature.cpp"
        ).resolve()
        generated_feature_b = (
            module_b / ".supernote-generated/android/feature.cpp"
        ).resolve()
        assert "FILE_BACKEND_PUBLIC=41" in command_for[generated_feature]
        for expected_definition in (
            "FILE_BACKEND_SUPPORT_PUBLIC=1",
            "Q4_HEADER_ONLY_PUBLIC=2",
            "Q4_IMPORTED_SHARED_PUBLIC=70",
            "Q4_IMPORTED_STATIC_PUBLIC=50",
            "Q4_SOURCE_SHARED_PUBLIC=60",
        ):
            assert expected_definition in command_for[generated_feature]
        assert "FILE_BACKEND_PUBLIC=41" in command_for[authored_feature]
        assert "FILE_BACKEND_PUBLIC=41" not in command_for[generated_feature_b]
        assert "FILE_BACKEND_PUBLIC=41" not in command_for[
            (consumer / "main.cpp").resolve()
        ]
        assert "FILE_BACKEND_PUBLIC=41" not in command_for[
            (consumer / "sibling.cpp").resolve()
        ]
        assert "not_selected.cpp" not in "\n".join(row["file"] for row in commands)
        for expected_include in (
            "header_only/include",
            "file_backend/support/include",
            "prebuilt/include",
            "source_shared/include",
        ):
            assert expected_include in command_for[generated_feature]
        if os.name != "nt":
            assert "-fPIC" in command_for[generated_feature]
    java_build = tmp_path / "java-build"
    java_build.mkdir()
    compiled = subprocess.run(
        [
            str(javac),
            "-d",
            str(java_build),
            str(module / ".supernote-generated/android/jvm/JvmRegistration.java"),
            str(consumer / "JvmHarness.java"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    assert subprocess.run(
        [str(java), "-cp", str(java_build), "JvmHarness"], check=False
    ).returncode == 0
    assert _tree_state(module) == before
    assert _tree_state(module_b) == before_b


@pytest.mark.skipif(shutil.which("cmake") is None, reason="CMake is required")
def test_pinned_nlohmann_and_zlib_generated_consumer_execute(
    tmp_path: Path,
) -> None:
    nlohmann_value = os.environ.get("SUPERNOTE_Q4_NLOHMANN_ROOT")
    zlib_value = os.environ.get("SUPERNOTE_Q4_ZLIB_ROOT")
    if nlohmann_value is None or zlib_value is None:
        pytest.skip("set the pinned Q4 nlohmann/json and zlib source roots")
    nlohmann_root = Path(nlohmann_value).resolve()
    zlib_root = Path(zlib_value).resolve()
    assert (nlohmann_root / "include/nlohmann/json.hpp").is_file()
    assert (zlib_root / "CMakeLists.txt").is_file()
    cmake = shutil.which("cmake") or "cmake"
    cxx = os.environ.get("CXX") or shutil.which("c++") or shutil.which("clang++")
    node = shutil.which("node")
    if cxx is None or node is None:
        pytest.skip("C++ and Node toolchains are required")
    consumer = tmp_path / "real-library-consumer"
    module = consumer / "node_modules/@fixture/real-libraries"
    runtime = consumer / "node_modules/@supernote/runtime"
    _package_fixture(
        module,
        name="@fixture/real-libraries",
        public_name="RealLibraries",
        namespace="com.fixture.real_libraries",
        with_backend=False,
        real_libraries=True,
    )
    shutil.copytree(RUNTIME_PACKAGE, runtime)
    _write(
        consumer / "package.json",
        json.dumps(
            {
                "name": "q4-real-library-consumer",
                "dependencies": {
                    "@fixture/real-libraries": "1.2.3",
                    "@supernote/runtime": "0.1.0",
                },
            }
        )
        + "\n",
    )
    expected = (
        37
        + (zlib.crc32(b"q4-real-feature") & 0x7FFF)
        + 5
        + (zlib.crc32(b"q4-real-adapter") & 0x7FFF)
    )
    _write(
        consumer / "main.cpp",
        "extern \"C\" int generated_adapter_value_0123456789abcdef();\n"
        "int main() { return "
        "generated_adapter_value_0123456789abcdef() == "
        f"{expected} ? 0 : 1; }}\n",
    )
    _write(
        consumer / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0131 NEW)
project(q4_real_library_consumer LANGUAGES C CXX)
add_library(runtime_compile_contract INTERFACE)
target_compile_features(runtime_compile_contract INTERFACE c_std_23 cxx_std_23)
add_executable(q4_real_library_consumer main.cpp)
include("${CMAKE_CURRENT_LIST_DIR}/node_modules/@supernote/runtime/cmake/SupernoteModules.cmake")
supernote_link_modules(q4_real_library_consumer runtime_compile_contract
  "${CMAKE_CURRENT_LIST_DIR}/android/build/supernote-composition/inventory.json")
""",
    )
    composed = subprocess.run(
        [
            str(node),
            str(runtime / "compose-packages.js"),
            str(consumer),
            str(consumer / "android/build/supernote-composition"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert composed.returncode == 0, composed.stderr or composed.stdout
    before = _tree_state(module)
    if os.name != "nt":
        for path in sorted(module.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        module.chmod(0o555)
        before = _tree_state(module)
    environment = dict(os.environ)
    environment["CXX"] = str(cxx)
    normalized_nlohmann = (nlohmann_root / "include").as_posix()
    normalized_zlib = zlib_root.as_posix()
    for configuration in ("Debug", "Release"):
        build = tmp_path / f"real-build-{configuration.lower()}"
        configure = [
            cmake,
            "-S",
            str(consumer),
            "-B",
            str(build),
            f"-DCMAKE_BUILD_TYPE={configuration}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DQ4_NLOHMANN_ROOT={nlohmann_root}",
            f"-DQ4_ZLIB_ROOT={zlib_root}",
        ]
        if shutil.which("ninja") is not None:
            configure.extend(("-G", "Ninja"))
        configured = subprocess.run(
            configure,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert configured.returncode == 0, configured.stderr or configured.stdout
        built = subprocess.run(
            [cmake, "--build", str(build), "--verbose"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert built.returncode == 0, built.stderr or built.stdout
        assert "zlibstatic" in built.stdout + built.stderr
        executable = build / (
            "q4_real_library_consumer.exe"
            if os.name == "nt"
            else "q4_real_library_consumer"
        )
        assert subprocess.run([str(executable)], check=False).returncode == 0
        commands = json.loads((build / "compile_commands.json").read_text())
        generated_source = (
            module / ".supernote-generated/android/feature.cpp"
        ).resolve()
        generated_command = next(
            row.get("command", " ".join(row.get("arguments", [])))
            for row in commands
            if Path(row["file"]).resolve() == generated_source
        ).replace("\\", "/")
        assert normalized_nlohmann in generated_command
        assert normalized_zlib in generated_command
        assert any(
            Path(row["file"]).resolve().is_relative_to(zlib_root)
            for row in commands
        )
    assert _tree_state(module) == before


def test_package_cmake_rejects_wrong_abi_and_target_collision(tmp_path: Path) -> None:
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("CMake is required")
    package = tmp_path / "package"
    target, _symbol = _package_fixture(package)
    _write(
        tmp_path / "CMakeLists.txt",
        """cmake_minimum_required(VERSION 3.24)
project(q4_negative LANGUAGES C CXX)
add_library(runtime_contract INTERFACE)
set(SUPERNOTE_RUNTIME_COMPILE_TARGET runtime_contract)
set(SUPERNOTE_RUNTIME_NATIVE_ABI wrong-abi)
add_subdirectory(package/.supernote-generated/android package-build)
""",
    )
    wrong_abi = subprocess.run(
        [cmake, "-S", str(tmp_path), "-B", str(tmp_path / "bad-abi")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong_abi.returncode != 0
    assert "native ABI mismatch" in wrong_abi.stderr + wrong_abi.stdout
    source = (tmp_path / "CMakeLists.txt").read_text().replace(
        "set(SUPERNOTE_RUNTIME_NATIVE_ABI wrong-abi)",
        f"set(SUPERNOTE_RUNTIME_NATIVE_ABI {NATIVE_RUNTIME_ABI})\n"
        f"add_library({target} STATIC collision.cpp)",
    )
    _write(tmp_path / "collision.cpp", "int collision() { return 0; }\n")
    _write(tmp_path / "CMakeLists.txt", source)
    collision = subprocess.run(
        [cmake, "-S", str(tmp_path), "-B", str(tmp_path / "collision-build")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collision.returncode != 0
    assert "feature target collision" in (collision.stderr + collision.stdout).lower()
