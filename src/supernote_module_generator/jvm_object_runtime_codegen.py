"""Render the V4 JVM nominal-object handle and per-runtime identity registry."""
from __future__ import annotations


def render_jvm_object_runtime() -> str:
    # This source is inserted after AttachedEnv, clear_exception, and
    # retain_global in the generated JVM bridge translation unit.
    return r'''
std::shared_ptr<void> retain_weak_global(JNIEnv *env, jobject value) {
  if (env == nullptr || value == nullptr) {
    throw std::runtime_error("cannot weakly retain a null JVM object");
  }
  auto weak = env->NewWeakGlobalRef(value);
  if (weak == nullptr) {
    clear_exception(env);
    throw std::runtime_error("cannot allocate a JNI weak global reference");
  }
  auto cleanup = supernote::runtime::process_services().cleanup();
  return std::shared_ptr<void>(weak, [cleanup](void *raw) {
    auto release = [raw] {
      AttachedEnv attached;
      if (auto *env = attached.get()) {
        env->DeleteWeakGlobalRef(static_cast<jweak>(raw));
      }
    };
    if (!cleanup || !cleanup->submit(release)) release();
  });
}

class ManagedJvmRef final {
 public:
  ManagedJvmRef() = default;
  ManagedJvmRef(std::string type_id, std::shared_ptr<void> global)
      : type_id_(std::move(type_id)), global_(std::move(global)) {
    if (type_id_.empty() || !global_) {
      throw std::invalid_argument(
          "a JVM managed reference requires nominal identity and a global reference");
    }
  }

  explicit operator bool() const noexcept { return static_cast<bool>(global_); }
  std::string_view type_id() const noexcept { return type_id_; }
  jobject get() const noexcept { return static_cast<jobject>(global_.get()); }
  const std::shared_ptr<void> &global_ref() const noexcept { return global_; }

 private:
  std::string type_id_;
  std::shared_ptr<void> global_;
};

class ManagedJvmValue final {
 public:
  ManagedJvmValue() = default;
  explicit ManagedJvmValue(std::shared_ptr<void> global)
      : global_(std::move(global)) {}

  explicit operator bool() const noexcept { return static_cast<bool>(global_); }
  jobject get() const noexcept { return static_cast<jobject>(global_.get()); }
  const std::shared_ptr<void> &global_ref() const noexcept { return global_; }

 private:
  std::shared_ptr<void> global_;
};

class JvmObjectHandleBase : public facebook::jsi::HostObject {
 public:
  ~JvmObjectHandleBase() override = default;
  virtual std::string_view type_id() const noexcept = 0;
  virtual ManagedJvmRef managed_ref() const = 0;
};

class JvmObjectRegistry final
    : public std::enable_shared_from_this<JvmObjectRegistry> {
 public:
  JvmObjectRegistry() = default;
  JvmObjectRegistry(const JvmObjectRegistry &) = delete;
  JvmObjectRegistry &operator=(const JvmObjectRegistry &) = delete;

  template <typename Factory>
  facebook::jsi::Object wrap(
      facebook::jsi::Runtime &runtime,
      JNIEnv *env,
      std::string_view type_id,
      jobject instance,
      jint identity_hash,
      std::shared_ptr<void> strong_global,
      Factory &&factory) {
    assert_runtime(runtime);
    if (env == nullptr || instance == nullptr || !strong_global || type_id.empty()) {
      throw std::invalid_argument(
          "a JVM object result requires an environment, type, and live instance");
    }
    const auto hash = identity_hash;
    for (auto current = entries_.begin(); current != entries_.end();) {
      const auto weak = static_cast<jweak>(current->weak_global.get());
      const bool native_dead =
          weak == nullptr || env->IsSameObject(weak, nullptr) == JNI_TRUE;
      if (env->ExceptionCheck()) {
        clear_exception(env);
        throw std::runtime_error("cannot inspect JVM weak object identity");
      }
      if (native_dead) {
        current = entries_.erase(current);
        continue;
      }
      if (current->identity_hash != hash || current->type_id != type_id ||
          env->IsSameObject(weak, instance) != JNI_TRUE) {
        if (env->ExceptionCheck()) {
          clear_exception(env);
          throw std::runtime_error("cannot compare JVM object identity");
        }
        ++current;
        continue;
      }
      auto locked = current->javascript.lock(runtime);
      if (locked.isObject()) return locked.getObject(runtime);
      current = entries_.erase(current);
      break;
    }

    auto weak_global = retain_weak_global(env, instance);
    ManagedJvmRef managed(std::string(type_id), std::move(strong_global));
    auto host = std::invoke(
        std::forward<Factory>(factory), std::move(managed));
    static_assert(
        std::is_convertible_v<decltype(host),
                              std::shared_ptr<facebook::jsi::HostObject>>);
    auto object = facebook::jsi::Object::createFromHostObject(
        runtime, std::move(host));
    entries_.emplace_back(
        std::string(type_id), hash, std::move(weak_global),
        facebook::jsi::WeakObject(runtime, object));
    return object;
  }

  void purge(facebook::jsi::Runtime &runtime, JNIEnv *env) {
    assert_runtime(runtime);
    if (env == nullptr) throw std::invalid_argument("JNIEnv is required");
    for (auto current = entries_.begin(); current != entries_.end();) {
      const auto weak = static_cast<jweak>(current->weak_global.get());
      const bool native_dead =
          weak == nullptr || env->IsSameObject(weak, nullptr) == JNI_TRUE;
      if (env->ExceptionCheck()) {
        clear_exception(env);
        throw std::runtime_error("cannot inspect JVM weak object identity");
      }
      if (native_dead || !current->javascript.lock(runtime).isObject()) {
        current = entries_.erase(current);
      } else {
        ++current;
      }
    }
  }

  std::size_t size_for_testing() const noexcept { return entries_.size(); }

 private:
  struct Entry final {
    Entry(
        std::string type_id,
        jint identity_hash,
        std::shared_ptr<void> weak_global,
        facebook::jsi::WeakObject javascript)
        : type_id(std::move(type_id)),
          identity_hash(identity_hash),
          weak_global(std::move(weak_global)),
          javascript(std::move(javascript)) {}
    std::string type_id;
    jint identity_hash;
    std::shared_ptr<void> weak_global;
    facebook::jsi::WeakObject javascript;
  };

  void assert_runtime(facebook::jsi::Runtime &runtime) {
    if (runtime_ == nullptr) {
      runtime_ = &runtime;
    } else if (runtime_ != &runtime) {
      throw std::logic_error(
          "a JVM object registry cannot cross JavaScript runtimes");
    }
  }

  facebook::jsi::Runtime *runtime_ = nullptr;
  std::list<Entry> entries_;
};

class JvmObjectRegistryOwner final : public facebook::jsi::HostObject {
 public:
  explicit JvmObjectRegistryOwner(std::shared_ptr<JvmObjectRegistry> registry)
      : registry_(std::move(registry)) {
    if (!registry_) throw std::invalid_argument("JVM object registry is required");
  }

  const std::shared_ptr<JvmObjectRegistry> &registry() const noexcept {
    return registry_;
  }

 private:
  std::shared_ptr<JvmObjectRegistry> registry_;
};

std::string jvm_object_type_id(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value) {
  if (!value.isObject()) return {};
  auto object = value.getObject(runtime);
  if (!object.isHostObject<JvmObjectHandleBase>(runtime)) return {};
  return std::string(
      object.getHostObject<JvmObjectHandleBase>(runtime)->type_id());
}

ManagedJvmRef try_extract_jvm_object(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value,
    std::string_view expected_type_id) {
  if (!value.isObject()) return {};
  auto object = value.getObject(runtime);
  if (!object.isHostObject<JvmObjectHandleBase>(runtime)) return {};
  auto handle = object.getHostObject<JvmObjectHandleBase>(runtime);
  if (handle->type_id() != expected_type_id) return {};
  return handle->managed_ref();
}
'''


__all__ = ["render_jvm_object_runtime"]
