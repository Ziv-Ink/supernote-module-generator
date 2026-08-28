from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from supernote_module_generator.jsi_binding_decisions import (
    JsiBindingDecisionError,
    JsiRegistrationKind,
    async_helpers_required,
    binding_mode,
    registration_kind,
)


def test_binding_mode_is_immutable_and_preserves_canonical_feature_identity():
    standalone = binding_mode(None)
    feature = binding_mode("supernote:feature:0123456789abcdef")

    assert standalone.feature_id is None
    assert standalone.feature_suffix == ""
    assert standalone.feature_scoped is False
    assert feature.feature_id == "supernote:feature:0123456789abcdef"
    assert feature.feature_suffix == "0123456789abcdef"
    assert feature.feature_scoped is True
    with pytest.raises(FrozenInstanceError):
        feature.feature_suffix = "fedcba9876543210"


@pytest.mark.parametrize(
    "feature_id",
    [
        "",
        "supernote:feature:0123456789abcde",
        "supernote:feature:0123456789abcdef0",
        "supernote:feature:0123456789ABCDEf",
        "supernote:feature:0123456789abcdeg",
        "supernote:other:0123456789abcdef",
    ],
)
def test_binding_mode_rejects_every_noncanonical_feature_identity(feature_id: str):
    with pytest.raises(
        JsiBindingDecisionError,
        match=f"invalid V4 feature identity {feature_id!r}",
    ):
        binding_mode(feature_id)


def test_registration_kind_preserves_sync_and_feature_scoped_async_policy():
    standalone = binding_mode(None)
    feature = binding_mode("supernote:feature:0123456789abcdef")

    assert registration_kind(async_export=False, mode=standalone) is JsiRegistrationKind.SYNC
    assert registration_kind(async_export=False, mode=feature) is JsiRegistrationKind.SYNC
    assert registration_kind(async_export=True, mode=feature) is JsiRegistrationKind.ASYNC
    with pytest.raises(
        JsiBindingDecisionError,
        match="V4 async bindings require plugin-level feature lowering",
    ):
        registration_kind(async_export=True, mode=standalone)


@pytest.mark.parametrize(
    ("has_async_export", "has_async_method", "extra_uses_async"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, True),
    ],
)
def test_async_helper_admission_requires_feature_scope_and_one_async_route(
    has_async_export: bool,
    has_async_method: bool,
    extra_uses_async: bool,
):
    standalone = binding_mode(None)
    feature = binding_mode("supernote:feature:0123456789abcdef")
    expected = has_async_export or has_async_method or extra_uses_async

    assert not async_helpers_required(
        standalone,
        has_async_export=has_async_export,
        has_async_method=has_async_method,
        extra_uses_async=extra_uses_async,
    )
    assert async_helpers_required(
        feature,
        has_async_export=has_async_export,
        has_async_method=has_async_method,
        extra_uses_async=extra_uses_async,
    ) is expected
