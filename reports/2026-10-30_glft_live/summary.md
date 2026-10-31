# Backtest report: 2026-10-30_glft_live

Strategy `glft`, params `{"sigma": 0.1819074671, "A": 0.1948712852, "k": 3.8017552675, "gamma": 0.6858150359, "xi_equals_gamma": true, "skew_at_Q_ticks": 25, "fee_mode": "exact", "skew_off": false, "maker_fee": 0.0002, "order_qty": 0.01, "max_inventory": 0.05, "min_notional_usdt": 20, "min_qty": 0.001, "min_half_spread_ticks": 10, "requote_ticks": 5, "urgent_requote_ticks": 15, "min_requote_interval_ms": 1000, "reject_cooldown_ms": 1000, "stale_quote_ms": 3000, "gap_cooldown_ms": 5000, "spread_pause_ticks": 20, "vol_pause_mult": 3.0, "vol_resume_mult": 2.0, "pause_resume_ms": 5000, "vol_sample_ms": 1000, "vol_ewma_halflife_s": 60, "vol_min_samples": 60, "max_drawdown_usdt": 5.0, "max_rejects_per_min": 20, "reject_window_s": 60, "unknown_order_grace_ms": 2000, "inventory_breach_mult": 1.5, "flatten_on_kill": true, "flatten_slip_ticks": 5, "flatten_ioc_attempts": 1, "flatten_alert_ms": 10000, "rate_orders_10s_max": 150, "rate_orders_1m_max": 600, "rate_ip_weight_1m_max": 1200, "max_modifies_per_order": 9000, "ufr_warn": 0.98, "print_stats_on_stop": true, "cal_sigma_dt_ms": 1000, "cal_sigma_check_dt_ms": 10000, "cal_lambda_dt_ms": 500, "cal_latency_ms": 400, "cal_delta_min_ticks": 10, "cal_delta_max_ticks": 150, "cal_delta_step_ticks": 5, "cal_max_trade_silence_ms": 10000, "cal_min_hits": 20, "cal_bootstrap": 200}`. Seed 42. Symbol ETHUSDT (Binance USD-M perpetual). Research and education only; a backtest does not promise live results.

## What was run

* **in_sample**: 2026-10-24 to 2026-10-24 (UTC, inclusive), 24,633,737 market events replayed. First to last event 24.0 h, of which **23.8 h actually covered by events** (1 silences over 60 s removed, 0.2 h).
  * Warm-up: books fed from 2026-10-23 19:06:24.000 UTC, 5,998,452 events replayed but not traded; last depth snapshot 2026-10-23 19:06:24.874 UTC.
  * Data outages (silences over 60 s; the longest 1): 2026-10-24 22:59:32.557 to 2026-10-24 23:10:24.184 UTC (652 s).
* **out_of_sample**: 2026-10-25 to 2026-10-25 (UTC, inclusive), 22,185,895 market events replayed. First to last event 24.0 h, of which **24.0 h actually covered by events** (0 silences over 60 s removed, 0.0 h).
  * Warm-up: books fed from 2026-10-24 23:10:26.000 UTC, 692,882 events replayed but not traded; last depth snapshot 2026-10-24 23:10:26.214 UTC.

Fill model: `l2` (trade-through required: False, cancellations: proportional, queue factor: 1.0). Latency: market data drawn from table [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]] (probability, ms), orders 132.0 ms, reports 240.0 ms. Fees: maker 0.0002, taker 0.0005 (fractions of notional). Tick 0.01, lot 0.001.

## Results

In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, after parameters are frozen (never tune on it).

| metric | in_sample | out_of_sample |
|---|---|---|
| Net PnL (USDT) | -2.93 | -1.63 |
|   spread capture | 0.82 | 0.76 |
|   inventory PnL | -2.52 | -1.27 |
|   fees | -1.23 | -1.13 |
|   funding | -0.00 | 0.00 |
| Fills (maker / taker) | 289 (289 / 0) | 274 (274 / 0) |
| Traded notional (USDT) | 6,169 | 5,640 |
| New orders accepted / rejected | 1992 / 22 | 1894 / 24 |
| Replaces accepted / rejected | 39480 / 209 | 38930 / 194 |
| Fill ratio (new orders with a fill) | 13.3% | 12.7% |
| Fill ratio per action (new orders + replaces) | 0.6% | 0.6% |
| Fill ratio (qty filled / qty accepted) | 13.3% | 12.7% |
| Sharpe (annualised, 60s PnL buckets) | -38.36 | -22.55 |
| Max drawdown (USDT) | 3.30 | 2.37 |
| Mean abs inventory (ETH) | 0.014 | 0.019 |
| Max abs inventory (ETH) | 0.050 | 0.050 |
| Time at inventory limit | 0.0% | 3.9% |
| Final position (ETH) | -0.040 | 0.010 |
| Fills by mechanism (queue/through/cross/taker) | 81/195/13/0 | 91/174/9/0 |
| Net PnL if final inventory exited at the touch | -2.98 | -1.65 |
| t-stat of mean PnL per bucket | -2.01 | -1.18 |
| Risk-limit rejects | 0 | 0 |
| Mid move 1s after fill (bps, + = favourable) | -1.963 | -2.066 |
| Markout 1s per ETH (USDT, 95% bootstrap CI) | -0.1486 [-0.1870, -0.1110] | -0.1661 [-0.2219, -0.1160] |
| Mid move 10s after fill (bps, + = favourable) | -2.038 | -2.147 |
| Markout 10s per ETH (USDT, 95% bootstrap CI) | -0.1660 [-0.2645, -0.0586] | -0.1855 [-0.3204, -0.0563] |
| Mid move 60s after fill (bps, + = favourable) | -1.509 | -2.782 |
| Markout 60s per ETH (USDT, 95% bootstrap CI) | -0.0435 [-0.2780, +0.2155] (includes 0) | -0.3329 [-0.5780, -0.1140] |

