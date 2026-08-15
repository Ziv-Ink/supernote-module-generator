import json
from pathlib import Path

import pytest

from supernote_module_generator.jvm_manifest import (
    JVM_MANIFEST_SCHEMA_VERSION,
    JvmManifestError,
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_owner_identity,
    read_jvm_manifest,
    write_jvm_manifest,
)
from supernote_module_generator.jvm_projection import (
    JvmProjectionError,
    jvm_type_table,
    project_jvm_owners,
)
from supernote_module_generator.jvm_codegen import render_jvm_feature_jsi
from supernote_module_generator.internal_codegen import render_cpp_internal_facade
from supernote_module_generator.semantic import (
    DeclarationRole,
    ExecutionMode,
    SemanticClassKind,
    SemanticModelError,
    SemanticType,
    SourceProvenance,
)
from supernote_module_generator.source_models import (
    DeclarationTarget,
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmInjectedDependency,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    SourceIntent,
    SupernoteMarker,
)


FEATURE_ID = "supernote:feature:0123456789abcdef"


def provenance(identity: str, language: JvmLanguage, path: str, line: int):
    return SourceProvenance(identity, language.value, path, line, 1)


def intent(target: DeclarationTarget, *markers: SupernoteMarker):
    return SourceIntent.from_markers(target, markers, first_line=4)


def constructor(
    owner: str,
    language: JvmLanguage,
    descriptor: str = "()V",
    parameters: tuple[JvmParameterSource, ...] = (),
    *markers: SupernoteMarker,
):
    identity = jvm_declaration_identity(owner, "<init>", descriptor)
    return JvmConstructorSource(
        provenance(identity, language, "Api.kt", 8),
        descriptor,
        parameters,
        "public",
        intent(DeclarationTarget.CONSTRUCTOR, *markers),
        jvm_adapter_identity(identity),
    )


def declaration(
    owner: str,
    language: JvmLanguage,
    name: str,
    descriptor: str,
    parameters: tuple[JvmParameterSource, ...],
    result: str,
    *markers: SupernoteMarker,
    target: DeclarationTarget = DeclarationTarget.FUNCTION,
    suspend: bool = False,
    static: bool = False,
):
    identity = jvm_declaration_identity(owner, name, descriptor)
    return JvmDeclarationSource(
        provenance(identity, language, "Api.kt" if language is JvmLanguage.KOTLIN else "Api.java", 12),
        jvm_owner_identity(owner),
        owner,
        name,
        descriptor,
        parameters,
        result,
        False,
        intent(target, *markers),
        "public",
        jvm_adapter_identity(identity),
        language,
        suspend,
        static,
    )


def ordinary_kotlin_owner() -> JvmOwnerSource:
    owner = "com.example.DocumentApi"
    context = JvmParameterSource(
        "android.content.Context",
        "context",
        injected=JvmInjectedDependency.CONTEXT,
    )
    load = declaration(
        owner,
        JvmLanguage.KOTLIN,
        "loadPage",
        "(I)[B",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
        suspend=True,
    )
    return JvmOwnerSource(
        provenance(jvm_owner_identity(owner), JvmLanguage.KOTLIN, "Api.kt", 3),
        JvmLanguage.KOTLIN,
        owner,
        "DocumentApi",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS),
        (constructor(owner, JvmLanguage.KOTLIN, "(Landroid/content/Context;)V", (context,)),),
        (load,),
    )


def synchronous_kotlin_owner() -> JvmOwnerSource:
    owner = "com.example.DocumentApi"
    context = JvmParameterSource(
        "android.content.Context",
        "context",
        injected=JvmInjectedDependency.CONTEXT,
    )
    greet = declaration(
        owner,
        JvmLanguage.KOTLIN,
        "greet",
        "(Ljava/lang/String;)Ljava/lang/String;",
        (JvmParameterSource("kotlin.String", "name"),),
        "kotlin.String",
        SupernoteMarker.EXPORT,
    )
    return JvmOwnerSource(
        provenance(jvm_owner_identity(owner), JvmLanguage.KOTLIN, "Api.kt", 3),
        JvmLanguage.KOTLIN,
        owner,
        "DocumentApi",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS),
        (
            constructor(
                owner,
                JvmLanguage.KOTLIN,
                "(Landroid/content/Context;)V",
                (context,),
            ),
        ),
        (greet,),
    )
