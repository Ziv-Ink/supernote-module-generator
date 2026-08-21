"""Language-specific declaration facts retained by V3 frontends."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from .semantic import DeclarationRole, ExecutionMode, SourceProvenance


class SourceModelError(ValueError):
    """Raised when source intent or a language source record is inconsistent."""


class SupernoteMarker(str, Enum):
    OBJECT = "SupernotePluginObject"
    VALUE = "SupernotePluginValue"
    EXPORT = "SupernotePluginExport"
    INTERNAL = "SupernotePluginInternal"
    ASYNC = "SupernotePluginAsync"
    CONSTRUCTOR = "SupernoteConstructor"


class DeclarationTarget(str, Enum):
    FUNCTION = "function"
    CLASS = "class"
    ENUM = "enum"
    METHOD = "method"
    FIELD = "field"
    CONSTRUCTOR = "constructor"


@dataclass(frozen=True)
class MarkerOccurrence:
    """One source-located marker use retained for precise diagnostics."""

    marker: SupernoteMarker
    line: int
    column: int = 1

    def __post_init__(self) -> None:
        if self.line < 1 or self.column < 1:
            raise SourceModelError("marker line and column must be positive")


@dataclass(frozen=True)
class SourceIntent:
    target: DeclarationTarget
    occurrences: Tuple[MarkerOccurrence, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        markers = self.markers
        if len(set(markers)) != len(markers):
            duplicate = next(marker for marker in markers if markers.count(marker) > 1)
            raise SourceModelError(f"duplicate {duplicate.value} marker")
        marker_set = self.marker_set
        if SupernoteMarker.EXPORT in marker_set and SupernoteMarker.INTERNAL in marker_set:
            raise SourceModelError(
                "SupernotePluginExport and SupernotePluginInternal cannot mark one declaration"
            )

        type_markers = {SupernoteMarker.OBJECT, SupernoteMarker.VALUE}
        reachability = {SupernoteMarker.EXPORT, SupernoteMarker.INTERNAL}
        if self.target is DeclarationTarget.CONSTRUCTOR:
            invalid = marker_set - {SupernoteMarker.CONSTRUCTOR}
            if invalid:
                raise SourceModelError(
                    "constructors accept only SupernoteConstructor in initial V3"
                )
        elif self.target is DeclarationTarget.CLASS:
            if marker_set and marker_set not in (
                {SupernoteMarker.OBJECT},
                {SupernoteMarker.VALUE},
            ):
                raise SourceModelError(
                    "classes require exactly one of SupernotePluginObject or "
                    "SupernotePluginValue; reachability markers belong on members"
                )
        elif self.target is DeclarationTarget.ENUM:
            if marker_set != {SupernoteMarker.VALUE}:
                raise SourceModelError(
                    "a generated string enum requires exactly SupernotePluginValue"
                )
        elif self.target is DeclarationTarget.FIELD:
            if marker_set and marker_set != {SupernoteMarker.EXPORT}:
                raise SourceModelError(
                    "generated fields accept only SupernotePluginExport"
                )
        else:
            if SupernoteMarker.CONSTRUCTOR in marker_set:
                raise SourceModelError(
                    "SupernoteConstructor is valid only on a constructor"
                )
            if marker_set & type_markers:
                raise SourceModelError(
                    "SupernotePluginObject and SupernotePluginValue are valid only "
                    "on type declarations"
                )
            if (
                SupernoteMarker.ASYNC in marker_set
                and SupernoteMarker.EXPORT not in marker_set
                and SupernoteMarker.INTERNAL not in marker_set
            ):
                raise SourceModelError(
                    "SupernotePluginAsync requires SupernotePluginExport or SupernotePluginInternal"
                )
            if marker_set and not marker_set & reachability:
                raise SourceModelError(
                    "generated functions and methods require "
                    "SupernotePluginExport or SupernotePluginInternal"
                )

    @classmethod
    def from_markers(
        cls,
        target: DeclarationTarget,
        markers: Tuple[SupernoteMarker, ...],
        *,
        first_line: int = 1,
    ) -> "SourceIntent":
        return cls(
            target,
            tuple(
                MarkerOccurrence(marker, first_line + index)
                for index, marker in enumerate(markers)
            ),
        )

    @property
    def markers(self) -> Tuple[SupernoteMarker, ...]:
        return tuple(occurrence.marker for occurrence in self.occurrences)

    @property
    def marker_set(self) -> FrozenSet[SupernoteMarker]:
        return frozenset(self.markers)

    @property
    def role(self) -> DeclarationRole:
        if SupernoteMarker.EXPORT in self.marker_set:
            return DeclarationRole.EXPORTED
        if SupernoteMarker.INTERNAL in self.marker_set:
            return DeclarationRole.INTERNAL
        return DeclarationRole.ORDINARY

    @property
    def execution(self) -> ExecutionMode:
        return (
            ExecutionMode.ASYNC
            if SupernoteMarker.ASYNC in self.marker_set
            else ExecutionMode.SYNC
        )

    @property
    def selects_constructor(self) -> bool:
        return SupernoteMarker.CONSTRUCTOR in self.marker_set

    @property
    def declares_object(self) -> bool:
        return SupernoteMarker.OBJECT in self.marker_set

    @property
    def declares_value(self) -> bool:
        return SupernoteMarker.VALUE in self.marker_set


@dataclass(frozen=True)
class CppParameterSource:
    type_spelling: str
    name: str


@dataclass(frozen=True)
class CppFunctionSource:
    provenance: SourceProvenance
    cpp_name: str
    return_type_spelling: str
    parameters: Tuple[CppParameterSource, ...]
    intent: SourceIntent
    noexcept: bool = False
    definition_offset: int = -1
    namespace: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.FUNCTION:
            raise SourceModelError("a C++ function requires function source intent")


@dataclass(frozen=True)
class CppConstructorSource:
    provenance: SourceProvenance
    parameters: Tuple[CppParameterSource, ...]
    access: str
    intent: SourceIntent
    deleted: bool = False
    explicit: bool = False
    noexcept: bool = False
    implicit: bool = False

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.CONSTRUCTOR:
            raise SourceModelError("a C++ constructor requires constructor intent")

    @property
    def selected(self) -> bool:
        return self.intent.selects_constructor


@dataclass(frozen=True)
class CppMethodSource:
    provenance: SourceProvenance
    cpp_name: str
    return_type_spelling: str
    parameters: Tuple[CppParameterSource, ...]
    intent: SourceIntent
    access: str
    const: bool = False
    noexcept: bool = False
    static: bool = False

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.METHOD:
            raise SourceModelError("a C++ method requires method source intent")


@dataclass(frozen=True)
class CppClassSource:
    provenance: SourceProvenance
    cpp_name: str
    include: str
    intent: SourceIntent
    constructors: Tuple[CppConstructorSource, ...]
    methods: Tuple[CppMethodSource, ...]
    declaration_kind: str = "class"
    fields: Tuple["CppFieldSource", ...] = field(default_factory=tuple)
    namespace: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.CLASS:
            raise SourceModelError("a C++ class requires class source intent")
        if self.declaration_kind not in {"class", "struct"}:
            raise SourceModelError("a C++ class source kind must be class or struct")

    @property
    def qualified_name(self) -> str:
        return "::".join((*self.namespace, self.cpp_name))


@dataclass(frozen=True)
class CppFieldSource:
    provenance: SourceProvenance
    cpp_name: str
    type_spelling: str
    intent: SourceIntent
    access: str
    mutable: bool
    static: bool = False

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.FIELD:
            raise SourceModelError("a C++ field requires field source intent")


@dataclass(frozen=True)
class CppEnumSource:
    provenance: SourceProvenance
    cpp_name: str
    include: str
    intent: SourceIntent
    constants: Tuple[str, ...]
    namespace: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.ENUM:
            raise SourceModelError("a C++ enum requires enum source intent")

    @property
    def qualified_name(self) -> str:
        return "::".join((*self.namespace, self.cpp_name))


class JvmLanguage(str, Enum):
    KOTLIN = "kotlin"
    JAVA = "java"


class JvmOwnerForm(str, Enum):
    CLASS = "class"
    KOTLIN_OBJECT = "kotlin_object"
    KOTLIN_TOP_LEVEL = "kotlin_top_level"
    JAVA_STATIC = "java_static"


class JvmInjectedDependency(str, Enum):
    CONTEXT = "android.content.Context"
    REACT_APPLICATION_CONTEXT = "com.facebook.react.bridge.ReactApplicationContext"


@dataclass(frozen=True)
class JvmParameterSource:
    jvm_type: str
    name: str
    nullable: bool = False
    injected: Optional[JvmInjectedDependency] = None
    type_arguments: Tuple["JvmTypeSource", ...] = field(default_factory=tuple)

    @property
    def type_source(self) -> "JvmTypeSource":
        return JvmTypeSource(self.jvm_type, self.nullable, self.type_arguments)


@dataclass(frozen=True)
class JvmTypeSource:
    jvm_type: str
    nullable: bool = False
    arguments: Tuple["JvmTypeSource", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.jvm_type:
            raise SourceModelError("a JVM type spelling cannot be empty")


@dataclass(frozen=True)
class JvmConstructorSource:
    provenance: SourceProvenance
    jvm_descriptor: str
    parameters: Tuple[JvmParameterSource, ...]
    visibility: str
    intent: SourceIntent
    adapter_identity: str

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.CONSTRUCTOR:
            raise SourceModelError("a JVM constructor requires constructor intent")
        if not self.adapter_identity:
            raise SourceModelError("a JVM constructor adapter identity cannot be empty")

    @property
    def selected(self) -> bool:
        return self.intent.selects_constructor


@dataclass(frozen=True)
class JvmDeclarationSource:
    provenance: SourceProvenance
    owner_declaration_id: str
    owner_class: str
    jvm_name: str
    jvm_descriptor: str
    parameters: Tuple[JvmParameterSource, ...]
    result_jvm_type: str
    result_nullable: bool
    intent: SourceIntent
    visibility: str
    adapter_identity: str
    language: JvmLanguage
    is_suspend: bool = False
    is_static: bool = False
    result_type_arguments: Tuple[JvmTypeSource, ...] = field(default_factory=tuple)

    @property
    def result_type_source(self) -> JvmTypeSource:
        return JvmTypeSource(
            self.result_jvm_type,
            self.result_nullable,
            self.result_type_arguments,
        )

    def __post_init__(self) -> None:
        if self.intent.target not in {
            DeclarationTarget.FUNCTION,
            DeclarationTarget.METHOD,
        }:
            raise SourceModelError("a JVM declaration requires function or method intent")
        if not self.owner_declaration_id or not self.adapter_identity:
            raise SourceModelError("JVM owner and adapter identities cannot be empty")
        if self.is_suspend:
            if self.language is not JvmLanguage.KOTLIN:
                raise SourceModelError("only Kotlin declarations can be suspending")
            if self.intent.execution is not ExecutionMode.ASYNC:
                raise SourceModelError(
                    "a suspending Kotlin declaration requires SupernotePluginAsync"
                )
        for parameter in self.parameters:
            if parameter.injected is not None:
                raise SourceModelError(
                    "runtime-injected dependencies are valid only on constructors"
                )


@dataclass(frozen=True)
class JvmOwnerSource:
    provenance: SourceProvenance
    language: JvmLanguage
    owner_class: str
    source_name: str
    form: JvmOwnerForm
    intent: SourceIntent
    constructors: Tuple[JvmConstructorSource, ...]
    declarations: Tuple[JvmDeclarationSource, ...]
    visibility: str = "public"
    fields: Tuple["JvmFieldSource", ...] = field(default_factory=tuple)
    enum_constants: Tuple[str, ...] = field(default_factory=tuple)
    is_data: bool = False
    is_record: bool = False
    is_final: bool = True
    type_parameter_count: int = 0
    supertypes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.CLASS:
            raise SourceModelError("a JVM owner requires class source intent")
        if self.form in {
            JvmOwnerForm.KOTLIN_OBJECT,
            JvmOwnerForm.KOTLIN_TOP_LEVEL,
        } and self.language is not JvmLanguage.KOTLIN:
            raise SourceModelError(f"owner form {self.form.value} requires Kotlin")
        if (
            self.form is JvmOwnerForm.JAVA_STATIC
            and self.language is not JvmLanguage.JAVA
        ):
            raise SourceModelError("owner form java_static requires Java")
        if (
            self.form is not JvmOwnerForm.CLASS
            and self.constructors
        ):
            raise SourceModelError(
                f"construction-free owner form {self.form.value} cannot declare constructors"
            )
        if (
            self.intent.role is DeclarationRole.EXPORTED
            and self.form is not JvmOwnerForm.CLASS
        ):
            raise SourceModelError(
                "a JVM-backed JavaScript object requires class owner form"
            )
        for constructor in self.constructors:
            if constructor.provenance.language != self.language.value:
                raise SourceModelError(
                    "a JVM constructor language must match its containing owner"
                )
            for parameter in constructor.parameters:
                if (
                    parameter.injected is not None
                    and parameter.jvm_type != parameter.injected.value
                ):
                    raise SourceModelError(
                        "an injected constructor parameter type must match its dependency"
                    )
        for declaration in self.declarations:
            if declaration.owner_declaration_id != self.provenance.declaration_id:
                raise SourceModelError(
                    f"JVM declaration {declaration.provenance.declaration_id!r} "
                    "does not reference its containing owner"
                )
            if declaration.owner_class != self.owner_class:
                raise SourceModelError(
                    f"JVM declaration {declaration.provenance.declaration_id!r} "
                    "uses a different owner class"
                )
            if declaration.language is not self.language:
                raise SourceModelError(
                    "a JVM declaration language must match its containing owner"
                )
        for source_field in self.fields:
            if source_field.owner_declaration_id != self.provenance.declaration_id:
                raise SourceModelError(
                    f"JVM field {source_field.name!r} does not reference its owner"
                )
            if source_field.provenance.language != self.language.value:
                raise SourceModelError("a JVM field language must match its owner")
        if self.type_parameter_count < 0:
            raise SourceModelError("JVM type parameter count cannot be negative")


@dataclass(frozen=True)
class JvmFieldSource:
    provenance: SourceProvenance
    owner_declaration_id: str
    name: str
    type: JvmTypeSource
    intent: SourceIntent
    visibility: str
    mutable: bool
    is_static: bool = False
    accessor_identity: str = ""

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.FIELD:
            raise SourceModelError("a JVM field requires field source intent")
        if not self.owner_declaration_id:
            raise SourceModelError("a JVM field owner identity cannot be empty")
