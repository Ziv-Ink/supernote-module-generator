# Supernote Module Generator

Create Kotlin/Java, JNI C/C++, and synchronous JSI modules inside an existing
Supernote React Native plugin. You write the implementation; the generator owns
the bridge, native registration, build wiring, TypeScript declarations, and
React Native autolinking.

## Choose a module type

| Module type | Best suited to | JavaScript call model |
| --- | --- | --- |
| **Native Module** | For coding in Kotlin/Java and/or using Android APIs. | Value returns are asynchronous promises. |
| **Native JNI Module** | For combining Android APIs with existing or performance-intensive C/C++ code. | Value returns are asynchronous promises. |
| **JSI Module** | For low-latency synchronous calls from JavaScript. | Calls execute synchronously on the JavaScript thread. |

Not sure which one fits? Read [Choosing a module](docs/choosing-a-module.md).

## Quick start

Install the generator:

```bash
python3 -m pip install supernote-module-generator
```

Then run it from your **Supernote plugin root**:

```bash
supernote-module
```

Then:

1. Choose **Add module**.
2. Choose a module type.
3. Enter the local package name, such as `local-math`.
4. Accept the grey suggestions with Enter, or type replacements.
5. Open the generated `local_modules/<package-name>/README.md`, edit the
   user-owned example, and build/deploy the plugin:

```bash
bash deploy_plugin.sh
```

Add executes after the final valid answer. It normally installs the new local
npm/Yarn dependency; that links the package but does not compile or deploy the
plugin.

For a complete first export and call example, read
[Writing modules](docs/writing-modules.md).

## Requirements

- Python 3.9 or newer.
- An existing Supernote plugin containing `PluginConfig.json`, `package.json`,
  and an `android` directory.
- JDK 17 and Android SDK 35. Generated packages use `minSdk 27`.
- Node.js plus npm or Yarn for local dependency installation.
- Android NDK Clang and CMake 3.22.1 or newer for JNI and JSI modules.
- An `arm64-v8a` Supernote target.

Use **Doctor** to inspect the environment without changing the project:

```bash
supernote-module doctor
```

Doctor checks all module types by default. Use `--type native`, `--type jni`, or
`--type jsi` to check one module type.

## Install from source

To install a source checkout into a virtual environment:

```bash
python3 -m pip install ./supernote_module_generator
supernote-module
```

Direct execution from the source checkout still requires no installation.

## Interactive controls

The main menu provides **Add module**, **Update module**, **Validate module**,
**Remove module**, **Doctor**, **Help**, and **Exit**.

- Up and Down move through a cursor menu, Enter selects, and Esc goes back.
- Suggested names and versions appear grey in the input position after the
  prompt colon, matching `Name: suggestion`. Typing hides the suggestion;
  pressing Enter accepts it.
- Ordinary `q`, `quit`, `b`, and `back` text is treated as data, not navigation.
- Every single-line prompt rejects a multi-line paste as one invalid input. No
  pasted line can answer a later prompt.

`--plain` uses numbered, line-oriented ASCII prompts. Its textual controls are
`:back` and `:cancel`. Because plain output has no color, suggestions use an
explicit bracketed default such as `Name [suggestion]:`.

## Safety and ownership

Implementation files are user-owned:

- Native Module: Kotlin/Java beneath the generated Android source package.
- JNI and JSI Modules: the complete generated `android/src/main/cpp/` tree.

Bridge code, metadata, CMake, loaders, declarations, and autolinking files are
generator-owned and may be replaced by **Update module**. The generated module
README identifies every user-owned and replaceable path.

**Remove module deletes the complete module directory, including implementation
source. Commit or back it up first.** Removal requires the exact package name;
removing all modules requires `REMOVE ALL`.

Validation checks structure and integration. It compiles only when `--build` is
requested.

## Output modes

- Normal human output may use cursor interaction, Unicode, color, and elapsed
  durations for long operations.
- `--plain` is deterministic ASCII with no cursor control, animation, ANSI
  sequences, or elapsed-time text.
- `--quiet` keeps warnings, errors, and one final result line.
- `--json` emits one versioned JSON document, never prompts, and retains
  `duration_ms`.
- `--no-color` and `NO_COLOR` disable color.
- `--verbose` streams subprocess output; `--debug` adds internal diagnostics
  when applicable.

See [Automation and command reference](docs/automation.md) for every command,
option, exit code, and non-interactive rule.

## Documentation

- [Documentation index](docs/README.md)
- [Choosing a module](docs/choosing-a-module.md)
- [Writing modules](docs/writing-modules.md)
- [Automation and command reference](docs/automation.md)
- [Troubleshooting and recovery](docs/troubleshooting-and-recovery.md)
- [Publishing to PyPI](docs/publishing.md)
- [UX specification](UX_REDESIGN_SPECIFICATION.md) — contributor-facing
  behavioral contract

For exact command grammar and defaults, run:

```bash
supernote-module help <command>
```
