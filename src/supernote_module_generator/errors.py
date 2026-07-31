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


class TemplateError(GeneratorError):
    kind = "template_failed"
    phase = "generate"


class FilesystemError(GeneratorError):
    kind = "filesystem_failed"
    phase = "filesystem"


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
