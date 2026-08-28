from __future__ import annotations

import time

from supernote_module_generator.readme_codegen import render_feature_readme
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
    SemanticValueDeclaration,
    SourceProvenance,
    semantic_type_id,
)
from supernote_module_generator.semantic_types import SemanticType


FEATURE_ID = "supernote:feature:readme"


def _source(identity: str, *, language: str = "cpp") -> SourceProvenance:
    return SourceProvenance(identity, language, f"api.{language}", 1)


def _binding(
    name: str,
    *,
    result: SemanticType = SemanticType.VOID,
    parameters: tuple[SemanticParameter, ...] = (),
    execution: ExecutionMode = ExecutionMode.SYNC,
    role: DeclarationRole = DeclarationRole.EXPORTED,
    owner_id: str | None = None,
    owner_name: str | None = None,
    scope: MemberScope = MemberScope.TOP_LEVEL,
) -> SemanticBinding:
    return SemanticBinding(
        f"binding:{owner_name or 'root'}:{name}",
        BindingKind.FUNCTION if owner_id is None else BindingKind.OBJECT_METHOD,
        name,
        BindingCapabilities.for_role(role),
        execution,
        parameters,
        result,
        _source(f"source:{owner_name or 'root'}:{name}"),
        owner_id,
        owner_name,
        scope,
    )


def _api() -> SemanticApi:
    point_id = semantic_type_id(FEATURE_ID, "Point")
    color_id = semantic_type_id(FEATURE_ID, "Color")
    stroke_id = semantic_type_id(FEATURE_ID, "Stroke")
    projection = SemanticProjection(BackendFamily.CPP, _source("projection:stroke"))
    exported = BindingCapabilities.for_role(DeclarationRole.EXPORTED)
    point = SemanticValueDeclaration(
        FEATURE_ID,
        point_id,
        "Point",
        (
            SemanticField(
                "field:point:x",
                point_id,
                "x",
                SemanticType.FLOAT64,
                _source("field:point:x"),
                False,
                exported,
            ),
            SemanticField(
                "field:point:labels",
                point_id,
                "labels",
                SemanticType.array(SemanticType.nullable(SemanticType.STRING)),
                _source("field:point:labels"),
                False,
                exported,
            ),
            SemanticField(
                "field:point:color",
                point_id,
                "color",
                SemanticType.enum_ref(color_id),
                _source("field:point:color"),
                False,
                exported,
            ),
        ),
        (SemanticProjection(BackendFamily.CPP, _source("projection:point")),),
    )
    color = SemanticEnumDeclaration(
        FEATURE_ID,
        color_id,
        "Color",
        ("RED", "BLUE"),
        (SemanticProjection(BackendFamily.CPP, _source("projection:color")),),
    )
    stroke = SemanticObjectDeclaration(
        FEATURE_ID,
        stroke_id,
        "Stroke",
        projection,
        SemanticConstructor(
            _source("constructor:stroke"),
            (SemanticParameter("point", SemanticType.value_ref(point_id)),),
        ),
        (
            _binding(
                "empty",
                result=SemanticType.object_ref(stroke_id),
                owner_id=stroke_id,
                owner_name="Stroke",
                scope=MemberScope.STATIC,
            ),
            _binding(
                "transform",
                result=SemanticType.nullable(SemanticType.object_ref(stroke_id)),
                parameters=(
                    SemanticParameter("offset", SemanticType.value_ref(point_id)),
                ),
                execution=ExecutionMode.ASYNC,
                owner_id=stroke_id,
                owner_name="Stroke",
                scope=MemberScope.INSTANCE,
            ),
        ),
        (
            SemanticField(
                "field:stroke:id",
                stroke_id,
                "id",
                SemanticType.INT64,
                _source("field:stroke:id"),
                False,
                exported,
            ),
            SemanticField(
                "field:stroke:label",
                stroke_id,
                "label",
                SemanticType.STRING,
                _source("field:stroke:label"),
                True,
                exported,
            ),
        ),
    )
    return SemanticApi(
        functions=(
            _binding(
                "load",
                result=SemanticType.object_ref(stroke_id),
                parameters=(SemanticParameter("path", SemanticType.STRING),),
                execution=ExecutionMode.ASYNC,
            ),
            _binding("hidden", role=DeclarationRole.INTERNAL),
        ),
        declarations=(stroke, point, color),
    )


def _render(api: SemanticApi | None = None) -> str:
    return render_feature_readme(
        npm_name="local-drawing",
        public_name="Drawing",
        description="Draw and transform native strokes.",
        generator_version="4.0.0",
        implementation_roots=(
            ("C/C++", "android/src/main/cpp/"),
            ("Kotlin/Java", "android/src/main/java/"),
        ),
        api=api or _api(),
    )


def test_readme_lists_imports_and_the_complete_public_call_surface():
    readme = _render()

    assert "import Drawing from 'local-drawing';" in readme
    assert "import type { Color, Point, Stroke } from 'local-drawing';" in readme
    assert "`SupernoteError`" in readme
    assert "`nativeObjectInfo`" in readme
    assert "`Drawing.load(path: string): Promise<Stroke>`" in readme
    assert "`Drawing.Stroke.create(point: Point): Stroke`" in readme
    assert "`Drawing.Stroke.empty(): Stroke`" in readme
    assert "`stroke.transform(offset: Point): Promise<Stroke | null>`" in readme
    assert "`stroke.id: bigint` — read-only" in readme
    assert "`stroke.label: string` — mutable" in readme
    assert "hidden" not in readme


