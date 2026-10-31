# Lesson 1: Where a market maker's money comes from and goes

*Written 2026-10-19. Track: [strategy-glft](00-choosing-the-strategy.md). Assumes [venues-and-instruments.md](../market-setup/venues-and-instruments.md) (maker/taker, fees, funding). Corrected 2026-10-22 and 2026-10-23 to match [specs/glft-perp.md](../../specs/glft-perp.md) and the code (see Decisions log).*

## Key ideas
- A market maker **sells immediacy**. Takers who want to trade *now* pay you the distance from mid to your quote.
- Every dollar of MM PnL falls into **four buckets**: spread captured, inventory PnL, fees, funding.
- A textbook round trip at 60 ticks each side earns **+$1.20 spread − $0.96 fees = +$0.24** per ETH. Thin.
- **Adverse selection**: the fills you get are biased toward the bad ones. One toxic fill can wipe out about **9 good ones**.
- **Inventory risk**: one ETH held for an hour swings about **±$15**, vs **$0.12** earned per fill. Inventory, not spread, dominates the variance.
- Far quotes on ETHUSDT fill mostly **when the price comes to you**. That is where theory and reality part.

Numbers used throughout (ETHUSDT perp, VIP0): tick 0.01, mid ≈ 2,400, maker 2 bp, taker 5 bp, size 1 ETH.

---

## 1. The cast: who trades with whom

**Intuition.** A taker wants to trade now. A maker is willing to wait. The maker charges the taker for not having to wait, by buying a bit below fair price and selling a bit above.

| Order | What it does | Fee (VIP0) | Who uses it |
|---|---|---|---|
| Limit order (resting) | Sits in the book at your price until filled or cancelled | Maker, 2 bp | Us |
| Market order | Trades now against resting orders, best price first | Taker, 5 bp | Impatient traders |
| Marketable limit | A limit priced through the touch, so it trades now | Taker, 5 bp | Same as market |
| Post-only (`GTX` on Binance futures) | Limit order that is **rejected** if it would trade on arrival | Maker or nothing | MMs: guarantees we never pay taker by accident |
| Cancel | Removes a resting order | Free (but rate-limited) | MMs, constantly |

Who the takers are matters, because it decides whether a fill is good for us:

| Taker | Why they trade | Good or bad for us |
|---|---|---|
| Retail / impatient | Wants a position now | **Good**: price usually doesn't follow |
| Hedger | Offsetting risk elsewhere | Mostly good |
| Liquidation engine | Forced close of someone's position | Bad in a cascade: price keeps going |
| Arbitrageur / fast trader | Saw BTC, spot or another venue move first | **Bad**: they know the price is about to move |

**Remember: you are paid by the impatient and robbed by the informed. You cannot tell them apart at fill time.**

---

## 2. Where our quotes sit, and how they get filled

For clean numbers, assume the touch is 2399.99 / 2400.01, so mid = **2400.00**. (ETHUSDT is usually 1 tick wide; the extra tick changes nothing here.)

Our quotes: 60 ticks each side, so `δ = 0.60` USDT = **2.5 bp**.

```
  price       size (ETH)
  2400.60   [ OUR ASK 1.0 ]   <- 60 ticks above mid
    ...        many levels, hundreds of ETH each near the touch
  2400.01      210            <- best ask
  ---------- mid 2400.00 ----------
  2399.99      180            <- best bid
    ...
  2399.40   [ OUR BID 1.0 ]   <- 60 ticks below mid
```

Two ways our bid can fill:

```
 (a) SWEEP: one huge sell eats every level 2399.99 ... 2399.40
     mid before 2400.00, fill at 2399.40, we earned 0.60 vs mid
     rare: average perp trade ~0.78 ETH (facts doc); top level holds 100s of ETH

 (b) WALK-DOWN: mid drifts 2400.00 -> 2399.405; our bid becomes the best bid;
     an ordinary 0.5 ETH sell hits it
     common: but mid is now ~2399.40, so vs the *current* mid we earned ~0
```

