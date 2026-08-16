from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from supernote_module_generator import binding_codegen
from supernote_module_generator.semantic import (
    DeclarationRole,
    ExecutionMode,
    SemanticClassKind,
    SemanticType,
)
from supernote_module_generator.source_models import SupernoteMarker


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

    def test_v2_feature_renderer_has_no_one_shot_jni_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            source = binding_codegen.render_v2_feature_jsi(
                module,
                module_name="LocalTest",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn("void register_feature(", source)
            self.assertIn("feature_registry.setProperty", source)
            self.assertIn("createFromHostFunction", source)
            self.assertNotIn("JNI_OnLoad", source)
            self.assertNotIn("RegisterNatives", source)

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

    def test_cpp_source_and_semantic_models_cover_valid_v2_marker_combinations(self):
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
                self.assertIn("unsupported return type", message)
                self.assertIn(
                    "return one canonical V2 value type by value",
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

    def test_rejects_invalid_v2_free_function_marker_combinations(self):
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
                "initial V2 markers take no arguments",
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

    def test_v2_async_free_function_uses_promise_and_shared_runtime(self):
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
            source = binding_codegen.render_v2_feature_jsi(
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

    def test_v2_async_continuations_are_deleted_from_private_map(self):
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
            source = binding_codegen.render_v2_feature_jsi(
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

    def test_v2_async_object_method_retains_receiver_for_physical_work(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """#include <cstddef>
#include <cstdint>
#include <vector>
// @SupernotePluginExport
class Document {
public:
  Document();
  // @SupernotePluginExport
  // @SupernotePluginAsync
  std::vector<std::byte> load(std::int32_t page);
};
""",
            )

            source = binding_codegen.render_v2_feature_jsi(
                module,
                module_name="Files",
                feature_id="supernote:feature:0123456789abcdef",
            )

            self.assertIn('getPropertyAsFunction(runtime, "Promise")', source)
            self.assertIn(
                "supernote::runtime::ManagedRef<Document> instance_", source
            )
            self.assertIn(
                "supernote::runtime::process_services().cleanup()", source
            )
            self.assertIn("auto operation_receiver = native_instance", source)
            self.assertIn(
                "operation_receiver = std::move(operation_receiver)", source
            )
            self.assertIn("operation_receiver->load(supernote_input_0)", source)
            self.assertIn("weak_feature = feature_session_", source)
            self.assertIn("feature_session->accept_factory", source)
            self.assertNotIn(
                "async C++ object-method lowering is recognized", source
            )

    def test_cpp_class_source_and_semantic_models_use_explicit_member_intent(self):
        source = """// @SupernotePluginExport
class Document {
public:
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
            self.assertEqual(DeclarationRole.EXPORTED, item.intent.role)
            self.assertEqual(1, len(item.constructors))
            self.assertEqual("std::string", item.constructors[0].parameters[0].type_spelling)
            self.assertEqual(["pageCount", "rebuild"], [method.cpp_name for method in item.methods])
            self.assertTrue(item.methods[0].const)
            self.assertTrue(item.methods[0].noexcept)
            self.assertEqual(DeclarationRole.INTERNAL, item.methods[1].intent.role)
            self.assertEqual(ExecutionMode.ASYNC, item.methods[1].intent.execution)

            semantic = binding_codegen.scan_cpp_semantic_model(module)
            self.assertEqual(1, len(semantic.classes))
            document = semantic.classes[0]
            self.assertEqual(SemanticClassKind.JS_OBJECT, document.kind)
            self.assertEqual(SemanticType.STRING, document.constructor.parameters[0].type)
            self.assertEqual(["pageCount", "rebuild"], [method.name for method in document.methods])
            self.assertTrue(document.methods[0].capabilities.javascript_public)
            self.assertFalse(document.methods[1].capabilities.javascript_public)
            self.assertEqual(ExecutionMode.ASYNC, document.methods[1].execution)

    def test_cpp_class_constructor_selection_and_implicit_default(self):
        selected = """// @SupernotePluginExport
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
            semantic = binding_codegen.scan_cpp_semantic_model(module).classes[0]
            self.assertEqual(SemanticType.INT64, semantic.constructor.parameters[0].type)

        implicit = """// @SupernotePluginExport
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
            semantic = binding_codegen.scan_cpp_semantic_model(module).classes[0]
            self.assertEqual((), semantic.constructor.parameters)

    def test_cpp_class_rejects_ambiguous_or_missing_creation_paths(self):
        cases = (
            (
                "ambiguous",
                """// @SupernotePluginExport
class Document {
public:
  Document(std::string path);
  Document(std::int64_t handle);
};
""",
                "multiple eligible constructors require exactly one "
                "SupernoteConstructor selection",
            ),
            (
                "missing",
                """// @SupernotePluginExport
class Document {
private:
  Document();
};
""",
                "requires at least one eligible public constructor",
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

    def test_cpp_internal_class_projects_as_feature_service(self):
        source = """// @SupernotePluginInternal
class IndexService {
public:
  IndexService();

  // @SupernotePluginInternal
  std::int32_t rebuild();

  void ordinaryHelper();
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            service = binding_codegen.scan_cpp_semantic_model(module).classes[0]
            self.assertEqual(SemanticClassKind.INTERNAL_SERVICE, service.kind)
            self.assertFalse(service.capabilities.javascript_public)
            self.assertEqual(["rebuild"], [method.name for method in service.methods])
            self.assertFalse(service.methods[0].capabilities.javascript_public)

    def test_cpp_class_rejects_invalid_marked_members_and_containment(self):
        cases = (
            (
                "unmarked-class",
                """class Document {
public:
  Document();
  // @SupernotePluginExport
  void refresh();
};
""",
                "requires a marked top-level",
            ),
            (
                "private-method",
                """// @SupernotePluginExport
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
            (
                "field",
                """// @SupernotePluginExport
class Document {
public:
  Document();
  // @SupernotePluginExport
  std::int32_t pageCount;
};
""",
                "properties, fields",
            ),
            (
                "static",
                """// @SupernotePluginExport
class Document {
public:
  Document();
  // @SupernotePluginExport
  static void refresh();
};
""",
                "static methods are deferred",
            ),
            (
                "export-on-internal-service",
                """// @SupernotePluginInternal
class Service {
public:
  Service();
  // @SupernotePluginExport
  void refresh();
};
""",
                "may contain only SupernotePluginInternal",
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

    def test_cpp_class_and_member_marker_targets_fail_closed(self):
        cases = (
            (
                "class-role-conflict",
                """// @SupernotePluginExport
// @SupernotePluginInternal
class Document { public: Document(); };
""",
                "SupernotePluginExport and SupernotePluginInternal cannot mark one declaration",
            ),
            (
                "async-class",
                """// @SupernotePluginExport
// @SupernotePluginAsync
class Document { public: Document(); };
""",
                "SupernotePluginAsync cannot mark a class",
            ),
            (
                "constructor-on-class",
                """// @SupernoteConstructor
class Document { public: Document(); };
""",
                "SupernoteConstructor is valid only on a constructor",
            ),
            (
                "async-only-method",
                """// @SupernotePluginExport
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
                """// @SupernotePluginExport
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
                "constructor-on-service",
                """// @SupernotePluginInternal
class Service {
public:
  // @SupernoteConstructor
  Service();
};
""",
                "SupernoteConstructor does not apply to a SupernotePluginInternal",
            ),
            (
                "two-selected-constructors",
                """// @SupernotePluginExport
class Document {
public:
  // @SupernoteConstructor
  Document(std::string path);
  // @SupernoteConstructor
  Document(std::int64_t handle);
};
""",
                "multiple eligible constructors require exactly one",
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

    def test_v2_sync_class_lowers_to_retained_hostobject_machinery(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernotePluginExport\nclass Page { public: Page(); };\n",
            )
            objects = binding_codegen.scan_bindings(module).objects
            self.assertEqual(["Page"], [item.js_name for item in objects])

    def test_object_lowering_fails_closed_for_routes_not_implemented_yet(self):
        cases = (
            (
                "service",
                "// @SupernotePluginInternal\n"
                "class Service { public: Service(); };\n",
                "FeatureSession service route is not implemented yet",
            ),
            (
                "internal-method",
                """// @SupernotePluginExport
class Page {
public:
  Page();
  // @SupernotePluginInternal
  void rebuild();
};
""",
                "receiver-aware internal route is not implemented yet",
            ),
            (
                "async-method",
                """// @SupernotePluginExport
class Page {
public:
  Page();
  // @SupernotePluginExport
  // @SupernotePluginAsync
  void refresh();
};
""",
                "async HostObject lowering is not implemented yet",
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
                    binding_codegen.scan_bindings(module)

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
            ".c": "direct marked C bindings are unsupported in initial V2",
            ".h": "class marker stack must be followed by a complete class",
            ".hh": "class marker stack must be followed by a complete class",
            ".hpp": "class marker stack must be followed by a complete class",
            ".hxx": "class marker stack must be followed by a complete class",
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
            self.assertIn("std::memcpy(result.data()", generated)
            self.assertIn("SupernoteOwnedBytesBuffer", generated)
            self.assertIn("BigInt::fromInt64", generated)
            self.assertIn("supernote_throw_type_error", generated)
            self.assertIn("supernote_throw_range_error", generated)

    def test_jsi_hostobject_uses_initial_numeric_and_bytes_conversions(self):
        source = """#include <cstddef>
#include <cstdint>
#include <vector>
// @SupernotePluginExport
class Page {
public:
  Page(std::int64_t handle, std::vector<std::byte> seed);
  // @SupernotePluginExport
  std::vector<std::byte> render(std::int32_t page, float scale);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            binding_codegen.generate(module)
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text(encoding="utf-8")
            declarations = (module / "index.d.ts").read_text(encoding="utf-8")

            self.assertIn(
                "create(handle: bigint, seed: Uint8Array): Page;",
                declarations,
            )
            self.assertIn(
                "render(page: number, scale: number): Uint8Array;",
                declarations,
            )
            self.assertIn("std::make_shared<Page>", generated)
            self.assertIn("asBigInt(runtime).asInt64(runtime)", generated)
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
        source = """// @SupernotePluginExport
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

    def test_jsi_object_uses_source_struct_name_and_zero_argument_constructor(self):
        source = """// @SupernotePluginExport
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
            item = binding_codegen.scan_bindings(module).objects[0]
            self.assertEqual("NativeDocument", item.cpp_name)
            self.assertEqual("NativeDocument", item.js_name)
            self.assertEqual((), item.constructor.parameters)
            self.assertEqual(["pageCount"], [method.js_name for method in item.methods])

    def test_class_default_private_and_struct_default_public(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class PrivateByDefault {
  PrivateByDefault();
public:
  PrivateByDefault(double value);
  // @SupernotePluginExport
  double value();
};
// @SupernotePluginExport
struct PublicByDefault {
  PublicByDefault();
  // @SupernotePluginExport
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
                "marked method must use one canonical V2 result type",
            ),
            "unsupported-parameter": (
                "double evaluate(int value);",
                "argument 1 must use one named canonical V2 value type",
            ),
            "static": (
                "static double evaluate();",
                "static methods are deferred",
            ),
        }
        for name, (method, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernotePluginExport\nclass Example {\npublic:\n"
                    "  Example();\n"
                    f"  // @SupernotePluginExport\n  {method}\n"
                    "};\n",
                )
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_rejects_method_and_constructor_overloads(self):
        cases = {
            "method": (
                "Example();\n"
                "  // @SupernotePluginExport\n  double value();\n"
                "  // @SupernotePluginExport\n  double value(double fallback);",
                "duplicate generated method name 'value'",
            ),
            "constructor": (
                "Example();\n  Example(double value);",
                "multiple eligible constructors require exactly one",
            ),
        }
        for name, (members, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(
                    module,
                    "// @SupernotePluginExport\nclass Example {\npublic:\n  "
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
                """// @SupernotePluginExport
class add { public: add(); };
""",
            )
            with self.assertRaisesRegex(binding_codegen.CodegenError, "collides with free-function"):
                binding_codegen.scan_bindings(module)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class Thing { public: Thing(); };
""",
                relative="First.hpp",
            )
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class Thing { public: Thing(); };
""",
                relative="Second.hpp",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                re.escape("duplicate exported C++ object name"),
            ):
                binding_codegen.scan_bindings(module)

    def test_object_typescript_factory_name_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class Counter { public: Counter(); };
// @SupernotePluginExport
class CounterFactory { public: CounterFactory(); };
""",
            )
            with self.assertRaises(binding_codegen.CodegenError) as raised:
                binding_codegen.scan_bindings(module)
            message = str(raised.exception)
            self.assertIn("generated TypeScript name 'CounterFactory'", message)
            self.assertIn("object export 'Counter'", message)
            self.assertIn("export 'CounterFactory'", message)
            relative_header = str(Path("model/Counter.hpp"))
            self.assertIn(f"{relative_header}:1", message)
            self.assertIn(f"{relative_header}:3", message)

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
                "SupernoteExportObject is removed in V2",
            ):
                binding_codegen.scan_bindings(module)

    def test_object_typescript_module_interface_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class LocalTestModule { public: LocalTestModule(); };
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
                """// @SupernotePluginExport
class First { public: First(); };
// @SupernotePluginExport
class Second { public: Second(); };
""",
                relative="model/Objects.hpp",
            )
            self.write_object_header(
                module,
                """// @SupernotePluginExport
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
                "// @SupernotePluginExport\nclass Counter { public: Counter(); };\n",
                "supported top-level function definition",
            ),
            (
                "jni",
                "Counter.hpp",
                "// @SupernotePluginExport\nclass Counter { public: Counter(); };\n",
                "require the JSI frontend",
            ),
            (
                "jsi",
                "Counter.hpp",
                "// @SupernotePluginExport(bad)\nclass Counter { public: Counter(); };\n",
                "malformed Supernote marker",
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
                'const char *text = "// @SupernotePluginExport";\n'
                "/* // @SupernotePluginExport */\n"
                "// Documentation mentions @SupernotePluginExport here.\n",
            )
            self.assertEqual((), binding_codegen.scan_bindings(module).objects)
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "#if 0\n// @SupernotePluginExport\n"
                "class Hidden { public: Hidden(); };\n#endif\n",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                "preprocessor conditional",
            ):
                binding_codegen.scan_bindings(module)

    def test_object_rejects_templates_inheritance_and_nested_exports(self):
        cases = {
            "template": (
                "template <typename T>\n// @SupernotePluginExport\n"
                "class Example { public: Example(); };\n",
                "declaration prefix before the class marker",
            ),
            "inheritance": (
                "// @SupernotePluginExport\n"
                "class Example : public Base { public: Example(); };\n",
                "inheritance is not supported",
            ),
            "nested": (
                "class Outer {\n// @SupernotePluginExport\n"
                "class Example { public: Example(); };\n};\n",
                "requires a marked top-level",
            ),
        }
        for name, (source, diagnostic) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = self.make_module(Path(directory), backend="jsi")
                self.write_object_header(module, source)
                with self.assertRaisesRegex(binding_codegen.CodegenError, diagnostic):
                    binding_codegen.scan_bindings(module)

    def test_object_ignores_destructor_copy_constructor_and_public_fields(self):
        source = """// @SupernotePluginExport
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
            item = binding_codegen.scan_bindings(module).objects[0]
            self.assertEqual((), item.constructor.parameters)
            self.assertEqual(["value"], [method.js_name for method in item.methods])

    def test_constructor_containing_class_name_is_not_mistaken_for_copy(self):
        source = """// @SupernotePluginExport
class Example {
public:
  Example();
  Example(std::vector<Example> &values);
};
"""
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(module, source)
            item = binding_codegen.scan_bindings(module).objects[0]
            self.assertEqual((), item.constructor.parameters)

    def test_free_function_annotation_in_header_remains_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            self.write_object_header(
                module,
                "// @SupernotePluginExport\ndouble illegal(double value);\n",
            )
            with self.assertRaisesRegex(
                binding_codegen.CodegenError,
                re.escape("class marker stack must be followed by a class"),
            ):
                binding_codegen.scan_bindings(module)

    def test_object_manifest_typescript_hostobject_and_lifetime_generation(self):
        source = """#pragma once
#include <string>
// @SupernotePluginExport
class Counter {
public:
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
            self.assertNotIn("resetInternalCache", declarations)
            self.assertNotIn('property_name == "resetInternalCache"', generated)
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

    def test_selected_constructor_drives_generated_factory(self):
        source = """#include <string>
// @SupernotePluginExport
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
            binding_codegen.generate(module)
            declarations = (module / "index.d.ts").read_text()
            generated = (
                module
                / "android/build/generated/supernote/jni/generated_bindings.cpp"
            ).read_text()
            self.assertIn("create(path: string): Document;", declarations)
            self.assertIn(
                "std::make_shared<Document>(arguments[0].asString(runtime).utf8(runtime))",
                generated,
            )
            self.assertNotIn("create(handle: number): Document;", declarations)

    def test_modules_without_objects_emit_empty_manifest_array(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi")
            binding_codegen.generate(module)
            manifest = json.loads((module / "android/build/generated/supernote/exports.json").read_text())
            self.assertEqual([], manifest["objects"])

    def test_cli_summary_counts_native_objects_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.make_module(Path(directory), backend="jsi", source="")
            self.write_object_header(
                module,
                """// @SupernotePluginExport
class Counter { public: Counter(); };
""",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                result = binding_codegen.main(["--module-root", str(module)])
            self.assertEqual(0, result)
            self.assertIn(
                "Generated 0 free-function exports and 1 native-object exports",
                output.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
