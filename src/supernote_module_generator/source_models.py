"""Language-specific declaration facts retained by V2 frontends."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from .semantic import DeclarationRole, ExecutionMode, SourceProvenance


class SourceModelError(ValueError):
    """Raised when source intent or a language source record is inconsistent."""


class SupernoteMarker(str, Enum):
    EXPORT = "SupernotePluginExport"
    INTERNAL = "SupernotePluginInternal"
    ASYNC = "SupernotePluginAsync"
    CONSTRUCTOR = "SupernoteConstructor"


class DeclarationTarget(str, Enum):
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
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

        if self.target is DeclarationTarget.CONSTRUCTOR:
            invalid = marker_set - {SupernoteMarker.CONSTRUCTOR}
            if invalid:
                raise SourceModelError(
                    "constructors accept only SupernoteConstructor in initial V2"
                )
        else:
            if SupernoteMarker.CONSTRUCTOR in marker_set:
                raise SourceModelError(
                    "SupernoteConstructor is valid only on a constructor"
                )
            if (
                SupernoteMarker.ASYNC in marker_set
                and SupernoteMarker.EXPORT not in marker_set
                and SupernoteMarker.INTERNAL not in marker_set
            ):
                raise SourceModelError(
                    "SupernotePluginAsync requires SupernotePluginExport or SupernotePluginInternal"
                )
            if self.target is DeclarationTarget.CLASS and SupernoteMarker.ASYNC in marker_set:
                raise SourceModelError("SupernotePluginAsync cannot mark a class")

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

    def __post_init__(self) -> None:
        if self.intent.target is not DeclarationTarget.CLASS:
            raise SourceModelError("a C++ class requires class source intent")
        if self.declaration_kind not in {"class", "struct"}:
            raise SourceModelError("a C++ class source kind must be class or struct")


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
