// Veto Sentinel C++20 client SDK (prototype).
//
// Fail-safe contract (identical to the Python SDK, see docs/CLIENTS.md):
//   * Startup state is HALT.
//   * Stale heartbeat, invalid message, sequence gap / duplicate / reordering, publisher
//     restart, stale or future timestamp, or monitor_ready=false -> local HALT immediately.
//   * Leaving that fault requires `recovery_snapshots_required` consecutive valid, fresh,
//     in-sequence snapshots from a ready monitor.
//   * FLATTEN (operator instruction) is never downgraded by a fault.
//
// The strategy-facing checks (`current_state`, `can_submit_order`, `position_limit`) only
// read std::atomic values and the steady clock: no locks, no allocation, no network.
//
// Shadow mode (`ClientConfig::shadow`): the gate always allows and counts what it would have
// blocked; the action hooks (on_caution, on_halt) are not called. `decide()` gives the real answer.
// Fleet reports: when the monitor advertises a telemetry endpoint, the watchdog thread sends a
// small JSON report about this client every second (and on state changes) over a ZeroMQ PUSH
// socket. Never from the order path. Off with `ClientConfig::report = false`.
// There is NO broker integration: hooks are where a strategy wires its own handling.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace vetosentinel {

enum class State : std::uint8_t { NORMAL = 0, CAUTION = 1, HALT = 2, FLATTEN = 3 };

const char* to_string(State s) noexcept;
std::optional<State> parse_state(std::string_view s) noexcept;
constexpr int severity(State s) noexcept { return static_cast<int>(s); }

enum class Fault : std::uint8_t {
  None = 0, Startup, StaleHeartbeat, InvalidMessage, SequenceGap, Duplicate, OutOfOrder,
  PublisherRestart, StaleTimestamp, FutureTimestamp, MonitorNotReady, Count_
};
const char* to_string(Fault f) noexcept;

struct ClientConfig {
  std::string endpoint = "tcp://127.0.0.1:5556";
  std::string topic = "vs.v1";
  std::int64_t heartbeat_timeout_ms = 3000;
  std::int64_t max_message_age_ms = 3000;
  std::int64_t max_future_skew_ms = 1000;
  int recovery_snapshots_required = 2;
  bool honor_advertised_timeout = true;  // use min(ours, advertised)
  double caution_position_fraction = 0.25;
  std::int64_t watchdog_period_ms = 50;
  bool shadow = false;  // count, don't block (see above); never the default
  bool report = true;   // fleet reports to the advertised telemetry endpoint
  std::string name;     // shown in the fleet view
};

// Validated subset of the schema_version 1 message (see docs/MESSAGE_SCHEMA.md).
struct Message {
  std::string publisher_id;
  std::int64_t seq = 0;
  std::string kind;
  std::chrono::system_clock::time_point ts;
  State state = State::HALT;
  std::optional<double> risk_score;
  bool monitor_ready = false;
  std::int64_t heartbeat_timeout_ms = 0;
  std::string reason;
  std::string data_origin;
  bool fallback_in_use = false;
  std::string telemetry;  // optional: where to send fleet reports
};

constexpr std::size_t kMaxMessageBytes = 64 * 1024;

// Parse + validate. Returns nullopt and sets `error` on ANY problem (callers must fail safe).
std::optional<Message> parse_message(std::string_view payload, std::string& error);
// Parse "YYYY-MM-DDTHH:MM:SS[.fff]Z" or with a "+HH:MM"/"-HH:MM" offset. Naive times rejected.
std::optional<std::chrono::system_clock::time_point> parse_utc_timestamp(std::string_view s) noexcept;

struct Transition {
  State old_state;
  State new_state;
  std::string reason;
};

struct Stats {
  std::uint64_t received = 0;
  std::uint64_t valid = 0;
  std::uint64_t recoveries = 0;
  std::uint64_t faults[static_cast<int>(Fault::Count_)] = {};
};

struct TrackerSnapshot {
  State local_state;
  bool in_fault;
  Fault fault;
  std::string fault_detail;
  std::string publisher_id;
  std::int64_t last_seq;
  std::optional<State> published_state;
  std::optional<double> risk_score;
  Stats stats;
  std::optional<double> heartbeat_age_ms = std::nullopt;
  std::optional<double> bus_latency_ms = std::nullopt;  // publish -> receive of the last valid message
  std::string telemetry = {};
};

