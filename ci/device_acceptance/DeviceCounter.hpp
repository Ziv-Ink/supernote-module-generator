#pragma once

// @SupernotePluginObject
class DeviceCounter {
 public:
  // @SupernoteConstructor
  explicit DeviceCounter(double initial) : value_(initial) {}

  // @SupernotePluginExport
  double add(double delta) {
    value_ += delta;
    return value_;
  }

  // @SupernotePluginExport
  double value() const {
    return value_;
  }

 private:
  double value_;
};
