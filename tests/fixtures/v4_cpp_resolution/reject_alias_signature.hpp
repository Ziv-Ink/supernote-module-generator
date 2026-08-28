#include <memory>

namespace ink {
// @SupernotePluginObject
class Stroke {};
}

using StrokeAlias = ink::Stroke;

// @SupernotePluginExport
std::shared_ptr<StrokeAlias> copy_stroke(
    const std::shared_ptr<StrokeAlias>& stroke);
