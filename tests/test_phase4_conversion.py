from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
import math
import random

import pytest

from supernote_module_generator.conversion import (
    ARRAY_HOLE,
    UNDEFINED,
    AllocationFaultInjector,
    ConversionAllocationError,
    ConversionBudget,
    ConversionLimits,
    ConversionNodeKind,
    ConversionPlanError,
    ConversionRangeError,
    ConversionTypeError,
    JsBigInt,
    JsUint8Array,
    NativeObjectToken,
    accept_transactionally,
    assign_transactionally,
    construct_transactionally,
    invoke_transactionally,
    plan_api_conversion,
    plan_binding_conversion,
    prepare_arguments,
    prepare_result,
)
from supernote_module_generator.lowering import (
    CppFunctionRoute,
    JvmMethodRoute,
    LoweringError,
    LoweringPlan,
    RouteKind,
    SchedulingKind,
)
from supernote_module_generator.semantic import (
    BackendFamily,
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    ExecutionMode,
    SemanticApi,
    SemanticBinding,
    SemanticConstructor,
    SemanticEnumDeclaration,
    SemanticField,
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticProjection,
    SemanticType,
    SemanticValueDeclaration,
    SourceProvenance,
    semantic_type_id,
)


FEATURE = "supernote:feature:conversion"


def source(identity: str) -> SourceProvenance:
    return SourceProvenance(identity, "cpp", f"{identity}.hpp", 7)


def projection(identity: str) -> SemanticProjection:
    return SemanticProjection(BackendFamily.CPP, source(identity))


def field(owner: str, name: str, semantic_type: SemanticType) -> SemanticField:
    return SemanticField(
        f"{owner}:field:{name}", owner, name, semantic_type, source(f"field-{name}"), False
    )


def conversion_api(*, limits: ConversionLimits | None = None):
    color_id = semantic_type_id(FEATURE, "Color")
    point_id = semantic_type_id(FEATURE, "Point")
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    payload_id = semantic_type_id(FEATURE, "Payload")
    color = SemanticEnumDeclaration(
        FEATURE, color_id, "Color", ("RED", "BLUE"), (projection("color"),)
    )
    point = SemanticValueDeclaration(
        FEATURE,
        point_id,
        "Point",
        (
            field(point_id, "name", SemanticType.STRING),
            field(point_id, "color", SemanticType.enum_ref(color_id)),
            field(
                point_id,
                "samples",
                SemanticType.array(SemanticType.nullable(SemanticType.INT32)),
            ),
            field(point_id, "blob", SemanticType.BYTES),
        ),
        (projection("point"),),
    )
    stroke = SemanticObjectDeclaration(
        FEATURE, stroke_id, "Stroke", projection("stroke")
    )
    payload = SemanticValueDeclaration(
        FEATURE,
        payload_id,
        "Payload",
        (
            field(payload_id, "points", SemanticType.array(SemanticType.value_ref(point_id))),
            field(
                payload_id,
                "owner",
                SemanticType.nullable(SemanticType.object_ref(stroke_id)),
            ),
        ),
        (projection("payload"),),
    )
    binding = SemanticBinding(
        "binding:convert",
        BindingKind.FUNCTION,
        "convert",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.SYNC,
        (
            SemanticParameter("payload", SemanticType.value_ref(payload_id)),
            SemanticParameter("revision", SemanticType.INT64),
        ),
        SemanticType.value_ref(payload_id),
        source("convert"),
    )
    api = SemanticApi(functions=(binding,), declarations=(payload, stroke, point, color))
    return api, binding, plan_binding_conversion(
        api, binding, limits=limits or ConversionLimits()
    )


def point(name: str = "ink", *, extra: object = 42) -> dict[str, object]:
    return {
        "name": name,
        "color": "RED",
        "samples": [1, None, 3],
        "blob": JsUint8Array(b"PREFIX-visible-SUFFIX", 7, 7),
        "ignored": extra,
    }


def test_plan_is_recursive_deterministic_and_contains_one_limits_contract():
    _, binding, plan = conversion_api()
    assert plan.binding_id == binding.binding_id
    payload = plan.parameters[0].node
    assert payload.kind is ConversionNodeKind.VALUE
    assert [item.name for item in payload.fields] == ["points", "owner"]
    points = payload.fields[0].node
    assert points.kind is ConversionNodeKind.ARRAY
    assert points.element.kind is ConversionNodeKind.VALUE
    samples = points.element.fields[2].node
    assert samples.kind is ConversionNodeKind.ARRAY
    assert samples.element.kind is ConversionNodeKind.NULLABLE
    assert plan.manifest() == conversion_api()[2].manifest()
    assert plan.manifest()["limits"] == ConversionLimits().manifest()


