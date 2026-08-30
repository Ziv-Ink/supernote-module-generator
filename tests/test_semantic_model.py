from __future__ import annotations

import random

import pytest

from supernote_module_generator.feature_model import (
    FeatureRequirements,
    ImplementationFamily,
)
from supernote_module_generator.semantic import (
    BackendFamily,
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    ExecutionMode,
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticConstructor,
    SemanticEnumDeclaration,
    SemanticField,
    SemanticModelError,
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticProjection,
    SemanticType,
    SemanticValueDeclaration,
    SourceProvenance,
    merge_semantic_apis,
    semantic_api_from_manifest,
    semantic_type_id,
    validate_semantic_route,
)
from supernote_module_generator.semantic_types import (
    SemanticTypeError,
    SemanticTypeKind,
    semantic_type_from_manifest,
)


FEATURE = "supernote:feature:geometry"


@pytest.mark.parametrize(
    ("value", "manifest"),
    [
        (SemanticType.VOID, {"kind": "void"}),
        (SemanticType.BOOL, {"kind": "scalar", "name": "bool"}),
        (SemanticType.INT32, {"kind": "scalar", "name": "int32"}),
        (SemanticType.INT64, {"kind": "scalar", "name": "int64"}),
        (SemanticType.FLOAT32, {"kind": "scalar", "name": "float32"}),
        (SemanticType.FLOAT64, {"kind": "scalar", "name": "float64"}),
        (SemanticType.STRING, {"kind": "scalar", "name": "string"}),
        (SemanticType.BYTES, {"kind": "scalar", "name": "bytes"}),
    ],
)
def test_every_base_type_has_an_exact_manifest(value, manifest):
    assert value.manifest() == manifest
    assert semantic_type_from_manifest(manifest) is value


@pytest.mark.parametrize(
    "leaf",
    [
        SemanticType.BOOL,
        SemanticType.INT32,
        SemanticType.INT64,
        SemanticType.FLOAT32,
        SemanticType.FLOAT64,
        SemanticType.STRING,
        SemanticType.BYTES,
        SemanticType.enum_ref("feature:type:Enum"),
        SemanticType.value_ref("feature:type:Value"),
        SemanticType.object_ref("feature:type:Object"),
    ],
)
@pytest.mark.parametrize("wrapper", [SemanticType.array, SemanticType.nullable])
def test_every_non_void_family_is_legal_in_each_direct_wrapper(leaf, wrapper):
    wrapped = wrapper(leaf)
    assert semantic_type_from_manifest(wrapped.manifest()) == wrapped


def source(identity: str, language: str = "cpp", line: int = 10) -> SourceProvenance:
    suffix = "hpp" if language == "cpp" else "kt"
    return SourceProvenance(identity, language, f"src/{identity}.{suffix}", line)


def projection(identity: str, backend: BackendFamily) -> SemanticProjection:
    language = "cpp" if backend is BackendFamily.CPP else "kotlin"
    return SemanticProjection(backend, source(identity, language))


def field(
    owner: str,
    name: str,
    value_type: SemanticType,
    identity: str,
    *,
    language: str = "cpp",
    mutable: bool = False,
) -> SemanticField:
    return SemanticField(
        f"{owner}:field:{name}",
        owner,
        name,
        value_type,
        source(identity, language),
        mutable,
    )


def value_declaration(
    name: str,
    backend: BackendFamily,
    fields: tuple[SemanticField, ...],
    identity: str,
) -> SemanticValueDeclaration:
    type_id = semantic_type_id(FEATURE, name)
    return SemanticValueDeclaration(
        FEATURE,
        type_id,
        name,
        fields,
        (projection(identity, backend),),
    )