How to read it: **spread capture** is what you earn by trading away from the mid; **inventory PnL** is what the position you were holding earned as the mid moved (adverse selection shows up as negative inventory PnL and negative 'mid move after fill'). The two add up to gross trading PnL; subtract fees and funding for net.

## Strategy's own counters (its `summary()`)

Pauses, kills, rejects by reason etc., as counted by the strategy itself. Full detail in `metrics.json` under `periods.<period>.strategy_summary`.

| strategy summary | in_sample | out_of_sample |
|---|---|---|
| pauses.spread | 1036 | 934 |
| pauses.vol | 5 | 3 |
| pauses.stale | 2 |  |
| pauses.gap | 12 |  |
| rejects.unknown_order | 205 | 189 |
| rejects.post_only_would_cross | 24 | 28 |
| rejects.awaiting_resync | 2 |  |
| fills.sell | 144 | 139 |
| fills.buy | 145 | 135 |
| placed.buy | 1010 | 943 |
| placed.sell | 1004 | 975 |
| replaced.buy | 19809 | 19524 |
| replaced.sell | 19880 | 19600 |
| mean_posted_delta_ticks.buy | 73.64 | 74.03 |
| mean_posted_delta_ticks.sell | 78.88 | 77.35 |
| frac_time_quoting.buy | 0.9044 | 0.9261 |
| frac_time_quoting.sell | 0.9043 | 0.9175 |
| flatten_orders | 0 | 0 |
| rate_throttled | 0 | 0 |
| modify_cap_cancels | 0 | 0 |
| race_explained | 205 | 190 |
| race_unexplained | 0 | 0 |
| flatten_failed_ticks | 0 | 0 |
| ufr_max | 1 | 1 |
| ufr_minutes_above | 1358 | 1406 |
| h2_predicted_fills | 1849 | 1980 |
| rejects.notional_below_min |  | 1 |

**Spec H2 (fitted fill rate vs realised): realised / predicted fills** = in_sample 0.156 (through-only 0.105; 289 fills vs 1849 predicted); out_of_sample 0.138 (through-only 0.088; 274 fills vs 1980 predicted). The hypothesis holds only inside [0.5, 2]; far below 0.5 means the model promises many more fills than a resting quote really gets.

Order counts: 'new orders' are fresh placements; 'replaces' are modifies of a live order (each re-queues the order at the back unless `exchange.replace_keeps_priority`). The fill ratio is taken over new orders; the per-action ratio over new orders plus replaces. The markout interval is a seeded bootstrap over fills (fills cluster in time, so it is optimistically narrow).

## Sensitivity (does the result survive worse assumptions?)

Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, the base result depended on optimistic assumptions. `colocated` is the old fast-network setting (10/20 ms) for comparison, NOT the base case.


**in_sample**

