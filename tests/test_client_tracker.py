from datetime import timedelta

from helpers import Feed, make_msg
from vetosentinel.client.tracker import ClientConfig, Fault, StateTracker
from vetosentinel.models import RiskState


def recovered_feed(**cfg):
    f = Feed(StateTracker(ClientConfig(**cfg)))
    f.send()
    f.heartbeat()
    assert f.tr.local_state is RiskState.NORMAL and not f.tr.in_fault
    return f


def test_startup_is_halt_and_needs_two_valid_snapshots():
    f = Feed(StateTracker(ClientConfig()))
    assert f.tr.local_state is RiskState.HALT
    assert f.tr.effective_state(f.mono) is RiskState.HALT
    f.send()
    assert f.tr.local_state is RiskState.HALT  # one snapshot is not enough
    f.heartbeat()
    assert f.tr.local_state is RiskState.NORMAL


def test_recovery_count_configurable():
    f = Feed(StateTracker(ClientConfig(recovery_snapshots_required=1)))
    f.send()
    assert f.tr.local_state is RiskState.NORMAL


def test_watchdog_halts_on_stale_heartbeat():
    f = recovered_feed()
    assert f.advance(2.9) == []
    out = f.advance(0.2)
    assert out and out[-1].new is RiskState.HALT
    assert f.tr.fault is Fault.STALE_HEARTBEAT


def test_hot_path_halts_even_without_watchdog_thread():
    f = recovered_feed()
    f.mono += 3.5  # watchdog never runs
    assert f.tr.local_state is RiskState.NORMAL
    assert f.tr.effective_state(f.mono) is RiskState.HALT


def test_advertised_shorter_timeout_is_honoured():
    f = Feed(StateTracker(ClientConfig()))
    f.send(timeout_ms=1500)
    f.heartbeat(timeout_ms=1500)
    assert f.tr.timeout_s == 1.5
    assert f.advance(1.6)[-1].new is RiskState.HALT


def test_malformed_messages_fail_safe():
    for bad in [b"not json", b"[]", b"{}", b'{"schema_version": 2}', b"\xff\xfe",
                make_msg(99).replace(b'"NORMAL"', b'"YOLO"'),
                make_msg(99).replace(b'"seq":99', b'"seq":"99"'),
                make_msg(99).replace(b'"monitor_ready":true', b'"monitor_ready":"true"'),
                make_msg(99).replace(b'"risk_score":0.4', b'"risk_score":NaN'),
                make_msg(99).replace(b'.000Z"', b'.000"', 1),  # naive timestamp
                b"x" * 70_000]:
        f = recovered_feed()
        out = f.send(payload=bad)
        assert f.tr.local_state is RiskState.HALT, bad[:60]
        assert out and f.tr.fault is Fault.INVALID_MESSAGE


def test_sequence_gap_halts_then_recovers():
    f = recovered_feed()
    f.seq += 3  # lose three messages
    f.heartbeat()
    assert f.tr.local_state is RiskState.HALT and f.tr.fault is Fault.SEQUENCE_GAP
    f.heartbeat()
    assert f.tr.local_state is RiskState.HALT  # 1 of 2
    f.heartbeat()
    assert f.tr.local_state is RiskState.NORMAL
    assert f.tr.stats.faults["sequence_gap"] == 1


def test_duplicate_and_replayed_messages_halt():
    f = recovered_feed()
    dup = make_msg(f.seq, ts=f.wall)
    f.send(payload=dup)
    assert f.tr.fault is Fault.DUPLICATE and f.tr.local_state is RiskState.HALT
    f = recovered_feed()
    f.send(payload=make_msg(1, ts=f.wall))
    assert f.tr.fault is Fault.OUT_OF_ORDER and f.tr.local_state is RiskState.HALT


def test_publisher_restart_requires_fresh_recovery():
    f = recovered_feed()
    f.seq = 0
    f.heartbeat(publisher_id="pubB")
    assert f.tr.fault is Fault.PUBLISHER_RESTART and f.tr.local_state is RiskState.HALT
    f.heartbeat(publisher_id="pubB")
    f.heartbeat(publisher_id="pubB")
    assert f.tr.local_state is RiskState.NORMAL


def test_stale_and_future_timestamps_rejected():
    f = recovered_feed()
    f.seq += 1
    f.send(payload=make_msg(f.seq, ts=f.wall - timedelta(seconds=10)))
    assert f.tr.fault is Fault.STALE_TIMESTAMP and f.tr.local_state is RiskState.HALT
    f = recovered_feed()
    f.seq += 1
    f.send(payload=make_msg(f.seq, ts=f.wall + timedelta(seconds=5)))
    assert f.tr.fault is Fault.FUTURE_TIMESTAMP and f.tr.local_state is RiskState.HALT


def test_stale_messages_do_not_refresh_the_watchdog():
    f = recovered_feed()
    for _ in range(5):
        f.advance(1.0)
        f.seq += 1
        f.send(payload=make_msg(f.seq, ts=f.wall - timedelta(seconds=30)))
    assert f.tr.effective_state(f.mono) is RiskState.HALT


def test_monitor_not_ready_halts():
    f = recovered_feed()
    f.heartbeat(monitor_ready=False, state="CAUTION")
    assert f.tr.local_state is RiskState.HALT and f.tr.fault is Fault.MONITOR_NOT_READY
    f.heartbeat(state="CAUTION")
    f.heartbeat(state="CAUTION")
    assert f.tr.local_state is RiskState.CAUTION


def test_published_state_changes_apply_immediately_when_healthy():
    f = recovered_feed()
    f.heartbeat(state="CAUTION", kind="STATE")
    assert f.tr.local_state is RiskState.CAUTION
    f.heartbeat(state="HALT", kind="STATE")
    assert f.tr.local_state is RiskState.HALT
    f.heartbeat(state="NORMAL")
    assert f.tr.local_state is RiskState.NORMAL


def test_flatten_propagates_even_while_in_fault_and_is_not_downgraded():
    f = Feed(StateTracker(ClientConfig()))
    f.send(state="FLATTEN", kind="CONTROL")
    assert f.tr.local_state is RiskState.FLATTEN
    f.advance(5.0)
    assert f.tr.local_state is RiskState.FLATTEN
    assert f.tr.effective_state(f.mono) is RiskState.FLATTEN


def test_order_gate_semantics():
    from vetosentinel.client import VetoSentinelClient

    c = VetoSentinelClient()  # never started: no network
    assert c.current_state is RiskState.HALT
    assert not c.can_submit_order(opening=True) and not c.can_submit_order(opening=False)
    assert c.position_limit(100) == 0.0
    import time
    c.tracker.local_state = RiskState.CAUTION
    c.tracker.last_valid_mono = time.monotonic()
    assert not c.can_submit_order(opening=True) and c.can_submit_order(opening=False)
    assert c.position_limit(100) == 25.0
    c.tracker.local_state = RiskState.NORMAL
    assert c.can_submit_order(opening=True)
    c.tracker.last_valid_mono = time.monotonic() - 10
    assert not c.can_submit_order(opening=False)
