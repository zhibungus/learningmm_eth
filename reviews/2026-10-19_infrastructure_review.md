# Infrastructure review: data layer, backtester, paper trader (2026-10-19)

Reviewer: validator. Scope: `src/mm/data`, `src/mm/backtest`, `src/mm/paper`, the three smoke reports in `reports/`.
No strategy exists yet, so this review checks the machinery every future strategy will be judged by. Demo-quoter PnL is ignored.

## Verdict: FIT WITH CAVEATS. Do not sign off any strategy until H1-H4 are fixed

The parts that are hardest to get right are done well:

* deterministic event ordering
* a correct Binance order book synchronisation
* exact accounting
* seeded, bit-identical reruns
* a paper trader that cannot send orders

The **default assumptions** are a different matter. Latency, delivery timing and how much size a fill gets
all lean optimistic, and the built-in "pessimistic" sensitivity run does not test the main fill mechanism.
With today's settings, a market-making strategy would look better than it would be live.

Until H1-H4 are fixed, read the `higher_latency` / `pessimistic_all` rows of a report as the base case, not the
`base` row.

What was run: `pytest` (52 passed). Throwaway probes are in my scratch area. None of `src/` or `tests/` was edited.
I also took measurements on the archive data, the live recordings and the smoke-report fills, plus 6 public
`GET /fapi/v1/time` calls to measure network round-trip time.

Each finding is marked **[CONFIRMED]** (reproduced or measured) or **[SUSPICION]** (from reading the code, not demonstrated).

---

## HIGH

### H1. Latency defaults are about 10x too fast for this PC [CONFIRMED]
*Where:* `src/mm/backtest/config.py:44-46` (feed 10 ms, order 20 ms, report = feed), `config/backtest/demo_smoke.yaml:30-33`,
`config/paper/demo_paper.yaml:22-27` (feed **0**, report **0**).

*Measured:*
* Live recordings: `recv_ts_ns - E` has a median of about 5,382 ms. The recorder logged a clock offset of 5,131-5,146 ms, and I
  measured 5,207-5,211 ms today, so the PC clock drifts. That leaves a **one-way feed delay of roughly 170-250 ms**, never below about 130 ms.
* Warm REST round trip to `fapi.binance.com` is about **222 ms**, so one-way about 110 ms. A signed order request would cost at least that,
  plus gateway time.

*Plain language:* the backtest assumes you see the market 10 ms after it happens and your order lands 20 ms later.
From this PC you actually see it about 0.2 s later and your order lands about 0.1-0.2 s after that. A market maker that is
0.3 s slow gets picked off much more often, and its cancels lose more races. The smoke report already shows that
latency changes fill count by about 18% (`higher_latency` row), even for a demo that only requotes once a second.

*Fix (backtester):*
* Make the base defaults match measurements, for example feed 150-250 ms and order 100-200 ms with jitter.
* Keep a separate "colocated" variant for comparison.
* Add a small tool that turns the recorder's `recv_ts_ns`, `E` and `clock_offset_ms` into a latency distribution.
  The data-engineer should own that tool.
* Re-measure the clock offset per session, because it drifts.

### H2. The strategy can see archive quotes before Binance published them [CONFIRMED, lookahead]
*Where:* `src/mm/backtest/engine.py:259-265`. Delivery time is `ts_ns + feed_latency`, and `ts_ns` is the match time `T`, not the
publish time `E`. `src/mm/backtest/README.md:113-114` says "the feed has hidden delay", but the engine never adds that delay.

*Measured on archive bookTicker:* `E - T` median is 7 ms (2024-03-29) and 6 ms (2024-03-30). p99 is 17-19 ms and the max is about 3 s.
**7.9% / 5.9% of quotes have `E - T > 10 ms`.** With the 10 ms default, the strategy acts on those quotes before Binance even
sent them. On a median quote, the simulated network is only about 3-4 ms.

*Plain language:* `T` is when the trade happened inside Binance. `E` is when Binance sent the message out. You can only
receive it after `E`. The engine counts from `T`, so it hands out news early.

*Fix (backtester):* deliver at `max(T, E) + feed_latency` when `event_ts_ns` exists. Archive trades have no `E`, so use `T` plus a
calibrated extra delay; live aggTrade `E - T` is about 1 ms. Keep exchange-side processing on `T`, which is already correct.
Add a test with `event_ts_ns` set.

### H3. The paper trader runs with zero feed and report latency on exchange time [CONFIRMED]
*Where:* `config/paper/demo_paper.yaml:23-25`, which says "real network delay already exists on the feed, so extra feed latency is 0".
Also `src/mm/paper/run.py:121-123` and the module docstring.

