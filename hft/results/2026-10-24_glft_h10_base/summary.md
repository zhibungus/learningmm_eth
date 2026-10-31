# glft / h10_base / 2026-10-24T10 .. 2026-10-24T10

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 7
- filled_qty_eth: 0.07
- filled_notional_usdt: 165.591
- orders_new: 12
- orders_with_fill: 7
- fill_rate_orders: 0.583333
- fill_rows_per_new_order: 0.583333
- cancels: 0
- modifies: 1181
- post_only_rejects: 3
- quote_cycles: 3605
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [71.49999999996908, 81.50000000000546, 91.50000000004184]
- quote_ask_dist_ticks_min_p50_max: [61.49999999997817, 71.50000000001455, 81.50000000000546]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [0.5000000000109139, 32.500000000027285, 86.49999999997817]
- final_position_eth: 0.03
- final_mid: 2369.03
- fees_usdt: 0.0331182
- fees_check_from_fill_log: 0.0331182
- net_pnl_usdt: 0.0545318
- gross_pnl_usdt: 0.08765
- spread_capture_usdt: 0.02735
- inventory_pnl_usdt: 0.0603
- markout_1s_per_eth_mean: -0.257857
- markout_1s_total_usdt: -0.01805
- markout_1s_n: 7
- markout_10s_per_eth_mean: -0.662143
- markout_10s_total_usdt: -0.04635
- markout_10s_n: 7
- markout_60s_per_eth_mean: 0.495
- markout_60s_total_usdt: 0.03465
- markout_60s_n: 7
- spread_capture_per_eth_mean: 0.390714
- inventory_mean_eth: 0.00991944
- inventory_std_eth: 0.0105869
- inventory_abs_max_eth: 0.03
- time_at_limit_frac: 0
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 3599.8
- max_drawdown_usdt: 0.0998676
- sharpe_annualised_60s: 51.7432
- state_num_trades: 7
- state_trading_volume_eth: 0.07
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 14.22
- data_span_s: 3600.33
- feed_latency_ms: {'n_sampled': 216251, 'min_ms': 302.831803, 'p50_ms': 308.851406, 'p90_ms': 322.851047, 'p99_ms': 456.459504, 'max_ms': 2990.356065}
- gap_intervals_used: 7
- coverage_frac: 0.999853
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
