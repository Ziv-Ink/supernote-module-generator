#pragma once

#include <android/log.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

namespace supernote_feature_Q5RuntimeProbe {

// @SupernotePluginValue
enum class ProbeMode { One, Two };

// @SupernotePluginValue
struct ProbePayload {
  // @SupernotePluginExport
  std::int32_t count;
  // @SupernotePluginExport
  std::optional<std::string> label;
  // @SupernotePluginExport
  std::vector<std::byte> bytes;
  // @SupernotePluginExport
  ProbeMode mode;
  // @SupernotePluginExport
  std::vector<std::optional<std::string>> tags;
  // @SupernotePluginExport
  std::optional<double> score;
};

// @SupernotePluginObject
class ProbeCounter {
 public:
  // @SupernoteConstructor
  explicit ProbeCounter(std::int32_t initial)
      : value_(initial), creator_tid_(static_cast<std::int64_t>(gettid())) {
    __android_log_print(
        ANDROID_LOG_INFO, "Q5R7Native",
        "Q5_R7_COUNTER_CREATE creator=%lld value=%d",
        static_cast<long long>(creator_tid_), value_);
  }

  ~ProbeCounter() {
    __android_log_print(
        ANDROID_LOG_INFO, "Q5R7Native",
        "Q5_R7_COUNTER_DESTROY creator=%lld destroy=%lld value=%d",
        static_cast<long long>(creator_tid_),
        static_cast<long long>(gettid()), value_);
  }

  // @SupernotePluginExport
  std::int32_t add(std::int32_t delta) {
    value_ += delta;
    return value_;
  }

  // @SupernotePluginExport
  std::int32_t value() const { return value_; }

  // @SupernotePluginExport
  std::int64_t creatorThreadId() const { return creator_tid_; }

  // @SupernotePluginExport
  bool same(const std::shared_ptr<ProbeCounter> &other) const {
    return other.get() == this;
  }

 private:
  std::int32_t value_;
  std::int64_t creator_tid_;
};

// @SupernotePluginObject
class ForeignCounter {
 public:
  // @SupernoteConstructor
  explicit ForeignCounter(std::int32_t initial) : value_(initial) {}

  // @SupernotePluginExport
  std::int32_t value() const { return value_; }

 private:
  std::int32_t value_;
};

// @SupernotePluginObject
class ReturnedToken {
 public:
  explicit ReturnedToken(std::int32_t value) : value_(value) {}

  // @SupernotePluginExport
  std::int32_t value() const { return value_; }

 private:
  std::int32_t value_;
};

}  // namespace supernote_feature_Q5RuntimeProbe
