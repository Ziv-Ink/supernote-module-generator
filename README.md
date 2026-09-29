# Supernote Module Generator

Supernote Module Generator adds typed C/C++ and Kotlin/Java capabilities to an
existing Supernote plugin. It generates the JSI, JNI, Kotlin Symbol Processing,
TypeScript, build, and lifecycle code that connects those implementations to
JavaScript.

A feature can use C++, C helper files, Kotlin, and Java together while exposing
one JavaScript and TypeScript API. The generator connects everything through
JSI and builds one shared runtime component for the plugin.

Native objects keep a reference to the original C++, Kotlin, or Java instance.
Declared value objects are validated and copied across the bridge. The same
type system also supports arrays, nullable values, string enums, live object
fields, returned-only objects, explicit constructors, factories, and async
object retention.

## Install

Python 3.9 or newer is required:

```bash
python3 -m pip install sn-module-gen
```

Package and command names:

```text
Python distribution: sn-module-gen
CLI command:         sn-module-gen
```

Run the CLI from an existing Supernote plugin root.

## Add a feature

Choose which starter source families to scaffold:

```bash
sn-module-gen add document --starter cpp --yes
sn-module-gen add document --starter kotlin --yes
sn-module-gen add document --starter cpp --starter kotlin --yes
```

The guided command shows the same choices as `C/C++ (native)` and
`Kotlin/Java (JVM)`. These options only choose which example files to create.
You can add either source family later.

The native root compiles C23 and C++23 source. Exported declarations are written
in C++23. C23 code can be used behind an ordinary C-compatible interface and a
small marked C++ boundary.

Common commands:

```bash
sn-module-gen update --yes
sn-module-gen validate
sn-module-gen doctor
```

Authors own source, `package.json`, lockfiles, dependency installation, and app
builds. `add` scaffolds a feature and prints the npm/Yarn dependency command to
run separately. It never invokes a package manager, app build, or Git operation.
Plain `update` always regenerates every current local feature and the shared
generated runtime. Generated output is disposable and manual edits to it are
overwritten; authored files are preserved.

To remove a feature, delete its authored module directory, remove its dependency
with npm or Yarn, and run plain `sn-module-gen update`. There is no generator
`remove`, selective update, dry-run/diff preview, or app-build command.

Doctor reports only generator-relevant host setup and package provenance. It can
probe Node package resolution, CMake, a C23/C++23 compiler, and Java/Kotlin/KSP
when the current project requires them. Doctor never installs tools, invokes an
app build, accesses a device, or claims PluginHost execution.

## Marking exports

The generator leaves ordinary source alone. It only processes declarations
with a Supernote marker.

In C++, markers are exact source comments:

<!-- sn-module-gen-release-example: readme-cpp android/src/main/cpp/feature.cpp -->
```cpp
#include <cstddef>
#include <cstdint>
#include <vector>

// @SupernotePluginExport
std::int32_t pageCount() {
  return 42;
}

// @SupernotePluginInternal
void rebuildIndex() {}

// @SupernotePluginExport
// @SupernotePluginAsync
std::vector<std::byte> loadPage(std::int32_t page) {
  (void)page;
  return {};
}

void ordinaryHelper() {} // ignored
```

For Kotlin and Java, use the generated annotations with the same names:

<!-- sn-module-gen-release-example: readme-jvm android/src/main/java/com/example/readme_jvm/FeatureApi.kt -->
```kotlin
package com.example.readme_jvm

import supernote.generated.annotations.SupernotePluginAsync
import supernote.generated.annotations.SupernotePluginExport
import supernote.generated.annotations.SupernotePluginInternal

@SupernotePluginExport
fun pageCount(): Int = 42

@SupernotePluginInternal
fun rebuildIndex() = Unit

@SupernotePluginExport
@SupernotePluginAsync
suspend fun loadPage(page: Int): ByteArray = TODO()
```

`SupernotePluginInternal` generates typed cross-language routing without adding
the declaration to JavaScript or TypeScript. `SupernotePluginAsync` is always
explicit; Kotlin `suspend`, C++ future-like types, and blocking implementation
code do not change the public API on their own.

`SupernotePluginObject` declares reference semantics;
`SupernotePluginValue` declares copied structural semantics. Neither marker
publishes members or construction by itself. Every JavaScript-visible function,
method, field, and constructor requires its own explicit marker:

<!-- sn-module-gen-release-example: readme-cpp android/src/main/cpp/FeatureTypes.hpp -->
```cpp
#pragma once

#include <memory>
#include <string>
#include <utility>
#include <vector>

// @SupernotePluginValue
struct Point {
  // @SupernotePluginExport
  double x;
  // @SupernotePluginExport
  double y;
};

// @SupernotePluginObject
class Stroke {
public:
  // @SupernoteConstructor
  explicit Stroke(std::vector<Point> points) : points_(std::move(points)) {}

  // @SupernotePluginExport
  bool intersects(const std::shared_ptr<Stroke> &other) const {
    return other != nullptr;
  }

  // @SupernotePluginExport
  std::shared_ptr<Stroke> transformed(Point offset) const {
    (void)offset;
    return std::make_shared<Stroke>(*this);
  }

  // @SupernotePluginExport
  std::string label;

  void resetInternalCache();  // ignored

private:
  std::vector<Point> points_;
};
```

