# V4 architecture

This document summarizes the V4 architecture for contributors. V1, V2, and V3
generated layouts are unsupported; V4 rejects recognizable legacy state before
mutation and provides no converter, migrator, compatibility mode, or downgrade.

## Deliberate clean V4 boundary

V4 uses the existing Python distribution (`supernote-module-generator`) and CLI
command (`supernote-module`). Historical tags preserve earlier development
baselines, but no prior implementation, schema, template, generated layout, or
runtime contract remains active solely for compatibility. Create or regenerate
a clean V4 project when legacy state is detected.

## Logical features replace backend-specific modules

Earlier generators asked developers to create backend-specific module types. V4 asks
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

The V4 pipeline is:

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

V4 represents declared native instances as nominal, runtime-local JavaScript
objects with stable identity, automatic lifetime management, and live marked
fields. Declared value types are validated copied data. Arbitrary JavaScript
object graphs are not accepted.

V4 ignores ordinary code regardless of language visibility.
`SupernotePluginExport` publishes a declaration to JavaScript,
`SupernotePluginInternal` creates hidden generated routing,
`SupernotePluginAsync` selects async Supernote semantics, and
`SupernoteConstructor` resolves an otherwise ambiguous construction path.

A marked object publishes its nominal type. Construction, every method, field,
static API, and factory still require explicit intent. Returned-only object
types are valid. There is no automatic-member compatibility mode.

## One generated runtime per plugin

V4 generates one plugin-level native build component containing shared runtime services and
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

Worker and deferred-destruction services are bounded process-lifetime
infrastructure and start lazily. Invalidating a runtime session atomically marks
it inactive, rejects new calls, suppresses stale completions, detaches its
JavaScript ownership, and queues physical cleanup. The invalidating thread does
not join arbitrary user work or wait indefinitely. Receivers and other native
resources remain retained until physical work can no longer access them.

If bounded cleanup infrastructure cannot accept ownership, invalidation retains
the native session mapping and returns the stable restart-required diagnostic;
it never destroys user resources synchronously on the invalidating thread. This
cleanup does not depend on a native-library static destructor because PluginHost
may retain loaded generations in one process.

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
tests, and regression history remain useful only when they satisfy V4 decisions.

## Static contract boundaries

The checked-in Ruff gate applies Python correctness checks to all active source
and tests. The gradual mypy boundary covers the public CLI grammar and canonical
V4 identity, semantic IR, artifact plan, integrity manifest, template
capability, transaction, and command-result models. Those lower-level contracts must not import terminal
rendering, Doctor, or operation orchestration. CI runs both gates on every
supported Python version.

Artifact planning is split into independently typed phases for artifact
identity, dependency edits, wiring edits, tree-removal authority, stale-file
comparison, and execution preconditions. A dedicated CI complexity check keeps
that decision module below the checked-in McCabe ceiling.

The public command grammar is likewise separated into command discovery, token
collection, positional policy, output-mode policy, and value validation. Its
own complexity ratchet keeps grammar changes reviewable without coupling the
parser to terminal presentation or command execution.

Integrity-manifest loading separates header and record-list validation, feature,
artifact, and wiring record parsing, ownership-anchor coherence, and descriptor-
bound live ownership verification. Its complexity ratchet covers the complete
module so destructive authority cannot accumulate inside one branch-ordered
parser or filesystem-validation function.

The recursive semantic-type contract separates immutable node-shape validation
from manifest object/kind/field parsing and scalar, named-reference, or wrapper
payload construction. The same typed and complexity-ratcheted module is shared
by both language frontends, so backend source spellings never enter this layer.

C/C++ discovery begins with a separate minimal lexer that records identifiers,
punctuation, string extents, real line comments, preprocessor directives,
conditional depth, brace depth, decoded-source string indices, and source lines.
The `start` and `end` fields are Unicode code-point positions that slice the
original decoded Python source string; they are not encoded-byte offsets. Its
handlers for directives, comments, raw/quoted strings, identifiers, and
punctuation are typed and complexity-ratcheted independently from declaration
semantics.

C/C++ source-family routing is a separate typed decision before declaration
lowering. It classifies implementation, header, ordinary C23, helper, and
ignored suffixes; identifies the generated `JNI_OnLoad` ownership boundary;
and supplies the exact marker rejection policy. The binding frontend retains
filesystem traversal, descriptor reads, source-context formatting, and the
ordered header/source parser handoff. The routing module is independently typed
and complexity-ratcheted.

