# Evaluation

Every number on this page was produced by `vetosentinel evaluate` on 2026-10-24 and copied
from the generated reports in `reports/`. Re-running the commands below reproduces them. The
replay is deterministic given the fixture, the config, and the model implementation. Model
weights are the Hugging Face revisions downloaded that day.

```bash
vetosentinel evaluate --config config/default.toml --fallback-models --out reports/synthetic_fallback
vetosentinel evaluate --config config/default.toml --out reports/synthetic_finbert_minilm
vetosentinel fetch-gdelt-gkg --start 2023-02-27T00:00:00Z --end 2023-03-15T00:00:00Z --every-n 4 \
    --out data/historical/gdelt_gkg_svb_2023.jsonl \
    --event "svb_failure_window=2023-03-08T21:00:00Z..2023-03-14T00:00:00Z"
vetosentinel evaluate --config config/default.toml --fixture data/historical/gdelt_gkg_svb_2023.jsonl \
    --out reports/historical_gdelt_svb_2023
```

Each report records: data origin and disclaimer, parameter status, fixture SHA-256, caveats
and declared gaps, model implementations, warm-up end, transitions per day, per-scenario
state and **evidence** latency, time in each state, not-ready/degraded minutes, and
elevated episodes outside labelled windows. *State latency* counts any escalation, including
fail-safe ones (warm-up, data gaps). *Evidence latency* counts only ticks where R(t) or the
change-point would escalate by itself, with a ready monitor and complete data.

All parameters are **unvalidated starting parameters**. During development they, and two
methodology fixes (drift bias correction, Poisson volume floor), were adjusted against the
synthetic fixture only. The historical replay was run once with the parameters already fixed
and was not used for tuning.

## 1. Synthetic demo fixture (invented data)

> **SYNTHETIC DEMO DATA.** These results only show that the pipeline and state machine behave
> as designed on invented scenarios. They are not evidence of real-world detection ability,
> lead time, or profitability.

`fixtures/synthetic_demo.jsonl`: 7,418 items over 12 days, with a diurnal volume pattern, a
3-hour feed outage, a "policy surprise" burst, and a "bank stress" episode. The first 3 days
are warm-up.

| run | scenario | min to CAUTION evidence | min to HALT evidence | peak R | outage held at |
|---|---|---|---|---|---|
| fallback heuristics | policy_surprise | 10 | 25 | 6.16 | — |
| fallback heuristics | bank_stress | 10 | 25 | 7.28 | — |
| fallback heuristics | feed_outage | 45 | — | 1.84 | CAUTION (0 min NORMAL) |
| FinBERT + MiniLM | policy_surprise | 25 | 40 | 4.28 | — |
| FinBERT + MiniLM | bank_stress | 30 | 45 | 6.77 | — |
| FinBERT + MiniLM | feed_outage | — | — | 1.99 | CAUTION (0 min NORMAL) |

After warm-up and outside the labelled windows, elevated episodes not caused by warm-up were 1
(fallback) and 4 (FinBERT + MiniLM). They were all CAUTION, lasting 45–115 min.
Transitions per day after warm-up: 1.90 (fallback) and 2.79 (models). Both runs also spend
2,880 min NOT READY → HALT, because the first weekend has no weekend baseline. That is the
designed fail-safe, not a detection.

## 2. Historical replay: GDELT GKG around the March 2023 SVB failure

Data: 49,820 finance-themed page titles from GDELT GKG 2.1 raw files, 2023-02-27 to
2023-03-15. One 15-minute file per hour was used. One file (2023-03-09T00:00) returned 404
and is declared as a data gap. The fixture is committed (`data/historical/gdelt_gkg_svb_2023.jsonl`,
14 MB) so the replay runs offline. It is derived from GDELT data, so GDELT's terms of use apply,
and the command above rebuilds it.

**Event label.** The window 2023-03-08T21:00Z → 2023-03-14T00:00Z was chosen by the
implementer from public knowledge of the timeline: SVB's loss and capital-raise announcement
after the US close on 8 Mar, FDIC receivership on 10 Mar, and the Signature Bank closure plus
the joint Treasury/Fed/FDIC statement on 12 Mar. It is approximate and should be verified
independently.

Result (FinBERT + MiniLM, default parameters):

