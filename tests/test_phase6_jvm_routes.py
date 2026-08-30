from supernote_module_generator.jvm_manifest import (
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_field_accessor_identity,
    jvm_field_identity,
    jvm_owner_identity,
)
from supernote_module_generator.jvm_codegen import render_jvm_feature_jsi
from supernote_module_generator.jvm_projection import project_jvm_owners
from supernote_module_generator.jvm_object_runtime_codegen import (
    render_jvm_object_runtime,
)
from supernote_module_generator.jvm_routes import plan_jvm_routes
from supernote_module_generator.semantic import SourceProvenance
from supernote_module_generator.semantic_types import SemanticTypeKind
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
    SupernoteMarker,
)


FEATURE = "supernote:feature:6666666666666666"


def _intent(target, *markers):
    return SourceIntent.from_markers(target, tuple(markers))


def _source(identity, language, path="Model.kt"):
    return SourceProvenance(identity, language.value, path, 1)


def _constructor(owner, language, descriptor, parameters, *markers):
    identity = jvm_declaration_identity(owner, "<init>", descriptor)
    return JvmConstructorSource(
        _source(identity, language),
        descriptor,
        parameters,
        "public",
        _intent(DeclarationTarget.CONSTRUCTOR, *markers),
        jvm_adapter_identity(identity),
    )


def _field(owner, language, name, type_, mutable=False):
    identity = jvm_field_identity(owner, name)
    return JvmFieldSource(
        _source(identity, language),
        jvm_owner_identity(owner),
        name,
        type_,
        _intent(DeclarationTarget.FIELD, SupernoteMarker.EXPORT),
        "public",
        mutable,
        False,
        jvm_field_accessor_identity(identity),
    )


def _method(
    owner,
    language,
    name,
    descriptor,
    parameters,
    result,
    *,
    static=False,
    async_=False,
    suspend=False,
):
    identity = jvm_declaration_identity(owner, name, descriptor)
    markers = [SupernoteMarker.EXPORT]
    if async_:
        markers.append(SupernoteMarker.ASYNC)
    return JvmDeclarationSource(
        _source(identity, language),
        jvm_owner_identity(owner),
        owner,
        name,
        descriptor,
        parameters,
        result.jvm_type,
        result.nullable,
        _intent(DeclarationTarget.METHOD, *markers),
        "public",
        jvm_adapter_identity(identity),
        language,
        suspend,
        static,
        result.arguments,
    )


def _matrix():
    language = JvmLanguage.KOTLIN
    point_name = "com.example.Point"
    stroke_name = "com.example.Stroke"
    color_name = "com.example.Color"
    point = JvmOwnerSource(
        _source(jvm_owner_identity(point_name), language),
        language,
        point_name,
        "Point",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (
            _constructor(
                point_name,
                language,
                "(DLjava/lang/Long;[B)V",
                (
                    JvmParameterSource("kotlin.Double", "x"),
                    JvmParameterSource("kotlin.Long", "tag", nullable=True),
                    JvmParameterSource("kotlin.ByteArray", "bytes"),
                ),
            ),
        ),
        (),
        fields=(
            _field(point_name, language, "x", JvmTypeSource("kotlin.Double")),
            _field(
                point_name,
                language,
                "tag",
                JvmTypeSource("kotlin.Long", nullable=True),
            ),
            _field(
                point_name,
                language,
                "bytes",
                JvmTypeSource("kotlin.ByteArray"),
            ),
        ),
        is_data=True,
    )
    color = JvmOwnerSource(
        _source(jvm_owner_identity(color_name), language),
        language,
        color_name,
        "Color",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (),
        (),
        enum_constants=("RED", "BLUE"),
    )
    strokes = JvmTypeSource(
        "kotlin.collections.List",
        arguments=(JvmTypeSource(stroke_name, nullable=True),),
    )
    stroke = JvmOwnerSource(
        _source(jvm_owner_identity(stroke_name), language),
        language,
        stroke_name,
        "Stroke",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (
            _constructor(
                stroke_name,
                language,
                "(Lcom/example/Point;)V",
                (JvmParameterSource(point_name, "point"),),
                SupernoteMarker.CONSTRUCTOR,
            ),
        ),
        (
            _method(
                stroke_name,
                language,
                "echoAll",
                "(Ljava/util/List;)Ljava/util/List;",
                (
                    JvmParameterSource(
                        strokes.jvm_type,
                        "values",
                        type_arguments=strokes.arguments,
                    ),
                ),
                strokes,
                async_=True,
            ),
            _method(
                stroke_name,
                language,
                "empty",
                "()Lcom/example/Stroke;",
                (),
                JvmTypeSource(stroke_name),
                static=True,
            ),
        ),
        fields=(
            _field(
                stroke_name,
                language,
                "color",
                JvmTypeSource(color_name),
                mutable=True,
            ),
            _field(
                stroke_name,
                language,
                "peer",
                JvmTypeSource(stroke_name, nullable=True),
                mutable=True,
            ),
        ),
    )
    return point, color, stroke


