"""Project authoritative KSP/JVM source facts into common V4 semantics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .semantic import (
    BindingCapabilities,
    BindingKind,
    BackendFamily,
    DeclarationRole,
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticClass,
    SemanticClassKind,
    SemanticConstructor,
    SemanticEnumDeclaration,
    SemanticField,
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticProjection,
    SemanticType,
    SemanticValueDeclaration,
    semantic_type_id,
)
from .source_models import (
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmLanguage,
    JvmFieldSource,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    JvmTypeSource,
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
_JAVA_BOXED_TYPES = {
    "java.lang.Boolean": SemanticType.BOOL,
    "java.lang.Integer": SemanticType.INT32,
    "java.lang.Long": SemanticType.INT64,
    "java.lang.Float": SemanticType.FLOAT32,
    "java.lang.Double": SemanticType.FLOAT64,
}


@dataclass(frozen=True)
class _JvmNamedType:
    kind: str
    type_id: str
    public_name: str


class _JvmTypeRegistry:
    def __init__(self, feature_id: str, owners: tuple[JvmOwnerSource, ...]) -> None:
        self.feature_id = feature_id
        self.values: dict[str, _JvmNamedType] = {}
        for owner in owners:
            if not (owner.intent.declares_object or owner.intent.declares_value):
                continue
            kind = (
                "enum"
                if owner.enum_constants
                else "object" if owner.intent.declares_object else "value"
            )
            self.values[owner.owner_class] = _JvmNamedType(
                kind,
                semantic_type_id(feature_id, owner.source_name),
                owner.source_name,
            )

    def resolve(self, name: str) -> Optional[_JvmNamedType]:
        return self.values.get(name)


def canonical_jvm_type(
    spelling: str,
    *,
    language: JvmLanguage,
    nullable: bool,
    result: bool,
    source: JvmDeclarationSource | JvmConstructorSource,
    arguments: tuple[JvmTypeSource, ...] = (),
    registry: Optional[_JvmTypeRegistry] = None,
    generic_argument: bool = False,
) -> SemanticType:
    if nullable and spelling in {"kotlin.Unit", "void"}:
        raise _error(source, "void/Unit cannot be nullable")
    if spelling in {"kotlin.collections.List", "java.util.List"}:
        if len(arguments) != 1:
            raise _error(source, "List requires exactly one invariant type argument")
        element = canonical_jvm_type(
            arguments[0].jvm_type,
            language=language,
            nullable=arguments[0].nullable,
            result=False,
            source=source,
            arguments=arguments[0].arguments,
            registry=registry,
            generic_argument=True,
        )
        semantic = SemanticType.array(element)
        return SemanticType.nullable(semantic) if nullable else semantic
    table = _KOTLIN_TYPES if language is JvmLanguage.KOTLIN else _JAVA_TYPES
    semantic = table.get(spelling)
    if language is JvmLanguage.JAVA:
        boxed = _JAVA_BOXED_TYPES.get(spelling)
        if generic_argument or nullable:
            if boxed is not None:
                semantic = boxed
            elif spelling in {"boolean", "int", "long", "float", "double"}:
                raise _error(
                    source,
                    f"Java {spelling} must use its boxed reference spelling in "
                    "nullable or generic positions",
                )
        elif boxed is not None:
            raise _error(
                source,
                f"Java direct non-null scalar {spelling!r} must use its primitive spelling",
            )
    if semantic is None and registry is not None:
        named = registry.resolve(spelling)
        if named is not None:
            if named.kind == "object":
                semantic = SemanticType.object_ref(named.type_id)
            elif named.kind == "value":
                semantic = SemanticType.value_ref(named.type_id)
            else:
                semantic = SemanticType.enum_ref(named.type_id)
    if semantic is None:
        accepted = ", ".join(table)
        raise _error(
            source,
            f"unsupported marked {language.value} type {spelling!r}; "
            f"use one canonical V4 type ({accepted})",
        )
    if semantic is SemanticType.VOID and not result:
        raise _error(source, "void/Unit is valid only as a marked result")
    return SemanticType.nullable(semantic) if nullable else semantic


def project_jvm_owners(
    owners: Iterable[JvmOwnerSource],
    *,
    feature_id: str = "supernote:feature:legacy",
) -> SemanticApi:
    owner_sources = tuple(owners)
    registry = _JvmTypeRegistry(feature_id, owner_sources)
    functions: list[SemanticBinding] = []
    classes: list[SemanticClass] = []
    declarations = []
    for owner in owner_sources:
        if owner.provenance.language != owner.language.value:
            raise _error(owner, "JVM owner provenance language does not match")
        if owner.intent.declares_object:
            declarations.append(_project_jvm_object(owner, registry, feature_id))
            continue
        if owner.intent.declares_value:
            if owner.enum_constants:
                declarations.append(_project_jvm_enum(owner, feature_id))
            else:
                declarations.append(_project_jvm_value(owner, registry, feature_id))
            continue
        if owner.intent.role is DeclarationRole.ORDINARY:
            _validate_ordinary_owner_route(owner)
            functions.extend(
                _project_function(owner, item, registry) for item in owner.declarations
            )
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
    return SemanticApi(tuple(functions), tuple(classes), tuple(declarations))


def _parameters(
    source: JvmDeclarationSource | JvmConstructorSource,
    language: JvmLanguage,
    parameters: tuple[JvmParameterSource, ...],
    registry: Optional[_JvmTypeRegistry] = None,
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
                    arguments=item.type_arguments,
                    registry=registry,
                ),
            )
        )
    return tuple(result)


def _selected_jvm_object_constructor(
    owner: JvmOwnerSource,
    registry: _JvmTypeRegistry,
) -> Optional[SemanticConstructor]:
    selected = [item for item in owner.constructors if item.selected]
    if len(selected) > 1:
        raise _error(owner, "an object may select at most one SupernoteConstructor")
    if not selected:
        return None
    constructor = selected[0]
    if constructor.visibility != "public":
        raise _error(constructor, "SupernoteConstructor must select a public constructor")
    return SemanticConstructor(
        constructor.provenance,
        _parameters(
            constructor,
            owner.language,
            constructor.parameters,
            registry,
        ),
    )


def _jvm_field(
    owner: JvmOwnerSource,
    source: JvmFieldSource,
    owner_id: str,
    registry: _JvmTypeRegistry,
) -> SemanticField:
    if source.owner_declaration_id != owner.provenance.declaration_id:
        raise _error(source, "JVM field owner identity does not match its type")
    if source.visibility != "public":
        raise _error(source, "a generated JVM field/property must be public")
    if source.is_static:
        raise _error(source, "static generated fields are unsupported")
    if source.intent.role is not DeclarationRole.EXPORTED:
        raise _error(source, "generated fields require SupernotePluginExport")
    semantic = canonical_jvm_type(
        source.type.jvm_type,
        language=owner.language,
        nullable=source.type.nullable,
        result=False,
        source=source,
        arguments=source.type.arguments,
        registry=registry,
    )
    return SemanticField(
        f"{owner_id}:field:{source.name}",
        owner_id,
        source.name,
        semantic,
        source.provenance,
        source.mutable,
    )


def _object_method(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    owner_id: str,
    registry: _JvmTypeRegistry,
) -> SemanticBinding:
    if source.visibility != "public":
        raise _error(source, "a marked JVM object method must be public")
    return SemanticBinding(
        f"supernote:binding:{source.provenance.declaration_id}",
        BindingKind.OBJECT_METHOD,
        source.jvm_name,
        BindingCapabilities.for_role(source.intent.role),
        source.intent.execution,
        _parameters(source, owner.language, source.parameters, registry),
        canonical_jvm_type(
            source.result_jvm_type,
            language=owner.language,
            nullable=source.result_nullable,
            result=True,
            source=source,
            arguments=source.result_type_arguments,
            registry=registry,
        ),
        source.provenance,
        owner_id,
        owner.source_name,
        MemberScope.STATIC if source.is_static else MemberScope.INSTANCE,
    )


def _validate_jvm_bridge_type(owner: JvmOwnerSource) -> None:
    if owner.visibility != "public":
        raise _error(owner, "a marked JVM type must be public")
    if owner.type_parameter_count:
        raise _error(owner, "generic marked JVM types are unsupported")
    if owner.supertypes:
        raise _error(owner, "inheritance and interfaces on marked JVM types are unsupported")
    if owner.language is JvmLanguage.JAVA and not owner.is_final:
        raise _error(owner, "marked Java object/value classes must be final")


def _project_jvm_object(
    owner: JvmOwnerSource,
    registry: _JvmTypeRegistry,
    feature_id: str,
) -> SemanticObjectDeclaration:
    _validate_jvm_bridge_type(owner)
    if owner.form is not JvmOwnerForm.CLASS:
        raise _error(owner, "SupernotePluginObject requires a normal class")
    owner_id = semantic_type_id(feature_id, owner.source_name)
    return SemanticObjectDeclaration(
        feature_id,
        owner_id,
        owner.source_name,
        SemanticProjection(BackendFamily.JVM, owner.provenance),
        _selected_jvm_object_constructor(owner, registry),
        tuple(
            _object_method(owner, item, owner_id, registry)
            for item in owner.declarations
            if item.intent.role is not DeclarationRole.ORDINARY
        ),
        tuple(_jvm_field(owner, item, owner_id, registry) for item in owner.fields),
    )


def _project_jvm_value(
    owner: JvmOwnerSource,
    registry: _JvmTypeRegistry,
    feature_id: str,
) -> SemanticValueDeclaration:
    _validate_jvm_bridge_type(owner)
    if owner.language is JvmLanguage.KOTLIN and not owner.is_data:
        raise _error(owner, "SupernotePluginValue requires a Kotlin data class")
    if owner.language is JvmLanguage.JAVA and not (owner.is_record or owner.is_final):
        raise _error(owner, "Java values require a record or supported final class")
    if any(item.selected for item in owner.constructors):
        raise _error(owner, "SupernoteConstructor cannot mark a value type")
    if any(item.intent.role is not DeclarationRole.ORDINARY for item in owner.declarations):
        raise _error(owner, "value types expose fields/properties, not generated methods")
    owner_id = semantic_type_id(feature_id, owner.source_name)
    fields = tuple(
        _jvm_field(owner, item, owner_id, registry) for item in owner.fields
    )
    if owner.language is JvmLanguage.JAVA:
        if any(item.mutable for item in owner.fields):
            raise _error(owner, "Java value fields/record components must be final")
        eligible = [
            item for item in owner.constructors if item.visibility == "public"
        ]
        if len(eligible) != 1:
            raise _error(
                owner,
                "a Java value requires exactly one public constructor matching "
                "its ordered fields/record components",
            )
        parameters = _parameters(
            eligible[0], owner.language, eligible[0].parameters, registry
        )
        expected = tuple((item.name, item.type) for item in fields)
        actual = tuple((item.name, item.type) for item in parameters)
        if actual != expected:
            raise _error(
                eligible[0],
                "Java value constructor parameters must match the ordered "
                "field/component names and types exactly",
            )
    return SemanticValueDeclaration(
        feature_id,
        owner_id,
        owner.source_name,
        fields,
        (SemanticProjection(BackendFamily.JVM, owner.provenance),),
    )


def _project_jvm_enum(
    owner: JvmOwnerSource,
    feature_id: str,
) -> SemanticEnumDeclaration:
    _validate_jvm_bridge_type(owner)
    return SemanticEnumDeclaration(
        feature_id,
        semantic_type_id(feature_id, owner.source_name),
        owner.source_name,
        owner.enum_constants,
        (SemanticProjection(BackendFamily.JVM, owner.provenance),),
    )


def _project_function(
    owner: JvmOwnerSource,
    source: JvmDeclarationSource,
    registry: Optional[_JvmTypeRegistry] = None,
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
        parameters=_parameters(source, owner.language, source.parameters, registry),
        result=canonical_jvm_type(
            source.result_jvm_type,
            language=owner.language,
            nullable=source.result_nullable,
            result=True,
            source=source,
            arguments=source.result_type_arguments,
            registry=registry,
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
            "a SupernotePluginInternal JVM class may contain only "
            "SupernotePluginInternal generated methods",
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
                "SupernoteConstructor does not apply to a SupernotePluginInternal service",
            )
        if len(eligible) != 1:
            raise _error(
                owner,
                "a JVM internal service requires exactly one eligible public "
                "Api(), Api(Context), or Api(ReactApplicationContext) constructor",
            )
        return eligible[0]
    if not eligible:
        raise _error(owner, "a SupernotePluginExport JVM class has no eligible constructor")
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
