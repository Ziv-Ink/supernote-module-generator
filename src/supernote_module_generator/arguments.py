"""Strict public command grammar independent from presentation and workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .errors import ConfigurationError

COMMANDS = (
    "add", "update", "validate", "check", "repair", "remove", "template", "doctor", "help"
)
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
        "--package-manager": "package_manager",
    },
    "update": {"--package-manager": "package_manager"},
    "validate": {},
    "check": {"--jvm-manifest-root": "jvm_manifest_root"},
    "repair": {},
    "remove": {"--package-manager": "package_manager"},
    "template": {},
    "doctor": {},
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
        "--all": "all",
        "--dry-run": "dry_run",
        "--diff": "diff",
    },
    "validate": {"--all": "all", "--build": "build"},
    "check": {"--build": "build", "--build-hook": "build_hook"},
    "repair": {
        "--dry-run": "dry_run",
        "--diff": "diff",
        "--yes": "yes",
        "-y": "yes",
    },
    "remove": {
        "--all": "all",
        "--delete-build-files": "delete_build_files",
        "--skip-install": "skip_install",
        "--yes": "yes",
        "-y": "yes",
    },
    "template": {
        "--dry-run": "dry_run",
        "--yes": "yes",
        "-y": "yes",
    },
    "doctor": {"--build": "build"},
    "help": {},
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
    if name == "starter":
        selected = collection.repeated_values.setdefault(name, [])
        if value in selected:
            raise ConfigurationError(f'{option} "{value}" was provided more than once')
        selected.append(value)
        collection.provided.add(name)
    else:
        _set_value(collection.values, collection.provided, name, option, value)
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
        if attached is not None:
            raise ConfigurationError(f'unknown option "{raw}"')
        collection.globals_seen.add(GLOBAL_BOOLEANS[option])
        return index + 1, False
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
        if attached is not None:
            raise ConfigurationError(f'unknown option "{raw}"')
        collection.booleans.add(COMMAND_BOOLEAN_OPTIONS[command][option])
        return index + 1, False
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
    if (
        command in {"update", "validate", "remove"}
        and positional
        and "all" in collection.booleans
    ):
        raise ConfigurationError("--all cannot be used with a module name")
    if command in {"check", "repair"} and positional is not None:
        raise ConfigurationError(f"{command} does not accept a module name")
    if command == "template" and positional not in {"status", "sync"}:
        raise ConfigurationError("template requires status or sync")
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
    if (
        "package_manager" in collection.provided
        and collection.values["package_manager"] not in {"npm", "yarn"}
    ):
        raise ConfigurationError(
            f'invalid package manager "{collection.values["package_manager"]}"'
        )


def _validate_template_options(
    command: Optional[str],
    positional: Optional[str],
    collection: _ArgumentCollection,
) -> None:
    if command != "template":
        return
    if positional == "status" and ({"dry_run", "yes"} & collection.booleans):
        raise ConfigurationError(
            "template status does not accept --dry-run or --yes"
        )
    if positional == "sync" and {"dry_run", "yes"} <= collection.booleans:
        raise ConfigurationError("--dry-run and --yes cannot be combined")


def parse_arguments(arguments: List[str]) -> ParsedArguments:
    command_index, candidate = _command_index(arguments)
    if candidate is not None and candidate not in COMMANDS:
        raise ConfigurationError(f'unknown command "{candidate}"')
    command = candidate
    collection = _collect_arguments(arguments, command_index, command)
    positional = _validate_positionals(command, collection)
    output_mode = _output_mode(collection.globals_seen)
    _validate_values(collection)
    _validate_template_options(command, positional, collection)
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


COMMAND_HELP_TARGETS = {
    "add", "update", "validate", "check", "repair", "remove", "template", "doctor"
}
