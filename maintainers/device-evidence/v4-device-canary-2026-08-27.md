# V4 device canary — 2026-08-27

This record qualifies the V4 loader and lifecycle implementation represented by
the candidate state below. It is device evidence, not a general compatibility
claim for other firmware or PluginHost versions.

## Candidate identity

- Generator version: `4.0.0`
- Git HEAD: `2e5261a80794b7edd3613b14dd401608bb70aff6`
- Candidate tracked-diff SHA-256:
  `ee7718d73890939a75931d5008bbbde4b82d76b19ee43c54ad8d48ef4a0874ff`
- Candidate porcelain SHA-256:
  `0885e5e05db183992992fb70e592f87f277aa492058c0e4fc54976875bf26428`
- Candidate untracked-content SHA-256:
  `3506ca4a79b6d396b8391789aca774d2abdbd416df37aec66d8b8438ad56ecda`
- Wheel SHA-256:
  `a1c9e7bee4b6970776ea0655b5589ee4fddd06d6d81af72ee2a3777060e13731`
- Source distribution SHA-256:
  `f8722095baefd58b30a244e6cee6069c2146954abcc5772c4a517d948a095fc4`
- Official plugin-template commit:
  `af3f36f6d6f61d9dbd153b0ebb444a3d3621d25f`

## Device identity

- Serial: `SN100C10004301`
- Product/model: Supernote Nomad
- Firmware fingerprint:
  `Supernote/Supernote/Supernote:11/RQ2A.210505.003/eng.supern.20260824.133801:user/release-keys`
- Firmware incremental version: `eng.supern.20260824.133801`
- PluginHost version: `1.00.2608211` (`versionCode=1002608211`)
- ABI list: `arm64-v8a,armeabi-v7a,armeabi`
- SELinux: Enforcing

## Initial scoped canary

The test installed only `HelloWorld` (`wwo8c62drftuszqi`) and wrote only
`/storage/emulated/0/MyStyle/HelloWorld.snplg`. It did not clear PluginHost
data, uninstall another plugin, reboot the device, or delete unrelated files.

The canary exercised:

- a synchronous C++ export and synchronous Kotlin export;
- a C++ async export and Promise completion;
- C++ value conversion;
- C++ native-object construction, property access, method dispatch, and native
  object identity inspection;
- 25 distinct native replacements in one PluginHost process;
- a process restart followed by fresh loading and execution.

Every launch emitted this success payload:

```text
SNV4_CANARY_PASS {"cppPageCount":42,"jvmPageCount":42,"cppStatus":"available","jvmStatus":"available","label":"device-canary","intersects":true,"objectInfo":{"type":"Stroke","originFamily":"cpp"},"asyncBytes":0}
```

## Reload and restart result

- Reload cycles: 25 passed
- Unique semantic generation IDs: 25
- Unique package SHA-256 values: 25
- Same-process PluginHost PID for all reloads: `10699`
- First reload generation:
  `9bd222d953b93064048da6021a4e29d0cbb5a26be7ac995ca05a554f307365fc`
- Final reload generation:
  `f8a2f72fdac15311d0841dfc25729d72f8d5b949837b2fc240374c14666d1118`
- Final installed package SHA-256:
  `b42731278af3b9e0d1899aae9e424539d613b07b2f8cde325cb0a79a3625e71e`
- `SNV4_RESTART_REQUIRED` observations: 0
- V4 canary failures, fatal signals, or V4 loader failures: 0
- PluginHost PID after the authorized force-stop/relaunch boundary: `22473`
- The complete canary payload passed again in PID `22473`.

The cycle ledger SHA-256 is
`5eaae0ed364006f13711218059dd1fdf99069cd05402d26954afe8f7edee3a4d`.
The complete local evidence-tree SHA-256 is
`53234d40ce322d09a3cbc062fa933d2bcda4e5a9d36102e859fa3946b6af2573`.

## Expanded lifecycle qualification

The owner subsequently authorized one bounded continuation using the same
device, plugin identity, and destination. It installed 33 distinct HelloWorld
native packages, exercised one pending native call across replacement, reached
the process generation ceiling, and used exactly two PluginHost force-stop
boundaries: one before generation 1 and one after the expected limit result.
No PluginHost data was cleared; no unrelated plugin was installed, removed, or
modified; and the device was not rebooted.

Generation 1 started a 120-second native call in PluginHost PID `25803`:

