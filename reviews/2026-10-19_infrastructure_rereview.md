# Infrastructure re-review (2026-10-19, second pass)

Reviewer: validator. This checks the fixes made for `reviews/2026-10-19_infrastructure_review.md`.
Full suite: **90 passed**. I re-read the changed code and ran new throwaway probes from my scratch area. Nothing in `src/` or
`tests/` was edited.

## Verdict

* **Archive top-of-book backtests (`quote_trade` model): FIT WITH CAVEATS.** H1-H4 are fixed or mostly fixed. One remaining caveat
  (R2, cross-fill refill) must be fixed before anyone tests order sizes larger than the demo's 0.05 ETH.
* **L2 replay of live recordings, and paper trading: NOT FIT YET.** The gap fix introduced a new blocker (R1). A disconnect of the
  trade websocket wipes the simulated L2 book and blocks quoting for the rest of the session.
* One more new bug (R4) makes a strict replay crash on any live-recorded day that spans a midnight rotation.

[CONFIRMED] = reproduced with a probe or measured. [SUSPICION] = from reading the code only.

---

## New problems introduced by the fixes

### R1. Trade-socket disconnect destroys the L2 book and blocks quoting forever [CONFIRMED, BLOCKER for L2/paper]
*Where:* every GapEvent is treated the same, whatever its `stream`:
* `engine.py:288-291` sends every gap to `exchange.py:96-101`, which clears the book and sets `awaiting_resync`.
* `exchange.py:78-80` only lifts `awaiting_resync` for depth models after a *snapshot*.
* `live.py:154-161` and `182-190` emit `GapEvent(stream="market")` when the aggTrade socket drops.
* `paper/run.py:151-154` calls `on_book_lost` on that socket's "disconnected" status.

*Why it breaks:* the depth socket did not drop, so it never sends a new snapshot. The probe shows the result:
* the exchange book is wiped and then rebuilt from diffs only. Bids `{100.0, 99.99, 99.97}`, asks `{}`, while the real book still
  had asks at 100.01 and 100.02.
* every later order is rejected `awaiting_resync`.

The same happens when replaying recordings that contain persisted "market" disconnect markers.

*Plain language:* losing the trades feed for a second makes the simulator forget the whole order book and stop trading for the
rest of the day. It also fills the book with half-information. Paper results after any trade-socket reconnect mean nothing.

*Fix (backtester):* act on `ev.stream`.
* A "market" (trades) gap: cancel orders if you like, but do **not** clear the depth book, and resume at once (or after a short
  configurable pause).
* Only depth/"public" gaps and resyncs should clear the book and wait for a snapshot.
* For the quote model, a "public" gap waits for the next quote, as now.
* Add a test with `stream="market"`. The current tests only use `stream=None`.

### R2. A crossed quote refills on every later crossed quote, so the cross-fill cap leaks [CONFIRMED, declared residual]
*Where:* `fills.py:286-314` resets the pool to the displayed quantity on every quote event.

*Probe:* a 2.0 ETH bid. The market sits below it for 10 quote updates, and only the ask *size* flickers between 0.5 and 0.4 ETH.
Fills: 0.5, 0.4, 0.5, 0.4, 0.2, a **total of 2.0 ETH**, with no trade at all.

The demo is barely affected: 358 of 8,446 cross-filled orders in-sample had more than one cross fill, and at most 5. That is only
because 0.05 ETH is smaller than a typical displayed ask. At realistic sizes the cap does little.

*Fix (backtester):* remember how much of the opposite side we have taken during one "crossing episode" (key it by price level, or
by the bookTicker `update_id` chain). Let the pool grow only when the displayed size grows, or reset it when the book stops
crossing our price.

### R3. Effective feed delay: median too slow, tail too fast [CONFIRMED, medium]
*Where:* `engine.py:309-319`. Delivery is `max(T, E) + 240 ms + U(0, 59 ms)`, then a running maximum keeps events in order.

