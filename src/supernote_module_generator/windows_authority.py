"""Generation-bound ownership for retained Windows handles and descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable


@dataclass(frozen=True)
class RawGenerationReference:
    handle: int
    generation: "GenerationAuthority"


@dataclass(frozen=True, eq=False)
class GenerationAuthority:
    """Unique identity and retained raw generations for one allocation."""

    ancestor_generations: tuple[RawGenerationReference, ...]

    @property
    def ancestors(self) -> tuple[int, ...]:
        return tuple(item.handle for item in self.ancestor_generations)


class RawCloseState(Enum):
    CLOSED = "closed"
    RETRYABLE = "retryable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RawCloseOutcome:
    state: RawCloseState
    error: BaseException | None = None


@dataclass(frozen=True)
class _GenerationTransition:
    authority: dict[int, GenerationAuthority]
    numeric_value: int
    current: GenerationAuthority
    obsolete: tuple[GenerationAuthority, ...]
    superseded: tuple[RawGenerationReference, ...]


RawCloser = Callable[[int], RawCloseOutcome]
DescriptorCloser = Callable[[int], object]


class WindowsAuthorityRegistry:
    """Own every numeric Windows generation across claim, close, and reuse."""

    def __init__(self, close_raw: RawCloser) -> None:
        self.handles: dict[int, GenerationAuthority] = {}
        self.descriptors: dict[int, GenerationAuthority] = {}
        self.retired: dict[object, tuple[RawGenerationReference, ...]] = {}
        self.ambiguous_retired: dict[
            object, tuple[RawGenerationReference, ...]
        ] = {}
        self.ambiguous: dict[int, tuple[GenerationAuthority, ...]] = {}
        self.pending: dict[object, _GenerationTransition] = {}
        self._close_raw = close_raw

    def capture_raw_generations(
        self,
        handles: tuple[int, ...],
    ) -> tuple[RawGenerationReference, ...]:
        references: list[RawGenerationReference] = []
        for handle in handles:
            generation = self.handles.get(handle)
            if generation is None:
                generation = GenerationAuthority(())
            references.append(RawGenerationReference(handle, generation))
        return tuple(references)

    def claim_handle(
        self,
        handle: int,
        ancestors: tuple[RawGenerationReference, ...],
    ) -> GenerationAuthority:
        return self._claim(self.handles, handle, ancestors)

    def claim_descriptor(
        self,
        descriptor: int,
        ancestors: tuple[RawGenerationReference, ...],
    ) -> GenerationAuthority:
        return self._claim(self.descriptors, descriptor, ancestors)

    def handle_generation(self, handle: int) -> GenerationAuthority | None:
        return self.handles.get(handle)

    def descriptor_generation(
        self,
        descriptor: int,
    ) -> GenerationAuthority | None:
        return self.descriptors.get(descriptor)

    def ensure_handle(
        self,
        handle: int,
        generation: GenerationAuthority,
    ) -> None:
        if handle not in self.handles:
            self.handles[handle] = generation

    def ensure_descriptor(
        self,
        descriptor: int,
        generation: GenerationAuthority,
    ) -> None:
        if descriptor not in self.descriptors:
            self.descriptors[descriptor] = generation

    def release_handle_if(
        self,
        handle: int,
        generation: GenerationAuthority | None,
    ) -> None:
        if generation is not None and self.handles.get(handle) is generation:
            del self.handles[handle]

    def close_raw(self, handle: int) -> None:
        registered = self._current_generation(self.handles, handle)
        if self._generation_is_ambiguous(handle, registered):
            raise OSError(
                "Windows handle close outcome is ambiguous; refusing to retry"
            )
        outcome = self._close_raw(handle)
        live_generation = self.handles.get(handle)
        if live_generation is not registered:
            if outcome.state is RawCloseState.AMBIGUOUS:
                self._record_ambiguous(handle, registered)
            if outcome.error is not None:
                raise outcome.error
            return
        if outcome.state is RawCloseState.AMBIGUOUS:
            self._record_ambiguous(handle, registered)
        if outcome.state is not RawCloseState.CLOSED:
            if outcome.error is not None:
                raise outcome.error
            raise OSError("Windows handle close failed without a diagnostic")
        self._retire_current(self.handles, handle, registered)
        if outcome.error is not None:
            raise outcome.error

    def close_descriptor(
        self,
        descriptor: int,
        closer: DescriptorCloser,
    ) -> None:
        registered = self._current_generation(self.descriptors, descriptor)
        closed = False
        try:
            closed = closer(descriptor) is None
        except BaseException:
            if closed:
                self._retire_current(self.descriptors, descriptor, registered)
            raise
        self._retire_current(self.descriptors, descriptor, registered)

    def reconcile(self) -> None:
        self._reconcile_pending()
        first_error: BaseException | None = None
        for token in tuple(self.retired):
            try:
                self._close_retired(token)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _claim(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
        ancestors: tuple[RawGenerationReference, ...],
    ) -> GenerationAuthority:
        current: GenerationAuthority | None = None
        stale: GenerationAuthority | None = None
        try:
            current = GenerationAuthority(ancestors)
            stale = authority.get(numeric_value)
            return self._register(authority, numeric_value, current, stale)
        except BaseException:
            if current is None:
                current = GenerationAuthority(ancestors)
            live = authority.get(numeric_value)
            if live is not current:
                self._retain_failed_registration(
                    authority,
                    numeric_value,
                    current,
                    stale if stale is not None else live,
                )
            raise

    def _register(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
        current: GenerationAuthority,
        stale: GenerationAuthority | None,
    ) -> GenerationAuthority:
        superseded: tuple[RawGenerationReference, ...] = ()
        try:
            if authority is self.handles:
                superseded = self._superseded_references(numeric_value, current)
            obsolete = self._unique_generations(
                (() if stale is None else (stale,))
                + tuple(reference.generation for reference in superseded)
            )
            token = object()
            transition = _GenerationTransition(
                authority,
                numeric_value,
                current,
                obsolete,
                superseded,
            )
            self.pending[token] = transition
        except BaseException:
            self._retain_failed_registration(
                authority,
                numeric_value,
                current,
                stale,
                superseded,
            )
            raise
        try:
            authority[numeric_value] = current
            self._complete_transition(token)
            self.reconcile()
        except BaseException:
            if authority.get(numeric_value) is not current:
                authority[numeric_value] = current
            raise
        return current

    def _retain_failed_registration(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
        current: GenerationAuthority,
        stale: GenerationAuthority | None,
        discovered: tuple[RawGenerationReference, ...] = (),
    ) -> None:
        superseded = discovered
        if not superseded and authority is self.handles:
            superseded = self._superseded_references(numeric_value, current)
        obsolete = self._unique_generations(
            (() if stale is None else (stale,))
            + tuple(reference.generation for reference in superseded)
        )
        token = object()
        transition = _GenerationTransition(
            authority,
            numeric_value,
            current,
            obsolete,
            superseded,
        )
        retained = self._transition_retired_references(transition, token)
        if retained:
            self.retired[token] = retained
        self._remove_superseded(superseded)
        authority[numeric_value] = current

    def _retire_current(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
        generation: GenerationAuthority | None,
    ) -> None:
        if generation is None:
            return
        token = object()
        self.retired[token] = generation.ancestor_generations
        if authority.get(numeric_value) is generation:
            del authority[numeric_value]
        self._close_retired(token)

    def _close_retired(self, token: object) -> None:
        ancestors = self.retired.get(token, ())
        if not ancestors:
            self.retired.pop(token, None)
            return
        remaining = list(ancestors)
        ambiguous = list(self.ambiguous_retired.get(token, ()))
        first_error: BaseException | None = None
        for reference in reversed(ancestors):
            error = self._close_retired_reference(
                token,
                reference,
                remaining,
                ambiguous,
            )
            if error is not None and first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def _close_retired_reference(
        self,
        token: object,
        reference: RawGenerationReference,
        remaining: list[RawGenerationReference],
        ambiguous: list[RawGenerationReference],
    ) -> BaseException | None:
        handle = reference.handle
        expected = reference.generation
        registered = self.handles.get(handle)
        if registered is not None and registered is not expected:
            remaining.remove(reference)
            self._publish_retired(token, remaining, ambiguous)
            return None
        if self._generation_is_ambiguous(handle, expected):
            remaining.remove(reference)
            self._mark_retired_ambiguous(reference, ambiguous)
            self._publish_retired(token, remaining, ambiguous)
            return None
        outcome = self._close_raw(handle)
        live_generation = self.handles.get(handle)
        if live_generation is not None and live_generation is not expected:
            remaining.remove(reference)
            if outcome.state is RawCloseState.AMBIGUOUS:
                self._mark_retired_ambiguous(reference, ambiguous)
        else:
            self._apply_retired_outcome(
                reference,
                outcome,
                live_generation,
                remaining,
                ambiguous,
            )
        self._publish_retired(token, remaining, ambiguous)
        return outcome.error

    def _publish_retired(
        self,
        token: object,
        remaining: list[RawGenerationReference],
        ambiguous: list[RawGenerationReference],
    ) -> None:
        if remaining:
            self.retired[token] = tuple(remaining)
        else:
            self.retired.pop(token, None)
        if ambiguous:
            self.ambiguous_retired[token] = tuple(ambiguous)
        else:
            self.ambiguous_retired.pop(token, None)

    def _apply_retired_outcome(
        self,
        reference: RawGenerationReference,
        outcome: RawCloseOutcome,
        live_generation: GenerationAuthority | None,
        remaining: list[RawGenerationReference],
        ambiguous: list[RawGenerationReference],
    ) -> None:
        if outcome.state is RawCloseState.CLOSED:
            remaining.remove(reference)
            if live_generation is reference.generation:
                del self.handles[reference.handle]
        elif outcome.state is RawCloseState.AMBIGUOUS:
            remaining.remove(reference)
            self._mark_retired_ambiguous(reference, ambiguous)

    def _mark_retired_ambiguous(
        self,
        reference: RawGenerationReference,
        ambiguous: list[RawGenerationReference],
    ) -> None:
        ambiguous.append(reference)
        self._record_ambiguous(reference.handle, reference.generation)

    def _record_ambiguous(
        self,
        handle: int,
        generation: GenerationAuthority,
    ) -> None:
        recorded = self.ambiguous.get(handle, ())
        if not any(item is generation for item in recorded):
            self.ambiguous[handle] = (*recorded, generation)

    def _generation_is_ambiguous(
        self,
        handle: int,
        generation: GenerationAuthority,
    ) -> bool:
        return any(item is generation for item in self.ambiguous.get(handle, ()))

    def _current_generation(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
    ) -> GenerationAuthority:
        registered = self._pending_generation(authority, numeric_value)
        if registered is not None:
            if authority.get(numeric_value) is not registered:
                authority[numeric_value] = registered
            return registered
        registered = authority.get(numeric_value)
        if registered is None and authority is self.handles:
            registered = self._ambiguous_retired_generation(numeric_value)
        if registered is None:
            registered = GenerationAuthority(())
            authority[numeric_value] = registered
        return registered

    def _ambiguous_retired_generation(
        self,
        handle: int,
    ) -> GenerationAuthority | None:
        for references in self.ambiguous_retired.values():
            for reference in references:
                if reference.handle == handle:
                    return reference.generation
        return None

    def _pending_generation(
        self,
        authority: dict[int, GenerationAuthority],
        numeric_value: int,
    ) -> GenerationAuthority | None:
        for transition in reversed(tuple(self.pending.values())):
            if (
                transition.authority is authority
                and transition.numeric_value == numeric_value
            ):
                return transition.current
        return None

    def _superseded_references(
        self,
        handle: int,
        current: GenerationAuthority,
    ) -> tuple[RawGenerationReference, ...]:
        return tuple(
            reference
            for references in self.retired.values()
            for reference in references
            if reference.handle == handle and reference.generation is not current
        )

    @staticmethod
    def _unique_generations(
        generations: Iterable[GenerationAuthority],
    ) -> tuple[GenerationAuthority, ...]:
        unique: list[GenerationAuthority] = []
        for generation in generations:
            if not any(item is generation for item in unique):
                unique.append(generation)
        return tuple(unique)

    @staticmethod
    def _same_reference(
        left: RawGenerationReference,
        right: RawGenerationReference,
    ) -> bool:
        return left.handle == right.handle and left.generation is right.generation

    def _transition_retired_references(
        self,
        transition: _GenerationTransition,
        token: object,
    ) -> tuple[RawGenerationReference, ...]:
        recorded = tuple(
            reference
            for owner, references in self.retired.items()
            if owner is not token
            for reference in references
        )
        retained: list[RawGenerationReference] = []
        for generation in transition.obsolete:
            for reference in generation.ancestor_generations:
                if any(self._same_reference(reference, item) for item in recorded):
                    continue
                if any(self._same_reference(reference, item) for item in retained):
                    continue
                retained.append(reference)
        return tuple(retained)

    def _remove_superseded(
        self,
        superseded: tuple[RawGenerationReference, ...],
    ) -> None:
        for token, references in tuple(self.retired.items()):
            retained = tuple(
                reference
                for reference in references
                if not any(
                    self._same_reference(reference, stale)
                    for stale in superseded
                )
            )
            if retained:
                self.retired[token] = retained
            else:
                self.retired.pop(token, None)

    def _complete_transition(self, token: object) -> None:
        transition = self.pending.get(token)
        if transition is None:
            return
        retained = self._transition_retired_references(transition, token)
        if retained:
            self.retired[token] = retained
        self._remove_superseded(transition.superseded)
        self.pending.pop(token, None)

    def _reconcile_pending(self) -> None:
        for token in tuple(self.pending):
            self._complete_transition(token)
