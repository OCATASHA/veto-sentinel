#include "vetosentinel/client.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <iostream>
#include <memory>
#include <random>

#include <unistd.h>

#include <nlohmann/json.hpp>
#include <zmq.hpp>

namespace vetosentinel {

using json = nlohmann::json;
using namespace std::chrono;

const char* to_string(State s) noexcept {
  switch (s) {
    case State::NORMAL: return "NORMAL";
    case State::CAUTION: return "CAUTION";
    case State::HALT: return "HALT";
    case State::FLATTEN: return "FLATTEN";
  }
  return "HALT";
}

std::optional<State> parse_state(std::string_view s) noexcept {
  if (s == "NORMAL") return State::NORMAL;
  if (s == "CAUTION") return State::CAUTION;
  if (s == "HALT") return State::HALT;
  if (s == "FLATTEN") return State::FLATTEN;
  return std::nullopt;
}

const char* to_string(Fault f) noexcept {
  switch (f) {
    case Fault::None: return "none";
    case Fault::Startup: return "startup";
    case Fault::StaleHeartbeat: return "stale_heartbeat";
    case Fault::InvalidMessage: return "invalid_message";
    case Fault::SequenceGap: return "sequence_gap";
    case Fault::Duplicate: return "duplicate_seq";
    case Fault::OutOfOrder: return "out_of_order_seq";
    case Fault::PublisherRestart: return "publisher_restart";
    case Fault::StaleTimestamp: return "stale_timestamp";
    case Fault::FutureTimestamp: return "future_timestamp";
    case Fault::MonitorNotReady: return "monitor_not_ready";
    case Fault::Count_: break;
  }
  return "unknown";
}

std::int64_t steady_now_ns() noexcept {
  return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
}

// ------------------------------------------------------------------ parsing
namespace {

bool parse_int(std::string_view s, int& out) {
  if (s.empty()) return false;
  auto [p, ec] = std::from_chars(s.data(), s.data() + s.size(), out);
  return ec == std::errc() && p == s.data() + s.size();
}

}  // namespace

std::optional<system_clock::time_point> parse_utc_timestamp(std::string_view s) noexcept {
  // YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM)
  if (s.size() < 20 || s[4] != '-' || s[7] != '-' || (s[10] != 'T' && s[10] != ' ') || s[13] != ':' ||
      s[16] != ':')
    return std::nullopt;
  int Y, M, D, h, m, sec;
  if (!parse_int(s.substr(0, 4), Y) || !parse_int(s.substr(5, 2), M) || !parse_int(s.substr(8, 2), D) ||
      !parse_int(s.substr(11, 2), h) || !parse_int(s.substr(14, 2), m) || !parse_int(s.substr(17, 2), sec))
    return std::nullopt;
  std::size_t i = 19;
  std::int64_t frac_ns = 0;
  if (i < s.size() && s[i] == '.') {
    ++i;
    int digits = 0;
    while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
      if (digits < 9) { frac_ns = frac_ns * 10 + (s[i] - '0'); ++digits; }
      ++i;
    }
    if (digits == 0) return std::nullopt;
    for (int k = digits; k < 9; ++k) frac_ns *= 10;
  }
  if (i >= s.size()) return std::nullopt;  // naive timestamp: reject
  int offset_min = 0;
  if (s[i] == 'Z') {
    if (i + 1 != s.size()) return std::nullopt;
  } else if (s[i] == '+' || s[i] == '-') {
    if (s.size() != i + 6 || s[i + 3] != ':') return std::nullopt;
    int oh, om;
    if (!parse_int(s.substr(i + 1, 2), oh) || !parse_int(s.substr(i + 4, 2), om)) return std::nullopt;
    offset_min = (oh * 60 + om) * (s[i] == '-' ? -1 : 1);
  } else {
    return std::nullopt;
  }
  year_month_day ymd{year{Y}, month{static_cast<unsigned>(M)}, day{static_cast<unsigned>(D)}};
  if (!ymd.ok() || h > 23 || m > 59 || sec > 60) return std::nullopt;
  auto tp = sys_days{ymd} + hours{h} + minutes{m} + seconds{sec} - minutes{offset_min};
  return time_point_cast<system_clock::duration>(tp + nanoseconds{frac_ns});
}

