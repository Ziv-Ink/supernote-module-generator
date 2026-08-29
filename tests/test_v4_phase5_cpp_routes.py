from __future__ import annotations

import json
from pathlib import Path

import pytest

from supernote_module_generator import binding_codegen
from supernote_module_generator.cpp_routes import (
    CppCallableKind,
    CppObjectPassing,
    CppRouteError,
    plan_cpp_routes,
)
from supernote_module_generator.semantic_types import SemanticTypeKind


def _module(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "drawing"
    cpp = root / "android/src/main/cpp"
    cpp.mkdir(parents=True)
    (root / ".supernote-module.json").write_text(
        json.dumps({"feature_id": "supernote:feature:0123456789abcdef"}),
        encoding="utf-8",
    )
    (cpp / "drawing.hpp").write_text(source, encoding="utf-8")
    (cpp / "functions.cpp").write_text(
        """#include <memory>
namespace drawing {
class Stroke;
// @SupernotePluginExport
std::shared_ptr<Stroke> select(std::shared_ptr<Stroke> stroke) { return stroke; }
}
""",
        encoding="utf-8",
    )
    return root


def _plan(tmp_path: Path):
    root = _module(
        tmp_path,
        """#include <memory>
#include <optional>
#include <vector>
namespace drawing {
// @SupernotePluginValue
struct Point {
  // @SupernotePluginExport
  double x;
  // @SupernotePluginExport
  double y;
};

// @SupernotePluginValue
enum class Color { Red, Blue };

// @SupernotePluginObject
class Stroke {
public:
  // @SupernoteConstructor
  explicit Stroke(std::vector<Point> points);
  // @SupernotePluginExport
  static std::shared_ptr<Stroke> empty();
  // @SupernotePluginExport
  bool mutableBorrow(Stroke& other);
  // @SupernotePluginExport
  bool constBorrow(const Stroke& other) const;
  // @SupernotePluginExport
  bool sharedValue(std::shared_ptr<Stroke> other);
  // @SupernotePluginExport
  bool sharedRef(const std::shared_ptr<Stroke>& other) const;
  // @SupernotePluginExport
  std::vector<std::shared_ptr<Stroke>> children() const;
  // @SupernotePluginExport
  // @SupernotePluginAsync
  std::vector<std::shared_ptr<Stroke>> asyncChildren(
      std::vector<std::shared_ptr<Stroke>> children) const;
  // @SupernotePluginExport
  std::shared_ptr<Stroke> child;
};

}
""",
    )
    api = binding_codegen.scan_cpp_semantic_model(root, module_name="Drawing")
    functions = binding_codegen.scan_cpp_source_model(root, module_name="Drawing")
    classes = binding_codegen.scan_cpp_class_source_model(root, module_name="Drawing")
    enums = binding_codegen.scan_cpp_enum_source_model(root, module_name="Drawing")
    return plan_cpp_routes(api, functions, classes, enums), api


def test_cpp_routes_preserve_exact_nominal_types_and_object_passing(tmp_path: Path):
    plan, api = _plan(tmp_path)
    by_name = {item.public_name: item for item in plan.named_types}
    stroke_semantic = next(item for item in api.declarations if item.name == "Stroke")

    assert by_name["Stroke"].type_id == stroke_semantic.type_id
    assert by_name["Stroke"].cpp_type == "::drawing::Stroke"
    assert by_name["Stroke"].kind is SemanticTypeKind.OBJECT_REF
    assert by_name["Point"].kind is SemanticTypeKind.VALUE_REF
    assert by_name["Color"].kind is SemanticTypeKind.ENUM_REF
    assert plan.values[0].named_type.public_name == "Point"
    assert [field.cpp_name for field in plan.values[0].fields] == ["x", "y"]
    assert plan.enums[0].constants == ("Red", "Blue")

    object_route = plan.objects[0]
    assert object_route.constructor is not None
    assert object_route.constructor.kind is CppCallableKind.CONSTRUCTOR
    assert object_route.constructor.result.type_id == stroke_semantic.type_id
    assert object_route.constructor.parameters[0].object_passing is None

    methods = {item.public_name: item for item in object_route.methods}
    assert methods["empty"].kind is CppCallableKind.STATIC_METHOD
    assert methods["mutableBorrow"].parameters[0].object_passing is (
        CppObjectPassing.BORROWED_MUTABLE
    )
    assert methods["constBorrow"].parameters[0].object_passing is (
        CppObjectPassing.BORROWED_CONST
    )
    assert methods["sharedValue"].parameters[0].object_passing is (
        CppObjectPassing.SHARED_VALUE
    )
    assert methods["sharedRef"].parameters[0].object_passing is (
        CppObjectPassing.SHARED_CONST_REF
    )
    assert methods["children"].result.kind is SemanticTypeKind.ARRAY
    assert methods["children"].result.element.kind is SemanticTypeKind.OBJECT_REF
    assert methods["asyncChildren"].execution.value == "async"

    assert object_route.fields[0].cpp_spelling == "std::shared_ptr<Stroke>"
    assert object_route.fields[0].mutable
    assert plan.functions[0].cpp_name == "::drawing::select"
    assert plan.functions[0].parameters[0].object_passing is (
        CppObjectPassing.SHARED_VALUE
    )
    assert plan.functions[0].result.type_id == stroke_semantic.type_id


def test_cpp_route_plan_rejects_stale_or_mismatched_source(tmp_path: Path):
    plan, api = _plan(tmp_path)
    assert plan.objects
    with pytest.raises(CppRouteError, match="missing C\\+\\+ function source"):
        plan_cpp_routes(api, (), (), ())


def test_feature_renderer_emits_nominal_object_routes_through_registry(tmp_path: Path):
    root = _module(
        tmp_path,
        """#include <memory>
namespace drawing {
// @SupernotePluginObject
class Stroke {
public:
  // @SupernoteConstructor
  Stroke();
  // @SupernotePluginExport
  static std::shared_ptr<Stroke> empty();
  // @SupernotePluginExport
  bool intersects(const Stroke& other) const;
  // @SupernotePluginExport
  std::shared_ptr<Stroke> child;
};
}
""",
    )

    source = binding_codegen.render_v4_feature_jsi(
        root,
        module_name="Drawing",
        feature_id="supernote:feature:0123456789abcdef",
        conversion_digest="a" * 64,
        include_prefix="typed-cpp/android/src/main/cpp",
    )

    assert '#include "typed-cpp/android/src/main/cpp/drawing.hpp"' in source
    assert "CppObjectHandle<::drawing::Stroke>" in source
    assert "CppObjectRegistry" in source
    assert "try_extract_cpp_object<" in source
    assert '"supernote:feature:0123456789abcdef:type:Stroke"' in source
    assert "supernote_wrap_v4_object_0" in source
    assert "std::make_shared<::drawing::Stroke>" in source
    assert "native_instance->intersects(*supernote_input_0)" in source
    assert "this->managed_ref()->child = supernote_input_0" in source
    assert "namespace drawing {\nstd::shared_ptr<Stroke> select" in source
    assert "supernote_attach_preflight" in source
    assert 'PropNameID::forAscii(runtime, "select.accepts")' in source
    assert 'PropNameID::forAscii(runtime, "select.checkArguments")' in source
    assert 'object_type.setProperty(runtime, "is"' in source
    assert 'object_type.setProperty(runtime, "check"' in source
    assert '"NOMINAL_MISMATCH", path, "Stroke"' in source
    assert '"__supernoteCppObjectInfo"' in source
    accepts = source.index('PropNameID::forAscii(runtime, "select.accepts")')
    check = source.index('PropNameID::forAscii(runtime, "select.checkArguments")')
    assert "::drawing::select(" not in source[accepts:check]


def test_feature_renderer_emits_recursive_values_arrays_nullable_enums_and_scalars(
    tmp_path: Path,
):
    root = _module(
        tmp_path,
        """#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>
namespace drawing {
// @SupernotePluginValue
enum class Color { Red, Blue };

// @SupernotePluginValue
struct Point {
  // @SupernotePluginExport
  std::int32_t x;
  // @SupernotePluginExport
  std::optional<std::string> label;
  // @SupernotePluginExport
  Color color;
};

// @SupernotePluginObject
class Stroke {
public:
  // @SupernoteConstructor
  explicit Stroke(std::vector<Point> points);
  // @SupernotePluginExport
  std::vector<Point> points() const;
  // @SupernotePluginExport
  std::vector<std::byte> bytes;
  // @SupernotePluginExport
  std::int64_t revision;
};

}
""",
    )
    functions = root / "android/src/main/cpp/functions.cpp"
    functions.write_text(
        functions.read_text(encoding="utf-8")
        + """
// @SupernotePluginExport
std::vector<std::byte> echoBytes(std::vector<std::byte> value) {
  return value;
}
""",
        encoding="utf-8",
    )

    source = binding_codegen.render_v4_feature_jsi(
        root,
        module_name="Drawing",
        feature_id="supernote:feature:0123456789abcdef",
        conversion_digest="b" * 64,
    )

    assert "object.isArray(runtime)" in source
    assert "supernote::conversion::field_path" in source
    assert "supernote::conversion::index_path" in source
    assert "std::optional<std::string>" in source
    assert "::drawing::Color::Red" in source
    assert "const auto bigint = value.getBigInt(runtime)" in source
    assert "bigint.isInt64(runtime)" in source
    assert "BigInt::fromInt64" in source
    assert "supernote_copy_uint8_array" in source
    assert "supernote_array_has_own_index" in source
    own_index = source.index("if (!supernote_array_has_own_index")
    item_read = source.index("array.getValueAtIndex", own_index)
    assert own_index < item_read
    assert source.count('supernote_view_index(runtime, view, "byteOffset")') == 1
    assert source.count('supernote_view_index(runtime, view, "byteLength")') == 1
    first_budget = source.index("budget.check_byte_buffer(path, snapshot.length)")
    allocation = source.index(
        "return supernote_copy_uint8_array(runtime, snapshot)", first_budget
    )
    snapshot = source.rfind(
        "auto snapshot = supernote_snapshot_uint8_array", 0, allocation
    )
    budget = source.index(
        "budget.check_byte_buffer(path, snapshot.length)", snapshot, allocation
    )
    assert snapshot < budget < allocation
    assert source.count("budget.check_byte_buffer(path, snapshot.length)") >= 2
    validator = source.index("budget.check_byte_buffer(path, snapshot.length)")
    validator_snapshot = source.rfind(
        "auto snapshot = supernote_snapshot_uint8_array(runtime, value);",
        0,
        validator,
    )
    assert validator_snapshot != -1
    assert "supernote_copy_uint8_array" not in source[validator_snapshot:validator]
    accepts_start = source.index('"echoBytes.accepts"')
    check_start = source.index('"echoBytes.checkArguments"')
    accepts_end = source.index("\n        })", accepts_start)
    check_end = source.index("\n        })", check_start)
    for preflight in (
        source[accepts_start:accepts_end],
        source[check_start:check_end],
    ):
        assert preflight.count("supernote_validate_js_") == 1
        assert "supernote_copy_uint8_array" not in preflight
        assert "std::vector<std::byte> result" not in preflight
    assert 'range ? "LIMIT_EXCEEDED" : "TYPE_MISMATCH"' in source
    assert "supernote_make_uint8_array" in source
    assert "supernote_v4_throw_conversion_failure" in source
    assert "supernote_validate_js_" in source
    assert 'exports.setProperty(runtime, "Point"' in source
    assert 'exports.setProperty(runtime, "Color"' in source
    assert '"INVALID_ENUM"' in source


def test_recursive_copied_free_function_uses_v4_renderer_without_object_leaf(
    tmp_path: Path,
):
    root = tmp_path / "copied-function"
    cpp = root / "android/src/main/cpp"
    cpp.mkdir(parents=True)
    (root / ".supernote-module.json").write_text(
        json.dumps({"feature_id": "supernote:feature:0123456789abcdef"}),
        encoding="utf-8",
    )
    (cpp / "point.hpp").write_text(
        """namespace drawing {
// @SupernotePluginValue
struct Point {
  // @SupernotePluginExport
  double x;
  // @SupernotePluginExport
  double y;
};
}
""",
        encoding="utf-8",
    )
    (cpp / "point.cpp").write_text(
        """#include "point.hpp"
namespace drawing {
// @SupernotePluginExport
Point echoPoint(Point point) { return point; }
}
""",
        encoding="utf-8",
    )

    source = binding_codegen.render_v4_feature_jsi(
        root,
        module_name="Drawing",
        feature_id="supernote:feature:0123456789abcdef",
        conversion_digest="d" * 64,
    )

    assert 'exports.setProperty(runtime, "echoPoint"' in source
    assert "::drawing::Point supernote_v4_from_js_" in source
    assert "facebook::jsi::Value supernote_v4_to_js_" in source


def test_feature_renderer_emits_async_object_retention_and_js_thread_wrapping(
    tmp_path: Path,
):
    root = _module(
        tmp_path,
        """#include <memory>
#include <vector>
namespace drawing {
// @SupernotePluginObject
class Stroke {
public:
  // @SupernoteConstructor
  Stroke();
  // @SupernotePluginExport
  // @SupernotePluginAsync
  std::vector<std::shared_ptr<Stroke>> echoLater(
      std::vector<std::shared_ptr<Stroke>> strokes) const;
};
}
""",
    )

    source = binding_codegen.render_v4_feature_jsi(
        root,
        module_name="Drawing",
        feature_id="supernote:feature:0123456789abcdef",
        conversion_digest="c" * 64,
    )

    assert "supernote_register_continuation" in source
    assert "retained_objects = std::move(retained_objects)" in source
    assert "retained_input_state = std::make_shared<std::tuple<" in source
    assert "operation->set_retained_state(retained_input_state)" in source
    assert "retained_result" in source
    assert "process_services().workers().submit" in source
    assert "supernote_v4_object_registry(runtime)" in source
    assert "schedule_completion" in source

    argument = source.index("auto supernote_input_0 =")
    retained = source.index("auto retained_input_state =", argument)
    accepted = source.index("accept_factory", retained)
    attached = source.index("set_retained_state", accepted)
    queued = source.index("workers().submit", attached)
    scheduled = source.index("schedule_completion", attached)
    js_identity_lookup = source.index(
        "supernote_v4_object_registry(runtime)", scheduled
    )
    assert argument < retained < accepted < attached < queued
    assert queued < scheduled < js_identity_lookup
    worker_capture = source[queued : source.index("mutable {", queued)]
    assert "facebook::jsi::Runtime" not in worker_capture
    assert "supernote_v4_object_registry" not in worker_capture
    completion = source[scheduled:js_identity_lookup]
    assert "void *runtime_pointer" in completion
