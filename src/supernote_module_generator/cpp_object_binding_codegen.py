"""Emit synchronous JSI bindings for V4 C++ object routes."""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .cpp_routes import (
    CppCallableKind,
    CppCallableRoute,
    CppObjectPassing,
    CppObjectRoute,
    CppParameterRoute,
    CppRouteError,
    CppRoutePlan,
)
from .semantic import ExecutionMode
from .semantic_types import ScalarKind, SemanticType, SemanticTypeKind


def _type_suffix(semantic: SemanticType) -> str:
    payload = json.dumps(
        semantic.manifest(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _from_js_name(semantic: SemanticType) -> str:
    return f"supernote_v4_from_js_{_type_suffix(semantic)}"


def _to_js_name(semantic: SemanticType) -> str:
    return f"supernote_v4_to_js_{_type_suffix(semantic)}"


def _retain_native_name(semantic: SemanticType) -> str:
    return f"supernote_v4_retain_native_{_type_suffix(semantic)}"


def _cpp_type(semantic: SemanticType, plan: CppRoutePlan) -> str:
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
    if semantic.kind in {
        SemanticTypeKind.ENUM_REF,
        SemanticTypeKind.VALUE_REF,
        SemanticTypeKind.OBJECT_REF,
    }:
        assert semantic.type_id is not None
        native = plan.named_types_by_id[semantic.type_id].cpp_type
        if semantic.kind is SemanticTypeKind.OBJECT_REF:
            return f"std::shared_ptr<{native}>"
        return native
    assert semantic.element is not None
    child = _cpp_type(semantic.element, plan)
    wrapper = "std::vector" if semantic.kind is SemanticTypeKind.ARRAY else "std::optional"
    return f"{wrapper}<{child}>"


def _collect_types(
    roots: Iterable[SemanticType], plan: CppRoutePlan
) -> tuple[SemanticType, ...]:
    found: dict[str, SemanticType] = {}

    def visit(item: SemanticType) -> None:
        if item.kind is SemanticTypeKind.VOID:
            return
        key = _type_suffix(item)
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


def _conversion_prototype(semantic: SemanticType, plan: CppRoutePlan) -> str:
    native = _cpp_type(semantic, plan)
    return f"""{native} {_from_js_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value,
    supernote::conversion::Budget &budget,
    std::vector<supernote::runtime::ManagedAnyRef> &retained,
    const std::string &path,
    std::uint64_t depth);
facebook::jsi::Value {_to_js_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const {native} &value,
    const std::shared_ptr<supernote::runtime::CppObjectRegistry> &registry,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    supernote::conversion::Budget &budget,
    const std::string &path,
    std::uint64_t depth);
void {_retain_native_name(semantic)}(
    const {native} &value,
    std::vector<supernote::runtime::ManagedAnyRef> &retained,
    const std::shared_ptr<supernote::runtime::DeferredDestruction> &cleanup);"""


def _validation_prototype(semantic: SemanticType) -> str:
    return f"""void {_validate_js_name(semantic)}(
    facebook::jsi::Runtime &runtime,
    const facebook::jsi::Value &value,
    supernote::conversion::Budget &budget,
    const std::string &path,
    std::uint64_t depth);"""


def _validate_js_name(semantic: SemanticType) -> str:
    return "supernote_validate_js_" + _type_suffix(semantic)


def _validate_js_definition(semantic: SemanticType, plan: CppRoutePlan) -> str:
    lines = [
        f"void {_validate_js_name(semantic)}(",
        "    facebook::jsi::Runtime &runtime,",
        "    const facebook::jsi::Value &value,",
        "    supernote::conversion::Budget &budget,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  budget.visit(path, depth);",
        "  if (value.isUndefined()) {",
        f"    {_input_type_error('a defined value')};",
        "  }",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        lines.extend([
            "  if (value.isNull()) return;",
            f"  {_validate_js_name(semantic.element)}(",
            "      runtime, value, budget, path, depth + 1);",
        ])
    else:
        lines.extend([
            "  if (value.isNull()) {",
            f"    {_input_type_error('a non-null value')};",
            "  }",
        ])
        if kind is SemanticTypeKind.SCALAR:
            scalar = semantic.scalar
            if scalar is ScalarKind.BOOL:
                lines.append(f"  if (!value.isBool()) {_input_type_error('boolean')};")
            elif scalar is ScalarKind.INT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_input_type_error('an int32 number')};",
                    "  const double number = value.asNumber();",
                    "  if (!std::isfinite(number) || std::trunc(number) != number ||",
                    "      number < static_cast<double>(std::numeric_limits<std::int32_t>::min()) ||",
                    "      number > static_cast<double>(std::numeric_limits<std::int32_t>::max())) {",
                    "    supernote_throw_range_error(runtime, path + \": int32 value is out of range\",",
                    "        \"OUT_OF_RANGE\", path, \"int32\", supernote_describe_value(runtime, value));",
                    "  }",
                ])
            elif scalar is ScalarKind.INT64:
                lines.extend([
                    f"  if (!value.isBigInt()) {_input_type_error('an int64 bigint')};",
                    "  if (!value.getBigInt(runtime).isInt64(runtime)) {",
                    "    supernote_throw_range_error(runtime, path + \": int64 value is out of range\",",
                    "        \"OUT_OF_RANGE\", path, \"int64 bigint\", \"bigint\");",
                    "  }",
                ])
            elif scalar is ScalarKind.FLOAT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_input_type_error('a float32 number')};",
                    "  const double number = value.asNumber();",
                    "  if (std::isfinite(number) &&",
                    "      (number < static_cast<double>(std::numeric_limits<float>::lowest()) ||",
                    "       number > static_cast<double>(std::numeric_limits<float>::max()))) {",
                    "    supernote_throw_range_error(runtime, path + \": float32 value is out of range\",",
                    "        \"OUT_OF_RANGE\", path, \"float32\", \"number\");",
                    "  }",
                ])
            elif scalar is ScalarKind.FLOAT64:
                lines.append(f"  if (!value.isNumber()) {_input_type_error('a float64 number')};")
            elif scalar is ScalarKind.STRING:
                lines.extend([
                    f"  if (!value.isString()) {_input_type_error('a string')};",
                    "  auto text = value.asString(runtime).utf8(runtime);",
                    "  budget.check_string_bytes(path, text.size());",
                ])
            else:
                lines.extend([
                    f"  if (!supernote_is_uint8_array(runtime, value)) {_input_type_error('a Uint8Array')};",
                    "  auto snapshot = supernote_snapshot_uint8_array(runtime, value);",
                    "  budget.check_byte_buffer(path, snapshot.length);",
                ])
        elif kind is SemanticTypeKind.OBJECT_REF:
            assert semantic.type_id is not None
            named = plan.named_types_by_id[semantic.type_id]
            lines.extend([
                f"  auto managed = supernote::runtime::try_extract_cpp_object<{named.cpp_type}>(",
                f"      runtime, value, {json.dumps(named.type_id)});",
                "  if (!managed) {",
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
                f"  if (!value.isString()) {_input_type_error(route.named_type.public_name)};",
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
                f"  if (!value.isObject()) {_input_type_error('a dense Array')};",
                "  auto object = value.getObject(runtime);",
                f"  if (!object.isArray(runtime)) {_input_type_error('a dense Array')};",
                "  auto array = object.getArray(runtime);",
                "  const auto length = static_cast<std::uint64_t>(array.size(runtime));",
                "  budget.check_array_length(path, length);",
                "  for (std::uint64_t index = 0; index < length; ++index) {",
                "    auto item_path = supernote::conversion::index_path(path, index);",
                "    if (!supernote_array_has_own_index(runtime, array, static_cast<std::size_t>(index))) {",
                "      supernote_throw_type_error(runtime, item_path + \": expected a present array element\",",
                "          \"TYPE_MISMATCH\", item_path, \"present array element\", \"missing\");",
                "    }",
                "    auto item = array.getValueAtIndex(runtime, static_cast<std::size_t>(index));",
                f"    {_validate_js_name(semantic.element)}(",
                "        runtime, item, budget, item_path, depth + 1);",
                "  }",
            ])
        else:
            assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
            route = next(item for item in plan.values if item.named_type.type_id == semantic.type_id)
            lines.extend([
                f"  if (!value.isObject()) {_input_type_error(route.named_type.public_name)};",
                "  auto object = value.getObject(runtime);",
                f"  if (object.isArray(runtime)) {_input_type_error(route.named_type.public_name)};",
            ])
            for field in route.fields:
                lines.extend([
                    f"  auto {field.cpp_name}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                    f"  auto {field.cpp_name}_value = object.getProperty(runtime, {json.dumps(field.public_name)});",
                    f"  {_validate_js_name(field.semantic_type)}(",
                    f"      runtime, {field.cpp_name}_value, budget, {field.cpp_name}_path, depth + 1);",
                ])
    lines.append("}")
    return "\n".join(lines)