def test_jvm_route_plan_uses_exact_adapter_descriptors_and_nominal_types():
    owners = _matrix()
    api = project_jvm_owners(owners, feature_id=FEATURE)
    plan = plan_jvm_routes(api, owners)

    assert [item.named_type.public_name for item in plan.values] == ["Point"]
    assert [item.named_type.public_name for item in plan.enums] == ["Color"]
    assert [item.named_type.public_name for item in plan.objects] == ["Stroke"]
    point = plan.values[0]
    assert point.constructor.adapter_descriptor == (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;"
        "DLjava/lang/Long;[B)Lcom/example/Point;"
    )
    assert point.fields[1].getter_descriptor == (
        "(Lcom/example/Point;)Ljava/lang/Long;"
    )
    assert point.fields[1].setter_descriptor is None

    stroke = plan.objects[0]
    assert stroke.named_type.kind is SemanticTypeKind.OBJECT_REF
    assert stroke.constructor.adapter_descriptor == (
        "(Lcom/facebook/react/bridge/ReactApplicationContext;"
        "Lcom/example/Point;)Lcom/example/Stroke;"
    )
    assert stroke.methods[0].adapter_descriptor == (
        "(Lcom/example/Stroke;Ljava/util/List;)Ljava/util/List;"
    )
    assert stroke.methods[1].adapter_descriptor == "()Lcom/example/Stroke;"
    assert stroke.fields[0].setter_descriptor == (
        "(Lcom/example/Stroke;Lcom/example/Color;)V"
    )
    assert stroke.fields[1].getter_descriptor == (
        "(Lcom/example/Stroke;)Lcom/example/Stroke;"
    )


def test_jvm_suspend_adapter_descriptor_retains_completion_token():
    owner_name = "com.example.Worker"
    language = JvmLanguage.KOTLIN
    owner = JvmOwnerSource(
        _source(jvm_owner_identity(owner_name), language),
        language,
        owner_name,
        "Worker",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (),
        (
            _method(
                owner_name,
                language,
                "later",
                "()Lcom/example/Worker;",
                (),
                JvmTypeSource(owner_name),
                async_=True,
                suspend=True,
            ),
        ),
    )
    api = project_jvm_owners((owner,), feature_id=FEATURE)
    route = plan_jvm_routes(api, (owner,)).objects[0].methods[0]
    assert route.adapter_descriptor == (
        "(Lcom/example/Worker;J)Lkotlinx/coroutines/Job;"
    )
    assert route.suspend
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE, "4.0.0.dev0", (owner,)),
        api,
        feature_id=FEATURE,
        module_name="Drawing",
    )
    assert "SupernoteSuspendExecutor" in generated
    assert "register_jvm_async_completion" in generated
    assert "supernote_module_jvm_to_js_" in generated
    assert "operation->set_retained_state(retained_input_state)" in generated
    assert "JvmObjectHandleBase" in generated


def test_jvm_suspend_nullable_result_accepts_a_null_jobject():
    owner_name = "com.example.Worker"
    language = JvmLanguage.KOTLIN
    owner = JvmOwnerSource(
        _source(jvm_owner_identity(owner_name), language),
        language,
        owner_name,
        "Worker",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.OBJECT),
        (),
        (
            _method(
                owner_name,
                language,
                "maybeLater",
                "(Lcom/example/Worker;)Lcom/example/Worker;",
                (JvmParameterSource(owner_name, "other", nullable=True),),
                JvmTypeSource(owner_name, nullable=True),
                async_=True,
                suspend=True,
            ),
        ),
    )
    api = project_jvm_owners((owner,), feature_id=FEATURE)
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE, "4.0.0.dev0", (owner,)),
        api,
        feature_id=FEATURE,
        module_name="Drawing",
    )

    decode_start = generated.index(
        "auto *env = static_cast<JNIEnv *>(environment);"
    )
    decode_end = generated.index("state->success = true;", decode_start)
    decode = generated[decode_start:decode_end]
    assert "Kotlin coroutine result has no JNI environment" in decode
    assert "if (object == nullptr)" not in decode
    assert "decoded_result = object == nullptr" in decode
    assert "? ManagedJvmValue{}" in decode

    argument = generated.index("auto argument_0 =")
    retained = generated.index("auto retained_input_state =", argument)
    accepted = generated.index("accept_factory", retained)
    attached = generated.index("set_retained_state", accepted)
    queued = generated.index("workers().submit", attached)
    scheduled = generated.index("schedule_completion", attached)
    js_identity_lookup = generated.index(
        "supernote_module_jvm_object_registry(runtime)", scheduled
    )
    assert argument < retained < accepted < attached < queued
    assert attached < scheduled < js_identity_lookup < queued
    worker = generated[queued : generated.index("operation->set_work(work)", queued)]
    assert "facebook::jsi::Runtime" not in worker
    assert "supernote_module_jvm_object_registry" not in worker


