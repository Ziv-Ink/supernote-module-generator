"""Emit synchronous JSI bindings for V3 JVM object and composite routes."""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .jvm_manifest import jvm_adapter_identity
from .jvm_routes import (
    JvmCallableRoute,
    JvmFieldRoute,
    JvmObjectRoute,
    JvmRouteError,
    JvmRoutePlan,
)
from .semantic import ExecutionMode
from .semantic_types import ScalarKind, SemanticType, SemanticTypeKind


def _suffix(semantic: SemanticType) -> str:
    encoded = json.dumps(
        semantic.manifest(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _from_name(semantic: SemanticType) -> str:
    return f"supernote_v3_jvm_from_js_{_suffix(semantic)}"


def _to_name(semantic: SemanticType) -> str:
    return f"supernote_v3_jvm_to_js_{_suffix(semantic)}"


def _validate_name(semantic: SemanticType) -> str:
    return f"supernote_v3_jvm_validate_js_{_suffix(semantic)}"


def _native(semantic: SemanticType) -> str:
    if semantic.kind is SemanticTypeKind.VOID:
        return "void"
    if semantic.kind is SemanticTypeKind.SCALAR:
        return {
            ScalarKind.BOOL: "bool",
            ScalarKind.INT32: "std::int32_t",
            ScalarKind.INT64: "std::int64_t",
            ScalarKind.FLOAT32: "float",
            ScalarKind.FLOAT64: "double",
            ScalarKind.STRING: "std::string",
            ScalarKind.BYTES: "std::vector<std::byte>",
        }[semantic.scalar]
    if semantic.kind is SemanticTypeKind.OBJECT_REF:
        return "ManagedJvmRef"
    return "ManagedJvmValue"


def _collect_types(
    roots: Iterable[SemanticType], plan: JvmRoutePlan
) -> tuple[SemanticType, ...]:
    found: dict[str, SemanticType] = {}

    def visit(item: SemanticType) -> None:
        if item.kind is SemanticTypeKind.VOID:
            return
        key = _suffix(item)
        if key in found:
            return
        found[key] = item
        if item.element is not None:
            visit(item.element)
        elif item.kind is SemanticTypeKind.VALUE_REF:
            assert item.type_id is not None
            route = next(
                value for value in plan.values
                if value.named_type.type_id == item.type_id
            )
            for field in route.fields:
                visit(field.semantic_type)

    for root in roots:
        visit(root)
    return tuple(found[key] for key in sorted(found))


def _bridge_class(feature_id: str) -> str:
    digest = hashlib.sha256(feature_id.encode("utf-8")).hexdigest()[:20]
    return f"supernote.generated.adapters.Identity_{digest}"


def _enum_class(source_declaration_id: str) -> str:
    identity = jvm_adapter_identity(source_declaration_id + "#enum")
    return "supernote.generated.adapters.Adapter_" + identity.rsplit(".", 1)[-1]


def _route_expression(
    *, key: str, adapter_class: str, descriptor: str, method: str = "invoke"
) -> str:
    return (
        "supernote_v3_jvm_route(feature, "
        + json.dumps(key)
        + ", "
        + json.dumps(adapter_class)
        + ", "
        + json.dumps(descriptor)
        + ", "
        + json.dumps(method)
        + ")"
    )


def _helper_route(
    feature_id: str, method: str, descriptor: str
) -> str:
    return _route_expression(
        key=f"jvm-v3-helper:{method}:{descriptor}",
        adapter_class=_bridge_class(feature_id),
        descriptor=descriptor,
        method=method,
    )


def _prototype(semantic: SemanticType) -> str:
    native = _native(semantic)
    return f"""{native} {_from_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value,
    JNIEnv *env,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    supernote::conversion::Budget &budget,
    const std::string &path,
    std::uint64_t depth);
facebook::jsi::Value {_to_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const {native} &value,
    JNIEnv *env,
    const std::shared_ptr<JvmObjectRegistry> &registry,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    supernote::conversion::Budget &budget,
    const std::string &path,
    std::uint64_t depth);
void {_validate_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value,
    supernote::conversion::Budget &budget,
    const std::string &path,
    std::uint64_t depth);"""


def _validate_definition(semantic: SemanticType, plan: JvmRoutePlan) -> str:
    lines = [
        f"void {_validate_name(semantic)}(",
        "    facebook::jsi::Runtime &runtime,",
        "    const facebook::jsi::Value &value,",
        "    supernote::conversion::Budget &budget,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  budget.visit(path, depth);",
        "  if (value.isUndefined()) {",
        f"    {_type_error('a defined value')};",
        "  }",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        lines.extend([
            "  if (value.isNull()) return;",
            f"  {_validate_name(semantic.element)}(",
            "      runtime, value, budget, path, depth + 1);",
        ])
    else:
        lines.extend([
            "  if (value.isNull()) {",
            f"    {_type_error('a non-null value')};",
            "  }",
        ])
        if kind is SemanticTypeKind.SCALAR:
            scalar = semantic.scalar
            if scalar is ScalarKind.BOOL:
                lines.append(f"  if (!value.isBool()) {_type_error('boolean')};")
            elif scalar is ScalarKind.INT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_type_error('an int32 number')};",
                    "  const auto number = value.asNumber();",
                    "  if (!std::isfinite(number) || std::trunc(number) != number ||",
                    "      number < static_cast<double>(std::numeric_limits<std::int32_t>::min()) ||",
                    "      number > static_cast<double>(std::numeric_limits<std::int32_t>::max())) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside int32 range\",",
                    "        \"OUT_OF_RANGE\", path, \"int32\", \"number\");",
                    "  }",
                ])
            elif scalar is ScalarKind.INT64:
                lines.extend([
                    f"  if (!value.isBigInt()) {_type_error('an int64 bigint')};",
                    "  if (!value.getBigInt(runtime).isInt64(runtime)) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside int64 range\",",
                    "        \"OUT_OF_RANGE\", path, \"int64 bigint\", \"bigint\");",
                    "  }",
                ])
            elif scalar is ScalarKind.FLOAT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_type_error('a float32 number')};",
                    "  const auto number = value.asNumber();",
                    "  if (std::isfinite(number) &&",
                    "      (number < static_cast<double>(std::numeric_limits<float>::lowest()) ||",
                    "       number > static_cast<double>(std::numeric_limits<float>::max()))) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside float32 range\",",
                    "        \"OUT_OF_RANGE\", path, \"float32\", \"number\");",
                    "  }",
                ])
            elif scalar is ScalarKind.FLOAT64:
                lines.append(f"  if (!value.isNumber()) {_type_error('a number')};")
            elif scalar is ScalarKind.STRING:
                lines.extend([
                    f"  if (!value.isString()) {_type_error('a string')};",
                    "  auto text = value.asString(runtime).utf8(runtime);",
                    "  budget.check_string_bytes(path, text.size());",
                ])
            else:
                lines.extend([
                    f"  if (!supernote_is_uint8_array(runtime, value)) {_type_error('a Uint8Array')};",
                    "  auto view = value.getObject(runtime);",
                    "  budget.check_byte_buffer(path, supernote_view_index(runtime, view, \"byteLength\"));",
                ])
        elif kind is SemanticTypeKind.OBJECT_REF:
            assert semantic.type_id is not None
            named = plan.named_types_by_id[semantic.type_id]
            lines.extend([
                f"  auto object = try_extract_jvm_object(runtime, value, {json.dumps(semantic.type_id)});",
                "  if (!object) {",
                f"    supernote_throw_type_error(runtime, path + \": expected {named.public_name}\",",
                f"        \"NOMINAL_MISMATCH\", path, {json.dumps(named.public_name)},",
                "        supernote_describe_value(runtime, value));",
                "  }",
                "  budget.reserve(path, sizeof(void *));",
            ])
        elif kind is SemanticTypeKind.ENUM_REF:
            assert semantic.type_id is not None
            route = next(item for item in plan.enums if item.named_type.type_id == semantic.type_id)
            condition = " && ".join(
                f"text != {json.dumps(constant)}" for constant in route.constants
            ) or "true"
            lines.extend([
                f"  if (!value.isString()) {_type_error(route.named_type.public_name)};",
                "  auto text = value.asString(runtime).utf8(runtime);",
                "  budget.check_string_bytes(path, text.size());",
                f"  if ({condition}) {{",
                f"    supernote_throw_type_error(runtime, path + \": expected {route.named_type.public_name}\",",
                f"        \"INVALID_ENUM\", path, {json.dumps(route.named_type.public_name)}, \"string\");",
                "  }",
            ])
        elif kind is SemanticTypeKind.ARRAY:
            assert semantic.element is not None
            lines.extend([
                f"  if (!value.isObject()) {_type_error('a dense Array')};",
                "  auto object = value.getObject(runtime);",
                f"  if (!object.isArray(runtime)) {_type_error('a dense Array')};",
                "  auto array = object.getArray(runtime);",
                "  const auto length = static_cast<std::uint64_t>(array.size(runtime));",
                "  budget.check_array_length(path, length);",
                "  for (std::uint64_t index = 0; index < length; ++index) {",
                "    auto item = array.getValueAtIndex(runtime, static_cast<std::size_t>(index));",
                "    auto item_path = supernote::conversion::index_path(path, index);",
                f"    {_validate_name(semantic.element)}(",
                "        runtime, item, budget, item_path, depth + 1);",
                "  }",
            ])
        else:
            assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
            route = next(item for item in plan.values if item.named_type.type_id == semantic.type_id)
            lines.extend([
                f"  if (!value.isObject()) {_type_error(route.named_type.public_name)};",
                "  auto object = value.getObject(runtime);",
                f"  if (object.isArray(runtime)) {_type_error(route.named_type.public_name)};",
            ])
            for index, field in enumerate(route.fields):
                lines.extend([
                    f"  auto field_{index}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                    f"  auto field_{index}_value = object.getProperty(runtime, {json.dumps(field.public_name)});",
                    f"  {_validate_name(field.semantic_type)}(",
                    f"      runtime, field_{index}_value, budget, field_{index}_path, depth + 1);",
                ])
    lines.append("}")
    return "\n".join(lines)


def _type_error(expected: str) -> str:
    return (
        "supernote_throw_type_error(runtime, path + "
        + json.dumps(f": expected {expected}")
        + ", \"TYPE_MISMATCH\", path, "
        + json.dumps(expected)
        + ", supernote_describe_value(runtime, value))"
    )


def _box_scalar(
    scalar: ScalarKind, expression: str, feature_id: str, indent: str
) -> list[str]:
    method, descriptor, field = {
        ScalarKind.BOOL: ("boxBoolean", "(Z)Ljava/lang/Object;", "z"),
        ScalarKind.INT32: ("boxInt", "(I)Ljava/lang/Object;", "i"),
        ScalarKind.INT64: ("boxLong", "(J)Ljava/lang/Object;", "j"),
        ScalarKind.FLOAT32: ("boxFloat", "(F)Ljava/lang/Object;", "f"),
        ScalarKind.FLOAT64: ("boxDouble", "(D)Ljava/lang/Object;", "d"),
    }[scalar]
    cast = {
        ScalarKind.BOOL: f"{expression} ? JNI_TRUE : JNI_FALSE",
        ScalarKind.INT32: f"static_cast<jint>({expression})",
        ScalarKind.INT64: f"static_cast<jlong>({expression})",
        ScalarKind.FLOAT32: f"static_cast<jfloat>({expression})",
        ScalarKind.FLOAT64: f"static_cast<jdouble>({expression})",
    }[scalar]
    route = _helper_route(feature_id, method, descriptor)
    return [
        f"{indent}auto boxed_route = {route};",
        f"{indent}jvalue boxed_arguments[1]{{}};",
        f"{indent}boxed_arguments[0].{field} = {cast};",
        f"{indent}auto boxed = env->CallStaticObjectMethodA(",
        f"{indent}    static_cast<jclass>(boxed_route->adapter_class.get()),",
        f"{indent}    boxed_route->method, boxed_arguments);",
        f"{indent}require_no_implementation_exception(env);",
        f"{indent}if (boxed == nullptr) throw std::runtime_error(\"JVM boxing returned null\");",
    ]


