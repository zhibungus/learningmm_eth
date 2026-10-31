# Lesson 4: The optimal spread: GLFT's closed form, the inventory cap and ticks

*Written 2026-10-22. Track: [strategy-glft](00-choosing-the-strategy.md). Assumes [02-inventory-risk-and-reservation-price.md](02-inventory-risk-and-reservation-price.md) and [03-fill-probability.md](03-fill-probability.md). Notation and numbers: [specs/glft-perp.md](../../specs/glft-perp.md) §3, §4, §6.4, §10. Code: [`glft.py`](../../src/mm/strategies/glft.py) (quotes), [`glft_calibrate.py`](../../src/mm/strategies/glft_calibrate.py) (γ), tests: [`test_glft.py`](../../tests/test_glft.py).*

## Key ideas
- Each quote sits at **fee + static half-spread + inventory term**: `δ_b = c + c1 + (q + Δ/2)·ω`, `δ_a = c + c1 − (q − Δ/2)·ω`.
- **`c1 ≈ 1/k`** (≈10 ticks here) is the markup that maximises profit when fills fall off as `e^(−kδ)`. **`ω`** is the skew per ETH held.
- The formula has **no horizon T and no Q**. It is stationary because ETHUSDT trades 24/7. Q is enforced outside the formula, by not quoting the side that would add to |q|.
- **γ is not guessed.** `solve_gamma` (offline, in `glft_calibrate.py`) picks the γ that makes the skew at Q equal 25 ticks (γ ≈ 0.8953 at the spec placeholders, 0.2875 calibrated).
- To get prices, **floor the bid and ceil the ask** (away from mid). Then apply a 10-tick floor, which can only bind on the reducing side, and never cross the book.
- Worked by hand: q = 0 gives **2399.39 / 2400.62**. q = +Q gives **ask 2400.35 only**, 34 ticks out, inside the 48-tick fee. With calibrated values that ask is ≈ 75.5 ticks out, *outside* the 70.7-tick fee.

Numbers used throughout (spec §10): book 2400.00 / 2400.01, so `m = 2400.005`; σ = 0.26 USDT/√s, A = 4 /s, k = 10 /USDT, γ = ξ = 1, Δ = 0.01 ETH, Q = 0.05 ETH, f_m = 0.0002, θ = 0.01, fee mode "exact", δ_min = 10 ticks. *These are placeholders, not calibrated values. Calibrated ones appear where noted.*

---

## 0. Recap (from lessons 2 and 3)
- L2: the quotes are centred on the reservation price `r = m − q·ω`, with `ω = σ·c2` (stationary GLFT, linear in σ). `Q` caps |q|.
- L3: fills arrive at rate `λ(δ) = A·e^(−kδ)`, with δ measured from the mid we saw about 400 ms earlier.

---

## 1. The closed form in one picture

**Intuition.** Start at the reservation price r. Go out by the fee, then by a markup that trades fill rate against profit per fill. Inventory moves r and so moves both quotes together.

```
 price
   ^
   |   ask = ceil( m + h − q·ω )   --+
   |                                 |  h = c + c1 + (Δ/2)·ω   (half-spread around r)
   |                                 |
   |   r = m − q·ω   <- centre ------+  q > 0: r is below m, so both quotes move DOWN
   |                                 |
   |   bid = floor( m − h − q·ω )  --+
   |
   |   m (mid) is NOT the centre unless q = 0
```

**Formal** (spec §4.1, §4.4; ξ = γ):

```
c   = f_m · m                                                fee per ETH          [USDT]
c1  = (1/(ξΔ)) · ln(1 + ξΔ/k)                                static half-spread   [USDT]
c2  = sqrt( (γ/(2·A_use·Δ·k)) · (1 + ξΔ/k)^(k/(ξΔ) + 1) )    skew coefficient     [√s/ETH]
ω   = σ · c2                                                 skew per ETH         [USDT/ETH]
h0  = c1 + (Δ/2)·ω            h = c + h0                      half-spread around r [USDT]
δ_b = max( h + q·ω , δ_min )   δ_a = max( h − q·ω , δ_min )
bid_px = floor_to_tick(m − δ_b)    ask_px = ceil_to_tick(m + δ_a)    then the no-cross guard
```

`A_use` is `A·e^(−kc)` in "exact" fee mode and plain `A` in "shift" mode. That is lesson 5. This lesson takes `A_use` as given.

