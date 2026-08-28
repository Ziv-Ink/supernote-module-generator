"""Pure feature-scope and registration decisions for generated JSI bindings."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class JsiBindingDecisionError(ValueError):
    """A binding-mode decision failure awaiting generator error adaptation."""


class JsiRegistrationKind(str, Enum):
    """Which generated callable renderer owns one export."""

    SYNC = "sync"
    ASYNC = "async"


@dataclass(frozen=True)
class JsiBindingMode:
    """Immutable standalone or feature-scoped JSI generation mode."""

    feature_id: str | None
    feature_suffix: str

    @property
    def feature_scoped(self) -> bool:
        return self.feature_id is not None


def binding_mode(feature_id: str | None) -> JsiBindingMode:
    """Validate one optional V4 feature identity and derive its namespace suffix."""

    if feature_id is None:
        return JsiBindingMode(None, "")
    if not re.fullmatch(r"supernote:feature:[0-9a-f]{16}", feature_id):
        raise JsiBindingDecisionError(f"invalid V4 feature identity {feature_id!r}")
    return JsiBindingMode(
        feature_id,
        feature_id.removeprefix("supernote:feature:"),
    )


def registration_kind(
    *,
    async_export: bool,
    mode: JsiBindingMode,
) -> JsiRegistrationKind:
    """Route an export to its sync or feature-scoped async renderer."""

    if not async_export:
        return JsiRegistrationKind.SYNC
    if not mode.feature_scoped:
        raise JsiBindingDecisionError(
            "V4 async bindings require plugin-level feature lowering"
        )
    return JsiRegistrationKind.ASYNC


def async_helpers_required(
    mode: JsiBindingMode,
    *,
    has_async_export: bool,
    has_async_method: bool,
    extra_uses_async: bool,
) -> bool:
    """Return whether the feature unit needs generated promise helpers."""

    return mode.feature_scoped and (
        has_async_export or has_async_method or extra_uses_async
    )
