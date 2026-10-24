from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from vetosentinel.config import CompositeConfig, StateMachineConfig
from vetosentinel.engine.state_machine import EngineInput, StateMachine
from vetosentinel.models import ChangePointResult, RiskState

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def cfg(**kw):
    base = dict(caution_enter=2.0, caution_exit=1.0, halt_enter=3.5, halt_exit=2.5, cooldown_minutes=30)
    base.update(kw)
    return StateMachineConfig(**base)


class Driver:
    def __init__(self, sm):
        self.sm = sm
        self.t = T0

    def step(self, score, ready=True, degraded=False, cp_active=False):
        cp = ChangePointResult(detector="cusum", statistic=7.0 if cp_active else 0.0, threshold=6.0,
                               alarm=cp_active, alarm_active=cp_active)
        d = self.sm.step(EngineInput(as_of=self.t, risk_score=score, monitor_ready=ready,
                                     degraded=degraded, degraded_reasons=["drift unavailable"] if degraded else [],
                                     not_ready_reasons=[] if ready else ["warm-up"], changepoint=cp))
        self.t += STEP
        return d


def ready_normal(drv):
    """Bring a fresh machine from startup HALT to NORMAL with a calm score."""
    for _ in range(40):
        drv.step(0.0)
        if drv.sm.state is RiskState.NORMAL:
            return
    raise AssertionError("never reached NORMAL")


def test_starts_in_halt_and_stays_while_not_ready():
    drv = Driver(StateMachine(cfg()))
    assert drv.sm.state is RiskState.HALT
    for _ in range(20):
        d = drv.step(0.0, ready=False)
        assert d.new_state is RiskState.HALT and d.rule in ("monitor_not_ready", "hold")
    assert "warm-up" in d.reason


def test_none_score_is_treated_as_not_ready():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    d = drv.step(None, ready=True)
    assert d.new_state is RiskState.HALT and d.rule == "monitor_not_ready"


def test_recovery_is_one_level_per_cooldown():
    drv = Driver(StateMachine(cfg()))
    states = [drv.step(0.0).new_state for _ in range(16)]
    # 30 min cooldown / 5 min ticks: first tick starts the timer, HALT->CAUTION at tick 7,
    # CAUTION->NORMAL six ticks after that (timer restarts after each transition).
    assert states[:6] == [RiskState.HALT] * 6
    assert states[6] is RiskState.CAUTION
    assert RiskState.NORMAL in states
    first_normal = states.index(RiskState.NORMAL)
    assert first_normal - 6 >= 6


def test_escalation_is_immediate():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    d = drv.step(2.1)
    assert (d.prev_state, d.new_state, d.rule) == (RiskState.NORMAL, RiskState.CAUTION, "escalate")
    d = drv.step(4.0)
    assert d.new_state is RiskState.HALT and d.changed


def test_jump_straight_to_halt():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    assert drv.step(5.0).new_state is RiskState.HALT


def test_hysteresis_prevents_flapping():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    drv.step(2.2)  # -> CAUTION
    changes = 0
    for s in [1.9, 2.1, 1.5, 2.05, 1.2, 1.99] * 5:  # oscillates around caution_enter, above caution_exit
        changes += drv.step(s).changed
    assert drv.sm.state is RiskState.CAUTION
    assert changes == 0


def test_cooldown_resets_when_exit_condition_interrupted():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    drv.step(2.5)  # CAUTION
    for _ in range(5):  # 25 min below exit
        assert drv.step(0.5).new_state is RiskState.CAUTION
    drv.step(1.5)  # interrupt: above caution_exit, below caution_enter
    for _ in range(6):  # another 25 minutes - would have completed without the reset
        assert drv.step(0.5).new_state is RiskState.CAUTION
    assert drv.step(0.5).new_state is RiskState.NORMAL


def test_halt_needs_score_below_halt_exit():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    drv.step(4.0)
    for _ in range(20):
        assert drv.step(3.0).new_state is RiskState.HALT  # between halt_exit and halt_enter


def test_degraded_floor_blocks_normal():
    drv = Driver(StateMachine(cfg()))
    for _ in range(60):
        d = drv.step(0.0, degraded=True)
    assert d.new_state is RiskState.CAUTION
    assert "degraded" in d.reason
    ready_normal(drv)
    assert drv.step(0.0, degraded=True).new_state is RiskState.CAUTION  # escalates immediately to floor


def test_degraded_floor_halt_option():
    drv = Driver(StateMachine(cfg(degraded_floor="HALT")))
    for _ in range(60):
        assert drv.step(0.0, degraded=True).new_state is RiskState.HALT


def test_changepoint_escalates():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    assert drv.step(1.5, cp_active=True).new_state is RiskState.CAUTION
    assert drv.step(2.2, cp_active=True).new_state is RiskState.HALT


def test_changepoint_can_be_disabled():
    drv = Driver(StateMachine(cfg(changepoint_escalates=False)))
    ready_normal(drv)
    assert drv.step(1.5, cp_active=True).new_state is RiskState.NORMAL


