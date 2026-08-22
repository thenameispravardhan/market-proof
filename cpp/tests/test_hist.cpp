#include <cassert>
#include <cstdio>

#include "tb/hist.hpp"

using namespace tb;

int main() {
  Histogram h;
  // Exact for small values: the first octave is 1:1.
  for (int i = 1; i <= 1000; ++i) h.record(static_cast<std::uint64_t>(i));
  assert(h.count() == 1000);
  assert(h.max() == 1000);
  assert(h.percentile(0.50) >= 495 && h.percentile(0.50) <= 505);
  assert(h.percentile(0.99) >= 985 && h.percentile(0.99) <= 995);

  // Bucketed range: the reported value must never UNDERSTATE the truth, or a
  // p99 gate (abort criterion A2) would pass when it should fail.
  Histogram g;
  for (int i = 0; i < 100000; ++i) g.record(50'000);
  const auto p99 = g.percentile(0.99);
  assert(p99 >= 50'000);
  assert(p99 < 50'000 * 1.005);  // within the 0.4% bucket width

  Histogram e;
  assert(e.percentile(0.99) == 0 && e.count() == 0);
  e.record(0);
  assert(e.percentile(0.50) == 0);

  // A percentile must never exceed the maximum. The bucket upper bound can,
  // and /metrics reporting p99 > max reads as a broken exporter.
  Histogram m;
  for (int i = 0; i < 50; ++i) m.record(17416);
  assert(m.percentile(0.99) <= m.max());
  assert(m.percentile(0.999) <= m.max());
  assert(m.percentile(0.50) <= m.max());
  Histogram spread;
  for (std::uint64_t v : {1ULL, 7ULL, 999ULL, 100000ULL, 3000001ULL}) spread.record(v);
  assert(spread.percentile(1.0) <= spread.max());
  assert(spread.percentile(0.99) >= 100000);  // still not optimistic

  std::puts("test_hist OK");
  return 0;
}