def _unbox_scalar(
    scalar: ScalarKind, expression: str, feature_id: str, indent: str
) -> tuple[list[str], str]:
    method, descriptor, call, cast = {
        ScalarKind.BOOL: ("unboxBoolean", "(Ljava/lang/Object;)Z", "CallStaticBooleanMethodA", "unboxed == JNI_TRUE"),
        ScalarKind.INT32: ("unboxInt", "(Ljava/lang/Object;)I", "CallStaticIntMethodA", "static_cast<std::int32_t>(unboxed)"),
        ScalarKind.INT64: ("unboxLong", "(Ljava/lang/Object;)J", "CallStaticLongMethodA", "static_cast<std::int64_t>(unboxed)"),
        ScalarKind.FLOAT32: ("unboxFloat", "(Ljava/lang/Object;)F", "CallStaticFloatMethodA", "static_cast<float>(unboxed)"),
        ScalarKind.FLOAT64: ("unboxDouble", "(Ljava/lang/Object;)D", "CallStaticDoubleMethodA", "static_cast<double>(unboxed)"),
    }[scalar]
    route = _helper_route(feature_id, method, descriptor)
    lines = [
        f"{indent}auto unbox_route = {route};",
        f"{indent}jvalue unbox_arguments[1]{{}};",
        f"{indent}unbox_arguments[0].l = {expression};",
        f"{indent}auto unboxed = env->{call}(",
        f"{indent}    static_cast<jclass>(unbox_route->adapter_class.get()),",
        f"{indent}    unbox_route->method, unbox_arguments);",
        f"{indent}require_no_implementation_exception(env);",
    ]
    return lines, cast


def _managed_from_child(
    child: SemanticType,
    expression: str,
    feature_id: str,
    indent: str,
) -> list[str]:
    if child.kind is SemanticTypeKind.SCALAR:
        if child.scalar in {
            ScalarKind.BOOL,
            ScalarKind.INT32,
            ScalarKind.INT64,
            ScalarKind.FLOAT32,
            ScalarKind.FLOAT64,
        }:
            lines = _box_scalar(child.scalar, expression, feature_id, indent)
            lines.append(f"{indent}return ManagedJvmValue(retain_global(env, boxed));")
            return lines
        lines = []
        if child.scalar is ScalarKind.STRING:
            data = (
                "reinterpret_cast<const std::byte *>("
                f"{expression}.data())"
            )
        else:
            data = f"{expression}.data()"
        lines.extend([
            f"{indent}auto boxed = write_byte_array(env, {data}, {expression}.size());",
            f"{indent}return ManagedJvmValue(retain_global(env, boxed));",
        ])
        return lines
    if child.kind is SemanticTypeKind.OBJECT_REF:
        return [
            f"{indent}return ManagedJvmValue({expression}.global_ref());"
        ]
    return [f"{indent}return {expression};"]


def _from_definition(
    semantic: SemanticType, plan: JvmRoutePlan, feature_id: str
) -> str:
    native = _native(semantic)
    lines = [
        f"{native} {_from_name(semantic)}(",
        "    facebook::jsi::Runtime &runtime,",
        "    const facebook::jsi::Value &value,",
        "    JNIEnv *env,",
        "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
        "    supernote::conversion::Budget &budget,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  budget.visit(path, depth);",
        "  if (value.isUndefined()) {",
        f"    {_type_error('a defined value')};",
        "  }",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        child = semantic.element
        lines.extend([
            "  if (value.isNull()) return {};",
            f"  auto converted = {_from_name(child)}(",
            "      runtime, value, env, feature, budget, path, depth + 1);",
        ])
        lines.extend(_managed_from_child(child, "converted", feature_id, "  "))
    else:
        lines.extend([
            "  if (value.isNull()) {",
            f"    {_type_error('a non-null value')};",
            "  }",
        ])
        if kind is SemanticTypeKind.SCALAR:
            scalar = semantic.scalar
            if scalar is ScalarKind.BOOL:
                lines.extend([
                    f"  if (!value.isBool()) {_type_error('boolean')};",
                    "  return value.getBool();",
                ])
            elif scalar is ScalarKind.INT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_type_error('an int32 number')};",
                    "  const auto number = value.asNumber();",
                    "  if (!std::isfinite(number) || std::trunc(number) != number ||",
                    "      number < static_cast<double>(std::numeric_limits<std::int32_t>::min()) ||",
                    "      number > static_cast<double>(std::numeric_limits<std::int32_t>::max())) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside int32 range\");",
                    "  }",
                    "  return static_cast<std::int32_t>(number);",
                ])
            elif scalar is ScalarKind.INT64:
                lines.extend([
                    f"  if (!value.isBigInt()) {_type_error('an int64 bigint')};",
                    "  auto bigint = value.getBigInt(runtime);",
                    "  if (!bigint.isInt64(runtime)) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside int64 range\");",
                    "  }",
                    "  return static_cast<std::int64_t>(bigint.asInt64(runtime));",
                ])
            elif scalar is ScalarKind.FLOAT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_type_error('a float32 number')};",
                    "  const auto number = value.asNumber();",
                    "  if (std::isfinite(number) &&",
                    "      (number < static_cast<double>(std::numeric_limits<float>::lowest()) ||",
                    "       number > static_cast<double>(std::numeric_limits<float>::max()))) {",
                    "    supernote_throw_range_error(runtime, path + \" is outside float32 range\");",
                    "  }",
                    "  return static_cast<float>(number);",
                ])
            elif scalar is ScalarKind.FLOAT64:
                lines.extend([
                    f"  if (!value.isNumber()) {_type_error('a number')};",
                    "  return value.asNumber();",
                ])
            elif scalar is ScalarKind.STRING:
                lines.extend([
                    f"  if (!value.isString()) {_type_error('a string')};",
                    "  auto result = value.asString(runtime).utf8(runtime);",
                    "  budget.check_string_bytes(path, result.size());",
                    "  budget.reserve(path, result.size());",
                    "  return result;",
                ])
            else:
                lines.extend([
                    f"  if (!supernote_is_uint8_array(runtime, value)) {_type_error('a Uint8Array')};",
                    "  auto result = supernote_copy_uint8_array(runtime, value);",
                    "  budget.check_byte_buffer(path, result.size());",
                    "  budget.reserve(path, result.size());",
                    "  return result;",
                ])
        elif kind is SemanticTypeKind.OBJECT_REF:
            assert semantic.type_id is not None
            lines.extend([
                f"  auto result = try_extract_jvm_object(runtime, value, {json.dumps(semantic.type_id)});",
                "  if (!result) {",
                f"    {_type_error('the exact nominal JVM object type')};",
                "  }",
                "  return result;",
            ])
        elif kind is SemanticTypeKind.ENUM_REF:
            assert semantic.type_id is not None
            route = next(
                item for item in plan.enums
                if item.named_type.type_id == semantic.type_id
            )
            adapter = _enum_class(route.named_type.source_declaration_id)
            descriptor = f"([B)L{route.named_type.owner_class.replace('.', '/')};"
            resolved = _route_expression(
                key=f"jvm-v3-enum-from:{semantic.type_id}",
                adapter_class=adapter,
                descriptor=descriptor,
                method="fromName",
            )
            lines.extend([
                f"  if (!value.isString()) {_type_error(route.named_type.public_name)};",
                "  auto text = value.asString(runtime).utf8(runtime);",
                "  budget.check_string_bytes(path, text.size());",
                f"  auto route = {resolved};",
                "  jvalue arguments[1]{};",
                "  arguments[0].l = write_byte_array(",
                "      env, reinterpret_cast<const std::byte *>(text.data()), text.size());",
                "  auto local = env->CallStaticObjectMethodA(",
                "      static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                "  require_no_implementation_exception(env);",
                "  if (local == nullptr) throw std::runtime_error(\"JVM enum adapter returned null\");",
                "  return ManagedJvmValue(retain_global(env, local));",
            ])
        elif kind is SemanticTypeKind.ARRAY:
            assert semantic.element is not None
            child = semantic.element
            create = _helper_route(feature_id, "newList", "()Ljava/util/List;")
            add = _helper_route(
                feature_id,
                "listAdd",
                "(Ljava/util/List;Ljava/lang/Object;)V",
            )
            lines.extend([
                f"  if (!value.isObject()) {_type_error('a dense Array')};",
                "  auto object = value.getObject(runtime);",
                f"  if (!object.isArray(runtime)) {_type_error('a dense Array')};",
                "  auto array = object.getArray(runtime);",
                "  const auto length = static_cast<std::uint64_t>(array.size(runtime));",
                "  budget.check_array_length(path, length);",
                f"  auto create_route = {create};",
                "  auto list = env->CallStaticObjectMethod(",
                "      static_cast<jclass>(create_route->adapter_class.get()),",
                "      create_route->method);",
                "  require_no_implementation_exception(env);",
                "  if (list == nullptr) throw std::runtime_error(\"JVM list adapter returned null\");",
                f"  auto add_route = {add};",
                "  for (std::uint64_t index = 0; index < length; ++index) {",
                "    auto item_value = array.getValueAtIndex(runtime, static_cast<std::size_t>(index));",
                "    auto item_path = supernote::conversion::index_path(path, index);",
                f"    auto item = {_from_name(child)}(runtime, item_value, env, feature, budget, item_path, depth + 1);",
            ])
            lines.extend(_as_jobject(child, "item", feature_id, "    "))
            lines.extend([
                "    jvalue add_arguments[2]{};",
                "    add_arguments[0].l = list;",
                "    add_arguments[1].l = item_object;",
                "    env->CallStaticVoidMethodA(",
                "        static_cast<jclass>(add_route->adapter_class.get()),",
                "        add_route->method, add_arguments);",
                "    require_no_implementation_exception(env);",
                "  }",
                "  return ManagedJvmValue(retain_global(env, list));",
            ])
        else:
            assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
            route = next(
                item for item in plan.values
                if item.named_type.type_id == semantic.type_id
            )
            lines.extend([
                f"  if (!value.isObject()) {_type_error(route.named_type.public_name)};",
                "  auto object = value.getObject(runtime);",
                f"  if (object.isArray(runtime)) {_type_error(route.named_type.public_name)};",
            ])
            for index, field in enumerate(route.fields):
                lines.extend([
                    f"  auto field_{index}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                    f"  auto field_{index}_value = object.getProperty(runtime, {json.dumps(field.public_name)});",
                    f"  auto field_{index} = {_from_name(field.semantic_type)}(",
                    f"      runtime, field_{index}_value, env, feature, budget, field_{index}_path, depth + 1);",
                ])
            resolved = _route_expression(
                key=f"jvm-v3-value-constructor:{semantic.type_id}",
                adapter_class="supernote.generated.adapters.Adapter_"
                + route.constructor.adapter_identity.rsplit(".", 1)[-1],
                descriptor=route.constructor.adapter_descriptor,
            )
            lines.extend([
                f"  auto route = {resolved};",
                f"  jvalue arguments[{max(1, len(route.fields) + 1)}]{{}};",
                "  auto runtime_session = feature->runtime();",
                "  auto context = runtime_session ? runtime_session->platform_context() : nullptr;",
                "  if (!context) throw std::runtime_error(\"platform Context is unavailable\");",
                "  arguments[0].l = static_cast<jobject>(context.get());",
            ])
            for argument_index, field in enumerate(route.constructor_fields):
                field_index = next(
                    index for index, candidate in enumerate(route.fields)
                    if candidate.field_id == field.field_id
                )
                lines.extend(
                    _argument_assignment(
                        field.semantic_type,
                        f"field_{field_index}",
                        argument_index + 1,
                        feature_id,
                        "  ",
                    )
                )
            lines.extend([
                "  auto local = env->CallStaticObjectMethodA(",
                "      static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                "  require_no_implementation_exception(env);",
                "  if (local == nullptr) throw std::runtime_error(\"JVM value constructor returned null\");",
                "  return ManagedJvmValue(retain_global(env, local));",
            ])
    lines.append("}")
    return "\n".join(lines)


