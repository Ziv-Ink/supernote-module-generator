# Troubleshooting by symptom

Start with the exact failing layer. A linked package is not yet compiled, a
compiled Android library is not yet a `.snplg`, a transferred package is not yet
installed, and an installed JSI library may still be blocked by PluginHost or
SELinux.

## The generator says this is not a plugin root

Likely cause: the command is not running from the exact plugin directory or one
required marker is missing.

POSIX check:

```bash
pwd
ls -ld PluginConfig.json package.json android android/settings.gradle*
```

PowerShell check:

```powershell
Get-Location
Get-Item PluginConfig.json, package.json, android, android\settings.gradle*
```

The generator does not search parent directories. If `PluginConfig.json` is
missing in a newly scaffolded official plugin, run its packaging script once;
the template creates the config on first packaging.

## npm or Yarn installation failed

Add may have generated a package but rolled back if dependency linking or its
postcondition failed. Read the final rollback/recovery lines first.

Check project evidence:

```bash
ls package-lock.json yarn.lock 2>/dev/null
```

Then rerun the matching manager with its full output:

```bash
npm install
```

or:

```bash
yarn install
```

If both lockfiles exist, choose intentionally with `--package-manager`. Do not
delete the recovery journal while the generator reports pending recovery.

## The generated package is missing in JavaScript

Confirm the parent dependency and Node resolution from the plugin root:

```bash
node -p "require('./package.json').dependencies['local-math']"
node -p "require.resolve('local-math/package.json')"
```

Expected dependency value:

```text
file:./local_modules/local-math
```

If Add used `--skip-install`, run `npm install` or `yarn install`. Then package,
reinstall, and inspect JavaScript logs:

```bash
adb logcat -d -s ReactNativeJS:V '*:S'
```

## The TypeScript import fails

The generated package has one default export. Use:

```typescript
import Math from 'local-math';
```

Do not use a named import such as `{Math}`. Confirm `package.json` names
`index.js` as `main`/`react-native` and `index.d.ts` as `types`.

The Android build regenerates declarations after export changes. Run the parent
packaging script or its Android build before treating a stale declaration as the
new API.

## Gradle cannot find the local package

First prove Node resolution as above. Then inspect React Native autolinking from
the parent plugin and run the real Android task with diagnostics.

macOS/Linux:

```bash
cd android
./gradlew :app:assembleDebug --stacktrace
```

Windows PowerShell:

```powershell
Set-Location android
.\gradlew.bat :app:assembleDebug --stacktrace
```

Return to the plugin root before running `buildPlugin.sh`/`buildPlugin.ps1`.

## CMake or the NDK is missing

Run:

```bash
supernote-module doctor --type jni
```

Check `ANDROID_HOME`/`ANDROID_SDK_ROOT`, the installed NDK under the SDK, and
CMake 3.22.1+. The selected NDK Clang must accept C23 and C++23 for
`aarch64-linux-android27`. Doctor probes those languages but does not run the
complete plugin build.

## A new Kotlin/Java export is missing

Check all of these:

- the annotation import belongs to this module's namespace;
- the method and containing concrete class are public;
- the method is an instance method with supported parameter/return types;
- the class has a supported Context or zero-argument constructor;
- the JavaScript export name is unique;
- the plugin was rebuilt and reinstalled.

Run structural plus build validation:

```bash
supernote-module validate local-math --build --verbose
```

Inspect runtime errors:

```bash
adb logcat -d -s SupernoteNativeMath:V ReactNativeJS:V '*:S'
```

Replace `Math` with the configured JavaScript name.

## A new C++ export is missing

Confirm the marker is directly before a top-level definition in `.cc`, `.cpp`,
or `.cxx`, uses only supported by-value types, and has a unique name. C files
may provide helpers but cannot export.

```bash
supernote-module validate local-math-jni --build --verbose
```

Rebuilding matters: Gradle inventories sources and regenerates bindings before
CMake compilation.

## JNI compiles but a call rejects or a native method is not found

Inspect both bridge and JavaScript tags:

```bash
adb logcat -d -s SupernoteNativeMathJni:V ReactNativeJS:V '*:S'
```

Likely causes include a stale installed plugin, a changed export without a full
rebuild, unsupported generated signature, or native registration/loading
failure. Do not add handwritten JNI symbols; generated `JNI_OnLoad` and
registration own that boundary.

## The plugin builds but does not load

Prove that the expected package was installed, then capture PluginHost and
React Native failures:

```bash
adb logcat -d | rg -i 'pluginhost|ReactNativeJS|UnsatisfiedLinkError|dlopen|FATAL EXCEPTION|AndroidRuntime'
```

If `rg` is unavailable, use your terminal's text search. Verify React Native
`0.79.2`, the `arm64-v8a` package contents, and that the current `.snplg` was
reinstalled after the last native build.

## A JSI function is undefined

The proxy can be imported before native installation completes; it resolves the
global at property access. Rebuild/reinstall, trigger the plugin after
initialization, then inspect:

```bash
adb logcat -d -s SupernoteJsiMathJsi:V ReactNativeJS:V '*:S'
```

Look for library lookup, host `libjsi.so`, `nativeInstall`, runtime-pointer, or
HostFunction installation failures. Confirm the exact generated tag in the
module README or generated Kotlin loader.

## The JSI `.so` cannot execute on the Supernote

An error containing `dlopen ... Permission denied` or an SELinux
`avc: denied { execute }` is a device-policy failure, not an export-marker
problem. The tested enforcing retail configuration blocks extracted JSI native
libraries. The generator cannot modify SELinux policy.

Use Native/JNI for a supported architecture, or obtain firmware/PluginHost
support for an executable native-library location. Do not treat a permissive or
root device success as retail support. See
[JSI support gates](../reference/compatibility.md#jsi-has-three-separate-support-gates).

## Update would overwrite changed files

Update has no dry-run/diff. Commit and inspect the module's
`.supernote-module.json`:

- `generated_files` may be replaced;
- `implementation_roots` are preserved by Update;
- the generated README is replaceable;
- Remove ignores the distinction and deletes the whole package after success.

Move intended customizations out of generator-owned infrastructure or maintain
them as an explicit post-generation patch. Read
[Manage modules safely](../guides/managing-modules.md#update).

## Generation stopped halfway

Run the generator again. Startup recovery reads
`.supernote-module-transaction.json` and attempts to restore or finalize the
recorded transaction. Exit code `3` means manual recovery is still required;
follow the one printed action and preserve the journal while investigating.

Compare `package.json`, lockfiles, Android settings, `local_modules/`, and
`node_modules/` with version control before making manual changes.

## Validate passes but packaging or the device fails

Plain Validate proves structure, parent integration, binding generation, and
the local dependency link. `--build` adds `:app:assembleDebug`. Neither mode
runs `buildPlugin.sh`, checks the final `.snplg`, installs it, or qualifies the
target firmware. Continue at the first unproven layer instead of treating
Validate as end-to-end success.
