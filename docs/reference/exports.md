# Exports, paths, and ownership

## Terms

| Term | Meaning in this project |
| --- | --- |
| Plugin | The parent Supernote React Native application packaged as `.snplg`. |
| Local module/package | One generator-managed dependency below `local_modules/`; not a separate plugin. |
| Package name | npm/Yarn identity, folder segment, and JS/TS import specifier. |
| JavaScript name | Object registered with React Native or exposed by the JSI proxy. |
| Android namespace | Java/Kotlin package for implementation and generated classes. |
| Export | A Kotlin/Java method or C++ function exposed to JS/TS. |
| Bridge | Generated conversion/dispatch code between JS and Kotlin/Java or JNI. |
| Autolinking | React Native discovery of the local package through `package.json` and `react-native.config.js`. |
| Synchronous | The call returns before JavaScript continues; it runs on the JavaScript thread for JSI. |
| Asynchronous | A returned value is delivered through a Promise and the caller uses `await`. |
| User-owned | Preserved by Update. |
| Generator-owned | Replaceable by Update; do not hand-edit. |

The public CLI label “Native Module” means the Kotlin/Java backend in this
documentation. JNI and JSI are also native code in the general sense.

## Kotlin/Java exports

Write `.kt` or `.java` beneath the generated Android source tree, normally:

```text
local_modules/<package>/android/src/main/java/<namespace-path>/
```

Use the module-specific generated annotation import:

```kotlin
import com.example.math.nativemodule.annotation.SupernoteExport

class Example {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right

  @SupernoteExport(name = "greet")
  fun makeGreeting(name: String): String = "Hello, $name"
}
```

Requirements:

- a public instance method on a concrete public class;
- no suspend, inline, operator, static, generic, extension, or vararg export;
- unique JavaScript export names across the module;
- a public constructor taking `ReactApplicationContext`, a public constructor
  taking `android.content.Context`, or a public zero-argument constructor, in
  that preference order;
- no Activity or arbitrary constructor injection.

Supported boundary values:

| Kotlin/Java | TypeScript | Call model |
| --- | --- | --- |
| `Boolean` / `boolean` | `boolean` | Promise when returned |
| `Double` / `double` | `number` | Promise when returned |
| `String` | `string` | Promise when returned |
| `Unit` / `void` return | `void` | Fire-and-forget |

Nullable values, collections, arrays, objects, integer-specific types, floats,
callbacks, and arbitrary Android objects are not supported at the generated
boundary.

Value-returning call:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
```

`Unit`/`void` call:

```typescript
Math.setEnabled(true);
```

Value failures reject the Promise. Failures from fire-and-forget exports are
logged with module/export context.

## C/C++ exports for JNI and JSI

Write user code beneath:

```text
local_modules/<package>/android/src/main/cpp/
```

Place the marker directly before an ordinary top-level C++ definition:

```cpp
// @SupernoteExport
double add(double left, double right) {
  return left + right;
}

// @SupernoteExport(name = "greet")
std::string makeGreeting(std::string name) {
  return "Hello, " + name;
}
```

Supported boundary types are exactly `bool`, `double`, and UTF-8
`std::string` by value, plus `void` returns. `noexcept` is supported.

An export must not be overloaded, namespaced, templated, `static`, `inline`,
`constexpr`, `extern "C"`, variadic, pointer-based, reference-based, or declared
with default arguments/attributes/macros around its signature. Parameters must
be named, and C++/JavaScript export names must be unique.

`.cc`, `.cpp`, and `.cxx` files can export. `.c` files compile as C23 and may
provide internal helpers, but cannot contain export markers. C++ and generated
bindings compile as C++23. Use `extern "C"` guards in headers when C++ calls a C
helper.

Do not write JNI functions, `JNI_OnLoad`, HostFunctions, native registration,
loaders, or competing bootstrap symbols. Do not edit generated CMake.

### JNI call model

Returned values are Promises:

```typescript
import MathJni from 'local-math-jni';

const total = await MathJni.add(20, 22);
```

Generated JNI carries strings as UTF-8 byte arrays rather than JNI modified
UTF-8.

### JSI call model

Returned values and errors are synchronous:

```typescript
import MathJsi from 'local-math-jsi';

const total = MathJsi.add(20, 22);
```

Every call runs on the JavaScript thread. Runtime availability is host/policy
dependent; read the [JSI support gates](compatibility.md#jsi-has-three-separate-support-gates).

## Ownership by path

| Path | Ownership | Update behavior |
| --- | --- | --- |
| Native `android/src/main/java/` user packages | User | Preserved, except generated subpackages |
| JNI/JSI `android/src/main/cpp/` | User | Entire tree preserved |
| Parent plugin JS/TS | User | Not managed by the generator |
| Generated module `README.md` | Generator | Replaced |
| `.supernote-module.json`, package metadata, `.gitignore` | Generator | Replaced |
| `index.js`, `index.d.ts`, `react-native.config.js` | Generator | Replaced/regenerated |
| Gradle, CMake, manifest, annotations, processors, bridge, loaders, registration | Generator | Replaced |
| Android/KSP/CMake build output | Build output | May be regenerated/cleaned |

Generated metadata contains the exact `generated_files` and
`implementation_roots` for each package. It is machine-readable ownership data,
not a place for manual customization.

Update has no dry-run and replaces the generated README. Remove deletes both
ownership classes because it removes the complete package. See
[Manage modules safely](../guides/managing-modules.md).
