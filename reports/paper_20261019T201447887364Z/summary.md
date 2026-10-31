# Paper-trading report: paper_20261019T201447887364Z

Strategy `demo_quoter`, params `{"half_spread_ticks": 2, "order_qty": 0.05, "max_position": 0.5, "requote_ticks": 1}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**

## What was run

* **paper**: live, started 20261019T201447887364Z to stopped 20:15:53Z (UTC, inclusive), 18,130 market events replayed, 0.0 h of data.
* PAPER RUN: live public data, simulated exchange; no real orders were sent.
* Engine clock = exchange time; simulated latency feed 240.0 ms, order 132.0 ms (+ jitter), events re-ordered with a 250.0 ms hold.
* Wall time 65.8 s, 18,130 events, 0 feed drops/resyncs (resting orders are cancelled on each).

Fill model: `l2` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | paper |
|---|---|
| Net PnL (USDT) | -0.41 |
|   spread capture | 0.01 |
|   inventory PnL | -0.14 |
|   fees | -0.29 |
|   funding | -0.00 |
| Fills (maker / taker) | 32 (32 / 0) |
| Traded notional (USDT) | 1,440 |
| Orders accepted / rejected | 30 / 3 |
| Fill ratio (orders with a fill) | 40.0% |
| Fill ratio (qty filled / qty accepted) | 40.0% |
| Sharpe (annualised, 60s PnL buckets) | n/a |
| Max drawdown (USDT) | 0.41 |
| Mean abs inventory (ETH) | 0.074 |
| Max abs inventory (ETH) | 0.150 |
| Time at inventory limit | 0.0% |
| Final position (ETH) | -0.100 |
| Fills by mechanism (queue/through/cross/taker) | 3/29/0/0 |
| Net PnL if final inventory exited at the touch | -0.53 |
| t-stat of mean PnL per bucket | n/a |
| Risk-limit rejects | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.115 |
| Mid move 10s after fill (bps, + = favourable) | -1.630 |
| Mid move 60s after fill (bps, + = favourable) | n/a |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 21 modules, combined SHA-256 `5ea58e75b6542d06c656d645fc239c2a491b2d6654f96a6e6cb7fb48e4fed925` (the git commit alone does not identify untracked code)
* git commit: `1b68ff078b9b6d57876c6490f57daa4d69646010` (working tree has uncommitted changes)
* data manifest: 0 input files with SHA-256 hashes and row counts are listed in `config.json` under `meta.manifest`.

## Assumptions that most limit realism

1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue by trades at our price; cancellations are split proportionally (see config). With only top-of-book data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Feed delay is drawn from an empirical table** (p50/p90/p99 measured on the owner's PC with `mm.data.latency`, smoothly varying so slow stretches come in bursts); order latency is a base value plus uniform jitter. The strategy sees archive events no earlier than their publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and same-millisecond events are processed before any order arriving in that millisecond.
3b. **Fill sizes are capped** (trade-through by print size, quote cross by displayed size), but a crossed quote can fill again on each later crossed quote. The 'Fills by mechanism' row shows how much of the result rests on through/cross fills; `no_cross_fills` tests it.
4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. Recent dates have trades only and cannot support a passive fill model.
5. **Taker fills (quote model)** assume liquidity beyond the touch comes in tiers as big as the touch, each a configured number of ticks worse; L2 data walks the real book.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, exchange outages, fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
