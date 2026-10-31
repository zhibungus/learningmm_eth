# glft / full_pess_latency / 2026-10-24T00 .. 2026-10-24T23

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 300 ms / resp 240 ms).

## Results

- fill_rows: 1208
- filled_qty_eth: 12.08
- filled_notional_usdt: 28098.1
- orders_new: 1702
- orders_with_fill: 1208
- fill_rate_orders: 0.709753
- fill_rows_per_new_order: 0.709753
- cancels: 4
- modifies: 35006
- post_only_rejects: 488
- quote_cycles: 87487
- cycles_without_valid_book: 685
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_ask_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-94.50000000001637, 21.500000000014552, 395.4999999999927]
- final_position_eth: -0.04
- final_mid: 2357.64
- fees_usdt: 5.61962
- fees_check_from_fill_log: 5.61962
- net_pnl_usdt: -10.2553
- gross_pnl_usdt: -4.6357
- spread_capture_usdt: 4.07125
- inventory_pnl_usdt: -8.70695
- markout_1s_per_eth_mean: -0.120091
- markout_1s_total_usdt: -1.4507
- markout_1s_n: 1208
- markout_10s_per_eth_mean: -0.159002
- markout_10s_total_usdt: -1.92075
- markout_10s_n: 1208
- markout_60s_per_eth_mean: -0.184346
- markout_60s_total_usdt: -2.2269
- markout_60s_n: 1208
- spread_capture_per_eth_mean: 0.337024
- inventory_mean_eth: -0.00081153
- inventory_std_eth: 0.0174222
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.00794099
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 86399.8
- max_drawdown_usdt: 10.4084
- sharpe_annualised_60s: -126.332
- state_num_trades: 1208
- state_trading_volume_eth: 12.08
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 24.04
- data_span_s: 86400.6
- feed_latency_ms: {'n_sampled': 6133783, 'min_ms': 1.0, 'p50_ms': 311.631945, 'p90_ms': 343.756973, 'p99_ms': 709.224807, 'max_ms': 16093.736109}
- gap_intervals_used: 7
- coverage_frac: 0.999991
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
