# V3 focused-plugin test run: 2026-08-21T12-41-03Z

Status: focused device lane and final source/artifact/stress gates complete.

## Checkout

- Branch: `feature/v3-native-objects`
- Base commit: `bb4e4414946de5b3d009b0efda5c7e0473aab862`
- Local V3 implementation and test-fixture changes are intentionally present.
- The earlier combined-plugin result is superseded and contributes no pass to
  this run.

## Fresh host baseline

| Check | Result |
| --- | --- |
| Generator suite | PASS: 571 tests in 14.55 seconds |
| Focused generator validation | PASS: 6 of 6 plugins, one independent feature each |
| Focused TypeScript | PASS: 6 of 6 plugins |
| Focused Jest | PASS: 24 tests across 6 plugins |
| Focused lint | PASS with warnings only: no errors |
| Generated Gradle semantic projection | PASS: 6 of 6 plugins |
| Debug Android/NDK package build | PASS: 6 of 6 plugins |
| Official package verification | PASS: 6 of 6 plugins |

## Verified packages

| Plugin | Plugin ID | SHA-256 | Generated runtime |
| --- | --- | --- | --- |
| `v3-minimal-legacy` | `v3legacyprobe001` | `ba02a02e759cf267673a616cf8c89a3dcf148c3eaba3533f0073a650442c7e62` | `092a75f1d622` |
| `v3-cpp-objects` | `v3cppobject00001` | `5481181b8efd1cd2fc600b324473b0e127ca81cf93957c7ed875ea138d554130` | `b64dcf248c5b` |
| `v3-jvm-objects` | `v3jvmobject00001` | `bd87dcbd706bc5a7b851feb7dbe047384e469f122414ec121a789fef44c4d39a` | `4c3c0cd6e9fb` |
| `v3-cross-family-values` | `v3crossvalue0001` | `81f18251649866a54ec8b6493d75d1feadd1a4c82c8cc56b8b2a40246783e933` | `a08c75047e65` |
| `v3-async-lifecycle` | `v3lifecycle00001` | `228a50cb1c9a7571a6391123d8a680762cf02a69c6e90bbd5048ff0a024ae54b` | `efdbd4c4ca93` |
| `v3-reload-probe` | `v3reloadprobe001` | `cc5c339aa774fbac46bcdacbc405bfb1e6cb0908c082cf887e393d3cefcc84e5` | `7a1b606af789` |

## Device lane

- Device: `SN100C10004301` (`Supernote_Nomad`), connected over USB.
- Device operations are serialized and use each official fixture script.
- Recovery is reserved for an isolated clean-state lane or an observed
  PluginHost crash/restart loop, with diagnostics collected first.
- Clean-start action: device reboot requested at 100% charge. The device briefly
  reappeared during boot and then remained absent from `adb devices` for the
  initial bounded wait window. It later reconnected as the same authorized
  serial, and testing continued without clearing PluginHost data.
- `v3-minimal-legacy`: PASS in fresh PluginHost PID `1346`. Official deploy,
  run, and diagnostics completed; `V3_MINIMAL_LEGACY_SYNC_PASS` and
  `V3_MINIMAL_LEGACY_ASYNC_PASS` were present, with zero PluginHost/runtime
  error-pattern lines. Evidence:
  `evidence/v3-minimal-legacy-20260821T125013Z`.
- `v3-cpp-objects`: PASS in recovered PluginHost PID `2177`. C++ object
  parameters/returns, identity, values, fields, inspection, preflight,
  returned-only objects, nominal rejection, and async identity reached
  `V3_CPP_OBJECT_SYNC_PASS` and `V3_CPP_OBJECT_ASYNC_PASS`; diagnostics found
  zero PluginHost/runtime error-pattern lines. Evidence:
  `evidence/v3-cpp-objects-20260821T125812Z`.
- Recovery before this lane used the official recovery script directly after
  `npm run recover` failed to supply the script's mandatory `--yes`. This is
  recorded as a template npm-wrapper defect and is not counted as a successful
  npm recovery workflow.
- The fixture npm wrappers were then corrected to bind the mandatory recovery
  confirmation flags. `npm run recover` passed end to end before the next lane;
  its first sandboxed attempt was blocked from starting ADB, and the authorized
  retry succeeded without rebooting the tablet.
- `v3-jvm-objects`: PASS in isolated PluginHost PID `3091`. Kotlin and Java
  objects, parameters/returns, identity, fields, records, inspection,
  preflight, nominal rejection, and async identity reached
  `V3_JVM_OBJECT_SYNC_PASS` and `V3_JVM_OBJECT_ASYNC_PASS`; diagnostics found
  zero PluginHost/runtime error-pattern lines. Deployed package SHA-256:
  `c4efcf2b70c65f014e0be6ce51dd139cfc637430dc26286526681ab0c1e95738`.
  Evidence: `evidence/v3-jvm-objects-20260821T130557Z`.
