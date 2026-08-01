# Choose a module

The CLI keeps its public labels—Native Module, Native JNI Module, and JSI
Module—but these are easier to understand as a Kotlin/Java module, a C/C++
module behind JNI, and an experimental synchronous C++ module.

## Start with the required call model

| Question | If yes | Why |
| --- | --- | --- |
| Do you need Android services, permissions, content resolvers, or another Android API? | **Native** | Kotlin/Java is the supported user-owned layer for Android APIs. |
| Do you already have C/C++ code? | **JNI** unless a synchronous result is essential | JNI keeps the JavaScript API asynchronous and lets the work run without blocking JavaScript. |
| Can the work wait on files, networking, locks, devices, or another unpredictable resource? | **Native** or **JNI** | Promise-based calls avoid a synchronous wait on the JavaScript thread. |
| Does JavaScript genuinely need the result before it can continue? | Consider **JSI** | JSI returns directly, but only if the work is short and the host can load it. |
| Have you verified JSI execution on the exact target PluginHost and enforcing policy? | **JSI may be viable** | Generation and compilation alone do not establish runtime support. |

## Kotlin/Java module (`native`)

Choose Native when Android APIs or a Kotlin/Java library are central to the
feature. The generator discovers annotated public instance methods and creates
the React Native package and bridge.

Returned values become JavaScript promises. A `Unit`/`void` export is
fire-and-forget. A promise does not make a blocking implementation inherently
safe: keep long work off latency-sensitive Android/React Native threads in your
own implementation.

Typical uses:

- Android storage, content resolvers, permissions, or services;
- existing Kotlin/Java libraries;
- a conventional React Native native-module API.

## Kotlin/Java + JNI module (`jni`)

Choose JNI when the implementation belongs in C or C++ and JavaScript can await
the result. You write ordinary top-level C++ exports; generated Kotlin, JNI
conversion/registration, loading, CMake, and TypeScript declarations are
replaceable infrastructure.

This backend does not provide a user-owned Kotlin glue layer for arbitrary
Android APIs. If a feature needs substantial Android API work as well as C++,
use a Kotlin/Java module or design an explicit supported boundary rather than
editing the generated JNI bridge.

Typical uses:

- an existing C/C++ library;
- parsing, compression, image processing, or batched computation;
- file work that should have a Promise-based JavaScript interface.

## Experimental synchronous JSI module (`jsi`)

Choose JSI only when all of these are true:

1. JavaScript needs a synchronous return value.
2. The operation is short, deterministic, and safe on the JavaScript thread.
3. The exact target PluginHost provides the compatible React Native/JSI runtime.
4. The device's linker namespace and SELinux policy permit the extracted plugin
   library to execute.

Do not use JSI for files, networks, waits, locks, large parsing jobs,
compression, or long computation. A blocked JavaScript thread freezes plugin
logic and UI work.

The official Supernote runtime description currently says plugins can use Java
capabilities and then call C/C++, while direct C/C++ calls from JS/TS are not
supported. The generator can create the JSI package, but that is experimental
generator capability rather than official host support. See
[Plugin Principles](https://docs.supernote.com/en/principle) and the local
[compatibility matrix](../reference/compatibility.md).

## Quick decision

```text
Need Android APIs or prefer Kotlin/Java?
  -> Native

Need C/C++ and an asynchronous call is acceptable or the work may block?
  -> JNI

Need a short synchronous C++ call, and target-host execution is proven?
  -> JSI (experimental)
```

The generator cannot convert an existing module between types. If uncertain,
start with Native or JNI; they match the officially described Java/TurboModule
to C/C++ architecture more closely than direct JSI.
