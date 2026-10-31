# Backtest report: 2026-10-23_glft_v1_2

Strategy `glft`, params `{"sigma": 0.2500744437, "A": 0.3563803315, "k": 3.6661773601, "gamma": 0.2874652098, "xi_equals_gamma": true, "skew_at_Q_ticks": 25, "fee_mode": "exact", "skew_off": false, "maker_fee": 0.0002, "order_qty": 0.01, "max_inventory": 0.05, "min_notional_usdt": 20, "min_qty": 0.001, "min_half_spread_ticks": 10, "requote_ticks": 5, "urgent_requote_ticks": 15, "min_requote_interval_ms": 1000, "reject_cooldown_ms": 1000, "stale_quote_ms": 3000, "gap_cooldown_ms": 5000, "spread_pause_ticks": 20, "vol_pause_mult": 3.0, "vol_resume_mult": 2.0, "pause_resume_ms": 5000, "vol_sample_ms": 1000, "vol_ewma_halflife_s": 60, "vol_min_samples": 60, "max_drawdown_usdt": 5.0, "max_rejects_per_min": 20, "reject_window_s": 60, "unknown_order_grace_ms": 2000, "inventory_breach_mult": 1.5, "flatten_on_kill": true, "flatten_slip_ticks": 5, "flatten_ioc_attempts": 1, "flatten_alert_ms": 10000, "rate_orders_10s_max": 150, "rate_orders_1m_max": 600, "rate_ip_weight_1m_max": 1200, "max_modifies_per_order": 9000, "ufr_warn": 0.98, "print_stats_on_stop": true, "cal_sigma_dt_ms": 1000, "cal_sigma_check_dt_ms": 10000, "cal_lambda_dt_ms": 500, "cal_latency_ms": 400, "cal_delta_min_ticks": 10, "cal_delta_max_ticks": 150, "cal_delta_step_ticks": 5, "cal_max_trade_silence_ms": 10000, "cal_min_hits": 20, "cal_bootstrap": 200}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

## What was run

* **in_sample**: 2024-03-29 to 2024-03-29 (UTC, inclusive), 8,902,733 market events replayed. First to last event 24.0 h, of which **24.0 h actually covered by events** (0 silences over 60 s removed, 0.0 h).
* **out_of_sample**: 2024-03-30 to 2024-03-30 (UTC, inclusive), 6,677,660 market events replayed. First to last event 24.0 h, of which **24.0 h actually covered by events** (0 silences over 60 s removed, 0.0 h).

Fill model: `quote_trade` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | in_sample | out_of_sample |
|---|---|---|
| Net PnL (USDT) | -3.11 | -5.09 |
|   spread capture | 2.13 | 1.30 |
|   inventory PnL | 0.06 | -3.70 |
|   fees | -5.25 | -2.68 |
|   funding | -0.04 | -0.01 |
| Fills (maker / taker) | 812 (812 / 0) | 400 (399 / 1) |
| Traded notional (USDT) | 26,250 | 13,118 |
| New orders accepted / rejected | 1831 / 106 | 1046 / 58 |
| Replaces accepted / rejected | 75707 / 583 | 43257 / 248 |
| Fill ratio (new orders with a fill) | 40.6% | 35.2% |
| Fill ratio per action (new orders + replaces) | 1.0% | 0.8% |
| Fill ratio (qty filled / qty accepted) | 40.6% | 35.4% |
| Sharpe (annualised, 60s PnL buckets) | -31.52 | -63.41 |
| Max drawdown (USDT) | 3.72 | 5.09 |
| Mean abs inventory (ETH) | 0.019 | 0.016 |
| Max abs inventory (ETH) | 0.050 | 0.050 |
| Time at inventory limit | 3.7% | 4.0% |
| Final position (ETH) | 0.020 | 0.000 |
| Fills by mechanism (queue/through/cross/taker) | 17/728/67/0 | 5/365/29/1 |
| Net PnL if final inventory exited at the touch | -3.14 | -5.09 |
| t-stat of mean PnL per bucket | -1.65 | -3.32 |
| Risk-limit rejects | 0 | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.437 | -1.570 |
| Markout 1s per ETH (USDT, 95% bootstrap CI) | -0.2207 [-0.2597, -0.1830] | -0.2045 [-0.2695, -0.1396] |
| Mid move 10s after fill (bps, + = favourable) | -1.422 | -1.594 |
| Markout 10s per ETH (USDT, 95% bootstrap CI) | -0.2150 [-0.2961, -0.1257] | -0.2131 [-0.3436, -0.0880] |
| Mid move 60s after fill (bps, + = favourable) | -1.577 | -1.774 |
| Markout 60s per ETH (USDT, 95% bootstrap CI) | -0.2694 [-0.4655, -0.0750] | -0.2773 [-0.5482, -0.0095] |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Strategy's own counters (its `summary()`)

Pauses, kills, rejects by reason etc., as counted by the strategy itself. Full detail in `metrics.json` under `periods.<period>.strategy_summary`.

| strategy summary | in_sample | out_of_sample |
|---|---|---|
| pauses.stale | 2 | 4 |
| pauses.spread | 681 | 414 |
| pauses.vol | 2 |  |
| rejects.unknown_order | 570 | 241 |
| rejects.post_only_would_cross | 119 | 65 |
| fills.sell | 406 | 193 |
| fills.buy | 406 | 207 |
| placed.buy | 959 | 546 |
| placed.sell | 978 | 557 |
| replaced.buy | 38247 | 21539 |
| replaced.sell | 38043 | 21966 |
| mean_posted_delta_ticks.buy | 102.5 | 100.4 |
| mean_posted_delta_ticks.sell | 98.68 | 99.73 |
| frac_time_quoting.buy | 0.9318 | 0.9321 |
| frac_time_quoting.sell | 0.9318 | 0.9557 |
| flatten_orders | 0 | 1 |
| rate_throttled | 0 | 0 |
| modify_cap_cancels | 0 | 0 |
| race_explained | 570 | 241 |
| race_unexplained | 0 | 0 |
| flatten_failed_ticks | 0 | 0 |
| ufr_max | 1 | 1 |
| ufr_minutes_above | 1321 | 1092 |
| kills.drawdown |  | 1 |
| flat_after_kill_ms |  | 1000 |

Order counts: 'new orders' are fresh placements; 'replaces' are modifies of a live order (each re-queues the order at the back unless `exchange.replace_keeps_priority`). The fill ratio is taken over new orders; the per-action ratio over new orders plus replaces. The markout interval is a seeded bootstrap over fills (fills cluster in time, so it is optimistically narrow).

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**in_sample**

| variant | seeds | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | kills | time at limit | mean abs q (ETH) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 5 | -3.28 (0.30) | 2.20 (0.05) | -0.17 (0.28) | -5.27 (0.09) | 815 (12) | 7.5 (0.1) | 100% (0) | -1.382 (0.034) | 0/5 | 2.1% (0.9) | 0.019 (0.001) | 0.2 / +0.00; 6.7 / +2.19; 0.6 / +0.01; 0.0 / +0.00 |
| colocated | 1 | -1.69 | 1.75 | -1.15 | -2.20 | 325 | 3.1 | 100% | -1.862 | 0/1 | 4.9% | 0.021 | 0.0 / +0.00; 3.1 / +1.76; 0.0 / -0.00; 0.0 / +0.00 |
| pessimistic_fills | 1 | -3.30 | 2.18 | -0.20 | -5.24 | 817 | 7.4 | 100% | -1.388 | 0/1 | 3.0% | 0.020 | 0.0 / +0.00; 7.4 / +2.18; 0.0 / +0.00; 0.0 / +0.00 |
| no_cross_fills | 1 | -3.15 | 2.14 | -0.07 | -5.18 | 811 | 7.3 | 100% | -1.371 | 0/1 | 3.0% | 0.020 | 0.2 / +0.00; 7.2 / +2.14; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | 1 | -3.97 | 2.28 | -0.37 | -5.84 | 897 | 8.3 | 100% | -1.270 | 0/1 | 1.8% | 0.018 | 0.3 / +0.00; 7.1 / +2.26; 0.9 / +0.02; 0.0 / +0.00 |
| higher_latency | 1 | -4.89 | 1.85 | -1.10 | -5.62 | 875 | 7.9 | 99% | -1.090 | 1/1 | 0.7% | 0.012 | 0.3 / +0.00; 6.5 / +1.82; 1.1 / +0.02; 0.0 / -0.00 |
| pessimistic_all | 1 | -4.91 | 1.84 | -1.28 | -5.46 | 854 | 7.6 | 99% | -1.055 | 1/1 | 0.8% | 0.011 | 0.0 / +0.00; 7.6 / +1.84; 0.0 / +0.00; 0.0 / -0.00 |
| skew_off | 5 | -2.33 (0.51) | 1.74 (0.04) | 0.17 (0.49) | -4.17 (0.08) | 660 (14) | 5.9 (0.1) | 100% (0) | -1.372 (0.023) | 0/5 | 31.7% (3.5) | 0.032 (0.001) | 0.2 / +0.00; 5.2 / +1.73; 0.6 / +0.01; 0.0 / +0.00 |
| fee_shift | 5 | -2.06 (0.72) | 2.00 (0.02) | 0.92 (0.69) | -4.92 (0.09) | 776 (11) | 7.0 (0.1) | 100% (0) | -1.359 (0.061) | 0/5 | 22.9% (1.7) | 0.029 (0.001) | 0.2 / +0.00; 6.1 / +1.98; 0.6 / +0.02; 0.0 / +0.00 |
| gamma_low | 5 | -2.23 (0.39) | 2.04 (0.02) | 0.80 (0.36) | -5.00 (0.05) | 772 (8) | 7.1 (0.1) | 100% (0) | -1.389 (0.055) | 0/5 | 16.9% (1.2) | 0.027 (0.001) | 0.2 / +0.00; 6.3 / +2.01; 0.6 / +0.02; 0.0 / +0.00 |
| gamma_high | 5 | -4.31 (0.49) | 2.21 (0.07) | -1.29 (0.40) | -5.18 (0.10) | 809 (20) | 7.3 (0.1) | 100% (0) | -1.396 (0.049) | 1/5 | 0.1% (0.1) | 0.011 (0.000) | 0.2 / +0.00; 6.5 / +2.20; 0.6 / +0.02; 0.0 / +0.00 |

**out_of_sample**

| variant | seeds | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | kills | time at limit | mean abs q (ETH) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 5 | -4.98 (0.30) | 1.35 (0.12) | -3.57 (0.38) | -2.75 (0.21) | 409 (36) | 3.8 (0.3) | 99% (1) | -1.704 (0.068) | 4/5 | 3.5% (0.3) | 0.015 (0.001) | 0.0 / +0.00; 3.4 / +1.34; 0.3 / +0.01; 0.0 / +0.00 |
| colocated | 1 | -3.45 | 1.22 | -3.18 | -1.47 | 214 | 2.1 | 100% | -3.024 | 0/1 | 5.9% | 0.019 | 0.0 / +0.00; 2.0 / +1.23; 0.1 / -0.01; 0.0 / +0.00 |
| pessimistic_fills | 1 | -5.07 | 1.30 | -3.76 | -2.60 | 394 | 3.6 | 99% | -1.641 | 1/1 | 4.9% | 0.016 | 0.0 / +0.00; 3.6 / +1.30; 0.0 / +0.00; 0.0 / -0.00 |
| no_cross_fills | 1 | -5.07 | 1.28 | -3.66 | -2.69 | 407 | 3.7 | 99% | -1.673 | 1/1 | 4.1% | 0.016 | 0.1 / +0.00; 3.6 / +1.29; 0.0 / +0.00; 0.1 / -0.01 |
| latency_spikes | 1 | -3.85 | 1.63 | -2.01 | -3.46 | 522 | 4.9 | 100% | -1.519 | 0/1 | 3.5% | 0.016 | 0.1 / +0.01; 4.3 / +1.61; 0.5 / +0.01; 0.0 / +0.00 |
| higher_latency | 1 | -5.07 | 1.48 | -2.70 | -3.84 | 593 | 5.4 | 99% | -1.407 | 1/1 | 3.5% | 0.015 | 0.1 / +0.00; 4.6 / +1.46; 0.6 / +0.02; 0.0 / -0.00 |
| pessimistic_all | 1 | -5.11 | 1.40 | -2.87 | -3.64 | 558 | 5.1 | 99% | -1.480 | 1/1 | 3.5% | 0.015 | 0.0 / +0.00; 5.0 / +1.40; 0.0 / +0.00; 0.1 / -0.00 |
| skew_off | 5 | -2.79 (0.62) | 1.18 (0.05) | -1.64 (0.57) | -2.27 (0.07) | 348 (11) | 3.2 (0.1) | 100% (0) | -1.902 (0.083) | 0/5 | 35.0% (3.1) | 0.036 (0.001) | 0.1 / +0.00; 2.9 / +1.17; 0.2 / +0.00; 0.0 / +0.00 |
| fee_shift | 5 | -3.24 (0.47) | 1.43 (0.02) | -1.68 (0.46) | -2.97 (0.03) | 454 (5) | 4.2 (0.0) | 100% (0) | -1.682 (0.077) | 0/5 | 16.8% (2.4) | 0.029 (0.001) | 0.1 / +0.00; 3.8 / +1.42; 0.3 / +0.01; 0.0 / +0.00 |
| gamma_low | 5 | -4.15 (0.84) | 1.37 (0.17) | -2.66 (0.97) | -2.85 (0.32) | 432 (55) | 4.0 (0.5) | 99% (1) | -1.756 (0.119) | 2/5 | 11.2% (1.9) | 0.024 (0.003) | 0.1 / +0.00; 3.6 / +1.36; 0.3 / +0.01; 0.0 / +0.00 |
| gamma_high | 5 | -4.39 (0.39) | 1.61 (0.08) | -2.74 (0.46) | -3.26 (0.16) | 508 (27) | 4.6 (0.2) | 100% (0) | -1.684 (0.091) | 1/5 | 0.5% (0.3) | 0.011 (0.001) | 0.1 / +0.00; 4.1 / +1.59; 0.4 / +0.03; 0.0 / +0.00 |

Cells are mean (sd across seeds); the sd is blank with one seed. The base row averages the base seeds; the headline run (first table, charts) is the run.seed run alone.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 16 modules, combined SHA-256 `384ef8512bf095bbbdeeb255a82d7bbaa0321192e80a108ac5bdc8af63c9ee0b` (the git commit alone does not identify untracked code)
* git commit: `4b1640dee1582b0fde25f4f70db6ba0b51078556` (working tree has uncommitted changes)
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
8. **Replaces re-queue** (Binance reorders modified orders); `exchange.replace_keeps_priority: true` is the optimistic old rule. IOC orders never rest. A replace whose total_qty is already filled cancels the order (per the Binance docs; not checked on testnet).
