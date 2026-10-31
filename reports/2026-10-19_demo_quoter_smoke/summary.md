# Backtest report: 2026-10-19_demo_quoter_smoke

Strategy `demo_quoter`, params `{"half_spread_ticks": 2, "order_qty": 0.05, "max_position": 0.5, "requote_ticks": 1}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**

## What was run

* **in_sample**: 2024-03-29 to 2024-03-29 (UTC, inclusive), 8,902,733 market events replayed, 24.0 h of data.
* **out_of_sample**: 2024-03-30 to 2024-03-30 (UTC, inclusive), 6,677,660 market events replayed, 24.0 h of data.

Fill model: `quote_trade` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | in_sample | out_of_sample |
|---|---|---|
| Net PnL (USDT) | -1,138.08 | -838.32 |
|   spread capture | 26.14 | 16.86 |
|   inventory PnL | -413.24 | -307.90 |
|   fees | -751.02 | -546.62 |
|   funding | 0.04 | -0.67 |
| Fills (maker / taker) | 36623 (36623 / 0) | 27906 (27906 / 0) |
| Traded notional (USDT) | 3,755,106 | 2,733,083 |
| Orders accepted / rejected | 50868 / 8868 | 37175 / 5129 |
| Fill ratio (orders with a fill) | 41.8% | 41.8% |
| Fill ratio (qty filled / qty accepted) | 41.8% | 41.8% |
| Sharpe (annualised, 60s PnL buckets) | -834.54 | -719.78 |
| Max drawdown (USDT) | 1,138.08 | 838.34 |
| Mean abs inventory (ETH) | 0.301 | 0.288 |
| Max abs inventory (ETH) | 0.500 | 0.500 |
| Time at inventory limit | 8.7% | 15.0% |
| Final position (ETH) | 0.294 | 0.050 |
| Fills by mechanism (queue/through/cross/taker) | 476/27193/8954/0 | 344/21468/6094/0 |
| Net PnL if final inventory exited at the touch | -1,138.60 | -838.41 |
| t-stat of mean PnL per bucket | -43.65 | -37.65 |
| Risk-limit rejects | 0 | 0 |
| Mid move 1s after fill (bps, + = favourable) | -0.932 | -0.902 |
| Mid move 10s after fill (bps, + = favourable) | -1.044 | -1.068 |
| Mid move 60s after fill (bps, + = favourable) | -1.060 | -1.106 |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**in_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -1,138.08 | 26.14 | -413.24 | -751.02 | 36623 | 1,062.1 | 100% | -1.044 | 17.2 / +0.09; 700.0 / +23.36; 344.8 / +2.69; 0.0 / +0.00 |
| no_cross_fills | -1,089.40 | 2.88 | -377.87 | -714.42 | 40848 | 1,010.3 | 100% | -0.999 | 15.8 / +0.08; 994.5 / +2.80; 0.0 / +0.00; 0.0 / +0.00 |

**out_of_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -838.32 | 16.86 | -307.90 | -546.62 | 27906 | 776.2 | 100% | -1.068 | 13.7 / +0.09; 533.5 / +15.54; 228.9 / +1.23; 0.0 / +0.00 |
| no_cross_fills | -796.72 | 1.70 | -275.49 | -522.43 | 30886 | 741.8 | 100% | -1.023 | 13.6 / +0.08; 728.2 / +1.62; 0.0 / +0.00; 0.0 / +0.00 |

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 18 modules, combined SHA-256 `f4c4ccff8ce0fee58c57556cff0c9412bcd01bd8526be518f575eccee0dafad8` (the git commit alone does not identify untracked code)
* git commit: `1b68ff078b9b6d57876c6490f57daa4d69646010` (working tree has uncommitted changes)
* data manifest: 6 input files with SHA-256 hashes and row counts are listed in `config.json` under `meta.manifest`.

## Assumptions that most limit realism

1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue by trades at our price; cancellations are split proportionally (see config). With only top-of-book data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Feed delay is drawn from an empirical table** (p50/p90/p99 measured on the owner's PC with `mm.data.latency`, smoothly varying so slow stretches come in bursts); order latency is a base value plus uniform jitter. The strategy sees archive events no earlier than their publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and same-millisecond events are processed before any order arriving in that millisecond.
3b. **Fill sizes are capped** (trade-through by print size, quote cross by displayed size), but a crossed quote can fill again on each later crossed quote. The 'Fills by mechanism' row shows how much of the result rests on through/cross fills; `no_cross_fills` tests it.
4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. Recent dates have trades only and cannot support a passive fill model.
5. **Taker fills (quote model)** assume liquidity beyond the touch comes in tiers as big as the touch, each a configured number of ticks worse; L2 data walks the real book.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, exchange outages, fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
