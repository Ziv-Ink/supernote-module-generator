# Build your first module

This guide starts with an existing Supernote React Native plugin and ends with a
function running on a device. It uses the same `add(left, right)` behavior for
all three module types so the call models are easy to compare.

## 1. Confirm the plugin root

Run every generator command from the directory that contains all four markers:

```text
PluginConfig.json
package.json
android/
android/settings.gradle       # or settings.gradle.kts
```

If a newly scaffolded plugin has no `PluginConfig.json`, package it once with
`./buildPlugin.sh` or `.\buildPlugin.ps1`; the official template creates the file
on its first packaging run.

Check the environment without changing the plugin:

```bash
supernote-module doctor --type native
```

Use `--type jni` or `--type jsi` when checking a C/C++ module.

## 2. Generate the package

Start the guided interface:

```bash
supernote-module
```

Choose **Add module**, then choose a module type. For the Native example, use:

| Prompt | Answer | Result |
| --- | --- | --- |
| Module type | Native Module | Kotlin/Java implementation; Promise returns |
| Package name | `local-math` | `local_modules/local-math/` and import `'local-math'` |
| Description | empty | No package description |
| JavaScript name | accept `Math` | React Native object name |
| Android namespace | accept `com.example.math` | Package and source directory |
| Package version | accept `0.1.0` | Local module version |
| Install dependency now | Yes | npm/Yarn links the local package |

The package name, JavaScript name, and Android namespace must each be unique
among managed modules. Update cannot rename them or convert the module type.

The dependency installation creates the local package link used by React Native
autolinking. It does not compile Android, package a plugin, transfer a file, or
install anything on the device.

## 3. Implement `add`

Choose the subsection that matches the generated module.

### Kotlin/Java module (`native`)

Editable file:

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

Call it with `await`:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
```

### Kotlin/Java + JNI module (`jni`)

Generate a separate example package such as `local-math-jni`, accept the derived
JavaScript name `MathJni` and namespace `com.example.math_jni`, then edit:

```text
local_modules/local-math-jni/android/src/main/cpp/math.cpp
```

```cpp
// @SupernoteExport
double add(double left, double right) {
  return left + right;
}
```

The marker belongs immediately before a top-level C++ definition. Do not write a
JNI entry point or edit generated registration/CMake files.

Call it with `await`:

```typescript
import MathJni from 'local-math-jni';

const total = await MathJni.add(20, 22);
```

### Experimental synchronous JSI module (`jsi`)

Generate `local-math-jsi`, accept `MathJsi` and `com.example.math_jsi`, then edit:

```text
local_modules/local-math-jsi/android/src/main/cpp/math.cpp
```

The C++ export is the same as JNI:

```cpp
// @SupernoteExport
double add(double left, double right) {
  return left + right;
}
```

The call is synchronous and must not use `await`:

```typescript
import MathJsi from 'local-math-jsi';

const total = MathJsi.add(20, 22);
```

Every JSI export runs on the JavaScript thread. Do not use it for file access,
networking, waits, locks, parsing with unpredictable duration, compression, or
long computation. More importantly, generation and compilation do not prove
that the target Supernote PluginHost can execute the extracted `.so`. Current
retail enforcing-firmware support is blocked; see the
[compatibility matrix](../reference/compatibility.md#jsi-has-three-separate-support-gates).

## 4. Call the function from the plugin UI

For Native or JNI, a minimal `App.tsx` handler can log the result and display it:

```tsx
import React, {useState} from 'react';
import {Button, Text, View} from 'react-native';
import Math from 'local-math';

export default function App(): React.JSX.Element {
  const [result, setResult] = useState('Not run');

  async function runAdd() {
    try {
      const value = await Math.add(20, 22);
      console.log('Math.add result:', value);
      setResult(String(value));
    } catch (error) {
      console.error('Math.add failed:', error);
      setResult(String(error));
    }
  }

  return (
    <View>
      <Button title="Run native add" onPress={runAdd} />
      <Text testID="math-result">{result}</Text>
    </View>
  );
}
```

For JSI, make `runAdd` a normal function and call `MathJsi.add(20, 22)` without
`await`.

## 5. Compile and package

If Add used `--skip-install`, link dependencies first from the plugin root:

```bash
npm install
```

For a Yarn project:

```bash
yarn install
```

Package on macOS/Linux:

```bash
./buildPlugin.sh
```

Package on Windows PowerShell:

```powershell
.\buildPlugin.ps1
```

The Android build regenerates bindings and `index.d.ts`. The final package is:

```text
build/outputs/plugin.snplg
```

`plugin.snplg` is the official template's default. If the `name` in
`PluginConfig.json` differs, use the filename actually written to
`build/outputs/`. The generator's `--build` option is different: it runs the
parent Gradle assemble task as a compile check and does not replace
`buildPlugin.sh` or prove that a `.snplg` was produced.

## 6. Transfer and install

Copy with Android Debug Bridge (ADB):

```bash
adb push "build/outputs/plugin.snplg" /storage/emulated/0/MyStyle/
```

Or copy the file manually into the device's `MyStyle` folder. Then, on the
device, open **Settings > Apps > Plugins > Add Plugin**, select the package, and
install it. These are separate transfer and installation steps.

The official Supernote guide documents the parent plugin workflow in
[Your First Plugin](https://docs.supernote.com/en/first-plugin).

## 7. Verify the result

Open the plugin and press **Run native add**. The UI should show `42`.

To inspect JavaScript logs:

```bash
adb logcat -d -s ReactNativeJS:V '*:S'
```

For module-specific native errors, add the generated tag:

```bash
adb logcat -d -s SupernoteNativeMath:V ReactNativeJS:V '*:S'
```

JNI uses `SupernoteNative<JavaScriptName>`; JSI uses
`SupernoteJsi<JavaScriptName>`. If the import, build, installation, or runtime
step fails, continue with [Troubleshooting by symptom](../troubleshooting/README.md).

## 8. Know what may be edited

Safe user-owned paths:

- Native: Kotlin/Java below the module's `android/src/main/java/` source tree,
  excluding generated packages;
- JNI/JSI: everything below the module's `android/src/main/cpp/`;
- the parent plugin's JavaScript/TypeScript source.

Do not edit metadata, wrappers, declarations, Gradle/CMake files, generated
Kotlin, loaders, registration, or autolinking. Update replaces them, including
the generated README. Remove deletes the entire package including user source.
See [Managing modules safely](../guides/managing-modules.md) before either action.
