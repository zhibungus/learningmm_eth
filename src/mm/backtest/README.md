# Backtest engine: design and fill-model assumptions (plain language)

Code: `src/mm/backtest/`. Paper trading reuses it: `src/mm/paper/`.

## The idea in one picture

```
 market data (exchange time t)
      |  feed latency
      v
  STRATEGY  --- actions (place / cancel / replace) --->  order latency  --->  SIMULATED EXCHANGE
      ^                                                                            |
      |  report latency                                                            |
      +------------------  fills, acks, rejects  <---------------------------------+
```

* The strategy only ever sees the past, delayed by the feed latency.
* Its orders reach the exchange later still, so a cancel can lose a race against a fill.
* The simulated exchange sees the market at exact exchange time and decides if/when your resting
  orders fill (`fills.py`).
* Nothing reads a wall clock. Time moves only when a market event arrives, so a run is
  reproducible: same data + same config + same seed = identical fills.

## Files

| file | what it does |
|---|---|
| `strategy.py` | The interface a strategy implements (`Strategy`, actions, `MarketState`, `Fill`, `OrderUpdate`). Start here. |
| `engine.py` | The clock and the scheduler: applies latencies, routes events and orders. Also `build_engine`. |
| `exchange.py` | Simulated exchange: tick/lot checks, post-only rejection, taker vs maker, replace/cancel. |
| `fills.py` | Fill models: queue position, trade-through, L2 vs top-of-book. Fees. |
| `metrics.py` | Account (position, cash, PnL, fees, funding) and all metrics. |
| `report.py` | Writes `reports/<run>/` (also used by paper trading). |
| `run.py` | CLI: `python -m mm.backtest.run --config config/backtest/<file>.yaml` |
| `config.py` | Defaults for every setting. A YAML only lists what it changes. |
| `examples/demo_quoter.py` | **Not a strategy.** Plumbing test only. |

## How to run

```powershell
# backtest (in-sample + out-of-sample + sensitivity variants -> reports/<date>_<strategy>_<tag>/)
.venv\Scripts\python.exe -m mm.backtest.run --config config\backtest\demo_smoke.yaml
.venv\Scripts\python.exe -m mm.backtest.run --config config\backtest\demo_smoke.yaml --no-sensitivity
# run the replays (base + each sensitivity variant, per period) in 8 parallel processes: same results,
# less wall time; each process needs ~0.8-1.5 GB RAM
.venv\Scripts\python.exe -m mm.backtest.run --config config\backtest\glft_smoke.yaml --workers 8

# paper trading against the live public feed (no keys, nothing sent to Binance); Ctrl+C to stop
.venv\Scripts\python.exe -m mm.paper.run --config config\paper\demo_paper.yaml --duration 60

# tests
.venv\Scripts\python.exe -m pytest
```

Adding a real strategy: put a module in `src/mm/strategies/<name>.py` with ONE `Strategy` subclass
(or a `STRATEGY = Cls` attribute), then use `strategy: {name: <name>, params: {...}}` in the YAML.
A new short name can also be added to `STRATEGY_REGISTRY` in `engine.py`.

## Rules the run enforces (and you should too)

* Calibrate on `in_sample`; look at `out_of_sample` only after parameters are frozen. Never tune on it.
* Every run records its seed (`run.seed`) and writes the full resolved config to `config.json`.
* A sensitivity block (pessimistic fills, higher latency, both) is run automatically. If PnL
  disappears there, the base result leaned on optimistic assumptions.
* **Random streams (changed 2026-10-23, validator N2).** `run.seed` is split with `SeedSequence(seed).spawn(3)`
  into three independent generators: feed delay, order jitter, and the strategy's own `self.rng`. Before, feed
  delay and order jitter shared one generator, so a variant that sent a different number of orders got a
  different feed-delay path (no common random numbers). Now two variants with the same seed see the SAME
  feed delays. This deliberately changes every number versus runs made before this date.