JavaScript receives stable runtime-local identity: if the same live native
instance is exposed again in one active runtime generation, the same JavaScript
object is returned. C++ objects use generated shared ownership; JVM objects use
managed global references and `IsSameObject`. Returned-only objects omit a
constructor but retain the same methods, argument/result behavior, lifetime,
and identity. Marked native-object fields are live properties; source
mutability determines whether they are writable.

Kotlin data classes and supported Java records or final classes can declare
copied values. Kotlin and Java object classes use `@SupernotePluginObject`, and
constructors exposed to JavaScript use `@SupernoteConstructor`. A marked static
or top-level function can also return an object; there is no separate factory
annotation.

## Supported types and copied values

The generator supports these JavaScript and TypeScript mappings:

| Supernote value | JavaScript/TypeScript |
| --- | --- |
| `void` | `void` |
| `bool` | `boolean` |
| `int32` | `number` |
| `int64` | `bigint` |
| `float32`, `float64` | `number` |
| `string` | `string` |
| `bytes` | `Uint8Array` |
| string enum | string-literal union |
| declared value object | typed plain object |
| native reference object | nominally branded generated interface |
| homogeneous array of `T` | `T[]` |
| nullable `T` | `T \| null` |

Strings use UTF-8 when crossing native/JNI boundaries. Byte values use
copy-based snapshot semantics and pass only the visible `Uint8Array` view.
Declared value fields are required and strictly validated. Extra JavaScript
fields are ignored without being read. Values and array containers are copied;
native-object leaves retain references and identity. Arrays must be dense and
homogeneous. `null` is accepted only where declared, while omitted values and
`undefined` remain invalid.

The generated boundary does not accept arbitrary JavaScript objects,
dynamic/JSON trees, callbacks, maps, sets, tuples, general unions, recursive
value objects, raw pointers, numeric native handles, unsigned or
platform-dependent C++ integer types, or unmarked structural lookalikes.

## Language-family routing

The public API does not expose implementation-language details. C++ native
objects can be passed to C++ routes, while Kotlin and Java objects can be passed
within the JVM family. Copied values may cross generated C++/JVM internal routes
when both sides declare the same schema.

Cross-language native-object proxies are not generated yet. Passing a C++
object to a JVM route, or a JVM object to a C++ route, produces a source-located
generation error. Public TypeScript types remain independent of the
implementation language.

## Async, errors, and lifetime

An accepted async call immediately returns a normal `Promise<T>`. Ordinary
blocking implementations use the plugin's shared bounded worker executor;
supported Kotlin `suspend` implementations use the generated coroutine adapter.
Both routes share the same cancellation, teardown, error, and completion rules.

Argument count/type/integer/range misuse throws `TypeError` or `RangeError`
before an operation is accepted. Later failures reject with the exported
`SupernoteError`, whose stable string `code` includes
`RESOURCE_EXHAUSTED`, `CANCELLED`, `FEATURE_CLOSED`,
`IMPLEMENTATION_ERROR`, and `INTERNAL`.

Accepted async object methods retain their implementation receiver until
physical work can no longer access it. Generated code prevents use-after-free
but does not add a mutex or serial queue around user object state; plugin
implementations remain responsible for their own thread safety.

Generated code destroys C++ receivers and resources away from the JavaScript
thread. Cleanup may run on different threads and must not access JSI. If a
resource must be released on a particular thread, the plugin must arrange that
itself. The generated runtime releases JNI global references; the JVM decides
when the underlying objects are collected.

## Validation and package consumption

`sn-module-gen validate` checks all authored local modules, declared direct module
dependencies, exported APIs, generated output, completion state, and compatibility
records without publishing files. It may run bounded compiler/KSP analysis in the
designated build scratch area, but it does not regenerate output, install
dependencies, or invoke the app build.

Plain `update` creates complete package-local output beneath
`.supernote-generated/`. A publisher can run ordinary `npm pack` after update.
Consumers install that module package and `@supernote/runtime` as direct
dependencies with npm, Yarn classic, or modern Yarn configured with
`nodeLinker: node-modules`. Consumer builds use the packaged CMake, registration,
and JVM adapter sources without running `sn-module-gen` or KSP. Yarn Plug'n'Play
is not supported. Install author module packages as copied packages; external
linked author-source directories are unsupported. A supported shared-runtime
link does not imply support for linked author sources.

For the 0.1.3 features and bounded host/device qualification, see the
[release notes](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/maintainers/release-notes-v0.1.3.md).

Host generation, compilation, and package consumption do not prove that a
particular Supernote firmware, PluginHost, linker namespace, or SELinux policy
will execute the code. Validate target-device behavior separately on the intended
device when device evidence is required.

PluginHost can load up to 32 native generations for one plugin component in the
same process. Restart PluginHost before installing another changed native
generation after reaching that limit.

The generator does not create the surrounding Supernote plugin. Plugin creation,
installation, and device debugging are covered by the
[official Supernote plugin documentation](https://docs.supernote.com/).

## Contributing

See [CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md), the
[testing guide](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/TESTING.md), and [CI workflow](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/.github/workflows/quality.yml).
The [architecture guide](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/ARCHITECTURE.md) explains the runtime model. The [`v0.1.0` release record](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/maintainers/release-evidence/v0.1.0.md)
lists exactly what was tested for the first public release.

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
