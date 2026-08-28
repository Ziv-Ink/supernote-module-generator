import json
from pathlib import Path
import random

import pytest

from supernote_module_generator import binding_codegen
from supernote_module_generator.jvm_manifest import (
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_field_accessor_identity,
    jvm_field_identity,
    jvm_owner_identity,
    read_jvm_manifest,
    write_jvm_manifest,
)
from supernote_module_generator.jvm_projection import (
    JvmProjectionError,
    project_jvm_owners,
)
from supernote_module_generator.semantic import (
    BindingKind,
    MemberScope,
    SemanticDeclarationKind,
    SemanticType,
    SourceProvenance,
    merge_semantic_apis,
)
from supernote_module_generator.source_models import (
    DeclarationTarget,
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmFieldSource,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    JvmTypeSource,
    SourceIntent,
    SourceModelError,
    SupernoteMarker,
)


FEATURE_ID = "supernote:feature:0123456789abcdef"


def intent(target: DeclarationTarget, *markers: SupernoteMarker) -> SourceIntent:
    return SourceIntent.from_markers(target, markers, first_line=3)


def cpp_module(tmp_path: Path, header: str) -> Path:
    root = tmp_path / "feature"
    native = root / "android/src/main/cpp"
    native.mkdir(parents=True)
    (native / "feature.cpp").write_text("", encoding="utf-8")
    (native / "model.hpp").write_text(header, encoding="utf-8")
    config = root / "android/.supernote-module/codegen-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"backend": "jsi", "module_name": "Phase2"}),
        encoding="utf-8",
    )
    (root / ".supernote-module.json").write_text(
        json.dumps({"feature_id": FEATURE_ID}), encoding="utf-8"
    )
    return root


def test_seeded_cpp_parser_mutations_terminate_and_are_deterministic(tmp_path):
    seed = 0xC23F_0220
    rng = random.Random(seed)
    root = cpp_module(
        tmp_path,
        """namespace drawing {
// SupernotePluginValue
struct Point { int32_t x; int32_t y; };

// SupernotePluginObject
class Stroke {
 public:
  // SupernotePluginConstructor
  Stroke(Point origin);
  // SupernotePluginExport
  Point origin() const;
};
}  // namespace drawing
""",
    )
    path = root / "android/src/main/cpp/model.hpp"
    baseline = path.read_text(encoding="utf-8")
    insertions = (
        "/*",
        "*/",
        'R\"tag(',
        ')tag\"',
        "#if 0\n",
        "#endif\n",
        "{",
        "}",
        ";",
        "// SupernotePluginObject\n",
        "// SupernotePluginValue\n",
        "// SupernotePluginExport\n",
        "\x00",
    )

    for iteration in range(1_024):
        source = baseline
        for _ in range(1 + rng.randrange(4)):
            operation = rng.randrange(4)
            start = rng.randrange(len(source) + 1)
            if operation == 0:
                source = source[:start]
            elif operation == 1:
                source = source[:start] + rng.choice(insertions) + source[start:]
            elif operation == 2 and source:
                end = min(len(source), start + rng.randrange(1, 25))
                source = source[:start] + source[end:]
            else:
                source = source.replace("SupernotePlugin", "SupernotePluginX", 1)
        path.write_text(source, encoding="utf-8")
        try:
            first = binding_codegen.scan_cpp_semantic_model(root)
            first_result = ("ok", first.manifest())
        except binding_codegen.CodegenError as exc:
            first_result = ("error", str(exc))

        # Periodic replay makes determinism part of the retained campaign without
        # doubling the cost of every mutation.
        if iteration % 32 == 0:
            try:
                second = binding_codegen.scan_cpp_semantic_model(root)
                second_result = ("ok", second.manifest())
            except binding_codegen.CodegenError as exc:
                second_result = ("error", str(exc))
            assert second_result == first_result, (seed, iteration, source)


