from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

from supernote_module_generator.reachability import (
    PublicReachabilityError,
    compute_public_api,
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
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticProjection,
    SemanticType,
    SemanticValueDeclaration,
    SourceProvenance,
    semantic_type_id,
)
from supernote_module_generator.typescript_codegen import render_typescript


FEATURE = "supernote:feature:phase3"


def source(identity: str, language: str = "cpp", line: int = 1) -> SourceProvenance:
    return SourceProvenance(identity, language, f"{identity}.{language}", line)


def projection(
    identity: str,
    backend: BackendFamily = BackendFamily.CPP,
    language: str | None = None,
):
    if language is None:
        language = "cpp" if backend is BackendFamily.CPP else "kotlin"
    return SemanticProjection(backend, source(identity, language))


def field(
    owner_id: str,
    name: str,
    semantic_type: SemanticType,
    *,
    mutable: bool = False,
    language: str = "cpp",
) -> SemanticField:
    return SemanticField(
        f"{owner_id}:field:{name}",
        owner_id,
        name,
        semantic_type,
        source(f"{name}-field", language),
        mutable,
    )


def method(
    owner_id: str,
    owner_name: str,
    name: str,
    *,
    result: SemanticType = SemanticType.VOID,
    parameters: tuple[SemanticParameter, ...] = (),
    scope: MemberScope = MemberScope.INSTANCE,
    execution: ExecutionMode = ExecutionMode.SYNC,
    language: str = "cpp",
) -> SemanticBinding:
    return SemanticBinding(
        f"{owner_id}:method:{scope.value}:{name}",
        BindingKind.OBJECT_METHOD,
        name,
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        execution,
        parameters,
        result,
        source(f"{owner_name}-{scope.value}-{name}", language),
        owner_id,
        owner_name,
        scope,
    )


def function(
    name: str,
    *,
    result: SemanticType = SemanticType.VOID,
    parameters: tuple[SemanticParameter, ...] = (),
    execution: ExecutionMode = ExecutionMode.SYNC,
) -> SemanticBinding:
    return SemanticBinding(
        f"binding:{name}",
        BindingKind.FUNCTION,
        name,
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        execution,
        parameters,
        result,
        source(f"function-{name}"),
    )