def _input_type_error(expected: str) -> str:
    return (
        "supernote_throw_type_error(runtime, path + \": expected "
        + expected.replace('"', '\\"')
        + "\", \"TYPE_MISMATCH\", path, \""
        + expected.replace('"', '\\"')
        + "\", supernote_describe_value(runtime, value))"
    )


def _from_js_definition(semantic: SemanticType, plan: CppRoutePlan) -> str:
    native = _cpp_type(semantic, plan)
    name = _from_js_name(semantic)
    lines = [
        f"{native} {name}(",
        "    facebook::jsi::Runtime &runtime,",
        "    const facebook::jsi::Value &value,",
        "    supernote::conversion::Budget &budget,",
        "    std::vector<supernote::runtime::ManagedAnyRef> &retained,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  (void)retained;",
        "  budget.visit(path, depth);",
        "  if (value.isUndefined()) {",
        f"    {_input_type_error('a defined value')};",
        "  }",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        child = _from_js_name(semantic.element)
        lines.extend(
            [
                "  if (value.isNull()) return std::nullopt;",
                f"  return {child}(runtime, value, budget, retained, path, depth + 1);",
            ]
        )
    else:
        lines.extend(
            [
                "  if (value.isNull()) {",
                f"    {_input_type_error('a non-null value')};",
                "  }",
            ]
        )
        if kind is SemanticTypeKind.SCALAR:
            scalar = semantic.scalar
            if scalar is ScalarKind.BOOL:
                lines.extend([
                    f"  if (!value.isBool()) {_input_type_error('boolean')};",
                    "  return value.getBool();",
                ])
            elif scalar is ScalarKind.INT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_input_type_error('an int32 number')};",
                    "  const double number = value.asNumber();",
                    "  if (!std::isfinite(number) || std::trunc(number) != number ||",
                    "      number < static_cast<double>(std::numeric_limits<std::int32_t>::min()) ||",
                    "      number > static_cast<double>(std::numeric_limits<std::int32_t>::max())) {",
                    "    supernote_throw_range_error(runtime, path + \": int32 value is out of range\");",
                    "  }",
                    "  return static_cast<std::int32_t>(number);",
                ])
            elif scalar is ScalarKind.INT64:
                lines.extend([
                    f"  if (!value.isBigInt()) {_input_type_error('an int64 bigint')};",
                    "  const auto bigint = value.getBigInt(runtime);",
                    "  if (!bigint.isInt64(runtime)) {",
                    "    supernote_throw_range_error(runtime, path + \": int64 value is out of range\");",
                    "  }",
                    "  return static_cast<std::int64_t>(bigint.asInt64(runtime));",
                ])
            elif scalar is ScalarKind.FLOAT32:
                lines.extend([
                    f"  if (!value.isNumber()) {_input_type_error('a float32 number')};",
                    "  const double number = value.asNumber();",
                    "  if (std::isfinite(number) &&",
                    "      (number < static_cast<double>(std::numeric_limits<float>::lowest()) ||",
                    "       number > static_cast<double>(std::numeric_limits<float>::max()))) {",
                    "    supernote_throw_range_error(runtime, path + \": float32 value is out of range\");",
                    "  }",
                    "  return static_cast<float>(number);",
                ])
            elif scalar is ScalarKind.FLOAT64:
                lines.extend([
                    f"  if (!value.isNumber()) {_input_type_error('a float64 number')};",
                    "  return value.asNumber();",
                ])
            elif scalar is ScalarKind.STRING:
                lines.extend([
                    f"  if (!value.isString()) {_input_type_error('a string')};",
                    "  auto result = value.asString(runtime).utf8(runtime);",
                    "  budget.check_string_bytes(path, result.size());",
                    "  budget.reserve(path, result.size());",
                    "  return result;",
                ])
            else:
                assert scalar is ScalarKind.BYTES
                lines.extend([
                    f"  if (!supernote_is_uint8_array(runtime, value)) {_input_type_error('a Uint8Array')};",
                    "  auto snapshot = supernote_snapshot_uint8_array(runtime, value);",
                    "  budget.check_byte_buffer(path, snapshot.length);",
                    "  budget.reserve(path, snapshot.length);",
                    "  return supernote_copy_uint8_array(runtime, snapshot);",
                ])
        elif kind is SemanticTypeKind.OBJECT_REF:
            assert semantic.type_id is not None
            named = plan.named_types_by_id[semantic.type_id]
            lines.extend([
                f"  auto managed = supernote::runtime::try_extract_cpp_object<{named.cpp_type}>(",
                f"      runtime, value, {json.dumps(named.type_id)});",
                "  if (!managed) {",
                f"    supernote_throw_type_error(runtime, path + \": expected {named.public_name}\",",
                f"        \"NOMINAL_MISMATCH\", path, {json.dumps(named.public_name)},",
                "        supernote_describe_value(runtime, value));",
                "  }",
                "  budget.reserve(path, sizeof(void *));",
                "  retained.emplace_back(",
                "      managed.shared_ref(), supernote::runtime::process_services().cleanup());",
                "  return managed.shared_ref();",
            ])
        elif kind is SemanticTypeKind.ENUM_REF:
            assert semantic.type_id is not None
            route = next(item for item in plan.enums if item.named_type.type_id == semantic.type_id)
            lines.extend([
                f"  if (!value.isString()) {_input_type_error(route.named_type.public_name)};",
                "  auto text = value.asString(runtime).utf8(runtime);",
                "  budget.check_string_bytes(path, text.size());",
                "  budget.reserve(path, text.size());",
            ])
            for constant in route.constants:
                lines.append(
                    f"  if (text == {json.dumps(constant)}) return {route.named_type.cpp_type}::{constant};"
                )
            lines.append(
                "  supernote_throw_type_error(runtime, path + \": expected "
                + route.named_type.public_name
                + "\", \"INVALID_ENUM\", path, "
                + json.dumps(route.named_type.public_name)
                + ", supernote_describe_value(runtime, value));"
            )
        elif kind is SemanticTypeKind.ARRAY:
            assert semantic.element is not None
            child = _from_js_name(semantic.element)
            lines.extend([
                f"  if (!value.isObject()) {_input_type_error('a dense Array')};",
                "  auto object = value.getObject(runtime);",
                f"  if (!object.isArray(runtime)) {_input_type_error('a dense Array')};",
                "  auto array = object.getArray(runtime);",
                "  const auto length = static_cast<std::uint64_t>(array.size(runtime));",
                "  budget.check_array_length(path, length);",
                "  budget.reserve(path, 24ULL + length * sizeof(void *));",
                f"  {native} result;",
                "  result.reserve(static_cast<std::size_t>(length));",
                "  for (std::uint64_t index = 0; index < length; ++index) {",
                "    const auto item_path = supernote::conversion::index_path(path, index);",
                "    if (!supernote_array_has_own_index(runtime, array, static_cast<std::size_t>(index))) {",
                "      supernote_throw_type_error(runtime, item_path + \": expected a present array element\",",
                "          \"TYPE_MISMATCH\", item_path, \"present array element\", \"missing\");",
                "    }",
                "    auto item = array.getValueAtIndex(runtime, static_cast<std::size_t>(index));",
                f"    result.push_back({child}(runtime, item, budget, retained, item_path, depth + 1));",
                "  }",
                "  return result;",
            ])
        else:
            assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
            route = next(item for item in plan.values if item.named_type.type_id == semantic.type_id)
            lines.extend([
                f"  if (!value.isObject()) {_input_type_error(route.named_type.public_name)};",
                "  auto object = value.getObject(runtime);",
                f"  if (object.isArray(runtime)) {_input_type_error(route.named_type.public_name)};",
                f"  budget.reserve(path, 32ULL + {len(route.fields)}ULL * 16ULL);",
            ])
            locals_: list[str] = []
            for index, field in enumerate(route.fields):
                child = _from_js_name(field.semantic_type)
                local = f"field_{index}"
                locals_.append(f"std::move({local})")
                lines.extend([
                    f"  auto {local}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                    f"  auto {local}_value = object.getProperty(runtime, {json.dumps(field.public_name)});",
                    f"  auto {local} = {child}(runtime, {local}_value, budget, retained, {local}_path, depth + 1);",
                ])
            lines.append(
                "  return "
                + route.named_type.cpp_type
                + "{"
                + ", ".join(locals_)
                + "};"
            )
    lines.append("}")
    return "\n".join(lines)


