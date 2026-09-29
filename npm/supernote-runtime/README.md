# `@supernote/runtime`

This package is the single shared runtime dependency for generated Supernote
native modules. Declare it directly in a consumer application's `dependencies`.

## Install and build

Declare the shared runtime and every generated module as direct dependencies:

```sh
npm install @supernote/runtime my-file-module
# or: yarn add @supernote/runtime my-file-module
```

Yarn classic works directly. For modern Yarn, configure
`nodeLinker: node-modules` in `.yarnrc.yml`. Yarn Plug'n'Play is not supported.
Import each module as its generated README shows, then run the plugin template's
normal packaging command once:

```sh
./buildPlugin.sh
# Windows: .\buildPlugin.ps1
```

The normal build discovers and compiles declared modules. Do not apply the
runtime Gradle helper manually, and do not run `sn-module-gen` in the consumer.

## How the build works

The versioned discovery contract reads only the consumer's declared direct
dependencies. It does not execute dependency JavaScript and does not mutate
installed packages.

The auto-linked Android runtime includes `cmake/SupernoteModules.cmake` and calls
`supernote_configure_runtime(<runtime-target>)` to compile the package's hashed
native runtime payload, then call
`supernote_link_modules(<runtime-target> <compile-contract-target> <validated-inventory-file>)`.
The helper adds each package's generated stable static target and links it with
CMake's `LINK_ONLY` semantics, so one feature's public compile requirements do
not leak into the runtime or sibling features.

The auto-linked Android runtime applies `gradle/supernote-modules.gradle`. It adds only the
hashed, package-local authored JVM roots, pre-generated registration sources,
and the runtime's source-retained annotation declarations. Neither integration
path runs `sn-module-gen` or KSP in the consumer.
