# Superseded generator test contracts

The complete base-test disposition for the latest historical replay is in
`Q1-Q2-HISTORICAL-DISPOSITION.md`. It assigns every remaining failed base test
to retained Q1/Q2 safety intent, a specifically retired contract, excluded
template work, or a later Q3/Q4/Q6 gate.

The refocused public generator intentionally removes the following historical
contracts. Their safety intent is retained by the named current tests.

| Superseded contract | Reason | Current coverage |
| --- | --- | --- |
| Exact atime restoration after reads | Read-only commands must not write input metadata; filesystem-induced atime change is allowed. | `test_inventory_never_invokes_metadata_write_callbacks`, `test_observed_directory_does_not_rewrite_access_time`, `test_update_and_validate_never_repair_authored_metadata` |
| Whole-project transaction rollback and old generated-byte restoration | Generated output is disposable; an incomplete marker plus plain `update` is the recovery contract. | `test_input_change_before_completion_leaves_marker_and_survives`, `test_add_activation_interruption_needs_no_transaction_recovery`, `test_process_death_during_publication_is_rerunnable`, `test_zero_feature_obsolete_cleanup_is_rerunnable_after_process_death` |
| Identical-byte write suppression and selective update | Plain `update` always rewrites every current module and shared generated output. | `test_plain_update_rewrites_every_generated_leaf_and_overwrites_edits`, `test_equal_replan_still_carries_complete_output_set_for_rewrite` |
| Generator-managed dependency edits, Gradle wiring, app build and build hook | Authors own package metadata and builds; generator analysis is a bounded standalone KSP build. | `test_parent_package_and_build_files_are_never_planned_for_edit`, `test_jvm_frontend_uses_standalone_generated_analysis_build` |
| Generator `remove`, `repair`, `check`, template enforcement and source-tree deletion | Those commands and mutation authorities are retired. Authors remove source/dependencies; update reconciles only validated generated leaves. | argument retirement matrix, `test_zero_feature_update_removes_owned_runtime_leaves`, `test_unowned_runtime_leaf_survives_generated_reconciliation` |
| Feature-root JS, declarations, README and combined authored/generated manifest | Authored files and disposable generated files now occupy separate paths. | `test_authored_manifest_is_separate_from_rerendered_output`, `test_plan_contains_separate_feature_output_runtime_and_manifest` |
| JSI, SELinux and PluginHost doctor advisory | Doctor is limited to generator-relevant host setup and provenance. | doctor supported-flow tests and retired-advisory assertions |
| r13 refusal whenever Linux lacks `RENAME_NOREPLACE` | The user accepted a narrow eCryptFS compatibility rule: after the stronger operation is genuinely unsupported, fresh `add` creates the exact final module directory exclusively, immediately opens it without following links, writes only the known scaffold through retained descriptors, and uses one deterministic incomplete marker for same-input reruns. Every entry that exists when exclusive mkdir runs is refused. | `test_unsupported_noreplace_uses_descriptor_relative_linux_fallback`, `test_linux_fallback_refuses_a_concurrently_created_empty_directory`, `test_linux_fallback_preserves_every_nonempty_or_nondirectory_destination`, `test_linux_destination_first_interruption_reruns_the_exact_incomplete_add`, `test_linux_incomplete_add_refuses_and_preserves_an_intervening_edit`, `test_linux_public_add_refuses_a_mismatched_incomplete_marker`, `test_linux_public_add_preserves_an_ambiguous_incomplete_scaffold`, `test_linux_public_add_refuses_an_unrelated_incomplete_module` |

## Retained safety contracts

- Author/path validation remains in `tests/test_validation.py`, including
  canonical generation, generated JavaScript syntax, no-follow symlink and
  unreadable-file handling, missing/modified classification, and preservation
  of paths that were never adopted as generated output.
- Same-root serialization remains in `tests/test_operation_lock.py`, including
  public busy errors, JSON error kind/phase, two overlapping public adds,
  cross-process aliases, owner death, and safe refusal while an old journal is
  present.
- Native publication boundaries are in `tests/test_q2_native_boundaries.py`:
  immutable authored inputs, same-size/same-mtime plan conflicts, redirection,
  hardlinks, crash/rerun boundaries, zero-feature cleanup, Windows junctions,
  Windows sharing failures, and a real read-only Windows ACL control.
- Add activation has its own public regressions in
  `tests/test_refocus_generated_contract.py`: a swapped parent cannot receive
  output; strict no-replace remains the primary path; and Linux's eCryptFS-only
  fallback refuses every pre-existing entry, including an empty module
  directory. The fallback publishes the fixed fresh-add scaffold directly into
  an exclusively created destination. Its deterministic incomplete marker lets
  the same unmodified `add` complete after interruption; ambiguous state and
  intervening edits are preserved and refused.

