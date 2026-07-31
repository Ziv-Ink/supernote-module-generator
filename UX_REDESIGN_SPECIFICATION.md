# Supernote Module Generator UX Specification

Status: Approved design  
Audience: CLI/TUI implementation, QA, documentation, and product review  
Design baseline: historical `docs/history/UX_AUDIT_2026-07-31.md`
Interface generation: First public interface; no legacy compatibility contract

This document is normative. Words such as **must**, **must not**, **should**, and
**may** describe implementation requirements. Examples are exact unless marked
as illustrative. An engineer should be able to implement the interface from
this specification without making additional product-design decisions.
End-user setup and programming guidance belongs in `README.md` and `docs/`;
users do not need this specification to operate the generator.

## 1. UX philosophy

The Supernote Module Generator should feel like Cargo with slightly more natural
language:

- calm rather than playful;
- restrained rather than decorative;
- technically precise rather than terse for its own sake;
- predictable across machines and repeated runs;
- understandable without sounding conversational;
- compact enough for daily use, but never cryptic.

The interface is a command-line tool with a guided terminal layer, not a
full-screen application. Commands remain the primary product model. The
interactive menu is a discoverable way to select and complete those commands.

Every launch uses the same interface. There is no onboarding, first-run mode,
walkthrough, tutorial, hidden preference state, or adaptive explanation level.
The wizard must be understandable from its labels, choices, defaults, and at
most one short sentence of guidance. Anything requiring more explanation
belongs in command help, the user guide, or generated module documentation.

The primary optimization target is long-term daily use by developers. A
first-time user must still be able to complete the ordinary workflow without
reading documentation.

## 2. Design principles

1. **Validate before advancing.** Invalid input is corrected at the field where
   it was entered. Previously entered answers remain intact.
2. **Do not claim unverified success.** Generation, integration, dependency
   installation, structural validation, and build validation are separate
   postconditions.
3. **Complete or recover.** Mutations are transactional. Interruptions and
   failures either restore the previous state or identify a partial state and
   provide one exact recovery action.
4. **Ask only for decisions that matter.** Do not ask for a package manager
   when dependency work is skipped. Do not ask Add or Update about builds unless
   `--build` was supplied.
5. **Use stable, visible defaults.** Pressing Enter always selects the displayed
   default. No hidden default may alter generated output.
6. **Keep routine output short.** Show the current decision, current phase,
   result, and next required action. Do not repeat documentation.
7. **Preserve invocation context.** A wizard opened from the main menu returns
   there when backed out. A directly invoked subcommand exits when cancelled.
8. **Separate human and machine interfaces.** Human wording may improve;
   versioned JSON field meanings may not change incompatibly.
9. **Treat terminal capabilities as input.** Color, Unicode, cursor control,
   width, and height are detected and respected.
10. **Use one term for one concept.** Internal implementation names do not leak
    into user-facing text.
11. **Prefer explicit scope over inference when consequences differ.** Doctor
    asks what to check. Conflicting lockfiles require an explicit manager.
12. **Keep the first public interface clean.** Do not carry hidden aliases,
    deprecated spellings, or compatibility branches into the release.

## 3. Information architecture

### 3.1 Product hierarchy

```text
Supernote Module Generator
├── Add module
├── Update module
├── Validate module
├── Remove module
├── Doctor
├── Help
└── Exit
```

### 3.2 Command hierarchy

```text
supernote-module
supernote-module add [PACKAGE_NAME] [OPTIONS]
supernote-module update [MODULE] [OPTIONS]
supernote-module validate [MODULE] [OPTIONS]
supernote-module remove [MODULE] [OPTIONS]
supernote-module doctor [OPTIONS]
supernote-module help [COMMAND]
supernote-module --help
supernote-module --version
```

There is no `build` command. Deeper build verification is expressed as
`--build` on Add, Update, and Validate.

There is no Update-all operation. Update always targets one module.
Validate and Remove support `--all`.

### 3.3 Invocation routing

| Invocation | TTY behavior | Non-TTY behavior |
| --- | --- | --- |
| `supernote-module` | Verify plugin root, then open main menu | Print usage error; exit 2 |
| `supernote-module add` | Open full Add wizard | Report missing required decisions; exit 2 |
| `supernote-module add local-math` | Keep package name; ask unsupplied decisions | Require explicit values or `--yes`; never prompt |
| Any lifecycle subcommand with missing selection | Open only that command's selector/wizard | Report missing selection; exit 2 |
| Complete lifecycle subcommand | Prompt only for unsupplied interactive decisions | Execute without prompting |
| `help`, `--help`, `--version`, `doctor` | Run directly | Run directly |

Explicit arguments and flags are validated before the wizard opens. An invalid
explicit flag is an input error; the wizard must not silently replace it.

### 3.4 Plugin-root behavior

Add, Update, Validate, and Remove require the current directory to be the exact
plugin root containing:

- `PluginConfig.json`;
- `package.json`;
- `android/`;
- `android/settings.gradle` or `android/settings.gradle.kts`.

The tool must not search parent directories silently.

With no arguments, an invalid root prevents the lifecycle menu from opening and
shows only the recovery screen in section 26.8. Explicit Help and Doctor remain
available outside a plugin root. Doctor marks project-specific checks failed or
unavailable rather than fabricating paths.

## 4. Canonical terminology

| Term | Exact meaning |
| --- | --- |
| **Module** | The complete managed object created, updated, validated, or removed |
| **Module type** | Native Module, Native JNI Module, or JSI Module |
| **Package name** | npm/Yarn package identity, module directory segment, and JS/TS import specifier |
| **JavaScript name** | Object exposed to application JavaScript/TypeScript |
| **Android namespace** | Java/Kotlin namespace used by generated and implementation source |
| **Package version** | Semantic version stored in module package metadata |
| **Module directory** | Root directory of a module beneath `local_modules/` |
| **Implementation source** | User-owned Kotlin/Java or C/C++ source |
| **Generated files** | Replaceable generator-owned infrastructure |
| **Plugin root** | Parent Supernote plugin directory |
| **Dependency installation** | npm/Yarn operation that links the local module |
| **Structural validation** | Metadata, generated-output, binding, and parent-integration checks |
| **Build validation** | Full Android build requested with `--build` |

Do not use `folder`, `native code module`, `backend`, `package`, and `project`
as interchangeable names for a Module.

### 4.1 Module-type labels

The public command values are exclusively:

```text
native
jni
jsi
```

The user-facing labels are:

```text
Native Module
  For coding in Kotlin/Java and/or using Android APIs.
Native JNI Module
  For combining Android APIs with existing or performance-intensive C/C++ code.
JSI Module
  For low-latency synchronous calls from JavaScript.
```

There are no aliases for `kotlin`, `cpp`, `--add`, or other legacy forms.
Unknown legacy values receive the same usage error as any unknown value.

## 5. Navigation model

### 5.1 Main menu

The main menu is flat. No nested category headings are used.

```text
Supernote Module Generator

› Add module        Create and link a local module.
  Update module     Refresh generated parts of a module.
  Validate module   Check module structure and integration.
  Remove module     Permanently delete a local module.
  Doctor           Verify your development environment.
  Help             Show commands and usage.
  Exit              Close the generator.

↑/↓ move  Enter select  Esc exit
```

Rules:

- `Add module` is selected initially.
- Descriptions appear only when width allows; selection labels never wrap.
- The plugin path and module count are omitted from the normal header. Errors
  and success output include exact paths when relevant.
- Selecting Help prints the top-level help and exits.
- Selecting Exit exits with status 0 and prints nothing else.
- `Esc` at the main menu exits with status 0.

### 5.2 Wizard history

Wizard output is append-only:

- completed questions remain visible;
- prior screens are never cleared;
- only the currently active arrow-key menu, text field, or spinner redraws in
  place;
- completed text and confirmation prompts remain exactly as entered on their
  original line; they are not reformatted as separate answer rows;
- after a menu selection, that menu collapses in place to one completed-answer
  line before the next prompt is appended.

Example:

```text
Module type: Native Module — Kotlin/Java
Package name: local-math
Description (optional):
```

### 5.3 Back and cancellation

- `Esc` goes back exactly one wizard question.
- Going back restores the previous answer and makes it editable.
- `Esc` on the first question returns to the main menu only when the operation
  was launched from that menu.
- `Esc` on the first question of a directly invoked subcommand cancels and
  exits.
- `Ctrl+C` cancels the entire operation immediately.
- Before mutation, Ctrl+C prints `Operation cancelled.` and exits 130.
- During mutation, Ctrl+C stops the active child process, rolls back, reports
  rollback status, and exits 130 if rollback succeeds.
- If cancellation leaves a partial state, exit 3 and print the recovery action.
- Ctrl+D in a text prompt is handled as a closed input stream. It must never
  show a traceback. It cancels with exit 0 before mutation. During mutation it
  initiates rollback and exits 0 if rollback completes or 3 if recovery remains.
- Answering No to a confirmation is a normal cancellation, not an error. It
  returns to the originating main menu or exits a direct subcommand with status
  0.

### 5.4 Selection controls

Normal capable-terminal mode supports only:

| Key | Behavior |
| --- | --- |
| `↑` / `↓` | Move selection; stop at list boundaries |
| `Enter` | Confirm the current selection |
| `Esc` | Go back |
| `Ctrl+C` | Cancel the entire operation |

There are no Vim `j`/`k` bindings, number shortcuts, Tab navigation, or bare
`q`/`b` commands. `q`, `quit`, `b`, and `back` remain valid field data.