*Why it is wrong:* the engine re-clocks every event to its exchange `ts_ns` (`engine.py:242`). So the real network delay is
thrown away, and the simulated strategy reacts at the exact exchange instant. Paper results are therefore *more*
optimistic than a 10 ms backtest, not more realistic. The paper report confirms "market data 0 ms, reports 0 ms".

*Fix (backtester):* either drive the paper engine from `recv_ts_ns - clock_offset`, which is the true arrival time, or keep exchange
time and set feed/report latency to the measured values from H1. Also correct the YAML comment.

### H4. Trade-through and quote-cross fills give the full order size, with no volume cap [CONFIRMED]
*Where:* `src/mm/backtest/fills.py:229-230` (through means fill everything remaining), `fills.py:243-251` with `298-300` (a quote crossing
our price means fill everything, quote model), and `fills.py:380-382` (L2 model).
The test `tests/test_backtest_fills.py:112-116` locks this behaviour in.

*Probes:*
* A 0.001 ETH print one tick through a 10 ETH bid fills **10 ETH**.
* A quote whose ask drops onto our bid with 0.001 ETH displayed, and no trade at all, fills **10 ETH**.

*Measured in the smoke report* (`reports/2026-10-19_demo_quoter_smoke/fills.parquet`), by joining each fill to the same-millisecond
aggTrades:

| period | fills | trade-through | quote-cross, no print in that ms | at-price (queue) |
|---|---|---|---|---|
| in_sample | 27,984 | 21,054 (75%) | 6,207 (22%) | 723 (3%) |
| out_of_sample | 18,917 | 14,571 (77%) | 3,877 (20%) | 469 (2%) |

In 23.6% / 22.8% of trade-through fills, the through volume printed in that millisecond was smaller than our 0.05 ETH order.
About **13% of all trade-through quantity has no matching volume** (137 of 1,045 ETH in-sample). At realistic quote sizes this
share grows fast.

*Plain language:* "price traded through my bid, so I must have been filled" is true. "So I was filled for my *whole*
size" is not. You only get as much as the seller still had left after the queue in front of you.

*Direction of bias:* for the demo these fills are adverse (mean 10 s move about -1 bp for every mechanism), so they *hurt* its PnL.
For a real strategy they inflate volume, fill ratio and any maker rebate (rebate times phantom volume equals phantom PnL). They also
flatter mean-reverting strategies that profit from being filled on overshoots.

**None of the sensitivity variants touch this mechanism.** `pessimistic_fills` changed the fill count by only 2% (27,984 to 27,412),
because trade-through fills, which are 75% of fills, are treated the same in every variant.

*Fix (backtester):*
* Cap a trade-through fill at the aggressor volume printed at or through our price in that millisecond, after the queue ahead
  (at-price volume minus queue ahead, plus through volume).
* Cap a quote-cross fill at the displayed opposite quantity.
* Record the fill mechanism (`through` / `at_price` / `cross` / `taker`) on every `Fill` and in `fills.parquet`, so reports show the mix.
* Add a sensitivity variant that turns these caps on.

---

## MEDIUM

### M1. The same-millisecond tie rule is not what the docstring says [CONFIRMED]
*Where:* the docstring at `src/mm/backtest/engine.py:22-23` says "the exchange sees the market event BEFORE an order that arrives at that instant".
The code at `engine.py:245` and `266` processes an order arriving at time X *after the first* market event at X but *before the
remaining* events at X.

*Probe:* an order arriving at 30.000 ms, with two through-prints at 30 ms. The first print misses it and the second print fills it
(the fill shows in `eng.rec.fills`).

*Why it matters:* without jitter, every arrival lands exactly on a millisecond boundary (decision `T + 10` ms, arrival `+ 20` ms).
**49.2% of archive events share their millisecond with another event**, and 64,251 trade-milliseconds hold more than one aggTrade
(2024-03-30). So whether an order sees a sweep depends on how many prints happened to share the millisecond. That is arbitrary, and it
contradicts the stated conservative rule.

*Fix (backtester):* group all market events with the same `ts_ns` and process all of them before any order arrival at that `ts_ns`
(or apply a documented, configurable rule). Add a regression test with two same-millisecond prints.

### M2. Paper trading processes events in arrival order across two websockets, which double-counts queue depletion [CONFIRMED]
*Where:* `src/mm/paper/run.py:113-123`, `engine.py:242` (clamp), `fills.py:254-276` (`_observe`).

