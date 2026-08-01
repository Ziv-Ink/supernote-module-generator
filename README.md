# Supernote Module Generator

A code generator that adds Native, JNI, and JSI modules to an **existing
Supernote plugin**.

It generates the local package, native bridge, Android build wiring,
declarations, and React Native autolinking needed to call Kotlin, Java, C, or
C++ from the plugin you already have.

## Before using this tool

You need a working Supernote plugin that already builds successfully. Creating,
packaging, installing, and debugging that plugin are outside this project's
scope; use the [official Supernote documentation](https://docs.supernote.com/)
for that workflow.

Run this generator when the existing plugin needs native functionality.

## Install

Python 3.9 or newer is required:

```bash
python3 -m pip install supernote-module-generator
```

Run it from the root of the existing plugin:

```bash
supernote-module doctor --type native
supernote-module
```

## Add native functionality

For example, choose **Add module**, **Native Module**, and package name
`local-math`. Accept the derived JavaScript name `Math` and Android namespace
`com.example.math`.

Then edit the generated implementation file:

```text
local_modules/local-math/android/src/main/java/com/example/math/Example.kt
```

```kotlin
package com.example.math

import com.example.math.nativemodule.annotation.SupernoteExport

class Example {
  @SupernoteExport
  fun add(left: Double, right: Double): Double = left + right
}
```

Call it from the existing plugin:

```typescript
import Math from 'local-math';

const answer = await Math.add(20, 22);
```

For building, installing, and debugging the plugin itself, continue with the
[official Supernote documentation](https://docs.supernote.com/).

## Choose a backend

| Type | Write | Call model | Use it for |
| --- | --- | --- | --- |
| `native` | Kotlin or Java | Promise for returned values | Android APIs and Kotlin/Java libraries |
| `jni` | C or C++ behind JNI | Promise for returned values | C/C++ work that can be asynchronous |
| `jsi` | C or C++ through JSI | Synchronous | Short JavaScript-thread work on a tested PluginHost |

JSI is a supported generator backend. Runtime execution still depends on the
target PluginHost, firmware, linker namespace, and SELinux policy.

## Documentation

The [Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki) documents
only the native-module generator:

- [Add a Module](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Add-a-Module)
- [Choose a Module](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Choosing-a-Module)
- [Export Functions](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Export-Functions)
- [Manage Generated Modules](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Managing-Modules)
- [Troubleshoot Generated Modules](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Troubleshooting)

For exact options in the installed version:

```bash
supernote-module help add
```

## Contributing

See [CONTRIBUTING.md](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/CONTRIBUTING.md)
when changing the generator itself.

## License

MIT. See [LICENSE](https://github.com/Ziv-Ink/supernote-module-generator/blob/main/LICENSE).