When a list has no selection—for example, conflicting lockfiles—Enter does
nothing and displays:

```text
! Select npm or Yarn.
```

No terminal bell is used.

### 5.5 Menu text input

Cursor menus do not implement text filtering. Printable characters, including
`/`, have no effect. Users select with Up, Down, and Enter. Plain-mode module
lists additionally accept an exact package name so scripts and screen-reader
users do not have to count list positions.

## 6. Visual language guide

### 6.1 Overall style

The interface is structured without panels:

- whitespace separates phases;
- short cyan headings establish hierarchy;
- aligned labels support scanning;
- thin separators may distinguish a title from content;
- enclosing boxes are not used in the first release;
- decoration is prohibited.

### 6.2 Color roles

| Role | Color/style |
| --- | --- |
| Current selection | Cyan |
| Active field/question | Cyan or bold foreground |
| Section heading | Cyan |
| Active phase | Cyan |
| Successful completion | Green |
| Warning/skipped/optional attention | Yellow |
| Error/failed required check | Red |
| Primary question/final result | Bold default foreground |
| Hint/path/default/supporting text | Dim default foreground |

No gradients, backgrounds, brand rainbows, or multicolor decoration are used.
Color always reinforces a symbol, label, or wording; it never carries meaning
alone.

### 6.3 Symbols and fallbacks

| Meaning | Unicode | ASCII fallback |
| --- | --- | --- |
| Selection | `›` | `>` |
| Success | `✓` | `[OK]` |
| Warning | `!` | `[!]` |
| Failure | `×` | `[X]` |
| Not applicable | `—` | `[-]` |
| Active phase | animated spinner | `-`, `\`, `|`, `/` |

Plain mode uses static ASCII labels and no animation.

### 6.4 Headings and separators

Application header:

```text
Supernote Module Generator
```

Command header:

```text
Supernote Module Generator

Add module
```

Subtle separators may divide dense result groups, but routine wizard screens do
not add a rule beneath every heading. Box drawing is omitted in plain mode.

Headings use sentence case. Menu labels retain the canonical title casing of
module types.

### 6.5 Spacing and rhythm

- One blank line follows the application/command context.
- No blank line appears between an immediate hint and its prompt line.
- Consecutive text and confirmation prompts use adjacent lines, matching a
  normal terminal form.
- A blank line is optional between the completed wizard form and execution
  progress.
- One blank line separates progress from final result.
- Tables use two spaces between aligned columns.
- Supporting details are indented two spaces; nested subprocess excerpts are
  indented four.
- Output never begins with multiple blank lines.
- Output ends with exactly one newline.

### 6.6 Responsive layout

- At 72 columns or wider, menu descriptions appear in an aligned second column.
- Below 72 columns, menu descriptions are hidden; labels remain one line.
- Module-type use explanations remain visible beneath their choices at every
  width and wrap at word boundaries. They are guidance, not the optional aligned
  description column.
- At any width, long prose wraps at words, never inside words.
- Active lists use terminal height to calculate a viewport. At least three
  items and the keyboard hint must remain visible.
- Module selectors do not show paths; this avoids truncating them in menus.
- Paths in errors, final results, and recovery commands are printed in full on
  their own line so they remain copyable.
- When an aligned summary does not fit, its value moves to the next indented
  line rather than being truncated.
- Terminal resize redraws only the active menu/spinner using the new dimensions.

## 7. Prompt writing guide

### 7.1 Voice

Use direct, natural, technically precise language.

Use:

```text
Package name
Install the local dependency now? [Y/n]:
Could not derive a valid Android namespace from "123-math".
```

Do not use:

```text
What awesome module would you like to create?
Oops! Something went wrong.
Don't worry, we'll fix this for you.
```

The tool is never playful, apologetic, anthropomorphic, congratulatory, or
patronizing.

### 7.2 Prompt rules

1. A prompt asks one decision.
2. Labels are short noun phrases.
3. Optional fields contain `(optional)` in the label.
4. Defaults use `[value]`.
5. Yes/no defaults use `[Y/n]` or `[y/N]`.
6. Pressing Enter performs exactly the displayed default.
7. Typed destructive confirmations never use bracket syntax.
8. A prompt has at most one short guidance sentence.
9. Technical teaching does not appear inside the wizard.
10. User values are quoted in errors and destructive confirmations.
11. Commands are shell-quoted correctly.
12. Errors identify the correction without blaming the user.

### 7.3 Input normalization

- Identifier fields remove surrounding ASCII whitespace.
- If whitespace was removed, show one dim line:

  ```text
  Using "local-math" (surrounding whitespace removed).
  ```

- Internal whitespace is never removed or transformed silently.
- Description removes surrounding whitespace. An empty result means omitted.
- Description is single-line Unicode text. Terminal control characters,
  newlines, and bidi control characters are rejected.
- Package, JavaScript, namespace, and version values are ASCII by their
  respective grammars.
- Any other normalization or derivation is shown as a default before execution.

### 7.4 Pasted input

- Bracketed paste is enabled in capable terminals.
- A multi-line paste into a single-line field is rejected as one input; queued
  lines must not answer later prompts.
- Message:

  ```text
  This field accepts one line. Paste a single value.
  ```

- Plain mode drains buffered lines before rendering the next prompt and never
  permits a pasted sequence to bypass an Update or Remove confirmation.

## 8. Default behavior guide

### 8.1 Global defaults

| Decision | Default |
| --- | --- |
| Main-menu action | Add module |
| Output mode | Human, normal detail |
| Color | Enabled only in a capable TTY unless disabled |
| Unicode | Enabled only when locale and terminal support it |
| Plain mode | Automatic only when cursor interaction is unsafe |
| Doctor scope | All |

### 8.2 Add defaults

| Decision | Default |
| --- | --- |
| Module type | Native Module (`native`) |
| Package name | None; required |
| Description | Omitted |
| JavaScript name | Deterministically derived and shown as an editable suggestion |
| Android namespace | Deterministically derived and shown as an editable suggestion |
| Package version | `0.1.0`, shown as an editable suggestion |
| Install dependency now | Yes |
| Build | No; only `--build` requests it |
| Final confirmation | None |

### 8.3 Update defaults

| Decision | Default |
| --- | --- |
| Target | First alphabetical module in selector |
| Dependency refresh | Only when metadata or local link changed |
| Build | No; only `--build` requests it |
| Confirmation | Yes, `[Y/n]` |

### 8.4 Validate defaults

| Decision | Default |
| --- | --- |
| Target | All modules; appears first |
| Android build | No, `[y/N]` |
| Mutation | None |

### 8.5 Remove defaults

| Decision | Default |
| --- | --- |
| Target | First alphabetical module; All appears last |
| Confirmation | Exact module name; `REMOVE ALL` for bulk |
| Dependency refresh | Yes |
| Build | Not applicable |

### 8.6 Package-manager selection

| Project evidence | Behavior |
| --- | --- |
| Only `package-lock.json` | Select npm without asking |
| Only `yarn.lock` | Select Yarn without asking |
| No lockfile | Show npm/Yarn selector with npm selected |
| Both lockfiles | Warn, show selector with no selection, require explicit choice |
| `--package-manager` supplied | Validate and use it |
| Dependency work skipped | Do not detect or ask for a manager |

Conflicting-lockfile prompt:

```text
! Both package-lock.json and yarn.lock were found.

Package manager:
  npm
  Yarn

