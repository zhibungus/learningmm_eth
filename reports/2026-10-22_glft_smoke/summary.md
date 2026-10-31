# Backtest report: 2026-10-22_glft_smoke

Strategy `glft`, params `{"sigma": 0.2500744437, "A": 0.3563679559, "k": 3.6661773601, "gamma": 0.2874549916, "xi_equals_gamma": true, "skew_at_Q_ticks": 25, "fee_mode": "exact", "skew_off": false, "maker_fee": 0.0002, "order_qty": 0.01, "max_inventory": 0.05, "min_notional_usdt": 20, "min_qty": 0.001, "min_half_spread_ticks": 10, "requote_ticks": 5, "urgent_requote_ticks": 15, "min_requote_interval_ms": 1000, "reject_cooldown_ms": 1000, "stale_quote_ms": 3000, "gap_cooldown_ms": 5000, "spread_pause_ticks": 20, "vol_pause_mult": 3.0, "vol_resume_mult": 2.0, "pause_resume_ms": 5000, "vol_sample_ms": 1000, "vol_ewma_halflife_s": 60, "vol_min_samples": 60, "max_drawdown_usdt": 5.0, "max_rejects_per_min": 20, "reject_window_s": 60, "inventory_breach_mult": 1.5, "flatten_on_kill": true, "flatten_slip_ticks": 5, "flatten_max_attempts": 5, "print_stats_on_stop": true, "cal_sigma_dt_ms": 1000, "cal_sigma_check_dt_ms": 10000, "cal_lambda_dt_ms": 500, "cal_latency_ms": 400, "cal_delta_min_ticks": 10, "cal_delta_max_ticks": 150, "cal_delta_step_ticks": 5, "cal_min_hits": 20, "cal_bootstrap": 200}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

## What was run

* **in_sample**: 2024-03-29 to 2024-03-29 (UTC, inclusive), 8,902,733 market events replayed, 24.0 h of data.
* **out_of_sample**: 2024-03-30 to 2024-03-30 (UTC, inclusive), 6,677,660 market events replayed, 24.0 h of data.

Fill model: `quote_trade` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | in_sample | out_of_sample |
|---|---|---|
| Net PnL (USDT) | -3.10 | -5.05 |
|   spread capture | 2.21 | 1.30 |
|   inventory PnL | -0.07 | -3.77 |
|   fees | -5.21 | -2.58 |
|   funding | -0.04 | -0.01 |
| Fills (maker / taker) | 802 (802 / 0) | 394 (394 / 0) |
| Traded notional (USDT) | 26,035 | 12,904 |
| Orders accepted / rejected | 78510 / 116 | 44559 / 66 |
| Fill ratio (orders with a fill) | 0.9% | 0.8% |
| Fill ratio (qty filled / qty accepted) | 0.9% | 0.8% |
| Sharpe (annualised, 60s PnL buckets) | -32.76 | -66.93 |
| Max drawdown (USDT) | 3.40 | 5.05 |
| Mean abs inventory (ETH) | 0.018 | 0.014 |
| Max abs inventory (ETH) | 0.050 | 0.050 |
| Time at inventory limit | 0.7% | 3.5% |
| Final position (ETH) | 0.020 | 0.000 |
| Fills by mechanism (queue/through/cross/taker) | 23/698/81/0 | 5/355/34/0 |
| Net PnL if final inventory exited at the touch | -3.14 | -5.05 |
| t-stat of mean PnL per bucket | -1.71 | -3.50 |
| Risk-limit rejects | 0 | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.450 | -1.606 |
| Mid move 10s after fill (bps, + = favourable) | -1.412 | -1.729 |
| Mid move 60s after fill (bps, + = favourable) | -1.469 | -2.125 |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**in_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -3.10 | 2.21 | -0.07 | -5.21 | 802 | 7.4 | 100% | -1.412 | 0.2 / +0.00; 6.4 / +2.19; 0.8 / +0.02; 0.0 / +0.00 |
| no_cross_fills | -3.47 | 2.25 | -0.43 | -5.22 | 809 | 7.4 | 100% | -1.395 | 0.2 / +0.00; 7.2 / +2.25; 0.0 / +0.00; 0.0 / +0.00 |

**out_of_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -5.05 | 1.30 | -3.77 | -2.58 | 394 | 3.7 | 100% | -1.729 | 0.1 / +0.00; 3.3 / +1.29; 0.3 / +0.01; 0.0 / +0.00 |
| no_cross_fills | -4.50 | 1.61 | -2.98 | -3.15 | 484 | 4.5 | 100% | -1.686 | 0.1 / +0.00; 4.3 / +1.61; 0.0 / +0.00; 0.0 / +0.00 |

> WARNING (in_sample): net PnL is better WITH cross fills than without (`no_cross_fills`). Treat this result as suspect.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 18 modules, combined SHA-256 `86befc2de2f110bcd71fffcf4ac56282a8353137f5ea5266fa0f0d5611d54933` (the git commit alone does not identify untracked code)
* git commit: `2de3d1a2e197620755d48159e023d08e315ee5e2` (working tree has uncommitted changes)
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
