import json
from pathlib import Path
import re
import tempfile
import unittest

from supernote_module_generator import binding_codegen


class BindingCodegenScannerTests(unittest.TestCase):
    def make_module(
        self,
        root: Path,
        *,
        backend: str = "jni",
        source: str = (
            "// @SupernoteExport\n"
            "double add(double left, double right) { return left + right; }\n"
        ),
    ) -> Path:
        module = root / "local-test"
        cpp = module / "android/src/main/cpp"
        config = module / "android/.supernote-module/codegen-config.json"
        cpp.mkdir(parents=True)
        config.parent.mkdir(parents=True)
        (cpp / "math.cpp").write_text(source, encoding="utf-8")
        config.write_text(
            json.dumps(
                {
                    "android_namespace": "com.example.test",
                    "backend": backend,
                    "class_prefix": "LocalTest",
                    "jsi_global_name": "__supernoteLocalTest",
                    "module_name": "LocalTest",
                    "native_library_name": "supernote_local_test",
                }
            ),
            encoding="utf-8",
        )
        return module

    def test_spaced_rename_and_noexcept_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                source=(
                    'const char *example = R"tag(// @SupernoteExport)tag";\n'
                    "/* // @SupernoteExport */\n"
                    "// Documentation mentions @SupernoteExport here.\n"
                    '// @SupernoteExport(name = "difference")\n'
                    "double subtract(\n"
                    "    double left,\n"
                    "    double right) noexcept {\n"
                    "  return left - right;\n"
                    "}\n"
                ),
            )
            exports = binding_codegen.scan_sources(module)
            self.assertEqual(["difference"], [export.js_name for export in exports])
            self.assertEqual(4, exports[0].line)
            self.assertTrue(exports[0].noexcept)
            binding_codegen.generate(module)
            manifest = json.loads(
                (
                    module / "android/build/generated/supernote/exports.json"
                ).read_text(encoding="utf-8")
            )
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            self.assertTrue(manifest["exports"][0]["noexcept"])
            self.assertIn(
                "double subtract(double left, double right) noexcept;",
                generated,
            )

    def test_rejects_marker_in_preprocessor_conditional(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                source=(
                    "#if 0\n"
                    "// @SupernoteExport\n"
                    "double hidden(double value) { return value; }\n"
                    "#endif\n"
                ),
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_sources(module)
            message = str(raised.exception)
            self.assertIn("math.cpp:2", message)
            self.assertIn("module 'LocalTest'", message)
            self.assertIn("preprocessor conditional", message)

    def test_rejects_prefixes_before_and_after_marker(self):
        cases = {
            "before-static": (
                "static\n"
                "// @SupernoteExport\n"
                "double hidden(double value) { return value; }\n",
                "declaration prefix before the marker",
            ),
            "after-inline": (
                "// @SupernoteExport\n"
                "inline double hidden(double value) { return value; }\n",
                "modifier 'inline' is forbidden",
            ),
            "attribute": (
                "// @SupernoteExport\n"
                "[[nodiscard]] double hidden(double value) { return value; }\n",
                r"modifier '\[\[' is forbidden",
            ),
            "declaration": (
                "// @SupernoteExport\n"
                "double hidden(double value);\n",
                "tagged declarations are not exported",
            ),
            "suffix": (
                "// @SupernoteExport\n"
                "double hidden(double value) FINAL { return value; }\n",
                "unsupported tokens after the parameter list",
            ),
        }
        for name, (source, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), source=source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError, diagnostic
                ):
                    binding_codegen.scan_sources(module)

    def test_rejects_exact_markers_in_all_helper_and_header_suffixes(self):
        for suffix in (
            ".c",
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
            ".inl",
            ".inc",
            ".ipp",
            ".tpp",
        ):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory))
                (module / f"android/src/main/cpp/forbidden{suffix}").write_text(
                    "// @SupernoteExport\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    "allowed only in .cc, .cpp, or .cxx",
                ):
                    binding_codegen.scan_sources(module)

    def test_jni_reserved_names_do_not_apply_to_jsi(self):
        source = (
            "// @SupernoteExport\n"
            "double choose(double when) { return when; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jni", source=source)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "argument 1 name 'when' is reserved",
            ):
                binding_codegen.scan_sources(module)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi", source=source)
            self.assertEqual("choose", binding_codegen.scan_sources(module)[0].js_name)

    def test_cpp23_keywords_are_rejected_for_both_backends(self):
        for backend in ("jni", "jsi"):
            for keyword in ("class", "nullptr", "co_await"):
                cases = (
                    (
                        f"function-{keyword}",
                        "// @SupernoteExport\n"
                        f"double {keyword}() {{ return 0.0; }}\n",
                        f"C++ function name {keyword!r} is a C++23 keyword",
                    ),
                    (
                        f"parameter-{keyword}",
                        "// @SupernoteExport\n"
                        f"double evaluate(double {keyword}) {{ return 0.0; }}\n",
                        f"argument 1 name {keyword!r} is a C++23 keyword",
                    ),
                )
                for name, source, diagnostic in cases:
                    with self.subTest(backend=backend, name=name):
                        with tempfile.TemporaryDirectory() as directory:
                            module = self.make_module(
                                Path(directory),
                                backend=backend,
                                source=source,
                            )
                            with self.assertRaisesRegex(
                                binding_codegen.CodegenError,
                                re.escape(diagnostic),
                            ):
                                binding_codegen.scan_sources(module)

    def test_jni_rejects_generated_method_and_promise_collisions(self):
        cases = {
            "getName": (
                "// @SupernoteExport\n"
                "double getName() { return 1.0; }\n",
                "collides with a generated Kotlin method",
            ),
            "initialize": (
                "// @SupernoteExport\n"
                "void initialize() {}\n",
                "collides with a generated Kotlin method",
            ),
            "promise": (
                "// @SupernoteExport\n"
                "double evaluate(double promise) { return promise; }\n",
                "generated React Native Promise parameter",
            ),
        }
        for name, (source, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), source=source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError, diagnostic
                ):
                    binding_codegen.scan_sources(module)

    def test_legacy_cpp_backend_generates_canonical_jni_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="cpp")
            binding_codegen.generate(module)
            manifest = json.loads(
                (
                    module / "android/build/generated/supernote/exports.json"
                ).read_text(encoding="utf-8")
            )
            declarations = (module / "index.d.ts").read_text(encoding="utf-8")
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            self.assertEqual("jni", manifest["backend"])
            self.assertIn("Promise<number>", declarations)
            self.assertIn("LocalTest.add: ", generated)

    def test_generated_errors_include_module_export_and_jsi_argument_details(self):
        source = (
            "// @SupernoteExport\n"
            "double add(double left, bool enabled) { return enabled ? left : 0; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi", source=source)
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            self.assertIn("LocalTest.add: expected 2 arguments", generated)
            self.assertIn(
                "argument 1 (left) must be a number; expected 2 arguments "
                "(number left, boolean enabled)",
                generated,
            )
            self.assertIn(
                "argument 2 (enabled) must be a boolean; expected 2 arguments "
                "(number left, boolean enabled)",
                generated,
            )
            self.assertIn("LocalTest.add: unknown C++ exception", generated)

    def test_rejects_user_owned_jni_on_load(self):
        source = (
            'extern "C" int JNI_OnLoad(void *, void *) { return 0; }\n'
            "// @SupernoteExport\n"
            "double add(double value) { return value; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "generated binding layer owns that bootstrap symbol",
            ):
                binding_codegen.scan_sources(module)

    def test_rejects_untagged_overload_in_same_source(self):
        source = (
            "double add(double value) { return value; }\n"
            "// @SupernoteExport\n"
            "double add(double left, double right) { return left + right; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_sources(module)
            message = str(raised.exception)
            self.assertIn("math.cpp:1", message)
            self.assertIn("untagged global definition", message)
            self.assertIn("exported C++ name 'add'", message)

    def test_rejects_untagged_declaration_in_another_source(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory))
            (
                module / "android/src/main/cpp/forward.cc"
            ).write_text(
                "double add(double value);\n",
                encoding="utf-8",
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_sources(module)
            message = str(raised.exception)
            self.assertIn("forward.cc:1", message)
            self.assertIn("untagged global declaration", message)
            self.assertIn("math.cpp:1", message)

    def test_calls_inside_function_bodies_are_not_overloads(self):
        source = (
            "// @SupernoteExport\n"
            "double add(double left, double right) { return left + right; }\n"
            "\n"
            "double call_export(double value) {\n"
            "  return add(value, value);\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            exports = binding_codegen.scan_sources(module)
            self.assertEqual(["add"], [export.cpp_name for export in exports])

    def test_empty_jni_and_jsi_modules_generate_and_pass_check_mode(self):
        for backend in ("jni", "jsi"):
            with self.subTest(backend=backend):
                with tempfile.TemporaryDirectory() as directory:
                    module = self.make_module(
                        Path(directory),
                        backend=backend,
                    )
                    self.assertEqual(1, len(binding_codegen.generate(module)))
                    (
                        module / "android/src/main/cpp/math.cpp"
                    ).write_text(
                        "// This module intentionally has no exports.\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        binding_codegen.CodegenError,
                        "generated bindings are stale",
                    ):
                        binding_codegen.generate(module, check=True)
                    self.assertEqual([], binding_codegen.generate(module))
                    self.assertEqual(
                        [],
                        binding_codegen.generate(module, check=True),
                    )

                    manifest = json.loads(
                        (
                            module
                            / "android/build/generated/supernote/exports.json"
                        ).read_text(encoding="utf-8")
                    )
                    declarations = (
                        module / "index.d.ts"
                    ).read_text(encoding="utf-8")
                    generated = (
                        module
                        / "android/build/generated/supernote/jni/"
                        "generated_bindings.cpp"
                    ).read_text(encoding="utf-8")

                    self.assertEqual([], manifest["exports"])
                    self.assertIn(
                        "export interface LocalTestModule {\n\n}",
                        declarations,
                    )
                    if backend == "jni":
                        self.assertIn("JNI_OnLoad", generated)
                        self.assertNotIn("JNINativeMethod", generated)
                        self.assertNotIn("RegisterNatives", generated)
                    else:
                        self.assertIn("Object exports(runtime);", generated)
                        self.assertIn(
                            "setProperty(runtime, kGlobalName",
                            generated,
                        )
                        self.assertNotIn("createFromHostFunction", generated)

    def test_jni_exception_messages_use_real_utf8_java_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jni")
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/"
                "generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            self.assertIn("java/nio/charset/StandardCharsets", generated)
            self.assertIn(
                'standards_class, "UTF_8", "Ljava/nio/charset/Charset;"',
                generated,
            )
            self.assertIn(
                '"([BLjava/nio/charset/Charset;)V"',
                generated,
            )
            self.assertIn("env->Throw(exception)", generated)
            self.assertNotIn(
                "env->ThrowNew(error_class, message.c_str())",
                generated,
            )


if __name__ == "__main__":
    unittest.main()
