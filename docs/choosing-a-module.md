# Choosing a module

All three module types expose a local package to JavaScript or TypeScript. The
important difference is where you write the implementation and whether the
JavaScript call is asynchronous or synchronous.

| Module type | Implementation | JavaScript call | Use it for |
| --- | --- | --- | --- |
| **Native Module** | Kotlin/Java | Promise for value returns | For coding in Kotlin/Java and/or using Android APIs. |
| **Native JNI Module** | C++ exports, with optional C helpers | Promise for value returns | For combining Android APIs with existing or performance-intensive C/C++ code. |
| **JSI Module** | C++ exports, with optional C helpers | Synchronous return | For low-latency synchronous calls from JavaScript. |

## Native Module

Choose Native when Kotlin or Java is the natural implementation language, or
when the code needs Android services, permissions, content resolvers, or other
Android APIs. Exported classes can receive a supported Android Context through
their constructor.

Value-returning methods resolve or reject a JavaScript promise. `Unit`/`void`
methods are fire-and-forget.

Typical uses:

- Android platform integrations.
- Kotlin/Java libraries.
- Work that already has a natural React Native Native Module interface.

## Native JNI Module

Choose JNI when JavaScript should use an asynchronous Native Module interface
but the implementation belongs in C or C++. The generated Kotlin bridge crosses
JNI and performs the supported value conversions; you do not write JNI entry
points or registration code yourself.

JNI is normally preferable to JSI when:

- you already have a C/C++ library;
- the work performs file I/O, parsing, compression, networking, or another
  potentially blocking operation;
- the computation is large enough that running it synchronously would stall the
  JavaScript thread;
- the module belongs in an Android Native Module workflow but needs a C/C++
  implementation.

Value-returning exports are promises. `void` exports are fire-and-forget.

## JSI Module

Choose JSI only when JavaScript needs the result synchronously and the native
operation is short enough to run safely on the JavaScript thread. Calls and
errors return directly without a promise.

Typical uses:

- small low-latency calculations;
- tight synchronous access where a Promise would change the required API;
- short native operations that do not wait on files, networks, locks, or other
  slow resources.

Do not use JSI for blocking or unpredictable work. File I/O, parsing,
compression, networking, or long computation freezes React Native's JavaScript
thread; use Native JNI instead.

JSI also depends on the Supernote host being allowed to load and execute the
module library. The generator does not inspect or modify SELinux policy.

## Quick decision

1. Need to write Kotlin/Java or use Android APIs directly? Choose **Native**.
2. Need C/C++, and the call may block or can be asynchronous? Choose **JNI**.
3. Need a short C/C++ operation to return synchronously? Choose **JSI**.

The generator does not convert an existing module from one type to another.
Choose the call model before implementation begins.
