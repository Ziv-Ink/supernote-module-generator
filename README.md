# Supernote Module Generator

Add Kotlin/Java, C/C++ through JNI, or synchronous JSI
functionality to an existing Supernote React Native plugin. The generator
creates the local package, bridge, Android build wiring, declarations, and
autolinking; you write the native behavior your plugin needs.

## Why use it

- Keep native code inside the plugin as a local npm or Yarn dependency.
- Generate repeatable React Native and Android integration for three backends.
- Add exports without hand-writing registration, conversion, or autolinking.
- Safely validate and update generated infrastructure while preserving defined
  implementation roots.

## Install

Python 3.9 or newer is required:

```bash
python3 -m pip install supernote-module-generator
```

Run it from an existing Supernote plugin root containing `PluginConfig.json`,
`package.json`, `android/`, and `android/settings.gradle` (or `.kts`):

```bash
supernote-module doctor --type native
supernote-module
```

## Quick example

Choose **Add module**, **Native Module**, package name `local-math`, and accept
the derived JavaScript name `Math` and namespace `com.example.math`.

Edit:

```text
local_modules/local-math/android/src/main/java/com/example/math/Example.kt
```

```kotlin
package com.example.math

import com.example.math.nativemodule.annotation.SupernoteExport

class Example {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right
}
```

Call the default export from the plugin:

```typescript
import Math from 'local-math';

const answer = await Math.add(20, 22);
```

Package with `./buildPlugin.sh` on macOS/Linux or `.\buildPlugin.ps1` in
Windows PowerShell. The official template normally writes
`build/outputs/plugin.snplg`.

The [Getting Started Wiki guide](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Getting-Started)
covers generation, implementation, packaging, device installation, logging,
editable paths, and complete JNI/JSI alternatives.

## Module types

| CLI type | Implementation | JavaScript call | Best fit |
| --- | --- | --- | --- |
| `native` | Kotlin or Java | Promise for returned values | Android APIs and Kotlin/Java libraries |
| `jni` | C or C++ behind JNI | Promise for returned values | Existing C/C++ or work that can be asynchronous |
| `jsi` | C or C++ through JSI | Synchronous | Short JavaScript-thread work on a qualified host |

JSI is a supported generator backend, but generation and compilation do not
prove that the target PluginHost can execute the extracted library. Runtime
availability still depends on the host, firmware, linker, and SELinux policy.
Use the Wiki's
[module decision guide](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Choosing-a-Module)
before selecting a backend.

## Compatibility summary

- Python 3.9+; repository CI covers Python 3.9–3.13.
- Current official Supernote template: React Native 0.79.2, Android SDK 35,
  and an arm64 target.
- JNI/JSI additionally require CMake 3.22.1+ and NDK Clang accepting C23 and
  C++23 for Android API 27.
- Native/JNI follow the officially described Java/TurboModule and
  Java-to-C/C++ paths. JSI runtime support is host-, firmware-, linker-, and
  SELinux-dependent.

The canonical status and evidence are in the Wiki
[compatibility matrix](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Compatibility).

## Documentation

The [GitHub Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki)
is the primary documentation for plugin developers:

- [Getting Started](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Getting-Started)
- [Choosing a Module](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Choosing-a-Module)
- [Exports and JavaScript API](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Exports-and-JavaScript-API)
- [Managing Modules](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Managing-Modules)
- [Troubleshooting](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Troubleshooting)

For exact options in the installed version, run:

```bash
supernote-module help add
```

## Contributing

Changing or releasing the Python generator is a separate workflow. See
[CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md).

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