**Code map** (execution order inside one quote cycle, all in [`glft.py`](../../src/mm/strategies/glft.py)):

| Step | What | Code |
|---|---|---|
| 1 | `c = f_m·m` | `fee_per_eth` |
| 2 | `A_use` | `a_use` |
| 3 | `c1` | `c1_static` |
| 3 | `c2`, then `ω = σ·c2` | `c2_skew`; ω in `closed_form` |
| 4 | `h = c + c1 + (Δ/2)·ω` | `closed_form` (returns a `ClosedForm`) |
| 5–7 | skew, floor, rounding, no-cross | `compute_quotes` |
| called from | each quote cycle | `GlftMarketMaker._quote_cycle` |

- **Why split into pure functions?** Each one is a formula with no engine objects, so a test can check it against the hand-computed numbers (`test_worked_example_intermediate_numbers`).
- They take only `GlftParams` and numbers; there is no `gamma=` override. To try another γ, copy the params with `dataclasses.replace(p, gamma=g)`, as `solve_gamma` does (§6).
- `c1_static` uses `math.log1p(x)` = ln(1+x), which stays accurate when x ≈ 0.001.
- `c2_skew` writes `(1+x)^(1/x+1)` as `exp((1/x+1)·log1p(x))`, so a tiny x cannot overflow.

**Remember: quote = centre r ± (fee + c1 + half a lot of skew).**

---

## 2. `c1`: the static half-spread

**Intuition.** Ignore inventory and the fee for a moment. Quoting further out earns more per fill but gets fewer fills. Somewhere in between, profit per second is highest.

Profit rate from one side at distance δ is `δ · A·e^(−kδ)` (USDT per second, per unit of size). With A = 4, k = 10:

| δ (ticks) | δ (USDT) | fills/s `4·e^(−10δ)` | δ × fills/s |
|---|---|---|---|
| 5 | 0.05 | 2.43 | 0.121 |
| **10** | **0.10** | **1.47** | **0.147** ← peak |
| 15 | 0.15 | 0.89 | 0.134 |
| 20 | 0.20 | 0.54 | 0.108 |

- Calculus: d/dδ [δ·e^(−kδ)] = 0 gives **δ* = 1/k**. Going one more unit out gains 1 per fill but loses a fraction k of the fills.
- `c1 = (1/(ξΔ))·ln(1 + ξΔ/k) ≈ 1/k − ξΔ/(2k²)`. Risk aversion to *non-execution* (ξ) pulls it slightly inside 1/k.
  - §10: ξΔ/k = 0.001, so `c1 = 100·ln(1.001) = 0.0999500` USDT = **9.995 ticks** (vs 1/k = 10).
- `c1` does **not** depend on A, σ or q. It depends only on how fast fills decay (k), plus a tiny ξΔ correction.
- With the calibrated k = 3.666 /USDT, `c1` = 0.2727 USDT ≈ **27 ticks** ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `derived.c1`). A flatter fill curve means a wider markup.

**Remember: `c1 ≈ 1/k`. Flatter fill curve (small k), wider quotes.**

---

## 3. `ω` and the `(Δ/2)·ω` term

`ω` was built in lesson 2. Here we only need **what each input does to it** and why half a lot appears.

### Why `(Δ/2)·ω`
- Inventory moves in lumps of Δ. A bid fill takes q to q + Δ.
- GLFT prices that fill at the **average inventory over the fill**, q + Δ/2. So `δ_b = c + c1 + (q + Δ/2)·ω`. That is the paper's `((2q+Δ)/2)` form (spec §4.1).
- §10: `(Δ/2)·ω = 0.005 × 5.28437 = 0.0264` USDT = **2.64 ticks**. Small, but it is why `h0 > c1` even at q = 0.

### What each input does (exact mode, from the §10 base ω = 5.284 USDT/ETH)

| Change | `c1` (ticks) | `ω` (USDT/ETH) | Skew at Q (ticks) | Why |
|---|---|---|---|---|
| base | 9.995 | 5.284 | 26.4 | |
| σ × 2 (0.52) | 9.995 | **10.57** | 52.8 | ω ∝ σ: more volatility, inventory is riskier |
| γ × 4 (4.0) | 9.98 | **≈10.58** | ≈52.9 | ω ∝ √γ |
| A × 2 (8) | 9.995 | **3.74** | 18.7 | ω ∝ 1/√A: more fills, inventory unwinds by itself |
| k 10 → 12 | **8.33** | **7.80** | 39.0 | exact mode: `A_use = A·e^(−kc)` falls fast as k rises (lesson 5). In shift mode ω would *fall* to ≈0.44 |
| Δ × 2 (0.02) | 9.99 | ≈3.74 per ETH (7.48 ticks per lot) | 18.7 | ω per ETH ∝ 1/√Δ; skew per lot ∝ √Δ |

