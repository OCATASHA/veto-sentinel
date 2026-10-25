# Signal bus contract (schema_version 1)

Transport: ZeroMQ PUB/SUB. The publisher binds `tcp://*:5556`, and each message is a
two-frame multipart `[topic, json]` with topic `vs.v1`. JSON is UTF-8, compact, NaN/Infinity
forbidden, at most 64 KiB.

## Fields

Every message (`STATE`, `HEARTBEAT`, `CONTROL`) carries a **complete snapshot**:

| field | type | meaning |
|---|---|---|
| `schema_version` | int, must be 1 | contract version |
| `publisher_id` | string ≤ 64 | random id per publisher process start; a change means a restart |
| `seq` | int ≥ 1 | per-publisher counter, **+1 for every message of any kind** |
| `kind` | `STATE` \| `HEARTBEAT` \| `CONTROL` | STATE = state changed; CONTROL = operator event; HEARTBEAT = periodic |
| `ts` | ISO-8601 UTC with `Z`, ms | wall-clock emission time (used for freshness) |
| `as_of` | ISO-8601 UTC or null | data time of the assessment (simulated time during replay) |
| `state` | `NORMAL`\|`CAUTION`\|`HALT`\|`FLATTEN` | published state |
| `risk_score` | number or null | R(t), finite |
| `components` | `{panic_z, drift_z, volume_z}` numbers or null | null = unavailable (never 0 by default) |
| `reason` | string | human-readable explanation of the current state |
| `degraded`, `degraded_reasons` | bool, [string] | degraded-data policy status, including not-ready reasons |
| `top_headlines` | [{headline_id, headline, source}] ≤ 20 | most negative recent headlines |
| `monitor_ready` | bool (strict) | false during warm-up, stale ingestion, missing required data, or pipeline stall |
| `heartbeat_interval_ms`, `heartbeat_timeout_ms` | int | publisher's timing |
| `data_origin` | string | `live_provider` / `historical_dataset` / `synthetic_fixture` |
| `implementations` | {component: id} | e.g. `finbert:ProsusAI/finbert` or `fallback-lexicon-v1` |
| `fallback_in_use` | bool | true if any component uses a fallback heuristic |
| `control` | object or null | last operator event, with `"automatic_execution": false` |
| `focus` | string or null | what the risk score is optimized for (comma-separated phrases); null = whole market |
| `telemetry` | string or null | where clients may send fleet reports (bind address; a `*` or loopback host means "the host you subscribed to"); null = no fleet view |

Unknown extra fields are ignored, which keeps the format forward compatible. A missing or
mistyped required field invalidates the message. Strict types are enforced: `"seq": "7"`
and `"monitor_ready": "true"` are rejected.

The example in the original brief (`"seq": 18422, "state": "HALT", ...`) illustrates the
shape only. Its values are not observed results.

## Heartbeat

The publisher sends a HEARTBEAT every 1000 ms (configurable), and STATE/CONTROL messages
immediately on change. `seq` is assigned in the single publisher thread at send time, and only
after the message has encoded successfully, so it is strictly monotonic. If the analytics loop
stops reporting liveness for 30 s, heartbeats are overridden to `state=HALT,
monitor_ready=false` with reason "analytics pipeline stalled".

## Client recovery policy (Python `client/tracker.py` and C++ `StateTracker`, identical)

- Startup: local state `HALT` (fault `startup`).
- These trigger immediate local `HALT` (fault) and are counted:
  `stale_heartbeat` (no valid message within `min(client timeout, advertised timeout)`, default 3 s),
  `invalid_message`, `sequence_gap`, `duplicate_seq`, `out_of_order_seq`, `publisher_restart`,
  `stale_timestamp` (older than 3 s), `future_timestamp` (more than 1 s ahead), `monitor_not_ready`.
- Stale or invalid messages do **not** refresh the watchdog.
- A gap message re-baselines the sequence but does not count toward recovery.
- Recovery requires `recovery_snapshots_required` (default 2) consecutive valid, fresh,
  in-sequence snapshots with `monitor_ready=true`. The client then adopts the published state.
- When healthy, published changes (both directions) apply immediately. De-escalation
  hysteresis is the monitor's job.
- `FLATTEN` is never downgraded by a fault.
- The hot path (`can_submit_order`, `current_state`, `position_limit`) reads only local
  atomics and the monotonic clock. It also checks heartbeat age itself, so a stalled or crashed
  subscriber thread still produces HALT.

Order gate: NORMAL allows any order. CAUTION allows only position-reducing orders, with the
limit multiplied by `caution_position_fraction` (0.25). HALT and FLATTEN allow nothing.

## What PUB/SUB does and does not give you

ZeroMQ PUB/SUB is fire-and-forget. Subscribers that connect late miss earlier messages (the
slow-joiner problem). Messages are dropped at the high-water mark and lost during
disconnects, and nothing is acknowledged. Veto Sentinel does **not** make the transport
reliable. It makes loss *fail safe*: every message is a full snapshot, so the next heartbeat
repairs a missed update, and any silence, gap or reordering forces HALT until fresh
in-sequence snapshots arrive. Wall-clock freshness checks assume publisher and client clocks
are NTP-synchronised to well within `max_message_age_ms`. Messages are not authenticated (no
CURVE). Anyone who can reach the port can publish, which matters for anything beyond a local demo.

## Fleet reports (reverse channel, display only)

Clients send one JSON object per ZeroMQ message over PUSH to the advertised `telemetry` endpoint:
every second, on every local state change, and once with `"bye": true` when they stop. It is sent
from a background thread, never from the order path, and dropped if nobody is listening.

| field | meaning |
|---|---|
| `v`, `kind` | `1`, `"BOT"` |
| `client_id`, `name`, `sdk`, `pid`, `host` | who is reporting (`sdk`: `python` or `cpp`) |
| `local_state`, `published_state`, `in_fault`, `fault`, `last_seq` | the client's own view |
| `heartbeat_age_ms`, `received`, `valid`, `recoveries` | its watchdog and counters |
| `shadow`, `would_block` | shadow mode, and orders it would have blocked |
| `check_ns` | measured cost of one order check (timed by the client itself) |
| `bus_latency_ms` | publish-to-receive time of the last valid message |
| `bye` | the client stopped cleanly |

The monitor validates each report (at most 4 KiB, typed fields, 50 clients) and shows the latest per
client: silent after the heartbeat timeout, forgotten after two minutes. Reports are
unauthenticated, like the bus, and never influence the risk state or any bot.

## Demo fault injection

`POST /api/fault/{silence_5s|malformed|seq_gap}` (dashboard buttons). This is enabled by
`bus.allow_fault_injection` and exists to demonstrate the client fail-safes. Turn it off
outside demos.
