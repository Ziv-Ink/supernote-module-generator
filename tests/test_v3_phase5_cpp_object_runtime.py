from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from supernote_module_generator.cpp_object_runtime_codegen import (
    render_cpp_object_runtime,
)
from supernote_module_generator.feature_model import PluginRuntimeRegistry
from supernote_module_generator.plugin_runtime_codegen import generated_runtime_files


def test_plugin_runtime_owns_nominal_cpp_object_header():
    files = generated_runtime_files(
        PluginRuntimeRegistry.create(
            plugin_id="phase5-cpp-objects",
            generator_version="3.0.0.dev0",
            features=(),
        )
    )
    header = files["include/supernote/cpp_objects.hpp"]
    assert "class CppObjectHandleBase" in header
    assert "class CppObjectHandle" in header
    assert "class CppObjectRegistry" in header
    assert "class ManagedAnyRef" in header
    assert "facebook::jsi::WeakObject" in header
    assert "std::owner_less<std::weak_ptr<void>>" in header
    assert "reinterpret_cast" not in header
    assert "include/supernote/cpp_objects.hpp" in files["ownership.json"]


def test_cpp_object_registry_identity_aliasing_and_nominal_extraction(tmp_path: Path):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++23 compiler is unavailable")

    (tmp_path / "jsi").mkdir()
    (tmp_path / "jsi/jsi.h").write_text(
        r'''#pragma once
#include <memory>
#include <utility>

namespace facebook::jsi {
class Runtime {};
class HostObject {
 public:
  virtual ~HostObject() = default;
};
struct ObjectState {
  explicit ObjectState(std::shared_ptr<HostObject> value)
      : host(std::move(value)) {}
  std::shared_ptr<HostObject> host;
};
class Object;
class Value {
 public:
  Value() = default;
  explicit Value(std::shared_ptr<ObjectState> state) : state_(std::move(state)) {}
  bool isObject() const { return static_cast<bool>(state_); }
  Object getObject(Runtime &) const;
 protected:
  std::shared_ptr<ObjectState> state_;
  friend class Object;
  friend class WeakObject;
};
class Object : public Value {
 public:
  Object() = default;
  explicit Object(std::shared_ptr<ObjectState> state) : Value(std::move(state)) {}
  static Object createFromHostObject(
      Runtime &, std::shared_ptr<HostObject> host) {
    return Object(std::make_shared<ObjectState>(std::move(host)));
  }
  template <typename T>
  bool isHostObject(Runtime &) const {
    return state_ && std::dynamic_pointer_cast<T>(state_->host);
  }
  template <typename T>
  std::shared_ptr<T> getHostObject(Runtime &) const {
    return std::dynamic_pointer_cast<T>(state_->host);
  }
  const void *identity() const { return state_.get(); }
};
inline Object Value::getObject(Runtime &) const { return Object(state_); }
class WeakObject {
 public:
  WeakObject(Runtime &, const Object &object) : state_(object.state_) {}
  WeakObject(WeakObject &&) = default;
  WeakObject &operator=(WeakObject &&) = default;
  Value lock(Runtime &) const { return Value(state_.lock()); }
 private:
  std::weak_ptr<ObjectState> state_;
};
}  // namespace facebook::jsi
''',
        encoding="utf-8",
    )
    (tmp_path / "runtime_services.hpp").write_text(
        r'''#pragma once
#include <memory>
#include <functional>
#include <utility>
#include <vector>
namespace supernote::runtime {
class DeferredDestruction {
 public:
  template <typename Cleanup>
  bool submit(Cleanup &&cleanup) {
    queued_.emplace_back(std::forward<Cleanup>(cleanup));
    return true;
  }
  void drain() {
    auto queued = std::move(queued_);
    for (auto &cleanup : queued) cleanup();
  }
 private:
  std::vector<std::function<void()>> queued_;
};
template <typename T>
class ManagedRef {
 public:
  ManagedRef() = default;
  ManagedRef(std::shared_ptr<T> value, std::shared_ptr<DeferredDestruction>)
      : value_(std::move(value)) {}
  T *get() const { return value_.get(); }
  explicit operator bool() const { return static_cast<bool>(value_); }
  const std::shared_ptr<T> &shared_ref() const { return value_; }
 private:
  std::shared_ptr<T> value_;
};
}  // namespace supernote::runtime
''',
        encoding="utf-8",
    )
    (tmp_path / "cpp_objects.hpp").write_text(
        render_cpp_object_runtime(), encoding="utf-8"
    )
    (tmp_path / "harness.cpp").write_text(
        r'''#include "cpp_objects.hpp"

#include <memory>
#include <string>

namespace {
struct Native { int value = 7; };
struct Other { int value = 9; };
struct Owner { Native first; Native second; };
struct Tracked {
  explicit Tracked(int *destroyed) : destroyed(destroyed) {}
  ~Tracked() { ++*destroyed; }
  int *destroyed;
};

class NativeHost final
    : public supernote::runtime::CppObjectHandle<Native> {
 public:
  using CppObjectHandle::CppObjectHandle;
};
class OtherHost final
    : public supernote::runtime::CppObjectHandle<Other> {
 public:
  using CppObjectHandle::CppObjectHandle;
};
}  // namespace

int main() {
  using namespace supernote::runtime;
  constexpr char kNativeType[] = "supernote:type:native";
  facebook::jsi::Runtime runtime;
  auto cleanup = std::make_shared<DeferredDestruction>();
  auto registry = std::make_shared<CppObjectRegistry>(cleanup);
  auto native = std::make_shared<Native>();

  const void *first_identity = nullptr;
  {
    auto first = registry->wrap(
        runtime, kNativeType, native,
        [](ManagedRef<Native> value) {
          return std::make_shared<NativeHost>(
              "supernote:type:native", std::move(value));
        });
    auto second = registry->wrap(
        runtime, kNativeType, native,
        [](ManagedRef<Native> value) {
          return std::make_shared<NativeHost>(
              "supernote:type:native", std::move(value));
        });
    if (first.identity() != second.identity()) return 1;
    first_identity = first.identity();
    auto extracted = try_extract_cpp_object<Native>(
        runtime, first, kNativeType);
    if (!extracted || extracted.shared_ref() != native) return 2;
    if (try_extract_cpp_object<Native>(runtime, first, "wrong")) return 3;
    if (try_extract_cpp_object<Other>(runtime, first, kNativeType)) return 4;
    if (cpp_object_type_id(runtime, first) != kNativeType) return 5;
  }

  auto replacement = registry->wrap(
      runtime, kNativeType, native,
      [](ManagedRef<Native> value) {
        return std::make_shared<NativeHost>(
            "supernote:type:native", std::move(value));
      });
  if (replacement.identity() == first_identity) return 6;
  if (registry->size_for_testing() != 1) return 7;

  for (int iteration = 0; iteration < 10000; ++iteration) {
    auto exposure = registry->wrap(
        runtime, kNativeType, native,
        [](ManagedRef<Native> value) {
          return std::make_shared<NativeHost>(
              "supernote:type:native", std::move(value));
        });
    if (exposure.identity() != replacement.identity()) return 100;
  }

  auto gc_native = std::make_shared<Native>();
  for (int iteration = 0; iteration < 1000; ++iteration) {
    const void *collected_identity = nullptr;
    {
      auto collected = registry->wrap(
          runtime, "supernote:type:gc", gc_native,
          [](ManagedRef<Native> value) {
            return std::make_shared<NativeHost>(
                "supernote:type:gc", std::move(value));
          });
      collected_identity = collected.identity();
    }
    auto reexposed = registry->wrap(
        runtime, "supernote:type:gc", gc_native,
        [](ManagedRef<Native> value) {
          return std::make_shared<NativeHost>(
              "supernote:type:gc", std::move(value));
        });
    if (reexposed.identity() == collected_identity) return 101;
  }

  auto owner = std::make_shared<Owner>();
  std::shared_ptr<Native> first_alias(owner, &owner->first);
  std::shared_ptr<Native> same_alias(owner, &owner->first);
  std::shared_ptr<Native> other_alias(owner, &owner->second);
  auto identity = CppObjectIdentity::from(kNativeType, first_alias);
  if (!identity.matches(kNativeType, same_alias)) return 8;
  if (identity.matches(kNativeType, other_alias)) return 9;
  std::shared_ptr<Native> foreign_owner(
      &owner->first, [](Native *) {});
  if (identity.matches(kNativeType, foreign_owner)) return 10;
  if (identity.matches("another-type", same_alias)) return 11;

  auto alias_object = registry->wrap(
      runtime, "supernote:type:alias", first_alias,
      [](ManagedRef<Native> value) {
        return std::make_shared<NativeHost>(
            "supernote:type:alias", std::move(value));
      });
  bool conflicting_owner_rejected = false;
  try {
    (void)registry->wrap(
        runtime, "supernote:type:alias", foreign_owner,
        [](ManagedRef<Native> value) {
          return std::make_shared<NativeHost>(
              "supernote:type:alias", std::move(value));
        });
  } catch (const std::logic_error &) {
    conflicting_owner_rejected = true;
  }
  if (!conflicting_owner_rejected || !alias_object.isObject()) return 12;

  auto other = std::make_shared<Other>();
  auto other_object = registry->wrap(
      runtime, "supernote:type:other", other,
      [](ManagedRef<Other> value) {
        return std::make_shared<OtherHost>(
            "supernote:type:other", std::move(value));
      });
  if (!try_extract_cpp_object<Other>(
          runtime, other_object, "supernote:type:other")) return 13;

  int destroyed = 0;
  auto tracked = std::make_shared<Tracked>(&destroyed);
  ManagedAnyRef retained(tracked, cleanup);
  tracked.reset();
  retained.reset();
  if (destroyed != 0) return 14;
  cleanup->drain();
  if (destroyed != 1) return 15;
  return 0;
}
''',
        encoding="utf-8",
    )

    binary = tmp_path / "harness"
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++23",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-I",
            str(tmp_path),
            str(tmp_path / "harness.cpp"),
            "-o",
            str(binary),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout
    environment = dict(os.environ)
    environment["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    executed = subprocess.run(
        [str(binary)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert executed.returncode == 0, executed.stdout