- Signs at a glance: **σ↑, γ↑ widen the skew. A↑ narrows it. k↑ narrows c1.** In exact mode it also raises ω through `e^(kc/2)`.
- The ξ term `(1+ξΔ/k)^(k/(ξΔ)+1)` is ≈ e = 2.718 when ξΔ/k ≪ 1. Here it is 2.719641 ([spec §10](../../specs/glft-perp.md)).

**Remember: skew grows with σ and √γ, shrinks with √A. `c1` cares only about k.**

---

## 4. Why there is no horizon T: a 24/7 market

| | Avellaneda–Stoikov (L1 §8) | Our GLFT (spec §4.1) |
|---|---|---|
| Horizon | Finite T. Inventory is marked or dumped at T (end of day) | None. Guéant (2017) eqs. 4.6–4.7 are the **large-horizon (asymptotic)** limit |
| Skew | `q·γ·σ²·(T−t)`, shrinks to 0 as t → T | `q·ω`, constant over time |
| Fits | Equity markets with a close | Crypto perps: no open, no close, no weekend halt |

- On a stock exchange, "flat by 16:00" is a real deadline, and the quotes should change as it nears.
- ETHUSDT never closes. Our holding times are minutes, far shorter than any horizon. So the large-T limit is the natural model: **quotes depend on (m, q), not on the clock**.
- Honest caveat: crypto still has *soft* horizons, and the formula ignores them:
  - funding timestamps (every 8 h, sometimes more often);
  - thin weekend and Asian-night liquidity;
  - the end of each backtest period (inventory marked at mid).

**Remember: no closing bell, so no T. The quote is a function of mid and inventory only.**

---

## 5. The inventory cap Q

- **Q is not in the closed form.** The asymptotic formula assumes inventory can drift freely. We impose |q| ≤ Q from outside (spec §4.5).
- **How:** the side that would push |q| past Q is not quoted. Its live order is cancelled.
- **Headroom** counts every order we think is live, including in-flight ones ([`glft.py`](../../src/mm/strategies/glft.py) `GlftMarketMaker._desired`; the other live orders are summed by `_exposure`):

```
headroom_buy  = Q − q − (our other live buy orders)      e.g. q = +0.04: 0.05 − 0.04 = 0.01 → bid size 0.01
headroom_sell = Q + q − (our other live sell orders)          q = +0.05: 0             → no bid
```

- **Why Q = 0.05 ETH = 5·Δ** (spec §6.3):
  - The affine-in-q approximation is good only for small |q|/Δ (Guéant uses Q/Δ = 4).
  - It keeps the risk tiny: 1σ swing at Q over an hour ≈ 0.05 × 0.26 × 60 ≈ 0.78 USDT.
- **Exchange size rules bite near the cap** (spec §1): lot 0.001 ETH, min notional 20 USDT.
  - Δ = 0.01 ETH is 24 USDT at 2,400. That clears 20.
  - At q = 0.045 the headroom is 0.005 ETH = 12 USDT < 20, so **no bid at all** (`test_headroom_exactly_one_order_and_partial_headroom`).
- The reducing side always quotes full Δ, even beyond Q (e.g. after a lagging fill report): `test_beyond_limit_reducing_side_still_quotes_full_size`.
- Backstop: `exchange.max_position: 0.05` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml). A `risk_max_position` reject would mean a strategy bug.

**Remember: the formula sets prices; Q sets which sides exist.**

---

## 6. Choosing γ from a target skew (`solve_gamma`)

**Intuition.** "Risk aversion 0.9 per USDT" means nothing to a trader. "At max long, my ask moves 25 ticks closer to mid" does. So we fix the skew and back out γ (spec §6.4). This matches firms tuning a linear skew `−κ·q` from data (L0 §3).