def _to_js_definition(semantic: SemanticType, plan: CppRoutePlan) -> str:
    native = _cpp_type(semantic, plan)
    name = _to_js_name(semantic)
    lines = [
        f"facebook::jsi::Value {name}(",
        "    facebook::jsi::Runtime &runtime,",
        f"    const {native} &value,",
        "    const std::shared_ptr<supernote::runtime::CppObjectRegistry> &registry,",
        "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
        "    supernote::conversion::Budget &budget,",
        "    const std::string &path,",
        "    std::uint64_t depth) {",
        "  budget.visit(path, depth);",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.NULLABLE:
        assert semantic.element is not None
        child = _to_js_name(semantic.element)
        lines.extend([
            "  if (!value.has_value()) return facebook::jsi::Value::null();",
            f"  return {child}(runtime, *value, registry, feature, budget, path, depth + 1);",
        ])
    elif kind is SemanticTypeKind.SCALAR:
        scalar = semantic.scalar
        if scalar is ScalarKind.BOOL:
            lines.append("  return facebook::jsi::Value(value);")
        elif scalar in {ScalarKind.INT32, ScalarKind.FLOAT32, ScalarKind.FLOAT64}:
            lines.append("  return facebook::jsi::Value(static_cast<double>(value));")
        elif scalar is ScalarKind.INT64:
            lines.extend([
                "  return facebook::jsi::Value(facebook::jsi::BigInt::fromInt64(",
                "      runtime, static_cast<std::int64_t>(value)));",
            ])
        elif scalar is ScalarKind.STRING:
            lines.extend([
                "  budget.check_string_bytes(path, value.size());",
                "  budget.reserve(path, value.size());",
                "  return facebook::jsi::Value(facebook::jsi::String::createFromUtf8(runtime, value));",
            ])
        else:
            assert scalar is ScalarKind.BYTES
            lines.extend([
                "  budget.check_byte_buffer(path, value.size());",
                "  budget.reserve(path, value.size());",
                "  return supernote_make_uint8_array(runtime, value);",
            ])
    elif kind is SemanticTypeKind.OBJECT_REF:
        assert semantic.type_id is not None
        wrapper = _wrap_function(_object_index(plan, semantic.type_id))
        lines.extend([
            "  budget.reserve(path, sizeof(void *));",
            f"  return facebook::jsi::Value({wrapper}(runtime, registry, feature, value));",
        ])
    elif kind is SemanticTypeKind.ENUM_REF:
        assert semantic.type_id is not None
        route = next(item for item in plan.enums if item.named_type.type_id == semantic.type_id)
        for constant in route.constants:
            lines.extend([
                f"  if (value == {route.named_type.cpp_type}::{constant}) {{",
                f"    constexpr char text[] = {json.dumps(constant)};",
                "    budget.check_string_bytes(path, sizeof(text) - 1);",
                "    budget.reserve(path, sizeof(text) - 1);",
                "    return facebook::jsi::Value(facebook::jsi::String::createFromAscii(runtime, text));",
                "  }",
            ])
        lines.append(
            f"  throw std::invalid_argument({json.dumps('native enum ' + route.named_type.public_name + ' has an invalid value')});"
        )
    elif kind is SemanticTypeKind.ARRAY:
        assert semantic.element is not None
        child = _to_js_name(semantic.element)
        lines.extend([
            "  const auto length = static_cast<std::uint64_t>(value.size());",
            "  budget.check_array_length(path, length);",
            "  budget.reserve(path, 24ULL + length * sizeof(void *));",
            "  facebook::jsi::Array result(runtime, static_cast<std::size_t>(length));",
            "  for (std::uint64_t index = 0; index < length; ++index) {",
            "    const auto item_path = supernote::conversion::index_path(path, index);",
            f"    auto item = {child}(runtime, value[static_cast<std::size_t>(index)], registry, feature, budget, item_path, depth + 1);",
            "    result.setValueAtIndex(runtime, static_cast<std::size_t>(index), std::move(item));",
            "  }",
            "  return facebook::jsi::Value(std::move(result));",
        ])
    else:
        assert kind is SemanticTypeKind.VALUE_REF and semantic.type_id is not None
        route = next(item for item in plan.values if item.named_type.type_id == semantic.type_id)
        lines.extend([
            f"  budget.reserve(path, 32ULL + {len(route.fields)}ULL * 16ULL);",
            "  facebook::jsi::Object result(runtime);",
        ])
        for field in route.fields:
            child = _to_js_name(field.semantic_type)
            lines.extend([
                f"  auto {field.cpp_name}_path = supernote::conversion::field_path(path, {json.dumps(field.public_name)});",
                f"  auto {field.cpp_name}_value = {child}(runtime, value.{field.cpp_name}, registry, feature, budget, {field.cpp_name}_path, depth + 1);",
                f"  result.setProperty(runtime, {json.dumps(field.public_name)}, std::move({field.cpp_name}_value));",
            ])
        lines.append("  return facebook::jsi::Value(std::move(result));")
    lines.append("}")
    return "\n".join(lines)


def _retain_native_definition(semantic: SemanticType, plan: CppRoutePlan) -> str:
    native = _cpp_type(semantic, plan)
    lines = [
        f"void {_retain_native_name(semantic)}(",
        f"    const {native} &value,",
        "    std::vector<supernote::runtime::ManagedAnyRef> &retained,",
        "    const std::shared_ptr<supernote::runtime::DeferredDestruction> &cleanup) {",
    ]
    kind = semantic.kind
    if kind is SemanticTypeKind.OBJECT_REF:
        lines.append("  if (value) retained.emplace_back(value, cleanup);")
    elif kind in {SemanticTypeKind.ARRAY, SemanticTypeKind.NULLABLE}:
        assert semantic.element is not None
        child = _retain_native_name(semantic.element)
        if kind is SemanticTypeKind.ARRAY:
            lines.extend([
                "  for (const auto &item : value) {",
                f"    {child}(item, retained, cleanup);",
                "  }",
            ])
        else:
            lines.extend([
                f"  if (value) {child}(*value, retained, cleanup);",
            ])
    elif kind is SemanticTypeKind.VALUE_REF:
        assert semantic.type_id is not None
        route = next(item for item in plan.values if item.named_type.type_id == semantic.type_id)
        for field in route.fields:
            lines.append(
                f"  {_retain_native_name(field.semantic_type)}(value.{field.cpp_name}, retained, cleanup);"
            )
    else:
        lines.extend(["  (void)value;", "  (void)retained;", "  (void)cleanup;"])
    lines.append("}")
    return "\n".join(lines)


def _conversion_helpers(types: tuple[SemanticType, ...], plan: CppRoutePlan) -> str:
    if not types:
        return ""
    prototypes = "\n\n".join(
        _conversion_prototype(item, plan) + "\n" + _validation_prototype(item)
        for item in types
    )
    definitions = "\n\n".join(
        value
        for item in types
        for value in (
            _from_js_definition(item, plan),
            _validate_js_definition(item, plan),
            _to_js_definition(item, plan),
            _retain_native_definition(item, plan),
        )
    )
    failure = r'''constexpr char kCppObjectRegistryProperty[] =
    "__supernoteV4CppObjectRegistry_5f271b119c3a";

std::shared_ptr<supernote::runtime::CppObjectRegistry>
supernote_v4_object_registry(facebook::jsi::Runtime &runtime) {
  auto registry = runtime.global().getPropertyAsObject(
      runtime, kFeatureRegistryGlobal);
  auto exports = registry.getPropertyAsObject(runtime, kFeatureId);
  auto owner = exports.getPropertyAsObject(
      runtime, kCppObjectRegistryProperty);
  return owner.getHostObject<supernote::runtime::CppObjectRegistryOwner>(
      runtime)->registry();
}

[[noreturn]] void supernote_v4_throw_conversion_failure(
    facebook::jsi::Runtime &runtime,
    const supernote::conversion::Failure &failure) {
  if (failure.kind() == supernote::conversion::FailureKind::TYPE) {
    supernote_throw_type_error(runtime, failure.what());
  }
  if (failure.kind() == supernote::conversion::FailureKind::RANGE) {
    supernote_throw_range_error(runtime, failure.what());
  }
  supernote_throw_error(runtime, "INTERNAL", failure.what());
}'''
    return f"{prototypes}\n\n{failure}\n\n{definitions}"


def _identifier(index: int) -> str:
    return f"GeneratedV4Object{index}HostObject"


def _wrap_function(index: int) -> str:
    return f"supernote_wrap_v4_object_{index}"


def _object_index(plan: CppRoutePlan, type_id: str) -> int:
    for index, item in enumerate(plan.objects):
        if item.named_type.type_id == type_id:
            return index
    raise CppRouteError(f"C++ object type {type_id!r} has no generated wrapper")


def _argument_expression(
    parameter: CppParameterRoute,
    number: int,
) -> str:
    local = f"supernote_input_{number}"
    if parameter.object_passing in {
        CppObjectPassing.BORROWED_MUTABLE,
        CppObjectPassing.BORROWED_CONST,
    }:
        return f"*{local}"
    if parameter.object_passing in {
        CppObjectPassing.SHARED_VALUE,
        CppObjectPassing.SHARED_CONST_REF,
    }:
        return local
    return local


def _prepare_parameter(
    parameter: CppParameterRoute,
    number: int,
    *,
    diagnostic: str,
    plan: CppRoutePlan,
    value_expression: str,
    indent: str,
) -> list[str]:
    local = f"supernote_input_{number}"
    semantic = parameter.semantic_type
    path = f"{diagnostic}.argument[{number}]({parameter.name})"
    return [
        f"{indent}auto {local} = {_from_js_name(semantic)}(",
        f"{indent}    runtime, {value_expression}, conversion_budget, retained_objects,",
        f"{indent}    {json.dumps(path)}, 1);",
    ]


def _result_lines(
    semantic: SemanticType,
    call: str,
    *,
    plan: CppRoutePlan,
    registry: str,
    feature: str,
    diagnostic: str,
    indent: str,
) -> list[str]:
    if semantic.kind is SemanticTypeKind.VOID:
        return [f"{indent}{call};", f"{indent}return Value::undefined();"]
    return [
        f"{indent}auto native_result = {call};",
        f"{indent}supernote::conversion::Budget result_budget;",
        f"{indent}return {_to_js_name(semantic)}(",
        f"{indent}    runtime, native_result, {registry}, {feature}, result_budget,",
        f"{indent}    {json.dumps(diagnostic + '.result')}, 1);",
    ]


def _callable_body(
    route: CppCallableRoute,
    call: str,
    *,
    diagnostic: str,
    plan: CppRoutePlan,
    registry: str,
    feature: str,
    indent: str,
) -> str:
    lines = [
        f"{indent}if (argument_count != {len(route.parameters)}) {{",
        f"{indent}  supernote_throw_type_error(",
        f"{indent}      runtime, {json.dumps(diagnostic + ': wrong argument count')},",
        f"{indent}      \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
        f"{indent}      {json.dumps(str(len(route.parameters)) + ' arguments')},",
        f"{indent}      std::to_string(argument_count) + \" arguments\");",
        f"{indent}}}",
        f"{indent}try {{",
        f"{indent}  [[maybe_unused]] supernote::conversion::Budget conversion_budget;",
        f"{indent}  [[maybe_unused]] std::vector<supernote::runtime::ManagedAnyRef> retained_objects;",
    ]
    for number, parameter in enumerate(route.parameters):
        lines.extend(
            _prepare_parameter(
                parameter,
                number,
                diagnostic=diagnostic,
                plan=plan,
                value_expression=f"arguments[{number}]",
                indent=indent + "  ",
            )
        )
    lines.extend(
        [
            f"{indent}  auto active_feature = {feature};",
            f"{indent}  if (!active_feature ||",
            f"{indent}      active_feature->state() != supernote::runtime::FeatureState::ACTIVE) {{",
            f"{indent}    supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");",
            f"{indent}  }}",
            f"{indent}  supernote::runtime::FeatureCallScope feature_call_scope(active_feature);",
        ]
    )
    lines.extend(
        _result_lines(
            route.result,
            call,
            plan=plan,
            registry=registry,
            feature="active_feature",
            diagnostic=diagnostic,
            indent=indent + "  ",
        )
    )
    lines.extend(
        [
            f"{indent}}} catch (const facebook::jsi::JSError &) {{",
            f"{indent}  throw;",
            f"{indent}}} catch (const supernote::conversion::Failure &failure) {{",
            f"{indent}  supernote_v4_throw_conversion_failure(runtime, failure);",
            f"{indent}}} catch (const std::exception &error) {{",
            f"{indent}  supernote_throw_error(",
            f"{indent}      runtime, \"IMPLEMENTATION_ERROR\",",
            f"{indent}      std::string({json.dumps(diagnostic + ': ')}) + error.what());",
            f"{indent}}} catch (...) {{",
            f"{indent}  supernote_throw_error(",
            f"{indent}      runtime, \"IMPLEMENTATION_ERROR\",",
            f"{indent}      {json.dumps(diagnostic + ': unknown C++ exception')});",
            f"{indent}}}",
        ]
    )
    return "\n".join(lines)