- `v3-cross-family-values`: PASS in isolated PluginHost PID `3605`. Declared
  typed values copied across the internal C++/JVM boundary and the fixture
  reached `V3_CROSS_FAMILY_VALUE_SYNC_PASS` and
  `V3_CROSS_FAMILY_VALUE_ASYNC_PASS`; diagnostics found zero
  PluginHost/runtime error-pattern lines. Deployed package SHA-256:
  `2a3ec9c662f10e6ff251ae7f5fefa1b3ec03faa7f89ecac0788b690670d2890a`.
  Evidence: `evidence/v3-cross-family-values-20260821T130816Z`.
- `v3-async-lifecycle`: PASS in isolated PluginHost PID `4099`. Expected async
  failure handling and native-object retention through accepted async work
  reached `V3_ASYNC_LIFECYCLE_FAILURE_PASS` and
  `V3_ASYNC_LIFECYCLE_RETENTION_PASS`; diagnostics found zero
  PluginHost/runtime error-pattern lines. Deployed package SHA-256:
  `4d105aebd60f1a5bfc73eee8f09acf9e8c4968f6d4bc86bd5be4996f4f87593c`.
  Evidence: `evidence/v3-async-lifecycle-20260821T131032Z`.
- `v3-reload-probe`: PASS in PluginHost PID `5553`. The installed baseline
  emitted `V3_RELOAD_PROBE_PASS bundle=1 native=1`; a JavaScript-only
  `npm run send` then emitted `bundle=2 native=1` without reinstalling the
  native package or changing the PluginHost PID. Both diagnostics scans found
  zero PluginHost/runtime error-pattern lines. Baseline deployed package
  SHA-256: `09823cc017922b4eff7174b2073cc4960fdaf6a55003bf55f409ba4f30a035b1`.
  Evidence: `evidence/v3-reload-probe-baseline-20260821T132532Z` and
  `evidence/v3-reload-probe-replaced-20260821T132637Z`. The fixture source was
  restored to bundle revision 1 and TypeScript passed afterward.
- `v3-full-capability`: PASS in isolated PluginHost PID `6419`. All 15 expected
  phase markers passed across records, weak locking, C++ objects/composites,
  copied cross-family values, JVM GC/objects, safe inspection, C++ async, JVM
  async/failure, and JVM suspend composite/nullable/failure/lifecycle routes.
  The intentionally long suspend route completed after 15.02 seconds in the
  same PID. Both diagnostic captures found zero PluginHost/runtime
  error-pattern lines. Deployed package SHA-256:
  `097d08d46ec15df7460dd7d07f96db6db4b0936c234363667fe73ac21124c193`.
  TypeScript and Jest passed; lint exited zero with 12 warnings. Evidence:
  `evidence/v3-full-capability-20260821T132958Z`.
- `v3-full-capability` minified release: PASS in isolated PluginHost PID
  `7126`. `npm run phase0:release` completed R8/resource minification and
  package verification; `npm run deploy:release` installed that exact package
  without rebuilding debug. All 15 phase markers passed, including the
  intentional 15.02-second suspend route, and diagnostics found zero
  PluginHost/runtime error-pattern lines. Package SHA-256:
  `6f5eb9c2eed8139aa2adb193bd1f42dac1d5cd624ac625b203452eb8221d3671`.
  Evidence: `evidence/v3-full-capability-minified-20260821T133402Z`.
- Clean-room distributions: PASS. A local-source snapshot produced wheel
  SHA-256 `70689f712c6a3811d0a22556185b916446d1bf0dec302cb94d4a7581be7548fb`
  and sdist SHA-256
  `d49fde74abbfaa4c9720a28311b5a9f75e4d699c85f09d9e1340b636bbe3689b`.
  Both installed offline into separate virtual environments, resolved imports
  from their installed `site-packages`, exposed CLI version/help, passed
  `pip check`, and passed all 571 tests with pytest source-path injection
  disabled (wheel 13.68 seconds; sdist 13.95 seconds). The first attempt
  reached 568 passes for each artifact and exposed a harness setup omission:
  three source-inspection tests required a read-only `src/` fixture. That
  attempt is retained rather than overwritten. Evidence:
  `evidence/distributions/clean-room-results.txt` and the retained artifacts.