**Formal:**
```
target:  Q · ω(γ) = skew_at_Q_ticks · θ      →   ω_target = 25 × 0.01 / 0.05 = 5.0 USDT/ETH
solve:   bisection on γ ∈ [1e-4, 1e3]  (ω rises with γ)
check:   γ ≈ (ω_target/σ)² · 2·A_use·Δ·k / e = (5/0.26)² × 2 × 0.0329187 × 0.01 × 10 / 2.71828 ≈ 0.8957
```

**Code:** [`glft_calibrate.py`](../../src/mm/strategies/glft_calibrate.py) `solve_gamma(p, cal, target_omega, mid)`. It runs once, offline, inside `calibrate`; the live strategy only reads the γ it wrote. The bracket and tolerance come from `CalParams` (the `cal_*` keys), not from `GlftParams`.
1. Check the target is inside the bracket `[cal_gamma_min, cal_gamma_max]`, else raise `ValueError` (test `test_solve_gamma_out_of_bracket_is_an_error`).
2. Each trial γ is scored with `closed_form(replace(p, gamma=g), mid).omega`. Halve the bracket at the **geometric** mean `sqrt(lo·hi)`, because it spans 7 orders of magnitude. An arithmetic midpoint would spend its first ~10 steps above γ = 1 (500, 250, 125, …).
3. Stop when `hi − lo ≤ cal_gamma_rel_tol·lo` (1e-6). That takes about 24 halvings.

First steps (ω shown ≈ 5.284·√γ, ignoring the tiny ξ effect):

| Step | trial γ = √(lo·hi) | ω ≈ | vs 5.0 | new bracket |
|---|---|---|---|---|
| 1 | √(1e-4·1e3) = 0.316 | 2.97 | low | [0.316, 1000] |
| 2 | 17.8 | 22.3 | high | [0.316, 17.8] |
| 3 | 2.37 | 8.14 | high | [0.316, 2.37] |
| 4 | 0.866 | 4.92 | low | [0.866, 2.37] |
| 5 | 1.43 | 6.33 | high | [0.866, 1.43] |
| … | | | | → **γ = 0.8953** |

- Result (spec §10 third vector, `test_target_skew_gamma_vector`): γ ≈ 0.8953, `c1` = 0.099955, `h = 0.480001 + 0.099955 + 0.025 = 0.604956` (60.50 ticks). q = 0: **2399.40 / 2400.61**. q = +Q: ask **2400.36**.
- γ ∝ target²: 10 ticks gives γ ≈ 0.14, 50 ticks gives ≈ 3.6 (spec §6.4).
- **Calibrated run** ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `params.gamma`, `gamma_for_variants`): γ = 0.2875 (base, 25 ticks), 0.046 (10 ticks), 1.148 (50 ticks). Same ×0.16 / ×4 pattern.
- γ is solved once, in-sample, then frozen (`strategy.params.gamma` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml)). It is never tuned on PnL.
- **Does the skew work?** In the v1.1 backtest, yes for inventory (H3 holds in 10/10 seed-periods): mean |q| 0.018 vs 0.031 ETH with `skew_off`, time at the limit 2–4% vs ≈30%. `skew_off` loses less PnL only because it trades less ([review §3](../../reviews/2026-10-22_glft_v1_1.md)).

**Remember: we choose the skew in ticks. γ is just the number that delivers it.**

---

## 7. From distances to prices: ticks, floors, no-cross

### The 1-tick book and the half-tick mid
- Tick θ = 0.01 USDT ≈ 0.04 bp at 2,400. The ETHUSDT book is almost always **1 tick wide** ([venues doc §3](../market-setup/venues-and-instruments.md)).
- So mid sits on a **half tick**: (2400.00 + 2400.01)/2 = 2400.005.
- Our quotes are 35–90 ticks out, far from the touch queue game. Rounding costs us 0–1 tick, never more.

### Rounding: floor the bid, ceil the ask
```
 bid:  m − δ_b = 2399.398627  → floor → 2399.39   (0.86 tick further out)
 ask:  m + δ_a = 2400.611373  → ceil  → 2400.62   (0.86 tick further out)
```
- **Always away from mid**, so rounding never makes a quote tighter than the formula asked for. Rounding the other way would quietly give away edge.
- Code: [`glft.py`](../../src/mm/strategies/glft.py) `floor_to_tick` / `ceil_to_tick`. The `±1e-9` nudge stops float noise (e.g. 239939.99999999 ticks) from rounding a whole tick the wrong way. It matches `MarketState.round_price` (`test_rounding_is_away_from_mid_and_matches_marketstate`).