def _as_jobject(
    semantic: SemanticType, expression: str, feature_id: str, indent: str
) -> list[str]:
    if semantic.kind is SemanticTypeKind.SCALAR:
        if semantic.scalar in {
            ScalarKind.BOOL,
            ScalarKind.INT32,
            ScalarKind.INT64,
            ScalarKind.FLOAT32,
            ScalarKind.FLOAT64,
        }:
            lines = _box_scalar(semantic.scalar, expression, feature_id, indent)
            lines.append(f"{indent}jobject item_object = boxed;")
            return lines
        if semantic.scalar is ScalarKind.STRING:
            return [
                f"{indent}jobject item_object = write_byte_array(",
                f"{indent}    env, reinterpret_cast<const std::byte *>({expression}.data()), {expression}.size());",
            ]
        return [
            f"{indent}jobject item_object = write_byte_array(",
            f"{indent}    env, {expression}.data(), {expression}.size());"
        ]
    return [
        f"{indent}jobject item_object = {expression} ? {expression}.get() : nullptr;"
    ]


def _argument_assignment(
    semantic: SemanticType,
    expression: str,
    index: int,
    feature_id: str,
    indent: str,
) -> list[str]:
    if semantic.kind is SemanticTypeKind.SCALAR:
        if semantic.scalar is ScalarKind.BOOL:
            return [f"{indent}arguments[{index}].z = {expression} ? JNI_TRUE : JNI_FALSE;"]
        field, cast = {
            ScalarKind.INT32: ("i", "jint"),
            ScalarKind.INT64: ("j", "jlong"),
            ScalarKind.FLOAT32: ("f", "jfloat"),
            ScalarKind.FLOAT64: ("d", "jdouble"),
        }.get(semantic.scalar, (None, None))
        if field is not None:
            return [f"{indent}arguments[{index}].{field} = static_cast<{cast}>({expression});"]
        data = (
            f"reinterpret_cast<const std::byte *>({expression}.data())"
            if semantic.scalar is ScalarKind.STRING
            else f"{expression}.data()"
        )
        return [
            f"{indent}arguments[{index}].l = write_byte_array(",
            f"{indent}    env, {data}, {expression}.size());",
        ]
    return [
        f"{indent}arguments[{index}].l = {expression} ? {expression}.get() : nullptr;"
    ]


def _read_jobject(
    semantic: SemanticType,
    expression: str,
    plan: JvmRoutePlan,
    feature_id: str,
    indent: str,
    target: str,
) -> list[str]:
    if semantic.kind is SemanticTypeKind.NULLABLE:
        return [
            f"{indent}ManagedJvmValue {target} = {expression} == nullptr",
            f"{indent}    ? ManagedJvmValue{{}}",
            f"{indent}    : ManagedJvmValue(retain_global(env, {expression}));",
        ]
    if semantic.kind is SemanticTypeKind.OBJECT_REF:
        assert semantic.type_id is not None
        return [
            f"{indent}if ({expression} == nullptr) throw std::runtime_error(\"JVM object result was null\");",
            f"{indent}ManagedJvmRef {target}({json.dumps(semantic.type_id)}, retain_global(env, {expression}));",
        ]
    if semantic.kind in {
        SemanticTypeKind.ARRAY,
        SemanticTypeKind.VALUE_REF,
        SemanticTypeKind.ENUM_REF,
    }:
        return [
            f"{indent}if ({expression} == nullptr) throw std::runtime_error(\"JVM result was null\");",
            f"{indent}ManagedJvmValue {target}(retain_global(env, {expression}));",
        ]
    assert semantic.kind is SemanticTypeKind.SCALAR
    if semantic.scalar in {ScalarKind.STRING, ScalarKind.BYTES}:
        lines = [
            f"{indent}auto {target}_bytes = read_byte_array(env, static_cast<jbyteArray>({expression}));",
        ]
        if semantic.scalar is ScalarKind.STRING:
            lines.extend([
                f"{indent}std::string {target}(",
                f"{indent}    reinterpret_cast<const char *>({target}_bytes.data()), {target}_bytes.size());",
            ])
        else:
            lines.append(f"{indent}auto {target} = std::move({target}_bytes);")
        return lines
    unbox, converted = _unbox_scalar(
        semantic.scalar, expression, feature_id, indent
    )
    unbox.append(f"{indent}auto {target} = {converted};")
    return unbox


