# CLI reference

Use the guided interface for first use and explicit subcommands for repeatable
work. The installed command's help is authoritative for its exact grammar:

```bash
supernote-module help add
```

## Names requested by Add

| Prompt/option | Controls | Example and grammar | Unique? | Change later? |
| --- | --- | --- | --- | --- |
| Package name / `PACKAGE` | `local_modules/` path, npm/Yarn dependency, import string, metadata identity | `local-math`; lowercase npm package grammar, ASCII, up to 214 characters | Yes | No Update support; remove/recreate |
| Description / `--description` | Local package metadata only | One line; empty omits it | No | No Update option |
| JavaScript name / `--javascript-name` | React Native object or JSI proxy name and generated declarations | `Math`; non-reserved JS identifier beginning with a letter | Yes | No Update support |
| Android namespace / `--android-namespace` | Kotlin/Java package and generated source path | `com.example.math`; at least two Java/Kotlin-safe segments | Yes | No Update support |
| Package version / `--package-version` | Local module `package.json`, not `PluginConfig.json` | `0.1.0`; semantic version | No | No Update support |
| Plugin name | Parent `.snplg` filename/configuration | Stored in `PluginConfig.json`; not requested by this generator | Plugin-defined | Maintain in parent plugin |

For `local-math`, Add derives `Math` and `com.example.math`. Prefixes
`react-native-` and `local-` and a trailing `-plugin` do not contribute to the
derived names. Separators become PascalCase words for JavaScript and lowercase
underscore words beneath `com.example` for Android.

Derived names are suggestions, not hidden transformations. A package leaf that
becomes a Java/Kotlin keyword—such as `local-native`—needs an explicit safe
namespace.

## Global options

| Option | Effect |
| --- | --- |
| `-h`, `--help` | Show help. |
| `-V`, `--version` | Show the installed version. |
| `--quiet` | Keep warnings/errors and one final result line. |
| `--verbose` | Stream subprocess output and show diagnostics. |
| `--json` | Emit one schema-versioned JSON document and never prompt. |
| `--no-color` | Disable color. `NO_COLOR` is also respected. |
| `--plain` | Use line-oriented ASCII interaction/output. |
| `--debug` | Include internal diagnostics and unexpected tracebacks. |

`--quiet`, `--verbose`, and `--json` are mutually exclusive. `--plain` is the
copyable/screen-reader-friendly human mode and contains no ANSI cursor control,
animation, Unicode-only meaning, or elapsed-time text.

## Add

```text
supernote-module add [PACKAGE] [options]
```

| Option | Effect |
| --- | --- |
| `--type <native|jni|jsi>` | Select the module type. |
| `--description <TEXT>` | Set the local package description; `""` omits it. |
| `--javascript-name <NAME>` | Set the React Native/JSI object name. |
| `--android-namespace <NAME>` | Set the Java-style Android namespace. |
| `--package-version <VERSION>` | Set the semantic version; default `0.1.0`. |
| `--package-manager <npm|yarn>` | Select local dependency installation. |
| `--skip-install` | Generate and wire without npm/Yarn. |
| `--build` | Run `:app:assembleDebug` after verification. |
| `-y`, `--yes` | Accept documented safe defaults. |

Non-interactive Add always requires `PACKAGE`. Without `--yes`, every
output-affecting decision must be explicit. With `--yes`, type defaults to
Native, description is omitted, names are derived when valid, version is
`0.1.0`, and dependency installation is enabled. Conflicting lockfiles still
require an explicit manager.

Example:

```bash
supernote-module add local-math --type native --yes
```

## Update

```text
supernote-module update [MODULE] [options]
```

Options: `--package-manager <npm|yarn>`, `--skip-install`, `--build`, and
`-y`/`--yes`. Update always targets one module and cannot convert its type.

Read [Manage modules safely](../guides/managing-modules.md#update) before using
`--yes`.

## Validate

```text
supernote-module validate [MODULE] [--all] [--build]
```

Use either one module or `--all`. Validation is structural/integration/link
checking unless `--build` is present.

## Remove

```text
supernote-module remove [MODULE] [options]
```

Options: `--all`, `--package-manager <npm|yarn>`, `--skip-install`, and
`-y`/`--yes`. Outside an interactive terminal, `--yes` is required and is valid
only with an explicit module or `--all`.

Removal deletes the whole package, including user source. See
[Remove](../guides/managing-modules.md#remove).

## Doctor

```text
supernote-module doctor [--type <all|native|jni|jsi>]
```

Doctor defaults to strict `all`. Missing build requirements fail Doctor; ADB,
device, and SELinux checks are advisory. Doctor checks availability and a small
set of versions/probes; it is not a complete plugin build or device
qualification.

## Output and exit contracts

| Code | Meaning |
| ---: | --- |
| `0` | Success, help/version, empty state, or deliberate cancellation. |
| `1` | Operation, verification, build, Doctor, or internal failure. |
| `2` | Command usage or supplied-input error. |
| `3` | Partial completion or recovery still required. |
| `130` | Interrupted before mutation or after successful rollback. |

Human progress goes to stderr and final success to stdout. JSON uses stdout only
when a result document can be constructed. Scripts should use an explicit
subcommand and should not depend on normal human wording.