def test_flatten_is_sticky_and_operator_only():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    d = drv.sm.force_flatten(drv.t, "alice", "drill")
    assert d.new_state is RiskState.FLATTEN and d.rule == "operator_flatten"
    for _ in range(50):
        assert drv.step(0.0).new_state is RiskState.FLATTEN
    assert drv.step(0.0, ready=False).new_state is RiskState.FLATTEN
    d = drv.sm.clear_flatten(drv.t, "alice", "drill over")
    assert d.new_state is RiskState.HALT
    with pytest.raises(ValueError):
        drv.sm.clear_flatten(drv.t, "alice", "again")


def test_every_decision_is_auditable():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    d = drv.step(3.0)
    assert d.as_of.tzinfo is not None and d.wall_ts.tzinfo is not None
    assert d.rule and d.reason and d.changepoint is not None
    assert "monitor_ready" in d.health


def test_time_cannot_go_backwards():
    sm = StateMachine(cfg())
    sm.step(EngineInput(as_of=T0, risk_score=0.0, monitor_ready=True))
    with pytest.raises(ValueError):
        sm.step(EngineInput(as_of=T0 - STEP, risk_score=0.0, monitor_ready=True))


@pytest.mark.parametrize("bad", [
    dict(caution_exit=2.0, caution_enter=2.0),
    dict(halt_exit=3.6),
    dict(caution_enter=4.0),
])
def test_invalid_thresholds_rejected(bad):
    with pytest.raises(ValidationError):
        cfg(**bad)


def test_weights_validated():
    with pytest.raises(ValidationError):
        CompositeConfig(weights={"panic": 0.5, "drift": 0.5, "volume": 0.5})
    with pytest.raises(ValidationError):
        CompositeConfig(weights={"panic": 1.0, "drift": 0.0})
    with pytest.raises(ValidationError):
        CompositeConfig(weights={"panic": 1.2, "drift": -0.2, "volume": 0.0})


def test_cooldown_status_counts_down_and_resets():
    drv = Driver(StateMachine(cfg()))
    ready_normal(drv)
    drv.step(2.5)  # -> CAUTION
    assert drv.sm.cooldown_status(drv.t) is None  # still stressed: nothing counting yet
    drv.step(0.5)
    start = drv.t - STEP
    drv.step(0.5)
    st = drv.sm.cooldown_status(drv.t - STEP)
    assert st is not None and st["next_state"] == "NORMAL" and st["total_s"] == 1800
    assert st["elapsed_s"] == 300 and st["remaining_s"] == 1500 and st["started"] == start.isoformat()
    drv.step(1.5)  # flare-up above caution_exit: countdown resets
    assert drv.sm.cooldown_status(drv.t) is None


def test_live_cooldown_runs_on_the_clock_and_needs_newer_news():
    """Live news arrives late and in batches: the wait is timed on the monitor's clock, not news time."""
    sm = StateMachine(cfg())
    news, now = T0, T0 + timedelta(minutes=40)

    def step(score, news_t, now_t):
        return sm.step(EngineInput(as_of=news_t, risk_score=score, monitor_ready=True, now=now_t))

    def left(machine, at):
        st = machine.cooldown_status(at)
        assert st is not None
        return st["remaining_s"]

    assert step(2.2, news, now).rule == "cooldown_pending"  # startup HALT, eased: cooling off starts
    assert left(sm, now + timedelta(minutes=10)) == 1200
    d = step(2.2, news + timedelta(minutes=45), now + timedelta(minutes=5))  # a news backlog lands at once
    assert d.new_state is RiskState.HALT and left(sm, now + timedelta(minutes=5)) == 1500
    sm2 = StateMachine(cfg())
    step2 = lambda n, w: sm2.step(EngineInput(as_of=n, risk_score=2.2, monitor_ready=True, now=w))  # noqa: E731
    step2(news, now)
    d = step2(news, now + timedelta(minutes=31))  # 31 real minutes, but no newer news at all
    assert d.new_state is RiskState.HALT and "waiting for newer news" in d.reason
    st = sm2.cooldown_status(now + timedelta(minutes=31))
    assert st is not None and st["remaining_s"] == 0 and st["needs_newer_news"]
    d = step2(news + timedelta(minutes=15), now + timedelta(minutes=32))  # a newer batch, still calm
    assert d.new_state is RiskState.CAUTION and d.rule == "deescalate_after_cooldown"
    # Live news time can follow the clock without any new headline (GDELT stamps files ahead of
    # time): only newly ingested headlines count as new news.
    sm3 = StateMachine(cfg())
    step3 = lambda n, w, seen: sm3.step(EngineInput(as_of=n, risk_score=2.2, monitor_ready=True, now=w,  # noqa: E731
                                                    news_seen=seen))
    step3(news, now, 100)
    d = step3(news + timedelta(minutes=31), now + timedelta(minutes=31), 100)
    assert d.new_state is RiskState.HALT and "waiting for newer news" in d.reason
    assert step3(news + timedelta(minutes=32), now + timedelta(minutes=32), 103).new_state is RiskState.CAUTION