def phase3_api(
    backend: BackendFamily = BackendFamily.CPP,
    *,
    jvm_language: str = "kotlin",
) -> SemanticApi:
    language = "cpp" if backend is BackendFamily.CPP else jvm_language
    point_id = semantic_type_id(FEATURE, "Point")
    color_id = semantic_type_id(FEATURE, "Color")
    stroke_id = semantic_type_id(FEATURE, "Stroke")
    other_id = semantic_type_id(FEATURE, "OtherStroke")
    receipt_id = semantic_type_id(FEATURE, "Receipt")
    tools_id = semantic_type_id(FEATURE, "Tools")
    hidden_id = semantic_type_id(FEATURE, "Hidden")

    point = SemanticValueDeclaration(
        FEATURE,
        point_id,
        "Point",
        (
            field(point_id, "x", SemanticType.FLOAT64, language=language),
            field(
                point_id,
                "tags",
                SemanticType.array(SemanticType.nullable(SemanticType.STRING)),
                language=language,
            ),
            field(
                point_id,
                "color",
                SemanticType.enum_ref(color_id),
                language=language,
            ),
        ),
        (projection("point", backend, language),),
    )
    color = SemanticEnumDeclaration(
        FEATURE,
        color_id,
        "Color",
        ("RED", "BLUE"),
        (projection("color", backend, language),),
    )
    stroke = SemanticObjectDeclaration(
        FEATURE,
        stroke_id,
        "Stroke",
        projection("stroke", backend, language),
        SemanticConstructor(
            source("stroke-constructor", "cpp" if backend is BackendFamily.CPP else "kotlin"),
            (SemanticParameter("point", SemanticType.value_ref(point_id)),),
        ),
        (
            method(
                stroke_id,
                "Stroke",
                "fromPoints",
                result=SemanticType.object_ref(stroke_id),
                parameters=(
                    SemanticParameter(
                        "points", SemanticType.array(SemanticType.value_ref(point_id))
                    ),
                ),
                scope=MemberScope.STATIC,
                language=language,
            ),
            method(
                stroke_id,
                "Stroke",
                "transform",
                result=SemanticType.object_ref(stroke_id),
                parameters=(SemanticParameter("offset", SemanticType.value_ref(point_id)),),
                execution=ExecutionMode.ASYNC,
                language=language,
            ),
        ),
        (
            field(stroke_id, "id", SemanticType.INT64, language=language),
            field(
                stroke_id,
                "label",
                SemanticType.STRING,
                mutable=True,
                language=language,
            ),
        ),
    )
    other = SemanticObjectDeclaration(
        FEATURE, other_id, "OtherStroke", projection("other", backend, language)
    )
    receipt = SemanticObjectDeclaration(
        FEATURE,
        receipt_id,
        "Receipt",
        projection("receipt", backend, language),
        methods=(
            method(
                receipt_id,
                "Receipt",
                "status",
                result=SemanticType.STRING,
                language=language,
            ),
        ),
    )
    tools = SemanticObjectDeclaration(
        FEATURE,
        tools_id,
        "Tools",
        projection("tools", backend, language),
        methods=(
            method(
                tools_id,
                "Tools",
                "version",
                result=SemanticType.STRING,
                scope=MemberScope.STATIC,
                language=language,
            ),
        ),
    )
    hidden = SemanticObjectDeclaration(
        FEATURE, hidden_id, "Hidden", projection("hidden", backend, language)
    )
    functions = (
        function(
            "useOther",
            parameters=(SemanticParameter("other", SemanticType.object_ref(other_id)),),
        ),
        function("load", result=SemanticType.object_ref(receipt_id)),
        function(
            "maybe",
            parameters=(
                SemanticParameter(
                    "strokes",
                    SemanticType.array(SemanticType.nullable(SemanticType.object_ref(stroke_id))),
                ),
            ),
            result=SemanticType.nullable(
                SemanticType.array(SemanticType.object_ref(stroke_id))
            ),
        ),
    )
    return SemanticApi(
        functions=functions,
        declarations=(hidden, tools, receipt, other, stroke, color, point),
    )


def test_public_graph_distinguishes_static_namespaces_instances_and_hidden_types():
    public = compute_public_api(phase3_api())
    names = {item.name for item in public.declarations}
    by_name = {item.name: item.type_id for item in public.declarations}

    assert names == {"Point", "Color", "Stroke", "OtherStroke", "Receipt", "Tools"}
    assert "Hidden" not in names
    assert public.object_namespaces == frozenset(
        {by_name["Stroke"], by_name["Tools"]}
    )
    assert public.object_instances == frozenset(
        {by_name["Stroke"], by_name["OtherStroke"], by_name["Receipt"]}
    )


def test_typescript_emits_recursive_structural_nominal_and_member_contracts():
    text = render_typescript("Drawing", phase3_api())

    assert "export type Color = 'RED' | 'BLUE';" in text
    assert "export interface Point {" in text
    assert "  x: number;" in text
    assert "  tags: (string | null)[];" in text
    assert "  color: Color;" in text
    assert "readonly x" not in text
    assert "declare const __supernoteBrand_Stroke: unique symbol;" in text
    assert "readonly [__supernoteBrand_Stroke]: void;" in text
    assert "  readonly id: bigint;" in text
    assert "  label: string;" in text
    assert "transform: SupernoteCallable<[offset: Point], Promise<Stroke>>;" in text
    assert "Stroke: SupernoteTypeCompanion<Stroke> & {" in text
    assert "create: SupernoteCallable<[point: Point], Stroke>;" in text
    assert "fromPoints: SupernoteCallable<[points: Point[]], Stroke>;" in text
    assert "Tools: SupernoteTypeCompanion<Tools> & {" in text
    assert "version: SupernoteCallable<[], string>;" in text
    assert "Receipt: SupernoteTypeCompanion<Receipt>;" in text
    assert "status: SupernoteCallable<[], string>;" in text
    assert "maybe: SupernoteCallable<[strokes: (Stroke | null)[]], Stroke[] | null>;" in text
    assert "Hidden" not in text


