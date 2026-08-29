from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from supernote_module_generator.conversion import DEFAULT_CONVERSION_LIMITS
from supernote_module_generator.conversion_codegen import (
    render_cpp_conversion_kernel,
    render_jvm_conversion_kernel,
)
from supernote_module_generator.binding_codegen import render_v4_feature_jsi
from supernote_module_generator.feature_model import PluginRuntimeRegistry
from supernote_module_generator.jvm_codegen import render_jvm_feature_jsi
from supernote_module_generator.jvm_manifest import JvmSourceManifest
from supernote_module_generator.plugin_runtime_codegen import generated_runtime_files
from supernote_module_generator.semantic import SemanticApi


def test_plugin_runtime_contains_one_cpp_and_jvm_limits_contract():
    registry = PluginRuntimeRegistry.create(
        plugin_id="phase4-kernel",
        generator_version="4.0.0.dev0",
        features=(),
    )
    files = generated_runtime_files(registry)
    cpp = files["include/supernote/conversion.hpp"]
    kotlin = files[
        "src/main/java/supernote/generated/runtime/SupernoteConversionBudget.kt"
    ]
    for value in DEFAULT_CONVERSION_LIMITS.manifest().values():
        assert str(value) in cpp
        assert str(value) in kotlin
    ownership = files["ownership.json"]
    assert "include/supernote/conversion.hpp" in ownership
    assert "SupernoteConversionBudget.kt" in ownership


def test_v4_feature_translation_units_include_shared_conversion_kernel(
    tmp_path: Path,
):
    digest = "a" * 64
    feature_id = "supernote:feature:0123456789abcdef"
    cpp = render_v4_feature_jsi(
        tmp_path,
        module_name="Drawing",
        feature_id=feature_id,
        conversion_digest=digest,
    )
    jvm = render_jvm_feature_jsi(
        JvmSourceManifest(feature_id, "4.0.0.dev0", ()),
        SemanticApi(),
        feature_id=feature_id,
        module_name="Drawing",
        conversion_digest=digest,
    )
    expected = (
        "// Supernote V4 conversion plan SHA-256: " + digest + "\n"
        "#include <supernote/conversion.hpp>\n"
    )
    assert cpp.startswith(expected + "#include <supernote/cpp_objects.hpp>\n")
    assert jvm.startswith(expected)


def test_generated_cpp23_kernel_passes_strict_sanitized_bounded_fuzz(tmp_path: Path):
    compiler = shutil.which("clang++")
    if compiler is None:
        pytest.skip("clang++ is unavailable")
    header = tmp_path / "conversion.hpp"
    source = tmp_path / "harness.cpp"
    binary = tmp_path / "harness"
    header.write_text(render_cpp_conversion_kernel(), encoding="utf-8")
    source.write_text(
        r'''#include "conversion.hpp"

#include <cstdint>
#include <iostream>
#include <string>

int main() {
  using namespace supernote::conversion;
  if (field_path("root", "field") != "root.field") return 1;
  if (index_path("root.items", 17) != "root.items[17]") return 2;

  std::uint64_t state = 0x5A17U;
  for (std::uint64_t round = 0; round < 1000; ++round) {
    Budget budget;
    for (std::uint64_t index = 0; index < 128; ++index) {
      state = state * 6364136223846793005ULL + 1442695040888963407ULL;
      const auto depth = state % Limits::max_depth + 1;
      const auto path = index_path("fuzz", index);
      budget.visit(path, depth);
      budget.check_array_length(path, state % (Limits::max_array_length + 1));
      budget.check_string_bytes(path, state % (Limits::max_string_bytes + 1));
      budget.check_byte_buffer(path, state % (Limits::max_byte_buffer_bytes + 1));
      budget.reserve(path, state % 32);
    }
  }

  AllocationGate gate(2);
  Budget injected(&gate);
  injected.reserve("input.a", 1);
  injected.reserve("input.b", 1);
  try {
    injected.reserve("input.c", 1);
    return 3;
  } catch (const Failure &failure) {
    if (failure.kind() != FailureKind::ALLOCATION ||
        failure.path() != "input.c") return 4;
  }

  try {
    Budget budget;
    budget.reserve("huge", Limits::max_temporary_bytes + 1);
    return 5;
  } catch (const Failure &failure) {
    if (failure.kind() != FailureKind::RANGE || failure.path() != "huge") return 6;
  }
  try {
    Budget budget;
    budget.reserve("counter", static_cast<std::uint64_t>(INT64_MAX));
    budget.reserve("counter", 1);
    return 7;
  } catch (const Failure &failure) {
    if (failure.kind() != FailureKind::RANGE || failure.path() != "counter") return 8;
  }
  try {
    Budget budget;
    budget.check_byte_buffer(
        "oversized-bytes", Limits::max_byte_buffer_bytes + 1);
    return 9;
  } catch (const Failure &failure) {
    if (failure.kind() != FailureKind::RANGE ||
        failure.path() != "oversized-bytes") return 10;
  }
  std::cout << "CPP_CONVERSION_KERNEL_PASS\n";
  return 0;
}
''',
        encoding="utf-8",
    )
    compile_result = subprocess.run(
        [
            compiler,
            "-std=c++23",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            str(source),
            "-o",
            str(binary),
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout
    environment = dict(os.environ)
    environment["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    run_result = subprocess.run(
        [str(binary)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout
    assert "CPP_CONVERSION_KERNEL_PASS" in run_result.stdout


def test_generated_kotlin_kernel_compiles_and_runs_failure_harness(tmp_path: Path):
    kotlinc = shutil.which("kotlinc")
    java = shutil.which("java")
    if kotlinc is None or java is None:
        pytest.skip("Kotlin/JVM compiler is unavailable")
    kernel = tmp_path / "SupernoteConversionBudget.kt"
    harness = tmp_path / "Harness.kt"
    jar = tmp_path / "harness.jar"
    kernel.write_text(render_jvm_conversion_kernel(), encoding="utf-8")
    harness.write_text(
        '''package supernote.generated.runtime

fun main() {
  check(conversionFieldPath("root", "field") == "root.field")
  check(conversionIndexPath("root.items", 17) == "root.items[17]")
  val budget = SupernoteConversionBudget()
  repeat(10000) { index ->
    budget.visit(conversionIndexPath("fuzz", index.toLong()), 2)
    budget.reserve("fuzz", 1)
  }
  val gate = SupernoteAllocationGate(1)
  val injected = SupernoteConversionBudget(gate)
  injected.reserve("input.a", 1)
  try {
    injected.reserve("input.b", 1)
    error("allocation failure was not injected")
  } catch (failure: SupernoteConversionFailure) {
    check(failure.kind == SupernoteConversionFailureKind.ALLOCATION)
    check(failure.path == "input.b")
  }
  println("JVM_CONVERSION_KERNEL_PASS")
}
''',
        encoding="utf-8",
    )
    compile_result = subprocess.run(
        [kotlinc, str(kernel), str(harness), "-include-runtime", "-d", str(jar)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout
    run_result = subprocess.run(
        [java, "-jar", str(jar)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stdout
    assert "JVM_CONVERSION_KERNEL_PASS" in run_result.stdout
