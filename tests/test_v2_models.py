from __future__ import annotations

from typing import get_type_hints

import pytest

from supernote_module_generator.cpp_projection import (
    CppProjectionError,
    cpp_type_table,
    project_cpp_function,
    project_cpp_functions,
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
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    ExecutionMode,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticConstructor,
    SemanticModelError,
    SemanticParameter,
    SemanticType,
    SourceProvenance,
    merge_semantic_apis,
    semantic_api_from_manifest,
)
from supernote_module_generator.source_models import (
    CppConstructorSource,
    CppFunctionSource,
    CppParameterSource,
    DeclarationTarget,
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmInjectedDependency,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    SourceIntent,
    SourceModelError,
    SupernoteMarker,
)


def provenance(
    identity: str = "cpp:document.cpp:loadPage(int32_t)",
    *,
    language: str = "cpp",
    path: str = "document.cpp",
) -> SourceProvenance:
    return SourceProvenance(identity, language, path, 12)


def intent(
    target: DeclarationTarget,
    *markers: SupernoteMarker,
) -> SourceIntent:
    return SourceIntent.from_markers(target, tuple(markers), first_line=8)


def cpp_function(
    *markers: SupernoteMarker,
    return_type: str = "std::vector<std::byte>",
    parameter_type: str = "std::int32_t",
    source: SourceProvenance | None = None,
) -> CppFunctionSource:
    return CppFunctionSource(
        source or provenance(),
        "loadPage",
        return_type,
        (CppParameterSource(parameter_type, "page"),),
        intent(DeclarationTarget.FUNCTION, *markers),
        noexcept=True,
        definition_offset=48,
    )


def object_method(
    owner_id: str,
    owner_name: str,
    *,
    kind: BindingKind = BindingKind.OBJECT_METHOD,
    role: DeclarationRole = DeclarationRole.EXPORTED,
    name: str = "pageCount",
    identity: str = "cpp:method:Document.pageCount",
) -> SemanticBinding:
    source = provenance(identity, path="Document.hpp")
    return SemanticBinding(
        f"binding:{identity}",
        kind,
        name,
        BindingCapabilities.for_role(role),
        ExecutionMode.SYNC,
        (),
        SemanticType.INT32,
        source,
        owner_id,
        owner_name,
    )


def test_initial_semantic_types_are_exact_and_stable():
    assert [
        item.value
        for item in (
            SemanticType.VOID,
            SemanticType.BOOL,
            SemanticType.INT32,
            SemanticType.INT64,
            SemanticType.FLOAT32,
            SemanticType.FLOAT64,
            SemanticType.STRING,
            SemanticType.BYTES,
        )
    ] == [
        "void",
        "bool",
        "int32",
        "int64",
        "float32",
        "float64",
        "string",
        "bytes",
    ]


def test_reachability_and_javascript_visibility_are_independent_and_fail_closed():
    assert BindingCapabilities.for_role(DeclarationRole.ORDINARY) == BindingCapabilities(
        False, False
    )
    assert BindingCapabilities.for_role(DeclarationRole.INTERNAL) == BindingCapabilities(
        True, False
    )
    assert BindingCapabilities.for_role(DeclarationRole.EXPORTED) == BindingCapabilities(
        True, True
    )
    with pytest.raises(SemanticModelError, match="must also be generated/routable"):
        BindingCapabilities(False, True)
    with pytest.raises(SemanticModelError, match="unknown declaration role"):
        BindingCapabilities.for_role("typo")  # type: ignore[arg-type]


