# Final generator and artifact verification

Source suite:

- `python3 -m pytest -q`: 571 passed in 14.32 seconds.

Exact isolated artifacts:

| Artifact | SHA-256 | Offline installed suite |
| --- | --- | --- |
| `supernote_module_generator-3.0.0.dev0-py3-none-any.whl` | `2681ba5afe39ef3b6d6e33077e1e04455624a3d223fd16881252d7d21254220b` | 571 passed in 14.68 seconds |
| `supernote-module-generator-3.0.0.dev0.tar.gz` | `3854e7c86caaafa8ef932dacf60731bbd5132a9a8bb1ce75ed5995c9089bb669` | 571 passed in 14.91 seconds |

Both artifacts were built into an initially empty isolated directory using the
project's setuptools PEP 517 backend. `twine check` accepted both. Each was
installed without network access or dependencies, reported `supernote-module
3.0.0.dev0`, and imported from its own virtual environment's `site-packages`.
The sdist environment used the locally installed `wheel` build dependency via
system site packages because a plain new Python 3.9 venv omits that standard
build dependency; the generated package itself was installed from the exact
sdist and took precedence over the host's unrelated V2 installation.

Archive checks confirmed the V3 runtime/object generator sources, object/value
annotation templates, JSI module template, console-script entry point, and
runtime regression tests are present in the relevant artifacts.
