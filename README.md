# Supernote Module Generator

Generate local native-code packages inside an existing Supernote React Native
plugin. You write the Kotlin/Java or C/C++ implementation; the generator owns
the bridge, JNI registration, JSI HostFunctions, loaders, CMake, TypeScript
declarations, and React Native autolinking.

Version 1.0.0 supports three module types:

| Module type | Implementation you write | JavaScript call model | Best suited to |
| --- | --- | --- | --- |
| **Native module (Kotlin/Java)** | Kotlin/Java | Value returns are asynchronous promises | Android APIs and ordinary bridge modules |
| **Native JNI module (Kotlin/Java with C/C++ called via JNI)** | C++ exports with optional C helpers | Value returns are asynchronous promises | Native work that may be long-running or blocking |
| **JSI module (C/C++ that is called directly from JavaScript/TypeScript (not asynchronous))** | C++ exports with optional C helpers | Synchronous | Small, fast computations on the JavaScript thread |

## Requirements

- Python 3.9 or newer.
- An existing Supernote plugin with `PluginConfig.json`, `package.json`, and an
  `android` directory.
- JDK 17 and Android SDK 35 for Android builds. Generated packages have
  `minSdk 27`.
- Node.js plus npm or Yarn for local dependency installation.
- Android NDK Clang and CMake 3.22.1 or newer for JNI and JSI modules.
- An `arm64-v8a` Supernote target.

Use Doctor to inspect these without changing the computer or project:

```bash
python3 supernote_module_generator/supernote_module.py
```

Doctor checks all module types by default, or one explicit scope with
`--type {native,jni,jsi}`. Missing build requirements fail Doctor; ADB, device,
and SELinux-policy checks are advisories.

## Run directly or install locally

Run the tool from the **Supernote plugin root**:

```bash
python3 supernote_module_generator/supernote_module.py
```

Alternatively, install this checkout into a virtual environment:

```bash
python3 -m pip install ./supernote_module_generator
supernote-module
```

This project supports local/path installation. Publishing it to PyPI is outside
its current scope.

## Interactive UI

Running the tool without arguments opens its main menu:

```bash
python3 supernote_module_generator/supernote_module.py
```

Choose **Add module**, **Update module**, **Validate module**, **Remove
module**, **Doctor**, **Help**, or **Exit**. In a capable terminal, Up and Down
move without wrapping, Enter selects, Esc goes back, and `/` starts menu
filtering. Ordinary `q`, `quit`, `b`, and `back` text is data, not navigation.

`--plain` uses numbered, line-oriented ASCII prompts. Its only textual controls
are `:back` and `:cancel`. Every single-line prompt rejects a multi-line paste
as one invalid input and does not allow pasted lines to answer later prompts.

Add runs immediately after its last valid answer. Update shows a change plan
and asks for confirmation. Remove shows the destructive consequences and
requires the exact package name, or `REMOVE ALL`. A successful selected
workflow prints its result and exits.

The same lifecycle is available through strict subcommands. For example:

```bash
python3 supernote_module_generator/supernote_module.py add local-math \
  --type jni \
  --package-manager npm \
  --yes
```

The only public `--type` values are `native`, `jni`, and `jsi`. There are no
legacy aliases.

The basic prompts use:

| Prompt | Example | Purpose |
| --- | --- | --- |
| Local package/import name | `local-math` | Folder, npm/Yarn dependency, and TypeScript import |
| Description | `Local math operations` | Text stored in the generated `package.json` |

Choosing **Customize names and version** exposes:

| Prompt | Example | Purpose |
| --- | --- | --- |
| JavaScript module name | `Math` | Default-import object and native/JSI module identity |
| Android namespace | `com.example.math` | Package for generated and user Kotlin/Java |
| Package version | `0.1.0` | Version stored in the local package metadata |

Package, JavaScript, namespace, and version fields use the deterministic naming
and validation rules documented by `supernote-module help add`.

## Output modes

- Normal human output uses cursor interaction, Unicode, and color only when the
  target streams support them. Long active phases may show elapsed time.
- `--plain` is deterministic ASCII output with no cursor control, animation,
  ANSI sequences, or elapsed-time text.
- `--quiet` suppresses progress and supporting detail while keeping warnings,
  errors, and one final success line.
- `--json` emits exactly one schema-versioned JSON document on stdout. It never
  prompts, and handled outcomes leave stderr empty. JSON retains `duration_ms`.