def test_source_intent_validates_composable_source_located_markers_and_targets():
    exported_async = intent(
        DeclarationTarget.FUNCTION,
        SupernoteMarker.EXPORT,
        SupernoteMarker.ASYNC,
    )
    assert exported_async.role is DeclarationRole.EXPORTED
    assert exported_async.execution is ExecutionMode.ASYNC
    assert exported_async.occurrences[1].line == 9

    internal = intent(DeclarationTarget.METHOD, SupernoteMarker.INTERNAL)
    assert internal.role is DeclarationRole.INTERNAL
    assert internal.execution is ExecutionMode.SYNC

    selected = intent(DeclarationTarget.CONSTRUCTOR, SupernoteMarker.CONSTRUCTOR)
    assert selected.selects_constructor

    invalid = [
        (
            DeclarationTarget.FUNCTION,
            (SupernoteMarker.ASYNC,),
            "requires SupernotePluginExport or SupernotePluginInternal",
        ),
        (
            DeclarationTarget.FUNCTION,
            (SupernoteMarker.EXPORT, SupernoteMarker.INTERNAL),
            "cannot mark one declaration",
        ),
        (
            DeclarationTarget.METHOD,
            (SupernoteMarker.CONSTRUCTOR,),
            "valid only on a constructor",
        ),
        (
            DeclarationTarget.CLASS,
            (SupernoteMarker.EXPORT, SupernoteMarker.ASYNC),
            "classes require exactly one",
        ),
        (
            DeclarationTarget.FUNCTION,
            (SupernoteMarker.EXPORT, SupernoteMarker.EXPORT),
            "duplicate SupernotePluginExport marker",
        ),
    ]
    for target, markers, message in invalid:
        with pytest.raises(SourceModelError, match=message):
            intent(target, *markers)


def test_cpp_projection_maps_only_canonical_owned_types_and_ignores_ordinary_code():
    assert cpp_type_table() == {
        "void": SemanticType.VOID,
        "bool": SemanticType.BOOL,
        "int32_t": SemanticType.INT32,
        "std::int32_t": SemanticType.INT32,
        "int64_t": SemanticType.INT64,
        "std::int64_t": SemanticType.INT64,
        "float": SemanticType.FLOAT32,
        "double": SemanticType.FLOAT64,
        "std::string": SemanticType.STRING,
        "std::vector<std::byte>": SemanticType.BYTES,
    }
    binding = project_cpp_function(
        cpp_function(SupernoteMarker.EXPORT, SupernoteMarker.ASYNC)
    )
    assert binding is not None
    assert binding.name == "loadPage"
    assert binding.execution is ExecutionMode.ASYNC
    assert binding.parameters[0].type is SemanticType.INT32
    assert binding.result is SemanticType.BYTES
    assert binding.binding_id != binding.source.declaration_id
    assert project_cpp_function(cpp_function()) is None


@pytest.mark.parametrize(
    "unsupported",
    [
        "int",
        "long",
        "size_t",
        "const std::string &",
        "std::string_view",
        "const char *",
        "std::span<const std::byte>",
        "std::vector<uint8_t>",
    ],
)
def test_cpp_projection_rejects_noncanonical_marked_types(unsupported: str):
    with pytest.raises(CppProjectionError, match="document.cpp:12:1"):
        project_cpp_function(
            cpp_function(SupernoteMarker.EXPORT, parameter_type=unsupported)
        )


def test_cpp_projection_rejects_marked_c_but_not_ordinary_c():
    c_source = provenance(
        "c:document.c:loadPage(int32_t)", language="c", path="document.c"
    )
    with pytest.raises(CppProjectionError, match="ordinary C23 code"):
        project_cpp_function(cpp_function(SupernoteMarker.INTERNAL, source=c_source))
    assert project_cpp_function(cpp_function(source=c_source)) is None


def test_cpp_projection_fails_closed_for_wrong_frontend_and_split_tokens():
    wrong_frontend = provenance(
        "kotlin:document.cpp:loadPage", language="kotlin", path="document.cpp"
    )
    with pytest.raises(CppProjectionError, match=r"invalid C\+\+ frontend language"):
        project_cpp_function(
            cpp_function(SupernoteMarker.EXPORT, source=wrong_frontend)
        )
    with pytest.raises(CppProjectionError, match=r"unsupported marked C\+\+ type"):
        project_cpp_function(
            cpp_function(SupernoteMarker.EXPORT, parameter_type="i nt32_t")
        )


