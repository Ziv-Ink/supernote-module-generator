# Changelog

This file records user-visible changes to the generator. Documentation on the
default branch may be newer than the latest published package; released users
should also check their installed version with `sn-module-gen --version`.

## Unreleased

## 0.1.0 - 2026-08-30

- First public release as the `sn-module-gen` distribution and command.
- Rename the structured command-result resource to `command-result.schema.json`,
  reset that public envelope to schema `1.0`, and use `SNMG_*` diagnostics.
- Generated runtime and persisted-project identities remain at their qualified
  pre-public spellings until their separately reviewed transition.
- No migration or compatibility support is provided for pre-public generated projects.

The entries below record pre-public development history. Their original version
numbers and evidence identities are retained for provenance; they were not public
`sn-module-gen` releases.

## 4.0.0 - 2026-08-27

- Make one validated, versioned semantic model authoritative for every generated
  source, declaration, runtime, wiring, dependency, and documentation artifact.
- Preview, stage, validate, and atomically commit one complete generation plan;
  record its owned results in a strict integrity manifest and make read-only
  checks reconstruct and compare the same plan.
- Preserve user-owned POSIX file and directory symlinks without dereferencing
  them, reject unsafe managed destinations, and retain exact transactional
  recovery authority through interruption and conflict handling.
- Keep Gradle and KSP read-only with respect to committed plugin sources while
  compiling mixed C++ and Kotlin features through the external V4 generator.
- Add bounded native generation and cleanup accounting, immediate logical
  invalidation, stale-completion suppression, and deterministic restart-required
  behavior at the same-process generation ceiling.
- Reject V1, V2, V3, malformed, mixed, and unmanifested generated layouts before
  mutation. V4 intentionally provides no legacy migration or compatibility path.
- Add structured V4 JSON results, project-selected Android toolchain diagnostics,
  generated-source quality gates, reproducible release qualification, and a
  scoped Nomad lifecycle canary record.

## 3.0.3 - 2026-08-25

- Load generated V3 runtime and registration libraries from PluginHost's
  executable native-library directory instead of `code_cache`, preserving
  SELinux execution policy and same-process plugin upgrades.

## 3.0.2 - 2026-08-22

- Use the plugin's machine-local `devconfig.json` consistently for Doctor,
  Add/Update API generation, Android builds, dependency commands, and recovery,
  without changing the parent shell environment.

## 3.0.1 - 2026-08-22

- Generate feature-specific READMEs from the same merged C++ and JVM semantic
  API as `index.d.ts`, including imports, quick usage, public signatures,
  synchronous and asynchronous call behavior, native objects, copied values,
  enums, and implementation paths.
- Refresh the JavaScript API and README during Add and Update without requiring
  a complete Android build, while retaining `--build` for full build validation.
- Track README and TypeScript files as Gradle outputs, render package
  descriptions as literal text, and restore every affected feature document if
  semantic generation fails.

## 3.0.0 - 2026-08-22

- Add native reference objects for C++, Kotlin, and Java with stable JavaScript
  identity, generated lifetime management, strong type checks, and async
  retention.
- Support object parameters and results, explicit constructors, returned-only
  objects, live fields, and factory functions while keeping the public
  JavaScript and TypeScript API independent of the implementation language.
- Add declared copied values, string enums, homogeneous arrays, nullable
  values, and strict validation for nested type compositions.
- Generate safe `.is`, `.accepts`, `.checkArguments`, and native-object
  inspection APIs for validation and fallback routing without invoking native
  code.
- Keep native reference objects within their C++ or JVM family while allowing
  compatible copied values to cross generated C++/JVM internal routes.
- Bound same-process native generations and shut down generated worker and
  cleanup services when their final runtime session is invalidated.

## 2.0.4 - 2026-08-17

- Correct CLI help so `--yes` is described as applying defaults only to
  omitted choices and `--skip-install` remains an explicit override.
- Distinguish the local feature `--package-version` from the complete plugin's
  `versionCode` and `versionName` in `PluginConfig.json`.
- Align Update and Remove help with their actual confirmation and dependency
  refresh behavior.
- Document the supported Java 17 through 23 Gradle range, Java 17
  recommendation, and Android NDK Clang checks in Doctor help.

## 2.0.3 - 2026-08-16

