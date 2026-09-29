#include <string>

namespace supernote_feature_Q5UnavailableProbe {

// @SupernotePluginExport
std::string neverInstalled(std::string value) {
  return "unavailable:" + value;
}

}  // namespace supernote_feature_Q5UnavailableProbe