def test_semantic_api_is_backend_neutral_deterministic_and_validated():
    source = provenance()
    binding = SemanticBinding(
        "api:function:loadPage",
        BindingKind.FUNCTION,
        "loadPage",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        ExecutionMode.ASYNC,
        (SemanticParameter("page", SemanticType.INT32),),
        SemanticType.BYTES,
        source,
    )
    manifest = SemanticApi((binding,)).manifest()
    assert manifest["schema_version"] == 3
    assert manifest["kind"] == "supernote_v3_semantic_manifest"
    assert manifest["classes"] == []
    assert manifest["types"] == []
    assert manifest["functions"][0]["binding_id"] == "api:function:loadPage"
    assert manifest["functions"][0]["source_declaration_id"] == source.declaration_id
    assert "jniDescriptor" not in manifest["functions"][0]
    assert semantic_api_from_manifest(manifest).manifest() == manifest

    with pytest.raises(SemanticModelError, match="ordinary declarations"):
        SemanticBinding(
            "api:function:helper",
            BindingKind.FUNCTION,
            "helper",
            BindingCapabilities.for_role(DeclarationRole.ORDINARY),
            ExecutionMode.SYNC,
            (),
            SemanticType.VOID,
            source,
        )
    with pytest.raises(SemanticModelError, match="void is valid only"):
        SemanticParameter("bad", SemanticType.VOID)
    with pytest.raises(SemanticModelError, match="duplicate parameter name"):
        SemanticBinding(
            "api:function:duplicate",
            BindingKind.FUNCTION,
            "duplicate",
            BindingCapabilities.for_role(DeclarationRole.INTERNAL),
            ExecutionMode.SYNC,
            (
                SemanticParameter("value", SemanticType.INT32),
                SemanticParameter("value", SemanticType.INT32),
            ),
            SemanticType.VOID,
            source,
        )


def test_semantic_manifest_reader_rejects_versions_fields_and_merge_collisions():
    binding = project_cpp_function(cpp_function(SupernoteMarker.EXPORT))
    assert binding is not None
    api = SemanticApi((binding,))
    wrong_version = api.manifest()
    wrong_version["schema_version"] = 99
    with pytest.raises(SemanticModelError, match="incompatible semantic manifest"):
        semantic_api_from_manifest(wrong_version)

    unknown = api.manifest()
    unknown["backend"] = "jni"
    with pytest.raises(SemanticModelError, match="unknown backend"):
        semantic_api_from_manifest(unknown)

    with pytest.raises(SemanticModelError, match="semantic identity"):
        merge_semantic_apis(api, api)