| metric | value |
|---|---|
| state just before onset | NORMAL |
| minutes to first ≥CAUTION *state* | 180, **caused by the missing GDELT file (data gap → fail-safe)**, not by news |
| minutes to first CAUTION *evidence* | 570 (2023-03-09 06:30Z; panic z 4.3 + CUSUM alarm; brief) |
| minutes to first HALT *evidence* | 5,900 (2023-03-12 23:20Z; volume z 3.3, drift z 2.7, CUSUM alarm) |
| peak R in window | 2.35 |
| time in window: NORMAL / CAUTION / HALT | 5,045 / 655 / 1,685 min (1,555 min of HALT is NOT READY: the gap, plus Saturday 11 Mar with no weekend baseline yet) |

**Interpretation.** On this replay the monitor did **not** give a strong, timely
news-driven signal for the core of the SVB episode:

- During US trading hours on 9 Mar (14:30–21:00Z), when SVB shares collapsed, all 78 ticks were NORMAL
  and the maximum R was 0.27.
- On 10 Mar, the day of the FDIC receivership, the median panic z was 1.4. R stayed below the CAUTION
  threshold until a CUSUM alarm produced a CAUTION at 23:15Z.
- Drift became persistently elevated (z 2–5) on 12–13 Mar, the policy-response weekend. The only
  evidence-based HALT came on Sunday evening, 12 Mar.
- Several elevated episodes before onset (2 Mar, 3 Mar, 8 Mar morning UTC) are unlabelled. They
  are not automatically false alarms, and they were not investigated.

The most likely reason is dilution. The features average over every finance-themed headline
GDELT captures worldwide (around 130 per hour in this sample), so an episode concentrated on a
few institutions moves the aggregate mean little. Coarse hourly batch timestamps add to this.
Possible next steps include entity- or sector-filtered panic, cluster-level drift, and
shorter windows. They must be validated on **other** events before any claim is made, and this
event must not be used to tune them.

The fail-safe behaviour did work as designed. The missing data file and the warm-up weekend
both produced HALT, never NORMAL.

### 2b. The same replay with "Optimize for: Banks & credit"

This tests the dilution explanation, not a tuned model. The focus is the dashboard's
*Banks & credit* preset ("banks, bank run, deposits, credit markets, loan defaults, bank
capital"), written for the UI before this replay was run. The relevance settings (z_mid 3,
z_soft 0.5, other news 0.05) were set from one day of 2026 live headlines. Nothing was adjusted
after seeing the result. Report: `reports/historical_gdelt_svb_2023_focus_banks/`. Rerunning
whole-market with the current code reproduced section 2 exactly.

| metric | whole market | Banks & credit |
|---|---|---|
| minutes to first CAUTION *evidence* | 570 (9 Mar 06:30Z) | 1,905 (10 Mar 04:45Z; panic z 3.0 + CUSUM) |
| minutes to first HALT *evidence* | 5,900 (Sun 12 Mar 23:20Z) | **2,185 (Fri 10 Mar 09:25Z**; panic z 2.6, drift z 2.7, CUSUM) |
| peak R in window | 2.35 | **3.78** |
| max R, 9 Mar US hours (share collapse) | 0.27 | 0.48 |
| max R, 10 Mar 00:00–16:00Z | 1.43 | 2.11 |
| max R / HALT min (ready), 10 Mar 16:00Z – 12 Mar 22:00Z | 1.85 / 30 | 3.78 / 330 |
| elevated episodes outside the window | 6 | 4 |
| HALT min outside the window, monitor ready (2–8 Mar) | 175 | 30 |

**Reading.** Focusing on banks made the signal markedly stronger and earlier where it
mattered most. The first score-driven HALT moved from Sunday night to Friday morning (UTC),
hours before SVB was closed that day, and peak R rose by 60%. It also produced fewer alarms in
the week before. It did **not** fix everything: there is still no signal during the 9 Mar share
collapse itself, and the brief early CAUTION of 9 Mar 06:30Z (whole market) disappears. This is
one event with an implementer-chosen label. It supports dilution as the main cause, but it is
not a validation, and other events are needed before claiming a benefit.

## 3. What has *not* been evaluated

- Any second historical event, a calibration of false-alarm rates, or comparison with market
  data. Market prices are deliberately not inputs, and no price overlay was built.
- Live operation over days. A live process warms up for several days (see LIMITATIONS.md).
- Any effect on a trading strategy's P&L or risk. Veto Sentinel makes no such claim.