| variant | seeds | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | kills | time at limit | mean abs q (ETH) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 5 | -2.92 (0.30) | 0.84 (0.01) | -2.50 (0.29) | -1.26 (0.02) | 300 (8) | 2.7 (0.0) | 100% (0) | -1.919 (0.122) | 0/5 | 0.1% (0.1) | 0.014 (0.001) | 0.8 / +0.23; 1.8 / +0.59; 0.1 / +0.01; 0.0 / +0.00 |
| colocated | 1 | -2.25 | 0.46 | -2.41 | -0.30 | 69 | 0.6 | 100% | -3.455 | 0/1 | 4.9% | 0.016 | 0.2 / +0.16; 0.4 / +0.30; 0.0 / +0.00; 0.0 / +0.00 |
| pessimistic_fills | 1 | -2.90 | 0.76 | -2.50 | -1.16 | 261 | 2.5 | 100% | -1.958 | 0/1 | 0.0% | 0.015 | 0.0 / +0.00; 2.5 / +0.76; 0.0 / +0.00; 0.0 / +0.00 |
| no_cross_fills | 1 | -2.84 | 0.81 | -2.44 | -1.22 | 286 | 2.6 | 100% | -2.027 | 0/1 | 0.0% | 0.014 | 0.8 / +0.24; 1.8 / +0.57; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | 1 | -3.46 | 0.93 | -2.92 | -1.47 | 363 | 3.2 | 100% | -1.945 | 0/1 | 0.2% | 0.014 | 1.0 / +0.29; 2.0 / +0.62; 0.1 / +0.02; 0.0 / +0.00 |
| higher_latency | 1 | -4.56 | 1.11 | -3.54 | -2.13 | 526 | 4.6 | 100% | -1.681 | 0/1 | 0.3% | 0.015 | 1.6 / +0.36; 2.8 / +0.73; 0.1 / +0.02; 0.0 / +0.00 |
| pessimistic_all | 1 | -4.95 | 1.07 | -3.87 | -2.15 | 488 | 4.5 | 99% | -1.598 | 1/1 | 0.8% | 0.015 | 0.0 / +0.00; 4.5 / +1.07; 0.0 / +0.00; 0.0 / -0.00 |
| skew_off | 5 | -4.11 (0.64) | 0.54 (0.04) | -3.76 (0.65) | -0.90 (0.05) | 209 (18) | 1.9 (0.1) | 98% (2) | -1.949 (0.194) | 3/5 | 38.2% (3.0) | 0.033 (0.002) | 0.6 / +0.18; 1.2 / +0.35; 0.0 / +0.01; 0.0 / +0.00 |
| fee_shift | 3 | -3.92 (0.12) | 0.79 (0.00) | -3.48 (0.12) | -1.23 (0.02) | 290 (11) | 2.6 (0.0) | 100% (0) | -1.972 (0.043) | 0/3 | 13.8% (0.8) | 0.024 (0.001) | 0.8 / +0.24; 1.7 / +0.54; 0.1 / +0.01; 0.0 / +0.00 |
| gamma_low | 3 | -4.07 (0.29) | 0.77 (0.01) | -3.63 (0.31) | -1.21 (0.01) | 285 (5) | 2.6 (0.0) | 100% (0) | -1.967 (0.087) | 0/3 | 14.3% (0.7) | 0.025 (0.000) | 0.8 / +0.24; 1.7 / +0.52; 0.1 / +0.01; 0.0 / +0.00 |
| gamma_high | 3 | -2.24 (0.25) | 0.76 (0.02) | -1.81 (0.25) | -1.18 (0.02) | 278 (4) | 2.5 (0.0) | 100% (0) | -2.047 (0.143) | 0/3 | 0.0% (0.0) | 0.009 (0.000) | 0.7 / +0.20; 1.8 / +0.54; 0.1 / +0.01; 0.0 / +0.00 |

**out_of_sample**

