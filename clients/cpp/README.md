# Veto Sentinel C++20 client SDK

A minimal subscriber with the same fail-safe policy as the Python SDK (see
[../../docs/MESSAGE_SCHEMA.md](../../docs/MESSAGE_SCHEMA.md)).

- `vetosentinel::Client` runs a **subscriber thread** (ZeroMQ SUB, validation, sequencing) and a
  **watchdog thread** (heartbeat timeout).
- The locally published state is a `std::atomic<State>`. The last-valid time and timeout are
  `std::atomic<int64_t>`.
- `can_submit_order(bool opening)`, `current_state()` and `position_limit(x)` read only those
  atomics and `steady_clock`: no locks, no allocation, no network. They also check heartbeat age
  themselves, so a dead background thread still gives HALT.
- `vetosentinel::StateTracker` is the pure logic with injected clocks. It is used by the tests.
- `Hooks` (`on_state_change`, `on_caution`, `on_halt`, `on_flatten`) are where a strategy wires
  its own behaviour. There is no broker integration, and `on_flatten` must notify a human, never
  liquidate.
- **Shadow mode** (`cfg.shadow = true`): `can_submit_order` always allows and counts what it would
  have blocked (`would_block()`); `on_caution` and `on_halt` are not called. `decide()` always gives
  the real answer. For running next to a strategy before anyone relies on it.
- **Fleet reports**: when the monitor advertises a `telemetry` endpoint, the watchdog thread sends
  a small JSON report every second (and on state changes, plus a goodbye on `stop()`) over a ZeroMQ
  PUSH socket. It also times a million `decide()` calls every 10 s (`check_ns()`). None of this
  runs on the order path. Off with `cfg.report = false`.

```cpp
#include "vetosentinel/client.hpp"
vetosentinel::ClientConfig cfg;            // endpoint tcp://127.0.0.1:5556, timeout 3000 ms, ...
vetosentinel::Hooks hooks;
hooks.on_halt = [](const std::string& why) { /* request cancel of resting orders (your code) */ };
vetosentinel::Client client(cfg, hooks);
client.start();
if (client.can_submit_order(/*opening=*/true)) { /* strategy order path */ }
```

## Build

```bash
# macOS
brew install zeromq cppzmq nlohmann-json cmake
# Debian/Ubuntu
sudo apt install g++ cmake libzmq3-dev cppzmq-dev nlohmann-json3-dev

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure      # tracker unit tests + the threaded client test
./build/strategy_demo tcp://127.0.0.1:5556 10   # run 10 s against a running monitor (0 = until Ctrl+C)
./sanitize.sh                                   # the same tests under ThreadSanitizer and ASan + UBSan
```

`tests/test_client_threads.cpp` runs a real ZeroMQ publisher, the client's subscriber and watchdog
threads, two strategy threads on the order gate, heartbeat loss and recovery, fleet reports and
shadow mode at once, which is what ThreadSanitizer needs to see. Both sanitizer builds run clean
(Apple clang 21, macOS arm64); libzmq itself is not instrumented.

`strategy_demo` reads `VETOSENTINEL_BOT_NAME` (its name in the dashboard's fleet view) and
`VETOSENTINEL_SHADOW=1`. SIGINT/SIGTERM stop it cleanly; SIGKILL makes it go silent, like a crash.
The dashboard's **Start a C++ bot** button runs it.

`tests/test_cpp_interop.py` (in the Python suite) drives the built `strategy_demo` binary
from a Python publisher, including injected malformed and sequence-gap messages.

Tested here only with Apple clang 21 / libc++ on macOS arm64. The Debian Dockerfile is untested.
