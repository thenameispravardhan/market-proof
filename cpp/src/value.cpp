#include "tb/value.hpp"

#include <charconv>

#include "tb/str.hpp"

namespace tb {

std::optional<double> Value::as_double() const {
  if (is_num()) return num();
  // Python's float(True) is 1.0, and the rules engine relies on it for
  // boolean-ish analysis fields.
  if (is_bool()) return std::get<bool>(v) ? 1.0 : 0.0;
  if (is_str()) {
    const std::string_view s = strip(str());
    if (s.empty()) return std::nullopt;
    double out{};
    const auto* end = s.data() + s.size();
    auto [p, ec] = std::from_chars(s.data(), end, out);
    if (ec != std::errc{} || p != end) return std::nullopt;
    return out;
  }
  return std::nullopt;  // null / array -> Python raises TypeError
}

}  // namespace tb