↑/↓ move  Enter select  Esc back
```

Enter has no effect until a selection is made.

In a non-interactive Add with `--yes`, no lockfile selects npm. Without
`--yes`, package manager remains an omitted decision and must be supplied.
Explicit `--package-manager` always wins. If it conflicts with the sole
lockfile, continue with a warning and do not delete the other manager's lockfile.

### 8.7 Precedence

There is no user or project configuration file.

Precedence is:

1. explicit command flags;
2. existing module metadata for lifecycle actions;
3. deterministic project evidence such as one lockfile;
4. documented defaults;
5. a corrective error when a valid result cannot be derived.

Environment variables are limited to standard tool discovery and terminal
behavior, including `JAVA_HOME`, Android SDK variables, `PATH`, `TERM`, locale,
and `NO_COLOR`. They never change module naming or workflow defaults.

## 9. Stable identifier inference

Inference is part of the public interface and must be versioned and tested.
Explicit `--javascript-name` and `--android-namespace` always override it.

### 9.1 Source leaf

Given a valid package name:

1. If scoped, use the leaf after `/`.
2. Remove one leading `react-native-` if present.
3. Remove one leading `local-` if present.
4. Remove one trailing `-plugin` if present.
5. Split the remaining leaf on runs of `-`, `_`, `.`, or `~`.
6. Empty tokens are discarded.

Examples:

| Package name | Tokens |
| --- | --- |
| `local-math` | `math` |
| `react-native-file-tools` | `file`, `tools` |
| `@scope/local-jsi-math` | `jsi`, `math` |
| `network.cache` | `network`, `cache` |

### 9.2 JavaScript name

- Capitalize the first ASCII letter of each token and concatenate.
- Preserve digits after the first character.
- The result must match `[A-Za-z][A-Za-z0-9_]*` and must not be a reserved
  JavaScript word.

Examples:

```text
local-math           → Math
local-http-client    → HttpClient
@scope/local-jsi     → Jsi
```

If no valid result exists:

```text
Could not derive a valid JavaScript name from "123-math".
Provide one with --javascript-name.
```

In the interactive wizard, the custom-names path becomes required and the
JavaScript-name prompt has no default.

### 9.3 Android namespace

- Join tokens with `_` and lowercase them.
- Prefix the result with `com.example.`.
- Every segment must be a valid Java/Kotlin identifier and not a Java or Kotlin
  reserved word.

Examples:

```text
local-math        → com.example.math
network.cache     → com.example.network_cache
local-http-client → com.example.http_client
```

If no valid result exists:

```text
Could not derive a valid Android namespace from "123-math".
Provide one with --android-namespace.
```

In the interactive wizard, the custom-names path becomes required and the
namespace prompt has no default.

### 9.4 Stability rule

These rules must not change within a major JSON/interface schema version.
A future change requires an explicit interface-version migration because it can
change generated class names, native identities, and source paths.

## 10. CLI grammar and output controls

### 10.1 Global options

```text
-h, --help       Show help.
-V, --version    Show the generator version.
--quiet          Show errors and one final result line.
--verbose        Stream subprocess output and show additional diagnostics.
--json           Emit one versioned JSON document and no other output.
--no-color       Disable color.
--plain          Use line-oriented ASCII interaction and output.
--debug          Include internal diagnostics and Python tracebacks.
```

Global options are accepted before or after the subcommand. Conflicting value
options repeated with different values are usage errors. Repeating an
idempotent boolean flag has no additional effect.

`--quiet`, `--verbose`, and `--json` are mutually exclusive. `--plain` and
`--no-color` may accompany human modes. In JSON mode, `--plain` and
`--no-color` are accepted as harmless no-ops so generic scripts do not fail.
`--debug` may accompany any mode; in JSON it adds an `internal` error object
only for unexpected failures and never emits a separate traceback.

### 10.2 Add

```text
supernote-module add [PACKAGE_NAME]
  --type {native,jni,jsi}
  --description TEXT
  --javascript-name NAME
  --android-namespace NAMESPACE
  --package-version VERSION
  --package-manager {npm,yarn}
  --skip-install
  --build
  --yes
```

`--yes` accepts documented safe defaults for omitted decisions. It never
supplies a missing package name and never fabricates an identifier when stable
inference fails.

### 10.3 Update

```text
supernote-module update [MODULE]
  --package-manager {npm,yarn}
  --skip-install
  --build
  --yes
```

Update targets exactly one module. There is no `--all`.

### 10.4 Validate

```text
supernote-module validate [MODULE]
  --all
  --build
```

`MODULE` and `--all` are mutually exclusive.

### 10.5 Remove

```text
supernote-module remove [MODULE]
  --all
  --package-manager {npm,yarn}
  --skip-install
  --yes
```

`MODULE` and `--all` are mutually exclusive. `--yes` bypasses typed
confirmation and is intended for explicit automation.

### 10.6 Doctor

```text
supernote-module doctor
  --type {all,native,jni,jsi}
```

Default: `--type all`. All is strict: every build requirement for all module
types must pass for exit 0. Deployment-only checks remain advisory.

### 10.7 Help

```text
supernote-module help [COMMAND]
```

`supernote-module --help` and `supernote-module help` are identical.
`supernote-module COMMAND --help` and `supernote-module help COMMAND` are
identical.

### 10.8 Non-TTY decision rules

Commands never prompt when stdin or stdout is not a TTY.

For Add:

- package name is always required;
- without `--yes`, every omitted output-affecting default is reported;
- with `--yes`, module type defaults to `native`, description is omitted,
  identifiers are inferred, version is `0.1.0`, and dependency installation is
  Yes;
- when both lockfiles exist and dependency installation is enabled,
  `--package-manager` is required;
- when inference fails, the exact missing override flag is required.

For Update and Remove, `--yes` is required to bypass confirmation.
Validate and Doctor are read-only and do not require `--yes`.

Missing-decision error:

```text
error: Add needs more information in non-interactive mode

  missing  package name

next: provide PACKAGE_NAME, or run this command in a terminal
```

When several values are missing, list all of them in one error.

## 11. Stream contract

### 11.1 Human modes

- Help and reports go to stdout.
- Final success lines go to stdout.
- Prompts, progress, warnings, and errors go to stderr/the controlling terminal.
- Subprocess output in `--verbose` goes to the corresponding original stream.
- No ANSI or cursor-control sequence is emitted when its target stream is not a
  capable TTY.

### 11.2 Quiet mode

- No prompts are suppressed; `--quiet` is an output control, not an
  automation flag.
- Progress and supporting detail are suppressed.
- One final success line remains.
- Warnings and errors remain.

### 11.3 JSON mode

- Exactly one JSON document is written to stdout.
- stderr remains empty for handled outcomes.
- There is no spinner, phase stream, ANSI, Unicode decoration, or prose outside
  JSON.
- Subprocess output is captured. Relevant lines appear in the structured error;
  full output appears only when JSON and `--debug` are combined after an
  unexpected failure. `--verbose` is incompatible with JSON.

## 12. JSON interface

### 12.1 Envelope

Every command returns:

```json
{
  "schema_version": "1.0",
  "tool_version": "1.0.0",
  "command": "add",
  "status": "success",
  "exit_code": 0,
  "duration_ms": 1482,
  "module": null,
  "modules": [],
  "changes": [],
  "dependency": null,
  "validation": null,
  "doctor": null,
  "rollback": {
    "attempted": false,
    "status": "not_needed",
    "restored": []
  },
  "warnings": [],
  "recovery": null,
  "error": null
}
```

`module` contains the one affected module for single-target operations.
`modules` contains stable package-name-ordered results for `--all`; otherwise it
is empty. The top-level envelope and root `null` values are always present so
consumers do not need command-specific existence checks.

Stable `status` values:

```text
success
failure
partial
cancelled
```

### 12.2 Module object

```json
{
  "package_name": "local-math",
  "javascript_name": "Math",
  "type": "native",
  "type_label": "Native Module",
  "path": "/abs/plugin/local_modules/local-math",
  "implementation_path": "/abs/plugin/local_modules/local-math/android/src/main/java/com/example/math",
  "android_namespace": "com.example.math",
  "package_version": "0.1.0"
}
```

### 12.3 Change object

```json
{
  "path": "/abs/plugin/package.json",
  "action": "updated",
  "ownership": "parent"
}
```

Stable action values:

```text
created
updated
removed
preserved
restored
```

Stable ownership values:

```text
module_generated
module_implementation
parent
dependency
```

### 12.4 Dependency object

```json
{
  "requested": true,
  "manager": "npm",
  "status": "installed",
  "verified": true,
  "command": ["npm", "install"],
  "duration_ms": 821
}
```

Stable dependency statuses:

```text
installed
refreshed
removed
skipped
not_needed
failed
unverified
```

### 12.5 Warning object

```json
{
  "kind": "git_dirty",
  "message": "The plugin has 3 uncommitted changes.",
  "phase": "preflight",
  "recovery": null
}
```

Warning kinds are stable. New kinds may be added within schema 1.x.

### 12.6 Validation object

```json
{
  "structural": "passed",
  "integration": "passed",
  "dependency_link": "passed",
  "build": "not_requested",
  "issues": []
}
```

Validation-state values:

```text
passed
failed
skipped
not_requested
not_applicable
```

For `validate --all`, each `modules` entry contains the Module object plus its
own `validation` object. The top-level validation object summarizes the set.

### 12.7 Doctor object

```json
{
  "scope": "all",
  "required_passed": false,
  "required_issue_count": 1,
  "advisory_count": 1,
  "checks": [
    {
      "id": "android_ndk",
      "label": "Android NDK",
      "requirement": "required",
      "status": "failed",
      "detected_version": null,
      "path": null,
      "message": "The required Android NDK was not found."
    },
    {
      "id": "adb_device",
      "label": "Connected device",
      "requirement": "advisory",
      "status": "warning",
      "detected_version": null,
      "path": null,
      "message": "No authorized device is connected."
    }
  ]
}
```

Doctor check statuses are `passed`, `failed`, `warning`, and
`not_applicable`. Check IDs and requirement meanings are stable within schema
1.x. New check IDs may be added.

### 12.8 Rollback and recovery

```json
{
  "rollback": {
    "attempted": true,
    "status": "completed",
    "restored": [
      "/abs/plugin/package.json",
      "/abs/plugin/android/settings.gradle",
      "/abs/plugin/local_modules/local-math"
    ]
  },
  "recovery": null
}
```

Rollback statuses:

```text
not_needed
completed
partial
failed
```

Partial outcome:

```json
{
  "status": "partial",
  "exit_code": 3,
  "recovery": {
    "summary": "Parent integration was restored, but dependencies could not be reconciled.",
    "command": ["npm", "install"]
  }
}
```

### 12.9 Error object

```json
{
  "kind": "dependency_failed",
  "phase": "install_dependency",
  "message": "npm could not install local-math.",
  "subprocess": {
    "command": ["npm", "install"],
    "exit_code": 1,
    "relevant_lines": [
      "npm error ERESOLVE unable to resolve dependency tree"
    ]
  }
}
```

JSON fields may be added within schema 1.x. Existing fields, meanings, enum
values, and types must not be removed or changed incompatibly.

## 13. Error handling guide

### 13.1 Error anatomy

Human-facing failures use this order:

```text
× <Phase> failed

  <One-sentence explanation.>

  <Only the subprocess lines that identify the likely cause.>

  Rollback: <Completed | Partial | Failed | Not needed>
  Next:     <One recovery action or command.>
