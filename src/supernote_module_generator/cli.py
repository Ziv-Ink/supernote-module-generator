"""Public CLI coordinator."""
from __future__ import annotations

from contextlib import contextmanager
import sys
import time
import traceback
from difflib import get_close_matches
from pathlib import Path
from typing import IO, List, Optional

from . import __version__
from .arguments import COMMANDS, ParsedArguments, parse_arguments
from .devconfig import configured_developer_environment
from .doctor import DoctorService
from .feature_cli_operations import FeatureCliOperationService
from .feature_workflows import FeatureDecisionCollector
from .errors import ConfigurationError, GeneratorError, OperationCancelled, PartialFailure
from .filesystem import contained_entry_kind_no_follow
from .helptext import help_for
from .interaction import (
    BackRequested,
    CancelRequested,
    InputClosed,
    InterruptRequested,
    Interaction,
    MenuItem,
)
from .models import (
    CommandResult,
    ErrorInfo,
    RecoveryAction,
    SubprocessError,
    WarningInfo,
)
from .naming import infer_android_namespace, infer_javascript_name
from .operation_lock import plugin_operation_lock
from .project import resolve_plugin_root
from .project_model import assert_public_project
from .rendering import Renderer, TerminalCapabilities

JOURNAL_NAME = ".supernote-module-transaction.json"

# Public starter choices describe source developers write, not backends.
STARTER_CHOICES = [
    (
        "cpp",
        "C/C++ (native) — Create a C++ starter in the native implementation root.",
    ),
    (
        "kotlin",
        "Kotlin/Java (JVM) — Create a Kotlin starter in the JVM implementation root.",
    ),
]
STARTER_UI_CHOICES = STARTER_CHOICES
MAIN_ACTION_CHOICES = [
    ("add", "Add feature"),
    ("update", "Update project"),
    ("validate", "Validate project"),
    ("doctor", "Doctor"),
    ("help", "Help"),
    ("exit", "Exit"),
]


def _namespace(name: str) -> str:
    return infer_android_namespace(name)


def _module_name(name: str) -> str:
    return infer_javascript_name(name)


def _raw_has(arguments: List[str], option: str) -> bool:
    return any(token == option for token in arguments)


def _renderer(
    arguments: List[str],
    *,
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
    parsed: Optional[ParsedArguments] = None,
) -> Renderer:
    mode = parsed.output_mode if parsed is not None else (
        "json" if _raw_has(arguments, "--json") else "quiet" if _raw_has(arguments, "--quiet") else "verbose" if _raw_has(arguments, "--verbose") else "human"
    )
    plain = parsed.plain if parsed is not None else _raw_has(arguments, "--plain")
    no_color = parsed.no_color if parsed is not None else _raw_has(arguments, "--no-color")
    debug = parsed.debug if parsed is not None else _raw_has(arguments, "--debug")
    capabilities = TerminalCapabilities.detect(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        plain=plain or mode == "json",
        no_color=no_color or mode == "json",
    )
    return Renderer(
        mode,
        capabilities,
        stdout=stdout,
        stderr=stderr,
        debug=debug,
        plain=plain,
    )


def _guess_command(arguments: List[str]) -> str:
    for token in arguments:
        if not token.startswith("-"):
            return token
    return "unknown"


def _usage_result(
    command: str,
    message: str,
    *,
    recovery: Optional[str] = None,
) -> CommandResult:
    result = CommandResult(
        command,
        status="failure",
        exit_code=2,
        error=ErrorInfo("usage", "parse", message),
    )
    result.metadata["recovery_text"] = (
        recovery if recovery is not None else _usage_recovery(command, message)
    )
    return result