def _to_definition(
    semantic: SemanticType, plan: JvmRoutePlan, feature_id: str
) -> str:
    native = _native(semantic)
    lines = [
        f"facebook::jsi::Value {_to_name(semantic)}(",
        "    facebook::jsi::Runtime &runtime,",
        f"    const {native} &value,",
        "    JNIEnv *env,",
        "    const std::shared_ptr<JvmObjectRegistry> &registry,",
        "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
        "    supernote::conversion::Budget &budget,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  budget.visit(path, depth);",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        child = semantic.element
        lines.append("  if (!value) return facebook::jsi::Value::null();")
        lines.extend(
            _read_jobject(
                child, "value.get()", plan, feature_id, "  ", "child"
            )
        )
        lines.append(
            f"  return {_to_name(child)}(runtime, child, env, registry, feature, budget, path, depth + 1);"
        )
    elif kind is SemanticTypeKind.SCALAR:
        if semantic.scalar is ScalarKind.BOOL:
            lines.append("  return facebook::jsi::Value(value);")
        elif semantic.scalar in {ScalarKind.INT32, ScalarKind.FLOAT32, ScalarKind.FLOAT64}:
            lines.append("  return facebook::jsi::Value(static_cast<double>(value));")
        elif semantic.scalar is ScalarKind.INT64:
            lines.append(
                "  return facebook::jsi::Value(facebook::jsi::BigInt::fromInt64(runtime, value));"
            )
        elif semantic.scalar is ScalarKind.STRING:
            lines.extend([
                "  budget.check_string_bytes(path, value.size());",
                "  return facebook::jsi::Value(facebook::jsi::String::createFromUtf8(runtime, value));",
            ])
        else:
            lines.extend([
                "  budget.check_byte_buffer(path, value.size());",
                "  return supernote_make_uint8_array(runtime, value);",
            ])
    elif kind is SemanticTypeKind.OBJECT_REF:
        assert semantic.type_id is not None
        index = next(
            index for index, route in enumerate(plan.objects)
            if route.named_type.type_id == semantic.type_id
        )
        lines.extend([
            "  budget.reserve(path, sizeof(void *));",
            f"  return facebook::jsi::Value(supernote_v3_wrap_jvm_object_{index}(",
            "      runtime, env, registry, feature, value));",
        ])
    elif kind is SemanticTypeKind.ENUM_REF:
        assert semantic.type_id is not None
        route = next(
            item for item in plan.enums
            if item.named_type.type_id == semantic.type_id
        )
        adapter = _enum_class(route.named_type.source_declaration_id)
        resolved = _route_expression(
            key=f"jvm-v3-enum-name:{semantic.type_id}",
            adapter_class=adapter,
            descriptor=f"(L{route.named_type.owner_class.replace('.', '/')};)[B",
            method="name",
        )
        lines.extend([
            f"  auto route = {resolved};",
            "  jvalue arguments[1]{};",
            "  arguments[0].l = value.get();",
            "  auto bytes = env->CallStaticObjectMethodA(",
            "      static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
            "  require_no_implementation_exception(env);",
            "  auto text_bytes = read_byte_array(env, static_cast<jbyteArray>(bytes));",
            "  std::string text(reinterpret_cast<const char *>(text_bytes.data()), text_bytes.size());",
            "  budget.check_string_bytes(path, text.size());",
            "  return facebook::jsi::Value(facebook::jsi::String::createFromUtf8(runtime, text));",
        ])
    elif kind is SemanticTypeKind.ARRAY:
        assert semantic.element is not None
        size_route = _helper_route(feature_id, "listSize", "(Ljava/util/List;)I")
        get_route = _helper_route(
            feature_id,
            "listGet",
            "(Ljava/util/List;I)Ljava/lang/Object;",
        )
        lines.extend([
            f"  auto size_route = {size_route};",
            "  jvalue size_arguments[1]{};",
            "  size_arguments[0].l = value.get();",
            "  auto length = env->CallStaticIntMethodA(",
            "      static_cast<jclass>(size_route->adapter_class.get()),",
            "      size_route->method, size_arguments);",
            "  require_no_implementation_exception(env);",
            "  if (length < 0) throw std::runtime_error(\"JVM list has invalid size\");",
            "  budget.check_array_length(path, static_cast<std::uint64_t>(length));",
            "  facebook::jsi::Array result(runtime, static_cast<std::size_t>(length));",
            f"  auto get_route = {get_route};",
            "  for (jint index = 0; index < length; ++index) {",
            "    jvalue get_arguments[2]{};",
            "    get_arguments[0].l = value.get();",
            "    get_arguments[1].i = index;",
            "    auto local = env->CallStaticObjectMethodA(",
            "        static_cast<jclass>(get_route->adapter_class.get()),",
            "        get_route->method, get_arguments);",
            "    require_no_implementation_exception(env);",
            "    auto item_path = supernote::conversion::index_path(path, static_cast<std::uint64_t>(index));",
        ])
        lines.extend(
            _read_jobject(
                semantic.element,
                "local",
                plan,
                feature_id,
                "    ",
                "item",
            )
        )
        lines.extend([
            f"    auto converted = {_to_name(semantic.element)}(",
            "        runtime, item, env, registry, feature, budget, item_path, depth + 1);",
            "    result.setValueAtIndex(runtime, static_cast<std::size_t>(index), std::move(converted));",
            "  }",
            "  return facebook::jsi::Value(std::move(result));",
        ])
    else:
        assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
        route = next(
            item for item in plan.values
            if item.named_type.type_id == semantic.type_id
        )
        lines.extend([
            "  facebook::jsi::Object result(runtime);",
        ])
        for index, field in enumerate(route.fields):
            adapter = "supernote.generated.adapters.Adapter_" + field.accessor_identity.rsplit(".", 1)[-1]
            getter = _route_expression(
                key=f"jvm-v3-field-get:{field.source_declaration_id}",
                adapter_class=adapter,
                descriptor=field.getter_descriptor,
                method="get",
            )
            lines.extend([
                f"  auto getter_{index} = {getter};",
                f"  jvalue getter_{index}_arguments[1]{{}};",
                f"  getter_{index}_arguments[0].l = value.get();",
            ])
            lines.extend(
                _call_and_convert_result(
                    field.semantic_type,
                    f"getter_{index}",
                    f"getter_{index}_arguments",
                    f"field_{index}",
                    plan,
                    feature_id,
                    "  ",
                )
            )
            lines.extend([
                f"  auto field_{index}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                f"  auto field_{index}_js = {_to_name(field.semantic_type)}(",
                f"      runtime, field_{index}, env, registry, feature, budget, field_{index}_path, depth + 1);",
                f"  result.setProperty(runtime, {json.dumps(field.public_name)}, std::move(field_{index}_js));",
            ])
        lines.append("  return facebook::jsi::Value(std::move(result));")
    lines.append("}")
    return "\n".join(lines)


def _jni_call(semantic: SemanticType) -> str:
    if semantic.kind is SemanticTypeKind.SCALAR:
        return {
            ScalarKind.BOOL: "CallStaticBooleanMethodA",
            ScalarKind.INT32: "CallStaticIntMethodA",
            ScalarKind.INT64: "CallStaticLongMethodA",
            ScalarKind.FLOAT32: "CallStaticFloatMethodA",
            ScalarKind.FLOAT64: "CallStaticDoubleMethodA",
            ScalarKind.STRING: "CallStaticObjectMethodA",
            ScalarKind.BYTES: "CallStaticObjectMethodA",
        }[semantic.scalar]
    if semantic.kind is SemanticTypeKind.VOID:
        return "CallStaticVoidMethodA"
    return "CallStaticObjectMethodA"


def _call_and_convert_result(
    semantic: SemanticType,
    route: str,
    arguments: str,
    target: str,
    plan: JvmRoutePlan,
    feature_id: str,
    indent: str,
) -> list[str]:
    call = _jni_call(semantic)
    if semantic.kind is SemanticTypeKind.VOID:
        return [
            f"{indent}env->{call}(",
            f"{indent}    static_cast<jclass>({route}->adapter_class.get()),",
            f"{indent}    {route}->method, {arguments});",
            f"{indent}require_no_implementation_exception(env);",
        ]
    lines = [
        f"{indent}auto {target}_raw = env->{call}(",
        f"{indent}    static_cast<jclass>({route}->adapter_class.get()),",
        f"{indent}    {route}->method, {arguments});",
        f"{indent}require_no_implementation_exception(env);",
    ]
    if semantic.kind is SemanticTypeKind.SCALAR and semantic.scalar not in {
        ScalarKind.STRING,
        ScalarKind.BYTES,
    }:
        conversion = {
            ScalarKind.BOOL: f"{target}_raw == JNI_TRUE",
            ScalarKind.INT32: f"static_cast<std::int32_t>({target}_raw)",
            ScalarKind.INT64: f"static_cast<std::int64_t>({target}_raw)",
            ScalarKind.FLOAT32: f"static_cast<float>({target}_raw)",
            ScalarKind.FLOAT64: f"static_cast<double>({target}_raw)",
        }[semantic.scalar]
        lines.append(f"{indent}auto {target} = {conversion};")
    else:
        lines.extend(
            _read_jobject(
                semantic,
                f"{target}_raw",
                plan,
                feature_id,
                indent,
                target,
            )
        )
    return lines


def _callable_body(
    route: JvmCallableRoute,
    *,
    diagnostic: str,
    plan: JvmRoutePlan,
    feature_id: str,
    instance: bool,
    context: bool = False,
    indent: str,
) -> str:
    if route.execution is ExecutionMode.ASYNC:
        raise AssertionError("async callables require the async host-function emitter")
    lines = [
        f"{indent}if (argument_count != {len(route.parameters)}) {{",
        f"{indent}  supernote_throw_type_error(runtime, {json.dumps(diagnostic + ': wrong argument count')},",
        f"{indent}      \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
        f"{indent}      {json.dumps(str(len(route.parameters)) + ' arguments')},",
        f"{indent}      std::to_string(argument_count) + \" arguments\");",
        f"{indent}}}",
        f"{indent}if (!feature || feature->state() != supernote::runtime::FeatureState::ACTIVE) {{",
        f"{indent}  supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");",
        f"{indent}}}",
        f"{indent}AttachedEnv attached;",
        f"{indent}auto *env = attached.get();",
        f"{indent}if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
        f"{indent}LocalFrame frame(env);",
        f"{indent}supernote::conversion::Budget input_budget;",
    ]
    for index, parameter in enumerate(route.parameters):
        lines.extend([
            f"{indent}auto argument_{index} = {_from_name(parameter)}(",
            f"{indent}    runtime, arguments[{index}], env, feature, input_budget,",
            f"{indent}    {json.dumps(diagnostic + ': argument ' + str(index + 1))}, 0);",
        ])
    adapter = "supernote.generated.adapters.Adapter_" + route.adapter_identity.rsplit(".", 1)[-1]
    resolved = _route_expression(
        key=f"jvm-v3-call:{route.source_declaration_id}",
        adapter_class=adapter,
        descriptor=route.adapter_descriptor,
    )
    offset = 1 if (instance or context) else 0
    lines.extend([
        f"{indent}auto resolved = {resolved};",
        f"{indent}jvalue jvm_arguments[{max(1, len(route.parameters) + offset)}]{{}};",
    ])
    if instance:
        lines.append(f"{indent}jvm_arguments[0].l = owner.get();")
    elif context:
        lines.extend([
            f"{indent}auto runtime_session = feature->runtime();",
            f"{indent}auto context_value = runtime_session ? runtime_session->platform_context() : nullptr;",
            f"{indent}if (!context_value) throw std::runtime_error(\"platform Context is unavailable\");",
            f"{indent}jvm_arguments[0].l = static_cast<jobject>(context_value.get());",
        ])
    for index, parameter in enumerate(route.parameters):
        assignments = _argument_assignment(
            parameter,
            f"argument_{index}",
            index + offset,
            feature_id,
            indent,
        )
        lines.extend(line.replace("arguments[", "jvm_arguments[") for line in assignments)
    if route.result.kind is SemanticTypeKind.VOID:
        lines.extend(
            _call_and_convert_result(
                route.result,
                "resolved",
                "jvm_arguments",
                "result",
                plan,
                feature_id,
                indent,
            )
        )
        lines.append(f"{indent}return facebook::jsi::Value::undefined();")
    else:
        lines.extend(
            _call_and_convert_result(
                route.result,
                "resolved",
                "jvm_arguments",
                "result",
                plan,
                feature_id,
                indent,
            )
        )
        lines.extend([
            f"{indent}supernote::conversion::Budget output_budget;",
            f"{indent}return {_to_name(route.result)}(",
            f"{indent}    runtime, result, env, registry, feature, output_budget,",
            f"{indent}    {json.dumps(diagnostic + ': result')}, 0);",
        ])
    body = "\n".join("  " + line for line in lines)
    return f'''{indent}try {{
{body}
{indent}}} catch (const facebook::jsi::JSError &) {{
{indent}  throw;
{indent}}} catch (const supernote::conversion::Failure &error) {{
{indent}  if (error.kind() == supernote::conversion::FailureKind::TYPE) {{
{indent}    supernote_throw_type_error(runtime, error.what());
{indent}  }}
{indent}  if (error.kind() == supernote::conversion::FailureKind::RANGE) {{
{indent}    supernote_throw_range_error(runtime, error.what());
{indent}  }}
{indent}  supernote_throw_error(runtime, "RESOURCE_EXHAUSTED", error.what());
{indent}}} catch (const JvmImplementationFailure &error) {{
{indent}  supernote_throw_error(runtime, "IMPLEMENTATION_ERROR", error.what());
{indent}}} catch (const std::exception &error) {{
{indent}  supernote_throw_error(runtime, "INTERNAL", error.what());
{indent}}}'''


def _async_host_function(
    route: JvmCallableRoute,
    *,
    diagnostic: str,
    plan: JvmRoutePlan,
    feature_id: str,
    receiver: bool,
    indent: str,
) -> str:
    if route.suspend:
        return _suspend_host_function(
            route,
            diagnostic=diagnostic,
            plan=plan,
            feature_id=feature_id,
            receiver=receiver,
            indent=indent,
        )
    native_result = _native(route.result)
    state_value = (
        ""
        if route.result.kind is SemanticTypeKind.VOID
        else f"std::optional<{native_result}> value;"
    )
    adapter = "supernote.generated.adapters.Adapter_" + route.adapter_identity.rsplit(".", 1)[-1]
    resolved = _route_expression(
        key=f"jvm-v3-call:{route.source_declaration_id}",
        adapter_class=adapter,
        descriptor=route.adapter_descriptor,
    )
    offset = 1 if receiver else 0
    worker_lines = [
        "auto feature = implementation_feature;",
        "AttachedEnv attached;",
        "auto *env = attached.get();",
        "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
        "LocalFrame frame(env);",
        f"auto resolved = {resolved};",
        f"jvalue jvm_arguments[{max(1, len(route.parameters) + offset)}]{{}};",
    ]
    if receiver:
        worker_lines.append("jvm_arguments[0].l = owner.get();")
    for index, parameter in enumerate(route.parameters):
        assignments = _argument_assignment(
            parameter,
            f"argument_{index}",
            index + offset,
            feature_id,
            "",
        )
        worker_lines.extend(
            line.replace("arguments[", "jvm_arguments[") for line in assignments
        )
    worker_lines.extend(
        _call_and_convert_result(
            route.result,
            "resolved",
            "jvm_arguments",
            "result",
            plan,
            feature_id,
            "",
        )
    )
    if route.result.kind is not SemanticTypeKind.VOID:
        worker_lines.append("state->value.emplace(std::move(result));")
    worker_lines.append("state->success = true;")

    completion_lines = []
    if route.result.kind is SemanticTypeKind.VOID:
        completion_lines.append(
            "supernote_resolve_operation(runtime, operation_id, facebook::jsi::Value::undefined());"
        )
    else:
        completion_lines.extend([
            "AttachedEnv attached;",
            "auto *env = attached.get();",
            "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
            "LocalFrame frame(env);",
            "auto registry = supernote_v3_jvm_object_registry(runtime);",
            "supernote::conversion::Budget result_budget;",
            f"auto value = {_to_name(route.result)}(",
            "    runtime, *state->value, env, registry, completion_feature,",
            f"    result_budget, {json.dumps(diagnostic + ': result')}, 0);",
            "supernote_resolve_operation(runtime, operation_id, std::move(value));",
        ])

    outer_conversions = [
        "if (argument_count != " + str(len(route.parameters)) + ") {",
        "  supernote_throw_type_error(runtime, "
        + json.dumps(diagnostic + ": wrong argument count")
        + ", \"ARITY_MISMATCH\", "
        + json.dumps(diagnostic)
        + ", "
        + json.dumps(str(len(route.parameters)) + " arguments")
        + ", std::to_string(argument_count) + \" arguments\");",
        "}",
        "AttachedEnv attached;",
        "auto *env = attached.get();",
        "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
        "LocalFrame frame(env);",
        "supernote::conversion::Budget input_budget;",
    ]
    for index, parameter in enumerate(route.parameters):
        outer_conversions.extend([
            f"auto argument_{index} = {_from_name(parameter)}(",
            f"    runtime, arguments[{index}], env, feature, input_budget,",
            f"    {json.dumps(diagnostic + ': argument ' + str(index + 1))}, 0);",
        ])
    outer_conversions.extend([
        "if (!feature || feature->state() != supernote::runtime::FeatureState::ACTIVE) {",
        "  supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");",
        "}",
        "struct AsyncState {",
        "  bool success{false};",
        f"  {state_value}",
        "  std::string error_code{\"IMPLEMENTATION_ERROR\"};",
        "  std::string error;",
        "};",
        "auto state = std::make_shared<AsyncState>();",
    ])
    retained_types = [
        *(["ManagedJvmRef"] if receiver else []),
        *(_native(parameter) for parameter in route.parameters),
    ]
    retained_values = [
        *(["owner"] if receiver else []),
        *(f"argument_{index}" for index, _ in enumerate(route.parameters)),
    ]
    if retained_types:
        outer_conversions.extend([
            "auto retained_input_state = std::make_shared<std::tuple<",
            "    " + ", ".join(retained_types) + ">>(",
            "    " + ", ".join(retained_values) + ");",
        ])
    else:
        outer_conversions.append(
            "auto retained_input_state = std::make_shared<std::tuple<>>();"
        )
    executor_captures = ["feature", "state", "retained_input_state"]
    worker_captures = ["operation", "operation_id", "weak_feature", "state"]
    if receiver:
        executor_captures.append("owner")
        worker_captures.append("owner = std::move(owner)")
    for index, _ in enumerate(route.parameters):
        executor_captures.append(f"argument_{index} = std::move(argument_{index})")
        worker_captures.append(f"argument_{index} = std::move(argument_{index})")
    worker = "\n".join("                  " + line for line in worker_lines)
    completion = "\n".join("                        " + line for line in completion_lines)
    outer = "\n".join(indent + "      " + line for line in outer_conversions)
    argument_parameter = (
        "const facebook::jsi::Value *arguments"
        if route.parameters
        else "const facebook::jsi::Value *"
    )
    return f'''facebook::jsi::Function::createFromHostFunction(
{indent}    runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(route.public_name)}),
{indent}    {len(route.parameters)},
{indent}    [feature{', owner' if receiver else ''}](facebook::jsi::Runtime &runtime,
{indent}       const facebook::jsi::Value &, {argument_parameter},
{indent}       std::size_t argument_count) mutable -> facebook::jsi::Value {{
{indent}      try {{
{outer}
{indent}        auto executor = facebook::jsi::Function::createFromHostFunction(
{indent}            runtime,
{indent}            facebook::jsi::PropNameID::forAscii(runtime, "SupernoteAsyncExecutor"), 2,
{indent}            [{', '.join(executor_captures)}](facebook::jsi::Runtime &runtime,
{indent}               const facebook::jsi::Value &,
{indent}               const facebook::jsi::Value *continuation_arguments,
{indent}               std::size_t continuation_count) mutable -> facebook::jsi::Value {{
{indent}              if (continuation_count != 2 ||
{indent}                  !continuation_arguments[0].isObject() ||
{indent}                  !continuation_arguments[1].isObject()) {{
{indent}                throw facebook::jsi::JSError(
{indent}                    runtime, "Promise supplied invalid continuation functions");
{indent}              }}
{indent}              auto operation = feature->accept_factory(
{indent}                  [](supernote::runtime::SessionId operation_id) {{
{indent}                    return [operation_id](void *runtime_pointer) {{
{indent}                      auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);
{indent}                      supernote_reject_operation(
{indent}                          runtime, operation_id, "FEATURE_CLOSED",
{indent}                          "feature closed before async completion");
{indent}                    }};
{indent}                  }});
{indent}              if (!operation) {{
{indent}                supernote_reject_new_promise(
{indent}                    runtime, continuation_arguments[1], "FEATURE_CLOSED",
{indent}                    "feature is closed");
{indent}                return facebook::jsi::Value::undefined();
{indent}              }}
{indent}              operation->set_retained_state(retained_input_state);
{indent}              const auto operation_id = operation->id();
{indent}              supernote_register_continuation(
{indent}                  runtime, operation_id, continuation_arguments[0],
{indent}                  continuation_arguments[1]);
{indent}              std::weak_ptr<supernote::runtime::FeatureSession> weak_feature = feature;
{indent}              auto work = supernote::runtime::process_services().workers().submit(
{indent}                  [{', '.join(worker_captures)}](
{indent}                      supernote::runtime::CancellationToken executor_cancel) mutable {{
{indent}                    if (executor_cancel.is_cancelled() ||
{indent}                        operation->cancellation_token().is_cancelled()) return;
{indent}                    auto implementation_feature = weak_feature.lock();
{indent}                    if (!implementation_feature) return;
{indent}                    supernote::runtime::FeatureCallScope feature_call_scope(
{indent}                        implementation_feature);
{indent}                    try {{
{worker}
{indent}                    }} catch (const JvmImplementationFailure &error) {{
{indent}                      state->error_code = "IMPLEMENTATION_ERROR";
{indent}                      state->error = error.what();
{indent}                    }} catch (const std::exception &error) {{
{indent}                      state->error_code = "INTERNAL";
{indent}                      state->error = error.what();
{indent}                    }} catch (...) {{
{indent}                      state->error_code = "INTERNAL";
{indent}                      state->error = "unknown JVM route failure";
{indent}                    }}
{indent}                    if (executor_cancel.is_cancelled() ||
{indent}                        operation->cancellation_token().is_cancelled()) return;
{indent}                    auto completion_feature = weak_feature.lock();
{indent}                    if (!completion_feature) return;
{indent}                    completion_feature->schedule_completion(
{indent}                        operation,
{indent}                        [state, operation_id, completion_feature](void *runtime_pointer) {{
{indent}                          auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);
{indent}                          if (!state->success) {{
{indent}                            supernote_reject_operation(
{indent}                                runtime, operation_id, state->error_code.c_str(),
{indent}                                state->error.empty() ? "JVM implementation failed" : state->error);
{indent}                            return;
{indent}                          }}
{indent}                          try {{
{completion}
{indent}                          }} catch (const std::exception &error) {{
{indent}                            supernote_reject_operation(
{indent}                                runtime, operation_id, "INTERNAL", error.what());
{indent}                          }}
{indent}                        }});
{indent}                  }});
{indent}              operation->set_work(work);
{indent}              if (!work.accepted()) {{
{indent}                feature->schedule_completion(
{indent}                    operation, [operation_id](void *runtime_pointer) {{
{indent}                      auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);
{indent}                      supernote_reject_operation(
{indent}                          runtime, operation_id, "RESOURCE_EXHAUSTED",
{indent}                          "Supernote worker queue is full");
{indent}                    }});
{indent}              }}
{indent}              return facebook::jsi::Value::undefined();
{indent}            }});
{indent}        auto promise = runtime.global().getPropertyAsFunction(runtime, "Promise");
{indent}        const facebook::jsi::Value executor_argument(std::move(executor));
{indent}        return promise.callAsConstructor(
{indent}            runtime, &executor_argument, static_cast<std::size_t>(1));
{indent}      }} catch (const facebook::jsi::JSError &) {{
{indent}        throw;
{indent}      }} catch (const std::exception &error) {{
{indent}        supernote_throw_error(runtime, "INTERNAL", error.what());
{indent}      }}
{indent}    }})'''


