# Choosing the first strategy, and the GLFT curriculum

*Written 2026-10-19. Builds on [market-setup/](../market-setup/). The menu came from the [research survey](../../research/binance-eth-market-making-survey.md). Notation now follows the strategy spec, [specs/glft-perp.md](../../specs/glft-perp.md) §3 (corrections 2026-10-22 and 2026-10-23, see §5).*

## Key ideas
- A **base quoter** decides where to post the bid and ask. An **overlay** changes or cancels those quotes. You need a base before an overlay means anything.
- We chose the **fee-aware GLFT baseline** on Binance USDⓈ-M ETHUSDT perp: quote wide, skew against inventory, add the fee.
- At VIP0 the fee (2 bp ≈ $0.48 per ETH ≈ **48 ticks** at ETH 2,400) pushes the quotes far from mid. So we expect **few fills, and probably a small loss**. (At zero inventory; with inventory the unloading side can sit closer. See §1 note.)
- That is fine. It is a **benchmark**: the honest skeleton that later signals (microprice, lead-lag) plug into and get measured against.
- Firms rarely run textbook AS/GLFT as-is. They run **base quoter + signals + risk overlays**. Inventory skew is the part almost everyone keeps.

---

## 1. The menu

| # | Idea | Type | Quotes where | Edge comes from | Extra data needed | Main risk / doubt |
|---|---|---|---|---|---|---|
| 1 | **Fee-aware GLFT baseline** | Base | Wide: fee + optimal half-spread, ~48+ ticks from mid (at q = 0)¹ | Spread above fee; inventory skew limits risk | Trades + L1 (have) | Few fills; likely fee-negative at VIP0 |
| 2 | Microprice / imbalance touch quoting | Base | At the touch (1 tick) | Dodging fills just before adverse moves | L1/L2 (have), low latency | Touch round trip loses ~$0.95/ETH in fees; our ~240 ms latency eats the signal |
| 3 | Lead-lag stale-quote guard | Overlay | Cancels / skews the base quotes | Pull quotes when BTC (leader) moves first | BTCUSDT feed (not recorded) | BTC→ETH lead at ms is practitioner belief, unverified |
| 4 | ETH/U 0-fee spot pair | Base, other market | ~40-tick-wide thin spot book | Zero maker fee, so spread capture is real | Whole spot pipeline (not built) | Promo status unverified; thin book; leaves the perp decision |
| 5 | Basis / funding-aware reservation price | Overlay | Shifts the quote centre | Predictable funding cost and basis | `@markPrice` stream | Funding-time effects unverified |
| 6 | Liquidation-cascade filter | Overlay | Pulls or widens quotes | Avoiding cascade fills | `@forceOrder` (live only, sampled) | Rare events, hard to backtest |

¹ Only at zero inventory. With inventory, the side that **reduces** the position can quote inside the fee distance, down to a 10-tick floor (≈34 ticks at q = +Q in [spec §10](../../specs/glft-perp.md), which uses placeholder A, k; floor in spec §4.4). With the *calibrated* A, k ([glft_calibrated.yaml](../../config/strategies/glft_calibrated.yaml)) the +Q ask is ≈75.5 ticks, still outside the ≈70.7-tick fee; see [lesson 4](04-optimal-spread-glft-closed-form.md). 48 ticks assumes ETH ≈ 2,400; our 2024-03 data has ETH ≈ 3,500, so the fee is ≈70 ticks (spec §2). Same rule `c = f_m·m`.

How they fit together:
```
                 +-------------------------------+
  overlays  -->  | 3 lead-lag guard  (cancel)    |
  (sit on top)   | 5 funding/basis   (shift r)   |
                 | 6 cascade filter  (pull/widen)|
                 +---------------+---------------+
                                 |
  base quoter -->  1 GLFT (wide, inventory-aware)   or   2 microprice (touch, signal-driven)
                                 |
  later combo -->  feed 2's microprice into 1 as its fair price
```

**Remember: 1 and 2 are alternatives for the base. 3, 5 and 6 are add-ons.**

---

## 2. Why idea 1