std::optional<Message> parse_message(std::string_view payload, std::string& error) {
  if (payload.size() > kMaxMessageBytes) { error = "message too large"; return std::nullopt; }
  json j = json::parse(payload.begin(), payload.end(), nullptr, /*allow_exceptions=*/false);
  if (j.is_discarded() || !j.is_object()) { error = "payload is not a JSON object"; return std::nullopt; }
  auto need = [&](const char* k) -> const json* {
    auto it = j.find(k);
    if (it == j.end()) { error = std::string("missing field ") + k; return nullptr; }
    return &*it;
  };
  Message m;
  const json* v;
  if (!(v = need("schema_version")) || !v->is_number_integer() || v->get<std::int64_t>() != 1) {
    if (error.empty()) error = "unsupported schema_version";
    return std::nullopt;
  }
  if (!(v = need("publisher_id")) || !v->is_string() || v->get_ref<const std::string&>().empty() ||
      v->get_ref<const std::string&>().size() > 64) {
    if (error.empty()) error = "bad publisher_id";
    return std::nullopt;
  }
  m.publisher_id = v->get<std::string>();
  if (!(v = need("seq")) || !v->is_number_integer() || v->get<std::int64_t>() < 1) {
    if (error.empty()) error = "bad seq";
    return std::nullopt;
  }
  m.seq = v->get<std::int64_t>();
  if (!(v = need("kind")) || !v->is_string()) { if (error.empty()) error = "bad kind"; return std::nullopt; }
  m.kind = v->get<std::string>();
  if (m.kind != "STATE" && m.kind != "HEARTBEAT" && m.kind != "CONTROL") { error = "unknown kind"; return std::nullopt; }
  if (!(v = need("ts")) || !v->is_string()) { if (error.empty()) error = "bad ts"; return std::nullopt; }
  auto ts = parse_utc_timestamp(v->get_ref<const std::string&>());
  if (!ts) { error = "unparseable or naive ts"; return std::nullopt; }
  m.ts = *ts;
  if (!(v = need("state")) || !v->is_string()) { if (error.empty()) error = "bad state"; return std::nullopt; }
  auto st = parse_state(v->get_ref<const std::string&>());
  if (!st) { error = "unknown state"; return std::nullopt; }
  m.state = *st;
  if (!(v = need("risk_score"))) return std::nullopt;
  if (v->is_number()) {
    double r = v->get<double>();
    if (!std::isfinite(r)) { error = "non-finite risk_score"; return std::nullopt; }
    m.risk_score = r;
  } else if (!v->is_null()) { error = "bad risk_score"; return std::nullopt; }
  if (!(v = need("monitor_ready")) || !v->is_boolean()) { if (error.empty()) error = "bad monitor_ready"; return std::nullopt; }
  m.monitor_ready = v->get<bool>();
  if (!(v = need("heartbeat_timeout_ms")) || !v->is_number_integer() || v->get<std::int64_t>() <= 0) {
    if (error.empty()) error = "bad heartbeat_timeout_ms";
    return std::nullopt;
  }
  m.heartbeat_timeout_ms = v->get<std::int64_t>();
  if (!(v = need("reason")) || !v->is_string()) { if (error.empty()) error = "bad reason"; return std::nullopt; }
  m.reason = v->get<std::string>();
  if (!(v = need("components")) || !v->is_object()) { if (error.empty()) error = "bad components"; return std::nullopt; }
  for (const char* k : {"panic_z", "drift_z", "volume_z"}) {
    auto it = v->find(k);
    if (it == v->end() || !(it->is_null() || it->is_number())) { error = std::string("bad component ") + k; return std::nullopt; }
  }
  for (const char* k : {"degraded", "fallback_in_use"}) {
    if (!(v = need(k)) || !v->is_boolean()) { if (error.empty()) error = std::string("bad ") + k; return std::nullopt; }
  }
  m.fallback_in_use = j["fallback_in_use"].get<bool>();
  if (!(v = need("data_origin")) || !v->is_string()) { if (error.empty()) error = "bad data_origin"; return std::nullopt; }
  m.data_origin = v->get<std::string>();
  if (auto it = j.find("telemetry"); it != j.end() && !it->is_null()) {  // optional, like the Python schema
    if (!it->is_string() || it->get_ref<const std::string&>().size() > 200) { error = "bad telemetry"; return std::nullopt; }
    m.telemetry = it->get<std::string>();
  }
  return m;
}