def test_readme_distinguishes_async_calls_and_copied_types():
    readme = _render()

    assert "const result = await Drawing.load(path);" in readme
    assert "Async calls return promises" in readme
    assert "`Drawing.load`" in readme
    assert "`stroke.transform`" in readme
    assert "All other listed calls are synchronous" in readme
    assert "`Point` — copied value" in readme
    assert "`labels: (string | null)[]`" in readme
    assert "`Color = 'RED' | 'BLUE'`" in readme
    assert "Native objects keep their native identity" in readme
    assert "Copied values are validated" in readme


def test_readme_without_public_declarations_stays_useful_and_brief():
    readme = _render(SemanticApi())

    assert "import Drawing from 'local-drawing';" in readme
    assert "import type" not in readme
    assert "No JavaScript-public declarations are currently generated" in readme
    assert "After marking an API" in readme
    assert len(readme.splitlines()) < 70


def test_readme_is_deterministic():
    assert _render() == _render()


def test_readme_stress_renders_two_thousand_public_functions_boundedly():
    api = SemanticApi(
        functions=tuple(
            _binding(
                f"operation{index:04d}",
                result=SemanticType.INT32,
                parameters=(SemanticParameter("value", SemanticType.INT32),),
            )
            for index in range(2_000)
        )
    )

    started = time.perf_counter()
    readme = _render(api)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0
    assert "`Drawing.operation0000(value: number): number` — sync" in readme
    assert "`Drawing.operation1999(value: number): number` — sync" in readme
    assert readme.count("— sync") == 2_000
    assert readme == _render(api)


def test_readme_preserves_very_long_valid_api_names_without_truncation():
    long_type = "Native" + "Document" * 24
    long_function = "calculate" + "Checksum" * 24
    type_id = semantic_type_id(FEATURE_ID, long_type)
    item = SemanticObjectDeclaration(
        FEATURE_ID,
        type_id,
        long_type,
        SemanticProjection(BackendFamily.CPP, _source("projection:long")),
    )
    api = SemanticApi(
        functions=(
            _binding(long_function, result=SemanticType.object_ref(type_id)),
        ),
        declarations=(item,),
    )

    readme = render_feature_readme(
        npm_name="@extreme/" + "feature-" * 16 + "docs",
        public_name="Feature" + "Surface" * 20,
        description="Long but valid generated names.",
        generator_version="4.0.0",
        implementation_roots=(("C/C++", "android/src/main/cpp/"),),
        api=api,
    )

    assert long_type in readme
    assert long_function in readme
    assert f"{long_function}(): {long_type}" in readme
    assert readme.count(long_type) >= 3


def test_readme_handles_returned_only_objects_async_void_and_deep_types():
    receipt_id = semantic_type_id(FEATURE_ID, "Receipt")
    receipt = SemanticObjectDeclaration(
        FEATURE_ID,
        receipt_id,
        "Receipt",
        SemanticProjection(BackendFamily.CPP, _source("projection:receipt")),
        methods=(
            _binding(
                "finish",
                execution=ExecutionMode.ASYNC,
                owner_id=receipt_id,
                owner_name="Receipt",
                scope=MemberScope.INSTANCE,
            ),
        ),
    )
    deeply_nested = SemanticType.array(
        SemanticType.nullable(
            SemanticType.array(SemanticType.nullable(SemanticType.INT32))
        )
    )
    result = SemanticType.nullable(
        SemanticType.array(SemanticType.nullable(SemanticType.BYTES))
    )
    api = SemanticApi(
        functions=(
            _binding("fetch", result=SemanticType.object_ref(receipt_id)),
            _binding("flush", execution=ExecutionMode.ASYNC),
            _binding(
                "convert",
                parameters=(SemanticParameter("values", deeply_nested),),
                result=result,
            ),
        ),
        declarations=(receipt,),
    )

    readme = _render(api)

    assert "`Drawing.fetch(): Receipt` — sync" in readme
    assert "`receipt.finish(): Promise<void>` — async" in readme
    assert "Drawing.Receipt.create" not in readme
    assert "`Drawing.flush(): Promise<void>` — async" in readme
    assert (
        "`Drawing.convert(values: ((number | null)[] | null)[]): "
        "(Uint8Array | null)[] | null` — sync"
    ) in readme


def test_readme_renders_package_description_as_plain_text():
    readme = render_feature_readme(
        npm_name="safe-description",
        public_name="SafeDescription",
        description="# **Fast** [docs](https://example.com) <script> `code`",
        generator_version="4.0.0",
        implementation_roots=(("C/C++", "android/src/main/cpp/"),),
        api=SemanticApi(),
    )

    assert "\n# **Fast**" not in readme
    assert "<script>" not in readme
    assert "\\# \\*\\*Fast\\*\\*" in readme
    assert "&lt;script&gt;" in readme