def _call_expression(route: CppCallableRoute, receiver: str | None = None) -> str:
    arguments = ", ".join(
        _argument_expression(parameter, number)
        for number, parameter in enumerate(route.parameters)
    )
    if route.kind is CppCallableKind.FUNCTION:
        return f"{route.cpp_name}({arguments})"
    if route.kind is CppCallableKind.STATIC_METHOD:
        return f"{route.owner_cpp_type}::{route.cpp_name}({arguments})"
    if route.kind is CppCallableKind.INSTANCE_METHOD:
        assert receiver is not None
        return f"{receiver}->{route.cpp_name}({arguments})"
    if route.kind is CppCallableKind.CONSTRUCTOR:
        return f"std::make_shared<{route.owner_cpp_type}>({arguments})"
    raise AssertionError(route.kind)


def _async_host_function(
    route: CppCallableRoute,
    *,
    diagnostic: str,
    plan: CppRoutePlan,
    name: str,
    receiver: str | None,
    capture: str,
    feature_expression: str,
    indent: str,
) -> str:
    if route.kind is CppCallableKind.CONSTRUCTOR:
        raise CppRouteError(f"{diagnostic}: constructors cannot be async")
    argument_parameter = "const Value *arguments" if route.parameters else "const Value *"
    lines = [
        f"{indent}    if (argument_count != {len(route.parameters)}) {{",
        f"{indent}      supernote_throw_type_error(",
        f"{indent}          runtime, {json.dumps(diagnostic + ': wrong argument count')},",
        f"{indent}          \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
        f"{indent}          {json.dumps(str(len(route.parameters)) + ' arguments')},",
        f"{indent}          std::to_string(argument_count) + \" arguments\");",
        f"{indent}    }}",
        f"{indent}    try {{",
        f"{indent}      supernote::conversion::Budget conversion_budget;",
        f"{indent}      std::vector<supernote::runtime::ManagedAnyRef> retained_objects;",
    ]
    for number, parameter in enumerate(route.parameters):
        lines.extend(
            _prepare_parameter(
                parameter,
                number,
                diagnostic=diagnostic,
                plan=plan,
                value_expression=f"arguments[{number}]",
                indent=indent + "      ",
            )
        )
    lines.extend([
        f"{indent}      auto active_feature = {feature_expression};",
        f"{indent}      if (!active_feature ||",
        f"{indent}          active_feature->state() != supernote::runtime::FeatureState::ACTIVE) {{",
        f"{indent}        supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");",
        f"{indent}      }}",
    ])
    result_type = _cpp_type(route.result, plan)
    if route.result.kind is SemanticTypeKind.VOID:
        state_fields = "bool success{false};\n            std::string error;"
    else:
        state_fields = (
            "bool success{false};\n"
            f"            std::optional<{result_type}> value;\n"
            "            std::vector<supernote::runtime::ManagedAnyRef> retained_result;\n"
            "            std::string error;"
        )
    lines.extend([
        f"{indent}      struct AsyncState {{",
        f"{indent}        {state_fields}",
        f"{indent}      }};",
        f"{indent}      auto state = std::make_shared<AsyncState>();",
    ])
    retained_types = [
        "std::vector<supernote::runtime::ManagedAnyRef>",
        *(
            [
                "supernote::runtime::ManagedRef<"
                + str(route.owner_cpp_type)
                + ">"
            ]
            if receiver is not None
            else []
        ),
        *(_cpp_type(item.semantic_type, plan) for item in route.parameters),
    ]
    retained_values = [
        "retained_objects",
        *([receiver] if receiver is not None else []),
        *(f"supernote_input_{number}" for number, _ in enumerate(route.parameters)),
    ]
    lines.extend([
        f"{indent}      auto retained_input_state = std::make_shared<std::tuple<",
        f"{indent}          {', '.join(retained_types)}>>(",
        f"{indent}          {', '.join(retained_values)});",
    ])
    executor_captures = [
        "active_feature",
        "state",
        "retained_input_state",
        "retained_objects = std::move(retained_objects)",
    ]
    worker_captures = [
        "operation",
        "operation_id",
        "weak_feature",
        "state",
        "retained_objects = std::move(retained_objects)",
    ]
    if receiver is not None:
        executor_captures.append(f"{receiver} = std::move({receiver})")
        worker_captures.append(f"{receiver} = std::move({receiver})")
    for number, _ in enumerate(route.parameters):
        local = f"supernote_input_{number}"
        executor_captures.append(f"{local} = std::move({local})")
        worker_captures.append(f"{local} = std::move({local})")
    call = _call_expression(route, receiver)
    if route.result.kind is SemanticTypeKind.VOID:
        execution = f"{call};\n{indent}                  state->success = true;"
        resolution = (
            f"{indent}                    supernote_resolve_operation(\n"
            f"{indent}                        runtime, operation_id, Value::undefined());"
        )
    else:
        execution = (
            f"state->value.emplace({call});\n"
            f"{indent}                  {_retain_native_name(route.result)}(\n"
            f"{indent}                      *state->value, state->retained_result,\n"
            f"{indent}                      supernote::runtime::process_services().cleanup());\n"
            f"{indent}                  state->success = true;"
        )
        resolution = (
            f"{indent}                    auto object_registry = supernote_v4_object_registry(runtime);\n"
            f"{indent}                    supernote::conversion::Budget result_budget;\n"
            f"{indent}                    auto value = {_to_js_name(route.result)}(\n"
            f"{indent}                        runtime, *state->value, object_registry,\n"
            f"{indent}                        completion_feature, result_budget,\n"
            f"{indent}                        {json.dumps(diagnostic + '.result')}, 1);\n"
            f"{indent}                    supernote_resolve_operation(\n"
            f"{indent}                        runtime, operation_id, std::move(value));"
        )
    lines.extend([
        f"{indent}      auto executor = Function::createFromHostFunction(",
        f"{indent}          runtime, PropNameID::forAscii(runtime, \"SupernoteAsyncExecutor\"), 2,",
        f"{indent}          [{', '.join(executor_captures)}](facebook::jsi::Runtime &runtime,",
        f"{indent}             const Value &, const Value *continuation_arguments,",
        f"{indent}             std::size_t continuation_count) mutable -> Value {{",
        f"{indent}            if (continuation_count != 2 ||",
        f"{indent}                !continuation_arguments[0].isObject() ||",
        f"{indent}                !continuation_arguments[1].isObject()) {{",
        f"{indent}              throw facebook::jsi::JSError(",
        f"{indent}                  runtime, \"Promise supplied invalid continuation functions\");",
        f"{indent}            }}",
        f"{indent}            auto operation = active_feature->accept_factory(",
        f"{indent}                [](supernote::runtime::SessionId operation_id) {{",
        f"{indent}                  return [operation_id](void *runtime_pointer) {{",
        f"{indent}                    auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);",
        f"{indent}                    supernote_reject_operation(",
        f"{indent}                        runtime, operation_id, \"FEATURE_CLOSED\",",
        f"{indent}                        \"feature closed before async completion\");",
        f"{indent}                  }};",
        f"{indent}                }});",
        f"{indent}            if (!operation) {{",
        f"{indent}              supernote_reject_new_promise(",
        f"{indent}                  runtime, continuation_arguments[1], \"FEATURE_CLOSED\",",
        f"{indent}                  \"feature is closed\");",
        f"{indent}              return Value::undefined();",
        f"{indent}            }}",
        f"{indent}            operation->set_retained_state(retained_input_state);",
        f"{indent}            const auto operation_id = operation->id();",
        f"{indent}            supernote_register_continuation(",
        f"{indent}                runtime, operation_id, continuation_arguments[0],",
        f"{indent}                continuation_arguments[1]);",
        f"{indent}            std::weak_ptr<supernote::runtime::FeatureSession> weak_feature = active_feature;",
        f"{indent}            auto work = supernote::runtime::process_services().workers().submit(",
        f"{indent}                [{', '.join(worker_captures)}](supernote::runtime::CancellationToken executor_cancel) mutable {{",
        f"{indent}                  (void)retained_objects;",
        f"{indent}                  if (executor_cancel.is_cancelled() ||",
        f"{indent}                      operation->cancellation_token().is_cancelled()) return;",
        f"{indent}                  auto implementation_feature = weak_feature.lock();",
        f"{indent}                  if (!implementation_feature) return;",
        f"{indent}                  supernote::runtime::FeatureCallScope feature_call_scope(implementation_feature);",
        f"{indent}                  implementation_feature.reset();",
        f"{indent}                  try {{",
        f"{indent}                    {execution}",
        f"{indent}                  }} catch (const std::exception &error) {{",
        f"{indent}                    state->error = error.what();",
        f"{indent}                  }} catch (...) {{",
        f"{indent}                    state->error = \"unknown C++ implementation failure\";",
        f"{indent}                  }}",
        f"{indent}                  if (executor_cancel.is_cancelled() ||",
        f"{indent}                      operation->cancellation_token().is_cancelled()) return;",
        f"{indent}                  auto completion_feature = weak_feature.lock();",
        f"{indent}                  if (!completion_feature) return;",
        f"{indent}                  completion_feature->schedule_completion(",
        f"{indent}                      operation, [state, operation_id, completion_feature](void *runtime_pointer) {{",
        f"{indent}                        auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);",
        f"{indent}                        if (!state->success) {{",
        f"{indent}                          supernote_reject_operation(",
        f"{indent}                              runtime, operation_id, \"IMPLEMENTATION_ERROR\",",
        f"{indent}                              state->error.empty() ? \"C++ implementation failed\" : state->error);",
        f"{indent}                          return;",
        f"{indent}                        }}",
        f"{indent}                        try {{",
        resolution,
        f"{indent}                        }} catch (const std::exception &error) {{",
        f"{indent}                          supernote_reject_operation(",
        f"{indent}                              runtime, operation_id, \"INTERNAL\", error.what());",
        f"{indent}                        }}",
        f"{indent}                      }});",
        f"{indent}                }});",
        f"{indent}            operation->set_work(work);",
        f"{indent}            if (!work.accepted()) {{",
        f"{indent}              active_feature->schedule_completion(",
        f"{indent}                  operation, [operation_id](void *runtime_pointer) {{",
        f"{indent}                    auto &runtime = *static_cast<facebook::jsi::Runtime *>(runtime_pointer);",
        f"{indent}                    supernote_reject_operation(",
        f"{indent}                        runtime, operation_id, \"RESOURCE_EXHAUSTED\",",
        f"{indent}                        \"Supernote worker queue is full\");",
        f"{indent}                  }});",
        f"{indent}            }}",
        f"{indent}            return Value::undefined();",
        f"{indent}          }});",
        f"{indent}      auto promise = runtime.global().getPropertyAsFunction(runtime, \"Promise\");",
        f"{indent}      const Value executor_argument(std::move(executor));",
        f"{indent}      return promise.callAsConstructor(",
        f"{indent}          runtime, &executor_argument, static_cast<std::size_t>(1));",
        f"{indent}    }} catch (const facebook::jsi::JSError &) {{",
        f"{indent}      throw;",
        f"{indent}    }} catch (const supernote::conversion::Failure &failure) {{",
        f"{indent}      supernote_v4_throw_conversion_failure(runtime, failure);",
        f"{indent}    }} catch (const std::exception &error) {{",
        f"{indent}      supernote_throw_error(",
        f"{indent}          runtime, \"INTERNAL\", std::string({json.dumps(diagnostic + ': ')}) + error.what());",
        f"{indent}    }}",
    ])
    return (
        "Function::createFromHostFunction(\n"
        f"{indent}    runtime, PropNameID::forAscii(runtime, {json.dumps(name)}),\n"
        f"{indent}    {len(route.parameters)},\n"
        f"{indent}    {capture}(facebook::jsi::Runtime &runtime, const Value &,\n"
        f"{indent}       {argument_parameter}, std::size_t argument_count) mutable -> Value {{\n"
        + "\n".join(lines)
        + f"\n{indent}    }})"
    )


