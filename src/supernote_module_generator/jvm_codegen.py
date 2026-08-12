"""Lower authoritative KSP facts into plugin-level JVM JSI call routes."""
from __future__ import annotations

import json
from typing import Iterable

from .binding_codegen import (
    Parameter,
    _jsi_expected_type,
    _jsi_range_validation,
    _jsi_type_check,
    _jsi_value_helpers,
)
from .jvm_manifest import JvmSourceManifest
from .semantic import (
    DeclarationRole,
    ExecutionMode,
    SemanticApi,
    SemanticBinding,
    SemanticType,
)
from .source_models import (
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmOwnerForm,
    JvmOwnerSource,
)


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
) -> str:
    if manifest.feature_id != feature_id:
        raise JvmCodegenError("JVM manifest and feature identity disagree")
    by_source = {
        binding.source.declaration_id: binding for binding in semantic.functions
    }
    for item in semantic.classes:
        if item.methods or item.capabilities.routable:
            raise JvmCodegenError(
                f"{item.source.path}:{item.source.line}: JVM-backed object/service "
                "routing is recognized but not implemented yet"
            )
    registrations: list[str] = []
    for owner in manifest.owners:
        if owner.intent.role is not DeclarationRole.ORDINARY:
            continue
        for declaration in owner.declarations:
            binding = by_source.get(declaration.provenance.declaration_id)
            if binding is None:
                continue
            if binding.capabilities.routable and not binding.capabilities.javascript_public:
                raise _error(declaration, "internal JVM routing is not implemented yet")
            if binding.execution is ExecutionMode.ASYNC:
                raise _error(declaration, "async JVM routing is not implemented yet")
            if declaration.is_suspend:
                raise _error(declaration, "Kotlin suspend routing is not implemented yet")
            registrations.append(
                _render_function(owner, declaration, binding, module_name)
            )
    suffix = _feature_suffix(feature_id)
    helpers = _jsi_value_helpers().replace(
        "auto exports = runtime.global().getPropertyAsObject(runtime, kGlobalName);",
        "auto registry = runtime.global().getPropertyAsObject(\n"
        "      runtime, kFeatureRegistryGlobal);\n"
        "  auto exports = registry.getPropertyAsObject(runtime, kFeatureId);",
    )
    return f'''#include <jni.h>
#include <jsi/jsi.h>

#include <android/log.h>

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "runtime_services.hpp"

namespace supernote::generated::jvm_feature_{suffix} {{
namespace {{

constexpr char kLogTag[] = "SupernoteV2Jvm";
constexpr char kFeatureRegistryGlobal[] =
    "__supernoteV2FeatureRegistry_63f6999c8c67";
constexpr char kFeatureId[] = {json.dumps(feature_id)};

{helpers}

class JvmImplementationFailure final : public std::runtime_error {{
 public:
  using std::runtime_error::runtime_error;
}};

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

void clear_exception(JNIEnv *env) {{
  if (env != nullptr && env->ExceptionCheck()) env->ExceptionClear();
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

struct JvmRoute {{
  std::shared_ptr<void> adapter_class;
  jmethodID method{{nullptr}};
}};

class LazyJvmRoute {{
 public:
  LazyJvmRoute(std::string adapter_class, std::string descriptor)
      : adapter_class_(std::move(adapter_class)),
        descriptor_(std::move(descriptor)) {{}}

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
        static_cast<jclass>(local_class), "invoke", descriptor_.c_str());
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
  env->ExceptionClear();
  throw JvmImplementationFailure("Kotlin/Java implementation failed");
}}

}}  // namespace

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
{chr(10).join(registrations)}
}}

}}  // namespace supernote::generated::jvm_feature_{suffix}
'''


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
        f"    exports.setProperty(runtime, {json.dumps(binding.name)}, "
        "std::move(function));\n"
        "  }"
    )


def _validations(binding: SemanticBinding, diagnostic: str) -> str:
    expected = ", ".join(
        f"{_jsi_expected_type(_CPP_TYPES[item.type])} {item.name}"
        for item in binding.parameters
    )
    count = len(binding.parameters)
    description = f"{count} argument{'' if count == 1 else 's'} ({expected})"
    lines = [
        f"          if (argument_count != {count}) {{",
        "            supernote_throw_type_error(",
        f"                runtime, std::string({json.dumps(diagnostic + ': expected ' + description + '; received ')}) +",
        "                std::to_string(argument_count));",
        "          }",
    ]
    for index, item in enumerate(binding.parameters):
        parameter = Parameter(_CPP_TYPES[item.type], item.name)
        lines.extend(
            [
                f"          if ({_jsi_type_check(parameter, index)}) {{",
                "            supernote_throw_type_error(",
                f"                runtime, {json.dumps(diagnostic + ': argument ' + str(index + 1) + ' (' + item.name + ') has the wrong JavaScript type')});",
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
    for index, item in enumerate(binding.parameters):
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
                f"supernote_copy_uint8_array(runtime, arguments[{index}]);"
            )
            value = f"write_byte_array(env, argument_{index}.data(), argument_{index}.size())"
        else:
            raise AssertionError(item.type)
        lines.append(f"            jvm_arguments[{target}].{field} = {value};")
    call = _jni_call(binding.result)
    if binding.result is SemanticType.VOID:
        lines.append(
            f"            env->{call}(static_cast<jclass>(resolved->adapter_class.get()), "
            "resolved->method, jvm_arguments);"
        )
        lines.append("            require_no_implementation_exception(env);")
        lines.append("            return Value::undefined();")
        return "\n".join(lines)
    lines.append(
        f"            auto result = env->{call}("
        "static_cast<jclass>(resolved->adapter_class.get()), "
        "resolved->method, jvm_arguments);"
    )
    lines.append("            require_no_implementation_exception(env);")
    if binding.result is SemanticType.BOOL:
        lines.append("            return Value(result == JNI_TRUE);")
    elif binding.result in {
        SemanticType.INT32,
        SemanticType.FLOAT32,
        SemanticType.FLOAT64,
    }:
        lines.append("            return Value(static_cast<double>(result));")
    elif binding.result is SemanticType.INT64:
        lines.extend(
            [
                "            return Value(facebook::jsi::BigInt::fromInt64(",
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
                    "            return Value(String::createFromUtf8(runtime, text));",
                ]
            )
        else:
            lines.append("            return supernote_make_uint8_array(runtime, bytes);")
    else:
        raise AssertionError(binding.result)
    return "\n".join(lines)


def _render_owner_setup(owner: JvmOwnerSource) -> str:
    constructor = _owner_constructor(owner)
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


def _constructor_descriptor(owner: JvmOwnerSource) -> str:
    return (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;)"
        f"L{owner.owner_class.replace('.', '/')};"
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
