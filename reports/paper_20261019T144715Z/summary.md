# Paper-trading report: paper_20261019T144715Z

Strategy `demo_quoter`, params `{"half_spread_ticks": 2, "order_qty": 0.05, "max_position": 0.5, "requote_ticks": 1}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**

## What was run

* **paper**: live, started 20261019T144715Z to stopped 14:48:25Z (UTC, inclusive), 110,725 market events replayed, 0.0 h of data.
* PAPER RUN: live public data, simulated exchange; no real orders were sent.
* Wall time 70.0 s, 110,725 events, 0 feed drops/resyncs (resting orders are cancelled on each).

Fill model: `l2` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data 0 ms, orders 30 ms, reports 0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | paper |
|---|---|
| Net PnL (USDT) | -1.79 |
|   spread capture | 0.09 |
|   inventory PnL | -0.58 |
|   fees | -1.30 |
|   funding | -0.00 |
| Fills (maker / taker) | 63 (63 / 0) |
| Traded notional (USDT) | 6,519 |
| Orders accepted / rejected | 99 / 6 |
| Fill ratio (orders with a fill) | 54.5% |
| Fill ratio (qty filled / qty accepted) | 54.5% |
| Sharpe (annualised, 60s PnL buckets) | n/a |
| Max drawdown (USDT) | 1.79 |
| Mean abs inventory (ETH) | 0.212 |
| Max abs inventory (ETH) | 0.500 |
| Time at inventory limit | 14.7% |
| Final position (ETH) | 0.500 |
| Mid move 1s after fill (bps, + = favourable) | -1.393 |
| Mid move 10s after fill (bps, + = favourable) | -0.703 |
| Mid move 60s after fill (bps, + = favourable) | 3.023 |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Assumptions that most limit realism

1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue by trades at our price; cancellations are split proportionally (see config). With only top-of-book data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Latency is a fixed number** (plus optional jitter), not the real distribution; archive timestamps are millisecond resolution, so sub-ms ordering is unknowable.
4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. Recent dates have trades only and cannot support a passive fill model.
5. **Taker fills at the touch** (quote model) ignore depth beyond the top level except for a flat slippage penalty; L2 data walks the real book.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, exchange outages, fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