def test_recursive_type_algebra_is_immutable_structured_and_strict():
    point_id = semantic_type_id(FEATURE, "Point")
    value = SemanticType.nullable(
        SemanticType.array(SemanticType.value_ref(point_id))
    )
    assert value.manifest() == {
        "kind": "nullable",
        "inner": {
            "kind": "array",
            "element": {"kind": "value_ref", "type_id": point_id},
        },
    }
    assert semantic_type_from_manifest(value.manifest()) == value
    assert semantic_type_from_manifest({"kind": "scalar", "name": "int32"}) \
        is SemanticType.INT32

    with pytest.raises(SemanticTypeError, match="void cannot be nested"):
        SemanticType.array(SemanticType.VOID)
    with pytest.raises(SemanticTypeError, match="nested nullable"):
        SemanticType.nullable(SemanticType.nullable(SemanticType.STRING))
    with pytest.raises(SemanticTypeError, match="invalid fields"):
        semantic_type_from_manifest({"kind": "void", "name": "void"})
    with pytest.raises(SemanticTypeError, match="kind is invalid"):
        semantic_type_from_manifest({"kind": "dynamic"})


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: SemanticType(SemanticTypeKind.SCALAR),
            "a scalar semantic type requires a scalar kind",
        ),
        (
            lambda: SemanticType(
                SemanticTypeKind.SCALAR,
                SemanticType.BOOL.scalar,
                type_id="unexpected",
            ),
            "a scalar semantic type forbids reference payload",
        ),
        (
            lambda: SemanticType(SemanticTypeKind.OBJECT_REF),
            "a named semantic reference requires a type ID",
        ),
        (
            lambda: SemanticType(
                SemanticTypeKind.VALUE_REF,
                scalar=SemanticType.INT32.scalar,
                type_id="feature:type:Value",
            ),
            "a named semantic reference has only a type ID",
        ),
        (
            lambda: SemanticType(SemanticTypeKind.ARRAY),
            "a semantic wrapper requires an element type",
        ),
        (
            lambda: SemanticType(
                SemanticTypeKind.ARRAY,
                scalar=SemanticType.INT32.scalar,
                element=SemanticType.INT32,
            ),
            "a semantic wrapper has only an element type",
        ),
        (
            lambda: SemanticType(SemanticTypeKind.ARRAY, element=SemanticType.VOID),
            "void cannot be nested in a semantic type",
        ),
        (
            lambda: SemanticType(
                SemanticTypeKind.NULLABLE,
                element=SemanticType.nullable(SemanticType.STRING),
            ),
            "nested nullable semantic types are forbidden",
        ),
        (
            lambda: SemanticType(
                SemanticTypeKind.VOID,
                scalar=SemanticType.BOOL.scalar,
            ),
            "void has no semantic type payload",
        ),
    ],
)
def test_semantic_type_node_validation_order_is_exact(factory, message: str):
    with pytest.raises(SemanticTypeError, match=message):
        factory()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "semantic type must be an object"),
        ({}, "semantic type.kind must be a non-empty string"),
        (
            {"kind": "dynamic", "unexpected": True},
            "semantic type.kind is invalid: 'dynamic'",
        ),
        (
            {"kind": "scalar", "unexpected": True},
            "semantic type has invalid fields: missing name; unknown unexpected",
        ),
        (
            {"kind": "scalar", "name": 1},
            "semantic type is invalid: semantic type.name must be a string",
        ),
        (
            {"kind": "object_ref", "type_id": ""},
            "semantic type is invalid: semantic type.type_id must be a non-empty string",
        ),
        (
            {"kind": "array", "element": {"kind": "void", "name": "void"}},
            "semantic type is invalid: semantic type.element has invalid fields",
        ),
    ],
)
def test_semantic_type_manifest_validation_order_is_exact(raw, message: str):
    with pytest.raises(SemanticTypeError, match=message):
        semantic_type_from_manifest(raw)


def test_logical_ids_and_manifests_are_language_neutral_and_deterministic():
    point_id = semantic_type_id(FEATURE, "Point")
    point = value_declaration(
        "Point",
        BackendFamily.CPP,
        (
            field(point_id, "x", SemanticType.FLOAT64, "cpp-point-x"),
            field(point_id, "y", SemanticType.FLOAT64, "cpp-point-y"),
        ),
        "cpp-point",
    )
    api = SemanticApi(declarations=(point,))
    manifest = api.manifest()
    assert manifest["schema_version"] == "1.0"
    assert manifest["types"][0]["type_id"] == point_id
    assert manifest["types"][0]["fields"][0]["type"] == {
        "kind": "scalar",
        "name": "float64",
    }
    assert "descriptor" not in repr(manifest).lower()
    assert "adapter" not in repr(manifest).lower()
    assert semantic_api_from_manifest(manifest).manifest() == manifest

    with pytest.raises(SemanticModelError, match="stable identity"):
        SemanticValueDeclaration(
            FEATURE, "cpp::Point", "Point", point.fields, point.projections
        )