def _usage_recovery(command: str, message: str) -> str:
    if message.startswith("unknown command"):
        matches = get_close_matches(command, COMMANDS, n=1, cutoff=0.6)
        suggestion = (
            f"Did you mean `sn-module-gen {matches[0]}`?\n"
            if matches
            else ""
        )
        return suggestion + "Run `sn-module-gen --help` for available commands."
    if message.startswith("unknown option"):
        target = f" {command}" if command in {"add", "update", "validate", "doctor"} else ""
        return f"Run `sn-module-gen{target} --help` for valid options."
    if message == "--starter is required without --yes in non-interactive mode":
        return (
            "Provide --starter cpp, --starter kotlin, or both; or use --yes to accept\n"
            "the C/C++ starter."
        )
    if message == "--quiet, --verbose, and --json cannot be combined":
        return "Choose one output mode."
    if message.startswith("invalid starter family"):
        return "Choose cpp or kotlin. Repeat --starter to scaffold both."
    if message.startswith("invalid package name"):
        return (
            "Use a valid npm package name containing lowercase letters, numbers, hyphens,\n"
            "underscores, dots, tildes, or a valid @scope/name prefix."
        )
    if message.startswith("invalid JavaScript name"):
        return (
            "Use an identifier beginning with a letter, followed by letters or numbers.\n"
            "Provide it with --javascript-name."
        )
    if message.startswith("invalid Android namespace"):
        return (
            "Use dot-separated Java identifiers, for example com.example.local_math.\n"
            "Provide it with --android-namespace."
        )
    if message.startswith("invalid package version"):
        return (
            "Use a valid semantic version, for example 0.1.0.\n"
            "Provide it with --package-version."
        )
    if message.startswith("could not derive a valid JavaScript name"):
        return "Provide one with --javascript-name."
    if message.startswith("could not derive a valid Android namespace"):
        return "Provide one with --android-namespace."
    if message == "package name is required":
        return "Provide it as `sn-module-gen add <PACKAGE>`."
    if message == "node is not available":
        return "Install Node.js, then rerun the command."
    if message.startswith("not a Supernote plugin"):
        return (
            "Expected package.json, android/, and either PluginConfig.json or the\n"
            "official template build script. Run the command from the plugin root."
        )
    if message.startswith("non-interactive Add is missing required decisions"):
        return ""
    if "requires --yes" in message:
        return "Provide --yes for non-interactive confirmation, or run this command in a terminal."
    if message.startswith("module ") and message.endswith(" already exists"):
        return "Choose another package name; update never rewrites authored scaffolding."
    if message.startswith('"') and "exists but is not managed" in message:
        return "Move it, choose another package name, or remove it manually after reviewing its contents."
    if message.startswith("JavaScript name ") and "already used" in message:
        return "Choose another value with --javascript-name."
    if message.startswith("Android namespace ") and "already used" in message:
        return "Choose another value with --android-namespace."
    if message.startswith("dependency ") and "different location" in message:
        return "Review package.json and remove or rename the conflicting dependency."
    if message.startswith("module metadata for "):
        return "Restore the metadata or recreate the module before updating or removing it."
    if message.startswith("package.json could not be read"):
        return "Fix the file and rerun the command."
    if message.startswith("target resolves outside"):
        return "Replace the escaping symlink or choose a path inside the plugin root."
    return "Correct the input and rerun the command."