def _suspend_host_function(
    route: JvmCallableRoute,
    *,
    diagnostic: str,
    plan: JvmRoutePlan,
    feature_id: str,
    receiver: bool,
    indent: str,
) -> str:
    native_result = _native(route.result)
    state_value = (
        ""
        if route.result.kind is SemanticTypeKind.VOID
        else f"std::optional<{native_result}> value;"
    )
    adapter = (
        "supernote.generated.adapters.Adapter_"
        + route.adapter_identity.rsplit(".", 1)[-1]
    )
    resolved = _route_expression(
        key=f"jvm-v3-call:{route.source_declaration_id}",
        adapter_class=adapter,
        descriptor=route.adapter_descriptor,
    )
    cancel_resolved = _route_expression(
        key="jvm-v3-coroutine-cancel",
        adapter_class="supernote.generated.runtime.SupernoteCoroutineBridge",
        descriptor="(Lkotlinx/coroutines/Job;)V",
        method="cancel",
    )
    offset = 1 if receiver else 0
    argument_count = len(route.parameters) + offset + 1
    launch_lines = [
        "auto feature = implementation_feature;",
        "AttachedEnv attached;",
        "auto *env = attached.get();",
        "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
        "LocalFrame frame(env);",
        f"auto resolved = {resolved};",
        f"auto cancel_resolved = {cancel_resolved};",
        f"jvalue jvm_arguments[{max(1, argument_count)}]{{}};",
    ]
    if receiver:
        launch_lines.append("jvm_arguments[0].l = owner.get();")
    for index, parameter in enumerate(route.parameters):
        assignments = _argument_assignment(
            parameter,
            f"argument_{index}",
            index + offset,
            feature_id,
            "",
        )
        launch_lines.extend(
            line.replace("arguments[", "jvm_arguments[") for line in assignments
        )
    launch_lines.extend([
        f"jvm_arguments[{argument_count - 1}].j = static_cast<jlong>(completion_id);",
        "auto local_job = env->CallStaticObjectMethodA(",
        "    static_cast<jclass>(resolved->adapter_class.get()),",
        "    resolved->method, jvm_arguments);",
        "if (env->ExceptionCheck()) require_no_implementation_exception(env);",
        "if (local_job == nullptr) {",
        "  throw std::runtime_error(\"cannot launch generated Kotlin coroutine adapter\");",
        "}",
        "auto job = retain_global(env, local_job);",
        "operation->set_cancel_hook([completion_id, job, cancel_resolved] {",
        "  supernote::runtime::process_services().discard_jvm_async_completion(completion_id);",
        "  try {",
        "    AttachedEnv attached;",
        "    auto *env = attached.get();",
        "    if (env == nullptr) return;",
        "    LocalFrame frame(env);",
        "    jvalue arguments[1]{}; arguments[0].l = static_cast<jobject>(job.get());",
        "    env->CallStaticVoidMethodA(",
        "        static_cast<jclass>(cancel_resolved->adapter_class.get()),",
        "        cancel_resolved->method, arguments);",
        "    clear_exception(env);",
        "  } catch (...) {}",
        "});",
    ])
    decoded = []
    if route.result.kind is SemanticTypeKind.VOID:
        decoded.append("state->success = true;")
    else:
        decoded.extend([
            "auto *env = static_cast<JNIEnv *>(environment);",
            "auto object = static_cast<jobject>(result);",
            "if (env == nullptr) {",
            "  throw std::runtime_error(\"Kotlin coroutine result has no JNI environment\");",
            "}",
        ])
        if route.result.kind is not SemanticTypeKind.NULLABLE:
            decoded.extend([
                "if (object == nullptr) {",
                "  throw std::runtime_error(\"Kotlin coroutine returned null\");",
                "}",
            ])
        decoded.append("LocalFrame frame(env);")
        decoded.extend(
            line.strip()
            for line in _read_jobject(
                route.result,
                "object",
                plan,
                feature_id,
                "",
                "decoded_result",
            )
        )
        decoded.extend([
            "state->value.emplace(std::move(decoded_result));",
            "state->success = true;",
        ])
    completion_lines = []
    if route.result.kind is SemanticTypeKind.VOID:
        completion_lines.append(
            "supernote_resolve_operation(runtime, operation_id, facebook::jsi::Value::undefined());"
        )
    else:
        completion_lines.extend([
            "AttachedEnv attached;",
            "auto *env = attached.get();",
            "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
            "LocalFrame frame(env);",
            "auto registry = supernote_v3_jvm_object_registry(runtime);",
            "supernote::conversion::Budget result_budget;",
            f"auto value = {_to_name(route.result)}(",
            "    runtime, *state->value, env, registry, completion_feature,",
            f"    result_budget, {json.dumps(diagnostic + ': result')}, 0);",
            "supernote_resolve_operation(runtime, operation_id, std::move(value));",
        ])
    outer = [
        f"if (argument_count != {len(route.parameters)}) {{",
        "  supernote_throw_type_error(runtime, "
        + json.dumps(diagnostic + ": wrong argument count")
        + ", \"ARITY_MISMATCH\", "
        + json.dumps(diagnostic)
        + ", "
        + json.dumps(str(len(route.parameters)) + " arguments")
        + ", std::to_string(argument_count) + \" arguments\");",
        "}",
        "AttachedEnv attached;",
        "auto *env = attached.get();",
        "if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
        "LocalFrame frame(env);",
        "supernote::conversion::Budget input_budget;",
    ]
    for index, parameter in enumerate(route.parameters):
        outer.extend([
            f"auto argument_{index} = {_from_name(parameter)}(",
            f"    runtime, arguments[{index}], env, feature, input_budget,",
            f"    {json.dumps(diagnostic + ': argument ' + str(index + 1))}, 0);",
        ])
    outer.extend([
        "if (!feature || feature->state() != supernote::runtime::FeatureState::ACTIVE) {",
        "  supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");",
        "}",
        "struct SuspendState {",
        "  bool success{false};",
        f"  {state_value}",
        "  std::string error_code;",
        "  std::string error;",
        "};",
        "auto state = std::make_shared<SuspendState>();",
    ])
    retained_types = [
        *(["ManagedJvmRef"] if receiver else []),
        *(_native(parameter) for parameter in route.parameters),
    ]
    retained_values = [
        *(["owner"] if receiver else []),
        *(f"argument_{index}" for index, _ in enumerate(route.parameters)),
    ]
    if retained_types:
        outer.extend([
            "auto retained_input_state = std::make_shared<std::tuple<",
            "    " + ", ".join(retained_types) + ">>(",
            "    " + ", ".join(retained_values) + ");",
        ])
    else:
        outer.append("auto retained_input_state = std::make_shared<std::tuple<>>();")
    executor_captures = ["feature", "state", "retained_input_state"]
    worker_captures = [
        "operation",
        "weak_feature",
        "completion_id",
    ]
    if receiver:
        executor_captures.append("owner")
        worker_captures.append("owner = std::move(owner)")
    for index, _ in enumerate(route.parameters):
        executor_captures.append(f"argument_{index} = std::move(argument_{index})")
        worker_captures.append(f"argument_{index} = std::move(argument_{index})")
    outer_text = "\n".join(indent + "      " + line for line in outer)
    launch = "\n".join(indent + "                    " + line for line in launch_lines)
    decode = "\n".join(indent + "                                " + line for line in decoded)
    completion = "\n".join(
        indent + "                                  " + line
        for line in completion_lines
    )
    argument_parameter = (
        "const facebook::jsi::Value *arguments"
        if route.parameters
        else "const facebook::jsi::Value *"
    )
    return f'''facebook::jsi::Function::createFromHostFunction(
{indent}    runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(route.public_name)}),
{indent}    {len(route.parameters)},
{indent}    [feature{', owner' if receiver else ''}](facebook::jsi::Runtime &runtime,
{indent}       const facebook::jsi::Value &, {argument_parameter},
{indent}       std::size_t argument_count) mutable -> facebook::jsi::Value {{
{indent}      try {{
{outer_text}
{indent}        auto executor = facebook::jsi::Function::createFromHostFunction(
{indent}            runtime, facebook::jsi::PropNameID::forAscii(runtime, "SupernoteSuspendExecutor"), 2,
{indent}            [{', '.join(executor_captures)}](facebook::jsi::Runtime &runtime,
{indent}               const facebook::jsi::Value &,
{indent}               const facebook::jsi::Value *continuation_arguments,
{indent}               std::size_t continuation_count) mutable -> facebook::jsi::Value {{
{indent}              if (continuation_count != 2 ||
{indent}                  !continuation_arguments[0].isObject() ||
{indent}                  !continuation_arguments[1].isObject()) {{
{indent}                throw facebook::jsi::JSError(
{indent}                    runtime, "Promise supplied invalid continuation functions");
{indent}              }}
{indent}              auto operation = feature->accept_factory(
{indent}                  [](supernote::runtime::SessionId operation_id) {{
{indent}                    return [operation_id](void *runtime_pointer) {{
{indent}                      auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);
{indent}                      supernote_reject_operation(
{indent}                          runtime, operation_id, "FEATURE_CLOSED",
{indent}                          "feature closed before async completion");
{indent}                    }};
{indent}                  }});
{indent}              if (!operation) {{
{indent}                supernote_reject_new_promise(
{indent}                    runtime, continuation_arguments[1], "FEATURE_CLOSED",
{indent}                    "feature is closed");
{indent}                return facebook::jsi::Value::undefined();
{indent}              }}
{indent}              operation->set_retained_state(retained_input_state);
{indent}              const auto operation_id = operation->id();
{indent}              supernote_register_continuation(
{indent}                  runtime, operation_id, continuation_arguments[0],
{indent}                  continuation_arguments[1]);
{indent}              std::weak_ptr<supernote::runtime::FeatureSession> weak_feature = feature;
{indent}              const auto completion_id =
{indent}                  supernote::runtime::process_services().register_jvm_async_completion(
{indent}                      [operation, operation_id, weak_feature, state](
{indent}                          void *environment, void *result,
{indent}                          std::string error_code, std::string error_message) mutable {{
{indent}                        if (operation->cancellation_token().is_cancelled()) return;
{indent}                        if (!error_code.empty()) {{
{indent}                          state->error_code = std::move(error_code);
{indent}                          state->error = std::move(error_message);
{indent}                        }} else {{
{indent}                          try {{
{decode}
{indent}                          }} catch (const std::exception &error) {{
{indent}                            state->error_code = "INTERNAL";
{indent}                            state->error = error.what();
{indent}                          }} catch (...) {{
{indent}                            state->error_code = "INTERNAL";
{indent}                            state->error = "cannot decode Kotlin coroutine result";
{indent}                          }}
{indent}                        }}
{indent}                        if (operation->cancellation_token().is_cancelled()) return;
{indent}                        auto completion_feature = weak_feature.lock();
{indent}                        if (!completion_feature) return;
{indent}                        completion_feature->schedule_completion(
{indent}                            operation,
{indent}                            [state, operation_id, completion_feature](void *runtime_pointer) {{
{indent}                              auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);
{indent}                              if (!state->success) {{
{indent}                                supernote_reject_operation(
{indent}                                    runtime, operation_id,
{indent}                                    state->error_code.empty() ? "INTERNAL" : state->error_code.c_str(),
{indent}                                    state->error.empty() ? "Kotlin coroutine failed" : state->error);
{indent}                                return;
{indent}                              }}
{indent}                              try {{
{completion}
{indent}                              }} catch (const std::exception &error) {{
{indent}                                supernote_reject_operation(
{indent}                                    runtime, operation_id, "INTERNAL", error.what());
{indent}                              }}
{indent}                            }});
{indent}                      }});
{indent}              operation->set_cancel_hook([completion_id] {{
{indent}                supernote::runtime::process_services().discard_jvm_async_completion(completion_id);
{indent}              }});
{indent}              auto work = supernote::runtime::process_services().workers().submit(
{indent}                  [{', '.join(worker_captures)}](
{indent}                      supernote::runtime::CancellationToken executor_cancel) mutable {{
{indent}                    if (executor_cancel.is_cancelled() ||
{indent}                        operation->cancellation_token().is_cancelled()) return;
{indent}                    auto implementation_feature = weak_feature.lock();
{indent}                    if (!implementation_feature ||
{indent}                        implementation_feature->state() !=
{indent}                            supernote::runtime::FeatureState::ACTIVE) return;
{indent}                    supernote::runtime::FeatureCallScope feature_call_scope(
{indent}                        implementation_feature);
{indent}                    try {{
{launch}
{indent}                    }} catch (const JvmImplementationFailure &error) {{
{indent}                      supernote::runtime::process_services().complete_jvm_async(
{indent}                          completion_id, nullptr, nullptr, "IMPLEMENTATION_ERROR", error.what());
{indent}                    }} catch (const std::exception &error) {{
{indent}                      supernote::runtime::process_services().complete_jvm_async(
{indent}                          completion_id, nullptr, nullptr, "INTERNAL", error.what());
{indent}                    }} catch (...) {{
{indent}                      supernote::runtime::process_services().complete_jvm_async(
{indent}                          completion_id, nullptr, nullptr, "INTERNAL",
{indent}                          "cannot launch Kotlin coroutine adapter");
{indent}                    }}
{indent}                  }});
{indent}              operation->set_work(work);
{indent}              if (!work.accepted()) {{
{indent}                supernote::runtime::process_services().complete_jvm_async(
{indent}                    completion_id, nullptr, nullptr, "RESOURCE_EXHAUSTED",
{indent}                    "Supernote worker queue is full");
{indent}              }}
{indent}              return facebook::jsi::Value::undefined();
{indent}            }});
{indent}        auto promise = runtime.global().getPropertyAsFunction(runtime, "Promise");
{indent}        const facebook::jsi::Value executor_argument(std::move(executor));
{indent}        return promise.callAsConstructor(
{indent}            runtime, &executor_argument, static_cast<std::size_t>(1));
{indent}      }} catch (const facebook::jsi::JSError &) {{
{indent}        throw;
{indent}      }} catch (const std::exception &error) {{
{indent}        supernote_throw_error(runtime, "INTERNAL", error.what());
{indent}      }}
{indent}    }})'''


