# Supernote Module Generator

Generate Kotlin/Java, JNI C/C++, and synchronous JSI modules inside an existing
Supernote React Native plugin. You write the implementation; the generator owns
the bridge, native registration, build wiring, TypeScript declarations, and
React Native autolinking.

## Module types

| Module type | Best suited to | JavaScript call model |
| --- | --- | --- |
| **Native Module** | For coding in Kotlin/Java and/or using Android APIs. | Value returns are asynchronous promises. |
| **Native JNI Module** | For combining Android APIs with existing or performance-intensive C/C++ code. | Value returns are asynchronous promises. |
| **JSI Module** | For low-latency synchronous calls from JavaScript. | Calls execute synchronously on the JavaScript thread. |

## Install

```bash
python3 -m pip install supernote-module-generator
```

Run the command from an existing Supernote plugin root:

```bash
supernote-module
```

Choose **Add module**, select a module type, enter the local package name, and
accept or edit the suggested JavaScript name, Android namespace, and package
version. The generated module contains a README with its exact source paths,
first export example, JavaScript call, ownership boundaries, and build steps.

To inspect the development environment without changing the project:

```bash
supernote-module doctor
```

For exact command grammar and defaults:

```bash
supernote-module help <command>
```

## Requirements

- Python 3.9 or newer.
- An existing Supernote plugin containing `PluginConfig.json`, `package.json`,
  and an `android` directory.
- JDK 17 and Android SDK 35. Generated packages use `minSdk 27`.
- Node.js plus npm or Yarn for local dependency installation.
- Android NDK Clang and CMake 3.22.1 or newer for JNI and JSI modules.
- An `arm64-v8a` Supernote target.

## Safety

Update preserves user-owned Kotlin/Java or C/C++ implementation source and
replaces generator-owned infrastructure. Remove deletes the complete module,
including implementation source, so commit or back it up first.

Validation is structural unless `--build` is supplied. npm/Yarn links the local
package but does not compile or deploy the Android plugin.

## Output modes

Normal output is designed for interactive terminals. `--plain` provides
deterministic ASCII output without elapsed-time text, `--quiet` produces a
minimal human result, and `--json` emits one schema-versioned JSON document.

This project is licensed under the MIT License.

Source code and issues are available at
[github.com/Ziv-Ink/supernote-module-generator](https://github.com/Ziv-Ink/supernote-module-generator).
