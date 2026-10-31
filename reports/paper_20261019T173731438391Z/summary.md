# Paper-trading report: paper_20261019T173731438391Z

Strategy `demo_quoter`, params `{"half_spread_ticks": 2, "order_qty": 0.05, "max_position": 0.5, "requote_ticks": 1}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**

## What was run

* **paper**: live, started 20261019T173731438391Z to stopped 17:38:38Z (UTC, inclusive), 27,782 market events replayed, 0.0 h of data.
* PAPER RUN: live public data, simulated exchange; no real orders were sent.
* Engine clock = exchange time; simulated latency feed 240.0 ms, order 132.0 ms (+ jitter), events re-ordered with a 250.0 ms hold.
* Wall time 67.1 s, 27,782 events, 0 feed drops/resyncs (resting orders are cancelled on each).

Fill model: `l2` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data 240.0 ms, orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | paper |
|---|---|
| Net PnL (USDT) | -0.73 |
|   spread capture | 0.04 |
|   inventory PnL | -0.27 |
|   fees | -0.50 |
|   funding | -0.00 |
| Fills (maker / taker) | 44 (44 / 0) |
| Traded notional (USDT) | 2,517 |
| Orders accepted / rejected | 46 / 6 |
| Fill ratio (orders with a fill) | 45.7% |
| Fill ratio (qty filled / qty accepted) | 45.7% |
| Sharpe (annualised, 60s PnL buckets) | n/a |
| Max drawdown (USDT) | 0.73 |
| Mean abs inventory (ETH) | 0.145 |
| Max abs inventory (ETH) | 0.350 |
| Time at inventory limit | 0.0% |
| Final position (ETH) | 0.150 |
| Fills by mechanism (queue/through/cross/taker) | 5/36/3/0 |
| Net PnL if final inventory exited at the touch | -0.91 |
| t-stat of mean PnL per bucket | n/a |
| Risk-limit rejects | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.025 |
| Mid move 10s after fill (bps, + = favourable) | -1.227 |
| Mid move 60s after fill (bps, + = favourable) | -0.000 |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* git commit: `1b68ff078b9b6d57876c6490f57daa4d69646010` (working tree has uncommitted changes)
* data manifest: 0 input files with SHA-256 hashes and row counts are listed in `config.json` under `meta.manifest`.

## Assumptions that most limit realism

1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue by trades at our price; cancellations are split proportionally (see config). With only top-of-book data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Latency is a base value plus uniform jitter**, not the measured distribution (defaults come from measurements on the owner's PC); the strategy sees archive events no earlier than their publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and same-millisecond events are processed before any order arriving in that millisecond.
3b. **Fill sizes are capped** (trade-through by print size, quote cross by displayed size), but a crossed quote can fill again on each later crossed quote. The 'Fills by mechanism' row shows how much of the result rests on through/cross fills; `no_cross_fills` tests it.
4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. Recent dates have trades only and cannot support a passive fill model.
5. **Taker fills (quote model)** assume liquidity beyond the touch comes in tiers as big as the touch, each a configured number of ticks worse; L2 data walks the real book.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, exchange outages, fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
