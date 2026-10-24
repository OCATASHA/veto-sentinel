# Veto Sentinel replay evaluation

> **Data origin: historical_dataset**
>
> HISTORICAL REPLAY: results depend on the dataset's coverage and timestamps. Parameters are unvalidated starting parameters; they must not be tuned on the event windows reported here.

- Parameters: **unvalidated starting parameters**
- Optimize for: **banks, bank run, deposits, credit markets, loan defaults, bank capital** (other news weight 0.05)
- Fixture: `data/historical/gdelt_gkg_svb_2023.jsonl` (sha256 `8672349bf4414a46...`, 49820 items)
- Fixture caveat: timestamps are GDELT 15-minute capture-batch times, not publication times
- Fixture caveat: sampled one 15-minute file in every 4
- Fixture caveat: theme filter: ['ECON_STOCKMARKET', 'ECON_WORLDCURRENCIES', 'ECON_INFLATION', 'ECON_INTEREST_RATE', 'ECON_BANKRUPTCY', 'ECON_DEBT', 'ECON_CENTRALBANK', 'EPU_CATS_MONETARY_POLICY', 'EPU_POLICY_INTEREST_RATES', 'WB_318_FINANCIAL_ARCHITECTURE_AND_BANKING', 'WB_332_CAPITAL_MARKETS', 'WB_1235_CENTRAL_BANKS', 'WB_442_INFLATION']
- Fixture caveat: language not filtered by GDELT; 'und' recorded
- Fixture caveat: no per-file cap
- Declared data gap: 2023-03-09T00:00:00+00:00 -> 2023-03-09T01:00:00+00:00 (HTTP 404 for 20230309000000.gkg.csv.zip)
- Run: `replay-20261024T222945Z-719a5f`, 4596 ticks of 5 min
- First tick with monitor ready: 2023-03-02T01:00:00+00:00
- State transitions: 35 (2.71 per day after warm-up)

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
| svb_failure_window | stress | NORMAL | 180.00 | 185.00 | 1905.00 | 2185.00 | 3.78 | 4130 / 1185 / 2070 | 1555 / 5 | 0 |

- `svb_failure_window` window 2023-03-08T21:00:00+00:00 -> 2023-03-14T00:00:00+00:00; label source: chosen by the implementer from public knowledge of the event timeline (SVB loss/capital-raise announcement after the US close on 2023-03-08; FDIC receivership 2023-03-10; Signature Bank closure and joint Treasury/Fed/FDIC statement 2023-03-12). Approximate - verify independently.

## Outside labelled windows (after warm-up)

- Elevated episodes (>= CAUTION) not overlapping any labelled window: **4** (of which 1 is the cooling-off from the warm-up HALT)
- State minutes: NORMAL 8020, CAUTION 260, HALT 2940, FLATTEN 0
- Of which monitor not ready (warm-up, e.g. no weekend baseline yet): 2880 min; degraded data: 0 min

  - 2023-03-02T01:00:00+00:00 -> 2023-03-02T02:05:00+00:00 peak HALT (65 min) [warm-up recovery]
  - 2023-03-02T04:45:00+00:00 -> 2023-03-02T06:20:00+00:00 peak CAUTION (95 min)
  - 2023-03-02T09:45:00+00:00 -> 2023-03-02T11:20:00+00:00 peak CAUTION (95 min)
  - 2023-03-04T00:00:00+00:00 -> 2023-03-06T01:05:00+00:00 peak HALT (2945 min)

Note: 'outside labelled windows' episodes are not automatically false alarms on historical data - unlabelled real stress exists - and on synthetic data they reflect the generator's noise.