C/C++ declaration intent is a separate typed phase over that lexical model. It
recognizes the exact V4 marker grammar, groups adjacent marker stacks, validates
marker combinations and source locations, and resolves the named namespace at
each declaration. Source-path and module formatting remain in the binding
frontend, so this low-level parser has no command, rendering, or filesystem
mutation dependency. Its own complexity ratchet covers the complete module.

Class-body token segmentation is a following low-level phase. It separates
top-level member declarations, tracks `public`/`protected`/`private` labels,
splits parameter lists only at their active parenthesis depth, and skips nested
inline bodies without attempting semantic lowering. Access policy, supported
types, and diagnostics remain the responsibility of the consuming declaration
parser. This phase is independently typed and complexity-ratcheted.

Member suffix decisions follow segmentation without acquiring source-path or
rendering dependencies. This typed phase validates canonical const/noexcept and
default/delete suffixes and classifies the supported unqualified copy/move
constructor spellings. It returns source-line failures to a binding-layer
adapter, which retains the public module, export, path, and CodegenError format.

Generated class-member shapes are decided separately from marker intent and IR
construction. This typed phase classifies constructor, destructor, and method
heads; decomposes stored-value fields; and validates method result/name/static
structure. It preserves marker-relative field diagnostics and source-relative
method diagnostics while leaving access, overload, provenance, and capability
policy in the binding layer.

Stored-member routing is decided before field lowering. The same typed phase
distinguishes callable declarations, marked fields, and ignorable unmarked
storage, while requiring every non-static stored member of a generated value
class to carry an export marker. Marker-stack intent, access checks, detailed
field-shape validation, provenance, and IR construction retain their original
binding-layer order.

Constructor routing is likewise a typed member-shape decision. After balanced
parameter grouping and callable-head classification, it distinguishes ordinary
constructors, unsupported prefixes, and canonical copy/move constructors while
retaining the marked-versus-unmarked policy. The binding adapter keeps the
authoritative lowering order: marker intent and access, parameter validation,
constructor suffix or member-initializer-list validation, deleted-selection
rejection, duplicate-signature detection, provenance, then source-model
construction. Header-only constructors and out-of-line constructor declarations
therefore share one semantic route.

Method lowering uses the same bounded binding adaptation over the typed member
shape and qualifier decisions. It preserves destructor rejection, marker intent,
public access, structural method validation, duplicate-name detection, parameter
validation, qualifier validation, provenance, and source-model construction in
that order. Untagged non-constructor callables remain outside generated method
lowering.

Class member accumulation is a bounded binding orchestration over those typed
decisions. It dispatches segmented declarations through stored-field, callable,
constructor, and marked-method paths; preserves missing-parenthesis handling;
tracks duplicate constructor and method identities; and adds an implicit public
constructor only when no user constructor declaration was observed. The
individual structural and semantic decisions remain in their typed modules.

JSI registration mode is a separate immutable decision. It validates the
optional canonical feature identity, routes sync and feature-scoped async
exports, and admits promise helpers only when a feature route actually needs
them. The binding frontend retains diagnostics adaptation and byte rendering.
Both the decision module and the complete binding frontend are typed or
complexity-ratcheted at their appropriate boundary.

The containing class-definition envelope is a separate typed decision. It
validates the ordinary class or struct name, rejects inheritance and unsupported
pre-body tokens, finds the balanced outer body despite nested inline bodies, and
requires the closing semicolon. Marker adjacency, namespace placement, default
member access, source-path diagnostics, and semantic IR assembly stay in the
binding frontend.

The binding frontend derives one immutable parse context before applying that
envelope. Its following-token snapshot is an immutable tuple rather than a
mutable alias of the frontend token list. Marked and unmarked owner paths share
namespace discovery, but retain their distinct marker-location,
declaration-prefix, owner-depth, following-token, intent, and diagnostic-line
rules. This keeps source-context adaptation bounded without moving filesystem or
semantic ownership into the token layer.

Class-member marker routing belongs to the typed declaration phase. It validates
each stack at the class-member brace depth, selects the following segmented
declaration, requires whitespace-only adjacency, prevents multiple stacks from
claiming one declaration, and returns the complete consumed-marker set. The
binding frontend supplies file/module context and continues to own member
lowering and IR construction.

