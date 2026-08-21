#include <memory>

namespace ink {
// @SupernotePluginObject
class Stroke {};

// @SupernotePluginExport
std::shared_ptr<Stroke> copy_stroke(const std::shared_ptr<Stroke>& stroke);
}
