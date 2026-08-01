# Changelog

This file records user-visible changes to the generator. Documentation on the
default branch may be newer than the latest published package; released users
should also check their installed version with `supernote-module --version`.

## 1.1.0.dev0 - Unreleased

- Defined the product boundary as native-module generation for an existing,
  working Supernote plugin; removed general plugin packaging, installation, and
  debugging guidance from repository and Wiki documentation.
- Removed Doctor's unrelated ADB and connected-device probes while retaining
  the JSI-specific runtime-policy advisory.
- Made the GitHub Wiki the canonical plugin-developer documentation and reduced
  the repository README to a product entry point.
- Updated generated module READMEs to link directly to task-oriented Wiki
  pages.
- Described JSI as a supported generator backend while retaining its explicit
  host, firmware, linker, and SELinux runtime requirements.
- Reorganized documentation around native-module generation in an existing
  Supernote plugin.
- Clarified Native/JNI call models and the host-dependent JSI runtime boundary.
- Separated contributor and PyPI release documentation from plugin-developer
  guides.
- Added generator-version markers to generated module metadata and READMEs.
- Fixed the initial Native TypeScript interface name so template text does not
  leak into `index.d.ts`.

## 1.0.0 - 2026-07-31

- First PyPI release with Native, JNI, and JSI generation.
- Added guided and non-interactive Add, Update, Validate, Remove, and Doctor
  workflows with transactional recovery and human/JSON output modes.