def test_cpp_and_jvm_recursive_lowerings_require_and_share_the_same_plan():
    _, binding, conversion = conversion_api()
    without = LoweringPlan(
        binding.binding_id,
        binding.source.declaration_id,
        RouteKind.DIRECT_CPP_FUNCTION,
        SchedulingKind.INLINE,
        CppFunctionRoute("convert"),
    )
    with pytest.raises(LoweringError, match="shared binding conversion plan"):
        without.validate_binding(binding)

    cpp = LoweringPlan(
        binding.binding_id,
        binding.source.declaration_id,
        RouteKind.DIRECT_CPP_FUNCTION,
        SchedulingKind.INLINE,
        CppFunctionRoute("convert"),
        conversion,
    )
    jvm = LoweringPlan(
        binding.binding_id,
        binding.source.declaration_id,
        RouteKind.JVM_FUNCTION,
        SchedulingKind.INLINE,
        JvmMethodRoute("FeatureApi", "convert", "()V", "adapter"),
        conversion,
    )
    cpp.validate_binding(binding)
    jvm.validate_binding(binding)
    assert cpp.conversion.manifest() == jvm.conversion.manifest()


def test_lowering_rejects_a_conversion_plan_for_another_semantic_signature():
    _, binding, conversion = conversion_api()
    route = LoweringPlan(
        binding.binding_id,
        binding.source.declaration_id,
        RouteKind.DIRECT_CPP_FUNCTION,
        SchedulingKind.INLINE,
        CppFunctionRoute("convert"),
        replace(conversion, result_type=SemanticType.STRING),
    )
    with pytest.raises(LoweringError, match="signature disagrees"):
        route.validate_binding(binding)


def test_api_plan_covers_free_methods_constructors_and_live_fields():
    api, binding, _ = conversion_api()
    stroke = next(
        item for item in api.declarations if isinstance(item, SemanticObjectDeclaration)
    )
    payload_id = semantic_type_id(FEATURE, "Payload")
    method = SemanticBinding(
        "binding:stroke-transform",
        BindingKind.OBJECT_METHOD,
        "transform",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.SYNC,
        (SemanticParameter("payload", SemanticType.value_ref(payload_id)),),
        SemanticType.object_ref(stroke.type_id),
        source("stroke-transform"),
        stroke.type_id,
        stroke.name,
    )
    live_field = SemanticField(
        f"{stroke.type_id}:field:payload",
        stroke.type_id,
        "payload",
        SemanticType.value_ref(payload_id),
        source("stroke-payload"),
        True,
    )
    extended = SemanticObjectDeclaration(
        stroke.feature_id,
        stroke.type_id,
        stroke.name,
        stroke.projection,
        SemanticConstructor(
            source("stroke-constructor"),
            (SemanticParameter("payload", SemanticType.value_ref(payload_id)),),
        ),
        (method,),
        (live_field,),
    )
    planned_api = SemanticApi(
        functions=api.functions,
        declarations=tuple(
            extended if item is stroke else item for item in api.declarations
        ),
    )
    planned = plan_api_conversion(planned_api)
    assert {item.binding_id for item in planned.bindings} == {
        binding.binding_id,
        method.binding_id,
    }
    assert [item.type_id for item in planned.constructors] == [stroke.type_id]
    assert any(item.field_id == live_field.field_id for item in planned.fields)


def test_input_snapshot_copies_only_visible_bytes_and_retains_nested_object_leaves():
    _, _, plan = conversion_api()
    token = NativeObjectToken(
        semantic_type_id(FEATURE, "Stroke"), "cpp", object()
    )
    source_point = point()
    arguments = [{"points": [source_point], "owner": token}, JsBigInt(9)]
    prepared = prepare_arguments(plan, arguments, public_path="Drawing.convert")

    payload = prepared.values[0]
    assert payload["points"][0]["blob"] == b"visible"
    assert payload["points"][0]["samples"] == (1, None, 3)
    assert payload["owner"] is token
    assert prepared.values[1] == 9
    assert prepared.retained_objects == (token,)
    source_point["samples"].append(99)
    assert payload["points"][0]["samples"] == (1, None, 3)


class DeclaredOnlyMapping(Mapping[str, object]):
    def __init__(self, values: dict[str, object]):
        self.values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> object:
        self.reads.append(key)
        if key == "ignored":
            raise AssertionError("undeclared property was inspected")
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("value conversion enumerated JavaScript properties")

    def __len__(self) -> int:
        raise AssertionError("value conversion measured the dynamic object")


