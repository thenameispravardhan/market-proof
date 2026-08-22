#include "tb/str.hpp"

namespace tb {

std::string collapse_ws(std::string_view text, CpMap* map) {
  std::string out;
  out.reserve(text.size());
  if (map != nullptr) {
    map->byte_to_cp.clear();
    map->cp_to_byte.clear();
    map->byte_to_cp.reserve(text.size() + 1);
    map->cp_to_byte.reserve(text.size() + 1);
  }

  // `cp` is the code-point index of the byte about to be written; it advances
  // only on a UTF-8 LEAD byte, so every continuation byte (10xxxxxx) maps back
  // to the code point that started it.
  std::int32_t cp = 0;
  bool started = false;
  const auto put = [&](char c) {
    if ((static_cast<unsigned char>(c) & 0xC0) != 0x80) {
      if (started) ++cp;
      started = true;
      if (map != nullptr) map->cp_to_byte.push_back(out.size());
    }
    if (map != nullptr) map->byte_to_cp.push_back(cp);
    out.push_back(c);
  };

  bool first_token = true;
  std::size_t i = 0;
  const std::size_t n = text.size();
  while (i < n) {
    while (i < n && is_space(text[i])) ++i;
    if (i >= n) break;
    if (!first_token) put(' ');
    first_token = false;
    while (i < n && !is_space(text[i])) put(text[i++]);
  }
  if (map != nullptr) {
    map->byte_to_cp.push_back(started ? cp + 1 : 0);
    map->cp_to_byte.push_back(out.size());
  }
  return out;
}

}  // namespace tb
