import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

from supernote_module_generator.feature_model import (
    FeatureManifest,
    FeatureRegistryEntry,
    PluginRuntimeRegistry,
)
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.transaction import Transaction
from supernote_module_generator.jvm_manifest import (
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_owner_identity,
)
from supernote_module_generator.plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    activate_plugin_runtime,
    generate_plugin_runtime,
    restore_plugin_runtime,
    stage_plugin_runtime,
)
from supernote_module_generator.semantic import SemanticApi
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


def entry(name: str) -> FeatureRegistryEntry:
    feature = FeatureManifest.create(
        npm_name=f"@local/{name}",
        public_name=name.title(),
        android_namespace=f"com.example.{name}",
    )
    return FeatureRegistryEntry.create(feature, SemanticApi())


def registry(*names: str) -> PluginRuntimeRegistry:
    return PluginRuntimeRegistry.create(
        plugin_id="com.example.plugin",
        generator_version="4.0.0.dev0",
        features=(entry(name) for name in names),
    )


def host_cxx_compiler():
    candidates = [
        os.environ.get("CXX"),
        shutil.which("c++"),
        shutil.which("clang++"),
    ]
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(str(Path(program_files) / "LLVM/bin/clang++.exe"))
    return next(
        (
            str(Path(candidate))
            for candidate in candidates
            if candidate and Path(candidate).is_file()
        ),
        None,
    )


def test_ksp_feature_roots_use_one_compiler_option_per_feature(tmp_path: Path):
    for feature_count in (0, 1, 2, 32):
        generated = generate_plugin_runtime(
            tmp_path / str(feature_count),
            registry(*(f"feature{index}" for index in range(feature_count))),
        )
        gradle = (generated / "build.gradle").read_text()
        root_options = [
            line.strip()
            for line in gradle.splitlines()
            if line.strip().startswith("arg('supernoteFeatureRoot_")
        ]

        assert len(root_options) == feature_count
        assert "supernoteFeatureRoots" not in gradle
        for index, option in enumerate(root_options):
            assert option.startswith(
                f"arg('supernoteFeatureRoot_{index:08d}', "
            )
            assert "\\tlocal_modules/" in option

        cmake = (generated / "CMakeLists.txt").read_text()
        assert '"${CMAKE_CURRENT_LIST_DIR}/../../../local_modules"' in cmake


def test_generated_runtime_does_not_vendor_the_python_compiler(
    tmp_path: Path,
):
    generated = generate_plugin_runtime(tmp_path, registry("jvm"))
    gradle = (generated / "build.gradle").read_text()

    assert not (generated / "common_codegen.py").exists()
    assert not (generated / "common_support").exists()
    assert "checkSupernote${buildVariant}State" in gradle
    assert "workingDir supernotePluginRoot" in gradle
    assert "'check'" in gradle
    assert "'--build-hook'" in gradle
    assert "outputs.files" not in gradle