* **Several seeds.** `sensitivity.seeds: [42, 1, 2]` runs the BASE row at those seeds; a variant takes its own
  `seeds: [...]` key (else it runs once at `run.seed`). One replay job per (variant incl. base, period, seed),
  all through `--workers`. The base run at `run.seed` stays the headline (summary tables, charts, fills);
  the sensitivity table shows mean (sd) across seeds, the number of seeds, `kills` (runs whose strategy
  summary says `killed` / seeds), time at inventory limit and mean abs inventory. `metrics.json` keeps the
  per-seed headline values plus mean/sd (`base_seeds`, `sensitivity`); `config.json` has `meta.seeds`.
  The cross-fill warning fires only if base minus `no_cross_fills` net PnL exceeds the base net PnL sd across
  seeds (with one seed it still fires, labelled "single seed, noise not estimated").
* Each period starts flat and is not liquidated at the end (inventory is marked at mid).
* **Warm start (2026-10-30).** A live recording has depth snapshots only at (re)connects, so a day usually
  has none at 00:00 and the L2 book would begin empty. A period may say how far back to feed the books:
  `warmup_from: "2026-10-23 19:06:24"` (UTC; pick the last snapshot before the window) or `warmup_hours: 2`.
  Events in `[warm-up start, window start)` go through `Engine.warm_up`: they update the exchange's book
  and the strategy's `MarketState` prices, and nothing else (no strategy callbacks, clock, orders, fills,
  account or metrics). The window itself is unchanged. Only data before the window is used, so it is causal.
  Gap markers inside the warm-up behave as in a replay (book dropped until the next snapshot). If the
  warm-up ends without a snapshot after the last book gap, the run stops with an error naming the fix (go
  back further). So the rule is: `warmup_from` must be at or before the last snapshot before the window.
  A tiny difference remains: at the window start the strategy's prices are the current ones, not delayed by
  the feed latency, which is irrelevant after the first second. `metrics.json` periods.<p> records
  `warmup_from_ns`, `warmup_events`, `warmup_last_snapshot_ns`; the warm-up days' files are in the manifest.
  `mm.data.loaders.replay(..., from_ns=)` starts inside the first day so a warm-up does not build (then
  discard) hours of events.

## Fill models: what is assumed

A resting limit order is only filled when the simulator decides that enough volume traded at or
through its price. Two models (`fill_model.type`):

### `quote_trade` (top-of-book data: trades + bookTicker)

1. **Joining the queue.** An order at the best price starts BEHIND all displayed quantity there
   (times `initial_queue_factor`). Inside the spread it is first in line. Deeper than the best
   price the queue is invisible, so it is treated as infinitely long: such an order fills only when
   price trades THROUGH it.
2. **Trade-through** (price prints strictly beyond our price): a certain fill, but only up to the SIZE of
   that print (`cap_fills_by_volume`). A 0.001 ETH print cannot fill a 10 ETH order.3. **Trade at our price:** first eats the queue ahead of us, and only the excess fills us (partial
   fills happen naturally).
4. **Cancellations.** If the displayed size at our price drops by more than the trades we saw, the
   difference is a cancellation. `cancel_assumption`: `proportional` (default; the share ahead of
   us is `queue_ahead / displayed`), `all_behind` (pessimistic), `all_ahead` (optimistic).
5. `require_trade_through: true` is the conservative switch: trades AT our price never fill us.
6. If the public quote crosses our resting price without a trade print (data timing), we are filled.

### `l2` (depth data, our own live recordings)

Same rules, but the queue at any level is known from the book, not only at the best price. Taker
orders walk the real book level by level.

### Both

* **Post-only** orders that would cross are rejected, not filled.
* **IOC and market orders never rest.** What is marketable on arrival fills as taker; the rest is cancelled
  at once (`ioc_remainder`, or `no_liquidity` if nothing filled). A non-marketable IOC used to rest in the
  book and fill later as a maker (fixed 2026-10-22, validator finding F1).