```

The phase line is red when color is enabled. Labels remain present without
color. Do not print a traceback unless `--debug` is active. Do not print a
generic “Something went wrong” before the useful error.

For an input error detected before execution:

```text
error: <specific problem>

<recovery instruction>
```

Usage errors do not mention rollback because no mutation began. A usage excerpt
is included only when it helps resolve the specific error.

### 13.2 Subprocess excerpts

Default output includes at most eight relevant, non-duplicated lines. Prefer,
in order:

1. the tool's own error or fatal line;
2. the immediately related file, package, or task name;
3. one causal line before and after it;
4. the subprocess exit status.

Boilerplate, repeated stack frames, download progress, and unrelated warnings
are omitted. If output was shortened:

```text
  Additional output omitted. Rerun with --verbose for complete subprocess output.
```

`--verbose` streams the subprocess unchanged while it runs and still ends with
the structured failure summary. `--debug` additionally prints the Python
traceback and transaction identifiers.

### 13.3 Warnings

A warning describes a real risk or ambiguity, never routine information:

```text
! The plugin has 3 uncommitted changes.
  The operation can continue, but those changes may affect manual recovery.
```

Warnings never rely on yellow alone. A warning does not add a confirmation
unless the operation is Update or Remove, which already has one. A clean Git
working tree never weakens Remove's typed confirmation.

### 13.4 Recovery rules

- Always state whether mutation began.
- If mutation began, always state the rollback result.
- Never claim success solely because a subprocess returned zero; verify the
  required postconditions.
- If the previous state is fully restored, exit with the original operation
  failure (`1`) or cancellation (`130`).
- If restoration or dependency reconciliation is incomplete, exit `3` and give
  one exact recovery command.
- Never tell the user to delete an internal staging directory manually.
- Never retain logs by default. Transaction state is not a log and is removed
  after successful completion or recovery.

## 14. Progress indicator guide

### 14.1 Timing

- For work under 250 ms, replace the active phase directly with its completed
  line; do not flash a spinner.
- At 250 ms, show the spinner and phase.
- At one second, append elapsed time rounded to tenths below ten seconds and to
  whole seconds thereafter.
- Do not show percentages unless the underlying process reports trustworthy
  units. None of the initial workflows uses a percentage.

Example:

```text
✓ Prepared module
✓ Generated module
⠹ Installing dependency  4.2s
```

ASCII:

```text
[OK] Prepared module
[OK] Generated module
| Installing dependency  4.2s
```

Completed phases remain visible as concise lines. Only the active spinner line
is redrawn. `--plain` prints one start line and one result line per phase:

```text
... Installing dependency
[OK] Installed dependency
```

### 14.2 Canonical phases

Add:

```text
Preparing module
Generating module
Updating plugin
Installing dependency                 # omitted when skipped
Verifying module
Building Android                      # only with --build
```

Update:

```text
Preparing update
Staging generated changes
Updating plugin
Refreshing dependencies               # only when required
Verifying module
Building Android                      # only with --build
```

Validate:

```text
Checking module                        # or Checking modules
Building Android                      # only when requested
```

Remove:

```text
Preparing removal
Detaching module
Refreshing dependencies               # unless skipped
Verifying plugin
Deleting module
```

Doctor:

```text
Checking project
Checking JavaScript tools
Checking Android tools
Checking native tools                 # applicable scope only
Checking deployment tools
```

Use past-tense completion lines: `Prepared update`, `Updated plugin`,
`Verified module`, and `Built Android`. A skipped optional phase remains
visible only when its absence matters:

```text
! Skipped dependency installation
```

## 15. Transaction and recovery model

Mutation commands are transactions even though package managers and build tools
are external processes.

### 15.1 Preflight

Before the first write:

1. validate all supplied and inferred values;
2. resolve the plugin root to a canonical path;
3. verify every target remains within that root;
4. inspect ownership metadata and all name/namespace collisions;
5. parse parent manifests and Gradle settings;
6. resolve and execute a minimal health check for each required tool;
7. confirm target directories are writable;
8. compute the complete change plan.

Late-discovered user input errors are design defects. Preflight must catch them.

### 15.2 Staging and commit

Stage generated content on the same filesystem as the destination. Record a
small transaction journal containing only paths, hashes, phase state, and
restore locations. Snapshot every parent file that may change. Commit with
atomic renames where the platform supports them.

For Remove, rename the module to a transaction-owned restore location before
editing parent integration. Permanently delete it only after dependency refresh
and postcondition checks pass.

### 15.3 External side effects

`node_modules` is not treated as transactionally restorable. The tool restores
the manifest and lockfile, then runs the selected package manager to reconcile
the dependency tree. If reconciliation fails, parent source state is restored
but the result is partial:

```text
× Dependency recovery failed

  The plugin files were restored, but node_modules may not match package.json.

  Rollback: Partial
  Next:     Run npm install
```

Exit `3`.

### 15.4 Interruption

`Ctrl+C` is handled at every phase:

- before mutation: print `Operation cancelled.` and exit `130`;
- after mutation with successful rollback:
  `Operation cancelled. Previous state restored.` and exit `130`;
- after mutation with incomplete rollback: print the recovery summary and exit
  `3`.

The first interrupt requests orderly cancellation. A second interrupt stops
immediately but leaves the journal for startup recovery.

At the next launch, recover an incomplete transaction before handling the new
command:

```text
! Recovered an interrupted Update for "local-math".
  Previous state restored.
```

Then continue. If automatic recovery cannot complete, report exit `3` and do
not begin the requested command. In JSON mode, successful startup recovery is
an entry in `warnings`; failed recovery is the command result.

## 16. Complete rewritten workflows

### 16.1 Startup and main menu

In an interactive terminal, `supernote-module`:

1. resolves the current plugin root;
2. performs startup transaction recovery if needed;
3. draws the flat main menu;
4. starts the selected workflow.

Selecting Help prints the root help and exits. Selecting Exit clears the active
menu line and exits `0` without a farewell. A workflow chosen from the menu
returns to the menu when backed out from its first question or when an Update or
Remove confirmation is declined. Successful workflows print their summary and
exit.

In a non-interactive environment, a missing command is a usage error:

```text
error: no command was provided

Run `supernote-module --help` for usage.
```

### 16.2 Add

Question order:

1. Module type, unless supplied.
2. Package name, unless supplied positionally.
3. Description (optional), unless supplied.
4. JavaScript name with its derived value as an editable suggestion, unless
   supplied or `--yes` is active.
5. Android namespace with its derived value as an editable suggestion, unless
   supplied or `--yes` is active.
6. Package version with `0.1.0` as an editable suggestion, unless supplied or
   `--yes` is active.
7. `Install the local dependency now? [Y/n]:`, unless decided by
   `--skip-install` or non-interactive `--yes`.
8. Package manager only when installation is selected and project evidence does
   not unambiguously select one.
9. Execute immediately.

Values supplied on the command line are shown in the persistent answer history
as dim supporting information and are not prompted again. They receive the same
validation as typed values.

Blank Description omits the `description` field. `--description ""` does the
same. In a capable cursor terminal, suggested values appear dim in the input
position after the colon: `<label>: <suggestion>`. Only the suggestion is dim.
It disappears when the user enters text and reappears if the field is cleared.
Enter on an untouched or cleared field accepts the suggestion. Because plain
mode cannot communicate a suggestion with color, it uses the explicit fallback
`<label> [<suggestion>]:`. Install defaults to Yes.

If a default cannot be derived interactively, the flow does not fail. It
replaces the relevant default with a required prompt:

```text
  Enter a Java-style namespace, for example com.example.local_math.
Android namespace:
```

The user may go back with `Esc` at any point. A valid final answer begins
preflight without a review or confirmation screen.

Non-interactive Add never prompts. Without `--yes`, every unsupplied decision
that normally has a prompt is reported together:

```text
error: non-interactive Add is missing required decisions

Provide:
  --description <TEXT> or --description ""
  --javascript-name <NAME>
  --android-namespace <NAMESPACE>
  --package-version <VERSION>
  --package-manager <npm|yarn>

Use --yes to accept documented defaults where available.
```

With `--yes`, Description is omitted, version is `0.1.0`, installation is
enabled, npm is used only when no lockfile exists, and deterministic name
inference is allowed. Package name remains required; type defaults to `native`.
Conflicting lockfiles still require `--package-manager`.

### 16.3 Update

Question order:

1. Module selection, unless supplied.
2. Package manager only if a dependency refresh is required and evidence is
   ambiguous.
3. Change plan.
4. `Update this module? [Y/n]:`
5. Execute and exit.

Update operates on exactly one module. The plan is generated from ownership
metadata and live parent integration. It groups information under fixed labels:

```text
Update "local-math"

  Replace
    Generated Kotlin source
    Generated Gradle configuration
    Generated package metadata

  Preserve
    Implementation source
    Description and package version

  Parent changes
    No dependency refresh required

  Git
    3 uncommitted changes

