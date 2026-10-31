# Infrastructure final check (2026-10-19, third pass)

Reviewer: validator. This checks the items left open in `reviews/2026-10-19_infrastructure_rereview.md`.
Full suite: **110 passed**. I re-read the changed code and ran new throwaway probes from my scratch area. Nothing in `src/` or
`tests/` was edited.

## Verdict

| Use | Verdict | In one line |
|---|---|---|
| **(a) Archive top-of-book backtests** (`quote_trade`, trades + bookTicker 2023-05..2024-03) | **FIT** (with documented limits) | No open problems in the machinery. What remains are known, reported modelling limits. |
| **(b) L2 backtests of live recordings** (`l2`) | **FIT WITH CAVEATS** | The machinery is fixed. But the only L2 config still overrides order latency to 20 ms, and the gap path has never seen a real recorded gap. |
| **(c) Paper trading** | **FIT WITH CAVEATS** | Safe, realistic latency, correct event order. Gap handling is right in code but has never been exercised on a real disconnect (0 drops in every paper run so far). |

**No blockers remain in the code.** Before the first real strategy is signed off:
1. Fix `config/backtest/demo_l2_live.yaml` (F6) if it is used as a template.
2. Commit the code to git (M5).

"Fit" means fit to *judge* a strategy honestly. It does not mean the results will hold live. The bookTicker archive is a
2.5-year-old market and the L2 history is only minutes long. Those are limits of the data, not bugs.

[CONFIRMED] = reproduced with a probe or measured. [SUSPICION] = from reading the code only.

---

## Item by item

### R1. Gap handling by stream: **FIXED** [CONFIRMED]
*Code:* `exchange.py:99-115`; `config.py:96-99` (`gap_trade_streams: ["market"]`); `strategy.py:261-267`; `paper/run.py:60-69`.

*Do the stream names match what is emitted?* Yes.
* `live.py:187-195` emits `GapEvent(stream=endpoint)`, where the endpoint names come from `config/data.yaml:36-41`:
  `public` (depth + bookTicker) and `market` (aggTrade).
* `live.py:229` and `255` emit `stream="depth"` for resync and session start.
* The recorder persists these unchanged, and replay synthesises `"depth"` markers.
* The paper parser reads `"<endpoint>: ..."` from the `StatusEvent` detail, the same format as `live.py:161` and `165`.

*Probe:* same L2 scenario as before, one run per stream value.

| stream | resting order | new order | book asks after |
|---|---|---|---|
| `market` | cancelled | **accepted** | **kept** {100.01, 100.02} |
| `public` / `depth` / `None` | cancelled | rejected `awaiting_resync` | wiped (waits for snapshot) |

Paper parser: `"market: ConnectionClosedError..."` gives `market`, `"public: OSError..."` gives `public`, `error` with `market:` gives
`market`, `book_gap` gives `depth`, and an empty detail gives `None` (handled conservatively). Tests:
`test_backtest_fixes.py:348`, `400`, `412-417`.

*Small note (low):* the endpoint names now live in two configs, `config/data.yaml` (endpoint keys) and
`exchange.gap_trade_streams`, plus the literal `("public", "market")` in `paper/run.py:69`. If someone renames an endpoint,
trade-socket gaps silently fall back to the conservative "wipe the book" path. That direction is safe, but the behaviour would
return to R1. Suggest one source of truth. Owner: backtester.

### R2. Cross-fill refill: **PARTLY FIXED** [CONFIRMED]
*Code:* `fills.py:314-340`. Per side it now remembers (level, last displayed size, size still available to us).

*Probes* (2.0 ETH bid, 10 crossed quotes, no trades):
* Same price level, displayed size flickering 0.5 / 0.4: fills total **0.9 ETH**, previously 2.0. This is correct by design: only
  *increases* in displayed size count as new liquidity.
* Ask flipping between **two levels** (99.99 and 99.98, 0.5 ETH each time): still **2.0 ETH**, because each level change starts a
  "new episode" (`fills.py:326`).

