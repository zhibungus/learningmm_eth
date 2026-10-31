# synthetic example a

Strategy `example` on hftbacktest 2.4.4 (queue model `risk_adverse`, no-partial fills, order latency constant entry 160 ms / resp 240 ms).

## Results

- net_pnl_usdt: -0.299831
- fees_usdt: 0.090031
- num_trades: 18
- trading_volume_eth: 0.18
- final_position_eth: 0.04
- abs_position_max_eth: 0.05
- max_drawdown_usdt: 0.508963
- records: 1202
- wall_clock_s_incl_compile: 4.85
- data_span_s: 119.9
- gap_intervals_used: 0
- coverage_frac: 1.00167
- run_complete: True

## Assumptions that most limit realism

- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model (constant or interpolated jitter), not measured.
- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.
- Your own orders never move the market (no impact), and fills against your resting order happen at your price.
- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.
- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).
- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.
- One sample period; no confidence intervals. Research and education, not financial advice.