def test_generates_one_compiled_runtime_component_for_all_features(tmp_path: Path):
    runtime_registry = registry("alpha", "beta")
    generated = generate_plugin_runtime(tmp_path, runtime_registry)
    component = runtime_registry.component_name
    registration_component = f"{component}_registration"
    cmake = (generated / "CMakeLists.txt").read_text()
    services = (generated / "src/runtime_services.cpp").read_text()
    services_header = (generated / "src/runtime_services.hpp").read_text()
    public_header = (generated / "include/supernote/runtime.hpp").read_text()
    source = (generated / "src/feature_registry.cpp").read_text()
    gradle = (generated / "build.gradle").read_text()
    consumer_rules = (generated / "consumer-rules.pro").read_text()
    processor = (
        generated
        / "processor/src/main/kotlin/supernote/generated/processor/SupernoteV4Processor.kt"
    ).read_text()
    bootstrap = (generated / "src/runtime_bootstrap.cpp").read_text()
    registration_bridge = (
        generated / "src/runtime_registration_bridge.c"
    ).read_text()
    module = (
        generated
        / "src/main/java/supernote/generated/runtime/SupernoteV4Module.kt"
    ).read_text()
    coroutine_bridge = (
        generated
        / "src/main/java/supernote/generated/runtime/SupernoteCoroutineBridge.kt"
    ).read_text()

    assert cmake.count(f"add_library({component} SHARED") == 1
    assert cmake.count(f"add_library({registration_component} SHARED") == 1
    assert '"${SUPERNOTE_NATIVE_ROOT}/*.c"' in cmake
    assert "C_STANDARD 23" in cmake
    assert "C_STANDARD_REQUIRED YES" in cmake
    assert "target_compile_features" in cmake and "cxx_std_23" in cmake
    assert "C_VISIBILITY_PRESET hidden" in cmake
    assert "CXX_VISIBILITY_PRESET hidden" in cmake
    assert "VISIBILITY_INLINES_HIDDEN YES" in cmake
    assert 'target_link_options' in cmake
    assert '"-Wl,-Bsymbolic-functions"' in cmake
    assert "if(SUPERNOTE_V4_WEAK_OBJECT_PROBE)" in cmake
    assert "SUPERNOTE_V4_WEAK_OBJECT_PROBE=1" in cmake
    assert "runtime_services.cpp" in cmake
    assert "feature_registry.cpp" in cmake
    assert "local_modules/@local/alpha/android/src/main/cpp" in cmake
    assert "local_modules/@local/beta/android/src/main/cpp" in cmake
    assert "SUPERNOTE_GENERATED_BINDINGS" in cmake
    assert "ReactAndroid::jsi" in cmake
    assert "ReactAndroid::reactnative" in cmake
    assert "find_package(fbjni REQUIRED CONFIG)" in cmake
    assert "fbjni::fbjni" in cmake
    assert "runtime_bootstrap.cpp" in cmake
    assert "runtime_registration_bridge.c" in cmake
    assert services.count("static ProcessServices services") == 1
    assert "void BoundedExecutor::ensure_started()" in services
    assert "DeferredDestruction::DeferredDestruction(std::size_t queue_capacity)" in services
    assert "state->queue.size() >= state->capacity" in services
    assert "DeferredDestruction::high_water_mark()" in services
    assert "DeferredDestruction::oldest_item_age_ms()" in services
    assert "DeferredDestruction::processed_count()" in services
    assert "DeferredDestruction::failure_count()" in services
    assert "ProcessServices::thread_count() const noexcept" in services
    assert "ProcessServices::shutdown() noexcept" in services
    assert "class FeatureCallScope" in services_header
    assert "claim_internal_completion" in services_header
    assert "queue_depth() const noexcept" in services_header
    assert "set_retained_state" in services_header
    assert "thread_local std::weak_ptr<FeatureSession>" in services
    assert "enum class ErrorCode" in public_header
    assert "class Result final" in public_header
    assert "supernote:feature:" in source
    assert '"Alpha"' in source
    assert '"Beta"' in source
    assert gradle.count("com.android.library") == 1
    assert "jniLibs.excludes" in gradle
    assert "org.jspecify:jspecify:1.0.0" in gradle
    assert "consumerProguardFiles 'consumer-rules.pro'" in gradle
    assert "-keep interface com.facebook.react.ReactPackage { *; }" in consumer_rules
    assert "-keep class com.facebook.soloader.SoLoader { *; }" in consumer_rules
    assert "-keep class com.facebook.soloader.SoSource { *; }" in consumer_rules
    assert "-keep class com.facebook.soloader.DirectorySoSource { *; }" in consumer_rules
    assert "-keep class kotlin.** { *; }" in consumer_rules
    assert "-keep class kotlinx.coroutines.** { *; }" in consumer_rules
    assert (
        "-keep,includedescriptorclasses class supernote.generated.runtime.** { *; }"
        in consumer_rules
    )
    assert (
        "-keep,includedescriptorclasses class supernote.generated.adapters.** { *; }"
        in consumer_rules
    )
    assert "-keep class com.example.alpha.** { *; }" in consumer_rules
    assert "-keep class com.example.beta.** { *; }" in consumer_rules
    assert "**/libjsi.so" in gradle
    assert "**/libreactnative.so" in gradle
    assert "local_modules/@local/alpha/android/src/main/java" in gradle
    assert "local_modules/@local/beta/android/src/main/java" in gradle
    assert "supernoteFeatureRoots" not in gradle
    assert "arg('supernoteFeatureRoot_00000000'" in gradle
    assert "arg('supernoteFeatureRoot_00000001'" in gradle
    assert "supernote:feature:" in gradle
    assert "\\tlocal_modules/@local/alpha/android/src/main/java" in gradle
    assert "\\tlocal_modules/@local/beta/android/src/main/java" in gradle
    assert "supernoteNativeRoots.findAll { it.isDirectory() }" in gradle
    assert "gradleProperty('supernoteV4WeakObjectProbe')" in gradle
    assert "-DSUPERNOTE_V4_WEAK_OBJECT_PROBE=" in gradle
    assert "def supernoteIsWindows" in gradle
    assert "'supernote-v4/sn_supernote_runtime_" in gradle
    assert "layout.buildDirectory.set(new File(supernoteWindowsBuildRoot, 'gradle'))" in gradle
    assert "new File(supernoteWindowsBuildRoot, 'cxx')" in gradle
    assert 'file("${rootProject.projectDir}/.cxx/snv4")' in gradle
    assert "checkSupernote${buildVariant}State" in gradle
    assert "workingDir supernotePluginRoot" in gradle
    assert "'--jvm-manifest-root'" in gradle
    assert "outputs.files" not in gradle
    assert "common_codegen.py" not in gradle
    assert "buildVariant == 'Release' ? 'RelWithDebInfo' : buildVariant" in gradle
    assert '"configureCMake${cmakeBuildType}[arm64-v8a]"' in gradle
    assert 'set(SUPERNOTE_GENERATED_ROOT "${CMAKE_CURRENT_LIST_DIR}/generated")' in cmake
    assert '"${SUPERNOTE_GENERATED_ROOT}/jni/*.cpp"' in cmake
    assert "supernotePythonCommand" not in gradle
    assert 'optionPrefix = "supernoteFeatureRoot_"' in processor
    assert "toSortedMap()" in processor
    assert "catch (_: SupernoteSourceDiagnostic)" in processor
    assert "throw SupernoteSourceDiagnostic()" in processor
    assert "throw IllegalArgumentException(message)" not in processor
    assert "schema_version" in processor
    assert "getSymbolsWithAnnotation" in processor
    assert "Kotlin suspend requires explicit SupernotePluginAsync" in processor
    assert "org.jspecify.annotations.Nullable" in processor
    assert "androidx.annotation.Nullable" not in processor
    assert "SNV4_RESTART_REQUIRED" in module
    assert "SNV4_GENERATION_STATE_CORRUPT" in module
    assert "retainedIds.size != retainedGenerations" in module
    assert "MessageDigest.getInstance(\"SHA-256\")" in module
    assert "cachedPublication != null" in module
    assert "Reused native generation" in module
    assert "logical invalidation completed" in bootstrap
    assert "process_services().shutdown();" not in bootstrap
    assert "Java nullability requires org.jspecify.annotations.Nullable" in processor
    assert "ReactMethod" not in processor
    assert "TypeScript" not in processor
    assert "nativeInstall" in bootstrap
    assert "nativeInvalidate" in bootstrap
    assert "nativeRunJsTask" not in bootstrap
    assert "RegisterNatives" in bootstrap
    assert "register_coroutine_bridge(env, class_loader)" in bootstrap
    assert "GetStringUTFChars(request, nullptr)" in bootstrap
    assert "GetStringUTFChars(generation_identity, nullptr)" in bootstrap
    assert "GetByteArrayRegion" in bootstrap
    assert 'const_cast<char *>("(JLjava/lang/Object;[BZ)V")' in bootstrap
    assert "failureMessageUtf8: ByteArray?" in coroutine_bridge
    assert "toByteArray(Charsets.UTF_8)" in coroutine_bridge
    assert "configure_worker_threads" in bootstrap
    assert "AttachCurrentThread" in bootstrap
    assert "DetachCurrentThread" in bootstrap
    assert f"{component}_register_natives" in bootstrap
    assert "retain_runtime_mapping" not in bootstrap
    assert "g_runtime_mapping" not in bootstrap
    jni_on_load = bootstrap.index('extern "C" JNIEXPORT jint JNICALL JNI_OnLoad')
    jni_on_load_end = bootstrap.index("\n}\n", jni_on_load) + 3
    jni_on_load_body = bootstrap[jni_on_load:jni_on_load_end]
    assert "return publish_runtime_registrar(env)" in jni_on_load_body
    assert f"supernote.v4.load-request.{component}.v1" in bootstrap
    assert f"supernote.v4.registrar.{component}.v1" in bootstrap
    assert "generated runtime generation identity mismatch" in bootstrap
    assert "supernote.generated.runtime.SupernoteV4Module" in bootstrap
    assert "(JLjava/lang/ClassLoader;" in bootstrap
    assert "Lcom/facebook/react/bridge/ReactApplicationContext;" in bootstrap
    assert "CallInvokerHolder;)J" in bootstrap
    assert "CallInvokerHolder::javaobject" in bootstrap
    assert "call_invoker->invokeAsync" in bootstrap
    assert "runOnJSQueueThread" not in bootstrap
    assert 'const_cast<char *>("nativeInvalidate")' in bootstrap
    assert "JNI_OnLoad" in bootstrap
    assert "RegisterNatives" not in jni_on_load_body
    assert "install_plugin_bindings" in bootstrap
    assert "class WeakObjectProbeHost" in bootstrap
    assert "std::optional<facebook::jsi::WeakObject>" in bootstrap
    assert "weak_->lock(runtime)" in bootstrap
    assert "weak_.reset()" in bootstrap
    assert "install_weak_object_probe(*runtime)" in bootstrap
    assert "runOnJSQueueThread" in module
    assert "context.jsCallInvokerHolder" in module
    assert "nativeInstall(runtimePointer, loader, context, callInvoker)" in module
    assert "private val lifecycleLock = Any()" in module
    assert "private var lifecycleState = LifecycleState.NEW" in module
    assert "LifecycleState.INSTALL_PENDING" in module
    assert "LifecycleState.INVALIDATED" in module
    install_guard = module[
        module.index("val installed = synchronized(lifecycleLock)") :
        module.index("?: return@runOnJSQueueThread")
    ]
    assert "lifecycleState != LifecycleState.INSTALL_PENDING" in install_guard
    assert "nativeInstall(runtimePointer, loader, context, callInvoker)" in install_guard
    invalidate_guard = module[
        module.index("val invalidated = synchronized(lifecycleLock)") :
        module.index("super.invalidate()")
    ]
    assert "lifecycleState = LifecycleState.INVALIDATED" in invalidate_guard
    assert "sessionId.also { sessionId = 0L }" in invalidate_guard
    assert "nativeRunJsTask" not in module
    assert f'findLibrary("{registration_component}")' in module
    assert "SupernoteV4NativeRegistrationBridge.register" in module
    assert "File(libraryPath).parentFile" in module
    assert "sourceLibrary.parentFile" in module
    assert "context.codeCacheDir" not in module
    assert 'Integer.toHexString(System.identityHashCode(pluginClassLoader))' in module
    assert 'java.lang.Long.toHexString(System.nanoTime())' in module
    assert 'File(runtimeDirectory, "lib$runtimeLoadName.so")' in module
    assert "DirectorySoSource.RESOLVE_DEPENDENCIES" in module
    assert "SoLoader.prependSoSource" in module
    assert "if (registeredSource != sourcePath)" in module
    assert "Generated V4 runtime source mismatch" not in module
    assert "SoLoader.loadLibrary(runtimeLoadName)" in module
    assert "System.load(runtimeCopy.absolutePath)" not in module
    assert "synchronized(System.getProperties())" in module
    assert "publishedGeneration != runtimeLoadName" in module
    assert f"supernote.v4.source.{component}.v1" in module
    assert f"supernote.v4.generations.{component}.v1" in module
    assert f"supernote.v4.binary.{component}.v1." in module
    assert "MAX_RETAINED_GENERATIONS = 32" in module
    assert "retainedGenerations !in 0 until MAX_RETAINED_GENERATIONS" in module
    assert "restart PluginHost" in module
    reservation = module.index("(retainedGenerations + 1).toString()")
    native_load = module.index("SoLoader.loadLibrary(runtimeLoadName)")
    assert reservation < native_load
    assert "runtimeCopy.delete()" in module
    assert f'SoLoader.loadLibrary("{component}")' not in module
    assert "File.createTempFile" in module
    assert "System.load(bridge.absolutePath)" in module
    assert "bridge.delete()" in module
    assert f"supernote.v4.load-request.{component}.v1" in module
    assert f"supernote.v4.registrar.{component}.v1" in module
    assert (
        "nativeRegister(registrarAddress, generationIdentity, classLoader)"
        in module
    )
    assert "SupernoteRuntimeRegistrar" in registration_bridge
    assert "nativeRegister" in registration_bridge
    assert "dladdr((void *)registrar, &registrar_info)" in registration_bridge
    assert "SupernoteV4Registration" in registration_bridge
    assert "published runtime registrar is no longer mapped" in registration_bridge
    assert "jsi" not in registration_bridge
    assert "RuntimeSession" not in registration_bridge
    assert "class SupernoteV4Package" in module
    assert (
        generated
        / "annotations/src/main/java/supernote/generated/annotations/SupernotePluginExport.java"
    ).is_file()
    assert (
        generated
        / "annotations/src/main/java/supernote/generated/annotations/SupernotePluginInternal.java"
    ).is_file()
    assert (
        generated
        / "annotations/src/main/java/supernote/generated/annotations/SupernotePluginAsync.java"
    ).is_file()
    assert not (
        generated
        / "annotations/src/main/java/supernote/generated/annotations/SupernoteExport.java"
    ).exists()


