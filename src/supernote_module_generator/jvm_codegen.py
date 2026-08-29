"""Lower authoritative KSP facts into plugin-level JVM JSI call routes."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .binding_codegen import (
    Parameter,
    _jsi_argument,
    _jsi_async_helpers,
    _jsi_async_host_function,
    _jsi_async_result_value,
    _jsi_expected_type,
    _jsi_range_validation,
    _jsi_type_check,
    _jsi_value_helpers,
)
from .jvm_manifest import JvmSourceManifest
from .jvm_object_binding_codegen import render_jvm_object_bindings
from .jvm_object_runtime_codegen import render_jvm_object_runtime
from .jvm_routes import JvmRouteError, plan_jvm_routes
from .internal_codegen import internal_header_path, internal_namespace
from .semantic import (
    DeclarationRole,
    ExecutionMode,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticType,
)
from .semantic_types import SemanticTypeKind
from .source_models import (
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmOwnerForm,
    JvmOwnerSource,
)

if TYPE_CHECKING:
    from .cross_family_codegen import CrossFamilyRenderer


class JvmCodegenError(ValueError):
    pass


_CPP_TYPES = {
    SemanticType.BOOL: "bool",
    SemanticType.INT32: "std::int32_t",
    SemanticType.INT64: "std::int64_t",
    SemanticType.FLOAT32: "float",
    SemanticType.FLOAT64: "double",
    SemanticType.STRING: "std::string",
    SemanticType.BYTES: "std::vector<std::byte>",
}
_JNI_FIELDS = {
    SemanticType.BOOL: "z",
    SemanticType.INT32: "i",
    SemanticType.INT64: "j",
    SemanticType.FLOAT32: "f",
    SemanticType.FLOAT64: "d",
    SemanticType.STRING: "l",
    SemanticType.BYTES: "l",
}
_JNI_DESCRIPTOR = {
    SemanticType.VOID: "V",
    SemanticType.BOOL: "Z",
    SemanticType.INT32: "I",
    SemanticType.INT64: "J",
    SemanticType.FLOAT32: "F",
    SemanticType.FLOAT64: "D",
    SemanticType.STRING: "[B",
    SemanticType.BYTES: "[B",
}


def render_jvm_feature_jsi(
    manifest: JvmSourceManifest,
    semantic: SemanticApi,
    *,
    feature_id: str,
    module_name: str,
    conversion_digest: str | None = None,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    if manifest.feature_id != feature_id:
        raise JvmCodegenError("JVM manifest and feature identity disagree")
    suffix = _feature_suffix(feature_id)
    by_source = {
        binding.source.declaration_id: binding for binding in semantic.functions
    }
    owners_by_source = {
        owner.provenance.declaration_id: owner for owner in manifest.owners
    }
    object_wrappers: list[str] = []
    object_registrations: list[str] = []
    internal_helpers: list[str] = []
    internal_facades: list[str] = []
    for index, item in enumerate(semantic.classes):
        owner = owners_by_source.get(item.source.declaration_id)
        if owner is None:
            raise JvmCodegenError(
                f"{item.source.path}:{item.source.line}: JVM class source facts are missing"
            )
        if item.kind is SemanticClassKind.INTERNAL_SERVICE:
            helpers, facades = _render_internal_service(
                owner,
                item,
                module_name=module_name,
                feature_suffix=suffix,
                helper_prefix=f"internal_service_{index}",
                cross_family=cross_family,
            )
            internal_helpers.extend(helpers)
            internal_facades.extend(facades)
            continue
        wrapper, registration = _render_object(owner, item, index, module_name)
        object_wrappers.append(wrapper)
        object_registrations.append(registration)
    try:
        v4_jvm_routes = plan_jvm_routes(semantic, manifest.owners)
        v4_wrappers, v4_registrations = render_jvm_object_bindings(
            v4_jvm_routes,
            feature_id=feature_id,
            module_name=module_name,
        )
    except JvmRouteError as exc:
        raise JvmCodegenError(str(exc)) from exc
    object_wrappers.extend(v4_wrappers)
    object_registrations.extend(v4_registrations)
    has_v4_jvm_objects = bool(v4_jvm_routes.objects) or any(
        item.kind.value not in {"void", "scalar"}
        for route in v4_jvm_routes.functions
        for item in (*route.parameters, route.result)
    )
    registrations: list[str] = []
    has_async = any(
        route.execution is ExecutionMode.ASYNC
        for route in v4_jvm_routes.functions
    ) or any(
        route.execution is ExecutionMode.ASYNC
        for item in v4_jvm_routes.objects
        for route in item.methods
    )
    for owner in manifest.owners:
        if owner.intent.role is not DeclarationRole.ORDINARY:
            continue
        for declaration in owner.declarations:
            binding = by_source.get(declaration.provenance.declaration_id)
            if binding is None:
                continue
            if binding.capabilities.routable and not binding.capabilities.javascript_public:
                helper, facade = _render_internal_function(
                    owner,
                    declaration,
                    binding,
                    module_name=module_name,
                    feature_suffix=suffix,
                    helper_name=f"internal_function_{len(internal_helpers)}",
                    cross_family=cross_family,
                )
                internal_helpers.append(helper)
                internal_facades.append(facade)
                continue
            if any(
                item.kind.value not in {"void", "scalar"}
                for item in (
                    *(parameter.type for parameter in binding.parameters),
                    binding.result,
                )
            ):
                # Recursive V4 JVM routes own object/value/enum/array/nullable
                # conversion.  The retained scalar renderer must not guess
                # descriptors for those types.
                continue
            if binding.execution is ExecutionMode.ASYNC:
                if declaration.is_suspend:
                    registrations.append(
                        _render_suspend_function(
                            owner, declaration, binding, module_name
                        )
                    )
                else:
                    registrations.append(
                        _render_async_function(
                            owner, declaration, binding, module_name
                        )
                    )
                has_async = True
                continue
            if declaration.is_suspend:
                raise _error(declaration, "Kotlin suspend routing is not implemented yet")
            registrations.append(
                _render_function(owner, declaration, binding, module_name)
            )
    has_async = has_async or any(
        method.capabilities.javascript_public
        and method.execution is ExecutionMode.ASYNC
        for item in semantic.classes
        for method in item.methods
    )
    helpers = _jsi_value_helpers().replace(
        "auto exports = runtime.global().getPropertyAsObject(runtime, kGlobalName);",
        "auto registry = runtime.global().getPropertyAsObject(\n"
        "      runtime, kFeatureRegistryGlobal);\n"
        "  auto exports = registry.getPropertyAsObject(runtime, kFeatureId);",
    )
    if has_async:
        helpers += "\n\n" + _jsi_async_helpers()
    if conversion_digest is not None and (
        len(conversion_digest) != 64
        or any(value not in "0123456789abcdef" for value in conversion_digest)
    ):
        raise JvmCodegenError("invalid V4 conversion-plan digest")
    digest_comment = (
        ""
        if conversion_digest is None
        else (
            f"// Supernote V4 conversion plan SHA-256: {conversion_digest}\n"
            "#include <supernote/conversion.hpp>\n"
        )
    )
    object_runtime = render_jvm_object_runtime()
    v4_registry_setup = (
        "  auto object_registry = std::make_shared<JvmObjectRegistry>();\n"
        "  exports.setProperty(\n"
        "      runtime, kJvmObjectRegistryProperty,\n"
        "      Object::createFromHostObject(\n"
        "          runtime, std::make_shared<JvmObjectRegistryOwner>(object_registry)));\n"
        if has_v4_jvm_objects
        else ""
    )
    return digest_comment + f'''#include <jni.h>
#include <jsi/jsi.h>

#include <android/log.h>

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <functional>
#include <list>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <utility>
#include <vector>

#include "runtime_services.hpp"
#include <{internal_header_path(feature_id).removeprefix("include/")}>

namespace supernote::generated::jvm_feature_{suffix} {{
namespace {{

constexpr char kLogTag[] = "SupernoteV4Jvm";
constexpr char kFeatureRegistryGlobal[] =
    "__supernoteV4FeatureRegistry_63f6999c8c67";
constexpr char kFeatureId[] = {json.dumps(feature_id)};
constexpr char kJvmObjectRegistryProperty[] =
    "__supernoteV4JvmObjectRegistry_2cfbc9ce6375";

{helpers}

class JvmImplementationFailure final : public std::runtime_error {{
 public:
  using std::runtime_error::runtime_error;
}};

template <typename Callback, typename Result>
void deliver_internal_callback(Callback &callback, Result result) noexcept {{
  try {{
    callback(std::move(result));
  }} catch (const std::exception &error) {{
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag,
        "internal JVM completion callback threw: %s", error.what());
  }} catch (...) {{
    __android_log_print(
        ANDROID_LOG_ERROR, kLogTag,
        "internal JVM completion callback threw an unknown exception");
  }}
}}

class AttachedEnv {{
 public:
  AttachedEnv() {{
    vm_ = static_cast<JavaVM *>(
        supernote::runtime::process_services().java_vm());
    if (vm_ == nullptr) return;
    const auto status = vm_->GetEnv(
        reinterpret_cast<void **>(&env_), JNI_VERSION_1_6);
    if (status == JNI_EDETACHED &&
        vm_->AttachCurrentThread(&env_, nullptr) == JNI_OK) {{
      attached_ = true;
    }}
  }}
  ~AttachedEnv() {{ if (attached_) vm_->DetachCurrentThread(); }}
  JNIEnv *get() const noexcept {{ return env_; }}

 private:
  JavaVM *vm_{{nullptr}};
  JNIEnv *env_{{nullptr}};
  bool attached_{{false}};
}};

class LocalFrame {{
 public:
  explicit LocalFrame(JNIEnv *env) : env_(env) {{
    if (env_ == nullptr || env_->PushLocalFrame(32) != JNI_OK) {{
      throw std::runtime_error("cannot create JNI local-reference frame");
    }}
  }}
  ~LocalFrame() {{ if (env_ != nullptr) env_->PopLocalFrame(nullptr); }}

 private:
  JNIEnv *env_;
}};

class LocalReference {{
 public:
  LocalReference(JNIEnv *env, jobject value) : env_(env), value_(value) {{}}
  LocalReference(const LocalReference &) = delete;
  LocalReference &operator=(const LocalReference &) = delete;
  ~LocalReference() {{
    if (env_ != nullptr && value_ != nullptr) env_->DeleteLocalRef(value_);
  }}
  jobject get() const noexcept {{ return value_; }}

 private:
  JNIEnv *env_;
  jobject value_;
}};

void clear_exception(JNIEnv *env) {{
  if (env != nullptr && env->ExceptionCheck()) env->ExceptionClear();
}}

std::string implementation_exception_message(
    JNIEnv *env, jthrowable failure) {{
  if (env == nullptr || failure == nullptr) return {{}};
  LocalReference failure_class(env, env->GetObjectClass(failure));
  if (env->ExceptionCheck() || failure_class.get() == nullptr) {{
    clear_exception(env);
    return {{}};
  }}
  auto get_message = env->GetMethodID(
      static_cast<jclass>(failure_class.get()),
      "getMessage", "()Ljava/lang/String;");
  if (env->ExceptionCheck() || get_message == nullptr) {{
    clear_exception(env);
    return {{}};
  }}
  LocalReference message(
      env, env->CallObjectMethod(failure, get_message));
  if (env->ExceptionCheck() || message.get() == nullptr) {{
    clear_exception(env);
    return {{}};
  }}
  const char *utf8 = env->GetStringUTFChars(
      static_cast<jstring>(message.get()), nullptr);
  if (env->ExceptionCheck() || utf8 == nullptr) {{
    clear_exception(env);
    return {{}};
  }}
  std::string result;
  try {{
    result.assign(utf8);
  }} catch (...) {{
    env->ReleaseStringUTFChars(
        static_cast<jstring>(message.get()), utf8);
    clear_exception(env);
    throw;
  }}
  env->ReleaseStringUTFChars(
      static_cast<jstring>(message.get()), utf8);
  if (env->ExceptionCheck()) clear_exception(env);
  return result;
}}

std::shared_ptr<void> retain_global(JNIEnv *env, jobject value) {{
  if (env == nullptr || value == nullptr) {{
    throw std::runtime_error("cannot retain a null JVM object");
  }}
  auto *global = env->NewGlobalRef(value);
  if (global == nullptr) {{
    clear_exception(env);
    throw std::runtime_error("cannot allocate a JNI global reference");
  }}
  auto cleanup = supernote::runtime::process_services().cleanup();
  return std::shared_ptr<void>(global, [cleanup](void *raw) {{
    auto release = [raw] {{
      AttachedEnv attached;
      if (auto *env = attached.get()) {{
        env->DeleteGlobalRef(static_cast<jobject>(raw));
      }}
    }};
    if (!cleanup || !cleanup->submit(release)) release();
  }});
}}

{object_runtime}

struct JvmRoute {{
  std::shared_ptr<void> adapter_class;
  jmethodID method{{nullptr}};
}};

class LazyJvmRoute {{
 public:
  LazyJvmRoute(std::string adapter_class, std::string descriptor,
               std::string method_name = "invoke")
      : adapter_class_(std::move(adapter_class)),
        descriptor_(std::move(descriptor)),
        method_name_(std::move(method_name)) {{}}

  std::shared_ptr<JvmRoute> get(
      const std::shared_ptr<supernote::runtime::FeatureSession> &feature) {{
    std::lock_guard lock(mutex_);
    if (route_) return route_;
    auto runtime = feature ? feature->runtime() : nullptr;
    if (!runtime || !runtime->active()) {{
      throw std::runtime_error("feature runtime is closed");
    }}
    auto loader = runtime->plugin_class_loader();
    if (!loader) throw std::runtime_error("plugin ClassLoader is unavailable");
    AttachedEnv attached;
    auto *env = attached.get();
    if (env == nullptr) throw std::runtime_error("cannot attach to JavaVM");
    LocalFrame frame(env);
    auto loader_class = env->GetObjectClass(static_cast<jobject>(loader.get()));
    auto load_class = loader_class == nullptr
        ? nullptr
        : env->GetMethodID(
              loader_class, "loadClass", "(Ljava/lang/String;)Ljava/lang/Class;");
    auto class_name = env->NewStringUTF(adapter_class_.c_str());
    auto local_class = load_class == nullptr || class_name == nullptr
        ? nullptr
        : env->CallObjectMethod(
              static_cast<jobject>(loader.get()), load_class, class_name);
    if (env->ExceptionCheck() || local_class == nullptr) {{
      clear_exception(env);
      throw std::runtime_error("cannot resolve generated JVM adapter class");
    }}
    auto method = env->GetStaticMethodID(
        static_cast<jclass>(local_class), method_name_.c_str(),
        descriptor_.c_str());
    if (env->ExceptionCheck() || method == nullptr) {{
      clear_exception(env);
      throw std::runtime_error("cannot resolve generated JVM adapter method");
    }}
    route_ = std::make_shared<JvmRoute>(JvmRoute{{
        retain_global(env, local_class), method}});
    return route_;
  }}

 private:
  std::mutex mutex_;
  std::string adapter_class_;
  std::string descriptor_;
  std::string method_name_;
  std::shared_ptr<JvmRoute> route_;
}};

struct JvmOwner {{
  explicit JvmOwner(std::shared_ptr<void> value) : value(std::move(value)) {{}}
  std::shared_ptr<void> value;
}};

std::vector<std::byte> read_byte_array(JNIEnv *env, jbyteArray value) {{
  if (value == nullptr) throw std::runtime_error("JVM adapter returned null");
  const auto length = env->GetArrayLength(value);
  if (env->ExceptionCheck() || length < 0) {{
    clear_exception(env);
    throw std::runtime_error("cannot read JVM byte-array length");
  }}
  std::vector<std::byte> result(static_cast<std::size_t>(length));
  if (length != 0) {{
    env->GetByteArrayRegion(
        value, 0, length, reinterpret_cast<jbyte *>(result.data()));
    if (env->ExceptionCheck()) {{
      clear_exception(env);
      throw std::runtime_error("cannot copy JVM byte-array result");
    }}
  }}
  return result;
}}

jbyteArray write_byte_array(
    JNIEnv *env, const std::byte *data, std::size_t size) {{
  if (size > static_cast<std::size_t>(std::numeric_limits<jsize>::max())) {{
    throw std::runtime_error("JVM byte-array argument is too large");
  }}
  auto result = env->NewByteArray(static_cast<jsize>(size));
  if (result == nullptr) {{
    clear_exception(env);
    throw std::runtime_error("cannot allocate JVM byte-array argument");
  }}
  if (size != 0) {{
    env->SetByteArrayRegion(
        result, 0, static_cast<jsize>(size),
        reinterpret_cast<const jbyte *>(data));
    if (env->ExceptionCheck()) {{
      clear_exception(env);
      throw std::runtime_error("cannot copy JVM byte-array argument");
    }}
  }}
  return result;
}}

void require_no_implementation_exception(JNIEnv *env) {{
  if (!env->ExceptionCheck()) return;
  LocalReference failure(env, env->ExceptionOccurred());
  env->ExceptionClear();
  auto message = implementation_exception_message(
      env, static_cast<jthrowable>(failure.get()));
  throw JvmImplementationFailure(
      message.empty()
          ? "Kotlin/Java implementation failed"
          : "Kotlin/Java implementation failed: " + message);
}}

{chr(10).join(object_wrappers)}

}}  // namespace

{cross_family.render_helpers() if cross_family is not None else ""}
{chr(10).join(internal_helpers)}

void register_jvm_feature(
    facebook::jsi::Runtime &runtime,
    facebook::jsi::Object &feature_registry,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature_session) {{
  using facebook::jsi::Function;
  using facebook::jsi::Object;
  using facebook::jsi::PropNameID;
  using facebook::jsi::String;
  using facebook::jsi::Value;

  auto exports = feature_registry.getPropertyAsObject(runtime, kFeatureId);
{v4_registry_setup}{chr(10).join(registrations)}
{chr(10).join(object_registrations)}
}}

}}  // namespace supernote::generated::jvm_feature_{suffix}

namespace supernote::internal::{internal_namespace(module_name)} {{

{chr(10).join(internal_facades)}

}}  // namespace supernote::internal::{internal_namespace(module_name)}
'''


def _render_internal_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    *,
    module_name: str,
    feature_suffix: str,
    helper_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> tuple[str, str]:
    if source.is_suspend:
        return _render_internal_suspend_route(
            owner,
            source,
            binding,
            feature_suffix=feature_suffix,
            helper_name=helper_name,
            facade_name=binding.name,
            cross_family=cross_family,
        )
    helper = _render_internal_blocking_helper(
        owner,
        source,
        binding,
        helper_name=helper_name,
        cross_family=cross_family,
    )
    if binding.execution is ExecutionMode.ASYNC:
        facade = _render_internal_blocking_async_facade(
            binding,
            feature_suffix=feature_suffix,
            helper_name=helper_name,
            facade_name=binding.name,
            cross_family=cross_family,
        )
    else:
        facade = _render_internal_sync_facade(
            binding,
            feature_suffix=feature_suffix,
            helper_name=helper_name,
            facade_name=binding.name,
            cross_family=cross_family,
        )
    return helper, facade


def _render_internal_service(
    owner: JvmOwnerSource,
    item: SemanticClass,
    *,
    module_name: str,
    feature_suffix: str,
    helper_prefix: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> tuple[list[str], list[str]]:
    del module_name
    declarations = {
        declaration.provenance.declaration_id: declaration
        for declaration in owner.declarations
    }
    helpers = []
    facades = []
    for index, binding in enumerate(item.methods):
        source = declarations.get(binding.source.declaration_id)
        if source is None:
            raise JvmCodegenError(
                f"{binding.source.path}:{binding.source.line}: JVM service "
                "method source facts are missing"
            )
        helper_name = f"{helper_prefix}_method_{index}"
        facade_name = f"{item.name}::{binding.name}"
        if source.is_suspend:
            helper, facade = _render_internal_suspend_route(
                owner,
                source,
                binding,
                feature_suffix=feature_suffix,
                helper_name=helper_name,
                facade_name=facade_name,
                cross_family=cross_family,
            )
        else:
            helper = _render_internal_blocking_helper(
                owner,
                source,
                binding,
                helper_name=helper_name,
                cross_family=cross_family,
            )
            if binding.execution is ExecutionMode.ASYNC:
                facade = _render_internal_blocking_async_facade(
                    binding,
                    feature_suffix=feature_suffix,
                    helper_name=helper_name,
                    facade_name=facade_name,
                    cross_family=cross_family,
                )
            else:
                facade = _render_internal_sync_facade(
                    binding,
                    feature_suffix=feature_suffix,
                    helper_name=helper_name,
                    facade_name=facade_name,
                    cross_family=cross_family,
                )
        helpers.append(helper)
        facades.append(facade)
    return helpers, facades


def _render_internal_blocking_helper(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    *,
    helper_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    takes_owner = owner.form is JvmOwnerForm.CLASS
    result_type = _internal_cpp_type(binding.result, cross_family)
    parameters = _internal_parameters(binding, cross_family)
    signature_tail = f", {parameters}" if parameters else ""
    descriptor = (
        cross_family.descriptor(
            binding, owner.owner_class if takes_owner else None
        )
        if cross_family is not None
        else _adapter_descriptor(binding, owner if takes_owner else None)
    )
    route_key = f"jvm-route:{source.provenance.declaration_id}"
    owner_setup = _render_internal_owner_setup(owner) if takes_owner else ""
    invocation = (
        cross_family.worker_invocation(binding, takes_owner)
        if cross_family is not None
        else _worker_invocation(binding, takes_owner)
    )
    release_before_call = "" if cross_family is not None else "  feature.reset();\n"
    return f'''static {result_type} {helper_name}(
    std::shared_ptr<supernote::runtime::FeatureSession> feature{signature_tail}) {{
  auto route = feature->service<LazyJvmRoute>(
      {json.dumps(route_key)}, [] {{
        return std::make_shared<LazyJvmRoute>(
            {json.dumps(_adapter_class(source.adapter_identity))},
            {json.dumps(descriptor)});
      }});
{owner_setup}  auto resolved = route->get(feature);
{release_before_call}
  AttachedEnv attached;
  auto *env = attached.get();
  if (env == nullptr) {{
    throw std::runtime_error("cannot attach to JavaVM");
  }}
  LocalFrame frame(env);
{_indent(invocation, 2)}
}}'''


def _render_internal_owner_setup(owner: JvmOwnerSource) -> str:
    constructor = _owner_constructor(owner)
    route_key = f"jvm-constructor-route:{constructor.provenance.declaration_id}"
    route = f'''  auto owner_route = feature->service<LazyJvmRoute>(
      {json.dumps(route_key)}, [] {{
        return std::make_shared<LazyJvmRoute>(
            {json.dumps(_adapter_class(constructor.adapter_identity))},
            {json.dumps(_constructor_descriptor(owner))});
      }});
'''
    setup = _render_owner_setup(owner).replace("feature_session", "feature")
    return route + _indent(setup, 2) + "\n"


def _render_internal_sync_facade(
    binding: SemanticBinding,
    *,
    feature_suffix: str,
    helper_name: str,
    facade_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    result_type = _internal_cpp_type(binding.result, cross_family)
    parameters = _internal_parameters(binding, cross_family)
    arguments = ", ".join(item.name for item in binding.parameters)
    call_tail = f", {arguments}" if arguments else ""
    invoke = (
        f"route::{helper_name}(std::move(feature){call_tail});\n    return;"
        if binding.result is SemanticType.VOID
        else f"return route::{helper_name}(std::move(feature){call_tail});"
    )
    return f'''{result_type} {facade_name}({parameters}) {{
  auto feature = supernote::runtime::current_feature_session();
  if (!feature ||
      feature->state() != supernote::runtime::FeatureState::ACTIVE) {{
    throw supernote::Error(
        supernote::ErrorCode::FEATURE_CLOSED, "feature is closed");
  }}
  namespace route = supernote::generated::jvm_feature_{feature_suffix};
  try {{
    {invoke}
  }} catch (const route::JvmImplementationFailure &error) {{
    throw supernote::Error(
        supernote::ErrorCode::IMPLEMENTATION_ERROR, error.what());
  }} catch (const supernote::Error &) {{
    throw;
  }} catch (const std::exception &error) {{
    throw supernote::Error(supernote::ErrorCode::INTERNAL, error.what());
  }} catch (...) {{
    throw supernote::Error(
        supernote::ErrorCode::INTERNAL,
        "unknown generated JVM route failure");
  }}
}}'''


def _render_internal_blocking_async_facade(
    binding: SemanticBinding,
    *,
    feature_suffix: str,
    helper_name: str,
    facade_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    parameters = _internal_parameters(binding, cross_family)
    callback = _internal_callback_type(binding.result, cross_family)
    signature = f"{parameters}, {callback} completion" if parameters else f"{callback} completion"
    captures = ", ".join(
        f"{item.name} = std::move({item.name})" for item in binding.parameters
    )
    capture_tail = f", {captures}" if captures else ""
    arguments = ", ".join(item.name for item in binding.parameters)
    call_tail = f", {arguments}" if arguments else ""
    result_type = _internal_result_type(binding.result, cross_family)
    retained_types = ", ".join(
        _internal_cpp_type(item.type, cross_family)
        for item in binding.parameters
    )
    retained_values = ", ".join(item.name for item in binding.parameters)
    retained_state = (
        "  auto retained_input_state = std::make_shared<std::tuple<\n"
        f"      {retained_types}>>({retained_values});\n"
        if retained_types
        else "  auto retained_input_state = std::make_shared<std::tuple<>>();\n"
    )
    if binding.result is SemanticType.VOID:
        invoke = (
            f"route::{helper_name}(std::move(feature){call_tail});\n"
            "      outcome = supernote::Result<void>::success();"
        )
    else:
        invoke = (
            f"outcome = {result_type}::success(\n"
            f"          route::{helper_name}(std::move(feature){call_tail}));"
        )
    return f'''void {facade_name}({signature}) {{
  auto feature = supernote::runtime::current_feature_session();
  if (!feature ||
      feature->state() != supernote::runtime::FeatureState::ACTIVE) {{
    throw supernote::Error(
        supernote::ErrorCode::FEATURE_CLOSED, "feature is closed");
  }}
  if (!completion) {{
    throw supernote::Error(
        supernote::ErrorCode::INTERNAL, "completion callback is empty");
  }}
  auto callback = std::make_shared<decltype(completion)>(
      std::move(completion));
{retained_state.rstrip()}
  auto operation = feature->accept({{}}, std::move(callback));
  if (!operation) {{
    throw supernote::Error(
        supernote::ErrorCode::FEATURE_CLOSED, "feature is closed");
  }}
  operation->set_retained_state(retained_input_state);
  std::weak_ptr<supernote::runtime::FeatureSession> weak_feature = feature;
  auto work = supernote::runtime::process_services().workers().submit(
      [operation, weak_feature{capture_tail}](
          supernote::runtime::CancellationToken executor_cancel) mutable {{
        if (executor_cancel.is_cancelled() ||
            operation->cancellation_token().is_cancelled()) return;
        auto feature = weak_feature.lock();
        if (!feature ||
            feature->state() != supernote::runtime::FeatureState::ACTIVE) return;
        supernote::runtime::FeatureCallScope scope(feature);
        namespace route =
            supernote::generated::jvm_feature_{feature_suffix};
        {result_type} outcome = {result_type}::failure(supernote::Error(
            supernote::ErrorCode::INTERNAL, "operation did not complete"));
        try {{
      {invoke}
        }} catch (const route::JvmImplementationFailure &error) {{
          outcome = {result_type}::failure(supernote::Error(
              supernote::ErrorCode::IMPLEMENTATION_ERROR, error.what()));
        }} catch (const std::exception &error) {{
          outcome = {result_type}::failure(supernote::Error(
              supernote::ErrorCode::INTERNAL, error.what()));
        }} catch (...) {{
          outcome = {result_type}::failure(supernote::Error(
              supernote::ErrorCode::INTERNAL,
              "unknown generated JVM route failure"));
        }}
        feature.reset();
        auto completion_feature = weak_feature.lock();
        if (operation->cancellation_token().is_cancelled() ||
            !completion_feature ||
            !completion_feature->claim_internal_completion(operation)) return;
        auto callback = std::static_pointer_cast<decltype(completion)>(
            operation->take_internal_completion());
        if (!callback) return;
        route::deliver_internal_callback(*callback, std::move(outcome));
      }});
  operation->set_work(work);
  if (!work.accepted()) {{
    namespace route = supernote::generated::jvm_feature_{feature_suffix};
    auto cleanup = supernote::runtime::process_services().cleanup();
    auto exhausted = [operation, weak_feature]() mutable {{
      auto completion_feature = weak_feature.lock();
      if (!completion_feature ||
          !completion_feature->claim_internal_completion(operation)) return;
      auto callback = std::static_pointer_cast<decltype(completion)>(
          operation->take_internal_completion());
      if (!callback) return;
      auto result = {result_type}::failure(supernote::Error(
          supernote::ErrorCode::RESOURCE_EXHAUSTED,
          "Supernote worker queue is full"));
      route::deliver_internal_callback(*callback, std::move(result));
    }};
    if (!cleanup || !cleanup->submit(std::move(exhausted))) {{
      if (feature->claim_internal_completion(operation)) {{
        operation->take_internal_completion();
      }}
      throw supernote::Error(
          supernote::ErrorCode::INTERNAL,
          "cannot schedule internal completion callback");
    }}
  }}
}}'''


def _render_internal_suspend_route(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    *,
    feature_suffix: str,
    helper_name: str,
    facade_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> tuple[str, str]:
    if binding.execution is not ExecutionMode.ASYNC:
        raise _error(
            source,
            "a Kotlin suspend implementation requires SupernotePluginAsync intent",
        )
    takes_owner = owner.form is JvmOwnerForm.CLASS
    parameters = _internal_parameters(binding, cross_family)
    signature_tail = f", {parameters}" if parameters else ""
    route_key = f"jvm-route:{source.provenance.declaration_id}"
    cancel_key = "jvm-route:supernote-coroutine-cancel"
    owner_setup = _render_internal_owner_setup(owner) if takes_owner else ""
    offset = 1 if takes_owner else 0
    argument_count = len(binding.parameters) + offset + 1
    argument_rows = (
        cross_family.suspend_worker_arguments(binding, takes_owner).splitlines()
        if cross_family is not None
        else [
            f"jvalue jvm_arguments[{max(1, argument_count)}]{{}};",
            *(
                ["jvm_arguments[0].l = static_cast<jobject>(owner->value.get());"]
                if takes_owner
                else []
            ),
            *(
                line.strip()
                for line in _owned_argument_lines(
                    binding.parameters, offset, ""
                )
            ),
            f"jvm_arguments[{argument_count - 1}].j = static_cast<jlong>(completion_id);",
        ]
    )
    descriptor = (
        cross_family.suspend_descriptor(
            binding, owner.owner_class if takes_owner else None
        )
        if cross_family is not None
        else _suspend_adapter_descriptor(
            binding, owner if takes_owner else None
        )
    )
    release_before_call = "" if cross_family is not None else "  feature.reset();\n"
    helper = f'''static std::pair<std::shared_ptr<void>, std::shared_ptr<JvmRoute>>
{helper_name}(
    std::shared_ptr<supernote::runtime::FeatureSession> feature,
    supernote::runtime::SessionId completion_id{signature_tail}) {{
  auto route = feature->service<LazyJvmRoute>(
      {json.dumps(route_key)}, [] {{
        return std::make_shared<LazyJvmRoute>(
            {json.dumps(_adapter_class(source.adapter_identity))},
            {json.dumps(descriptor)});
      }});
  auto cancel_route = feature->service<LazyJvmRoute>(
      {json.dumps(cancel_key)}, [] {{
        return std::make_shared<LazyJvmRoute>(
            "supernote.generated.runtime.SupernoteCoroutineBridge",
            "(Lkotlinx/coroutines/Job;)V", "cancel");
      }});
{owner_setup}  auto resolved = route->get(feature);
  auto cancel_resolved = cancel_route->get(feature);
{release_before_call}
  AttachedEnv attached;
  auto *env = attached.get();
  if (env == nullptr) {{
    throw std::runtime_error("cannot attach to JavaVM");
  }}
  LocalFrame frame(env);
{chr(10).join('  ' + line for line in argument_rows)}
  auto local_job = env->CallStaticObjectMethodA(
      static_cast<jclass>(resolved->adapter_class.get()),
      resolved->method, jvm_arguments);
  if (env->ExceptionCheck()) require_no_implementation_exception(env);
  if (local_job == nullptr) {{
    throw std::runtime_error(
        "cannot launch generated Kotlin coroutine adapter");
  }}
  return {{retain_global(env, local_job), std::move(cancel_resolved)}};
}}'''
    facade = _render_internal_suspend_facade(
        binding,
        feature_suffix=feature_suffix,
        helper_name=helper_name,
        facade_name=facade_name,
        cross_family=cross_family,
    )
    return helper, facade


def _render_internal_suspend_facade(
    binding: SemanticBinding,
    *,
    feature_suffix: str,
    helper_name: str,
    facade_name: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    parameters = _internal_parameters(binding, cross_family)
    callback_type = _internal_callback_type(binding.result, cross_family)
    signature = (
        f"{parameters}, {callback_type} completion"
        if parameters
        else f"{callback_type} completion"
    )
    captures = ", ".join(
        f"{item.name} = std::move({item.name})" for item in binding.parameters
    )
    capture_tail = f", {captures}" if captures else ""
    arguments = ", ".join(item.name for item in binding.parameters)
    call_tail = f", {arguments}" if arguments else ""
    result_type = _internal_result_type(binding.result, cross_family)
    decode = _internal_suspend_decode(
        binding.result, result_type, cross_family
    )
    retained_types = ", ".join(
        _internal_cpp_type(item.type, cross_family)
        for item in binding.parameters
    )
    retained_values = ", ".join(item.name for item in binding.parameters)
    retained_state = (
        "  auto retained_input_state = std::make_shared<std::tuple<\n"
        f"      {retained_types}>>({retained_values});\n"
        if retained_types
        else "  auto retained_input_state = std::make_shared<std::tuple<>>();\n"
    )
    return f'''void {facade_name}({signature}) {{
  auto feature = supernote::runtime::current_feature_session();
  if (!feature ||
      feature->state() != supernote::runtime::FeatureState::ACTIVE) {{
    throw supernote::Error(
        supernote::ErrorCode::FEATURE_CLOSED, "feature is closed");
  }}
  if (!completion) {{
    throw supernote::Error(
        supernote::ErrorCode::INTERNAL, "completion callback is empty");
  }}
  auto callback = std::make_shared<decltype(completion)>(
      std::move(completion));
{retained_state.rstrip()}
  auto operation = feature->accept({{}}, std::move(callback));
  if (!operation) {{
    throw supernote::Error(
        supernote::ErrorCode::FEATURE_CLOSED, "feature is closed");
  }}
  operation->set_retained_state(retained_input_state);
  std::weak_ptr<supernote::runtime::FeatureSession> weak_feature = feature;
  namespace route = supernote::generated::jvm_feature_{feature_suffix};
  const auto completion_id =
      supernote::runtime::process_services().register_jvm_async_completion(
          [operation, weak_feature](
              void *environment, void *result,
              std::string error_code, std::string error_message) mutable {{
            if (operation->cancellation_token().is_cancelled()) return;
            {result_type} outcome = {result_type}::failure(supernote::Error(
                supernote::ErrorCode::INTERNAL,
                "Kotlin coroutine did not complete"));
            if (!error_code.empty()) {{
              auto code = supernote::ErrorCode::INTERNAL;
              if (error_code == "CANCELLED") {{
                code = supernote::ErrorCode::CANCELLED;
              }} else if (error_code == "RESOURCE_EXHAUSTED") {{
                code = supernote::ErrorCode::RESOURCE_EXHAUSTED;
              }} else if (error_code == "FEATURE_CLOSED") {{
                code = supernote::ErrorCode::FEATURE_CLOSED;
              }} else if (error_code == "IMPLEMENTATION_ERROR") {{
                code = supernote::ErrorCode::IMPLEMENTATION_ERROR;
              }}
              outcome = {result_type}::failure(supernote::Error(
                  code,
                  error_message.empty()
                      ? "Kotlin coroutine failed"
                      : std::move(error_message)));
            }} else {{
              try {{
{decode}
              }} catch (const std::exception &error) {{
                outcome = {result_type}::failure(supernote::Error(
                    supernote::ErrorCode::INTERNAL, error.what()));
              }} catch (...) {{
                outcome = {result_type}::failure(supernote::Error(
                    supernote::ErrorCode::INTERNAL,
                    "cannot decode Kotlin coroutine result"));
              }}
            }}
            if (operation->cancellation_token().is_cancelled()) return;
            auto feature = weak_feature.lock();
            if (!feature || !feature->claim_internal_completion(operation)) return;
            auto callback = std::static_pointer_cast<decltype(completion)>(
                operation->take_internal_completion());
            if (!callback) return;
            route::deliver_internal_callback(*callback, std::move(outcome));
          }});
  operation->set_cancel_hook([completion_id] {{
    supernote::runtime::process_services()
        .discard_jvm_async_completion(completion_id);
  }});
  auto work = supernote::runtime::process_services().workers().submit(
      [operation, weak_feature, completion_id{capture_tail}](
          supernote::runtime::CancellationToken executor_cancel) mutable {{
        if (executor_cancel.is_cancelled() ||
            operation->cancellation_token().is_cancelled()) return;
        auto feature = weak_feature.lock();
        if (!feature ||
            feature->state() != supernote::runtime::FeatureState::ACTIVE) return;
        supernote::runtime::FeatureCallScope scope(feature);
        namespace route = supernote::generated::jvm_feature_{feature_suffix};
        try {{
          auto launched = route::{helper_name}(
              std::move(feature), completion_id{call_tail});
          auto job = std::move(launched.first);
          auto cancel_route = std::move(launched.second);
          operation->set_cancel_hook(
              [completion_id, job, cancel_route] {{
                supernote::runtime::process_services()
                    .discard_jvm_async_completion(completion_id);
                try {{
                  route::AttachedEnv attached;
                  auto *env = attached.get();
                  if (env == nullptr) return;
                  route::LocalFrame frame(env);
                  jvalue arguments[1]{{}};
                  arguments[0].l = static_cast<jobject>(job.get());
                  env->CallStaticVoidMethodA(
                      static_cast<jclass>(
                          cancel_route->adapter_class.get()),
                      cancel_route->method, arguments);
                  route::clear_exception(env);
                }} catch (...) {{}}
              }});
        }} catch (const route::JvmImplementationFailure &error) {{
          supernote::runtime::process_services().complete_jvm_async(
              completion_id, nullptr, nullptr,
              "IMPLEMENTATION_ERROR", error.what());
        }} catch (const std::exception &error) {{
          supernote::runtime::process_services().complete_jvm_async(
              completion_id, nullptr, nullptr, "INTERNAL", error.what());
        }} catch (...) {{
          supernote::runtime::process_services().complete_jvm_async(
              completion_id, nullptr, nullptr, "INTERNAL",
              "cannot launch Kotlin coroutine adapter");
        }}
      }});
  operation->set_work(work);
  if (!work.accepted()) {{
    supernote::runtime::process_services().complete_jvm_async(
        completion_id, nullptr, nullptr, "RESOURCE_EXHAUSTED",
        "Supernote worker queue is full");
  }}
}}'''


def _internal_suspend_decode(
    result: SemanticType,
    result_type: str,
    cross_family: "CrossFamilyRenderer | None" = None,
) -> str:
    indent = " " * 16
    if result is SemanticType.VOID:
        return f"{indent}outcome = {result_type}::success();"
    if cross_family is not None:
        expression = cross_family.suspend_result_expression(
            result,
            expression="result",
            feature="conversion_feature",
            budget="cross_budget",
        )
        lines = [
                f"{indent}auto *env = static_cast<JNIEnv *>(environment);",
                f"{indent}if (env == nullptr) {{",
                f'{indent}  throw std::runtime_error("Kotlin coroutine result has no JNI environment");',
                f"{indent}}}",
        ]
        if result.kind is not SemanticTypeKind.NULLABLE:
            lines.extend(
                [
                    f"{indent}if (result == nullptr) {{",
                    f'{indent}  throw std::runtime_error("Kotlin coroutine returned null");',
                    f"{indent}}}",
                ]
            )
        lines.extend(
            [
                f"{indent}auto conversion_feature = weak_feature.lock();",
                f"{indent}if (!conversion_feature ||",
                f"{indent}    conversion_feature->state() !=",
                f"{indent}        supernote::runtime::FeatureState::ACTIVE) {{",
                f'{indent}  throw std::runtime_error("feature closed before coroutine result conversion");',
                f"{indent}}}",
                f"{indent}route::LocalFrame frame(env);",
                f"{indent}supernote::conversion::Budget cross_budget;",
                f"{indent}outcome = {result_type}::success({expression});",
            ]
        )
        return "\n".join(lines)
    lines = [
        f"{indent}auto *env = static_cast<JNIEnv *>(environment);",
        f"{indent}auto object = static_cast<jobject>(result);",
        f"{indent}if (env == nullptr || object == nullptr) {{",
        f'{indent}  throw std::runtime_error("Kotlin coroutine returned null");',
        f"{indent}}}",
        f"{indent}route::LocalFrame frame(env);",
    ]
    if result in {SemanticType.STRING, SemanticType.BYTES}:
        lines.append(
            f"{indent}const auto bytes = route::read_byte_array("
            "env, static_cast<jbyteArray>(object));"
        )
        value = (
            "std::string(reinterpret_cast<const char *>(bytes.data()), "
            "bytes.size())"
            if result is SemanticType.STRING
            else "bytes"
        )
        lines.append(f"{indent}outcome = {result_type}::success({value});")
        return "\n".join(lines)
    method, descriptor, call, conversion = {
        SemanticType.BOOL: (
            "booleanValue", "()Z", "CallBooleanMethod", "value == JNI_TRUE"
        ),
        SemanticType.INT32: (
            "intValue", "()I", "CallIntMethod", "static_cast<std::int32_t>(value)"
        ),
        SemanticType.INT64: (
            "longValue", "()J", "CallLongMethod", "static_cast<std::int64_t>(value)"
        ),
        SemanticType.FLOAT32: (
            "floatValue", "()F", "CallFloatMethod", "static_cast<float>(value)"
        ),
        SemanticType.FLOAT64: (
            "doubleValue", "()D", "CallDoubleMethod", "static_cast<double>(value)"
        ),
    }[result]
    lines.extend(
        [
            f"{indent}auto value_class = env->GetObjectClass(object);",
            f"{indent}auto unbox = value_class == nullptr",
            f"{indent}    ? nullptr",
            f"{indent}    : env->GetMethodID(",
            f"{indent}          value_class, {json.dumps(method)}, {json.dumps(descriptor)});",
            f"{indent}if (unbox == nullptr) {{",
            f"{indent}  route::clear_exception(env);",
            f'{indent}  throw std::runtime_error("cannot unbox Kotlin coroutine result");',
            f"{indent}}}",
            f"{indent}auto value = env->{call}(object, unbox);",
            f"{indent}if (env->ExceptionCheck()) {{",
            f"{indent}  route::clear_exception(env);",
            f'{indent}  throw std::runtime_error("cannot read Kotlin coroutine result");',
            f"{indent}}}",
            f"{indent}outcome = {result_type}::success({conversion});",
        ]
    )
    return "\n".join(lines)


def _internal_parameters(
    binding: SemanticBinding, cross_family: "CrossFamilyRenderer | None" = None
) -> str:
    return ", ".join(
        f"{_internal_cpp_type(item.type, cross_family)} {item.name}"
        for item in binding.parameters
    )


def _internal_cpp_type(
    value: SemanticType, cross_family: "CrossFamilyRenderer | None" = None
) -> str:
    if cross_family is not None:
        return cross_family.cpp_type(value)
    return "void" if value is SemanticType.VOID else _CPP_TYPES[value]


def _internal_result_type(
    value: SemanticType, cross_family: "CrossFamilyRenderer | None" = None
) -> str:
    return (
        "supernote::Result<void>"
        if value is SemanticType.VOID
        else f"supernote::Result<{_internal_cpp_type(value, cross_family)}>"
    )


def _internal_callback_type(
    value: SemanticType, cross_family: "CrossFamilyRenderer | None" = None
) -> str:
    return f"std::function<void({_internal_result_type(value, cross_family)})>"


def _render_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    module_name: str,
) -> str:
    route_class = _adapter_class(source.adapter_identity)
    takes_owner = owner.form is JvmOwnerForm.CLASS
    descriptor = _adapter_descriptor(binding, owner if takes_owner else None)
    captures = ["route", "feature_session"]
    setup = [
        f"    auto route = std::make_shared<LazyJvmRoute>(\n"
        f"        {json.dumps(route_class)}, {json.dumps(descriptor)});"
    ]
    owner_setup = ""
    if takes_owner:
        constructor = _owner_constructor(owner)
        constructor_class = _adapter_class(constructor.adapter_identity)
        constructor_descriptor = _constructor_descriptor(owner)
        setup.append(
            "    auto owner_route = std::make_shared<LazyJvmRoute>(\n"
            f"        {json.dumps(constructor_class)}, "
            f"{json.dumps(constructor_descriptor)});"
        )
        captures.append("owner_route")
        owner_setup = _render_owner_setup(owner)
    validations = _validations(binding, f"{module_name}.{binding.name}")
    invocation = _invocation(binding, takes_owner)
    preflight = _scalar_preflight_statements(
        binding, f"{module_name}.{binding.name}"
    )
    return (
        "  {\n"
        + "\n".join(setup)
        + "\n    auto function = Function::createFromHostFunction(\n"
        "        runtime,\n"
        f"        PropNameID::forAscii(runtime, {json.dumps(binding.name)}),\n"
        f"        {len(binding.parameters)},\n"
        f"        [{', '.join(captures)}](facebook::jsi::Runtime &runtime,\n"
        "           const Value &,\n"
        "           const Value *arguments,\n"
        "           std::size_t argument_count) -> Value {\n"
        f"{validations}\n"
        "          try {\n"
        "            if (!feature_session ||\n"
        "                feature_session->state() != supernote::runtime::FeatureState::ACTIVE) {\n"
        "              supernote_throw_error(\n"
        "                  runtime, \"FEATURE_CLOSED\", \"feature is closed\");\n"
        "            }\n"
        f"{owner_setup}"
        "            auto resolved = route->get(feature_session);\n"
        "            AttachedEnv attached;\n"
        "            auto *env = attached.get();\n"
        "            if (env == nullptr) {\n"
        "              throw std::runtime_error(\"cannot attach to JavaVM\");\n"
        "            }\n"
        "            LocalFrame frame(env);\n"
        f"{invocation}\n"
        "          } catch (const facebook::jsi::JSError &) {\n"
        "            throw;\n"
        "          } catch (const JvmImplementationFailure &error) {\n"
        "            supernote_throw_error(\n"
        "                runtime, \"IMPLEMENTATION_ERROR\", error.what());\n"
        "          } catch (const std::exception &error) {\n"
        "            supernote_throw_error(runtime, \"INTERNAL\", error.what());\n"
        "          }\n"
        "        });\n"
        f"{preflight}\n"
        f"    exports.setProperty(runtime, {json.dumps(binding.name)}, "
        "std::move(function));\n"
        "  }"
    )


def _render_async_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    module_name: str,
) -> str:
    route_class = _adapter_class(source.adapter_identity)
    takes_owner = owner.form is JvmOwnerForm.CLASS
    descriptor = _adapter_descriptor(binding, owner if takes_owner else None)
    setup = [
        f"    auto route = std::make_shared<LazyJvmRoute>(\n"
        f"        {json.dumps(route_class)}, {json.dumps(descriptor)});"
    ]
    invoker_captures = ["route"]
    owner_setup = ""
    if takes_owner:
        constructor = _owner_constructor(owner)
        setup.append(
            "    auto owner_route = std::make_shared<LazyJvmRoute>(\n"
            f"        {json.dumps(_adapter_class(constructor.adapter_identity))}, "
            f"{json.dumps(_constructor_descriptor(owner))});"
        )
        invoker_captures.append("owner_route")
        owner_setup = _render_owner_setup(owner).replace(
            "feature_session", "implementation_feature"
        )
    result_type = (
        "void"
        if binding.result is SemanticType.VOID
        else _CPP_TYPES[binding.result]
    )
    parameter_rows = [
        "std::shared_ptr<supernote::runtime::FeatureSession> "
        "implementation_feature"
    ]
    parameter_rows.extend(
        _owned_cpp_parameter(parameter.type, parameter.name)
        for parameter in binding.parameters
    )
    invocation = _worker_invocation(binding, takes_owner)
    invoker = (
        f"    auto invoke = [{', '.join(invoker_captures)}](\n"
        f"        {', '.join(parameter_rows)}) -> {result_type} {{\n"
        f"{owner_setup}"
        "      auto resolved = route->get(implementation_feature);\n"
        "      implementation_feature.reset();\n"
        "      AttachedEnv attached;\n"
        "      auto *env = attached.get();\n"
        "      if (env == nullptr) {\n"
        "        throw std::runtime_error(\"cannot attach to JavaVM\");\n"
        "      }\n"
        "      LocalFrame frame(env);\n"
        f"{invocation}\n"
        "    };"
    )
    call = "invoke(std::move(implementation_feature)"
    if binding.parameters:
        call += ", __SUPERNOTE_ARGUMENTS__"
    call += ")"
    function = _jsi_async_host_function(
        js_name=binding.name,
        diagnostic=f"{module_name}.{binding.name}",
        parameters=tuple(
            Parameter(_CPP_TYPES[item.type], item.name)
            for item in binding.parameters
        ),
        return_type=result_type,
        call=call,
        outer_captures=("invoke", "feature_session"),
        executor_captures=("invoke",),
        worker_captures_extra=("invoke",),
        worker_prelude=(
            "                      if (implementation_feature->state() !=\n"
            "                              supernote::runtime::FeatureState::ACTIVE) return;"
        ),
        release_feature_before_execution=False,
        implementation_name="Kotlin/Java",
        implementation_exception_type="JvmImplementationFailure",
    )
    preflight = _scalar_preflight_statements(
        binding, f"{module_name}.{binding.name}"
    )
    return (
        "  {\n"
        + "\n".join(setup)
        + "\n"
        + invoker
        + "\n"
        + f"    auto function = {function};\n"
        + preflight
        + "\n"
        + f"    exports.setProperty(runtime, {json.dumps(binding.name)}, "
        + "std::move(function));\n"
        + "  }"
    )


def _render_suspend_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    binding: SemanticBinding,
    module_name: str,
    *,
    object_item: SemanticClass | None = None,
    object_route_name: str | None = None,
) -> str:
    object_method = object_item is not None
    if object_method and object_route_name is None:
        raise AssertionError("a JVM object suspend route requires its route member")
    takes_owner = object_method or owner.form is JvmOwnerForm.CLASS
    setup = [
        (
            f"      auto route = {object_route_name}_;"
            if object_method
            else (
                "    auto route = std::make_shared<LazyJvmRoute>(\n"
                f"        {json.dumps(_adapter_class(source.adapter_identity))}, "
                f"{json.dumps(_suspend_adapter_descriptor(binding, owner if takes_owner else None))});"
            )
        ),
        "    auto cancel_route = std::make_shared<LazyJvmRoute>(\n"
        '        "supernote.generated.runtime.SupernoteCoroutineBridge",\n'
        '        "(Lkotlinx/coroutines/Job;)V", "cancel");',
    ]
    route_captures = ["route", "cancel_route", "feature_session"]
    owner_setup = ""
    if object_method:
        setup.extend(
            [
                "      auto owner = owner_;",
                "      auto feature_session = feature_session_;",
            ]
        )
        route_captures.append("owner")
    elif takes_owner:
        constructor = _owner_constructor(owner)
        setup.append(
            "    auto owner_route = std::make_shared<LazyJvmRoute>(\n"
            f"        {json.dumps(_adapter_class(constructor.adapter_identity))}, "
            f"{json.dumps(_constructor_descriptor(owner))});"
        )
        route_captures.append("owner_route")
        owner_setup = _render_owner_setup(owner).replace(
            "feature_session", "implementation_feature"
        )
    parameters = tuple(
        Parameter(_CPP_TYPES[item.type], item.name)
        for item in binding.parameters
    )
    diagnostic = (
        f"{module_name}.{object_item.name}.{binding.name}"
        if object_item is not None
        else f"{module_name}.{binding.name}"
    )
    validations = _validations(binding, diagnostic)
    input_names = [f"supernote_input_{index}" for index in range(len(parameters))]
    inputs = "\n".join(
        f"          auto {name} = {_jsi_argument(parameter, index)};"
        for index, (name, parameter) in enumerate(zip(input_names, parameters))
    )
    executor_captures = [*route_captures, "state"]
    executor_captures.extend(
        f"{name} = std::move({name})" for name in input_names
    )
    worker_captures = [
        "operation",
        "weak_feature",
        "route",
        "cancel_route",
        "completion_id",
    ]
    if object_method:
        worker_captures.append("owner")
    elif takes_owner:
        worker_captures.append("owner_route")
    worker_captures.extend(
        f"{name} = std::move({name})" for name in input_names
    )
    result_type = (
        None
        if binding.result is SemanticType.VOID
        else _CPP_TYPES[binding.result]
    )
    state_value = (
        ""
        if result_type is None
        else f"            std::optional<{result_type}> value;\n"
    )
    result_read = _suspend_result_read(binding.result)
    if binding.result is SemanticType.VOID:
        resolution = (
            "                supernote_resolve_operation(\n"
            "                    runtime, operation_id, Value::undefined());"
        )
    else:
        resolution = (
            f"                auto value = {_jsi_async_result_value(result_type)};\n"
            "                supernote_resolve_operation(\n"
            "                    runtime, operation_id, std::move(value));"
        )
    offset = 1 if takes_owner else 0
    argument_count = len(binding.parameters) + offset + 1
    jvm_arguments = []
    if takes_owner:
        jvm_arguments.append(
            "              jvm_arguments[0].l = "
            "static_cast<jobject>(owner->value.get());"
        )
    jvm_arguments.extend(
        _owned_argument_lines(
            binding.parameters,
            offset,
            "              ",
            names=input_names,
        )
    )
    jvm_arguments.append(
        f"              jvm_arguments[{argument_count - 1}].j = "
        "static_cast<jlong>(completion_id);"
    )
    opening = (
        f"    if (property == {json.dumps(binding.name)}) {{"
        if object_method
        else "  {"
    )
    closing = (
        "      return Value(std::move(function));\n    }"
        if object_method
        else (
            f"    exports.setProperty(runtime, {json.dumps(binding.name)}, "
            "std::move(function));\n  }"
        )
    )
    preflight = _scalar_preflight_statements(
        binding, diagnostic, indent="    "
    )
    return f'''{opening}
{chr(10).join(setup)}
    auto function = Function::createFromHostFunction(
        runtime,
        PropNameID::forAscii(runtime, {json.dumps(binding.name)}),
        {len(binding.parameters)},
        [{', '.join(route_captures)}](facebook::jsi::Runtime &runtime,
           const Value &,
           const Value *arguments,
           std::size_t argument_count) -> Value {{
{validations}
{inputs}
          struct SuspendState {{
            bool success{{false}};
{state_value}            std::string code;
            std::string error;
          }};
          auto state = std::make_shared<SuspendState>();
          auto executor = Function::createFromHostFunction(
              runtime,
              PropNameID::forAscii(runtime, "SupernoteSuspendExecutor"),
              2,
              [{', '.join(executor_captures)}](
                  facebook::jsi::Runtime &runtime,
                  const Value &,
                  const Value *continuation_arguments,
                  std::size_t continuation_count) mutable -> Value {{
                if (continuation_count != 2 ||
                    !continuation_arguments[0].isObject() ||
                    !continuation_arguments[1].isObject()) {{
                  throw facebook::jsi::JSError(
                      runtime, "Promise supplied invalid continuation functions");
                }}
                auto operation = feature_session
                    ? feature_session->accept_factory(
                          [](supernote::runtime::SessionId operation_id) {{
                            return [operation_id](void *runtime_pointer) {{
                              auto &runtime = *static_cast<facebook::jsi::Runtime *>(
                                  runtime_pointer);
                              supernote_reject_operation(
                                  runtime, operation_id, "FEATURE_CLOSED",
                                  "feature closed before async completion");
                            }};
                          }})
                    : nullptr;
                if (!operation) {{
                  supernote_reject_new_promise(
                      runtime, continuation_arguments[1], "FEATURE_CLOSED",
                      "feature is closed");
                  return Value::undefined();
                }}
                const auto operation_id = operation->id();
                supernote_register_continuation(
                    runtime, operation_id, continuation_arguments[0],
                    continuation_arguments[1]);
                std::weak_ptr<supernote::runtime::FeatureSession> weak_feature =
                    feature_session;
                const auto completion_id =
                    supernote::runtime::process_services()
                        .register_jvm_async_completion(
                            [operation, operation_id, weak_feature, state](
                                void *environment, void *result,
                                std::string error_code,
                                std::string error_message) {{
                              if (operation->cancellation_token().is_cancelled()) return;
                              if (!error_code.empty()) {{
                                state->code = std::move(error_code);
                                state->error = std::move(error_message);
                              }} else {{
                                try {{
{result_read}
                                  state->success = true;
                                }} catch (const std::exception &error) {{
                                  state->code = "INTERNAL";
                                  state->error = error.what();
                                }} catch (...) {{
                                  state->code = "INTERNAL";
                                  state->error = "cannot decode Kotlin coroutine result";
                                }}
                              }}
                              if (operation->cancellation_token().is_cancelled()) return;
                              auto feature = weak_feature.lock();
                              if (!feature) return;
                              feature->schedule_completion(
                                  operation,
                                  [state, operation_id](void *runtime_pointer) {{
                                    auto &runtime = *static_cast<facebook::jsi::Runtime *>(
                                        runtime_pointer);
                                    if (!state->success) {{
                                      supernote_reject_operation(
                                          runtime, operation_id,
                                          state->code.empty()
                                              ? "INTERNAL"
                                              : state->code.c_str(),
                                          state->error.empty()
                                              ? "Kotlin coroutine failed"
                                              : state->error);
                                      return;
                                    }}
                                    try {{
{resolution}
                                    }} catch (const std::exception &error) {{
                                      supernote_reject_operation(
                                          runtime, operation_id, "INTERNAL", error.what());
                                    }}
                                  }});
                            }});
                operation->set_cancel_hook([completion_id] {{
                  supernote::runtime::process_services()
                      .discard_jvm_async_completion(completion_id);
                }});
                auto work = supernote::runtime::process_services().workers().submit(
                    [{', '.join(worker_captures)}](
                        supernote::runtime::CancellationToken executor_cancel) mutable {{
                      if (executor_cancel.is_cancelled() ||
                          operation->cancellation_token().is_cancelled()) return;
                      auto implementation_feature = weak_feature.lock();
                      if (!implementation_feature ||
                          implementation_feature->state() !=
                              supernote::runtime::FeatureState::ACTIVE) return;
                      supernote::runtime::FeatureCallScope feature_call_scope(
                          implementation_feature);
                      try {{
{owner_setup}              auto resolved = route->get(implementation_feature);
                        auto cancel_resolved = cancel_route->get(
                            implementation_feature);
                        AttachedEnv attached;
                        auto *env = attached.get();
                        if (env == nullptr) {{
                          throw std::runtime_error("cannot attach to JavaVM");
                        }}
                        LocalFrame frame(env);
                        jvalue jvm_arguments[{max(1, argument_count)}]{{}};
{chr(10).join(jvm_arguments)}
                        auto local_job = env->CallStaticObjectMethodA(
                            static_cast<jclass>(resolved->adapter_class.get()),
                            resolved->method, jvm_arguments);
                        if (env->ExceptionCheck()) {{
                          require_no_implementation_exception(env);
                        }}
                        if (local_job == nullptr) {{
                          throw std::runtime_error(
                              "cannot launch generated Kotlin coroutine adapter");
                        }}
                        auto job = retain_global(env, local_job);
                        operation->set_cancel_hook(
                            [completion_id, job, cancel_resolved] {{
                              supernote::runtime::process_services()
                                  .discard_jvm_async_completion(completion_id);
                              try {{
                                AttachedEnv attached;
                                auto *env = attached.get();
                                if (env == nullptr) return;
                                LocalFrame frame(env);
                                jvalue arguments[1]{{}};
                                arguments[0].l = static_cast<jobject>(job.get());
                                env->CallStaticVoidMethodA(
                                    static_cast<jclass>(
                                        cancel_resolved->adapter_class.get()),
                                    cancel_resolved->method, arguments);
                                clear_exception(env);
                              }} catch (...) {{}}
                            }});
                      }} catch (const std::exception &error) {{
                        supernote::runtime::process_services().complete_jvm_async(
                            completion_id, nullptr, nullptr, "INTERNAL", error.what());
                      }} catch (...) {{
                        supernote::runtime::process_services().complete_jvm_async(
                            completion_id, nullptr, nullptr, "INTERNAL",
                            "cannot launch Kotlin coroutine adapter");
                      }}
                    }});
                operation->set_work(work);
                if (!work.accepted()) {{
                  supernote::runtime::process_services().complete_jvm_async(
                      completion_id, nullptr, nullptr, "RESOURCE_EXHAUSTED",
                      "Supernote worker queue is full");
                }}
                return Value::undefined();
              }});
          auto promise = runtime.global().getPropertyAsFunction(runtime, "Promise");
          const Value executor_argument(std::move(executor));
          return promise.callAsConstructor(
              runtime, &executor_argument, static_cast<std::size_t>(1));
        }});
{preflight}
{closing}'''


def _render_async_object_method(
    item: SemanticClass,
    method: SemanticBinding,
    route_name: str,
    module_name: str,
) -> str:
    result_type = (
        "void"
        if method.result is SemanticType.VOID
        else _CPP_TYPES[method.result]
    )
    parameter_rows = [
        "std::shared_ptr<supernote::runtime::FeatureSession> "
        "implementation_feature"
    ]
    parameter_rows.extend(
        _owned_cpp_parameter(parameter.type, parameter.name)
        for parameter in method.parameters
    )
    invocation = _worker_invocation(method, True)
    invoker = (
        "      auto invoke = [route, owner](\n"
        f"          {', '.join(parameter_rows)}) -> {result_type} {{\n"
        "        auto resolved = route->get(implementation_feature);\n"
        "        implementation_feature.reset();\n"
        "        AttachedEnv attached;\n"
        "        auto *env = attached.get();\n"
        "        if (env == nullptr) {\n"
        "          throw std::runtime_error(\"cannot attach to JavaVM\");\n"
        "        }\n"
        "        LocalFrame frame(env);\n"
        f"{_indent(invocation, 8)}\n"
        "      };"
    )
    call = "invoke(std::move(implementation_feature)"
    if method.parameters:
        call += ", __SUPERNOTE_ARGUMENTS__"
    call += ")"
    function = _jsi_async_host_function(
        js_name=method.name,
        diagnostic=f"{module_name}.{item.name}.{method.name}",
        parameters=tuple(
            Parameter(_CPP_TYPES[parameter.type], parameter.name)
            for parameter in method.parameters
        ),
        return_type=result_type,
        call=call,
        outer_captures=("invoke", "feature_session"),
        executor_captures=("invoke",),
        worker_captures_extra=("invoke",),
        worker_prelude=(
            "                      if (implementation_feature->state() !=\n"
            "                              supernote::runtime::FeatureState::ACTIVE) return;"
        ),
        release_feature_before_execution=False,
        implementation_name="Kotlin/Java",
        implementation_exception_type="JvmImplementationFailure",
    )
    preflight = _scalar_preflight_statements(
        method,
        f"{module_name}.{item.name}.{method.name}",
        indent="      ",
    )
    return f'''    if (property == {json.dumps(method.name)}) {{
      auto route = {route_name}_;
      auto owner = owner_;
      auto feature_session = feature_session_;
{invoker}
      auto function = {function};
{preflight}
      return facebook::jsi::Value(std::move(function));
    }}'''


def _render_object(
    owner: JvmOwnerSource,
    item: SemanticClass,
    index: int,
    module_name: str,
) -> tuple[str, str]:
    constructors = {
        constructor.provenance.declaration_id: constructor
        for constructor in owner.constructors
    }
    constructor = constructors.get(item.constructor.source.declaration_id)
    if constructor is None:
        raise JvmCodegenError(
            f"{item.source.path}:{item.source.line}: selected JVM constructor facts are missing"
        )
    declarations = {
        declaration.provenance.declaration_id: declaration
        for declaration in owner.declarations
    }
    method_rows = []
    route_arguments = []
    route_members = []
    route_parameters = []
    route_setup = []
    route_captures = []
    for method_index, method in enumerate(item.methods):
        source = declarations.get(method.source.declaration_id)
        if source is None:
            raise JvmCodegenError(
                f"{method.source.path}:{method.source.line}: JVM method facts are missing"
            )
        if not method.capabilities.javascript_public:
            # The method remains routable in the common semantic model, but a
            # JS-created JVM receiver has no initial handwritten object-handle
            # API. It therefore stays off the HostObject surface without
            # inventing receiver lookup or passing semantics.
            continue
        route_name = f"method_route_{method_index}"
        route_parameters.append(
            f"std::shared_ptr<LazyJvmRoute> {route_name}"
        )
        route_arguments.append(f"std::move({route_name})")
        route_members.append(f"  std::shared_ptr<LazyJvmRoute> {route_name}_;")
        route_setup.append(
            f"    auto {route_name} = std::make_shared<LazyJvmRoute>(\n"
            f"        {json.dumps(_adapter_class(source.adapter_identity))},\n"
            f"        {json.dumps(_suspend_adapter_descriptor(method, owner) if source.is_suspend else _adapter_descriptor(method, owner))});"
        )
        route_captures.append(route_name)
        if source.is_suspend:
            method_rows.append(
                _render_suspend_function(
                    owner,
                    source,
                    method,
                    module_name,
                    object_item=item,
                    object_route_name=route_name,
                )
            )
            continue
        if method.execution is ExecutionMode.ASYNC:
            method_rows.append(
                _render_async_object_method(
                    item, method, route_name, module_name
                )
            )
            continue
        validations = _validations(
            method, f"{module_name}.{item.name}.{method.name}"
        )
        invocation = _invocation(method, True)
        method_rows.append(
            f'''    if (property == {json.dumps(method.name)}) {{
      auto route = {route_name}_;
      auto owner = owner_;
      auto feature_session = feature_session_;
      return facebook::jsi::Value(facebook::jsi::Function::createFromHostFunction(
          runtime,
          facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(method.name)}),
          {len(method.parameters)},
          [route, owner, feature_session](
              facebook::jsi::Runtime &runtime,
              const facebook::jsi::Value &,
              const facebook::jsi::Value *arguments,
              std::size_t argument_count) -> facebook::jsi::Value {{
{validations}
            try {{
              if (!feature_session ||
                  feature_session->state() != supernote::runtime::FeatureState::ACTIVE) {{
                supernote_throw_error(
                    runtime, "FEATURE_CLOSED", "feature is closed");
              }}
              auto resolved = route->get(feature_session);
              AttachedEnv attached;
              auto *env = attached.get();
              if (env == nullptr) {{
                throw std::runtime_error("cannot attach to JavaVM");
              }}
              LocalFrame frame(env);
{_indent(invocation, 14)}
            }} catch (const facebook::jsi::JSError &) {{
              throw;
            }} catch (const JvmImplementationFailure &error) {{
              supernote_throw_error(
                  runtime, "IMPLEMENTATION_ERROR", error.what());
            }} catch (const std::exception &error) {{
              supernote_throw_error(runtime, "INTERNAL", error.what());
            }}
          }}));
    }}'''
        )
    constructor_parameters = ",\n      ".join(
        [
            "std::shared_ptr<JvmOwner> owner",
            "std::shared_ptr<supernote::runtime::FeatureSession> feature_session",
            *route_parameters,
        ]
    )
    initializers = ",\n        ".join(
        [
            "owner_(std::move(owner))",
            "feature_session_(std::move(feature_session))",
            *[
                f"{route_name}_(std::move({route_name}))"
                for route_name in route_captures
            ],
        ]
    )
    names = "\n".join(
        "    names.push_back(facebook::jsi::PropNameID::forAscii(\n"
        f"        runtime, {json.dumps(method.name)}));"
        for method in item.methods
        if method.capabilities.javascript_public
    )
    wrapper = f'''class GeneratedJvmObject{index}HostObject final
    : public facebook::jsi::HostObject {{
 public:
  GeneratedJvmObject{index}HostObject(
      {constructor_parameters})
      : {initializers} {{}}

  facebook::jsi::Value get(
      facebook::jsi::Runtime &runtime,
      const facebook::jsi::PropNameID &name) override {{
    using facebook::jsi::Function;
    using facebook::jsi::PropNameID;
    using facebook::jsi::String;
    using facebook::jsi::Value;
    const auto property = name.utf8(runtime);
{chr(10).join(method_rows)}
    return facebook::jsi::Value::undefined();
  }}

  std::vector<facebook::jsi::PropNameID> getPropertyNames(
      facebook::jsi::Runtime &runtime) override {{
    std::vector<facebook::jsi::PropNameID> names;
    names.reserve({sum(method.capabilities.javascript_public for method in item.methods)});
{names}
    return names;
  }}

 private:
  std::shared_ptr<JvmOwner> owner_;
  std::shared_ptr<supernote::runtime::FeatureSession> feature_session_;
{chr(10).join(route_members)}
}};'''
    constructor_route = _adapter_class(constructor.adapter_identity)
    constructor_descriptor = _object_constructor_descriptor(owner, item)
    validation = _validations_parameters(
        item.constructor.parameters,
        f"{module_name}.{item.name}.create",
    )
    jvm_arguments = _argument_lines(item.constructor.parameters, 1)
    captured = ", ".join(["constructor_route", "feature_session", *route_captures])
    wrapper_arguments = ",\n"
    wrapper_arguments += " " * 20
    wrapper_arguments = wrapper_arguments.join(
        ["std::move(owner)", "feature_session", *route_arguments]
    )
    registration = f'''  {{
    auto constructor_route = std::make_shared<LazyJvmRoute>(
        {json.dumps(constructor_route)}, {json.dumps(constructor_descriptor)});
{chr(10).join(route_setup)}
    Object object_type(runtime);
    auto create = Function::createFromHostFunction(
        runtime,
        PropNameID::forAscii(runtime, "create"),
        {len(item.constructor.parameters)},
        [{captured}](facebook::jsi::Runtime &runtime,
           const Value &,
           const Value *arguments,
           std::size_t argument_count) -> Value {{
{validation}
          try {{
            if (!feature_session ||
                feature_session->state() != supernote::runtime::FeatureState::ACTIVE) {{
              supernote_throw_error(
                  runtime, "FEATURE_CLOSED", "feature is closed");
            }}
            auto resolved = constructor_route->get(feature_session);
            auto runtime_session = feature_session->runtime();
            auto context = runtime_session
                ? runtime_session->platform_context() : nullptr;
            if (!context) {{
              throw std::runtime_error("platform Context is unavailable");
            }}
            AttachedEnv attached;
            auto *env = attached.get();
            if (env == nullptr) {{
              throw std::runtime_error("cannot attach to JavaVM");
            }}
            LocalFrame frame(env);
            jvalue jvm_arguments[{max(1, len(item.constructor.parameters) + 1)}]{{}};
            jvm_arguments[0].l = static_cast<jobject>(context.get());
{chr(10).join(jvm_arguments)}
            auto local = env->CallStaticObjectMethodA(
                static_cast<jclass>(resolved->adapter_class.get()),
                resolved->method, jvm_arguments);
            require_no_implementation_exception(env);
            if (local == nullptr) {{
              throw std::runtime_error("JVM object constructor returned null");
            }}
            auto owner = std::make_shared<JvmOwner>(retain_global(env, local));
            return Value(facebook::jsi::Object::createFromHostObject(
                runtime,
                std::make_shared<GeneratedJvmObject{index}HostObject>(
                    {wrapper_arguments})));
          }} catch (const facebook::jsi::JSError &) {{
            throw;
          }} catch (const JvmImplementationFailure &error) {{
            supernote_throw_error(
                runtime, "IMPLEMENTATION_ERROR", error.what());
          }} catch (const std::exception &error) {{
            supernote_throw_error(runtime, "INTERNAL", error.what());
          }}
        }});
    object_type.setProperty(runtime, "create", std::move(create));
    exports.setProperty(runtime, {json.dumps(item.name)}, std::move(object_type));
  }}'''
    return wrapper, registration


def _validations(binding: SemanticBinding, diagnostic: str) -> str:
    return _validations_parameters(binding.parameters, diagnostic)


def _scalar_preflight_function(
    binding: SemanticBinding,
    diagnostic: str,
    *,
    name: str,
    check: bool,
    indent: str,
) -> str:
    validations = _validations(binding, diagnostic)
    success = (
        "supernote_validation_success(runtime)"
        if check
        else "facebook::jsi::Value(true)"
    )
    rejected = (
        "supernote_validation_failure(\n"
        f"{indent}          runtime, facebook::jsi::Value(runtime, error.value()))"
        if check
        else "facebook::jsi::Value(false)"
    )
    arguments = (
        "const facebook::jsi::Value *arguments"
        if binding.parameters
        else "const facebook::jsi::Value *"
    )
    return f'''facebook::jsi::Function::createFromHostFunction(
{indent}    runtime,
{indent}    facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(name)}),
{indent}    {len(binding.parameters)},
{indent}    [](facebook::jsi::Runtime &runtime,
{indent}       const facebook::jsi::Value &,
{indent}       {arguments},
{indent}       std::size_t argument_count) -> facebook::jsi::Value {{
{indent}      try {{
{validations}
{indent}        return {success};
{indent}      }} catch (const facebook::jsi::JSError &error) {{
{indent}        return {rejected};
{indent}      }} catch (const std::exception &error) {{
{indent}        supernote_throw_error(runtime, "INTERNAL", error.what());
{indent}      }}
{indent}    }})'''


def _scalar_preflight_statements(
    binding: SemanticBinding,
    diagnostic: str,
    *,
    indent: str = "    ",
) -> str:
    accepts = _scalar_preflight_function(
        binding,
        diagnostic,
        name=binding.name + ".accepts",
        check=False,
        indent=indent,
    )
    check = _scalar_preflight_function(
        binding,
        diagnostic,
        name=binding.name + ".checkArguments",
        check=True,
        indent=indent,
    )
    return (
        f"{indent}auto accepts = {accepts};\n"
        f"{indent}auto check_arguments = {check};\n"
        f"{indent}function = supernote_attach_preflight(\n"
        f"{indent}    runtime, std::move(function), std::move(accepts),\n"
        f"{indent}    std::move(check_arguments));"
    )


def _validations_parameters(parameters, diagnostic: str) -> str:
    expected = ", ".join(
        f"{_jsi_expected_type(_CPP_TYPES[item.type])} {item.name}"
        for item in parameters
    )
    count = len(parameters)
    description = f"{count} argument{'' if count == 1 else 's'} ({expected})"
    lines = [
        f"          if (argument_count != {count}) {{",
        "            supernote_throw_type_error(",
        f"                runtime, std::string({json.dumps(diagnostic + ': expected ' + description + '; received ')}) +",
        "                std::to_string(argument_count),",
        f"                \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
        f"                {json.dumps(description)},",
        "                std::to_string(argument_count) + \" arguments\");",
        "          }",
    ]
    for index, item in enumerate(parameters):
        parameter = Parameter(_CPP_TYPES[item.type], item.name)
        lines.extend(
            [
                f"          if ({_jsi_type_check(parameter, index)}) {{",
                "            supernote_throw_type_error(",
                f"                runtime, {json.dumps(diagnostic + ': argument ' + str(index + 1) + ' (' + item.name + ') has the wrong JavaScript type')},",
                f"                \"TYPE_MISMATCH\", {json.dumps(diagnostic + '.argument[' + str(index) + '](' + item.name + ')')},",
                f"                {json.dumps(_jsi_expected_type(_CPP_TYPES[item.type]))},",
                f"                supernote_describe_value(runtime, arguments[{index}]));",
                "          }",
            ]
        )
        lines.extend(
            _jsi_range_validation(
                parameter,
                index,
                diagnostic_name=diagnostic,
                indent="          ",
            )
        )
    return "\n".join(lines)


def _invocation(binding: SemanticBinding, takes_owner: bool) -> str:
    offset = 1 if takes_owner else 0
    size = len(binding.parameters) + offset
    lines = [f"            jvalue jvm_arguments[{max(1, size)}]{{}};"]
    if takes_owner:
        lines.append(
            "            jvm_arguments[0].l = "
            "static_cast<jobject>(owner->value.get());"
        )
    lines.extend(_argument_lines(binding.parameters, offset))
    call = _jni_call(binding.result)
    if binding.result is SemanticType.VOID:
        lines.append(
            f"            env->{call}(static_cast<jclass>(resolved->adapter_class.get()), "
            "resolved->method, jvm_arguments);"
        )
        lines.append("            require_no_implementation_exception(env);")
        lines.append("            return facebook::jsi::Value::undefined();")
        return "\n".join(lines)
    lines.append(
        f"            auto result = env->{call}("
        "static_cast<jclass>(resolved->adapter_class.get()), "
        "resolved->method, jvm_arguments);"
    )
    lines.append("            require_no_implementation_exception(env);")
    if binding.result is SemanticType.BOOL:
        lines.append("            return facebook::jsi::Value(result == JNI_TRUE);")
    elif binding.result in {
        SemanticType.INT32,
        SemanticType.FLOAT32,
        SemanticType.FLOAT64,
    }:
        lines.append(
            "            return facebook::jsi::Value(static_cast<double>(result));"
        )
    elif binding.result is SemanticType.INT64:
        lines.extend(
            [
                "            return facebook::jsi::Value(facebook::jsi::BigInt::fromInt64(",
                "                runtime, static_cast<std::int64_t>(result)));",
            ]
        )
    elif binding.result in {SemanticType.STRING, SemanticType.BYTES}:
        lines.append(
            "            const auto bytes = read_byte_array("
            "env, static_cast<jbyteArray>(result));"
        )
        if binding.result is SemanticType.STRING:
            lines.extend(
                [
                    "            const std::string text(",
                    "                reinterpret_cast<const char *>(bytes.data()), bytes.size());",
                    "            return facebook::jsi::Value(",
                    "                facebook::jsi::String::createFromUtf8(runtime, text));",
                ]
            )
        else:
            lines.append("            return supernote_make_uint8_array(runtime, bytes);")
    else:
        raise AssertionError(binding.result)
    return "\n".join(lines)


def _owned_cpp_parameter(type_: SemanticType, name: str) -> str:
    cpp_type = _CPP_TYPES[type_]
    if type_ in {SemanticType.STRING, SemanticType.BYTES}:
        return f"const {cpp_type} &{name}"
    return f"{cpp_type} {name}"


def _worker_invocation(
    binding: SemanticBinding,
    takes_owner: bool,
) -> str:
    indent = "      "
    offset = 1 if takes_owner else 0
    size = len(binding.parameters) + offset
    lines = [f"{indent}jvalue jvm_arguments[{max(1, size)}]{{}};"]
    if takes_owner:
        lines.append(
            f"{indent}jvm_arguments[0].l = "
            "static_cast<jobject>(owner->value.get());"
        )
    lines.extend(_owned_argument_lines(binding.parameters, offset, indent))
    call = _jni_call(binding.result)
    call_expression = (
        f"env->{call}(static_cast<jclass>(resolved->adapter_class.get()), "
        "resolved->method, jvm_arguments)"
    )
    if binding.result is SemanticType.VOID:
        lines.append(f"{indent}{call_expression};")
        lines.append(f"{indent}require_no_implementation_exception(env);")
        return "\n".join(lines)
    lines.append(f"{indent}auto result = {call_expression};")
    lines.append(f"{indent}require_no_implementation_exception(env);")
    if binding.result is SemanticType.BOOL:
        lines.append(f"{indent}return result == JNI_TRUE;")
    elif binding.result is SemanticType.INT32:
        lines.append(f"{indent}return static_cast<std::int32_t>(result);")
    elif binding.result is SemanticType.INT64:
        lines.append(f"{indent}return static_cast<std::int64_t>(result);")
    elif binding.result is SemanticType.FLOAT32:
        lines.append(f"{indent}return static_cast<float>(result);")
    elif binding.result is SemanticType.FLOAT64:
        lines.append(f"{indent}return static_cast<double>(result);")
    elif binding.result in {SemanticType.STRING, SemanticType.BYTES}:
        lines.append(
            f"{indent}const auto bytes = read_byte_array("
            "env, static_cast<jbyteArray>(result));"
        )
        if binding.result is SemanticType.STRING:
            lines.append(
                f"{indent}return std::string("
                "reinterpret_cast<const char *>(bytes.data()), bytes.size());"
            )
        else:
            lines.append(f"{indent}return bytes;")
    else:
        raise AssertionError(binding.result)
    return "\n".join(lines)


def _suspend_result_read(result: SemanticType) -> str:
    indent = " " * 34
    if result is SemanticType.VOID:
        return (
            f"{indent}(void)environment;\n"
            f"{indent}(void)result;"
        )
    lines = [
        f"{indent}auto *env = static_cast<JNIEnv *>(environment);",
        f"{indent}auto object = static_cast<jobject>(result);",
        f"{indent}if (env == nullptr || object == nullptr) {{",
        f'{indent}  throw std::runtime_error("Kotlin coroutine returned null");',
        f"{indent}}}",
        f"{indent}LocalFrame frame(env);",
    ]
    if result in {SemanticType.STRING, SemanticType.BYTES}:
        lines.append(
            f"{indent}const auto bytes = read_byte_array("
            "env, static_cast<jbyteArray>(object));"
        )
        if result is SemanticType.STRING:
            lines.append(
                f"{indent}state->value = std::string("
                "reinterpret_cast<const char *>(bytes.data()), bytes.size());"
            )
        else:
            lines.append(f"{indent}state->value = bytes;")
        return "\n".join(lines)
    method, descriptor, call, conversion = {
        SemanticType.BOOL: (
            "booleanValue",
            "()Z",
            "CallBooleanMethod",
            "value == JNI_TRUE",
        ),
        SemanticType.INT32: (
            "intValue",
            "()I",
            "CallIntMethod",
            "static_cast<std::int32_t>(value)",
        ),
        SemanticType.INT64: (
            "longValue",
            "()J",
            "CallLongMethod",
            "static_cast<std::int64_t>(value)",
        ),
        SemanticType.FLOAT32: (
            "floatValue",
            "()F",
            "CallFloatMethod",
            "static_cast<float>(value)",
        ),
        SemanticType.FLOAT64: (
            "doubleValue",
            "()D",
            "CallDoubleMethod",
            "static_cast<double>(value)",
        ),
    }[result]
    lines.extend(
        [
            f"{indent}auto value_class = env->GetObjectClass(object);",
            f"{indent}auto unbox = value_class == nullptr",
            f"{indent}    ? nullptr",
            f"{indent}    : env->GetMethodID(value_class, {json.dumps(method)},",
            f"{indent}                       {json.dumps(descriptor)});",
            f"{indent}if (unbox == nullptr) {{",
            f"{indent}  clear_exception(env);",
            f'{indent}  throw std::runtime_error("cannot unbox Kotlin coroutine result");',
            f"{indent}}}",
            f"{indent}auto value = env->{call}(object, unbox);",
            f"{indent}if (env->ExceptionCheck()) {{",
            f"{indent}  clear_exception(env);",
            f'{indent}  throw std::runtime_error("cannot read Kotlin coroutine result");',
            f"{indent}}}",
            f"{indent}state->value = {conversion};",
        ]
    )
    return "\n".join(lines)


def _owned_argument_lines(
    parameters,
    offset: int,
    indent: str,
    *,
    names: list[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(parameters):
        target = index + offset
        field = _JNI_FIELDS[item.type]
        name = names[index] if names is not None else item.name
        if item.type is SemanticType.BOOL:
            value = f"{name} ? JNI_TRUE : JNI_FALSE"
        elif item.type is SemanticType.INT32:
            value = f"static_cast<jint>({name})"
        elif item.type is SemanticType.INT64:
            value = f"static_cast<jlong>({name})"
        elif item.type is SemanticType.FLOAT32:
            value = f"static_cast<jfloat>({name})"
        elif item.type is SemanticType.FLOAT64:
            value = f"static_cast<jdouble>({name})"
        elif item.type is SemanticType.STRING:
            value = (
                "write_byte_array(env, reinterpret_cast<const std::byte *>("
                f"{name}.data()), {name}.size())"
            )
        elif item.type is SemanticType.BYTES:
            value = f"write_byte_array(env, {name}.data(), {name}.size())"
        else:
            raise AssertionError(item.type)
        lines.append(f"{indent}jvm_arguments[{target}].{field} = {value};")
    return lines


def _argument_lines(parameters, offset: int) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(parameters):
        target = index + offset
        field = _JNI_FIELDS[item.type]
        if item.type is SemanticType.BOOL:
            value = f"arguments[{index}].getBool() ? JNI_TRUE : JNI_FALSE"
        elif item.type is SemanticType.INT32:
            value = f"static_cast<jint>(arguments[{index}].asNumber())"
        elif item.type is SemanticType.INT64:
            value = (
                f"static_cast<jlong>(arguments[{index}].getBigInt(runtime)"
                ".asInt64(runtime))"
            )
        elif item.type is SemanticType.FLOAT32:
            value = f"static_cast<jfloat>(arguments[{index}].asNumber())"
        elif item.type is SemanticType.FLOAT64:
            value = f"static_cast<jdouble>(arguments[{index}].asNumber())"
        elif item.type is SemanticType.STRING:
            lines.append(
                f"            const auto argument_{index} = "
                f"arguments[{index}].asString(runtime).utf8(runtime);"
            )
            value = (
                f"write_byte_array(env, reinterpret_cast<const std::byte *>("
                f"argument_{index}.data()), argument_{index}.size())"
            )
        elif item.type is SemanticType.BYTES:
            lines.append(
                f"            const auto argument_{index} = "
                f"supernote_copy_uint8_array(runtime, supernote_snapshot_{index});"
            )
            value = f"write_byte_array(env, argument_{index}.data(), argument_{index}.size())"
        else:
            raise AssertionError(item.type)
        lines.append(f"            jvm_arguments[{target}].{field} = {value};")
    return lines


def _render_owner_setup(owner: JvmOwnerSource) -> str:
    return (
        "            auto owner = feature_session->service<JvmOwner>(\n"
        f"                {json.dumps(owner.provenance.declaration_id)}, [&] {{\n"
        "                  auto constructor = owner_route->get(feature_session);\n"
        "                  auto runtime_session = feature_session->runtime();\n"
        "                  auto context = runtime_session\n"
        "                      ? runtime_session->platform_context() : nullptr;\n"
        "                  if (!context) {\n"
        "                    throw std::runtime_error(\"platform Context is unavailable\");\n"
        "                  }\n"
        "                  AttachedEnv attached;\n"
        "                  auto *env = attached.get();\n"
        "                  if (env == nullptr) {\n"
        "                    throw std::runtime_error(\"cannot attach to JavaVM\");\n"
        "                  }\n"
        "                  LocalFrame frame(env);\n"
        "                  jvalue arguments[1]{};\n"
        "                  arguments[0].l = static_cast<jobject>(context.get());\n"
        "                  auto local = env->CallStaticObjectMethodA(\n"
        "                      static_cast<jclass>(constructor->adapter_class.get()),\n"
        "                      constructor->method, arguments);\n"
        "                  require_no_implementation_exception(env);\n"
        "                  if (local == nullptr) {\n"
        "                    throw std::runtime_error(\"JVM owner constructor returned null\");\n"
        "                  }\n"
        "                  return std::make_shared<JvmOwner>(retain_global(env, local));\n"
        "                });\n"
    )


def _owner_constructor(owner: JvmOwnerSource) -> JvmConstructorSource:
    eligible = [
        item
        for item in owner.constructors
        if item.visibility == "public"
        and len(item.parameters) <= 1
        and all(parameter.injected is not None for parameter in item.parameters)
    ]
    if len(eligible) != 1:
        raise JvmCodegenError(
            f"{owner.provenance.path}:{owner.provenance.line}: JVM owner does "
            "not have exactly one eligible injected constructor"
        )
    return eligible[0]


def _adapter_descriptor(
    binding: SemanticBinding, owner: JvmOwnerSource | None
) -> str:
    parameters = ""
    if owner is not None:
        parameters += f"L{owner.owner_class.replace('.', '/')};"
    parameters += "".join(_JNI_DESCRIPTOR[item.type] for item in binding.parameters)
    return f"({parameters}){_JNI_DESCRIPTOR[binding.result]}"


def _suspend_adapter_descriptor(
    binding: SemanticBinding, owner: JvmOwnerSource | None
) -> str:
    parameters = ""
    if owner is not None:
        parameters += f"L{owner.owner_class.replace('.', '/')};"
    parameters += "".join(
        _JNI_DESCRIPTOR[item.type] for item in binding.parameters
    )
    parameters += "J"
    return f"({parameters})Lkotlinx/coroutines/Job;"


def _constructor_descriptor(owner: JvmOwnerSource) -> str:
    return (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;)"
        f"L{owner.owner_class.replace('.', '/')};"
    )


def _object_constructor_descriptor(
    owner: JvmOwnerSource, item: SemanticClass
) -> str:
    parameters = "".join(
        _JNI_DESCRIPTOR[parameter.type]
        for parameter in item.constructor.parameters
    )
    return (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;"
        f"{parameters})L{owner.owner_class.replace('.', '/')};"
    )


def _jni_call(result: SemanticType) -> str:
    return {
        SemanticType.VOID: "CallStaticVoidMethodA",
        SemanticType.BOOL: "CallStaticBooleanMethodA",
        SemanticType.INT32: "CallStaticIntMethodA",
        SemanticType.INT64: "CallStaticLongMethodA",
        SemanticType.FLOAT32: "CallStaticFloatMethodA",
        SemanticType.FLOAT64: "CallStaticDoubleMethodA",
        SemanticType.STRING: "CallStaticObjectMethodA",
        SemanticType.BYTES: "CallStaticObjectMethodA",
    }[result]


def _adapter_class(identity: str) -> str:
    return "supernote.generated.adapters.Adapter_" + identity.rsplit(".", 1)[-1]


def _feature_suffix(feature_id: str) -> str:
    prefix = "supernote:feature:"
    suffix = feature_id.removeprefix(prefix)
    if not feature_id.startswith(prefix) or len(suffix) != 16:
        raise JvmCodegenError(f"invalid feature identity {feature_id!r}")
    return suffix


def _error(source: JvmDeclarationSource, message: str) -> JvmCodegenError:
    return JvmCodegenError(
        f"{source.provenance.path}:{source.provenance.line}: {message}"
    )


def _indent(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line.lstrip() for line in value.splitlines())
