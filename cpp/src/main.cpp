// Phase 1 skeleton -- c++.text §9 PHASE 1.
//
// EXIT CRITERION: `curl :8001/health` returns ok on the server, with both
// stacks running side by side. That is ALL this does, deliberately. It places
// no orders, reads no DB, and owns no responsibility (§4.2: the C++ binary
// "starts as a process that does nothing").
//
// WHY NOT DROGON, which §6 names for the REST layer: Ubuntu's DrogonConfig.cmake
// calls find_dependency unconditionally for jsoncpp, PostgreSQL, MySQL, sqlite3,
// brotli and hiredis, and still fails on FindMySQL after all of them are
// installed. Paying that on a live trading box to serve a static "ok" is not a
// trade worth making. cpp-httplib is one header with no dependency chain and
// covers two routes fine. Drogon comes back at §9 PHASE 6, where 89 routes and
// a WebSocket actually justify it -- this file is ~100 lines and is not what
// makes that migration hard.
//
// Port 8001 so it cannot collide with the Python on 8000, and it binds
// 127.0.0.1 only: nothing should be able to reach this from outside the box
// until a phase moves a real responsibility over.
#include <httplib.h>
#include <spdlog/sinks/stdout_sinks.h>
#include <spdlog/spdlog.h>

#include <chrono>
#include <format>
#include <memory>
#include <string>

#include "tb/hist.hpp"
#include "tb/market_clock.hpp"

namespace {

// §9 PHASE 1: "spdlog emitting the SAME JSON log shape as structlog, so your
// existing journalctl greps keep working". structlog renders
// {"event": ..., "level": ..., "timestamp": ...} so the pattern does too.
void configure_logging() {
  auto sink = std::make_shared<spdlog::sinks::stdout_sink_mt>();
  auto logger = std::make_shared<spdlog::logger>("tradebot", sink);
  logger->set_pattern(R"({"timestamp":"%Y-%m-%dT%H:%M:%S.%eZ","level":"%l","event":%v})");
  spdlog::set_default_logger(logger);
  spdlog::flush_on(spdlog::level::info);
}

const auto kStart = std::chrono::steady_clock::now();

// Phase 2's real histograms land here once there is a hot path to measure.
// The endpoint exists now so /metrics never has to be bolted on mid-migration.
tb::Histogram& request_latency() {
  static tb::Histogram h;
  return h;
}

std::string metrics_body() {
  const tb::Histogram& h = request_latency();
  return std::format(
      "# c++.text §9 PHASE 2 -- p50/p99/p99.9 per stage\n"
      "tradebot_up 1\n"
      "tradebot_uptime_seconds {}\n"
      "tradebot_request_count {}\n"
      "tradebot_request_latency_ns{{quantile=\"0.5\"}} {}\n"
      "tradebot_request_latency_ns{{quantile=\"0.99\"}} {}\n"
      "tradebot_request_latency_ns{{quantile=\"0.999\"}} {}\n"
      "tradebot_request_latency_ns_max {}\n",
      std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now() - kStart)
          .count(),
      h.count(), h.percentile(0.5), h.percentile(0.99), h.percentile(0.999), h.max());
}

}  // namespace

int main() {
  configure_logging();

  httplib::Server srv;

  srv.Get("/health", [](const httplib::Request&, httplib::Response& res) {
    const auto t0 = std::chrono::steady_clock::now();
    const auto now =
        std::chrono::time_point_cast<std::chrono::seconds>(std::chrono::system_clock::now());
    // Cheap proof the ported clock is live, and useful on its own.
    res.set_content(std::format(R"({{"status":"ok","stack":"cpp","phase":1,)"
                               R"("market_open":{},"square_off_due":{}}})",
                               tb::is_market_open(now), tb::square_off_due(now)),
                    "application/json");
    request_latency().record(static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - t0)
            .count()));
  });

  srv.Get("/metrics", [](const httplib::Request&, httplib::Response& res) {
    res.set_content(metrics_body(), "text/plain");
  });

  spdlog::info(R"("tradebot.listening","host":"127.0.0.1","port":8001)");
  if (!srv.listen("127.0.0.1", 8001)) {
    spdlog::error(R"("tradebot.listen_failed","port":8001)");
    return 1;
  }
  return 0;
}
