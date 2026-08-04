# Supernote Module Generator

Supernote Module Generator adds native functionality to an **existing Supernote plugin**.

If you are comfortable writing JavaScript or TypeScript and your plugin already does everything you need, then you should probably stick with it. Native code adds complexity, and there should be a real reason to use it.

This project exists for the point where a plugin needs to directly call Android APIs, integrate with an existing Android, JVM, or native library, or move performance-critical work into Kotlin, Java, C, or C++.

Doing that in React Native normally requires far more boilerplate than anyone should be subjected to. Depending on what the feature needs, you may have to deal with Android library setup, React Native registration, Kotlin or Java bridge code, JNI bindings, CMake configuration, JSI installation, package linking, and TypeScript declarations before reaching the code that actually matters.

The generator handles that dull and messy connection between JavaScript and native code so you can spend your time working on the feature itself.

It does **not** create the Supernote plugin, decide whether native code is actually faster for your workload, make an API or library compatible with the target device, or guarantee that a compiled native library can run inside PluginHost.

## Documentation

The [Wiki](https://github.com/Ziv-Ink/supernote-module-generator/wiki) explains how to install and use the generator, choose a module type, write and export native functions, manage generated modules, and troubleshoot problems.

Creating, building, installing, and debugging the Supernote plugin itself is covered by the [official Supernote plugin documentation](https://docs.supernote.com/).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) when changing the generator itself.

## License

MIT. See [LICENSE](LICENSE).
