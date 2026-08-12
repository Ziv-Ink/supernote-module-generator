# Changelog

This file records user-visible changes to the generator. Documentation on the
default branch may be newer than the latest published package; released users
should also check their installed version with `supernote-module --version`.

## 2.0.0.dev0 - Unreleased

- Begin the language-neutral V2 source/semantic/lowering architecture.
- Preserve `supernote-module-generator` and `supernote-module` as the package
  and command identities while reserving stable `2.0.0` for the completed V2
  release.
- Replace public Native/JNI/JSI module selection with language-neutral logical
  features and repeatable C/C++ or Kotlin/Java starter scaffolding.
- Generate one shared plugin-level V2 runtime/build component with explicit
  source intent, common semantics, typed JSI/JNI routes, lifecycle safety, and
  TypeScript declarations.
- Add opt-in `remove --delete-build-files`; ordinary removal and `--yes`
  preserve plugin build output.

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