- `--no-color` and the `NO_COLOR` environment variable disable color.
- `--verbose` streams subprocess output; `--debug` adds internal diagnostics
  only when applicable.

## Export Kotlin/Java

New Kotlin/Java modules use one public annotation name:

```kotlin
import com.example.math.nativemodule.annotation.SupernoteExport

class MathApi {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right

  @SupernoteExport(name = "greet")
  fun makeGreeting(name: String): String = "Hello, $name"
}
```

Java uses the same annotation:

```java
@SupernoteExport
public boolean negate(boolean value) {
  return !value;
}
```

Exports may be spread across multiple Kotlin/Java files. Supported boundary
types are `Boolean`/`boolean`, `Double`/`double`, `String`, and `Unit`/`void`
returns. Classes may have a public constructor taking
`ReactApplicationContext`, a public constructor taking `android.content.Context`,
or a public zero-argument constructor, in that preference order. Context
injection is how an exported class uses Android services, permissions, or a
content resolver; Activity injection and arbitrary subclasses are not supported.

Generated TypeScript declarations describe value-returning methods as promises
and `Unit`/`void` methods as fire-and-forget:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
Math.setEnabled(true);
```

The legacy annotation `ReactNativeExport` remains readable when an existing
module is updated, but new source and documentation use only `SupernoteExport`.

## Export C++

JNI and JSI modules use the same marker in any `.cc`, `.cpp`, or `.cxx` file
beneath `android/src/main/cpp`:

```cpp
// @SupernoteExport
double add(double left, double right) {
  return left + right;
}

// @SupernoteExport(name = "greet")
std::string makeGreeting(std::string name) {
  return "Hello, " + name;
}
```

Tagged declarations must be top-level C++ free-function definitions with
external C++ linkage and named parameters. Boundary types are exactly `bool`,
`double`, and `std::string` by value, plus `void` returns. The restricted export
grammar rejects overloads, namespaces, templates, `static`, `inline`,
`constexpr`, `extern "C"`, macros/attributes, declarations without definitions,
default arguments, variadics, pointers, and references. Export and JavaScript
names must be unique across the complete module.

Only C++ files can export. `.c` files compile as C23 and can provide unrestricted
internal helpers. Put declarations in a header and use ordinary language guards
when calling C from C++:

```c
#ifdef __cplusplus
extern "C" {
#endif
double scale_value(double value);
#ifdef __cplusplus
}
#endif
```

Exact export markers in C files or headers are errors. `std::string` values are
UTF-8 text at the generated JNI/JSI boundary, not arbitrary byte buffers.

Do not implement JNI entry points, HostFunctions, `JNI_OnLoad`, native
registration, loaders, or CMake. Those names and files belong to the generator;
competing user-defined bootstrap symbols are unsupported.

## JNI versus JSI calls

A Native JNI module uses React Native promises for value returns:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
```

Its `void` exports are fire-and-forget and log native failures. JNI is the
appropriate C/C++ backend for file I/O, parsing, compression, networking, or
other potentially blocking work.

A JSI module is synchronous:

```typescript
import Math from 'local-jsi-math';

const total = Math.add(20, 22);
```

JSI exports run directly on React Native's JavaScript thread. Blocking work
freezes that thread, so keep JSI functions short and deterministic.

The verified Supernote JSI host uses React Native 0.79.2. The generator does not
enforce a React Native version. PluginHost must be allowed to execute extracted
plugin `.so` libraries; the generator does not check SELinux policy.

## User-owned and generated files

For Kotlin/Java modules, edit implementation files beneath:

```text
android/src/main/java/<android namespace>/
```

For JNI/JSI modules, edit the entire tree beneath:

```text
android/src/main/cpp/
```

The included `Example.kt`, `math.cpp`, `text.cpp`, `helpers.c`, and `helpers.h`
are live user-owned examples. The starter `add`, `negate`, and `greet` exports
may be changed or removed.

The generator may replace:

- `.supernote-module.json`, `package.json`, `index.js`,
  `react-native.config.js`, and the complete generated module README.
- `android/build.gradle.kts`, manifests, and autolinking markers.
- `android/.native-module/` and `android/.supernote-module/`.
- Generated Kotlin/Java support code beneath
  `android/src/main/java/.../generated/`.
- `android/build/generated/supernote/` and generated `index.d.ts`.
- Its marked registration block in the parent `android/settings.gradle`.

`update` preserves user Kotlin/Java implementation source and the entire C/C++
source tree. Do not hand-edit replaceable infrastructure.

## npm/Yarn linking versus deployment

