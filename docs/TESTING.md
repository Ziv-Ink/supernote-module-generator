# Testing

This guide explains how to test changes to `sn-module-gen` and what each group
of tests actually checks. Results for a specific release belong in
`maintainers/release-evidence/`; do not copy old test counts or coverage
percentages into this guide.

[`quality.yml`](../.github/workflows/quality.yml) contains the commands CI
actually runs. Keep the checklist in [`CONTRIBUTING.md`](../CONTRIBUTING.md) and
the release steps in [`maintainers/releasing.md`](../maintainers/releasing.md)
in sync with it.

## Local environment

Python 3.9 or newer is supported. Create an isolated contributor environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

On Windows PowerShell, activate it with:

```powershell
.venv\Scripts\Activate.ps1
```

Release builds also use the pinned tool versions in:

```bash
python3 -m pip install -r ci/release-requirements.txt
```

Use the Python interpreter from the active environment for every command. A
successful run with one interpreter does not replace the Python-version matrix
in CI.

## Focused tests

Run the smallest relevant group while developing, then run all required checks
before requesting review. Useful groups include:

```bash
python3 -m pytest -q tests/test_documentation.py tests/test_release_qualification.py
python3 -m pytest -q tests/test_template_contract.py tests/test_device_acceptance_pack.py
python3 -m pytest -q tests/test_platform_tools.py tests/test_platform_paths.py
python3 -m pytest -q tests/test_operation_lock.py tests/test_regression_harness.py
python3 -m pytest -q tests/test_packaging.py tests/test_static_safeguards.py
```

These test Python behavior and host-side behavior. Parsing generated text or a
saved log is not the same as compiling Android code or running on a tablet.

## Complete Python and static checks

Run the complete suite:

```bash
python3 -m pytest -q
```

Measure branch coverage using the checked-in configuration and threshold:

```bash
python3 -m coverage run -m pytest -q
python3 -m coverage report
```

Run the linting, complexity, typing, and Python compilation checks:

```bash
python3 -m ruff check src tests ci
python3 -m ruff check \
  src/supernote_module_generator/generation_plan.py \
  src/supernote_module_generator/arguments.py \
  src/supernote_module_generator/integrity_manifest.py \
  src/supernote_module_generator/semantic_types.py \
  src/supernote_module_generator/cpp_lexer.py \
  src/supernote_module_generator/cpp_declarations.py \
  src/supernote_module_generator/cpp_members.py \
  src/supernote_module_generator/cpp_member_semantics.py \
  src/supernote_module_generator/cpp_member_shapes.py \
  src/supernote_module_generator/cpp_class_syntax.py \
  src/supernote_module_generator/cpp_function_syntax.py \
  src/supernote_module_generator/cpp_global_functions.py \
  src/supernote_module_generator/cpp_source_routing.py \
  src/supernote_module_generator/jsi_binding_decisions.py \
  src/supernote_module_generator/cpp_type_syntax.py \
  src/supernote_module_generator/binding_codegen.py \
  src/supernote_module_generator/template_contract.py \
  src/supernote_module_generator/windows_authority.py \
  ci/release_asset_preflight.py \
  ci/release_provenance.py \
  --select C901
python3 ci/check_filesystem_complexity.py
python3 ci/check_transaction_complexity.py
python3 -m mypy
python3 -m compileall -q src tests ci
git diff --check
```

The first Ruff command runs the repository's normal lint rules. The C901 list
and the two scripts stop complexity from increasing in the parts of the code
that are already tracked. Mypy only checks the files listed in
`pyproject.toml`; do not remove files from that list just to make a change pass.

## Native platform tests

The reusable workflow runs this platform-specific group on native
GitHub-hosted Ubuntu, macOS, and Windows:

```bash
python3 -m pytest -q \
  tests/test_platform_tools.py \
  tests/test_operation_lock.py \
  tests/test_regression_harness.py \
  tests/test_platform_paths.py \
  tests/test_devconfig.py \
  tests/test_packaging.py::test_clean_wheel_installs_only_the_public_console_script
```

It also compiles all Python sources on each host, verifies Bash and PowerShell
launch-script parity, parses the native shell boundary, and exercises
`npm run run` against a fake ADB device. Run this subset on the operating system
whose behavior changed. Wine, a POSIX PowerShell installation, or a mocked path
does not replace native Windows evidence.

The fake-ADB check covers launch-script selection, quoting, environment
variables, and the explicit "runtime success was not verified" result. It does
not install the plugin or prove PluginHost loading, JSI execution, or device UI
behavior.

## Reproducible package builds

Start from a clean checkout. The output directory must not already exist:

```bash
python3 ci/reproducible_release_build.py \
  --source . \
  --output dist \
  --commit "$(git rev-parse HEAD)"
python3 -m twine check dist/*
```

The builder verifies that `HEAD` is the requested commit and the checkout is
clean. It derives `SOURCE_DATE_EPOCH` from that commit, creates two fresh
detached clones separated in wall-clock time, normalizes source-distribution
timestamps and ownership, and compares artifact names, sizes, and SHA-256
digests byte for byte.