Update this module? [Y/n]:
```

Omit empty groups. The plan never says merely “Some files will be updated.”
`--build` adds `Android build` under Verify. `--yes` accepts the shown plan in
non-interactive use. A negative answer prints `Update cancelled.`; no mutation
occurs. A menu-launched flow returns to the main menu, while a direct command
exits `0`.

### 16.4 Validate

Question order:

1. Select one module or All, unless supplied.
2. `Run an Android build too? [y/N]:`, unless `--build` or non-interactive.
3. Execute and exit.

Structural validation checks ownership metadata, generated-file completeness,
unresolved template tokens, package metadata, parent integration, dependency
linking, Gradle inclusion, and type-specific generated contracts. `--build`
adds the applicable Android build.

All validates every owned module independently, then parent-wide integration.
It does not stop at the first module failure. Results are listed in stable
package-name order.

### 16.5 Remove one

Question order:

1. Module selection, unless supplied.
2. Package manager only if refresh is enabled and evidence is ambiguous.
3. Consequence summary and typed confirmation.
4. Execute and exit.

Exact confirmation:

```text
Remove "local-math"

  Module path   /work/plugin/local_modules/local-math
  Parent state  Dependency and Gradle integration will be removed
  Git           3 uncommitted changes

This will permanently delete the module and its implementation source.
Type "local-math" to continue:
```

The input must match the canonical package name exactly. Any other non-empty
input prints `Remove cancelled.` An empty input leaves the question active.
`Esc` goes back. A menu-launched cancellation returns to the main menu; a
direct command exits `0`.

`--yes` is the explicit automation equivalent and is accepted only when the
target module is supplied unambiguously.

### 16.6 Remove all

`remove --all` and the interactive All choice list every module and the count:

```text
Remove all modules

  3 modules will be permanently deleted:
    local-math
    native-search
    stylus-jsi

  Parent dependency and Gradle integration will be removed.

This will permanently delete every module and its implementation source.
Type "REMOVE ALL" to continue:
```

The match is case-sensitive. `remove --all --yes` is permitted for explicit
automation. A target module and `--all` are mutually exclusive.

### 16.7 Doctor

Question order:

1. Check scope: All, Native Module, Native JNI Module, or JSI Module.
2. Execute and exit.

All is preselected. Doctor does not infer scope from existing modules.
`doctor --type all` is the CLI default.

Doctor separates build requirements from deployment advisories:

```text
Supernote Module Generator

Doctor — All

✓ Project       Plugin root and package metadata
✓ JavaScript    Node.js, npm, or Yarn
✓ Android       Java, Android SDK, Gradle wrapper
× Native        CMake and Android NDK
! Deployment    adb found; no device connected

× Doctor found 1 required issue and 1 advisory.

  Required: Set ANDROID_NDK_HOME or install the project NDK version.
  Advisory: Connect a device before deploying a plugin.
```

All is strict: any missing build requirement for any of the three module types
fails with exit `1`. Deployment-only checks, including adb device presence, are
advisory and do not change a passing build result. Doctor must report when
SELinux execution policy was not inspected; it must never equate local library
visibility with device runtime compatibility.

### 16.8 Help

The menu Help action, `supernote-module help`, and
`supernote-module --help` print the same root help and exit. Command help never
opens a pager. It is plain, static output whose layout remains valid when
copied or redirected.

## 17. Exact interactive screens

These examples define wording, order, capitalization, and spacing. Paths and
values are examples. The `›` cursor and colors follow the visual language.

### 17.1 Main menu

Wide terminal:

```text
Supernote Module Generator

› Add module        Create and link a local module.
  Update module     Refresh generated parts of a module.
  Validate module   Check module structure and integration.
  Remove module     Permanently delete a local module.
  Doctor            Verify your development environment.
  Help              Show commands and usage.
  Exit              Close the generator.

↑/↓ move  Enter select  Esc exit
```

Narrow terminal:

```text
Supernote Module Generator

› Add module
  Update module
  Validate module
  Remove module
  Doctor
  Help
  Exit

↑/↓ move  Enter select  Esc exit
```

### 17.2 Add

```text
Supernote Module Generator

Add module

Module type:
› Native Module      Kotlin/Java
    For coding in Kotlin/Java and/or using Android APIs.
  Native JNI Module  C/C++ via JNI
    For combining Android APIs with existing or performance-intensive C/C++
    code.
  JSI Module         C/C++ (synchronous)
    For low-latency synchronous calls from JavaScript.

↑/↓ move  Enter select  Esc back
```

After selection, the answer remains:

```text
Supernote Module Generator

Add module

Module type:  Native Module — Kotlin/Java

  Used as the local folder and npm or Yarn dependency name.
Package name:
```

Then:

```text
Package name: local-math
Description (optional):
```

Then:

```text
Description (optional): Fast local math operations
JavaScript name: Math
```

After accepting the suggestion with Enter, the original prompt remains and the
next prompt follows it. `Math` and the other suggested values below are dim:

```text
JavaScript name: Math
Android namespace: com.example.math
```

```text
Android namespace: com.example.math
Package version: 0.1.0
```

```text
Package version: 0.1.0
Install the local dependency now? [Y/n]:
```

Typing a replacement removes the suggestion and keeps the typed value on the
same line, for example `JavaScript name: CustomMath`.

When a package manager question is required:

```text
Install the local dependency now? [Y/n]:
Package manager:
› npm
  Yarn

↑/↓ move  Enter select  Esc back
```

With conflicting lockfiles:

```text
Install the local dependency now? [Y/n]:
! Both package-lock.json and yarn.lock were found.

Package manager:
  npm
  Yarn

↑/↓ move  Enter select  Esc back
```

There is no initial selection in the conflict case. Enter does nothing until
the user moves to an option.

### 17.3 Module selection

Update:

```text
Supernote Module Generator

Update module

Module:
› local-math       Native Module
  native-search    Native JNI Module
  stylus-jsi       JSI Module

↑/↓ move  Enter select  Esc back
```

Validate includes a synthetic All choice first:

```text
Supernote Module Generator

Validate module

Module:
› All modules      3 modules
  local-math       Native Module
  native-search    Native JNI Module
  stylus-jsi       JSI Module
```

Remove uses the same structure and adds `All modules` as the final choice,
separated by a blank line so the destructive bulk action is not the default:

```text
Module:
› local-math       Native Module
  native-search    Native JNI Module
  stylus-jsi       JSI Module

  All modules      Permanently delete all 3 modules
```

### 17.4 Validate build question

```text
Module:  All modules

Run an Android build too? [y/N]:
```

### 17.5 Doctor scope

```text
Supernote Module Generator

Doctor

Check:
› All
  Native Module
  Native JNI Module
  JSI Module

↑/↓ move  Enter select  Esc back
```

### 17.6 Empty states

Update:

```text
Supernote Module Generator

Update module

No modules were found in this plugin.
Add one with `supernote-module add`.
```

Validate:

```text
Supernote Module Generator

Validate module

No modules were found in this plugin.
```

Remove:

```text
Supernote Module Generator

Remove module

No modules were found in this plugin.
```

When entered from the main menu, Enter or Esc returns to the menu. When invoked
as a direct command, the message prints and exits `0`.

### 17.7 Plain-mode numbered menus

`--plain` never reads individual keypresses. It uses line input:

```text
Module type:
  1. Native Module - Kotlin/Java
     For coding in Kotlin/Java and/or using Android APIs.
  2. Native JNI Module - C/C++ via JNI
     For combining Android APIs with existing or performance-intensive C/C++
     code.
  3. JSI Module - C/C++
     For low-latency synchronous calls from JavaScript.
Choose [1-3]:
```

Plain-mode module lists accept a number or an exact package name:

```text
Module:
  1. local-math - Native Module
  2. native-search - Native JNI Module
  3. stylus-jsi - JSI Module
Choose a number or package name:
```

Typing `:back` replaces Esc. Typing `:cancel` cancels the operation. These
control forms are documented in the prompt footer only when `--plain` is
interactive:

```text
Type ":back" for the previous question or ":cancel" to exit.
```

Numbered input is an accessibility fallback in `--plain`, not an additional
shortcut in the cursor interface. Ordinary words such as `back` and `cancel`
remain valid field data.

## 18. Prompt and confirmation catalog

The complete prompt vocabulary is:

| Purpose | Exact prompt |
|---|---|
| Type | `Module type:` |
| Package name | `Package name:` |
| Description | `Description (optional):` |
| JavaScript name | `JavaScript name: <derived>` (dim suggestion) |
| Namespace | `Android namespace: <derived>` (dim suggestion) |
| Version | `Package version: 0.1.0` (dim suggestion) |
| Install | `Install the local dependency now? [Y/n]:` |
| Manager | `Package manager:` |
| Select one | `Module:` |
| Validate build | `Run an Android build too? [y/N]:` |
| Doctor scope | `Check:` |
| Update | `Update this module? [Y/n]:` |
| Remove one | `Type "<package-name>" to continue:` |
| Remove all | `Type "REMOVE ALL" to continue:` |

Boolean inputs accept `y`, `yes`, `n`, or `no`, case-insensitively. An empty
answer accepts the displayed default. Other input remains on the same question:

```text
Enter yes or no.
Update this module? [Y/n]:
```

Typed Remove confirmations are case-sensitive and do not trim internal
whitespace. Leading and trailing whitespace is ignored. Clipboard paste is
accepted. A mismatch cancels instead of inviting repeated attempts, reducing
the chance that users mechanically work around the safeguard.

## 19. Success and cancellation catalog

Human success output begins with one green final line. Supporting lines appear
only when they convey a path, state change, or required next step.

### 19.1 Add

Installed:

```text
✓ Added module "local-math"
  Path: /work/plugin/local_modules/local-math
  Dependency: installed with npm
