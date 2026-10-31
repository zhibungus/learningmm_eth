# glft / full_base / 2026-10-25T00 .. 2026-10-25T23

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 636
- filled_qty_eth: 6.36
- filled_notional_usdt: 14860.6
- orders_new: 740
- orders_with_fill: 636
- fill_rate_orders: 0.859459
- fill_rows_per_new_order: 0.859459
- cancels: 0
- modifies: 31899
- post_only_rejects: 102
- quote_cycles: 86958
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [51.00000000002183, 71.50000000001455, 100.99999999997635]
- quote_ask_dist_ticks_min_p50_max: [50.49999999996544, 81.49999999995998, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-9.500000000025466, 22.499999999990905, 147.4999999999909]
- final_position_eth: 0.02
- final_mid: 2328.99
- fees_usdt: 2.97213
- fees_check_from_fill_log: 2.97213
- net_pnl_usdt: -4.86433
- gross_pnl_usdt: -1.8922
- spread_capture_usdt: 2.02195
- inventory_pnl_usdt: -3.91415
- markout_1s_per_eth_mean: -0.186132
- markout_1s_total_usdt: -1.1838
- markout_1s_n: 636
- markout_10s_per_eth_mean: -0.201203
- markout_10s_total_usdt: -1.27965
- markout_10s_n: 636
- markout_60s_per_eth_mean: -0.300197
- markout_60s_total_usdt: -1.90625
- markout_60s_n: 635
- spread_capture_per_eth_mean: 0.317917
- inventory_mean_eth: -0.00362083
- inventory_std_eth: 0.0197283
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.0202871
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 86399.8
- max_drawdown_usdt: 5.15957
- sharpe_annualised_60s: -75.7872
- state_num_trades: 636
- state_trading_volume_eth: 6.36
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 23
- data_span_s: 86399.7
- feed_latency_ms: {'n_sampled': 5903094, 'min_ms': 282.470099, 'p50_ms': 288.377655, 'p90_ms': 291.986888, 'p99_ms': 341.76475470000065, 'max_ms': 4740.856437}
- gap_intervals_used: 0
- coverage_frac: 1
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