def _host_function(
    route: CppCallableRoute,
    *,
    diagnostic: str,
    plan: CppRoutePlan,
    name: str,
    receiver: str | None = None,
    capture: str,
    feature_expression: str,
    registry_expression: str,
    indent: str,
) -> str:
    if route.execution is ExecutionMode.ASYNC:
        function = _async_host_function(
            route,
            diagnostic=diagnostic,
            plan=plan,
            name=name,
            receiver=receiver,
            capture=capture,
            feature_expression=feature_expression,
            indent=indent,
        )
    else:
        body = _callable_body(
            route,
            _call_expression(route, receiver),
            diagnostic=diagnostic,
            plan=plan,
            registry=registry_expression,
            feature=feature_expression,
            indent=indent + "    ",
        )
        arguments = "const Value *arguments" if route.parameters else "const Value *"
        function = (
            "Function::createFromHostFunction(\n"
            f"{indent}    runtime, PropNameID::forAscii(runtime, {json.dumps(name)}),\n"
            f"{indent}    {len(route.parameters)},\n"
            f"{indent}    {capture}(facebook::jsi::Runtime &runtime, const Value &,\n"
            f"{indent}       {arguments}, std::size_t argument_count) -> Value {{\n"
            f"{body}\n"
            f"{indent}    }})"
        )
    accepts = _preflight_host_function(
        route,
        diagnostic=diagnostic,
        name=name + ".accepts",
        check=False,
        indent=indent,
    )
    check_arguments = _preflight_host_function(
        route,
        diagnostic=diagnostic,
        name=name + ".checkArguments",
        check=True,
        indent=indent,
    )
    return (
        "supernote_attach_preflight(\n"
        f"{indent}    runtime,\n"
        f"{indent}    {function},\n"
        f"{indent}    {accepts},\n"
        f"{indent}    {check_arguments})"
    )


