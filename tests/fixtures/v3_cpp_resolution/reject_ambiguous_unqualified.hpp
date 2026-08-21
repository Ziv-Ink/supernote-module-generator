#include <memory>

namespace ink_a {
// @SupernotePluginObject
class Stroke {};
}
namespace ink_b {
// @SupernotePluginObject
class Stroke {};
}

using namespace ink_a;
using namespace ink_b;

// @SupernotePluginExport
std::shared_ptr<Stroke> ambiguous_stroke();
