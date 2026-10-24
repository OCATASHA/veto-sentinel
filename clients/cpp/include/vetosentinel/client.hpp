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
};

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
  // NORMAL: any order. CAUTION: only position-reducing orders. HALT/FLATTEN: none.
  bool can_submit_order(bool opening = true) const noexcept;
  double position_limit(double normal_limit) const noexcept;

  TrackerSnapshot snapshot() const;

 private:
  void subscriber_loop();
  void watchdog_loop();
  void dispatch(const std::vector<Transition>& ts);

  ClientConfig cfg_;
  Hooks hooks_;
  StateTracker tracker_;
  std::atomic<bool> stop_{false};
  std::thread sub_thread_;
  std::thread wd_thread_;
  std::mutex hook_mu_;  // serialise hook calls from the two threads
};

std::int64_t steady_now_ns() noexcept;

}  // namespace vetosentinel
