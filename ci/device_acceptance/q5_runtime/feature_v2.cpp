#define Q5_FIXTURE_REVISION 2
#include "ProbeTypes.hpp"

#include <android/log.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <stdexcept>
#include <thread>

namespace supernote_feature_Q5RuntimeProbe {
namespace {
std::atomic<std::int32_t> g_calls{0};
std::atomic<std::int32_t> g_async_calls{0};
std::atomic<std::int32_t> g_last_payload_count{-1};
}

// @SupernotePluginExport
void resetCallCount() {
  g_calls.store(0);
  g_async_calls.store(0);
}

// @SupernotePluginExport
std::int32_t callCount() { return g_calls.load(); }

// @SupernotePluginExport
std::int32_t asyncCallCount() { return g_async_calls.load(); }

// @SupernotePluginExport
std::int32_t binaryRevision() { return Q5_FIXTURE_REVISION; }

// @SupernotePluginExport
std::int32_t echoInt32(std::int32_t value) {
  ++g_calls;
  return value;
}

// @SupernotePluginExport
std::int64_t echoInt64(std::int64_t value) {
  ++g_calls;
  return value;
}

// @SupernotePluginExport
std::int32_t inspectString(std::string value) {
  ++g_calls;
  return static_cast<std::int32_t>(value.size());
}

// @SupernotePluginExport
std::int32_t inspectBytes(std::vector<std::byte> value) {
  ++g_calls;
  if (value.empty()) return 0;
  return static_cast<std::int32_t>(value.size());
}

// @SupernotePluginExport
std::vector<std::byte> echoBytes(std::vector<std::byte> value) {
  ++g_calls;
  return value;
}

// @SupernotePluginExport
std::int32_t inspectNullableStrings(
    std::vector<std::optional<std::string>> values) {
  ++g_calls;
  return static_cast<std::int32_t>(values.size());
}

// @SupernotePluginExport
ProbePayload echoPayload(ProbePayload value) {
  ++g_calls;
  g_last_payload_count.store(value.count);
  return value;
}

// @SupernotePluginExport
std::int32_t lastPayloadCount() { return g_last_payload_count.load(); }

// @SupernotePluginExport
std::string receiverHandle(const std::shared_ptr<ProbeCounter> &counter) {
  ++g_calls;
  return "q5-receiver-handle:" + std::to_string(Q5_FIXTURE_REVISION) + ":" +
         std::to_string(counter->value());
}

// @SupernotePluginExport
std::int32_t requireCurrentReceiverHandle(std::string handle) {
  ++g_calls;
  const auto expected =
      "q5-receiver-handle:" + std::to_string(Q5_FIXTURE_REVISION) + ":";
  if (!handle.starts_with(expected)) {
    throw std::runtime_error(
        "STALE_GENERATION_HANDLE expected_revision=" +
        std::to_string(Q5_FIXTURE_REVISION) + " handle=" + handle);
  }
  return Q5_FIXTURE_REVISION;
}

// @SupernotePluginExport
std::string completionMarker(std::string marker_path) {
  std::ifstream stream(marker_path, std::ios::binary);
  if (!stream) return "";
  return std::string(
      std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}

// @SupernotePluginExport
std::shared_ptr<ReturnedToken> makeReturnedToken(std::int32_t value) {
  ++g_calls;
  return std::make_shared<ReturnedToken>(value);
}

// @SupernotePluginExport
void throwImplementation() {
  ++g_calls;
  throw std::runtime_error("q5-r7-implementation-marker");
}

// @SupernotePluginExport
// @SupernotePluginAsync
std::int32_t asyncEcho(std::int32_t value) {
  ++g_async_calls;
  return value;
}

// @SupernotePluginExport
// @SupernotePluginAsync
std::int32_t delayedRevision(std::int32_t delay_ms, std::string marker_path) {
  ++g_async_calls;
  __android_log_print(
      ANDROID_LOG_INFO, "Q5R7Native",
      "Q5_R7_NATIVE_DELAY_START revision=%d delay_ms=%d tid=%lld",
      Q5_FIXTURE_REVISION, delay_ms, static_cast<long long>(gettid()));
  std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
  __android_log_print(
      ANDROID_LOG_INFO, "Q5R7Native",
      "Q5_R7_NATIVE_DELAY_END revision=%d delay_ms=%d tid=%lld",
      Q5_FIXTURE_REVISION, delay_ms, static_cast<long long>(gettid()));
  const auto temporary = marker_path + ".tmp";
  {
    std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
    if (!stream) {
      throw std::runtime_error("could not create completion marker");
    }
    stream << "revision=" << Q5_FIXTURE_REVISION << ";native-work-complete";
    if (!stream) {
      throw std::runtime_error("could not write completion marker");
    }
  }
  if (std::rename(temporary.c_str(), marker_path.c_str()) != 0) {
    std::remove(temporary.c_str());
    throw std::runtime_error("could not publish completion marker");
  }
  return Q5_FIXTURE_REVISION;
}

}  // namespace supernote_feature_Q5RuntimeProbe
