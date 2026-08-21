#include <memory>

namespace ink {
// @SupernotePluginObject
class Stroke {};
}

// @SupernotePluginExport
std::shared_ptr<ink::Stroke> copy_stroke(
    const std::shared_ptr<::ink::Stroke>& stroke);