@pytest.mark.parametrize(
    ("target", "markers"),
    [
        (DeclarationTarget.CLASS, ()),
        (DeclarationTarget.CLASS, (SupernoteMarker.OBJECT,)),
        (DeclarationTarget.CLASS, (SupernoteMarker.VALUE,)),
        (DeclarationTarget.ENUM, (SupernoteMarker.VALUE,)),
        (DeclarationTarget.FIELD, (SupernoteMarker.EXPORT,)),
        (DeclarationTarget.CONSTRUCTOR, (SupernoteMarker.CONSTRUCTOR,)),
        (DeclarationTarget.FUNCTION, (SupernoteMarker.EXPORT,)),
        (
            DeclarationTarget.METHOD,
            (SupernoteMarker.INTERNAL, SupernoteMarker.ASYNC),
        ),
    ],
)
def test_closed_marker_matrix_accepts_only_declared_compositions(target, markers):
    assert intent(target, *markers).marker_set == frozenset(markers)


@pytest.mark.parametrize(
    ("target", "markers"),
    [
        (DeclarationTarget.CLASS, (SupernoteMarker.EXPORT,)),
        (DeclarationTarget.CLASS, (SupernoteMarker.OBJECT, SupernoteMarker.VALUE)),
        (DeclarationTarget.ENUM, (SupernoteMarker.EXPORT,)),
        (DeclarationTarget.FIELD, (SupernoteMarker.INTERNAL,)),
        (DeclarationTarget.CONSTRUCTOR, (SupernoteMarker.EXPORT,)),
        (DeclarationTarget.FUNCTION, (SupernoteMarker.OBJECT,)),
        (DeclarationTarget.METHOD, (SupernoteMarker.ASYNC,)),
        (
            DeclarationTarget.FUNCTION,
            (SupernoteMarker.EXPORT, SupernoteMarker.INTERNAL),
        ),
    ],
)
def test_closed_marker_matrix_rejects_every_cross_target_composition(target, markers):
    with pytest.raises(SourceModelError):
        intent(target, *markers)


def test_cpp_frontend_projects_values_enums_objects_and_unmarked_owners(tmp_path):
    root = cpp_module(
        tmp_path,
        """namespace drawing {
// @SupernotePluginValue
enum class Color { Red, Green, Blue };

// @SupernotePluginValue
struct Point {
  // @SupernotePluginExport
  double x;
  // @SupernotePluginExport
  double y;
};

// @SupernotePluginObject
class Stroke {
public:
  Stroke();
  // @SupernoteConstructor
  explicit Stroke(std::vector<Point> points);
  // @SupernotePluginExport
  static std::shared_ptr<Stroke> empty();
  // @SupernotePluginExport
  bool intersects(const Stroke& other) const;
  // @SupernotePluginExport
  std::vector<std::optional<Point>> samples() const;
};

class Api {
public:
  // @SupernotePluginExport
  static std::shared_ptr<Stroke> load(Point point);
};
}
""",
    )
    api = binding_codegen.scan_cpp_semantic_model(root)

    assert [item.name for item in api.functions] == ["load"]
    assert api.functions[0].result.kind.value == "object_ref"
    by_name = {item.name: item for item in api.declarations}
    assert by_name["Color"].kind is SemanticDeclarationKind.ENUM
    assert by_name["Color"].constants == ("Red", "Green", "Blue")
    assert [field.name for field in by_name["Point"].fields] == ["x", "y"]
    stroke = by_name["Stroke"]
    assert stroke.constructor.parameters[0].type == SemanticType.array(
        SemanticType.value_ref(by_name["Point"].type_id)
    )
    methods = {item.name: item for item in stroke.methods}
    assert methods["empty"].member_scope is MemberScope.STATIC
    assert methods["intersects"].member_scope is MemberScope.INSTANCE
    assert methods["intersects"].parameters[0].type == SemanticType.object_ref(
        stroke.type_id
    )
    assert methods["samples"].result == SemanticType.array(
        SemanticType.nullable(SemanticType.value_ref(by_name["Point"].type_id))
    )


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    [
        (
            "// @SupernotePluginObject\nclass Bad : public Base {};",
            "inheritance is not supported",
        ),
        (
            "// @SupernotePluginValue\nusing Bad = int;",
            "followed by a class or struct",
        ),
        (
            "namespace {\n// @SupernotePluginValue\nstruct Bad {\n"
            "// @SupernotePluginExport\ndouble x;\n};\n}",
            "brace depth",
        ),
        (
            "// @SupernotePluginValue\nenum class Bad { A = 1 };",
            "comma-separated source constant names",
        ),
    ],
)
def test_cpp_frontend_rejects_deferred_or_ambiguous_declarations(
    tmp_path, source, diagnostic
):
    root = cpp_module(tmp_path, source)
    with pytest.raises(binding_codegen.CodegenError, match=diagnostic):
        binding_codegen.scan_cpp_semantic_model(root)


