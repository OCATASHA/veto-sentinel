#!/usr/bin/env bash
# Build and run the C++ client tests under ThreadSanitizer and AddressSanitizer + UBSan.
#
#   clients/cpp/sanitize.sh            # both
#   clients/cpp/sanitize.sh thread     # one of: thread, address
#
# The threaded test (tests/test_client_threads.cpp) runs a real ZeroMQ publisher, the client's
# subscriber and watchdog threads and two strategy threads on the order gate at once, which is
# what ThreadSanitizer needs to see. libzmq itself is not instrumented.
set -euo pipefail
cd "$(dirname "$0")"
kinds=("${@:-thread address}")
for kind in ${kinds[@]}; do
  dir="build-$kind"
  echo "== $kind sanitizer ($dir)"
  cmake -S . -B "$dir" -DCMAKE_BUILD_TYPE=Debug -DVS_SANITIZE="$kind" > /dev/null
  cmake --build "$dir" -j > /dev/null
  TSAN_OPTIONS="halt_on_error=1 second_deadlock_stack=1" ASAN_OPTIONS="halt_on_error=1 detect_leaks=0" \
    UBSAN_OPTIONS="halt_on_error=1 print_stacktrace=1" ctest --test-dir "$dir" --output-on-failure
done
echo "sanitizer runs clean: ${kinds[*]}"
