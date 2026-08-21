# V3 architecture

This document summarizes the V3 architecture for contributors. It is not a V2
converter guide, migration analyzer, compatibility promise, or supported V2
maintenance policy.

## Deliberate architecture break

V3 remains in the same repository, Python distribution
(`supernote-module-generator`), and CLI command (`supernote-module`). The
historical tags preserve earlier development baselines. Mainline V3 development
reuses proven machinery only where its behavior still matches the V3 contract.

There are no V2 users requiring migration support. Experimental projects can be
updated manually. Do not add an automatic converter,
read-only analyzer, legacy mode, source rewriter, or hidden compatibility
branch unless a real future user need produces a new explicit decision.

## Logical features replace backend-specific modules

Earlier generators asked developers to create backend-specific module types. V3 asks
which starter source families to scaffold:

```text
C/C++ (native)
Kotlin/Java (JVM)
```

That selection creates starter files only. A logical feature remains
language-neutral and may contain either or both families. Marked source and KSP
manifests determine its actual build and routing requirements.

JSI is the only JavaScript frontend. Kotlin/Java implementations route through
generated native/JNI adapters into the same JSI API rather than creating a
second React Native bridge frontend.

## Source facts, API meaning, and routes are separate

The V3 pipeline is:

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

## First-class objects and explicit intent

V3 represents declared native instances as nominal, runtime-local JavaScript
objects with stable identity, automatic lifetime management, and live marked
fields. Declared value types are validated copied data. Arbitrary JavaScript
object graphs are not accepted.

V3 ignores ordinary code regardless of language visibility.
`SupernotePluginExport` publishes a declaration to JavaScript,
`SupernotePluginInternal` creates hidden generated routing,
`SupernotePluginAsync` selects async Supernote semantics, and
`SupernoteConstructor` resolves an otherwise ambiguous construction path.

A marked object publishes its nominal type. Construction, every method, field,
static API, and factory still require explicit intent. Returned-only object
types are valid. There is no automatic-member compatibility mode.

## One generated runtime per plugin

V3 generates one plugin-level native build component containing shared runtime services and
all generated feature bindings. Logical features remain independent ownership
units, but they do not compile separate worker pools, JVM services, or runtime
singletons.

Each installed JavaScript runtime gets a generation-identified RuntimeSession;
each feature gets a child FeatureSession. Background work never stores a
`jsi::Runtime*`. It asks the runtime layer to schedule a callback on the JS
thread and receives valid runtime access only if the originating generation is
still alive.

Plugin replacement loads a uniquely named copy of the generated bindings and
performs an explicit native/JVM generation-identity handshake before JNI
registration. A stale or mismatched publication fails closed. Dependency lookup
uses one process-global SoLoader source per generated plugin component; native
generations retained by SoLoader are capped at 32 per PluginHost process. The
33rd load fails with a restart instruction instead of growing process state
without a bound. This leaves room for the required 25-cycle reload stress while
making the operational limit explicit.

## Async and teardown

Async is explicit API intent, not a Kotlin/C++ implementation inference.
Ordinary blocking implementations use the shared bounded worker executor;
supported Kotlin `suspend` implementations use the generated coroutine route.
Once accepted, both use the same pending-operation, exactly-once completion,
error, cancellation, and teardown lifecycle.

Worker and deferred-destruction services start lazily. When the final session
for one generated runtime generation is invalidated, that generation explicitly
stops and joins its workers, clears pending JVM completions, and drains cleanup.
This cleanup does not depend on the native library's static destructor because
PluginHost may retain loaded generations in one process.

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

## Language-family boundary

Current native-object routes remain within one implementation family: C++
objects go to C++ and Kotlin/Java objects stay on the JVM. Declared copied
values may cross generated internal C++/JVM routes. Cross-family object proxies
are deferred without changing the public JavaScript or TypeScript model.

Earlier parsers, code generation, JSI HostFunction/HostObject patterns, shared
ownership, transactions, diagnostics, build knowledge, KSP/JNI machinery,
tests, and regression history remain useful only when they satisfy V3 decisions.
