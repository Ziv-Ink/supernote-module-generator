# Contributing to Supernote Module Generator

This file is for people changing the generator. Plugin developers should start
with the short [README.md](README.md), then use the
[GitHub Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki).

## Set up a contributor environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

On Windows PowerShell, activate the virtual environment with:

```powershell
.venv\Scripts\Activate.ps1
```

Then run the same `pip` and `pytest` commands.

## Sources of truth

Do not add a long implementation prompt or agent specification as a competing
contract.

| Fact | Authoritative source | Secondary representation |
| --- | --- | --- |
| CLI grammar | `arguments.py` and tests | `helptext.py`, Wiki CLI and Automation page |
| Exact installed help | `helptext.py` | CLI route tests |
| Generated paths/ownership | feature/runtime generators and `.supernote-module.json` | generated feature README |
| Source signatures/types | C++ source projection and KSP JVM manifest frontend | root README examples |
| Common API meaning | semantic model and its projection tests | generated TypeScript |
| Runtime routes/lifetime | typed lowering plans and plugin runtime generator | architecture history summary |
| Tool/device support | Wiki Requirements and Compatibility page with dated evidence | README summary |
| First generated module | Wiki Add a Module page | concise README example and generated package links |
| Release procedure | `maintainers/releasing.md` | publish workflow |

Tests should assert observable behavior or a necessary safety invariant. They
must not parse a prose implementation plan solely to make that plan normative.
Exact terminal tests remain appropriate when they protect accessibility,
copyability, stream separation, machine output, keyboard safety, or recovery.

## Test layers and confidence

- Unit tests cover parsing, naming, source and semantic projection, typed
  lowering/code generation, rendering, transactions, and generated file shape.
- Integration tests cover CLI lifecycle behavior against temporary plugin
  roots, parent wiring, source preservation, rollback, and documentation
  artifacts.
- Android fixture tests must compile the single plugin-level V2 runtime with
  mixed C/C++ and Kotlin/Java feature input. Do not describe Python-only tests
  or generated-text checks as Android compilation proof.
- Device tests are relevant only when qualifying generated runtime integration,
  especially JSI. Record firmware, PluginHost, ABI, SELinux mode, commands, and
  logs; distinguish generated, compiled, loaded, and executed outcomes.

When a realistic fixture is added, keep it under `tests/fixtures/` and make at
least one test or documented maintainer command execute it. Do not retain an
unused placeholder fixture.

## Documentation and template checklist

For a change to CLI behavior, generated paths, module APIs, supported versions,
build integration, or device compatibility:

- [ ] Update the authoritative code/constant first.
- [ ] Add or update a behavior test that would fail without the change.
- [ ] Update the root generator example only if new users need the fact.
- [ ] Update the relevant GitHub Wiki page in its separate Wiki repository;
      avoid copying the fact into unrelated pages.
- [ ] Update generated README templates only for package-specific guidance.
- [ ] Generate C/C++-only, Kotlin/Java-only, and mixed logical features and scan
      every generated text file for unresolved template values.
- [ ] Confirm explicit source intent, generated TypeScript, sync/async behavior,
      and the files preserved by Update.
- [ ] Run every documented shell/PowerShell command that can be exercised in
      the available environment; label anything not run.
- [ ] Check repository and Wiki links, page slugs, and stale moved-file
      references.
- [ ] Update the Wiki Requirements and Compatibility page with evidence and date when a tool,
      host, device, or policy claim changes.
- [ ] Add a user-visible note to `CHANGELOG.md` when released behavior changes.
- [ ] Confirm that the change does not accidentally add V1 compatibility,
      conversion, migration-analysis, or source-rewriting behavior.

Generator-specific user guidance belongs in the Wiki. General Supernote plugin
development belongs in the official Supernote documentation. Generated READMEs
should contain actual package names, paths, calls, and Update/Remove warnings;
Update replaces them, so they must not be the only place a workflow is
explained.

## Validation before a pull request

```bash
python3 -m pytest -q
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Also inspect `git diff --stat` and generated samples. A successful Python suite
does not prove Android compilation or device runtime behavior; report the exact
validation tier completed.

## Repository boundaries

- `README.md` is a short product entry point; the separate GitHub Wiki contains
  generator-specific user guidance.
- `CONTRIBUTING.md` is contributor documentation.
- `docs/V1-TO-V2-ARCHITECTURE.md` records contributor-facing architectural
  history without defining a supported migration workflow.
- `maintainers/` contains release/operation procedures.
- The immutable `v1-final` tag and Git history preserve the implementation
  baseline. They create no V1 maintenance or compatibility contract.

The main repository must not contain a second copy of Wiki user guides. GitHub
stores Wiki pages in `supernote-module-generator.wiki.git`; update and review
that repository as a separate documentation change, then verify the public Wiki
before linking new pages from the generator.
