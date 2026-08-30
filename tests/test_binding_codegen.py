import json
from pathlib import Path
import re
import tempfile
import unittest

from supernote_module_generator import binding_codegen
from supernote_module_generator.semantic import (
    DeclarationRole,
    ExecutionMode,
    MemberScope,
    SemanticDeclarationKind,
    SemanticType,
)
from supernote_module_generator.source_models import DeclarationTarget
from supernote_module_generator.typescript_codegen import render_typescript

class BindingCodegenScannerTests(unittest.TestCase):
    def make_module(
        self,
        root: Path,
        *,
        backend: str = "jni",
        source: str = (
            "// @SupernotePluginExport\n"
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

    def test_generated_binding_scan_lowers_the_current_export_model(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")

            bindings = binding_codegen.scan_generated_bindings(
                module,
                module_name="LocalTest",
            )

            self.assertEqual(["add"], [item.js_name for item in bindings.exports])
            self.assertEqual((), bindings.objects)

    def test_feature_renderer_has_no_one_shot_jni_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            source = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn("void register_feature(", source)
            self.assertIn("feature_registry.setProperty", source)
            self.assertIn("createFromHostFunction", source)
            self.assertNotIn("JNI_OnLoad", source)
            self.assertNotIn("RegisterNatives", source)

    def test_jsi_binding_decision_failures_retain_codegen_error_contract(self):
        config = {
            "android_namespace": "com.example.test",
            "module_name": "LocalTest",
            "class_prefix": "LocalTest",
            "jsi_global_name": "__localTest",
        }
        with self.assertRaisesRegex(
            binding_codegen.CodegenError,
            "invalid feature identity 'invalid'",
        ):
            binding_codegen._jsi_binding(
                config,
                [],
                [],
                feature_id="invalid",
            )

        async_export = binding_codegen.Export(
            "math.cpp",
            1,
            "load",
            "load",
            "double",
            (),
            async_=True,
        )
        with self.assertRaisesRegex(
            binding_codegen.CodegenError,
            "async bindings require plugin-level feature lowering",
        ):
            binding_codegen._jsi_binding(config, [async_export], [])

    def test_feature_renderer_preserves_namespace_for_scalar_function(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                backend="jsi",
                source=(
                    "#include <string>\n"
                    "namespace supernote_feature_LocalTest {\n"
                    "// @SupernotePluginExport\n"
                    "std::string greet(std::string name) { return name; }\n"
                    "}\n"
                ),
            )
            source = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn(
                "namespace supernote_feature_LocalTest {\n"
                "std::string greet(std::string name);\n}",
                source,
            )
            self.assertIn(
                "::supernote_feature_LocalTest::greet(", source
            )
            self.assertNotIn("const auto result = greet(", source)

    def test_bare_export_noexcept_and_lexer_defenses_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                source=(
                    'const char *example = R"tag(// @SupernotePluginExport)tag";\n'
                    "/* // @SupernotePluginExport */\n"
                    "// Documentation mentions @SupernotePluginExport here.\n"
                    "// @SupernotePluginExport\n"
                    "double subtract(\n"
                    "    double left,\n"
                    "    double right) noexcept {\n"
                    "  return left - right;\n"
                    "}\n"
                ),
            )
            exports = binding_codegen.scan_sources(module)
            self.assertEqual(["subtract"], [export.js_name for export in exports])
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

    def test_cpp_source_and_semantic_models_cover_valid_v4_marker_combinations(self):
        cases = (
            (
                "export-sync",
                "// @SupernotePluginExport\n",
                DeclarationRole.EXPORTED,
                ExecutionMode.SYNC,
            ),
            (
                "internal-sync",
                "// @SupernotePluginInternal\n",
                DeclarationRole.INTERNAL,
                ExecutionMode.SYNC,
            ),
            (
                "export-async",
                "  // @SupernotePluginAsync\n\n// @SupernotePluginExport\n",
                DeclarationRole.EXPORTED,
                ExecutionMode.ASYNC,
            ),
            (
                "internal-async",
                "// @SupernotePluginInternal\n  // @SupernotePluginAsync\n",
                DeclarationRole.INTERNAL,
                ExecutionMode.ASYNC,
            ),
        )
        for name, markers, role, execution in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(
                    Path(directory),
                    source=(
                        markers
                        + "std::int32_t pageCount(std::int64_t document) "
                        "noexcept { return document > 0 ? 1 : 0; }\n"
                    ),
                )
                sources = binding_codegen.scan_cpp_source_model(module)
                self.assertEqual(1, len(sources))
                self.assertEqual(role, sources[0].intent.role)
                self.assertEqual(execution, sources[0].intent.execution)
                self.assertEqual("std::int32_t", sources[0].return_type_spelling)
                self.assertEqual(
                    "std::int64_t",
                    sources[0].parameters[0].type_spelling,
                )
                self.assertTrue(sources[0].noexcept)

                semantics = binding_codegen.scan_cpp_semantic_model(module)
                self.assertEqual(1, len(semantics.functions))
                self.assertEqual(role, semantics.functions[0].capabilities.role)
                self.assertEqual(execution, semantics.functions[0].execution)
                self.assertEqual("pageCount", semantics.functions[0].name)

    def test_cpp_source_model_ignores_ordinary_public_code(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                source="double ordinary(double value) { return value; }\n",
            )
            self.assertEqual([], binding_codegen.scan_cpp_source_model(module))
            self.assertEqual(
                (),
                binding_codegen.scan_cpp_semantic_model(module).functions,
            )

    def test_marked_pointer_and_reference_returns_report_boundary_type(self):
        cases = (
            ("pointer", "std::int32_t *", "raw pointers are not supported"),
            ("reference", "std::string &", "references are not supported"),
        )
        for name, result, diagnostic in cases:
            with self.subTest(name=name):
                directory_context = tempfile.TemporaryDirectory()
                self.addCleanup(directory_context.cleanup)
                directory = directory_context.name
                module = self.make_module(
                    Path(directory),
                    source=(
                        "// @SupernotePluginExport\n"
                        f"{result} invalid() {{ throw 1; }}\n"
                    ),
                )
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    diagnostic,
                ) as raised:
                    binding_codegen.scan_cpp_semantic_model(module)

                message = str(raised.exception)
                self.assertIn(f"{diagnostic} as marked C++ results", message)
                self.assertIn(
                    "return one canonical owned generated type",
                    message,
                )
                self.assertNotIn("expected a C++ function name", message)

    def test_cpp_source_identity_does_not_depend_on_blank_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory))
            first = binding_codegen.scan_cpp_source_model(module)[0]
            source = (module / "android/src/main/cpp/math.cpp").read_text(
                encoding="utf-8"
            )
            (module / "android/src/main/cpp/math.cpp").write_text(
                "\n\n" + source,
                encoding="utf-8",
            )
            second = binding_codegen.scan_cpp_source_model(module)[0]
            self.assertEqual(
                first.provenance.declaration_id,
                second.provenance.declaration_id,
            )
            self.assertNotEqual(first.provenance.line, second.provenance.line)

    def test_rejects_invalid_v4_free_function_marker_combinations(self):
        cases = (
            (
                "async-alone",
                "// @SupernotePluginAsync\n",
                "SupernotePluginAsync requires SupernotePluginExport or SupernotePluginInternal",
            ),
            (
                "conflicting-role",
                "// @SupernotePluginExport\n// @SupernotePluginInternal\n",
                "SupernotePluginExport and SupernotePluginInternal cannot mark one declaration",
            ),
            (
                "duplicate",
                "// @SupernotePluginExport\n// @SupernotePluginExport\n",
                "duplicate SupernotePluginExport marker",
            ),
            (
                "constructor",
                "// @SupernoteConstructor\n",
                "SupernoteConstructor is valid only on a constructor",
            ),
            (
                "alias",
                '// @SupernotePluginExport(name = "renamed")\n',
                "generated markers take no arguments",
            ),
            (
                "trailing-text",
                "// @SupernotePluginExport trailing\n",
                "malformed Supernote marker",
            ),
            (
                "unknown",
                "// @SupernoteService\n",
                "unknown Supernote marker 'SupernoteService'",
            ),
            (
                "obsolete-v1-export",
                "// @SupernoteExport\n",
                "unknown Supernote marker 'SupernoteExport'",
            ),
            (
                "obsolete-v1-internal",
                "// @SupernoteInternal\n",
                "unknown Supernote marker 'SupernoteInternal'",
            ),
            (
                "obsolete-v1-async",
                "// @SupernoteAsync\n",
                "unknown Supernote marker 'SupernoteAsync'",
            ),
        )
        for name, markers, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(
                    Path(directory),
                    source=markers + "double value() { return 1.0; }\n",
                )
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ) as raised:
                    binding_codegen.scan_cpp_source_model(module)
                self.assertIn("math.cpp:", str(raised.exception))

    def test_legacy_lowering_fails_closed_for_recognized_unimplemented_routes(self):
        cases = (
            (
                "internal",
                "// @SupernotePluginInternal\ndouble value() { return 1.0; }\n",
                "generated C++ caller route is not implemented yet",
            ),
            (
                "async",
                "// @SupernotePluginExport\n// @SupernotePluginAsync\n"
                "double value() { return 1.0; }\n",
                "async lowering is not implemented yet",
            ),
            (
                "recognized-type",
                "// @SupernotePluginExport\n"
                "std::int32_t value() { return 1; }\n",
                "synchronous JNI conversion is not implemented yet for "
                "std::int32_t",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), source=source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_sources(module)

    def test_async_free_function_uses_promise_and_shared_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                backend="jsi",
                source=(
                    "// @SupernotePluginExport\n"
                    "// @SupernotePluginAsync\n"
                    "std::int64_t load(std::string path) { return 42; }\n"
                ),
            )
            source = binding_codegen.render_feature_jsi(
                module,
                module_name="Files",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn('getPropertyAsFunction(runtime, "Promise")', source)
            self.assertIn("process_services().workers().submit", source)
            self.assertIn("accept_factory", source)
            self.assertIn("schedule_completion", source)
            self.assertIn('"RESOURCE_EXHAUSTED"', source)
            self.assertIn("BigInt::fromInt64", source)
            self.assertIn("operation, operation_id, weak_feature", source)
            self.assertIn("implementation_feature.reset();", source)
            self.assertIn("static_cast<std::size_t>(1)", source)
            self.assertNotIn("jsi::Runtime *runtime", source)

    def test_async_continuations_are_deleted_from_private_map(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                backend="jsi",
                source=(
                    "// @SupernotePluginExport\n"
                    "// @SupernotePluginAsync\n"
                    "double load() { return 1.0; }\n"
                ),
            )
            source = binding_codegen.render_feature_jsi(
                module,
                module_name="Files",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn('getPropertyAsFunction(runtime, "Map")', source)
            self.assertIn('getPropertyAsFunction(runtime, "set")', source)
            self.assertIn('getPropertyAsFunction(runtime, "get")', source)
            self.assertIn('getPropertyAsFunction(runtime, "delete")', source)
            self.assertIn("!removed.isBool() || !removed.getBool()", source)
            self.assertNotIn(
                "key.c_str(), facebook::jsi::Value::undefined()",
                source,
            )

    def test_async_object_method_retains_receiver_for_physical_work(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """#include <cstddef>
#include <cstdint>
#include <vector>
// @SupernotePluginObject
class Document {
public:
  Document();
  // @SupernotePluginExport
  // @SupernotePluginAsync
  std::vector<std::byte> load(std::int32_t page);
};
""",
            )

            source = binding_codegen.render_feature_jsi(
                module,
                module_name="Files",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn('getPropertyAsFunction(runtime, "Promise")', source)
            self.assertIn(
                "supernote::runtime::ManagedRef<::Document> instance", source
            )
            self.assertIn(
                "supernote::runtime::process_services().cleanup()", source
            )
            self.assertIn("native_instance = this->managed_ref()", source)
            self.assertIn("retained_input_state = std::make_shared<std::tuple<", source)
            self.assertIn("native_instance = std::move(native_instance)", source)
            self.assertIn("native_instance->load(supernote_input_0)", source)
            self.assertIn("operation->set_retained_state(retained_input_state)", source)

    def test_cpp_class_source_and_semantic_models_use_explicit_member_intent(self):
        source = """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  Document(std::string path);

  // @SupernotePluginExport
  std::int32_t pageCount() const noexcept;

  // @SupernotePluginInternal
  // @SupernotePluginAsync
  std::vector<std::byte> rebuild(std::int32_t page);

  int unsupportedButOrdinary();
  static void ordinaryStatic();
private:
  void privateHelper();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            classes = binding_codegen.scan_cpp_class_source_model(module)
            self.assertEqual(1, len(classes))
            item = classes[0]
            self.assertEqual("Document", item.cpp_name)
            self.assertEqual(DeclarationRole.ORDINARY, item.intent.role)
            self.assertEqual(1, len(item.constructors))
            self.assertEqual("std::string", item.constructors[0].parameters[0].type_spelling)
            self.assertEqual(["pageCount", "rebuild"], [method.cpp_name for method in item.methods])
            self.assertTrue(item.methods[0].const)
            self.assertTrue(item.methods[0].noexcept)
            self.assertEqual(DeclarationRole.INTERNAL, item.methods[1].intent.role)
            self.assertEqual(ExecutionMode.ASYNC, item.methods[1].intent.execution)

            semantic = binding_codegen.scan_cpp_semantic_model(module)
            self.assertEqual(1, len(semantic.declarations))
            document = semantic.declarations[0]
            self.assertEqual(SemanticDeclarationKind.OBJECT, document.kind)
            self.assertEqual(SemanticType.STRING, document.constructor.parameters[0].type)
            self.assertEqual(["pageCount", "rebuild"], [method.name for method in document.methods])
            self.assertTrue(document.methods[0].capabilities.javascript_public)
            self.assertFalse(document.methods[1].capabilities.javascript_public)
            self.assertEqual(ExecutionMode.ASYNC, document.methods[1].execution)

    def test_header_only_constructor_member_initializer_is_supported(self):
        source = """// @SupernotePluginObject
class Counter {
public:
  // @SupernoteConstructor
  explicit Counter(int initial) : value_(initial) {}

  // @SupernotePluginExport
  int value() const { return value_; }

private:
  int value_;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)

            classes = binding_codegen.scan_cpp_class_source_model(module)

            self.assertEqual(1, len(classes))
            self.assertEqual("Counter", classes[0].cpp_name)
            self.assertEqual(1, len(classes[0].constructors))
            self.assertEqual("initial", classes[0].constructors[0].parameters[0].name)
            self.assertEqual(["value"], [method.cpp_name for method in classes[0].methods])

    def test_marked_cpp_class_parse_context_is_an_immutable_snapshot(self):
        module_root = Path("/module")
        path = module_root / "android/src/main/cpp/model/Counter.hpp"
        text = (
            "namespace example {\n"
            "// @SupernotePluginObject\n"
            "class Counter {};\n"
            "}\n"
        )
        lexed = binding_codegen._lex_source(text)
        entries = binding_codegen._marker_entries(
            module_root,
            path,
            lexed,
            "LocalTest",
        )
        stack = binding_codegen._marker_stacks(text, entries)[0]
        active_tokens = [
            token for token in lexed.tokens if token.conditional_depth == 0
        ]

        context = binding_codegen._class_parse_context(
            module_root=module_root,
            path=path,
            lexed=lexed,
            active_tokens=active_tokens,
            class_stack=stack,
            class_token=None,
            module_name="LocalTest",
        )

        self.assertEqual(("example",), context.namespace)
        self.assertEqual(DeclarationTarget.CLASS, context.intent.target)
        self.assertEqual(2, context.diagnostic_line)
        self.assertIsInstance(context.following, tuple)
        self.assertEqual(
            ("class", "Counter", "{", "}", ";", "}"),
            tuple(token.value for token in context.following),
        )
        with self.assertRaises(AttributeError):
            context.following.append(active_tokens[0])
        with self.assertRaises(TypeError):
            context.following[0] = active_tokens[0]

    def test_unmarked_cpp_class_parse_context_is_an_immutable_snapshot(self):
        module_root = Path("/module")
        path = module_root / "android/src/main/cpp/model/Counter.hpp"
        text = (
            "namespace example {\n"
            "class Counter {\n"
            "public:\n"
            "  // @SupernotePluginExport\n"
            "  double value();\n"
            "};\n"
            "}\n"
        )
        lexed = binding_codegen._lex_source(text)
        active_tokens = [
            token for token in lexed.tokens if token.conditional_depth == 0
        ]
        class_token = next(
            token for token in active_tokens if token.value == "class"
        )

        context = binding_codegen._class_parse_context(
            module_root=module_root,
            path=path,
            lexed=lexed,
            active_tokens=active_tokens,
            class_stack=None,
            class_token=class_token,
            module_name="LocalTest",
        )

        self.assertEqual(("example",), context.namespace)
        self.assertEqual(DeclarationTarget.CLASS, context.intent.target)
        self.assertEqual((), context.intent.occurrences)
        self.assertEqual(2, context.diagnostic_line)
        self.assertIsInstance(context.following, tuple)
        self.assertEqual("class", context.following[0].value)
        self.assertEqual("}", context.following[-1].value)
        with self.assertRaises(AttributeError):
            context.following.append(active_tokens[0])
        with self.assertRaises(TypeError):
            context.following[0] = active_tokens[0]

    def test_cpp_class_constructor_selection_and_implicit_default(self):
        selected = """// @SupernotePluginObject
class Document {
public:
  Document(std::string path);
  // @SupernoteConstructor
  explicit Document(std::int64_t handle) noexcept;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, selected)
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertTrue(item.constructors[1].selected)
            self.assertTrue(item.constructors[1].explicit)
            self.assertTrue(item.constructors[1].noexcept)
            semantic = binding_codegen.scan_cpp_semantic_model(module).declarations[0]
            self.assertEqual(SemanticType.INT64, semantic.constructor.parameters[0].type)

        implicit = """// @SupernotePluginObject
struct Page {
  // @SupernotePluginExport
  void refresh();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, implicit)
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertTrue(item.constructors[0].implicit)
            semantic = binding_codegen.scan_cpp_semantic_model(module).declarations[0]
            self.assertIsNone(semantic.constructor)

    def test_cpp_class_rejects_ambiguous_or_missing_creation_paths(self):
        cases = (
            (
                "ambiguous",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  Document(std::string path);
  // @SupernoteConstructor
  Document(std::int64_t handle);
};
""",
                "an object may select at most one SupernoteConstructor",
            ),
            (
                "missing",
                """// @SupernotePluginObject
class Document {
private:
  // @SupernoteConstructor
  Document();
};
""",
                "SupernoteConstructor must mark a public constructor",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_cpp_constructor_lowering_preserves_validation_precedence(self):
        cases = (
            (
                "unsupported-prefix-before-copy",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  const Document(const Document &other);
};
""",
                "a generated constructor may use only the optional explicit modifier",
            ),
            (
                "parameter-before-suffix",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  Document(double) const;
};
""",
                "unsupported parameter 'double'; argument 1 must use one named",
            ),
            (
                "marker-intent-before-access",
                """// @SupernotePluginObject
class Document {
private:
  // @SupernotePluginExport
  Document();
};
""",
                "constructors accept only SupernoteConstructor",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_cpp_class_rejects_invalid_marked_members_and_containment(self):
        cases = (
            (
                "private-method",
                """// @SupernotePluginObject
class Document {
public:
  Document();
private:
  // @SupernotePluginExport
  void refresh();
};
""",
                "generated method must be public",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_cpp_method_lowering_preserves_validation_precedence(self):
        cases = (
            (
                "destructor-before-intent",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  ~Document();
};
""",
                "destructors cannot be generated members",
            ),
            (
                "intent-before-access",
                """// @SupernotePluginObject
class Document {
private:
  // @SupernotePluginAsync
  void refresh();
};
""",
                "SupernotePluginAsync requires SupernotePluginExport",
            ),
            (
                "access-before-shape",
                """// @SupernotePluginObject
class Document {
private:
  // @SupernotePluginExport
  virtual void refresh();
};
""",
                "a generated method must be public in C++",
            ),
            (
                "parameter-before-suffix",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernotePluginExport
  void refresh(double) FINAL;
};
""",
                "unsupported parameter 'double'; argument 1 must use one named",
            ),
            (
                "duplicate-before-second-parameters",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernotePluginExport
  void refresh();
  // @SupernotePluginExport
  void refresh(double);
};
""",
                "duplicate generated method name 'refresh'",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_cpp_class_member_dispatch_preserves_missing_parenthesis_policy(self):
        unmarked = """// @SupernotePluginObject
class Document {
public:
  void broken(double;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, unmarked)
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertEqual((), item.methods)
            self.assertTrue(item.constructors[0].implicit)

        marked = """// @SupernotePluginObject
class Document {
public:
  // @SupernotePluginExport
  void broken(double;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, marked)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                re.escape("missing ')' in marked member declaration"),
            ):
                binding_codegen.scan_cpp_class_source_model(module)

    def test_cpp_class_and_member_marker_targets_fail_closed(self):
        cases = (
            (
                "object-with-reachability-marker",
                """// @SupernotePluginObject
// @SupernotePluginInternal
class Document { public: Document(); };
""",
                "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ),
            (
                "async-class",
                """// @SupernotePluginObject
// @SupernotePluginAsync
class Document { public: Document(); };
""",
                "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ),
            (
                "constructor-on-class",
                """// @SupernoteConstructor
class Document { public: Document(); };
""",
                "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ),
            (
                "async-only-method",
                """// @SupernotePluginObject
class Document {
public:
  Document();
  // @SupernotePluginAsync
  void refresh();
};
""",
                "SupernotePluginAsync requires SupernotePluginExport or SupernotePluginInternal",
            ),
            (
                "constructor-on-method",
                """// @SupernotePluginObject
class Document {
public:
  Document();
  // @SupernoteConstructor
  void refresh();
};
""",
                "SupernoteConstructor is valid only on a constructor",
            ),
            (
                "two-selected-constructors",
                """// @SupernotePluginObject
class Document {
public:
  // @SupernoteConstructor
  Document(std::string path);
  // @SupernoteConstructor
  Document(std::int64_t handle);
};
""",
                "an object may select at most one SupernoteConstructor",
            ),
        )
        for name, source, diagnostic in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_sync_class_lowers_to_retained_hostobject_machinery(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernotePluginObject\nclass Page { public: Page(); };\n",
            )
            declarations = binding_codegen.scan_cpp_semantic_model(module).declarations
            self.assertEqual(["Page"], [item.name for item in declarations])

    def test_rejects_marker_in_preprocessor_conditional(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(
                Path(directory),
                source=(
                    "#if 0\n"
                    "// @SupernotePluginExport\n"
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
                "// @SupernotePluginExport\n"
                "double hidden(double value) { return value; }\n",
                "declaration prefix before the marker",
            ),
            "after-inline": (
                "// @SupernotePluginExport\n"
                "inline double hidden(double value) { return value; }\n",
                "modifier 'inline' is forbidden",
            ),
            "attribute": (
                "// @SupernotePluginExport\n"
                "[[nodiscard]] double hidden(double value) { return value; }\n",
                r"modifier '\[\[' is forbidden",
            ),
            "declaration": (
                "// @SupernotePluginExport\n"
                "double hidden(double value);\n",
                "tagged declarations are not exported",
            ),
            "suffix": (
                "// @SupernotePluginExport\n"
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

    def test_rejects_markers_in_c_headers_and_helper_suffixes(self):
        cases = {
            ".c": "direct marked C bindings are unsupported",
            ".h": "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ".hh": "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ".hpp": "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ".hxx": "classes require exactly one of SupernotePluginObject or SupernotePluginValue",
            ".inl": "allowed only in .cc, .cpp, or .cxx",
            ".inc": "allowed only in .cc, .cpp, or .cxx",
            ".ipp": "allowed only in .cc, .cpp, or .cxx",
            ".tpp": "allowed only in .cc, .cpp, or .cxx",
        }
        for suffix, diagnostic in cases.items():
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory))
                (module / f"android/src/main/cpp/forbidden{suffix}").write_text(
                    "// @SupernotePluginExport\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    binding_codegen.CodegenError,
                    re.escape(diagnostic),
                ):
                    if suffix in {".h", ".hh", ".hpp", ".hxx"}:
                        binding_codegen.scan_cpp_class_source_model(module)
                    else:
                        binding_codegen.scan_cpp_source_model(module)

    def test_jni_reserved_names_do_not_apply_to_jsi(self):
        source = (
            "// @SupernotePluginExport\n"
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

    def test_cpp_only_bindable_type_reports_the_v4_header_policy(self):
        source = (
            "// @SupernotePluginObject\n"
            "class Counter {\n"
            "public:\n"
            "  Counter();\n"
            "};\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "complete structural definition in a header.*implemented out-of-line",
            ):
                binding_codegen.scan_cpp_semantic_model(module)

    def test_namespaced_untagged_overload_is_rejected_at_namespace_depth(self):
        source = (
            "namespace sample {\n"
            "// @SupernotePluginExport\n"
            "double choose(double value) { return value; }\n"
            "double choose(int value) { return value; }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "untagged global definition.*overloads",
            ):
                binding_codegen.scan_cpp_semantic_model(module)

    def test_cpp23_keywords_are_rejected_for_both_backends(self):
        for backend in ("jni", "jsi"):
            for keyword in ("class", "nullptr", "co_await"):
                cases = (
                    (
                        f"function-{keyword}",
                        "// @SupernotePluginExport\n"
                        f"double {keyword}() {{ return 0.0; }}\n",
                        f"C++ function name {keyword!r} is a C++23 keyword",
                    ),
                    (
                        f"parameter-{keyword}",
                        "// @SupernotePluginExport\n"
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
                "// @SupernotePluginExport\n"
                "double getName() { return 1.0; }\n",
                "collides with a generated Kotlin method",
            ),
            "initialize": (
                "// @SupernotePluginExport\n"
                "void initialize() {}\n",
                "collides with a generated Kotlin method",
            ),
            "promise": (
                "// @SupernotePluginExport\n"
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

    def test_jni_free_function_generates_native_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jni")
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")

            self.assertIn("JNINativeMethod methods[]", generated)
            self.assertIn(
                '{const_cast<char *>("native0"), const_cast<char *>("(DD)D")',
                generated,
            )
            self.assertIn("reinterpret_cast<void *>(native_0)", generated)
            self.assertIn(
                "env->RegisterNatives(module_class, methods, method_count)",
                generated,
            )

    def test_jsi_sync_free_function_generates_host_function_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            declarations = (module / "index.d.ts").read_text(encoding="utf-8")

            self.assertIn("Function::createFromHostFunction", generated)
            self.assertIn(
                'PropNameID::forAscii(runtime, "add")',
                generated,
            )
            self.assertIn("if (argument_count != 2)", generated)
            self.assertIn(
                'exports.setProperty(runtime, "add", std::move(function))',
                generated,
            )
            self.assertIn("add(left: number, right: number): number;", declarations)
            self.assertNotIn("Promise<number>", declarations)

    def test_jsi_initial_numeric_and_bytes_types_generate_checked_conversions(self):
        source = """#include <cstddef>
#include <cstdint>
#include <vector>
// @SupernotePluginExport
std::vector<std::byte> convert(
    std::int32_t page,
    std::int64_t offset,
    float scale,
    std::vector<std::byte> bytes) {
  return bytes;
}
// @SupernotePluginExport
std::int64_t identity(std::int64_t value) { return value; }
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi", source=source)
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            declarations = (module / "index.d.ts").read_text(encoding="utf-8")

            self.assertIn(
                "convert(page: number, offset: bigint, scale: number, "
                "bytes: Uint8Array): Uint8Array;",
                declarations,
            )
            self.assertIn("identity(value: bigint): bigint;", declarations)
            self.assertIn("std::trunc(supernote_argument_0)", generated)
            self.assertIn("numeric_limits<std::int32_t>::min()", generated)
            self.assertIn("getBigInt(runtime).isInt64(runtime)", generated)
            self.assertIn("asBigInt(runtime).asInt64(runtime)", generated)
            self.assertIn("numeric_limits<float>::lowest()", generated)
            self.assertIn("supernote_is_uint8_array(runtime", generated)
            self.assertIn("supernote_copy_uint8_array(runtime", generated)
            self.assertIn('supernote_view_index(runtime, view, "byteOffset")', generated)
            self.assertIn('supernote_view_index(runtime, view, "byteLength")', generated)
            self.assertIn("std::memcpy(\n        result.data()", generated)
            self.assertIn("SupernoteOwnedBytesBuffer", generated)
            self.assertIn("BigInt::fromInt64", generated)
            self.assertIn("supernote_throw_type_error", generated)
            self.assertIn("supernote_throw_range_error", generated)
            self.assertIn(
                '"LIMIT_EXCEEDED",\n        path,',
                generated,
            )
            self.assertEqual(
                generated.count(
                    "auto supernote_snapshot_3 = "
                    "supernote_snapshot_uint8_array("
                ),
                1,
            )
            self.assertIn(
                "supernote_copy_uint8_array(runtime, supernote_snapshot_3)",
                generated,
            )
            snapshot = generated.index("auto supernote_snapshot_3 =")
            limit = generated.index(
                "supernote_check_uint8_array_snapshot_limit(", snapshot
            )
            copy = generated.index(
                "supernote_copy_uint8_array(runtime, supernote_snapshot_3)",
                limit,
            )
            self.assertLess(snapshot, limit)
            self.assertLess(limit, copy)

    def test_jsi_hostobject_uses_initial_numeric_and_bytes_conversions(self):
        source = """#include <cstddef>
#include <cstdint>
#include <vector>
// @SupernotePluginObject
class Page {
public:
  // @SupernoteConstructor
  Page(std::int64_t handle, std::vector<std::byte> seed);
  // @SupernotePluginExport
  std::vector<std::byte> render(std::int32_t page, float scale);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            api = binding_codegen.scan_cpp_semantic_model(module)
            generated = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )
            declarations = render_typescript("LocalTest", api)

            self.assertIn(
                "create: SupernoteCallable<[handle: bigint, seed: Uint8Array], Page>;",
                declarations,
            )
            self.assertIn(
                "render: SupernoteCallable<[page: number, scale: number], Uint8Array>;",
                declarations,
            )
            self.assertIn("std::make_shared<::Page>", generated)
            self.assertIn("bigint.asInt64(runtime)", generated)
            self.assertIn("native_instance->render(", generated)
            self.assertIn("supernote_make_uint8_array(runtime", generated)

    def test_generated_errors_include_module_export_and_jsi_argument_details(self):
        source = (
            "// @SupernotePluginExport\n"
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
            self.assertIn('runtime, "IMPLEMENTATION_ERROR"', generated)
            self.assertIn('runtime, "INTERNAL"', generated)
            self.assertIn("__supernoteErrorConstructor", generated)
            self.assertIn("LocalTest.add: unknown C++ exception", generated)

            declarations = (module / "index.d.ts").read_text(encoding="utf-8")
            self.assertIn("export type SupernoteErrorCode =", declarations)
            for code in (
                "RESOURCE_EXHAUSTED",
                "CANCELLED",
                "FEATURE_CLOSED",
                "IMPLEMENTATION_ERROR",
                "INTERNAL",
            ):
                self.assertIn(f'\"{code}\"', declarations)
            self.assertIn("export class SupernoteError extends Error", declarations)
            self.assertIn("readonly code: SupernoteErrorCode", declarations)

    def test_rejects_user_owned_jni_on_load(self):
        source = (
            'extern "C" int JNI_OnLoad(void *, void *) { return 0; }\n'
            "// @SupernotePluginExport\n"
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
            "// @SupernotePluginExport\n"
            "double add(double left, double right) { return left + right; }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), source=source)
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_sources(module)
            message = str(raised.exception)
            self.assertIn("math.cpp:1", message)
            self.assertIn("untagged global definition", message)
            self.assertIn("routable C++ name 'add'", message)

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
            "// @SupernotePluginExport\n"
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
        source = """// @SupernotePluginObject
class Counter {
public:
  Counter(bool enabled, double initial, std::string label);
  // @SupernotePluginExport
  bool enabled() const;
  // @SupernotePluginExport
  double value() const noexcept;
  // @SupernotePluginExport
  std::string label() noexcept;
  // @SupernotePluginExport
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
            exports = binding_codegen.scan_sources(module)
            objects = binding_codegen.scan_cpp_class_source_model(module)
            self.assertEqual(["add"], [item.js_name for item in exports])
            self.assertEqual(["Counter"], [item.cpp_name for item in objects])
            item = objects[0]
            self.assertEqual(
                ["bool", "double", "std::string"],
                [parameter.type_spelling for parameter in item.constructors[0].parameters],
            )
            self.assertEqual(
                ["enabled", "value", "label", "increment"],
                [method.cpp_name for method in item.methods],
            )
            self.assertTrue(item.methods[0].const)
            self.assertTrue(item.methods[1].const)
            self.assertTrue(item.methods[1].noexcept)
            self.assertTrue(item.methods[2].noexcept)

    def test_jsi_object_uses_source_struct_name_and_zero_argument_constructor(self):
        source = """// @SupernotePluginObject
struct NativeDocument {
  NativeDocument();
  // @SupernotePluginExport
  double pageCount() const;
private:
  int hidden();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source, relative="NativeDocument.hxx")
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertEqual("NativeDocument", item.cpp_name)
            self.assertEqual((), item.constructors[0].parameters)
            self.assertEqual(["pageCount"], [method.cpp_name for method in item.methods])

    def test_class_default_private_and_struct_default_public(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class PrivateByDefault {
  PrivateByDefault();
public:
  PrivateByDefault(double value);
  // @SupernotePluginExport
  double value();
};
// @SupernotePluginObject
struct PublicByDefault {
  PublicByDefault();
  // @SupernotePluginExport
  void reset();
};
""",
                relative="access.hh",
            )
            objects = binding_codegen.scan_cpp_class_source_model(module)
            self.assertEqual(["PrivateByDefault", "PublicByDefault"], [item.cpp_name for item in objects])
            self.assertEqual(
                ["private", "public"],
                [item.access for item in objects[0].constructors],
            )
            self.assertEqual(1, len(objects[0].constructors[1].parameters))
            self.assertEqual(0, len(objects[1].constructors[0].parameters))

    def test_object_rejects_unsupported_types_and_accepts_static_methods(self):
        cases = {
            "unsupported-return": (
                "int unsupported();",
                "unsupported marked C\\+\\+ type 'int'",
            ),
            "unsupported-parameter": (
                "double evaluate(int value);",
                "unsupported marked C\\+\\+ type 'int'",
            ),
        }
        for name, (method, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernotePluginObject\nclass Example {\npublic:\n"
                    "  Example();\n"
                    f"  // @SupernotePluginExport\n  {method}\n"
                    "};\n",
                )
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_cpp_semantic_model(module)

        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernotePluginObject\nclass Example {\npublic:\n"
                "  // @SupernotePluginExport\n  static double evaluate();\n};\n",
            )
            item = binding_codegen.scan_cpp_semantic_model(module).declarations[0]
            self.assertEqual(MemberScope.STATIC, item.methods[0].member_scope)

    def test_object_rejects_method_and_constructor_overloads(self):
        cases = {
            "method": (
                "Example();\n"
                "  // @SupernotePluginExport\n  double value();\n"
                "  // @SupernotePluginExport\n  double value(double fallback);",
                "duplicate generated method name 'value'",
            ),
            "constructor": (
                "// @SupernoteConstructor\n  Example();\n"
                "  // @SupernoteConstructor\n  Example(double value);",
                "an object may select at most one SupernoteConstructor",
            ),
        }
        for name, (members, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernotePluginObject\nclass Example {\npublic:\n  "
                    + members
                    + "\n};\n",
                )
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_object_export_name_collisions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class add {
public:
  // @SupernoteConstructor
  add();
};
""",
            )
            api = binding_codegen.scan_cpp_semantic_model(module)
            with self.assertRaisesRegex(ValueError, "collid|duplicate"):
                render_typescript("LocalTest", api)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class Thing {
public:
  // @SupernoteConstructor
  Thing();
};
""",
                relative="First.hpp",
            )
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class Thing {
public:
  // @SupernoteConstructor
  Thing();
};
""",
                relative="Second.hpp",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                re.escape("duplicate marked C++ type definition"),
            ):
                binding_codegen.scan_cpp_semantic_model(module)

    def test_object_named_factory_does_not_collide_with_another_object(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class Counter {
public:
  // @SupernoteConstructor
  Counter();
};
// @SupernotePluginObject
class CounterFactory {
public:
  // @SupernoteConstructor
  CounterFactory();
};
""",
            )
            api = binding_codegen.scan_cpp_semantic_model(module)
            declarations = render_typescript("LocalTest", api)
            self.assertIn("export interface Counter {", declarations)
            self.assertIn("export interface CounterFactory {", declarations)

    def test_v1_object_marker_and_alias_syntax_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernoteExportObject(name = "Counter")
class NativeCounter { public: NativeCounter(); };
""",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "SupernoteExportObject is removed",
            ):
                binding_codegen.scan_bindings(module)

    def test_object_name_no_longer_collides_with_legacy_module_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class LocalTestModule {
public:
  // @SupernoteConstructor
  LocalTestModule();
};
""",
            )
            api = binding_codegen.scan_cpp_semantic_model(module)
            declarations = render_typescript("LocalTest", api)
            self.assertIn("export interface LocalTestModule {", declarations)
            self.assertIn("export interface LocalTestFeature {", declarations)

    def test_generated_object_header_includes_are_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class First { public: First(); };
// @SupernotePluginObject
class Second { public: Second(); };
""",
                relative="model/Objects.hpp",
            )
            self.write_object_header(
                module,
                """// @SupernotePluginObject
class Third { public: Third(); };
""",
                relative="other/Third.hh",
            )
            generated = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )
            self.assertEqual(1, generated.count('#include "model/Objects.hpp"'))
            self.assertEqual(1, generated.count('#include "other/Third.hh"'))

    def test_object_annotation_location_backend_and_malformed_diagnostics(self):
        cases = (
            (
                "Counter.cpp",
                "// @SupernotePluginObject\nclass Counter { public: Counter(); };\n",
                "complete structural definition in a header",
            ),
            (
                "Counter.hpp",
                "// @SupernotePluginObject(bad)\nclass Counter { public: Counter(); };\n",
                "malformed Supernote marker",
            ),
        )
        for relative, source, diagnostic in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source, relative=relative)
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_object_marker_lexer_defenses_and_conditional_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                'const char *text = "// @SupernotePluginObject";\n'
                "/* // @SupernotePluginObject */\n"
                "// Documentation mentions @SupernotePluginObject here.\n",
            )
            self.assertEqual([], binding_codegen.scan_cpp_class_source_model(module))
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "#if 0\n// @SupernotePluginObject\n"
                "class Hidden { public: Hidden(); };\n#endif\n",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "preprocessor conditional",
            ):
                binding_codegen.scan_cpp_semantic_model(module)

    def test_object_rejects_templates_inheritance_and_nested_exports(self):
        cases = {
            "template": (
                "template <typename T>\n// @SupernotePluginObject\n"
                "class Example { public: Example(); };\n",
                "declaration prefix before the class marker",
            ),
            "inheritance": (
                "// @SupernotePluginObject\n"
                "class Example : public Base { public: Example(); };\n",
                "inheritance is not supported",
            ),
            "nested": (
                "class Outer {\n// @SupernotePluginObject\n"
                "class Example { public: Example(); };\n};\n",
                "marked C\\+\\+ types must be at global or named-namespace brace depth",
            ),
        }
        for name, (source, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_cpp_semantic_model(module)

    def test_object_ignores_destructor_copy_constructor_and_public_fields(self):
        source = """// @SupernotePluginObject
class Example {
public:
  Example();
  Example(const Example &other);
  Example(Example &&);
  ~Example();
  double (*callback)();
  // @SupernotePluginExport
  double value() const { return 1.0; }
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertTrue(item.constructors[0].implicit is False)
            self.assertEqual(["value"], [method.cpp_name for method in item.methods])

    def test_constructor_containing_class_name_is_not_mistaken_for_copy(self):
        source = """// @SupernotePluginObject
class Example {
public:
  Example();
  Example(std::vector<Example> &values);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            item = binding_codegen.scan_cpp_class_source_model(module)[0]
            self.assertEqual(2, len(item.constructors))
            self.assertEqual(
                "std::vector<Example>&",
                item.constructors[1].parameters[0].type_spelling,
            )

    def test_free_function_annotation_in_header_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernotePluginExport\ndouble illegal(double value);\n",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                re.escape(
                    "classes require exactly one of SupernotePluginObject or "
                    "SupernotePluginValue"
                ),
            ):
                binding_codegen.scan_bindings(module)

    def test_object_manifest_typescript_hostobject_and_lifetime_generation(self):
        source = """#pragma once
#include <string>
// @SupernotePluginObject
class Counter {
public:
  // @SupernoteConstructor
  Counter(double initial);
  // @SupernotePluginExport
  double value() const noexcept;
  // @SupernotePluginExport
  void increment(double amount);
  void resetInternalCache();
private:
  double value_;
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            api = binding_codegen.scan_cpp_semantic_model(module)
            manifest = api.manifest()
            declarations = render_typescript("LocalTest", api)
            generated = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )
            counter = manifest["types"][0]
            self.assertEqual("Counter", counter["name"])
            self.assertEqual("float64", counter["constructor"]["parameters"][0]["type"]["name"])
            self.assertIn("export interface Counter {", declarations)
            self.assertIn("value: SupernoteCallable<[], number>;", declarations)
            self.assertIn("create: SupernoteCallable<[initial: number], Counter>;", declarations)
            self.assertIn("Counter: SupernoteTypeCompanion<Counter>", declarations)
            self.assertIn('#include "model/Counter.hpp"', generated)
            self.assertIn("public supernote::runtime::CppObjectHandle<::Counter>", generated)
            self.assertIn("ManagedRef<::Counter>", generated)
            self.assertIn("std::make_shared<::Counter>", generated)
            self.assertIn("Object::createFromHostObject", generated)
            self.assertIn("getPropertyNames", generated)
            self.assertIn("properties.push_back", generated)
            self.assertIn('property_name == "increment"', generated)
            self.assertIn("native_instance->increment", generated)
            self.assertNotIn("resetInternalCache", declarations)
            self.assertNotIn('property_name == "resetInternalCache"', generated)

    def test_selected_constructor_drives_generated_factory(self):
        source = """#include <string>
// @SupernotePluginObject
class Document {
public:
  Document(double handle);
  // @SupernoteConstructor
  explicit Document(std::string path);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            api = binding_codegen.scan_cpp_semantic_model(module)
            declarations = render_typescript("LocalTest", api)
            generated = binding_codegen.render_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )
            self.assertIn("create: SupernoteCallable<[path: string], Document>;", declarations)
            self.assertIn("std::make_shared<::Document>", generated)
            self.assertNotIn("[handle: number]", declarations)

    def test_modules_without_objects_emit_empty_manifest_array(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            binding_codegen.generate(module)
            manifest = json.loads((module / "android/build/generated/supernote/exports.json").read_text())
            self.assertEqual([], manifest["objects"])

if __name__ == "__main__":
    unittest.main()