def _exception_result(command: str, exc: Exception, debug: bool) -> CommandResult:
    if isinstance(exc, GeneratorError):
        if exc.kind == "usage" and exc.phase == "parse":
            return _usage_result(command, exc.message)
        subprocess_error = None
        if exc.subprocess:
            subprocess_error = SubprocessError(
                [str(item) for item in exc.subprocess.get("command", [])],
                int(exc.subprocess.get("exit_code", 1)),
                [str(item) for item in exc.subprocess.get("relevant_lines", [])],
            )
        internal = (
            {"traceback": traceback.format_exc()}
            if debug and exc.kind == "internal"
            else None
        )
        result = CommandResult(
            command,
            status="partial" if exc.exit_code == 3 else "failure",
            exit_code=exc.exit_code,
            error=ErrorInfo(
                exc.kind,
                exc.phase,
                exc.message,
                subprocess_error,
                internal,
            ),
            recovery=(
                RecoveryAction("Recovery is required.", exc.recovery)
                if exc.recovery
                else None
            ),
        )
        result.metadata["phase_label"] = {
            "prepare": "Preparing module",
            "preflight": "Preflight",
            "stage": "Generating module",
            "apply": "Updating plugin",
            "build": "Building Android",
            "api_generation": "Refreshing JavaScript API",
            "startup_recovery": "Startup recovery",
            "internal": "Internal error",
        }.get(exc.phase, exc.phase.replace("_", " ").capitalize())
        if exc.kind == "filesystem_failed" and exc.phase == "prepare":
            result.metadata["next_action"] = (
                f"Correct the directory permissions and rerun {command.capitalize()}."
            )
        elif exc.kind == "internal":
            result.metadata["next_action"] = (
                "Rerun with --debug and report the resulting traceback."
            )
        elif exc.kind == "unsupported_legacy_project":
            result.metadata["next_action"] = (
                "Create a clean plugin and copy only reviewed user-owned source files; "
                "sn-module-gen does not migrate V1-V4 generated state."
            )
        elif exc.kind == "unmanifested_generated_project":
            result.metadata["next_action"] = (
                "Preserve the unmanifested files. Restore the exact schema-version 1.0 "
                "integrity manifest that owns them, or create a clean plugin and copy only "
                "reviewed user-owned source files."
            )
        elif result.recovery is None:
            result.metadata["next_action"] = (
                f"Correct the reported problem and rerun {command.capitalize()}."
            )
        return result
    return CommandResult(
        command,
        status="failure",
        exit_code=1,
        error=ErrorInfo(
            "internal",
            "internal",
            "Supernote Module Generator could not complete the command.",
            internal={"traceback": traceback.format_exc()} if debug else None,
        ),
        metadata={
            "phase_label": "Internal error",
            "next_action": "Rerun with --debug and report the resulting traceback.",
        },
    )


def _guard_legacy_journal(root: Path) -> None:
    """Fail closed on journals created by the retired rollback workflow."""

    journal = root / JOURNAL_NAME
    kind = contained_entry_kind_no_follow(root, journal)
    if kind is None:
        return
    raise PartialFailure(
        "A pending transaction from an older generator is present. Preserve the "
        "project and journal, resolve it with the generator version that created "
        "it, then rerun plain `sn-module-gen update`.",
        kind="legacy_pending_transaction",
        phase="startup_recovery",
    )


def _interactive_for(parsed: ParsedArguments, renderer: Renderer) -> bool:
    return renderer.capabilities.interactive and parsed.output_mode != "json"


@contextmanager
def _developer_environment(
    root: Path, renderer: Renderer, *, report_issues: bool = True
):
    with configured_developer_environment(root) as application:
        if report_issues:
            for message in application.issues:
                warning = WarningInfo(
                    "devconfig",
                    message,
                    "preflight",
                    f"Review {application.path}.",
                )
                if renderer.mode == "json":
                    renderer.pending_warnings.append(warning)
                else:
                    renderer.warning(warning)
        yield


def _run_command(
    parsed: ParsedArguments,
    renderer: Renderer,
    *,
    cwd: Path,
    stdin: IO[str],
    launched_from_menu: bool = False,
) -> CommandResult:
    command = parsed.command or "unknown"
    interactive = _interactive_for(parsed, renderer)
    interaction = Interaction(renderer, stdin=stdin) if interactive else None

    if command == "doctor":
        try:
            valid_root = resolve_plugin_root(cwd)
        except ConfigurationError:
            valid_root = None
        if valid_root is None:
            collector = FeatureDecisionCollector(
                cwd.resolve(),
                parsed,
                interaction,
                launched_from_menu=launched_from_menu,
            )
            return DoctorService(cwd, renderer).execute(collector.doctor_scope())
        with plugin_operation_lock(valid_root):
            assert_public_project(valid_root)
            with _developer_environment(
                valid_root,
                renderer,
                report_issues=not launched_from_menu,
            ):
                _guard_legacy_journal(valid_root)
                collector = FeatureDecisionCollector(
                    valid_root,
                    parsed,
                    interaction,
                    launched_from_menu=launched_from_menu,
                )
                return DoctorService(cwd, renderer).execute(collector.doctor_scope())

    root = resolve_plugin_root(cwd)
    assert_public_project(
        root,
        allow_incomplete_update=parsed.command == "update",
        allow_incomplete_add=parsed.command == "add",
    )
    with plugin_operation_lock(root):
        assert_public_project(
            root,
            allow_incomplete_update=parsed.command == "update",
            allow_incomplete_add=parsed.command == "add",
        )
        _guard_legacy_journal(root)
        with _developer_environment(
            root,
            renderer,
            report_issues=not launched_from_menu,
        ):
            return _run_feature_command(
                parsed,
                renderer,
                root=root,
                stdin=stdin,
                interaction=interaction,
                launched_from_menu=launched_from_menu,
            )