```

Installed and built:

```text
✓ Added and built module "local-math"
  Path: /work/plugin/local_modules/local-math
  Dependency: installed with npm
```

Installation skipped:

```text
✓ Added module "local-math"
  Path: /work/plugin/local_modules/local-math
  Next: Run npm install
```

The package-manager command is `yarn install` when Yarn is selected or inferred.
If dependency work was skipped while both lockfiles exist and no manager was
supplied, use the only truthful next step:

```text
✓ Added module "local-math"
  Path: /work/plugin/local_modules/local-math
  Next: Choose npm or Yarn, then install dependencies
```

Update and Remove use the same Next line in this ambiguous skipped state.

### 19.2 Update

```text
✓ Updated module "local-math"
```

With dependency refresh:

```text
✓ Updated module "local-math"
  Dependency: refreshed with npm
```

With build:

```text
✓ Updated and built module "local-math"
```

If a required dependency refresh was skipped:

```text
✓ Updated module "local-math"
  Next: Run npm install
```

### 19.3 Validate

One:

```text
✓ Module "local-math" is valid
```

One with build:

```text
✓ Module "local-math" is valid and builds successfully
```

All:

```text
✓ All 3 modules are valid
```

All with build:

```text
✓ All 3 modules are valid and build successfully
```

### 19.4 Remove

One:

```text
✓ Removed module "local-math"
```

All:

```text
✓ Removed 3 modules
```

If dependency refresh was skipped:

```text
✓ Removed module "local-math"
  Next: Run npm install
```

### 19.5 Doctor

```text
✓ Doctor found no required issues
```

Advisories precede the same success line:

```text
! Deployment: adb found; no device connected

✓ Doctor found no required issues
```

### 19.6 Quiet

`--quiet` prints exactly one line on success:

```text
Added module "local-math"
Updated module "local-math"
Module "local-math" is valid
Removed module "local-math"
Doctor found no required issues
```

No symbols or paths are included. Failures retain the normal concise error
summary because suppressing recovery information would be harmful.

### 19.7 Cancellation

```text
Add cancelled.
Update cancelled.
Validation cancelled.
Remove cancelled.
Doctor cancelled.
Operation cancelled.
Operation cancelled. Previous state restored.
```

Menu Exit has no output. A user answer of No or a typed-confirmation mismatch is
a successful cancellation: a menu-launched flow returns to the menu and a
direct command exits `0`. `Ctrl+C` always exits the application with `130`.

## 20. Error message catalog

Variables appear in angle brackets in this catalog; implementation substitutes
actual values without angle brackets.

### 20.1 Command and input

```text
error: unknown command "<command>"

Run `supernote-module --help` for available commands.
```

```text
error: unknown option "<option>"

Run `supernote-module <command> --help` for valid options.
```

```text
error: --type is required without --yes in non-interactive mode

Provide one of: --type native, --type jni, --type jsi; or use --yes to accept
Native Module.
```

```text
error: invalid module type "<value>"

Choose one of: native, jni, jsi.
```

```text
error: package name is required

Provide it as `supernote-module add <PACKAGE>`.
```

```text
error: invalid package name "<value>"

Use a valid npm package name containing lowercase letters, numbers, hyphens,
underscores, dots, tildes, or a valid @scope/name prefix.
```

```text
error: invalid JavaScript name "<value>"

Use an identifier beginning with a letter, followed by letters or numbers.
Provide it with --javascript-name.
```

```text
error: invalid Android namespace "<value>"

Use dot-separated Java identifiers, for example com.example.local_math.
Provide it with --android-namespace.
```

```text
error: invalid package version "<value>"

Use a valid semantic version, for example 0.1.0.
Provide it with --package-version.
```

```text
error: --all cannot be used with a module name

Choose one target or use --all.
```

```text
error: --quiet, --verbose, and --json cannot be combined

Choose one output mode.
```

`--plain` and `--no-color` may be combined with human output modes.

### 20.2 Inference and package manager

```text
error: could not derive a valid JavaScript name from "<package>"

Provide one with --javascript-name.
```

```text
error: could not derive a valid Android namespace from "<package>"

Provide one with --android-namespace.
```

```text
error: package manager is ambiguous

Both package-lock.json and yarn.lock were found.
Provide --package-manager npm or --package-manager yarn.
```

```text
error: <manager> is not available

Install <manager> or choose the other supported package manager with
--package-manager.
```

### 20.3 Project and ownership

```text
error: not a Supernote plugin: <current-directory>

Expected PluginConfig.json, package.json, and android/.
Run the command from the plugin root.
```

```text
error: package.json could not be read

<path>: <concise parser error>
Fix the file and rerun the command.
```

```text
error: module "<module>" was not found

Run `supernote-module validate --all` to list and check managed modules.
```

```text
error: "<path>" exists but is not managed by Supernote Module Generator

Move it, choose another package name, or remove it manually after reviewing
its contents.
```

```text
error: module "<module>" already exists

Use `supernote-module update <module>` to refresh it.
```

```text
error: JavaScript name "<name>" is already used by "<module>"

Choose another value with --javascript-name.
```

```text
error: Android namespace "<namespace>" is already used by "<module>"

Choose another value with --android-namespace.
```

```text
error: dependency "<package>" already points to a different location

Review <path> and remove or rename the conflicting dependency.
```

```text
error: module metadata for "<module>" is invalid

<specific field or parse problem>
Restore the metadata or recreate the module before updating or removing it.
```

### 20.4 Validation

```text
× Checking module failed

  Module "local-math" is missing 2 generated files.

  Missing:
    android/build.gradle
    src/index.ts

  Rollback: Not needed
  Next:     Run supernote-module update local-math
```

```text
× Checking module failed

  Module "local-math" contains an unresolved template value.

  android/src/main/java/.../LocalMathModule.kt:12: {{JAVASCRIPT_NAME}}

  Rollback: Not needed
  Next:     Run supernote-module update local-math
```

```text
× Checking integration failed

  package.json does not link "local-math" to its generated module path.

  Rollback: Not needed
  Next:     Run supernote-module update local-math
```

```text
× Building Android failed

  Gradle could not build module "local-math".

  > Task :local-math:compileDebugKotlin FAILED
  <most relevant compiler line>

  Rollback: Not needed
  Next:     Fix the reported source error, then rerun with --build.
```

For multi-module validation:

```text
× Validation failed for 2 of 5 modules

  local-math     Missing generated file: src/index.ts
  stylus-jsi     Android build failed

  Rollback: Not needed
  Next:     Run supernote-module validate stylus-jsi --build --verbose
```

### 20.5 Dependency and postcondition failures

```text
× Installing dependency failed

  npm could not install "local-math".

  npm error ERESOLVE unable to resolve dependency tree

  Rollback: Completed
  Next:     Resolve the npm dependency conflict, then rerun Add.
```

```text
× Verifying module failed

  npm completed, but "local-math" is not linked from node_modules.

  Rollback: Completed
  Next:     Rerun with --verbose to inspect npm output.
```

```text
× Updating plugin failed

  Android settings could not be updated safely.

  <path>:<line>: <specific structural problem>

  Rollback: Completed
  Next:     Fix the settings file, then rerun Update.
```

### 20.6 Filesystem and transaction failures

```text
× Preparing module failed

  The destination is not writable:
  <path>

  Rollback: Not needed
  Next:     Correct the directory permissions and rerun Add.
```

```text
× Generating module failed

  The filesystem is out of space.

  Rollback: Completed
  Next:     Free disk space and rerun Add.
```

```text
× Rollback failed

  Update failed and the previous state could not be fully restored.

  Restored:
    <path>
  Not restored:
    <path>

  Rollback: Partial
  Next:     Run <exact recovery command>
```

### 20.7 Doctor failures

```text
× Doctor found 2 required issues

  JavaScript  Yarn was selected but is not available.
  Native      Android NDK <required-version> was not found.

  Next: Install the missing tools, then rerun `supernote-module doctor`.
```

Doctor reports the path and detected version beneath a failed check when that
information explains the mismatch.

### 20.8 Internal failures

```text
× Internal error

  Supernote Module Generator could not complete <phase>.

  Rollback: <status>
  Next:     Rerun with --debug and report the resulting traceback.
```

An internal error exits `1`, uses JSON `error.kind = "internal"`, and never
prints a traceback by default.

## 21. Complete help screens

Help is rendered without color or cursor control so it is safe to redirect.
Options appear in the order shown. These fenced screens must match the runtime
help strings byte for byte; automated conformance tests enforce that contract.

### 21.1 Root

```text
Supernote Module Generator

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
```

### 21.2 Add

```text
Supernote Module Generator

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
  supernote-module add @acme/stylus-jsi --type jsi \
    --javascript-name StylusJsi \
    --android-namespace com.acme.stylus_jsi \
    --package-manager yarn --yes

Exit:
  0 success or user cancellation
  1 operation or verification failure
  2 usage or input error
  3 partial completion requiring recovery
  130 interrupted
```

### 21.3 Update

```text
Supernote Module Generator

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
```

### 21.4 Validate

```text
Supernote Module Generator

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
```

### 21.5 Remove

```text
Supernote Module Generator

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
```

### 21.6 Doctor

```text
Supernote Module Generator

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
```

### 21.7 Help command

```text
Supernote Module Generator

Show the command overview or help for one command.

Usage:
  supernote-module help [COMMAND]

Arguments:
  COMMAND  add, update, validate, remove, or doctor.

Examples:
  supernote-module help
  supernote-module help add
