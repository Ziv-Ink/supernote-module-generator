"""Exact, redirect-safe help screens from UX specification section 21."""
from __future__ import annotations

from typing import Dict


ROOT_HELP = """Supernote Module Generator

Create and manage local modules in a Supernote plugin.

Usage:
  supernote-module
  supernote-module <command> [options]

Commands:
  add        Create and link a local module.
  update     Refresh generated parts of one module.
  validate   Check module structure, integration, and optionally its build.
  remove     Permanently delete one or all modules.
  doctor     Verify the development environment.
  help       Show help for a command.

Module types:
  Native Module
    For coding in Kotlin/Java and/or using Android APIs.
  Native JNI Module
    For combining Android APIs with existing or performance-intensive C/C++
    code.
  JSI Module
    For low-latency synchronous calls from JavaScript.

Global options:
  -h, --help      Show help.
  -V, --version   Show the version.
      --quiet     Show errors and one final result line.
      --verbose   Show complete subprocess output and diagnostics.
      --json      Emit one versioned JSON result.
      --no-color  Disable color.
      --plain     Use line-oriented ASCII output.
      --debug     Include internal diagnostics and tracebacks.

Examples:
  supernote-module
  supernote-module add local-math --type native
  supernote-module validate --all
  supernote-module doctor --type jsi

Run `supernote-module help <command>` for command-specific help.
"""

ADD_HELP = """Supernote Module Generator

Create and link a local module.

Usage:
  supernote-module add [PACKAGE] [options]

Arguments:
  PACKAGE                         npm or Yarn package name and module folder.

Options:
      --type <native|jni|jsi>     Module type.
      --description <TEXT>        Package description; use "" to omit.
      --javascript-name <NAME>    React Native or JSI name.
      --android-namespace <NAME>  Java-style Android namespace.
      --package-version <VERSION> Initial semantic version [default: 0.1.0].
      --package-manager <npm|yarn>
                                      Package manager for local linking.
      --skip-install              Do not install the local dependency.
      --build                     Run an Android build after verification.
  -y, --yes                       Accept safe documented defaults.
  -h, --help                      Show help.

Output options:
      --quiet                     Show errors and one final result line.
      --verbose                   Show subprocess output and diagnostics.
      --json                      Emit one versioned JSON result.
      --no-color                  Disable color.
      --plain                     Use line-oriented ASCII output.
      --debug                     Include internal diagnostics and tracebacks.

Interactive behavior:
  Missing values are requested in a linear wizard. Derived names and version
  appear as dim inline suggestions; Enter accepts them. Add executes after
  the final valid answer without a confirmation. Installation defaults to Yes.

Non-interactive behavior:
  Input is never requested. PACKAGE is always required. Without --yes, --type
  and every other output-affecting decision are required. With --yes, type is
  native, the description is omitted, the version is 0.1.0, names are derived
  when valid, and installation is enabled. Conflicting lockfiles require
  --package-manager.

Name inference:
  Use the unscoped package name, remove an initial react-native-, local-, or
  trailing -plugin, then split on hyphens, underscores, dots, and tildes.
  JavaScript names join the words in PascalCase. Android namespaces use
  com.example followed by lowercase words joined with underscores. Explicit
  options always override inferred values.

Examples:
  supernote-module add
  supernote-module add local-math --type native
  supernote-module add local-math --type native --yes
  supernote-module add native-search --type jni --build
  supernote-module add @acme/stylus-jsi --type jsi \\
    --javascript-name StylusJsi \\
    --android-namespace com.acme.stylus_jsi \\
    --package-manager yarn --yes

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
"""

UPDATE_HELP = """Supernote Module Generator

Refresh generated parts of one module while preserving implementation source.

Usage:
  supernote-module update [MODULE] [options]

Arguments:
  MODULE                         Managed package name.

Options:
      --package-manager <npm|yarn>
                                     Package manager when refresh is required.
      --skip-install             Skip a required dependency refresh.
      --build                    Run an Android build after verification.
  -y, --yes                      Accept the displayed update plan.
  -h, --help                     Show help.

Output options:
      --quiet                    Show errors and one final result line.
      --verbose                  Show subprocess output and diagnostics.
      --json                     Emit one versioned JSON result.
      --no-color                 Disable color.
      --plain                    Use line-oriented ASCII output.
      --debug                    Include internal diagnostics and tracebacks.

Behavior:
  Update shows what will be replaced, preserved, and changed in the parent
  plugin. Confirmation defaults to Yes. Dependencies are refreshed only when
  package metadata or the local link changes. Update always targets one module.

Examples:
  supernote-module update
  supernote-module update local-math
  supernote-module update local-math --build
  supernote-module update local-math --yes --json

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
"""