def _preflight_host_function(
    route: CppCallableRoute,
    *,
    diagnostic: str,
    name: str,
    check: bool,
    indent: str,
) -> str:
    argument_parameter = "const Value *arguments" if route.parameters else "const Value *"
    lines: list[str] = []
    if check:
        lines.extend([
            f"{indent}      if (argument_count != {len(route.parameters)}) {{",
            f"{indent}        auto error = supernote_make_builtin_error(",
            f"{indent}            runtime, \"TypeError\",",
            f"{indent}            {json.dumps(diagnostic + ': wrong argument count')},",
            f"{indent}            \"ARITY_MISMATCH\", {json.dumps(diagnostic)},",
            f"{indent}            {json.dumps(str(len(route.parameters)) + ' arguments')},",
            f"{indent}            std::to_string(argument_count) + \" arguments\");",
            f"{indent}        return supernote_validation_failure(runtime, std::move(error));",
            f"{indent}      }}",
        ])
    else:
        lines.extend([
            f"{indent}      if (argument_count != {len(route.parameters)}) {{",
            f"{indent}        return Value(false);",
            f"{indent}      }}",
        ])
    lines.extend([
        f"{indent}      try {{",
        f"{indent}        [[maybe_unused]] supernote::conversion::Budget conversion_budget;",
    ])
    for number, parameter in enumerate(route.parameters):
        path = f"{diagnostic}.argument[{number}]({parameter.name})"
        lines.extend([
            f"{indent}        {_validate_js_name(parameter.semantic_type)}(",
            f"{indent}            runtime, arguments[{number}], conversion_budget,",
            f"{indent}            {json.dumps(path)}, 1);",
        ])
    lines.append(
        f"{indent}        return "
        + ("supernote_validation_success(runtime);" if check else "Value(true);")
    )
    lines.extend([
        f"{indent}      }} catch (const facebook::jsi::JSError &error) {{",
        (
            f"{indent}        return supernote_validation_failure(\n"
            f"{indent}            runtime, Value(runtime, error.value()));"
            if check
            else f"{indent}        return Value(false);"
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
        lines.append(f"{indent}        return Value(false);")
    lines.extend([
        f"{indent}      }} catch (const std::exception &error) {{",
        f"{indent}        supernote_throw_error(runtime, \"INTERNAL\", error.what());",
        f"{indent}      }}",
    ])
    return (
        "Function::createFromHostFunction(\n"
        f"{indent}    runtime, PropNameID::forAscii(runtime, {json.dumps(name)}),\n"
        f"{indent}    {len(route.parameters)},\n"
        f"{indent}    [](facebook::jsi::Runtime &runtime, const Value &,\n"
        f"{indent}       {argument_parameter}, std::size_t argument_count) -> Value {{\n"
        + "\n".join(lines)
        + f"\n{indent}    }})"
    )


def _wrapper(plan: CppRoutePlan, item: CppObjectRoute, index: int, module: str) -> str:
    class_name = _identifier(index)
    instance_methods = [
        route for route in item.methods
        if route.kind is CppCallableKind.INSTANCE_METHOD
        and route.javascript_public
    ]
    branches = []
    for route in instance_methods:
        capture = (
            "[native_instance = this->managed_ref(), feature = feature_session_]"
            if route.execution is ExecutionMode.ASYNC
            else (
                "[native_instance = this->managed_ref(), "
                "feature = feature_session_, registry = registry_]"
            )
        )
        function = _host_function(
            route,
            diagnostic=f"{module}.{item.named_type.public_name}.{route.public_name}",
            plan=plan,
            name=route.public_name,
            receiver="native_instance",
            capture=capture,
            feature_expression="feature.lock()",
            registry_expression="registry",
            indent="      ",
        )
        branches.append(
            f"    if (property_name == {json.dumps(route.public_name)}) {{\n"
            f"      return {function};\n"
            "    }"
        )
    for field in item.fields:
        path = f"{module}.{item.named_type.public_name}.{field.public_name}"
        result = (
            "supernote::conversion::Budget field_budget;\n"
            f"        return {_to_js_name(field.semantic_type)}(\n"
            f"            runtime, this->managed_ref()->{field.cpp_name}, registry_,\n"
            "            active_feature, field_budget,\n"
            f"            {json.dumps(path)}, 1);"
        )
        branches.append(
            f"    if (property_name == {json.dumps(field.public_name)}) {{\n"
            "      auto active_feature = feature_session_.lock();\n"
            "      if (!active_feature ||\n"
            "          active_feature->state() != supernote::runtime::FeatureState::ACTIVE) {\n"
            "        supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");\n"
            "      }\n"
            "      supernote::runtime::FeatureCallScope feature_call_scope(active_feature);\n"
            "      try {\n"
            f"        {result}\n"
            "      } catch (const facebook::jsi::JSError &) {\n"
            "        throw;\n"
            "      } catch (const supernote::conversion::Failure &failure) {\n"
            "        supernote_v4_throw_conversion_failure(runtime, failure);\n"
            "      } catch (const std::exception &error) {\n"
            "        supernote_throw_error(\n"
            "            runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"            std::string({json.dumps(module + '.' + item.named_type.public_name + '.' + field.public_name + ': ')}) + error.what());\n"
            "      } catch (...) {\n"
            "        supernote_throw_error(\n"
            "            runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"            {json.dumps(module + '.' + item.named_type.public_name + '.' + field.public_name + ': unknown C++ exception')});\n"
            "      }\n"
            "    }"
        )
    properties = "\n".join(
        "    properties.push_back(PropNameID::forAscii(runtime, "
        f"{json.dumps(name)}));"
        for name in [
            *(route.public_name for route in instance_methods),
            *(field.public_name for field in item.fields),
        ]
    ) or "    (void)runtime;"
    mutable_fields = [field for field in item.fields if field.mutable]
    set_branches = []
    for number, field in enumerate(mutable_fields):
        parameter = CppParameterRoute(
            field.public_name,
            field.cpp_spelling,
            field.semantic_type,
            (
                CppObjectPassing.SHARED_VALUE
                if field.semantic_type.kind is SemanticTypeKind.OBJECT_REF
                else None
            ),
        )
        prepared = _prepare_parameter(
            parameter,
            number,
            diagnostic=f"{module}.{item.named_type.public_name}.{field.public_name}",
            plan=plan,
            value_expression="value",
            indent="      ",
        )
        expression = _argument_expression(parameter, number)
        set_branches.append(
            f"    if (property_name == {json.dumps(field.public_name)}) {{\n"
            "      try {\n"
            "        supernote::conversion::Budget conversion_budget;\n"
            "        std::vector<supernote::runtime::ManagedAnyRef> retained_objects;\n"
            + "\n".join("  " + line for line in prepared)
            + "\n        auto active_feature = feature_session_.lock();\n"
            "        if (!active_feature ||\n"
            "            active_feature->state() != supernote::runtime::FeatureState::ACTIVE) {\n"
            "          supernote_throw_error(runtime, \"FEATURE_CLOSED\", \"feature is closed\");\n"
            "        }\n"
            "        supernote::runtime::FeatureCallScope feature_call_scope(active_feature);\n"
            f"        this->managed_ref()->{field.cpp_name} = {expression};\n"
            "        return;\n"
            "      } catch (const facebook::jsi::JSError &) {\n"
            "        throw;\n"
            "      } catch (const supernote::conversion::Failure &failure) {\n"
            "        supernote_v4_throw_conversion_failure(runtime, failure);\n"
            "      } catch (const std::exception &error) {\n"
            "        supernote_throw_error(\n"
            "            runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"            std::string({json.dumps(module + '.' + item.named_type.public_name + '.' + field.public_name + ': ')}) + error.what());\n"
            "      } catch (...) {\n"
            "        supernote_throw_error(\n"
            "            runtime, \"IMPLEMENTATION_ERROR\",\n"
            f"            {json.dumps(module + '.' + item.named_type.public_name + '.' + field.public_name + ': unknown C++ exception')});\n"
            "      }\n"
            "    }"
        )
    set_override = ""
    if set_branches:
        set_override = f"""

  void set(facebook::jsi::Runtime &runtime,
           const facebook::jsi::PropNameID &name,
           const facebook::jsi::Value &value) override {{
    const std::string property_name = name.utf8(runtime);
{chr(10).join(set_branches)}
  }}"""
    return f"""class {class_name} final
    : public supernote::runtime::CppObjectHandle<{item.named_type.cpp_type}> {{
 public:
  {class_name}(
      supernote::runtime::ManagedRef<{item.named_type.cpp_type}> instance,
      std::weak_ptr<supernote::runtime::FeatureSession> feature_session,
      std::shared_ptr<supernote::runtime::CppObjectRegistry> registry)
      : supernote::runtime::CppObjectHandle<{item.named_type.cpp_type}>(
            {json.dumps(item.named_type.type_id)}, std::move(instance)),
        feature_session_(std::move(feature_session)),
        registry_(std::move(registry)) {{}}

  facebook::jsi::Value get(
      facebook::jsi::Runtime &runtime,
      const facebook::jsi::PropNameID &name) override {{
    using facebook::jsi::Function;
    using facebook::jsi::PropNameID;
    using facebook::jsi::String;
    using facebook::jsi::Value;
    const std::string property_name = name.utf8(runtime);
{chr(10).join(branches)}
    return Value::undefined();
  }}{set_override}

  std::vector<facebook::jsi::PropNameID> getPropertyNames(
      facebook::jsi::Runtime &runtime) override {{
    using facebook::jsi::PropNameID;
    std::vector<facebook::jsi::PropNameID> properties;
    properties.reserve({len(instance_methods) + len(item.fields)});
{properties}
    return properties;
  }}

 private:
  std::weak_ptr<supernote::runtime::FeatureSession> feature_session_;
  std::shared_ptr<supernote::runtime::CppObjectRegistry> registry_;
}};"""


