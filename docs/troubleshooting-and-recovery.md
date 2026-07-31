# Troubleshooting and recovery

## Package linking is not deployment

Add normally runs npm or Yarn to link the generated local package into the
parent plugin. That operation does not compile Android code or deploy the
plugin. After changing a native export, run from the plugin root:

```bash
./buildPlugin.sh
```

On Windows PowerShell, run `.\buildPlugin.ps1` instead. Install the generated
`build/outputs/<plugin-name>.snplg` with ADB (replace the placeholder with the
generated filename):

```bash
adb push "build/outputs/<plugin-name>.snplg" /storage/emulated/0/MyStyle/
```

Alternatively, manually copy the `.snplg` file into the device's `MyStyle`
folder. Then open **Settings > Apps > Plugins** and choose **Add Plugin**.

If `--skip-install` was used, run the parent project's package-manager install
before building.

## Common problems

| Symptom | What to do |
| --- | --- |
| Package is unavailable or not linked | Install the local dependency, rebuild, redeploy, and inspect `ReactNativeJS` in logcat. |
| A new Native method is missing | Check the exact `SupernoteExport` import, supported signature, and public class/constructor, then rebuild. |
| A new C++ export is missing | Check the marker, supported top-level signature, and unique exported name, then rebuild. |
| Validate passes but Android compilation fails | Validate is structural unless `--build` is requested. Run Doctor for the module type, then build for compiler diagnostics. |
| A JNI promise rejects | Inspect the module-specific `SupernoteNative...` tag and `ReactNativeJS`; generated errors include module/export context. |
| A JSI object is unavailable | Rebuild and redeploy, then inspect the module-specific `SupernoteJsi...` tag and `ReactNativeJS`; bootstrap or native loading may have failed. |
| C/C++ changes appear stale | Rebuild the plugin so Gradle inventories sources and regenerates bindings. |
| A Context-dependent Android API cannot be used | Use a supported public Native Module constructor taking `ReactApplicationContext` or `android.content.Context`. |

## Ownership and Update

Update replaces generator-owned infrastructure but preserves implementation
source:

- Native: Kotlin/Java beneath the module's package source directory.
- JNI and JSI: the complete `android/src/main/cpp/` tree.

Do not hand-edit generated metadata, bridge code, loaders, CMake, declarations,
or autolinking files. The generated module README lists the exact paths for that
module.

## Removal safety

Remove deletes the entire module directory, including implementation source.
Commit or back up the module first. Interactive removal requires the exact
package name; removing all modules requires `REMOVE ALL`.

## Interrupted or incomplete operations

Add, Update, and Remove stage project changes, journal affected paths, verify
postconditions, and then commit. When rollback or dependency reconciliation is
incomplete, the command exits with code `3`, keeps recovery information, and
prints the exact recovery action.

Follow the printed recovery instruction. The next generator invocation also
attempts startup recovery before beginning new work. Do not manually delete the
journal or recovery storage while recovery is pending.

## Device and SELinux limits

Doctor can report ADB, device, and policy evidence, but those checks are
advisory. The generator does not claim compatibility with an enforcing SELinux
device without a separate device test, and it does not inspect or modify SELinux
policy.
