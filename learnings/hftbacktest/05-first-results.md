# 05. Results: hour 10, two full days, and the comparison with our engine

*Written 2026-10-30, updated the same day with final full-day results. Track: [hftbacktest/](README.md). Source of truth: `hft/results/COMPARISON_INPUT.md`. Runs: `hft/results/2026-10-24_glft_h10_base/`, `..._example_h10/`, `<day>_glft_full_{base,pess_latency,queue_prob3}/`. Engine: `reports/2026-10-30_glft_live/`. Metric formulas: `hft/postprocess.py`, same as our engine's [metrics glossary](../../src/mm/backtest/README.md).*

## Key ideas
- **Agreement:** both simulators show **negative markouts at 1, 10 and 60 s** (roughly −0.1 to −0.3 USDT/ETH) and **both lose money** on both days. **Adverse selection is confirmed independently.**
- **Disagreement:** hftbacktest fills **4×** more on 10-24 and **2.3×** more on clean 10-25, so it loses about 3× more.
- Cause 1, **risk logic**: the port has no spread/vol pause. **771 of 1,188** hft fills on 10-24 fall in the 14:00–16:00 crash.
- Cause 2, **fills are sweeps ~75 ticks out**, so the queue model barely matters (1,188 vs 1,190 fills).
- Cause 3, **modify vs replace**: order counts and fill rates are **not comparable**.
- Three bugs taught lessons: pending orders are live, `PartialFillExchange` crashes in 2.4.4, and a "pessimistic" variant was optimistic.

All runs: GLFT params from `glft_live.yaml` (σ 0.182, A 0.195 /s, k 3.80 /USDT, γ 0.686, Δ 0.01, Q 0.05 ETH), fitted on 10-24, so **10-24 is in-sample and 10-25 out-of-sample**. hftbacktest: risk_adverse queue, **NoPartialFillExchange**, recorded feed latency, order latency 160/240 ms unless stated.

---

## 1. Hour 10 of 2026-10-24 (rerun on the robust files, feed p50 309 ms)

| Metric | GLFT base |
|---|---|
| Fills / new orders / fill rate | 7 / 12 / 0.583 |
| Modifies / cancels / post-only rejects | 1,181 / 0 / 3 |
| Quote dist. bid / ask p50 (ticks) | 81.5 / 71.5 (mostly long, so the ask is closer) |
| Fill dist. from mid_before min/p50/max | 0.5 / **32.5** / 86.5 ticks |
| Spread capture / inventory PnL / fees | +0.027 / +0.060 / 0.033 USDT |
| **Net PnL** | **+0.055 USDT** |
| Markout 1 s / 10 s / 60 s (USDT/ETH) | −0.26 / −0.66 / +0.50 |
| Final / max inventory | +0.03 / 0.03 ETH |
| Wall time | 14.2 s incl. compile |

- The PnL identity holds: 0.027 + 0.060 = 0.088 gross; 0.088 − 0.033 = +0.055 net.
- **The profit is inventory luck**, not spread: we were long 0.03 ETH as the price rose. Spread capture alone (0.027) does not cover fees (0.033).
- Fill distance p50 32.5 ticks vs ~76 quoted: the **price walked to us**, and 1 s markouts are negative.
- Model check *(my rough calculation)*: λ(71.5 t) + λ(81.5 t) = 0.0129 + 0.0088 = 0.0217 /s, which is about **78 fills/h** expected vs **7** realised.
- Example MM (10 ticks from reservation): 378 fills, 3.78 ETH, fees 1.79, net **−2.66 USDT**. That is a gross loss of −0.23 /ETH before fees: near-touch quoting at VIP0 loses twice ([venues-and-instruments](../market-setup/venues-and-instruments.md)).

**Remember: one hour (7 fills) cannot rank anything. Use it to check the plumbing.**

---

## 2. Full days: engine vs hftbacktest

### Day 1: 2026-10-24 (in-sample; 14:15–15:30 crash, three book gaps)