### The 10-tick floor `δ_min` (spec §4.4)
- `δ_b = max(h + q·ω, δ_min)`, `δ_a = max(h − q·ω, δ_min)` (`compute_quotes`, step 5).
- **Why 10 ticks:** it is the near edge of the A, k fit range (L3). Closer than that, the model would be extrapolated beyond its data, and the touch queue game takes over.
- **It depends on inventory.** Only `h − |q|·ω` can get small, and that is the reducing side. The adding side is always ≥ h.
- At §10 values it binds only when `|q|·ω > h − 0.10`, i.e. |q| > 0.096 ETH. That is beyond Q, so **it never binds at defaults**.
- It binds under extreme σ: σ = 50 at q = +Q gives δ_a = exactly 10 ticks (`test_reducing_side_may_quote_inside_the_fee_but_not_inside_delta_min`).
- The fee `c` is **not** a floor here. Lesson 5 explains why.

### Never cross the book (step 7)
```
 bid_px = min(bid_px, best_ask − θ)      ask_px = max(ask_px, best_bid + θ)      (compute_quotes, step 7)
```
- Our view of the book is ~240 ms old. If the market jumped, a "safe" bid could now be at or above the real best ask.
- Test: mid still 2400.005, but the book has fallen to 2399.29 / 2399.30. The bid is clamped to **2399.29** (`test_no_cross_guard_clamps_to_one_tick_inside_the_touch`).
- Honest note: inside the live strategy, mid and book come from the same snapshot. With δ ≥ 10 ticks and the spread pause at 20 ticks, the clamp rarely binds. The real protection against a stale view is **post-only (GTX)**: the exchange rejects a crossing order on arrival, and that side then cools down for 1 s.

**Remember: round away from mid; the 10-tick floor guards the reducing side; post-only guards against stale views.**

---

## 8. The spec §10 worked example, by hand

### Shared numbers (q-independent)

| Step | Formula | Value | Ticks |
|---|---|---|---|
| `c` | 0.0002 × 2400.005 | 0.480001 | 48.00 |
| `c1` | 100 × ln(1.001) | 0.0999500 | 9.995 |
| `A_use` | 4 × e^(−10 × 0.480001) | 0.0329187 /s | |
| power | 1.001^1001 | 2.719641 | |
| `c2` | √(2.719641 / (2 × 0.0329187 × 0.01 × 10)) = √413.085 | 20.3245 | |
| `ω` | 0.26 × 20.3245 | 5.28437 USDT/ETH | 5.28 per lot |
| `(Δ/2)·ω` | 0.005 × 5.28437 | 0.0264219 | 2.64 |
| `h` | 0.480001 + 0.099950 + 0.026422 | **0.606373** | **60.64** |

Checked in code: `test_worked_example_intermediate_numbers`.

### q = 0
```
 δ_b = δ_a = 0.606373
 bid = floor(2400.005 − 0.606373) = floor(2399.398627) = 2399.39     (61.5 ticks below mid)
 ask = ceil (2400.005 + 0.606373) = ceil (2400.611373) = 2400.62     (61.5 ticks above mid)
 both sides, qty 0.01
```

### q = +Q = +0.05 (max long)
```
 skew q·ω = 0.05 × 5.28437 = 0.264219  (26.42 ticks)
 δ_b = 0.606373 + 0.264219 = 0.870592 → bid floor(2399.134408) = 2399.13   NOT SENT (headroom 0)
 δ_a = 0.606373 − 0.264219 = 0.342154 → ask ceil (2400.347154) = 2400.35   34.5 ticks out, ask only
 r   = 2400.005 − 0.264219 = 2399.740781
```

### q = −Q = −0.05 (max short): mirror image
```
 δ_b = 0.342154 → bid floor(2399.662846) = 2399.66     bid only
 δ_a = 0.870592 → ask ceil (2400.875592) = 2400.88     NOT SENT
```

Checked: `test_worked_example_quote_prices`, `test_worked_example_deltas_and_reservation_price`, `test_at_max_long_only_the_ask_is_quoted_and_ask_is_inside_fee` and `test_at_max_short_only_the_bid_is_quoted`.

### The ladder: q from −Q to +Q
One character ≈ 3 ticks. `B` bid, `A` ask, `x` a price that is computed but not sent, `|` mid, `:` the fee distance (48 ticks).