def _jvm_preflight_host_function(
    route: JvmCallableRoute,
    *,
    diagnostic: str,
    name: str,
    check: bool,
    indent: str,
) -> str:
    lines = []
    if check:
        lines.extend([
            f"{indent}      if (argument_count != {len(route.parameters)}) {{",
            f"{indent}        auto error = supernote_make_builtin_error(",
            f"{indent}            runtime, \"TypeError\", {json.dumps(diagnostic + ': wrong argument count')},",
            f"{indent}            \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
            f"{indent}            {json.dumps(str(len(route.parameters)) + ' arguments')},",
            f"{indent}            std::to_string(argument_count) + \" arguments\");",
            f"{indent}        return supernote_validation_failure(runtime, std::move(error));",
            f"{indent}      }}",
        ])
    else:
        lines.append(
            f"{indent}      if (argument_count != {len(route.parameters)}) return facebook::jsi::Value(false);"
        )
    lines.extend([
        f"{indent}      try {{",
        f"{indent}        supernote::conversion::Budget input_budget;",
    ])
    for index, semantic in enumerate(route.parameters):
        lines.extend([
            f"{indent}        {_validate_name(semantic)}(",
            f"{indent}            runtime, arguments[{index}], input_budget,",
            f"{indent}            {json.dumps(diagnostic + '.argument[' + str(index) + ']')}, 1);",
        ])
    lines.append(
        f"{indent}        return "
        + ("supernote_validation_success(runtime);" if check else "facebook::jsi::Value(true);")
    )
    lines.extend([
        f"{indent}      }} catch (const facebook::jsi::JSError &error) {{",
        (
            f"{indent}        return supernote_validation_failure(\n"
            f"{indent}            runtime, facebook::jsi::Value(runtime, error.value()));"
            if check
            else f"{indent}        return facebook::jsi::Value(false);"
        ),
        f"{indent}      }} catch (const supernote::conversion::Failure &failure) {{",
        f"{indent}        if (failure.kind() == supernote::conversion::FailureKind::ALLOCATION) {{",
        f"{indent}          supernote_throw_error(runtime, \"RESOURCE_EXHAUSTED\", failure.what());",
        f"{indent}        }}",
    ])
    if check:
        lines.extend([
            f"{indent}        const bool range =",
            f"{indent}            failure.kind() == supernote::conversion::FailureKind::RANGE;",
            f"{indent}        auto error = supernote_make_builtin_error(",
            f"{indent}            runtime, range ? \"RangeError\" : \"TypeError\", failure.what(),",
            f"{indent}            range ? \"LIMIT_EXCEEDED\" : \"TYPE_MISMATCH\",",
            f"{indent}            failure.path(), \"within generated conversion limits\", \"rejected\");",
            f"{indent}        return supernote_validation_failure(runtime, std::move(error));",
        ])
    else:
        lines.append(f"{indent}        return facebook::jsi::Value(false);")
    lines.extend([
        f"{indent}      }} catch (const std::exception &error) {{",
        f"{indent}        supernote_throw_error(runtime, \"INTERNAL\", error.what());",
        f"{indent}      }}",
    ])
    argument_parameter = (
        "const facebook::jsi::Value *arguments"
        if route.parameters
        else "const facebook::jsi::Value *"
    )
    return (
        "facebook::jsi::Function::createFromHostFunction(\n"
        f"{indent}    runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(name)}),\n"
        f"{indent}    {len(route.parameters)},\n"
        f"{indent}    [](facebook::jsi::Runtime &runtime, const facebook::jsi::Value &,\n"
        f"{indent}       {argument_parameter}, std::size_t argument_count) -> facebook::jsi::Value {{\n"
        + "\n".join(lines)
        + f"\n{indent}    }})"
    )


def _with_jvm_preflight(
    function: str,
    route: JvmCallableRoute,
    *,
    diagnostic: str,
    name: str,
    indent: str,
) -> str:
    accepts = _jvm_preflight_host_function(
        route, diagnostic=diagnostic, name=name + ".accepts", check=False, indent=indent
    )
    check = _jvm_preflight_host_function(
        route, diagnostic=diagnostic, name=name + ".checkArguments", check=True, indent=indent
    )
    return (
        "supernote_attach_preflight(\n"
        f"{indent}    runtime,\n"
        f"{indent}    {function},\n"
        f"{indent}    {accepts},\n"
        f"{indent}    {check})"
    )


