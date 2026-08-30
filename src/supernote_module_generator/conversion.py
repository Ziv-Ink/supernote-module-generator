"""Shared JavaScript validation and transactional conversion planning.

The semantic plan in this module is backend-neutral. C++ and JVM lowering may
choose different native storage, but they must consume this exact tree so null,
array, enum, value-field, object-leaf, path, and budget behavior cannot drift.
The executable Python snapshot engine is also the reference oracle used by
generated C++/JVM harnesses and failure-injection tests.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import math
import struct
from typing import Callable, Optional, Tuple

from .semantic import (
    SemanticApi,
    SemanticBinding,
    SemanticEnumDeclaration,
    SemanticObjectDeclaration,
    SemanticValueDeclaration,
)
from .semantic_types import ScalarKind, SemanticType, SemanticTypeKind


class ConversionPlanError(ValueError):
    """Raised when common semantics cannot form a conversion plan."""


class ConversionTypeError(TypeError):
    """Reference equivalent of the generated JavaScript TypeError."""


class ConversionRangeError(ValueError):
    """Reference equivalent of the generated JavaScript RangeError."""


class ConversionAllocationError(MemoryError):
    """Injected or real temporary-allocation failure before publication."""


class ConversionDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class ConversionNodeKind(str, Enum):
    VOID = "void"
    SCALAR = "scalar"
    ENUM = "enum"
    VALUE = "value"
    OBJECT = "object"
    ARRAY = "array"
    NULLABLE = "nullable"


@dataclass(frozen=True)
class ConversionLimits:
    """One consistent set of overflow-safe temporary-conversion limits."""

    max_depth: int = 32
    max_array_length: int = 65_536
    max_visited_nodes: int = 262_144
    max_string_bytes: int = 8 * 1024 * 1024
    max_byte_buffer_bytes: int = 32 * 1024 * 1024
    max_temporary_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in self.manifest().items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConversionPlanError(f"conversion limit {name} must be positive")
            if value > (1 << 63) - 1:
                raise ConversionPlanError(
                    f"conversion limit {name} exceeds the shared signed 64-bit range"
                )

    def manifest(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_array_length": self.max_array_length,
            "max_visited_nodes": self.max_visited_nodes,
            "max_string_bytes": self.max_string_bytes,
            "max_byte_buffer_bytes": self.max_byte_buffer_bytes,
            "max_temporary_bytes": self.max_temporary_bytes,
        }


DEFAULT_CONVERSION_LIMITS = ConversionLimits()


@dataclass(frozen=True)
class ConversionField:
    name: str
    node: "ConversionNode"

    def manifest(self) -> dict[str, object]:
        return {"name": self.name, "node": self.node.manifest()}


@dataclass(frozen=True)
class ConversionNode:
    kind: ConversionNodeKind
    expected: str
    scalar: Optional[ScalarKind] = None
    type_id: Optional[str] = None
    public_name: Optional[str] = None
    constants: Tuple[str, ...] = ()
    fields: Tuple[ConversionField, ...] = ()
    element: Optional["ConversionNode"] = None

    def __post_init__(self) -> None:
        if not self.expected:
            raise ConversionPlanError("conversion nodes require a public expectation")
        if self.kind is ConversionNodeKind.SCALAR:
            if self.scalar is None:
                raise ConversionPlanError("scalar conversion nodes require a scalar")
        elif self.scalar is not None:
            raise ConversionPlanError("only scalar conversion nodes carry a scalar")
        if self.kind in {
            ConversionNodeKind.ENUM,
            ConversionNodeKind.VALUE,
            ConversionNodeKind.OBJECT,
        }:
            if not self.type_id or not self.public_name:
                raise ConversionPlanError("named conversion nodes require identity and name")
        elif self.type_id is not None or self.public_name is not None:
            raise ConversionPlanError("unnamed conversion nodes cannot carry type identity")
        if self.kind is ConversionNodeKind.ENUM:
            if not self.constants:
                raise ConversionPlanError("enum conversion nodes require constants")
        elif self.constants:
            raise ConversionPlanError("only enum conversion nodes carry constants")
        if self.kind is ConversionNodeKind.VALUE:
            if not self.fields:
                raise ConversionPlanError("value conversion nodes require fields")
        elif self.fields:
            raise ConversionPlanError("only value conversion nodes carry fields")
        if self.kind in {ConversionNodeKind.ARRAY, ConversionNodeKind.NULLABLE}:
            if self.element is None:
                raise ConversionPlanError("wrapper conversion nodes require an element")
        elif self.element is not None:
            raise ConversionPlanError("non-wrapper conversion nodes forbid an element")

    def manifest(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": self.kind.value,
            "expected": self.expected,
        }
        if self.scalar is not None:
            value["scalar"] = self.scalar.value
        if self.type_id is not None:
            value["type_id"] = self.type_id
            value["public_name"] = self.public_name
        if self.constants:
            value["constants"] = list(self.constants)
        if self.fields:
            value["fields"] = [item.manifest() for item in self.fields]
        if self.element is not None:
            value["element"] = self.element.manifest()
        return value


@dataclass(frozen=True)
class ParameterConversion:
    name: str
    semantic_type: SemanticType
    node: ConversionNode

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "semantic_type": self.semantic_type.manifest(),
            "node": self.node.manifest(),
        }


@dataclass(frozen=True)
class BindingConversionPlan:
    binding_id: str
    parameters: Tuple[ParameterConversion, ...]
    result_type: SemanticType
    result: ConversionNode
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ConversionPlanError("binding conversion identity cannot be empty")
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ConversionPlanError("binding conversion parameters must be unique")

    def manifest(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "parameters": [item.manifest() for item in self.parameters],
            "result_type": self.result_type.manifest(),
            "result": self.result.manifest(),
            "limits": self.limits.manifest(),
        }

    def validate_binding(self, binding: SemanticBinding) -> None:
        if binding.binding_id != self.binding_id:
            raise ConversionPlanError("conversion plan references another binding")
        expected_parameters = tuple(item.name for item in binding.parameters)
        actual_parameters = tuple(item.name for item in self.parameters)
        if actual_parameters != expected_parameters:
            raise ConversionPlanError("conversion parameter order disagrees with semantics")
        actual_types = tuple(item.semantic_type for item in self.parameters)
        expected_types = tuple(item.type for item in binding.parameters)
        if actual_types != expected_types or self.result_type != binding.result:
            raise ConversionPlanError("conversion signature disagrees with semantics")


@dataclass(frozen=True)
class ConstructorConversionPlan:
    type_id: str
    public_name: str
    parameters: Tuple[ParameterConversion, ...]
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS

    def manifest(self) -> dict[str, object]:
        return {
            "type_id": self.type_id,
            "public_name": self.public_name,
            "parameters": [item.manifest() for item in self.parameters],
            "limits": self.limits.manifest(),
        }


@dataclass(frozen=True)
class FieldConversionPlan:
    field_id: str
    owner_id: str
    public_name: str
    mutable: bool
    node: ConversionNode

    def manifest(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "owner_id": self.owner_id,
            "public_name": self.public_name,
            "mutable": self.mutable,
            "node": self.node.manifest(),
        }


@dataclass(frozen=True)
class ApiConversionPlan:
    bindings: Tuple[BindingConversionPlan, ...]
    constructors: Tuple[ConstructorConversionPlan, ...]
    fields: Tuple[FieldConversionPlan, ...]
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "kind": "supernote_module_conversion_plan",
            "limits": self.limits.manifest(),
            "bindings": [item.manifest() for item in self.bindings],
            "constructors": [item.manifest() for item in self.constructors],
            "fields": [item.manifest() for item in self.fields],
        }


def plan_api_conversion(
    api: SemanticApi,
    *,
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS,
) -> ApiConversionPlan:
    declarations = {item.type_id: item for item in api.declarations}
    bindings = list(api.functions)
    bindings.extend(
        method
        for item in api.declarations
        if isinstance(item, SemanticObjectDeclaration)
        for method in item.methods
    )
    constructors = []
    fields = []
    for item in api.declarations:
        if isinstance(item, SemanticObjectDeclaration) and item.constructor is not None:
            constructors.append(
                ConstructorConversionPlan(
                    item.type_id,
                    item.name,
                    tuple(
                        ParameterConversion(
                            parameter.name,
                            parameter.type,
                            _plan_type(
                                parameter.type,
                                declarations,
                                active_values=(),
                            ),
                        )
                        for parameter in item.constructor.parameters
                    ),
                    limits,
                )
            )
        if isinstance(item, (SemanticObjectDeclaration, SemanticValueDeclaration)):
            fields.extend(
                FieldConversionPlan(
                    value.field_id,
                    value.owner_id,
                    value.name,
                    value.mutable,
                    _plan_type(value.type, declarations, active_values=()),
                )
                for value in item.fields
            )
    return ApiConversionPlan(
        tuple(
            plan_binding_conversion(api, binding, limits=limits)
            for binding in sorted(bindings, key=lambda value: value.binding_id)
        ),
        tuple(sorted(constructors, key=lambda value: value.type_id)),
        tuple(sorted(fields, key=lambda value: value.field_id)),
        limits,
    )


def plan_binding_conversion(
    api: SemanticApi,
    binding: SemanticBinding,
    *,
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS,
) -> BindingConversionPlan:
    declarations = {item.type_id: item for item in api.declarations}
    plan = BindingConversionPlan(
        binding.binding_id,
        tuple(
            ParameterConversion(
                item.name,
                item.type,
                _plan_type(item.type, declarations, active_values=()),
            )
            for item in binding.parameters
        ),
        binding.result,
        _plan_type(binding.result, declarations, active_values=()),
        limits,
    )
    plan.validate_binding(binding)
    return plan


def plan_type_conversion(api: SemanticApi, semantic_type: SemanticType) -> ConversionNode:
    return _plan_type(
        semantic_type,
        {item.type_id: item for item in api.declarations},
        active_values=(),
    )


def _plan_type(
    semantic_type: SemanticType,
    declarations: dict[str, object],
    *,
    active_values: Tuple[str, ...],
) -> ConversionNode:
    if semantic_type.kind is SemanticTypeKind.VOID:
        return ConversionNode(ConversionNodeKind.VOID, "void")
    if semantic_type.kind is SemanticTypeKind.SCALAR:
        assert semantic_type.scalar is not None
        return ConversionNode(
            ConversionNodeKind.SCALAR,
            _SCALAR_EXPECTED[semantic_type.scalar],
            scalar=semantic_type.scalar,
        )
    if semantic_type.kind in {SemanticTypeKind.ARRAY, SemanticTypeKind.NULLABLE}:
        assert semantic_type.element is not None
        child = _plan_type(
            semantic_type.element,
            declarations,
            active_values=active_values,
        )
        if semantic_type.kind is SemanticTypeKind.ARRAY:
            return ConversionNode(
                ConversionNodeKind.ARRAY,
                f"dense Array<{child.expected}>",
                element=child,
            )
        return ConversionNode(
            ConversionNodeKind.NULLABLE,
            f"{child.expected} or null",
            element=child,
        )
    assert semantic_type.type_id is not None
    declaration = declarations.get(semantic_type.type_id)
    if declaration is None:
        raise ConversionPlanError(
            f"conversion references unknown type {semantic_type.type_id!r}"
        )
    if isinstance(declaration, SemanticEnumDeclaration):
        if semantic_type.kind is not SemanticTypeKind.ENUM_REF:
            raise ConversionPlanError("enum declaration has a non-enum reference")
        return ConversionNode(
            ConversionNodeKind.ENUM,
            declaration.name,
            type_id=declaration.type_id,
            public_name=declaration.name,
            constants=declaration.constants,
        )
    if isinstance(declaration, SemanticObjectDeclaration):
        if semantic_type.kind is not SemanticTypeKind.OBJECT_REF:
            raise ConversionPlanError("object declaration has a non-object reference")
        return ConversionNode(
            ConversionNodeKind.OBJECT,
            declaration.name,
            type_id=declaration.type_id,
            public_name=declaration.name,
        )
    if not isinstance(declaration, SemanticValueDeclaration):
        raise ConversionPlanError("unsupported semantic declaration in conversion plan")
    if semantic_type.kind is not SemanticTypeKind.VALUE_REF:
        raise ConversionPlanError("value declaration has a non-value reference")
    if declaration.type_id in active_values:
        raise ConversionPlanError(
            "recursive value conversion graph: "
            + " -> ".join((*active_values, declaration.type_id))
        )
    nested_active = (*active_values, declaration.type_id)
    return ConversionNode(
        ConversionNodeKind.VALUE,
        declaration.name,
        type_id=declaration.type_id,
        public_name=declaration.name,
        fields=tuple(
            ConversionField(
                item.name,
                _plan_type(item.type, declarations, active_values=nested_active),
            )
            for item in declaration.fields
        ),
    )


_SCALAR_EXPECTED = {
    ScalarKind.BOOL: "boolean",
    ScalarKind.INT32: "int32 number",
    ScalarKind.INT64: "int64 bigint",
    ScalarKind.FLOAT32: "float32 number",
    ScalarKind.FLOAT64: "float64 number",
    ScalarKind.STRING: "string",
    ScalarKind.BYTES: "Uint8Array",
}


class _Undefined:
    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()
ARRAY_HOLE = _Undefined()


@dataclass(frozen=True)
class JsBigInt:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError("JsBigInt requires an integer")


@dataclass(frozen=True)
class JsUint8Array:
    buffer: bytes
    byte_offset: int = 0
    byte_length: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.buffer, bytes):
            raise TypeError("JsUint8Array buffer must be bytes")
        length = len(self.buffer) - self.byte_offset if self.byte_length is None else self.byte_length
        if (
            not isinstance(self.byte_offset, int)
            or isinstance(self.byte_offset, bool)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or self.byte_offset < 0
            or length < 0
            or self.byte_offset > len(self.buffer)
            or length > len(self.buffer) - self.byte_offset
        ):
            raise ValueError("Uint8Array view exceeds its ArrayBuffer")
        object.__setattr__(self, "byte_length", length)

    def visible_bytes(self) -> bytes:
        assert self.byte_length is not None
        return self.buffer[self.byte_offset : self.byte_offset + self.byte_length]


@dataclass(frozen=True)
class NativeObjectToken:
    type_id: str
    backend_family: str
    instance: object

    def __post_init__(self) -> None:
        if not self.type_id or self.backend_family not in {"cpp", "jvm"}:
            raise ValueError("native object tokens require nominal type and backend")


@dataclass(frozen=True)
class PreparedValue:
    value: object
    retained_objects: Tuple[NativeObjectToken, ...]


@dataclass(frozen=True)
class PreparedArguments:
    values: Tuple[object, ...]
    retained_objects: Tuple[NativeObjectToken, ...]


@dataclass
class AllocationFaultInjector:
    """Fail the Nth temporary reservation; zero fails the first reservation."""

    fail_after: Optional[int] = None
    reservations: int = 0

    def reserve(self, path: str) -> None:
        if self.fail_after is not None and self.reservations >= self.fail_after:
            raise ConversionAllocationError(
                f"{path}: injected temporary allocation failure"
            )
        self.reservations += 1


@dataclass
class ConversionBudget:
    limits: ConversionLimits
    injector: AllocationFaultInjector = field(default_factory=AllocationFaultInjector)
    visited_nodes: int = 0
    temporary_bytes: int = 0

    _MAX_COUNTER = (1 << 63) - 1

    def visit(self, path: str, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise ConversionRangeError(
                f"{path}: conversion depth exceeds {self.limits.max_depth}"
            )
        self.visited_nodes = self._checked_add(
            self.visited_nodes, 1, path, "visited-node"
        )
        if self.visited_nodes > self.limits.max_visited_nodes:
            raise ConversionRangeError(
                f"{path}: conversion visits exceed {self.limits.max_visited_nodes}"
            )

    def reserve(self, amount: int, path: str) -> None:
        if amount < 0:
            raise ConversionRangeError(f"{path}: negative allocation size")
        self.injector.reserve(path)
        self.temporary_bytes = self._checked_add(
            self.temporary_bytes, amount, path, "temporary-byte"
        )
        if self.temporary_bytes > self.limits.max_temporary_bytes:
            raise ConversionRangeError(
                f"{path}: temporary allocation exceeds "
                f"{self.limits.max_temporary_bytes} bytes"
            )

    def _checked_add(self, left: int, right: int, path: str, label: str) -> int:
        if left < 0 or right < 0 or right > self._MAX_COUNTER - left:
            raise ConversionRangeError(f"{path}: {label} counter overflow")
        return left + right


def prepare_arguments(
    plan: BindingConversionPlan,
    arguments: Sequence[object],
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedArguments:
    return _prepare_parameters(
        plan.parameters,
        arguments,
        limits=plan.limits,
        public_path=public_path,
        injector=injector,
    )


def prepare_constructor_arguments(
    plan: ConstructorConversionPlan,
    arguments: Sequence[object],
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedArguments:
    return _prepare_parameters(
        plan.parameters,
        arguments,
        limits=plan.limits,
        public_path=public_path,
        injector=injector,
    )


def _prepare_parameters(
    parameters: Sequence[ParameterConversion],
    arguments: Sequence[object],
    *,
    limits: ConversionLimits,
    public_path: str,
    injector: Optional[AllocationFaultInjector],
) -> PreparedArguments:
    if not isinstance(arguments, (list, tuple)):
        raise ConversionTypeError(f"{public_path}: arguments must be an ordered list")
    if len(arguments) != len(parameters):
        raise ConversionTypeError(
            f"{public_path}: expected {len(parameters)} arguments, got {len(arguments)}"
        )
    budget = ConversionBudget(limits, injector or AllocationFaultInjector())
    converted = []
    retained: list[NativeObjectToken] = []
    for index, (parameter, value) in enumerate(zip(parameters, arguments)):
        path = f"{public_path}.argument[{index}]({parameter.name})"
        converted.append(
            _convert(
                parameter.node,
                value,
                ConversionDirection.INPUT,
                path,
                1,
                budget,
                retained,
            )
        )
    return PreparedArguments(tuple(converted), tuple(retained))


def construct_transactionally(
    plan: ConstructorConversionPlan,
    arguments: Sequence[object],
    constructor: Callable[..., object],
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> object:
    """Invoke construction only after every caller-visible input is owned."""

    prepared = prepare_constructor_arguments(
        plan, arguments, public_path=public_path, injector=injector
    )
    return constructor(*prepared.values)


def prepare_result(
    plan: BindingConversionPlan,
    value: object,
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedValue:
    budget = ConversionBudget(plan.limits, injector or AllocationFaultInjector())
    retained: list[NativeObjectToken] = []
    converted = _convert(
        plan.result,
        value,
        ConversionDirection.OUTPUT,
        f"{public_path}.result",
        1,
        budget,
        retained,
    )
    return PreparedValue(converted, tuple(retained))


def invoke_transactionally(
    plan: BindingConversionPlan,
    arguments: Sequence[object],
    implementation: Callable[..., object],
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedValue:
    prepared = prepare_arguments(
        plan, arguments, public_path=public_path, injector=injector
    )
    result = implementation(*prepared.values)
    return prepare_result(plan, result, public_path=public_path, injector=injector)


def accept_transactionally(
    plan: BindingConversionPlan,
    arguments: Sequence[object],
    accept: Callable[[PreparedArguments], None],
    *,
    public_path: str,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedArguments:
    """Prepare a complete retained snapshot before an async queue accepts it."""

    prepared = prepare_arguments(
        plan, arguments, public_path=public_path, injector=injector
    )
    accept(prepared)
    return prepared


def assign_transactionally(
    node: ConversionNode,
    value: object,
    setter: Callable[[object], None],
    *,
    public_path: str,
    limits: ConversionLimits = DEFAULT_CONVERSION_LIMITS,
    injector: Optional[AllocationFaultInjector] = None,
) -> PreparedValue:
    budget = ConversionBudget(limits, injector or AllocationFaultInjector())
    retained: list[NativeObjectToken] = []
    converted = _convert(
        node,
        value,
        ConversionDirection.INPUT,
        public_path,
        1,
        budget,
        retained,
    )
    setter(converted)
    return PreparedValue(converted, tuple(retained))


def _convert(
    node: ConversionNode,
    value: object,
    direction: ConversionDirection,
    path: str,
    depth: int,
    budget: ConversionBudget,
    retained: list[NativeObjectToken],
) -> object:
    budget.visit(path, depth)
    if value is UNDEFINED:
        _type_error(path, node.expected, "undefined")
    if node.kind is ConversionNodeKind.VOID:
        if direction is ConversionDirection.OUTPUT and value is None:
            return None
        _type_error(path, "void", _actual(value))
    if node.kind is ConversionNodeKind.NULLABLE:
        if value is None:
            return None
        assert node.element is not None
        return _convert(
            node.element, value, direction, path, depth + 1, budget, retained
        )
    if value is None:
        _type_error(path, node.expected, "null")
    if node.kind is ConversionNodeKind.SCALAR:
        assert node.scalar is not None
        return _convert_scalar(node.scalar, value, direction, path, budget)
    if node.kind is ConversionNodeKind.ENUM:
        if not isinstance(value, str) or value not in node.constants:
            _type_error(path, node.expected, _actual(value))
        byte_count = len(value.encode("utf-8"))
        _check_string_bytes(byte_count, path, budget)
        budget.reserve(byte_count, path)
        return value[:]
    if node.kind is ConversionNodeKind.OBJECT:
        if not isinstance(value, NativeObjectToken) or value.type_id != node.type_id:
            _type_error(path, node.expected, _actual(value))
        budget.reserve(struct.calcsize("P"), path)
        retained.append(value)
        return value
    if node.kind is ConversionNodeKind.ARRAY:
        valid_array = (
            isinstance(value, list)
            if direction is ConversionDirection.INPUT
            else isinstance(value, (list, tuple))
        )
        if not valid_array:
            _type_error(path, node.expected, _actual(value))
        if len(value) > budget.limits.max_array_length:
            raise ConversionRangeError(
                f"{path}: array length {len(value)} exceeds "
                f"{budget.limits.max_array_length}"
            )
        budget.reserve(24 + len(value) * struct.calcsize("P"), path)
        assert node.element is not None
        result = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if item is ARRAY_HOLE:
                raise ConversionTypeError(f"{item_path}: sparse array hole is invalid")
            result.append(
                _convert(
                    node.element,
                    item,
                    direction,
                    item_path,
                    depth + 1,
                    budget,
                    retained,
                )
            )
        return tuple(result) if direction is ConversionDirection.INPUT else result
    assert node.kind is ConversionNodeKind.VALUE
    if not isinstance(value, Mapping):
        _type_error(path, node.expected, _actual(value))
    budget.reserve(32 + len(node.fields) * 16, path)
    result: dict[str, object] = {}
    for item in node.fields:
        field_path = f"{path}.{item.name}"
        try:
            raw = value[item.name]
        except KeyError:
            raise ConversionTypeError(f"{field_path}: required field is missing")
        result[item.name] = _convert(
            item.node,
            raw,
            direction,
            field_path,
            depth + 1,
            budget,
            retained,
        )
    return result


def _convert_scalar(
    scalar: ScalarKind,
    value: object,
    direction: ConversionDirection,
    path: str,
    budget: ConversionBudget,
) -> object:
    if scalar is ScalarKind.BOOL:
        if type(value) is not bool:
            _type_error(path, "boolean", _actual(value))
        return value
    if scalar is ScalarKind.INT32:
        if type(value) not in {int, float}:
            _type_error(path, "int32 number", _actual(value))
        numeric = float(value)
        if not math.isfinite(numeric) or math.trunc(numeric) != numeric:
            raise ConversionRangeError(f"{path}: int32 value must be finite and integral")
        if numeric < -(1 << 31) or numeric > (1 << 31) - 1:
            raise ConversionRangeError(f"{path}: int32 value is out of range")
        return int(numeric)
    if scalar is ScalarKind.INT64:
        if direction is ConversionDirection.INPUT:
            if not isinstance(value, JsBigInt):
                _type_error(path, "int64 bigint", _actual(value))
            numeric = value.value
        else:
            if type(value) is not int:
                _type_error(path, "native int64", _actual(value))
            numeric = value
        if numeric < -(1 << 63) or numeric > (1 << 63) - 1:
            raise ConversionRangeError(f"{path}: int64 value is out of range")
        return numeric if direction is ConversionDirection.INPUT else JsBigInt(numeric)
    if scalar in {ScalarKind.FLOAT32, ScalarKind.FLOAT64}:
        if type(value) not in {int, float}:
            _type_error(path, _SCALAR_EXPECTED[scalar], _actual(value))
        numeric = float(value)
        if scalar is ScalarKind.FLOAT32 and math.isfinite(numeric):
            if abs(numeric) > 3.4028234663852886e38:
                raise ConversionRangeError(f"{path}: float32 value is out of range")
        return numeric
    if scalar is ScalarKind.STRING:
        if not isinstance(value, str):
            _type_error(path, "string", _actual(value))
        encoded = value.encode("utf-8")
        _check_string_bytes(len(encoded), path, budget)
        budget.reserve(len(encoded), path)
        return encoded.decode("utf-8")
    assert scalar is ScalarKind.BYTES
    if direction is ConversionDirection.INPUT:
        if not isinstance(value, JsUint8Array):
            _type_error(path, "Uint8Array", _actual(value))
        raw = value.visible_bytes()
    else:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            _type_error(path, "native byte buffer", _actual(value))
        raw = bytes(value)
    if len(raw) > budget.limits.max_byte_buffer_bytes:
        raise ConversionRangeError(
            f"{path}: byte buffer exceeds {budget.limits.max_byte_buffer_bytes} bytes"
        )
    budget.reserve(len(raw), path)
    copied = bytes(raw)
    return copied if direction is ConversionDirection.INPUT else JsUint8Array(copied)


def _check_string_bytes(length: int, path: str, budget: ConversionBudget) -> None:
    if length > budget.limits.max_string_bytes:
        raise ConversionRangeError(
            f"{path}: UTF-8 string exceeds {budget.limits.max_string_bytes} bytes"
        )


def _type_error(path: str, expected: str, actual: str) -> None:
    raise ConversionTypeError(f"{path}: expected {expected}, got {actual}")


def _actual(value: object) -> str:
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if value is ARRAY_HOLE:
        return "array hole"
    if isinstance(value, NativeObjectToken):
        return f"native object {value.type_id}"
    if isinstance(value, JsUint8Array):
        return "Uint8Array"
    if isinstance(value, JsBigInt):
        return "bigint"
    if type(value) is bool:
        return "boolean"
    if type(value) in {int, float}:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__