Record and verify where the packages came from, along with their hashes, using
a new output directory:

```bash
python3 ci/release_provenance.py record \
  dist build/qualification/provenance \
  --repository Ziv-Ink/supernote-module-generator \
  --commit "$(git rev-parse HEAD)"
python3 ci/release_provenance.py verify \
  dist build/qualification/provenance \
  --repository Ziv-Ink/supernote-module-generator \
  --commit "$(git rev-parse HEAD)"
```

Install the wheel and source distribution separately as well. The package job
in CI shows the complete clean-install checks. Publication jobs download and
verify those already-tested files; they do not build them again.

## Generated Android plugin and official template

The `Generated Android plugin` job in `quality.yml` is the complete end-to-end
build test. It checks out the pinned official plugin template and Wiki
revisions, installs the wheel being tested, and then:

1. generates C/C++ and Kotlin/Java features from root README examples;
2. applies and verifies the official template support packaged in the wheel;
3. checks that running `update --all` a second time changes nothing;
4. runs JavaScript lint and TypeScript checking without warnings;
5. runs `sn-module-gen --json check --build` and validates its schema;
6. compiles Gradle, KSP, Kotlin, CMake, JNI, and JSI;
7. runs the official plugin build and package verifier.

Local reproduction requires Node 20, Java 17, Android platform 35, build tools
35.0.0, NDK 27.1.12297006, and CMake 3.22.1. Keep
`SUPERNOTE_MODULE_COMMAND` pointed at the `sn-module-gen` executable installed
from the wheel being tested. An editable checkout does not test the package that
will be released.

`check --build` and a verified `.snplg` prove generation, compilation, and
package structure for that host and toolchain. They do not prove that a target
firmware will load or execute the native runtime.

## Pinned Wiki and `file_reader_test`

CI uses self-contained Git bundles so a moving external checkout cannot change
the release input:

- `ci/fixtures/supernote-module-generator-wiki.bundle` is verified at the Wiki
  commit declared in `quality.yml` and exercised by
  `ci/run_wiki_acceptance.py`.
- `ci/fixtures/file_reader_test-9f626ed.bundle` is verified at
  `9f626ed39be82b43ff74eb735d10b7de61f51508` and exercised by
  `ci/run_file_reader_acceptance.py`.

The Wiki runner checks the documented commands, runs the public CLI scenario,
checks that regeneration makes no unnecessary changes, compiles the Android
project, and builds and verifies its package. The `file_reader_test` runner
tests the declared non-migration scenarios without modifying the pinned source,
builds the plugin, and creates separate NOTE and DOC packages from the 15 checks
in `ci/device_acceptance/cases.json`.

To verify the embedded inputs themselves:

```bash
git clone ci/fixtures/supernote-module-generator-wiki.bundle \
  build/qualification/wiki
git clone ci/fixtures/file_reader_test-9f626ed.bundle \
  build/qualification/file-reader-test
git -C build/qualification/wiki rev-parse HEAD
git -C build/qualification/file-reader-test rev-parse HEAD
```

Follow the complete CI jobs when running these helpers. The jobs also install
the required wheel, official template, Node, Java, Android tools, and project
dependencies. A run against a dirty development checkout is not a test of the
pinned input.

## Tablet tests

[`maintainers/device-evidence/`](../maintainers/device-evidence/README.md)
contains the saved, dated tablet test results. Some filenames still use the V4
development name because renaming recorded commands or identities would make
the history inaccurate. V4 was a development name, not a public version or a
migration promise.

These tests validate the record format and parse the saved logs again:

```bash
python3 -m pytest -q \
  tests/test_device_acceptance_pack.py \
  tests/test_release_qualification.py
```

Only a real run on the intended device can test installation, PluginHost
loading, JSI/JNI execution, permissions, NOTE/DOC behavior, generation
replacement, or firmware compatibility. Record the exact generator commit and
package hashes, plugin identity, device serial and model, firmware and
PluginHost versions, ABI, SELinux state, commands, logs, result markers, and any
approved device changes. State clearly whether something was only generated,
compiled, packaged, installed, loaded, or actually executed.

## What CI proves

Green CI at one exact commit proves only the checks that ran:

- supported Python parsing and behavior on the declared Python matrix;
- checked-in correctness, complexity, typing, compilation, and coverage gates;
- native host path, lock, subprocess, symlink, shell, and fake-ADB boundaries
  on the declared GitHub-hosted operating systems;
- byte-reproducible artifacts and validated package metadata;
- generated Android and pinned Wiki/real-project compilation and packaging;
- consistency of retained device evidence with its source-backed manifests.

It does not cover every Python version, operating system, filesystem, Android
toolchain, Supernote model, firmware, PluginHost build, permission choice, or
runtime lifecycle. A build, fake-ADB launch, saved-log check, or UI tap without
a plugin result marker is not proof that the plugin ran successfully.