def test_js_object_and_internal_service_have_distinct_enforced_semantics():
    object_id = "class:Document"
    object_source = provenance("cpp:class:Document", path="Document.hpp")
    constructor = SemanticConstructor(
        provenance("cpp:constructor:Document(string)", path="Document.hpp"),
        (SemanticParameter("path", SemanticType.STRING),),
    )
    method = object_method(object_id, "Document")
    item = SemanticClass(
        object_id,
        SemanticClassKind.JS_OBJECT,
        "Document",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        object_source,
        constructor,
        (method,),
    )
    assert item.manifest()["methods"] == [method.manifest()]

    service_id = "class:IndexService"
    service_source = provenance("cpp:class:IndexService", path="IndexService.hpp")
    service = SemanticClass(
        service_id,
        SemanticClassKind.INTERNAL_SERVICE,
        "IndexService",
        BindingCapabilities.for_role(DeclarationRole.INTERNAL),
        service_source,
        SemanticConstructor(
            provenance("cpp:constructor:IndexService()", path="IndexService.hpp")
        ),
        (
            object_method(
                service_id,
                "IndexService",
                kind=BindingKind.SERVICE_METHOD,
                role=DeclarationRole.INTERNAL,
                name="rebuild",
                identity="cpp:method:IndexService.rebuild",
            ),
        ),
    )
    assert not service.capabilities.javascript_public

    with pytest.raises(SemanticModelError, match="no caller-visible parameters"):
        SemanticClass(
            service_id,
            SemanticClassKind.INTERNAL_SERVICE,
            "IndexService",
            BindingCapabilities.for_role(DeclarationRole.INTERNAL),
            service_source,
            constructor,
        )
    with pytest.raises(SemanticModelError, match="cannot be JavaScript-public"):
        SemanticClass(
            service_id,
            SemanticClassKind.INTERNAL_SERVICE,
            "IndexService",
            BindingCapabilities.for_role(DeclarationRole.INTERNAL),
            service_source,
            SemanticConstructor(
                provenance("cpp:constructor:IndexService()", path="IndexService.hpp")
            ),
            (
                object_method(
                    service_id,
                    "IndexService",
                    kind=BindingKind.SERVICE_METHOD,
                    role=DeclarationRole.EXPORTED,
                    name="rebuild",
                    identity="cpp:method:IndexService.rebuild",
                ),
            ),
        )


def test_jvm_source_model_keeps_owner_constructor_adapter_and_injection_facts():
    owner_source = SourceProvenance(
        "jvm:owner:com.example.DocumentApi", "kotlin", "DocumentApi.kt", 8
    )
    constructor = JvmConstructorSource(
        SourceProvenance(
            "jvm:ctor:com.example.DocumentApi(Context)",
            "kotlin",
            "DocumentApi.kt",
            10,
        ),
        "(Landroid/content/Context;)V",
        (
            JvmParameterSource(
                "android.content.Context",
                "context",
                injected=JvmInjectedDependency.CONTEXT,
            ),
        ),
        "public",
        intent(DeclarationTarget.CONSTRUCTOR),
        "adapter:ctor:DocumentApi:context",
    )
    declaration = JvmDeclarationSource(
        SourceProvenance(
            "jvm:method:com.example.DocumentApi.loadPage:(I)[B",
            "kotlin",
            "DocumentApi.kt",
            42,
        ),
        owner_source.declaration_id,
        "com.example.DocumentApi",
        "loadPage",
        "(ILkotlin/coroutines/Continuation;)Ljava/lang/Object;",
        (JvmParameterSource("kotlin.Int", "page"),),
        "kotlin.ByteArray",
        False,
        intent(
            DeclarationTarget.METHOD,
            SupernoteMarker.EXPORT,
            SupernoteMarker.ASYNC,
        ),
        "public",
        "adapter:method:DocumentApi:loadPage:int",
        JvmLanguage.KOTLIN,
        is_suspend=True,
    )
    owner = JvmOwnerSource(
        owner_source,
        JvmLanguage.KOTLIN,
        "com.example.DocumentApi",
        "DocumentApi",
        JvmOwnerForm.CLASS,
        intent(DeclarationTarget.CLASS),
        (constructor,),
        (declaration,),
    )
    assert owner.constructors[0].parameters[0].injected is JvmInjectedDependency.CONTEXT
    assert owner.declarations[0].adapter_identity.startswith("adapter:method")
    assert owner.declarations[0].result_nullable is False


