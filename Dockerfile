# Veto Sentinel monitor (pipeline + ZeroMQ publisher + dashboard).
# Default image has NO model weights: the monitor runs with the clearly labelled fallback
# heuristics. Build with --build-arg INSTALL_ML=1 to add transformers/torch/sentence-transformers
# (models are downloaded from Hugging Face on first start unless pre-cached).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ARG INSTALL_ML=0
RUN if [ "$INSTALL_ML" = "1" ]; then pip install --no-cache-dir ".[ml]"; fi

COPY config ./config
COPY fixtures ./fixtures
COPY scripts ./scripts

RUN useradd --create-home vs && mkdir -p /data && chown vs /data
USER vs

EXPOSE 8000 5556
CMD ["vetosentinel", "replay", "--config", "config/default.toml", "--fixture", "fixtures/synthetic_demo.jsonl", \
     "--host", "0.0.0.0", "--pub", "tcp://*:5556", "--db", "/data/vetosentinel.duckdb"]
