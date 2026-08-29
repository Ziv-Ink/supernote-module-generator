from __future__ import annotations

import pytest

from supernote_module_generator.windows_authority import (
    GenerationAuthority,
    RawCloseOutcome,
    RawCloseState,
    RawGenerationReference,
    WindowsAuthorityRegistry,
)


def _authority(ancestors: tuple[int, ...] = ()) -> GenerationAuthority:
    return GenerationAuthority(
        tuple(
            RawGenerationReference(handle, GenerationAuthority(()))
            for handle in ancestors
        )
    )


def test_reused_generation_supersedes_retryable_reference() -> None:
    handle = 55
    old_generation = _authority()
    close_attempts: list[int] = []
    registry = WindowsAuthorityRegistry(
        lambda value: (
            close_attempts.append(value),
            RawCloseOutcome(RawCloseState.CLOSED),
        )[1]
    )
    registry.handles[handle] = old_generation
    token = object()
    registry.retired[token] = (
        RawGenerationReference(handle, old_generation),
    )

    current = registry.claim_handle(handle, ())

    assert registry.handles == {handle: current}
    assert current is not old_generation
    assert registry.retired == {}
    assert close_attempts == []


def test_late_direct_ambiguity_is_bound_to_old_generation() -> None:
    handle = 55
    old_generation = _authority()
    new_generation = _authority()
    close_attempts: list[int] = []
    registry: WindowsAuthorityRegistry

    def ambiguous_old_close(value: int) -> RawCloseOutcome:
        close_attempts.append(value)
        registry.handles[handle] = new_generation
        return RawCloseOutcome(
            RawCloseState.AMBIGUOUS,
            OSError("old generation close is ambiguous"),
        )

    registry = WindowsAuthorityRegistry(ambiguous_old_close)
    registry.handles[handle] = old_generation

    with pytest.raises(OSError, match="old generation close is ambiguous"):
        registry.close_raw(handle)

    assert registry.handles == {handle: new_generation}
    assert registry.ambiguous == {handle: (old_generation,)}

    def retryable_new_close(value: int) -> RawCloseOutcome:
        close_attempts.append(value)
        return RawCloseOutcome(
            RawCloseState.RETRYABLE,
            OSError("new generation remains open"),
        )

    registry._close_raw = retryable_new_close
    with pytest.raises(OSError, match="new generation remains open"):
        registry.close_raw(handle)

    assert close_attempts == [handle, handle]
    assert registry.handles == {handle: new_generation}


def test_late_retired_ambiguity_is_bound_to_old_generation() -> None:
    handle = 55
    old_generation = _authority()
    new_generation = _authority()
    reference = RawGenerationReference(handle, old_generation)
    close_attempts: list[int] = []
    registry: WindowsAuthorityRegistry

    def ambiguous_old_close(value: int) -> RawCloseOutcome:
        close_attempts.append(value)
        registry.handles[handle] = new_generation
        return RawCloseOutcome(
            RawCloseState.AMBIGUOUS,
            OSError("retired generation close is ambiguous"),
        )

    registry = WindowsAuthorityRegistry(ambiguous_old_close)
    registry.handles[handle] = old_generation
    token = object()
    registry.retired[token] = (reference,)

    with pytest.raises(OSError, match="retired generation close is ambiguous"):
        registry.reconcile()

    assert registry.handles == {handle: new_generation}
    assert registry.retired == {}
    assert registry.ambiguous_retired == {token: (reference,)}
    assert registry.ambiguous == {handle: (old_generation,)}

    def retryable_new_close(value: int) -> RawCloseOutcome:
        close_attempts.append(value)
        return RawCloseOutcome(
            RawCloseState.RETRYABLE,
            OSError("new generation remains open"),
        )

    registry._close_raw = retryable_new_close
    with pytest.raises(OSError, match="new generation remains open"):
        registry.close_raw(handle)

    assert close_attempts == [handle, handle]
    assert registry.handles == {handle: new_generation}
