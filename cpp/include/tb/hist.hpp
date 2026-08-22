// Latency histogram -- c++.text §9 PHASE 2 ("NOT OPTIONAL -- skipping it is
// how rewrites end up slower than what they replaced, with nobody able to
// prove it"). Abort criterion A2 is a p99 number, so it has to be measurable.
//
// ponytail: this is ~60 lines instead of the HdrHistogram dependency the plan
// names, because "an HdrHistogram wrapper" IS this: bucket by exponent, keep
// `precision` linear sub-buckets per octave, report percentiles by walking the
// counts. Swap in the real library if a phase ever needs its log/merge format.
#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <vector>

namespace tb {

class Histogram {
 public:
  // 256 sub-buckets per octave over 32 octaves: ~0.4% relative error (800 ns
  // on a 200 us p99), and 64 KB per histogram. Sizing matters -- the box has
  // 127 MB free (§1.4) and the migration's own memory target is < 150 MB.
  static constexpr int kSub = 256;
  static constexpr int kOctaves = 32;  // covers 1 ns .. ~9 minutes

  // NOT thread-safe by design: one histogram per thread. The hot thread owns
  // its own, so plain counters are correct and free.

  void record(std::uint64_t v) noexcept {
    ++count_;
    sum_ += v;
    if (v > max_) max_ = v;
    ++buckets_[index(v)];
  }

  std::uint64_t count() const noexcept { return count_; }
  std::uint64_t max() const noexcept { return max_; }
  double mean() const noexcept { return count_ ? static_cast<double>(sum_) / count_ : 0.0; }

  // Value at `p` in [0, 1]. Returns the bucket's upper bound, so a reported
  // percentile is never optimistic -- but capped at the true maximum, because
  // a bucket's upper bound can exceed every value actually recorded. Without
  // the cap /metrics reports p99 > max, which reads as a broken exporter.
  std::uint64_t percentile(double p) const noexcept {
    if (count_ == 0) return 0;
    const auto target = static_cast<std::uint64_t>(p * static_cast<double>(count_) + 0.5);
    std::uint64_t seen = 0;
    for (std::size_t i = 0; i < buckets_.size(); ++i) {
      seen += buckets_[i];
      if (seen >= target && buckets_[i] != 0) return std::min(upper_bound(i), max_);
    }
    return max_;
  }

  void reset() noexcept {
    buckets_.fill(0);
    count_ = sum_ = max_ = 0;
  }

 private:
  static int octave_of(std::uint64_t v) noexcept {
    int o = 0;
    while ((v >> o) >= kSub && o < kOctaves - 1) ++o;
    return o;
  }
  static std::size_t index(std::uint64_t v) noexcept {
    const int o = octave_of(v);
    const auto sub = static_cast<std::size_t>((v >> o) & (kSub - 1));
    return static_cast<std::size_t>(o) * kSub + sub;
  }
  static std::uint64_t upper_bound(std::size_t i) noexcept {
    const auto o = static_cast<int>(i / kSub);
    const auto sub = static_cast<std::uint64_t>(i % kSub);
    return ((sub + 1) << o) - 1;
  }

  std::array<std::uint64_t, static_cast<std::size_t>(kSub) * kOctaves> buckets_{};
  std::uint64_t count_{}, sum_{}, max_{};
};

}  // namespace tb
