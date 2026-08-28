# Release the Python generator

This procedure is for maintainers publishing `supernote-module-generator`. It
is not part of the workflow for developers building Supernote plugins.

Publish only from a clean checkout after the package version, changelog,
templates, and linked Wiki documentation are final. Publication is gated by
the exact release commit; a successful workflow from another commit is not
release evidence.

## Prepare

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

Update the version in `src/supernote_module_generator/__init__.py`. Confirm that
`CHANGELOG.md` has a dated section for that version and that generated README
links and version markers are correct.

## Validate

```bash
python3 -m ruff check src tests ci
python3 -m mypy
python3 -m compileall -q src tests ci
python3 -m coverage run -m pytest -q
python3 -m coverage report
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Inspect both artifacts. The wheel must contain the MIT license, Python package,
templates, console entry point, and root README as its package metadata/PyPI
long description. The source distribution must additionally contain the
changelog, contributor/maintainer/architecture docs, and intended test sources.
Wiki pages live in the separate Wiki repository and must not be duplicated in
the package. Neither artifact may contain superseded audits or agent
implementation prompts.

Install the wheel in a fresh virtual environment and verify:

```bash
supernote-module --version
supernote-module --help
```

Generate features with the C/C++ starter, Kotlin/Java starter, and both starters
from the wheel. At minimum, verify names, source paths, explicit markers,
declarations, ownership metadata, one plugin runtime component, generator
version, and links. The required automated fixture starts from the remotely
reachable pinned official plugin-template commit recorded in
`.github/workflows/quality.yml`, then applies and verifies the packaged V4
template capability,
materializes the tagged root-README examples, checks out the pinned Wiki
revision, and requires:

- a second `update --all` to be a true no-op;
- generated JavaScript lint and TypeScript checking with no warnings;
- `check --build` to pass Gradle, KSP, Kotlin, CMake, JNI, and JSI compilation;
- the external read-only state hook to run during the Android build;
- the official plugin build and package-verification scripts to pass.

The same release gate runs native host qualification on Ubuntu, macOS, and
Windows. It checks platform path and command selection, operation locking,
symlink capability/preflight behavior, spaces, Unicode, Windows long paths,
both Bash and PowerShell launch-script
syntax, and the exact `npm run run` outcome against a fake ADB device. A tap is
not runtime proof: without a plugin-specific marker the scripts must say that
launch was attempted but runtime success was not verified.

Every root README and Wiki CLI example is inventoried directly from its source,
grammar-checked, and classified. Non-runnable placeholders and environment,
Android, or device commands state the gate that covers them. The pinned Wiki's
bounded stateful release-command block is additionally executed against a second
disposable official-template project. That project must be a true generator
no-op after generation, remain source-identical through `check --build`, pass
lint and TypeScript, compile Gradle/KSP/Kotlin/CMake/JNI/JSI, and produce a
package accepted by the official verifier.

The exact `file_reader_test` revision is stored as a checked-in Git bundle and
cloned only into a disposable CI directory. Its non-migration scenarios 7.1
through 7.7 run against the built wheel; retained authorized device evidence
qualifies scenarios 7.9 and 7.10. The dirty live developer checkout is never a
release input.

The same pinned revision also seeds two separate disposable host fixtures for
the bounded final integration pack. One fixture runs in a dedicated NOTE and
denies `plugin.permission.FILE:WRITE`; the other runs in a dedicated PDF/DOC
context and allows `plugin.permission.FILE:READ` once. Each package compiles and
executes the same 15 source-backed checks spanning generated C++/JSI, generated
Kotlin/JVM, safe Android build information, mixed-family calls, PluginManager,
common host APIs, the NOTE/DOC-specific API, and permission status/request/result.
This is a focused two-context release gate, not the separate 100-plugin matrix.
The definitions live in `ci/device_acceptance/`, and CI retains both identities
and installable packages as exact-commit artifacts.
The retained Nomad result in
`maintainers/device-evidence/v4-bounded-note-doc-2026-08-27/` is reparsed by
the real-project acceptance runner and must match the same 15-check source
manifest before scenario 7.11 can pass.

The fixture installs and uses the wheel produced from that same commit. It must
not use an editable checkout or a globally installed `supernote-module`.
Do not claim Android/package/device validation unless that tier was actually
run.

Before release, review:

- the root quick example and Wiki Add a Feature workflow against a working
  external plugin fixture;
- the Wiki initial-capability boundaries and dated device/JSI evidence;
- Update/Remove ownership and source-preservation behavior;
- documented CLI options against parser/help metadata;
- repository links, Wiki pages/slugs, and generated README links;
- release notes for user-visible template, command, or support changes.

Clone or inspect `supernote-module-generator.wiki.git` during release review.
Confirm that the Wiki describes the release being published or clearly labels
newer default-branch behavior. Publish Wiki corrections before cutting a
release whose generated READMEs link to them.

## Trusted publishing setup

This repository publishes through GitHub's `pypi` environment and PyPI trusted
publishing. It does not store a long-lived PyPI token.

For the initial publisher configuration, use:

- PyPI project: `supernote-module-generator`
- Owner: `Ziv-Ink`
- Repository: `supernote-module-generator`
- Workflow: `publish.yml`
- Environment: `pypi`

The workflow uses OpenID Connect and a short-lived credential.

## Publish

Confirm `main` CI is green and the version is not already present on PyPI. Run
any required device canary for loader or lifecycle changes and link its evidence
from the release notes. Then create a GitHub release whose tag exactly matches
the package version:

The dated V4 qualification baseline is recorded in
[`device-evidence/v4-device-canary-2026-08-27.md`](device-evidence/v4-device-canary-2026-08-27.md).
A later loader, lifecycle, PluginHost, or firmware change requires new evidence;
do not reuse this record as proof for a different candidate or target.

```bash
gh release create "v${VERSION}" --title "v${VERSION}" --generate-notes
```

Set `VERSION` to the version being released. The workflow rejects a release tag
that does not exactly match the version embedded in the package.

Publishing the release runs `.github/workflows/publish.yml`. The reusable V4
quality workflow checks out `github.sha`, verifies that exact checkout, runs the
complete Python/static/package/generated-Android matrix, and builds the release
artifacts once. Only after every job passes does the isolated publishing job
download the SHA-named artifact, obtain the PyPI credential, and upload it.
The publishing job never rebuilds an unqualified artifact.

## Verify the public release

Use a temporary virtual environment outside the repository:

```bash
python3 -m venv /tmp/supernote-module-generator-smoke
/tmp/supernote-module-generator-smoke/bin/python -m pip install \
  "supernote-module-generator==${VERSION}"
/tmp/supernote-module-generator-smoke/bin/supernote-module --version
/tmp/supernote-module-generator-smoke/bin/supernote-module --help
```

Set `VERSION` to the version being verified.

PyPI does not permit replacing a file or reusing a published version. If an
artifact is wrong, increment the version, rebuild from a clean checkout, and
publish a new release with corrective notes.
