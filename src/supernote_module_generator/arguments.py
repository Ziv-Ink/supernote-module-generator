"""Strict public command grammar independent from presentation and workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .errors import ConfigurationError

COMMANDS = ("add", "update", "validate", "remove", "doctor", "help")
GLOBAL_BOOLEANS = {
    "-h": "help",
    "--help": "help",
    "-V": "version",
    "--version": "version",
    "--quiet": "quiet",
    "--verbose": "verbose",
    "--json": "json",
    "--no-color": "no_color",
    "--plain": "plain",
    "--debug": "debug",
}
COMMAND_VALUE_OPTIONS: Dict[str, Dict[str, str]] = {
    "add": {
        "--type": "type",
        "--description": "description",
        "--javascript-name": "javascript_name",
        "--android-namespace": "android_namespace",
        "--package-version": "package_version",
        "--package-manager": "package_manager",
    },
    "update": {"--package-manager": "package_manager"},
    "validate": {},
    "remove": {"--package-manager": "package_manager"},
    "doctor": {"--type": "type"},
    "help": {},
}
COMMAND_BOOLEAN_OPTIONS: Dict[str, Dict[str, str]] = {
    "add": {
        "--skip-install": "skip_install",
        "--build": "build",
        "--yes": "yes",
        "-y": "yes",
    },
    "update": {
        "--skip-install": "skip_install",
        "--build": "build",
        "--yes": "yes",
        "-y": "yes",
    },
    "validate": {"--all": "all", "--build": "build"},
    "remove": {
        "--all": "all",
        "--skip-install": "skip_install",
        "--yes": "yes",
        "-y": "yes",
    },
    "doctor": {},
    "help": {},
}


@dataclass(frozen=True)
class ParsedArguments:
    command: Optional[str]
    positional: Optional[str] = None
    values: Dict[str, str] = field(default_factory=dict)
    provided: Set[str] = field(default_factory=set)
    booleans: Set[str] = field(default_factory=set)
    output_mode: str = "human"
    no_color: bool = False
    plain: bool = False
    debug: bool = False
    show_help: bool = False
    show_version: bool = False

    def value(self, name: str) -> Optional[str]:
        return self.values.get(name)

    def has(self, name: str) -> bool:
        return name in self.booleans or name in self.provided


def _split_option(token: str) -> Tuple[str, Optional[str]]:
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return token, None


def _command_index(arguments: List[str]) -> Tuple[Optional[int], Optional[str]]:
    index = 0
    while index < len(arguments):
        token, attached = _split_option(arguments[index])
        if token in GLOBAL_BOOLEANS:
            if attached is not None:
                raise ConfigurationError(f'unknown option "{arguments[index]}"')
            index += 1
            continue
        if token.startswith("-"):
            # A command-specific option cannot validly precede the command.
            raise ConfigurationError(f'unknown option "{token}"')
        return index, token
    return None, None


def _set_value(
    values: Dict[str, str], provided: Set[str], name: str, option: str, value: str
) -> None:
    if name in values and values[name] != value:
        raise ConfigurationError(
            f"{option} was provided more than once with conflicting values"
        )
    values[name] = value
    provided.add(name)


def parse_arguments(arguments: List[str]) -> ParsedArguments:
    command_index, candidate = _command_index(arguments)
    if candidate is not None and candidate not in COMMANDS:
        raise ConfigurationError(f'unknown command "{candidate}"')
    command = candidate
    values: Dict[str, str] = {}
    provided: Set[str] = set()
    booleans: Set[str] = set()
    globals_seen: Set[str] = set()
    positionals: List[str] = []

    index = 0
    while index < len(arguments):
        if command_index is not None and index == command_index:
            index += 1
            continue
        raw = arguments[index]
        option, attached = _split_option(raw)
        if option in GLOBAL_BOOLEANS:
            if attached is not None:
                raise ConfigurationError(f'unknown option "{raw}"')
            globals_seen.add(GLOBAL_BOOLEANS[option])
            index += 1
            continue
        if command is None:
            raise ConfigurationError(f'unknown option "{option}"')
        value_options = COMMAND_VALUE_OPTIONS[command]
        boolean_options = COMMAND_BOOLEAN_OPTIONS[command]
        if option in value_options:
            value: Optional[str] = attached
            if value is None:
                index += 1
                if index >= len(arguments):
                    raise ConfigurationError(f"{option} requires a value")
                value = arguments[index]
            _set_value(values, provided, value_options[option], option, value)
            index += 1
            continue
        if option in boolean_options:
            if attached is not None:
                raise ConfigurationError(f'unknown option "{raw}"')
            booleans.add(boolean_options[option])
            index += 1
            continue
        if option.startswith("-"):
            raise ConfigurationError(f'unknown option "{option}"')
        positionals.append(raw)
        index += 1

    if len(positionals) > 1:
        raise ConfigurationError(
            f'{command} accepts at most one argument; unexpected "{positionals[1]}"'
        )
    positional = positionals[0] if positionals else None

    if command == "help":
        if positional is not None and positional not in COMMAND_HELP_TARGETS:
            raise ConfigurationError(f'unknown command "{positional}"')
    if command in {"validate", "remove"} and positional and "all" in booleans:
        raise ConfigurationError("--all cannot be used with a module name")

    output_flags = [name for name in ("quiet", "verbose", "json") if name in globals_seen]
    if len(output_flags) > 1:
        raise ConfigurationError("--quiet, --verbose, and --json cannot be combined")
    output_mode = output_flags[0] if output_flags else "human"
    if command == "add" and "type" in provided and values["type"] not in {"native", "jni", "jsi"}:
        raise ConfigurationError(f'invalid module type "{values["type"]}"')
    if command == "doctor" and "type" in provided and values["type"] not in {"all", "native", "jni", "jsi"}:
        raise ConfigurationError(f'invalid module type "{values["type"]}"')
    if "package_manager" in provided and values["package_manager"] not in {"npm", "yarn"}:
        raise ConfigurationError(
            f'invalid package manager "{values["package_manager"]}"'
        )

    return ParsedArguments(
        command=command,
        positional=positional,
        values=values,
        provided=provided,
        booleans=booleans,
        output_mode=output_mode,
        no_color="no_color" in globals_seen,
        plain="plain" in globals_seen,
        debug="debug" in globals_seen,
        show_help="help" in globals_seen,
        show_version="version" in globals_seen,
    )


COMMAND_HELP_TARGETS = {"add", "update", "validate", "remove", "doctor"}