```text
1787803116.081 SNV4_PENDING_PROMISE_STARTED generation=1
1787803116.083 SNV4_LONG_ASYNC_NATIVE_START generation=1
```

Generation 2 replaced it while that call was still running. Logical
invalidation began and completed at `1787803206.694`, with no wait for the
untrusted call. Generation 2 then passed the complete mixed C++/Kotlin/value/
object canary at `1787803219.281`. The old physical call ended later at
`1787803236.084` with `SNV4_LONG_ASYNC_NATIVE_END generation=1`, 120.001
seconds after it began and 29.390 seconds after
logical invalidation. Neither raw log contains `SNV4_STALE_COMPLETION` or
`SNV4_STALE_REJECTION`, proving that the stale Promise outcome was suppressed.

After the initial reset, PluginHost automatically loaded one already-installed
V4 plugin before HelloWorld. HelloWorld generations 1 through 31 then loaded
in the same PID, producing exactly 32 process-native registrations. Generation
31 was admitted normally. Generation 32 was the 33rd process-native attempt and
was rejected deterministically without changing PID:

```text
SNV4_RESTART_REQUIRED: native generation limit reached
(count=32, limit=32, loaded=...); restart PluginHost
```

The second and final authorized force-stop changed PluginHost from PID `25803`
to PID `5343`. Generation 33 then installed from package SHA-256
`a1e40a81b7dd59dd9bbf515cd9457eeb0b1e5a535691623a401de705062cbebc`,
loaded normally with reset process accounting, and emitted the complete canary
success payload at `1787822721.854`. The device copy at
`/storage/emulated/0/MyStyle/HelloWorld.snplg` had that exact hash.

Expanded evidence summary:

- distinct HelloWorld package updates prepared and exercised: 33;
- same-process admitted native registrations before the limit: 32 total,
  comprising one automatic pre-existing V4 load and HelloWorld 1 through 31;
- expected `SNV4_RESTART_REQUIRED` observations: 1 at generation 32;
- stale Promise completions or rejections observed: 0;
- PluginHost force-stop/relaunch boundaries: exactly 2;
- final recovery canary failures: 0.

The expanded raw evidence remains in
`/private/tmp/snv4-expanded-canary.cCyeBQ` on the qualification host. Its
principal retained hashes are:

- first raw device log:
  `47e546df54fde6b33ab4bdde7a63caaf0b3d28a576472f9b7a37e77a69a76cdd`;
- resumed raw device log:
  `989e80ab927c22604301ecdb41f1022cff7ff062f2e87788f8a5b514d2659984`;
- package/native hash ledger:
  `9d10309635c9dc5d0c010e7305a43f1d8a29d1b9508652d24b2707b758d78a6f`;
- generation-limit screenshot:
  `ba0bc5999141f5228d89036c98d8aa0b586edb4e4e616e00003e151034ee9e18`;
- post-restart generation-33 screenshot:
  `e3bbfa99563d32595efa07ab68736e9fa60dbd694d86a20757035c8c33bd8254`.

## Device observations outside the V4 result

PluginHost logged its existing
`CatalystInstanceImpl.jniGetHermesHeapSizeBytes()` missing-implementation
warning during repeated React Native startup. After the process restart, an
already-installed V3 plugin also failed to map its own
`supernote-v3-runtime` library from PluginHost code cache. Neither warning
referenced the V4 component `sn_supernote_runtime_872e4e50ce99`; the V4 canary
loaded and passed afterward in the fresh process. These firmware/legacy-plugin
observations are not classified as V4 generator failures.

The device firmware does not expose `su`, so `/proc/<pid>/maps` could not be
used to count retained libraries. Same-process retention is instead evidenced
by the unchanged PID together with 25 unique generation IDs, 25 unique package
hashes, successful normal Plugin Manager installs, and a passing native canary
after every replacement.

## Post-canary launch-only scope note

At `2026-08-27 06:27 IDT`, preparation of the cross-platform official-template
launch harness omitted its fake-ADB environment on the first invocation. The
script therefore used the configured Nomad, opened NOTE, opened the plugin
picker, and tapped the already-installed `HelloWorld` entry once. It performed
no install or package write, did not force-stop or clear PluginHost, did not
uninstall anything, and did not reboot or delete files. No runtime-success
claim is derived from that tap; the corrected harness was immediately rerun
against `FAKE-SERIAL` and emitted the required explicit unverified outcome.