| Metric | Engine | hft base |
|---|---|---|
| Fills / ETH | 289 / 2.64 | **1,188** / 11.88 |
| New orders / fill rate | 1,992 / 13.3% | 1,677 / 70.8% |
| Post-only rejects | 22 | 483 |
| Spread capture | +0.82 | +4.28 |
| Inventory PnL | −2.52 | −8.44 |
| Fees | 1.23 | 5.53 |
| **Net PnL (USDT)** | **−2.93** | **−9.68** |
| Markout 1 / 10 / 60 s (USDT/ETH) | −0.149 / −0.166 / −0.044 | −0.125 / −0.100 / −0.142 |
| Time at inventory limit | 0.001% | 0.66% |
| Max drawdown | 3.30 | 9.84 |
| Run time | ~12–15 min | **24 s** |

Variants: **pess_latency** (entry 300 ms): 1,208 fills, net −10.26. **queue_prob3**: 1,190 fills, net −9.77.

### Day 2: 2026-10-25 (out-of-sample, clean day)

| Metric | Engine | hft base |
|---|---|---|
| Fills / ETH | 274 / 2.71 | **636** / 6.36 |
| New orders / fill rate | 1,894 / 12.7% | 740 / 85.9% |
| Post-only rejects | 24 | 102 |
| Spread capture | +0.76 | +2.02 |
| Inventory PnL | −1.27 | −3.91 |
| Fees | 1.13 | 2.97 |
| **Net PnL (USDT)** | **−1.63** | **−4.86** |
| Markout 1 / 10 / 60 s (USDT/ETH) | −0.166 / −0.186 / −0.333 | −0.186 / −0.201 / −0.300 |
| Time at inventory limit | 3.9% | 2.0% |
| Max drawdown | 2.37 | 5.16 |

Variants: **pess_latency**: 674 fills, net −5.55. **queue_prob3**: 636 fills, net −4.90.

**Remember: different fill counts, same sign everywhere that matters.**

---

## 3. Why the numbers differ: the three big reasons

### 1. The port has no spread or vol pause
```
 fills per hour, hft base, 2026-10-24 (schematic)
 00 ─────────────────────── 14 ████████████████ 16 ──── 17 ─────── 24
                                  771 fills          (crash)
 hft fills outside 14:00–17:00 (21 hours): 288     engine, whole day: 289
```
- Our engine stops quoting when the spread blows out (1,036 pauses on 10-24), on vol spikes and on stale quotes. The port has only the inventory cap and gap pauses (decision (e)).
- In the crash the port kept quoting into a falling market, and every sweep filled it.
- Outside the crash window the two engines agree closely (288 vs 289). The engine's own fills by hour were not checked.
- On clean 10-25 hft still fills 2.3× as often, so reasons 2 and 3 (and the latency) are real too.

### 2. Fills are sweeps ~75 ticks out, so the queue barely matters
- At ~75 ticks the strategy is almost never at the front of a queue. It fills when a move **trades through** the level, which fills it **regardless of the queue** ([01 §7](01-how-hftbacktest-works.md)).
- Evidence: risk_adverse 1,188 fills vs power_prob(3) 1,190.
- So these variants **cannot** bracket our engine's `proportional` cancel rule. A queue test needs quotes at the touch.
- The fill model does differ: our engine allows partial fills and splits fills into queue / through / cross (81 / 195 / 13 on 10-24). hft runs all-or-nothing.

### 3. Modify vs replace makes order counts incomparable
| | Engine | hft port |
|---|---|---|
| Price requote | `ReplaceOrder` (~39k/day), always back of queue | `modify(px)` on an untouched order (~35k/day) |
| Size change / partial fill | `total_qty` in the replace | cancel, then a **new** order |
| "New orders" | 1,992 / 1,894 | 1,677 / 740 |
- The denominators count different things, so **13% vs 71–86% fill rate says nothing** about fill realism.
- The engine's low rate also includes rejects and 205 `unknown_order` fill races, which have no counterpart in hft.

