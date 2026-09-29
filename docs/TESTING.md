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

Python 3.9 through the current stable Python 3.14 are supported and run in the
release-quality matrix. Create an isolated contributor environment:

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
python3 -m pytest -q tests/test_q5_installed_qualification.py
python3 -m pytest -q tests/test_q5_combined_installed_acceptance.py
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

It also compiles all Python sources on each host. Run this subset on the
operating system whose behavior changed. Wine or a mocked path does not replace
native Windows evidence. Template launch, app build/package/deploy scripts, and
fake-ADB behavior are separate products and are not generator quality gates.

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

Install the wheel and source distribution separately as well. Rebuild a wheel
from the source distribution with the same pinned release tools and
`SOURCE_DATE_EPOCH`; it must be byte-identical to the directly built canonical
wheel. Separately build and install the source distribution with the declared
minimum backend (`setuptools==58.0.4`, `wheel==0.37.0`). The package job in CI
shows the complete clean-install checks. Publication jobs download and verify
those already-tested files; they do not build them again.

## Installed generator and generated-code acceptance

The `Installed generator` matrix in `quality.yml` runs on native Linux, macOS,
and Windows with CMake 3.24.4 and each runner's current CMake. Every row installs
the exact canonical wheel in a fresh environment, pins
`SUPERNOTE_MODULE_COMMAND` to that absolute console executable, and records the
wheel, source distribution, import path, tool versions, JUnit, and structured
acceptance evidence. It then:

1. exercises installed `add`, plain `update`, and project-wide `validate` for
   C++, Kotlin, Java, and mixed authored features;
2. runs real publisher KSP and packs production-generated npm payloads;
3. installs the shared runtime and modules into unrelated clean consumers;
4. traps generator use in consumers and proves consumer KSP does not run;
5. compiles, links, and executes actual generated C++/JVM/JavaScript/runtime
   paths in Debug and Release;
6. checks package immutability, compatibility failures, lifecycle rejection,
   and the guard-disabled sensitivity control.

Local reproduction requires Node/npm, Java 17, Gradle 8.13, CMake, and a C++23
compiler. Keep `SUPERNOTE_MODULE_COMMAND` pointed at the `sn-module-gen`
executable installed from the wheel being tested. An editable checkout does not
test the package that will be released.

This is generator and host-runtime evidence. It does not build an app archive,
exercise template scripts, or prove that target firmware loads the runtime.

## Excluded app and documentation products

The official plugin template, Wiki, existing plugin repositories, app
build/package/deploy scripts, and device packages are outside the generator's
delivery gate. Historical fixtures and evidence remain in the source archive
for provenance, but release CI does not invoke or mutate them. Qualify those
products separately when their owners or contracts change.

## Tablet tests

[`maintainers/device-evidence/`](../maintainers/device-evidence/README.md)
contains the saved, dated tablet test results. Some filenames still use the V4
development name because renaming recorded commands or identities would make
the history inaccurate. V4 was a development name, not a public version or a
migration promise.

These tests validate historical record format and parse saved logs again:

```bash
python3 -m pytest -q \
  tests/test_device_acceptance_pack.py \
  tests/test_release_qualification.py
```

They are not a release gate for generator-only changes. Only a real run on the
intended device can test installation, PluginHost loading, JSI/JNI execution,
permissions, NOTE/DOC behavior, generation
replacement, or firmware compatibility. Record the exact generator commit and
package hashes, plugin identity, device serial and model, firmware and
PluginHost versions, ABI, SELinux state, commands, logs, result markers, and any
approved device changes. State clearly whether something was only generated,
compiled, packaged, installed, loaded, or actually executed.

## What CI proves

Green CI at one exact commit proves only the checks that ran:

- supported Python parsing and behavior on the declared Python matrix;
- checked-in correctness, complexity, typing, compilation, and coverage gates;
- native host path, lock, subprocess, and symlink boundaries on the declared
  GitHub-hosted operating systems;
- byte-reproducible artifacts and validated package metadata;
- a byte-identical wheel rebuilt from the source distribution and a separately
  installed minimum-backend wheel;
- installed publisher generation and immutable consumer compile/link/execution
  at minimum/current CMake on all three native hosts.

It does not cover every Python version, operating system, filesystem, Android
toolchain, Supernote model, firmware, PluginHost build, permission choice, or
runtime lifecycle. It makes no app-package or device-runtime claim.
