FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ cmake make libzmq3-dev cppzmq-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt ./
COPY include ./include
COPY src ./src
COPY examples ./examples
COPY tests ./tests
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j \
    && ctest --test-dir build --output-on-failure

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libzmq5 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/strategy_demo /usr/local/bin/strategy_demo
ENTRYPOINT ["strategy_demo"]
