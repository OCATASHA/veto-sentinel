// Dependency-free unit tests for the C++ StateTracker (no sockets, injected clocks, no sleeps).
#include <cstdio>
#include <cstdlib>
#include <string>

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

static const system_clock::time_point WALL0 = sys_days{year{2026} / 3 / 10} + hours{13};

static std::string iso(system_clock::time_point t) {
  auto ms = duration_cast<milliseconds>(t.time_since_epoch()).count();
  auto secs = sys_seconds{seconds{ms / 1000}};
  auto dp = floor<days>(secs);
  year_month_day ymd{dp};
  hh_mm_ss hms{secs - dp};
  char buf[40];
  std::snprintf(buf, sizeof buf, "%04d-%02u-%02uT%02ld:%02ld:%02lld.%03lldZ", int(ymd.year()), unsigned(ymd.month()),
                unsigned(ymd.day()), long(hms.hours().count()), long(hms.minutes().count()),
                (long long)hms.seconds().count(), (long long)(ms % 1000));
  return buf;
}

static std::string msg(long long seq, system_clock::time_point ts, const char* state = "NORMAL",
                       const char* pub = "pubA", bool ready = true, int timeout_ms = 3000) {
  return std::string(R"({"schema_version":1,"publisher_id":")") + pub + R"(","seq":)" + std::to_string(seq) +
         R"(,"kind":"HEARTBEAT","ts":")" + iso(ts) + R"(","as_of":null,"state":")" + state +
         R"(","risk_score":0.4,"components":{"panic_z":0.1,"drift_z":null,"volume_z":0.2},"reason":"test",)"
         R"("degraded":false,"degraded_reasons":[],"top_headlines":[],"monitor_ready":)" +
         (ready ? "true" : "false") + R"(,"heartbeat_interval_ms":1000,"heartbeat_timeout_ms":)" +
         std::to_string(timeout_ms) +
         R"(,"data_origin":"synthetic_fixture","implementations":{},"fallback_in_use":true,"control":null})";
}

struct Feed {
  StateTracker tr;
  std::int64_t mono = 1'000'000'000'000;
  system_clock::time_point wall = WALL0;
  long long seq = 0;
  explicit Feed(ClientConfig c = {}) : tr(std::move(c)) {}
  void advance(double s) {
    mono += static_cast<std::int64_t>(s * 1e9);
    wall += duration_cast<system_clock::duration>(duration<double>(s));
    tr.check_watchdog(mono);
  }
  void send(const char* state = "NORMAL", const char* pub = "pubA", bool ready = true, int to = 3000) {
    tr.on_payload(msg(++seq, wall, state, pub, ready, to), mono, wall);
  }
  void raw(const std::string& p) { tr.on_payload(p, mono, wall); }
  void hb(const char* state = "NORMAL", const char* pub = "pubA", bool ready = true) { advance(1.0); send(state, pub, ready); }
  State st() const { return tr.effective_state(mono); }
  Fault fault() const { return tr.snapshot(mono).fault; }
};

static void recover(Feed& f) {
  f.send();
  f.hb();
}