std::string resolve_telemetry(std::string_view advertised, std::string_view sub_endpoint) {
  auto split = [](std::string_view e, std::string& host, std::string& port) {
    if (e.substr(0, 6) != "tcp://") return false;
    e.remove_prefix(6);
    const auto c = e.rfind(':');
    if (c == std::string_view::npos || c == 0 || c + 1 >= e.size()) return false;
    host = std::string(e.substr(0, c));
    port = std::string(e.substr(c + 1));
    return std::all_of(port.begin(), port.end(), [](char ch) { return ch >= '0' && ch <= '9'; });
  };
  std::string host, port, sub_host, sub_port;
  if (!split(advertised, host, port)) return {};
  if ((host == "*" || host == "0.0.0.0" || host == "127.0.0.1" || host == "localhost") &&
      split(sub_endpoint, sub_host, sub_port))
    host = sub_host;
  if (host == "*") host = "127.0.0.1";
  return "tcp://" + host + ":" + port;
}

// ------------------------------------------------------------------ tracker
StateTracker::StateTracker(ClientConfig cfg)
    : cfg_(std::move(cfg)), timeout_ns_(cfg_.heartbeat_timeout_ms * 1'000'000) {
  stats_.faults[static_cast<int>(Fault::Startup)] = 1;
}

bool StateTracker::is_stale(std::int64_t now) const noexcept {
  const auto lv = last_valid_mono_ns_.load(std::memory_order_acquire);
  return lv < 0 || (now - lv) > timeout_ns_.load(std::memory_order_relaxed);
}

State StateTracker::effective_state(std::int64_t now) const noexcept {
  const State s = local_state_.load(std::memory_order_acquire);
  if (is_stale(now) && severity(s) < severity(State::HALT)) return State::HALT;
  return s;
}

void StateTracker::set_local(State s, std::string reason, std::vector<Transition>& out) {
  const State old = local_state_.load(std::memory_order_relaxed);
  if (old != s) {
    local_state_.store(s, std::memory_order_release);
    out.push_back({old, s, std::move(reason)});
  }
}

void StateTracker::enter_fault(Fault f, std::string detail, std::vector<Transition>& out) {
  if (!in_fault_ || fault_ != f) stats_.faults[static_cast<int>(f)]++;
  in_fault_ = true;
  fault_ = f;
  fault_detail_ = detail;
  consecutive_good_ = 0;
  const bool flatten = last_message_ && last_message_->state == State::FLATTEN;
  set_local(flatten ? State::FLATTEN : State::HALT, std::string("fail-safe: ") + to_string(f) + ": " + detail, out);
}

std::vector<Transition> StateTracker::check_watchdog(std::int64_t now) {
  std::vector<Transition> out;
  std::lock_guard lk(mu_);
  if (!is_stale(now)) return out;
  if (!(in_fault_ && (fault_ == Fault::StaleHeartbeat || fault_ == Fault::Startup))) {
    enter_fault(Fault::StaleHeartbeat, "no valid message within heartbeat timeout", out);
  } else if (severity(local_state_.load()) < severity(State::HALT)) {
    set_local(State::HALT, "fail-safe: heartbeat stale", out);
  }
  return out;
}

