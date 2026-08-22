// Small string helpers shared by the ported pure-logic leaves.
//
// PARITY NOTE (c++.text §10.4): Python's str.strip()/str.split() treat every
// Unicode whitespace code point as a separator; these treat ASCII whitespace
// only. Exchange headlines are ASCII in every one of the 28,381 rows checked,
// but a U+00A0 in a future filing would diverge. Recorded in cpp/DIFFS.md.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace tb {

inline constexpr bool is_space(char c) noexcept {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}

inline std::string_view strip(std::string_view s) noexcept {
  std::size_t b = 0, e = s.size();
  while (b < e && is_space(s[b])) ++b;
  while (e > b && is_space(s[e - 1])) --e;
  return s.substr(b, e - b);
}

inline bool blank(std::string_view s) noexcept { return strip(s).empty(); }

// ASCII-only case folding. Deliberate: Python's str.lower() is full Unicode,
// but every token these are compared against is ASCII, and a Unicode-aware
// fold can change the string's LENGTH (e.g. U+0130), which would break the
// offset arithmetic in fast_track's context windows.
inline std::string lower(std::string_view s) {
  std::string out(s);
  for (char& c : out)
    if (c >= 'A' && c <= 'Z') c += 32;
  return out;
}

inline std::string upper(std::string_view s) {
  std::string out(s);
  for (char& c : out)
    if (c >= 'a' && c <= 'z') c -= 32;
  return out;
}

inline bool contains(std::string_view hay, std::string_view needle) noexcept {
  return hay.find(needle) != std::string_view::npos;
}

// Byte <-> code-point index over one buffer.
//
// fast_track slices its context windows by CODE POINT because that is what
// Python's str slicing does, and filings carry multi-byte characters (the
// rupee sign is 3 bytes in UTF-8, Devanagari 3 each). Doing the same
// arithmetic on byte offsets silently shifts the window and changes which
// values land inside it.
struct CpMap {
  std::vector<std::int32_t> byte_to_cp;  // size = bytes + 1
  std::vector<std::size_t> cp_to_byte;   // size = code points + 1

  std::int32_t cp_at(std::size_t byte) const noexcept {
    return byte_to_cp[byte < byte_to_cp.size() ? byte : byte_to_cp.size() - 1];
  }
  std::size_t byte_at(std::int64_t cp) const noexcept {
    if (cp < 0) cp = 0;
    const auto n = static_cast<std::int64_t>(cp_to_byte.size()) - 1;
    return cp_to_byte[static_cast<std::size_t>(cp < n ? cp : n)];
  }
};

// Python's `" ".join(text.split())`: collapse every run of whitespace to a
// single space and trim the ends. Fills `map` when non-null.
std::string collapse_ws(std::string_view text, CpMap* map = nullptr);

}  // namespace tb
