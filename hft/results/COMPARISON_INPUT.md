# hftbacktest vs our engine: numbers for the comparison (GLFT, calibrated live params)

Same strategy parameters on both sides (`config/backtest/glft_live.yaml`, sigma 0.1819, A 0.1949, k 3.80, gamma 0.686, Q 0.05, delta 0.01).
Engine = `reports/2026-10-30_glft_live/` (in_sample 2026-10-24, out_of_sample 2026-10-25). hftbacktest = `hft/results/<day>_glft_full_*`.
Definitions are the same on both sides: spread capture = sum side*(mid before fill - px)*qty; inventory PnL = gross - spread capture;
net = inventory marked at mid, after maker fees; markout per ETH = qty-weighted mean of side*(mid(+h) - px); fill rate = new orders that got a fill / new orders (hft: orders_with_fill / orders_new).
Engine "new orders" = new orders accepted (1992 / 1894); its replaces (39480 / 38930) are separate and keep the order count; in hft a modify also keeps the id, a cancel + new is a new order.

## hftbacktest variants (all: risk_adverse queue unless stated, NoPartialFillExchange, feed latency = recorded data)
| name | queue | order latency entry/resp | note |
|---|---|---|---|
| base | risk_adverse | 160 / 240 ms | main hftbacktest number |
| pess_latency | risk_adverse | 300 / 240 ms | only latency is worse |
| queue_prob3 | power_prob(n=3) | 160 / 240 ms | MORE optimistic on queue than risk_adverse (advances on cancels); sits between risk_adverse and the engine's `cancel_assumption: proportional`. Not a pessimistic case |
| base + PartialFillExchange | risk_adverse | 160 / 240 ms | INCOMPLETE: `hbt.elapse` returns error 14 at 59.7% (10-24) and 25.2% (10-25) of the day and stays stuck; not comparable (folders `*_INCOMPLETE`) |

## Day 1: 2026-10-24 (engine "in_sample"; contains the 14:15-15:30 crash and three book gaps)
| metric | engine | hft base | hft pess_latency | hft queue_prob3 |
|---|---|---|---|---|
| Fills | 289 | 1188 | 1208 | 1190 |
| Filled qty (ETH) | 2.64 | 11.88 | 12.08 | 11.90 |
| New orders | 1992 | 1677 | 1702 | 1679 |
| Fill rate (orders with fill / new orders) | 13.3% | 70.8% | 71.0% | 70.9% |
| Post-only rejects | 22 | 483 | 488 | 483 |
| Spread capture (USDT) | +0.82 | +4.28 | +4.07 | +4.31 |
| Inventory PnL (USDT) | -2.52 | -8.44 | -8.71 | -8.55 |
| Fees (USDT) | 1.23 | 5.53 | 5.62 | 5.54 |
| Net PnL (USDT) | -2.93 | -9.68 | -10.26 | -9.77 |
| Markout 1 s per ETH (n) | -0.149 (289) | -0.125 (1188) | -0.120 (1208) | -0.125 (1190) |
| Markout 10 s per ETH | -0.166 | -0.100 | -0.159 | -0.099 |
| Markout 60 s per ETH | -0.044 | -0.142 | -0.184 | -0.145 |
| Max abs inventory (ETH) | 0.050 | 0.050 | 0.050 | 0.050 |
| Time at limit | 0.001% | 0.66% | 0.79% | 0.66% |
| Max drawdown (USDT) | 3.30 | 9.84 | 10.41 | 9.93 |
| Cycles skipped for gap/invalid book (1 s cycles) | 12 gap pauses (+1036 spread, 5 vol, 2 stale) | 685 | 685 | 685 |
| Run time | | 24 s | 24 s | 25 s |

## Day 2: 2026-10-25 (engine "out_of_sample"; clean day)
| metric | engine | hft base | hft pess_latency | hft queue_prob3 |
|---|---|---|---|---|
| Fills | 274 | 636 | 674 | 636 |
| Filled qty (ETH) | 2.71 | 6.36 | 6.74 | 6.36 |
| New orders | 1894 | 740 | 807 | 739 |
| Fill rate | 12.7% | 85.9% | 83.5% | 86.1% |
| Post-only rejects | 24 | 102 | 131 | 101 |
| Spread capture (USDT) | +0.76 | +2.02 | +2.02 | +2.03 |
| Inventory PnL (USDT) | -1.27 | -3.91 | -4.42 | -3.95 |
| Fees (USDT) | 1.13 | 2.97 | 3.15 | 2.97 |
| Net PnL (USDT) | -1.63 | -4.86 | -5.55 | -4.90 |
| Markout 1 s per ETH (n) | -0.166 (274) | -0.186 (636) | -0.179 (674) | -0.188 (636) |
| Markout 10 s per ETH | -0.186 | -0.201 | -0.163 | -0.202 |
| Markout 60 s per ETH | -0.333 | -0.300 | -0.290 | -0.297 |
| Max abs inventory (ETH) | 0.050 | 0.050 | 0.050 | 0.050 |
| Time at limit | 3.9% | 2.0% | 0.8% | 2.0% |
| Max drawdown (USDT) | 2.37 | 5.16 | 5.74 | 5.19 |
| Cycles skipped (gap/invalid book) | 0 gap pauses (934 spread, 3 vol) | 5 (start-up wait only) | 5 | 5 |