def source(identity: str, language: JvmLanguage, path: str, line: int = 1):
    return SourceProvenance(identity, language.value, path, line)


def constructor(
    owner: str,
    language: JvmLanguage,
    descriptor: str,
    parameters: tuple[JvmParameterSource, ...],
    *markers: SupernoteMarker,
) -> JvmConstructorSource:
    identity = jvm_declaration_identity(owner, "<init>", descriptor)
    return JvmConstructorSource(
        source(identity, language, "Model.kt"),
        descriptor,
        parameters,
        "public",
        intent(DeclarationTarget.CONSTRUCTOR, *markers),
        jvm_adapter_identity(identity),
    )


def field(
    owner: str,
    language: JvmLanguage,
    name: str,
    type_: JvmTypeSource,
    *,
    mutable: bool = False,
) -> JvmFieldSource:
    identity = jvm_field_identity(owner, name)
    return JvmFieldSource(
        source(identity, language, "Model.kt"),
        jvm_owner_identity(owner),
        name,
        type_,
        intent(DeclarationTarget.FIELD, SupernoteMarker.EXPORT),
        "public",
        mutable,
        False,
        jvm_field_accessor_identity(identity),
    )


def method(
    owner: str,
    language: JvmLanguage,
    name: str,
    descriptor: str,
    parameters: tuple[JvmParameterSource, ...],
    result: JvmTypeSource,
    *,
    static: bool = False,
    target: DeclarationTarget = DeclarationTarget.METHOD,
) -> JvmDeclarationSource:
    identity = jvm_declaration_identity(owner, name, descriptor)
    return JvmDeclarationSource(
        source(identity, language, "Model.kt", 8),
        jvm_owner_identity(owner),
        owner,
        name,
        descriptor,
        parameters,
        result.jvm_type,
        result.nullable,
        intent(target, SupernoteMarker.EXPORT),
        "public",
        jvm_adapter_identity(identity),
        language,
        False,
        static,
        result.arguments,
    )


def projected_java_result(type_: JvmTypeSource) -> SemanticType:
    owner_name = "com.example.JavaMatrix"
    declaration = method(
        owner_name,
        JvmLanguage.JAVA,
        "route",
        "()Ljava/lang/Object;",
        (),
        type_,
        static=True,
        target=DeclarationTarget.FUNCTION,
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.JAVA, "JavaMatrix.java"),
        JvmLanguage.JAVA,
        owner_name,
        "JavaMatrix",
        JvmOwnerForm.JAVA_STATIC,
        intent(DeclarationTarget.CLASS),
        (),
        (declaration,),
    )
    return project_jvm_owners((owner,), feature_id=FEATURE_ID).functions[0].result


