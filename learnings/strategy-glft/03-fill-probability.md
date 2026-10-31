# Lesson 3: Fill probability vs distance, `λ(δ) = A·e^(−kδ)`

*Written 2026-10-22. Track: [strategy-glft](00-choosing-the-strategy.md). Assumes [lesson 1](01-how-a-market-maker-earns-and-loses.md) §2 (sweep vs walk-down) and [lesson 2](02-inventory-risk-and-reservation-price.md). Fill rules and queues: [market-data.md §7](../market-setup/market-data.md), [paper-trading-and-simulation.md §3](../market-setup/paper-trading-and-simulation.md). Notation = [spec §3](../../specs/glft-perp.md). Synced with code 4b1640d / spec v1.2 on 2026-10-23.*

## Key ideas
- Quote further from mid → earn more per fill, but fill less often. GLFT models the rate as **`λ(δ) = A·e^(−kδ)`** fills per second, per side.
- **k** = how fast fills die off with distance. Calibrated **k = 3.666 /USDT = 0.0367 per tick**: every **18.9 ticks** further halves the fill rate.
- **A** = the curve's scale, extrapolated to δ = 0. Calibrated **A = 0.356 /s**. It is a fit constant, not "trades per second at mid".
- At our half-spread (100.5 ticks) the model says **0.0090 fills/s ≈ 32 per hour per side**, about one every 2 minutes.
- We measure δ from the **mid seen 400 ms earlier**, because that is the mid our quote was actually built on.
- Real flow is **bursty**, not Poisson: far hits are much more common than smooth diffusion predicts, and they cluster.

