# Lesson 5: Fees and break-even

*Written 2026-10-22. Track: [strategy-glft](00-choosing-the-strategy.md). Assumes [04-optimal-spread-glft-closed-form.md](04-optimal-spread-glft-closed-form.md) and [venues-and-instruments.md](../market-setup/venues-and-instruments.md) (fees, BNB, funding). Notation: [specs/glft-perp.md](../../specs/glft-perp.md) §3, §4.2–4.4, §9 H4/H8, §10, App. A.*

## Key ideas
- The maker fee is a **toll per ETH**: `c = f_m·m` = 48 ticks at 2,400, or 70.7 ticks at the 2024 data's 3,536.
- **"Exact" mode** treats the fee as a change of variable `δ̃ = δ − c`. The fee problem becomes the fee-free one with **`A → A·e^(−kc)`**: fills 121× rarer, so **ω × 11** at the placeholders (× 3.66 calibrated). It works **only because λ is exponential**.
- **"Shift" mode** just adds c and keeps A. Under target-skew γ both modes hit the **same ω**, but γ differs (**0.8953 vs ≈103.5** at the placeholders, 0.2875 vs ≈3.8 calibrated). Through ξ = γ that makes `c1`, and so the half-spread, differ by **≈0.5 tick** (≈0.13 tick calibrated).
- **The fee floor binds on the side that opens or adds to a position** (both sides at q = 0). The reducing side may go inside c, down to the 10-tick floor. Measured from r, every quote still clears the fee.
- **Break-even:** in the model you keep `c1 + (Δ/2)·ω` ≈ 12.5 ticks per fill (placeholders) at any fee tier. In the v1.1 backtest, `f* = gross / notional` = **0.82 bp** in-sample and **−1.9 bp** out-of-sample, against 2 bp paid.
- Funding stays out of v1 quotes. It is booked and measured separately.