def test_jvm_source_model_rejects_impossible_suspend_and_owner_forms():
    source = SourceProvenance(
        "jvm:method:Example.load:()V", "java", "Example.java", 12
    )
    with pytest.raises(SourceModelError, match="only Kotlin"):
        JvmDeclarationSource(
            source,
            "jvm:owner:Example",
            "Example",
            "load",
            "()V",
            (),
            "void",
            False,
            intent(
                DeclarationTarget.METHOD,
                SupernoteMarker.EXPORT,
                SupernoteMarker.ASYNC,
            ),
            "public",
            "adapter:load",
            JvmLanguage.JAVA,
            is_suspend=True,
        )

    kotlin_source = SourceProvenance(
        "jvm:method:Example.load:()V", "kotlin", "Example.kt", 12
    )
    with pytest.raises(SourceModelError, match="requires SupernotePluginAsync"):
        JvmDeclarationSource(
            kotlin_source,
            "jvm:owner:Example",
            "Example",
            "load",
            "()V",
            (),
            "kotlin.Unit",
            False,
            intent(DeclarationTarget.METHOD, SupernoteMarker.EXPORT),
            "public",
            "adapter:load",
            JvmLanguage.KOTLIN,
            is_suspend=True,
        )

    with pytest.raises(SourceModelError, match="requires Kotlin"):
        JvmOwnerSource(
            SourceProvenance("jvm:owner:Example", "java", "Example.java", 1),
            JvmLanguage.JAVA,
            "Example",
            "Example",
            JvmOwnerForm.KOTLIN_OBJECT,
            intent(DeclarationTarget.CLASS),
            (),
            (),
        )


def test_lowering_routes_are_typed_and_enforce_semantic_execution_and_kind():
    binding = project_cpp_function(cpp_function(SupernoteMarker.EXPORT))
    assert binding is not None
    plan = LoweringPlan(
        binding.binding_id,
        binding.source.declaration_id,
        RouteKind.DIRECT_CPP_FUNCTION,
        SchedulingKind.INLINE,
        CppFunctionRoute("loadPage"),
    )
    plan.validate_binding(binding)
    plan.validate_source(cpp_function(SupernoteMarker.EXPORT))

    with pytest.raises(LoweringError, match="requires JvmMethodRoute"):
        LoweringPlan(
            binding.binding_id,
            binding.source.declaration_id,
            RouteKind.JVM_FUNCTION,
            SchedulingKind.INLINE,
            CppFunctionRoute("loadPage"),
        )
    with pytest.raises(LoweringError, match="suspending implementation"):
        LoweringPlan(
            binding.binding_id,
            binding.source.declaration_id,
            RouteKind.JVM_FUNCTION,
            SchedulingKind.KOTLIN_SUSPEND,
            JvmMethodRoute("DocumentApi", "loadPage", "(I)[B", "adapter", False),
        )

    async_binding = project_cpp_function(
        cpp_function(SupernoteMarker.EXPORT, SupernoteMarker.ASYNC)
    )
    assert async_binding is not None
    with pytest.raises(LoweringError, match="cannot use inline"):
        LoweringPlan(
            async_binding.binding_id,
            async_binding.source.declaration_id,
            RouteKind.DIRECT_CPP_FUNCTION,
            SchedulingKind.INLINE,
            CppFunctionRoute("loadPage"),
        ).validate_binding(async_binding)
    with pytest.raises(LoweringError, match="unknown lowering route"):
        LoweringPlan(
            binding.binding_id,
            binding.source.declaration_id,
            "unknown",  # type: ignore[arg-type]
            SchedulingKind.INLINE,
            CppFunctionRoute("loadPage"),
        )


def test_semantic_model_type_hints_are_resolvable_on_supported_python():
    assert get_type_hints(SemanticBinding)["owner_id"] is not None
    assert get_type_hints(SemanticClass)["constructor"] is SemanticConstructor


def test_projection_detects_public_collisions_after_filtering_ordinary_code():
    first = cpp_function(
        SupernoteMarker.EXPORT,
        source=provenance("cpp:a.cpp:loadPage(int32_t)", path="a.cpp"),
    )
    second = cpp_function(
        SupernoteMarker.EXPORT,
        source=provenance("cpp:b.cpp:loadPage(int32_t)", path="b.cpp"),
    )
    with pytest.raises(SemanticModelError, match="JavaScript-public top-level name"):
        project_cpp_functions((first, second))
