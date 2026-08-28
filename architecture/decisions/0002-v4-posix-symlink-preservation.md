# ADR 0002: Preserve user-owned source symlinks in V4

- Status: Accepted
- Scope: V4 discovery, staging, update, transaction rollback, and validation
- Audit findings: CB-05, DR-03

## Decision

V4 preserves POSIX file and directory symlinks in user-owned source trees.

- The link is preserved as a link; its target contents are not copied.
- The exact target text returned by `readlink` is retained, including relative,
  absolute, and broken targets.
- Recursive source discovery and preservation do not enter directory symlinks.
- Filesystem classification uses `lstat` semantics, including for broken links.
- Transaction snapshots, hashes, and rollback distinguish regular files,
  directories, and symlinks and preserve their modes where the platform allows.
- Generator-owned write destinations remain lexically contained in the plugin,
  and their parent paths are canonically contained before mutation. A symlinked
  managed parent cannot redirect a write outside the plugin.

Windows support is capability-based. When the environment can create the
required symlink kinds, V4 preserves them. When it cannot, preflight reports a
platform-specific error. V4 never dereferences links as a compatibility
fallback.

## Consequences

User source may deliberately link to content outside a plugin, but generation
does not recursively discover or copy through a directory link. Generator-owned
destinations and parents do not gain that freedom. Tests must cover relative,
absolute, broken, file, and directory links across update, repeated update, and
failure rollback.
