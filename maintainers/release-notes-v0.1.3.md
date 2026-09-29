# sn-module-gen 0.1.3

Release date: 2026-09-30.

## User-visible changes

Native modules can be distributed through ordinary npm and Yarn packages.
An author runs `sn-module-gen add`, edits C++/C helper or Kotlin/Java sources,
and runs plain `sn-module-gen update` to produce package-local generated
bindings, TypeScript, documentation, compatibility records, and native payloads.
One feature can contain both language families. The public executable remains
`sn-module-gen`; the build selection variable remains `SUPERNOTE_MODULE_COMMAND`.

The publisher owns generation and compiler/KSP analysis. A consumer installs
the completed module packages and `@supernote/runtime` as direct dependencies;
React Native autolinking and runtime composition collect the installed modules
into one plugin runtime registration. Consumer builds use the packaged output
without invoking the Python generator or consumer KSP. Dependency installation,
lockfiles, app builds, packaging, and publication remain separate author actions.

Plain `update` regenerates every local module; `validate` checks authored and
installed inputs, completion state, generated output, and compatibility without
publishing files. Diagnostics reject stale, incomplete, incompatible, and unsafe
inputs. The generator does not install packages, build the app, or operate the
tablet. Selective update, generator remove, and legacy template-sync commands
are not part of this contract. Authored source remains user-owned.

The supported distribution route installs module tarballs as copied packages.
npm, Yarn classic, and modern Yarn using `nodeLinker: node-modules` are supported;
Yarn Plug'n'Play and external linked author sources are unsupported. A supported
shared-runtime link is not evidence for external linked author-source support.
See [the npm package contract](../docs/npm-package-contract.md) and
[README](../README.md) for the current public workflow.

## Accepted implementation evidence and scope

The independently reviewed implementation candidate preceded the 0.1.3 version
and release-document edits. Its source base was
`9f2ddc21e8c9a94081f4320b0b4e24c2c91fb38b` plus the frozen 306-entry candidate;
dirty identity SHA-256:
`1f3e1b586e754bd3a306e280f8958b1ce627ce034c4406171ee437e5b40a7538`.
These results are dated implementation evidence, **not exact 0.1.3 artifact or
hosted-CI qualification**. The release commit must pass its own source/package
checks and the unchanged release workflow before publication.

- Native Linux complete-suite and Linux/macOS/Windows platform coverage inputs
  were independently combined: coverage.py combined score **82.49%**, above the
  unchanged **82.03%** floor. Statement coverage was 85.10%; branch coverage was
  75.31%. The complete Linux run had 1293 passed and 30 skipped; platform subsets
  had 31 passed/7 skipped on Mac and Linux, 33 passed/5 skipped on native Windows.
  This does not qualify Windows Android builds or turn skips into passes.
- The accepted relative Bash checkpoint covered Mac Flashcards, Linux Dashboard,
  and Linux Flashcards: 12 positive clean/incremental builds, six original versus
  candidate package comparisons, and six incremental payload identities. The
  accepted first-clean ordering experiment showed generated runtime registration
  collected after Gradle rather than an empty pre-build list. This is evidence
  for the separate ordering-only template patch, not a template change shipped
  in this Python release.
- A Linux candidate package passed TypeScript, the complete native build, and
  strict package inspection: one generated runtime registration, ARM64 runtime
  and registration libraries, DEX definitions, and protected package preservation.
  Package SHA-256:
  `1fb9f170d22653be94708a03885a520b16ce726f0dc22ed50e9acab9cbf972bf`.
- On a Supernote Nomad, Android 11, firmware
  `Chauvet.E103.2609111001.2505_beta`, PluginHost `1.00.26009090`, and SELinux
  Enforcing, the npm-installed copied C++ module returned **F7API2** through its
  generated binding. The actual return was both visible and logged; the bundled
  JavaScript contained no literal success value. One native invocation is proven.
  Closing and reopening showed the result again, but there was no second native
  return marker: a second invocation, remount, teardown, or runtime reinitialization
  is not proven. Retained React state is only a possible explanation.

The final independent verdict for the shortened relative goal was **PASS WITH
RESIDUAL RISKS**. No broader host/community/device lifecycle matrix is implied.

## Residual limitations and release boundary

- PowerShell is unchanged; Windows Android is not qualified under the shortened
  scope. Native Windows platform tests are a separate, narrower claim.
- Original and candidate Bash scripts share stale-output and failure-masking
  limitations. Shared failures were nonblocking under the explicit relative
  acceptance rule, not proof of absolute fail-closed packaging.
- The Mac Dashboard row lacked JDK 21 and remains unverified, not passing.
- External linked author sources remain unsupported. Use copied package installs.
- The one device invocation does not qualify broad lifecycle, changed native
  generations, all firmware, or all PluginHost versions.
- Template and Wiki are separate repositories and are not published with this
  generator commit. At preparation, the Wiki still describes 0.1.1 and retired
  selective-update/remove workflows. Its public contract must be reconciled
  separately before publication as required by the release procedure; this
  release preparation does not claim that Wiki work is complete.

Historical 0.1.2 notes and device records remain unchanged for provenance. They
do not establish results for the new release artifacts.
