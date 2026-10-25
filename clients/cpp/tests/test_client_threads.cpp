// Threaded end-to-end test of the C++ Client: a real ZeroMQ publisher thread, the client's own
// subscriber and watchdog threads, two strategy threads hammering the order gate, heartbeat loss
// and recovery, fleet reports (including the goodbye) and shadow mode. Written to be run under
// ThreadSanitizer and AddressSanitizer too (see clients/cpp/sanitize.sh); takes a few seconds.
#include <atomic>
#include <chrono>
#include <cstdio>
#include <functional>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>
#include <zmq.hpp>

#include "vetosentinel/client.hpp"

using namespace vetosentinel;
using namespace std::chrono;

static int g_failures = 0;
#define CHECK(cond)                                                              \
  do {                                                                           \
    if (!(cond)) {                                                               \
      std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                              \
    }                                                                            \
  } while (0)

namespace {

std::string iso_now() {
  const auto now = system_clock::now();
  const auto ms = duration_cast<milliseconds>(now.time_since_epoch()).count();
  const auto secs = sys_seconds{seconds{ms / 1000}};
  const auto dp = floor<days>(secs);
  const year_month_day ymd{dp};
  const hh_mm_ss hms{secs - dp};
  char buf[40];
  std::snprintf(buf, sizeof buf, "%04d-%02u-%02uT%02ld:%02ld:%02lld.%03lldZ", int(ymd.year()), unsigned(ymd.month()),
                unsigned(ymd.day()), long(hms.hours().count()), long(hms.minutes().count()),
                (long long)hms.seconds().count(), (long long)(ms % 1000));
  return buf;
}

// Publishes valid heartbeats every 50 ms until told to pause or stop; state and telemetry endpoint
// are set by the test thread.
struct Publisher {
  std::string endpoint, telemetry;
  std::atomic<bool> stop{false}, paused{false};
  std::atomic<int> state{0};  // 0 NORMAL, 2 HALT
  std::thread th;

  void run(zmq::context_t& ctx) {
    zmq::socket_t pub{ctx, zmq::socket_type::pub};
    pub.set(zmq::sockopt::linger, 0);
    pub.bind(endpoint);
    long long seq = 0;
    while (!stop.load()) {
      if (!paused.load()) {
        nlohmann::json j = {
            {"schema_version", 1}, {"publisher_id", "pubT"}, {"seq", ++seq}, {"kind", "HEARTBEAT"}, {"ts", iso_now()},
            {"as_of", nullptr}, {"state", state.load() == 2 ? "HALT" : "NORMAL"}, {"risk_score", 0.3},
            {"components", {{"panic_z", 0.1}, {"drift_z", nullptr}, {"volume_z", 0.2}}}, {"reason", "test"},
            {"degraded", false}, {"degraded_reasons", nlohmann::json::array()}, {"top_headlines", nlohmann::json::array()},
            {"monitor_ready", true}, {"heartbeat_interval_ms", 50}, {"heartbeat_timeout_ms", 400},
            {"data_origin", "synthetic_fixture"}, {"implementations", nlohmann::json::object()},
            {"fallback_in_use", true}, {"control", nullptr}, {"telemetry", telemetry}};
        const std::string body = j.dump();
        pub.send(zmq::buffer(std::string_view{"vs.v1"}), zmq::send_flags::sndmore);
        pub.send(zmq::buffer(body), zmq::send_flags::none);
      }
      std::this_thread::sleep_for(milliseconds{50});
    }
  }
};

bool wait_for(const std::function<bool()>& ok, milliseconds limit) {
  const auto end = steady_clock::now() + limit;
  while (steady_clock::now() < end) {
    if (ok()) return true;
    std::this_thread::sleep_for(milliseconds{10});
  }
  return ok();
}

}  // namespace