Is that acceptable? A move to a *deeper* crossing price does mean new sellers at a lower price, so a refill there is defensible.
A *flip back* to a level seen moments ago is the same liquidity counted twice. In the new smoke run, cross fills are 344.8 of
1,062.1 ETH filled, but at 0.05 ETH per order the flip case rarely matters. **Not a blocker.** It must be fixed before testing
quote sizes of a few ETH or more. Fix: keep the per-level memory for the whole episode (a small dict keyed by price, cleared when
crossing stops), not just for the latest level. Owner: backtester.

### R3. Feed delay from an empirical table: **FIXED, acceptable** [CONFIRMED]
*Code:* `engine.py:331-344`, `config.py:54-63`. One delay is drawn per 2 s window from the measured quantiles, and event order is kept.

*Probe on 2M archive quotes* (delay added after publish time E):

| | p50 | p90 | p99 | max |
|---|---|---|---|---|
| table (measured) | 239 | 296 | 772 | 1500 (assumed ceiling) |
| simulated | **240** | **373** | **930** | 1490 |

*Is a p90 about 26% high acceptable?* **Yes.**
* The median matches exactly.
* The overshoot is in the *pessimistic* direction: slower data means more pick-offs and more lost cancel races.
* It comes from a physical effect: after a slow window, the backlog drains in order, as on a real websocket.
* The heavy tail that mattered (lag spikes) is now present, and a `latency_spikes` variant (p99 1.8 s) exists.

Caveats to keep in mind:
1. Within a 2 s window every message gets the same delay, so there is no per-message jitter.
2. The 0.0 and 1.0 end points of the table are guesses.
3. The table comes from about 2 minutes of recording on one evening. Re-measure with `python -m mm.data.latency` after a few days
   of recording and at different times of day.

Order latency is still a fixed 132 ms plus uniform jitter. It cannot be measured without an account.

### R4. Recorder file naming and replay order: **FIXED** [CONFIRMED]
* New files are named `part-<first exchange ts_ns>-<n>` (`record.py:87-91`). The probe gave
  `2026-10-19/part-1792454397000000000-0001` and `2026-10-20/part-1792454403000000000-0001`.
* Loaders sort live files by the smallest `ts_ns` inside each file, read cheaply from parquet statistics (`loaders.py:156-177`).
* I rebuilt the old failure: a PC-named `part-000000` file holding the day's last 5 s, next to `part-235000`. Strict replay now
  succeeds with 0 backwards steps. Test: `test_data_gaps_latency.py:321-350`.

### Late resync markers: **FIXED** for new recordings, **limitation remains** for old ones [CONFIRMED]
* `live.py:223-230` emits `GapEvent("resync", "depth")` stamped with the exchange time of the diff that revealed the gap. The
  recorder persists it.
* Replay lets each snapshot "consume" earlier persisted markers (`loaders.py:348-377`).
* Tests: `test_data_gaps_latency.py:280-314`.
* Recordings made before this fix have no stored detection time, so their synthesised markers still sit at the snapshot time
  (documented at `loaders.py:354-358`).
* Cosmetic: in the current live data, the 2nd and 3rd old *sessions* are labelled "resync" rather than "session_start"
  (5 markers: session_start, resync, resync, session_start, session_start). Behaviour is identical.

### Minor items
* **Paper heartbeat:** **fixed.** `paper/run.py:146-151` no longer adds hold plus feed delay. `engine.advance` now uses
  `inclusive=False` (`engine.py:358-366`), so the same-millisecond tie rule also holds in paper.
* **Manifest covers depth and gap files:** **fixed.** An L2 replay with the current code lists 17 files across `depth`, `gaps`,
  `quotes` and `trades`.
* **YAML comments:** **fixed** in `demo_smoke.yaml:30-31` and `demo_paper.yaml:25`. Not fixed in `demo_l2_live.yaml` (see F6).

### M5. Code provenance: **FIXED in substance, one gap** [CONFIRMED]
* `config.json` → `meta.code` holds the SHA-256 of all 18 source modules that ran, plus a combined hash. I re-hashed the current
  files: **0 mismatches** against the regenerated smoke report, so the report matches today's code.