- **Ground up.** Every other idea assumes you already know reservation price, skew, fill probability and PnL decomposition. GLFT teaches all four.
- **Explicit maths.** Closed-form quotes, so each number can be checked by hand.
- **Uses data we already have.** σ, A, k can be fitted from aggTrades and L1.
- **Latency-tolerant.** Quotes 48+ ticks out (≥10 ticks even when fully skewed) do not need to win a 240 ms race. Idea 2 does.
- **Makes the fee visible.** It forces the question "how far from break-even are we?" instead of hiding it.

### Accepted downsides

| Downside | Why | What we do about it |
|---|---|---|
| A **benchmark**, not a money-maker | No signal; fair price is plain mid | Use it to measure ideas 2/3 later |
| **Likely fee-negative at VIP0** | 2 bp per side, plus adverse selection on the fills it gets | Report how far from break-even, by fee tier |
| **Few fills** | Far quotes fill mostly when price runs to them | Long samples; wide error bars on PnL and on A, k |
| A, k hard to fit | Most ETHUSDT trades happen 0–1 tick from mid | Fit from trade-through depth; lesson 6 |

- **First result (v1.1 backtest): confirmed.** Net −3.10 USDT in-sample, −5.10 out-of-sample. Break-even maker fee ≈ 0.8 bp vs the 2 bp we pay ([review](../../reviews/2026-10-22_glft_v1_1.md) §3).

**Remember: success for idea 1 means a correct, well-measured baseline, not profit.**

---

## 3. How firms decide

Firms are secretive. These are common patterns from public talks, papers and practitioner code, not rules.

- **Fair value first.** The biggest effort goes into a better "true price" than mid: microprice, other venues, BTC, the spot index. The quoter is centred on that.
- **Inventory skew everywhere.** Shifting quotes against your position (the AS/GLFT idea) is near universal, often as a simple linear skew `−κ·q` tuned from data rather than derived from utility.
- **Spread from data, not formula.** Width is set from measured markouts and fill rates per regime, with a floor at fee + expected adverse selection. (That floor is for quotes that open or add to a position. A quote that *reduces* inventory may sit inside the fee: [spec §13](../../specs/glft-perp.md) row 6b, lesson 4.)
- **Overlays for risk.** Stale-quote guards, volatility and cascade filters, inventory and loss limits, kill switches.
- **Hedging.** Inventory is often offloaded on another venue or instrument rather than waited out.
- **Fees decide the style.** With rebates or MM-program fees, touch quoting can pay. At retail fees, firms would not touch-quote ETHUSDT at all.

```
 firm stack (typical)           our plan
 fair value model      <-----   lesson 8 / idea 2 (microprice)
 base quoter (skew)    <-----   lessons 2-5 (GLFT)
 signals / overlays    <-----   ideas 3, 5, 6
 risk limits, hedging  <-----   inventory cap Q (lesson 4); later
```

**Remember: GLFT is our skeleton. Firms add the muscle (signals) and the brakes (overlays).**

---

## 4. Curriculum (one lesson per doc, at your own pace)

Market structure comes in only when a lesson needs it. Basics already covered live in [market-setup/](../market-setup/) and are linked, not repeated.

**Notation from lesson 2 on = [spec §3](../../specs/glft-perp.md):** `m` mid, `q` ETH, `Δ` order size, `Q` limit, `c = f_m·m`, `c1`, `c2`, `ω = σ·c2`, `r = m − q·ω`. Lessons 1–5 are written; 6–8 are planned.

