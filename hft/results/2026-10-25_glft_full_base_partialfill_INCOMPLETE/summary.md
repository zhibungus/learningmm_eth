# glft / full_base_partialfill_INCOMPLETE / 2026-10-25T00 .. 2026-10-25T23

**INCOMPLETE RUN: covered only 25.2% of the data span (elapse end code 14). Numbers below are NOT comparable.**

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 43
- filled_qty_eth: 0.42
- filled_notional_usdt: 990.119
- orders_new: 55
- orders_with_fill: 43
- fill_rate_orders: 0.781818
- fill_rows_per_new_order: 0.781818
- cancels: 0
- modifies: 4439
- post_only_rejects: 10
- quote_cycles: 21843
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 14
- quote_bid_dist_ticks_min_p50_max: [51.49999999998727, 68.49999999999454, 98.50000000001273]
- quote_ask_dist_ticks_min_p50_max: [54.500000000007276, 84.49999999997999, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [0.5000000000109139, 22.499999999990905, 90.50000000002001]
- final_position_eth: 0.026
- final_mid: 2346.95
- fees_usdt: 0.195187
- fees_check_from_fill_log: 0.198024
- net_pnl_usdt: -0.151597
- gross_pnl_usdt: 0.04359
- spread_capture_usdt: 0.1751
- inventory_pnl_usdt: -0.13151
- markout_1s_per_eth_mean: -0.108286
- markout_1s_total_usdt: -0.04548
- markout_1s_n: 43
- markout_10s_per_eth_mean: -0.165571
- markout_10s_total_usdt: -0.06954
- markout_10s_n: 43
- markout_60s_per_eth_mean: 0.0885385
- markout_60s_total_usdt: 0.03453
- markout_60s_n: 40
- spread_capture_per_eth_mean: 0.416905
- inventory_mean_eth: -0.0146156
- inventory_std_eth: 0.0170941
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.0572804
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 21808.5
- max_drawdown_usdt: 0.371687
- sharpe_annualised_60s: -16.1606
- state_num_trades: 43
- state_trading_volume_eth: 0.414
- fill_log_matches_state_volume: False
- wall_clock_s_incl_compile: 15.47
- data_span_s: 86399.7
- feed_latency_ms: {'n_sampled': 5903094, 'min_ms': 282.470099, 'p50_ms': 288.377655, 'p90_ms': 291.986888, 'p99_ms': 341.76475470000065, 'max_ms': 4740.856437}
- gap_intervals_used: 0
- coverage_frac: 0.252414
- run_complete: False

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
