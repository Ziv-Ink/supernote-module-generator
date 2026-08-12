"""Project authoritative KSP/JVM source facts into common V2 semantics."""
from __future__ import annotations

from typing import Iterable

from .semantic import (
    BindingCapabilities,
    BindingKind,
    DeclarationRole,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticConstructor,
    SemanticParameter,
    SemanticType,
)
from .source_models import (
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
)


class JvmProjectionError(ValueError):
    """A source-located JVM frontend/semantic diagnostic."""


_KOTLIN_TYPES = {
    "kotlin.Unit": SemanticType.VOID,
    "kotlin.Boolean": SemanticType.BOOL,
    "kotlin.Int": SemanticType.INT32,
    "kotlin.Long": SemanticType.INT64,
    "kotlin.Float": SemanticType.FLOAT32,
    "kotlin.Double": SemanticType.FLOAT64,
    "kotlin.String": SemanticType.STRING,
    "kotlin.ByteArray": SemanticType.BYTES,
}
_JAVA_TYPES = {
    "void": SemanticType.VOID,
    "boolean": SemanticType.BOOL,
    "int": SemanticType.INT32,
    "long": SemanticType.INT64,
    "float": SemanticType.FLOAT32,
    "double": SemanticType.FLOAT64,
    "java.lang.String": SemanticType.STRING,
    "byte[]": SemanticType.BYTES,
}


def canonical_jvm_type(
    spelling: str,
    *,
    language: JvmLanguage,
    nullable: bool,
    result: bool,
    source: JvmDeclarationSource | JvmConstructorSource,
) -> SemanticType:
    if nullable:
        raise _error(source, "nullable marked JVM values are deferred")
    table = _KOTLIN_TYPES if language is JvmLanguage.KOTLIN else _JAVA_TYPES
    semantic = table.get(spelling)
    if semantic is None:
        accepted = ", ".join(table)
        raise _error(
            source,
            f"unsupported marked {language.value} type {spelling!r}; "
            f"use one canonical V2 type ({accepted})",
        )
    if semantic is SemanticType.VOID and not result:
        raise _error(source, "void/Unit is valid only as a marked result")
    return semantic


def project_jvm_owners(owners: Iterable[JvmOwnerSource]) -> SemanticApi:
    functions: list[SemanticBinding] = []
    classes: list[SemanticClass] = []
    for owner in owners:
        if owner.provenance.language != owner.language.value:
            raise _error(owner, "JVM owner provenance language does not match")
        if owner.intent.role is DeclarationRole.ORDINARY:
            _validate_ordinary_owner_route(owner)
            functions.extend(_project_function(owner, item) for item in owner.declarations)
            continue
        if owner.visibility != "public":
            raise _error(owner, "a marked JVM class must be public")
        if owner.form is not JvmOwnerForm.CLASS:
            raise _error(
                owner,
                "a marked JVM class must be a normal class; top-level, object, "
                "and static forms are construction-free function owners",
            )
        class_kind = (
            SemanticClassKind.JS_OBJECT
            if owner.intent.role is DeclarationRole.EXPORTED
            else SemanticClassKind.INTERNAL_SERVICE
        )
        constructor, parameters = _select_class_constructor(owner, class_kind)
        class_id = f"supernote:class:{owner.provenance.declaration_id}"
        methods = tuple(
            _project_method(owner, item, class_id, class_kind)
            for item in owner.declarations
        )
        classes.append(
            SemanticClass(
                class_id=class_id,
                kind=class_kind,
                name=owner.source_name,
                capabilities=BindingCapabilities.for_role(owner.intent.role),
                source=owner.provenance,
                constructor=SemanticConstructor(constructor.provenance, parameters),
                methods=methods,
            )
        )
    return SemanticApi(tuple(functions), tuple(classes))


def _parameters(
    source: JvmDeclarationSource | JvmConstructorSource,
    language: JvmLanguage,
    parameters: tuple[JvmParameterSource, ...],
) -> tuple[SemanticParameter, ...]:
    result = []
    for item in parameters:
        if item.injected is not None:
            if item.nullable:
                raise _error(source, "runtime-injected JVM dependencies cannot be nullable")
            continue
        result.append(
            SemanticParameter(
                item.name,
                canonical_jvm_type(
                    item.jvm_type,
                    language=language,
                    nullable=item.nullable,
                    result=False,
                    source=source,
                ),
            )
        )
    return tuple(result)


def _project_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
) -> SemanticBinding:
    if source.intent.role is DeclarationRole.ORDINARY:
        raise _error(source, "ordinary JVM declarations do not become bindings")
    if source.visibility != "public":
        raise _error(source, "a marked JVM declaration must be public")
    return SemanticBinding(
        binding_id=f"supernote:binding:{source.provenance.declaration_id}",
        kind=BindingKind.FUNCTION,
        name=source.jvm_name,
        capabilities=BindingCapabilities.for_role(source.intent.role),
        execution=source.intent.execution,
        parameters=_parameters(source, owner.language, source.parameters),
        result=canonical_jvm_type(
            source.result_jvm_type,
            language=owner.language,
            nullable=source.result_nullable,
            result=True,
            source=source,
        ),
        source=source.provenance,
    )