def _wrap_declarations(plan: CppRoutePlan) -> str:
    lines = []
    for index, item in enumerate(plan.objects):
        lines.extend(
            [
                f"class {_identifier(index)};",
                f"facebook::jsi::Object {_wrap_function(index)}(",
                "    facebook::jsi::Runtime &runtime,",
                "    const std::shared_ptr<supernote::runtime::CppObjectRegistry> &registry,",
                "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
                f"    std::shared_ptr<{item.named_type.cpp_type}> instance);",
            ]
        )
    return "\n".join(lines)


def _wrap_definitions(plan: CppRoutePlan) -> str:
    values = []
    for index, item in enumerate(plan.objects):
        values.append(
            f"""facebook::jsi::Object {_wrap_function(index)}(
    facebook::jsi::Runtime &runtime,
    const std::shared_ptr<supernote::runtime::CppObjectRegistry> &registry,
    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,
    std::shared_ptr<{item.named_type.cpp_type}> instance) {{
  return registry->wrap<{item.named_type.cpp_type}>(
      runtime, {json.dumps(item.named_type.type_id)}, std::move(instance),
      [feature, registry](
          supernote::runtime::ManagedRef<{item.named_type.cpp_type}> managed) {{
        return std::make_shared<{_identifier(index)}>(
            std::move(managed), feature, registry);
      }});
}}"""
        )
    return "\n\n".join(values)


def _object_registration(
    plan: CppRoutePlan,
    item: CppObjectRoute,
    module: str,
) -> str:
    semantic = SemanticType.object_ref(item.named_type.type_id)
    members = [
        f"    auto existing_type = exports.getProperty(runtime, {json.dumps(item.named_type.public_name)});",
        "    Object object_type = existing_type.isObject()",
        "        ? existing_type.getObject(runtime)",
        "        : Object(runtime);",
        f"    auto is_type = {_type_guard_host_function(semantic, diagnostic=f'{module}.{item.named_type.public_name}', name='is', check=False, indent='    ')};",
        "    object_type.setProperty(runtime, \"is\", std::move(is_type));",
        f"    auto check_type = {_type_guard_host_function(semantic, diagnostic=f'{module}.{item.named_type.public_name}', name='check', check=True, indent='    ')};",
        "    object_type.setProperty(runtime, \"check\", std::move(check_type));",
    ]
    if item.constructor is not None:
        route = item.constructor
        function = _host_function(
            route,
            diagnostic=f"{module}.{item.named_type.public_name}.create",
            plan=plan,
            name="create",
            capture=(
                "[feature_session]"
                if route.execution is ExecutionMode.ASYNC
                else "[feature_session, object_registry]"
            ),
            feature_expression="feature_session",
            registry_expression="object_registry",
            indent="    ",
        )
        members.extend(
            [
                f"    auto create = {function};",
                "    object_type.setProperty(runtime, \"create\", std::move(create));",
            ]
        )
    for route in item.methods:
        if route.kind is not CppCallableKind.STATIC_METHOD:
            continue
        if not route.javascript_public:
            continue
        function = _host_function(
            route,
            diagnostic=f"{module}.{item.named_type.public_name}.{route.public_name}",
            plan=plan,
            name=route.public_name,
            capture="[feature_session, object_registry]",
            feature_expression="feature_session",
            registry_expression="object_registry",
            indent="    ",
        )
        members.extend(
            [
                f"    auto method = {function};",
                f"    object_type.setProperty(runtime, {json.dumps(route.public_name)}, std::move(method));",
            ]
        )
    members.append(
        f"    exports.setProperty(runtime, {json.dumps(item.named_type.public_name)}, std::move(object_type));"
    )
    return "  {\n" + "\n".join(members) + "\n  }"


