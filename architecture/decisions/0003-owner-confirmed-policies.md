# ADR 0003: Owner-confirmed public policies

- Status: Accepted
- Scope: Generation ownership, updates, runtime lifecycle, legacy rejection, and source control

## Generation ownership

The installed external `sn-module-gen` tool is the sole owner of committed
generated source. Gradle and KSP may write intermediate semantic data only below
build or staging directories. Builds perform a read-only external state check
before compilation and never copy the Python generator into a plugin.

## Targeted updates

A targeted update computes and atomically applies the complete transitive
affected closure. Human, JSON, dry-run, and diff output are projections of the
same `GenerationPlan` and distinguish requested from additionally affected
targets. An already canonical project is a true no-op.

## Runtime invalidation

Logical invalidation is immediate and nonblocking. New calls are rejected and
stale promise completions are suppressed. Physical cleanup uses bounded
process-lifetime infrastructure. Native-generation accounting is monotonic with
a hard same-process ceiling of 32. Capacity exhaustion returns a stable,
actionable restart-required result; process restart resets the accounting.

## Legacy projects

Pre-public V1 through V4 layouts are unsupported and are rejected before
mutation. The public generator does not provide migration, downgrade,
compatibility aliases, or partial reinterpretation. The diagnostic directs
users to create a clean 0.1.0 project and copy only reviewed user-owned source
files.

## Generated output in source control

Canonical generated output remains checked in and inspectable. The public
integrity manifest and external generator are authoritative. Generated files
are clearly marked and fully content-validated; compilation neither regenerates
them nor embeds the generator.

The separately accepted POSIX/Windows symlink policy remains defined by ADR
0002.
