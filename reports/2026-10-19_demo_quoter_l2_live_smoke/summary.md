# Backtest report: 2026-10-19_demo_quoter_l2_live_smoke

Strategy `demo_quoter`, params `{"half_spread_ticks": 1, "order_qty": 0.05, "max_position": 0.5}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**

## What was run

* **live_recording**: 2026-10-19 to 2026-10-19 (UTC, inclusive), 77,909 market events replayed, 5.6 h of data.

Fill model: `l2` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | live_recording |
|---|---|
| Net PnL (USDT) | -1.54 |
|   spread capture | 0.55 |
|   inventory PnL | -0.94 |
|   fees | -1.15 |
|   funding | -0.00 |
| Fills (maker / taker) | 90 (90 / 0) |
| Traded notional (USDT) | 5,755 |
| Orders accepted / rejected | 136 / 47 |
| Fill ratio (orders with a fill) | 35.3% |
| Fill ratio (qty filled / qty accepted) | 35.3% |
| Sharpe (annualised, 60s PnL buckets) | 14.97 |
| Max drawdown (USDT) | 4.93 |
| Mean abs inventory (ETH) | 0.340 |
| Max abs inventory (ETH) | 0.500 |
| Time at inventory limit | 8.4% |
| Final position (ETH) | 0.400 |
| Fills by mechanism (queue/through/cross/taker) | 15/68/7/0 |
| Net PnL if final inventory exited at the touch | -2.02 |
| t-stat of mean PnL per bucket | 0.38 |
| Risk-limit rejects | 0 |
| Mid move 1s after fill (bps, + = favourable) | -3.488 |
| Mid move 10s after fill (bps, + = favourable) | -4.605 |
| Mid move 60s after fill (bps, + = favourable) | -3.168 |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**live_recording**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -1.54 | 0.55 | -0.94 | -1.15 | 90 | 2.4 | 100% | -4.605 | 0.6 / +0.01; 1.5 / +0.53; 0.3 / +0.01; 0.0 / +0.00 |
| colocated | -2.62 | 1.59 | -2.10 | -2.11 | 172 | 4.4 | 100% | -2.747 | 0.9 / +0.12; 2.8 / +1.45; 0.7 / +0.02; 0.0 / +0.00 |
| pessimistic_fills | -1.92 | 0.51 | -1.30 | -1.13 | 102 | 2.3 | 100% | -4.632 | 0.0 / +0.00; 2.4 / +0.51; 0.0 / +0.00; 0.0 / +0.00 |
| no_cross_fills | -1.92 | 0.51 | -1.30 | -1.13 | 90 | 2.3 | 100% | -4.632 | 0.6 / +0.01; 1.7 / +0.51; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | -1.55 | 0.55 | -1.04 | -1.06 | 81 | 2.2 | 100% | -4.953 | 0.5 / +0.01; 1.5 / +0.53; 0.2 / +0.01; 0.0 / +0.00 |
| higher_latency | -1.44 | 0.05 | -0.63 | -0.86 | 66 | 1.8 | 100% | -4.308 | 0.7 / +0.01; 1.1 / +0.04; 0.0 / +0.00; 0.0 / +0.00 |
| pessimistic_all | -1.44 | 0.05 | -0.63 | -0.86 | 77 | 1.8 | 100% | -4.308 | 0.0 / +0.00; 1.8 / +0.05; 0.0 / +0.00; 0.0 / +0.00 |

> WARNING (live_recording): net PnL is better WITH cross fills than without (`no_cross_fills`). Treat this result as suspect.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 18 modules, combined SHA-256 `f4c4ccff8ce0fee58c57556cff0c9412bcd01bd8526be518f575eccee0dafad8` (the git commit alone does not identify untracked code)
* git commit: `1b68ff078b9b6d57876c6490f57daa4d69646010` (working tree has uncommitted changes)
* data manifest: 17 input files with SHA-256 hashes and row counts are listed in `config.json` under `meta.manifest`.

## Assumptions that most limit realism

1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue by trades at our price; cancellations are split proportionally (see config). With only top-of-book data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Feed delay is drawn from an empirical table** (p50/p90/p99 measured on the owner's PC with `mm.data.latency`, smoothly varying so slow stretches come in bursts); order latency is a base value plus uniform jitter. The strategy sees archive events no earlier than their publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and same-millisecond events are processed before any order arriving in that millisecond.
3b. **Fill sizes are capped** (trade-through by print size, quote cross by displayed size), but a crossed quote can fill again on each later crossed quote. The 'Fills by mechanism' row shows how much of the result rests on through/cross fills; `no_cross_fills` tests it.
4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. Recent dates have trades only and cannot support a passive fill model.
5. **Taker fills (quote model)** assume liquidity beyond the touch comes in tiers as big as the touch, each a configured number of ticks worse; L2 data walks the real book.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, exchange outages, fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