def test_exact_cpp_and_jvm_value_and_enum_projections_merge():
    point_id = semantic_type_id(FEATURE, "Point")
    cpp_fields = (
        field(point_id, "x", SemanticType.FLOAT64, "cpp-x"),
        field(point_id, "y", SemanticType.FLOAT64, "cpp-y"),
    )
    jvm_fields = (
        field(point_id, "x", SemanticType.FLOAT64, "jvm-x", language="kotlin"),
        field(point_id, "y", SemanticType.FLOAT64, "jvm-y", language="kotlin"),
    )
    cpp = value_declaration("Point", BackendFamily.CPP, cpp_fields, "cpp-point")
    jvm = value_declaration("Point", BackendFamily.JVM, jvm_fields, "jvm-point")
    merged = merge_semantic_apis(
        SemanticApi(declarations=(jvm,)), SemanticApi(declarations=(cpp,))
    )
    assert [item.backend for item in merged.declarations[0].projections] == [
        BackendFamily.CPP,
        BackendFamily.JVM,
    ]
    assert [item["backend"] for item in merged.manifest()["types"][0]["projections"]] == [
        "cpp",
        "jvm",
    ]

    enum_id = semantic_type_id(FEATURE, "Color")
    enum_cpp = SemanticEnumDeclaration(
        FEATURE, enum_id, "Color", ("RED", "BLUE"), (projection("cpp-color", BackendFamily.CPP),)
    )
    enum_jvm = SemanticEnumDeclaration(
        FEATURE, enum_id, "Color", ("RED", "BLUE"), (projection("jvm-color", BackendFamily.JVM),)
    )
    assert len(
        merge_semantic_apis(
            SemanticApi(declarations=(enum_cpp,)),
            SemanticApi(declarations=(enum_jvm,)),
        ).declarations[0].projections
    ) == 2


def test_copied_value_projection_merge_ignores_source_storage_mutability():
    point_id = semantic_type_id(FEATURE, "Point")
    cpp = value_declaration(
        "Point",
        BackendFamily.CPP,
        (field(point_id, "x", SemanticType.FLOAT64, "cpp-x", mutable=True),),
        "cpp-point",
    )
    jvm = value_declaration(
        "Point",
        BackendFamily.JVM,
        (
            field(
                point_id,
                "x",
                SemanticType.FLOAT64,
                "jvm-x",
                language="kotlin",
                mutable=False,
            ),
        ),
        "jvm-point",
    )
    merged = merge_semantic_apis(
        SemanticApi(declarations=(cpp,)), SemanticApi(declarations=(jvm,))
    )
    assert len(merged.declarations[0].projections) == 2


def test_projection_merge_reports_both_sources_for_every_conflict():
    enum_id = semantic_type_id(FEATURE, "Color")
    cpp = SemanticEnumDeclaration(
        FEATURE, enum_id, "Color", ("RED", "BLUE"), (projection("cpp-color", BackendFamily.CPP),)
    )
    duplicate = SemanticEnumDeclaration(
        FEATURE, enum_id, "Color", ("RED", "BLUE"), (projection("cpp-color-2", BackendFamily.CPP),)
    )
    mismatch = SemanticEnumDeclaration(
        FEATURE, enum_id, "Color", ("BLUE", "RED"), (projection("jvm-color", BackendFamily.JVM),)
    )
    with pytest.raises(SemanticModelError, match=r"cpp-color.*cpp-color-2"):
        merge_semantic_apis(
            SemanticApi(declarations=(cpp,)), SemanticApi(declarations=(duplicate,))
        )
    with pytest.raises(SemanticModelError, match=r"cpp-color.*jvm-color"):
        merge_semantic_apis(
            SemanticApi(declarations=(cpp,)), SemanticApi(declarations=(mismatch,))
        )

    point_id = semantic_type_id(FEATURE, "Point")
    point_cpp = value_declaration(
        "Point",
        BackendFamily.CPP,
        (
            field(point_id, "x", SemanticType.FLOAT64, "point-cpp-x"),
            field(point_id, "y", SemanticType.FLOAT64, "point-cpp-y"),
        ),
        "point-cpp",
    )
    point_jvm = value_declaration(
        "Point",
        BackendFamily.JVM,
        (
            field(point_id, "x", SemanticType.FLOAT32, "point-jvm-x", language="java"),
            field(point_id, "y", SemanticType.FLOAT64, "point-jvm-y", language="java"),
        ),
        "point-jvm",
    )
    with pytest.raises(SemanticModelError, match=r"point-cpp.*point-jvm"):
        merge_semantic_apis(
            SemanticApi(declarations=(point_cpp,)),
            SemanticApi(declarations=(point_jvm,)),
        )


