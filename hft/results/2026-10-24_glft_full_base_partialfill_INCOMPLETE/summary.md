# glft / full_base_partialfill_INCOMPLETE / 2026-10-24T00 .. 2026-10-24T23

**INCOMPLETE RUN: covered only 59.7% of the data span (elapse end code 14). Numbers below are NOT comparable.**

Strategy `glft` on hftbacktest 2.4.4 (queue model `risk_adverse`, partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 213
- filled_qty_eth: 2.105
- filled_notional_usdt: 4966.41
- orders_new: 295
- orders_with_fill: 213
- fill_rate_orders: 0.722034
- fill_rows_per_new_order: 0.722034
- cancels: 0
- modifies: 16234
- post_only_rejects: 80
- quote_cycles: 51802
- cycles_without_valid_book: 5
- submit_errors: 0
- elapse_end_code: 14
- quote_bid_dist_ticks_min_p50_max: [56.49999999995998, 76.50000000003274, 101.50000000003274]
- quote_ask_dist_ticks_min_p50_max: [51.00000000002183, 76.49999999998727, 97.50000000003638]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-37.5, 24.50000000003456, 187.5]
- final_position_eth: -0.022
- final_mid: 2346.02
- fees_usdt: 0.965536
- fees_check_from_fill_log: 0.993283
- net_pnl_usdt: -1.67359
- gross_pnl_usdt: -0.70805
- spread_capture_usdt: 0.746015
- inventory_pnl_usdt: -1.45407
- markout_1s_per_eth_mean: -0.184083
- markout_1s_total_usdt: -0.387495
- markout_1s_n: 213
- markout_10s_per_eth_mean: -0.106703
- markout_10s_total_usdt: -0.222475
- markout_10s_n: 211
- markout_60s_per_eth_mean: -0.361382
- markout_60s_total_usdt: -0.699275
- markout_60s_n: 196
- spread_capture_per_eth_mean: 0.354401
- inventory_mean_eth: 0.00329112
- inventory_std_eth: 0.0149345
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.00153077
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 51608
- max_drawdown_usdt: 1.82996
- sharpe_annualised_60s: -57.6075
- state_num_trades: 213
- state_trading_volume_eth: 2.046
- fill_log_matches_state_volume: False
- wall_clock_s_incl_compile: 19.33
- data_span_s: 86400.6
- feed_latency_ms: {'n_sampled': 6133783, 'min_ms': 1.0, 'p50_ms': 311.631945, 'p90_ms': 343.756973, 'p99_ms': 709.224807, 'max_ms': 16093.736109}
- gap_intervals_used: 7
- coverage_frac: 0.597311
- run_complete: False

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
