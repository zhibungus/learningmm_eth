# glft / full_base / 2026-10-24T00 .. 2026-10-24T23

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 1188
- filled_qty_eth: 11.88
- filled_notional_usdt: 27628.3
- orders_new: 1677
- orders_with_fill: 1188
- fill_rate_orders: 0.708408
- fill_rows_per_new_order: 0.708408
- cancels: 4
- modifies: 35367
- post_only_rejects: 483
- quote_cycles: 87468
- cycles_without_valid_book: 685
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_ask_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-162.49999999995453, 24.50000000003456, 546.4999999999691]
- final_position_eth: -0.04
- final_mid: 2357.64
- fees_usdt: 5.52567
- fees_check_from_fill_log: 5.52567
- net_pnl_usdt: -9.68277
- gross_pnl_usdt: -4.1571
- spread_capture_usdt: 4.28005
- inventory_pnl_usdt: -8.43715
- markout_1s_per_eth_mean: -0.125253
- markout_1s_total_usdt: -1.488
- markout_1s_n: 1188
- markout_10s_per_eth_mean: -0.100404
- markout_10s_total_usdt: -1.1928
- markout_10s_n: 1188
- markout_60s_per_eth_mean: -0.141511
- markout_60s_total_usdt: -1.68115
- markout_60s_n: 1188
- spread_capture_per_eth_mean: 0.360274
- inventory_mean_eth: -0.00231682
- inventory_std_eth: 0.016471
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.00658566
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 86399.8
- max_drawdown_usdt: 9.83581
- sharpe_annualised_60s: -111.95
- state_num_trades: 1188
- state_trading_volume_eth: 11.88
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 24.21
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
