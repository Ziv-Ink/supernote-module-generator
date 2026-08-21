"""Canonical redirect-safe help screens for the public CLI."""
from __future__ import annotations

from typing import Dict


ROOT_HELP = """Supernote Module Generator

Generate and manage language-neutral V3 features in an existing Supernote plugin.

Usage:
  supernote-module
  supernote-module <command> [options]

Commands:
  add        Create and link a local feature.
  update     Refresh generated parts of one feature.
  validate   Check feature structure, integration, and optionally its build.
  remove     Permanently delete one or all features.
  doctor     Verify the development environment.
  help       Show help for a command.

Starter code families:
  C/C++ (native)
    Creates a C++ starter. C23 files can be added to the same native root.
  Kotlin/Java (JVM)
    Creates a Kotlin starter. Java files can be added to the same JVM root.
  Starter selection controls only initial files; one feature can use both.

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
  supernote-module add local-math --starter cpp
  supernote-module add document --starter cpp --starter kotlin
  supernote-module validate --all
  supernote-module doctor

For command-specific help, run a command such as `supernote-module help add`.
"""

ADD_HELP = """Supernote Module Generator

Create and link a language-neutral local feature.

Usage:
  supernote-module add [PACKAGE] [options]

Arguments:
  PACKAGE                         npm or Yarn package name and feature folder.

Options:
      --starter <cpp|kotlin>      Starter code family; repeat to scaffold both.
      --description <TEXT>        Package description; use "" to omit.
      --javascript-name <NAME>    JavaScript feature name.
      --android-namespace <NAME>  Java-style Android namespace.
      --package-version <VERSION> Local feature package version [default: 0.1.0].
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
  Input is never requested. PACKAGE is always required. Without --yes,
  --starter and every initial feature-metadata decision are required. Repeat
  --starter to select both families. With --yes, omitted choices use documented
  defaults: C/C++ starter, empty description, version 0.1.0, derived names, and
  dependency installation unless --skip-install is present. Explicit options
  still override those defaults. Conflicting lockfiles require --package-manager.

Version boundary:
  --package-version belongs to this local feature package. It does not change
  versionCode or versionName in the plugin root's PluginConfig.json.

Name inference:
  Use the unscoped package name, remove an initial react-native-, local-, or
  trailing -plugin, then split on hyphens, underscores, dots, and tildes.
  JavaScript names join the words in PascalCase. Android namespaces use
  com.example followed by lowercase words joined with underscores. Explicit
  options always override inferred values.

Examples:
  supernote-module add
  supernote-module add local-math --starter cpp
  supernote-module add local-math --starter cpp --yes
  supernote-module add document --starter cpp --starter kotlin --build
  supernote-module add @acme/stylus --starter kotlin \\
    --javascript-name Stylus \\
    --android-namespace com.acme.stylus \\
    --package-manager yarn --yes

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
"""

UPDATE_HELP = """Supernote Module Generator

Refresh generated parts of one feature while preserving implementation source.

Usage:
  supernote-module update [MODULE] [options]

Arguments:
  MODULE                         Managed feature package name.

Options:
      --package-manager <npm|yarn>
                                     Package manager when refresh is required.
      --skip-install             Skip a required dependency refresh.
      --build                    Run an Android build after verification.
  -y, --yes                      Update without asking for confirmation.
  -h, --help                     Show help.

Output options:
      --quiet                    Show errors and one final result line.
      --verbose                  Show subprocess output and diagnostics.
      --json                     Emit one versioned JSON result.
      --no-color                 Disable color.
      --plain                    Use line-oriented ASCII output.
      --debug                    Include internal diagnostics and tracebacks.

Behavior:
  Without --yes, interactive Update shows what will be preserved and
  regenerated; confirmation defaults to Yes. Dependencies are refreshed only
  when the parent dependency entry or installed local link needs repair. Update
  always targets one feature.

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

Check feature structure, shared-runtime integration, and optionally the Android build.

Usage:
  supernote-module validate [MODULE] [options]
  supernote-module validate --all [options]

Arguments:
  MODULE       Managed feature package name.

Options:
      --all    Validate every managed feature.
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
  --all reports every feature failure before exiting.

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

Permanently delete one or all managed features and update the shared runtime.

Usage:
  supernote-module remove [MODULE] [options]
  supernote-module remove --all [options]

Arguments:
  MODULE                         Managed feature package name.

Options:
      --all                      Remove every managed feature.
      --delete-build-files       Also remove build/, android/build/, and
                                 android/app/build/.
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
  Without --yes, interactive removal requires the exact package name; removing
  all requires REMOVE ALL. --yes bypasses that prompt only when the target is
  unambiguous. Build output is preserved by default. --yes never enables its
  deletion; pass --delete-build-files explicitly when cleanup is intended.

Recovery:
  Implementation source is retained until parent changes, dependency refresh,
  and postcondition checks succeed. An interrupted operation restores it when
  possible.

Examples:
  supernote-module remove
  supernote-module remove local-math
  supernote-module remove local-math --yes
  supernote-module remove local-math --delete-build-files --yes
  supernote-module remove --all --yes --json

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
"""

DOCTOR_HELP = """Supernote Module Generator

Verify the development environment required by this V3 plugin.

Usage:
  supernote-module doctor [options]

Options:
  -h, --help   Show help.

Output options:
      --quiet     Show errors and one final result line.
      --verbose   Show subprocess output and diagnostics.
      --json      Emit one versioned JSON result.
      --no-color  Disable color.
      --plain     Use line-oriented ASCII output.
      --debug     Include internal diagnostics and tracebacks.

Behavior:
  Doctor checks JavaScript, Kotlin/KSP, Gradle, Java 17 through 23 (Java 17 is
  recommended), Android SDK/NDK tools, NDK Clang with C23/C++23, CMake, and JSI
  requirements used by the plugin-level V3 runtime. It also reports the target-
  device runtime boundary that cannot be proven locally.

Examples:
  supernote-module doctor
  supernote-module doctor --json

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
