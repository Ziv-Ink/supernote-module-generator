# sn-module-gen 0.1.1

This patch release fixes host-filesystem timestamp handling on filesystems such
as eCryptfs that can accept a nanosecond timestamp request but store a coarser
value.

- Source inventory no longer reports a false concurrent-modification error
  solely because the generator's access-time restoration was rounded.
- Durable transaction cleanup uses and verifies the stable directory
  timestamps applied by the filesystem, allowing a completed recovery pointer
  to be removed.
- Metadata the generator did not write is still compared exactly, including
  inode, type, mode, size, and modification time.

This host-side correction does not change generated APIs, templates, project
schemas, Android integration, or native runtime behavior. The default version
for a newly generated local feature package remains `0.1.0`.

See the [README](https://github.com/Ziv-Ink/supernote-module-generator#readme)
and [Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki) for the
supported workflow and current boundaries.
