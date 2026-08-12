import json
from pathlib import Path
import shutil
import subprocess
import textwrap

from supernote_module_generator.feature_model import (
    FeatureManifest,
    FeatureRegistryEntry,
    PluginRuntimeRegistry,
)
from supernote_module_generator.plugin_runtime_codegen import (
    RUNTIME_RELATIVE_ROOT,
    activate_plugin_runtime,
    generate_plugin_runtime,
    restore_plugin_runtime,
    stage_plugin_runtime,
)
from supernote_module_generator.semantic import SemanticApi


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
        generator_version="2.0.0.dev0",
        features=(entry(name) for name in names),
    )


def test_generates_one_compiled_runtime_component_for_all_features(tmp_path: Path):
    generated = generate_plugin_runtime(tmp_path, registry("alpha", "beta"))
    cmake = (generated / "CMakeLists.txt").read_text()
    services = (generated / "src/runtime_services.cpp").read_text()
    source = (generated / "src/feature_registry.cpp").read_text()

    assert cmake.count("add_library(") == 1
    assert "runtime_services.cpp" in cmake
    assert "feature_registry.cpp" in cmake
    assert "alpha" not in cmake
    assert "beta" not in cmake
    assert services.count("static ProcessServices services") == 1
    assert "supernote:feature:" in source
    assert '"Alpha"' in source
    assert '"Beta"' in source


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
    compiler = shutil.which("c++")
    assert compiler is not None
    generated = generate_plugin_runtime(tmp_path, registry("alpha"))
    harness = tmp_path / "runtime_contract.cpp"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "runtime_services.hpp"

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

            int main() {
              std::vector<RuntimeSession::JsTask> js_queue;
              auto runtime = RuntimeSession::create(
                  [&](RuntimeSession::JsTask task) {
                    js_queue.push_back(std::move(task));
                  },
                  std::make_shared<int>(7));
              auto cleanup = std::make_shared<DeferredDestruction>();
              auto feature = FeatureSession::create(runtime, cleanup);
              std::atomic<int> resolved{0};
              std::atomic<int> rejected{0};
              auto operation = feature->accept(
                  [&](void *) { ++rejected; });
              if (!operation || operation->cancellation_token().is_cancelled()) return 1;
              if (!feature->schedule_completion(
                      operation, [&](void *) { ++resolved; })) return 2;
              feature->close_feature();
              int fake_runtime = 1;
              for (auto &task : js_queue) task(&fake_runtime);
              if (resolved != 0 || rejected != 1) return 3;
              if (operation->winner() != OperationWinner::CANCELLED_BY_FEATURE ||
                  !operation->cancellation_token().is_cancelled()) return 4;

              js_queue.clear();
              auto replacement = RuntimeSession::create(
                  [&](RuntimeSession::JsTask task) {
                    js_queue.push_back(std::move(task));
                  });
              if (replacement->id() == runtime->id()) return 5;
              auto replacement_feature = FeatureSession::create(replacement, cleanup);
              auto dropped = replacement_feature->accept(
                  [&](void *) { ++rejected; });
              replacement->invalidate();
              if (!js_queue.empty() || !dropped->cancellation_token().is_cancelled() ||
                  dropped->winner() != OperationWinner::CANCELLED_BY_RUNTIME) return 6;

              BoundedExecutor executor(1, 2);
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
              cleanup->drain_and_shutdown();
              return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    executable = tmp_path / "runtime_contract"
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            "-pthread",
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
        [str(executable)], capture_output=True, text=True, check=False, timeout=10
    )
    assert executed.returncode == 0, executed.stderr
