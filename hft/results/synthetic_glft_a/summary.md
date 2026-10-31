# synthetic glft a

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 64
- filled_qty_eth: 0.64
- filled_notional_usdt: 1599.92
- orders_new: 66
- orders_with_fill: 64
- fill_rate_orders: 0.969697
- fill_rows_per_new_order: 0.969697
- cancels: 0
- modifies: 181
- post_only_rejects: 0
- quote_cycles: 169
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [63.49999999997635, 84.49999999997999, 100.50000000001091]
- quote_ask_dist_ticks_min_p50_max: [58.49999999995816, 74.50000000003456, 95.5000000000382]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [0.4999999999654392, 5.500000000029104, 24.499999999989086]
- final_position_eth: -3.46945e-18
- final_mid: 2499.9
- fees_usdt: 0.319985
- fees_check_from_fill_log: 0.319985
- net_pnl_usdt: -0.371985
- gross_pnl_usdt: -0.052
- spread_capture_usdt: 0.0448
- inventory_pnl_usdt: -0.0968
- markout_1s_per_eth_mean: -0.834206
- markout_1s_total_usdt: -0.52555
- markout_1s_n: 63
- markout_10s_per_eth_mean: -0.152241
- markout_10s_total_usdt: -0.0883
- markout_10s_n: 58
- markout_60s_per_eth_mean: -0.032
- markout_60s_total_usdt: -0.0096
- markout_60s_n: 30
- spread_capture_per_eth_mean: 0.07
- inventory_mean_eth: 0.00418333
- inventory_std_eth: 0.0222636
- inventory_abs_max_eth: 0.04
- time_at_limit_frac: 0
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 120
- max_drawdown_usdt: 0.407043
- sharpe_annualised_60s: nan
- state_num_trades: 64
- state_trading_volume_eth: 0.64
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 4.57
- data_span_s: 119.9
- gap_intervals_used: 0
- coverage_frac: 1.00083
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
