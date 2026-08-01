# Supernote plugin module documentation

These pages are for developers adding Kotlin/Java, JNI C/C++, or experimental
JSI functionality to a Supernote React Native plugin.

Start with the root [README](../README.md) for the shortest Kotlin success, or
use the task index below.

| I need to... | Read... |
| --- | --- |
| Generate, implement, package, install, and test a first module | [Build your first module](getting-started/first-module.md) |
| Decide between Kotlin/Java, JNI, and JSI | [Choose a module](guides/choosing-a-module.md) |
| Update, validate, recover, or remove a module | [Manage modules safely](guides/managing-modules.md) |
| Check supported signatures, editable paths, and generated files | [Exports and ownership](reference/exports.md) |
| Understand names, commands, output modes, and automation | [CLI reference](reference/cli.md) |
| Check tools, versions, operating systems, devices, and JSI status | [Compatibility matrix](reference/compatibility.md) |
| Diagnose a concrete failure | [Troubleshooting by symptom](troubleshooting/README.md) |

Generated module READMEs contain the exact names and paths for one package. They
supplement these guides and are replaced by `update`; they are not the canonical
source for general workflow or compatibility information.

Documentation for changing or publishing the Python generator is not part of
this user path. Contributors should use [CONTRIBUTING.md](../CONTRIBUTING.md),
and release maintainers should use [maintainers/releasing.md](../maintainers/releasing.md).