def _type_guard_host_function(
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
        lines.extend([
            f"{indent}      if (argument_count != 1) return Value(false);",
        ])
    lines.extend([
        f"{indent}      try {{",
        f"{indent}        supernote::conversion::Budget conversion_budget;",
        f"{indent}        {_validate_js_name(semantic)}(",
        f"{indent}            runtime, arguments[0], conversion_budget,",
        f"{indent}            {json.dumps(diagnostic)}, 1);",
        f"{indent}        return " + (
            "supernote_validation_success(runtime);" if check else "Value(true);"
        ),
        f"{indent}      }} catch (const facebook::jsi::JSError &error) {{",
        (
            f"{indent}        return supernote_validation_failure(\n"
            f"{indent}            runtime, Value(runtime, error.value()));"
            if check
            else f"{indent}        return Value(false);"
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
        lines.append(f"{indent}        return Value(false);")
    lines.extend([
        f"{indent}      }} catch (const std::exception &error) {{",
        f"{indent}        supernote_throw_error(runtime, \"INTERNAL\", error.what());",
        f"{indent}      }}",
    ])
    return (
        "Function::createFromHostFunction(\n"
        f"{indent}    runtime, PropNameID::forAscii(runtime, {json.dumps(name)}), 1,\n"
        f"{indent}    [](facebook::jsi::Runtime &runtime, const Value &,\n"
        f"{indent}       const Value *arguments, std::size_t argument_count) -> Value {{\n"
        + "\n".join(lines)
        + f"\n{indent}    }})"
    )


def _copied_type_registration(
    semantic: SemanticType,
    *,
    public_name: str,
    module: str,
) -> str:
    diagnostic = f"{module}.{public_name}"
    is_function = _type_guard_host_function(
        semantic, diagnostic=diagnostic, name="is", check=False, indent="    "
    )
    check_function = _type_guard_host_function(
        semantic, diagnostic=diagnostic, name="check", check=True, indent="    "
    )
    return f'''  {{
    auto existing_type = exports.getProperty(runtime, {json.dumps(public_name)});
    Object object_type = existing_type.isObject()
        ? existing_type.getObject(runtime)
        : Object(runtime);
    auto is_type = {is_function};
    object_type.setProperty(runtime, "is", std::move(is_type));
    auto check_type = {check_function};
    object_type.setProperty(runtime, "check", std::move(check_type));
    exports.setProperty(runtime, {json.dumps(public_name)}, std::move(object_type));
  }}'''


def _cpp_object_info_registration(plan: CppRoutePlan) -> str:
    branches = []
    for item in plan.objects:
        branches.extend([
            f"      if (type_id == {json.dumps(item.named_type.type_id)}) {{",
            "        Object result(runtime);",
            f"        result.setProperty(runtime, \"type\", {json.dumps(item.named_type.public_name)});",
            "        result.setProperty(runtime, \"originFamily\", \"cpp\");",
            "        return Value(std::move(result));",
            "      }",
        ])
    return f'''  {{
    auto inspect = Function::createFromHostFunction(
        runtime, PropNameID::forAscii(runtime, "__supernoteCppObjectInfo"), 1,
        [](facebook::jsi::Runtime &runtime, const Value &,
           const Value *arguments, std::size_t argument_count) -> Value {{
      if (argument_count != 1) return Value::undefined();
      auto type_id = supernote::runtime::cpp_object_type_id(runtime, arguments[0]);
      if (type_id.empty()) return Value::undefined();
{chr(10).join(branches)}
      return Value::undefined();
    }});
    exports.setProperty(
        runtime, "__supernoteCppObjectInfo", std::move(inspect));
  }}'''


def _function_registration(
    plan: CppRoutePlan,
    route: CppCallableRoute,
    module: str,
) -> str:
    function = _host_function(
        route,
        diagnostic=f"{module}.{route.public_name}",
        plan=plan,
        name=route.public_name,
        capture=(
            "[feature_session]"
            if route.execution is ExecutionMode.ASYNC
            else "[feature_session, object_registry]"
        ),
        feature_expression="feature_session",
        registry_expression="object_registry",
        indent="    ",
    )
    return f"""  {{
    auto function = {function};
    exports.setProperty(runtime, {json.dumps(route.public_name)}, std::move(function));
  }}"""


def render_cpp_object_bindings(
    plan: CppRoutePlan,
    *,
    module_name: str,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Return include, namespace-body, and registration fragments.

    Every public free function is emitted through the V4 route renderer.  This
    preserves its exact namespace-qualified native symbol while keeping the
    public JavaScript name independent of the implementation spelling.
    """

    object_functions = tuple(
        item for item in plan.functions if item.javascript_public
    )
    if not plan.objects and not object_functions:
        return (), (), (), ()
    roots: list[SemanticType] = []
    for route in object_functions:
        roots.extend(parameter.semantic_type for parameter in route.parameters)
        roots.append(route.result)
    for item in plan.objects:
        if item.constructor is not None:
            roots.extend(
                parameter.semantic_type for parameter in item.constructor.parameters
            )
            roots.append(item.constructor.result)
        for route in item.methods:
            roots.extend(parameter.semantic_type for parameter in route.parameters)
            roots.append(route.result)
        roots.extend(field.semantic_type for field in item.fields)
    conversion_types = _collect_types(roots, plan)
    wrappers = (
        _wrap_declarations(plan),
        _conversion_helpers(conversion_types, plan),
        *(_wrapper(plan, item, index, module_name) for index, item in enumerate(plan.objects)),
        _wrap_definitions(plan),
    )
    registrations = [
        "  auto object_registry = std::make_shared<supernote::runtime::CppObjectRegistry>(\n"
        "      supernote::runtime::process_services().cleanup());\n"
        "  exports.setProperty(\n"
        "      runtime, kCppObjectRegistryProperty,\n"
        "      Object::createFromHostObject(\n"
        "          runtime, std::make_shared<supernote::runtime::CppObjectRegistryOwner>(\n"
        "              object_registry)));"
    ]
    registrations.extend(
        _function_registration(plan, item, module_name) for item in object_functions
    )
    registrations.extend(
        _object_registration(plan, item, module_name) for item in plan.objects
    )
    converted_named_types = {
        (item.kind, item.type_id)
        for item in conversion_types
        if item.type_id is not None
    }
    registrations.extend(
        _copied_type_registration(
            SemanticType.value_ref(item.named_type.type_id),
            public_name=item.named_type.public_name,
            module=module_name,
        )
        for item in plan.values
        if (SemanticTypeKind.VALUE_REF, item.named_type.type_id)
        in converted_named_types
    )
    registrations.extend(
        _copied_type_registration(
            SemanticType.enum_ref(item.named_type.type_id),
            public_name=item.named_type.public_name,
            module=module_name,
        )
        for item in plan.enums
        if (SemanticTypeKind.ENUM_REF, item.named_type.type_id)
        in converted_named_types
    )
    if plan.objects:
        registrations.append(_cpp_object_info_registration(plan))
    includes = tuple(
        dict.fromkeys(item.include for item in plan.named_types if item.include)
    )
    declarations = tuple(
        _function_declaration(item) for item in object_functions
    )
    return (
        includes,
        declarations,
        tuple(filter(None, wrappers)),
        tuple(registrations),
    )


def _requires_recursive_conversion(semantic: SemanticType) -> bool:
    return semantic.kind not in {
        SemanticTypeKind.VOID,
        SemanticTypeKind.SCALAR,
    }


def _function_declaration(route: CppCallableRoute) -> str:
    parameters = ", ".join(
        f"{item.cpp_spelling} {item.name}" for item in route.parameters
    )
    exception = " noexcept" if route.noexcept else ""
    declaration = (
        f"{route.result_cpp_spelling} {route.public_name}({parameters})"
        f"{exception};"
    )
    for namespace in reversed(route.cpp_namespace):
        declaration = f"namespace {namespace} {{\n{declaration}\n}}"
    return declaration
