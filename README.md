# Supernote Module Generator

Supernote Module Generator adds typed C/C++ and Kotlin/Java capabilities to an
existing Supernote plugin. It generates the JSI, JNI, Kotlin Symbol Processing,
TypeScript, build, and lifecycle code that connects those implementations to
JavaScript.

V2 models one user-facing feature, regardless of where its implementation
lives. One feature may contain C++, C helper files, Kotlin, and Java together.
JSI is the only JavaScript frontend, and the plugin compiles one generated V2
runtime/build component shared by all features.

V2 is the current stable architecture. The initial `2.0.0` release deliberately
keeps advanced value/object features and caller-controlled cancellation out of
scope; the supported foundation is described below.

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

An exported class publishes the object type. Its single eligible public
constructor becomes the normal `create(...)` factory, while every other method
still needs its own marker:

```cpp
// @SupernotePluginExport
class Document {
public:
  explicit Document(std::string path);

  // @SupernotePluginExport
  std::int32_t pageCount() const;

  void resetInternalCache(); // ignored
};
```

Initial V2 also supports the same narrow per-JavaScript-object model for
deliberately marked Kotlin/Java classes. Object parameters/results,
returned-only objects, inheritance, properties, custom factories, and general
object graphs are deferred.

## Initial value types

The initial semantic types and JavaScript/TypeScript mappings are:

| Supernote value | JavaScript/TypeScript |
| --- | --- |
| `void` | `void` |
| `bool` | `boolean` |
| `int32` | `number` |
| `int64` | `bigint` |
| `float32`, `float64` | `number` |
| `string` | `string` |
| `bytes` | `Uint8Array` |

Strings use UTF-8 when crossing native/JNI boundaries. Byte values use
copy-based snapshot semantics and pass only the visible `Uint8Array` view.
Nullability, generic collections, maps, value structs, enums, unsigned values,
and zero-copy buffers are not part of the initial foundation.

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

The generator does not create the surrounding Supernote plugin. Plugin creation,
installation, and device debugging are covered by the
[official Supernote plugin documentation](https://docs.supernote.com/).

## Contributing

See [CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md)
for development and validation rules and
[V1 to V2 architecture](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/docs/V1-TO-V2-ARCHITECTURE.md)
for contributor-facing
architectural history. That history is not a project migration guide or a
compatibility promise.

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