def test_optional_object_construction_member_scope_fields_and_exact_nominality():
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    label = field(
        stroke_id, "label", SemanticType.STRING, "stroke-label", mutable=True
    )
    static_factory = SemanticBinding(
        f"{stroke_id}:method:load",
        BindingKind.OBJECT_METHOD,
        "load",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.SYNC,
        (SemanticParameter("path", SemanticType.STRING),),
        SemanticType.object_ref(stroke_id),
        source("stroke-load"),
        stroke_id,
        "Stroke",
        MemberScope.STATIC,
    )
    returned_only = SemanticObjectDeclaration(
        FEATURE,
        stroke_id,
        "Stroke",
        projection("stroke", BackendFamily.CPP),
        None,
        (static_factory,),
        (label,),
    )
    api = SemanticApi(declarations=(returned_only,))
    assert api.manifest()["types"][0]["constructor"] is None
    assert api.manifest()["types"][0]["methods"][0]["member_scope"] == "static"
    assert api.manifest()["types"][0]["fields"][0]["mutable"] is True
    assert FeatureRequirements.from_semantic_api(api).families == (
        ImplementationFamily.NATIVE,
    )

    constructed = SemanticObjectDeclaration(
        FEATURE,
        stroke_id,
        "Stroke",
        projection("stroke-2", BackendFamily.CPP),
        SemanticConstructor(source("stroke-constructor")),
    )
    assert constructed.constructor is not None

    fake_value = SemanticValueDeclaration(
        FEATURE,
        semantic_type_id(FEATURE, "Point"),
        "Point",
        (field(semantic_type_id(FEATURE, "Point"), "x", SemanticType.FLOAT64, "point-x"),),
        (projection("point", BackendFamily.CPP),),
    )
    wrong_ref = SemanticBinding(
        "binding:wrong",
        BindingKind.FUNCTION,
        "wrong",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.SYNC,
        (),
        SemanticType.object_ref(fake_value.type_id),
        source("wrong"),
    )
    with pytest.raises(SemanticModelError, match="nominal reference"):
        SemanticApi(functions=(wrong_ref,), declarations=(fake_value,))


def test_unknown_references_value_cycles_duplicates_and_strict_manifest_fail():
    missing = SemanticBinding(
        "binding:missing",
        BindingKind.FUNCTION,
        "missing",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.SYNC,
        (),
        SemanticType.value_ref("missing:type"),
        source("missing"),
    )
    with pytest.raises(SemanticModelError, match="unknown semantic value_ref"):
        SemanticApi(functions=(missing,))

    a_id = semantic_type_id(FEATURE, "A")
    b_id = semantic_type_id(FEATURE, "B")
    a = value_declaration(
        "A", BackendFamily.CPP, (field(a_id, "b", SemanticType.value_ref(b_id), "a-b"),), "a"
    )
    b = value_declaration(
        "B", BackendFamily.CPP, (field(b_id, "a", SemanticType.value_ref(a_id), "b-a"),), "b"
    )
    with pytest.raises(SemanticModelError, match="recursive value declaration cycle"):
        SemanticApi(declarations=(a, b))

    raw = SemanticApi().manifest()
    raw["types"] = [{"kind": "value"}]
    with pytest.raises(SemanticModelError, match="feature_id"):
        semantic_api_from_manifest(raw)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"value_type": SemanticType.VOID}, "void is invalid"),
        ({"scope": MemberScope.STATIC}, "static bridge fields"),
        ({"required": False}, "optional/missing fields"),
        (
            {
                "capabilities": BindingCapabilities.for_role(
                    DeclarationRole.INTERNAL
                )
            },
            "explicitly exported",
        ),
    ],
)
def test_field_contract_rejects_every_forbidden_semantic_shape(changes, message):
    owner = semantic_type_id(FEATURE, "Owner")
    arguments = {
        "field_id": f"{owner}:field:value",
        "owner_id": owner,
        "name": "value",
        "type": changes.get("value_type", SemanticType.INT32),
        "source": source("owner-value"),
        "mutable": False,
        "scope": changes.get("scope", MemberScope.INSTANCE),
        "required": changes.get("required", True),
        "capabilities": changes.get(
            "capabilities",
            BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ),
    }
    with pytest.raises(SemanticModelError, match=message):
        SemanticField(**arguments)


def test_separately_constructed_void_is_still_forbidden_as_an_input():
    with pytest.raises(SemanticModelError, match="void is valid only"):
        SemanticParameter("bad", SemanticType(SemanticTypeKind.VOID))


