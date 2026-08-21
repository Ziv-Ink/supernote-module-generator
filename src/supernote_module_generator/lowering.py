"""Typed implementation routes from semantic bindings to source declarations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from .conversion import BindingConversionPlan
from .semantic import BindingKind, ExecutionMode, SemanticBinding
from .semantic_types import SemanticTypeKind
from .source_models import (
    CppFunctionSource,
    CppMethodSource,
    JvmDeclarationSource,
)


class LoweringError(ValueError):
    """Raised when a route cannot implement the selected semantic binding."""


class RouteKind(str, Enum):
    DIRECT_CPP_FUNCTION = "direct_cpp_function"
    CPP_OBJECT_METHOD = "cpp_object_method"
    CPP_SERVICE_METHOD = "cpp_service_method"
    JVM_FUNCTION = "jvm_function"
    JVM_SERVICE_METHOD = "jvm_service_method"
    JVM_OBJECT_METHOD = "jvm_object_method"


class SchedulingKind(str, Enum):
    INLINE = "inline"
    SHARED_WORKER = "shared_worker"
    KOTLIN_SUSPEND = "kotlin_suspend"


@dataclass(frozen=True)
class CppFunctionRoute:
    cpp_name: str


@dataclass(frozen=True)
class CppMethodRoute:
    cpp_owner: str
    cpp_method: str


@dataclass(frozen=True)
class JvmMethodRoute:
    owner_class: str
    method_name: str
    descriptor: str
    adapter_identity: str
    is_suspend: bool = False


RouteData = Union[CppFunctionRoute, CppMethodRoute, JvmMethodRoute]


@dataclass(frozen=True)
class LoweringPlan:
    binding_id: str
    source_declaration_id: str
    route: RouteKind
    scheduling: SchedulingKind
    data: RouteData
    conversion: Optional[BindingConversionPlan] = None

    def __post_init__(self) -> None:
        if not self.binding_id or not self.source_declaration_id:
            raise LoweringError("lowering identities cannot be empty")
        if not isinstance(self.route, RouteKind):
            raise LoweringError(f"unknown lowering route {self.route!r}")
        if not isinstance(self.scheduling, SchedulingKind):
            raise LoweringError(f"unknown scheduling kind {self.scheduling!r}")
        expected = {
            RouteKind.DIRECT_CPP_FUNCTION: CppFunctionRoute,
            RouteKind.CPP_OBJECT_METHOD: CppMethodRoute,
            RouteKind.CPP_SERVICE_METHOD: CppMethodRoute,
            RouteKind.JVM_FUNCTION: JvmMethodRoute,
            RouteKind.JVM_SERVICE_METHOD: JvmMethodRoute,
            RouteKind.JVM_OBJECT_METHOD: JvmMethodRoute,
        }[self.route]
        if not isinstance(self.data, expected):
            raise LoweringError(
                f"route {self.route.value!r} requires {expected.__name__} data"
            )
        if (
            self.scheduling is SchedulingKind.KOTLIN_SUSPEND
            and not isinstance(self.data, JvmMethodRoute)
        ):
            raise LoweringError("Kotlin suspend scheduling requires a JVM route")
        if (
            isinstance(self.data, JvmMethodRoute)
            and self.scheduling is SchedulingKind.KOTLIN_SUSPEND
            and not self.data.is_suspend
        ):
            raise LoweringError(
                "Kotlin suspend scheduling requires a suspending implementation"
            )
        if (
            isinstance(self.data, JvmMethodRoute)
            and self.data.is_suspend
            and self.scheduling is not SchedulingKind.KOTLIN_SUSPEND
        ):
            raise LoweringError(
                "a suspending Kotlin implementation requires Kotlin suspend scheduling"
            )

    def validate_binding(self, binding: SemanticBinding) -> None:
        if binding.binding_id != self.binding_id:
            raise LoweringError("lowering plan references a different semantic binding")
        if binding.source.declaration_id != self.source_declaration_id:
            raise LoweringError("lowering plan references a different source declaration")
        if binding.execution is ExecutionMode.SYNC:
            if self.scheduling is not SchedulingKind.INLINE:
                raise LoweringError("a synchronous binding requires inline scheduling")
        elif self.scheduling is SchedulingKind.INLINE:
            raise LoweringError("an asynchronous binding cannot use inline scheduling")

        expected_kind = {
            RouteKind.DIRECT_CPP_FUNCTION: BindingKind.FUNCTION,
            RouteKind.JVM_FUNCTION: BindingKind.FUNCTION,
            RouteKind.CPP_OBJECT_METHOD: BindingKind.OBJECT_METHOD,
            RouteKind.JVM_OBJECT_METHOD: BindingKind.OBJECT_METHOD,
            RouteKind.CPP_SERVICE_METHOD: BindingKind.SERVICE_METHOD,
            RouteKind.JVM_SERVICE_METHOD: BindingKind.SERVICE_METHOD,
        }[self.route]
        if binding.kind is not expected_kind:
            raise LoweringError(
                f"route {self.route.value!r} cannot implement binding kind "
                f"{binding.kind.value!r}"
            )
        if self.conversion is None:
            semantic_types = [item.type for item in binding.parameters]
            semantic_types.append(binding.result)
            if any(
                item.kind not in {SemanticTypeKind.VOID, SemanticTypeKind.SCALAR}
                for item in semantic_types
            ):
                raise LoweringError(
                    "recursive V3 routes require the shared binding conversion plan"
                )
        else:
            try:
                self.conversion.validate_binding(binding)
            except ValueError as exc:
                raise LoweringError(str(exc)) from exc

    def validate_source(
        self,
        source: Union[CppFunctionSource, CppMethodSource, JvmDeclarationSource],
    ) -> None:
        if source.provenance.declaration_id != self.source_declaration_id:
            raise LoweringError("lowering plan references a different source declaration")

        if self.route is RouteKind.DIRECT_CPP_FUNCTION:
            if not isinstance(source, CppFunctionSource) or not isinstance(
                self.data, CppFunctionRoute
            ):
                raise LoweringError("direct C++ function route requires C++ function source")
            if self.data.cpp_name != source.cpp_name:
                raise LoweringError("C++ function route name does not match its source")
            return

        if self.route in {
            RouteKind.CPP_OBJECT_METHOD,
            RouteKind.CPP_SERVICE_METHOD,
        }:
            if not isinstance(source, CppMethodSource) or not isinstance(
                self.data, CppMethodRoute
            ):
                raise LoweringError("C++ member route requires C++ method source")
            if self.data.cpp_method != source.cpp_name:
                raise LoweringError("C++ method route name does not match its source")
            return

        if not isinstance(source, JvmDeclarationSource) or not isinstance(
            self.data, JvmMethodRoute
        ):
            raise LoweringError("JVM route requires a JVM declaration source")
        if self.data.owner_class != source.owner_class:
            raise LoweringError("JVM route owner does not match its source")
        if self.data.method_name != source.jvm_name:
            raise LoweringError("JVM route method does not match its source")
        if self.data.descriptor != source.jvm_descriptor:
            raise LoweringError("JVM route descriptor does not match its source")
        if self.data.adapter_identity != source.adapter_identity:
            raise LoweringError("JVM route adapter identity does not match its source")
        if self.data.is_suspend != source.is_suspend:
            raise LoweringError("JVM route suspend form does not match its source")