def _project_method(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    class_id: str,
    class_kind: SemanticClassKind,
) -> SemanticBinding:
    if source.intent.role is DeclarationRole.ORDINARY:
        raise _error(source, "ordinary JVM members do not become bindings")
    if source.visibility != "public":
        raise _error(source, "a marked JVM member must be public")
    if source.is_static:
        raise _error(source, "static methods are not object/service members")
    if (
        class_kind is SemanticClassKind.INTERNAL_SERVICE
        and source.intent.role is not DeclarationRole.INTERNAL
    ):
        raise _error(
            source,
            "a SupernoteInternal JVM class may contain only "
            "SupernoteInternal generated methods",
        )
    kind = (
        BindingKind.OBJECT_METHOD
        if class_kind is SemanticClassKind.JS_OBJECT
        else BindingKind.SERVICE_METHOD
    )
    return SemanticBinding(
        binding_id=f"supernote:binding:{source.provenance.declaration_id}",
        kind=kind,
        name=source.jvm_name,
        capabilities=BindingCapabilities.for_role(source.intent.role),
        execution=source.intent.execution,
        parameters=_parameters(source, owner.language, source.parameters),
        result=canonical_jvm_type(
            source.result_jvm_type,
            language=owner.language,
            nullable=source.result_nullable,
            result=True,
            source=source,
        ),
        source=source.provenance,
        owner_id=class_id,
        owner_name=owner.source_name,
    )


def _validate_ordinary_owner_route(owner: JvmOwnerSource) -> None:
    if owner.visibility != "public":
        raise _error(owner, "a JVM implementation owner must be public")
    for declaration in owner.declarations:
        if declaration.intent.target.value != "function":
            raise _error(
                declaration,
                "a declaration in an unmarked JVM owner is a feature function, "
                "not an object member",
            )
        requires_static = owner.form in {
            JvmOwnerForm.KOTLIN_TOP_LEVEL,
            JvmOwnerForm.JAVA_STATIC,
        }
        if declaration.is_static != requires_static:
            raise _error(
                declaration,
                f"owner form {owner.form.value!r} has inconsistent static method facts",
            )
    if owner.form is not JvmOwnerForm.CLASS:
        return
    eligible = [item for item in owner.constructors if _owner_constructor(item)]
    if len(eligible) != 1:
        raise _error(
            owner,
            "a JVM implementation owner requires exactly one eligible public "
            "Api(), Api(Context), or Api(ReactApplicationContext) constructor",
        )
    if any(item.selected for item in owner.constructors):
        raise _error(
            owner,
            "SupernoteConstructor selects a JavaScript object constructor, not "
            "a feature implementation owner",
        )


def _owner_constructor(source: JvmConstructorSource) -> bool:
    return (
        source.visibility == "public"
        and len(source.parameters) <= 1
        and all(parameter.injected is not None for parameter in source.parameters)
    )


def _select_class_constructor(
    owner: JvmOwnerSource,
    kind: SemanticClassKind,
) -> tuple[JvmConstructorSource, tuple[SemanticParameter, ...]]:
    eligible: list[tuple[JvmConstructorSource, tuple[SemanticParameter, ...]]] = []
    for constructor in owner.constructors:
        try:
            parameters = _parameters(constructor, owner.language, constructor.parameters)
        except JvmProjectionError:
            if constructor.selected:
                raise
            continue
        if constructor.visibility == "public":
            eligible.append((constructor, parameters))
        elif constructor.selected:
            raise _error(constructor, "SupernoteConstructor must select a public constructor")
    if kind is SemanticClassKind.INTERNAL_SERVICE:
        eligible = [item for item in eligible if _owner_constructor(item[0])]
        if any(item.selected for item in owner.constructors):
            raise _error(
                owner,
                "SupernoteConstructor does not apply to a SupernoteInternal service",
            )
        if len(eligible) != 1:
            raise _error(
                owner,
                "a JVM internal service requires exactly one eligible public "
                "Api(), Api(Context), or Api(ReactApplicationContext) constructor",
            )
        return eligible[0]
    if not eligible:
        raise _error(owner, "a SupernoteExport JVM class has no eligible constructor")
    selected = [item for item in eligible if item[0].selected]
    if len(eligible) == 1:
        return selected[0] if selected else eligible[0]
    if len(selected) != 1:
        raise _error(
            owner,
            "multiple eligible JVM object constructors require exactly one "
            "SupernoteConstructor selection",
        )
    return selected[0]


def _error(source: object, message: str) -> JvmProjectionError:
    provenance = getattr(source, "provenance")
    return JvmProjectionError(
        f"{provenance.path}:{provenance.line}:{provenance.column}: {message}"
    )


def jvm_type_table(language: JvmLanguage) -> dict[str, SemanticType]:
    return dict(_KOTLIN_TYPES if language is JvmLanguage.KOTLIN else _JAVA_TYPES)