def test_route_capabilities_recurse_and_reject_cross_family_object_leaves():
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    payload_id = semantic_type_id(FEATURE, "Payload")
    stroke = SemanticObjectDeclaration(
        FEATURE, stroke_id, "Stroke", projection("stroke", BackendFamily.CPP)
    )
    payload_cpp = value_declaration(
        "Payload",
        BackendFamily.CPP,
        (field(payload_id, "stroke", SemanticType.object_ref(stroke_id), "payload-stroke"),),
        "payload-cpp",
    )
    api = SemanticApi(declarations=(stroke, payload_cpp))
    cpp_end = source("cpp-route")
    jvm_end = source("jvm-route", "java")
    with pytest.raises(
        SemanticModelError,
        match=(
            r"cannot cross cpp->jvm; cross-family object proxies are deferred "
                r"in the current generator at value\[\].*stroke.hpp:10.*cpp-route.*jvm-route"
        ),
    ):
        validate_semantic_route(
            api,
            SemanticType.array(SemanticType.object_ref(stroke_id)),
            BackendFamily.CPP,
            BackendFamily.JVM,
            cpp_end,
            jvm_end,
        )
    validate_semantic_route(
        api,
        SemanticType.value_ref(payload_id),
        BackendFamily.CPP,
        BackendFamily.CPP,
        cpp_end,
        cpp_end,
    )

    with pytest.raises(SemanticModelError, match=r"missing jvm.*cpp-route.*jvm-route"):
        validate_semantic_route(
            api,
            SemanticType.value_ref(payload_id),
            BackendFamily.CPP,
            BackendFamily.JVM,
            cpp_end,
            jvm_end,
        )


@pytest.mark.parametrize(
    ("routed_type", "position"),
    [
        (lambda object_type, _payload: object_type, r"value"),
        (lambda object_type, _payload: SemanticType.nullable(object_type), r"value\?"),
        (lambda object_type, _payload: SemanticType.array(object_type), r"value\[\]"),
        (
            lambda _object_type, payload: SemanticType.value_ref(payload),
            r"value\.stroke\[\]\?",
        ),
    ],
)
def test_cross_family_object_rejection_names_every_nested_position(
    routed_type, position
):
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    payload_id = semantic_type_id(FEATURE, "Payload")
    object_type = SemanticType.object_ref(stroke_id)
    stroke = SemanticObjectDeclaration(
        FEATURE,
        stroke_id,
        "Stroke",
        projection("stroke-native", BackendFamily.CPP),
    )
    payload = SemanticValueDeclaration(
        FEATURE,
        payload_id,
        "Payload",
        (
            field(
                payload_id,
                "stroke",
                SemanticType.array(SemanticType.nullable(object_type)),
                "payload-stroke",
            ),
        ),
        (
            projection("payload-cpp", BackendFamily.CPP),
            projection("payload-jvm", BackendFamily.JVM),
        ),
    )
    api = SemanticApi(declarations=(stroke, payload))

    with pytest.raises(
        SemanticModelError,
        match=(
            rf"native object 'Stroke' cannot cross cpp->jvm; cross-family object "
                rf"proxies are deferred in the current generator at {position}.*"
            rf"stroke-native.hpp:10.*cpp-route.hpp:10.*jvm-route.kt:10"
        ),
    ):
        validate_semantic_route(
            api,
            routed_type(object_type, payload_id),
            BackendFamily.CPP,
            BackendFamily.JVM,
            source("cpp-route"),
            source("jvm-route", "kotlin"),
        )


def test_jvm_family_accepts_objects_shared_between_kotlin_and_java_routes():
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    stroke = SemanticObjectDeclaration(
        FEATURE,
        stroke_id,
        "Stroke",
        projection("stroke-jvm", BackendFamily.JVM),
    )
    validate_semantic_route(
        SemanticApi(declarations=(stroke,)),
        SemanticType.array(SemanticType.nullable(SemanticType.object_ref(stroke_id))),
        BackendFamily.JVM,
        BackendFamily.JVM,
        source("kotlin-route", "kotlin"),
        source("java-route", "java"),
    )


def test_seeded_property_graphs_round_trip_and_invalid_wrappers_fail():
    rng = random.Random(0x5A17)
    leaves = [
        SemanticType.BOOL,
        SemanticType.INT32,
        SemanticType.INT64,
        SemanticType.FLOAT32,
        SemanticType.FLOAT64,
        SemanticType.STRING,
        SemanticType.BYTES,
    ]
    for _ in range(10_000):
        value = rng.choice(leaves)
        for _ in range(rng.randrange(0, 7)):
            value = (
                SemanticType.array(value)
                if rng.randrange(2) == 0 or value.kind is SemanticTypeKind.NULLABLE
                else SemanticType.nullable(value)
            )
        assert semantic_type_from_manifest(value.manifest()) == value

    invalid = [
        {"kind": "array", "element": {"kind": "void"}},
        {
            "kind": "nullable",
            "inner": {"kind": "nullable", "inner": {"kind": "scalar", "name": "bool"}},
        },
        {"kind": "object_ref", "type_id": ""},
        {"kind": "scalar", "name": "uint32"},
    ]
    for raw in invalid:
        with pytest.raises(SemanticTypeError):
            semantic_type_from_manifest(raw)
