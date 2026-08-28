#include <chrono>
#include <memory>
#include <string>
#include <thread>

namespace supernote_feature_DeviceProbe {

// @SupernotePluginExport
double addNumbers(double left, double right) {
  return left + right;
}

// @SupernotePluginExport
std::string nativeEcho(std::string value) {
  return "cpp:" + value;
}

// @SupernotePluginExport
bool invert(bool value) {
  return !value;
}

// @SupernotePluginExport
// @SupernotePluginAsync
std::string nativeAsyncEcho(std::string value) {
  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  return "cpp-async:" + value;
}

}  // namespace supernote_feature_DeviceProbe
