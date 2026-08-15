# Changelog

This file records user-visible changes to the generator. Documentation on the
default branch may be newer than the latest published package; released users
should also check their installed version with `supernote-module --version`.

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
