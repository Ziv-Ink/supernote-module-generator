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

    def write_object_header(
        self,
        module: Path,
        source: str,
        *,
        relative: str = "model/Counter.hpp",
    ) -> Path:
        path = module / "android/src/main/cpp" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

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

    def test_jsi_object_scans_constructor_methods_and_access_control(self):
        source = """// @SupernoteExportObject
class Counter {
public:
  Counter(bool enabled, double initial, std::string label);
  bool enabled() const;
  double value() const noexcept;
  std::string label() noexcept;
  void increment(double amount, bool notify, std::string reason);
  double public_field;
private:
  int privateUnsupported();
protected:
  int protectedUnsupported();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            bindings = binding_codegen.scan_bindings(module)
            self.assertEqual(["add"], [item.js_name for item in bindings.exports])
            self.assertEqual(["Counter"], [item.js_name for item in bindings.objects])
            item = bindings.objects[0]
            self.assertEqual(
                ["bool", "double", "std::string"],
                [parameter.cpp_type for parameter in item.constructor.parameters],
            )
            self.assertEqual(
                ["enabled", "value", "label", "increment"],
                [method.js_name for method in item.methods],
            )
            self.assertTrue(item.methods[0].const)
            self.assertTrue(item.methods[1].const)
            self.assertTrue(item.methods[1].noexcept)
            self.assertTrue(item.methods[2].noexcept)

    def test_jsi_object_rename_struct_and_zero_argument_constructor(self):
        source = """// @SupernoteExportObject(name = "Document")
struct NativeDocument {
  NativeDocument();
  double pageCount() const;
private:
  int hidden();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source, relative="NativeDocument.hxx")
            item = binding_codegen.scan_bindings(module).objects[0]
            self.assertEqual("NativeDocument", item.cpp_name)
            self.assertEqual("Document", item.js_name)
            self.assertEqual((), item.constructor.parameters)
            self.assertEqual(["pageCount"], [method.js_name for method in item.methods])

    def test_class_default_private_and_struct_default_public(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject
class PrivateByDefault {
  PrivateByDefault();
public:
  PrivateByDefault(double value);
  double value();
};
// @SupernoteExportObject
struct PublicByDefault {
  PublicByDefault();
  void reset();
};
""",
                relative="access.hh",
            )
            objects = binding_codegen.scan_bindings(module).objects
            self.assertEqual(["PrivateByDefault", "PublicByDefault"], [item.js_name for item in objects])
            self.assertEqual(1, len(objects[0].constructor.parameters))
            self.assertEqual(0, len(objects[1].constructor.parameters))

    def test_object_rejects_unsupported_public_method_and_static_method(self):
        cases = {
            "unsupported-return": (
                "int unsupported();",
                "unsupported public method declaration",
            ),
            "unsupported-parameter": (
                "double evaluate(int value);",
                "argument 1 must be a named bool, double, or std::string",
            ),
            "static": (
                "static double evaluate();",
                "static methods are not supported",
            ),
        }
        for name, (method, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernoteExportObject\nclass Example {\npublic:\n"
                    "  Example();\n"
                    f"  {method}\n"
                    "};\n",
                )
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_rejects_method_and_constructor_overloads(self):
        cases = {
            "method": (
                "Example();\n  double value();\n  double value(double fallback);",
                "overloaded or duplicate method 'value'",
            ),
            "constructor": (
                "Example();\n  Example(double value);\n  double value();",
                "overloaded public constructors",
            ),
        }
        for name, (members, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernoteExportObject\nclass Example {\npublic:\n  "
                    + members
                    + "\n};\n",
                )
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_export_name_collisions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject(name = "add")
class Counter { public: Counter(); };
""",
            )
            with self.assertRaisesRegex(binding_codegen.CodegenError, "collides with free-function"):
                binding_codegen.scan_bindings(module)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject(name = "Thing")
class First { public: First(); };
// @SupernoteExportObject(name = "Thing")
class Second { public: Second(); };
""",
            )
            with self.assertRaisesRegex(binding_codegen.CodegenError, "duplicate JavaScript object export"):
                binding_codegen.scan_bindings(module)

    def test_object_typescript_factory_name_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject
class Counter { public: Counter(); };
// @SupernoteExportObject
class CounterFactory { public: CounterFactory(); };
""",
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_bindings(module)
            message = str(raised.exception)
            self.assertIn("generated TypeScript name 'CounterFactory'", message)
            self.assertIn("object export 'Counter'", message)
            self.assertIn("export 'CounterFactory'", message)
            self.assertIn("model/Counter.hpp:1", message)
            self.assertIn("model/Counter.hpp:3", message)

    def test_renamed_object_typescript_factory_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject(name = "Counter")
class NativeCounter { public: NativeCounter(); };
// @SupernoteExportObject(name = "CounterFactory")
class NativeFactory { public: NativeFactory(); };
""",
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_bindings(module)
            message = str(raised.exception)
            self.assertIn("generated TypeScript name 'CounterFactory'", message)
            self.assertIn("object export 'Counter'", message)
            self.assertIn("export 'CounterFactory'", message)
            self.assertIn('@SupernoteExportObject(name = "...")', message)

    def test_object_typescript_module_interface_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject(name = "LocalTestModule")
class NativeModule { public: NativeModule(); };
""",
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_bindings(module)
            message = str(raised.exception)
            self.assertIn("generated TypeScript name 'LocalTestModule'", message)
            self.assertIn("generated module interface 'LocalTestModule'", message)
            self.assertIn("export 'LocalTestModule'", message)

    def test_generated_object_header_includes_are_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject
class First { public: First(); };
// @SupernoteExportObject
class Second { public: Second(); };
""",
                relative="model/Objects.hpp",
            )
            self.write_object_header(
                module,
                """// @SupernoteExportObject
class Third { public: Third(); };
""",
                relative="other/Third.hh",
            )
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            self.assertEqual(1, generated.count('#include "model/Objects.hpp"'))
            self.assertEqual(1, generated.count('#include "other/Third.hh"'))

    def test_object_annotation_location_backend_and_malformed_diagnostics(self):
        cases = (
            (
                "jsi",
                "Counter.cpp",
                "// @SupernoteExportObject\nclass Counter { public: Counter(); };\n",
                "allowed only in .h",
            ),
            (
                "jni",
                "Counter.hpp",
                "// @SupernoteExportObject\nclass Counter { public: Counter(); };\n",
                "supported only by the JSI backend",
            ),
            (
                "jsi",
                "Counter.hpp",
                "// @SupernoteExportObject(bad)\nclass Counter { public: Counter(); };\n",
                "malformed object export tag",
            ),
        )
        for backend, relative, source, diagnostic in cases:
            with self.subTest(backend=backend, relative=relative), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend=backend)
                self.write_object_header(module, source, relative=relative)
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_marker_lexer_defenses_and_conditional_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                'const char *text = "// @SupernoteExportObject";\n'
                "/* // @SupernoteExportObject */\n"
                "// Documentation mentions @SupernoteExportObject here.\n",
            )
            self.assertEqual((), binding_codegen.scan_bindings(module).objects)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "#if 0\n// @SupernoteExportObject\n"
                "class Hidden { public: Hidden(); };\n#endif\n",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "preprocessor conditionals",
            ):
                binding_codegen.scan_bindings(module)

    def test_object_rejects_templates_inheritance_and_nested_exports(self):
        cases = {
            "template": (
                "template <typename T>\n// @SupernoteExportObject\n"
                "class Example { public: Example(); };\n",
                "declaration prefix before the object marker",
            ),
            "inheritance": (
                "// @SupernoteExportObject\n"
                "class Example : public Base { public: Example(); };\n",
                "inheritance is not supported",
            ),
            "nested": (
                "class Outer {\n// @SupernoteExportObject\n"
                "class Example { public: Example(); };\n};\n",
                "nested exported classes are not supported",
            ),
        }
        for name, (source, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_ignores_destructor_copy_constructor_and_public_fields(self):
        source = """// @SupernoteExportObject
class Example {
public:
  Example();
  Example(const Example &other);
  Example(Example &&);
  ~Example();
  double (*callback)();
  double value() const { return 1.0; }
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            item = binding_codegen.scan_bindings(module).objects[0]
            self.assertEqual((), item.constructor.parameters)
            self.assertEqual(["value"], [method.js_name for method in item.methods])

    def test_constructor_containing_class_name_is_not_mistaken_for_copy(self):
        source = """// @SupernoteExportObject
class Example {
public:
  Example();
  Example(std::vector<Example> &values);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "unsupported parameter",
            ):
                binding_codegen.scan_bindings(module)

    def test_free_function_annotation_in_header_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernoteExport\ndouble illegal(double value);\n",
            )
            with self.assertRaisesRegex(binding_codegen.CodegenError, "allowed only in .cc"):
                binding_codegen.scan_bindings(module)

    def test_object_manifest_typescript_hostobject_and_lifetime_generation(self):
        source = """#pragma once
#include <string>
// @SupernoteExportObject
class Counter {
public:
  Counter(double initial);
  double value() const noexcept;
  void increment(double amount);
private:
  double value_;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            binding_codegen.generate(module)
            manifest = json.loads((module / "android/build/generated/supernote/exports.json").read_text())
            declarations = (module / "index.d.ts").read_text()
            generated = (module / "android/build/generated/supernote/jni/generated_bindings.cpp").read_text()
            self.assertEqual("Counter", manifest["objects"][0]["cpp_name"])
            self.assertEqual("double", manifest["objects"][0]["constructor"]["parameters"][0]["type"])
            self.assertTrue(manifest["objects"][0]["methods"][0]["const"])
            self.assertIn("export interface Counter {", declarations)
            self.assertIn("value(): number;", declarations)
            self.assertIn("create(initial: number): Counter;", declarations)
            self.assertIn("Counter: CounterFactory;", declarations)
            self.assertIn('#include "model/Counter.hpp"', generated)
            self.assertIn("public facebook::jsi::HostObject", generated)
            self.assertIn("std::shared_ptr<Counter> instance_", generated)
            self.assertIn("std::make_shared<Counter>", generated)
            self.assertIn("Object::createFromHostObject", generated)
            self.assertIn("getPropertyNames", generated)
            self.assertIn("properties.push_back", generated)
            self.assertNotIn("return {\n", generated)
            self.assertIn('property_name == "increment"', generated)
            self.assertIn("[native_instance = std::move(native_instance)]", generated)
            self.assertNotIn("[this]", generated)
            self.assertNotIn("this->", generated)
            self.assertIn("LocalTest.Counter.increment: expected 1 argument", generated)
            self.assertEqual(
                ["add"],
                [
                    item.js_name
                    for item in binding_codegen.generate(module, check=True)
                ],
            )
            self.write_object_header(module, source.replace("increment", "increase"))
            with self.assertRaisesRegex(binding_codegen.CodegenError, "generated bindings are stale"):
                binding_codegen.generate(module, check=True)

    def test_modules_without_objects_emit_empty_manifest_array(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            binding_codegen.generate(module)
            manifest = json.loads((module / "android/build/generated/supernote/exports.json").read_text())
            self.assertEqual([], manifest["objects"])


if __name__ == "__main__":
    unittest.main()
