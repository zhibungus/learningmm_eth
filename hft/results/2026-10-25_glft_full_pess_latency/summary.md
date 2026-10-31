# glft / full_pess_latency / 2026-10-25T00 .. 2026-10-25T23

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 300 ms / resp 240 ms).

## Results

- fill_rows: 674
- filled_qty_eth: 6.74
- filled_notional_usdt: 15750.8
- orders_new: 807
- orders_with_fill: 674
- fill_rate_orders: 0.835192
- fill_rows_per_new_order: 0.835192
- cancels: 0
- modifies: 31799
- post_only_rejects: 131
- quote_cycles: 86993
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [51.00000000002183, 76.49999999998727, 101.50000000003274]
- quote_ask_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-20.499999999992724, 20.000000000004547, 247.00000000002547]
- final_position_eth: 0.02
- final_mid: 2328.99
- fees_usdt: 3.15016
- fees_check_from_fill_log: 3.15016
- net_pnl_usdt: -5.55046
- gross_pnl_usdt: -2.4003
- spread_capture_usdt: 2.0225
- inventory_pnl_usdt: -4.4228
- markout_1s_per_eth_mean: -0.179451
- markout_1s_total_usdt: -1.2095
- markout_1s_n: 674
- markout_10s_per_eth_mean: -0.163101
- markout_10s_total_usdt: -1.0993
- markout_10s_n: 674
- markout_60s_per_eth_mean: -0.29003
- markout_60s_total_usdt: -1.9519
- markout_60s_n: 673
- spread_capture_per_eth_mean: 0.300074
- inventory_mean_eth: -0.0018793
- inventory_std_eth: 0.0190831
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.00801159
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 86399.8
- max_drawdown_usdt: 5.73688
- sharpe_annualised_60s: -83.3858
- state_num_trades: 674
- state_trading_volume_eth: 6.74
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 23.02
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