Class-owner discovery uses the same typed declaration contract. It classifies
each marker stack as a marked class, enum, or ignorable nested member marker;
rejects nested marked types; selects unmarked containing classes for remaining
member markers; and reports the first unclaimed marker. Filesystem traversal,
source-path diagnostics, class parsing, and IR assembly remain in the binding
frontend.

Free-function result/name, parameter segmentation, and post-parameter
boundaries use a corresponding typed decision phase. It validates ordinary
marker-to-declaration spacing and directive boundaries, external-linkage
prefixes, owned result spelling, and the C++23 name policy. It splits parameters
only at their active parenthesis depth, normalizes an empty list, and validates
bare `noexcept` plus the required body opening. Marker-stack intent, parameter
lowering, source-path diagnostics, and semantic IR assembly remain in the
binding frontend.

Untagged global-function detection is a separate typed token decision. It
matches balanced parameters, optional `noexcept` expressions, trailing return
types, declaration or definition terminators at the active namespace depth,
and filters call/assignment/member-access shapes before the frontend compares
the candidate with tagged ownership. The detector is independently typed and
complexity-ratcheted; source-path and tagged-definition diagnostics remain in
the binding frontend.

The active class path is the versioned declaration/member pipeline described
above. There is no parallel legacy object parser behind the public scanner:
the obsolete disconnected object-export/member parser was removed after its
call graph was proven unreachable. V1, V2, and V3 layouts are rejected at the
project boundary rather than kept alive as alternate source parsers.

C++ type-token normalization and named-parameter validation are another shared
typed decision phase. Free functions, object members, constructors, and methods
use the same structural type-spelling rules, forbidden parameter-form
precedence, C++23 keyword policy, argument ordering, and duplicate-name rule.
This low-level phase returns normalized list syntax or a source-line failure;
the binding adapter adds module, export, and
path context without redefining the decision.

The typing file list is a ratchet, not a claim that unlisted orchestration and
code-generation modules are already fully typed. Contributors should make a
new boundary clean before adding it and must not remove a covered contract to
work around a type error.

## Release qualification boundary

Every V4 integrity manifest records the required official-template capability.
`template status` compares the live Bash/PowerShell launch scripts without
mutation and reports current, drifted, or missing state. `template sync`
previews by default and, only with `--yes`, transactionally applies a recognized
official-template baseline. Unknown drift and unsafe or missing entries fail
closed.

A release is an exact-commit product, not a version string attached to an
unrelated green run. The reusable quality workflow verifies the checked-out
`github.sha`, runs the supported-Python test/static/coverage matrix, and checks
native path, lock, symlink-capability, spaces, Unicode, long-path, Bash, and
PowerShell boundaries on
Ubuntu, macOS, and Windows. It builds and installs the wheel and source
distribution, then uses that wheel to generate realistic disposable plugins
from a remotely reachable pinned official template commit. The generator-owned
template sync is applied before launch qualification. The Wiki candidate and
the exact `file_reader_test` revision are cloned from checked-in immutable Git
bundles, so a fresh runner never depends on an unpublished local object. Every
root-README and Wiki CLI example is source-inventoried, grammar-checked, and
classified; placeholders and environment/device/build commands retain an
explicit non-runnable reason and are covered by their corresponding fixture.
The root-README and pinned-Wiki
fixtures must be true no-ops on their second update, pass JavaScript and
TypeScript checks, compile Gradle/KSP/Kotlin/CMake/JNI/JSI through the read-only
build hook, and pass the official plugin package verifier. The template launch
contract is exercised against fake ADB and must never convert a UI tap into an
unverified runtime-success claim. PyPI publication consumes the SHA-named
artifact from that qualification run and does not rebuild it.

The pinned real-project boundary additionally materializes one disposable NOTE
fixture and one disposable DOC fixture from `file_reader_test`. Both compile the
same 15 source-backed generated/native/JVM/host-API checks, but retain distinct
host contexts and permission outcomes. The device tier may install only those
named packages, create only their dedicated NOTE/PDF documents, and record the
real permission request/result interaction. Retained device logs are validated
again against the checked-in 15-check manifest by the pinned real-project gate;
they cannot be replaced by a free-form success claim. This bounded pack is
deliberately separate from the larger plugin-matrix program.

Device evidence is a separate, explicit tier. Loader or lifecycle changes need
an approved PluginHost canary before release; a host build never labels itself
device-tested.
