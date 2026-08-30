from __future__ import annotations

import json
from pathlib import Path

from supernote_module_generator import binding_codegen
from supernote_module_generator.cross_family_codegen import build_cross_family_renderer
from supernote_module_generator.internal_codegen import render_cpp_internal_facade
from supernote_module_generator.jvm_codegen import (
    _internal_suspend_decode,
    render_jvm_feature_jsi,
)
from supernote_module_generator.jvm_manifest import (
    JvmSourceManifest,
    jvm_adapter_identity,
    jvm_declaration_identity,
    jvm_field_accessor_identity,
    jvm_field_identity,
    jvm_owner_identity,
)
from supernote_module_generator.jvm_projection import project_jvm_owners
from supernote_module_generator.semantic import (
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
    SupernoteMarker,
)


FEATURE = "supernote:feature:7777777777777777"
PACKAGE = "com.example.cross"


def _provenance(identity: str, line: int) -> SourceProvenance:
    return SourceProvenance(identity, "kotlin", "Cross.kt", line, 1)


def _intent(target: DeclarationTarget, *markers: SupernoteMarker) -> SourceIntent:
    return SourceIntent.from_markers(target, markers)


def _field(owner: str, name: str, type_: JvmTypeSource, line: int) -> JvmFieldSource:
    identity = jvm_field_identity(owner, name)
    return JvmFieldSource(
        _provenance(identity, line),
        jvm_owner_identity(owner),
        name,
        type_,
        _intent(DeclarationTarget.FIELD, SupernoteMarker.EXPORT),
        "public",
        False,
        False,
        jvm_field_accessor_identity(identity),
    )


def _jvm_manifest(*, suspend: bool = False) -> JvmSourceManifest:
    mode_owner = f"{PACKAGE}.Mode"
    payload_owner = f"{PACKAGE}.Payload"
    api_owner = f"{PACKAGE}.CrossKt"
    mode = JvmOwnerSource(
        _provenance(jvm_owner_identity(mode_owner), 3),
        JvmLanguage.KOTLIN,
        mode_owner,
        "Mode",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (),
        (),
        enum_constants=("One", "Two"),
    )
    field_types = (
        ("count", JvmTypeSource("kotlin.Int")),
        ("text", JvmTypeSource("kotlin.String")),
        ("bytes", JvmTypeSource("kotlin.ByteArray")),
        ("mode", JvmTypeSource(mode_owner)),
        (
            "tags",
            JvmTypeSource(
                "kotlin.collections.List",
                arguments=(JvmTypeSource("kotlin.String", nullable=True),),
            ),
        ),
        ("score", JvmTypeSource("kotlin.Double", nullable=True)),
    )
    constructor_id = jvm_declaration_identity(
        payload_owner,
        "<init>",
        f"(ILjava/lang/String;[BL{mode_owner.replace('.', '/')};Ljava/util/List;Ljava/lang/Double;)V",
    )
    constructor = JvmConstructorSource(
        _provenance(constructor_id, 8),
        f"(ILjava/lang/String;[BL{mode_owner.replace('.', '/')};Ljava/util/List;Ljava/lang/Double;)V",
        tuple(
            JvmParameterSource(value.jvm_type, name, value.nullable, type_arguments=value.arguments)
            for name, value in field_types
        ),
        "public",
        _intent(DeclarationTarget.CONSTRUCTOR),
        jvm_adapter_identity(constructor_id),
    )
    payload = JvmOwnerSource(
        _provenance(jvm_owner_identity(payload_owner), 7),
        JvmLanguage.KOTLIN,
        payload_owner,
        "Payload",
        JvmOwnerForm.CLASS,
        _intent(DeclarationTarget.CLASS, SupernoteMarker.VALUE),
        (constructor,),
        (),
        fields=tuple(
            _field(payload_owner, name, value, 9 + index)
            for index, (name, value) in enumerate(field_types)
        ),
        is_data=True,
    )
    route_id = jvm_declaration_identity(
        api_owner,
        "roundTrip",
        f"(L{payload_owner.replace('.', '/')};)L{payload_owner.replace('.', '/')};",
    )
    route = JvmDeclarationSource(
        _provenance(route_id, 20),
        jvm_owner_identity(api_owner),
        api_owner,
        "roundTrip",
        f"(L{payload_owner.replace('.', '/')};)L{payload_owner.replace('.', '/')};",
        (JvmParameterSource(payload_owner, "payload"),),
        payload_owner,
        False,
        _intent(
            DeclarationTarget.FUNCTION,
            SupernoteMarker.INTERNAL,
            *([SupernoteMarker.ASYNC] if suspend else []),
        ),
        "public",
        jvm_adapter_identity(route_id),
        JvmLanguage.KOTLIN,
        suspend,
        True,
    )
    api = JvmOwnerSource(
        _provenance(jvm_owner_identity(api_owner), 19),
        JvmLanguage.KOTLIN,
        api_owner,
        "CrossKt",
        JvmOwnerForm.KOTLIN_TOP_LEVEL,
        _intent(DeclarationTarget.CLASS),
        (),
        (route,),
    )
    return JvmSourceManifest(FEATURE, "4.0.0.dev0", (mode, payload, api))


