# Backtest report: 2026-10-22_glft_v1_1

Strategy `glft`, params `{"sigma": 0.2500744437, "A": 0.3563803315, "k": 3.6661773601, "gamma": 0.2874652098, "xi_equals_gamma": true, "skew_at_Q_ticks": 25, "fee_mode": "exact", "skew_off": false, "maker_fee": 0.0002, "order_qty": 0.01, "max_inventory": 0.05, "min_notional_usdt": 20, "min_qty": 0.001, "min_half_spread_ticks": 10, "requote_ticks": 5, "urgent_requote_ticks": 15, "min_requote_interval_ms": 1000, "reject_cooldown_ms": 1000, "stale_quote_ms": 3000, "gap_cooldown_ms": 5000, "spread_pause_ticks": 20, "vol_pause_mult": 3.0, "vol_resume_mult": 2.0, "pause_resume_ms": 5000, "vol_sample_ms": 1000, "vol_ewma_halflife_s": 60, "vol_min_samples": 60, "max_drawdown_usdt": 5.0, "max_rejects_per_min": 20, "reject_window_s": 60, "unknown_order_grace_ms": 2000, "inventory_breach_mult": 1.5, "flatten_on_kill": true, "flatten_slip_ticks": 5, "flatten_max_attempts": 5, "rate_orders_10s_max": 150, "rate_orders_1m_max": 600, "rate_ip_weight_1m_max": 1200, "max_modifies_per_order": 9000, "ufr_warn": 0.98, "print_stats_on_stop": true, "cal_sigma_dt_ms": 1000, "cal_sigma_check_dt_ms": 10000, "cal_lambda_dt_ms": 500, "cal_latency_ms": 400, "cal_delta_min_ticks": 10, "cal_delta_max_ticks": 150, "cal_delta_step_ticks": 5, "cal_max_trade_silence_ms": 10000, "cal_min_hits": 20, "cal_bootstrap": 200}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

## What was run

* **in_sample**: 2024-03-29 to 2024-03-29 (UTC, inclusive), 8,902,733 market events replayed. First to last event 24.0 h, of which **24.0 h actually covered by events** (0 silences over 60 s removed, 0.0 h).
* **out_of_sample**: 2024-03-30 to 2024-03-30 (UTC, inclusive), 6,677,660 market events replayed. First to last event 24.0 h, of which **24.0 h actually covered by events** (0 silences over 60 s removed, 0.0 h).

Fill model: `quote_trade` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | in_sample | out_of_sample |
|---|---|---|
| Net PnL (USDT) | -3.10 | -5.10 |
|   spread capture | 2.21 | 1.30 |
|   inventory PnL | -0.07 | -3.76 |
|   fees | -5.21 | -2.63 |
|   funding | -0.04 | -0.01 |
| Fills (maker / taker) | 802 (802 / 0) | 392 (391 / 1) |
| Traded notional (USDT) | 26,035 | 12,904 |
| New orders accepted / rejected | 1843 / 100 | 1046 / 60 |
| Replaces accepted / rejected | 76667 / 576 | 43514 / 252 |
| Fill ratio (new orders with a fill) | 40.0% | 34.6% |
| Fill ratio per action (new orders + replaces) | 0.9% | 0.8% |
| Fill ratio (qty filled / qty accepted) | 40.0% | 34.7% |
| Sharpe (annualised, 60s PnL buckets) | -32.76 | -67.06 |
| Max drawdown (USDT) | 3.40 | 5.10 |
| Mean abs inventory (ETH) | 0.018 | 0.014 |
| Max abs inventory (ETH) | 0.050 | 0.050 |
| Time at inventory limit | 0.7% | 3.5% |
| Final position (ETH) | 0.020 | 0.000 |
| Fills by mechanism (queue/through/cross/taker) | 23/698/81/0 | 5/352/34/1 |
| Net PnL if final inventory exited at the touch | -3.14 | -5.10 |
| t-stat of mean PnL per bucket | -1.71 | -3.51 |
| Risk-limit rejects | 0 | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.450 | -1.606 |
| Markout 1s per ETH (USDT, 95% bootstrap CI) | -0.2118 [-0.2520, -0.1705] | -0.2107 [-0.2725, -0.1503] |
| Mid move 10s after fill (bps, + = favourable) | -1.412 | -1.721 |
| Markout 10s per ETH (USDT, 95% bootstrap CI) | -0.1980 [-0.2871, -0.1057] | -0.2514 [-0.3669, -0.1349] |
| Mid move 60s after fill (bps, + = favourable) | -1.469 | -2.127 |
| Markout 60s per ETH (USDT, 95% bootstrap CI) | -0.2183 [-0.4207, -0.0230] | -0.3945 [-0.6252, -0.1567] |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Strategy's own counters (its `summary()`)

Pauses, kills, rejects by reason etc., as counted by the strategy itself. Full detail in `metrics.json` under `periods.<period>.strategy_summary`.

| strategy summary | in_sample | out_of_sample |
|---|---|---|
| pauses.stale | 2 | 5 |
| pauses.spread | 682 | 416 |
| pauses.vol | 2 |  |
| rejects.unknown_order | 560 | 246 |
| rejects.post_only_would_cross | 116 | 66 |
| fills.sell | 398 | 188 |
| fills.buy | 404 | 204 |
| placed.buy | 965 | 554 |
| placed.sell | 978 | 550 |
| replaced.buy | 38764 | 21741 |
| replaced.sell | 38479 | 22025 |
| mean_posted_delta_ticks.buy | 101.6 | 100.1 |
| mean_posted_delta_ticks.sell | 100.3 | 100.2 |
| frac_time_quoting.buy | 0.9474 | 0.9363 |
| frac_time_quoting.sell | 0.9447 | 0.9567 |
| flatten_orders | 0 | 2 |
| rate_throttled | 0 | 0 |
| modify_cap_cancels | 0 | 0 |
| unknown_order_explained | 560 | 246 |
| unknown_order_unexplained | 0 | 0 |
| ufr_max | 1 | 1 |
| ufr_minutes_above | 1325 | 1098 |
| kills.drawdown |  | 1 |

Order counts: 'new orders' are fresh placements; 'replaces' are modifies of a live order (each re-queues the order at the back unless `exchange.replace_keeps_priority`). The fill ratio is taken over new orders; the per-action ratio over new orders plus replaces. The markout interval is a seeded bootstrap over fills (fills cluster in time, so it is optimistically narrow).

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**in_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -3.10 | 2.21 | -0.07 | -5.21 | 802 | 7.4 | 100% | -1.412 | 0.2 / +0.00; 6.4 / +2.19; 0.8 / +0.02; 0.0 / +0.00 |
| colocated | -1.69 | 1.75 | -1.15 | -2.20 | 325 | 3.1 | 100% | -1.862 | 0.0 / +0.00; 3.1 / +1.76; 0.0 / -0.00; 0.0 / +0.00 |
| pessimistic_fills | -3.59 | 2.20 | -0.62 | -5.12 | 801 | 7.3 | 100% | -1.386 | 0.0 / +0.00; 7.3 / +2.20; 0.0 / +0.00; 0.0 / +0.00 |
| no_cross_fills | -3.13 | 2.21 | 0.01 | -5.29 | 822 | 7.5 | 100% | -1.395 | 0.3 / +0.00; 7.2 / +2.21; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | -4.90 | 1.97 | -2.05 | -4.77 | 736 | 6.7 | 100% | -1.313 | 0.2 / +0.00; 5.7 / +1.96; 0.7 / +0.01; 0.0 / -0.00 |
| higher_latency | -4.87 | 1.81 | -1.33 | -5.36 | 829 | 7.5 | 100% | -1.067 | 0.2 / +0.00; 6.2 / +1.78; 1.1 / +0.03; 0.0 / -0.00 |
| pessimistic_all | -4.90 | 1.86 | -1.26 | -5.50 | 843 | 7.7 | 100% | -1.197 | 0.0 / +0.00; 7.7 / +1.86; 0.0 / +0.00; 0.0 / -0.00 |
| skew_off | -1.02 | 1.80 | 1.38 | -4.11 | 655 | 5.8 | 100% | -1.336 | 0.2 / +0.00; 5.1 / +1.79; 0.5 / +0.01; 0.0 / +0.00 |
| fee_shift | -2.64 | 2.03 | 0.34 | -4.94 | 782 | 7.0 | 100% | -1.346 | 0.1 / +0.00; 6.3 / +2.02; 0.6 / +0.01; 0.0 / +0.00 |
| gamma_low | -2.20 | 2.09 | 0.79 | -5.01 | 779 | 7.1 | 100% | -1.451 | 0.2 / +0.00; 6.3 / +2.07; 0.7 / +0.01; 0.0 / +0.00 |
| gamma_high | -4.27 | 2.17 | -1.25 | -5.14 | 809 | 7.3 | 100% | -1.485 | 0.1 / +0.00; 6.5 / +2.15; 0.6 / +0.02; 0.0 / +0.00 |

**out_of_sample**

| variant | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|
| base | -5.10 | 1.30 | -3.76 | -2.63 | 392 | 3.7 | 99% | -1.721 | 0.1 / +0.00; 3.3 / +1.29; 0.3 / +0.01; 0.1 / -0.00 |
| colocated | -3.45 | 1.22 | -3.18 | -1.47 | 214 | 2.1 | 100% | -3.024 | 0.0 / +0.00; 2.0 / +1.23; 0.1 / -0.01; 0.0 / +0.00 |
| pessimistic_fills | -5.08 | 1.32 | -3.75 | -2.66 | 398 | 3.7 | 99% | -1.640 | 0.0 / +0.00; 3.6 / +1.32; 0.0 / +0.00; 0.1 / -0.00 |
| no_cross_fills | -4.51 | 1.61 | -2.99 | -3.14 | 486 | 4.5 | 100% | -1.701 | 0.1 / +0.00; 4.3 / +1.61; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | -3.88 | 1.63 | -2.05 | -3.46 | 530 | 4.9 | 100% | -1.575 | 0.1 / +0.00; 4.3 / +1.62; 0.5 / +0.01; 0.0 / +0.00 |
| higher_latency | -3.46 | 1.31 | -1.57 | -3.20 | 484 | 4.5 | 100% | -1.400 | 0.1 / +0.00; 4.0 / +1.30; 0.4 / +0.01; 0.0 / +0.00 |
| pessimistic_all | -5.07 | 1.50 | -2.86 | -3.67 | 559 | 5.1 | 99% | -1.536 | 0.0 / +0.00; 5.1 / +1.50; 0.0 / +0.00; 0.0 / -0.00 |
| skew_off | -3.52 | 1.21 | -2.31 | -2.38 | 363 | 3.4 | 100% | -1.821 | 0.1 / +0.00; 3.1 / +1.21; 0.2 / +0.00; 0.0 / +0.00 |
| fee_shift | -3.14 | 1.44 | -1.56 | -3.01 | 455 | 4.3 | 100% | -1.688 | 0.1 / +0.00; 3.8 / +1.43; 0.4 / +0.01; 0.0 / +0.00 |
| gamma_low | -3.31 | 1.52 | -1.70 | -3.14 | 476 | 4.5 | 100% | -1.631 | 0.1 / +0.00; 4.0 / +1.50; 0.3 / +0.01; 0.0 / +0.00 |
| gamma_high | -4.09 | 1.67 | -2.43 | -3.33 | 524 | 4.7 | 100% | -1.643 | 0.1 / +0.00; 4.2 / +1.63; 0.4 / +0.04; 0.0 / +0.00 |

> WARNING (in_sample): net PnL is better WITH cross fills than without (`no_cross_fills`). Treat this result as suspect.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 16 modules, combined SHA-256 `100f12c207c41115e5009781ea7ff47c38be76a1910dd268b1b7af25195e3836` (the git commit alone does not identify untracked code)
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
8. **Replaces re-queue** (Binance reorders modified orders); `exchange.replace_keeps_priority: true` is the optimistic old rule. IOC orders never rest. Whether Binance rejects or cancels an order whose total_qty is already filled is not verified.
