from __future__ import annotations

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
    SemanticParameter,
    SemanticType,
    SourceProvenance,
)
from supernote_module_generator.typescript_codegen import render_typescript


def source(identity: str):
    return SourceProvenance(identity, "cpp", "feature.cpp", 1)


def binding(
    identity: str,
    name: str,
    role: DeclarationRole,
    execution: ExecutionMode,
    result: SemanticType,
    *,
    kind: BindingKind = BindingKind.FUNCTION,
    owner_id: str | None = None,
    owner_name: str | None = None,
):
    return SemanticBinding(
        identity,
        kind,
        name,
        BindingCapabilities.for_role(role),
        execution,
        (SemanticParameter("value", SemanticType.INT64),),
        result,
        source("source:" + identity),
        owner_id,
        owner_name,
    )


def test_typescript_uses_only_public_common_semantics_and_exact_value_mappings():
    public = binding(
        "load", "load", DeclarationRole.EXPORTED, ExecutionMode.ASYNC, SemanticType.BYTES
    )
    internal = binding(
        "hidden", "hidden", DeclarationRole.INTERNAL, ExecutionMode.SYNC, SemanticType.VOID
    )
    text = render_typescript("Document", SemanticApi((public, internal)))

    assert "load(value: bigint): Promise<Uint8Array>;" in text
    assert "hidden" not in text
    assert "export class SupernoteError extends Error" in text
    assert 'readonly code: SupernoteErrorCode;' in text


def test_typescript_generates_public_object_factory_and_explicit_members_only():
    class_id = "class:Document"
    method = binding(
        "pageCount",
        "pageCount",
        DeclarationRole.EXPORTED,
        ExecutionMode.SYNC,
        SemanticType.INT32,
        kind=BindingKind.OBJECT_METHOD,
        owner_id=class_id,
        owner_name="Document",
    )
    hidden = binding(
        "rebuild",
        "rebuild",
        DeclarationRole.INTERNAL,
        ExecutionMode.ASYNC,
        SemanticType.VOID,
        kind=BindingKind.OBJECT_METHOD,
        owner_id=class_id,
        owner_name="Document",
    )
    item = SemanticClass(
        class_id,
        SemanticClassKind.JS_OBJECT,
        "Document",
        BindingCapabilities.for_role(DeclarationRole.EXPORTED),
        source("class-source"),
        SemanticConstructor(
            source("constructor-source"),
            (SemanticParameter("path", SemanticType.STRING),),
        ),
        (method, hidden),
    )
    text = render_typescript("Feature", SemanticApi(classes=(item,)))

    assert "export interface Document {" in text
    assert "pageCount(value: bigint): number;" in text
    assert "rebuild" not in text
    assert "create(path: string): Document;" in text
    assert "Document: DocumentFactory;" in text
