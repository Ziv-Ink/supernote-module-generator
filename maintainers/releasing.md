# Release the Python generator

This procedure is for maintainers publishing `sn-module-gen`. It is not part of
the workflow for developers building Supernote plugins.

Publish only from a clean checkout after the package version, changelog,
generated runtime resources, and linked Wiki documentation are final.
Publication is gated by the exact release commit; a successful workflow from
another commit is not release evidence.

The release-quality Python matrix covers the declared minimum Python 3.9 and
every stable minor through the current supported Python 3.14.

## Prepare

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
python3 -m pip install -r ci/release-requirements.txt
```

Update the version in `src/supernote_module_generator/__init__.py`. Confirm that
`CHANGELOG.md` has a dated section for that version and that generated README
links and version markers are correct.

## Validate

```bash
python3 -m ruff check src tests ci
python3 ci/check_filesystem_complexity.py
python3 ci/check_transaction_complexity.py
python3 -m mypy
python3 -m compileall -q src tests ci
python3 -m coverage run -m pytest -q
python3 -m coverage report
python3 ci/reproducible_release_build.py \
  --source . --output dist --commit "$(git rev-parse HEAD)"
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
sn-module-gen --version
sn-module-gen --help
```

Rebuild a wheel from the source distribution with the pinned release tools and
the commit's `SOURCE_DATE_EPOCH`; it must be byte-identical to the directly
built wheel. Also build a wheel from the source distribution with the declared
minimum backend (`setuptools==58.0.4`, `wheel==0.37.0`), install it separately,
and smoke the public console entry point.

The required generator-only matrix installs the canonical wheel outside the
source checkout and pins `SUPERNOTE_MODULE_COMMAND` to that absolute console
executable. On native Ubuntu, macOS, and Windows, with CMake 3.24.4 and each
runner's current CMake, it must:

- run installed `add`, plain `update`, and project-wide `validate` for C++,
  Kotlin, Java, and mixed authored features;
- run publisher KSP and pack the production-generated module/runtime payloads;
- install those immutable tarballs in two unrelated consumer roots after the
  publisher becomes unavailable;
- trap every consumer generator invocation and prove consumer KSP is absent;
- compile, link, and execute the actual generated C++/JVM/JavaScript/runtime
  paths in Debug and Release;
- reject stale payloads, incompatible identities, stale receivers, and a
  guard-disabled sensitivity mutant;
- retain JUnit and structured success or failure evidence for every matrix row.

The official plugin template, Wiki, existing plugin repositories, app
build/package/deploy scripts, and device packages are separate products. The
generator release workflow does not invoke or mutate them and makes no
app-package or device-runtime claim.

Before release, review the root quick example, current add/update/validate
grammar, authored/generated ownership boundaries, npm runtime/module protocol,
repository links, generated README links, and release notes for user-visible
generator or support changes.

Clone or inspect `supernote-module-generator.wiki.git` during release review.
Confirm that the Wiki describes the release being published or clearly labels
newer default-branch behavior. Publish Wiki corrections before cutting a
release whose generated READMEs link to them.

## Trusted publishing setup

This repository publishes through GitHub's `pypi` environment and PyPI trusted
publishing. It does not store a long-lived PyPI token.

For the initial publisher configuration, use:

- PyPI project: `sn-module-gen`
- Owner: `Ziv-Ink`
- Repository: `supernote-module-generator`
- Workflow: `publish.yml`
- Environment: `pypi`

The workflow uses OpenID Connect and a short-lived credential.

For the first publication, when the PyPI project does not exist yet, configure
those exact values as a pending publisher from the PyPI account publishing
page. The GitHub `pypi` environment must already exist. Keep repository and
environment secret lists free of `PYPI_TOKEN`, passwords, and other long-lived
publication credentials.

Immediately before configuring the pending publisher, verify that the project
name is still unclaimed:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://pypi.org/pypi/sn-module-gen/json
```

For the initial release the expected response is `404`. Stop if the response is
anything else until project ownership and contents have been reviewed.

## GitHub presentation