def test_jvm_frontend_projects_recursive_kotlin_object_and_value():
    point_name = "com.example.Point"
    stroke_name = "com.example.Stroke"
    point = JvmOwnerSource(
        source(jvm_owner_identity(point_name), JvmLanguage.KOTLIN, "Point.kt"),
        JvmLanguage.KOTLIN,
        point_name,
        "Point",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (
            constructor(
                point_name,
                JvmLanguage.KOTLIN,
                "(DD)V",
                (
                    JvmParameterSource("kotlin.Double", "x"),
                    JvmParameterSource("kotlin.Double", "y"),
                ),
            ),
        ),
        (),
        fields=(
            field(
                point_name,
                JvmLanguage.KOTLIN,
                "x",
                JvmTypeSource("kotlin.Double"),
                mutable=True,
            ),
            field(
                point_name,
                JvmLanguage.KOTLIN,
                "y",
                JvmTypeSource("kotlin.Double"),
                mutable=True,
            ),
        ),
        is_data=True,
    )
    points = JvmTypeSource(
        "kotlin.collections.List",
        arguments=(JvmTypeSource(point_name),),
    )
    stroke = JvmOwnerSource(
        source(jvm_owner_identity(stroke_name), JvmLanguage.KOTLIN, "Stroke.kt"),
        JvmLanguage.KOTLIN,
        stroke_name,
        "Stroke",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (
            constructor(
                stroke_name,
                JvmLanguage.KOTLIN,
                "(Ljava/util/List;)V",
                (
                    JvmParameterSource(
                        points.jvm_type,
                        "points",
                        type_arguments=points.arguments,
                    ),
                ),
                SupernoteMarker.CONSTRUCTOR,
            ),
        ),
        (
            method(
                stroke_name,
                JvmLanguage.KOTLIN,
                "empty",
                "()Lcom/example/Stroke;",
                (),
                JvmTypeSource(stroke_name),
                static=True,
            ),
        ),
        fields=(
            field(
                stroke_name,
                JvmLanguage.KOTLIN,
                "label",
                JvmTypeSource("kotlin.String", nullable=True),
                mutable=True,
            ),
        ),
    )
    api = project_jvm_owners((point, stroke), feature_id=FEATURE_ID)
    by_name = {item.name: item for item in api.declarations}
    assert by_name["Stroke"].constructor.parameters[0].type == SemanticType.array(
        SemanticType.value_ref(by_name["Point"].type_id)
    )
    assert by_name["Stroke"].fields[0].type == SemanticType.nullable(
        SemanticType.STRING
    )
    assert by_name["Stroke"].methods[0].kind is BindingKind.OBJECT_METHOD
    assert by_name["Stroke"].methods[0].member_scope is MemberScope.STATIC


@pytest.mark.parametrize(
    ("spelling", "nullable", "arguments", "expected"),
    [
        ("int", False, (), SemanticType.INT32),
        (
            "java.lang.Integer",
            True,
            (),
            SemanticType.nullable(SemanticType.INT32),
        ),
        (
            "java.util.List",
            False,
            (JvmTypeSource("java.lang.Integer"),),
            SemanticType.array(SemanticType.INT32),
        ),
        (
            "java.util.List",
            True,
            (JvmTypeSource("java.lang.Integer", nullable=True),),
            SemanticType.nullable(
                SemanticType.array(SemanticType.nullable(SemanticType.INT32))
            ),
        ),
    ],
)
def test_java_direct_boxed_and_nested_type_use_matrix(
    spelling, nullable, arguments, expected
):
    owner_name = "com.example.Api"
    declaration = method(
        owner_name,
        JvmLanguage.JAVA,
        "route",
        "()V",
        (),
        JvmTypeSource(spelling, nullable, arguments),
        static=True,
        target=DeclarationTarget.FUNCTION,
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.JAVA, "Api.java"),
        JvmLanguage.JAVA,
        owner_name,
        "Api",
        JvmOwnerForm.JAVA_STATIC,
        intent(DeclarationTarget.CLASS),
        (),
        (declaration,),
    )
    assert project_jvm_owners((owner,), feature_id=FEATURE_ID).functions[0].result == expected