### Smaller differences
- **Latency**: recorded feed p50 312 / 288 ms with bursts up to 16 s vs the engine's drawn 239 ms. pess_latency fills **more** and loses more, because stale quotes get swept: adverse selection again.
- **Post-only rejects** 483 vs 22: the port quotes on a book ~460 ms old (feed + entry) with no spread pause.
- **Mid before fill**: hft samples it every 100 ms, so in fast moves it lags. On 10-24, 11 fills have negative distance and 14 have > 200 ticks. This shifts the spread vs inventory split but **not net PnL**.

**Remember: the gaps come from risk overlays and order bookkeeping, not from a different view of the market.**

---

## 4. What agrees, and why it matters

| | 1 s | 10 s | 60 s | Net |
|---|---|---|---|---|
| 10-24 engine / hft | −0.149 / −0.125 | −0.166 / −0.100 | −0.044 / −0.142 | −2.93 / −9.68 |
| 10-25 engine / hft | −0.166 / −0.186 | −0.186 / −0.201 | −0.333 / −0.300 | −1.63 / −4.86 |

- Two independent codebases with different fill, queue and latency mechanics agree that **after a fill the mid moves against us by ~0.1–0.3 USDT/ETH**. Both lose money.
- On the clean day the per-ETH markouts nearly match (within ~0.03).
- Compare with what GLFT expects to earn: a ~76-tick half-spread minus a 47-tick fee leaves ~0.29 USDT/ETH of edge. The markouts eat most or all of it.
- Conclusion: **the losses are not an artefact of our engine.** The strategy is adversely selected at these parameters.
- Caveats: one day each, no confidence intervals, and 10-24 is dominated by the crash.

**Remember: when two independent simulators agree on the sign, believe the sign. Argue about the size later.**

---

## 5. Lessons from the bugs found

| Bug | What happened | Lesson |
|---|---|---|
| **Pending order treated as gone** | A new order is `status NONE (0), req = NEW` until its ack arrives (~400 ms). The first port treated that as terminal, forgot the order and quoted again: up to **22 live bids**, inventory 0.10 > Q, fills at −377 / +635 ticks. Fixed (`PENDING_NEW` in `glft_hft.py`); the runs now show max 1 live order per side. | **An order you sent is live until the exchange says otherwise.** Always log max live orders per side. |
| **`PartialFillExchange` error 14** | In 2.4.4, `hbt.elapse` returns error 14 after 25–60% of a day on our data, with any queue model, latency or requote mode, and then stays stuck. The runs are marked `*_INCOMPLETE`. | **Check the end code and coverage of every run** (`elapse_end_code`, `run_complete`). A silent half-day looks like a quiet day. |
| **"Pessimistic" that wasn't** | The first `pess` variant used power_prob(3). That model advances the queue on cancels, so it is **more optimistic** than risk_adverse. Renamed `queue_prob3`; `pess_latency` changes latency only. | **Change one knob per variant, and check which way each knob points.** |

**Remember: the first full-day numbers were wrong. Sanity counters (live orders, inventory vs Q, run completion) caught it.**

---

## Decisions log
- **2026-10-30.** First hour-10 runs recorded (later superseded).
- **2026-10-30.** Hour 10 rerun on the robust conversion. The earlier runs had used the old measured-offset files (feed p50 644 ms), so their numbers are void.
- **2026-10-30.** Full-day results for 10-24 and 10-25 final, with the comparison filled in. **NoPartialFillExchange is now the default** because partial fill crashes in 2.4.4 (decision (g) in [README.md](README.md#decisions-log)). Variants renamed: `pess_latency` (latency only) and `queue_prob3` (more optimistic queue).

---

## Check your understanding
1. hft has 1,188 fills on 10-24 and the engine has 289, yet you are told outside 14:00–17:00 they agree. What does that say about which feature of our engine matters most in a crash?
2. Why does swapping risk_adverse for power_prob(3) change fills by only 2 out of 1,188?
3. pess_latency (slower orders) got **more** fills and lost more. Explain with adverse selection.

*Answers on request.*