```
 ticks:  -90          -48              0              +48          +90
 q=-0.05               :    B          |               :            x
 q=-0.04               :  B            |               :          A
 q=-0.03               :B              |               :        A
 q=-0.02              B:               |               :       A
 q=-0.01             B :               |               :     A
 q= 0.00           B   :               |               :   A
 q=+0.01         B     :               |               : A
 q=+0.02       B       :               |               :A
 q=+0.03      B        :               |              A:
 q=+0.04    B          :               |            A  :
 q=+0.05  x            :               |          A    :
```

| q (ETH) | δ_b (t) | δ_a (t) | bid_px | ask_px |
|---|---|---|---|---|
| −0.05 | 34.22 | 87.06 | 2399.66 | (2400.88) |
| −0.04 | 39.50 | 81.77 | 2399.61 | 2400.83 |
| −0.03 | 44.78 | 76.49 | 2399.55 | 2400.77 |
| −0.02 | 50.07 | 71.21 | 2399.50 | 2400.72 |
| −0.01 | 55.35 | 65.92 | 2399.45 | 2400.67 |
| 0 | 60.64 | 60.64 | **2399.39** | **2400.62** |
| +0.01 | 65.92 | 55.35 | 2399.34 | 2400.56 |
| +0.02 | 71.21 | 50.07 | 2399.29 | 2400.51 |
| +0.03 | 76.49 | 44.78 | 2399.24 | 2400.46 |
| +0.04 | 81.77 | 39.50 | 2399.18 | 2400.40 |
| +0.05 | 87.06 | 34.22 | (2399.13) | **2400.35** |

- Each lot of inventory slides **both** quotes by ω·Δ ≈ 5.3 ticks. The gap between them stays ≈ 2h ≈ 121 ticks.
- The reducing side goes **inside the fee** once `|q|·ω > c1 + (Δ/2)·ω`, i.e. |q| > 0.024 ETH (from |q| = 0.03 on, here).
- *Simplification:* this uses the fixed γ = 1 test vector, not the target-skew γ. With γ = 0.8953 every row moves by ≤ 1.6 ticks.
- **Calibrated reality** ([`glft_calibrated.yaml`](../../config/strategies/glft_calibrated.yaml) `derived`, mean mid 3,535.70; spec §10 last block): fee 70.7 ticks, `c1` 27.3 ticks, h ≈ 100.5 ticks. At q = +Q the ask is ≈ 75.5 ticks out, still *outside* the fee. The "inside the fee" case comes from the placeholder numbers.

**Remember: inventory slides the whole pair. The gap stays the same width; only its centre moves.**

---

## 9. Requoting under ~400 ms latency (spec §4.6)

**The problem.** The top of book updates ~70 times a second. Chasing every change would mean many replaces per second. Each one takes ~400 ms to complete and costs our queue place.