def test_manifest_round_trip_is_deterministic_versioned_and_backend_specific(
    tmp_path: Path,
):
    manifest = JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (ordinary_kotlin_owner(),))
    path = tmp_path / "jvm-source.json"
    write_jvm_manifest(path, manifest)
    first = path.read_bytes()
    parsed = read_jvm_manifest(path, expected_feature_id=FEATURE_ID)
    write_jvm_manifest(path, parsed)

    assert path.read_bytes() == first
    raw = json.loads(first)
    assert raw["schema_version"] == JVM_MANIFEST_SCHEMA_VERSION
    declaration_raw = raw["owners"][0]["declarations"][0]
    assert declaration_raw["jvm_descriptor"] == "(I)[B"
    assert declaration_raw["is_suspend"] is True
    assert "javascript_name" not in declaration_raw
    assert "jsi" not in json.dumps(raw).lower()


def test_projection_maps_kotlin_suspend_to_common_semantics_without_losing_route_facts():
    owner = ordinary_kotlin_owner()
    api = project_jvm_owners((owner,))
    binding = api.functions[0]

    assert binding.name == "loadPage"
    assert binding.capabilities.role is DeclarationRole.EXPORTED
    assert binding.execution is ExecutionMode.ASYNC
    assert binding.parameters[0].type is SemanticType.INT32
    assert binding.result is SemanticType.BYTES
    assert owner.declarations[0].is_suspend is True
    assert owner.declarations[0].jvm_descriptor == "(I)[B"


def test_kotlin_suspend_route_uses_coroutine_job_and_common_completion():
    owner = ordinary_kotlin_owner()
    source = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        project_jvm_owners((owner,)),
        feature_id=FEATURE_ID,
        module_name="Document",
    )

    assert "Lkotlinx/coroutines/Job;" in source
    assert '"SupernoteSuspendExecutor"' in source
    assert "register_jvm_async_completion" in source
    assert "discard_jvm_async_completion" in source
    assert "operation->set_cancel_hook" in source
    assert '"cancel"' in source
    assert "CallStaticObjectMethodA" in source
    assert "process_services().workers().submit" in source
    assert "schedule_completion" in source
    assert "runtime_pointer" in source
    assert 'getPropertyAsFunction(runtime, "Map")' in source
    assert 'getPropertyAsFunction(runtime, "delete")' in source


def test_sync_jvm_route_targets_ksp_adapter_and_feature_scoped_owner():
    owner = synchronous_kotlin_owner()
    manifest = JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,))
    api = project_jvm_owners((owner,))
    source = render_jvm_feature_jsi(
        manifest,
        api,
        feature_id=FEATURE_ID,
        module_name="Document",
    )

    adapter = owner.declarations[0].adapter_identity.rsplit(".", 1)[-1]
    assert f"supernote.generated.adapters.Adapter_{adapter}" in source
    assert "(Lcom/example/DocumentApi;[B)[B" in source
    assert 'feature_session->service<JvmOwner>' in source
    assert "CallStaticObjectMethodA" in source
    assert 'exports.setProperty(runtime, "greet"' in source
    assert "FindClass" not in source
    assert 'loadClass' in source


def test_blocking_jvm_async_route_uses_shared_worker_and_owned_values():
    owner_name = "com.example.DocumentApi"
    context = JvmParameterSource(
        "android.content.Context",
        "context",
        injected=JvmInjectedDependency.CONTEXT,
    )
    load = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadPage",
        "(I)[B",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
    )
    owner = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Api.kt", 3),
        JvmLanguage.KOTLIN,
        owner_name,
        "DocumentApi",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS),
        (
            constructor(
                owner_name,
                JvmLanguage.KOTLIN,
                "(Landroid/content/Context;)V",
                (context,),
            ),
        ),
        (load,),
    )
    source = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        project_jvm_owners((owner,)),
        feature_id=FEATURE_ID,
        module_name="Document",
    )

    assert '"unknown Kotlin/Java implementation failure"' in source
    assert '"Kotlin/Java implementation failed"' in source
    assert "implementation_exception_message" in source
    assert "catch (const JvmImplementationFailure &error)" in source
    assert '"getMessage"' in source
    assert "class LocalReference" in source
    assert "DeleteLocalRef" in source
    assert "ExceptionOccurred" in source
    assert '"unknown C++ implementation failure"' not in source

    assert 'getPropertyAsFunction(runtime, "Promise")' in source
    assert "process_services().workers().submit" in source
    assert "auto implementation_feature = weak_feature.lock()" in source
    assert "invoke(std::move(implementation_feature)" in source
    assert "implementation_feature.reset();" in source
    assert "feature_session->service<JvmOwner>" not in source
    assert "implementation_feature->service<JvmOwner>" in source
    assert "CallStaticObjectMethodA" in source
    assert "supernote_input_0" in source
    worker = source.index("auto invoke =")
    assert "facebook::jsi::Runtime" not in source[worker:source.index("};", worker)]