The eCryptFS fallback protects against ordinary concurrent generator use,
pre-existing or peer-created entries observed by exclusive mkdir, symlinks,
redirection, and namespace replacement after a directory descriptor has been
retained. It deliberately does not claim to defeat a hostile same-user process
that replaces a newly created empty directory in the small interval between
successful mkdir and the immediate no-follow open. That adversarial interval is
outside the developer-tool threat model; the opened/named identity check is an
integrity check, not proof that such a replacement was impossible.

## Reduced shipped caller inventory

The console entry point calls `supernote_module_generator.cli:main`. Its public
grammar reaches only `add`, `update`, `validate`, `doctor`, and `help`.

- `add` -> `FeatureCliOperationService.add` ->
  `FeatureOperationService.add` for one authored scaffold -> full
  `GenerationService` plan/execution -> `GeneratedProjectValidator`.
- `update` -> `FeatureCliOperationService.update` -> full `GenerationService`
  plan/execution -> `GeneratedProjectValidator`.
- `validate` -> `FeatureCliOperationService.validate` -> bounded JVM analysis
  when required -> `GeneratedProjectValidator`; it does not publish generated
  output.
- `doctor` -> `DoctorService`; it reports generator host/provenance setup and
  does not launch an app build or mutate source.
- All plugin commands acquire `plugin_operation_lock` and stop on a historical
  journal before reaching mutation.

Q6 removes the unreachable historical `Transaction`, `cli_operations`, template
enforcement, repair/remove planning, build verification, dependency installation,
and app-build helper modules from the shipped package. No supported parser branch
or `FeatureCliOperationService` caller reached them. Reusable low-level filesystem
and publication primitives remain in their current reachable modules and keep
their replacement safety tests.

## Retired tests removed during Q1/Q2

- Q4 replaces cross-file rejection of every unmarked C/C++ declaration with
  marker-first discovery over the ordinary source root. Unmarked wrappers,
  vendor code, tests, and alternate backends are author-owned CMake inputs and
  do not affect export discovery. Duplicate or conflicting declarations in a
  file containing a Supernote marker remain diagnosed, while
  `test_ignores_untagged_declaration_in_another_source` and
  `test_marker_discovery_ignores_unmarked_vendor_and_tests` cover the new
  boundary.
- Q4 removes the remaining generated Gradle KSP/build-check hook. Publisher
  `add`/`update` runs standalone KSP analysis and emits finalized package JVM
  bindings; ordinary author and installed-consumer builds compile those bytes
  without `SUPERNOTE_MODULE_COMMAND`, KSP, or an app-build validation callback.
  The production Gradle and real immutable-consumer tests cover the replacement.

- `test_missing_marker_end_is_a_single_runtime_scoped_issue`: generator-owned
  app Gradle wiring was removed. Generated runtime structure is covered by the
  expected-plan validator and standalone-analysis test.
- `test_build_is_additive_and_diagnostics_are_outside_source_state`,
  `test_build_failure_prioritizes_source_cause_and_preserves_full_log`,
  `test_first_build_restores_protected_directory_metadata_after_cache_creation`,
  `test_successful_gradle_exit_that_mutates_source_fails_build_validation`,
  `test_build_mutation_detector_includes_cache_named_user_source_directory`, and
  `test_build_mutation_detector_rejects_touch_only_source_change`: app builds
  are author-owned and `validate(build=True)` is no longer a public or internal
  generator contract. Read-only validation and author-input coherence remain
  covered independently.
- `test_matching_parent_build_hook_reuses_read_only_check_without_lock_race`
  and `test_parent_build_hook_bypass_rejects_wrong_generation_and_active_journal`:
  the parent build hook was removed. Same-root serialization, busy JSON,
  overlapping public adds, owner death, and old-journal safe-stop remain covered.
- `test_busy_command_does_not_recover_an_active_transaction`: supported commands
  stop before touching an old journal; they never create a live `Transaction`.
  `test_old_pending_journal_is_a_safe_stop_without_recovery` retains that safety
  contract without exercising retired rollback machinery.
- `test_rollback_inventory_accepts_only_probed_timestamp_representation`,
  `test_timestamp_representation_probe_rejects_exact_and_unsupported`,
  `test_rollback_inventory_reports_structural_and_directory_timestamp_changes`,
  and `test_timestamp_representation_probe_rejects_unstable_and_failed_application`:
  these served whole-source rollback and metadata restoration. Current input
  coherence uses read-only inventory and never probes by writing timestamps.