def _wrapper(
    plan: JvmRoutePlan,
    item: JvmObjectRoute,
    index: int,
    module_name: str,
    feature_id: str,
) -> str:
    method_rows = []
    for route in item.methods:
        if not route.javascript_public:
            continue
        if route.static:
            continue
        if route.execution is ExecutionMode.ASYNC:
            function = _async_host_function(
                route,
                diagnostic=f"{module_name}.{item.named_type.public_name}.{route.public_name}",
                plan=plan,
                feature_id=feature_id,
                receiver=True,
                indent="      ",
            )
            function = _with_jvm_preflight(
                function,
                route,
                diagnostic=f"{module_name}.{item.named_type.public_name}.{route.public_name}",
                name=route.public_name,
                indent="      ",
            )
            method_rows.append(f'''    if (property == {json.dumps(route.public_name)}) {{
      auto owner = owner_;
      auto feature = feature_;
      return facebook::jsi::Value({function});
    }}''')
            continue
        body = _callable_body(
            route,
            diagnostic=f"{module_name}.{item.named_type.public_name}.{route.public_name}",
            plan=plan,
            feature_id=feature_id,
            instance=True,
            context=False,
            indent="            ",
        )
        main_function = f'''facebook::jsi::Function::createFromHostFunction(
          runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(route.public_name)}),
          {len(route.parameters)},
          [owner, feature, registry](facebook::jsi::Runtime &runtime,
             const facebook::jsi::Value &,
             const facebook::jsi::Value *arguments,
             std::size_t argument_count) -> facebook::jsi::Value {{
{body}
          }})'''
        function = _with_jvm_preflight(
            main_function,
            route,
            diagnostic=f"{module_name}.{item.named_type.public_name}.{route.public_name}",
            name=route.public_name,
            indent="      ",
        )
        method_rows.append(f'''    if (property == {json.dumps(route.public_name)}) {{
      auto owner = owner_;
      auto feature = feature_;
      auto registry = registry_;
      return facebook::jsi::Value({function});
    }}''')
    for field_index, field in enumerate(item.fields):
        adapter = "supernote.generated.adapters.Adapter_" + field.accessor_identity.rsplit(".", 1)[-1]
        getter = _route_expression(
            key=f"jvm-v3-field-get:{field.source_declaration_id}",
            adapter_class=adapter,
            descriptor=field.getter_descriptor,
            method="get",
        ).replace("feature", "feature_")
        get_lines = [
            "      AttachedEnv attached;",
            "      auto *env = attached.get();",
            "      if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
            "      LocalFrame frame(env);",
            f"      auto route = {getter};",
            "      jvalue arguments[1]{};",
            "      arguments[0].l = owner_.get();",
        ]
        get_lines.extend(
            _call_and_convert_result(
                field.semantic_type,
                "route",
                "arguments",
                "result",
                plan,
                feature_id,
                "      ",
            )
        )
        get_lines.extend([
            "      supernote::conversion::Budget budget;",
            f"      return {_to_name(field.semantic_type)}(",
            "          runtime, result, env, registry_, feature_, budget,",
            f"          {json.dumps(module_name + '.' + item.named_type.public_name + '.' + field.public_name)}, 0);",
        ])
        method_rows.append(
            f"    if (property == {json.dumps(field.public_name)}) {{\n"
            + "\n".join(get_lines)
            + "\n    }"
        )
    property_names = [
        route.public_name
        for route in item.methods
        if route.javascript_public and not route.static
    ] + [field.public_name for field in item.fields]
    names = "\n".join(
        "    names.push_back(facebook::jsi::PropNameID::forAscii(runtime, "
        + json.dumps(name)
        + "));"
        for name in property_names
    )
    setters = []
    for field in item.fields:
        if not field.mutable:
            continue
        adapter = "supernote.generated.adapters.Adapter_" + field.accessor_identity.rsplit(".", 1)[-1]
        setter = _route_expression(
            key=f"jvm-v3-field-set:{field.source_declaration_id}",
            adapter_class=adapter,
            descriptor=field.setter_descriptor or "",
            method="set",
        ).replace("feature", "feature_")
        rows = [
            f"    if (property == {json.dumps(field.public_name)}) {{",
            "      AttachedEnv attached;",
            "      auto *env = attached.get();",
            "      if (env == nullptr) throw std::runtime_error(\"cannot attach to JavaVM\");",
            "      LocalFrame frame(env);",
            "      supernote::conversion::Budget budget;",
            f"      auto converted = {_from_name(field.semantic_type)}(",
            "          runtime, value, env, feature_, budget,",
            f"          {json.dumps(module_name + '.' + item.named_type.public_name + '.' + field.public_name)}, 0);",
            f"      auto route = {setter};",
            "      jvalue arguments[2]{};",
            "      arguments[0].l = owner_.get();",
        ]
        rows.extend(
            _argument_assignment(
                field.semantic_type,
                "converted",
                1,
                feature_id,
                "      ",
            )
        )
        rows.extend([
            "      env->CallStaticVoidMethodA(",
            "          static_cast<jclass>(route->adapter_class.get()),",
            "          route->method, arguments);",
            "      require_no_implementation_exception(env);",
            "      return;",
            "    }",
        ])
        setters.append("\n".join(rows))
    get_rows = "\n".join(
        "  " + line for line in "\n".join(method_rows).splitlines()
    )
    set_rows = "\n".join(
        "  " + line for line in "\n".join(setters).splitlines()
    )
    return f'''class GeneratedV3JvmObject{index}HostObject final
    : public JvmObjectHandleBase {{
 public:
  GeneratedV3JvmObject{index}HostObject(
      ManagedJvmRef owner,
      std::shared_ptr<supernote::runtime::FeatureSession> feature,
      std::shared_ptr<JvmObjectRegistry> registry)
      : owner_(std::move(owner)),
        feature_(std::move(feature)),
        registry_(std::move(registry)) {{}}

  std::string_view type_id() const noexcept override {{ return owner_.type_id(); }}
  ManagedJvmRef managed_ref() const override {{ return owner_; }}

  facebook::jsi::Value get(
      facebook::jsi::Runtime &runtime,
      const facebook::jsi::PropNameID &name) override {{
    try {{
      const auto property = name.utf8(runtime);
{get_rows}
      return facebook::jsi::Value::undefined();
    }} catch (const facebook::jsi::JSError &) {{
      throw;
    }} catch (const supernote::conversion::Failure &error) {{
      if (error.kind() == supernote::conversion::FailureKind::TYPE) {{
        supernote_throw_type_error(runtime, error.what());
      }}
      if (error.kind() == supernote::conversion::FailureKind::RANGE) {{
        supernote_throw_range_error(runtime, error.what());
      }}
      supernote_throw_error(runtime, "RESOURCE_EXHAUSTED", error.what());
    }} catch (const JvmImplementationFailure &error) {{
      supernote_throw_error(runtime, "IMPLEMENTATION_ERROR", error.what());
    }} catch (const std::exception &error) {{
      supernote_throw_error(runtime, "INTERNAL", error.what());
    }}
  }}

  void set(
      facebook::jsi::Runtime &runtime,
      const facebook::jsi::PropNameID &name,
      const facebook::jsi::Value &value) override {{
    try {{
      const auto property = name.utf8(runtime);
{set_rows}
    }} catch (const facebook::jsi::JSError &) {{
      throw;
    }} catch (const supernote::conversion::Failure &error) {{
      if (error.kind() == supernote::conversion::FailureKind::TYPE) {{
        supernote_throw_type_error(runtime, error.what());
      }}
      if (error.kind() == supernote::conversion::FailureKind::RANGE) {{
        supernote_throw_range_error(runtime, error.what());
      }}
      supernote_throw_error(runtime, "RESOURCE_EXHAUSTED", error.what());
    }} catch (const JvmImplementationFailure &error) {{
      supernote_throw_error(runtime, "IMPLEMENTATION_ERROR", error.what());
    }} catch (const std::exception &error) {{
      supernote_throw_error(runtime, "INTERNAL", error.what());
    }}
  }}

  std::vector<facebook::jsi::PropNameID> getPropertyNames(
      facebook::jsi::Runtime &runtime) override {{
    std::vector<facebook::jsi::PropNameID> names;
    names.reserve({len(property_names)});
{names}
    return names;
  }}

 private:
  ManagedJvmRef owner_;
  std::shared_ptr<supernote::runtime::FeatureSession> feature_;
  std::shared_ptr<JvmObjectRegistry> registry_;
}};'''


def _wrap_declarations(plan: JvmRoutePlan) -> str:
    return "\n".join(
        f"facebook::jsi::Object supernote_v3_wrap_jvm_object_{index}(\n"
        "    facebook::jsi::Runtime &runtime, JNIEnv *env,\n"
        "    const std::shared_ptr<JvmObjectRegistry> &registry,\n"
        "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,\n"
        "    const ManagedJvmRef &value);"
        for index, _ in enumerate(plan.objects)
    )


def _wrap_definitions(plan: JvmRoutePlan) -> str:
    rows = []
    for index, item in enumerate(plan.objects):
        rows.append(f'''facebook::jsi::Object supernote_v3_wrap_jvm_object_{index}(
    facebook::jsi::Runtime &runtime, JNIEnv *env,
    const std::shared_ptr<JvmObjectRegistry> &registry,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    const ManagedJvmRef &value) {{
  return registry->wrap(
      runtime, env, {json.dumps(item.named_type.type_id)}, value.get(),
      supernote_v3_jvm_identity_hash(env, feature, value.get()),
      value.global_ref(),
      [feature, registry](ManagedJvmRef managed) {{
        return std::make_shared<GeneratedV3JvmObject{index}HostObject>(
            std::move(managed), feature, registry);
      }});
}}''')
    return "\n\n".join(rows)


def _identity_helper(feature_id: str) -> str:
    route = _helper_route(
        feature_id, "identityHash", "(Ljava/lang/Object;)I"
    )
    return f'''std::shared_ptr<JvmObjectRegistry> supernote_v3_jvm_object_registry(
    facebook::jsi::Runtime &runtime) {{
  auto feature_registry = runtime.global().getPropertyAsObject(
      runtime, kFeatureRegistryGlobal);
  auto exports = feature_registry.getPropertyAsObject(runtime, kFeatureId);
  auto owner_object = exports.getPropertyAsObject(
      runtime, kJvmObjectRegistryProperty);
  if (!owner_object.isHostObject<JvmObjectRegistryOwner>(runtime)) {{
    throw std::runtime_error("JVM object registry is unavailable");
  }}
  return owner_object
      .getHostObject<JvmObjectRegistryOwner>(runtime)->registry();
}}

std::shared_ptr<JvmRoute> supernote_v3_jvm_route(
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    const char *key,
    const char *adapter_class,
    const char *descriptor,
    const char *method) {{
  auto route = feature->service<LazyJvmRoute>(key, [=] {{
    return std::make_shared<LazyJvmRoute>(adapter_class, descriptor, method);
  }});
  return route->get(feature);
}}

jint supernote_v3_jvm_identity_hash(
    JNIEnv *env,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    jobject value) {{
  auto route = {route};
  jvalue arguments[1]{{}};
  arguments[0].l = value;
  auto result = env->CallStaticIntMethodA(
      static_cast<jclass>(route->adapter_class.get()), route->method, arguments);
  require_no_implementation_exception(env);
  return result;
}}'''


