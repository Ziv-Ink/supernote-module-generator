# Final V3 device campaigns

Device: `SN100C10004301`
PluginHost PID: `19801`

- `mixed-soak-results.jsonl` retains one invalid 6m53s attempt caused by the
  host-side unbounded `adb logcat` read, followed by the passing 30m run
  `20260821151557566`. The passing run completed 1,721 mixed C++/JVM async
  waves, preserved identity, and emitted `V3_MIXED_SOAK_PASS`.
- `pending-lifecycle-results.jsonl` retains one invalid pre-scenario attempt
  with the same host-side log-buffer limit, followed by passing run
  `20260821154939409`. Close, application switch, sleep/wake, and bundle
  replacement passed; the replacement case observed zero old-work completion
  markers.
- `final-diagnostics/` is the post-campaign fixture diagnostic snapshot. Its
  V3-specific log lines contain the mixed-soak pass marker, the three
  resolvable lifecycle markers, and the replacement-ready marker. No V3
  failure marker appears.

The harness now reads a bounded recent log window and restores the ordinary
fixture `App.tsx` and bundle in `finally` after each campaign.