* **Replace (modify)** is one request that goes to the BACK of the queue, like Binance ("modified orders
  will be reordered in the match queue"). Config `exchange.replace_keeps_priority: true` restores the old,
  optimistic rule (a same-price size cut keeps its place). If the re-placed order would be rejected
  (e.g. post-only would cross) the old order stays and you get `replace_rejected`.
  `ReplaceOrder.qty` = new REMAINING size. `ReplaceOrder.total_qty` = new TOTAL size (Binance's modify
  `quantity`): the exchange sets remaining = total_qty minus what is already filled, so a fill still in
  flight cannot push inventory past a limit. If total_qty is not above the filled amount, the order is
  CANCELLED (`canceled`, reason `qty_below_filled`), as the Binance Modify Order docs say; not checked on testnet. Giving both fields raises `ValueError`.
* **Taker** orders pay the spread and the taker fee. With top-of-book data only the displayed top
  size fills at the touch; the excess fills `sweep_penalty_ticks` worse (crude).
* **Fees:** maker 0.0002, taker 0.0005 of notional (config; negative = rebate).
* **Funding:** on each funding event, `position * mark_price * rate` is paid by longs (received by
  shorts when the rate is positive).
* **Tick 0.01 / lot 0.001 / min notional** enforced: bad orders are rejected, as on Binance.
* **Order latency** is real: an order is invisible to the market until it arrives, and a cancel only
  takes effect when it arrives.

## Known biases (read before trusting any number)

1. **No market impact.** Our orders never change what the market does. Real quoting changes how
   others behave, especially when large.
2. **Queue position is a guess.** Errors tend to flatter us when the queue estimate is too short and
   hurt us when it is too long. `pessimistic_fills` shows how much that matters.
3. **A quote can cross us without a print** (data timing). We count it as a fill, capped by the displayed
   size on the crossing side, with memory per crossing episode (liquidity already taken is not refilled
   when the displayed size merely flickers; it resets when the quote stops crossing). `cross_fills: false` (the `no_cross_fills` sensitivity) turns it off; the
   report's "fills by mechanism" row (queue / through / cross / taker) shows how much a result rests on it.
   aggTrades merging is NOT a problem: aggTrade volume equals raw-trade volume.4. **Deeper-than-touch orders fill only on trade-through** in the quote model: realistic only in
   the sense of being conservative about the unseen queue.
5. **Millisecond timestamps.** ALL market events with one `ts_ns` are processed before any order that
   arrives at that `ts_ns`, so a result cannot depend on how many prints share a millisecond.
6. **Latency** defaults are MEASURED on this PC (`python -m mm.data.latency`, 2026-10-19). The feed delay
   (exchange publish time E to our receipt) is drawn from a configured table (p50 239 / p90 296 / p99 772 ms):
   one draw per 2 s window, shared by its events, so slow stretches come in bursts like real lag spikes, and
   events stay in order (a late event holds back those behind it). Effective percentiles match the table
   within ~5% (p50) and ~15% (p99); p90 comes out ~20-25% high because of the backlog drain. Order latency is
   132 ms + up to 59 ms uniform jitter, reports 240 ms. The strategy sees an event no earlier than
   `max(T, E) + delay` (T = match time; archive trades have no E, so a configured 7 ms is added). The old
   10/20 ms setting is the `colocated` row, a heavier tail is `latency_spikes`. Zero latency is refused (it
   leaks same-millisecond information) unless a test sets `allow_zero_latency`.7. **bookTicker history ends 2024-03-30.** Strategies are tested on a market 2.5 years old; recent
   data has trades only and cannot drive the passive models.
8. **Not modelled:** margin, liquidation, rate limits, hidden/iceberg quantity, self-trade prevention,
   outages, VIP tier changes. Exchange-side `max_position` / `max_order_notional` (config) reject orders
   that could breach the limit. Final inventory is marked at mid; the report also shows PnL if it had to be
   exited at the touch.9. **Paper trading** uses the exchange clock plus the SAME simulated latencies as the backtest (the real
   network delay is not part of exchange time). Trades and depth come from two websockets, so events are held
   `paper.reorder_hold_ms` and released sorted like a replay. On a gap (`GapEvent`, or a StatusEvent
   disconnect) all simulated orders are cancelled. What else is forgotten depends on `GapEvent.stream`:
   the TRADES socket ("market") keeps the book and quoting resumes at once; the book socket ("public" /
   "depth") or an unknown stream (None) wipes the book and rejects orders ("awaiting_resync") until the
   next snapshot (quote model: next quote).
10. **Provenance.** `config.json` records the git commit (+ dirty flag), a SHA-256 of every project source
    file that ran (`meta.code`, because untracked code is not identified by the commit) and every input
    file (including depth and gap-marker files) with row count and SHA-256 (`meta.manifest`); periods may be intraday (`start_time` / `end_time`, UTC).
11. **Taker orders, quote data:** liquidity beyond the touch is assumed to come in tiers as big as the touch,
    each `sweep_penalty_ticks` worse, up to `sweep_max_tiers`; liquidity already used by an earlier taker is
    remembered until the next quote/depth update.
12. **Replace queue priority.** Binance re-queues every modify, so by default every replace here goes to the
    back (`exchange.replace_keeps_priority: false`). `true` keeps priority on a same-price size cut: that
    flatters a strategy that re-quotes often. Results from before 2026-10-22 used the optimistic rule.
13. **Modify edge cases not modelled like Binance:** a post-only modify that would cross is rejected here
    (`replace_rejected`, old order stays), while Binance cancels the order; a `total_qty` at or below the
    filled amount cancels the order here (per the Binance docs, unverified on testnet).
14. **Bootstrap intervals are optimistic.** The markout confidence interval resamples fills as if
    independent, but fills cluster in fast markets, so the true uncertainty is larger.

## Build vs reuse (hftbacktest)

`hftbacktest` (Python/Rust, numba) is the standard open-source tool for this: it already has L2/L3
queue-position models, latency models and Binance data converters, and is far faster. We did not
use it now because (a) the owner is learning, and a small readable engine whose every assumption is
documented here is the point; (b) our data adapters, paper trading and the strategy interface
needed to match the existing `mm.data` event layer; (c) free top-of-book history is the only
long dataset, and that works fine with the simple model. Recommendation: keep this engine as the
reference, and once the live recorder has a few weeks of L2 depth, re-run the same strategy on
`hftbacktest` as an independent cross-check of the fill model (agreement on fill rates and markouts
is good evidence; disagreement tells us which assumption to examine). This comparison has not been
done and its API was not checked in this environment.

## Metrics glossary

* **Spread capture**: `side * (mid before fill - fill price) * qty`, summed. Positive for a maker
  buying below mid.
* **Inventory PnL**: `position * change in mid`, summed over time. Adverse selection shows here.
* Spread capture + inventory PnL = gross trading PnL; minus fees and funding = net PnL (exact).
* **Markouts**: for each fill, `side * (mid at +1s/+10s/+60s - fill price)` and the same versus the
  mid at the fill. Negative average move = the market goes against you right after you trade.
* **Sharpe**: mean/std of PnL increments on a fixed bucket (default 60 s), annualised. Perps have no
  capital base, so use it to compare runs, not as a fund Sharpe. Very short or very churny runs give
  huge magnitudes.
* **Order counts**: `orders.accepted / rejected` are NEW orders only. Replaces are separate
  (`replace_requests`, `replaced`, `replace_rejected`, `replace_reject_reasons`). Before 2026-10-22
  "accepted" included replaces, which made the fill ratio tiny and misleading.
* **Fill ratio** (`fill_ratio_orders`): new orders with any fill / new orders accepted (a replaced order counts
  once). `fill_ratio_per_action`: order incarnations with a fill / (new orders + replaces accepted).
  `fill_ratio_qty`: filled qty / accepted qty (replaces adjust the accepted size).
* **Markout CI**: `markouts.<h>.avg_markout_per_eth_ci95` is a seeded bootstrap (`metrics.bootstrap_n`
  resamples, seed = `run.seed`) 95% interval for the size-weighted mean markout per ETH. Shown in
  summary.md for every horizon (spec hypothesis H5 uses the 10 s one).
* **Coverage**: `coverage.covered_hours` = time between consecutive market events, leaving out silences
  longer than `metrics.coverage_gap_s` (60 s). `duration_hours` is first to last event. summary.md
  states both, not the calendar window. Note a strategy that stops quoting (e.g. a kill switch) still
  sees the full data period; the validator's "19.3 h" for the 2024-03-30 run was the kill time, not missing data.
* **Strategy summary**: if the strategy has a `summary()` method returning a JSON-able dict, it is stored
  at `periods.<p>.strategy_summary` in metrics.json and its numeric fields are tabulated in summary.md
  (pauses, kills, rejects by reason ...). Base runs only, not sensitivity variants.