Hour 10 of 2026-10-24 (re-run on the corrected files, feed p50 309 ms): example MM net -2.66 USDT, 378 trades, max |q| 0.05; GLFT base 7 fills, net +0.055. One hour is too few fills to compare.

## Data used
Recorded feed latency (local_ts - exch_ts) p50/p99: 10-24 312/709 ms (max 16 s from the recorder's REST-probe bursts), 10-25 288/342 ms. Gaps: 10-24 has 7 `gaps.json` entries (book clears at 15:10, 15:30, 22:59-23:10 plus market-stream outage); all were treated as "cancel, stay out gap_cooldown_ms (5 s)". 10-25 has none.

## Where and why the two engines differ (read before trusting any ratio)
1. **Risk logic (largest cause of the fill-count gap, inferred from the fills by hour).** Our engine pauses quoting on spread blow-out (1036 / 934 pauses), vol spikes and stale quotes, and has a kill switch. The port has none of these, only the inventory cap and gap handling. On 10-24, 771 of 1188 hft fills fall in the 14:00-16:00 UTC crash where the engine was paused much of the time. Excluding 14:00-17:00 UTC, hft has 288 fills in the other 21 hours, close to the engine's 289 for the whole day (the engine's own fills by hour were not checked), so the crash window explains most of the 4x gap on 10-24. On the clean 10-25 hft still fills 2.3x as often (636 vs 274, with no crash), so fill-model and requote differences (items 2, 5, 6) are real too.
2. **Fill model.** Engine `l2`: queue at every level, `cancel_assumption: proportional`, partial fills (fills split queue/through/cross: 81/195/13). hftbacktest NoPartialFill + risk_adverse: an order fills in full when the price trades through it or the book crosses it, and queue position barely matters here: risk_adverse and power_prob(3) give 1188 vs 1190 fills. PartialFillExchange (closer to the engine) cannot run a full day in 2.4.4 (error 14).
3. **Queue.** Our quotes sit ~75 ticks from mid, so fills are sweeps through the level, not queue consumption; this is why the queue model is nearly irrelevant for hft and why the engine's `proportional` cancel assumption cannot be bracketed by these variants. A queue-sensitive comparison would need quotes at the touch.
4. **Latency.** Engine: market data from a sampled table (median 239 ms, p99 772 ms), orders 132 ms, reports 240 ms. hft: the recorded feed latency (median ~300 ms, bursts to 16 s), orders 160 ms entry (base) / 300 ms (pess_latency), response 240 ms. pess_latency fills slightly MORE (stale quotes get swept) and loses more, as expected for adverse selection.
5. **Replace vs cancel/new.** Engine ReplaceOrder (39k per day, always re-queues). hft requote = `modify(price)` on an untouched order (35k per day); size changes and partly filled orders use cancel then new. Order counts are therefore not comparable (hft 740-1700 new orders vs engine ~1900); the engine's low fill ratio comes from re-queueing and from rejects (`unknown_order` 205 = fill races).
6. **Post-only rejects.** hft 483 / 102 vs engine 22 / 24: the port quotes on a book that is ~460 ms old (feed + entry) and has no spread pause, so GTX orders are expired more often; the engine's 205 `unknown_order` races have no counterpart in hftbacktest.
7. **Gaps.** Engine `on_gap` / stale handling vs port: cancel everything, wait 5 s of valid book. Same intent; the engine also masks the 653 s outage from its time base.
8. **Mid at fill.** hft builds the mid-before-fill from the local book sampled every 100 ms and indexed by the exchange time of the newest feed row. In fast moves (>1 USD/s, latency bursts) that mid lags the exchange by hundreds of ticks: 11 of 1188 fills on 10-24 have fill distance < 0 and 14 have > 200 ticks (range -162 to +546 ticks); 10-25 has 3 below 0 and max 147. The engine uses its own pre-fill mid, so spread capture / inventory PnL split differ slightly even for identical fills; net PnL does not depend on it.
9. **Sizing and rounding.** Same Delta 0.01 / Q 0.05 / lot 0.001 / tick 0.01. The port skips the 20 USDT min-notional check (not binding at ~2350 USDT x 0.01 = 23.5).
10. **Fees.** Both maker 0.0002 of notional; hft fees reproduce hftbacktest's own state fee exactly.

## Reliability notes for this file
- The first full-day hft numbers (pess 1674 fills, inventory 0.10, fills at -377/+635 ticks) were produced by a bug in my port, fixed: a freshly submitted order has status NONE with req=NEW until acknowledged, and I treated that as terminal, so the strategy quoted extra orders (up to 22 live bids). The runs here show max one live order per side (`max_live_orders_*` = 1).
- Single day per row, no confidence intervals; 10-24 is dominated by the crash window.