Keep the repository URL and the `main` default branch. Before release, confirm
that the public description names `sn-module-gen` and that the topics include
`sn-module-gen`, `supernote`, `code-generator`, `python`, `android`, `cpp`,
`kotlin`, `jni`, `jsi`, `react-native`, and `pypi`. The default-branch README
and the live Wiki must describe the same public `0.1.3` contract before the tag
is created.

## Publish

Confirm `main` CI is green and `0.1.3` is not already present on PyPI. Run
any required device canary for loader or lifecycle changes and link its evidence
from the release notes. Review `maintainers/release-notes-v0.1.3.md`; it must
distinguish generator/package checks from the accepted Mac/Linux relative Bash
evidence and the one proven device native invocation. Retain the unsupported
external linked-source, unchanged PowerShell, shared Bash failure-masking,
Mac Dashboard JDK 21, Windows Android, and device-lifecycle limitations.
Historical 0.1.2 notes retain the complete APFS/ext4 installed-CLI workflows and
focused eCryptFS coverage for that earlier candidate only.
Create the signed or annotated `v0.1.3` tag
from the approved release SHA, push that exact tag, and then create the GitHub
release from the existing tag:

The dated pre-public V4 qualification baseline is retained in
[`device-evidence/v4-device-canary-2026-08-27.md`](device-evidence/v4-device-canary-2026-08-27.md).
A later loader, lifecycle, PluginHost, or firmware change requires new evidence;
do not reuse this record as proof for a different candidate or target.

```bash
git tag --annotate v0.1.3 "$RELEASE_SHA" --message 'sn-module-gen 0.1.3'
git push origin v0.1.3
gh release create v0.1.3 --verify-tag \
  --title 'sn-module-gen 0.1.3' \
  --notes-file maintainers/release-notes-v0.1.3.md
```

Set `RELEASE_SHA` to the independently approved exact commit. The workflow
rejects every tag except `v0.1.3`, verifies that it matches the embedded package
version, and refuses prerelease publication.

The release builder derives `SOURCE_DATE_EPOCH` from the exact source commit,
builds in two fresh detached clones separated in wall-clock time, normalizes
source-distribution archive ownership and timestamps, and refuses to emit
artifacts unless both wheel and source distribution are byte-identical. Release
tool versions come from the exact-pinned `ci/release-requirements.txt`, and the
build is non-isolated so a later package index update cannot change the approved
bytes.

Publishing the release runs `.github/workflows/publish.yml`. The reusable
quality workflow checks out `github.sha`, verifies that exact checkout, runs the
complete Python/static/package/installed-generator matrix, and builds the release
artifacts once. Only after every job passes does the isolated publishing job
download the SHA-named artifact and its SHA-256 provenance, verify both again,
obtain the PyPI credential, and upload it. A separate least-privilege job attaches
the same qualified wheel, source distribution, checksums, and provenance JSON to
the GitHub release. The publishing job never rebuilds an unqualified artifact.

## Verify the public release

Use a temporary virtual environment outside the repository:

```bash
python3 -m venv /tmp/sn-module-gen-smoke
/tmp/sn-module-gen-smoke/bin/python -m pip install \
  "sn-module-gen==${VERSION}"
/tmp/sn-module-gen-smoke/bin/sn-module-gen --version
/tmp/sn-module-gen-smoke/bin/sn-module-gen --help
```

Set `VERSION` to the version being verified.

PyPI does not permit replacing a file or reusing a published version. If an
artifact is wrong, increment the version, rebuild from a clean checkout, and
publish a new release with corrective notes.

## Retire the pre-public PyPI distribution

Do this only after `sn-module-gen==0.1.0` installs from production PyPI and the
public CLI and generated-plugin smoke checks pass. Until then, leave
`supernote-module-generator` unchanged.

In the PyPI management UI for `supernote-module-generator`, yank every release
with this exact reason:

```text
Pre-public development package; replaced by sn-module-gen
```

Do not delete the project or its files, and do not upload a redirect package.
After yanking, use a fresh environment and verify that an ordinary unpinned
`pip install supernote-module-generator` no longer selects an unyanked release.
An explicit pin may still select a yanked file; that does not justify deleting
the retained provenance history.
