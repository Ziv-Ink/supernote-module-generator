# ADR 0001: Generated ownership and transactional lifecycle changes

- Status: Accepted
- Scope: Add, Update, Validate, and Remove

## Context

A local module contains both replaceable bridge/build infrastructure and the
plugin developer's implementation. Update must refresh generator output without
silently replacing implementation source. Remove intentionally deletes the
whole package, but dependency or parent-integration failure must not strand the
plugin in a split state.

## Decision

Every managed package records:

- `generated_files`: paths the generator may replace;
- `implementation_roots`: user-owned source preserved by Update;
- module identity/type needed to reproduce generated infrastructure.

Native implementation is Kotlin/Java below the Android source roots, excluding
generated packages. JNI/JSI implementation is the complete
`android/src/main/cpp/` tree. Generated README, metadata, wrappers,
declarations, Gradle/CMake, loaders, bridge, registration, and autolinking are
replaceable.

Update stages a new package, copies preserved implementation into it, verifies
postconditions, and atomically activates it. Remove detaches a package but
retains recoverable source until parent changes, dependency reconciliation, and
postconditions succeed. Mutations journal affected paths; failed or interrupted
work rolls back when possible and exits with a partial/recovery result when not.

The generated README is infrastructure, not user storage. Update does not offer
a dry-run or file-level diff, so the interactive plan and Git status are
advisory and contributors document version control as the independent recovery
path.

## Consequences

- Update can evolve generated infrastructure without owning plugin logic.
- Deleted starter implementation files stay deleted during Update.
- Native `index.d.ts` may be retained until the next successful KSP build;
  users must rebuild after changing exports.
- Successful Remove deletes both ownership classes because the product action
  is removal of the complete local package.
- Metadata/ownership changes require migration tests and release notes.
- Tests must inject failures around staging, activation, dependency work,
  verification, commit, and startup recovery; prose specifications are not a
  substitute for these tests.