def _run_feature_command(
    parsed: ParsedArguments,
    renderer: Renderer,
    *,
    root: Path,
    stdin: IO[str],
    interaction: Interaction | None,
    launched_from_menu: bool,
) -> CommandResult:
    command = parsed.command or "unknown"
    collector = FeatureDecisionCollector(
        root,
        parsed,
        interaction,
        launched_from_menu=launched_from_menu,
    )
    service = FeatureCliOperationService(root, renderer)
    if command == "add":
        decisions = collector.add()
        result = service.add(decisions)
    elif command == "update":
        decisions = collector.update()
        result = service.update(decisions)
    elif command == "validate":
        decisions = collector.validate()
        result = service.validate(decisions)
    else:
        raise ConfigurationError(f'unknown command "{command}"')
    result.warnings = [
        *collector.warnings,
        *result.warnings,
    ]
    return result


MAIN_MENU_ITEMS = [
    MenuItem("add", "Add feature", "Scaffold one authored local feature."),
    MenuItem("update", "Update project", "Regenerate every owned output."),
    MenuItem("validate", "Validate project", "Check the complete generated project."),
    MenuItem("doctor", "Doctor", "Verify your development environment."),
    MenuItem("help", "Help", "Show commands and usage."),
    MenuItem("exit", "Exit", "Close the generator."),
]

INVALID_ROOT_ITEMS = [
    MenuItem("doctor", "Doctor", "Check feature-generation tools."),
    MenuItem("help", "Help", "Show commands and usage."),
    MenuItem("exit", "Exit"),
]


def _interactive_loop(
    renderer: Renderer,
    *,
    cwd: Path,
    stdin: IO[str],
) -> int:
    ui = Interaction(renderer, stdin=stdin)
    try:
        root = resolve_plugin_root(cwd)
    except ConfigurationError:
        ui.header()
        print(f"\nNot a Supernote plugin: {cwd.resolve()}\n", file=renderer.stderr)
        print(
            "Expected:\n"
            "  package.json\n"
            "  android/\n"
            "  PluginConfig.json or scripts/buildPlugin.sh/.ps1\n",
            file=renderer.stderr,
        )
        try:
            choice = ui.menu(
                "",
                INVALID_ROOT_ITEMS,
                default="doctor",
                footer="Esc exit",
            )
        except InterruptRequested:
            print("Operation cancelled.", file=renderer.stdout)
            return 130
        except (BackRequested, CancelRequested, InputClosed):
            return 0
        if choice == "exit":
            return 0
        if choice == "help":
            renderer.stdout.write(help_for(None))
            return 0
        parsed = parse_arguments(["doctor"])
        result = _run_command(parsed, renderer, cwd=cwd, stdin=stdin, launched_from_menu=True)
        renderer.render(result)
        return result.exit_code

    try:
        with plugin_operation_lock(root):
            _guard_legacy_journal(root)
    except GeneratorError as exc:
        renderer.render(_exception_result("menu", exc, renderer.debug))
        return exc.exit_code
    while True:
        ui.header()
        try:
            choice = ui.menu(
                "",
                MAIN_MENU_ITEMS,
                default="add",
                footer="Esc exit",
            )
        except InterruptRequested:
            print("Operation cancelled.", file=renderer.stdout)
            return 130
        except (BackRequested, CancelRequested, InputClosed):
            return 0
        if choice == "exit":
            return 0
        if choice == "help":
            renderer.stdout.write(help_for(None))
            return 0
        parsed = parse_arguments([choice])
        try:
            result = _run_command(
                parsed,
                renderer,
                cwd=root,
                stdin=stdin,
                launched_from_menu=True,
            )
        except InterruptRequested:
            result = CommandResult(
                choice,
                status="cancelled",
                exit_code=130,
                metadata={"cancellation_message": "Operation cancelled."},
            )
            renderer.render(result)
            return 130
        except OperationCancelled as exc:
            if exc.exit_code == 130:
                result = CommandResult(
                    choice,
                    status="cancelled",
                    exit_code=130,
                    metadata={"cancellation_message": "Operation cancelled."},
                )
                renderer.render(result)
                return 130
            continue
        renderer.render(result)
        return result.exit_code


