from datetime import UTC, datetime, timedelta

from vetosentinel.bus.schema import encode
from vetosentinel.timeutil import iso_ms

WALL0 = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)


def make_msg(seq, *, state="NORMAL", publisher_id="pubA", ts=None, monitor_ready=True, kind="HEARTBEAT",
             timeout_ms=3000, **over):
    body = {
        "schema_version": 1, "publisher_id": publisher_id, "seq": seq, "kind": kind,
        "ts": iso_ms(ts or WALL0), "as_of": iso_ms(WALL0), "state": state, "risk_score": 0.4,
        "components": {"panic_z": 0.1, "drift_z": 0.2, "volume_z": None}, "reason": "test",
        "degraded": False, "degraded_reasons": [], "top_headlines": [], "monitor_ready": monitor_ready,
        "heartbeat_interval_ms": 1000, "heartbeat_timeout_ms": timeout_ms, "data_origin": "synthetic_fixture",
        "implementations": {"panic": "fallback-lexicon-v1"}, "fallback_in_use": True, "control": None,
    }
    body.update(over)
    return encode(body)


class Feed:
    """Drives a StateTracker with a fake monotonic + wall clock (no sleeps)."""

    def __init__(self, tracker):
        self.tr = tracker
        self.mono = 1000.0
        self.wall = WALL0
        self.seq = 0

    def advance(self, seconds):
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)
        return self.tr.check_watchdog(self.mono)

    def send(self, payload=None, **kw):
        if payload is None:
            self.seq += 1
            payload = make_msg(self.seq, ts=self.wall, **kw)
        return self.tr.on_payload(payload, self.mono, self.wall)

    def heartbeat(self, **kw):
        out = self.advance(1.0)
        return out + self.send(**kw)