By default, `add` does two dependency operations:

1. It writes a `file:./local_modules/<name>` dependency to the parent
   `package.json`.
2. It runs `npm install --save file:...` or `yarn add file:... --exact`.

You normally do **not** run `npm install` separately. `--skip-install` leaves
dependency installation pending, which is useful for generation-only tests or
offline work.

npm/Yarn only links the local JavaScript/Android package. It does not compile or
install the Supernote plugin. Build and deploy after native API changes with:

```bash
bash deploy_plugin.sh
```

## Lifecycle actions

| UI action | Behavior |
| --- | --- |
| **Add module** | Creates, wires, and normally installs one local module |
| **Update module** | Selects a module and refreshes replaceable infrastructure without changing its module type or overwriting user source |
| **Validate module** | Selects one or all modules and checks metadata, generated bindings, and parent wiring; it does not compile or device-test |
| **Remove module** | Selects one or all modules, requires typed confirmation, then deletes the complete module directory including user source |
| **Doctor** | Reports all detected tools or enforces one selected module type's build requirements |
| **Help** | Prints the exact command overview and command-specific usage |

The UI shows the canonical module-type labels and implementation language.

Removal is destructive. Commit or back up implementation source first.
The UI requires typing the module name; removing all modules requires typing
`REMOVE ALL`.

### Automation commands

Scripts and CI use `add`, `update`, `validate`, `remove`, and `doctor`. Commands
never prompt when stdin or stdout is not a TTY.

`add [name]` accepts:

- `--type {native,jni,jsi}` selects the module type.
- `--description TEXT` sets the generated package description.
- `--package-version VERSION` sets its local package version; the default is
  `0.1.0`.
- `--android-namespace NAME` sets the generated Kotlin/Java package.
- `--javascript-name NAME` sets the object exposed to JavaScript/TypeScript.
- `--package-manager {npm,yarn}` selects local dependency installation. When
  omitted, one unambiguous parent lockfile selects the manager.
- `--skip-install` creates and wires the package without running npm/Yarn, so
  dependency installation remains pending.
- `--yes` (or `-y`) accepts the generated configuration and runs
  noninteractively.
- `--verbose` streams package-manager output.

`update [MODULE]` accepts `--package-manager`, `--skip-install`, `--build`, and
`--yes`. It has no `--type` option because type conversion is unsupported.

`remove [MODULE]` accepts `--all`, `--package-manager`,
`--skip-install`, `--verbose`, and `--yes`. Outside an interactive terminal,
`--yes` is mandatory; in an interactive terminal, omission of `--yes` enables
the typed confirmation guard.

`validate [MODULE]` accepts `--all` and `--build`. `doctor` accepts
`--type {all,native,jni,jsi}` and defaults to strict `all` scope.

Add requires a package name in non-interactive use. Without `--yes`, every
omitted output-affecting decision is reported together. Update and Remove
require `--yes` non-interactively. For Remove, `--yes` is accepted only with an
explicit module or `--all`.

Run `supernote-module help <command>` or `supernote-module <command> --help`
for the exact public grammar and defaults.

## Transactions and recovery

Add, Update, and Remove stage generated changes, journal every affected path,
verify postconditions, and commit only after verification. Remove first moves
implementation source into transaction-owned recovery storage. If dependency
reconciliation or rollback is incomplete, the command exits `3`, retains the
journal, and prints one exact recovery command. The next invocation attempts
startup recovery before beginning new work.

## Troubleshooting

| Symptom | Interpretation and action |
| --- | --- |
| Package “not available” or “not linked” | Confirm npm/Yarn installed the local dependency, then rebuild and redeploy. The error includes package name, JavaScript module, backend, and logcat tag. |
| New export is missing | Check the exact `SupernoteExport` syntax and supported signature, then rebuild so KSP or native codegen runs. |
| Validate passes but Android fails | Validation is structural only. Choose Doctor for that module type, then build the plugin to obtain compiler diagnostics. |
| JSI object is unavailable | Inspect the module-specific `SupernoteJsi...` tag and `ReactNativeJS` in logcat; bootstrap may still be running or native loading may have failed. |
| JNI call rejects | Inspect the `SupernoteNative...` and `ReactNativeJS` tags; generated wrappers prefix exceptions with module/export context. |
| C/C++ changes seem stale | Rebuild the Android plugin. Gradle inventories native sources and regenerates bindings when export files change. |

The tool deliberately does not claim enforcing-SELinux compatibility without a
separate enforcing-device test.