def test_jvm_identity_registry_uses_weak_globals_hash_buckets_and_is_same_object():
    source = render_jvm_object_runtime()

    assert "NewWeakGlobalRef" in source
    assert "DeleteWeakGlobalRef" in source
    assert "jint identity_hash" in source
    assert "current->identity_hash != hash" in source
    assert "env->IsSameObject(weak, instance) != JNI_TRUE" in source
    assert "env->IsSameObject(weak, nullptr) == JNI_TRUE" in source
    assert "facebook::jsi::WeakObject" in source
    assert "a JVM object registry cannot cross JavaScript runtimes" in source
    assert "std::shared_ptr<void> strong_global" in source
    assert "ManagedJvmRef managed" in source
    assert "cleanup->submit(release)" in source


def test_generated_ksp_processor_emits_live_field_accessors():
    from pathlib import Path

    template = Path(
        "src/supernote_module_generator/templates/"
        "runtime.SupernoteModuleProcessor.kt.tmpl"
    ).read_text(encoding="utf-8")
    assert "owner.fields.forEach { field ->" in template
    assert 'fun get(owner: $ownerType): $fieldType' in template
    assert 'fun set(owner: $ownerType, value: $fieldType)' in template
    assert "adapterOutput(field.type, property)" in template
    assert 'owner.${kotlinIdentifier(field.name)}' in template
    assert "System.identityHashCode(value)" in template
    assert '"Identity_${hash(root.featureId).take(20)}"' in template
    assert '"List<${adapterBridgeType(type.arguments.single())}>"' in template
    assert '"$name${if (type.nullable) "?" else ""}.map { item -> $mapped }"' in template


def test_generated_ksp_processor_emits_an_empty_manifest_for_every_feature_root():
    from pathlib import Path

    template = Path(
        "src/supernote_module_generator/templates/"
        "runtime.SupernoteModuleProcessor.kt.tmpl"
    ).read_text(encoding="utf-8")

    assert "roots.sortedBy { it.featureId }.forEach { root ->" in template
    assert "generateFeature(root, grouped[root].orEmpty())" in template
    assert '"owners" to owners.map(::ownerJson)' in template


def test_jvm_byte_input_uses_one_view_snapshot_and_budgets_before_copy():
    owner_name = "com.example.BlobApiKt"
    language = JvmLanguage.KOTLIN
    declaration_id = jvm_declaration_identity(owner_name, "echoBytes", "([B)[B")
    declaration = JvmDeclarationSource(
        _source(declaration_id, language, "BlobApi.kt"),
        jvm_owner_identity(owner_name),
        owner_name,
        "echoBytes",
        "([B)[B",
        (JvmParameterSource("kotlin.ByteArray", "value"),),
        "kotlin.ByteArray",
        False,
        _intent(DeclarationTarget.FUNCTION, SupernoteMarker.EXPORT),
        "public",
        jvm_adapter_identity(declaration_id),
        language,
        False,
        True,
    )
    owner = JvmOwnerSource(
        _source(jvm_owner_identity(owner_name), language, "BlobApi.kt"),
        language,
        owner_name,
        "BlobApiKt",
        JvmOwnerForm.KOTLIN_TOP_LEVEL,
        _intent(DeclarationTarget.CLASS),
        (),
        (declaration,),
    )
    api = project_jvm_owners((owner,), feature_id=FEATURE)
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE, "4.0.0.dev0", (owner,)),
        api,
        feature_id=FEATURE,
        module_name="Drawing",
    )

    assert generated.count(
        'supernote_view_index(runtime, view, "byteOffset")'
    ) == 1
    assert generated.count(
        'supernote_view_index(runtime, view, "byteLength")'
    ) == 1
    budget = generated.index("if (snapshot.length > kMaxByteBufferBytes)")
    allocation = generated.index("std::vector<std::byte> result(snapshot.length)", budget)
    caller_snapshot = generated.index(
        "auto snapshot = supernote_snapshot_uint8_array(runtime, value)"
    )
    caller_copy = generated.index(
        "return supernote_copy_uint8_array(runtime, snapshot)", caller_snapshot
    )
    assert budget < allocation < caller_snapshot < caller_copy
    accepts_start = generated.index('"echoBytes.accepts"')
    check_start = generated.index('"echoBytes.checkArguments"')
    attach_start = generated.index("function = supernote_attach_preflight(", check_start)
    for preflight in (
        generated[accepts_start:check_start],
        generated[check_start:attach_start],
    ):
        assert preflight.count(
            "auto supernote_snapshot_0 = supernote_snapshot_uint8_array("
        ) == 1
        assert preflight.count(
            "supernote_check_uint8_array_snapshot_limit("
        ) == 1
        assert "supernote_copy_uint8_array" not in preflight
        assert "std::vector<std::byte> result" not in preflight
    assert generated.count(
        "auto supernote_snapshot_0 = supernote_snapshot_uint8_array("
    ) == 3
    assert generated.count(
        "supernote_copy_uint8_array(runtime, supernote_snapshot_0)"
    ) == 1
    assert "supernote_copy_uint8_array(runtime, arguments[0])" not in generated
    assert '"LIMIT_EXCEEDED"' in generated
    assert '"at most 33554432 bytes"' in generated