Numbers (spec §10 unless noted): `m = 2400.005`, σ = 0.26, A = 4 /s, k = 10 /USDT, Δ = 0.01, Q = 0.05, f_m = 0.0002, θ = 0.01. Calibrated values from [`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) are marked "calibrated".

---

## 0. Recap (lessons 2–4)
- Quotes: `δ_b,a = c + c1 + (Δ/2)·ω ± q·ω`, centred on `r = m − q·ω`. Fill rate `λ(δ) = A·e^(−kδ)`.
- `c1 ≈ 1/k` ignores A. `ω = σ·c2` with `c2 ∝ sqrt(γ/A_use)`. Lesson 4 left `A_use` open, and this lesson fills it in.

---

## 1. The fee as a toll: `c = f_m·m`

```
 mid m                                   our bid
   |<------------ c ------------>|<--- δ̃ --->|
   |   toll paid to Binance      | our markup |
   |<------------------- δ (what takers see) ->|
```

| | f_m | c at 2,400 | c at 3,535.70 (calibrated mean mid) |
|---|---|---|---|
| VIP0 (spec §1) | 2.0 bp | 0.480 = **48.0 ticks** | 0.707 = **70.7 ticks** |
| VIP0 + BNB (10% off) | 1.8 bp | 43.2 ticks | 63.6 ticks |

- Code: [`glft.py`](../../src/mm/strategies/glft.py) `fee_per_eth`. It is recomputed from the **live mid** every cycle, so `c` drifts about 1% a day with price.
- *Simplification:* Binance charges the fee on the fill price, not on mid. The error is O(f_m) ≈ 2e-4 relative and is ignored (spec §8).
- L1 §3: per fill you keep **(distance from mid) − c**. A 1-tick touch quote keeps 0.5 − 48 ticks.

**Remember: the fee is a fixed toll in USDT per ETH, about 48 ticks at 2,400. Every fill pays it.**

---

## 2. Two ways to fold the fee in

### Intuition
- The **takers** only see δ, the posted distance. That is what sets the fill rate.
- **We** only care about δ̃ = δ − c, the markup we keep.
- **Shift mode** says: "solve GLFT as if there were no fee, then move both quotes out by c". It uses the fill rate of a quote at δ̃, but our quote really sits at c + δ̃. So it **overstates fills by `e^(kc)`**.
- **Exact mode** says: "solve GLFT in δ̃, using the fill rate we really get there". That rate is `A·e^(−k(c+δ̃)) = [A·e^(−kc)]·e^(−kδ̃)`. It is the same exponential with a smaller A.

### Why the change of variable is exact (spec App. A)
```
 fill rate at posted δ:   A·e^(−kδ) = A·e^(−k(c + δ̃)) = ( A·e^(−kc) ) · e^(−kδ̃)
                                                         \__ A_eff __/
 cash per bid fill:       −(S − δ + c)·Δ = −(S − δ̃)·Δ        ← identical to the fee-free cash flow in δ̃
```
- In δ̃, both the cash dynamics and the intensity have exactly the fee-free form, just with `A_eff`.
- So GLFT's solution applies unchanged: `δ̃* = GLFT(A_eff)`, and then `δ* = c + δ̃*`.
- **This relies on λ being exponential.** Exponentials are memoryless: moving c further out costs the same *fraction* `e^(−kc)` of fills, wherever you start.
  - A power law `λ = A·δ^(−α)` does not do that. `(c + δ̃)^(−α)` is not a constant times `δ̃^(−α)`, so the GLFT formulas would have to be re-derived (spec App. A).
- It also assumes c is constant. In reality c drifts with m and is recomputed live.
- Honest note: the researcher derived this. It is standard algebra, but the spec says it was not found stated in GLFT or Guéant (2017).

### What it does to the numbers (equal γ = 1; spec §10 vectors 1 and 2)
```
 A_eff = 4 · e^(−10 × 0.480001) = 4 / 121.5 = 0.0329187 /s      fills 121.5× rarer
 ω ∝ 1/√A_use   ⇒   ω_exact / ω_shift = √121.5 = e^(kc/2) = e^(2.4) ≈ 11.0
```

| γ = 1 | `A_use` | `c1` (t) | ω (USDT/ETH) | skew at Q (t) | h (t) | q = 0 quotes |
|---|---|---|---|---|---|---|
| **exact** | 0.0329 | 9.995 | **5.284** | **26.4** | 60.64 | 2399.39 / 2400.62 |
| shift | 4 | 9.995 | 0.479 | 2.4 | 58.23 | 2399.42 / 2400.59 |

- `c1` is identical, because `c1` ignores A (lesson 4 §2). Only the skew changes.
- Shift mode barely skews: 2.4 ticks at max inventory, against a 58-tick half-spread. Inventory would take far longer to unwind.
- **Why exact makes sense:** the fee pushes us out → fewer fills → inventory unwinds more slowly → we must skew harder (spec §4.2).
- Code: [`glft.py`](../../src/mm/strategies/glft.py) `a_use`, a one-line `X if cond else Y`. Tests: `test_worked_example_intermediate_numbers` (exact) and `test_shift_mode_vector` (shift, including the `e^(kc/2)` ratio).

### The amplification depends on k·c

| Setting | k (1/USDT) | c (USDT) | `e^(kc)` (fills rarer) | `e^(kc/2)` (ω larger) |
|---|---|---|---|---|
| spec §10 placeholders | 10 | 0.480 | 121.5 | **11.0** |
| calibrated (mean mid 3,535.70) | 3.666 | 0.707 | 13.4 | **3.66** |

- The "≈11×" in the spec is a placeholder number. With the calibrated fit it is ≈3.66×, because the fitted fill curve is much flatter.
- Side effect: in exact mode **ω depends on the price level**, through `c = f_m·m`. A 1% rise in ETH raises ω by `k·f_m·m·1%/2` ≈ 1.3% (calibrated).

**Remember: the fee doesn't only push quotes out. Fewer fills means skewing harder: ω × `e^(kc/2)`. That is exact only for an exponential λ.**

---

## 3. Under target-skew γ: same ω, different γ

γ is not fixed by hand. It is solved so that `Q·ω = 25 ticks` (lesson 4 §6). So both modes reach **ω = 5.0** by construction. The difference moves into γ.

| Target skew 25 t, `m = 2400.005` | exact | shift |
|---|---|---|
| `A_use` | 0.0329187 | 4 |
| solved γ (= ξ) | **0.8953** | **≈103.5** |
| ξΔ/k | 0.0009 | 0.1035 |
| power `(1+ξΔ/k)^(k/(ξΔ)+1)` | 2.7195 | 2.858 |
| `c1` | 0.099955 (9.996 t) | 0.09516 (9.52 t) |
| ω | 5.0 | 5.0 |
| h | 0.604956 (60.50 t) | 0.600156 (60.02 t) |
| q = 0 | 2399.40 / 2400.61 | 2399.40 / 2400.61 |
| q = +Q ask | 2400.36 | 2400.36 |

Why γ is ≈100× larger in shift mode:
1. `c2² ∝ γ/A_use`. To get the same ω with A 121.5× larger, γ must be ~121.5× larger: 0.8953 × 121.5 ≈ 108.8.
2. But ξ = γ, and the power term grows with ξ (2.7195 → 2.858). That gets part of the way, so γ ≈ 108.8 × 2.7195/2.858 ≈ **103.5**.
3. Large ξ shrinks `c1 = (1/(ξΔ))·ln(1 + ξΔ/k)`: 9.996 → 9.52 ticks. Same ω and same c, so **h is 0.48 tick narrower** in shift mode.

- After rounding, the two modes give the **same prices in the table above** (and the same bid at +Q, 2399.15). They differ by 1 tick only when a tick boundary falls inside that 0.48-tick gap.
  - On a 1-tick book the mid always sits on a half tick. What moves the boundary is c drifting with price: 1 tick of c per ~50 USDT of ETH at 2 bp.
  - So over a day it happens on roughly half of the quote cycles, per side. That is a rough estimate.
- **Calibrated:** `A_use` is only 13.4× smaller, so γ_shift ≈ 3.8 vs 0.2875. Then ξΔ/k ≈ 0.010, `c1` is 0.2714 vs 0.2727 USDT, and the h gap is **≈ 0.13 tick** (spec §4.2, §13 item 1). By the same rough logic, a one-tick price difference on ≈ 13% of cycles.
- Verified against the code: `test_target_skew_same_omega_in_both_fee_modes_but_not_identical_quotes` asserts γ_shift ≈ 103.5, equal ω, `c1` 0.09996 vs 0.0952, and an h gap of 0.5 ± 0.15 tick. The prices in the table are hand-calculated here from those numbers. The tests do not check them.
- **The γ number is not comparable across modes.** It is a knob, not a utility parameter (spec §6.4).
- The fee mode matters a lot **when γ is held fixed**. That is the `fee_shift` sensitivity variant (`sensitivity.variants.fee_shift` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml), spec §9 H8):
  - calibrated γ = 0.2875 in shift mode gives ω = 5.0/3.66 ≈ 1.37;
  - the skew at Q is then ≈ 6.8 ticks instead of 25.
  - v1.1 result (H8 holds): worse inventory control in 10/10 seed-periods, 24% / 16% of the time at the limit vs 2% / 4%. Its better PnL is just fewer fills ([review §3](../../reviews/2026-10-22_glft_v1_1.md)).

**Remember: target-skew γ makes the skew mode-independent. The mode then only changes γ, and through ξ about half a tick of spread (≈ 0.13 tick calibrated).**

---

## 4. Which side has to clear the fee

```
 long q = +Q (placeholders, γ = 1)

  2400.485 - - - - - - - - - - - - - -   m + c   fee line, ask side
  2400.35  [ ASK ]  reducing side         34.5 t from m: INSIDE the fee
  2400.005 ---------- m -------------
  2399.741 ---------- r = m − q·ω ---    centre of our quotes
  2399.525 - - - - - - - - - - - - - -   m − c   fee line, bid side
  2399.13  ( bid )  adding side           NOT SENT at Q (headroom 0); 87 t from m
```

**Rule (spec §4.4):** the fee floor binds on the side that **opens or adds** to a position. At q = 0 that is both sides.

- **Adding side:** `δ = h + |q|·ω ≥ h = c + c1 + (Δ/2)·ω > c`. It always clears the fee. Nothing in the code enforces this. It falls out of the formula.
- **Reducing side:** `δ = h − |q|·ω` can drop below c, down to `δ_min` = 10 ticks. That happens once |q| > 0.024 ETH at the placeholders (lesson 4 ladder).

**Why quoting inside the fee is rational.** Price the options for unloading 0.01 ETH at q = +Q:

| Option | Cost per ETH vs mid | Per 0.01 ETH | Catch |
|---|---|---|---|
| Passive ask at 2400.35 | 34.5 − 48.0 = **−13.5 ticks** | −0.00135 USDT | May not fill soon |
| Ask moved out to the fee line (48 t) | 0 | 0 | Fills ~`e^(1.35)` ≈ 4× less often (k = 0.1/tick), so you stay at Q longer |
| Taker sell at the bid 2400.00 | −0.5 t spread − 120 t fee = **−120.5 ticks** | −0.01205 USDT | Certain fill |
| Do nothing | 0 expected | — | 1σ over 1 min on 0.05 ETH ≈ 0.05 × 0.26 × √60 ≈ **±0.10 USDT** |

- Measured from **r**, the ask still earns more than the fee: `2400.35 − 2399.740781 − 0.480001 = +0.129` USDT = **+12.9 ticks**. GLFT treats r as fair value when you are long, and both quotes sit h ≈ 60.6 ticks from r.
- So "spread ≥ fee" (L0 §3) is right **around r**. It only looks violated when measured from m.
- **Calibrated reality:** h ≈ 100.5 ticks, fee 70.7, skew at Q 25. So the reducing ask at +Q sits ≈ 75.5 ticks out, *outside* the fee. With these fitted numbers the inside-fee case never occurs (it would need |q| > 0.06 > Q).

**Remember: open only at a profit after fees. Reduce at a small loss if it buys a lot less risk. A passive loss of 13.5 ticks beats a taker loss of 120.**

---

## 5. Break-even maths

### In the model (no adverse selection)
- Per fill at q = 0 you keep `h − c = c1 + (Δ/2)·ω`, plus up to 1 tick of rounding.
  - Placeholders, target-skew γ: 60.5 − 48.0 = **12.5 ticks** per ETH (0.00125 USDT per 0.01 ETH fill). The fee on the same fill is 0.0048 USDT.
  - Calibrated: 27.3 + 2.5 ≈ **30 ticks** per ETH ≈ 0.84 bp at 3,536.
- **This does not depend on the fee.** In exact mode with target-skew γ, a cheaper tier does not widen your margin. It moves both quotes in by the fee saving, so you get **more fills**:
  - VIP0 → 1.2 bp at the calibrated numbers: c falls by 0.283 USDT, so λ rises by `e^(3.666 × 0.283)` ≈ **2.8×**.
- So in the model, **break-even means adverse selection per fill < `c1 + (Δ/2)·ω`**: about 12.5 ticks (placeholders) or 30 ticks (calibrated).

### In the backtest: the break-even fee `f*` (spec §9 H4)
```
 f* = gross / Σ fill notional        gross = spread capture + inventory PnL
 net ≈ gross − f_m·notional − funding
```

v1.1 run [`reports/2026-10-22_glft_v1_1/summary.md`](../../reports/2026-10-22_glft_v1_1/summary.md) (calibrated params, seed 42; validator [review](../../reviews/2026-10-22_glft_v1_1.md): H4 holds):

| | in-sample (03-29) | out-of-sample (03-30) |
|---|---|---|
| spread capture | +2.21 | +1.30 |
| inventory PnL | −0.07 | −3.76 |
| **gross** | **+2.14** | **−2.46** |
| fill notional | 26,035 | 12,904 |
| fees paid | −5.21 | −2.63 (incl. one taker exit) |
| **f\*** | **0.82 bp** | **−1.91 bp** (needs a rebate) |

Net in-sample if the **same fills** happened at other maker rates (net = 2.14 − f·26,035 − 0.04 funding):

| Maker rate | Net (USDT) |
|---|---|
| 2.0 bp (VIP0) | −3.11 (report: −3.10, rounding) |
| 1.8 bp (VIP0 + BNB) | −2.59 |
| 1.2 bp | −1.02 |
| **0.8 bp** | **+0.02** ≈ break-even |
| 0.0 bp | +2.10 |

Out-of-sample, net stays negative even at 0 fee (−2.47). The fills lost more on inventory than they earned. (Seed 42 OOS was also stopped by the 5 USDT drawdown kill; seeds 1–4 end at −4.36 to −4.53 without one, spec §9.)

**Caveats (big):**
- The "same fills" assumption is wrong, for the reason given in the model part above: a lower fee means tighter quotes and more, different fills.
- It covers one day each, at one seed.
- The report warns that in-sample PnL is better *with* cross fills than without. The validator finds the gap (0.03 USDT, ≈ 0.1 seed-sd) not material (review N6).
- f* is a yardstick for "how far from break-even", not a forecast.

**Remember: break-even = adverse selection per fill below the model's kept edge. Measured in v1.1: about 0.8 bp in-sample, below zero out-of-sample.**

---

## 6. Market structure: tiers, BNB, rebates, MM programs

Base numbers are in the [venues doc §1, §3](../market-setup/venues-and-instruments.md) and [facts doc](../../research/binance-eth-spot-vs-perp-facts.md). Higher tiers below are **[Approx, unverified]**: they come from a third-party README ("verified 2026-09-18"), not from Binance. Another third-party source gives VIP2 as 1.4 bp / $75M, and Finder puts the regular-user cut-off at $15M, so the sources disagree.

| Tier (USDⓈ-M) | 30-day futures volume | Maker | Taker |
|---|---|---|---|
| Regular (VIP0), **our assumption** | < $5M (Finder: < $15M) | **2.0 bp** | 5.0 bp |
| VIP1 | ≥ $5M (+ BNB held) | 1.8 bp | 5.0 bp |
| VIP2 | ≥ $10M | 1.6 bp | 4.0 bp |
| VIP3 | ≥ $50M | 1.2 bp | 3.2 bp |
| VIP5 | ≥ $1B | 0.8 bp | 2.7 bp |
| VIP9 | ≥ $25B | 0.0 bp | 1.7 bp |

- **BNB discount:** 10% off futures fees if paid in BNB held in the futures wallet (venues doc). The spec assumes none (spec §1).
- **Our own volume** is ≈ $26k a day ≈ **$0.8M per 30 days**. That is far below VIP1. A strategy cannot buy its way to a tier, because the tier comes from the account's total volume.
- **Rebates** (negative maker fee) come from **MM / liquidity-provider programs**, not public tiers. They require large maker volume or share, and quoting obligations. A USDⓈ-M LP program exists, but its terms are unchecked (facts doc). Spot's tier 1 needed ≥ $25M of maker volume a week.
- **What a rebate does to the formula.** A hypothetical −0.5 bp rebate at 2,400 gives c = −0.12. Then `A_use = A·e^(+1.2)` grows, ω shrinks to 0.26, and h = −0.120 + 0.100 + 0.001 = **−0.019** (γ = 1).
  - The model wants to quote *through* the mid. The 10-tick floor clamps both sides.
  - The model is then outside its fit range. You are in the touch queue game (idea 2), not GLFT territory.

**Remember: at our volume the fee is 2 bp and not negotiable. Better tiers mostly buy fills, and only rebates change the game.**

---

## 7. Funding: excluded from v1 quotes (spec §4.3)
Funding only costs you on inventory held across a funding time. At Q = 0.05 ETH and +0.01% that is ≈ 0.012 USDT per settlement, and our inventory turns over in minutes (v1.1 run: −0.04 / −0.01 USDT a day, 1.3% of IS net, so H9 holds). A funding-aware centre is idea 5, a separate overlay. The backtest books funding as its own bucket and flags it if it exceeds 10% of |net PnL| (H9).

---

## 8. How firms handle fees (patterns, not rules)
- **The fee tier is a business input, not a strategy parameter.** Firms pool volume across strategies and accounts to reach tiers. They sign MM programs that trade obligations (uptime, max spread, min size) for rebates.
- **Edge accounting per fill:** expected edge = distance from fair value − fee − expected adverse selection, all measured from markouts.
- **The style follows the fee.** Rebates support touch quoting. Retail fees force wide or signal-driven quoting, or none at all.
- They watch the **break-even fee** of each strategy, as we do with f*, to decide where volume should go.

---

## Decisions log
- **2026-10-22.** No owner decisions yet. Researcher defaults in force: VIP0, no BNB discount (spec §1); `fee_mode: exact` (spec §4.2, `strategy.params.fee_mode` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml)); funding excluded from quotes (spec §4.3). If the owner picks a different fee assumption for analysis (e.g. BNB on, or a tier ladder in the report), log it here with the options and why.
- **2026-10-23 sync with code 4b1640d / spec v1.2.** No owner decision. Citations now name functions, tests and YAML keys instead of line numbers. Added the calibrated shift-mode numbers (γ ≈ 3.8 vs 0.2875, h gap ≈ 0.13 tick; spec §4.2, §13 item 1) and the v1.1 H8 result. The break-even table now comes from the v1.1 report: f* is unchanged (0.82 / −1.91 bp); OOS inventory PnL is −3.76 and fees −2.63.

---

## Check your understanding
1. Turn on the BNB discount (f_m = 0.00018) at the §10 placeholders with γ = 1, exact mode. Compute c, `A_use`, and the factor by which ω changes versus VIP0. Do the quotes move in or out at q = 0, and by roughly how much?
2. Under target-skew γ, why does shift mode need γ ≈ 103.5 instead of 0.8953? Why does that make its half-spread about half a tick narrower, even though ω is identical?
3. Suppose fills really followed a power law in δ rather than an exponential. Which step of the "exact" argument breaks? Would the shift mode then be right?

*Answers on request.*

---

## Sources (fee tiers, checked 2026-10-22)
- [Binance support: futures fees and BNB discount](https://www.binance.com/fr/support/articles/360033544231)
- [Finder: Binance futures fees](https://www.finder.com/cryptocurrency/trading/binance-futures-fees) (regular user 0.02% / 0.05%, < $15M)
- Third-party tier table: [jack0752168/crypto-exchange-fee-data](https://github.com/jack0752168/crypto-exchange-fee-data) and [DEV post](https://dev.to/jacktrader/i-open-sourced-every-binance-and-okx-2026-fee-tier-as-json-a-calculator-fnj). The author is a fee-rebate sub-broker. Confirm with Binance's account commission endpoint once an account exists.
