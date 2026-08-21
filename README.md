# Supernote Module Generator

Supernote Module Generator adds typed C/C++ and Kotlin/Java capabilities to an
existing Supernote plugin. It generates the JSI, JNI, Kotlin Symbol Processing,
TypeScript, build, and lifecycle code that connects those implementations to
JavaScript.

V3 models one user-facing feature, regardless of where its implementation
lives. One feature may contain C++, C helper files, Kotlin, and Java together.
JSI is the only JavaScript frontend, and the plugin compiles one generated V3
runtime/build component shared by all features.

Version `3.0.0.dev0` is the development line for first-class native objects and
declared copied value types. JavaScript keeps references to original C++,
Kotlin, and Java object instances, while declared value objects are validated
and copied. Arrays, nullable values, string enums, live object fields,
returned-only objects, explicit constructors/factories, and async object retention use
one language-neutral JavaScript and TypeScript model.

There are no V2 users or migration requirements. V3 deliberately has no V2
manifest reader, converter, compatibility mode, or migration tool.

## Install

Python 3.9 or newer is required:

```bash
python3 -m pip install supernote-module-generator
```

The public identities remain:

```text
Python distribution: supernote-module-generator
CLI command:         supernote-module
```

Run the CLI from an existing Supernote plugin root.

## Add a feature

Choose which starter source families to scaffold:

```bash
supernote-module add document --starter cpp --yes
supernote-module add document --starter kotlin --yes
supernote-module add document --starter cpp --starter kotlin --yes
```

The guided command presents the same choices as `C/C++ (native)` and
`Kotlin/Java (JVM)`. This choice creates initial example files only. It does not
make the feature a native or JVM feature, and either source family can be added
later without conversion or metadata changes.

The native root compiles C23 and C++23 implementation source. Initial
first-class marked declarations are C++23 only; C23 code remains fully supported
behind normal C-compatible interfaces and a canonical marked C++ boundary.

Useful lifecycle commands are:

```bash
supernote-module update document --yes
supernote-module validate document
supernote-module validate --all --build
supernote-module doctor
supernote-module remove document --yes
```

Removal preserves plugin build output by default. To remove the three known
generated build directories as part of an explicit removal:

```bash
supernote-module remove document --delete-build-files --yes
```

That option targets only `build/`, `android/build/`, and
`android/app/build/`. `--yes` by itself never enables build-output deletion or
widens a single-feature target to all features.

## Explicit source intent

Normal public source is ignored by the generator. A declaration participates
only when it has a deliberate Supernote marker.

For C++ the initial marker form is an exact source comment:

```cpp
// @SupernotePluginExport
std::int32_t pageCount();

// @SupernotePluginInternal
void rebuildIndex();

// @SupernotePluginExport
// @SupernotePluginAsync
std::vector<std::byte> loadPage(std::int32_t page);

void ordinaryHelper(); // ignored
```

For Kotlin and Java, use the generated annotations with the same names:

```kotlin
@SupernotePluginExport
fun pageCount(): Int = 42

@SupernotePluginInternal
fun rebuildIndex() = Unit

@SupernotePluginExport
@SupernotePluginAsync
suspend fun loadPage(page: Int): ByteArray = TODO()
```

`SupernotePluginInternal` generates typed cross-language routing without adding the
declaration to JavaScript or TypeScript. `SupernotePluginAsync` is always explicit;
Kotlin `suspend`, C++ future-like types, or blocking implementation code never
silently change the public API.

`SupernotePluginObject` declares reference semantics;
`SupernotePluginValue` declares copied structural semantics. Neither marker
publishes members or construction by itself. Every JavaScript-visible function,
method, field, and constructor requires its own explicit marker:

```cpp
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
  explicit Stroke(std::vector<Point> points);

  // @SupernotePluginExport
  bool intersects(const std::shared_ptr<Stroke> &other) const;

  // @SupernotePluginExport
  std::shared_ptr<Stroke> transformed(Point offset) const;

  // @SupernotePluginExport
  std::string label;

  void resetInternalCache();  // ignored
};

// @SupernotePluginExport
std::shared_ptr<Stroke> loadStroke(std::string path);
```

JavaScript receives stable runtime-local identity: if the same live native
instance is exposed again in one active runtime generation, the same JavaScript
object is returned. C++ objects use generated shared ownership; JVM objects use
managed global references and `IsSameObject`. Returned-only objects omit a
constructor but retain the same methods, argument/result behavior, lifetime,
and identity. Marked native-object fields are live properties; source
mutability determines whether they are writable.

Kotlin data classes and supported Java records/final classes can declare copied
values. Kotlin/Java object classes use `@SupernotePluginObject`, and an eligible
constructor uses `@SupernoteConstructor`. Static/top-level functions returning
an object are ordinary explicitly marked factories; no separate factory marker
is needed.

## V3 types and copied values

The closed V3 semantic types and JavaScript/TypeScript mappings are:

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

V3 intentionally does not accept arbitrary JavaScript objects, dynamic/JSON
trees, callbacks, maps, sets, tuples, general unions, recursive value objects,
raw pointers, numeric native handles, unsigned/platform-dependent C++ integer
types, or unmarked structural lookalikes.

## Language-family routing

The public API does not expose implementation-family details. Current V3 passes
C++ native objects only to C++ routes and Kotlin/Java native objects within the
shared JVM family. Complete copied values may cross generated C++/JVM internal
routes when both families declare the same logical schema.

Current V3 does not generate C++/JVM native-object proxies. A direct or nested
cross-family object reference is rejected during generation with a source-
located diagnostic. Object type IDs and public TypeScript shapes remain
language-neutral so a later proxy implementation does not require a public API
redesign.

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

Final generated C++ receiver/resource destruction is deferred to a managed
non-JS context. There is no promise of a particular cleanup thread, exact
timing, or JSI access. Resources requiring a specific thread must be managed by
the plugin implementation. JNI global references are released safely by the
generated runtime, while later JVM object collection remains controlled by the
JVM.

## Validation boundary

`supernote-module validate` checks generated structure by default; `--build`
also invokes the Android build. A successful local build proves generation and
compilation for that environment, not that a particular Supernote firmware,
PluginHost, linker namespace, or SELinux policy will load and execute the code.
Target-device behavior must be validated on the intended device.

Same-process native runtime replacement is generation-checked and bounded. A
PluginHost process accepts at most 32 generated native generations for one
plugin component; restart PluginHost before another replacement if that limit
is reached.

The generator does not create the surrounding Supernote plugin. Plugin creation,
installation, and device debugging are covered by the
[official Supernote plugin documentation](https://docs.supernote.com/).

## Contributing

See [CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md)
for development and validation rules and
[V3 architecture](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/V3-ARCHITECTURE.md)
for the contributor-facing runtime and type model. It is not a V2 migration
guide or compatibility promise.

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
