"""Generate copied C++ to JVM internal-route conversions for V4 values."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .binding_codegen import (
    scan_cpp_class_source_model,
    scan_cpp_enum_source_model,
    scan_cpp_source_model,
)
from .cpp_object_binding_codegen import _cpp_type
from .cpp_routes import CppRoutePlan, plan_cpp_routes
from .jvm_manifest import JvmSourceManifest, jvm_adapter_identity
from .jvm_routes import JvmRoutePlan, plan_jvm_routes
from .semantic import (
    BackendFamily,
    DeclarationRole,
    SemanticApi,
    SemanticBinding,
    SemanticClassKind,
    SemanticModelError,
    SourceProvenance,
    validate_semantic_route,
)
from .semantic_types import ScalarKind, SemanticType, SemanticTypeKind


class CrossFamilyCodegenError(ValueError):
    """Raised before emission when an internal copied route is impossible."""


_PRIMITIVE_FIELDS = {
    ScalarKind.BOOL: ("z", "JNI_TRUE", "JNI_FALSE", "jboolean", "CallStaticBooleanMethodA"),
    ScalarKind.INT32: ("i", None, None, "jint", "CallStaticIntMethodA"),
    ScalarKind.INT64: ("j", None, None, "jlong", "CallStaticLongMethodA"),
    ScalarKind.FLOAT32: ("f", None, None, "jfloat", "CallStaticFloatMethodA"),
    ScalarKind.FLOAT64: ("d", None, None, "jdouble", "CallStaticDoubleMethodA"),
}


def _suffix(value: SemanticType) -> str:
    encoded = json.dumps(
        value.manifest(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _to_name(value: SemanticType) -> str:
    return f"supernote_v4_cross_to_jvm_{_suffix(value)}"


def _from_name(value: SemanticType) -> str:
    return f"supernote_v4_cross_from_jvm_{_suffix(value)}"


def _route_expression(key: str, adapter: str, descriptor: str, method: str) -> str:
    return (
        "supernote_v4_jvm_route(feature, "
        + json.dumps(key)
        + ", "
        + json.dumps(adapter)
        + ", "
        + json.dumps(descriptor)
        + ", "
        + json.dumps(method)
        + ")"
    )


def _adapter_class(identity: str) -> str:
    return "supernote.generated.adapters.Adapter_" + identity.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class CrossFamilyRenderer:
    api: SemanticApi
    cpp: CppRoutePlan
    jvm: JvmRoutePlan
    feature_id: str

    def __post_init__(self) -> None:
        for binding in self.internal_jvm_bindings:
            self.validate_binding(binding)

    @property
    def internal_jvm_bindings(self) -> tuple[SemanticBinding, ...]:
        result = [
            item
            for item in self.api.functions
            if item.source.language in {"kotlin", "java"}
            and item.capabilities.role is DeclarationRole.INTERNAL
        ]
        for owner in self.api.classes:
            if (
                owner.kind is SemanticClassKind.INTERNAL_SERVICE
                and owner.source.language in {"kotlin", "java"}
            ):
                result.extend(owner.methods)
        return tuple(result)

    def validate_binding(self, binding: SemanticBinding) -> None:
        source = self._cpp_endpoint(binding)
        try:
            for parameter in binding.parameters:
                validate_semantic_route(
                    self.api,
                    parameter.type,
                    BackendFamily.CPP,
                    BackendFamily.JVM,
                    source,
                    binding.source,
                )
            validate_semantic_route(
                self.api,
                binding.result,
                BackendFamily.CPP,
                BackendFamily.JVM,
                source,
                binding.source,
            )
        except SemanticModelError as exc:
            raise CrossFamilyCodegenError(str(exc)) from exc

    def _cpp_endpoint(self, binding: SemanticBinding) -> SourceProvenance:
        declarations = {item.type_id: item for item in self.api.declarations}

        def find(value: SemanticType) -> SourceProvenance | None:
            if value.element is not None:
                return find(value.element)
            if value.type_id is None:
                return None
            declaration = declarations[value.type_id]
            for projection in declaration.projections:
                if projection.backend is BackendFamily.CPP:
                    return projection.source
            return None

        for value in (*[item.type for item in binding.parameters], binding.result):
            endpoint = find(value)
            if endpoint is not None:
                return endpoint
        return SourceProvenance(
            "generated:cpp-internal:" + binding.binding_id,
            "cpp",
            f"<generated C++ internal facade for {binding.name}>",
            1,
            1,
        )

    def cpp_type(self, value: SemanticType) -> str:
        try:
            return _cpp_type(value, self.cpp)
        except KeyError as exc:
            raise CrossFamilyCodegenError(
                f"copied internal type {value.value!r} has no C++ projection"
            ) from exc

    def descriptor(self, binding: SemanticBinding, owner_class: str | None) -> str:
        named = self.jvm.named_types_by_id
        parameters = ""
        if owner_class is not None:
            parameters += f"L{owner_class.replace('.', '/')};"
        parameters += "".join(self._descriptor(item.type, named) for item in binding.parameters)
        return f"({parameters}){self._descriptor(binding.result, named)}"

    def suspend_descriptor(
        self, binding: SemanticBinding, owner_class: str | None
    ) -> str:
        descriptor = self.descriptor(binding, owner_class)
        parameters, _result = descriptor.split(")", 1)
        return parameters + "J)Lkotlinx/coroutines/Job;"

    def _descriptor(self, value: SemanticType, named) -> str:
        if value.kind is SemanticTypeKind.VOID:
            return "V"
        if value.kind is SemanticTypeKind.NULLABLE:
            assert value.element is not None
            child = value.element
            if child.kind is SemanticTypeKind.SCALAR and child.scalar in _PRIMITIVE_FIELDS:
                return {
                    ScalarKind.BOOL: "Ljava/lang/Boolean;",
                    ScalarKind.INT32: "Ljava/lang/Integer;",
                    ScalarKind.INT64: "Ljava/lang/Long;",
                    ScalarKind.FLOAT32: "Ljava/lang/Float;",
                    ScalarKind.FLOAT64: "Ljava/lang/Double;",
                }[child.scalar]
            return self._descriptor(child, named)
        if value.kind is SemanticTypeKind.ARRAY:
            return "Ljava/util/List;"
        if value.kind is SemanticTypeKind.SCALAR:
            return {
                ScalarKind.BOOL: "Z",
                ScalarKind.INT32: "I",
                ScalarKind.INT64: "J",
                ScalarKind.FLOAT32: "F",
                ScalarKind.FLOAT64: "D",
                ScalarKind.STRING: "[B",
                ScalarKind.BYTES: "[B",
            }[value.scalar]
        assert value.type_id is not None
        route = named.get(value.type_id)
        if route is None:
            raise CrossFamilyCodegenError(
                f"copied internal type {value.type_id!r} has no JVM projection"
            )
        return f"L{route.owner_class.replace('.', '/')};"

    def includes(self) -> tuple[str, ...]:
        used = {item.type_id for item in self._collected_types() if item.type_id}
        return tuple(
            sorted(
                {item.include for item in self.cpp.named_types if item.type_id in used}
            )
        )

    def render_helpers(self) -> str:
        values = self._collected_types()
        prototypes = []
        definitions = []
        for value in values:
            if value.kind is SemanticTypeKind.VOID:
                continue
            native = self.cpp_type(value)
            prototypes.append(
                f"jobject {_to_name(value)}(const {native} &value, JNIEnv *env, "
                "const std::shared_ptr<supernote::runtime::FeatureSession> &feature, "
                "supernote::conversion::Budget &budget, const std::string &path, "
                "std::uint64_t depth);"
            )
            prototypes.append(
                f"{native} {_from_name(value)}(jobject value, JNIEnv *env, "
                "const std::shared_ptr<supernote::runtime::FeatureSession> &feature, "
                "supernote::conversion::Budget &budget, const std::string &path, "
                "std::uint64_t depth);"
            )
        for value in values:
            if value.kind is SemanticTypeKind.VOID:
                continue
            definitions.append(self._render_to(value))
            definitions.append(self._render_from(value))
        return "\n".join((*prototypes, *definitions))

    def worker_invocation(self, binding: SemanticBinding, takes_owner: bool) -> str:
        self.validate_binding(binding)
        offset = 1 if takes_owner else 0
        size = len(binding.parameters) + offset
        lines = [
            "      supernote::conversion::Budget cross_budget;",
            f"      jvalue jvm_arguments[{max(1, size)}]{{}};",
        ]
        if takes_owner:
            lines.append(
                "      jvm_arguments[0].l = static_cast<jobject>(owner->value.get());"
            )
        for index, parameter in enumerate(binding.parameters):
            target = index + offset
            value = parameter.type
            if self._direct_primitive(value):
                assert value.scalar is not None
                field = _PRIMITIVE_FIELDS[value.scalar][0]
                expression = parameter.name
                if value.scalar is ScalarKind.BOOL:
                    expression += " ? JNI_TRUE : JNI_FALSE"
                else:
                    expression = f"static_cast<{_PRIMITIVE_FIELDS[value.scalar][3]}>({expression})"
                lines.append(f"      cross_budget.visit({json.dumps(parameter.name)}, 0);")
                lines.append(f"      jvm_arguments[{target}].{field} = {expression};")
            else:
                local = f"cross_argument_{index}"
                lines.append(
                    f"      auto {local} = {_to_name(value)}({parameter.name}, env, feature, "
                    f"cross_budget, {json.dumps(parameter.name)}, 0);"
                )
                lines.append(f"      jvm_arguments[{target}].l = {local};")
        call = self._jni_call(binding.result)
        expression = (
            f"env->{call}(static_cast<jclass>(resolved->adapter_class.get()), "
            "resolved->method, jvm_arguments)"
        )
        if binding.result.kind is SemanticTypeKind.VOID:
            lines.extend([f"      {expression};", "      require_no_implementation_exception(env);"])
        elif self._direct_primitive(binding.result):
            lines.extend([
                f"      auto result = {expression};",
                "      require_no_implementation_exception(env);",
                "      cross_budget.visit(\"result\", 0);",
                f"      return {self._primitive_result(binding.result, 'result')};",
            ])
        else:
            lines.extend([
                f"      auto result = {expression};",
                "      require_no_implementation_exception(env);",
                f"      return {_from_name(binding.result)}(result, env, feature, cross_budget, \"result\", 0);",
            ])
        return "\n".join(lines)

    def suspend_worker_arguments(
        self, binding: SemanticBinding, takes_owner: bool
    ) -> str:
        self.validate_binding(binding)
        offset = 1 if takes_owner else 0
        size = len(binding.parameters) + offset + 1
        lines = [
            "supernote::conversion::Budget cross_budget;",
            f"jvalue jvm_arguments[{max(1, size)}]{{}};",
        ]
        if takes_owner:
            lines.append(
                "jvm_arguments[0].l = static_cast<jobject>(owner->value.get());"
            )
        for index, parameter in enumerate(binding.parameters):
            target = index + offset
            value = parameter.type
            if self._direct_primitive(value):
                assert value.scalar is not None
                field = _PRIMITIVE_FIELDS[value.scalar][0]
                expression = parameter.name
                if value.scalar is ScalarKind.BOOL:
                    expression += " ? JNI_TRUE : JNI_FALSE"
                else:
                    expression = (
                        f"static_cast<{_PRIMITIVE_FIELDS[value.scalar][3]}>"
                        f"({expression})"
                    )
                lines.append(
                    f"cross_budget.visit({json.dumps(parameter.name)}, 0);"
                )
                lines.append(f"jvm_arguments[{target}].{field} = {expression};")
            else:
                local = f"cross_argument_{index}"
                lines.append(
                    f"auto {local} = {_to_name(value)}({parameter.name}, env, "
                    f"feature, cross_budget, {json.dumps(parameter.name)}, 0);"
                )
                lines.append(f"jvm_arguments[{target}].l = {local};")
        lines.append(
            f"jvm_arguments[{size - 1}].j = static_cast<jlong>(completion_id);"
        )
        return "\n".join(lines)

    def suspend_result_expression(
        self,
        value: SemanticType,
        *,
        expression: str,
        feature: str,
        budget: str,
    ) -> str:
        if value.kind is SemanticTypeKind.VOID:
            raise CrossFamilyCodegenError("void has no copied suspend result")
        return (
            f"{_from_name(value)}(static_cast<jobject>({expression}), env, "
            f"{feature}, {budget}, \"result\", 0)"
        )

    def _collected_types(self) -> tuple[SemanticType, ...]:
        roots = [
            value
            for binding in self.internal_jvm_bindings
            for value in (*[item.type for item in binding.parameters], binding.result)
        ]
        values_by_id = {item.named_type.type_id: item for item in self.cpp.values}
        found: dict[str, SemanticType] = {}

        def visit(value: SemanticType) -> None:
            if value.kind is SemanticTypeKind.VOID:
                return
            key = _suffix(value)
            if key in found:
                return
            found[key] = value
            if value.element is not None:
                visit(value.element)
            elif value.kind is SemanticTypeKind.VALUE_REF:
                assert value.type_id is not None
                route = values_by_id.get(value.type_id)
                if route is not None:
                    for field in route.fields:
                        visit(field.semantic_type)

        for root in roots:
            visit(root)
        return tuple(found[key] for key in sorted(found))

    @staticmethod
    def _direct_primitive(value: SemanticType) -> bool:
        return (
            value.kind is SemanticTypeKind.SCALAR
            and value.scalar in _PRIMITIVE_FIELDS
        )

    @staticmethod
    def _jni_call(value: SemanticType) -> str:
        if value.kind is SemanticTypeKind.VOID:
            return "CallStaticVoidMethodA"
        if CrossFamilyRenderer._direct_primitive(value):
            assert value.scalar is not None
            return _PRIMITIVE_FIELDS[value.scalar][4]
        return "CallStaticObjectMethodA"

    @staticmethod
    def _primitive_result(value: SemanticType, expression: str) -> str:
        assert value.scalar is not None
        if value.scalar is ScalarKind.BOOL:
            return f"{expression} == JNI_TRUE"
        return f"static_cast<{CrossFamilyRenderer._primitive_cpp(value.scalar)}>({expression})"

    @staticmethod
    def _primitive_cpp(value: ScalarKind) -> str:
        return {
            ScalarKind.BOOL: "bool",
            ScalarKind.INT32: "std::int32_t",
            ScalarKind.INT64: "std::int64_t",
            ScalarKind.FLOAT32: "float",
            ScalarKind.FLOAT64: "double",
        }[value]

    def _helper_route(self, method: str, descriptor: str) -> str:
        digest = hashlib.sha256(self.feature_id.encode("utf-8")).hexdigest()[:20]
        return _route_expression(
            f"jvm-v4-cross-helper:{method}:{descriptor}",
            f"supernote.generated.adapters.Identity_{digest}",
            descriptor,
            method,
        )

    def _render_to(self, value: SemanticType) -> str:
        native = self.cpp_type(value)
        lines = [
            f"jobject {_to_name(value)}(const {native} &value, JNIEnv *env,",
            "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
            "    supernote::conversion::Budget &budget, const std::string &path,",
            "    std::uint64_t depth) {",
            "  budget.visit(path, depth);",
        ]
        kind = value.kind
        if kind is SemanticTypeKind.SCALAR:
            assert value.scalar is not None
            if value.scalar in _PRIMITIVE_FIELDS:
                method, descriptor, field = {
                    ScalarKind.BOOL: ("boxBoolean", "(Z)Ljava/lang/Object;", "z"),
                    ScalarKind.INT32: ("boxInt", "(I)Ljava/lang/Object;", "i"),
                    ScalarKind.INT64: ("boxLong", "(J)Ljava/lang/Object;", "j"),
                    ScalarKind.FLOAT32: ("boxFloat", "(F)Ljava/lang/Object;", "f"),
                    ScalarKind.FLOAT64: ("boxDouble", "(D)Ljava/lang/Object;", "d"),
                }[value.scalar]
                expression = "value ? JNI_TRUE : JNI_FALSE" if value.scalar is ScalarKind.BOOL else f"static_cast<{_PRIMITIVE_FIELDS[value.scalar][3]}>(value)"
                lines.extend([
                    f"  auto route = {self._helper_route(method, descriptor)};",
                    "  jvalue arguments[1]{};",
                    f"  arguments[0].{field} = {expression};",
                    "  auto result = env->CallStaticObjectMethodA(",
                    "      static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                    "  require_no_implementation_exception(env);",
                    "  if (result == nullptr) throw std::runtime_error(\"JVM boxing returned null\");",
                    "  return result;",
                ])
            else:
                data = "reinterpret_cast<const std::byte *>(value.data())" if value.scalar is ScalarKind.STRING else "value.data()"
                check = "check_string_bytes" if value.scalar is ScalarKind.STRING else "check_byte_buffer"
                lines.extend([
                    f"  budget.{check}(path, value.size());",
                    "  budget.reserve(path, value.size());",
                    f"  return write_byte_array(env, {data}, value.size());",
                ])
        elif kind is SemanticTypeKind.NULLABLE:
            assert value.element is not None
            lines.extend([
                "  if (!value) return nullptr;",
                f"  return {_to_name(value.element)}(*value, env, feature, budget, path, depth + 1);",
            ])
        elif kind is SemanticTypeKind.ENUM_REF:
            assert value.type_id is not None
            cpp_enum = next(item for item in self.cpp.enums if item.named_type.type_id == value.type_id)
            jvm_enum = next(item for item in self.jvm.enums if item.named_type.type_id == value.type_id)
            adapter = _adapter_class(jvm_adapter_identity(jvm_enum.named_type.source_declaration_id + "#enum"))
            descriptor = f"([B)L{jvm_enum.named_type.owner_class.replace('.', '/')};"
            lines.extend(["  const char *name = nullptr;", "  switch (value) {"])
            for constant in cpp_enum.constants:
                lines.append(f"    case {cpp_enum.named_type.cpp_type}::{constant}: name = {json.dumps(constant)}; break;")
            lines.extend([
                "  }",
                "  if (name == nullptr) throw std::runtime_error(\"unknown C++ enum value\");",
                "  const std::string text(name);",
                f"  auto route = {_route_expression('jvm-v4-cross-enum-from:' + value.type_id, adapter, descriptor, 'fromName')};",
                "  jvalue arguments[1]{};",
                "  arguments[0].l = write_byte_array(env, reinterpret_cast<const std::byte *>(text.data()), text.size());",
                "  auto result = env->CallStaticObjectMethodA(static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                "  require_no_implementation_exception(env);",
                "  if (result == nullptr) throw std::runtime_error(\"JVM enum adapter returned null\");",
                "  return result;",
            ])
        elif kind is SemanticTypeKind.ARRAY:
            assert value.element is not None
            lines.extend([
                "  budget.check_array_length(path, value.size());",
                f"  auto create_route = {self._helper_route('newList', '()Ljava/util/List;')};",
                "  auto list = env->CallStaticObjectMethod(static_cast<jclass>(create_route->adapter_class.get()), create_route->method);",
                "  require_no_implementation_exception(env);",
                "  if (list == nullptr) throw std::runtime_error(\"JVM list adapter returned null\");",
                f"  auto add_route = {self._helper_route('listAdd', '(Ljava/util/List;Ljava/lang/Object;)V')};",
                "  for (std::size_t index = 0; index < value.size(); ++index) {",
                "    auto item_path = supernote::conversion::index_path(path, index);",
                f"    auto item = {_to_name(value.element)}(value[index], env, feature, budget, item_path, depth + 1);",
                "    jvalue arguments[2]{}; arguments[0].l = list; arguments[1].l = item;",
                "    env->CallStaticVoidMethodA(static_cast<jclass>(add_route->adapter_class.get()), add_route->method, arguments);",
                "    require_no_implementation_exception(env);",
                "    if (item != nullptr) env->DeleteLocalRef(item);",
                "  }",
                "  return list;",
            ])
        elif kind is SemanticTypeKind.VALUE_REF:
            assert value.type_id is not None
            cpp_value = next(item for item in self.cpp.values if item.named_type.type_id == value.type_id)
            jvm_value = next(item for item in self.jvm.values if item.named_type.type_id == value.type_id)
            lines.extend([
                f"  auto route = {_route_expression('jvm-v4-cross-value:' + value.type_id, _adapter_class(jvm_value.constructor.adapter_identity), jvm_value.constructor.adapter_descriptor, 'invoke')};",
                f"  jvalue arguments[{max(1, len(jvm_value.constructor_fields) + 1)}]{{}};",
                "  auto runtime_session = feature->runtime();",
                "  auto context = runtime_session ? runtime_session->platform_context() : nullptr;",
                "  if (!context) throw std::runtime_error(\"platform Context is unavailable\");",
                "  arguments[0].l = static_cast<jobject>(context.get());",
            ])
            cpp_fields = {item.public_name: item for item in cpp_value.fields}
            for index, field in enumerate(jvm_value.constructor_fields, 1):
                cpp_field = cpp_fields[field.public_name]
                expression = f"value.{cpp_field.cpp_name}"
                if self._direct_primitive(field.semantic_type):
                    assert field.semantic_type.scalar is not None
                    jfield = _PRIMITIVE_FIELDS[field.semantic_type.scalar][0]
                    cast = expression + " ? JNI_TRUE : JNI_FALSE" if field.semantic_type.scalar is ScalarKind.BOOL else f"static_cast<{_PRIMITIVE_FIELDS[field.semantic_type.scalar][3]}>({expression})"
                    lines.append(f"  arguments[{index}].{jfield} = {cast};")
                else:
                    lines.extend([
                        f"  auto field_{index} = {_to_name(field.semantic_type)}({expression}, env, feature, budget, supernote::conversion::field_path(path, {json.dumps(field.public_name)}), depth + 1);",
                        f"  arguments[{index}].l = field_{index};",
                    ])
            lines.extend([
                "  auto result = env->CallStaticObjectMethodA(static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                "  require_no_implementation_exception(env);",
                "  if (result == nullptr) throw std::runtime_error(\"JVM value constructor returned null\");",
                "  return result;",
            ])
        else:
            raise CrossFamilyCodegenError("native objects cannot use copied converters")
        lines.append("}")
        return "\n".join(lines)

    def _render_from(self, value: SemanticType) -> str:
        native = self.cpp_type(value)
        lines = [
            f"{native} {_from_name(value)}(jobject value, JNIEnv *env,",
            "    const std::shared_ptr<supernote::runtime::FeatureSession> &feature,",
            "    supernote::conversion::Budget &budget, const std::string &path,",
            "    std::uint64_t depth) {",
            "  budget.visit(path, depth);",
        ]
        kind = value.kind
        if kind is SemanticTypeKind.NULLABLE:
            assert value.element is not None
            lines.extend([
                "  if (value == nullptr) return std::nullopt;",
                f"  return {_from_name(value.element)}(value, env, feature, budget, path, depth + 1);",
            ])
        else:
            lines.append("  if (value == nullptr) throw std::runtime_error(\"non-null JVM copied result was null\");")
            if kind is SemanticTypeKind.SCALAR:
                assert value.scalar is not None
                if value.scalar in _PRIMITIVE_FIELDS:
                    method, descriptor, call = {
                        ScalarKind.BOOL: ("unboxBoolean", "(Ljava/lang/Object;)Z", "CallStaticBooleanMethodA"),
                        ScalarKind.INT32: ("unboxInt", "(Ljava/lang/Object;)I", "CallStaticIntMethodA"),
                        ScalarKind.INT64: ("unboxLong", "(Ljava/lang/Object;)J", "CallStaticLongMethodA"),
                        ScalarKind.FLOAT32: ("unboxFloat", "(Ljava/lang/Object;)F", "CallStaticFloatMethodA"),
                        ScalarKind.FLOAT64: ("unboxDouble", "(Ljava/lang/Object;)D", "CallStaticDoubleMethodA"),
                    }[value.scalar]
                    lines.extend([
                        f"  auto route = {self._helper_route(method, descriptor)};",
                        "  jvalue arguments[1]{}; arguments[0].l = value;",
                        f"  auto result = env->{call}(static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                        "  require_no_implementation_exception(env);",
                        f"  return {self._primitive_result(value, 'result')};",
                    ])
                else:
                    check = "check_string_bytes" if value.scalar is ScalarKind.STRING else "check_byte_buffer"
                    lines.extend([
                        "  auto bytes = read_byte_array(env, static_cast<jbyteArray>(value));",
                        f"  budget.{check}(path, bytes.size());",
                        "  budget.reserve(path, bytes.size());",
                    ])
                    if value.scalar is ScalarKind.STRING:
                        lines.append("  return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());")
                    else:
                        lines.append("  return bytes;")
            elif kind is SemanticTypeKind.ENUM_REF:
                assert value.type_id is not None
                cpp_enum = next(item for item in self.cpp.enums if item.named_type.type_id == value.type_id)
                jvm_enum = next(item for item in self.jvm.enums if item.named_type.type_id == value.type_id)
                adapter = _adapter_class(jvm_adapter_identity(jvm_enum.named_type.source_declaration_id + "#enum"))
                enum_descriptor = (
                    f"(L{jvm_enum.named_type.owner_class.replace('.', '/')};)[B"
                )
                lines.extend([
                    f"  auto route = {_route_expression('jvm-v4-cross-enum-name:' + value.type_id, adapter, enum_descriptor, 'name')};",
                    "  jvalue arguments[1]{}; arguments[0].l = value;",
                    "  auto raw = env->CallStaticObjectMethodA(static_cast<jclass>(route->adapter_class.get()), route->method, arguments);",
                    "  require_no_implementation_exception(env);",
                    "  auto bytes = read_byte_array(env, static_cast<jbyteArray>(raw));",
                    "  std::string name(reinterpret_cast<const char *>(bytes.data()), bytes.size());",
                ])
                for constant in cpp_enum.constants:
                    lines.append(f"  if (name == {json.dumps(constant)}) return {cpp_enum.named_type.cpp_type}::{constant};")
                lines.append("  throw std::runtime_error(\"unknown JVM enum name\");")
            elif kind is SemanticTypeKind.ARRAY:
                assert value.element is not None
                lines.extend([
                    f"  auto size_route = {self._helper_route('listSize', '(Ljava/util/List;)I')};",
                    "  jvalue size_arguments[1]{}; size_arguments[0].l = value;",
                    "  auto size = env->CallStaticIntMethodA(static_cast<jclass>(size_route->adapter_class.get()), size_route->method, size_arguments);",
                    "  require_no_implementation_exception(env);",
                    "  if (size < 0) throw std::runtime_error(\"JVM list size was negative\");",
                    "  budget.check_array_length(path, static_cast<std::uint64_t>(size));",
                    f"  {native} result; result.reserve(static_cast<std::size_t>(size));",
                    f"  auto get_route = {self._helper_route('listGet', '(Ljava/util/List;I)Ljava/lang/Object;')};",
                    "  for (jint index = 0; index < size; ++index) {",
                    "    jvalue arguments[2]{}; arguments[0].l = value; arguments[1].i = index;",
                    "    auto item = env->CallStaticObjectMethodA(static_cast<jclass>(get_route->adapter_class.get()), get_route->method, arguments);",
                    "    require_no_implementation_exception(env);",
                    "    auto item_path = supernote::conversion::index_path(path, static_cast<std::uint64_t>(index));",
                    f"    result.push_back({_from_name(value.element)}(item, env, feature, budget, item_path, depth + 1));",
                    "    if (item != nullptr) env->DeleteLocalRef(item);",
                    "  }",
                    "  return result;",
                ])
            elif kind is SemanticTypeKind.VALUE_REF:
                assert value.type_id is not None
                cpp_value = next(item for item in self.cpp.values if item.named_type.type_id == value.type_id)
                jvm_value = next(item for item in self.jvm.values if item.named_type.type_id == value.type_id)
                jvm_fields = {item.public_name: item for item in jvm_value.fields}
                for index, cpp_field in enumerate(cpp_value.fields):
                    field = jvm_fields[cpp_field.public_name]
                    adapter = _adapter_class(field.accessor_identity)
                    lines.append(f"  auto field_route_{index} = {_route_expression('jvm-v4-cross-field:' + field.field_id, adapter, field.getter_descriptor, 'get')};")
                    lines.append(f"  jvalue field_arguments_{index}[1]{{}}; field_arguments_{index}[0].l = value;")
                    if self._direct_primitive(field.semantic_type):
                        assert field.semantic_type.scalar is not None
                        call = _PRIMITIVE_FIELDS[field.semantic_type.scalar][4]
                        lines.extend([
                            f"  auto field_raw_{index} = env->{call}(static_cast<jclass>(field_route_{index}->adapter_class.get()), field_route_{index}->method, field_arguments_{index});",
                            "  require_no_implementation_exception(env);",
                            f"  auto field_{index} = {self._primitive_result(field.semantic_type, f'field_raw_{index}')};",
                        ])
                    else:
                        lines.extend([
                            f"  auto field_raw_{index} = env->CallStaticObjectMethodA(static_cast<jclass>(field_route_{index}->adapter_class.get()), field_route_{index}->method, field_arguments_{index});",
                            "  require_no_implementation_exception(env);",
                            f"  auto field_{index} = {_from_name(field.semantic_type)}(field_raw_{index}, env, feature, budget, supernote::conversion::field_path(path, {json.dumps(field.public_name)}), depth + 1);",
                        ])
                values = ", ".join(f"field_{index}" for index in range(len(cpp_value.fields)))
                lines.append(f"  return {cpp_value.named_type.cpp_type}{{{values}}};")
            else:
                raise CrossFamilyCodegenError("native objects cannot use copied converters")
        lines.append("}")
        return "\n".join(lines)


def build_cross_family_renderer(
    module_root: Path,
    api: SemanticApi,
    manifest: JvmSourceManifest,
    *,
    feature_id: str,
    module_name: str,
) -> CrossFamilyRenderer:
    """Build and validate the shared internal copied-route renderer."""

    functions = scan_cpp_source_model(module_root, module_name=module_name)
    classes = scan_cpp_class_source_model(module_root, module_name=module_name)
    enums = scan_cpp_enum_source_model(module_root, module_name=module_name)
    return CrossFamilyRenderer(
        api,
        plan_cpp_routes(api, functions, classes, enums),
        plan_jvm_routes(api, manifest.owners),
        feature_id,
    )


__all__ = [
    "CrossFamilyCodegenError",
    "CrossFamilyRenderer",
    "build_cross_family_renderer",
]
