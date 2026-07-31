# Automation and command reference

Scripts and CI should invoke a subcommand explicitly. The generator never
prompts when stdin or stdout is not a TTY.

For the exact public grammar and defaults at the installed version, run:

```bash
supernote-module help <command>
```

## Global options

| Option | Effect |
| --- | --- |
| `-h`, `--help` | Show help. |
| `-V`, `--version` | Show the version. |
| `--quiet` | Show errors and one final result line. |
| `--verbose` | Show complete subprocess output and diagnostics. |
| `--json` | Emit one versioned JSON result. |
| `--no-color` | Disable color. |
| `--plain` | Use deterministic line-oriented ASCII output. |
| `--debug` | Include internal diagnostics and tracebacks when applicable. |

## Add

```text
supernote-module add [PACKAGE] [options]
```

| Option | Effect |
| --- | --- |
| `--type <native|jni|jsi>` | Select the module type. |
| `--description <TEXT>` | Set the package description; `""` omits it. |
| `--javascript-name <NAME>` | Set the JavaScript/JSI object name. |
| `--android-namespace <NAME>` | Set the Java-style Android namespace. |
| `--package-version <VERSION>` | Set the initial semantic version; default `0.1.0`. |
| `--package-manager <npm|yarn>` | Select local dependency installation. |
| `--skip-install` | Generate and wire without running npm/Yarn. |
| `--build` | Run an Android build after verification. |
| `-y`, `--yes` | Accept documented safe defaults. |

In non-interactive use, PACKAGE is required. Without `--yes`, every
output-affecting choice must be supplied. With `--yes`, the type defaults to
Native, description is omitted, names are derived when valid, version is
`0.1.0`, and dependency installation is enabled. Conflicting lockfiles still
require `--package-manager`.

## Update

```text
supernote-module update [MODULE] [options]
```

Options: `--package-manager <npm|yarn>`, `--skip-install`, `--build`, and
`-y`/`--yes`. Type conversion is unsupported, so Update has no `--type` option.

Update refreshes replaceable generator infrastructure while preserving the
module type and user-owned implementation source.

## Validate

```text
supernote-module validate [MODULE] [--all] [--build]
```

Use either a module or `--all`. Validation is structural unless `--build` is
present.

## Remove

```text
supernote-module remove [MODULE] [options]
```

Options: `--all`, `--package-manager <npm|yarn>`, `--skip-install`, and
`-y`/`--yes`. Outside an interactive terminal, `--yes` is required and is valid
only with an explicit module or `--all`.

Removal deletes the complete module directory, including user source.

## Doctor

```text
supernote-module doctor [--type <all|native|jni|jsi>]
```

Doctor defaults to strict `all` scope. Missing build requirements fail Doctor;
ADB, device, and SELinux-policy checks are advisories.

## Help

```text
supernote-module help [COMMAND]
supernote-module <command> --help
```

## Output contracts

- Normal human output may use Unicode, color, cursor interaction, and elapsed
  durations for long operations.
- `--plain` is ASCII-only and contains no ANSI, animation, cursor control, or
  elapsed-time text.
- `--quiet` retains errors, warnings, and one final result line.
- `--json` emits exactly one schema-versioned document on stdout, never prompts,
  and includes `duration_ms` where duration data applies.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success or user cancellation. |
| `1` | Operation or verification failure. |
| `2` | Usage or input error. |
| `3` | Partial completion requiring recovery. |
| `130` | Interrupted. |
