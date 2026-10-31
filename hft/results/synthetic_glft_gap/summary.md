# synthetic glft gap

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 57
- filled_qty_eth: 0.57
- filled_notional_usdt: 1424.89
- orders_new: 61
- orders_with_fill: 57
- fill_rate_orders: 0.934426
- fill_rows_per_new_order: 0.934426
- cancels: 2
- modifies: 169
- post_only_rejects: 0
- quote_cycles: 167
- cycles_without_valid_book: 20
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [58.50000000000364, 79.50000000000728, 100.50000000001091]
- quote_ask_dist_ticks_min_p50_max: [58.49999999995816, 79.4999999999618, 100.50000000001091]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [1.499999999987267, 5.500000000029104, 24.499999999989086]
- final_position_eth: -0.01
- final_mid: 2499.93
- fees_usdt: 0.284978
- fees_check_from_fill_log: 0.284978
- net_pnl_usdt: -0.380528
- gross_pnl_usdt: -0.09555
- spread_capture_usdt: 0.03955
- inventory_pnl_usdt: -0.1351
- markout_1s_per_eth_mean: -0.808455
- markout_1s_total_usdt: -0.44465
- markout_1s_n: 55
- markout_10s_per_eth_mean: -0.3234
- markout_10s_total_usdt: -0.1617
- markout_10s_n: 50
- markout_60s_per_eth_mean: -0.105333
- markout_60s_total_usdt: -0.0316
- markout_60s_n: 30
- spread_capture_per_eth_mean: 0.069386
- inventory_mean_eth: 0.0021197
- inventory_std_eth: 0.0218796
- inventory_abs_max_eth: 0.04
- time_at_limit_frac: 0
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 120.3
- max_drawdown_usdt: 0.404838
- sharpe_annualised_60s: nan
- state_num_trades: 57
- state_trading_volume_eth: 0.57
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 0.01
- data_span_s: 119.9
- gap_intervals_used: 0
- coverage_frac: 1.00334
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