- Retained host robustness campaigns: PASS. The deterministic C++ parser, JVM
  manifest, semantic graph, nested conversion, generated-kernel, and C++ object
  runtime campaigns—including ASan/UBSan harnesses—passed 155 tests in 4.95
  seconds with no skips. The generated runtime teardown/cancellation contract
  separately passed under ThreadSanitizer in 1.91 seconds. Evidence:
  `evidence/robustness-host-results.txt`.
- Native-object identity/lifetime stress: PASS in unchanged minified PluginHost
  PID `7126`. The device completed 10,000 identity re-exposures per backend,
  1,000 forced JVM GC-retention cycles, 1,000 temporary-object cycles per
  backend, and 64 concurrent async identity operations per backend. Both stress
  markers passed with zero error-pattern lines. Threads changed from 55 to 54;
  PSS changed from 111,866 KB to 127,654 KB and RSS from 219,716 KB to 235,852
  KB. The single memory delta is retained as evidence but is not by itself a
  leak conclusion. The temporary workload was removed and TypeScript/Jest
  passed afterward. Evidence:
  `evidence/v3-identity-lifetime-stress-20260821T134924Z`.
- Twenty-five JavaScript bundle replacements, pre-fix: FUNCTIONAL PASS,
  BOUNDED-LIFETIME FAIL. All 25 unique markers (`bundle=2` through `bundle=26`) executed with
  native generation 1 and unchanged PluginHost PID `8822`; diagnostics found
  zero runtime error-pattern lines. However, threads increased from 55 to 181
  and remained at 181 after 30 seconds idle, including 115 retained
  `mqt_native_modu` threads. RSS fluctuated and ended below its starting sample.
  An ordinary reopen of the already-mounted unchanged bundle did not create a
  new React instance or add threads, tying the retention to bundle replacement
  and new runtime generations. Source inspection then identified the generated
  V3 runtime as the owner: every retained runtime DSO eagerly started a
  four-thread executor and one deferred-destruction thread, exactly matching
  the observed five-thread increment per generation. The first automation
  attempt is retained separately as a
  harness block because `npm run send` correctly updated JavaScript without
  launching it; the corrected runner uses `send` then `run` per cycle and
  restores `App.tsx`. Evidence:
  `evidence/v3-reload-stress-20260821T140705Z`.
- Generated runtime thread-lifecycle fix: PASS on host and device. The executor
  and deferred-destruction services now start lazily, and invalidating the last
  session explicitly shuts down the runtime generation's services. Focused
  regression tests passed 11/11, the related suite passed 99/99, the full
  generator suite passed 571/571 in 13.06 seconds, and the teardown contract
  passed under ThreadSanitizer. A fresh 25-replacement device rerun stayed in
  PluginHost PID `14490`, emitted all 26 expected pass markers and exactly 25
  service-shutdown markers, and produced zero runtime error-pattern lines.
  Threads changed from 39 to 41 at cycle 25 and settled at 40 after idle,
  compared with the pre-fix 55 to 181 result. TypeScript and four Jest tests
  passed after the runner restored revision 1; lint exited zero with three
  existing warnings. Evidence:
  `evidence/v3-reload-thread-fix-20260821T143347Z`.
- Final device lifecycle campaigns: PASS in PluginHost PID `19801`. The
  non-reload 30-minute mixed C++/JVM native-object soak completed 1,721 waves
  with stable threads (50 to 49) and no V3 failure marker. The pending-work
  matrix also passed close, application switch, sleep/wake, and bundle
  replacement. The first attempt for each campaign is retained as an invalid
  host-harness attempt: an unbounded `adb logcat` read exceeded Node's output
  buffer before a V3 result; the bounded-log reruns are the passing evidence.
  Evidence: `evidence/v3-final-device-campaigns-20260821T155301Z`.
- Final generator/artifact gate: PASS. The source suite passed 571/571 in
  14.32 seconds. An isolated exact wheel
  (`2681ba5afe39ef3b6d6e33077e1e04455624a3d223fd16881252d7d21254220b`)
  and sdist
  (`3854e7c86caaafa8ef932dacf60731bbd5132a9a8bb1ce75ed5995c9089bb669`)
  both passed `twine check`, offline installation, CLI version/help, and the
  full 571-test suite while importing from their respective virtual
  environment `site-packages` (14.68 and 14.91 seconds). Evidence:
  `evidence/final-artifacts-20260822`.

## Scope boundary

- This record includes the focused fixture lanes, full generator/artifact
  gates, reload-lifecycle regression, and the final mixed-object and
  pending-work device campaigns described above.
- A broader fresh-process device matrix, beyond the isolated Nomad lanes in
  this record, was not part of this campaign and is not claimed here.