int main() {
  {  // timestamps
    CHECK(parse_utc_timestamp("2026-03-10T13:00:00.000Z").has_value());
    CHECK(*parse_utc_timestamp("2026-03-10T09:00:00-04:00") == WALL0);
    CHECK(*parse_utc_timestamp("2026-03-10T13:00:00Z") == WALL0);
    CHECK(!parse_utc_timestamp("2026-03-10T13:00:00"));      // naive
    CHECK(!parse_utc_timestamp("2026-02-30T13:00:00Z"));     // invalid date
    CHECK(!parse_utc_timestamp("garbage"));
  }
  {  // startup HALT, recovery after two snapshots
    Feed f;
    CHECK(f.st() == State::HALT);
    f.send();
    CHECK(f.st() == State::HALT);
    f.hb();
    CHECK(f.st() == State::NORMAL);
  }
  {  // watchdog
    Feed f;
    recover(f);
    f.advance(2.9);
    CHECK(f.st() == State::NORMAL);
    f.advance(0.2);
    CHECK(f.st() == State::HALT && f.fault() == Fault::StaleHeartbeat);
  }
  {  // hot path halts even if the watchdog never runs
    Feed f;
    recover(f);
    f.mono += 3'500'000'000;
    CHECK(f.tr.effective_state(f.mono) == State::HALT);
  }
  {  // malformed inputs
    const std::string good = msg(3, WALL0);
    for (std::string bad : {std::string("not json"), std::string("[]"), std::string("{}"),
                            std::string(R"({"schema_version":2})"), std::string(70000, 'x')}) {
      Feed f;
    recover(f);
      f.raw(bad);
      CHECK(f.st() == State::HALT && f.fault() == Fault::InvalidMessage);
    }
    auto replace = [](std::string s, const std::string& a, const std::string& b) {
      auto p = s.find(a);
      if (p != std::string::npos) s.replace(p, a.size(), b);
      return s;
    };
    for (auto [a, b] : {std::pair{"\"NORMAL\"", "\"YOLO\""}, {"\"seq\":3", "\"seq\":\"3\""},
                        {"\"monitor_ready\":true", "\"monitor_ready\":\"true\""}, {"0.4", "NaN"},
                        {".000Z", ".000"}}) {
      Feed f;
    recover(f);
      f.seq = 2;
      f.raw(replace(msg(3, f.wall), a, b));
      CHECK(f.st() == State::HALT && f.fault() == Fault::InvalidMessage);
    }
  }
  {  // sequence gap -> HALT -> recovery
    Feed f;
    recover(f);
    f.seq += 3;
    f.hb();
    CHECK(f.st() == State::HALT && f.fault() == Fault::SequenceGap);
    f.hb();
    CHECK(f.st() == State::HALT);
    f.hb();
    CHECK(f.st() == State::NORMAL);
  }
  {  // duplicate / out of order
    Feed f;
    recover(f);
    f.raw(msg(f.seq, f.wall));
    CHECK(f.fault() == Fault::Duplicate && f.st() == State::HALT);
    Feed g;
    recover(g);
    g.raw(msg(1, g.wall));
    CHECK(g.fault() == Fault::OutOfOrder && g.st() == State::HALT);
  }
  {  // publisher restart
    Feed f;
    recover(f);
    f.seq = 0;
    f.hb("NORMAL", "pubB");
    CHECK(f.fault() == Fault::PublisherRestart && f.st() == State::HALT);
    f.hb("NORMAL", "pubB");
    f.hb("NORMAL", "pubB");
    CHECK(f.st() == State::NORMAL);
  }
  {  // stale / future timestamps
    Feed f;
    recover(f);
    f.raw(msg(++f.seq, f.wall - seconds{10}));
    CHECK(f.fault() == Fault::StaleTimestamp && f.st() == State::HALT);
    Feed g;
    recover(g);
    g.raw(msg(++g.seq, g.wall + seconds{5}));
    CHECK(g.fault() == Fault::FutureTimestamp && g.st() == State::HALT);
  }
  {  // monitor not ready
    Feed f;
    recover(f);
    f.hb("CAUTION", "pubA", false);
    CHECK(f.st() == State::HALT && f.fault() == Fault::MonitorNotReady);
    f.hb("CAUTION");
    f.hb("CAUTION");
    CHECK(f.st() == State::CAUTION);
  }
  {  // published changes apply immediately when healthy; FLATTEN never downgraded
    Feed f;
    recover(f);
    f.hb("HALT");
    CHECK(f.st() == State::HALT);
    f.hb("NORMAL");
    CHECK(f.st() == State::NORMAL);
    Feed g;
    g.send("FLATTEN");
    CHECK(g.st() == State::FLATTEN);
    g.advance(10);
    CHECK(g.st() == State::FLATTEN);
  }
  {  // advertised shorter timeout honoured
    Feed f;
    f.tr.on_payload(msg(++f.seq, f.wall, "NORMAL", "pubA", true, 1500), f.mono, f.wall);
    f.advance(1.0);
    f.tr.on_payload(msg(++f.seq, f.wall, "NORMAL", "pubA", true, 1500), f.mono, f.wall);
    CHECK(f.st() == State::NORMAL);
    f.advance(1.6);
    CHECK(f.st() == State::HALT);
  }
  {  // order gate on an unstarted client: HALT, no network
    Client c{ClientConfig{}};
    CHECK(c.current_state() == State::HALT);
    CHECK(!c.can_submit_order(true) && !c.can_submit_order(false));
    CHECK(c.position_limit(100.0) == 0.0);
  }
  if (g_failures) {
    std::fprintf(stderr, "%d check(s) failed\n", g_failures);
    return 1;
  }
  std::printf("all C++ tracker tests passed\n");
  return 0;
}
