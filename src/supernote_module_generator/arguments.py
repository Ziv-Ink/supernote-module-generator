"""Strict public command grammar independent from presentation and workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .errors import ConfigurationError

COMMANDS = ("add", "update", "validate", "doctor", "help")
RETIRED_COMMANDS = {
    "check": (
        "check was removed; use plain `sn-module-gen validate` to check the "
        "project or plain `sn-module-gen update` to regenerate it"
    ),
    "repair": "repair was removed; use plain `sn-module-gen update` to regenerate the project",
    "remove": (
        "remove was removed; delete the authored module and remove its npm/Yarn "
        "dependency, then run plain `sn-module-gen update`"
    ),
    "template": (
        "template commands were removed; template and app scripts are outside "
        "the generator"
    ),
}
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
        "--starter": "starter",
        "--description": "description",
        "--javascript-name": "javascript_name",
        "--android-namespace": "android_namespace",
        "--package-version": "package_version",
    },
    "update": {},
    "validate": {},
    "doctor": {},
    "help": {},
}
COMMAND_BOOLEAN_OPTIONS: Dict[str, Dict[str, str]] = {
    "add": {
        "--yes": "yes",
        "-y": "yes",
    },
    "update": {
        "--yes": "yes",
        "-y": "yes",
    },
    "validate": {},
    "doctor": {},
    "help": {},
}

_RETIRED_OPTIONS = {
    "--all": "selective operation was removed; use the plain project-wide command",
    "--build": "app builds were removed from the generator; run the author-owned build separately",
    "--build-hook": "build hooks were removed; run plain `sn-module-gen validate` directly",
    "--delete-build-files": "the generator no longer removes author build output",
    "--diff": "update preview/diff was removed; use plain `sn-module-gen validate` to check correctness",
    "--dry-run": "update preview/diff was removed; use plain `sn-module-gen validate` to check correctness",
    "--jvm-manifest-root": "external build-hook manifests are not a public command input",
    "--package-manager": "dependency installation is author-owned; use npm or Yarn directly",
    "--skip-install": "dependency installation is author-owned; use npm or Yarn directly",
}


@dataclass(frozen=True)
class ParsedArguments:
    command: Optional[str]
    positional: Optional[str] = None
    values: Dict[str, str] = field(default_factory=dict)
    repeated_values: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
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

    def values_for(self, name: str) -> Tuple[str, ...]:
        return self.repeated_values.get(name, ())


def _split_option(token: str) -> Tuple[str, Optional[str]]:
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return token, None


def _command_index(arguments: List[str]) -> Tuple[Optional[int], Optional[str]]:
    index = 0
    options_ended = False
    while index < len(arguments):
        raw = arguments[index]
        if raw == "--" and not options_ended:
            options_ended = True
            index += 1
            continue
        token, attached = _split_option(raw)
        if not options_ended and token in GLOBAL_BOOLEANS:
            if attached is not None:
                raise ConfigurationError(f'unknown option "{raw}"')
            index += 1
            continue
        if not options_ended and token.startswith("-"):
            # A command-specific option cannot validly precede the command.
            raise ConfigurationError(f'unknown option "{token}"')
        return index, raw
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


@dataclass
class _ArgumentCollection:
    values: Dict[str, str] = field(default_factory=dict)
    repeated_values: Dict[str, List[str]] = field(default_factory=dict)
    provided: Set[str] = field(default_factory=set)
    booleans: Set[str] = field(default_factory=set)
    globals_seen: Set[str] = field(default_factory=set)
    positionals: List[str] = field(default_factory=list)


def _consume_value_option(
    arguments: List[str],
    index: int,
    option: str,
    attached: Optional[str],
    name: str,
    collection: _ArgumentCollection,
) -> int:
    value = attached
    if value is None:
        index += 1
        if index >= len(arguments):
            raise ConfigurationError(f"{option} requires a value")
        value = arguments[index]
        if value.startswith("-"):
            raise ConfigurationError(f"{option} requires a value")
    if name == "starter":
        selected = collection.repeated_values.setdefault(name, [])
        if value in selected:
            raise ConfigurationError(f'{option} "{value}" was provided more than once')
        selected.append(value)
        collection.provided.add(name)
    else:
        _set_value(collection.values, collection.provided, name, option, value)
    return index + 1


def _consume_boolean_option(
    *,
    raw: str,
    option: str,
    attached: Optional[str],
    destinations: Dict[str, str],
    selected: Set[str],
    index: int,
) -> int:
    if attached is not None:
        raise ConfigurationError(f'unknown option "{raw}"')
    selected.add(destinations[option])
    return index + 1


def _consume_token(
    arguments: List[str],
    index: int,
    command: Optional[str],
    options_ended: bool,
    collection: _ArgumentCollection,
) -> Tuple[int, bool]:
    raw = arguments[index]
    if raw == "--" and not options_ended:
        return index + 1, True
    if options_ended:
        collection.positionals.append(raw)
        return index + 1, True
    option, attached = _split_option(raw)
    if option in GLOBAL_BOOLEANS:
        return (
            _consume_boolean_option(
                raw=raw,
                option=option,
                attached=attached,
                destinations=GLOBAL_BOOLEANS,
                selected=collection.globals_seen,
                index=index,
            ),
            False,
        )
    if command is None:
        raise ConfigurationError(f'unknown option "{option}"')
    if option in COMMAND_VALUE_OPTIONS[command]:
        next_index = _consume_value_option(
            arguments,
            index,
            option,
            attached,
            COMMAND_VALUE_OPTIONS[command][option],
            collection,
        )
        return next_index, False
    if option in COMMAND_BOOLEAN_OPTIONS[command]:
        return (
            _consume_boolean_option(
                raw=raw,
                option=option,
                attached=attached,
                destinations=COMMAND_BOOLEAN_OPTIONS[command],
                selected=collection.booleans,
                index=index,
            ),
            False,
        )
    if option in _RETIRED_OPTIONS:
        raise ConfigurationError(f"{option} was removed: {_RETIRED_OPTIONS[option]}")
    if option.startswith("-"):
        raise ConfigurationError(f'unknown option "{option}"')
    collection.positionals.append(raw)
    return index + 1, False


def _collect_arguments(
    arguments: List[str], command_index: Optional[int], command: Optional[str]
) -> _ArgumentCollection:
    collection = _ArgumentCollection()
    options_ended = False
    index = 0
    while index < len(arguments):
        if command_index is not None and index == command_index:
            index += 1
            continue
        index, options_ended = _consume_token(
            arguments, index, command, options_ended, collection
        )
    return collection


def _validate_positionals(
    command: Optional[str], collection: _ArgumentCollection
) -> Optional[str]:
    if len(collection.positionals) > 1:
        raise ConfigurationError(
            f'{command} accepts at most one argument; '
            f'unexpected "{collection.positionals[1]}"'
        )
    positional = collection.positionals[0] if collection.positionals else None
    if command == "help":
        if positional is not None and positional not in COMMAND_HELP_TARGETS:
            raise ConfigurationError(f'unknown command "{positional}"')
    if command == "update" and positional is not None:
        raise ConfigurationError(
            "update no longer accepts a module name; use plain `sn-module-gen update`"
        )
    if command == "validate" and positional is not None:
        raise ConfigurationError(
            "validate no longer accepts a module name; use plain `sn-module-gen validate`"
        )
    return positional


def _output_mode(globals_seen: Set[str]) -> str:
    output_flags = [
        name for name in ("quiet", "verbose", "json") if name in globals_seen
    ]
    if len(output_flags) > 1:
        raise ConfigurationError("--quiet, --verbose, and --json cannot be combined")
    return output_flags[0] if output_flags else "human"


def _validate_values(collection: _ArgumentCollection) -> None:
    invalid_starters = [
        value
        for value in collection.repeated_values.get("starter", [])
        if value not in {"cpp", "kotlin"}
    ]
    if invalid_starters:
        raise ConfigurationError(f'invalid starter family "{invalid_starters[0]}"')


def parse_arguments(arguments: List[str]) -> ParsedArguments:
    command_index, candidate = _command_index(arguments)
    if candidate in RETIRED_COMMANDS:
        raise ConfigurationError(RETIRED_COMMANDS[candidate])
    if candidate is not None and candidate not in COMMANDS:
        raise ConfigurationError(f'unknown command "{candidate}"')
    command = candidate
    collection = _collect_arguments(arguments, command_index, command)
    positional = _validate_positionals(command, collection)
    output_mode = _output_mode(collection.globals_seen)
    _validate_values(collection)
    return ParsedArguments(
        command=command,
        positional=positional,
        values=collection.values,
        repeated_values={
            name: tuple(items) for name, items in collection.repeated_values.items()
        },
        provided=collection.provided,
        booleans=collection.booleans,
        output_mode=output_mode,
        no_color="no_color" in collection.globals_seen,
        plain="plain" in collection.globals_seen,
        debug="debug" in collection.globals_seen,
        show_help="help" in collection.globals_seen,
        show_version="version" in collection.globals_seen,
    )


COMMAND_HELP_TARGETS = {"add", "update", "validate", "doctor"}