def test_kotlin_and_java_canonical_type_tables_are_exact():
    assert jvm_type_table(JvmLanguage.KOTLIN) == {
        "kotlin.Unit": SemanticType.VOID,
        "kotlin.Boolean": SemanticType.BOOL,
        "kotlin.Int": SemanticType.INT32,
        "kotlin.Long": SemanticType.INT64,
        "kotlin.Float": SemanticType.FLOAT32,
        "kotlin.Double": SemanticType.FLOAT64,
        "kotlin.String": SemanticType.STRING,
        "kotlin.ByteArray": SemanticType.BYTES,
    }
    assert jvm_type_table(JvmLanguage.JAVA) == {
        "void": SemanticType.VOID,
        "boolean": SemanticType.BOOL,
        "int": SemanticType.INT32,
        "long": SemanticType.INT64,
        "float": SemanticType.FLOAT32,
        "double": SemanticType.FLOAT64,
        "java.lang.String": SemanticType.STRING,
        "byte[]": SemanticType.BYTES,
    }


def test_jvm_export_object_uses_selected_constructor_and_only_marked_members():
    owner_name = "com.example.Document"
    first = constructor(
        owner_name,
        JvmLanguage.KOTLIN,
        "(J)V",
        (JvmParameterSource("kotlin.Long", "handle"),),
    )
    selected = constructor(
        owner_name,
        JvmLanguage.KOTLIN,
        "(Ljava/lang/String;)V",
        (JvmParameterSource("kotlin.String", "path"),),
        SupernoteMarker.CONSTRUCTOR,
    )
    method = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "pageCount",
        "()I",
        (),
        "kotlin.Int",
        SupernoteMarker.EXPORT,
        target=DeclarationTarget.METHOD,
    )
    hidden = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "hiddenCache",
        "()V",
        (),
        "kotlin.Unit",
        SupernoteMarker.INTERNAL,
        target=DeclarationTarget.METHOD,
    )
    owner = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Document.kt", 2),
        JvmLanguage.KOTLIN,
        owner_name,
        "Document",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.EXPORT),
        (first, selected),
        (method, hidden),
    )
    semantic = project_jvm_owners((owner,)).classes[0]

    assert semantic.kind is SemanticClassKind.JS_OBJECT
    assert semantic.constructor.parameters[0].type is SemanticType.STRING
    assert [item.name for item in semantic.methods] == ["pageCount", "hiddenCache"]
    assert semantic.methods[1].capabilities.role is DeclarationRole.INTERNAL
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        project_jvm_owners((owner,)),
        feature_id=FEATURE_ID,
        module_name="Documents",
    )
    assert "GeneratedJvmObject0HostObject" in generated
    assert "Object::createFromHostObject" in generated
    assert 'property == "pageCount"' in generated
    assert 'property == "hiddenCache"' not in generated
    assert "method_route_1_" not in generated
    assert "std::shared_ptr<JvmOwner> owner_" in generated


def test_java_export_object_has_distinct_instance_and_worker_async_routes():
    owner_name = "com.example.JavaDocument"
    selected = constructor(
        owner_name,
        JvmLanguage.JAVA,
        "(J)V",
        (JvmParameterSource("long", "handle"),),
    )
    value = declaration(
        owner_name,
        JvmLanguage.JAVA,
        "value",
        "()J",
        (),
        "long",
        SupernoteMarker.EXPORT,
        target=DeclarationTarget.METHOD,
    )
    load = declaration(
        owner_name,
        JvmLanguage.JAVA,
        "load",
        "(I)[B",
        (JvmParameterSource("int", "page"),),
        "byte[]",
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
        target=DeclarationTarget.METHOD,
    )
    owner = JvmOwnerSource(
        provenance(
            jvm_owner_identity(owner_name), JvmLanguage.JAVA, "JavaDocument.java", 2
        ),
        JvmLanguage.JAVA,
        owner_name,
        "JavaDocument",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.EXPORT),
        (selected,),
        (value, load),
    )
    semantic = project_jvm_owners((owner,))
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        semantic,
        feature_id=FEATURE_ID,
        module_name="Documents",
    )

    assert semantic.classes[0].kind is SemanticClassKind.JS_OBJECT
    assert "Object::createFromHostObject" in generated
    assert "CallStaticObjectMethodA" in generated
    assert 'property == "value"' in generated
    assert 'property == "load"' in generated
    assert "process_services().workers().submit" in generated
    assert "auto owner = owner_" in generated