def test_declared_value_conversion_never_enumerates_or_reads_unknown_properties():
    _, _, plan = conversion_api()
    wrapped_point = DeclaredOnlyMapping(point())
    payload = DeclaredOnlyMapping({"points": [wrapped_point], "owner": None})
    prepared = prepare_arguments(
        plan,
        [payload, JsBigInt(1)],
        public_path="Drawing.convert",
    )
    assert set(payload.reads) == {"points", "owner"}
    assert set(wrapped_point.reads) == {"name", "color", "samples", "blob"}
    assert prepared.values[0]["points"][0]["name"] == "ink"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["points"][0].pop("name"), r"points\[0\]\.name.*missing"),
        (lambda value: value["points"][0].__setitem__("color", "GREEN"), r"color.*Color"),
        (lambda value: value["points"][0]["samples"].__setitem__(1, UNDEFINED), r"samples\[1\].*undefined"),
        (lambda value: value["points"][0]["samples"].__setitem__(1, ARRAY_HOLE), r"samples\[1\].*hole"),
        (lambda value: value.__setitem__("owner", NativeObjectToken(semantic_type_id(FEATURE, "Other"), "cpp", object())), r"owner.*Stroke"),
    ],
)
def test_nested_type_failures_have_exact_field_and_index_paths(mutation, message):
    _, _, plan = conversion_api()
    value = {"points": [point()], "owner": None}
    mutation(value)
    with pytest.raises(ConversionTypeError, match=message):
        prepare_arguments(plan, [value, JsBigInt(1)], public_path="Drawing.convert")


def test_dense_arrays_reject_holes_even_when_elements_are_nullable():
    _, _, plan = conversion_api()
    value = {"points": [point()], "owner": None}
    value["points"][0]["samples"] = [None, ARRAY_HOLE]
    with pytest.raises(ConversionTypeError, match=r"samples\[1\].*hole"):
        prepare_arguments(plan, [value, JsBigInt(1)], public_path="Drawing.convert")


def test_output_conversion_creates_fresh_containers_bigints_and_uint8arrays():
    _, _, plan = conversion_api()
    token = NativeObjectToken(semantic_type_id(FEATURE, "Stroke"), "cpp", object())
    native_point = {
        "name": "ink",
        "color": "BLUE",
        "samples": (1, None, 3),
        "blob": bytearray(b"abc"),
    }
    native = {"points": (native_point,), "owner": token}
    prepared = prepare_result(plan, native, public_path="Drawing.convert")
    assert prepared.value is not native
    assert prepared.value["points"] is not native["points"]
    assert prepared.value["points"][0] is not native_point
    assert prepared.value["points"][0]["samples"] == [1, None, 3]
    assert prepared.value["points"][0]["blob"] == JsUint8Array(b"abc")
    assert prepared.retained_objects == (token,)


def test_numeric_and_utf8_rules_preserve_v4_visible_behavior():
    _, _, plan = conversion_api()
    value = {"points": [point("שלום")], "owner": None}
    prepared = prepare_arguments(plan, [value, JsBigInt(-(1 << 63))], public_path="x")
    assert prepared.values[0]["points"][0]["name"] == "שלום"
    assert prepared.values[1] == -(1 << 63)

    value["points"][0]["samples"] = [1.5]
    with pytest.raises(ConversionRangeError, match="finite and integral"):
        prepare_arguments(plan, [value, JsBigInt(0)], public_path="x")
    with pytest.raises(ConversionRangeError, match="out of range"):
        prepare_arguments(plan, [{"points": [point()], "owner": None}, JsBigInt(1 << 63)], public_path="x")

    # Existing floating-point behavior permits non-finite JS numbers while
    # still rejecting finite float32 overflow.
    float_api = SemanticApi()
    for number in (math.inf, -math.inf, math.nan):
        node = plan_binding_conversion(
            float_api,
            SemanticBinding(
                f"float:{number}", BindingKind.FUNCTION, "f",
                BindingCapabilities.for_role(DeclarationRole.EXPORTED),
                ExecutionMode.SYNC, (SemanticParameter("x", SemanticType.FLOAT32),),
                SemanticType.VOID, source(f"float-{number}"),
            ),
        )
        assert math.isnan(prepare_arguments(node, [number], public_path="f").values[0]) if math.isnan(number) else prepare_arguments(node, [number], public_path="f").values[0] == number


@pytest.mark.parametrize(
    ("limits", "change", "message"),
    [
        (ConversionLimits(max_depth=3), lambda value: None, "depth exceeds"),
        (ConversionLimits(max_array_length=1), lambda value: value["points"].append(point()), "array length"),
        (ConversionLimits(max_visited_nodes=4), lambda value: None, "visits exceed"),
        (ConversionLimits(max_string_bytes=3), lambda value: value["points"][0].__setitem__("name", "four"), "UTF-8 string"),
        (ConversionLimits(max_byte_buffer_bytes=2), lambda value: None, "byte buffer"),
        (ConversionLimits(max_temporary_bytes=16), lambda value: None, "temporary allocation"),
    ],
)
def test_every_conversion_budget_fails_closed_with_a_range_error(limits, change, message):
    _, _, plan = conversion_api(limits=limits)
    value = {"points": [point()], "owner": None}
    change(value)
    with pytest.raises(ConversionRangeError, match=message):
        prepare_arguments(plan, [value, JsBigInt(1)], public_path="Drawing.convert")