```
 t=0 ms    we see the book (already ~240 ms old) -> compute targets -> send Replace
 t≈132 ms  replace reaches the matching engine (order latency 132 ms + jitter)
 t≈372 ms  we hear "replaced" (report latency 240 ms)   <- order is "in flight" until here
           meanwhile the mid has moved again; acting now would chase our own tail
```
Latency values: [`config.py`](../../src/mm/backtest/config.py) `DEFAULTS["latency"]` (measured on the owner's PC 2026-10-19).

**The rule** ([`glft.py`](../../src/mm/strategies/glft.py) `GlftMarketMaker._side_action`, once per side per cycle):

| Situation | Action | Code |
|---|---|---|
| no live order, side allowed | place post-only GTC | `PlaceOrder(..., post_only=True, tif="GTC")` |
| an action on this order still in flight | wait | `cid in self._inflight` |
| side must not quote (e.g. at Q) | cancel once | `_cancel` |
| target ≥ **5 ticks** off **and** ≥ **1 s** since last action | replace (price and size) | `_replace` |
| ... or the live order is ≥ **15 ticks too close** to mid | replace now (urgent) | `urgent` |
| live size > what the headroom allows | shrink in place, same price | last branch of `_side_action` |
| we just got a fill | requote now, ignoring the 1 s | `on_fill` → `_quote_cycle(force=True)` |
| otherwise | keep (keeps queue priority) | |

Why these numbers:
- **5 ticks** = half the floor, ≈ 5–8% of h. It is a judgement call, not an optimum. Sitting 4 ticks off target changes the model fill rate by `e^(0.4)` ≈ 1.5× at the placeholder k (0.1 per tick), or `e^(0.15)` ≈ 1.15× at the calibrated k (0.037 per tick).
- **1 s** is well above the ~400 ms round trip, and equals the timer (`latency.timer_interval_ms` in [`glft_smoke.yaml`](../../config/backtest/glft_smoke.yaml)).
- **Urgent only in the dangerous direction.** A quote too close to mid is a cheap option for informed takers (L1 §5). A quote too far is merely idle.
- **A fill always moves both targets by ω·Δ ≈ 5.3 ticks**, which is above the 5-tick threshold. So the fill triggers a requote straight away (`test_fill_triggers_immediate_requote_ignoring_interval`).
- **Every replace goes to the back of the queue**, even a same-price size cut. Binance re-queues every modify, and so does the engine (`exchange.replace_keeps_priority: false`). Only "keep" keeps your place.
- A replace sends the order's **total** size (`ReplaceOrder.total_qty` = filled we have seen + wanted remaining), as Binance does. A fill we have not heard of yet is subtracted automatically. If total ≤ filled, the order is cancelled.
- **Rate guard** (spec §7.4): places + replaces are capped at 50% of Binance's limits, **150 per 10 s and 600 per minute**. When a budget is full, no place or replace is sent (urgent included); cancels always go. Kill-switch exits skip the check but are counted (lesson 7).
- Load is ~1 action per second per side, so the guard never bound in v1.1 (`rate_throttled` 0). The real compliance risk is the unfilled ratio (≈ 98 actions per fill, UFR ≈ 0.99). It is report-only, and Binance's exact rule is unverified (spec §7.4, §12).

**Remember: move only when it matters (≥ 5 ticks), not faster than the latency allows (≥ 1 s), and immediately when danger or a fill appears.**

---

## 10. Theory vs our market

| GLFT closed form assumes | ETHUSDT / our setup | Effect |
|---|---|---|
| Continuous prices | 0.01 tick grid, half-tick mid | Up to 1 tick extra distance, always outward |
| Continuous re-pegging | Requote ≥ 5 ticks, ≤ 1/s, ~400 ms late | Live quotes lag targets by up to ~5 ticks |
| Affine in q, any q | True optimum is non-affine at large \|q\| | Q/Δ kept at 5 |
| No position limit | Hard cap Q, min notional 20 USDT | Adding side switches off at the cap, or just before it (q = 0.045) |
| Large horizon | 24/7, but funding times and weekends exist | Fine for minute-scale holding |
| `λ` exponential everywhere | Fitted only 10–150 ticks | Floor at 10 ticks |

---

## Decisions log
- **2026-10-22.** No owner decisions in this lesson yet. `skew_at_Q_ticks = 25`, `Q = 0.05`, `Δ = 0.01`, `δ_min = 10` ticks and the requote numbers are researcher defaults (spec §6.3, §6.4, §11). If the owner changes one (e.g. skew 10 vs 50), log it here with the options, the reason, and how firms set skew (from data, per regime).
- **2026-10-23 sync with code 4b1640d / spec v1.2.** No owner decision. Code citations now name functions, tests and YAML keys instead of line numbers. `solve_gamma(p, cal, target_omega, mid)` now lives in `glft_calibrate.py`, with its bracket in `CalParams`; the closed-form functions have no `gamma=` override. Added the calibrated γ and +Q ask to Key ideas, the v1.1 H3 result to §6, and replace-loses-priority, total-size replaces, shrink-in-place and the rate guard to §9.
- **2026-10-30. Gap-mask fix (spec v1.2.1, no owner decision).** 50-tick γ back to 1.148 (the 1.149 came from a wrong trade-gap rule; see [lesson 3](03-fill-probability.md) step 5b). Every other rounded number unchanged.

---

## Check your understanding
1. At §10 values, you are long q = +0.02. Compute δ_b, δ_a, then bid_px and ask_px by hand. Which side is the reducing side, and is it inside the 48-tick fee?
2. Volatility doubles (σ = 0.52) and nothing else changes. What happens to `c1`, to ω, and to the ask at q = +Q? With γ re-solved for a 25-tick skew instead, what changes?
3. Why does the code floor the bid and ceil the ask instead of rounding to the nearest tick? Give a case where nearest-tick rounding would put the bid closer to mid than the formula intended.

*Answers on request.*