std::vector<Transition> StateTracker::on_payload(std::string_view payload, std::int64_t now, WallTime now_wall) {
  std::vector<Transition> out;
  std::lock_guard lk(mu_);
  stats_.received++;
  std::string err;
  auto msg = parse_message(payload, err);
  if (!msg) { enter_fault(Fault::InvalidMessage, err, out); return out; }

  if (!publisher_id_.empty() && msg->publisher_id != publisher_id_) {
    enter_fault(Fault::PublisherRestart, "publisher_id " + publisher_id_ + " -> " + msg->publisher_id, out);
    last_seq_ = -1;
  }
  publisher_id_ = msg->publisher_id;
  bool gap = false;
  if (last_seq_ >= 0) {
    if (msg->seq == last_seq_) { enter_fault(Fault::Duplicate, "seq " + std::to_string(msg->seq) + " repeated", out); return out; }
    if (msg->seq < last_seq_) {
      enter_fault(Fault::OutOfOrder, "seq " + std::to_string(msg->seq) + " after " + std::to_string(last_seq_), out);
      return out;
    }
    if (msg->seq > last_seq_ + 1) {
      enter_fault(Fault::SequenceGap, "missed " + std::to_string(msg->seq - last_seq_ - 1) + " message(s)", out);
      gap = true;
    }
  }
  last_seq_ = msg->seq;

  const auto age_ms = duration_cast<milliseconds>(now_wall - msg->ts).count();
  if (age_ms > cfg_.max_message_age_ms) {
    enter_fault(Fault::StaleTimestamp, "message is " + std::to_string(age_ms) + "ms old", out);
    return out;
  }
  if (-age_ms > cfg_.max_future_skew_ms) {
    enter_fault(Fault::FutureTimestamp, "message is " + std::to_string(-age_ms) + "ms in the future", out);
    return out;
  }

  stats_.valid++;
  last_latency_ms_ = duration<double, std::milli>(now_wall - msg->ts).count();
  last_message_ = *msg;
  last_valid_mono_ns_.store(now, std::memory_order_release);
  if (cfg_.honor_advertised_timeout)
    timeout_ns_.store(std::min(cfg_.heartbeat_timeout_ms, msg->heartbeat_timeout_ms) * 1'000'000);
  if (gap) return out;  // re-baselines sequencing, does not count toward recovery

  if (!msg->monitor_ready) { enter_fault(Fault::MonitorNotReady, msg->reason.substr(0, 200), out); return out; }

  if (in_fault_) {
    consecutive_good_++;
    if (msg->state == State::FLATTEN) set_local(State::FLATTEN, "published FLATTEN (operator instruction)", out);
    if (consecutive_good_ >= cfg_.recovery_snapshots_required) {
      in_fault_ = false;
      fault_ = Fault::None;
      fault_detail_.clear();
      stats_.recoveries++;
      set_local(msg->state, "recovered: " + msg->reason.substr(0, 200), out);
    }
    return out;
  }
  set_local(msg->state, msg->reason.substr(0, 300), out);
  return out;
}

TrackerSnapshot StateTracker::snapshot(std::int64_t now) const {
  std::lock_guard lk(mu_);
  TrackerSnapshot s{effective_state(now), in_fault_, fault_, fault_detail_, publisher_id_, last_seq_,
                    std::nullopt, std::nullopt, stats_};
  if (last_message_) {
    s.published_state = last_message_->state;
    s.risk_score = last_message_->risk_score;
    s.telemetry = last_message_->telemetry;
  }
  const auto lv = last_valid_mono_ns_.load(std::memory_order_acquire);
  if (lv >= 0) s.heartbeat_age_ms = static_cast<double>(now - lv) / 1e6;
  s.bus_latency_ms = last_latency_ms_;
  return s;
}

std::string StateTracker::telemetry_endpoint() const {
  std::lock_guard lk(mu_);
  return last_message_ ? last_message_->telemetry : std::string{};
}

// ------------------------------------------------------------------ client
namespace {

std::string random_id() {
  std::random_device rd;
  std::mt19937_64 gen((static_cast<std::uint64_t>(rd()) << 32) ^ rd());
  char buf[17];
  std::snprintf(buf, sizeof buf, "%016llx", static_cast<unsigned long long>(gen()));
  return std::string(buf, 12);
}

std::string iso_now_ms() {
  const auto now = system_clock::now();
  const std::time_t t = system_clock::to_time_t(now);
  std::tm tm{};
  gmtime_r(&t, &tm);
  const auto ms = duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000;
  char buf[40];
  std::snprintf(buf, sizeof buf, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms));
  return buf;
}

std::string hostname() {
  char buf[256] = {};
  if (gethostname(buf, sizeof buf - 1) != 0) return "unknown";
  return std::string(buf).substr(0, 64);
}

}  // namespace