Numbers from [`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) (in-sample 2024-03-29) and [`fit_table.csv`](../../reports/2026-10-22_glft_perp_calibration/fit_table.csv). Units: δ in USDT (ticks for display), λ and A in **1/s**, k in **1/USDT**, latency and grid in **ms** in the config, converted to seconds in the maths.

---

## 1. The trade-off

**Intuition.** A bid 1 tick below mid gets hit all the time, but earns half a tick. A bid 200 ticks below earns a lot, but almost never fills. Somewhere between is the best distance. To find it, GLFT needs to know *how fast* the fill rate drops.

```
 fill rate λ (per s, per side)
 0.25 |*                                   10 t: 0.247 /s   (every 4 s)
      | *
 0.12 |   *                                30 t: 0.119 /s
      |     *
 0.06 |       *                            50 t: 0.057 /s
 0.03 |          *   <- fee distance c     70.7 t: 0.027 /s
 0.009|               *    <- h            100.5 t: 0.0090 /s (every 112 s)
 0.001|                          *  *      150 t: 0.0015 /s
      +----+----+----+----+----+----+--> δ (ticks)
          25   50   75  100  125  150
 earn per fill (δ − c) grows linearly →    fill rate falls exponentially ↓
```

**Remember: distance buys edge per fill and costs fill rate. λ(δ) is the exchange rate between them.**

---

## 2. The formula, A and k

```
 λ(δ) = A · e^(−k·δ)          [fills per second, per side]
 ln λ = ln A − k·δ            (a straight line on a log scale)
```

### k: the decay
- Units 1/USDT. Per tick: `k·θ = 3.666 × 0.01 = 0.0367`.
- One tick further: rate × `e^(−0.0367) = 0.964` (−3.6%).
- **Halving distance** `ln 2 / k = 0.189 USDT = 18.9 ticks`.
- **e-folding distance** `1/k = 0.273 USDT = 27.3 ticks`. This is (almost exactly) GLFT's `c1` from lesson 2 (0.2727). **The flatter the curve (small k), the wider GLFT quotes.**

### A: the scale
- `A = λ(0)`, the rate the curve would give at the mid. Units 1/s.
- **It is an extrapolation.** We only measured from 10 ticks out ([spec §6.2](../../specs/glft-perp.md) step 3). A 1-tick book has no quote at the mid.
- Compare: 921,565 aggTrades that day = 10.7 per second ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `diagnostics.n_trades`). A is 30x smaller, because it counts **windows with a trade-through of ≥ Δ**, not trades (see §4).
- Useful identity: `λ(c) = A·e^(−k·c) = A_eff = 0.0267 /s`. The `A_use` of lesson 2 is just **the fill rate at the fee distance**.

### Table (calibrated A, k)

| δ (ticks) | Model λ (/s) | Per hour | Mean wait `1/λ` | Measured λ̂ (/s) | Measured / model |
|---|---|---|---|---|---|
| 10 | 0.2470 | 889 | 4.0 s | 0.2480 | 1.00 |
| 30 | 0.1186 | 427 | 8.4 s | 0.1190 | 1.00 |
| 50 | 0.0570 | 205 | 17.5 s | 0.0535 | 0.94 |
| 70.7 (= c) | 0.0267 | 96 | 37.5 s | ≈0.025 (70-tick bin) | ≈0.92 |
| **100.5 (= h)** | **0.0090** | **32** | **112 s** | 0.0092 (100-tick bin) | ≈1.0 |
| 120 | 0.0044 | 16 | 228 s | 0.0051 | 1.18 |
| 150 | 0.0015 | 5.2 | 686 s | 0.0025 | **1.75** |

- R² = 0.996 ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `fit_weighted.r2`), yet the far tail is **fatter than exponential**: 75% more hits at 150 ticks than the line says. A single exponential bends the wrong way at the ends. That is why practitioners fit only where they quote (spec §6.2 caveats). We quote at ~75–125 ticks, where the fit is within ~25% (0.93 at 75 ticks, 1.25 at 125).

### Spec placeholders vs calibrated

| | Placeholder (spec §10, ETH 2,400) | Calibrated (ETH ≈ 3,536) |
|---|---|---|
| A | 4.0 /s | 0.356 /s |
| k | 10 /USDT (0.1 /tick) | 3.67 /USDT (0.037 /tick) |
| 1/k ≈ c1 | 10 ticks | 27.3 ticks |
| h at q = 0 | 60.6 ticks | 100.5 ticks |
| λ at h | 0.0093 /s | 0.0090 /s |

The real curve is much **flatter** (smaller k), so GLFT sits further out. The fill rate at h ends up similar.

**Remember: k sets the width (c1 ≈ 1/k). A mostly sets the skew (through A_eff in c2). Both are fitted, both are noisy.**

---

## 3. Poisson arrivals: from a rate to a probability

GLFT assumes fills at distance δ arrive as a **Poisson process** with rate λ(δ): independent, memoryless.

```
 P(at least one fill within t seconds) = 1 − e^(−λ·t)
 mean waiting time                     = 1/λ
```

At `h = 100.5 ticks`, λ = 0.0090 /s:

| Wait | P(filled) |
|---|---|
| 10 s | 8.6% |
| 60 s | 41.6% |
| 112 s (mean) | 63% |
| 5 min | 93% |

At q = +Q the ask sits at 75.5 ticks: λ = 0.0224 /s, mean wait **45 s**. That is the skew at work (lesson 2 §4).

**Remember: λ is a rate, not a probability. Turn it into a probability with 1 − e^(−λt).**

---

## 4. Market structure: what actually fills a far quote

### Who trades and how big
- Takers send market orders: impatient traders, hedgers, liquidations, arbitrageurs ([lesson 1 §1](01-how-a-market-maker-earns-and-loses.md)).
- Average ETHUSDT perp trade ≈ **0.78 ETH** (2026 snapshot, [facts doc](../../research/binance-eth-spot-vs-perp-facts.md)). Our Δ = 0.01 ETH. **Any single print through our price fills us completely**, so the "≥ Δ" test is almost "any print through".
- One aggTrade row = one taker order at one price ([market-data.md §2](../market-setup/market-data.md)). A sweep through 5 levels leaves 5 rows.

### Depth: why sweeps rarely reach 100 ticks
```
 price (mid 3535.70)       size
 3535.71   ask            ~200 ETH   <- top level often holds 100s of ETH
   ...      99 levels, each with depth
 3534.70  [ OUR BID 0.01 ]           <- 100 ticks below mid
```
- To reach our bid in one go, a seller must eat every level between. With hundreds of ETH near the touch, that takes an enormous order.
- So most far fills are **walk-downs** (lesson 1 §2 case b): the mid drifts to our quote, then an ordinary print goes through.

### Bursty flow
- Flow clusters: one big move triggers stops, liquidations and arbitrage, which trigger more. Quiet minutes follow busy ones.
- Evidence 1, **far tail** (my rough calculation): over the 0.9 s from seeing the mid to the window's end, σ = 0.25 gives a 1σ move of 0.24 USDT. A smooth Gaussian would reach 100 ticks (4.2σ) in about **2 in 100,000** windows. Measured: 874 bid hits / 172,782 windows = **0.5%**, about 200x more. Jumps and sweeps, not diffusion.
- Evidence 2, **grid sensitivity**: the same data fitted on 100 / 500 / 1000 ms windows gives λ(10 ticks) ≈ **0.39 / 0.25 / 0.19 /s** ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `diagnostics.grid_variants`). A Poisson process would give nearly the same number on every grid (≈0.26 / 0.25 / 0.23). Coarse windows count a burst only once. *(My reading; spec H2 tests it formally.)* k moved only −10% to +3%, inside H2's 50% tolerance. **k is robust; A is soft.**

### The queue
- Our quotes sit ~75–125 ticks deep. Nothing in L1 data tells us the queue there. The engine assumes it is **infinite**, so a deep order fills **only on trade-through** (`fills.py` `QuoteTradeFillModel.on_new_resting` sets the queue to `INF`).
- Once the mid walks to us and our price becomes the touch, the engine counts **all displayed size at that price as ahead of us** (conservative). Joining early would help in reality, but **every replace re-queues** the order (Binance, and our engine with `exchange.replace_keeps_priority: false`), and we replace each side about every 2 s.
- The calibration counts **strict** trade-throughs only, the conservative rule ([paper-trading §3](../market-setup/paper-trading-and-simulation.md)).
- v1.1 backtest, in-sample fills by mechanism: **698 through, 81 cross, 23 queue** out of 802 ([`summary.md`](../../reports/2026-10-22_glft_v1_1/summary.md)). The calibration describes the dominant 87%.

**Remember: a far quote fills because the price came to it, often in a burst. The fitted curve is a summary of that, not a picture of smooth random arrivals.**

---

## 5. Measuring λ(δ) against the mid seen 400 ms earlier

### Why not the current mid?
Our quote is built from a mid we saw late, and reaches the book later still:

```
 exchange time ─────────────────────────────────────────────────────────────>
 t_i                 t_i + 400 ms                          t_i + 900 ms
  │ mid m_i seen      │ order is live                          │
  │ (feed ~240 ms     │<───────── window W_i (500 ms) ────────>│
  │  + order ~160 ms) │  count sell prints strictly below b_ij │
  └── we price the bid at b_ij = floor(m_i − δ_j)
```
- L = `cal_latency_ms = 400` = feed p50 ≈ 240 ms + order ≈ 132 ms + jitter up to 59 ms (`config.py` `DEFAULTS["latency"]`; spec §6.2 step 2).
- **The question we need answered:** "If I post at δ from the mid I *know*, how often am I filled?" δ is the only thing the strategy controls, and it sets δ relative to `m_i`.

### Worked example
```
 t_i      m_i = 3535.70   → our bid at δ = 50 ticks: floor(3535.20) = 3535.20
 +600 ms  mid has walked to 3535.25
 +700 ms  a 0.3 ETH sell prints at 3535.19   (strictly below 3535.20)
```
| Measured against | Distance at fill | Counted in bin |
|---|---|---|
| **Mid seen at t_i (ours)** | 50 ticks | 50-tick bin ✓ (what we posted) |
| Current mid at fill | 6 ticks | ~5–10-tick bin |

- Current-mid measurement would move walk-down fills into the near bins. Far bins would look emptier, **k would come out larger**, and the model would under-predict fills at the distances we actually post.
- The lag also rules out **look-ahead**: `m_i` is the last quote at or before `t_i` (`glft_calibrate.py` `mid_on_grid`), and the window starts after we could have acted.
- This deliberately departs from the textbook "fills at distance δ from the current mid" ([spec §13](../../specs/glft-perp.md) row 5).
- *Simplification:* the live strategy requotes only when ≥ 5 ticks off and at most once a second ([spec §4.6](../../specs/glft-perp.md)), so real quotes are often staler than 400 ms (spec §8, "continuous re-pegging" row).

**Remember: measure fills the way your orders experience them: from the stale mid you priced on, in the window your order is actually live.**

---

## 6. In the code: how λ̂(δ) is measured and fitted

`lambda_curve(m, p, cal)` in `src/mm/strategies/glft_calibrate.py`, in execution order. The `cal_*` settings now live in their own `CalParams` dataclass (same YAML section as the strategy params).

| Step | What | Code (inside `lambda_curve`) | In-sample value |
|---|---|---|---|
| 1 | Grid step and latency, ms → ns | `step, lat = ...` | 500 ms, 400 ms |
| 2 | Distance bins 10, 15, …, 150 ticks | `deltas = np.arange(...)` | 29 bins |
| 3 | Grid times `t_i` whose window fits in the day | `grid = np.arange(...)` | |
| 4 | `m_i` = last mid at or before `t_i` | `mid_on_grid` | |
| 5 | Valid = have a mid and no quote silence > 2 s in the window | `silence_ranges`, `overlaps_gap` | |
| 5b | **New:** also drop windows overlapping a trade-stream outage: a break in the aggregate `trade_id` (≠ previous + 1), or no trade for > `cal_max_trade_silence_ms` = 10 s | `trade_gap_ranges` | 0 gaps, 0 steps masked → **172,782** valid = 24.0 h |
| 6 | Window of each trade: `(t − start − L − 1) // step` | `win = ...` | |
| 7 | Per window, `p*` = price where cumulative sell size (lowest price first) reaches Δ | inner function `p_star` | |
| 8 | Hypothetical prices with the strategy's rounding: bid down, ask up | `b_tick`, `a_tick` | |
| 9 | Bid hit ⇔ `b > p*` (≥ Δ sold strictly below b) | `hit_b`, `hit_a` | |
| 10 | Hits per hour (for the bootstrap) | `hb`, `ha`, `hv` | |

Why step 5b: a missing trade would read as "no hit" and bias λ̂ down. 10 s, not the 2 s quote rule, because short trade silences are real quiet market (spec §6.2). Effect on 2024-03-29: nothing masked, so A = 0.356368 as before. A first version checked *raw* `first_id`/`last_id` continuity. Binance skips raw ids, so that flagged 5 false gaps; **check the counter that is meant to be continuous** (spec v1.2.1 §6.2). Our own 2026-10-24 recording had 1 real gap (a 653 s network outage).

*Different day, different numbers:* calibrating on that 2026-10-24 recording gives σ 0.182, A 0.195 /s, k 3.80 /USDT, γ 0.686 ([`glft_live_calibrated.yaml`](../../config/strategies/glft_live_calibrated.yaml)). ETH ≈ 2,356, so the fee is 47.1 ticks and h = 75.9 ticks. **Parameters are regime-specific**, which lesson 6 covers.

Why steps 7–9 look odd: instead of looping over 172,782 windows × 29 bins and summing trades, the code finds **one crossing price per window** and compares every bin with it at once (numpy broadcasting, an N × 29 table). Same answer, seconds instead of minutes.

Then `fit_curve` (same file):

```
 λ̂_side(δ_j) = hits / (N_valid · Δt)              lam_b, lam_a = hb / (n_valid * dt_s), ...
 at 10 ticks:  bid 21,251 / (172,782 × 0.5 s) = 0.2460 /s
               ask 21,592 / 86,388 s          = 0.2499 /s
               average                         = 0.2480 /s     (fit_table.csv line 2)
 keep bins with ≥ 2·20 hits                        keep = w >= 2 * cal.cal_min_hits   (all 29 kept; fit range 10–150)
 WLS of ln λ̂ on δ, weight = hits                   wls_fit
 k = −slope = 3.666,  A = e^intercept = 0.356
```
- **Log-linear fit**: an exponential becomes a straight line, so the many near-touch hits don't swamp the far bins.
- **Weights = hit counts**: a bin with n hits has `Var(ln λ̂) ≈ 1/n`. Trust busy bins more.
- **Saturation**: at most one hit per 500 ms window, so λ̂ ≤ 2 /s. At 10 ticks only 12% of windows are hit, so the cap barely bites.

Robustness checks the code runs (all passed, [`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `diagnostics`):

| Check | Result | Gate (spec §6.2 step 9, stop conditions in `calibrate`) |
|---|---|---|
| Weighted R² | 0.996 | ≥ 0.9 ✓ |
| Fit range end | 150 ticks | ≥ 60 ✓ |
| Bid-side k vs ask-side k | 3.547 vs 3.796 (6.8% apart) | ≤ 30% ✓ |
| Unweighted OLS | A = 0.298, k = 3.360 | info |
| Hour-block bootstrap 95% CI | A 0.322–0.398, k 3.32–4.14 | info |
| 100 / 1000 ms grids | k = 3.79 / 3.30 | H2: k within 50% ✓ |

Honest caveats:
- **Bins are nested** (a hit at 50 ticks is also a hit at 45), so the WLS error bars are too small. Hence the hour-block bootstrap (`bootstrap` in `glft_calibrate.py`).
- **One day.** A Friday. The bootstrap CI is only within-day uncertainty.
- **First reality check (rough):** the backtest had 802 fills in 24 h in-sample, ≈ 17 per side per hour, vs the model's 32 at q = 0 ([`summary.md`](../../reports/2026-10-22_glft_v1_1/summary.md)).
- **The proper test, H2, is falsified.** Realised fills ÷ model fills integrated over each live quote's real distance and time = **0.44 in-sample, 0.28 out-of-sample** (stable over 5 seeds), outside [0.5, 2]. So we fill at less than half the rate the curve promises ([review](../../reviews/2026-10-22_glft_v1_1.md); likely causes in spec §9).

**Remember: λ̂ = hits ÷ seconds watched, per distance; fit a straight line to its log; then distrust the line's tails and its error bars.**

---

## Decisions log
- None yet. The measurement choices (400 ms lag, 500 ms grid, 10–150-tick range, strict trade-through) are spec defaults, not owner decisions.
- **2026-10-23. Sync with code 4b1640d / spec v1.2 (no owner decision).** Line-number citations replaced by function names (`lambda_curve`, `trade_gap_ranges`, `fit_curve`, `CalParams`, …). §6: added the new trade-outage mask (step 5b); valid steps 172,782 → 172,776, which leaves every rounded number unchanged. §4: queue bullet corrected (engine counts all displayed size ahead; replaces re-queue). §6: added the H2 result (0.44 / 0.28). Fill counts now cite the v1.1 report (same 802 fills as the smoke run).
- **2026-10-30. Gap-mask fix (spec v1.2.1 §6.2, no owner decision).** Step 5b uses aggregate `trade_id` continuity; the raw-id rule had flagged 5 false gaps. Valid steps back to 172,782, A = 0.356368 (the 172,776 / 0.356380 figures in the 2026-10-23 entry are void). Added the 2026-10-24 live calibration for contrast.

---

## Check your understanding
1. With A = 0.356 /s and k = 3.666 /USDT, what is the model fill rate at 60 ticks, and the probability of at least one fill in the next 60 s?
2. Suppose we had measured δ against the mid at the moment of each trade. Would the fitted k come out larger or smaller? What would that do to GLFT's half-spread (`c1 ≈ 1/k`), and why would it mislead us?
3. On a 100 ms grid λ̂(10 ticks) ≈ 0.39 /s; on 1000 ms it is ≈ 0.19 /s. What would a Poisson process predict, and what does the gap tell you about ETHUSDT flow?

*Answers on request.*
