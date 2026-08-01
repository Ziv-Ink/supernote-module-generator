# Supernote Module Generator

Add Kotlin/Java, C/C++ through JNI, or experimental synchronous JSI code to an
existing Supernote React Native plugin. The generator creates the local package,
React Native bridge, Android build wiring, TypeScript declarations, and
autolinking; you write the function that your plugin needs.

## What this adds to your plugin

After generation, plugin code can use a normal local import:

```typescript
import Math from 'local-math';

const answer = await Math.add(20, 22);
```

The package lives inside the plugin under `local_modules/`; it is not a separate
Supernote plugin.

## Choose the module type

| CLI type | Write code in | Call from JavaScript | Choose it when |
| --- | --- | --- | --- |
| **Native Module** (`native`) | Kotlin or Java | Promise for returned values | You need Android APIs or already work in Kotlin/Java. |
| **Native JNI Module** (`jni`) | C or C++ | Promise for returned values | You have C/C++ code or work that may block and can be asynchronous. |
| **JSI Module** (`jsi`) | C or C++ | Synchronous | You need a short synchronous call and have verified that the target PluginHost can execute the library. |

JSI is not simply the “fast” choice. It runs on the JavaScript thread, and the
current official Supernote architecture does not promise direct C/C++ calls from
JavaScript. Retail firmware may also block the extracted library through
SELinux. Read [Choosing a module](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/guides/choosing-a-module.md)
before selecting JSI.

## Before you start

Run the generator from the exact root of an existing Supernote plugin. That
directory must contain:

```text
PluginConfig.json
package.json
android/
android/settings.gradle       # or settings.gradle.kts
```

The official Supernote template creates `PluginConfig.json` during its first
packaging run. If it is missing, package the unmodified plugin once before using
the generator.

Minimum generator/build inputs:

- Python 3.9 or newer;
- the official Supernote React Native project version (`0.79.2` currently);
- Node.js and the plugin's npm or Yarn package manager;
- Android SDK platform 35 and a Java toolchain compatible with the parent plugin;
- for JNI/JSI, Android NDK Clang with C23/C++23 support, CMake 3.22.1 or newer,
  and an `arm64-v8a` target.

There are important Java, operating-system, device, and JSI qualification notes
in the [compatibility matrix](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/reference/compatibility.md).

## Install the generator

```bash
python3 -m pip install supernote-module-generator
```

Check the current plugin and toolchain without changing files:

```bash
supernote-module doctor --type native
```

## First success: add a Kotlin function

This is the shortest complete path. The
[first-module guide](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/getting-started/first-module.md)
also shows JNI and JSI variants.

### 1. Generate `local-math`

From the plugin root:

```bash
supernote-module
```

Choose **Add module**, then **Native Module**, and use these answers:

| Prompt | Value | What it controls |
| --- | --- | --- |
| Package name | `local-math` | Folder, local dependency, and import string |
| Description | leave empty | Local package metadata only |
| JavaScript name | accept `Math` | Object registered with React Native |
| Android namespace | accept `com.example.math` | Kotlin/Java package and source path |
| Package version | accept `0.1.0` | Local module version, not the plugin version |
| Install dependency | Yes | Runs npm or Yarn so autolinking can find the package |

### 2. Add the exported function

Edit this user-owned file:

```text
local_modules/local-math/android/src/main/java/com/example/math/Example.kt
```

Use the generated annotation import:

```kotlin
package com.example.math

import com.example.math.nativemodule.annotation.SupernoteExport

class Example {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right
}
```

### 3. Call it from the plugin

In `App.tsx` or another plugin TypeScript file:

```typescript
import Math from 'local-math';

async function verifyNativeMath() {
  const total = await Math.add(20, 22);
  console.log('Math.add result:', total);
}
```

Native and JNI value-returning calls require `await`. JSI returns synchronously.

### 4. Package the plugin

From the plugin root on macOS or Linux:

```bash
./buildPlugin.sh
```

On Windows PowerShell:

```powershell
.\buildPlugin.ps1
```

The final package is:

```text
build/outputs/plugin.snplg
```

`plugin.snplg` is the official template's default. If the `name` in
`PluginConfig.json` differs, use the filename actually written to
`build/outputs/`.

### 5. Copy, install, and verify

Copy the package with Android Debug Bridge (ADB):

```bash
adb push "build/outputs/plugin.snplg" /storage/emulated/0/MyStyle/
```

This command copies the file; it does not install it. On the Supernote, open
**Settings > Apps > Plugins > Add Plugin**, select the package, and install it.
Trigger the code path that calls `verifyNativeMath`, then inspect JavaScript logs:

```bash
adb logcat -d -s ReactNativeJS:V '*:S'
```

Look for `Math.add result: 42`.

## Generated files and Update safety

Your implementation is user-owned:

- Native: Kotlin/Java under the module's `android/src/main/java/` tree, except
  generated packages;
- JNI/JSI: the complete module `android/src/main/cpp/` tree;
- application calls in the parent plugin.

**Update replaces the generated README, metadata, package wrapper, declarations,
Gradle/CMake wiring, loaders, registration, and autolinking files. It has no
dry-run or file-by-file diff. Commit first.**

**Remove deletes the complete module directory, including user-owned source.**
Read [Managing modules safely](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/guides/managing-modules.md)
before Update or Remove.

## Next steps

- [First module: Native, JNI, and JSI](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/getting-started/first-module.md)
- [Choose a module](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/guides/choosing-a-module.md)
- [Exports, types, paths, and ownership](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/reference/exports.md)
- [Commands, names, and automation](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/reference/cli.md)
- [Compatibility matrix](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/reference/compatibility.md)
- [Troubleshooting by symptom](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/troubleshooting/README.md)

For the grammar and defaults of the installed version, use:

```bash
supernote-module help add
```

Contributor and release documentation is intentionally separate from the
plugin-developer guides. See
[CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md)
only when changing the generator itself.

## Support status

Generation support, Android compilation, PluginHost loading, and execution on a
particular firmware are different claims. This repository's Python suite does
not currently perform a complete official-template build, produce a `.snplg`,
or run it on a retail device. The compatibility matrix records those boundaries
instead of treating generated files as runtime proof.

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
