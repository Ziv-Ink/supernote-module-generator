"""Canonical redirect-safe help screens for the public CLI."""
from __future__ import annotations

from typing import Dict


ROOT_HELP = """Supernote Module Generator

Generate typed native features in an existing Supernote plugin.

Usage:
  sn-module-gen
  sn-module-gen <command> [options]

Commands:
  add        Scaffold one authored feature and generate the complete project.
  update     Regenerate every feature and the shared generated runtime.
  validate   Check all source, dependencies, and generated output without writing it.
  doctor     Verify generator tools and their selected versions.
  help       Show help for a command.

Workflow:
  Authors own source, package.json, lockfiles, dependencies, and builds. Use npm
  or Yarn directly, run `sn-module-gen update` after API edits, then run the
  author-owned build separately. Generated files are disposable and overwritten
  by every update.

Starter code families:
  C/C++ (native)
    Creates a C++23 starter and ordinary author-owned CMakeLists.txt. C23 helper
    files can be added behind a marked C++ boundary.
  Kotlin/Java (JVM)
    Creates a Kotlin starter. Update runs the bounded compiler/KSP analysis
    required to derive its typed API; it does not build the app.
  Starter selection controls only initial files; one feature can use both.

Global options:
  -h, --help      Show help.
  -V, --version   Show the version.
      --quiet     Show errors and one final result line.
      --verbose   Show complete analyzer output and diagnostics.
      --json      Emit one versioned JSON result.
      --no-color  Disable color.
      --plain     Use line-oriented ASCII output.
      --debug     Include internal diagnostics and tracebacks.

Examples:
  sn-module-gen
  sn-module-gen add local-math --starter cpp --yes
  sn-module-gen add document --starter cpp --starter kotlin --yes
  sn-module-gen update --yes
  sn-module-gen validate
  sn-module-gen doctor

For command-specific help, run a command such as `sn-module-gen help add`.
"""

ADD_HELP = """Supernote Module Generator

Scaffold one authored feature and generate the complete project.

Usage:
  sn-module-gen add [PACKAGE] [options]

Arguments:
  PACKAGE                         npm or Yarn package name and feature folder.

Options:
      --starter <cpp|kotlin>      Starter code family; repeat to scaffold both.
      --description <TEXT>        Package description; use "" to omit.
      --javascript-name <NAME>    JavaScript feature name.
      --android-namespace <NAME>  Java-style Android namespace.
      --package-version <VERSION> Local feature package version [default: 0.1.0].
  -y, --yes                       Accept safe documented defaults.
  -h, --help                      Show help.

Behavior:
  Add creates only a destination that does not already exist. Authored source,
  metadata, package.json, and CMakeLists.txt are scaffolded once and never
  rewritten by update. Generated files live in dedicated generated locations.
  Add does not invoke npm, Yarn, an app build, or Git. A Kotlin starter uses
  the same bounded standalone Gradle/KSP analysis as update. Add prints the
  dependency command the author may run separately.

Non-interactive behavior:
  PACKAGE is required. Without --yes, every initial decision must be explicit.
  With --yes, omitted choices use documented defaults: C/C++ starter, empty
  description, version 0.1.0, and derived JavaScript/Android names.

Examples:
  sn-module-gen add local-math --starter cpp --yes
  sn-module-gen add document --starter cpp --starter kotlin --yes
  sn-module-gen add @acme/stylus --starter kotlin \\
    --javascript-name Stylus \\
    --android-namespace com.acme.stylus --yes

Exit:
  0 success or user cancellation
  1 operation or validation failure
  2 usage or input error
  3 partial generated output; fix the cause and rerun update
  130 interrupted
"""

UPDATE_HELP = """Supernote Module Generator

Regenerate every current feature and the shared generated runtime.

Usage:
  sn-module-gen update [--yes] [output options]

Options:
  -y, --yes  Confirm overwriting all generator-owned output.
  -h, --help Show help.

Behavior:
  Update discovers the complete current project, runs bounded Kotlin/Java KSP
  analysis when needed, renders every module and shared runtime, and writes all
  generator-owned output even when bytes are unchanged. Manual generated edits
  are overwritten. Obsolete output is removed only inside verified generated
  ownership; zero modules is valid. Update never installs dependencies, changes
  authored/package files, runs an app build, or invokes Git.

  If an update is interrupted, fix the immediate cause and run update again.
  There is no selective update, dry-run/diff preview, or old-output rollback.

Examples:
  sn-module-gen update --yes
  sn-module-gen --json update --yes
"""

VALIDATE_HELP = """Supernote Module Generator

Check the complete current project without publishing generated artifacts.

Usage:
  sn-module-gen validate [output options]

Behavior:
  Validate checks every authored module, declared package dependency, exported
  API, expected generated file, completion marker, and compatibility record.
  Bounded compiler/KSP scratch may be created only in the designated build
  scratch area and is measured separately. Validate does not rewrite generated
  output, install dependencies, or run the app build.

Examples:
  sn-module-gen validate
  sn-module-gen --json validate
"""

DOCTOR_HELP = """Supernote Module Generator

Verify generator tools and their selected versions.

Usage:
  sn-module-gen doctor [output options]

Behavior:
  Doctor checks only generator-relevant setup: Python/package provenance, Node
  package resolution when dependencies are present, CMake 3.24 or newer, a
  C23/C++23 compiler, and Java/Kotlin/KSP tools when JVM source is present. It
  never installs tools, runs an app build, inspects PluginHost/SELinux policy,
  or claims device execution.

Examples:
  sn-module-gen doctor
  sn-module-gen doctor --json
"""

HELP_HELP = """Supernote Module Generator

Show the command overview or help for one command.

Usage:
  sn-module-gen help [COMMAND]

Arguments:
  COMMAND  add, update, validate, or doctor.

Examples:
  sn-module-gen help
  sn-module-gen help add
"""

COMMAND_HELP: Dict[str, str] = {
    "add": ADD_HELP,
    "update": UPDATE_HELP,
    "validate": VALIDATE_HELP,
    "doctor": DOCTOR_HELP,
    "help": HELP_HELP,
}


def help_for(command: str | None) -> str:
    return ROOT_HELP if command is None else COMMAND_HELP[command]
