// The dynamic value the rules engine compares against.
//
// app/analyzer/rules_engine.py evaluates plain dicts whose values are
// strings, floats, bools or None, so that is exactly what this models -- not
// a general JSON DOM. Keeping it this narrow is what lets `_apply_op` stay a
// direct transliteration of the Python instead of a type-coercion maze.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace tb {

struct Value;
using Array = std::vector<Value>;

struct Value {
  // Order matters: index 0 is the "missing / null" state, which the rules
  // engine treats as a non-match for every ordering operator (fail-safe).
  std::variant<std::monostate, bool, double, std::string, Array> v{};

  Value() = default;
  Value(bool b) : v(b) {}
  Value(double d) : v(d) {}
  Value(int i) : v(static_cast<double>(i)) {}
  Value(std::string s) : v(std::move(s)) {}
  Value(const char* s) : v(std::string(s)) {}
  Value(Array a) : v(std::move(a)) {}

  bool is_null() const noexcept { return v.index() == 0; }
  bool is_bool() const noexcept { return std::holds_alternative<bool>(v); }
  bool is_num() const noexcept { return std::holds_alternative<double>(v); }
  bool is_str() const noexcept { return std::holds_alternative<std::string>(v); }
  bool is_arr() const noexcept { return std::holds_alternative<Array>(v); }

  double num() const noexcept { return std::get<double>(v); }
  const std::string& str() const { return std::get<std::string>(v); }
  const Array& arr() const { return std::get<Array>(v); }

  // Python's float(x): numbers pass through, bools are 1.0/0.0, numeric
  // strings parse, anything else raises (here: nullopt -> RuleError).
  std::optional<double> as_double() const;
};

using Object = std::map<std::string, Value>;

inline const Value* find(const Object& o, const std::string& k) {
  auto it = o.find(k);
  return it == o.end() ? nullptr : &it->second;
}

}  // namespace tb