* Remaining gap: a hash proves *which* code ran but cannot *recover* it. `src/mm/backtest`, `src/mm/paper`,
  `src/mm/data/latency.py` and the new tests are **still untracked in git** (the commit is still `1b68ff0`, dirty). Commit them
  before the first strategy run you want to be able to reproduce. Owner: the owner (or whoever manages git).

### `no_cross_fills` required row, per-mechanism columns: **FIXED, can be bypassed** [CONFIRMED]
* The sensitivity table now shows filled quantity and per-mechanism quantity / spread capture. There is a warning when net PnL is
  better *with* cross fills (`report.py:169-171`). Config validation refuses a variant list without `no_cross_fills`
  (`config.py:178-182`).
* New smoke run, in-sample: base -1,138.08, `no_cross_fills` -1,089.40. Cross fills are 344.8 ETH, with only +2.69 of the +26.14
  spread capture. The conclusion from the re-review stands: they are adverse fills and do not flatter PnL.
* Bypass: the check only runs when `sensitivity.enabled` is true. `--no-sensitivity` or `enabled: false` (as in
  `demo_l2_live.yaml`) produces a report without the row. Suggest that the summary states plainly "SENSITIVITY NOT RUN: not valid
  for sign-off". Until then, **validator rule: no strategy report is accepted without the `no_cross_fills` row.** Owner: backtester.

---

## New findings in this pass

### F6. The only L2 config still sets order latency to 20 ms [CONFIRMED, medium for use (b)]
`config/backtest/demo_l2_live.yaml:10` sets `latency: {feed_latency_ms: 10, order_latency_ms: 20, ...}`. The resolved config is:
* feed delay from the measured table, because `feed_latency_ms` is ignored when the table is set. That is confusing but harmless.
* **order latency 20 ms**, the colocated value, not the measured 132 ms.
* report latency 240 ms.

Orders reach the simulated exchange about 6x faster than they could from this PC, so cancels win races they would lose.
The test `test_backtest_fixes.py` (`test_demo_configs_use_measured_not_zero_latency`) only checks the smoke and paper configs,
so it passes without covering this file.

The `2026-10-19_demo_quoter_l2_live_smoke` report is also stale: it was produced at 15:49 with the original code and should be
regenerated.

Fix: delete the `latency:` overrides (keep `timer_interval_ms`), enable sensitivity, add this config to that test, then re-run.
Owner: backtester.

### F7. The gap path has never met a real gap [CONFIRMED, declared]
* The live recordings contain only session-start snapshots.
* Every paper run reports "0 feed drops/resyncs".
* Everything above is proven on synthetic events.

Before trusting paper results over a long session, force one disconnect of each socket during a paper run. For example, block
`fstream.binance.com` in the firewall for about 5 s, or temporarily point one endpoint at a bad URL. Then check the status lines,
the cancellations and that quoting resumes. Owner: backtester, with the owner's help to cut the network.

---

## Still-open items from earlier reviews (non-blocking)

| Item | Status |
|---|---|
| L5: no bootstrap / daily PnL statistics (a t-statistic exists) | not fixed (declared) |
| L9: inside-spread orders start first in line | not fixed (declared); revisit for strategies that quote inside wide spreads |
| R2 level-flip refill | partly fixed (see above) |
| Endpoint names in three places (R1 note) | low |
| Summary text `3b` still says "a crossed quote can fill again on each later crossed quote" | low; now only true across level changes, update the wording |

## What each use still needs

* **(a) Archive top-of-book:** nothing in the code. Read reports with the base row *and* the `no_cross_fills`, `latency_spikes`
  and `pessimistic_all` rows. Fix R2 fully before testing large quote sizes.
* **(b) L2 on live recordings:** fix F6. Record far more data (days, not minutes). Re-measure the latency table. Re-run the L2 smoke.
* **(c) Paper trading:** do the forced-disconnect exercise (F7). It already sends nothing to Binance except public market-data
  requests (re-confirmed: no order endpoints, keys or signing in `src/`).

Research and education only. Backtest results do not promise live performance.