def _module(tmp_path: Path) -> Path:
    root = tmp_path / "cross"
    cpp = root / "android/src/main/cpp"
    cpp.mkdir(parents=True)
    (root / ".supernote-module.json").write_text(
        json.dumps({"feature_id": FEATURE}), encoding="utf-8"
    )
    (cpp / "Cross.hpp").write_text(
        """#pragma once
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>
namespace cross {
// @SupernotePluginValue
enum class Mode { One, Two };
// @SupernotePluginValue
struct Payload {
  // @SupernotePluginExport
  std::int32_t count;
  // @SupernotePluginExport
  std::string text;
  // @SupernotePluginExport
  std::vector<std::byte> bytes;
  // @SupernotePluginExport
  Mode mode;
  // @SupernotePluginExport
  std::vector<std::optional<std::string>> tags;
  // @SupernotePluginExport
  std::optional<double> score;
};
}
""",
        encoding="utf-8",
    )
    return root


def test_copied_cross_family_codegen_is_typed_recursive_and_hidden(tmp_path: Path):
    root = _module(tmp_path)
    cpp = binding_codegen.scan_cpp_semantic_model(root, module_name="Cross")
    manifest = _jvm_manifest()
    jvm = project_jvm_owners(manifest.owners, feature_id=FEATURE)
    semantic = merge_semantic_apis(cpp, jvm)
    renderer = build_cross_family_renderer(
        root,
        semantic,
        manifest,
        feature_id=FEATURE,
        module_name="Cross",
    )

    helpers = renderer.render_helpers()
    binding = next(item for item in semantic.functions if item.name == "roundTrip")
    invocation = renderer.worker_invocation(binding, False)
    header, internal = render_cpp_internal_facade(
        root,
        module_name="Cross",
        feature_id=FEATURE,
        jvm_manifest=manifest,
        jvm_semantic=jvm,
        cross_family=renderer,
    )
    generated_jvm = render_jvm_feature_jsi(
        manifest,
        jvm,
        feature_id=FEATURE,
        module_name="Cross",
        cross_family=renderer,
    )

    assert "::cross::Payload roundTrip(::cross::Payload payload);" in header
    assert "supernote_module_cross_to_jvm_" in helpers
    assert "supernote_module_cross_from_jvm_" in helpers
    assert "check_array_length" in helpers
    assert "check_string_bytes" in helpers
    assert "check_byte_buffer" in helpers
    assert "std::optional<std::string>" in helpers
    assert "::cross::Mode::One" in helpers
    assert "return ::cross::Payload{" in helpers
    assert "jvm_arguments[0].l = cross_argument_0" in invocation
    assert "cross_budget" in invocation
    worker = generated_jvm[
        generated_jvm.index("internal_function_0(") :
        generated_jvm.index("void register_jvm_feature")
    ]
    assert "feature.reset();" not in worker
    assert 'exports.setProperty(runtime, "roundTrip"' not in generated_jvm

    copied_section = helpers + invocation
    for forbidden in (
        "uintptr_t",
        "reinterpret_cast<jlong",
        "JvmObjectHandle",
        "ManagedRef",
        "serialize",
        "JSON",
        "dynamic_handle",
        "Proxy",
    ):
        assert forbidden not in copied_section


def test_composite_suspend_cross_family_route_uses_typed_phase8_completion(
    tmp_path: Path,
):
    root = _module(tmp_path)
    cpp = binding_codegen.scan_cpp_semantic_model(root, module_name="Cross")
    manifest = _jvm_manifest(suspend=True)
    jvm = project_jvm_owners(manifest.owners, feature_id=FEATURE)
    semantic = merge_semantic_apis(cpp, jvm)
    renderer = build_cross_family_renderer(
        root,
        semantic,
        manifest,
        feature_id=FEATURE,
        module_name="Cross",
    )

    generated = render_jvm_feature_jsi(
        manifest,
        jvm,
        feature_id=FEATURE,
        module_name="Cross",
        cross_family=renderer,
    )
    assert "SupernoteCoroutineBridge" in generated
    assert "supernote_module_cross_to_jvm_" in generated
    assert "supernote_module_cross_from_jvm_" in generated
    assert "operation->set_retained_state(retained_input_state)" in generated
    assert "feature closed before coroutine result conversion" in generated


def test_nullable_cross_family_suspend_result_accepts_null_before_conversion():
    class Renderer:
        @staticmethod
        def suspend_result_expression(*_args, **_kwargs):
            return "decode_nullable(result)"

    decode = _internal_suspend_decode(
        SemanticType.nullable(SemanticType.value_ref(f"{FEATURE}:type:Payload")),
        "Result<std::optional<cross::Payload>>",
        Renderer(),
    )

    assert "Kotlin coroutine result has no JNI environment" in decode
    assert "result == nullptr" not in decode
    assert "decode_nullable(result)" in decode