// "tcp://*:5656" advertised by the monitor -> where to connect, given the endpoint we subscribe
// to (a wildcard or loopback host means "the host I subscribed to"). Empty if unusable.
std::string resolve_telemetry(std::string_view advertised, std::string_view sub_endpoint);

// Pure state logic with injected clocks (monotonic ns + wall time); used by Client and tests.
class StateTracker {
 public:
  using WallTime = std::chrono::system_clock::time_point;

  explicit StateTracker(ClientConfig cfg);

  std::vector<Transition> on_payload(std::string_view payload, std::int64_t now_mono_ns, WallTime now_wall);
  std::vector<Transition> check_watchdog(std::int64_t now_mono_ns);

  // Lock-free hot-path read: local state, forced to at least HALT if the heartbeat is stale.
  State effective_state(std::int64_t now_mono_ns) const noexcept;
  bool is_stale(std::int64_t now_mono_ns) const noexcept;

  TrackerSnapshot snapshot(std::int64_t now_mono_ns) const;
  const ClientConfig& config() const noexcept { return cfg_; }
  std::string telemetry_endpoint() const;

 private:
  void set_local(State s, std::string reason, std::vector<Transition>& out);
  void enter_fault(Fault f, std::string detail, std::vector<Transition>& out);

  ClientConfig cfg_;
  // hot-path atomics
  std::atomic<State> local_state_{State::HALT};
  std::atomic<std::int64_t> last_valid_mono_ns_{-1};
  std::atomic<std::int64_t> timeout_ns_;
  // guarded by mu_
  mutable std::mutex mu_;
  bool in_fault_ = true;
  Fault fault_ = Fault::Startup;
  std::string fault_detail_ = "startup: no valid message received yet";
  std::string publisher_id_;
  std::int64_t last_seq_ = -1;
  int consecutive_good_ = 0;
  std::optional<Message> last_message_;
  Stats stats_;
  std::optional<double> last_latency_ms_;
};

struct Hooks {
  std::function<void(State, State, const std::string&)> on_state_change;
  std::function<void(double position_fraction, const std::string&)> on_caution;  // smaller limit, no openings
  std::function<void(const std::string&)> on_halt;     // request cancellation of resting orders
  std::function<void(const std::string&)> on_flatten;  // notify a human; NEVER auto-liquidate
};

class Client {
 public:
  explicit Client(ClientConfig cfg, Hooks hooks = {});
  ~Client();
  Client(const Client&) = delete;
  Client& operator=(const Client&) = delete;

  void start();
  void stop();

  // ---- strategy hot path: atomics + steady clock only
  State current_state() const noexcept;
  // The real answer: NORMAL any order; CAUTION only position-reducing orders; HALT/FLATTEN none.
  bool decide(bool opening = true) const noexcept;
  // The order gate: decide(), except in shadow mode, where it allows and counts would-blocks.
  bool can_submit_order(bool opening = true) const noexcept;
  double position_limit(double normal_limit) const noexcept;

  TrackerSnapshot snapshot() const;
  std::uint64_t would_block() const noexcept { return would_block_.load(std::memory_order_relaxed); }
  // Average cost of one decide() call, measured over 1M calls on the watchdog thread (0 = not yet).
  double check_ns() const noexcept { return check_ns_.load(std::memory_order_relaxed); }
  // The fleet report this client sends (JSON), e.g. for logging or tests.
  std::string report_json(bool bye = false);
  void measure_check();

 private:
  void subscriber_loop();
  void watchdog_loop();
  void dispatch(const std::vector<Transition>& ts);

  ClientConfig cfg_;
  Hooks hooks_;
  StateTracker tracker_;
  std::string client_id_;
  std::uint64_t report_seq_ = 0;
  mutable std::atomic<std::uint64_t> would_block_{0};
  std::atomic<double> check_ns_{0.0};
  std::atomic<std::uint64_t> bench_sink_{0};
  std::atomic<bool> stop_{false};
  std::thread sub_thread_;
  std::thread wd_thread_;
  std::mutex hook_mu_;  // serialise hook calls from the two threads
};

std::int64_t steady_now_ns() noexcept;

}  // namespace vetosentinel
