# Manage modules safely

Add, Update, Validate, and Remove operate on packages managed by
`.supernote-module.json`. Commit the plugin before lifecycle operations so you
can inspect or restore changes independently of the generator's transaction
recovery.

## Validate

Structural validation checks generated files, metadata, parent integration,
export scanning, and the `node_modules` local-package link:

```bash
supernote-module validate local-math
```

Add an Android compile check:

```bash
supernote-module validate local-math --build --verbose
```

`--build` invokes the parent plugin's `android/gradlew :app:assembleDebug` task.
It does not run `buildPlugin.sh`, produce a `.snplg`, transfer it, or prove
PluginHost/device compatibility.

Validate every managed module:

```bash
supernote-module validate --all
```

## Update

**There is no dry-run or file-by-file diff. Commit first.** Interactive Update
shows a coarse replace/preserve plan and Git status; `--yes` accepts that plan.

```bash
supernote-module update local-math
```

Update cannot change the module type, package name, JavaScript name, Android
namespace, or package version. Recreate the module deliberately if one of those
identities must change.

### What Update preserves

| Module type | Preserved user source |
| --- | --- |
| Native | Kotlin/Java below `android/src/main/java/` (and migrated legacy `android/src/main/kotlin/`), except generated packages |
| JNI/JSI | Complete `android/src/main/cpp/` tree, including deleted starter-file choices |

Native Update also retains the previous `index.d.ts` until the Android/KSP build
regenerates it. Build after changing exports.

### What Update may replace

- `.supernote-module.json`, `README.md`, `.gitignore`, and package metadata;
- `index.js`, `index.d.ts` when generated, and `react-native.config.js`;
- Android manifest, Gradle, CMake, annotation/processor, loader, bridge,
  registration, and generated Kotlin/C++ files;
- the managed Native registration block in parent Android settings;
- the parent dependency entry and package-manager link when refresh is needed.

Do not keep notes or custom instructions in the generated README: Update
replaces it. Put project-specific documentation in the parent plugin instead.

## Remove

**Remove permanently deletes the complete module directory, including preserved
implementation source. Commit or copy the implementation first.**

```bash
supernote-module remove local-math
```

Interactive removal requires typing the exact package name. Automation must
name an unambiguous target and use `--yes`:

```bash
supernote-module remove local-math --yes
```

Removing every managed module requires the explicit scope and confirmation:

```bash
supernote-module remove --all --yes
```

Remove detaches the package, updates the parent dependency/Native Gradle
integration, refreshes dependencies unless skipped, verifies the detached
state, then commits deletion. A failure before commit attempts rollback.

## Package-manager effects

Add normally links the local dependency. Update refreshes dependencies only
when the package metadata or `node_modules` link is not current. Remove normally
refreshes after detaching.

With `--skip-install`, the filesystem and parent metadata may be ready while the
package-manager link remains stale. Run the printed recovery command, normally:

```bash
npm install
```

or:

```bash
yarn install
```

When both `package-lock.json` and `yarn.lock` exist, lifecycle operations that
need dependency work require `--package-manager npm` or
`--package-manager yarn`.

## Interrupted operations and rollback

Mutating commands stage changes and journal affected paths. If interruption or
external dependency reconciliation cannot be fully reversed, the command exits
with code `3`, retains recovery data, and prints one recovery action.

- Follow the printed action.
- Run the generator again to allow startup recovery.
- Do not manually delete `.supernote-module-transaction.json` or its recovery
  storage while recovery is pending.
- If recovery repeatedly fails, preserve the journal and inspect the reported
  paths before manual intervention.

The transaction mechanism reduces partial-state risk; it is not a replacement
for version control or a backup of code that a successful Remove intentionally
deletes.