Client::Client(ClientConfig cfg, Hooks hooks)
    : cfg_(cfg), hooks_(std::move(hooks)), tracker_(std::move(cfg)), client_id_(random_id()) {}

Client::~Client() { stop(); }

void Client::start() {
  stop_.store(false);
  sub_thread_ = std::thread([this] { subscriber_loop(); });
  wd_thread_ = std::thread([this] { watchdog_loop(); });
}

void Client::stop() {
  stop_.store(true);
  if (sub_thread_.joinable()) sub_thread_.join();
  if (wd_thread_.joinable()) wd_thread_.join();
}

State Client::current_state() const noexcept { return tracker_.effective_state(steady_now_ns()); }

bool Client::decide(bool opening) const noexcept {
  switch (current_state()) {
    case State::NORMAL: return true;
    case State::CAUTION: return !opening;
    default: return false;
  }
}

bool Client::can_submit_order(bool opening) const noexcept {
  if (decide(opening)) return true;
  if (cfg_.shadow) {  // allow, but count it: shadow mode never blocks
    would_block_.fetch_add(1, std::memory_order_relaxed);
    return true;
  }
  return false;
}

double Client::position_limit(double normal_limit) const noexcept {
  if (cfg_.shadow) return normal_limit;
  switch (current_state()) {
    case State::NORMAL: return normal_limit;
    case State::CAUTION: return normal_limit * cfg_.caution_position_fraction;
    default: return 0.0;
  }
}

TrackerSnapshot Client::snapshot() const { return tracker_.snapshot(steady_now_ns()); }

void Client::dispatch(const std::vector<Transition>& ts) {
  if (ts.empty()) return;
  std::lock_guard lk(hook_mu_);
  for (const auto& t : ts) {
    try {
      if (hooks_.on_state_change) hooks_.on_state_change(t.old_state, t.new_state, t.reason);
      // shadow mode only observes: no limit changes, no cancellation requests
      if (t.new_state == State::CAUTION && hooks_.on_caution && !cfg_.shadow) hooks_.on_caution(cfg_.caution_position_fraction, t.reason);
      if (t.new_state == State::HALT && hooks_.on_halt && !cfg_.shadow) hooks_.on_halt(t.reason);
      if (t.new_state == State::FLATTEN && hooks_.on_flatten) hooks_.on_flatten(t.reason);
    } catch (const std::exception& e) {
      std::cerr << "vetosentinel: hook threw: " << e.what() << "\n";  // never kill the SDK threads
    }
  }
}

void Client::subscriber_loop() {
  try {
    zmq::context_t ctx{1};
    zmq::socket_t sock{ctx, zmq::socket_type::sub};
    sock.set(zmq::sockopt::linger, 0);
    sock.set(zmq::sockopt::rcvhwm, 1000);
    sock.set(zmq::sockopt::reconnect_ivl, 200);
    sock.set(zmq::sockopt::reconnect_ivl_max, 2000);
    sock.set(zmq::sockopt::subscribe, cfg_.topic);
    sock.connect(cfg_.endpoint);
    zmq::pollitem_t items[] = {{sock.handle(), 0, ZMQ_POLLIN, 0}};
    while (!stop_.load()) {
      zmq::poll(items, 1, std::chrono::milliseconds{50});
      if (!(items[0].revents & ZMQ_POLLIN)) continue;
      std::vector<zmq::message_t> frames;
      while (true) {
        zmq::message_t msg;
        if (!sock.recv(msg, zmq::recv_flags::dontwait)) break;
        const bool more = msg.more();
        frames.push_back(std::move(msg));
        if (!more) {
          std::string_view payload = "<bad framing>";
          if (frames.size() == 2 && frames[0].to_string_view() == cfg_.topic) payload = frames[1].to_string_view();
          dispatch(tracker_.on_payload(payload, steady_now_ns(), std::chrono::system_clock::now()));
          frames.clear();
        }
      }
    }
  } catch (const std::exception& e) {
    // The watchdog and the hot-path staleness check will report HALT.
    std::cerr << "vetosentinel: subscriber thread stopped: " << e.what() << "\n";
  }
}