def test_blocking_jvm_async_object_method_retains_global_receiver():
    owner_name = "com.example.Document"
    load = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadPage",
        "(I)[B",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
        target=DeclarationTarget.METHOD,
    )
    owner = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Document.kt", 2),
        JvmLanguage.KOTLIN,
        owner_name,
        "Document",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.EXPORT),
        (constructor(owner_name, JvmLanguage.KOTLIN),),
        (load,),
    )
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        project_jvm_owners((owner,)),
        feature_id=FEATURE_ID,
        module_name="Documents",
    )

    assert 'getPropertyAsFunction(runtime, "Promise")' in generated
    assert "auto owner = owner_;" in generated
    assert "auto invoke = [route, owner]" in generated
    assert "process_services().workers().submit" in generated
    assert "jvm_arguments[0].l" in generated
    assert "owner->value.get()" in generated
    assert "CallStaticObjectMethodA" in generated


def test_suspend_jvm_object_method_retains_receiver_until_job_finishes():
    owner_name = "com.example.Document"
    load = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadPage",
        "(ILkotlin/coroutines/Continuation;)Ljava/lang/Object;",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
        target=DeclarationTarget.METHOD,
        suspend=True,
    )
    owner = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Document.kt", 2),
        JvmLanguage.KOTLIN,
        owner_name,
        "Document",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.EXPORT),
        (constructor(owner_name, JvmLanguage.KOTLIN),),
        (load,),
    )
    generated = render_jvm_feature_jsi(
        JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,)),
        project_jvm_owners((owner,)),
        feature_id=FEATURE_ID,
        module_name="Documents",
    )

    assert 'property == "loadPage"' in generated
    assert "auto owner = owner_;" in generated
    assert "operation, weak_feature, route, cancel_route, completion_id, owner" in generated
    assert "owner->value.get()" in generated
    assert "Lkotlinx/coroutines/Job;" in generated
    assert "operation->set_cancel_hook" in generated


def test_internal_jvm_class_is_a_hidden_feature_service():
    owner_name = "com.example.IndexService"
    method = declaration(
        owner_name,
        JvmLanguage.JAVA,
        "rebuild",
        "()V",
        (),
        "void",
        SupernoteMarker.INTERNAL,
        target=DeclarationTarget.METHOD,
    )
    owner = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.JAVA, "IndexService.java", 2),
        JvmLanguage.JAVA,
        owner_name,
        "IndexService",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS, SupernoteMarker.INTERNAL),
        (constructor(owner_name, JvmLanguage.JAVA),),
        (method,),
    )
    semantic = project_jvm_owners((owner,)).classes[0]

    assert semantic.kind is SemanticClassKind.INTERNAL_SERVICE
    assert semantic.capabilities.javascript_public is False
    assert semantic.methods[0].capabilities.role is DeclarationRole.INTERNAL
    manifest = JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,))
    api = project_jvm_owners((owner,))
    generated = render_jvm_feature_jsi(
        manifest,
        api,
        feature_id=FEATURE_ID,
        module_name="Documents",
    )
    header, _ = render_cpp_internal_facade(
        Path("/does/not/need/native/sources"),
        module_name="Documents",
        feature_id=FEATURE_ID,
        jvm_manifest=manifest,
        jvm_semantic=api,
    )

    assert "struct IndexService final" in header
    assert "static void rebuild();" in header
    assert "IndexService::rebuild" in generated
    assert "feature->service<JvmOwner>" in generated
    assert 'exports.setProperty(runtime, "rebuild"' not in generated


