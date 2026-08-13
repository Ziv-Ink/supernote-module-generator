# V1 to V2 architecture

This document is architectural history for contributors. It explains why V2
code does not preserve several V1 shapes. It is not a converter guide, migration
analyzer, compatibility promise, or supported V1 maintenance policy.

## Same product, deliberate architecture break

V2 remains in the same repository, Python distribution
(`supernote-module-generator`), and CLI command (`supernote-module`). The
immutable `v1-final` tag preserves the exact final V1 development baseline;
`v1.0.0` remains the earlier historical release tag. Mainline V2 development
reuses proven V1 machinery where its behavior still matches the V2 contract.

There are no external V1 projects requiring migration support. Experimental
V1 projects can be updated manually. Do not add an automatic converter,
read-only analyzer, legacy mode, source rewriter, or hidden compatibility
branch unless a real future user need produces a new explicit decision.

## Logical features replace backend-specific modules

V1 asked developers to create Native, Native JNI, or JSI module types. V2 asks
which starter source families to scaffold:

```text
C/C++ (native)
Kotlin/Java (JVM)
```

That selection creates example files only. A logical feature remains
language-neutral and may contain either or both families. Marked source and KSP
manifests determine its actual build and routing requirements.

JSI is the only JavaScript frontend. Kotlin/Java implementations route through
generated native/JNI adapters into the same JSI API rather than creating a
second React Native bridge frontend.

## Source facts, API meaning, and routes are separate

The V2 pipeline is:

```text
language source model
    -> common Supernote semantic model
    -> implementation-specific route/lowering plan
```

The C++ frontend keeps C++ facts. KSP is authoritative for Kotlin/Java compiler
facts and emits a deterministic versioned JVM manifest plus route-neutral JVM
adapters where compiler knowledge is required. The common model contains only
facts with common Supernote meaning; it is not a collection of optional JNI,
C++, or Kotlin backend fields.

## Explicit intent replaces inference

V1 object exports exposed supported public methods automatically. V2 ignores
ordinary code regardless of language visibility. `SupernotePluginExport` publishes a
declaration to JavaScript, `SupernotePluginInternal` creates hidden generated routing,
`SupernotePluginAsync` selects async Supernote semantics, and
`SupernoteConstructor` resolves an otherwise ambiguous construction path.

An exported class publishes its type and automatically uses its one eligible
public constructor as `create(...)`. Every regular method, property-like API,
static API, or special factory still requires explicit intent. There is no V1
automatic-member compatibility mode.

## One compiled runtime per plugin

V1 generated a local React Native/Android package for each module. V2 generates
one plugin-level native build component containing shared runtime services and
all generated feature bindings. Logical features remain independent ownership
units, but they do not compile separate worker pools, JVM services, or runtime
singletons.

Each installed JavaScript runtime gets a generation-identified RuntimeSession;
each feature gets a child FeatureSession. Background work never stores a
`jsi::Runtime*`. It asks the runtime layer to schedule a callback on the JS
thread and receives valid runtime access only if the originating generation is
still alive.

## Async and teardown

Async is explicit API intent, not a Kotlin/C++ implementation inference.
Ordinary blocking implementations use the shared bounded worker executor;
supported Kotlin `suspend` implementations use the generated coroutine route.
Once accepted, both use the same pending-operation, exactly-once completion,
error, cancellation, and teardown lifecycle.

Feature-only teardown rejects pending Promises while the runtime is healthy.
Runtime teardown performs no JSI work and drops later completions. Physical work
is cooperatively cancelled, never forcibly terminated, and teardown never waits
for it on the JS thread.

## Ownership and cleanup

Accepted async object methods retain their receiver until the implementation can
no longer access it. This is a lifetime guarantee, not a concurrency guarantee:
generated code adds no per-object mutex or serial queue, so user state must be
thread-safe when calls can overlap.

Final generated C++ receiver/resource destruction transfers to a managed non-JS
context. Contributors must preserve all parts of that contract:

- no particular cleanup thread, worker, construction thread, or UI thread is
  promised;
- user destruction cannot use JSI or assume a live JavaScript realm;
- physical destruction may occur after logical release;
- feature/runtime teardown does not wait for it;
- specially thread-affine resources remain the implementation's responsibility;
- JNI global references are deleted with a valid attached environment, while
  subsequent Java/Kotlin object collection remains JVM-controlled; and
- final component shutdown cannot unload code while queued or late cleanup can
  still execute.

## What V1 still contributes

V1 remains useful for its parsers, code generation, JSI HostFunction/HostObject
patterns, shared ownership, transactions, diagnostics, build knowledge, KSP/JNI
machinery, tests, and regression history. Reuse those pieces when they satisfy
V2 decisions. Replace behavior that V2 deliberately changed instead of wrapping
it in a compatibility branch.
