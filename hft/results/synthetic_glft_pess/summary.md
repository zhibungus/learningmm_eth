# synthetic glft pess

Strategy `glft` on hftbacktest 2.4.4 (queue model `power_prob`, no-partial fills, order latency constant entry 300 ms / resp 240 ms).

## Results

- fill_rows: 70
- filled_qty_eth: 0.7
- filled_notional_usdt: 1749.93
- orders_new: 72
- orders_with_fill: 70
- fill_rate_orders: 0.972222
- fill_rows_per_new_order: 0.972222
- cancels: 0
- modifies: 173
- post_only_rejects: 0
- quote_cycles: 183
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [63.49999999997635, 84.49999999997999, 105.5000000000291]
- quote_ask_dist_ticks_min_p50_max: [52.49999999996362, 74.50000000001182, 95.5000000000382]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [0.4999999999654392, 7.49999999998181, 22.50000000003638]
- final_position_eth: -3.46945e-18
- final_mid: 2500.05
- fees_usdt: 0.349986
- fees_check_from_fill_log: 0.349986
- net_pnl_usdt: -0.412386
- gross_pnl_usdt: -0.0624
- spread_capture_usdt: 0.0555
- inventory_pnl_usdt: -0.1179
- markout_1s_per_eth_mean: -0.813551
- markout_1s_total_usdt: -0.56135
- markout_1s_n: 69
- markout_10s_per_eth_mean: -0.165156
- markout_10s_total_usdt: -0.1057
- markout_10s_n: 64
- markout_60s_per_eth_mean: -0.0084375
- markout_60s_total_usdt: -0.0027
- markout_60s_n: 32
- spread_capture_per_eth_mean: 0.0792857
- inventory_mean_eth: 0.00506645
- inventory_std_eth: 0.0240931
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.0415282
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 120.4
- max_drawdown_usdt: 0.441446
- sharpe_annualised_60s: nan
- state_num_trades: 70
- state_trading_volume_eth: 0.7
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 0.01
- data_span_s: 119.9
- gap_intervals_used: 0
- coverage_frac: 1.00417
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