| # | Lesson | Strategy concept | Market structure woven in |
|---|---|---|---|
| 1 | [Where a market maker's money comes from and goes](01-how-a-market-maker-earns-and-loses.md) | Spread capture, fees, adverse selection, inventory, funding; the PnL identity | Limit vs market vs post-only orders; why takers pay; sweeps; why far quotes rarely fill |
| 2 | [Inventory risk and the reservation price](02-inventory-risk-and-reservation-price.md) | AS intuition `r = s − q·γ·σ²·(T−t)`, then the **AS→GLFT transition**: stationary GLFT `r = m − q·ω`, `ω = σ·c2` (linear in σ, no horizon; spec §4.1); γ and ξ = γ; γ as a skew knob (spec §6.4) | Margin and liquidation of inventory; position limits; taker exits |
| 3 | [Fill probability vs distance](03-fill-probability.md) | `λ(δ) = A·e^(−kδ)`; Poisson arrivals; δ measured from the **mid seen L ≈ 400 ms earlier**, not the current mid (spec §6.2 step 2) | Who sends market orders; trade sizes; depth; bursty flow; queue at the touch |
| 4 | [The optimal spread and GLFT](04-optimal-spread-glft-closed-form.md) | GLFT closed form `h0 = c1 + (Δ/2)·ω`, `δ_b,a = h0 ± q·ω`; inventory cap `|q| ≤ Q`; **inventory-dependent distance**: floor `δ_min` = 10 ticks, binds only on the reducing side (spec §4.4) | Tick rounding (away from mid); 24/7 market (why no horizon T); order size limits |
| 5 | [Fees and break-even](05-fees-and-break-even.md) | `c = f_m·m`; **"shift"** (`δ → δ + c`) vs **"exact"** (also `A → A·e^(−kc)`, so ω grows by `e^(kc/2)`) (spec §4.2, App. A); why the reducing side may quote inside the fee; break-even fee `f*` by tier | VIP tiers, BNB discount, rebates, MM programs |
| 6 | Calibrating σ, A, k from our data | Estimators (spec §6); latency-lagged trade-through fit, WLS, bootstrap; γ from target skew; freeze in-sample | aggTrades vs L1; trade-through rates; volatility clustering; funding times |
| 7 | PnL decomposition and what the backtest must measure | Spread, inventory, fees, funding; markouts; Sharpe; inventory stats | Latency, cancel/replace, rate limits, fill rules |
| 8 | Weaknesses and extensions | Where GLFT's assumptions break; microprice as fair price (idea 2); stale-quote guard (idea 3) | Price discovery (perp leads spot, BTC leads?); liquidation cascades |

---

## 5. Decisions log
- **2026-10-19. First strategy: idea 1, fee-aware GLFT baseline on Binance USDⓈ-M ETHUSDT perp.** Over microprice touch quoting (2), lead-lag guard (3), ETH/U 0-fee spot (4), funding/basis reservation price (5), cascade filter (6). Owner's words: "id like to build my knowledge from the ground up so 1 seems better. Ill also need you to teach me about the market structure and how trading works as we go into the strategy." Why: it teaches the core ideas every other option assumes; closed-form and checkable; uses data we have; tolerant of our latency. Accepted: it is a benchmark, likely fee-negative at VIP0, with few fills and noisy A, k estimates. Path: learn 1, then add 2's microprice as fair price and/or 3's guard. Teaching track: this folder, market structure introduced lesson by lesson.

### Corrections
- **2026-10-22. Aligned with [specs/glft-perp.md](../../specs/glft-perp.md) (§13 "Consistency with lessons").** Key ideas, §1 and §2: "48+ ticks from mid" now says *at zero inventory*. With inventory the reducing side can go down to the 10-tick floor (34 ticks at q = +Q). Also noted 48 ticks (ETH 2,400) vs ≈70 ticks (2024 data, ETH ≈ 3,500). §4: curriculum now uses spec notation and plans the AS→GLFT transition (L2), the latency-lagged mid (L3, L6), the floor (L4) and exact vs shift fees (L5). Why: the lessons must not contradict the code built from the spec.
- **2026-10-23. Sync with code 4b1640d / spec v1.2.** §4: lessons 2–5 are now written and linked (the doc said "2–8 planned"). §2: added the v1.1 result (net −3.10 / −5.10, break-even ≈ 0.8 bp), which confirms "likely fee-negative". §3: the "fee floor" applies to position-opening quotes only (spec §13 row 6b). No numbers changed.

---

## Check your understanding
1. Which of the six ideas could run on their own, and which need a base quoter underneath? Why?
2. Why does a 2 bp maker fee push GLFT's quotes about 48 ticks from mid at ETH ≈ $2,400 and zero inventory?
3. If idea 1 loses a little money in the backtest, why might it still be a useful result?

*Answers on request.*