def test_internal_jvm_functions_share_cpp_facade_across_sync_worker_and_suspend():
    owner_name = "com.example.FeatureApiKt"
    value = JvmParameterSource("kotlin.Int", "page")
    sync = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "pageCount",
        "(I)I",
        (value,),
        "kotlin.Int",
        SupernoteMarker.INTERNAL,
        static=True,
    )
    blocking = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadBlocking",
        "(I)[B",
        (value,),
        "kotlin.ByteArray",
        SupernoteMarker.INTERNAL,
        SupernoteMarker.ASYNC,
        static=True,
    )
    suspended = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadSuspend",
        "(ILkotlin/coroutines/Continuation;)Ljava/lang/Object;",
        (value,),
        "kotlin.ByteArray",
        SupernoteMarker.INTERNAL,
        SupernoteMarker.ASYNC,
        suspend=True,
        static=True,
    )
    owner = JvmOwnerSource(
        provenance(
            jvm_owner_identity(owner_name),
            JvmLanguage.KOTLIN,
            "FeatureApi.kt",
            2,
        ),
        JvmLanguage.KOTLIN,
        owner_name,
        "FeatureApiKt",
        JvmOwnerForm.KOTLIN_TOP_LEVEL,
        intent(DeclarationTarget.CLASS),
        (),
        (sync, blocking, suspended),
    )
    manifest = JvmSourceManifest(FEATURE_ID, "2.0.0.dev0", (owner,))
    api = project_jvm_owners((owner,))
    generated = render_jvm_feature_jsi(
        manifest,
        api,
        feature_id=FEATURE_ID,
        module_name="Documents",
    )
    header, _ = render_cpp_internal_facade(
        Path("/does/not/need/native/sources"),
        module_name="Documents",
        feature_id=FEATURE_ID,
        jvm_manifest=manifest,
        jvm_semantic=api,
    )

    assert "std::int32_t pageCount(std::int32_t page);" in header
    assert "void loadBlocking(" in header
    assert "void loadSuspend(" in header
    assert "supernote::Result<std::vector<std::byte>>" in header
    assert "namespace supernote::internal::Documents" in generated
    assert "process_services().workers().submit" in generated
    assert "register_jvm_async_completion" in generated
    assert "Lkotlinx/coroutines/Job;" in generated
    assert "claim_internal_completion" in generated
    assert generated.count("feature->accept({}, std::move(callback))") == 2
    assert "operation->take_internal_completion()" in generated
    assert "[operation, weak_feature, callback" not in generated
    assert "deliver_internal_callback" in generated
    assert "FeatureCallScope" in generated
    assert "feature->service<LazyJvmRoute>" in generated
    assert 'exports.setProperty(runtime, "pageCount"' not in generated
    assert 'exports.setProperty(runtime, "loadBlocking"' not in generated
    assert 'exports.setProperty(runtime, "loadSuspend"' not in generated
    assert "Promise" not in generated[generated.index("namespace supernote::internal") :]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw.update(schema_version=99), "incompatible JVM manifest schema"),
        (lambda raw: raw.update(extra=True), "unknown extra"),
        (
            lambda raw: raw["owners"][0]["declarations"][0].update(
                adapter_identity="public-name-dependent"
            ),
            "adapter_identity is not deterministic",
        ),
    ],
)
def test_manifest_rejects_incompatible_or_guessed_boundary_data(
    tmp_path: Path, change, message: str
):
    raw = JvmSourceManifest(
        FEATURE_ID, "2.0.0.dev0", (ordinary_kotlin_owner(),)
    ).manifest()
    change(raw)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(JvmManifestError, match=message):
        read_jvm_manifest(path, expected_feature_id=FEATURE_ID)


@pytest.mark.parametrize(
    "unsupported",
    ["kotlin.Int?", "kotlin.collections.List", "java.lang.Integer", "java.nio.ByteBuffer"],
)
def test_projection_rejects_noncanonical_jvm_types(unsupported: str):
    owner = ordinary_kotlin_owner()
    language = (
        JvmLanguage.JAVA if unsupported.startswith("java.") else JvmLanguage.KOTLIN
    )
    source = declaration(
        "com.example.Bad",
        language,
        "bad",
        "(Ljava/lang/Object;)V",
        (JvmParameterSource(unsupported, "value"),),
        "void" if language is JvmLanguage.JAVA else "kotlin.Unit",
        SupernoteMarker.EXPORT,
        static=True,
    )
    bad_owner = JvmOwnerSource(
        provenance(jvm_owner_identity("com.example.Bad"), language, "Bad.kt", 1),
        language,
        "com.example.Bad",
        "Bad",
        JvmOwnerForm.JAVA_STATIC if language is JvmLanguage.JAVA else JvmOwnerForm.KOTLIN_TOP_LEVEL,
        intent(DeclarationTarget.CLASS),
        (),
        (source,),
    )
    with pytest.raises(JvmProjectionError, match="unsupported marked"):
        project_jvm_owners((bad_owner,))


def test_cross_language_public_name_collisions_fail_in_common_semantics():
    first = ordinary_kotlin_owner()
    owner_name = "com.example.OtherKt"
    second_binding = declaration(
        owner_name,
        JvmLanguage.KOTLIN,
        "loadPage",
        "(I)[B",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        SupernoteMarker.EXPORT,
        static=True,
    )
    second = JvmOwnerSource(
        provenance(jvm_owner_identity(owner_name), JvmLanguage.KOTLIN, "Other.kt", 1),
        JvmLanguage.KOTLIN,
        owner_name,
        "OtherKt",
        JvmOwnerForm.KOTLIN_TOP_LEVEL,
        intent(DeclarationTarget.CLASS),
        (),
        (second_binding,),
    )
    with pytest.raises(SemanticModelError, match="JavaScript-public top-level name"):
        project_jvm_owners((first, second))
