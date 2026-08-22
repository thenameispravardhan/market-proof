// Phase 1 skeleton -- c++.text §9 PHASE 1.
//
// EXIT CRITERION: `curl :8001/health` returns ok on the server, with both
// stacks running side by side. That is ALL this does, deliberately. It places
// no orders, reads no DB, and owns no responsibility (§4.2: the C++ binary
// "starts as a process that does nothing").
//
// Port 8001 so it cannot collide with the Python on 8000. Caddy is not routed
// to it until a phase actually needs it.
#include <drogon/drogon.h>
#include <spdlog/sinks/stdout_sinks.h>
#include <spdlog/spdlog.h>

#include <chrono>
#include <format>
#include <functional>
#include <memory>
#include <string>

#include "tb/config.hpp"
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
  spdlog::set_level(spdlog::level::info);
}

const auto kStart = std::chrono::steady_clock::now();

// Phase 2's histograms live here once there is a hot path to measure. The
// endpoint exists now so /metrics never has to be "added later" mid-migration.
tb::Histogram& request_latency() {
  static tb::Histogram h;
  return h;
}

std::string metrics_body() {
  const tb::Histogram& h = request_latency();
  return std::format(
      "# c++.text §9 PHASE 2 -- p50/p99/p99.9/max per stage\n"
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
  spdlog::info(R"("tradebot.starting","port":8001)");

  drogon::app().registerHandler(
      "/health",
      [](const drogon::HttpRequestPtr&,
         std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
        const auto t0 = std::chrono::steady_clock::now();
        const auto now = std::chrono::time_point_cast<std::chrono::seconds>(
            std::chrono::system_clock::now());
        auto resp = drogon::HttpResponse::newHttpJsonResponse([&] {
          Json::Value j;
          j["status"] = "ok";
          j["stack"] = "cpp";
          j["phase"] = 1;
          // Cheap proof the ported clock is live, and useful on its own.
          j["market_open"] = tb::is_market_open(now);
          j["square_off_due"] = tb::square_off_due(now);
          return j;
        }());
        request_latency().record(static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - t0)
                .count()));
        cb(resp);
      },
      {drogon::Get});

  drogon::app().registerHandler(
      "/metrics",
      [](const drogon::HttpRequestPtr&,
         std::function<void(const drogon::HttpResponsePtr&)>&& cb) {
        auto resp = drogon::HttpResponse::newHttpResponse();
        resp->setContentTypeCode(drogon::CT_TEXT_PLAIN);
        resp->setBody(metrics_body());
        cb(resp);
      },
      {drogon::Get});

  // One event-loop thread: the box has 2 vCPUs and §5.2 budgets exactly one
  // for Drogon. Do not raise this without re-reading §2.3.
  drogon::app().addListener("127.0.0.1", 8001).setThreadNum(1).run();
  return 0;
}
