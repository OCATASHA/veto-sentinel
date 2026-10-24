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
ctest --test-dir build --output-on-failure      # StateTracker unit tests (no sockets, no sleeps)
./build/strategy_demo tcp://127.0.0.1:5556 10   # run 10 s against a running monitor
```

`tests/test_cpp_interop.py` (in the Python suite) drives the built `strategy_demo` binary
from a Python publisher, including injected malformed and sequence-gap messages.

Tested here only with Apple clang 21 / libc++ on macOS arm64. The Debian Dockerfile is untested.
