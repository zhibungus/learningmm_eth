# example / h10 / 2026-10-24T10 .. 2026-10-24T10

Strategy `example` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- net_pnl_usdt: -2.659
- fees_usdt: 1.7896
- num_trades: 378
- trading_volume_eth: 3.78
- final_position_eth: -3.46945e-18
- abs_position_max_eth: 0.05
- max_drawdown_usdt: 2.65921
- records: 35999
- wall_clock_s_incl_compile: 7.85
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
