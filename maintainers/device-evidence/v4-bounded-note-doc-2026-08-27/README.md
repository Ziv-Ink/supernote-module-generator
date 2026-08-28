# V4 bounded NOTE/DOC acceptance — 2026-08-27

This record covers the owner-authorized final `file_reader_test` integration
pack only. It is not the separate 100-plugin matrix.

## Device and scope

- Device: Supernote Nomad `SN100C10004301`
- Firmware: `Chauvet.E103.2608241001.2481_beta`
- Pinned `file_reader_test`: `9f626ed39be82b43ff74eb735d10b7de61f51508`
- Generator HEAD: `2e5261a80794b7edd3613b14dd401608bb70aff6`
- Fixture definitions: `ci/device_acceptance/cases.json` (15 checks)
- NOTE plugin: `snv4-note-acceptance-finala`, ID `8eb47c9c5590a15e`
- DOC plugin: `snv4-doc-acceptance-finala`, ID `73cfc9e1f5eae24b`

Only the two named disposable plugins and these files were changed or opened:

- `/storage/emulated/0/MyStyle/snv4-note-acceptance-finala.snplg`
- `/storage/emulated/0/MyStyle/snv4-doc-acceptance-finala.snplg`
- `/storage/emulated/0/Note/SNV4_Bounded_Acceptance/SNV4_Bounded_NOTE.note`
- `/storage/emulated/0/Document/SNV4_Bounded_Acceptance.pdf`

The two fixtures inherit the same official-template Android application
identity, so they were installed and exercised sequentially. No unrelated
plugin was uninstalled or modified. PluginHost data was not cleared, the
device was not rebooted, and no ordinary NOTE or DOC file was edited.

## Build and package identity

Both fixtures passed lint, `tsc --noEmit`, the external read-only V4 state
check, Gradle/KSP/Kotlin/CMake/JNI/JSI compilation, `BUILD SUCCESSFUL`, and the
official package verifier.

- NOTE `.snplg`: `2acbf62d5b2befacd0ff89c41614d00e1495276e86cf02d95fd76f70a1a89faa`
- DOC `.snplg`: `e7e31c2930ce8d1c5009ad42493e498b95241cb0e1c258b9cbe1d59bd2c2b06b`
- DOC PDF: `b7a906e9fbbe3583e2d21a27c3e83abcf44e03c7d4c92244b63993ed56c17ca3`
- NOTE file after the run: `42ef41b28a26b99a82d3451de3ed288a64cf93c1c0008a747d30ab28fe5ec16e`

## Results

Both contexts passed all 15 source-backed checks in the same declared order:

1. C++ scalar
2. C++ string/bool
3. C++ async
4. generated C++ object lifecycle/value identity
5. Kotlin/JVM string
6. safe Android build information
7. Kotlin coroutine/async
8. mixed C++ to Kotlin/JVM call
9. PluginManager plugin name
10. PluginManager private directory
11. PluginManager device type
12. common current-file API
13. common page/handwriting API
14. NOTE or DOC host-specific API
15. permission status/request/result

NOTE ran in the exact dedicated `.note` file. The real FILE:WRITE prompt was
answered **Don't Allow** and produced `before=0`, `requested=0`, `after=0`.
`PluginNoteAPI.saveCurrentNote()` succeeded without changing the NOTE file
after the final evidence hash was captured.

DOC ran in the exact one-page dedicated PDF. The real FILE:READ prompt was
answered **Allow This Time Only** and produced `before=0`, `requested=1`,
`after=1`. `PluginDocAPI.getCurrentTotalPages()` returned `1`.

The normalized results are `note-evidence.json` and `doc-evidence.json`; the
raw ReactNativeJS marker logs are retained beside them. Permission-dialog,
exact-context, and exact-plugin-menu screenshots/XML are retained in this
directory. The validator that turns raw logs into the normalized result is
`ci/device_acceptance/evidence.py`.

## Final device state

The DOC acceptance plugin remains installed, the NOTE acceptance plugin is not
installed, both verified `.snplg` files remain in `MyStyle`, and the dedicated
PDF remains open in the DOC context. This state is intentionally confined to
the named disposable acceptance scope.