```

`supernote-module <command> --help` must be byte-for-byte identical, excluding
the process invocation path, to `supernote-module help <command>`.

## 22. Complete keyboard interaction specification

### 22.1 Selection controls

| Key | Selection list behavior |
|---|---|
| Up | Move to the previous visible enabled item; stop at the first. |
| Down | Move to the next visible enabled item; stop at the last. |
| Enter | Select the active item. Does nothing when no item is active. |
| Esc | Go to the previous question. |
| Ctrl+C | Cancel the whole operation. |

Menus do not wrap. Disabled items, if ever introduced, cannot receive focus.
There are no number shortcuts, Vim bindings, Page Up/Down behavior, or
single-letter action keys in the cursor UI.

### 22.2 Printable menu input

Cursor menus ignore printable characters, including `/`. They never open a
filter or alter the active selection. Text entry begins only after the user has
entered a text field.

### 22.3 Text controls

Left/Right move by Unicode scalar value; Home/End move to boundaries;
Backspace/Delete edit; Ctrl+U clears the field; Enter submits; Esc goes back;
Ctrl+C cancels. Paste is treated as text and control characters other than tab
and newline are rejected. A multi-line paste is rejected as one input and the
entire pasted sequence is discarded. It displays:

```text
This field accepts one line. Paste a single value.
```

The current field remains active. No pasted line is accepted as the value or
allowed to answer a later question.

### 22.4 History and Back

Wizard state is an ordered list of decisions. Going back:

1. removes the active question;
2. restores the preceding question with its previous answer;
3. invalidates every derived answer downstream of a changed dependency;
4. retains explicit downstream answers only when they remain valid;
5. never performs filesystem work.

On the first question, Esc returns to the main menu if that menu launched the
workflow. For a direct command it prints `<Operation> cancelled.` and exits `0`.
Esc from the main menu exits `0`.

### 22.5 Terminal resize

Resize is handled without losing the selected item, text buffer, or
wizard history. The next render applies the responsive rules. A resize never
submits or cancels input.

## 23. Accessibility specification

### 23.1 Required modes

- Default cursor UI: capable interactive terminals only.
- ASCII fallback: automatic when Unicode rendering is unsafe.
- `--no-color` and `NO_COLOR`: disable all ANSI color while retaining cursor
  interaction when capable.
- `--plain`: ASCII, line-oriented input, no cursor positioning, no animation,
  no terminal title, no alternate screen, no hidden cursor, and no timing-based
  content.
- `--json`: machine-only output with the same no-ANSI guarantees as plain mode.

`NO_COLOR` is respected when its environment variable is present regardless of
value. An explicit future color-enabling flag must not override it; none exists
in this release.

### 23.2 Meaning without color

Every semantic state has a word, symbol, or label:

```text
Success: ✓ / [OK]
Warning: ! / [!]
Failure: × / [X]
Active:  › / >
```

Color never distinguishes selectable from disabled, required from optional, or
pass from fail by itself. Prompts include `(optional)` or an explicit default.

### 23.3 Screen readers

`--plain` is the supported screen-reader mode and is documented in root help.
It does not rewrite prior output. Every prompt is emitted once, and every answer
is echoed once. Spinner frames and elapsed-time updates are absent. Numbered
choices use stable, visible numbers only in this mode.

### 23.4 Narrow terminals and wrapping

- At 72 columns or wider, show aligned menu descriptions and summaries.
- From 48–71, omit menu descriptions and place values on a following indented
  line when needed.
- Below 48, use one item per line, no alignment padding, and wrap at word
  boundaries.
- Never truncate package names, paths, errors, recovery commands, or confirmation
  tokens.
- Long paths wrap with a hanging four-space indent; they are not ellipsized.
- Lists taller than the terminal use a viewport and `↑ more` / `↓ more` labels.

### 23.5 Terminal capability and cleanup

Capability detection considers TTY status, `TERM=dumb`, encoding, cursor-control
support, and terminal dimensions. If uncertain, choose plain behavior.

On every exit path the application restores cooked input mode, cursor
visibility, line wrapping, and prior signal handlers. Tests must cover normal
exit, validation error, subprocess failure, first and second interrupt, and a
closed output pipe.

## 24. Beginner and expert experience

The design uses one interface rather than beginner and expert modes.

For a first-time user:

- visible type labels answer both “what am I building?” and “what code will I
  write?”;
- field order follows the user's mental model from module identity through
  editable suggestions to installation;
- safe defaults are visible before acceptance;
- inline validation prevents late surprises;
- one sentence of guidance appears only where the label cannot carry the full
  meaning;
- errors always provide a next action.

For a daily user:

- supplied flags skip questions;
- deterministic defaults reduce the Add wizard to four short decisions;
- Add does not stop for a redundant review;
- Update's default-Yes confirmation follows an intentional command choice;
- commands exit after one result;
- quiet and JSON modes avoid parsing or visual noise;
- no onboarding state, contextual tutorials, or recurring tips appear.

The deliberate cost is that novices must use the user guide for conceptual
comparisons and implementation education. Keeping that material out of every
run protects long-term fluency and makes the CLI's behavior identical across
machines.

## 25. Major design decisions and tradeoffs

| Decision | Rationale | Accepted tradeoff |
|---|---|---|
| Linear wizard | Preserves orientation and makes Back predictable. | Less simultaneous editing than a full-screen form. |
| Append-only history | Makes prior choices and derivations visible. | Uses more vertical space. |
| Add executes immediately | Creation is additive and rollback-protected; review would tax every valid run. | Users do not receive a final consolidated edit screen. |
| Update defaults Yes | The user deliberately selected Update and sees an exact plan. | Enter can authorize mutation, mitigated by rollback. |
| Typed Remove | Source deletion deserves deliberate target recall. | Slower than a yes/no prompt. |
| Strict Doctor All | The selected scope has stable meaning independent of project contents. | Default Doctor can fail on tools the current plugin does not use. |
| Structural validation by default | Fast enough for routine use; build is opt-in. | A plain Validate does not prove compilation. |
| No first-run mode | Reproducible UX across machines and reinstalls. | No guided onboarding. |
| No contextual help | Keeps every run concise and prevents dual documentation. | Users leave the wizard for deeper explanations. |
| No config layer | Prevents invisible, machine-specific behavior. | Repeated non-default preferences require flags. |
| npm default without lockfiles | Completes the common workflow with a stable default. | Yarn users without a lockfile must select or pass Yarn. |
| No choice with conflicting locks | Project evidence is contradictory. | Adds one required decision. |
| Phase progress, not percentages | Phases are truthful; package-manager percentages are not comparable. | Completion time cannot be predicted precisely. |
| No default log files | Avoids hidden artifacts and retention questions. | Users must rerun with `--verbose` for full diagnostics. |
| Separate `--debug` | Keeps user errors concise while preserving engineering evidence. | One additional global option. |
| `--plain` separate from no-color | Accessibility requires changing interaction, not merely color. | Two related output controls must be documented. |
| One-module Update | Makes plans, confirmation, and rollback understandable. | Bulk refresh requires repeated commands. |
| Bulk Validate and Remove | Validation benefits from coverage; full cleanup is a legitimate explicit operation. | Remove All requires a special confirmation path. |
| Public types only native/jni/jsi | Starts the first release with one stable vocabulary. | No compatibility with experimental spellings. |
| External dependency state is reconcilable, not atomic | npm and Yarn cannot be rolled back like files. | Some failures correctly end as partial and require one recovery command. |

Engineering correctness and UX polish conflict most visibly in transaction
reporting. It would be visually cleaner to say only “Update failed,” but that
would hide whether user source is safe. The design chooses a slightly longer
failure summary whenever rollback occurred. Conversely, raw internal detail is
not correctness: complete output remains available through `--verbose` and
`--debug` without burdening ordinary failures.

## 26. Implementation contract

This section converts the interaction design into testable application rules.

### 26.1 Command lifecycle

Every command uses the same state machine:

```text
parse
  → resolve root
  → recover prior transaction
  → collect decisions
  → validate decisions
  → preflight
  → show plan/confirm when required
  → stage
  → apply
  → run external work
  → verify postconditions
  → commit
  → report
