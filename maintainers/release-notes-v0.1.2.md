# sn-module-gen 0.1.2

This patch release hardens host-filesystem transactions without changing the
generated JavaScript, Android, JNI, or JSI API contracts.

- Files, directories, and symlinks now use the stable timestamp representation
  actually stored by the filesystem. This prevents false
  concurrent-modification failures when a filesystem accepts a nanosecond value
  but stores a coarser timestamp.
- Temporary feature-stage and pre-activation backup names no longer grow with
  the feature or package name. They remain within eCryptFS `NAME_MAX=143` for
  the longest accepted public names.
- A completed rollback is verified against the filesystem's stable coarse-mtime
  representation while content, entry type, mode, size, and other protected
  state remain strict.
- Recovery-copy verification records source device/inode identity and rejects
  atomic source replacement, even if the replacement preserves content, size,
  mode, and timestamps.

Manual qualification used the real installed `sn-module-gen` executable in
fresh disposable projects. Complete Add, Check, Validate, Update, drift/repair,
Remove, rollback/failure, and long-name workflows passed on macOS APFS and Linux
ext4. Focused affected-path workflows passed on Linux eCryptFS, including
generated-state copy/rollback and long stage/backup names; this is not a claim
that every CLI scenario was repeated on eCryptFS.

A disposable C++ feature was also built, packaged, installed through the normal
Supernote UI, and launched on a Supernote Nomad running
`Chauvet.E103.2608241001.2481_beta`. The generated native call produced exactly:

```text
SNMG_NATIVE_RESULT:Hello, SNMG_NATIVE_260905_04301
```

The canary loaded in an already-running PluginHost process. It did not test a
same-plugin reload or a fresh-process lifecycle. Native Windows qualification
was not run for this candidate; Windows-specific filesystem behavior is covered
by automated platform simulations and remains a release-evidence limitation.

See the [README](https://github.com/Ziv-Ink/supernote-module-generator#readme)
and [Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki) for the
supported workflow and current boundaries.