int main() {
  zmq::context_t ctx{1};
  zmq::socket_t pull{ctx, zmq::socket_type::pull};
  pull.set(zmq::sockopt::linger, 0);
  pull.bind("tcp://127.0.0.1:*");
  const std::string tele = pull.get(zmq::sockopt::last_endpoint);

  Publisher pub;
  pub.endpoint = "tcp://127.0.0.1:47391";
  pub.telemetry = "tcp://*:" + tele.substr(tele.rfind(':') + 1);  // wildcard host: resolved by the client
  pub.th = std::thread([&] { pub.run(ctx); });

  ClientConfig cfg;
  cfg.endpoint = pub.endpoint;
  cfg.heartbeat_timeout_ms = 400;
  cfg.name = "threads-test";
  cfg.watchdog_period_ms = 10;
  Client client{cfg};
  ClientConfig shadow_cfg = cfg;
  shadow_cfg.shadow = true;
  shadow_cfg.report = false;
  Client shadow{shadow_cfg};
  client.start();
  shadow.start();

  // two strategy threads reading the hot path while messages arrive and the state changes
  std::atomic<bool> hammer{true};
  std::atomic<std::uint64_t> calls{0};
  std::vector<std::thread> strategies;
  for (int i = 0; i < 2; ++i) {
    strategies.emplace_back([&] {
      while (hammer.load()) {
        (void)client.can_submit_order(true);
        (void)client.position_limit(100.0);
        (void)shadow.can_submit_order(false);
        calls.fetch_add(3, std::memory_order_relaxed);
      }
    });
  }

  CHECK(wait_for([&] { return client.current_state() == State::NORMAL; }, seconds{5}));  // recovered from startup HALT
  CHECK(client.can_submit_order(true));

  pub.paused = true;  // heartbeat lost: fail-safe HALT within the timeout
  CHECK(wait_for([&] { return client.current_state() == State::HALT; }, seconds{2}));
  CHECK(!client.can_submit_order(false));
  pub.paused = false;  // fresh heartbeats: recovers after two good snapshots
  CHECK(wait_for([&] { return client.current_state() == State::NORMAL; }, seconds{3}));

  pub.state = 2;  // published HALT: the shadow client lets orders through but counts them
  CHECK(wait_for([&] { return shadow.current_state() == State::HALT; }, seconds{3}));
  const auto before = shadow.would_block();
  CHECK(shadow.can_submit_order(true));
  CHECK(!shadow.decide(true));
  CHECK(shadow.would_block() > before);
  CHECK(shadow.position_limit(100.0) == 100.0);

  // fleet reports arrive on the advertised endpoint (wildcard host resolved to the subscribe host)
  std::vector<nlohmann::json> reports;
  CHECK(wait_for([&] {
    zmq::message_t m;
    while (pull.recv(m, zmq::recv_flags::dontwait)) reports.push_back(nlohmann::json::parse(m.to_string_view()));
    return !reports.empty() && reports.back().contains("check_ns") && !reports.back()["check_ns"].is_null();
  }, seconds{4}));
  if (!reports.empty()) {
    const auto& r = reports.back();
    CHECK(r["v"] == 1 && r["sdk"] == "cpp" && r["name"] == "threads-test");
    CHECK(r["client_id"].get<std::string>().size() == 12);
    CHECK(r["bus_latency_ms"].is_number());
    CHECK(r["check_ns"].get<double>() > 0 && r["check_ns"].get<double>() < 1e5);
  }

  hammer = false;
  for (auto& t : strategies) t.join();
  client.stop();  // says goodbye
  shadow.stop();
  bool bye = false;
  CHECK(wait_for([&] {
    zmq::message_t m;
    while (pull.recv(m, zmq::recv_flags::dontwait)) bye = bye || nlohmann::json::parse(m.to_string_view())["bye"] == true;
    return bye;
  }, seconds{2}));
  pub.stop = true;
  pub.th.join();

  if (g_failures) {
    std::fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
  }
  std::printf("threaded client test passed (%llu hot-path calls, %zu fleet reports)\n",
              (unsigned long long)calls.load(), reports.size());
  return 0;
}