*Measured in the live recordings:* **20.8% of trades arrived after the depth message that already reflects them.** Separately,
8,338 of 53,739 events (15.5%) arrived with an exchange time earlier than an event already received, and get clamped.

*Probe (L2 model):* queue of 10 ahead of us, then a 4 ETH sell at our price.
* Replay order (trade, then depth): `q_ahead = 6.0`, which is correct.
* Paper order (depth, then trade): `q_ahead = 2.0`. The depth drop is counted as a cancellation and then the trade is subtracted again.

*Plain language:* paper trading moves you up the queue too fast, so the same data gives more fills in paper than in replay.

*Fix (backtester):* add a small reorder buffer in the paper trader. Hold events for about 150-300 ms, then release them sorted by
`(ts_ns, kind priority)`, the same rule replay uses. Add a test.

### M3. Feed gaps: weak handling in paper, no handling at all in replay of live data [CONFIRMED by code reading]
*Paper:* `paper/run.py:116-118` cancels resting orders on `disconnected` / `book_gap` / `error`. But:
* nothing stops the strategy from quoting again right away. Report latency is 0, so it hears "canceled" instantly.
* the simulated exchange keeps the **stale** L2 book until the new snapshot arrives (`exchange.py:79-81`; `fills.py:367-373`
  never clears `mkt.book`).

So new orders get queue positions from a book that is known to be wrong.

*Replay:* status events are only written to `status.jsonl` (`record.py:123-126`), stamped with the PC clock, which is about 5 s off.
`loaders.replay` has no status stream, so a backtest on live recordings runs straight through gaps on a stale book.
The current recordings show 0 gaps, but long recordings will have some.

*Fix:*
* Data-engineer: store gap and resync markers, in exchange time, as a replayable stream.
* Backtester: call `on_book_lost` on those markers, and reject new orders with `no_market_data` until a snapshot arrives,
  in both paper and replay.

### M4. Several of our own orders at one price: the displayed-size cap erases our own queue [CONFIRMED]
*Where:* `fills.py:261` and `274` (`q_ahead` capped at the public displayed quantity, which never includes our simulated orders) together
with `exchange.py:197-199` (`own_ahead`).

*Probe:* two orders of ours (5 ETH, then 1 ETH) at a bid with 1.0 ETH displayed. The second order's queue collapses from 6.0 to 1.0.
A 2.0 ETH sell print then fills us for **2.0 ETH**, but at most 1.0 ETH was possible.

*Fix (backtester):* cap only the public part (`min(public_ahead, displayed) + own_ahead`). Matters for layered quoting.

### M5. Reports cannot be reproduced from their folder: no data manifest, no code version [CONFIRMED]
*Where:* `src/mm/backtest/report.py:161-170`.

*Evidence:* re-running `config/backtest/demo_l2_live.yaml` now gives net +2.69 and 65 fills. The report says -2.94 and 61 fills.
The cause is a third live session recorded at 14:55 UTC, after the report was written at 14:49 UTC. If I stop at the report's
`last_ts_ns`, it reproduces **exactly** (47,709 events, -2.9382, 61 fills), so the engine is deterministic. Reruns are bit-identical,
and the seed is recorded in `metrics.json` and `config.json`. The gap is provenance: which files went in. Also, `src/mm/backtest/`
and `src/mm/paper/` are untracked in git, so there is no commit hash to record.

*Fix:*
* Backtester: write the input file list with row counts and SHA-256, plus the git commit and a dirty flag, into `config.json`.
* Allow periods with times, not just whole dates.
* Owner: commit the backtest and paper code.

### M6. Taker fills are crude [CONFIRMED by code reading]
*Where:* `fills.py:336-355`. With top-of-book only, any excess beyond the touch fills just 1 tick worse.
`fills.py:400-420`: in the L2 model, the liquidity a taker order uses is never removed, so the next taker order can use it again.

Optimistic for strategies that hedge or flatten with market orders, especially in fast markets.

*Fix (backtester):* on quotes data, refuse to fill beyond the touch, or charge a penalty that grows with size. In L2, remember consumed
size until the next depth update.

### M7. No hard inventory or risk limit in the engine [CONFIRMED]
*Where:* `exchange.py:16` says "position limits not modelled". The only limit is the strategy's own check against its *delayed* position.

In a flash move, all resting orders on one side fill at once, and nothing stops a buggy strategy.

*Fix:* backtester adds an exchange-side `max_position` / `max_order_notional` reject (from config) and reports breaches.
Quant-developer keeps strategy-side limits on top.

---

## LOW

