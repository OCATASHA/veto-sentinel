// Example strategy loop using the Veto Sentinel C++ client. Simulated only: no broker, no orders.
//
//   strategy_demo [endpoint] [duration_seconds]
//
// Prints the local state once per second and a final JSON summary line (used by the
// Python<->C++ interop test).
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

#include "vetosentinel/client.hpp"

using namespace vetosentinel;

int main(int argc, char** argv) {
  ClientConfig cfg;
  if (argc > 1) cfg.endpoint = argv[1];
  const double duration = argc > 2 ? std::atof(argv[2]) : 0.0;

  Hooks hooks;
  hooks.on_state_change = [](State a, State b, const std::string& why) {
    std::printf("LOCAL STATE %s -> %s: %.140s\n", to_string(a), to_string(b), why.c_str());
    std::fflush(stdout);
  };
  hooks.on_caution = [](double f, const std::string&) {
    std::printf("  hook: CAUTION -> position limit x%.2f, no new openings (simulated)\n", f);
  };
  hooks.on_halt = [](const std::string&) {
    std::printf("  hook: HALT -> request cancellation of resting orders (simulated; no broker)\n");
  };
  hooks.on_flatten = [](const std::string&) {
    std::printf("  hook: FLATTEN instruction -> notify a human; NOT liquidating automatically\n");
  };

  Client client(cfg, hooks);
  std::printf("C++ client subscribing to %s; startup state %s (fail-safe)\n", cfg.endpoint.c_str(),
              to_string(client.current_state()));
  client.start();

  const auto t_end = std::chrono::steady_clock::now() + std::chrono::duration<double>(duration);
  std::int64_t worst_ns = 0, total_ns = 0, n = 0;
  while (duration <= 0 || std::chrono::steady_clock::now() < t_end) {
    // Hot path: measure the cost of the local order gate.
    for (int i = 0; i < 100000; ++i) {
      const auto a = steady_now_ns();
      volatile bool ok = client.can_submit_order(true);
      (void)ok;
      const auto d = steady_now_ns() - a;
      total_ns += d;
      worst_ns = d > worst_ns ? d : worst_ns;
      ++n;
    }
    const auto s = client.snapshot();
    std::printf("state=%-7s can_open=%d can_reduce=%d limit=%6.1f fault=%s seq=%lld\n", to_string(s.local_state),
                client.can_submit_order(true), client.can_submit_order(false), client.position_limit(100.0),
                to_string(s.fault), static_cast<long long>(s.last_seq));
    std::fflush(stdout);
    std::this_thread::sleep_for(std::chrono::seconds{1});
  }
  client.stop();
  const auto s = client.snapshot();
  std::printf(
      "SUMMARY {\"local_state\":\"%s\",\"published_state\":\"%s\",\"received\":%llu,\"valid\":%llu,"
      "\"recoveries\":%llu,\"last_seq\":%lld,\"invalid\":%llu,\"gaps\":%llu,\"stale\":%llu,"
      "\"check_mean_ns\":%.1f,\"check_worst_ns\":%lld}\n",
      to_string(s.local_state), s.published_state ? to_string(*s.published_state) : "none",
      (unsigned long long)s.stats.received, (unsigned long long)s.stats.valid,
      (unsigned long long)s.stats.recoveries, (long long)s.last_seq,
      (unsigned long long)s.stats.faults[int(Fault::InvalidMessage)],
      (unsigned long long)s.stats.faults[int(Fault::SequenceGap)],
      (unsigned long long)s.stats.faults[int(Fault::StaleHeartbeat)], n ? double(total_ns) / n : 0.0,
      (long long)worst_ns);
  return 0;
}