@pytest.mark.parametrize(
    ("direct", "boxed", "semantic"),
    [
        ("boolean", "java.lang.Boolean", SemanticType.BOOL),
        ("int", "java.lang.Integer", SemanticType.INT32),
        ("long", "java.lang.Long", SemanticType.INT64),
        ("float", "java.lang.Float", SemanticType.FLOAT32),
        ("double", "java.lang.Double", SemanticType.FLOAT64),
        ("java.lang.String", "java.lang.String", SemanticType.STRING),
        ("byte[]", "byte[]", SemanticType.BYTES),
    ],
)
def test_every_java_scalar_supports_direct_nullable_list_and_nested_nullable_forms(
    direct, boxed, semantic
):
    assert projected_java_result(JvmTypeSource(direct)) == semantic
    assert projected_java_result(
        JvmTypeSource(boxed, nullable=True)
    ) == SemanticType.nullable(semantic)
    assert projected_java_result(
        JvmTypeSource(
            "java.util.List",
            arguments=(JvmTypeSource(boxed),),
        )
    ) == SemanticType.array(semantic)
    assert projected_java_result(
        JvmTypeSource(
            "java.util.List",
            nullable=True,
            arguments=(JvmTypeSource(boxed, nullable=True),),
        )
    ) == SemanticType.nullable(
        SemanticType.array(SemanticType.nullable(semantic))
    )


@pytest.mark.parametrize(
    ("changes", "diagnostic"),
    [
        ({"type_parameter_count": 1}, "generic marked JVM types"),
        ({"supertypes": ("com.example.Base",)}, "inheritance and interfaces"),
        ({"is_final": False}, "must be final"),
    ],
)
def test_marked_java_types_reject_deferred_type_forms(changes, diagnostic):
    owner_name = "com.example.Stroke"
    values = dict(
        provenance=source(
            jvm_owner_identity(owner_name), JvmLanguage.JAVA, "Stroke.java", 12
        ),
        language=JvmLanguage.JAVA,
        owner_class=owner_name,
        source_name="Stroke",
        form=JvmOwnerForm.CLASS,
        intent=intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        constructors=(),
        declarations=(),
    )
    values.update(changes)
    owner = JvmOwnerSource(**values)
    with pytest.raises(JvmProjectionError, match=diagnostic) as raised:
        project_jvm_owners((owner,), feature_id=FEATURE_ID)
    assert "Stroke.java:12:1" in str(raised.value)


def test_jvm_object_rejects_multiple_selected_constructors_with_source_location():
    owner_name = "com.example.Stroke"
    selected = constructor(
        owner_name,
        JvmLanguage.KOTLIN,
        "()V",
        (),
        SupernoteMarker.CONSTRUCTOR,
    )
    second = constructor(
        owner_name,
        JvmLanguage.KOTLIN,
        "(I)V",
        (JvmParameterSource("kotlin.Int", "size"),),
        SupernoteMarker.CONSTRUCTOR,
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Stroke.kt", 7),
        JvmLanguage.KOTLIN,
        owner_name,
        "Stroke",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (selected, second),
        (),
    )
    with pytest.raises(JvmProjectionError, match="at most one") as raised:
        project_jvm_owners((owner,), feature_id=FEATURE_ID)
    assert "Stroke.kt:7:1" in str(raised.value)


def test_jvm_object_rejects_inaccessible_marked_members():
    owner_name = "com.example.Stroke"
    inaccessible = method(
        owner_name,
        JvmLanguage.KOTLIN,
        "hidden",
        "()Z",
        (),
        JvmTypeSource("kotlin.Boolean"),
    )
    inaccessible = JvmDeclarationSource(
        inaccessible.provenance,
        inaccessible.owner_declaration_id,
        inaccessible.owner_class,
        inaccessible.jvm_name,
        inaccessible.jvm_descriptor,
        inaccessible.parameters,
        inaccessible.result_jvm_type,
        inaccessible.result_nullable,
        inaccessible.intent,
        "private",
        inaccessible.adapter_identity,
        inaccessible.language,
        inaccessible.is_suspend,
        inaccessible.is_static,
        inaccessible.result_type_arguments,
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Stroke.kt"),
        JvmLanguage.KOTLIN,
        owner_name,
        "Stroke",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (),
        (inaccessible,),
    )
    with pytest.raises(JvmProjectionError, match="must be public") as raised:
        project_jvm_owners((owner,), feature_id=FEATURE_ID)
    assert "Model.kt:8:1" in str(raised.value)