def _jvm_type_guard_host_function(
    semantic: SemanticType,
    *,
    diagnostic: str,
    name: str,
    check: bool,
    indent: str,
) -> str:
    lines = []
    if check:
        lines.extend([
            f"{indent}      if (argument_count != 1) {{",
            f"{indent}        auto error = supernote_make_builtin_error(",
            f"{indent}            runtime, \"TypeError\", {json.dumps(diagnostic + ': expected one value')},",
            f"{indent}            \"ARITY_MISMATCH\", {json.dumps(diagnostic)}, \"1 argument\",",
            f"{indent}            std::to_string(argument_count) + \" arguments\");",
            f"{indent}        return supernote_validation_failure(runtime, std::move(error));",
            f"{indent}      }}",
        ])
    else:
        lines.append(f"{indent}      if (argument_count != 1) return facebook::jsi::Value(false);")
    lines.extend([
        f"{indent}      try {{",
        f"{indent}        supernote::conversion::Budget input_budget;",
        f"{indent}        {_validate_name(semantic)}(",
        f"{indent}            runtime, arguments[0], input_budget, {json.dumps(diagnostic)}, 1);",
        f"{indent}        return " + (
            "supernote_validation_success(runtime);"
            if check
            else "facebook::jsi::Value(true);"
        ),
        f"{indent}      }} catch (const facebook::jsi::JSError &error) {{",
        (
            f"{indent}        return supernote_validation_failure(\n"
            f"{indent}            runtime, facebook::jsi::Value(runtime, error.value()));"
            if check
            else f"{indent}        return facebook::jsi::Value(false);"
        ),
        f"{indent}      }} catch (const supernote::conversion::Failure &failure) {{",
        f"{indent}        if (failure.kind() == supernote::conversion::FailureKind::ALLOCATION) {{",
        f"{indent}          supernote_throw_error(runtime, \"RESOURCE_EXHAUSTED\", failure.what());",
        f"{indent}        }}",
    ])
    if check:
        lines.extend([
            f"{indent}        const bool range =",
            f"{indent}            failure.kind() == supernote::conversion::FailureKind::RANGE;",
            f"{indent}        auto error = supernote_make_builtin_error(",
            f"{indent}            runtime, range ? \"RangeError\" : \"TypeError\", failure.what(),",
            f"{indent}            range ? \"LIMIT_EXCEEDED\" : \"TYPE_MISMATCH\",",
            f"{indent}            failure.path(), \"valid declared value\", \"rejected\");",
            f"{indent}        return supernote_validation_failure(runtime, std::move(error));",
        ])
    else:
        lines.append(f"{indent}        return facebook::jsi::Value(false);")
    lines.extend([
        f"{indent}      }} catch (const std::exception &error) {{",
        f"{indent}        supernote_throw_error(runtime, \"INTERNAL\", error.what());",
        f"{indent}      }}",
    ])
    return (
        "facebook::jsi::Function::createFromHostFunction(\n"
        f"{indent}    runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(name)}), 1,\n"
        f"{indent}    [](facebook::jsi::Runtime &runtime, const facebook::jsi::Value &,\n"
        f"{indent}       const facebook::jsi::Value *arguments, std::size_t argument_count) -> facebook::jsi::Value {{\n"
        + "\n".join(lines)
        + f"\n{indent}    }})"
    )


def _jvm_copied_type_registration(
    semantic: SemanticType,
    *,
    public_name: str,
    module_name: str,
) -> str:
    diagnostic = f"{module_name}.{public_name}"
    is_type = _jvm_type_guard_host_function(
        semantic, diagnostic=diagnostic, name="is", check=False, indent="    "
    )
    check_type = _jvm_type_guard_host_function(
        semantic, diagnostic=diagnostic, name="check", check=True, indent="    "
    )
    return f'''  {{
    auto existing_type = exports.getProperty(runtime, {json.dumps(public_name)});
    facebook::jsi::Object object_type = existing_type.isObject()
        ? existing_type.getObject(runtime)
        : facebook::jsi::Object(runtime);
    auto is_type = {is_type};
    object_type.setProperty(runtime, "is", std::move(is_type));
    auto check_type = {check_type};
    object_type.setProperty(runtime, "check", std::move(check_type));
    exports.setProperty(runtime, {json.dumps(public_name)}, std::move(object_type));
  }}'''


def _jvm_object_info_registration(plan: JvmRoutePlan) -> str:
    branches = []
    for item in plan.objects:
        branches.extend([
            f"      if (type_id == {json.dumps(item.named_type.type_id)}) {{",
            "        facebook::jsi::Object result(runtime);",
            f"        result.setProperty(runtime, \"type\", {json.dumps(item.named_type.public_name)});",
            "        result.setProperty(runtime, \"originFamily\", \"jvm\");",
            "        return facebook::jsi::Value(std::move(result));",
            "      }",
        ])
    return f'''  {{
    auto inspect = facebook::jsi::Function::createFromHostFunction(
        runtime, facebook::jsi::PropNameID::forAscii(
            runtime, "__supernoteJvmObjectInfo"), 1,
        [](facebook::jsi::Runtime &runtime, const facebook::jsi::Value &,
           const facebook::jsi::Value *arguments,
           std::size_t argument_count) -> facebook::jsi::Value {{
      if (argument_count != 1) return facebook::jsi::Value::undefined();
      auto type_id = jvm_object_type_id(runtime, arguments[0]);
      if (type_id.empty()) return facebook::jsi::Value::undefined();
{chr(10).join(branches)}
      return facebook::jsi::Value::undefined();
    }});
    exports.setProperty(
        runtime, "__supernoteJvmObjectInfo", std::move(inspect));
  }}'''


def _registration(
    plan: JvmRoutePlan,
    item: JvmObjectRoute,
    index: int,
    module_name: str,
    feature_id: str,
) -> str:
    functions = []
    if item.constructor is not None:
        functions.append(("create", item.constructor, False, True))
    functions.extend(
        (route.public_name, route, False, False)
        for route in item.methods
        if route.javascript_public and route.static
    )
    semantic = SemanticType.object_ref(item.named_type.type_id)
    is_type = _jvm_type_guard_host_function(
        semantic,
        diagnostic=f"{module_name}.{item.named_type.public_name}",
        name="is",
        check=False,
        indent="    ",
    )
    check_type = _jvm_type_guard_host_function(
        semantic,
        diagnostic=f"{module_name}.{item.named_type.public_name}",
        name="check",
        check=True,
        indent="    ",
    )
    rows = [
        "  {",
        f"    auto existing_type = exports.getProperty(runtime, {json.dumps(item.named_type.public_name)});",
        "    facebook::jsi::Object object_type = existing_type.isObject()",
        "        ? existing_type.getObject(runtime)",
        "        : facebook::jsi::Object(runtime);",
        f"    auto is_type = {is_type};",
        "    object_type.setProperty(runtime, \"is\", std::move(is_type));",
        f"    auto check_type = {check_type};",
        "    object_type.setProperty(runtime, \"check\", std::move(check_type));",
    ]
    for public_name, route, instance, constructor in functions:
        if route.execution is ExecutionMode.ASYNC:
            function = _async_host_function(
                route,
                diagnostic=f"{module_name}.{item.named_type.public_name}.{public_name}",
                plan=plan,
                feature_id=feature_id,
                receiver=False,
                indent="      ",
            )
            function = _with_jvm_preflight(
                function,
                route,
                diagnostic=f"{module_name}.{item.named_type.public_name}.{public_name}",
                name=public_name,
                indent="      ",
            )
            rows.extend([
                "    {",
                "      auto feature = feature_session;",
                f"      auto function = {function};",
                f"      object_type.setProperty(runtime, {json.dumps(public_name)}, std::move(function));",
                "    }",
            ])
            continue
        body = _callable_body(
            route,
            diagnostic=f"{module_name}.{item.named_type.public_name}.{public_name}",
            plan=plan,
            feature_id=feature_id,
            instance=instance,
            context=constructor,
            indent="          ",
        )
        main_function = "\n".join([
            "facebook::jsi::Function::createFromHostFunction(",
            f"          runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(public_name)}),",
            f"          {len(route.parameters)},",
            "          [feature, registry](facebook::jsi::Runtime &runtime,",
            "             const facebook::jsi::Value &,",
            "             const facebook::jsi::Value *arguments,",
            "             std::size_t argument_count) -> facebook::jsi::Value {",
            body,
            "          })",
        ])
        function = _with_jvm_preflight(
            main_function,
            route,
            diagnostic=f"{module_name}.{item.named_type.public_name}.{public_name}",
            name=public_name,
            indent="      ",
        )
        rows.extend([
            "    {",
            "      auto feature = feature_session;",
            "      auto registry = object_registry;",
            f"      auto function = {function};",
            f"      object_type.setProperty(runtime, {json.dumps(public_name)}, std::move(function));",
            "    }",
        ])
    rows.extend([
        f"    exports.setProperty(runtime, {json.dumps(item.named_type.public_name)}, std::move(object_type));",
        "  }",
    ])
    return "\n".join(rows)


def _contains_composite(semantic: SemanticType) -> bool:
    return semantic.kind not in {SemanticTypeKind.VOID, SemanticTypeKind.SCALAR}


def _function_registration(
    route: JvmCallableRoute,
    plan: JvmRoutePlan,
    module_name: str,
    feature_id: str,
) -> str:
    if route.execution is ExecutionMode.ASYNC:
        function = _async_host_function(
            route,
            diagnostic=f"{module_name}.{route.public_name}",
            plan=plan,
            feature_id=feature_id,
            receiver=False,
            indent="    ",
        )
        function = _with_jvm_preflight(
            function,
            route,
            diagnostic=f"{module_name}.{route.public_name}",
            name=route.public_name,
            indent="    ",
        )
        return f'''  {{
    auto feature = feature_session;
    auto function = {function};
    exports.setProperty(runtime, {json.dumps(route.public_name)}, std::move(function));
  }}'''
    body = _callable_body(
        route,
        diagnostic=f"{module_name}.{route.public_name}",
        plan=plan,
        feature_id=feature_id,
        instance=False,
        context=False,
        indent="        ",
    )
    main_function = f'''facebook::jsi::Function::createFromHostFunction(
        runtime, facebook::jsi::PropNameID::forAscii(runtime, {json.dumps(route.public_name)}),
        {len(route.parameters)},
        [feature, registry](facebook::jsi::Runtime &runtime,
           const facebook::jsi::Value &,
           const facebook::jsi::Value *arguments,
           std::size_t argument_count) -> facebook::jsi::Value {{
{body}
        }})'''
    function = _with_jvm_preflight(
        main_function,
        route,
        diagnostic=f"{module_name}.{route.public_name}",
        name=route.public_name,
        indent="    ",
    )
    return f'''  {{
    auto feature = feature_session;
    auto registry = object_registry;
    auto function = {function};
    exports.setProperty(runtime, {json.dumps(route.public_name)}, std::move(function));
  }}'''


def render_jvm_object_bindings(
    plan: JvmRoutePlan,
    *,
    feature_id: str,
    module_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    object_functions = tuple(
        route for route in plan.functions
        if _contains_composite(route.result)
        or any(_contains_composite(item) for item in route.parameters)
    )
    if not plan.objects and not object_functions:
        return (), ()
    roots = []
    for item in plan.objects:
        if item.constructor is not None:
            roots.extend(item.constructor.parameters)
            roots.append(item.constructor.result)
        for method in item.methods:
            roots.extend(method.parameters)
            roots.append(method.result)
        roots.extend(field.semantic_type for field in item.fields)
    for route in object_functions:
        roots.extend(route.parameters)
        roots.append(route.result)
    types = _collect_types(roots, plan)
    wrappers = (
        _identity_helper(feature_id),
        _wrap_declarations(plan),
        "\n\n".join(_prototype(item) for item in types),
        "\n\n".join(
            _from_definition(item, plan, feature_id) for item in types
        ),
        "\n\n".join(
            _validate_definition(item, plan) for item in types
        ),
        "\n\n".join(
            _to_definition(item, plan, feature_id) for item in types
        ),
        *(
            _wrapper(plan, item, index, module_name, feature_id)
            for index, item in enumerate(plan.objects)
        ),
        _wrap_definitions(plan),
    )
    registrations = tuple(
        _function_registration(route, plan, module_name, feature_id)
        for route in object_functions
    ) + tuple(
        filter(None, (
                _registration(
                    plan, item, index, module_name, feature_id
                )
                for index, item in enumerate(plan.objects)
            ))
    )
    converted_named_types = {
        (item.kind, item.type_id)
        for item in types
        if item.type_id is not None
    }
    registrations += tuple(
        _jvm_copied_type_registration(
            SemanticType.value_ref(item.named_type.type_id),
            public_name=item.named_type.public_name,
            module_name=module_name,
        )
        for item in plan.values
        if (SemanticTypeKind.VALUE_REF, item.named_type.type_id)
        in converted_named_types
    )
    registrations += tuple(
        _jvm_copied_type_registration(
            SemanticType.enum_ref(item.named_type.type_id),
            public_name=item.named_type.public_name,
            module_name=module_name,
        )
        for item in plan.enums
        if (SemanticTypeKind.ENUM_REF, item.named_type.type_id)
        in converted_named_types
    )
    if plan.objects:
        registrations += (_jvm_object_info_registration(plan),)
    return tuple(filter(None, wrappers)), registrations


__all__ = ["render_jvm_object_bindings"]