void Client::measure_check() {
  constexpr int kCalls = 1'000'000;
  std::uint64_t sink = 0;
  const auto t0 = steady_now_ns();
  for (int i = 0; i < kCalls; ++i) sink += decide(true) ? 1u : 0u;  // decide() is the gate minus shadow counting
  const auto dt = steady_now_ns() - t0;
  bench_sink_.store(sink, std::memory_order_relaxed);  // keeps the loop from being optimised away
  check_ns_.store(static_cast<double>(dt) / kCalls, std::memory_order_relaxed);
}

std::string Client::report_json(bool bye) {
  const auto s = tracker_.snapshot(steady_now_ns());
  const std::string host = hostname();
  const double check = check_ns();
  json j = {
      {"v", 1}, {"kind", "BOT"}, {"client_id", client_id_}, {"seq", ++report_seq_}, {"sent_ts", iso_now_ms()},
      {"name", cfg_.name.empty() ? "C++ bot on " + host : cfg_.name.substr(0, 80)}, {"sdk", "cpp"},
      {"pid", static_cast<std::int64_t>(getpid())}, {"host", host}, {"local_state", to_string(s.local_state)},
      {"published_state", s.published_state ? json(to_string(*s.published_state)) : json(nullptr)},
      {"in_fault", s.in_fault}, {"fault", s.fault == Fault::None ? json(nullptr) : json(to_string(s.fault))},
      {"last_seq", s.last_seq >= 0 ? json(s.last_seq) : json(nullptr)},
      {"heartbeat_age_ms", s.heartbeat_age_ms ? json(*s.heartbeat_age_ms) : json(nullptr)},
      {"received", s.stats.received}, {"valid", s.stats.valid}, {"recoveries", s.stats.recoveries},
      {"shadow", cfg_.shadow}, {"would_block", would_block()}, {"check_ns", check > 0 ? json(check) : json(nullptr)},
      {"bus_latency_ms", s.bus_latency_ms ? json(*s.bus_latency_ms) : json(nullptr)}, {"bye", bye}};
  return j.dump();
}

void Client::watchdog_loop() {
  // Fleet reports live here, on the watchdog thread: never on the order path. Any failure in
  // them is swallowed so it can never stop the watchdog.
  std::unique_ptr<zmq::context_t> ctx;
  std::unique_ptr<zmq::socket_t> push;
  std::string push_to;
  auto next_report = steady_clock::now(), next_bench = steady_clock::now();
  State reported = State::HALT;
  auto send = [&](bool bye) {
    const std::string body = report_json(bye);
    (void)push->send(zmq::buffer(body), zmq::send_flags::dontwait);  // best effort: dropped if nobody listens
  };
  while (!stop_.load()) {
    dispatch(tracker_.check_watchdog(steady_now_ns()));
    if (cfg_.report) {
      try {
        const std::string target = resolve_telemetry(tracker_.telemetry_endpoint(), cfg_.endpoint);
        if (target != push_to) {
          push.reset();
          push_to = target;
          if (!target.empty()) {
            if (!ctx) ctx = std::make_unique<zmq::context_t>(1);
            push = std::make_unique<zmq::socket_t>(*ctx, zmq::socket_type::push);
            push->set(zmq::sockopt::linger, 200);
            push->set(zmq::sockopt::sndhwm, 10);
            push->connect(target);
          }
        }
        const auto now = steady_clock::now();
        if (push && now >= next_bench) {
          measure_check();
          next_bench = now + seconds{10};
        }
        const State cur = current_state();
        if (push && (now >= next_report || cur != reported)) {
          send(false);
          next_report = now + seconds{1};
          reported = cur;
        }
      } catch (const std::exception& e) {
        std::cerr << "vetosentinel: fleet report failed: " << e.what() << "\n";
        push.reset();
        push_to.clear();
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds{cfg_.watchdog_period_ms});
  }
  try {
    if (push) send(true);  // goodbye: the fleet view shows "stopped" instead of "silent"
  } catch (const std::exception&) {
  }
  push.reset();
  ctx.reset();
}

}  // namespace vetosentinel