def test_registry_and_ownership_are_deterministic(tmp_path: Path):
    first = generate_plugin_runtime(tmp_path, registry("beta", "alpha"))
    snapshot = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    repeated = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    ownership = json.loads((second / "ownership.json").read_text())

    assert snapshot == repeated
    assert set(ownership["generated_files"]) == set(snapshot)


def test_removing_feature_regenerates_registry_without_replacing_component(tmp_path: Path):
    full = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    component = json.loads((full / "feature-registry.json").read_text())[
        "component_name"
    ]
    reduced = generate_plugin_runtime(tmp_path, registry("beta"))
    value = json.loads((reduced / "feature-registry.json").read_text())

    assert value["component_name"] == component
    assert [item["public_name"] for item in value["features"]] == ["Beta"]
    assert "Alpha" not in (reduced / "src/feature_registry.cpp").read_text()


def test_activation_can_restore_previous_shared_component(tmp_path: Path):
    destination = generate_plugin_runtime(tmp_path, registry("alpha"))
    original = (destination / "feature-registry.json").read_bytes()
    staged = stage_plugin_runtime(tmp_path, registry("beta"))
    backup = activate_plugin_runtime(staged, tmp_path)
    assert backup is not None
    assert b'"Beta"' in (destination / "feature-registry.json").read_bytes()

    restore_plugin_runtime(tmp_path, backup)
    assert (tmp_path / RUNTIME_RELATIVE_ROOT / "feature-registry.json").read_bytes() == original


