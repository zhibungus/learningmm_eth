# glft / full_queue_prob3 / 2026-10-24T00 .. 2026-10-24T23

Strategy `glft` on hftbacktest 2.4.4 (queue model `power_prob`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- fill_rows: 1190
- filled_qty_eth: 11.9
- filled_notional_usdt: 27674.2
- orders_new: 1679
- orders_with_fill: 1190
- fill_rate_orders: 0.708755
- fill_rows_per_new_order: 0.708755
- cancels: 4
- modifies: 35368
- post_only_rejects: 483
- quote_cycles: 87471
- cycles_without_valid_book: 685
- submit_errors: 0
- elapse_end_code: 1
- quote_bid_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_ask_dist_ticks_min_p50_max: [50.49999999996544, 76.49999999998727, 101.50000000003274]
- quote_cycles_with_dist_below_floor: 0
- fill_dist_ticks_min_p50_max: [-162.49999999995453, 25.0, 546.4999999999691]
- final_position_eth: -0.04
- final_mid: 2357.64
- fees_usdt: 5.53485
- fees_check_from_fill_log: 5.53485
- net_pnl_usdt: -9.77235
- gross_pnl_usdt: -4.2375
- spread_capture_usdt: 4.31385
- inventory_pnl_usdt: -8.55135
- markout_1s_per_eth_mean: -0.12516
- markout_1s_total_usdt: -1.4894
- markout_1s_n: 1190
- markout_10s_per_eth_mean: -0.0991345
- markout_10s_total_usdt: -1.1797
- markout_10s_n: 1190
- markout_60s_per_eth_mean: -0.145013
- markout_60s_total_usdt: -1.72565
- markout_60s_n: 1190
- spread_capture_per_eth_mean: 0.362508
- inventory_mean_eth: -0.00225338
- inventory_std_eth: 0.0165005
- inventory_abs_max_eth: 0.05
- time_at_limit_frac: 0.00658566
- max_live_orders_bid: 1
- max_live_orders_ask: 1
- steps_with_more_than_one_live_order_on_a_side: 0
- duration_s: 86399.8
- max_drawdown_usdt: 9.92538
- sharpe_annualised_60s: -112.633
- state_num_trades: 1190
- state_trading_volume_eth: 11.9
- fill_log_matches_state_volume: True
- wall_clock_s_incl_compile: 24.8
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