- The textbook model (lesson 3) treats every fill like (a): a random arrival at distance δ from the current mid.
  - Our calibration instead measures δ from the **mid seen ~400 ms earlier** (our latency), so a short walk toward the quote still counts ([spec §6.2](../../specs/glft-perp.md) step 2).
- On a deep, 1-tick book like ETHUSDT, **(b) is the usual case**. A far quote mostly fills because the price came to it.
- Queue position in (b): you are near the front only if you joined early **and never modified the order since**. Every modify sends it to the back (Binance re-queues modified orders; so does our engine, `exchange.replace_keeps_priority: false`). Our quoter replaces each side about every 2 s, so this side benefit is small ([queue model](../market-setup/market-data.md)).

**Remember: on ETHUSDT, "my far bid filled" usually means "the price fell to my bid".**

---

## 3. A clean round trip, step by step

The textbook case: two sweeps, mid stays at 2400.00. Start with 10,000 USDT, flat.

*Perp bookkeeping:* no ETH changes hands. Our backtester still books a buy as "cash down by notional, position up" (`metrics.py` `Account`). Equity = cash + position × mid. Real margin use is much smaller (see leverage in [venues doc](../market-setup/venues-and-instruments.md)).

```
 time ->
 t0  post bid 2399.40 and ask 2400.60 (post-only). Cost: $0.
 t1  big seller sweeps down to 2399.40  -> our BID fills. We are long 1 ETH.
 t2  big buyer sweeps up to 2400.60     -> our ASK fills. We are flat again.
```

| Step | Event | Fee (2 bp × price) | Cash | Position | Mid | Equity | PnL |
|---|---|---|---|---|---|---|---|
| t0 | Post quotes | 0 | 10,000.00000 | 0 | 2400.00 | 10,000.00000 | 0 |
| t1 | Buy 1 @ 2399.40 | 0.47988 | 7,600.12012 | +1 | 2400.00 | 10,000.12012 | **+0.12** |
| t2 | Sell 1 @ 2400.60 | 0.48012 | 10,000.24000 | 0 | 2400.00 | 10,000.24000 | **+0.24** |

Cash at t1 = 10,000 − 2,399.40 − 0.47988. Cash at t2 = 7,600.12012 + 2,400.60 − 0.48012.

Per bucket:

| Bucket | Bid fill | Ask fill | Round trip |
|---|---|---|---|
| Spread captured (distance from mid) | +0.60 | +0.60 | **+1.20** |
| Fees | −0.48 | −0.48 | **−0.96** |
| Inventory PnL (mid didn't move) | 0 | 0 | 0 |
| **Net** | **+0.12** | **+0.12** | **+0.24** |

- Each side: 60 ticks earned, 48 ticks paid in fee, 12 ticks kept.
- **Fee per side ≈ 48 ticks.** That is why the fee-aware GLFT quotes sit at least ~48 ticks from mid.
  - Qualifier: **at zero inventory only.** With inventory, the skew lets the side that reduces the position quote inside the fee distance, down to a 10-tick floor (≈34 ticks at q = +Q with the spec's placeholder A, k). It pays a little to cut risk ([spec §4.4, §10](../../specs/glft-perp.md)). With the *calibrated* A, k ([glft_calibrated.yaml](../../config/strategies/glft_calibrated.yaml)) the +Q ask is ≈75.5 ticks, still outside the ≈70.7-tick fee; see [lesson 4](04-optimal-spread-glft-closed-form.md).
  - The fee is not just a shift by `c`. The spec's default "exact" mode also uses `A·e^(−kc)` in the skew, which makes the skew larger. The simple shift is the "shift" variant ([spec §4.2](../../specs/glft-perp.md)). Lesson 5.
  - 48 ticks is at ETH 2,400. In our 2024-03 backtest data ETH ≈ 3,500, so the fee ≈ 70 ticks. Same rule `c = f_m·m`.
- A 1-tick touch quote would earn 0.5 tick per side and still pay 48: the −$0.95 from the [venues doc](../market-setup/venues-and-instruments.md).

**Remember: per fill, you keep (distance from mid) − (fee). Everything else is risk.**

---

## 4. The formal version: the PnL identity

Let `m` be mid, `q` position (ETH), and for fill `i`: signed size `s_i` (+ buy, − sell), price `p_i`, mid at the time `m_i`, fee `f_i`.

```
 ΔEquity =  Σ s_i·(m_i − p_i)      spread captured   (+ when you buy below / sell above mid)
          + Σ q·Δm                  inventory PnL     (holding q while mid moves)
          − Σ f_i                   fees              (rate × price × size, always ≥ 0 for us at VIP0)
          − Σ q·mark·ρ              funding           (ρ > 0: longs pay)
```

Why it holds: equity = cash + q·m.
- At a fill, cash changes by `−s·p − f` and q·m by `+s·m`. Sum: `s(m − p) − f`.
- Between fills, only m moves: change `q·Δm`.
- At a funding time, cash changes by `−q·mark·ρ`.

Our backtester books exactly these four (all in `src/mm/backtest/metrics.py`, class `Account`):

| Bucket | Code |
|---|---|
| Spread captured | `spread_capture += s * (ref - f.price)` in `Account.on_fill` (`ref` = mid just before the event) |
| Fees | `cash -= s * f.price + f.fee`; `fees += f.fee` in `Account.on_fill`; rates in `config.py` `DEFAULTS["exchange"]` (`maker_fee`, `taker_fee`) |
| Inventory PnL | `inventory_pnl += position * (mid - last_mid)` in `Account.on_mid` |
| Funding | `pay = position * px * rate` in `Account.on_funding` |
| Self-check | `Account.identity_gap()` should be ~0 |

Caveat: the split between "spread" and "inventory" depends on which mid you call `m_i`. In case (b) above, the spread bucket is ~0 and the loss shows up as inventory. **The total does not change.**

**Remember: total PnL = spread + inventory − fees − funding. Lessons 2–7 are all about tilting these four.**

---

## 5. A fill that goes wrong: adverse selection

**Intuition.** A fast trader sees BTC drop. They sell ETH hard before ETH's book catches up. Their sweep reaches our bid. Ten seconds later ETH has fallen further. We bought from someone who knew.

```
 mid
 2400.00 ●─┐                     fill: buy 1 @ 2399.40
           │ sweep
 2399.40   └──●  <- our bid filled
                \
 2398.80         ●───────●       +10 s: mid 2398.80
             t1        t1+10s
```

| Bucket | Value |
|---|---|
| Spread captured (vs mid 2400.00 at fill) | +0.60 |
| Fee | −0.48 |
| Inventory (+1 ETH × −1.20) | −1.20 |
| **Net** | **−1.08** |

Equity check: 7,600.12012 + 2,398.80 = 9,998.92012, i.e. −1.08.

### Markout: how we measure it
```
 markout(h) = side × (mid at t+h − fill price)        side = +1 buy, −1 sell
 here:        +1 × (2398.80 − 2399.40) = −0.60 per ETH at h = 10 s
```
- Positive markout: the price moved our way after the fill. Negative: we were picked off.
- Our backtester records markouts at 1, 10 and 60 s (`config.py` `DEFAULTS["metrics"]["markout_horizons_s"]`), with a bootstrap 95% CI.
- **Real result (GLFT v1.1):** 10 s markout −0.198 USDT per ETH, CI [−0.287, −0.106] in-sample; −0.251 [−0.367, −0.135] out-of-sample. Both CIs are below 0, so our fills *are* adversely selected (spec H5; [review](../../reviews/2026-10-22_glft_v1_1.md) §3). The CI is optimistically narrow because fills cluster.

### Why it is so costly: the mix of flow
Good fill = +0.12. Toxic fill = −1.08. Let `x` = share of toxic fills.

| Toxic share x | Expected PnL per fill |
|---|---|
| 0% | +0.12 |
| 5% | 0.95 × 0.12 − 0.05 × 1.08 = **+0.06** |
| **10%** | 0.90 × 0.12 − 0.10 × 1.08 = **0.00** |
| 30% | 0.70 × 0.12 − 0.30 × 1.08 = **−0.24** |

- Break-even: `0.12·(1 − x) = 1.08·x` → **x = 10%**. One toxic fill cancels nine good ones.
- This is Glosten–Milgrom (1985) in miniature: the spread must cover the average loss to informed traders.
- Worse: **fills are not a random sample.** A far quote fills precisely when the price is moving toward it, and moves tend to continue for a moment. This is "winner's curse" for makers.

**Remember: judge fills by markout, not by fill price.**

---

## 6. A position you get stuck holding: inventory risk

**Intuition.** You buy and wait to sell. The market drifts down. Your ask never fills, but a naive quoter keeps buying as price falls.

Naive quoter: always bid mid − 0.60, ask mid + 0.60, re-centred on mid. ETH trends down 1.20 at a time.

```
 mid 2400.00 ── buy @2399.40 ──> pos +1
 mid 2398.80 ── buy @2398.20 ──> pos +2      our asks (mid+0.60) never touched
 mid 2397.60 ── buy @2397.00 ──> pos +3
 mid 2396.40                     stuck: long 3 ETH, avg entry 2398.20
```

| Bucket | Value |
|---|---|
| Spread captured (3 × 0.60) | +1.80 |
| Fees (0.47988 + 0.47964 + 0.47940) | −1.44 |
| Inventory (−1.20 × 1, −1.20 × 2, −1.20 × 3) | −7.20 |
| **Net** | **−6.84** |

Each fill "made" +0.12. The position lost 7.20.

### How big is inventory risk? (approximate)
Assume ETH annualised volatility ~60% (it varies, roughly 40–90%; lesson 6 measures it). Per √second: `σ ≈ 2400 × 0.60 / √(31,536,000) ≈ 0.26` USDT. One-sigma moves scale with √time:

| Holding time | 1σ move, 1 ETH | vs +0.12 per fill |
|---|---|---|
| 1 s | ±0.26 | 2x |
| 1 min | ±2.0 | 17x |
| 1 h | **±15.4** | **128x** |
| 8 h (funding interval) | ±43.5 | 360x |

- Risk grows with **q** (linear in money, q² in variance) and with **√time**.
- Average drift is about zero, so this is mostly *variance*, not expected loss. But a strategy earning +0.12 per fill cannot afford ±15 swings it didn't ask for.

### The stuck position's options

| Option | Cost for the 3 ETH above (mid 2396.40) | Catch |
|---|---|---|
| Wait for the ask to fill | ±46 per hour (3 × 15.4), 1σ | Pure gamble |
| Dump with a market sell | Taker fee 3 × 5 bp × 2396.39 ≈ **3.60**, plus ~half a tick | Locks in the −6.84 and adds 3.60 |
| Pay funding while holding | If funding is +0.01%: 3 × 2396.40 × 0.0001 = **0.72** | One funding equals six good fills |
| **Skew the quotes** | Lower both quotes: ask nearer mid (sell sooner, still maker), bid further away (buy less) | Give up some edge to shed risk. **Lesson 2** |

The backtester also reports what the final position would fetch if dumped at the touch with taker fee (`engine.py` `Engine.finish`, `net_if_liquidated_at_touch`), because marking at mid for free flatters a strategy that ends long.

**Remember: inventory is the big risk. Spread is the small reward for carrying it.**

---

## 7. The whole picture

```
                  +------------------------------------------+
  you post  ----> |  bid  2399.40        ask  2400.60         |
                  +------------------------------------------+
                         | fills (rare, biased to bad ones)
                         v
   + spread captured     distance from mid at fill      (+0.60 per fill here)
   − fees                2 bp x price                    (−0.48 per fill)
   ± inventory PnL       q x mid move while you hold     (±15 per ETH-hour)
   − adverse selection   the negative part of inventory PnL right after fills
   − funding             q x mark x rate, every 8 h      (−0.24 per ETH at +0.01%)
```

**Theory vs our market, honestly:**

| Textbook (AS/GLFT) assumes | ETHUSDT reality |
|---|---|
| Fills at distance δ from the *current* mid | Far fills mostly come after the mid walks to you. (Our calibration uses the mid ~400 ms earlier: [spec §6.2](../../specs/glft-perp.md).) |
| Fills independent of future price moves | Fills are biased toward adverse moves |
| No fees | 2 bp maker ≈ 48 ticks per side at 2,400 (≈70 at 3,500) |
| Mid moves smoothly (constant σ) | Volatility clusters and jumps |

---

## 8. Teaser for lesson 2

You are long 3 ETH. Where should your quotes be *centred* now?

Not on the mid. On a **reservation price**: the price at which you'd be indifferent to holding one more ETH. Avellaneda–Stoikov's answer:

```
 r = s − q·γ·σ²·(T − t)
```
- q = 0: `r = s`, quote symmetrically.
- q = +3: `r` drops below mid, so both quotes shift down. Your ask gets closer to mid and fills sooner. Your bid moves away and buys less.
- Note: this is **AS** (finite horizon T, σ²). Our spec uses **stationary GLFT**, `r = m − q·ω` with `ω = σ·c2` (linear in σ, no T) ([spec §4.1](../../specs/glft-perp.md)). Different models, not an error.

Lesson 2 builds that from scratch, with r computed for q = 0 … +5 on ETHUSDT numbers, then moves from AS to GLFT.

---

## Decisions log
- No new decisions in this lesson. Strategy choice: see [00-choosing-the-strategy.md](00-choosing-the-strategy.md).
- **2026-10-22. Corrections to match [specs/glft-perp.md](../../specs/glft-perp.md) §13.** No owner decision; the spec's notation is now the reference. §3: "at least ~48 ticks" holds only at zero inventory (reducing side can reach the 10-tick floor, 34 ticks at q = +Q); fee is "exact" (`A → A·e^(−kc)`) by default, not just a shift; 48 vs 70 ticks is ETH 2,400 vs 3,500. §2/§7: calibration measures δ from the ~400 ms-lagged mid. §8: AS teaser vs the spec's stationary GLFT `r = m − q·ω`. Why: so the lessons don't contradict the code.
- **2026-10-23. Sync with code 4b1640d / spec v1.2.** Code citations now name functions (`Account.on_fill`, `Engine.finish`, …) instead of line numbers. §2: "we are near the queue front" corrected: every replace re-queues (Binance and our engine), and we replace often. §5: added the v1.1 markout CIs (H5 holds). No lesson numbers changed.

---

## Check your understanding
1. Your bid at 2399.50 (1 ETH) fills when mid is 2400.00. Ten seconds later mid is 2399.70. Give the spread captured, fee, inventory PnL, net PnL and 10 s markout. Was this fill profitable?
2. Assume 20% of your fills are toxic, and each toxic fill is followed by a 1.20 mid move against you. Good fills see no move. With a 2 bp fee at 2,400, how far from mid (in ticks) must you quote for zero expected PnL per fill?
3. You are long 3 ETH at the bottom of the section 6 trend. Funding settles in 10 minutes at +0.01%. Rank wait, dump and skew by cost and by risk. Which would you choose, and what are you giving up?

*Answers on request.*