```

Any state before `stage` can exit without rollback. Any state from `stage`
through `verify postconditions` must be cancellable and rollback-aware.
`report` is generated from a result object, never directly from caught
exceptions or scattered print calls.

### 26.2 Result model

Every command returns one internal result with:

```text
command
status
exit_code
module target or target set
changes
dependency result
validation result
rollback result
warnings
recovery action
timings
error classification
```

Human, quiet, and JSON output are renderers over this same object. This prevents
machine output and human wording from disagreeing about success.

### 26.3 Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success, help/version, empty-state no-op, or deliberate user cancellation. |
| 1 | Operation, validation, build, Doctor requirement, or internal failure. |
| 2 | Command usage, supplied input, or non-interactive decision error. |
| 3 | Mutation partially completed or recovery still required. |
| 130 | `Ctrl+C` with no mutation or successful rollback. |

An internal exception uses exit `1`, not a separate undocumented code. A second
interrupt can be terminated by the shell, but startup recovery must recognize
the journal on the next run.

Interactive EOF before mutation is treated as a deliberate cancellation,
prints `<Operation> cancelled.`, and exits `0`. EOF after mutation follows the
same recovery path as `Ctrl+C`, but exits `3` if recovery is incomplete.

### 26.4 Argument normalization

- Reject different values supplied more than once for the same option:

  ```text
  error: --type was provided more than once with conflicting values

  Keep only one --type value.
  ```

- Identical repetitions are accepted and normalized to one value.
- Options that do not affect an operation are rejected rather than ignored.
  Representation flags explicitly documented as harmless JSON no-ops are the
  only exception.
- `--skip-install` prevents package-manager resolution entirely unless the
  manager value is needed only to print a user-supplied recovery command; in
  that case project evidence may be used, but ambiguity does not block the
  operation.
- `--yes` never relaxes validation, ownership, path containment, collision, or
  postcondition checks.
- The public parser has no aliases for type values or removed experimental
  command forms.

### 26.5 Validation constraints

Validate before filesystem work:

- npm/Yarn package grammar and a maximum of 214 ASCII characters;
- each generated filename component at most 120 characters;
- generated relative paths at most 180 characters;
- target-platform and host absolute-path limits using the resolved plugin root;
- JavaScript identifier grammar and reserved words;
- Java/Kotlin namespace-segment grammar and reserved words;
- semantic version grammar;
- no control characters, bidi controls, path separators, traversal segments,
  shell expansion, or invisible whitespace in identifiers;
- canonical target containment after symlink resolution;
- uniqueness of package, JavaScript, namespace, native library, and registration
  names across managed modules and parent integration.

User-facing errors state the violated limit or grammar and show one valid
example. Raw `errno` text may appear as a supporting detail, never as the
headline.

### 26.6 Generated-output requirements

Before success:

- no unresolved template marker may remain in any generated text file;
- generated module metadata must round-trip through its parser;
- all expected files for the selected type must exist;
- parent dependency and Gradle entries must occur exactly once;
- dependency resolution must point to the generated module and match its package
  name;
- generated Gradle subprojects must inherit repositories from parent settings
  and must not add project-level repositories that conflict with strict
  centralized repository policies;
- build mode must use the project's supported Gradle fixture/policy rather than
  weakening repository rules to obtain a pass.

These are postconditions, not merely test recommendations.

### 26.7 Doctor probes

Doctor executes and checks zero exit status for:

```text
node --version
npm --version                       # when npm applies
yarn --version                      # when Yarn applies
<plugin>/android/gradlew --version
cmake --version                     # JNI/JSI
<ndk tool or metadata probe>        # JNI/JSI
adb version                         # advisory deployment tool
adb devices                         # advisory device state
```

It parses and reports detected versions when a project declares a requirement.
A path existing on disk is not a passing check. The Gradle wrapper must be
executable or invocable through the platform-appropriate shell. Fake tools that
return nonzero fail their check.

Package-manager checks follow project evidence: one lockfile makes its manager
required; no lockfile makes npm required because npm is the documented default.
With both lockfiles, Doctor reports the ambiguity as an advisory, probes both
managers, and passes the package-manager requirement when at least one is
healthy. Lifecycle commands still require an explicit manager in that state.

### 26.8 Invalid-root startup

The main lifecycle menu is not shown when no plugin root can be resolved.
Instead:

```text
Supernote Module Generator

Not a Supernote plugin: /work/other

Expected:
  PluginConfig.json
  package.json
  android/

› Doctor   Check installed tools.
  Help     Show commands and usage.
  Exit
```

Lifecycle commands invoked directly use:

```text
error: not a Supernote plugin: /work/other

Expected PluginConfig.json, package.json, and android/.
Run the command from the plugin root.
```

Doctor remains available and marks the project check failed while continuing
tool checks. Help remains available. The invalid-root screen does not silently
switch its product description or show unusable lifecycle actions.

### 26.9 Version

```text
$ supernote-module --version
supernote-module <semantic-version>
```

This works without a command or valid plugin root, writes to stdout, and exits
`0`. The resolved version also appears in `--debug` diagnostics and JSON under
`tool_version`; it is not repeated in normal root help.

### 26.10 Git evidence

Before Update and Remove, perform a read-only Git status check with a short
timeout:

```text
Git  clean
Git  3 uncommitted changes
Git  not a Git repository
Git  status unavailable
```

Only the dirty state is yellow. Git status never blocks, never changes
confirmation strength, and never appears as an error. Add does not perform this
check because it does not replace or delete existing implementation source.

### 26.11 Output and stream conformance

- Human progress: stderr.
- Human final success: stdout.
- Human warnings and failures: stderr.
- Help and version: stdout.
- JSON document: stdout only; stderr remains empty unless the process cannot
  construct a JSON result at all.
- Broken pipes exit without a traceback after restoring terminal state.
- Redirected output never contains ANSI or spinner frames.
- No command writes a diagnostic log unless a future explicit log option is
  designed; none exists in this release.

## 27. Audit constraint traceability

Every confirmed finding from the source audit is resolved by this specification:

| Finding | Design resolution |
|---|---|
| F1 invalid-directory menu | Root resolution precedes lifecycle UI; invalid-root screen exposes only Doctor, Help, Exit and exact markers. |
| F2 missing version | Global `--version` works without command/root; help documents it. |
| F3 hidden defaults/recovery | Command help documents defaults/inference; skipped installs show one exact next command. |
| F4 manager requested when skipped | `--skip-install` bypasses manager resolution and ambiguity. |
| F5 repeated option conflict | Conflicting repetitions are usage errors; identical repetitions normalize. |
| F6 automation output | Quiet, verbose, JSON, no-color, plain, and debug contracts are global. |
| F7 late discarded validation | Field validation and full preflight precede progress/mutation; Back retains valid answers. |
| F8 navigation/data collision | Raw UI uses Esc/Ctrl+C; free text reserves no ordinary values. |
| F9 Back skips too far | Esc traverses one wizard decision and returns to menu only from the first. |
| F10 invalid derivation | Stable punctuation-aware rules; failure names exact override flag. |
| F11 missing length validation | Explicit package, component, relative-path, and host-path budgets are preflight checks. |
| F12 weak name guidance | Error copy states grammar, limit where applicable, example, and override. |
| F13 interrupt tracebacks | Phase-aware cancellation, terminal cleanup, rollback, and documented EOF handling. |
| F14 raw filesystem errors | Categorized failure, affected path, rollback state, and next action. |
| F15 interrupted Remove corruption | Source is retained until integration verifies; journaled rollback and startup recovery. |
| F16 interrupted Add ambiguity | Add is atomic; successful rollback is stated; incomplete recovery exits `3`. |
| F17 normal rollback strength | Existing snapshot principle becomes the common transaction contract and gains postcondition verification. |
| F18 impossible unowned advice | Owned module points to Update; unowned collision points to move/rename/manual review. |
| F19 trust-heavy Update | Exact Replace/Preserve/Parent/Git plan appears before confirmation. |
| F20 ignored Git state | Read-only dirty-state evidence appears on Update and Remove without weakening confirmation. |
| F21 Doctor false positives | Doctor executes tools, checks status, permissions, and versions. |
| F22 false install success | Dependency realpath/package postcondition is required before success. |
| F23 contradictory adb icon | Tool presence and connected-device advisory are separate states. |
| F24 terminal capability ignored | NO_COLOR, no-color, plain, ASCII fallback, and capability gates are defined. |
| F25 small terminal overflow | Width rules, height-aware viewport, and compact descriptions are specified. |
| F26 broken help wrapping | Static custom help uses word-boundary wrapping and compact primary descriptions. |
| F27 cancellation as error | Cancellation has neutral one-line results and a distinct success-control-flow result. |
| F28 repeated confirmation text | Exact quoted typed prompt has no misleading bracket default. |
| F29 unresolved generated token | Generated-text token scan is a mandatory success postcondition. |
| F30 strict repository policy | Generated subprojects inherit parent repositories; verification may not weaken strict policy. |
| F31 unchecked deploy command | Success never advertises deploy; it shows only a required, verified next action. |
| F32 silent long work | Phase starts, spinner, elapsed time, plain events, verbose streaming; no default logs by explicit product decision. |

F32 is the one place where the audit's proposed mechanism is intentionally not
adopted. The confirmed problem is silence; phase events and elapsed time solve
it. Automatic log retention would conflict with the chosen privacy and
artifact-minimization policy, so complete output is available on an explicit
`--verbose` rerun instead.

## 28. Delivery checklist

The specification covers the requested deliverables as follows:

| Requested deliverable | Section |
|---|---:|
| 1. UX philosophy document | 1 |
| 2. Design principles | 2 |
| 3. Information architecture | 3 |
| 4. Navigation model | 5, 22 |
| 5. Canonical terminology | 4 |
| 6. Visual language guide | 6 |
| 7. Prompt writing guide | 7 |
| 8. Default behavior guide | 8, 9 |
| 9. Error handling guide | 13, 20 |
| 10. Progress indicator guide | 14 |
| 11. Complete rewritten workflow | 16 |
| 12. Every screen rewritten | 17 |
| 13. Every prompt rewritten | 18 |
| 14. Every success message rewritten | 19 |
| 15. Every error rewritten | 13, 20 |
| 16. Every confirmation rewritten | 18 and workflow confirmations in 16 |
| 17. Every help screen rewritten | 21 |
| 18. Complete keyboard interaction specification | 22 |
| 19. Accessibility specification | 23 |
| 20. Beginner and expert experience analysis | 24 |
| 21. Rationale for every major design decision | 25 |
| 22. Implementation-ready final UX specification | 10–12, 15, 26 |

No subjective design decision remains open. New behavior not covered here
requires a product-design amendment rather than an implementation guess.