def _main(
    argv: Optional[List[str]] = None,
    *,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    cwd: Optional[Path] = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    cwd = cwd or Path.cwd()
    started = time.monotonic()
    try:
        parsed = parse_arguments(arguments)
    except Exception as exc:
        renderer = _renderer(arguments, stdin=stdin, stdout=stdout, stderr=stderr)
        result = _exception_result(_guess_command(arguments), exc, renderer.debug)
        result.duration_ms = round((time.monotonic() - started) * 1000)
        renderer.render(result)
        return result.exit_code

    renderer = _renderer(
        arguments,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        parsed=parsed,
    )
    if parsed.show_version:
        print(f"sn-module-gen {__version__}", file=stdout)
        return 0
    if parsed.command == "help":
        stdout.write(help_for(parsed.positional))
        return 0
    if parsed.show_help:
        stdout.write(help_for(parsed.command))
        return 0
    if parsed.command is None:
        if renderer.capabilities.interactive and parsed.output_mode != "json":
            try:
                return _interactive_loop(renderer, cwd=cwd, stdin=stdin)
            except (InterruptRequested, KeyboardInterrupt):
                print("Operation cancelled.", file=stdout)
                return 130
        result = _usage_result(
            "unknown",
            "no command was provided",
            recovery="Run `sn-module-gen --help` for usage.",
        )
        result.duration_ms = round((time.monotonic() - started) * 1000)
        renderer.render(result)
        return result.exit_code
    try:
        result = _run_command(parsed, renderer, cwd=cwd, stdin=stdin)
    except OperationCancelled as exc:
        result = CommandResult(
            parsed.command,
            status="cancelled",
            exit_code=exc.exit_code,
            metadata={
                "cancellation_message": (
                    "Operation cancelled." if exc.exit_code == 130 else exc.message
                )
            },
        )
    except (InterruptRequested, KeyboardInterrupt):
        result = CommandResult(
            parsed.command,
            status="cancelled",
            exit_code=130,
            metadata={"cancellation_message": "Operation cancelled."},
        )
    except InputClosed:
        label = "Validation" if parsed.command == "validate" else parsed.command.capitalize()
        result = CommandResult(
            parsed.command,
            status="cancelled",
            exit_code=0,
            metadata={"cancellation_message": f"{label} cancelled."},
        )
    except SystemExit as exc:
        return int(exc.code)
    except Exception as exc:
        result = _exception_result(parsed.command, exc, renderer.debug)
    result.duration_ms = round((time.monotonic() - started) * 1000)
    if renderer.pending_warnings:
        result.warnings = [*renderer.pending_warnings, *result.warnings]
        renderer.pending_warnings.clear()
    if not result.metadata.get("already_rendered"):
        renderer.render(result)
    return result.exit_code


def main(
    argv: Optional[List[str]] = None,
    *,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
    cwd: Optional[Path] = None,
) -> int:
    """Run the CLI without exposing closed-output-pipe tracebacks."""
    try:
        return _main(
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
        )
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
