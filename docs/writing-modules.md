# Writing modules

After generation, open `local_modules/<package-name>/README.md` first. It uses
your actual names and paths and identifies which files are safe to edit.

## Native Module

Write Kotlin or Java beneath the package's generated Android source directory:

```text
local_modules/<package-name>/android/src/main/java/<namespace-path>/
```

Add `@SupernoteExport` to public instance methods on a concrete public class:

```kotlin
import com.example.math.nativemodule.annotation.SupernoteExport

class Example {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right
}
```

The generated README contains the exact annotation import for your namespace.
Exported classes may have a public constructor taking
`ReactApplicationContext`, a public constructor taking `android.content.Context`,
or a public zero-argument constructor, in that preference order.

Supported boundary values are Boolean, Double, String, and Unit/void returns.
Value-returning methods are asynchronous:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
```

## Native JNI Module

Write C++ exports beneath:

```text
local_modules/<package-name>/android/src/main/cpp/
```

Tag a top-level definition with `// @SupernoteExport`:

```cpp
// @SupernoteExport
double add(double left, double right) {
  return left + right;
}
```

The supported boundary types are `bool`, `double`, and UTF-8 `std::string` by
value, plus `void` returns. Exported definitions cannot be overloaded, `static`,
`inline`, `constexpr`, templated, variadic, namespaced, pointer-based, or
reference-based. Do not write JNI functions, registration, loaders, or CMake.

C23 files may provide internal helpers but cannot be exported. C++ files and
generated bindings compile as C++23.

Value-returning calls are asynchronous:

```typescript
import Math from 'local-math';

const total = await Math.add(20, 22);
```

## JSI Module

JSI uses the same C/C++ source location, export marker, supported types, and
signature restrictions as JNI. The JavaScript call is synchronous:

```typescript
import Math from 'local-math';

const total = Math.add(20, 22);
```

Every export executes on React Native's JavaScript thread. Keep it short and
deterministic. Use JNI for file I/O, parsing, compression, networking, long
computation, or anything else that may block.

## Build after changing an export

From the parent plugin root:

```bash
./buildPlugin.sh
```

On Windows PowerShell, run `.\buildPlugin.ps1` instead. The Android build
regenerates bridge code and `index.d.ts`, then writes the plugin package to
`build/outputs/<plugin-name>.snplg`. npm or Yarn only links the local package;
it does not compile or deploy the plugin.

To install with ADB, replace the placeholder with the generated filename:

```bash
adb push "build/outputs/<plugin-name>.snplg" /storage/emulated/0/MyStyle/
```

Alternatively, manually copy the `.snplg` file into the device's `MyStyle`
folder. Then open **Settings > Apps > Plugins** and choose **Add Plugin**.

You may also request a build during Add, Update, or Validate with `--build`.

## Know what you own

User-owned implementation:

- Native: Kotlin/Java beneath the generated package source directory.
- JNI and JSI: the complete `android/src/main/cpp/` tree.
- JavaScript/TypeScript calls in the parent plugin.

Generator-owned infrastructure includes metadata, package files, bridge code,
loaders, CMake, declarations, and autolinking. **Update** preserves user-owned
implementation source. **Remove** deletes the complete module, including that
source, so commit or back it up first.