- Generate one KSP compiler option per feature so plugins with multiple V2
  features compile instead of passing an invalid newline-bearing option.
- Allow native-only, JVM-only, and mixed feature sets by ignoring absent
  optional implementation roots when declaring Gradle task inputs.
- Include generator templates explicitly in source distributions so every
  permitted build backend produces an installable, usable wheel.
- Make generator-managed commands, Android toolchain diagnostics, and native
  runtime tests portable across macOS, Linux, and Windows.
- Resolve Windows command shims and Python launchers without requiring users to
  provide POSIX command names.
- Coordinate Gradle, KSP, common code generation, and CMake through one short
  generated runtime build root, including normalized Windows paths.
- Avoid allocation during generated runtime invalidation and feature teardown.
- Harden subprocess, signal, transaction-lock, and non-UTF-8 failure handling.
- Keep generated JavaScript and TypeScript compatible with strict lint rules.

## 2.0.2 - 2026-08-15

- Make Add and Remove rollback restore Android application integration exactly,
  including interrupted or dependency-install failure paths.
- Serialize generator operations per plugin and report a clear busy error when
  another operation is already changing the same plugin.
- Validate leftover generated wiring even when a plugin currently has no V2
  features.
- Accept the conventional `--` option terminator and reject malformed feature
  metadata, escaping managed symlinks, and unsupported marked C++ boundaries
  with focused preflight diagnostics.
- Keep expected KSP source errors concise instead of appending processor stack
  traces.
- Preserve Kotlin/Java exception messages through synchronous and asynchronous
  JSI routes while retaining the stable `SupernoteError` contract.
- Expand executable coverage for the runtime-lazy feature Proxy, including
  runtime replacement and reflection behavior.

## 2.0.1 - 2026-08-14

- Make generated feature imports safe during ordinary JavaScript module
  evaluation. A normal static import such as `import document from 'document'`
  can now be evaluated before the Supernote JSI runtime is installed; the
  feature is resolved lazily when JavaScript actually accesses it.
- Resolve the current feature on each property access instead of caching a
  JavaScript wrapper across runtime/session replacement.
- Keep premature feature use explicit: calling or reading a feature property
  before the runtime is installed still fails immediately with a clear error.

## 2.0.0 - 2026-08-13

- Introduce the language-neutral V2 source/semantic/lowering architecture.
- Preserve `supernote-module-generator` and `supernote-module` as the package
  and command identities.
- Replace public Native/JNI/JSI module selection with language-neutral logical
  features and repeatable C/C++ or Kotlin/Java starter scaffolding.
- Generate one shared plugin-level V2 runtime/build component with explicit
  source intent, common semantics, typed JSI/JNI routes, lifecycle safety, and
  TypeScript declarations.
- Link the plugin runtime against host React Native/JSI libraries without
  repackaging those libraries into the runtime AAR, preventing duplicate JNI
  library failures in the parent Android application.
- Add opt-in `remove --delete-build-files`; ordinary removal and `--yes`
  preserve plugin build output.
- Improve guided CLI choices, validation summaries, next-step guidance, and
  source-located diagnostics for invalid marker combinations.
- Validate V2's initial value, object, async, error, bounded-executor,
  teardown, and same-process PluginHost reload behavior on the target device.

## 1.1.0.dev0 - Historical V1 development baseline

- Added first-class JSI native C++ object exports with generated HostObject
  lifetime management, `ObjectName.create(...)` factories, TypeScript types,
  and manifest metadata.
- Documentation now assumes an existing working Supernote plugin and focuses
  on adding and managing generated modules. User guides live in the GitHub
  Wiki; contributor and release documentation remains in the repository.
- Removed Doctor's unrelated ADB and connected-device probes while retaining
  the JSI-specific runtime-policy advisory.
- Described JSI as a supported generator backend while retaining its explicit
  host, firmware, linker, and SELinux runtime requirements.
- Added generator-version markers to generated module metadata and READMEs.
- Fixed the initial Native TypeScript interface name so template text does not
  leak into `index.d.ts`.

## 1.0.0 - 2026-07-31

- First PyPI release with Native, JNI, and JSI generation.
- Added guided and non-interactive Add, Update, Validate, Remove, and Doctor
  workflows with transactional recovery and human/JSON output modes.
