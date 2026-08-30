from __future__ import annotations

from typing import Any, Dict, List, Optional


class GeneratorError(Exception):
    """Base generator failure with a stable result classification."""

    exit_code = 1
    kind = "operation_failed"
    phase = "operation"

    def __init__(
        self,
        message: str,
        *,
        kind: Optional[str] = None,
        phase: Optional[str] = None,
        recovery: Optional[List[str]] = None,
        subprocess: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind or self.kind
        self.phase = phase or self.phase
        self.recovery = recovery
        self.subprocess = subprocess


class ConfigurationError(GeneratorError):
    exit_code = 2
    kind = "usage"
    phase = "parse"


class ValidationError(ConfigurationError):
    def __init__(self, message: str, *, field: Optional[str] = None) -> None:
        super().__init__(message, kind="invalid_input", phase="collect_decisions")
        self.field = field


class DestinationConflict(GeneratorError):
    exit_code = 2
    kind = "destination_conflict"
    phase = "preflight"


class UnsupportedLegacyProject(GeneratorError):
    """A V1-V4 generated layout was found at the public command boundary."""

    exit_code = 1
    kind = "unsupported_legacy_project"
    phase = "preflight"


class UnmanifestedGeneratedProject(GeneratorError):
    """Current-layout generated state exists without ownership authority."""

    exit_code = 1
    kind = "unmanifested_generated_project"
    phase = "preflight"


class TemplateError(GeneratorError):
    kind = "template_failed"
    phase = "generate"


class TemplateStateError(GeneratorError):
    """The live official-template capability cannot be inspected or synchronized."""

    kind = "template_state_failed"
    phase = "template_preflight"


class FilesystemError(GeneratorError):
    kind = "filesystem_failed"
    phase = "filesystem"


class ConcurrentSourceMutation(FilesystemError):
    """A source entry changed during one bounded observational capture."""


class SymlinkPreservationError(GeneratorError):
    kind = "invalid_source"
    phase = "preflight"


class OperationCancelled(GeneratorError):
    exit_code = 0
    kind = "cancelled"
    phase = "collect_decisions"

    def __init__(self, command: str, *, interrupted: bool = False) -> None:
        label = "Validation" if command == "validate" else command.capitalize()
        super().__init__(f"{label} cancelled.")
        self.command = command
        self.interrupted = interrupted
        if interrupted:
            self.exit_code = 130


class PartialFailure(GeneratorError):
    exit_code = 3
    kind = "partial"


class SubprocessFailure(GeneratorError):
    kind = "subprocess_failed"


class InternalFailure(GeneratorError):
    kind = "internal"
    phase = "internal"