def test_budget_counter_addition_is_overflow_safe():
    budget = ConversionBudget(ConversionLimits())
    budget.temporary_bytes = budget._MAX_COUNTER
    with pytest.raises(ConversionRangeError, match="counter overflow"):
        budget.reserve(1, "Drawing.convert.argument[0]")


@pytest.mark.parametrize("fail_after", range(7))
def test_allocation_failure_before_completion_never_invokes_or_accepts_work(fail_after):
    _, _, plan = conversion_api()
    value = {"points": [point()], "owner": None}
    calls: list[object] = []
    injector = AllocationFaultInjector(fail_after=fail_after)
    with pytest.raises(ConversionAllocationError):
        accept_transactionally(
            plan,
            [value, JsBigInt(1)],
            calls.append,
            public_path="Drawing.convert",
            injector=injector,
        )
    assert calls == []


@pytest.mark.parametrize("fail_after", range(7))
def test_allocation_failure_never_invokes_sync_code_or_constructor(fail_after):
    api, _, plan = conversion_api()
    value = {"points": [point()], "owner": None}
    sync_calls: list[object] = []
    with pytest.raises(ConversionAllocationError):
        invoke_transactionally(
            plan,
            [value, JsBigInt(1)],
            lambda *arguments: sync_calls.append(arguments),
            public_path="Drawing.convert",
            injector=AllocationFaultInjector(fail_after=fail_after),
        )
    assert sync_calls == []

    payload_id = semantic_type_id(FEATURE, "Payload")
    constructor_plan = plan_api_conversion(
        SemanticApi(
            declarations=tuple(
                SemanticObjectDeclaration(
                    item.feature_id,
                    item.type_id,
                    item.name,
                    item.projection,
                    SemanticConstructor(
                        source("constructor"),
                        (SemanticParameter("payload", SemanticType.value_ref(payload_id)),),
                    ),
                )
                if isinstance(item, SemanticObjectDeclaration)
                else item
                for item in api.declarations
            )
        )
    ).constructors[0]
    constructor_calls: list[object] = []
    with pytest.raises(ConversionAllocationError):
        construct_transactionally(
            constructor_plan,
            [value],
            lambda *arguments: constructor_calls.append(arguments),
            public_path="Stroke.create",
            injector=AllocationFaultInjector(fail_after=fail_after),
        )
    assert constructor_calls == []


def test_conversion_limits_fit_the_shared_signed_64_bit_counter():
    with pytest.raises(ConversionPlanError, match="signed 64-bit"):
        ConversionLimits(max_temporary_bytes=1 << 63)


def test_field_assignment_occurs_once_only_after_complete_validation():
    api, _, plan = conversion_api()
    payload_node = plan.parameters[0].node
    state = {"value": "unchanged", "sets": 0}

    def setter(value):
        state["sets"] += 1
        state["value"] = value

    invalid = {"points": [point()], "owner": UNDEFINED}
    with pytest.raises(ConversionTypeError):
        assign_transactionally(
            payload_node, invalid, setter, public_path="Stroke.payload"
        )
    assert state == {"value": "unchanged", "sets": 0}

    valid = {"points": [point()], "owner": None}
    assigned = assign_transactionally(
        payload_node, valid, setter, public_path="Stroke.payload"
    )
    assert state["sets"] == 1
    assert state["value"] == assigned.value
    assert state["value"] is not valid
    assert api.declarations


def test_seeded_nested_array_fuzz_never_loses_the_failing_path():
    _, _, plan = conversion_api()
    rng = random.Random(0xC0A7)
    for iteration in range(10_000):
        length = rng.randrange(0, 8)
        samples: list[object] = [rng.randrange(-(1 << 31), 1 << 31) for _ in range(length)]
        bad_index = None
        if samples and rng.random() < 0.7:
            bad_index = rng.randrange(len(samples))
            samples[bad_index] = rng.choice([UNDEFINED, ARRAY_HOLE, "wrong", 1.5])
        value = {"points": [point()], "owner": None}
        value["points"][0]["samples"] = samples
        if bad_index is None:
            prepare_arguments(plan, [value, JsBigInt(iteration)], public_path="fuzz")
        else:
            with pytest.raises((ConversionTypeError, ConversionRangeError)) as raised:
                prepare_arguments(plan, [value, JsBigInt(iteration)], public_path="fuzz")
            assert f"samples[{bad_index}]" in str(raised.value)