@pytest.mark.parametrize(
    ("spelling", "nullable", "arguments", "diagnostic"),
    [
        ("java.lang.Integer", False, (), "primitive spelling"),
        ("int", True, (), "boxed reference spelling"),
        ("java.util.List", False, (), "exactly one"),
    ],
)
def test_java_boxing_matrix_rejects_noncanonical_forms(
    spelling, nullable, arguments, diagnostic
):
    owner_name = "com.example.Api"
    declaration = method(
        owner_name,
        JvmLanguage.JAVA,
        "route",
        "()V",
        (),
        JvmTypeSource(spelling, nullable, arguments),
        static=True,
        target=DeclarationTarget.FUNCTION,
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.JAVA, "Api.java"),
        JvmLanguage.JAVA,
        owner_name,
        "Api",
        JvmOwnerForm.JAVA_STATIC,
        intent(DeclarationTarget.CLASS),
        (),
        (declaration,),
    )
    with pytest.raises(JvmProjectionError, match=diagnostic):
        project_jvm_owners((owner,), feature_id=FEATURE_ID)


def test_jvm_manifest_round_trips_recursive_fields_and_type_arguments(tmp_path):
    owner_name = "com.example.Points"
    list_type = JvmTypeSource(
        "java.util.List",
        arguments=(JvmTypeSource("java.lang.Integer", nullable=True),),
    )
    owner = JvmOwnerSource(
        source(jvm_owner_identity(owner_name), JvmLanguage.JAVA, "Points.java"),
        JvmLanguage.JAVA,
        owner_name,
        "Points",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (
            constructor(
                owner_name,
                JvmLanguage.JAVA,
                "(Ljava/util/List;)V",
                (
                    JvmParameterSource(
                        list_type.jvm_type,
                        "values",
                        type_arguments=list_type.arguments,
                    ),
                ),
            ),
        ),
        (),
        fields=(field(owner_name, JvmLanguage.JAVA, "values", list_type),),
        is_record=True,
    )
    manifest = JvmSourceManifest(FEATURE_ID, "4.0.0.dev0", (owner,))
    path = tmp_path / "jvm.json"
    write_jvm_manifest(path, manifest)
    assert read_jvm_manifest(path) == manifest


def test_cross_frontend_enum_and_value_projections_merge_exactly(tmp_path):
    root = cpp_module(
        tmp_path,
        """// @SupernotePluginValue
enum class Color { Red, Green };
// @SupernotePluginValue
struct Point {
// @SupernotePluginExport
double x;
// @SupernotePluginExport
double y;
};
""",
    )
    cpp = binding_codegen.scan_cpp_semantic_model(root)
    color_name = "com.example.Color"
    point_name = "com.example.Point"
    color = JvmOwnerSource(
        source(jvm_owner_identity(color_name), JvmLanguage.KOTLIN, "Color.kt"),
        JvmLanguage.KOTLIN,
        color_name,
        "Color",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (),
        (),
        enum_constants=("Red", "Green"),
    )
    point = JvmOwnerSource(
        source(jvm_owner_identity(point_name), JvmLanguage.KOTLIN, "Point.kt"),
        JvmLanguage.KOTLIN,
        point_name,
        "Point",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (
            constructor(
                point_name,
                JvmLanguage.KOTLIN,
                "(DD)V",
                (
                    JvmParameterSource("kotlin.Double", "x"),
                    JvmParameterSource("kotlin.Double", "y"),
                ),
            ),
        ),
        (),
        fields=(
            field(
                point_name, JvmLanguage.KOTLIN, "x",
                JvmTypeSource("kotlin.Double"), mutable=True,
            ),
            field(
                point_name, JvmLanguage.KOTLIN, "y",
                JvmTypeSource("kotlin.Double"), mutable=True,
            ),
        ),
        is_data=True,
    )
    jvm = project_jvm_owners((color, point), feature_id=FEATURE_ID)
    merged = merge_semantic_apis(cpp, jvm)
    assert len(merged.declarations) == 2
    assert all(len(item.projections) == 2 for item in merged.declarations)
