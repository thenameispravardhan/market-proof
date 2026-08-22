#include "tb/config.hpp"

namespace tb {

Settings& mutable_settings() {
  static Settings s;
  return s;
}

const Settings& settings() { return mutable_settings(); }

}  // namespace tb
