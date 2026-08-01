# Contributing to Supernote Module Generator

This file is for people changing the generator. Plugin developers should start
with [README.md](README.md) and [docs/README.md](docs/README.md).

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
| CLI grammar | `arguments.py` and tests | `helptext.py`, `docs/reference/cli.md` |
| Exact installed help | `helptext.py` | CLI route tests |
| Generated paths/ownership | generator and `.supernote-module.json` | generated README and export reference |
| Export signatures/types | KSP processor and `binding_codegen.py` | `docs/reference/exports.md` |
| Tool/device support | `docs/reference/compatibility.md` with dated evidence | README summary |
| First-success workflow | `docs/getting-started/first-module.md` | concise README path and generated package links |
| Release procedure | `maintainers/releasing.md` | publish workflow |

Tests should assert observable behavior or a necessary safety invariant. They
must not parse a prose implementation plan solely to make that plan normative.
Exact terminal tests remain appropriate when they protect accessibility,
copyability, stream separation, machine output, keyboard safety, or recovery.

## Test layers and confidence

- Unit tests cover parsing, naming, export scanning/code generation, rendering,
  transactions, metadata migration, and generated file shape.
- Integration tests cover CLI lifecycle behavior against temporary plugin
  roots, parent wiring, source preservation, rollback, and documentation
  artifacts.
- The repository currently lacks an official-template test that performs real
  npm/Yarn linking, compiles all three backends, runs `buildPlugin.sh`, and
  verifies a `.snplg`. Do not describe unit/simulated Gradle tests as that proof.
- Device tests are separate from CI. Record device model, firmware, PluginHost,
  ABI, SELinux mode, commands, and logs; distinguish generated, compiled,
  loaded, and executed outcomes.

When a realistic fixture is added, keep it under `tests/fixtures/` and make at
least one test or documented maintainer command execute it. Do not retain an
unused placeholder fixture.

## Documentation and template checklist

For a change to CLI behavior, generated paths, module APIs, supported versions,
build integration, or device compatibility:

- [ ] Update the authoritative code/constant first.
- [ ] Add or update a behavior test that would fail without the change.
- [ ] Update the root first-success summary only if beginners need the fact.
- [ ] Update the canonical guide/reference page; avoid copying the fact into
      unrelated pages.
- [ ] Update generated README templates only for package-specific guidance.
- [ ] Generate Native, JNI, and JSI samples and scan every generated text file
      for unresolved template values.
- [ ] Confirm the documented source path, default import, `await` behavior, and
      ownership boundary for all affected types.
- [ ] Run every documented shell/PowerShell command that can be exercised in
      the available environment; label anything not run.
- [ ] Check all relative Markdown links and stale moved-file references.
- [ ] Update `docs/reference/compatibility.md` with evidence and date when a
      tool, host, device, or policy claim changes.
- [ ] Add a user-visible note to `CHANGELOG.md` when released behavior changes.
- [ ] Review migration needs for older `.supernote-module.json` schemas,
      generated ownership, declarations, or loaders.

General concepts belong in canonical repository docs. Generated READMEs should
contain actual package names, paths, calls, and ownership; Update replaces them,
so they must not be the only place a workflow is explained.

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

- `README.md` and `docs/` are plugin-developer documentation.
- `CONTRIBUTING.md` is contributor documentation.
- `architecture/decisions/` contains short, current rationale that affects safe
  maintenance.
- `maintainers/` contains release/operation procedures.
- Git history stores superseded audits, implementation plans, and agent prompts.
  Do not keep them in the normal documentation tree unless converted into a
  concise current decision record.