* **L1. Markouts can look up to 10 ms into the future [CONFIRMED, reporting only].** The mid series keeps the *last* mid of each 10 ms slot
  but labels it with the slot *start* (`metrics.py:203-212`). The lookup takes "latest at or before target" (`metrics.py:256`).
  The probe returned a markout of 100 where the true value was 0. Markouts are only computed in `compute_metrics` after the run and
  never reach the strategy (confirmed). Fix: label each slot with the timestamp of its last update. Owner: backtester.
* **L2. `Fill.ref_mid` gives the strategy the simulator's true mid** (`strategy.py:132`, `exchange.py:246-247`). Binance sends no such
  field. If `report_latency_ms < feed_latency_ms`, this is a leak. Fix: strip it from the strategy-facing fill. Owner: backtester.
* **L3. Zero latency leaks same-millisecond information [CONFIRMED].** With feed = order = 0, a strategy reacting to print #1 is filled by
  print #2 in the same millisecond. The tests and the paper config use 0. Fix: `validate()` should require at least 1 ms each, unless an
  explicit test-only flag is set. Owner: backtester.
* **L4. Funding is correct in sign and timing.** Position at settlement times rate times mark; the long pays when rate > 0; funding is
  processed before trades at the same `ts_ns`. Small notes:
  * the 2024-03 archive has no mark, so the true mid is used (`metrics.py:132`). The effect is negligible.
  * REST funding timestamps are 1-5 ms after the hour. That is fine.
  * the strategy's `state.cash` never sees funding (`engine.py:253-254` versus `_on_report`).
  * a partial current-month funding file ends silently.

  Owners: backtester (state cash), data-engineer (flag partial months).
* **L5. The Sharpe ratio is computed on absolute PnL**, per 60 s, annualised by sqrt(525,960) (`metrics.py:338-345`). One-day runs give
  values around ±900, which are meaningless. Gaps get forward-filled. Use it only to compare runs. Fix: also report daily PnL and a
  bootstrap or t-statistic. Owner: backtester.
* **L6. The recorder picks date folders by PC clock**, which is about 5.1-5.2 s fast (`record.py:72`, `153-156`). The last ~5 s before
  midnight UTC land in the next day's folder, and the window filter (`loaders.py:129`) then drops them from single-day replays.
  Fix: partition by exchange `ts_ns`. Owner: data-engineer.
* **L7. Magic numbers in code:**
  * report horizons hard-coded to "1s/10s/60s" and the headline to "10s" (`report.py:33`, `125`)
  * `open_timeout=15`, `ping_interval=20` and the 60 s backoff reset (`live.py:148`, `232`)
  * 11 histogram bins (`metrics.py:356`)
  * `replay.chunk_rows` in `config/data.yaml` is never used

  Owners: backtester and data-engineer.
* **L8.** `_pending_replace` is not cleared when an order fully fills (`engine.py:191-203`). If a client id is reused, a stale replace
  could change the price recorded on a later "replaced" update. [SUSPICION] Owner: backtester.
* **L9.** Orders inside the spread start first in line (`fills.py:310-311`, `331-332`). This is reasonable at arrival time given the data,
  but together with H2 and M1 it is slightly generous. Recheck once those are fixed.
* **L10.** Final inventory is marked at mid with no exit cost. This is documented. Consider an optional "liquidate at end" variant.

### Not a real problem: aggTrades merging
An aggTrade sums the fills of one taker order at one price. Queue depletion uses *volume*, and aggTrade volume equals raw-trade volume
to a relative 1.4e-7 (`data/processed/README.md`). So merging does not inflate fills. README bias #3 overstates this risk. The real
size problem is H4.

---

## Lookahead checklist (item 1 of the brief)

* Replay order (`loaders.py:258-273`): `(ts_ns, kind priority, file order)` with funding < trade < depth < quote. Deterministic, and
  a trade comes before the book update it caused. **OK.**
* The strategy only sees delayed events through `_DELIVER` (`engine.py:136-140`). **OK, except H2** (counts from `T`, not `E`) **and L3** (zero latency).
* Fill decisions for orders sent in a millisecond: **M1**. An arrival is inserted part-way through that millisecond's events.
* Markouts are reporting-only: **confirmed**. They have a small internal lookahead (L1) that does not affect trading.
* Calibration statistics over the full dataset: none exist in the infrastructure. In-sample and out-of-sample periods run on fresh
  engines (`run.py:38`). **OK.**

## Fill-model optimism, ranked (most likely to make results too good first)

