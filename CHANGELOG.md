# Changelog

This file records user-visible changes to the generator. Documentation on the
default branch may be newer than the latest published package; released users
should also check their installed version with `supernote-module --version`.

## 1.1.0.dev0 - Unreleased

- Reorganized documentation around the complete Supernote plugin workflow.
- Clarified Native/JNI call models and the experimental, host-dependent JSI
  boundary.
- Separated contributor and PyPI release documentation from plugin-developer
  guides.
- Added generator-version markers to generated module metadata and READMEs.
- Fixed the initial Native TypeScript interface name so template text does not
  leak into `index.d.ts`.

## 1.0.0 - 2026-07-31

- First PyPI release with Native, JNI, and JSI generation.
- Added guided and non-interactive Add, Update, Validate, Remove, and Doctor
  workflows with transactional recovery and human/JSON output modes.