def test_generated_runtime_enforces_session_cancellation_and_cleanup_contracts(
    tmp_path: Path,
):
    compiler = host_cxx_compiler()
    if compiler is None:
        pytest.skip("a host C++ compiler is required for the runtime contract")
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    harness = tmp_path / "runtime_contract.cpp"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "runtime_services.hpp"

            #include <array>
            #include <atomic>
            #include <chrono>
            #include <condition_variable>
            #include <future>
            #include <mutex>
            #include <thread>
            #include <vector>

            using namespace supernote::runtime;

            struct Service {
              explicit Service(std::promise<std::thread::id> *destroyed)
                  : destroyed(destroyed) {}
              ~Service() { destroyed->set_value(std::this_thread::get_id()); }
              std::promise<std::thread::id> *destroyed;
            };

            struct BlockingService {
              BlockingService(std::promise<void> *started,
                              std::shared_future<void> release)
                  : started(started), release(std::move(release)) {}
              ~BlockingService() {
                started->set_value();
                release.wait();
              }
              std::promise<void> *started;
              std::shared_future<void> release;
            };

            int main() {
              auto wait_until = [](auto predicate) {
                const auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::seconds(2);
                while (!predicate()) {
                  if (std::chrono::steady_clock::now() >= deadline) return false;
                  std::this_thread::yield();
                }
                return true;
              };
              std::vector<RuntimeSession::JsTask> js_queue;
              auto runtime = RuntimeSession::create(
                  [&](RuntimeSession::JsTask task) {
                    js_queue.push_back(std::move(task));
                  },
                  std::make_shared<int>(7));
              auto cleanup = std::make_shared<DeferredDestruction>();
              if (cleanup->thread_count() != 0) return 39;
              auto feature = FeatureSession::create(runtime, cleanup);
              std::atomic<int> resolved{0};
              std::atomic<int> rejected{0};
              std::atomic<int> cancelled{0};
              auto operation = feature->accept(
                  [&](void *) { ++rejected; });
              if (!operation || operation->cancellation_token().is_cancelled()) return 1;
              auto retained_state = std::make_shared<int>(77);
              std::weak_ptr<int> retained_weak = retained_state;
              operation->set_retained_state(retained_state);
              retained_state.reset();
              if (retained_weak.expired()) return 29;
              operation->set_cancel_hook([&] { ++cancelled; });
              if (!feature->schedule_completion(
                      operation, [&](void *) { ++resolved; })) return 2;
              feature->close_feature();
              int fake_runtime = 1;
              for (auto &task : js_queue) task(&fake_runtime);
              if (resolved != 0 || rejected != 1) return 3;
              if (operation->winner() != OperationWinner::CANCELLED_BY_FEATURE ||
                  !operation->cancellation_token().is_cancelled() ||
                  cancelled != 1) return 4;
              if (retained_weak.expired()) return 30;
              js_queue.clear();
              operation.reset();
              if (!retained_weak.expired()) return 31;

              for (int iteration = 0; iteration < 1000; ++iteration) {
                std::atomic<int> callbacks{0};
                std::atomic<int> race_cancelled{0};
                std::atomic<bool> go{false};
                int race_runtime_pointer = iteration + 1;
                auto race_runtime = RuntimeSession::create(
                    [&](RuntimeSession::JsTask task) {
                      task(&race_runtime_pointer);
                    });
                auto race_feature = FeatureSession::create(race_runtime, cleanup);
                auto race_operation = race_feature->accept(
                    [&](void *) { ++callbacks; });
                race_operation->set_cancel_hook([&] { ++race_cancelled; });
                std::thread completing([&] {
                  while (!go.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                  }
                  race_feature->schedule_completion(
                      race_operation, [&](void *) { ++callbacks; });
                });
                std::thread closing([&] {
                  while (!go.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                  }
                  race_feature->close_feature();
                });
                go.store(true, std::memory_order_release);
                completing.join();
                closing.join();
                const auto winner = race_operation->winner();
                if (callbacks != 1) return 32;
                if (winner == OperationWinner::COMPLETING) {
                  if (race_cancelled != 0) return 33;
                } else if (winner == OperationWinner::CANCELLED_BY_FEATURE) {
                  if (race_cancelled != 1) return 34;
                } else {
                  return 35;
                }
                race_runtime->invalidate();
                if (!wait_until([&] {
                      return process_services().retired_runtime_count() == 0;
                    })) return 47;
              }

              for (int iteration = 0; iteration < 1000; ++iteration) {
                std::atomic<int> completed{0};
                std::atomic<int> race_cancelled{0};
                std::atomic<bool> go{false};
                int race_runtime_pointer = iteration + 1;
                auto race_runtime = RuntimeSession::create(
                    [&](RuntimeSession::JsTask task) {
                      task(&race_runtime_pointer);
                    });
                auto race_feature = FeatureSession::create(race_runtime, cleanup);
                auto race_operation = race_feature->accept({});
                race_operation->set_cancel_hook([&] { ++race_cancelled; });
                std::thread completing([&] {
                  while (!go.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                  }
                  race_feature->schedule_completion(
                      race_operation, [&](void *) { ++completed; });
                });
                std::thread closing([&] {
                  while (!go.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                  }
                  race_runtime->invalidate();
                });
                go.store(true, std::memory_order_release);
                completing.join();
                closing.join();
                if (!wait_until([&] {
                      return race_operation->winner() != OperationWinner::PENDING;
                    })) return 48;
                const auto winner = race_operation->winner();
                if (winner == OperationWinner::COMPLETING) {
                  if (completed != 1 || race_cancelled != 0) return 36;
                } else if (winner == OperationWinner::CANCELLED_BY_RUNTIME) {
                  if (!wait_until([&] { return race_cancelled == 1; })) return 50;
                  if (completed != 0 || race_cancelled != 1) return 37;
                } else {
                  return 38;
                }
              }

              auto replacement = RuntimeSession::create(
                  [&](RuntimeSession::JsTask task) {
                    js_queue.push_back(std::move(task));
                  });
              if (replacement->id() == runtime->id()) return 5;
              auto replacement_feature = FeatureSession::create(replacement, cleanup);
              if (current_feature_session()) return 15;
              {
                FeatureCallScope scope(replacement_feature);
                if (current_feature_session() != replacement_feature) return 16;
                auto completion_state = std::make_shared<int>(9);
                auto internal = replacement_feature->accept(
                    {}, completion_state);
                if (!replacement_feature->claim_internal_completion(internal) ||
                    internal->winner() != OperationWinner::COMPLETING) return 17;
                if (replacement_feature->claim_internal_completion(internal)) return 18;
                if (internal->take_internal_completion() != completion_state ||
                    internal->take_internal_completion()) return 25;
              }
              if (current_feature_session()) return 19;
              auto dropped = replacement_feature->accept(
                  [&](void *) { ++rejected; });
              replacement->invalidate();
              if (!wait_until([&] {
                    return dropped->winner() != OperationWinner::PENDING &&
                        cancelled >= 1;
                  })) return 49;
              if (!js_queue.empty() || !dropped->cancellation_token().is_cancelled() ||
                  dropped->winner() != OperationWinner::CANCELLED_BY_RUNTIME) return 6;

              auto scoped_runtime = RuntimeSession::create(
                  [](RuntimeSession::JsTask) {});
              auto scoped_feature = FeatureSession::create(scoped_runtime, cleanup);
              std::weak_ptr<FeatureSession> scoped_weak = scoped_feature;
              {
                FeatureCallScope scope(scoped_feature);
                scoped_runtime->invalidate();
                scoped_feature.reset();
                if (!wait_until([&] { return scoped_weak.expired(); })) return 22;
                if (current_feature_session()) return 23;
              }

              auto detached_runtime = RuntimeSession::create(
                  [](RuntimeSession::JsTask) {});
              auto detached_feature = FeatureSession::create(
                  detached_runtime, cleanup);
              std::weak_ptr<FeatureSession> detached_weak = detached_feature;
              detached_feature->close_feature();
              detached_feature.reset();
              if (!detached_weak.expired()) return 24;
              detached_runtime->invalidate();

              BoundedExecutor executor(1, 2);
              if (executor.thread_count() != 0) return 40;
              std::atomic<int> worker_initialized{0};
              std::atomic<int> worker_cleaned{0};
              executor.set_thread_initializer([&] {
                ++worker_initialized;
                return [&] { ++worker_cleaned; };
              });
              std::mutex mutex;
              std::condition_variable ready;
              bool started = false;
              bool release = false;
              std::atomic<bool> second_ran{false};
              auto first = executor.submit([&](CancellationToken) {
                std::unique_lock lock(mutex);
                started = true;
                ready.notify_one();
                ready.wait(lock, [&] { return release; });
              });
              {
                std::unique_lock lock(mutex);
                ready.wait(lock, [&] { return started; });
              }
              if (executor.thread_count() != 1) return 41;
              auto second = executor.submit(
                  [&](CancellationToken) { second_ran = true; });
              if (!first.accepted() || !second.accepted() || !second.cancel()) return 7;
              {
                std::lock_guard lock(mutex);
                release = true;
              }
              ready.notify_one();
              executor.shutdown();
              if (second_ran || !second.token().is_cancelled()) return 8;
              if (worker_initialized != 1 || worker_cleaned != 1) return 20;
              if (executor.thread_count() != 0) return 42;

              std::atomic<int> jvm_completions{0};
              if (process_services().thread_count() > 1) return 43;
              auto completion_id = process_services().register_jvm_async_completion(
                  [&](void *, void *, std::string code, std::string message) {
                    if (code == "IMPLEMENTATION_ERROR" && message == "failed") {
                      ++jvm_completions;
                    }
                  });
              process_services().complete_jvm_async(
                  completion_id, nullptr, nullptr,
                  "IMPLEMENTATION_ERROR", "failed");
              process_services().complete_jvm_async(
                  completion_id, nullptr, nullptr,
                  "IMPLEMENTATION_ERROR", "again");
              if (jvm_completions != 1) return 12;
              auto discarded = process_services().register_jvm_async_completion(
                  [&](void *, void *, std::string, std::string) {
                    ++jvm_completions;
                  });
              if (!process_services().discard_jvm_async_completion(discarded)) return 13;
              process_services().complete_jvm_async(
                  discarded, nullptr, nullptr, "", "");
              if (jvm_completions != 1) return 14;

              std::promise<std::thread::id> destroyed;
              auto destroyed_future = destroyed.get_future();
              auto cleanup_feature = FeatureSession::create(
                  RuntimeSession::create([](RuntimeSession::JsTask) {}), cleanup);
              auto service = cleanup_feature->service<Service>(
                  "cpp:Service", [&] { return std::make_shared<Service>(&destroyed); });
              auto same = cleanup_feature->service<Service>(
                  "cpp:Service", [&] { return std::make_shared<Service>(&destroyed); });
              if (service.get() != same.get()) return 9;
              service.reset();
              same.reset();
              const auto releasing_thread = std::this_thread::get_id();
              cleanup_feature->close_feature();
              if (destroyed_future.wait_for(std::chrono::seconds(2)) !=
                  std::future_status::ready) return 10;
              if (destroyed_future.get() == releasing_thread) return 11;
              if (cleanup->thread_count() != 1) return 44;

              std::promise<std::thread::id> callback_destroyed;
              auto callback_destroyed_future = callback_destroyed.get_future();
              auto callback_runtime = RuntimeSession::create(
                  [](RuntimeSession::JsTask) {});
              auto callback_feature = FeatureSession::create(
                  callback_runtime, cleanup);
              auto callback_capture =
                  std::make_shared<Service>(&callback_destroyed);
              auto callback_holder = std::make_shared<std::function<void()>>(
                  [callback_capture] {});
              callback_capture.reset();
              auto callback_operation = callback_feature->accept(
                  {}, callback_holder);
              callback_holder.reset();
              callback_feature->close_feature();
              if (callback_operation->take_internal_completion()) return 26;
              if (callback_destroyed_future.wait_for(std::chrono::seconds(2)) !=
                  std::future_status::ready) return 27;
              if (callback_destroyed_future.get() == releasing_thread) return 28;

              std::promise<void> blocking_started;
              auto blocking_started_future = blocking_started.get_future();
              std::promise<void> allow_blocking_release;
              auto blocking_release_future =
                  allow_blocking_release.get_future().share();
              auto blocking_feature = FeatureSession::create(
                  RuntimeSession::create([](RuntimeSession::JsTask) {}), cleanup);
              auto blocking = blocking_feature->service<BlockingService>(
                  "cpp:BlockingService", [&] {
                    return std::make_shared<BlockingService>(
                        &blocking_started, blocking_release_future);
                  });
              blocking.reset();
              blocking_feature->close_feature();
              if (blocking_started_future.wait_for(std::chrono::seconds(2)) !=
                  std::future_status::ready) return 21;
              allow_blocking_release.set_value();
              cleanup->drain_and_shutdown();
              if (cleanup->thread_count() != 0) return 45;
              process_services().shutdown();
              if (process_services().thread_count() != 0) return 46;
              return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    executable = tmp_path / (
        "runtime_contract.exe" if os.name == "nt" else "runtime_contract"
    )
    thread_flags = [] if os.name == "nt" else ["-pthread"]
    sanitizer_flags = (
        ["-fsanitize=thread", "-g"]
        if os.environ.get("SUPERNOTE_V4_TSAN") == "1"
        else []
    )
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            *thread_flags,
            *sanitizer_flags,
            str(generated / "src/runtime_services.cpp"),
            str(harness),
            "-I",
            str(generated / "src"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    executed = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=False, timeout=60
    )
    assert executed.returncode == 0, executed.stderr


def test_saturated_cleanup_queue_never_destroys_on_invalidating_thread(tmp_path: Path):
    compiler = host_cxx_compiler()
    if compiler is None:
        pytest.skip("a host C++ compiler is required for the saturation harness")
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    kotlin = (
        generated
        / "src/main/java/supernote/generated/runtime/SupernoteV4Module.kt"
    ).read_text()
    bootstrap = (generated / "src/runtime_bootstrap.cpp").read_text()
    services = (generated / "src/runtime_services.cpp").read_text()
    assert "sessionId.also { sessionId = 0L }" in kotlin
    assert kotlin.count("nativeInvalidate(invalidated)") == 1
    assert bootstrap.count("session->invalidate()") == 1
    assert "ensure_retirement_retry_worker" in services
    harness = tmp_path / "runtime_cleanup_saturation.cpp"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "runtime_services.hpp"

            #include <chrono>
            #include <future>
            #include <thread>

            using namespace supernote::runtime;

            struct SlowService {
              explicit SlowService(std::promise<std::thread::id>* destroyed)
                  : destroyed(destroyed) {}
              ~SlowService() { destroyed->set_value(std::this_thread::get_id()); }
              std::promise<std::thread::id>* destroyed;
            };

            int main() {
              auto cleanup = process_services().cleanup();
              std::promise<void> blocker_started;
              std::promise<void> release_blocker;
              auto release = release_blocker.get_future().share();
              if (!cleanup->submit([&] {
                    blocker_started.set_value();
                    release.wait();
                  })) return 1;
              if (blocker_started.get_future().wait_for(std::chrono::seconds(2)) !=
                  std::future_status::ready) return 2;

              auto runtime = RuntimeSession::create([](RuntimeSession::JsTask) {});
              auto feature = FeatureSession::create(runtime, cleanup);
              std::promise<std::thread::id> destroyed;
              auto destroyed_future = destroyed.get_future();
              auto service = feature->service<SlowService>(
                  "cpp:SlowService",
                  [&] { return std::make_shared<SlowService>(&destroyed); });
              service.reset();
              for (std::size_t index = 1; index < 1024; ++index) {
                if (!cleanup->submit([] {})) return 3;
              }

              const auto caller = std::this_thread::get_id();
              const auto started = std::chrono::steady_clock::now();
              if (!runtime->invalidate()) return 4;
              const auto elapsed = std::chrono::steady_clock::now() - started;
              if (elapsed > std::chrono::milliseconds(100)) return 5;
              if (destroyed_future.wait_for(std::chrono::milliseconds(20)) !=
                  std::future_status::timeout) return 6;
              if (process_services().restart_required() ||
                  process_services().retired_runtime_count() != 1) return 7;

              release_blocker.set_value();
              if (destroyed_future.wait_for(std::chrono::seconds(5)) !=
                  std::future_status::ready) return 8;
              if (destroyed_future.get() == caller) return 9;
              if (process_services().retired_runtime_count() != 0) return 10;
              process_services().shutdown();
              return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    executable = tmp_path / (
        "runtime_cleanup_saturation.exe"
        if os.name == "nt"
        else "runtime_cleanup_saturation"
    )
    thread_flags = [] if os.name == "nt" else ["-pthread"]
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            *thread_flags,
            str(generated / "src/runtime_services.cpp"),
            str(harness),
            "-I",
            str(generated / "src"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    executed = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=False, timeout=15
    )
    assert executed.returncode == 0, executed.stderr


def test_retry_worker_allocation_failure_is_restart_required_after_one_invalidate(
    tmp_path: Path,
):
    compiler = host_cxx_compiler()
    if compiler is None:
        pytest.skip("a host C++ compiler is required for the retry fault harness")
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    kotlin = (
        generated
        / "src/main/java/supernote/generated/runtime/SupernoteV4Module.kt"
    ).read_text()
    bootstrap = (generated / "src/runtime_bootstrap.cpp").read_text()
    services = (generated / "src/runtime_services.cpp").read_text()
    assert kotlin.count("nativeInvalidate(invalidated)") == 1
    assert bootstrap.count("session->invalidate()") == 1
    assert "if (retained)" in bootstrap
    assert "g_sessions.erase(found);" in bootstrap
    assert (
        "SNV4_RESTART_REQUIRED: runtime retirement could not be guaranteed"
        in bootstrap
    )
    assert "restart PluginHost" in bootstrap
    assert (
        "bool ProcessServices::ensure_retirement_retry_worker() noexcept"
        in services
    )
    assert "!ensure_retirement_retry_worker()" in services

    harness = tmp_path / "runtime_retry_worker_allocation_failure.cpp"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "runtime_services.hpp"

            #include <atomic>
            #include <chrono>
            #include <cstdlib>
            #include <future>
            #include <iostream>
            #include <new>
            #include <thread>

            namespace {
            std::atomic<bool> fault_window{false};
            std::atomic<std::size_t> allocation_count{0};
            std::atomic<std::size_t> fail_at{0};
            }

            void* operator new(std::size_t size) {
              if (fault_window.load(std::memory_order_acquire)) {
                const auto current =
                    allocation_count.fetch_add(1, std::memory_order_acq_rel) + 1;
                if (current == fail_at.load(std::memory_order_acquire)) {
                  throw std::bad_alloc();
                }
              }
              if (void* value = std::malloc(size)) return value;
              throw std::bad_alloc();
            }

            void operator delete(void* value) noexcept { std::free(value); }
            void operator delete(void* value, std::size_t) noexcept {
              std::free(value);
            }

            int main(int argc, char** argv) {
              if (argc != 2) return 2;
              using namespace supernote::runtime;
              const auto requested_failure = static_cast<std::size_t>(
                  std::strtoull(argv[1], nullptr, 10));
              auto cleanup = process_services().cleanup();
              std::promise<void> blocker_started;
              std::promise<void> release_blocker;
              auto release = release_blocker.get_future().share();
              if (!cleanup->submit([&] {
                    blocker_started.set_value();
                    release.wait();
                  })) return 3;
              if (blocker_started.get_future().wait_for(std::chrono::seconds(2)) !=
                  std::future_status::ready) return 4;
              for (std::size_t index = 0; index < 1024; ++index) {
                if (!cleanup->submit([] {})) return 5;
              }

              auto runtime = RuntimeSession::create([](RuntimeSession::JsTask) {});
              auto feature = FeatureSession::create(runtime, cleanup);
              (void)feature;
              fail_at.store(requested_failure, std::memory_order_release);
              allocation_count.store(0, std::memory_order_release);
              fault_window.store(requested_failure != 0, std::memory_order_release);
              const bool returned = runtime->invalidate();
              fault_window.store(false, std::memory_order_release);
              const auto allocations =
                  allocation_count.load(std::memory_order_acquire);
              const bool restart = process_services().restart_required();
              const auto before = process_services().retired_runtime_count();

              release_blocker.set_value();
              const auto deadline =
                  std::chrono::steady_clock::now() + std::chrono::seconds(3);
              while (process_services().retired_runtime_count() != 0 &&
                     std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
              }
              const auto after = process_services().retired_runtime_count();
              const bool eventual = after == 0;
              std::cout << "fail=" << requested_failure
                        << " allocations=" << allocations
                        << " return=" << returned
                        << " restart=" << restart
                        << " before=" << before
                        << " eventual=" << eventual
                        << " after=" << after << '\n';

              if (returned) {
                if (restart || before != 1 || !eventual) return 6;
              } else {
                if (!restart) return 7;
                if (before == 1 && eventual) return 8;
              }
              process_services().shutdown();
              return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    assert harness.read_text().count("runtime->invalidate()") == 1
    executable = tmp_path / (
        "runtime_retry_worker_allocation_failure.exe"
        if os.name == "nt"
        else "runtime_retry_worker_allocation_failure"
    )
    thread_flags = [] if os.name == "nt" else ["-pthread"]
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            *thread_flags,
            str(generated / "src/runtime_services.cpp"),
            str(harness),
            "-I",
            str(generated / "src"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr

    observed_retry_start_failure = False
    for failure_index in range(0, 33):
        executed = subprocess.run(
            [str(executable), str(failure_index)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert executed.returncode == 0, (
            f"failure_index={failure_index}: {executed.stdout}\n{executed.stderr}"
        )
        fields = dict(
            item.split("=", 1) for item in executed.stdout.strip().split()
        )
        if (
            fields.get("return") == "0"
            and fields.get("restart") == "1"
            and fields.get("before") == "1"
        ):
            assert fields.get("eventual") == "0"
            assert fields.get("after") == "1"
            observed_retry_start_failure = True
    assert observed_retry_start_failure, (
        "fault sweep did not reach retry-thread construction; expand or adapt the "
        "isolated allocation window for this standard library"
    )


def test_generated_runtime_teardown_survives_allocation_failure(tmp_path: Path):
    compiler = host_cxx_compiler()
    if compiler is None:
        pytest.skip("a host C++ compiler is required for the allocation harness")
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    harness = tmp_path / "runtime_allocation_failure.cpp"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "runtime_services.hpp"

            #include <array>
            #include <atomic>
            #include <cstdlib>
            #include <memory>
            #include <new>
            #include <string_view>
            #include <vector>

            namespace {
            std::atomic<bool> fail_next{false};
            struct LargeCleanup {
              std::array<unsigned char, 256> storage{};
              void operator()() const noexcept {}
            };
            }

            void* operator new(std::size_t size) {
              if (fail_next.exchange(false, std::memory_order_acq_rel)) {
                throw std::bad_alloc();
              }
              if (void* value = std::malloc(size)) return value;
              throw std::bad_alloc();
            }

            void operator delete(void* value) noexcept { std::free(value); }
            void operator delete(void* value, std::size_t) noexcept {
              std::free(value);
            }

            int main(int argc, char** argv) {
              if (argc != 2) return 2;
              std::set_terminate([] { std::_Exit(86); });
              using namespace supernote::runtime;
              std::vector<RuntimeSession::JsTask> queue;
              auto runtime = RuntimeSession::create(
                  [&](RuntimeSession::JsTask task) {
                    queue.push_back(std::move(task));
                  });
              auto cleanup = std::make_shared<DeferredDestruction>();
              auto feature = FeatureSession::create(runtime, cleanup);
              const std::string_view mode(argv[1]);

              if (mode == "schedule") {
                fail_next = true;
                if (runtime->schedule([](void*) {})) return 10;
              } else if (mode == "submit-large") {
                fail_next = true;
                if (cleanup->submit(LargeCleanup{})) return 12;
              } else if (mode == "invalidate") {
                fail_next = true;
                runtime->invalidate();
              } else if (mode == "close-feature") {
                if (!feature->accept([](void*) {})) return 11;
                fail_next = true;
                feature->close_feature();
              } else if (mode == "close-service") {
                auto service = feature->service<int>(
                    "cpp:Service", [] { return std::make_shared<int>(7); });
                fail_next = true;
                feature->close_feature();
              } else {
                return 3;
              }
              if (mode != "invalidate") runtime->invalidate();
              cleanup->drain_and_shutdown();
              return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    executable = tmp_path / (
        "runtime_allocation_failure.exe"
        if os.name == "nt"
        else "runtime_allocation_failure"
    )
    thread_flags = [] if os.name == "nt" else ["-pthread"]
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            *thread_flags,
            str(generated / "src/runtime_services.cpp"),
            str(harness),
            "-I",
            str(generated / "src"),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    for mode in (
        "schedule",
        "submit-large",
        "invalidate",
        "close-feature",
        "close-service",
    ):
        executed = subprocess.run(
            [str(executable), mode],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert executed.returncode == 0, f"{mode}: {executed.stderr}"


def test_standalone_common_codegen_runs_without_repository_pythonpath(tmp_path: Path):
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    ownership = json.loads((generated / "ownership.json").read_text())

    assert not any(path.endswith(".py") for path in ownership["generated_files"])
    assert not list(generated.rglob("*.py"))
    assert "supernote-module" in (generated / "build.gradle").read_text()


def test_common_codegen_emits_real_cpp_jsi_route(tmp_path: Path):
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text('{"name":"fixture"}\n')
    service = FeatureOperationService(tmp_path)
    feature_root = service.add(
        FeatureConfig(
            tmp_path / "local_modules/@local/math",
            "@local/math",
            "4.0.0-dev.0",
            "com.example.math",
            "Math",
            starters=(StarterFamily.NATIVE,),
        )
    )
    feature = service.find_record("@local/math").manifest
    cpp = feature_root / feature.roots.native
    (cpp / "math.cpp").write_text(
        "// @SupernotePluginExport\n"
        "double add(double left, double right) { return left + right; }\n"
    )
    generator = GenerationService(tmp_path)
    plan = generator.plan(
        operation="update",
        requested_targets=("@local/math",),
        allow_unmanifested_bootstrap=True,
    )
    generator.execute(plan, Transaction(tmp_path, "update", ("@local/math",)))
    generated = tmp_path / RUNTIME_RELATIVE_ROOT
    jni = generated / "generated/jni"
    source = next(jni.glob("feature_*.cpp")).read_text()
    assert "createFromHostFunction" in source
    assert 'exports.setProperty(runtime, "add"' in source
    assert feature.feature_id in source
    assert "JNI_OnLoad" not in source
    bootstrap = (jni / "plugin_bindings.cpp").read_text()
    assert "install_plugin_bindings" in bootstrap
    assert "__supernoteV4FeatureRegistry_" in bootstrap
    assert '"__supernoteV4"' in bootstrap
    readme = (feature_root / "README.md").read_text()
    assert "import Math from '@local/math';" in readme
    assert "`Math.add(left: number, right: number): number` — sync" in readme
    assert "All listed calls are synchronous" in readme


def test_common_codegen_builds_readme_from_ksp_jvm_manifest(tmp_path: Path):
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text('{"name":"fixture"}\n')
    service = FeatureOperationService(tmp_path)
    feature_root = service.add(
        FeatureConfig(
            tmp_path / "local_modules/@local/jvm-files",
            "@local/jvm-files",
            "4.0.0-dev.0",
            "com.example.jvm_files",
            "JvmFiles",
            description="Read files on the JVM.",
            starters=(StarterFamily.JVM,),
        )
    )
    feature = service.find_record("@local/jvm-files").manifest
    owner_class = "com.example.jvm_files.FeatureApiKt"
    owner_id = jvm_owner_identity(owner_class)
    declaration_id = jvm_declaration_identity(owner_class, "loadPage", "(I)[B")
    exported_async = SourceIntent.from_markers(
        DeclarationTarget.FUNCTION,
        (SupernoteMarker.EXPORT, SupernoteMarker.ASYNC),
    )
    declaration = JvmDeclarationSource(
        SourceProvenance(
            declaration_id, "kotlin", "FeatureApi.kt", 6, 1
        ),
        owner_id,
        owner_class,
        "loadPage",
        "(I)[B",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        False,
        exported_async,
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
    source_manifest = JvmSourceManifest(
        feature.feature_id, "4.0.0-dev.0", (owner,)
    )
    generator = GenerationService(tmp_path)
    plan = generator.plan(
        operation="update",
        requested_targets=("@local/jvm-files",),
        jvm_manifests={feature.feature_id: source_manifest},
        allow_unmanifested_bootstrap=True,
    )
    generator.execute(plan, Transaction(tmp_path, "update", ("@local/jvm-files",)))
    readme = (feature_root / "README.md").read_text(encoding="utf-8")
    assert "Read files on the JVM." in readme
    assert "import JvmFiles from '@local/jvm-files';" in readme
    assert "`JvmFiles.loadPage(page: number): Promise<Uint8Array>` — async" in readme
    assert "const result = await JvmFiles.loadPage(page);" in readme
    assert "Kotlin/Java: `android/src/main/java/`" in readme
    suffix = feature.feature_id.removeprefix("supernote:feature:")
    assert (
        tmp_path
        / RUNTIME_RELATIVE_ROOT
        / f"generated/jni/jvm_feature_{suffix}.cpp"
    ).is_file()


def test_common_codegen_emits_hidden_cpp_internal_facade(tmp_path: Path):
    (tmp_path / "android/app").mkdir(parents=True)
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n")
    (tmp_path / "android/app/build.gradle").write_text("plugins {}\n")
    (tmp_path / "package.json").write_text('{"name":"fixture"}\n')
    service = FeatureOperationService(tmp_path)
    feature_root = service.add(
        FeatureConfig(
            tmp_path / "local_modules/@local/documents",
            "@local/documents",
            "4.0.0-dev.0",
            "com.example.documents",
            "Documents",
            starters=(StarterFamily.NATIVE,),
        )
    )
    feature = service.find_record("@local/documents").manifest
    cpp = feature_root / feature.roots.native
    (cpp / "feature.cpp").unlink()
    (cpp / "documents.hpp").write_text(
        """#pragma once
#include <cstdint>
class IndexService {
public:
  IndexService();
  // @SupernotePluginInternal
  std::int32_t rebuild(std::int32_t page);
};
"""
    )
    (cpp / "documents.cpp").write_text(
        """#include "documents.hpp"
// @SupernotePluginInternal
std::int32_t pageCount(std::int32_t page) { return page; }
"""
    )
    generator = GenerationService(tmp_path)
    plan = generator.plan(
        operation="update",
        requested_targets=("@local/documents",),
        allow_unmanifested_bootstrap=True,
    )
    generator.execute(plan, Transaction(tmp_path, "update", ("@local/documents",)))
    generated = tmp_path / RUNTIME_RELATIVE_ROOT
    suffix = feature.feature_id.removeprefix("supernote:feature:")
    header = (
        generated / f"include/supernote/{suffix}/internal.hpp"
    ).read_text()
    source = (
        generated / f"generated/jni/internal_{suffix}.cpp"
    ).read_text()
    typescript = (feature_root / "index.d.ts").read_text()
    assert "std::int32_t pageCount(std::int32_t page);" in header
    assert "struct IndexService final" in header
    assert "current_feature_session" in source
    assert "feature->service<::IndexService>" in source
    assert "pageCount" not in typescript
    assert "IndexService" not in typescript