VALIDATE_HELP = """Supernote Module Generator

Check module structure, parent integration, and optionally the Android build.

Usage:
  supernote-module validate [MODULE] [options]
  supernote-module validate --all [options]

Arguments:
  MODULE       Managed package name.

Options:
      --all    Validate every managed module.
      --build  Run the applicable Android build.
  -h, --help   Show help.

Output options:
      --quiet     Show errors and one final result line.
      --verbose   Show subprocess output and diagnostics.
      --json      Emit one versioned JSON result.
      --no-color  Disable color.
      --plain     Use line-oriented ASCII output.
      --debug     Include internal diagnostics and tracebacks.

Behavior:
  Structural validation is the default and does not run a full build.
  Interactive use asks whether to add the Android build; the default is No.
  --all reports every module failure before exiting.

Examples:
  supernote-module validate
  supernote-module validate local-math
  supernote-module validate local-math --build
  supernote-module validate --all --json

Exit:
  0 valid
  1 invalid or build failed
  2 usage or input error
  130 interrupted
"""

REMOVE_HELP = """Supernote Module Generator

Permanently delete one or all managed modules and detach parent integration.

Usage:
  supernote-module remove [MODULE] [options]
  supernote-module remove --all [options]

Arguments:
  MODULE                         Managed package name.

Options:
      --all                      Remove every managed module.
      --package-manager <npm|yarn>
                                     Package manager for dependency refresh.
      --skip-install             Skip dependency refresh.
  -y, --yes                      Bypass typed confirmation for automation.
  -h, --help                     Show help.

Output options:
      --quiet                    Show errors and one final result line.
      --verbose                  Show subprocess output and diagnostics.
      --json                     Emit one versioned JSON result.
      --no-color                 Disable color.
      --plain                    Use line-oriented ASCII output.
      --debug                    Include internal diagnostics and tracebacks.

Confirmation:
  Interactive removal always requires the exact package name. Removing all
  requires REMOVE ALL. --yes is accepted only with an unambiguous target.

Recovery:
  Implementation source is retained until parent changes, dependency refresh,
  and postcondition checks succeed. An interrupted operation restores it when
  possible.

Examples:
  supernote-module remove
  supernote-module remove local-math
  supernote-module remove local-math --yes
  supernote-module remove --all --yes --json

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
"""

DOCTOR_HELP = """Supernote Module Generator

Verify the development environment for a known module scope.

Usage:
  supernote-module doctor [options]

Options:
      --type <all|native|jni|jsi>
               Scope to check [default: all].
  -h, --help   Show help.

Output options:
      --quiet     Show errors and one final result line.
      --verbose   Show subprocess output and diagnostics.
      --json      Emit one versioned JSON result.
      --no-color  Disable color.
      --plain     Use line-oriented ASCII output.
      --debug     Include internal diagnostics and tracebacks.

Behavior:
  All strictly checks build requirements for every module type. Native, JNI,
  and JSI check only their applicable requirements. Deployment checks such as
  adb device presence are advisory.

Examples:
  supernote-module doctor
  supernote-module doctor --type native
  supernote-module doctor --type jsi --json

Exit:
  0 required checks passed
  1 one or more required checks failed
  2 usage or input error
  130 interrupted
"""

HELP_HELP = """Supernote Module Generator

Show the command overview or help for one command.

Usage:
  supernote-module help [COMMAND]

Arguments:
  COMMAND  add, update, validate, remove, or doctor.

Examples:
  supernote-module help
  supernote-module help add
"""

COMMAND_HELP: Dict[str, str] = {
    "add": ADD_HELP,
    "update": UPDATE_HELP,
    "validate": VALIDATE_HELP,
    "remove": REMOVE_HELP,
    "doctor": DOCTOR_HELP,
    "help": HELP_HELP,
}


def help_for(command: str | None) -> str:
    return ROOT_HELP if command is None else COMMAND_HELP[command]
