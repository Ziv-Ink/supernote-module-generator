# npm package distribution contract

Supernote native-module consumers declare one `@supernote/runtime` package and
each module package as direct `dependencies`. They do not list modules in a
second configuration property and do not run `sn-module-gen`, KSP, or a binding
generator.

The current contract has four exact version identities:

- `supernoteNativeModule.schemaVersion`: `2.0`
- generated module `protocol`: `2.0`
- `supernoteNativeRuntime.protocol`: `2.0`
- generated native ABI: `supernote-jsi-v1`

The runtime accepts only those exact versions. There is no version negotiation
or dependency solving in this protocol.

The runtime package has its own `runtime-manifest.json`. That manifest fixes the
exact package-relative native header and source inventory and records a SHA-256
digest for every file. Discovery rejects missing, extra, redirected, or modified
runtime-native files before any consumer build uses them.

## Publisher output

A fresh feature scaffold declares `supernoteNativeModule` in its `package.json`.
Plain `sn-module-gen update` writes the finalized package payload under
`.supernote-generated/`, including JavaScript and TypeScript entrypoints,
semantic and conversion records, native/JVM binding sources, an identity-bound
registration record with callable native and, when applicable, JVM installer
symbols, a package-local CMake target, generated JVM registration,
the exact Kotlin adapter emitted by that publisher KSP run, and
`package-manifest.json`. The adapter is copied byte-for-byte into the generated
JVM source root and covered by the package payload hash. Publisher update performs the standalone
Kotlin/KSP analysis when a feature has JVM source; it does not invoke the
consumer app's Gradle wrapper or build/package the app.

The package manifest records SHA-256 digests for every authored or generated
file used by the consumer. It contains portable package-relative paths only.
The publisher then runs ordinary `npm pack`; no publish or install hook is
required.

## Consumer discovery

`@supernote/runtime` reads the consumer's `package.json` and resolves only its
declared direct dependencies using Node's `node_modules` search paths. A normal
package without `supernoteNativeModule` or `supernoteNativeRuntime` metadata is
ignored. `devDependencies`, peer-only or transitive modules, and undeclared
installed leftovers are never activated.

Package roots may be copied, hoisted, scoped, or valid workspace links. The
resolved package identity must match the declared dependency. Every referenced
payload path is both lexically and physically contained by that package root,
so a child symlink or Windows junction cannot redirect metadata or bindings
outside it. Package entrypoints are not evaluated during discovery, and
`package.json` remains discoverable even when `exports` hides it or `main` is
absent.

Discovery is read-only. It checks package identity, payload hashes, registration
identity, duplicate feature IDs, case-insensitive JavaScript and Android-name
collisions, and the single compatible runtime. Yarn classic and modern Yarn in
`nodeLinker: node-modules` mode use the same contract. Yarn Plug'n'Play is
rejected with an instruction to select `nodeLinker: node-modules`.

## Consumer composition and compilation

React Native auto-linking adds exactly one `@supernote/runtime` Android library.
During Gradle configuration that library invokes its small build adapter once.
The adapter delegates direct-dependency discovery and validation to
`resolve-packages.js`, then writes a feature inventory and plugin-level C++
composition sources into that consumer's Android build directory. It neither
parses feature APIs nor regenerates bindings. Explicit calls to each validated
publisher installer retain the corresponding static archive members and install
the exact feature set into the existing runtime registry and sessions.

The consumer root comes from the Android root project, never from the physical
location of a copied, hoisted, or workspace-linked runtime package. Consequently,
two consumers can share one physical runtime while keeping all registries,
native staging files, and Gradle output separate. Installed runtime and module
packages remain read-only inputs.

Every module package exposes one collision-resistant `STATIC` CMake target
derived from its canonical feature identity. The target has PIC enabled and
compiles the authored sources selected by the preserved author `CMakeLists.txt`
plus every generated native adapter. The author hook receives only
`SUPERNOTE_MODULE_TARGET`; normal `target_sources`, `add_subdirectory`, and
`target_link_libraries` calls remain authoritative.

The runtime's CMake helper first uses
`supernote_configure_runtime(<runtime-target>)` to compile the hashed native
runtime source shipped in the installed `@supernote/runtime` package and expose
only that package's native include/source roots. It then consumes package CMake
files from the adapter's already-validated inventory with
`supernote_link_modules(<runtime-target> <compile-contract-target> <inventory-file>)`.
The latter aggregates module targets with `LINK_ONLY`
under CMake 3.24 and CMP0131 NEW. Feature PUBLIC include directories and compile
definitions therefore reach that feature's authored wrappers and generated
adapters, without leaking into shared runtime sources or sibling modules.
Recursive export discovery does not imply recursive compilation: unmarked
vendor, test, and alternate-backend files compile only if author CMake selects
them.

Static linking is the primary documented dependency workflow. Author CMake may
also attach ordinary header-only `INTERFACE` targets, nested PIC `STATIC`
targets with transitive usage requirements, or explicit `IMPORTED STATIC`
targets to `SUPERNOTE_MODULE_TARGET`. Ordinary source-built and `IMPORTED`
shared targets use the same CMake link relationship; the generator does not
discover, copy, or package arbitrary shared-library runtime files.

Direct host harnesses can prove host-architecture compilation, symbol
resolution, shared-library loading, and deterministic execution. They do not
prove that a shared library was placed in an Android package, is compatible
with an Android ABI, loads in PluginHost, or behaves correctly on a Supernote.
Those packaging, deployment, and device claims require separate later evidence.

The auto-linked runtime's Gradle helper adds hashed authored JVM roots and each package's generated
JVM directory—containing both registration and the publisher-emitted KSP
adapter—to the ordinary Java and Kotlin source sets. It performs no KSP or
binding generation and does not require a consumer-side generator installation.
Both helpers and discovery are read-only over installed
package content. The runtime manifest, runtime native sources, module payloads,
and their permission modes remain byte-for-byte unchanged; all build output
stays under the consumer build tree.
