# Lesson 2: Inventory risk and the reservation price

*Written 2026-10-22. Track: [strategy-glft](00-choosing-the-strategy.md). Assumes [lesson 1](01-how-a-market-maker-earns-and-loses.md) (PnL buckets, stuck inventory) and the margin part of [venues-and-instruments.md](../market-setup/venues-and-instruments.md#leverage-margin-liquidation). Notation = [spec §3](../../specs/glft-perp.md). Synced with code 4b1640d / spec v1.2 on 2026-10-23.*

## Key ideas
- Holding `q` ETH for `t` seconds adds PnL variance **`q²·σ²·t`**. Double the position, four times the variance.
- The **reservation price** `r` is where you would be indifferent to trading one more lot. You centre your quotes on `r`, not on the mid `m`.
- Avellaneda–Stoikov (AS): `r = s − q·γ·σ²·(T−t)`. Needs an end time `T`. A 24/7 perp has none.
- Our spec uses **stationary GLFT**: `r = m − q·ω`, with `ω = σ·c2`. No `T`, and **linear in σ**, not σ².
- Calibrated: **ω = 5.00 USDT/ETH = 5 ticks per lot** (Δ = 0.01 ETH). At max inventory `Q = 0.05` both quotes sit 25 ticks lower.
- `Q` exists because inventory needs margin, can be liquidated, costs a taker fee to dump, and breaks the model when large.

Numbers used throughout (in-sample 2024-03-29, [`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `params` and `diagnostics.mean_mid`):
`m = 3535.70` (in-sample mean mid), `σ = 0.2501 USDT/√s`, `A = 0.3564 /s`, `k = 3.666 /USDT`, `γ = 0.2875 /USDT`, `Δ = 0.01 ETH`, `Q = 0.05 ETH`, tick `θ = 0.01`.

---

## 1. Why holding inventory is risky

**Intuition.** You earn a few ticks per fill. While you hold the ETH, the price wanders. The wander is far bigger than the ticks.

**Formal.** The spec models the mid as arithmetic Brownian motion with volatility `σ` (USDT/√s) ([spec §3](../../specs/glft-perp.md), §6.1).
- Mid change over `t` seconds: mean ≈ 0, standard deviation `σ·√t`.
- Inventory PnL of `q` ETH over `t`: `q·Δm`, so

```
 Var(inventory PnL) = q² · σ² · t          [USDT²]
 1σ swing           = |q| · σ · √t         [USDT]
```

**Numbers** (σ = 0.25 USDT/√s; 1 lot = Δ = 0.01 ETH):

| q | 1σ over 1 min | 1σ over 1 h | Variance over 1 h |
|---|---|---|---|
| 1 lot (0.01 ETH) | 0.019 | 0.15 | 0.0225 |
| 2 lots | 0.039 | 0.30 | 0.090 (4x) |
| **5 lots = Q (0.05 ETH)** | 0.097 | **0.75** | 0.5625 (25x) |
| 1 ETH (lesson 1 size) | 1.94 | 15.0 | 225 |

- Edge per fill at q = 0, after fee: `(h − c)·Δ = (1.0048 − 0.7071)·0.01 ≈ 0.003 USDT` ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `derived.h_usdt`, `derived.c_usdt`).
- One hour at Q risks a 1σ swing of 0.75 USDT. That is about **250 fills' worth of edge**.
- Calibrated σ = 0.25 at ETH ≈ 3,536 is about 40% annualised, lower than lesson 1's 60% guess. The hourly σ ran from 0.156 (19:00) to 0.503 (15:00) that day ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `diagnostics.sigma_hourly`). **Constant σ is a simplification** (spec §8).

```
 variance
   ^                              *  q = 5 lots: 25 units
   |
   |
   |                 *  q = 3: 9 units
   |        *  q = 2: 4
   |   *  q = 1: 1
   +---+----+--------+-------------+--> q (lots)
```

**Remember: risk grows with q², so the last lot you add is the most expensive one to hold.**

---

## 2. The reservation price: Avellaneda–Stoikov intuition

**Problem (AS 2008).** A dealer quotes a bid and ask until a fixed end time `T`. At `T` the inventory is valued at mid. The dealer has CARA utility with risk aversion `γ`. Where should the quotes go?

**Key idea.** Split the problem in two:
1. Where is my **indifference price** for one more unit, given what I already hold? That is `r`.
2. How far from `r` should I quote to earn spread? (That is the fill-rate trade-off: [lesson 3](03-fill-probability.md).)

### Building r by hand
Hold `q` until `T`, with `τ = T − t` seconds left. A mean-variance (CARA) investor values the position at

```
 value(q) ≈ q·s − (γ/2) · q² · σ² · τ          s = mid (AS's symbol; same as our m)
```

The second term is the risk penalty. Buying one more lot `Δ` changes the value by

```
 value(q+Δ) − value(q) = Δ·s − (γ/2)·σ²·τ·[(q+Δ)² − q²]
```

Divide by Δ to get a price per ETH. Average the "buy one more" and "sell one" prices and the Δ terms cancel:

```
 r = s − q · γ · σ² · (T − t)
```

### Numeric example: q from 0 to +5 lots
I borrow our calibrated γ = 0.2875 and σ = 0.25, and pick τ = 300 s (5 minutes) for illustration. Then
`γ·σ²·τ = 0.2875 × 0.0625 × 300 = 5.39 USDT per ETH`, i.e. **5.39 ticks per lot**.

| q (lots) | q (ETH) | shift `q·γσ²τ` | r = 3535.70 − shift |
|---|---|---|---|
| 0 | 0 | 0 | 3535.7000 |
| +1 | 0.01 | 0.0539 (5.39 t) | 3535.6461 |
| +2 | 0.02 | 0.1078 (10.78 t) | 3535.5922 |
| +3 | 0.03 | 0.1617 (16.17 t) | 3535.5383 |
| +4 | 0.04 | 0.2156 (21.56 t) | 3535.4844 |
| **+5** | 0.05 | **0.2695 (26.95 t)** | **3535.4305** |

At q = +5 lots the two one-lot indifference prices are (using `(γ/2)σ²τ = 2.695`):
- buy one more: penalty `2.695 × (0.06² − 0.05²) = 0.00296` per lot → **29.6 ticks below mid**;
- sell one: relief `2.695 × (0.05² − 0.04²) = 0.00243` per lot → **24.3 ticks below mid**;
- average 26.95 ticks = the table row. **When long, you would even sell below mid.**

```
 q = 0                                  q = +5 lots
   bid ── r=m ── ask                      bid ──────── r ── ask ─ m
          ^ quotes symmetric                   whole pair slides DOWN 27 ticks
                                               ask nearer m: sells sooner
                                               bid further:  buys less
```

**Remember: r is the mid minus a risk charge on what you already hold. Quotes are centred on r.**

---

## 3. Why AS's horizon T does not fit a 24/7 perp

AS needs `T`. The skew is proportional to `T − t`:

| Same q = +5 lots, γ, σ | τ = T − t | AS shift |
|---|---|---|
| End of day in 1 s | 1 s | 0.09 tick |
| 1 minute | 60 s | 5.4 ticks |
| 5 minutes | 300 s | 27 ticks |
| Next funding (8 h) | 28,800 s | **2,588 ticks** (25.9 USDT) |

Problems:
- **Skew collapses near T.** At `t → T` AS stops caring about inventory, because the model values leftover inventory at mid for free. A perp has no close where that happens.
- **Far T gives absurd skews.** AS assumes you *hold* q until T. In reality fills unwind you long before.
- **No natural T.** ETHUSDT trades 24/7. Funding times (venues doc) are payments, not a close.
- Any T we pick is a hidden magic number.

**GLFT's fix (Guéant, Lehalle, Fernandez-Tapia 2013; Guéant 2017).**
- Same CARA set-up, but with fill intensity `A·e^(−kδ)` ([lesson 3](03-fill-probability.md)) and inventory in steps of Δ.
- Let the horizon go to infinity. The optimal quotes converge to **constant, time-free** values. That is the "stationary" (asymptotic) solution in spec §4.1 (Guéant 2017 eqs. 4.6–4.7).
- The model now *knows* inventory gets unwound by fills, so it does not need T.

**Remember: AS asks "how long must I hold this?" GLFT asks "how fast will fills unwind this?" On a 24/7 market only the second question has an answer.**

---

## 4. Stationary GLFT: `r = m − q·ω`

From [spec §4.1](../../specs/glft-perp.md) and §4.4:

```
 c   = f_m · m                                          fee per ETH       [USDT]
 c1  = (1/(ξΔ)) · ln(1 + ξΔ/k)                         static half-spread [USDT]
 c2  = sqrt( γ/(2·A_use·Δ·k) · (1 + ξΔ/k)^(k/(ξΔ)+1) )   skew coefficient  [√s/ETH]
 ω   = σ · c2                                           skew per ETH       [USDT/ETH]
 h   = c + c1 + (Δ/2)·ω                                 half-spread around r
 r   = m − q·ω
 δ_b = h + q·ω ,  δ_a = h − q·ω                          (then floor at δ_min, round away from mid)
```

- `A_use = A·e^(−k·c)` in the default "exact" fee mode. Why: [lesson 5](05-fees-and-break-even.md). Here just take ω as given.
- `ξ` = non-execution risk aversion. The spec sets **ξ = γ** (spec §3, §4.1). At our numbers `ξΔ/k = 0.2875·0.01/3.666 = 0.00078`, so `c1 = 0.27266 ≈ 1/k = 0.27276`. The choice barely matters.

### What ω means
- **ω = how far both quotes move per ETH of inventory.** Units USDT per ETH. Per lot: `ω·Δ/θ` ticks.
- Calibrated: `ω = 5.000 USDT/ETH`, so **5.00 ticks per lot** ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `derived.omega_usdt_per_eth`, `derived.omega_ticks_per_lot`).

### Numeric example: q from 0 to +5 lots (GLFT, calibrated)
`h = 1.0048 USDT = 100.48 ticks` ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `derived.h_usdt`, `derived.h_ticks`). Distances before tick rounding:

| q (lots) | r = 3535.70 − q·ω | δ_b (ticks) | δ_a (ticks) | Model fills/h, bid | Model fills/h, ask |
|---|---|---|---|---|---|
| 0 | 3535.70 | 100.48 | 100.48 | 32.2 | 32.2 |
| +1 | 3535.65 | 105.48 | 95.48 | 26.8 | 38.7 |
| +2 | 3535.60 | 110.48 | 90.48 | 22.3 | 46.5 |
| +3 | 3535.55 | 115.48 | 85.48 | 18.6 | 55.9 |
| +4 | 3535.50 | 120.48 | 80.48 | 15.5 | 67.1 |
| **+5 = Q** | **3535.45** | 125.48 (**not sent**) | **75.48** | 0 | **80.6** |

- Fill rates are `A·e^(−kδ)·3600` per side ([lesson 3](03-fill-probability.md)). Each 5 ticks changes the rate by `e^(±k·0.05) = ×1.20` or `×0.83`.
- At +4 lots the model expects ~15 buys vs ~67 sells per hour. **The skew pulls inventory back toward 0.** Lesson 4 covers this, and the cap.
- At +Q the ask is 75.5 ticks out. That is still *outside* the 70.7-tick fee, so unloading still earns +4.8 ticks per ETH. *(Spec §10's "34 ticks, inside the fee" used placeholder A, k; the calibrated h is wider.)*
- *Caveat:* these are **model** rates. The backtest fills at well under half of them (H2 falsified, [lesson 3](03-fill-probability.md) §6), so real unwinding is slower.

### AS vs GLFT side by side

| | AS | Stationary GLFT (ours) |
|---|---|---|
| Reservation price | `s − q·γ·σ²·(T−t)` | `m − q·ω`, `ω = σ·c2` |
| Horizon | Needs T; skew → 0 as t → T | None; constant in time |
| σ dependence | **σ²** | **σ** (linear) |
| Depends on fill rates A, k? | No (r doesn't) | Yes, through c2 |
| If σ doubles (γ fixed) | skew ×4 | skew ×2 |
| Hour 15 (σ = 0.503) vs day (0.250) | ×4.0 | ×2.0 |

**Why linear in σ? (intuition, my reading, not from the paper).**
- Skew harder → you unwind faster → you hold inventory for less time → less risk.
- Solving that balance gives a square root: `ω ∝ √(γ·σ²) = σ·√γ` (see `c2 ∝ √γ` above).
- Equivalent picture: GLFT behaves like AS with an **effective horizon** `ω/(γσ²) = 5.00/(0.2875 × 0.0625) ≈ 278 s` (about 4.6 min). That effective horizon shrinks as σ rises, so the skew grows only linearly.
- The spec warns: different models, **do not "fix" σ into σ²** ([spec §13](../../specs/glft-perp.md) row 4).

**Remember: ω is the price, in USDT per ETH held, at which GLFT trades off inventory risk against slower fills. It is linear in σ and has no clock.**

---

## 5. γ: a skew knob, not a personality

- Literally, γ is CARA risk aversion over USDT wealth. A value like 0.29 /USDT has no meaningful utility story at our size.
- So the spec **solves γ from a target skew** ([spec §6.4](../../specs/glft-perp.md)): pick `skew_at_Q_ticks`, then find γ with `Q·ω(γ) = skew_at_Q_ticks·θ`.
- Default 25 ticks: `ω_target = 25 × 0.01 / 0.05 = 5.0 USDT/ETH` (`target_omega` in `glft_calibrate.py` `calibrate`, passed to `solve_gamma`).

| Target skew at Q | Solved γ (/USDT) | ω (USDT/ETH) | ticks per lot |
|---|---|---|---|
| 10 ticks | 0.0460 | 2.0 | 2 |
| **25 ticks (base)** | **0.2875** | **5.0** | **5** |
| 50 ticks | 1.148 | 10.0 | 10 |

Sources: [`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `params.gamma`, `gamma_for_variants`. Note `γ ∝ target²`: 2.5x the skew needs 6.25x the γ (0.046 × 6.25 = 0.2875). That is the `ω ∝ √γ` from §4.

This matches how firms work (L0 §3): a linear skew `−κ·q` **tuned from data**. Here `κ = ω`, and GLFT gives it a principled shape. No PnL-tuning of γ on one day (spec §6.4).

**Remember: we choose how many ticks to lean at max inventory. γ is just the number that makes the formula do that.**

---

## 6. In the code: how ω and the skew get computed

One quote cycle, in execution order (all in `src/mm/strategies/glft.py`):

| Step | What happens | Code | Value at m = 3535.70 |
|---|---|---|---|
| 1 | Quote cycle reads `m` and `q = state.position` | `GlftMarketMaker._quote_cycle` | |
| 2 | `compute_quotes` → `closed_form` | `compute_quotes`, `closed_form` | |
| 3 | Fee per ETH `c = f_m·m` | `fee_per_eth` | 0.70714 (70.7 t) |
| 4 | `A_use = A·e^(−k·c)` (exact mode) | `a_use` | 0.026668 /s |
| 5 | `c1` with ξ = γ | `c1_static` (ξ from `xi_of`) | 0.27266 |
| 6 | `c2`, power term via `exp(... log1p ...)` to avoid overflow | `c2_skew` | 19.994 |
| 7 | **`omega = p.sigma * c2`** | `closed_form` | **5.000** |
| 8 | `h = c + c1 + 0.5·Δ·ω` | `closed_form` | 1.0048 |
| 9 | `skew = q·ω` (0 in the `skew_off` variant) | `compute_quotes` | 0.25 at +Q |
| 10 | `δ_b = max(h + skew, δ_min)`, `δ_a = max(h − skew, δ_min)` | `compute_quotes` | |
| 11 | Bid rounds down, ask up | `floor_to_tick`, `ceil_to_tick` (called in `compute_quotes`) | |

Why it is written this way:
- **`r` is never stored.** The code adds `±q·ω` straight to the distances. Same thing: `m − δ_b = r − h` and `m + δ_a = r + h`. The test checks `r = m − q·ω` explicitly (`tests/test_glft.py` `test_worked_example_deltas_and_reservation_price`).
- **Calibration reuses the trading maths.** `solve_gamma(p, cal, target_omega, mid)` in `glft_calibrate.py` copies the params with a trial γ (`dataclasses.replace(p, gamma=g)`) and calls the same `closed_form` inside its bisection. Calibration and trading cannot drift apart. (`closed_form` itself no longer takes a γ override.)
- **`c` and `ω` are recomputed from the live mid** every cycle (`compute_quotes` calls `closed_form(p, mid)`). *Simplification: this lesson holds m = 3535.70; live, ω drifts about ±1% with the price.*
- **A fill changes q, so the code requotes at once**, ignoring the 1 s throttle (`GlftMarketMaker.on_fill` → `_quote_cycle(..., force=True)`).
- Test: long inventory lowers both quotes, short raises both (`test_long_inventory_lowers_both_quotes_short_raises_both`).

**Remember: in code, the reservation price is just "add q·ω to the bid distance, subtract it from the ask distance".**

---

## 7. Market structure: why the cap Q exists

The skew *encourages* unwinding. It does not *guarantee* it. A trend can keep filling your bid (lesson 1 §6). So there is a hard cap: `|q| ≤ Q = 0.05 ETH` ([spec §6.3](../../specs/glft-perp.md)).

### What inventory costs on a perp
Margin, leverage, mark price and liquidation are in the [venues doc](../market-setup/venues-and-instruments.md#leverage-margin-liquidation). Applied to our size (approximate, same simplified formula, 0.5% maintenance):

```
 position at Q:   0.05 ETH × 3535.70 = 176.79 USDT notional
 at 10x leverage: initial margin ≈ 17.68 USDT
 liquidation:     17.68 + 0.05·(P − 3535.70) = 0.005 · 0.05 · P   →   P ≈ 3,198  (≈ −9.5%)
 1σ hourly move:  0.25 × 60 = 15 USDT per ETH  →  liquidation is ~22 hourly σ away
```

### The reasons for Q, ranked for us

| Reason | Number | Who enforces it |
|---|---|---|
| **Risk budget** | 1σ hourly at Q = 0.75 USDT vs drawdown kill at 5 USDT | Kill + flatten (`GlftMarketMaker._risk_checks` → `_kill`; `max_drawdown_usdt` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml)) |
| **Model validity** | GLFT's affine-in-q formula degrades at large \|q\|; spec keeps `Q/Δ = 5` | Spec §4.1, §6.3 |
| **Taker exit cost** | Dumping Q: 5 bp × 176.79 ≈ 0.088 USDT + ½ tick. About 30 fills of edge | Kill exit (`_flatten_orders`): one limit IOC at touch ± 5 ticks, then a MARKET order every timer tick until flat; `flatten_failed` alert after 10 s (spec §7.1) |
| **Margin / liquidation** | Bounded notional → bounded margin → liquidation far away | Exchange (not simulated) |
| **Exchange limits** | Leverage brackets cap position per leverage | Backtest backstop `exchange.max_position: 0.05` ([`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml)) |

How the strategy keeps `|q| ≤ Q`:

```
 headroom_buy  = Q − q − (our other live buy orders)       GlftMarketMaker._desired
 headroom_sell = Q + q − (our other live sell orders)
 headroom ≤ 0          → that side is not quoted (cancel if live)
 |q| > 1.5·Q (breach)  → kill + flatten (limit IOC, then market)   GlftMarketMaker._risk_checks
```
- Code: `GlftMarketMaker._desired` (called from `_side_action`), spec §4.5.
- Live orders count against headroom, because fills are reported ~240 ms late.
- The reducing side always quotes. At +Q only the ask is live (table in §4).

Honest gaps:
- **The backtester does not model margin or liquidation** ([v1.1 `summary.md`](../../reports/2026-10-22_glft_v1_1/summary.md), "Assumptions" item 6). At Q = 0.05 that is harmless. At big size it would not be.
- Liquidation uses **mark price**, not our mid (venues doc). Fine for us; matters in cascades (idea 6).
- v1.1 run (seed 42): max |q| = 0.050, time at limit 0.7% in-sample, 3.5% out-of-sample ([`summary.md`](../../reports/2026-10-22_glft_v1_1/summary.md)).
- **The skew works (H3 holds).** Over 5 seeds, mean |q| 0.018 ETH vs 0.031 with `skew_off`, and time at limit 2–4% vs ~30% ([review](../../reviews/2026-10-22_glft_v1_1.md) §3).

**Remember: Q is a hard wall. The skew is the soft slope that keeps you away from it.**

---

## Decisions log
- None yet. γ comes from the spec's default `skew_at_Q_ticks = 25` and Q = 0.05 is the spec default; the owner has not chosen either. If you want a different lean at max inventory (10–50 ticks), that is your decision to make and log here.
- **2026-10-23. Sync with code 4b1640d / spec v1.2 (no owner decision).** All `file:line` citations replaced by function names (`solve_gamma` now lives in `glft_calibrate.py`; `closed_form` has no γ override any more). §7: the kill exit is now limit IOC then market orders until flat. Recalibrated A = 0.35638 and γ = 0.28747 round to the same 4 digits, so every derived number (h, ω, fill rates) is unchanged; γ for 50 ticks re-rounded 1.148 → 1.149. Added the H3 result and an H2 caveat on model fill rates. Lesson 5 link fixed.
- **2026-10-30. Gap-mask fix (spec v1.2.1, no owner decision).** Calibration is back to A = 0.356368, γ = 0.287455 (50-tick γ 1.148). The A = 0.35638 / γ = 0.28747 in the 2026-10-23 entry came from a wrong trade-gap rule (see [lesson 3](03-fill-probability.md) step 5b).

---

## Check your understanding
1. You are short 3 lots (q = −0.03 ETH) at m = 3535.70, with ω = 5.00 USDT/ETH and h = 100.48 ticks. Give r, δ_b and δ_a in ticks. Which side fills sooner, and roughly how much more often?
2. In hour 15 of the in-sample day σ was 0.503, about 2x the calibrated 0.250. With γ fixed, by what factor should the AS skew and the GLFT skew change? Our ω is frozen: is the strategy over- or under-skewed in that hour?
3. Why would AS with T = the next funding time give silly quotes, and what does GLFT know that AS does not?

*Answers on request.*
