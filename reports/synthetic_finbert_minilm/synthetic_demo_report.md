# Veto Sentinel replay evaluation

> **SYNTHETIC DEMO DATA - NOT OBSERVED HISTORY**
>
> SYNTHETIC DEMO DATA: these results only demonstrate that the pipeline and state machine behave as designed on an invented scenario. They are NOT evidence of real-world detection ability, lead time, or profitability.

- Parameters: **unvalidated starting parameters**
- Fixture: `fixtures/synthetic_demo.jsonl` (sha256 `0730f8cd5ab67f76...`, 7418 items)
- Run: `replay-20261024T195818Z-4bad40`, 3455 ticks of 5 min
- First tick with monitor ready: 2026-02-05T01:00:00+00:00
- State transitions: 25 (2.79 per day after warm-up)

## Implementations

- panic: `finbert:ProsusAI/finbert`
- drift: `sentence-transformers:sentence-transformers/all-MiniLM-L6-v2`
- volume: `unique-count-vs-time-of-day-v1`
  - note: FinBERT loaded
  - note: MiniLM loaded

## Labelled scenario windows

Two kinds of latency are reported. *State* latency counts any escalation, including fail-safe ones (warm-up, data gaps). *Evidence* latency counts only ticks where R(t) or the change-point would escalate by itself, with a ready monitor and complete data.

| scenario | kind | state before onset | min to >=CAUTION state | min to HALT state | min to CAUTION evidence | min to HALT evidence | peak R | NORMAL / CAUTION / HALT min | not-ready / degraded min | NORMAL while degraded |
|---|---|---|---|---|---|---|---|---|---|---|
| feed_outage | data_gap | CAUTION | 0.00 | - | - | - | 1.99 | 0 / 185 / 0 | 0 / 135 | 0 |
| policy_surprise | stress | NORMAL | 25.00 | 40.00 | 25.00 | 40.00 | 4.28 | 25 / 15 / 145 | 0 / 0 | 0 |
| bank_stress | stress | NORMAL | 30.00 | 45.00 | 30.00 | 45.00 | 6.77 | 970 / 305 / 770 | 0 / 0 | 0 |

- `feed_outage` window 2026-02-09T02:00:00+00:00 -> 2026-02-09T05:00:00+00:00; label source: fixture metadata
- `policy_surprise` window 2026-02-10T14:00:00+00:00 -> 2026-02-10T17:00:00+00:00; label source: fixture metadata
- `bank_stress` window 2026-02-12T13:30:00+00:00 -> 2026-02-13T23:30:00+00:00; label source: fixture metadata

## Outside labelled windows (after warm-up)

- Elevated episodes (>= CAUTION) not overlapping any labelled window: **5** (of which 1 is the cooling-off from the warm-up HALT)
- State minutes: NORMAL 6870, CAUTION 620, HALT 2995, FLATTEN 0
- Of which monitor not ready (warm-up, e.g. no weekend baseline yet): 2880 min; degraded data: 45 min

  - 2026-02-05T01:00:00+00:00 -> 2026-02-05T04:00:00+00:00 peak HALT (180 min) [warm-up recovery]
  - 2026-02-05T06:40:00+00:00 -> 2026-02-05T07:35:00+00:00 peak CAUTION (55 min)
  - 2026-02-06T06:05:00+00:00 -> 2026-02-06T08:00:00+00:00 peak CAUTION (115 min)
  - 2026-02-09T07:15:00+00:00 -> 2026-02-09T08:30:00+00:00 peak CAUTION (75 min)
  - 2026-02-10T06:50:00+00:00 -> 2026-02-10T08:05:00+00:00 peak CAUTION (75 min)

Note: 'outside labelled windows' episodes are not automatically false alarms on historical data - unlabelled real stress exists - and on synthetic data they reflect the generator's noise.