*Probe on 2M archive quotes:* in a busy stream the running maximum pushes nearly every event to the top of the jitter range.
* simulated T-to-strategy delay: p50 **295 ms**, p90 304 ms, p99 **310 ms**
* measured on this PC: p50 239 ms, p90 296 ms, p99 **772 ms** (`config.py:44-47`)

Also, the suggested 240 ms is the *T-to-arrival* median, but the engine adds it to `max(T, E)`, so `E - T` is counted twice.
That is only a few ms.

*Plain language:* most of the time the backtest is about 50 ms slower than reality, which is harmless. But it never has the bad
moments, the 0.5-1 s lag spikes that happen in bursts. Those are exactly when a market maker gets picked off.

*Fix (backtester, with the data-engineer's tool):*
* Sample delays from the measured distribution, ideally as a time series, because real delays come in correlated bursts.
* Or use base = p10 with a heavy-tailed jitter.
* Add a "latency spikes" sensitivity variant.

This is not a blocker. The base case is no longer optimistic at the median.

### R4. Recorder file names break replay order at midnight [CONFIRMED, medium]
*Where:* `record.py:82-92` files each piece of data by its exchange-time date, which is correct. But each file is named
`part-<HHMMSS of PC-clock window start>-<n>` and replay reads files in name order (`loaders.py:152-154`).

*Probe:* the PC clock is about 5 s fast. The window that starts at PC midnight ("000000") holds the last ~5 s of the previous
exchange day. Inside that day's folder, its file sorts **before** `part-235000-...`, even though it holds the day's latest data.
Strict replay of that day raises `ReplayOrderError`. With `strict=False`, time silently goes backwards once.

*Fix (data-engineer):* name parts by the first row's exchange `ts_ns`, for example `part-<first_ts_ns>-<n>`. Or have `find_files`
sort by each file's minimum `ts_ns`. Add a midnight test.

### R5. Smaller new issues
* **[SUSPICION] Paper heartbeat clock runs ahead of the data.** `paper/run.py:128-138`: the estimate is `last_ts + elapsed + hold
  + assumed_feed_delay`. The last released event was already that old, so adding hold and delay again puts the engine about
  450 ms ahead of the next event. That event then gets clamped, after order arrivals it really preceded. This only matters when
  the buffer is empty for a whole heartbeat, which is rare because depth and bookTicker arrive constantly. Low impact, but drop
  the `extra_ns` term. Owner: backtester.
* **[CONFIRMED] Paper `advance()` breaks the tie rule** at the oldest buffered timestamp. It runs with `inclusive=True`
  (`engine.py:334-340`), so order arrivals at exactly that ms run before the market events of that ms. Minor. Owner: backtester.
* **[CONFIRMED] The manifest can miss inputs.** `run.py:56` defaults a missing per-stream source to "archive". `replay` defaults
  depth to "live" (`loaders.py:97`). With a source dict that leaves out depth, the depth files are therefore not fingerprinted.
  The gap-marker files (`gaps` dataset) are never fingerprinted at all. Owner: backtester.
* **[CONFIRMED] Stale comments.** `config/backtest/demo_smoke.yaml` and `config/paper/demo_paper.yaml` say "feed ~200 ms, order
  ~150 ms, +50 ms jitter". The real defaults are 240 / 132 / 59 ms. Owner: backtester.

---

## Status of the original findings

| # | Finding | Status | Evidence / remaining gap |
|---|---|---|---|
| H1 | Latency 10x too fast | **Fixed** (shape caveat R3) | Defaults 240 / 132 / 240 ms with 59 ms jitter (`config.py:49-56`), from `mm.data.latency`. Colocated is a variant. Order latency (half the REST round trip + 20 ms gateway) cannot be verified without an account. |
| H2 | Delivery from `T` instead of `E` | **Fixed** | `engine.py:309-319` uses `max(T, E)`; archive trades get `T + 7 ms`. Test at `test_backtest_fills.py:312-332`. |
| H3 | Paper ran at 0 ms | **Fixed** | Paper inherits the measured defaults; zero is refused (`config.py:175-184`). Report shows 240 / 132 ms. |
| H4 | Uncapped through / cross fills | **Mostly fixed** | Through fills capped by print size (`fills.py:265-272`); mechanism column; `no_cross_fills` variant. Residual R2. |
| M1 | Same-ms ties | **Fixed** (paper caveat R5) | All events at `t` run before arrivals at `t` (`engine.py:24-28`, `303`). Test `test_backtest_fixes.py:29-37`. |
| M2 | Paper arrival-order double count | **Mostly fixed** | `ReorderBuffer`, 250 ms hold. On the live recordings, events still out of exchange order fall from 15.5% (no hold) to **2.4%** (250 ms hold), and 1.0% at 500 ms. |
| M3 | Gap handling | **Partially fixed; new blocker R1** | Gaps cancel, clear and block until resync. But (a) R1; (b) "resync" markers are stamped at the *snapshot* time, not when the lost message was detected (`live.py:216-220`, `237-243`; `loaders.py:330-341`). In replay, orders therefore rest on a stale book for the REST round trip, often 0.2-1 s+. Paper is fine here because it reacts to the `book_gap` status at detection. Never exercised on a real recorded gap (declared). |
| M4 | Own orders' queue erased | **Fixed** | `_own_ahead` computed on demand (`fills.py:232-240`). Test `test_backtest_fixes.py:66-70`. |
| M5 | Provenance | **Partially fixed** | SHA-256 + row-count manifest and intraday periods are in place. But `src/mm/backtest`, `src/mm/paper`, `src/mm/data/latency.py` and the new tests are **still untracked**, so the recorded commit `1b68ff0` (dirty) does not identify the code. See also R5 manifest gaps. |
| M6 | Crude taker sweeps | **Fixed (reasonable)** | Size tiers beyond the touch, consumed liquidity remembered until the next public update (`fills.py:399-427`, `472-498`). The tier sizes are an assumption; fine for now. |
| M7 | No risk limits | **Fixed** | `exchange.py:163-176` counts all open orders; rejects reported. |
| L1 | Markout slot lookahead | **Fixed** | Real-value tests `test_backtest_fixes.py:170-199`. |
| L2 | `ref_mid` leak | **Fixed** | Stripped from the strategy's copy (`engine.py:214-217`). |
| L3 | Zero latency | **Fixed** | Validator plus a test-only opt-in. |
| L4 | Funding notes | **Fixed** | Strategy cash includes funding (test); partial months flagged in quality. |
| L5 | Sharpe | **Partially fixed** | A t-statistic was added. No bootstrap or daily stats (declared). |
| L6 | PC-clock date folders | **Fixed, new bug R4** | Partitioned by exchange `ts_ns`; replay looks one day wider for old recordings. |
| L7 | Magic numbers | **Fixed** | Horizons, histogram bins, timeouts, backoff and clock-check now come from config. |
| L8 | Stale `_pending_replace` | **Fixed** | `engine.py:235`, test. |
| L9 | Inside-spread first in line | **Not fixed** (declared) | Unchanged. Acceptable now that H2 and M1 are fixed. Revisit for strategies that quote inside a wide spread. |
| L10 | Free exit at mid | **Fixed** | `net_if_liquidated_at_touch` reported. |

### Weak tests
* Cancel-assumption test: **fixed**. Three different expected values, including `all_ahead` (`test_backtest_fills.py:183-189`).
* Markouts: **fixed**. Sign, horizon and slot tests. The old existence-only test is still in `test_backtest_accounting.py:123-127`; it is harmless now.
* Trade-through overfill locked in as correct: **fixed**. It now asserts the cap (`test_backtest_fills.py:120-128`).
* Feed latency with no E: **fixed** (`test_backtest_fills.py:312-332`).
* Determinism with passive orders: **fixed** (`test_backtest_fixes.py:230-236`). The tie rule now has a test.
* Paper / gap tests: **added**, but they pass for a narrower reason than they claim:
  * the gap tests use `stream=None`, so they cannot catch R1;
  * the recorder test does not cover midnight file naming (R4);
  * nothing tests repeated crossed quotes (R2).
* Empty depth messages used as clock ticks: still present; harmless.

---

## Is the default cross-fill setting too generous?

Short answer: **for PnL, no.** On this data cross fills are bad fills and cost money. **For volume and fill size, yes, until R2 is
fixed.** The "spread capture" they show is an attribution effect, not earned edge.

Facts from the new smoke `fills.parquet`, in-sample (out-of-sample is similar):

| mechanism | fills | qty (ETH) | spread capture | 10 s markout PnL | mean 1 s move |
|---|---|---|---|---|---|
| through | 26,868 | 691.3 | **+23.74** | -237.0 | -0.99 bp |
| cross | 8,817 | 340.1 | **+2.68** | -121.6 | -0.84 bp |
| queue | 470 | 17.0 | +0.09 | -2.5 | -0.49 bp |

* In the **base** run, cross fills carry only about 10% of spread capture (2.68 of 26.50). Through fills carry the rest.
  The collapse to 3.23 in `no_cross_fills` comes from comparing two different runs. Without cross fills, the same orders stay
  resting and are trade-through-filled a moment later. By then the reference mid has already moved down to our price, so the
  same economic trade shows up as about zero spread capture and a smaller inventory loss.
* Net PnL: base -1,127.87, `no_cross_fills` -1,096.47 (in-sample); -832.93 vs -796.82 (out-of-sample). **Cross fills make
  the demo about 31-36 USDT worse.** They are adverse fills. The market moved through our price, so we buy just before it falls
  further.
* Plain language: "spread capture" measures your fill against the mid *just before* the event that filled you. For a cross fill,
  that mid is already out of date: the quote that filled you also moved the mid against you. So the decomposition shows a nice
  "spread capture" and a matching "inventory loss". Only the net number is meaningful.
* Realism is uncertain in both directions. With our bid at the top of the book, a seller posting at or below it would trade with
  us if it used a normal limit order, but would be *rejected* if it used post-only (GTX), which many Binance market makers use.
  So cross fills may be too many *and* too big (R2).

Recommendation:
* Keep `cross_fills: true` as the default, because it penalises adverse moves, which is the conservative direction for PnL. But
  fix R2 first.
* Make `no_cross_fills` a required row in every strategy report.
* Rule for sign-off: if a strategy's **net** PnL is *better* with cross fills on than off, treat the result as suspect.
* Report spread capture per mechanism, so nobody reads the cross-fill line as edge.
* Also show filled quantity, not just fill records, in the sensitivity table. Capped fills split one order into several partial
  records: `pessimistic_fills` shows *more* fills (40,943) than base (36,155) while filling less per order.

---

## Remaining work, by owner

| Priority | Item | Owner |
|---|---|---|
| Blocker (L2/paper) | R1: make gap handling depend on `stream`; a trades-socket gap must not clear the depth book or wait for a snapshot; add a test | backtester |
| Before larger sizes | R2: crossing-episode memory for cross fills; spread capture per mechanism; filled qty in the sensitivity table | backtester |
| Medium | R4: name or sort recorder parts by exchange `ts_ns`; midnight test | data-engineer |
| Medium | M3(b): stamp "resync" markers at the exchange time the gap was detected, not at the snapshot | data-engineer |
| Medium | R3: empirical or heavy-tailed latency sampling; "latency spikes" variant | backtester (+ data-engineer tool) |
| Medium | M5: commit the backtest, paper, latency and test code so the recorded commit means something; fix manifest source defaults and include `gaps` files | owner / backtester |
| Low | R5 paper heartbeat `extra_ns`, paper `advance` tie rule, stale YAML comments; L5 bootstrap; L9 | backtester |

Research and education only. Backtest results do not promise live performance.
