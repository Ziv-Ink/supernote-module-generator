import json
from pathlib import Path

from supernote_module_generator.internal_codegen import (
    internal_header_path,
    render_cpp_internal_facade,
)


FEATURE_ID = "supernote:feature:0123456789abcdef"


def module(tmp_path: Path) -> Path:
    root = tmp_path / "feature"
    source = root / "android/src/main/cpp"
    source.mkdir(parents=True)
    (root / "module.json").write_text(
        json.dumps({"module_name": "Documents", "backend": "jsi"})
    )
    (source / "internal.hpp").write_text(
        """#pragma once
#include <cstdint>
class IndexService {
public:
  IndexService();
  // @SupernotePluginInternal
  std::int32_t rebuild(std::int32_t page);
};
"""
    )
    (source / "internal.cpp").write_text(
        """#include "internal.hpp"
// @SupernotePluginInternal
std::int32_t pageCount(std::int32_t offset) { return offset + 1; }

// @SupernotePluginInternal
// @SupernotePluginAsync
std::int32_t loadIndex(std::int32_t page) { return page; }
"""
    )
    return root


def test_cpp_internal_facade_is_typed_hidden_and_feature_scoped(tmp_path: Path):
    root = module(tmp_path)
    header, source = render_cpp_internal_facade(
        root,
        module_name="Documents",
        feature_id=FEATURE_ID,
        include_prefix="documents/android/src/main/cpp",
    )

    assert internal_header_path(FEATURE_ID) == (
        "include/supernote/0123456789abcdef/internal.hpp"
    )
    assert "namespace supernote::internal::Documents" in header
    assert "std::int32_t pageCount(std::int32_t offset);" in header
    assert "std::function<void(supernote::Result<std::int32_t>)>" in header
    assert "struct IndexService final" in header
    assert '#include "documents/android/src/main/cpp/internal.hpp"' in source
    assert "current_feature_session()" in source
    assert 'feature->service<::IndexService>' in source
    assert "process_services().workers().submit" in source
    assert "claim_internal_completion" in source
    assert "feature->accept({}, std::move(callback))" in source
    assert "operation->set_retained_state(retained_input_state)" in source
    assert "operation->take_internal_completion()" in source
    worker_capture = source[
        source.index("process_services().workers().submit") :
        source.index("operation->set_work(work)")
    ]
    assert "[operation, weak_feature, callback" not in worker_capture
    assert "feature.reset();" in source
    assert "auto completion_feature = weak_feature.lock();" in source
    assert "deliver_callback" in source
    assert "facebook::jsi" not in source
    assert "Promise" not in source
