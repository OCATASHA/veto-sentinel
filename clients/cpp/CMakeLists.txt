cmake_minimum_required(VERSION 3.20)
project(vetosentinel_client CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# libzmq + cppzmq (header-only) + nlohmann/json (header-only).
# macOS: brew install zeromq cppzmq nlohmann-json
# Debian/Ubuntu: apt install libzmq3-dev cppzmq-dev nlohmann-json3-dev
find_path(ZMQ_INCLUDE_DIR zmq.h HINTS /opt/homebrew/include /usr/local/include)
find_path(CPPZMQ_INCLUDE_DIR zmq.hpp HINTS /opt/homebrew/include /usr/local/include)
find_path(NLOHMANN_INCLUDE_DIR nlohmann/json.hpp HINTS /opt/homebrew/include /usr/local/include)
find_library(ZMQ_LIBRARY NAMES zmq libzmq HINTS /opt/homebrew/lib /usr/local/lib)
foreach(v ZMQ_INCLUDE_DIR CPPZMQ_INCLUDE_DIR NLOHMANN_INCLUDE_DIR ZMQ_LIBRARY)
  if(NOT ${v})
    message(FATAL_ERROR "${v} not found: install libzmq, cppzmq and nlohmann-json (see clients/cpp/README.md)")
  endif()
endforeach()

find_package(Threads REQUIRED)

add_library(vetosentinel_client src/client.cpp)
target_include_directories(vetosentinel_client
  PUBLIC include
  PRIVATE ${ZMQ_INCLUDE_DIR} ${CPPZMQ_INCLUDE_DIR} ${NLOHMANN_INCLUDE_DIR})
target_link_libraries(vetosentinel_client PUBLIC ${ZMQ_LIBRARY} Threads::Threads)
target_compile_options(vetosentinel_client PRIVATE -Wall -Wextra -Wpedantic)

add_executable(strategy_demo examples/strategy_demo.cpp)
target_link_libraries(strategy_demo PRIVATE vetosentinel_client)

enable_testing()
add_executable(test_tracker tests/test_tracker.cpp)
target_link_libraries(test_tracker PRIVATE vetosentinel_client)
target_compile_options(test_tracker PRIVATE -Wall -Wextra)
add_test(NAME tracker COMMAND test_tracker)
