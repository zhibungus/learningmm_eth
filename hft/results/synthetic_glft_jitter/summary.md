# synthetic glft jitter

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency intp_jitter entry 160 ms / resp 240 ms).

## Results

- fill_rows: 66
- filled_qty_eth: 0.66
- filled_notional_usdt: 1649.93
- orders_new: 68
- orders_with_fill: 66
- fill_rate_orders: 0.970588
- fill_rows_per_new_order: 0.970588
- cancels: 0
- modifies: 175
- post_only_rejects: 0
- quote_cycles: 172
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [63.49999999997635, 84.49999999997999, 100.50000000001091]
- quote_ask_dist_ticks_min_p50_max: [58.49999999995816, 74.50000000003456, 95.5000000000382]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [0.4999999999654392, 5.500000000029104, 17.50000000001819]
- final_position_eth: -3.46945e-18
- final_mid: 2499.9
- fees_usdt: 0.329986
- fees_check_from_fill_log: 0.329986
- net_pnl_usdt: -0.348386
- gross_pnl_usdt: -0.0184
- spread_capture_usdt: 0.0442
- inventory_pnl_usdt: -0.0626
- markout_1s_per_eth_mean: -0.824692
- markout_1s_total_usdt: -0.53605
- markout_1s_n: 65
- markout_10s_per_eth_mean: -0.0323333
- markout_10s_total_usdt: -0.0194
- markout_10s_n: 60
- markout_60s_per_eth_mean: -0.036875
- markout_60s_total_usdt: -0.0118
- markout_60s_n: 32
- spread_capture_per_eth_mean: 0.0669697
- inventory_mean_eth: 0.00340833
- inventory_std_eth: 0.0227808
- inventory_abs_max_eth: 0.04
- time_at_limit_frac: 0
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 120
- max_drawdown_usdt: 0.383444
- sharpe_annualised_60s: nan
- state_num_trades: 66
- state_trading_volume_eth: 0.66
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 0.01
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