| variant | seeds | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | maker share | 10s move (bps) | kills | time at limit | mean abs q (ETH) | qty / spread capture: queue; through; cross; taker |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 5 | -1.71 (0.26) | 0.75 (0.03) | -1.34 (0.23) | -1.13 (0.05) | 276 (13) | 2.4 (0.1) | 100% (0) | -2.152 (0.119) | 0/5 | 2.9% (0.9) | 0.019 (0.001) | 0.9 / +0.26; 1.5 / +0.47; 0.1 / +0.02; 0.0 / +0.00 |
| colocated | 1 | 0.16 | 0.52 | -0.00 | -0.36 | 77 | 0.8 | 100% | -3.622 | 0/1 | 5.9% | 0.026 | 0.3 / +0.22; 0.4 / +0.30; 0.0 / +0.00; 0.0 / +0.00 |
| pessimistic_fills | 1 | -1.18 | 0.75 | -0.83 | -1.10 | 258 | 2.4 | 100% | -2.043 | 0/1 | 3.2% | 0.020 | 0.0 / +0.00; 2.4 / +0.75; 0.0 / +0.00; 0.0 / +0.00 |
| no_cross_fills | 1 | -1.54 | 0.77 | -1.15 | -1.16 | 282 | 2.5 | 100% | -2.062 | 0/1 | 3.2% | 0.020 | 0.9 / +0.26; 1.5 / +0.51; 0.0 / +0.00; 0.0 / +0.00 |
| latency_spikes | 1 | -2.10 | 0.84 | -1.61 | -1.34 | 326 | 2.9 | 100% | -2.046 | 0/1 | 3.7% | 0.019 | 1.1 / +0.32; 1.6 / +0.49; 0.1 / +0.03; 0.0 / +0.00 |
| higher_latency | 1 | -3.36 | 0.99 | -2.33 | -2.01 | 510 | 4.3 | 100% | -1.947 | 0/1 | 2.7% | 0.016 | 1.6 / +0.32; 2.6 / +0.65; 0.1 / +0.01; 0.0 / +0.00 |
| pessimistic_all | 1 | -3.16 | 0.96 | -2.17 | -1.95 | 463 | 4.2 | 100% | -1.944 | 0/1 | 1.9% | 0.017 | 0.0 / +0.00; 4.2 / +0.96; 0.0 / +0.00; 0.0 / +0.00 |
| skew_off | 5 | -0.69 (0.24) | 0.44 (0.03) | -0.47 (0.26) | -0.66 (0.05) | 156 (13) | 1.4 (0.1) | 100% (0) | -2.029 (0.145) | 0/5 | 52.3% (3.4) | 0.039 (0.001) | 0.4 / +0.13; 0.9 / +0.30; 0.0 / +0.01; 0.0 / +0.00 |
| fee_shift | 3 | -1.60 (0.17) | 0.67 (0.03) | -1.29 (0.16) | -0.98 (0.02) | 233 (5) | 2.1 (0.1) | 100% (0) | -2.062 (0.082) | 0/3 | 26.3% (2.0) | 0.032 (0.001) | 0.7 / +0.24; 1.3 / +0.41; 0.1 / +0.02; 0.0 / +0.00 |
| gamma_low | 3 | -1.65 (0.07) | 0.67 (0.03) | -1.32 (0.05) | -1.00 (0.02) | 237 (10) | 2.1 (0.0) | 100% (0) | -2.034 (0.123) | 0/3 | 25.7% (3.0) | 0.032 (0.001) | 0.8 / +0.25; 1.3 / +0.40; 0.1 / +0.02; 0.0 / +0.00 |
| gamma_high | 3 | -1.46 (0.18) | 0.75 (0.01) | -1.07 (0.17) | -1.14 (0.01) | 269 (1) | 2.4 (0.0) | 100% (0) | -2.242 (0.018) | 0/3 | 0.0% (0.0) | 0.011 (0.000) | 0.8 / +0.22; 1.5 / +0.51; 0.1 / +0.01; 0.0 / +0.00 |

Cells are mean (sd across seeds); the sd is blank with one seed. The base row averages the base seeds; the headline run (first table, charts) is the run.seed run alone.

## Charts

* `equity_inventory.png`
* `markouts.png`

## Provenance

* source files that ran: 16 modules, combined SHA-256 `b3831674a6fd18d0d21e6a5094cfeaa419de7dc00fae247e2e7ba09f289f6905` (the git commit alone does not identify untracked code)
* git commit: `038433dc12d44954dc768bdbbcc99ea7c40cc8c5` (working tree has uncommitted changes)
* data manifest: 2106 input files with SHA-256 hashes and row counts are listed in `config.json` under `meta.manifest`.

## Assumptions that most limit realism

1. **Queue position is an estimate, from depth diffs.** The full order book (all recorded levels) comes from our own live recording, so we join behind the real displayed queue at our price. The queue then shrinks by trades at our price and by depth decreases that are not explained by trades; the feed shows only size changes, so how many of those cancels were ahead of us is assumed (`cancel_assumption`). Depth updates arrive in 100 ms batches, so order inside a batch is unknown.
2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.
3. **Feed delay is drawn from an empirical table** (p50/p90/p99 measured on the owner's PC with `mm.data.latency`, smoothly varying so slow stretches come in bursts); order latency is a base value plus uniform jitter. The strategy sees market events no earlier than their publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and same-millisecond events are processed before any order arriving in that millisecond.
3b. **Fill sizes are capped** (trade-through by print size, at-price fills by the queue left after the queue ahead). Our own size is tiny against the queues, so we do not model our effect on them. The 'Fills by mechanism' row shows how much rests on through/cross fills; `no_cross_fills` tests it.
4. **Only the days we recorded ourselves.** A few live days (see 'What was run') cannot show day-to-day or weekday/weekend variation, so one in-sample and one out-of-sample day support the sign and the mechanism, not a ranking of variants. Recorded outages are replayed as gaps (book cleared, orders cancelled), but anything the recorder missed is invisible. Receive times on the recording PC are not used; exchange timestamps are.
5. **Taker fills walk the real recorded book** level by level, with no impact on it, so large sweeps are not penalised beyond what the book showed.
6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, our own order connection going down (feed gaps in the data are replayed), fee tier changes, funding forecasts.
7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).
8. **Replaces re-queue** (Binance reorders modified orders); `exchange.replace_keeps_priority: true` is the optimistic old rule. IOC orders never rest. A replace whose total_qty is already filled cancels the order (per the Binance docs; not checked on testnet).
