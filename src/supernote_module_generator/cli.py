"""Public CLI coordinator."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
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
from .feature_workflows import FeatureDecisionCollector, FeatureValidateDecisions
from .errors import ConfigurationError, GeneratorError, OperationCancelled, PartialFailure
from .filesystem import (
    _windows_host,
    _windows_path_key,
    contained_entry_kind_no_follow,
    read_contained_regular_bytes_no_follow,
)
from .helptext import help_for
from .integrity_manifest import IntegrityManifestError, load_integrity_manifest
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
    RollbackResult,
    SubprocessError,
    WarningInfo,
)
from .naming import infer_android_namespace, infer_javascript_name
from .operation_lock import plugin_operation_lock
from .project import resolve_plugin_root
from .project_model import assert_public_project
from .rendering import Renderer, TerminalCapabilities
from .cli_operations import CliOperationService
from .template_contract import TemplateContractService
from .subprocesses import run_process
from .transaction import JOURNAL_NAME, recover_pending

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
    ("update", "Update feature"),
    ("validate", "Validate feature"),
    ("remove", "Remove feature"),
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
        target = f" {command}" if command in {"add", "update", "validate", "remove", "doctor"} else ""
        return f"Run `sn-module-gen{target} --help` for valid options."
    if message == "--starter is required without --yes in non-interactive mode":
        return (
            "Provide --starter cpp, --starter kotlin, or both; or use --yes to accept\n"
            "the C/C++ starter."
        )
    if message == "--all cannot be used with a module name":
        return "Choose one target or use --all."
    if message == "--quiet, --verbose, and --json cannot be combined":
        return "Choose one output mode."
    if message.startswith("invalid starter family"):
        return "Choose cpp or kotlin. Repeat --starter to scaffold both."
    if message.startswith("invalid package manager"):
        return "Choose one of: npm, yarn."
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
    if message == "package manager is ambiguous":
        return "Both package-lock.json and yarn.lock were found.\nProvide --package-manager npm or --package-manager yarn."
    if message in {"npm is not available", "yarn is not available"}:
        manager = message.split(" ", 1)[0]
        return (
            f"Install {manager} or choose the other supported package manager with\n"
            "--package-manager."
        )
    if message == "node is not available":
        return "Install Node.js, then rerun the command."
    if message.startswith("not a Supernote plugin"):
        return (
            "Expected package.json, android/, and either PluginConfig.json or the\n"
            "official template build script. Run the command from the plugin root."
        )
    if message.startswith("non-interactive Add is missing required decisions"):
        return ""
    if "needs more information in non-interactive mode" in message:
        return "next: provide the missing target, or run this command in a terminal"
    if "requires --yes" in message:
        return "Provide --yes for non-interactive confirmation, or run this command in a terminal."
    if message == "--yes requires an explicit module or --all":
        return "Provide a module name or --all before using --yes."
    if message.startswith("module ") and message.endswith(" was not found"):
        return "Run `sn-module-gen validate --all` to list and check managed features."
    if message.startswith("module ") and message.endswith(" already exists"):
        module = message[len('module "') : -len('" already exists')]
        return f"Use `sn-module-gen update {module}` to refresh it."
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


def _startup_reconcile(root: Path, command: List[str]) -> bool:
    try:
        result = run_process(command, cwd=root, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _recover(root: Path, command: str, renderer: Renderer) -> List[object]:
    try:
        outcome = recover_pending(
            root,
            reconcile=lambda invocation: _startup_reconcile(root, invocation),
        )
    except PartialFailure as exc:
        result = _startup_failure(command, exc)
        renderer.render(result)
        raise SystemExit(3)
    if outcome.rollback.status == "partial":
        recovery_summary = (
            outcome.recovery_summary
            or "Automatic startup recovery is incomplete."
        )
        result = CommandResult(
            command,
            status="partial",
            exit_code=3,
            rollback=outcome.rollback,
            recovery=RecoveryAction(
                recovery_summary,
                outcome.recovery_command or ["sn-module-gen", "doctor"],
            ),
            error=ErrorInfo(
                "startup_recovery_failed",
                "startup_recovery",
                "The previous interrupted operation could not be fully recovered.",
            ),
            next_action=recovery_summary,
            metadata={"phase_label": "Startup recovery"},
        )
        renderer.render(result)
        raise SystemExit(3)
    if outcome.warning is None:
        return []
    if renderer.mode == "json":
        renderer.pending_warnings.append(outcome.warning)
    else:
        renderer.warning(outcome.warning)
    return []


def _startup_failure(command: str, exc: PartialFailure) -> CommandResult:
    return CommandResult(
        command,
        status="partial",
        exit_code=3,
        rollback=RollbackResult(True, "failed", []),
        recovery=RecoveryAction(
            "Automatic startup recovery is incomplete.",
            exc.recovery or ["sn-module-gen", "doctor"],
        ),
        error=ErrorInfo(
            "startup_recovery_failed",
            "startup_recovery",
            exc.message,
        ),
        metadata={"phase_label": "Startup recovery"},
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
            return DoctorService(cwd, renderer).execute(
                collector.doctor_scope(),
                build=parsed.has("build"),
            )
        with plugin_operation_lock(valid_root):
            assert_public_project(valid_root)
            with _developer_environment(
                valid_root,
                renderer,
                report_issues=not launched_from_menu,
            ):
                startup_warnings = _recover(valid_root, command, renderer)
                collector = FeatureDecisionCollector(
                    valid_root,
                    parsed,
                    interaction,
                    launched_from_menu=launched_from_menu,
                )
                result = DoctorService(cwd, renderer).execute(
                    collector.doctor_scope(),
                    build=parsed.has("build"),
                )
                result.warnings.extend(
                    warning for warning in startup_warnings if warning is not None
                )
                return result

    root = resolve_plugin_root(cwd)
    assert_public_project(root)
    if _trusted_parent_build_hook(parsed, root):
        manifest_root = parsed.value("jvm_manifest_root")
        return CliOperationService(root).check(
            jvm_manifest_root=(Path(manifest_root) if manifest_root else None),
        )
    with plugin_operation_lock(root):
        assert_public_project(root)
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


def _trusted_parent_build_hook(parsed: ParsedArguments, root: Path) -> bool:
    """Allow only a matching read-only child hook to reuse its parent check."""

    if parsed.command != "check" or not parsed.has("build_hook"):
        return False
    generation_id = os.environ.get("SUPERNOTE_MODULE_PARENT_GENERATION_ID")
    if not generation_id:
        return False
    try:
        manifest = load_integrity_manifest(root)
    except (IntegrityManifestError, GeneratorError):
        return False
    generation_matches = manifest.generation_id == generation_id
    if not generation_matches:
        return False
    journal_path = root / JOURNAL_NAME
    transaction_id = os.environ.get("SUPERNOTE_MODULE_PARENT_TRANSACTION_ID")
    try:
        journal_kind = contained_entry_kind_no_follow(root, journal_path)
    except GeneratorError:
        return False
    if journal_kind is None:
        return transaction_id is None
    if journal_kind != "file":
        return False
    if not transaction_id:
        return False
    try:
        journal_bytes, _metadata = read_contained_regular_bytes_no_follow(
            root, journal_path
        )
        journal = json.loads(journal_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, GeneratorError):
        return False
    return bool(
        isinstance(journal, dict)
        and journal.get("schema") == 1
        and journal.get("id") == transaction_id
        and (
            (
                _windows_path_key(Path(str(journal.get("root"))))
                == _windows_path_key(root)
            )
            if _windows_host()
            else journal.get("root") == str(root)
        )
        and journal.get("phase") != "commit"
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
    startup_warnings = _recover(root, command, renderer)
    collector = FeatureDecisionCollector(
        root,
        parsed,
        interaction,
        launched_from_menu=launched_from_menu,
    )
    service = FeatureCliOperationService(root, renderer)
    if command == "check":
        manifest_root = parsed.value("jvm_manifest_root")
        result = CliOperationService(root).check(
            build=parsed.has("build"),
            jvm_manifest_root=(Path(manifest_root) if manifest_root else None),
        )
    elif command == "repair":
        result = CliOperationService(root).update(
            (record.manifest.npm_name for record in service.features.records()),
            dry_run=parsed.has("dry_run") or not parsed.has("yes"),
            include_diff=parsed.has("diff"),
            command="repair",
        )
    elif command == "add":
        decisions = collector.add()
        result = service.add(decisions)
    elif command == "update":
        if parsed.has("all") or parsed.has("dry_run") or parsed.has("diff"):
            records = service.features.records()
            if parsed.has("all"):
                requested = tuple(
                    record.manifest.npm_name for record in records
                )
            elif parsed.positional is not None:
                requested = (
                    service.features.find_record(parsed.positional).manifest.npm_name,
                )
            else:
                raise ConfigurationError(
                    "update preview needs a feature or --all"
                )
            if not parsed.has("dry_run") and not parsed.has("yes"):
                raise ConfigurationError(
                    "non-interactive update execution requires --yes; use --dry-run to preview"
                )
            result = CliOperationService(root).update(
                requested,
                dry_run=parsed.has("dry_run"),
                include_diff=parsed.has("diff"),
            )
            result.warnings = [
                *(warning for warning in startup_warnings if warning is not None),
                *collector.warnings,
                *result.warnings,
            ]
            return result
        decisions = collector.update()
        if decisions is None:
            if renderer.mode == "json":
                return CommandResult("update", metadata={"empty": True})
            empty = "No features were found in this plugin.\nAdd one with `sn-module-gen add`."
            if interaction is not None:
                print(f"\n{empty}", file=renderer.stderr)
            elif interaction is None:
                print(empty, file=renderer.stdout)
            return CommandResult("update", metadata={"empty": True, "already_rendered": True})
        result = service.update(decisions)
    elif command == "validate":
        decisions = collector.validate()
        if decisions is None:
            structural_issues = service.features.verify_generated_state()
            if structural_issues:
                decisions = FeatureValidateDecisions((), True, False)
                result = service.validate(decisions)
                result.warnings = [
                    *(warning for warning in startup_warnings if warning is not None),
                    *collector.warnings,
                    *result.warnings,
                ]
                return result
            if renderer.mode == "json":
                return CommandResult("validate", metadata={"empty": True})
            empty = "No features were found in this plugin."
            if interaction is not None:
                print(f"\n{empty}", file=renderer.stderr)
            elif interaction is None:
                print(empty, file=renderer.stdout)
            return CommandResult("validate", metadata={"empty": True, "already_rendered": True})
        result = CliOperationService(root).check(
            build=decisions.build,
            command="validate",
            requested_targets=decisions.package_names,
        )
        records = [
            service.features.find_record(name) for name in decisions.package_names
        ]
        infos = [record.info() for record in records]
        if decisions.all:
            result.modules = infos
        elif infos:
            result.module = infos[0]
    elif command == "remove":
        decisions = collector.remove()
        if decisions is None:
            if renderer.mode == "json":
                return CommandResult("remove", metadata={"empty": True})
            empty = "No features were found in this plugin."
            if interaction is not None:
                print(f"\n{empty}", file=renderer.stderr)
            elif interaction is None:
                print(empty, file=renderer.stdout)
            return CommandResult("remove", metadata={"empty": True, "already_rendered": True})
        result = service.remove(decisions)
    elif command == "template":
        template_service = TemplateContractService(root)
        if parsed.positional == "status":
            result = template_service.status()
        else:
            result = template_service.sync(
                dry_run=parsed.has("dry_run") or not parsed.has("yes")
            )
    else:
        raise ConfigurationError(f'unknown command "{command}"')
    result.warnings = [
        *(warning for warning in startup_warnings if warning is not None),
        *collector.warnings,
        *result.warnings,
    ]
    return result


MAIN_MENU_ITEMS = [
    MenuItem("add", "Add feature", "Create and link a local feature."),
    MenuItem("update", "Update feature", "Refresh generated parts of a feature."),
    MenuItem("validate", "Validate feature", "Check feature structure and integration."),
    MenuItem("remove", "Remove feature", "Permanently delete a local feature."),
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
            with _developer_environment(root, renderer):
                startup = recover_pending(
                    root,
                    reconcile=lambda invocation: _startup_reconcile(
                        root, invocation
                    ),
                )
    except PartialFailure as exc:
        renderer.render(_startup_failure("menu", exc))
        return 3
    except GeneratorError as exc:
        renderer.render(_exception_result("menu", exc, renderer.debug))
        return exc.exit_code
    if startup.rollback.status == "partial":
        recovery_summary = (
            startup.recovery_summary
            or "Automatic startup recovery is incomplete."
        )
        result = CommandResult(
            "menu",
            status="partial",
            exit_code=3,
            rollback=startup.rollback,
            recovery=RecoveryAction(
                recovery_summary,
                startup.recovery_command or ["sn-module-gen", "doctor"],
            ),
            error=ErrorInfo("startup_recovery_failed", "startup_recovery", "The interrupted operation could not be recovered."),
            next_action=recovery_summary,
            metadata={"phase_label": "Startup recovery"},
        )
        renderer.render(result)
        return 3
    if startup.warning:
        renderer.warning(startup.warning)
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
