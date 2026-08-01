# Release the Python generator

This procedure is for maintainers publishing `supernote-module-generator`. It
is not part of the workflow for developers building Supernote plugins.

Publish only from a clean checkout after the package version, changelog,
compatibility matrix, templates, and user documentation are final.

## Prepare

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

Update the version in `src/supernote_module_generator/__init__.py`. Confirm that
`CHANGELOG.md` has a dated section for that version and that generated README
links/markers identify it correctly.

## Validate

```bash
python3 -m pytest -q
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Inspect both artifacts. The wheel must contain the MIT license, Python package,
templates, console entry point, and root README as its package metadata/PyPI
long description. The source distribution must additionally contain the
changelog, current user docs, contributor/maintainer/architecture docs, and
intended test sources. Neither artifact may contain superseded audits or agent
implementation prompts.

Install the wheel in a fresh virtual environment and verify:

```bash
supernote-module --version
supernote-module --help
```

Generate all three module types from the wheel. At minimum, verify names, source
paths, calls, declarations, ownership metadata, generator version, and links.
Do not claim Android/package/device validation unless that tier was actually
run.

Before release, review:

- the root first-success workflow against the official Supernote template;
- the canonical compatibility matrix and dated device/JSI evidence;
- Update/Remove ownership and migration behavior;
- documented CLI options against parser/help metadata;
- external and relative links;
- release notes for user-visible template, command, or support changes.

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

Confirm `main` CI is green and the version is not already present on PyPI. Then
create a GitHub release whose tag exactly matches the package version:

```bash
gh release create v1.1.0 --title "v1.1.0" --generate-notes
```

Replace `1.1.0` with the version being released.

Publishing the release runs `.github/workflows/publish.yml`. It builds/checks
artifacts from the tag, transfers them to the isolated publishing job, obtains
the PyPI credential, and uploads the distributions.

## Verify the public release

Use a temporary virtual environment outside the repository:

```bash
python3 -m venv /tmp/supernote-module-generator-smoke
/tmp/supernote-module-generator-smoke/bin/python -m pip install \
  supernote-module-generator==1.1.0
/tmp/supernote-module-generator-smoke/bin/supernote-module --version
/tmp/supernote-module-generator-smoke/bin/supernote-module --help
```

Replace `1.1.0` with the version being verified.

PyPI does not permit replacing a file or reusing a published version. If an
artifact is wrong, increment the version, rebuild from a clean checkout, and
publish a new release with corrective notes.