1. Latency far too low, plus delivery counted from `T` instead of `E` (H1, H2, H3): more edge, fewer pick-offs, cancels that win races they would lose.
2. Full-size trade-through and quote-cross fills (H4): inflated volume, fill ratio and rebates; flatters mean-reversion. Not stress-tested.
3. Paper-trading queue double count (M2), and our own orders' queue erased (M4).
4. Same-millisecond arrival placement (M1). The direction is arbitrary, but the effect is large because about half of events share a millisecond.
5. Crude taker sweeps (M6). Matters for strategies that hedge.
6. Inside-spread first in line (L9); queue factor and cancel split. The `proportional` default is neutral. `all_ahead` is optimistic and untested.

## Accounting (item 3 of the brief): sound
The identity `cash + pos*mid = start + realized + unrealized - fees - funding` holds over whole runs (test, plus `identity_gap` of about 0
in reports). Spread capture plus inventory PnL equals gross PnL exactly, because `ref_mid` equals the previous mark. Fees are 2/5 bp of
notional, with rebates supported. Funding: see L4. Sharpe: see L5.

## Paper-trader safety (item 6 of the brief): CONFIRMED SAFE
The only network calls in `src/` are `requests.get` to `/fapi/v1/depth`, `/fapi/v1/time`, `/fapi/v1/fundingRate` (download only),
the data.binance.vision archive, and public `wss://fstream.binance.com/{public,market}/stream`. A grep for
POST/PUT/DELETE, `/fapi/v1/order`, api keys, `X-MBX`, signatures/hmac, listenKey, `ws.send` and environment variables found nothing.
No credentials are read anywhere. Gap behaviour: see M3.

## Determinism and seeds (item 5 of the brief): sound, except provenance (M5)
The same seed gives identical fills, equity and metrics (my rerun of the L2 smoke run, plus the tests). The strategy gets its own RNG
derived from the seed. Jitter is seeded and keeps message order. Magic numbers: see L7.

## Tests that pass for the wrong reason (item 7 of the brief)

* `tests/test_backtest_fills.py:160-168`, `test_cancellations_proportional_vs_all_behind`: asserts **6.0 for both** assumptions,
  because the displayed-size cap hides the difference. The test never shows that the assumptions differ. `all_ahead` is never tested.
* `tests/test_backtest_accounting.py:123-127`, `test_markouts_use_future_mid`: only checks that the value is not null. It would pass
  with the wrong sign, the wrong horizon, or the L1 lookahead.
* `tests/test_backtest_fills.py:112-116`: locks in the H4 overfill as correct behaviour.
* `tests/test_backtest_fills.py:295-306`, `test_strategy_sees_market_data_late`: the events have `event_ts_ns=None`, so it cannot detect H2.
* `tests/test_backtest_accounting.py:112-118`, determinism: uses only market orders. The passive/queue path and the same-millisecond tie
  rule are never exercised, because every fill test uses zero latency and separate milliseconds. The engine's tie claim has no test (M1).
* No tests exist for the paper trader, `on_book_lost`, gap handling, funding with a missing mark, or funding and fills at the same `ts_ns`.
* The L2 tests use empty depth messages (`depth(1, [], [])`) as clock ticks. Such messages cannot exist in stored data. Harmless, but unrealistic.

---

## Fix list by owner

| # | Fix | Owner |
|---|---|---|
| H1 | Measured latency defaults (feed about 150-250 ms, order about 100-200 ms, jitter); keep a colocated variant | backtester |
| H1 | Latency-measurement tool from `recv_ts_ns`, `E` and per-session clock offset | data-engineer |
| H2 | Deliver at `max(T, E) + latency`; add a test with `event_ts_ns` | backtester |
| H3 | Paper: arrival-time clock or measured latencies; fix the YAML comment | backtester |
| H4 | Volume caps for through and cross fills; fill-mechanism column; sensitivity variant | backtester |
| M1 | Process all same-`ts_ns` market events before arrivals; regression test | backtester |
| M2 | Paper reorder buffer sorted by `(ts_ns, priority)` | backtester |
| M3 | Replayable gap/resync markers in exchange time | data-engineer |
| M3 | Block quoting until resync; clear the stale book | backtester |
| M4 | Cap only the public part of the queue | backtester |
| M5 | Data manifest with hashes, git commit, intraday periods; commit `src/mm/backtest`, `src/mm/paper` | backtester / owner |
| M6, M7 | Taker sweep realism; exchange-side risk limits | backtester (+ quant-developer for strategy limits) |
| L1-L10 | As listed above | backtester / data-engineer |
| Tests | Rewrite the weak tests listed above; add paper, gap and tie tests | backtester |

Research and education only. Backtest results do not promise live performance.