def test_jvm_object_codegen_emits_nominal_wrappers_converters_and_registry():
    owners = _matrix()
    api = project_jvm_owners(owners, feature_id=FEATURE)
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE, "4.0.0.dev0", owners),
        api,
        feature_id=FEATURE,
        module_name="Drawing",
    )

    assert "class GeneratedModuleJvmObject0HostObject final" in generated
    assert ": public JvmObjectHandleBase" in generated
    assert "try_extract_jvm_object" in generated
    assert "supernote_module_wrap_jvm_object_0" in generated
    assert "std::make_shared<JvmObjectRegistry>()" in generated
    assert "__supernoteModuleJvmObjectRegistry_2cfbc9ce6375" in generated
    assert "jvm-module-value-constructor:" in generated
    assert "jvm-module-enum-from:" in generated
    assert "jvm-module-field-get:" in generated
    assert "jvm-module-field-set:" in generated
    assert "listAdd" in generated
    assert "listGet" in generated
    assert "decodeString" not in generated
    assert "identityHash" in generated
    assert "IsSameObject" in generated
    assert "process_services().workers().submit" in generated
    assert "supernote_module_jvm_object_registry(runtime)" in generated
    assert "argument_0 = std::move(argument_0)" in generated
    assert "retained_input_state = std::make_shared<std::tuple<" in generated
    assert "v4" not in generated.lower()
    assert "operation->set_retained_state(retained_input_state)" in generated
    assert "schedule_completion" in generated
    assert generated.count("LocalFrame item_frame(env);") >= 2
    input_loop = generated.index(
        "for (std::uint64_t index = 0; index < length; ++index)"
    )
    input_frame = generated.index("LocalFrame item_frame(env);", input_loop)
    input_add = generated.index("CallStaticVoidMethodA", input_frame)
    assert input_loop < input_frame < input_add
    output_loop = generated.index("for (jint index = 0; index < length; ++index)")
    output_frame = generated.index("LocalFrame item_frame(env);", output_loop)
    output_get = generated.index("CallStaticObjectMethodA", output_frame)
    assert output_loop < output_frame < output_get
    own_index = generated.index("if (!supernote_array_has_own_index")
    item_read = generated.index("array.getValueAtIndex", own_index)
    assert own_index < item_read
    assert generated.count("budget.check_byte_buffer(path, snapshot.length)") >= 2
    assert 'exports.setProperty(runtime, "Stroke"' in generated
    assert "supernote_attach_preflight" in generated
    assert '"echoAll.accepts"' in generated
    assert '"echoAll.checkArguments"' in generated
    assert 'object_type.setProperty(runtime, "is"' in generated
    assert 'object_type.setProperty(runtime, "check"' in generated
    assert '"NOMINAL_MISMATCH", path, "Stroke"' in generated
    assert '"__supernoteJvmObjectInfo"' in generated
    assert 'exports.setProperty(runtime, "Point"' in generated
    assert 'exports.setProperty(runtime, "Color"' in generated
    assert "supernote_module_jvm_validate_js_" in generated
    assert generated.count(
        'supernote_throw_error(runtime, "IMPLEMENTATION_ERROR", error.what())'
    ) >= 3