def test_static_root_does_not_make_instance_members_reachable():
    object_id = semantic_type_id(FEATURE, "Parser")
    item = SemanticObjectDeclaration(
        FEATURE,
        object_id,
        "Parser",
        projection("parser"),
        methods=(
            method(
                object_id,
                "Parser",
                "version",
                result=SemanticType.STRING,
                scope=MemberScope.STATIC,
            ),
            method(object_id, "Parser", "parse", result=SemanticType.STRING),
        ),
    )
    with pytest.raises(
        PublicReachabilityError,
        match=r"Parser-instance-parse\.cpp:1:1.*Parser\.parse.*unreachable",
    ):
        compute_public_api(SemanticApi(declarations=(item,)))


def test_exported_value_field_without_a_public_type_path_is_an_error():
    point_id = semantic_type_id(FEATURE, "UnusedPoint")
    item = SemanticValueDeclaration(
        FEATURE,
        point_id,
        "UnusedPoint",
        (field(point_id, "x", SemanticType.FLOAT64),),
        (projection("unused-point"),),
    )
    with pytest.raises(PublicReachabilityError, match=r"x-field\.cpp:1:1.*unreachable"):
        compute_public_api(SemanticApi(declarations=(item,)))


def test_static_and_instance_names_are_separate_but_create_and_root_collisions_fail():
    object_id = semantic_type_id(FEATURE, "Codec")
    same_names = SemanticObjectDeclaration(
        FEATURE,
        object_id,
        "Codec",
        projection("codec"),
        SemanticConstructor(source("codec-constructor")),
        (
            method(object_id, "Codec", "parse", scope=MemberScope.STATIC),
            method(object_id, "Codec", "parse"),
        ),
    )
    text = render_typescript("Codecs", SemanticApi(declarations=(same_names,)))
    assert text.count("parse: SupernoteCallable<[], void>;") == 2

    create_collision = replace(
        same_names,
        methods=(method(object_id, "Codec", "create", scope=MemberScope.STATIC),),
    )
    with pytest.raises(PublicReachabilityError, match=r"create.*Codec type namespace"):
        compute_public_api(SemanticApi(declarations=(create_collision,)))

    returned_only = replace(same_names, constructor=None, methods=())
    with pytest.raises(PublicReachabilityError, match=r"Codec.*feature root"):
        compute_public_api(
            SemanticApi(
                functions=(function("Codec", result=SemanticType.object_ref(object_id)),),
                declarations=(returned_only,),
            )
        )


@pytest.mark.parametrize("reserved", ["SupernoteError", "SupernoteErrorCode", "DrawingFeature"])
def test_reachable_types_cannot_collide_with_generated_types(reserved: str):
    object_id = semantic_type_id(FEATURE, reserved)
    item = SemanticObjectDeclaration(
        FEATURE,
        object_id,
        reserved,
        projection(f"reserved-{reserved}"),
        SemanticConstructor(source(f"reserved-{reserved}-constructor")),
    )
    with pytest.raises(PublicReachabilityError, match=r"collides with generated TypeScript"):
        render_typescript("Drawing", SemanticApi(declarations=(item,)))


def test_equivalent_cpp_and_jvm_semantics_generate_identical_public_typescript():
    cpp = render_typescript("Drawing", phase3_api(BackendFamily.CPP))
    kotlin = render_typescript("Drawing", phase3_api(BackendFamily.JVM))
    java = render_typescript(
        "Drawing", phase3_api(BackendFamily.JVM, jvm_language="java")
    )
    assert cpp == kotlin == java


def test_generated_contract_and_expect_error_fixture_pass_real_tsc(tmp_path: Path):
    tsc = shutil.which("tsc")
    if tsc is None:
        pytest.skip("TypeScript compiler is unavailable")
    fixture_root = Path(__file__).parent / "fixtures/v4_typescript"
    generated = render_typescript("Drawing", phase3_api())
    assert generated == (fixture_root / "index.d.ts").read_text(encoding="utf-8")
    (tmp_path / "index.d.ts").write_text(generated, encoding="utf-8")
    fixture = fixture_root / "consumer.ts"
    (tmp_path / "consumer.ts").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    completed = subprocess.run(
        [
            tsc,
            "--noEmit",
            "--strict",
            "--target",
            "ES2020",
            "--module",
            "Node16",
            "--moduleResolution",
            "Node16",
            "consumer.ts",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
