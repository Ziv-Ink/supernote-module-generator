# Publishing to PyPI

Publish only from a clean checkout after the release version and documentation
are final.

## Prepare the environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

## Validate the release

```bash
python3 -m pytest -q
python3 -m build
python3 -m twine check dist/*
```

Inspect both archives before uploading. They must include the MIT license,
PyPI README, source documentation, Python package, templates, and tests. Install
the wheel into a fresh virtual environment and verify:

```bash
supernote-module --version
supernote-module --help
```

## Configure trusted publishing once

This repository publishes through GitHub's `pypi` environment and PyPI trusted
publishing. It does not store a long-lived PyPI token.

Before the first release, add a pending GitHub publisher on the PyPI account's
Publishing page with these exact values:

- PyPI project name: `supernote-module-generator`
- Owner: `Ziv-Ink`
- Repository: `supernote-module-generator`
- Workflow: `publish.yml`
- Environment: `pypi`

The pending publisher creates the PyPI project during the first successful
workflow run. The project name is not reserved until that upload happens.

## Publish a release

Confirm the `main` CI workflow is green. Then create a GitHub release whose tag
matches the package version:

```bash
gh release create v1.0.0 --title "v1.0.0" --generate-notes
```

Publishing the release runs `.github/workflows/publish.yml`. The workflow builds
and checks new artifacts from the tag, transfers them to an isolated publishing
job, obtains a short-lived PyPI credential through OpenID Connect, and uploads
the distributions.

After the workflow succeeds, install from the public index in a clean virtual
environment:

```bash
python3 -m venv /tmp/supernote-module-generator-smoke
/tmp/supernote-module-generator-smoke/bin/python -m pip install \
  supernote-module-generator==1.0.0
/tmp/supernote-module-generator-smoke/bin/supernote-module --version
/tmp/supernote-module-generator-smoke/bin/supernote-module --help
```

PyPI does not permit replacing a file or reusing a published version. If an
artifact is wrong, increment the version, rebuild from a clean checkout, and
upload the new release.
