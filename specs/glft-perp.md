# Spec: fee-aware GLFT market maker, Binance USDⓈ-M ETHUSDT perpetual

*Written 2026-10-22 by quant-researcher. Status: v1.1 (2026-10-22): adds Binance order rate limits (§7.4), records the developer interpretations (§4.5, §5, §7, §11), fixes file names, and adds the first smoke-run note (§9), states the calibration/engine fill-rule mismatch (§6.2, §8, §13), adds calibrated-value context to the placeholder claims (§4, §10, §13), and specifies trade-gap masking (§6.2). After the validator review: `unknown_order` storm rule (§7.1), total-size replaces (§4.6), H2 falsified with a v2 calibration list (§9), jump row (§8). **v1.2 (2026-10-23):** market-order flatten with no attempt cap and a `flatten_failed` state (§7.1), fill-race rejects (§4.6, §7.1), v1.1 results and testing rules (§9), stale lines fixed. **v1.2.1 (2026-10-30):** trade-gap mask uses aggregate `trade_id` continuity, not raw `first_id`/`last_id` (§6.2, §13 row 9).*
*Owner decision this implements: [00-choosing-the-strategy.md §5](../learnings/strategy-glft/00-choosing-the-strategy.md) (idea 1, perp, not spot).*
*Lesson refs like **(L1 §4)** point to `learnings/strategy-glft/01-...md` section 4. Use them in code comments. **(not yet covered in lessons)** marks material the tutor has not taught yet. The planned lesson number is given where there is one.*

## Key ideas
- Quote one bid and one ask around the **mid**. Each side sits at **fee + GLFT half-spread**, and both are shifted against inventory by **ω per ETH held**.
- The fee is folded in **exactly**: shift each quote out by `c = f_m·mid`, **and** use the fill rate you get at the shifted distance, `A·e^(−k·c)`. That second part makes the inventory skew much larger than a naive "+fee" shift.
- `γ` (risk aversion) is not guessed. It is **solved once from a target skew** (how far the quotes move at max inventory), after σ, A and k are fitted on in-sample data.
- Everything is calibrated once on `in_sample` (2024-03-29) and **frozen**. `out_of_sample` (2024-03-30) is never used for tuning.
- Expected result: few fills and **net PnL ≤ 0 at VIP0**. This is a benchmark, not a money-maker (L0 §2).

```
 price
   ^        ask = ceil( m + h − q·ω )          h = c + c1 + (Δ/2)·ω
   |  - - - - - - - - - - - - - - - - - -      c  = fee per ETH (≈48 ticks at 2,400)
   |   m (mid) ─────── r = m − q·ω            c1 ≈ 1/k (static half-spread)
   |  - - - - - - - - - - - - - - - - - -      ω  = σ·c2 = skew per ETH of inventory
   |        bid = floor( m − h − q·ω )
   |  long (q>0): both quotes move DOWN, so the ask fills sooner and the bid buys less
```

---

## 1. Objective and instrument

| Item | Value |
|---|---|
| Venue / product | Binance USDⓈ-M futures, **ETHUSDT perpetual** (not spot) |
| Objective | A correct, well-measured **passive baseline**. Earn the spread above the maker fee, with inventory bounded by `Q`. Then measure how far from break-even it is and why: fills, markouts, fee tier |
| Success criterion | All hypotheses in §9 are tested with the stated metrics, and the implementation matches the hand-checked numbers in §10. **Profit is not the success criterion** (L0 §2) |
| Exchange rules (exchangeInfo, 2026-10-19) | tick 0.01 USDT, lot 0.001 ETH, min qty 0.001 ETH, min notional 20 USDT |
| Fees (VIP0) | maker `f_m = 0.0002` (2 bp), taker `f_t = 0.0005` (5 bp), as fractions of notional. No BNB discount assumed |
| Engine | `src/mm/backtest/` (`Strategy` subclass in `src/mm/strategies/glft.py`; calibration in `src/mm/strategies/glft_calibrate.py`), same object in paper trading. Run config: `config/backtest/glft_smoke.yaml` |

---

## 2. Inputs

| Input | Source | Frequency | Used for |
|---|---|---|---|
| `QuoteEvent` (bid_px, bid_qty, ask_px, ask_qty) | archive `bookTicker` (`binance_um/quotes`), live `@bookTicker` | every update (≈70/s) | `m`, guards, live vol estimate |
| `FundingEvent` (rate, mark_price) | archive / REST funding | every 8 h (or more often at cap) | **Not used in the quoting rule** (§4.3). Subscribed only so `state.cash` mirrors funding |
| `TradeEvent` (aggTrades) | `binance_um/agg_trades` | every print | **Calibration only** (A, k). The live strategy does not read trades |
| `MarketState.position`, `open_orders`, `cash` | engine | per callback (delayed by report latency) | inventory `q`, order management, drawdown |
| Timer | `latency.timer_interval_ms = 1000` | 1 s | requote cadence, staleness check |

- `event_kinds = ("quote", "funding")`.
- `data.streams` **must still contain `trades`**: the simulated exchange needs them for fills.
- **Developer check:** confirm the engine still feeds trades to the fill model when the strategy does not subscribe to `"trade"`. If it does not, subscribe to `"trade"` and ignore those events in `on_event`.

**Data available** (`data/processed/binance_um/`):
- quotes and aggTrades for **2024-03-29** (in-sample) and **2024-03-30** (out-of-sample);
- funding for 2024-03.
- Note: `data/processed/README.md` still says quotes exist for 2024-03-30 only, but the 2024-03-29 files exist (see §12).
- ETH was roughly 3,500 USDT on those days (check from the data), so the fee is about **70 ticks**, not 48. All formulas use the live mid, so nothing is hard-coded.

---

## 3. Units (one convention, used everywhere)

**Distances and prices in USDT (price units). Time in seconds. Sizes in ETH.** Ticks are only for display and for rounding.

| Symbol | Meaning | Units | Tick conversion (tick `θ = 0.01`) |
|---|---|---|---|
| `m` | mid = (bid_px + ask_px)/2 | USDT | `m/θ` |
| `δ_b, δ_a` | distance of our bid below / ask above **mid** | USDT | `δ_ticks = δ/θ` |
| `q` | inventory, signed (+ long). `state.position` | ETH | lots = `q/0.001` |
| `Δ` | order size | ETH | |
| `Q` | max \|inventory\| | ETH | |
| `σ` | volatility of mid (arithmetic, Brownian) | USDT/√s | `σ_ticks = σ/θ` (ticks/√s) |
| `A` | fill intensity at δ = 0, per side | 1/s | same |
| `k` | decay of fill intensity with distance | 1/USDT | `k_tick = k·θ` (1/tick) |
| `γ` | CARA risk aversion | 1/USDT | see note below |
| `ξ` | non-execution risk aversion (Guéant 2017). **We set ξ = γ** | 1/USDT | |
| `f_m` | maker fee rate | fraction of notional | |
| `c` | maker fee per ETH = `f_m·m` | USDT (per ETH) | `c/θ` ticks |
| `c1` | static half-spread term | USDT | |
| `c2` | skew coefficient | √s/ETH | |
| `ω = σ·c2` | skew per ETH of inventory | USDT/ETH | per lot of size Δ: `ω·Δ/θ` ticks |

- Dimension check: `ξΔ/k` is dimensionless (1/USDT · ETH · USDT/ETH); `c1` is USDT; `σ·c2·q` is USDT. Every formula below passes this check.
- **Converting to hftbacktest-style units** (ticks for price, order lots for size, as in its tutorial):
  - `σ_tick = σ/θ`
  - `k_tick = k·θ`
  - `q_lots = q/Δ`, `Δ_lots = 1`
  - `γ_tick-lot = γ·Δ·θ`
  - Do not mix the two conventions in one formula.

---

## 4. Quoting rule

### 4.1 GLFT closed-form (fee-free)

**[Paper]** Guéant (2017), eqs. (4.6)–(4.7). I checked these on arXiv:1605.01862 v5 (HTML), section 4.2. They are the asymptotic (large-horizon) approximations for `Λ(δ) = A·e^(−kδ)`, with `q` moving in steps of Δ. For ξ > 0:

```
δ_b(q) ≈ (1/(ξΔ))·ln(1 + ξΔ/k) + ((2q + Δ)/2) · sqrt( (γσ²/(2AΔk)) · (1 + ξΔ/k)^(k/(ξΔ) + 1) )
δ_a(q) ≈ (1/(ξΔ))·ln(1 + ξΔ/k) − ((2q − Δ)/2) · sqrt( same )
```

For ξ = 0, the first term becomes `1/k` and the bracket inside the square root becomes `γσ²e/(2AΔk)`.
- **Choice: ξ = γ.** This is the CARA model of the original GLFT (2013) paper ("Model A" in Guéant 2017), and it is what the hftbacktest tutorial uses.
- At our parameters (exact fee mode) `ξΔ/k ≈ 0.001`, so ξ = γ and ξ = 0 give quotes within 0.01 tick of each other. The choice is immaterial; it is fixed only for reproducibility. In "shift" mode with target-skew γ, `ξΔ/k ≈ 0.1` and ξ matters at the ~0.5-tick level (§4.2).

Rewritten in the form the developer implements (identical algebra):

```
c1 = (1/(ξΔ)) · ln(1 + ξΔ/k)                                         [USDT]
c2 = sqrt( (γ/(2·A·Δ·k)) · (1 + ξΔ/k)^(k/(ξΔ) + 1) )                [√s/ETH]
ω  = σ · c2                                                          [USDT per ETH]
half-spread (around r):  h0 = c1 + (Δ/2)·ω                           [USDT]
reservation price:       r  = m − q·ω                                 [USDT]
δ_b = h0 + q·ω ,   δ_a = h0 − q·ω
```

- **Meaning [Paper, Guéant 2017 §4.2]:**
  - `c1` reflects static (non-execution) risk only and does not depend on `q`.
  - The `q·ω` term is the skew (dynamic inventory risk). It grows with γ and σ.
  - The paper says the approximation is good for small |q| and degrades for large |q| (the true quotes are not affine in q). We keep `Q/Δ = 5` partly for this reason.
- **Note:** GLFT's skew is **linear in σ** (`ω ∝ σ·√γ`). The Avellaneda–Stoikov reservation price in L1 §8 has `γσ²(T−t)`, which is σ². They are different models; do not "fix" one into the other. (Full AS and GLFT: not yet covered in lessons, planned L2 and L4.)

### 4.2 Folding in the maker fee (exact within the model)

**[Inference, derived here; short proof in Appendix A]**
- A bid filled at distance δ earns `δ − c` per ETH versus mid, after the fee (L1 §3: "you keep distance from mid minus fee").
- Write the net markup as `δ̃ = δ − c`. The fill intensity at the posted distance is `A·e^(−k(δ̃ + c)) = (A·e^(−kc))·e^(−kδ̃)`.
- So the fee problem **is** the fee-free GLFT problem in `δ̃`, with `A` replaced by

```
A_eff = A · exp(−k·c),      c = f_m · m
```

- Fee mode `"exact"` (**default**):
  - `c2` uses `A_eff`, so `ω_exact = ω_nofee · e^(k·c/2)`;
  - `δ = c + δ̃*(A_eff)`.
- Fee mode `"shift"`: `δ = c + δ*(A)`. This is the survey's simple shift (research survey §1.2). It under-states the skew by the factor `e^(kc/2)`, which is about 11× at the §10 placeholders but only **≈ 3.66×** at the calibrated values (k = 3.666 /USDT, c ≈ 0.707 USDT, so kc/2 ≈ 1.30). Keep it as a sensitivity variant only.
- Why the exact mode makes sense: the fee pushes us further out, so we get fewer fills, so inventory takes longer to unwind, so we must skew harder.
- `c` is recomputed from the **live mid** on every quote cycle. It changes by about 1% intraday.
- **When γ comes from the target-skew rule (§6.4, default), the two fee modes give the same ω, so the same skew, and nearly the same quotes.** γ is solved to hit the same ω, so only the reported γ and the `c1` term differ.
  - `c1` depends on γ through ξ = γ, and the shift-mode γ is about 100x larger (≈103.5 vs ≈0.895 at the §10 placeholders). So ξΔ/k ≈ 0.10 instead of 0.001.
  - Result: `c1` ≈ 0.09516 vs 0.09996 USDT, and the half-spread differs by **≈0.5 tick**. After rounding, prices can differ by one tick.
  - With calibrated values the shift-mode γ is ≈ e^(kc) ≈ 13× larger (≈ 3.8 vs 0.2875), `c1` ≈ 0.2714 vs 0.2727 USDT, so h differs by **≈ 0.13 tick**.
  - The distinction matters mostly when γ is set by hand.

### 4.3 Funding: explicitly excluded from the quoting rule (v1)
- Holding `q` ETH through a funding time costs `q·mark·ρ` (L1 §4, venues doc "Funding").
  - At ρ = 1e-4 and mark 2,400, that is 0.24 USDT per ETH, or about 0.012 USDT at our Q = 0.05.
- Reasons to exclude it:
  1. The expected cost only matters for positions **held across** the funding timestamp. Our holding times are expected to be minutes.
  2. A funding/basis-aware reservation price is **idea 5**, a separate overlay the owner did not choose for v1 (L0 §1).
  3. The engine books funding as its own PnL bucket, so the backtest will measure it. If |funding bucket| > 10% of |net PnL|, flag it for v2.
- Do not hard-code 8 h anywhere. Funding can settle more often at the cap (facts doc §3).

### 4.4 Final prices

Each **quote cycle** (§5.3), with `m = state.mid`, `q = state.position`, `B = state.bid_px`, `Ak = state.ask_px`:

```
1. c      = f_m · m
2. A_use  = A·exp(−k·c)  if fee_mode == "exact"  else  A
3. c1, c2 from §4.1 with A_use;   ω = σ·c2
4. h      = c + c1 + (Δ/2)·ω
5. δ_b    = max( h + q·ω , δ_min )        δ_min = min_half_spread_ticks · θ
   δ_a    = max( h − q·ω , δ_min )
6. bid_px = floor_to_tick( m − δ_b )       state.round_price(x, "down")
   ask_px = ceil_to_tick ( m + δ_a )       state.round_price(x, "up")
7. no-cross guard (our view is ~240 ms old):
   bid_px = min( bid_px , Ak − θ )         never at or above the best ask
   ask_px = max( ask_px , B  + θ )         never at or below the best bid
8. all orders: post_only = True (Binance GTX), tif = "GTC"
```

- **Rounding** is always *away* from mid (bid down, ask up), so rounding never makes a quote tighter than the formula.
- **Floor `δ_min` = 10 ticks.** It equals the lower edge of the A, k fit range (§6.2). Closer than that, the model would be extrapolated outside the data it was fitted on, and the queue game at the touch takes over (survey §1.2, §2.3).
  - **The fee distance `c` is not a floor for the inventory-reducing side.** The "spread ≥ fee" logic (L0 §3) applies to the side that opens or adds to a position, which at q = 0 means both sides. The reducing side may quote inside `c`, down to `δ_min`, because GLFT values the drop in inventory risk above the fee.
    - With the §10 **placeholders**, the ask at q = +Q is at 34 ticks and loses about 14 ticks per ETH versus mid after the fee.
    - With the **calibrated** values (§10, last block) it is at ≈ 75.5 ticks, **outside** the 70.7-tick fee, so it does not go inside the fee at all. Going inside the fee is possible, not guaranteed: it needs `Q·ω > c1 + (Δ/2)·ω`.
    - Exiting as a taker instead would cost about 0.5 tick of spread plus a 5 bp fee (≈120 ticks at 2,400).
  - The floor only binds on the inventory-**reducing** side at large skew.
  - At default settings it never binds (§10). (Floor/rounding: not yet covered in lessons, planned L4.)
- **Joining the best bid or ask is allowed.** Crossing is not: the guard in step 7, plus post-only as a backstop.
  - With a 10-tick floor on a 1-tick book, joining only happens if the book is ≥ 20 ticks wide. The spread pause (§7) stops quoting before that.
- `bid_px < ask_px` always holds, because both sides are ≥ 10 ticks from `m`.
- If `m` is `None` (no quote yet, or after a gap), do nothing.

### 4.5 Order size and inventory limit

- Use one bid and one ask at most. Never more than one live order per side.
- Exposure counts **every** order the strategy believes is live: statuses `pending_new`, `open` and `pending_cancel`, plus any order with an in-flight replace.

```
headroom_buy  = Q − q − Σ remaining(our live buy orders other than the one being re-priced)
headroom_sell = Q + q − Σ remaining(our live sell orders other than the one being re-priced)
qty_b = round_qty( min(Δ, headroom_buy) )        state.round_qty (rounds DOWN to 0.001)
qty_a = round_qty( min(Δ, headroom_sell) )
quote a side only if  qty ≥ min_qty (0.001)  AND  qty · px ≥ min_notional (20 USDT)
```

- **At the limit, the side that would increase |q| is not quoted.** If that side has a live order, cancel it.
- **Near the limit, size is reduced** to the headroom. This happens naturally, because partial fills (the engine caps fills by print size) leave `q` off the Δ grid.
  - At 2,400 the minimum viable size is 0.009 ETH (20/2400 rounded up to the lot), so in practice `Δ = 0.01` either quotes in full or not at all.
- **Shrink in place (decision a, confirmed with a correction).** If a live order's remaining size exceeds the current headroom but `round_qty(headroom)` is still quotable (≥ min_qty and ≥ min_notional), send `ReplaceOrder(id, price=px_live, qty=round_qty(headroom))`. If it is not quotable, cancel. This takes priority over the requote rule and ignores `min_requote_interval_ms` (it is a limit check, not a requote).
  - It does not keep queue priority: Binance re-queues every modify ("modified orders will be reordered in the match queue"), and so does the engine by default (`exchange.replace_keeps_priority: false`). Shrinking is still preferred to cancel + new (one action, no moment with zero or two orders).
  - Live Binance `PUT /fapi/v1/order` needs **both** `price` and `quantity`, and `quantity` is the order's total size: send `executedQty + new remaining` (§4.6).
- The inventory-reducing side always quotes size `Δ`, even when |q| > Q (it only shrinks |q|).
- In the GLFT formula `q` is the continuous `state.position`; it does not need to be a multiple of Δ.
- Exchange backstop in the YAML: `exchange.max_position: Q`. Any `risk_max_position` reject means the strategy's own check is wrong (a bug). Count and report them.
- `state.position` lags fills by the report latency (~240 ms). The headroom rule counts the order itself, so a lagging position cannot add more than the order already being counted.

### 4.6 Requote rule (avoid churn under ~400 ms round-trip latency)

Per side, on every quote cycle:

```
desired (px*, qty*) from §4.4–4.5, or "none" if the side must not quote

no live order, desired != none, side not in cooldown        -> PlaceOrder(new_id("b"/"a"), side, qty*, px*, post_only=True)
live order, desired == none                                  -> CancelOrder (once; not again while pending)
live order, an action on it still in flight                  -> do nothing
live order, |px* − px_live| ≥ requote_ticks·θ, and
    (now − last_action_ns[side] ≥ min_requote_interval_ms
     OR urgent)                                              -> ReplaceOrder(id, price=px*, qty=qty*)
otherwise                                                     -> keep (queue priority is kept)

urgent (the dangerous direction: our order is too close to mid):
    bid:  px_live − px* ≥ urgent_requote_ticks·θ
    ask:  px* − px_live ≥ urgent_requote_ticks·θ
```

- **Always pass an explicit size on a replace.** A partially filled order's remainder can be below the 20 USDT minimum notional, and a replace that keeps it would be rejected.
- **Replace size after a possibly unseen fill (validator F5).** A fill may already have happened that we have not heard about (report latency ~240 ms). Rule:
  - The strategy computes the desired **remaining** `r* = qty*` from §4.5 with the position and fills it has seen.
  - It sends the order's **total** size: `total = filled_known + r*`, where `filled_known` is the filled qty of that order it has seen. This is Binance's native modify semantics (§4.5; that remaining is computed against executedQty at processing time is my reading of the Modify Order docs, to confirm on testnet): the exchange sets remaining = `total − executedQty`, and cancels the order if that is ≤ 0.
  - Then any unseen fill X is subtracted from the new remaining automatically, so exposure = `q_seen + r* ≤ Q` exactly, with no worst-case haircut.
  - The engine implements the same total-size semantics (`total_qty`); any `risk_max_position` reject is an H1 failure.
  - **If `total ≤ executedQty` (decision N4):** the Binance docs say the modify **cancels** the order, and the engine now does the same (`SimExchange._replace` sends `canceled`, reason `qty_below_filled`). On `canceled` the side is empty and the next cycle may place a new order. The strategy's `replace_rejected: qty_below_filled` branch (old order still live; the next cycle re-sizes or cancels it) is kept for live safety. It cannot fire in the backtest.
- "In flight": set when an action is sent. Clear it on the matching `OrderUpdate` (`accepted`, `rejected`, `replaced`, `replace_rejected`, `canceled`, `cancel_rejected`) or on a `Fill` with `leaves_qty == 0`. **Exception: a flatten order** stays in flight until it is terminal (`Fill` with `leaves_qty == 0`, `canceled` or `rejected`), never cleared on `accepted`.
- **Rejects:**
  - On `post_only_would_cross` (or any other reject not listed next), put that side in cooldown for `reject_cooldown_ms`.
  - **Fill races, no cooldown, never in the reject storm:** `unknown_order`, and `replace_rejected` with `notional_below_min` or `qty_below_filled`. All three mean a fill we have not heard about yet. They follow the explained/unexplained rule of §7.1. The next cycle re-sizes, re-places or cancels.
  - On any other `replace_rejected`, the old order is still live (engine rule); retry at the next cycle after cooldown.
  - `unknown_order` on a replace or cancel means the order already filled or was cancelled. Drop it from the strategy's local book.
- Expected load: at most about 1 action per second per side, plus rare urgent ones. That is ≤ ~150 actions per minute. All actions also pass the rate guard (§7.4).
- **Live difference:** on Binance a post-only (GTX) modify whose new price would cross is **cancelled**, not rejected (Modify Order docs). The engine instead returns `replace_rejected` and keeps the old order. The paper/live adapter must report that case as `canceled` with reason `post_only_would_cross`; the strategy then treats the side as empty, applies `reject_cooldown_ms`, and counts it toward the reject storm (§7.1).

---

## 5. Strategy lifecycle (what each callback does)

| Callback | Action |
|---|---|
| `on_start` | Load frozen params. Record `equity0 = state.cash` (position is 0) and `peak = equity0`. Pass the calibration snapshot through to the report |
| `on_event` (quote) | Update the live vol estimator (§7.3) and `last_quote_ts`. Run the risk checks (schedule below), then the quote cycle |
| `on_event` (funding) | Nothing beyond the state update (§4.3) |
| `on_timer` (1 s) | Risk checks (schedule below), then the quote cycle (this catches requotes held back by `min_requote_interval_ms`) |
| `on_fill` | Update the in-flight flags. Inventory-breach check. Run an **immediate quote cycle** that ignores `min_requote_interval_ms`, because `q` changed and so did both target prices |

**Risk-check schedule (decision d, confirmed):**

| Check (§7.1) | quote event | timer | fill |
|---|---|---|---|
| Vol spike | | ✓ | |
| Drawdown | | ✓ | ✓ |
| Spread blow-out | ✓ | | |
| Stale quotes | | ✓ | |
| Inventory breach | | ✓ | ✓ |

(Matches the code as reviewed in v1.1. A stale check on a fresh quote event is trivially false, and the spread can only change on a quote event, so nothing is lost.)

- Vol on the timer is enough: σ_live only changes once per `vol_sample_ms`. Drawdown on the timer means up to 1 s of delay, which is immaterial against a 5 USDT limit with Δ = 0.01 ETH.

**Fee consistency (decision e, confirmed):** the strategy cannot see the exchange config, so no start-up assert. Instead a unit test loads each GLFT YAML and asserts `strategy.params.maker_fee == exchange.maker_fee`, `min_notional_usdt == exchange.min_notional`, and `max_inventory == exchange.max_position == metrics.inventory_limit`. For paper/live, the adapter should compare `maker_fee` with the account's commission rate (`GET /fapi/v1/commissionRate`) at start and refuse to run on mismatch.
| `on_order_update` | Update in-flight flags, local order book and reject counters (§7) |
| `on_gap` | See §7.2 |
| `on_stop` | Nothing (the engine marks inventory at mid and reports the touch-exit value) |

### 5.3 Quote cycle

```
if killed or paused or m is None or in gap-cooldown:  ensure no live orders (CancelAll once); return
compute desired bid and ask (§4.4–4.5); apply the requote rule (§4.6) per side; return the actions
```

---

## 6. Calibration (offline, `in_sample` only, then frozen)

- **Rule:** run once on `data.periods.in_sample` (2024-03-29 00:00–24:00 UTC) with `mm.strategies.glft_calibrate`. It writes `config/strategies/glft_calibrated.yaml`; copy σ, A, k, γ into `strategy.params` of `config/backtest/glft_smoke.yaml`.
- Write diagnostics (fit tables, plots, R²) to `reports/<date>_glft_perp_calibration/`.
- **Never** look at, refit on, or tune on `out_of_sample`.
- Online or rolling recalibration is **out of scope for v1**. That makes it easy to attribute results to the parameters.
- (Calibration: not yet covered in lessons, planned L6.)
- All calibration uses **exchange time** `ts_ns`. Drop any sample interval that overlaps a data gap or a quote silence longer than 2 s.

### 6.1 σ: realised volatility of mid

1. Build a 1 s grid `t_i` over the period. Set `m_i` = the mid of the last quote at or before `t_i`.
2. Take increments `x_i = m_i − m_{i−1}`. Drop increments whose interval overlaps a gap.
3. Estimate `σ̂ = sqrt( Σ x_i² / (N·Δt_σ) )`, with `Δt_σ = 1 s`. Use no demeaning (the drift is negligible at 1 s).
   - Rolling vs EWMA: neither. Use **one number over the full in-sample day**.
4. **Noise check:** repeat with `Δt_σ = 10 s`. If the 1 s and 10 s estimates differ by more than 20%, report it.
   - On a 1-tick book, bid–ask bounce is small; a gap that large suggests microstructure noise or clustering.
   - Still use the 1 s value.
5. Also report the hourly σ̂ profile. It shows the constant-σ assumption failing (§8).

- Default (placeholder, **not calibrated**): `σ = 0.26 USDT/√s`. This is the 60%-annual-vol figure at 2,400 from L1 §6.
- At 3,500 and the same annual vol it would be about 0.37 USDT/√s.

### 6.2 A and k: fill intensity vs distance, `λ(δ) = A·e^(−kδ)`

(Model idea: L1 §2 and L1 §7 "fills at distance δ from the current mid". Fitting: not yet covered in lessons, planned L3 and L6.)

The measured quantity is: "how often would a resting order of size Δ, placed at distance δ from the mid we last saw, have been **traded through** by at least Δ ETH within the next interval, given our latency?" This is a **through-only** rate. It does **not** match the base engine, which runs `require_trade_through: false` and also fills on queue depletion at our price and on quote crosses (known mismatch, §8).

Step by step, per side:

1. **Grid.** Use `Δt_λ = 500 ms` steps `t_i`. Set `m_i` = the mid at or before `t_i`.
2. **Latency shift.** The window an order based on `m_i` would face is `W_i = (t_i + L, t_i + L + Δt_λ]`, with `L = cal_latency_ms = 400 ms` (feed p50 ≈ 240 ms plus order ≈ 160 ms).
3. **Distances.** Use `δ_j = j·θ` for `j = 10, 15, 20, …, 150` ticks (5-tick bins).
4. **Hypothetical prices.** For each `i, j`:
   - bid: `b_ij = floor_to_tick(m_i − δ_j)`;
   - ask: `a_ij = ceil_to_tick(m_i + δ_j)`.
   This is the same rounding as §4.4.
5. **Hit test.**
   - Bid hit if Σ qty of **sell-aggressor** aggTrades in `W_i` with `price < b_ij` (strictly) ≥ Δ.
   - Ask hit if Σ qty of **buy-aggressor** aggTrades in `W_i` with `price > a_ij` ≥ Δ.
   - Aggressor side comes from the schema's explicit `side` field. Never invert `is_buyer_maker` by hand (market-data.md §2).
6. **Rate.** `λ̂_side(δ_j) = N_hits_side(j) / (N_valid · Δt_λ)` in 1/s, where `N_valid` is the number of grid steps with a valid `m_i` and an intact window.
   - Then `λ̂(δ_j) = (λ̂_bid + λ̂_ask)/2`, because the model is symmetric.
7. **Bins used.** Keep bins with `N_hits_bid + N_hits_ask ≥ 2·cal_min_hits` (`cal_min_hits = 20`). The fit range ends at the last kept bin, or at 150 ticks.
8. **Fit.** Weighted least squares of `ln λ̂(δ_j)` on `δ_j`, with weight `n_j = N_hits_bid(j) + N_hits_ask(j)`. The weight approximates the inverse variance of a log Poisson count. Then `k = −slope`, `A = exp(intercept)`.
   - Also report the unweighted OLS (hftbacktest-style) fit, per-side fits, and R².
9. **Sanity gates.** Stop and report rather than proceed if:
   - k ≤ 0;
   - the fit range ends below 60 ticks (we would quote outside it);
   - weighted R² < 0.9;
   - per-side k differ by > 30%.

- Defaults (placeholders, **not calibrated**): `A = 4.0 /s`, `k = 10.0 /USDT` (0.1 per tick). They were picked so the §10 example has realistic magnitudes. **Do not run a backtest on them.**
- **Caveats [Inference]:**
  - The bins are nested (a hit at 50 ticks is also a hit at 45), so they are not independent, and the WLS standard errors are too small. Report a block bootstrap by hour (200 resamples) for A and k.
  - The 500 ms grid caps λ̂ at 2/s. Near 10 ticks, saturation can flatten the curve and bias k down. Variants with 100 ms and 1000 ms grids are in §9 H2.
  - Practitioners fit only a limited near-touch range because a single exponential fits poorly across the whole range. The hftbacktest tutorial uses the first 70 ticks. We fit only where we quote.
- **Trade-stream gaps (fixed in code).** Masking only quote silences > `cal_max_quote_silence_ms` would let a window with missing trade data count as "no hit" and bias λ̂ down. **Rule (implemented):** a separate key `cal_max_trade_silence_ms = 10000`. Drop grid step `i` (from `N_valid` and from the hit counts) if `W_i` overlaps either (1) a break in **aggregate** trade id continuity (`trade_id` ≠ previous `trade_id + 1`; `trade_id` is the aggTrade `a` field) or (2) a gap between consecutive aggTrades longer than `cal_max_trade_silence_ms`.
  - **Do not use `first_id`/`last_id`** (v1.2.1 fix). Those are raw trade ids, and Binance skips raw ids it never publishes; the jumps reconcile exactly with volume (data README). That test fired 12,882 times on live 2026-10-24 (median jump 1) and masked 15.8% of the day. The aggregate id is contiguous except at real outages: on 10-24 it has one jump (4,299 ids), at the 653 s network outage, which the 10 s silence rule also catches.
  - Why not reuse the 2 s quote key: quiet trade spells are real market information (a true "no hit"). The in-sample data has trade silences up to 5.3 s that are not feed gaps, so masking at 2 s would drop real quiet time and bias λ̂ **up**. 10 s is above every observed in-sample/OOS silence (5.3 s / 7.9 s), so it catches outages only.
  - Report the masked fraction of time. **Effect of the corrected rule:** 2024-03-29 masks **0** grid steps (the v1.2 "5 id gaps / 6 steps" were raw-id skips, not lost trades), so A = 0.3564 and γ = 0.2875 return to the pre-v1.1 values (σ, k unchanged). Live 2026-10-24 masks about 1 grid step plus the outage.

### 6.3 Order size Δ and max inventory Q (chosen, not fitted)

| | Default | Reason |
|---|---|---|
| `Δ` | **0.01 ETH** | Must clear the 20 USDT minimum notional at ETH ≥ 2,000 (0.01·2000 = 20). Small, so fills rarely exceed one print (fills are capped by print size). The fit uses the same Δ |
| `Q` | **0.05 ETH = 5·Δ** | Keeps the GLFT approximation in its good small-\|q\| range (Guéant 2017 uses Q/Δ = 4). 1σ inventory swing at Q over an hour: 0.05·0.26·60 ≈ 0.78 USDT |

### 6.4 γ: solved from a target skew (default), then frozen

(Risk aversion: L1 §8 teaser; full treatment not yet covered in lessons, planned L2 and L4.)

- Read literally, γ is CARA risk aversion over USDT wealth. Numbers like 1 /USDT are meaningless as a utility, so we treat γ as a **knob that sets the skew**. This matches "skew tuned from data" in L0 §3.
- **Rule:** after σ, A and k are fitted, choose γ so that the total skew at max inventory equals `skew_at_Q_ticks`:

```
target:  Q · ω(γ) = skew_at_Q_ticks · θ      with ω(γ) = σ·c2(γ; A_use(c at the in-sample mean mid), k, Δ, ξ=γ)
solve:   1-D bisection on γ ∈ [1e-4, 1e3] (ω is increasing in γ); tolerance 1e-6 relative
first guess (ξΔ/k ≪ 1):  γ ≈ (ω_target/σ)² · 2·A_use·Δ·k / e
```

- Default `skew_at_Q_ticks = 25`. At max long, the ask then sits about 25 ticks closer to mid than at q = 0. With the §10 **placeholder** A, k that puts the reducing quote inside the fee (paying away about 12 ticks of edge per ETH to unload). With the **calibrated** values it stays outside: the +Q ask is ≈ 75.5 ticks vs a 70.7-tick fee (§4.4, §10, §13 item 2).
- Sensible range: 10–50 ticks. That gives γ from about 0.14 to 3.6 at the placeholder σ, A, k (γ ∝ target²).
- The solved γ is written into the YAML as a number. The live strategy just uses that γ (and recomputes c, A_use and ω from the live mid). Report γ, ω and `ω·Δ/θ` (ticks per lot).
- **No PnL-based tuning of γ** on 1 in-sample day. That would be overfitting, and the validator would reject it. The variants in §9 H3 are for understanding, not for selecting.

---

## 7. Risk limits and kill switch

(Kill switches are mentioned in paper-trading doc §4. The details are not yet covered in lessons, planned L7.)

### 7.1 Limits

| Condition | Measured as | Default | Action | Resume |
|---|---|---|---|---|
| Inventory at limit | §4.5 headroom | `Q = 0.05 ETH` | Stop quoting the side that increases \|q\| | Automatic |
| Inventory breach (should never happen) | `abs(q) > 1.5·Q` | 0.075 ETH | **Kill + flatten** | Never (this period) |
| Stale quotes | `state.now_ns − last_quote_ev.ts_ns > stale_quote_ms` (checked on the timer) | 3,000 ms | Pause: cancel all | Next fresh quote, after `gap_cooldown_ms` |
| Spread blow-out | `ask_px − bid_px > spread_pause_ticks·θ` | 20 ticks | Pause | Spread ≤ limit continuously for `pause_resume_ms` |
| Volatility spike | `σ_live > vol_pause_mult·σ` | 3.0× | Pause | `σ_live < vol_resume_mult·σ` for `pause_resume_ms` (2.0×) |
| Drawdown | `peak_equity − state.equity` (peak since start) | `max_drawdown_usdt = 5.0` | **Kill + flatten** | Never (this period) |
| Reject storm | count of `rejected`, `replace_rejected`, `cancel_rejected` in the last `reject_window_s` = 60 s, **except** `awaiting_resync` and **explained** fill races (`unknown_order`, `notional_below_min`, `qty_below_filled` on a replace; below); trips when count **>** 20 | `max_rejects_per_min = 20` | **Kill** (no flatten; probably a bug) | Never (this period) |
| Order rate / exchange lock | §7.4 | §7.4 | Throttle, or kill | §7.4 |

- `pause_resume_ms = 5000`.
- **Reject storm (decision b, revised after validator F4).**
  - An `unknown_order` reject on a replace or cancel is **explained** if a terminal report for that order id (a `Fill` with `leaves_qty == 0`, or `canceled`) arrives within `unknown_order_grace_ms = 2000` of the reject. That is the normal race with a fill we had not heard about yet (all 560 IS / 246 OOS were). Explained ones are counted and reported, never toward the storm.
  - A `replace_rejected` with `notional_below_min` or `qty_below_filled` (decision N4) is **explained** if a `Fill` for that order id (any size) arrives within the same grace window, or arrived up to `unknown_order_grace_ms` before it. Same treatment: counted, reported, never toward the storm.
  - An **unexplained** fill race (no matching report within the grace period) means the local book is out of sync. It is added to the storm count when the grace period ends.
  - 2000 ms is about 2× the p99 report path (feed p99 772 ms + report 240 ms).
  - `post_only_would_cross` still counts (116 / 66 per day, spread out). In paper/live, the GTX-modify cancel of §4.6 also counts.
- The stale-quote threshold is about 1.5× the largest archive quote silence (2.04 s, data README) plus latency.
- The 5 USDT drawdown limit is about 6× the 1σ hourly inventory swing at Q (§6.3), and far above expected fee drag per hour (§10: about 0.3 USDT per hour at default rates).
- **Flatten (v1.2, after validator N1: in a fast move all 5 IOCs at touch ± 5 ticks expired unfilled and −0.05 ETH was held for 7.2 h).** Rule: one cheap try, then market orders until flat. No attempt cap.
  1. `CancelAll`. Attempt 1: `PlaceOrder(side reduces q, qty=round_qty(|q|), price=touch ± flatten_slip_ticks·θ, tif="IOC", post_only=False)`.
  2. Attempts 2, 3, …: **MARKET** order, `PlaceOrder(…, qty=round_qty(|q|), price=None, tif="IOC", post_only=False)`, live with `reduceOnly=true`. Send one per timer tick, after the previous flatten order is terminal, while `|q| ≥ min_qty`. `flatten_ioc_attempts = 1` sets how many limit IOCs come first.
  3. **Never stop while not flat.** If `|q| ≥ min_qty` is still true `flatten_alert_ms = 10000` after the kill, enter state **`flatten_failed`**: log it loudly on every timer tick, record it in the report (time, |q|, attempts), and keep sending market orders. In paper/live this is the "human must look now" signal.
  4. While a gap or resync blocks orders (`awaiting_resync`), wait; resume at the first tick after resync. These rejects are expected, not failures.
  5. **Residual below min notional:** if a market order for `|q| < min_notional/mid` is rejected, stop and report `flatten_residual` (< 20 USDT of exposure). [Whether Binance exempts reduce-only orders from min notional: not verified.]
- Why market: it guarantees the exit in the engine. A market order walks the book as a taker (in the quote-only fill model it pays the sweep-penalty tiers: each tier as big as the touch and `sweep_penalty_ticks` worse). At Q = 0.05 ETH against a touch of usually many ETH, that is normally just the touch. Live, a market order can only fail on exchange-side protections, which `flatten_failed` surfaces.
- Flatten orders bypass the rate budget but are counted (§7.4). They pay the taker fee. Report: flatten cost, time from kill to flat, attempts, and the position held after the kill (backtester, N1).
- Each attempt is sized from the `q` seen once the previous flatten order is terminal. If that overshot through zero (at most Δ), the next attempt flattens the new `q` the same way.
- `flatten_on_kill: true` (default). With `false`, the kill only cancels. `flatten_max_attempts` is **retired** (replaced by the rule above).
- Pauses do not flatten. Inventory is left alone, because the inventory-reducing quote also stops (we cannot price safely while paused).

### 7.2 Gap handling (`on_gap`)
- The engine has already cancelled all our orders, cleared prices, and will reject new orders until resync.
- The strategy must:
  1. Mark all local orders as gone. Clear in-flight flags.
  2. Reset the live vol estimator.
  3. Enter **gap cooldown**: no quoting until a fresh quote has arrived **and** `gap_cooldown_ms = 5000` have passed since that quote.
- Rejects with reason `awaiting_resync` are expected during this window. Do not count them toward the reject storm.
- A trades-socket gap (`stream == "market"`) keeps the book, but our orders were still cancelled. Use the same cooldown, for simplicity.

### 7.3 Live volatility estimator (for the pause only, never in the quote formula)
- Sample the mid every `vol_sample_ms = 1000` ms (on the timer).
- **Decision c (confirmed):** bias-corrected EWMA over the actual sample interval `dt` (s), half-life in seconds:
  ```
  α   = 1 − 2^(−dt / vol_ewma_halflife_s)
  num ← (1−α)·num + α·x²/dt          x = mid change since the last sample
  den ← (1−α)·den + α
  σ_live = sqrt(num/den)
  ```
  At `dt = 1 s` this is the v1 rule (`α = 1 − 2^(−1/60)`). Dividing by `den` removes the start-up bias toward 0 (a plain EWMA started at 0 reads σ about √2 too low at sample 60, which delays vol pauses). Using the real `dt` keeps it correct when a timer tick is late. **Adopted as the spec rule** (validator-endorsed; already in code).
- Valid only after `vol_min_samples = 60` samples. Before that, the vol pause is off.

### 7.4 Exchange order rate limits (compliance guard)

**Binance USDⓈ-M limits, checked 2026-10-22:**

| Limit | Value | Scope | Counts | Source |
|---|---|---|---|---|
| `ORDERS` per 10 s | **300** | per account | New Order 1, **Modify Order 1**, batch place/modify 5 | live `GET /fapi/v1/exchangeInfo` `rateLimits` (serverTime 1792650932118); per-endpoint weights on the New Order and Modify Order doc pages |
| `ORDERS` per 1 min | **1,200** | per account | New Order 1, Modify Order 1, batch 1 | same |
| `REQUEST_WEIGHT` per 1 min | **2,400** | per IP | New/Modify Order **0**; Cancel All 1; batch cancel 1; single cancel: not shown on the page I could read (assume 1) | same |
| Modifies per order | **< 10,000** per order | per order | each modify | Modify Order doc page |
| Quantitative rules (UFR etc.) | UFR = 1 − filled base qty / placed base qty over the past 10 min, per symbol; ban at **UFR ≥ 0.99** once a minimum order count is reached | per symbol, account | **not verified** | see below |

- Cancels are not listed against the `ORDERS` limits; they use IP weight only.
- Exceeding a limit returns HTTP 429; continuing after 429 gives an IP ban (HTTP 418) of 2 min up to 3 days (General Info page). Rejected orders "are not guaranteed" to carry the order-count headers, so whether they count is unclear; count them.
- **Quantitative rules, not verified.** The developer docs list the indicators (UFR, TMV; endpoint `GET /fapi/v1/apiTradingStatus`, weight 1 per symbol, example `triggerValue` 0.995). The rule text is in Binance FAQ 4f462ebe6ff445d4a170be7d9e897272, which I could not load (geo-redirect). The formula and the 0.99 threshold above come only from a search-engine summary of that FAQ; the VIP0 minimum order count, the lock length, and whether modifies count as "placed" are **unknown**.

**Does the smoke run breach them?** (in-sample: 78,510 accepted places + replaces in 24 h)
- 10 s: peaks ≈ 2 per 2 s ≈ 10 per 10 s, vs 300. **No.**
- 1 min: ≈ 55 average (peaks ≈ 60), vs 1,200. **No.**
- IP weight: cancels only, a few per minute, vs 2,400. **No.**
- Modifies per order: ≈ 98 actions per fill on average; a long-lived order could reach 10,000 only after ~2.8 h of 1/s replacing. **Unlikely, but guard it.**
- **UFR: possible breach.** Filled qty / accepted qty was 0.9%, so UFR ≈ 0.991 if modifies count as placed qty, just above 0.99. Whether it binds depends on the unverified VIP0 order-count threshold (≈ 545 actions per 10 min here). **This is the real compliance risk, not the request-rate limits.**

**Guard (strategy-side, all in config):**

| Key | Default | Source of default | Rule |
|---|---|---|---|
| `rate_orders_10s_max` | 150 | 50% of 300 | Rolling 10 s count of sent `PlaceOrder` + `ReplaceOrder` (incl. flatten orders, incl. ones later rejected) |
| `rate_orders_1m_max` | 600 | 50% of 1,200 | Same, rolling 60 s |
| `rate_ip_weight_1m_max` | 1,200 | 50% of 2,400 | Rolling 60 s sum: 1 per `CancelOrder` / `CancelAll`, plus any REST calls the adapter makes |
| `max_modifies_per_order` | 9,000 | below the 10,000 cap | At this count, cancel and place a new order instead of a replace |
| `ufr_warn` | 0.98 | below the 0.99 ban | Report-only: rolling 10 min `1 − filled qty / (placed + replaced qty)`; log and count minutes above it |

- 50% margin because the limits are per account/IP (shared with anything else on the key) and our rolling window will not align with Binance's.
- **When a budget is used up:** send no new place or replace (urgent included) until the window frees up. **Cancels are always allowed** (they reduce risk and use a different budget). Count `rate_throttled` events in the report. If throttling happens at all at default settings, that is a bug in the requote rule; report it.
- **Paper/live only:** on HTTP 429, send nothing for `Retry-After` (60 s if absent; resting orders stay live); a second 429 within 10 min or any 418 → **kill**. Poll `GET /fapi/v1/apiTradingStatus?symbol=ETHUSDT` every 60 s; if `isLocked` → **kill** (no flatten, the API is locked); log indicator values ≥ `ufr_warn`. Cross-check our counters against the `X-MBX-ORDER-COUNT-10S/1M` and `X-MBX-USED-WEIGHT-1M` response headers.
- These are compliance caps, not tuning knobs. They never bind in the smoke run, so they do not change backtest results.

---

## 8. Assumptions that rarely hold (flagged)

| Assumption in GLFT | Reality on ETHUSDT | Consequence for this spec |
|---|---|---|
| Fills are **Poisson** at rate `A·e^(−kδ)`, independent of price moves | Flow is bursty and self-exciting. Far fills happen mostly when the mid **walks to** the quote (L1 §2 case b) | Fill rate and timing are misestimated. Markouts will be negative (H5) |
| **Constant σ** | Volatility clusters and jumps. 2024-03-30 was a Saturday, likely calmer than Friday 03-29 | Frozen σ is wrong part of the day. Vol pause as a guard; hourly σ profile reported (H7) |
| Mid is a **Brownian diffusion** with constant σ | **Price moves in jumps** (validator F6, IS 2024-03-29): 1 s mid moves have median **0 ticks**, std 25 ticks, kurtosis **27**; the top 1% of seconds carry **32%** of the variance | σ̂ is mostly a jump number. Fills come from jumps that sweep our quotes (through fills land ≈ 15 ticks from the pre-print mid), exactly where GLFT's diffusion and no-adverse-selection assumptions fail. A 25-tick skew at Q against a ≈ 100-tick half-spread leaves the reducing quote ≈ 75 ticks out, so a jump against a full position is not unwound (OOS drawdown kill at +Q). Flagged for v2 (§9 H2 list, item 7), not changed in v1 |
| Mid is a **martingale**, **no adverse selection** | Informed and fast flow (BTC, other venues), liquidation cascades | Spread capture is offset by inventory PnL right after fills (L1 §5) |
| Fill intensity depends **only on distance**, not on queue | Real fills depend on queue position. Base engine (`require_trade_through: false`) fills on trade-through **and** on queue depletion at our price **and** on quote crosses (in-sample smoke: 698 through, 23 queue, 81 cross of 802) | **Known mismatch:** calibration counts strict trade-throughs only, so λ̂ is a conservative through-only rate and the engine adds fills the model does not predict. This pushes realised/model up in H2, so the observed ≈ 0.52 understates the shortfall: through-only fills alone give ≈ 0.45 (698/2 per side vs 774). `pessimistic_fills` (`require_trade_through: true`) is the like-for-like comparison |
| Continuous re-pegging to the current mid | We requote only when ≥ 5 ticks off, at most about once per second, and about 400 ms late | Calibration is latency-shifted (§6.2 step 2) to reduce this mismatch, not remove it |
| Fee is constant per ETH | `c = f_m·m` moves with price | Recomputed live; error negligible |
| Fee charged on mid | Binance charges the maker fee on the **fill price**. Done exactly: bid side `k_eff = k/(1+f_m)`, `c = f_m·S/(1+f_m)`; ask side the same with `1−f_m` | Our method is exact only to O(f_m) ≈ 2e-4 relative. **Negligible; ignored in v1** |
| Linear (affine) quotes in q | True optimum is non-affine at large \|q\| | Q/Δ kept at 5 |
| 2024 market resembles today | The data is 2.5 years old (backtest README bias 7) | Results describe 2024, not 2026 |

---

## 9. Hypotheses for the backtest, and what falsifies them

Run on `in_sample` and `out_of_sample` separately, with the engine's standard sensitivity block. **Report each hypothesis with its numbers.**

| # | Hypothesis | Metric | Falsified if |
|---|---|---|---|
| H1 | **Implementation is correct** | Recompute quotes from logged inputs (m, q, params); engine `identity_gap()`; `risk_max_position` rejects | Any quote differs from the formula by ≥ 1 tick after rounding; identity gap > 1e-6 USDT; any `risk_max_position` reject |
| H2 | **The fitted λ(δ) predicts our fill rate** | OOS: realised fills per second of quoting time per side, vs `Σ A·e^(−kδ_posted)·dt / Σ dt` | Ratio outside [0.5, 2]. Also refit A, k with `Δt_λ ∈ {100, 1000} ms`: if k changes > 50%, the fit is grid-dependent, so treat A, k as soft |
| H3 | **Skew controls inventory** | Mean \|q\|/Q, time at \|q\| ≥ Q, inventory autocorrelation half-life. Compare base vs variant `skew_off` (ω forced to 0 in the skew term only, same h) | Base does not have lower time-at-limit **and** lower mean \|q\| than `skew_off` |
| H4 | **VIP0 makes it fee-negative (benchmark expectation)** | Net PnL; gross = spread capture + inventory PnL; break-even maker fee `f* = gross / Σ fill notional` | Net PnL > 0 in base **and** in `pessimistic_fills` and `higher_latency`. If so, first suspect fill-model optimism (cross fills, queue), and check "fills by mechanism" |
| H5 | **Fills are adversely selected** (GLFT assumes they are not) | Mean markout at 1/10/60 s vs mid at fill (L1 §5), with a 95% CI from bootstrap over fills | 10 s mean markout not significantly < 0. If falsified, check whether fills are mainly `cross` (data-timing artefact) |
| H6 | **Result is not an artefact of optimistic fills or latency** | Sign of net PnL, and fill count, under `pessimistic_fills`, `no_cross_fills`, `higher_latency`, `latency_spikes` | Net PnL changes sign, or fill count falls > 50%, under any variant. Then the base result is not trustworthy |
| H7 | **Constant σ is a material error** | OOS realised σ vs in-sample σ̂; hourly σ profile; PnL by hourly σ tercile | Expected to hold. Falsified if OOS σ is within ±15% of σ̂ **and** PnL does not differ by σ tercile (constant σ would then be harmless here) |
| H8 | **Exact fee folding beats the naive shift at equal γ** | Variant `fee_shift` (fee_mode "shift", γ unchanged): time at limit, \|q\| percentiles | `fee_shift` has the same or better inventory control. Then the e^(kc/2) amplification is not useful here |
| H9 | **Funding is negligible for v1** | Funding bucket / \|net PnL\| | > 10%. Then idea 5 (funding overlay) moves up |

### First smoke run (2026-10-22)
Source: `reports/2026-10-22_glft_smoke/summary.md`, calibrated params, seed 42. Only `base` and `no_cross_fills` were run, so H6 is untested. Status so far, not verdicts:
- **H2 (fill-rate model): FALSIFIED** (validator review `reviews/2026-10-22_glft_smoke.md`, F2). Realised fills ÷ Σ λ(δ_live)·dt, integrated over each live quote's real distance and resting time: **0.44 IS, 0.28 OOS**, outside [0.5, 2]. (The cruder 774/side/day vs ≈ 400 gave 0.52; the engine's queue/cross fills, absent from the through-only calibration, inflate both numbers, §8.)
  - **What it means:** we fill as if quotes were ≈ ln(1/0.44)/k ≈ 22 ticks (IS) to ln(1/0.28)/k ≈ 35 ticks (OOS) further from mid than posted. Inventory unwinds 2.3–3.6× slower than GLFT assumes, so the model's spread-vs-skew trade-off is miscalibrated: for the risk it implies, the 25-tick skew is too weak (see the jump row in §8).
  - **Likely causes (validator, not decomposed):** (1) calibration re-pegs a fresh order every 500 ms, while the strategy keeps an order until it is ≥ 5 ticks off and cannot act while a replace is in flight; (2) selection: 682 / 416 spread pauses cancel quotes in exactly the busy post-jump moments that carry most calibration hits; (3) A is grid-dependent (0.57 / 0.36 / 0.26 at 100 / 500 / 1000 ms; k moves only ~15%), so λ̂ is a hit probability per latency window, not a stationary Poisson rate.
  - **v1 is unchanged.** Parameters and formulas stay frozen; the owner decides on v2.
  - **Candidate v2 calibration changes (IS only, owner decision):**
    1. Simulate the requote rule in calibration: an order persists until ≥ `requote_ticks` off, with the latency and in-flight block, instead of re-pegging every grid step.
    2. Mask windows in which the strategy would be paused (spread, stale, vol, plus `pause_resume_ms` after).
    3. Estimate a continuous-time hazard (time to first through-fill of a resting order at δ) instead of per-window counts, so A no longer depends on the grid.
    4. Add an A-stability gate: A across the 100/500/1000 ms grids within ±30% of the 500 ms value, else stop and report.
    5. Add an H2 gate on IS: realised/predicted in [0.5, 2] in an IS backtest before any OOS look.
    6. Count at-price queue fills in calibration, or judge H2 under `pessimistic_fills`, so the calibration and engine fill rules match.
    7. Consider a jump-robust σ (e.g. bipower variation) and setting the target skew relative to h (validator F6; owner choice).
- **H4 (fee-negative at VIP0): consistent, not falsified.** Spread capture ≈ 30 ticks/ETH vs fees ≈ 71 ticks/ETH. In-sample break-even `f*` ≈ (2.21 − 0.07)/26,035 ≈ 0.8 bp, well below the 2 bp maker fee; OOS gross is negative. Capture (30 ticks) is about a third of the ≈ 100-tick half-spread the formula gives at q = 0 (c ≈ 71 + c1 = 1/k ≈ 27 + 2.5). Most fills are trade-throughs, which happen after the mid has walked toward our quote **[Inference]**. The report should add mean posted δ (already a required field).
- **H5 (adverse selection): consistent, CI missing.** Mean 10 s markout −1.41 bp in-sample, −1.73 bp OOS, with 1 s and 60 s the same sign. The bootstrap 95% CI and the markout reference (mid at fill vs fill price, L1 §5) must be in the report before H5 is called.
- No parameter changes follow from this. Parameters stay frozen; OOS is not used for tuning.

### v1.1 run (`reports/2026-10-22_glft_v1_1/`, review `reviews/2026-10-22_glft_v1_1.md`)
Base net PnL **−3.10 IS / −5.10 OOS** (seed 42; OOS was killed by drawdown, seeds 1–4 give −4.36 to −4.53 without a kill). All 52 validator runs are negative. Read each hypothesis on its own metric:
- **H3 holds.** Base vs `skew_off`, 5 seeds: mean |q| 0.018 vs 0.031 (IS), time at limit 2–4% vs 30–34%, in 10/10 seed-periods. `skew_off` loses less only because, pinned at ±Q, it fills ~18% less in a strategy that loses on every fill. That says "trade less", not "skew less".
- **H8 holds** the same way: `fee_shift` has worse inventory control in 10/10; its better PnL is the same fewer-fills effect.
- **H6 holds:** no variant changes sign; even `colocated` loses (−1.69 / −3.45). Magnitudes of killed runs are censored.
- **H4 holds:** break-even maker fee ≈ 0.8 bp vs 2 bp paid. **H5 holds:** 10 s markout CI below 0 (IS −0.198 [−0.287, −0.106] per ETH as reported by the validator; the CI is too narrow because fills cluster).
- **H2 still falsified** (0.44 / 0.28, stable across seeds). **H7 untested** (no hourly σ / tercile output yet). H9 holds (funding 1.3% of IS net).
- No v1 parameter changes follow. Do not drop the skew or switch to `shift` on PnL.

### Testing rules for variant comparisons (validator N2, N6)
- **Common random numbers:** feed delay, order jitter and strategy use separate RNG streams (backtester: `SeedSequence(seed).spawn(3)`), so variants differ only by their own decisions.
- **Seeds:** strategy-param variants (`base`, `skew_off`, `fee_shift`, `gamma_low`, `gamma_high`) run on **≥ 5 seeds** per period. Report mean ± sd and the paired sign count (variant better than base in how many of the seed-pairs).
- **Kills censor PnL** near −`max_drawdown_usdt`. Always show next to PnL: kill flag (cause, time), time at limit, mean |q|, fills. Judge H3/H8 on inventory metrics, not on PnL.
- **Cross-fill warning (backtester):** warn only if `net(base) − net(no_cross_fills)` exceeds the seed sd of base net PnL (same period). With one seed, print the gap without a warning, and state that it is untested for noise.

- Extra strategy-param variants to add to `sensitivity.variants`: `skew_off`, `fee_shift`, `gamma_low` (`skew_at_Q_ticks` 10, re-solved γ), `gamma_high` (50). They are for interpretation only, **not selection**.
- **Required report fields:**
  - fills per side and per mechanism;
  - mean posted δ (ticks);
  - fraction of time quoting each side;
  - number of pauses and kills by cause;
  - calibration snapshot (σ, A, k, γ, ω, fit range, R², bootstrap CIs);
  - seed.

---

## 10. Worked example (hand-checked test vector)

**Inputs** (placeholders from §6; mid set to the L1 numbers):
- Book 2400.00 / 2400.01, so `m = 2400.005`.
- σ = 0.26 USDT/√s, A = 4 /s, k = 10 /USDT, **γ = ξ = 1.0 /USDT** (fixed, for the test).
- Δ = 0.01 ETH, Q = 0.05 ETH, f_m = 0.0002, θ = 0.01, fee_mode "exact", δ_min = 10 ticks.

| Step | Formula | Value | Ticks |
|---|---|---|---|
| fee per ETH | `c = 0.0002 · 2400.005` | 0.480001 USDT | 48.00 |
| `ξΔ/k` | 1·0.01/10 | 0.001 | |
| `c1` | `(1/0.01)·ln(1.001)` | 0.0999500 USDT | 9.995 |
| `A_eff` | `4·e^(−10·0.480001)` | 0.0329187 /s | |
| exponent factor | `1.001^(1001)` | 2.719641 | |
| `c2` | `sqrt( 1/(2·0.0329187·0.01·10) · 2.719641 ) = sqrt(413.085)` | 20.3245 √s/ETH | |
| `ω` | `0.26 · 20.3245` | 5.28437 USDT/ETH | 5.284 per lot (Δ) |
| `(Δ/2)·ω` | 0.005·5.28437 | 0.0264219 USDT | 2.642 |
| `h` | `c + c1 + (Δ/2)ω` | **0.606373 USDT** | **60.64** |

Quotes (rounding: bid down, ask up; floor 10 ticks not binding; no-cross guard not binding):

| q (ETH) | skew `q·ω` | δ_b | δ_a | bid_px | ask_px | Sides quoted |
|---|---|---|---|---|---|---|
| 0 | 0 | 0.606373 (60.64 t) | 0.606373 (60.64 t) | **2399.39** | **2400.62** | both, qty 0.01 |
| +0.05 (= +Q) | +0.264219 (26.42 t) | 0.870592 (87.06 t) | 0.342154 (34.22 t) | (2399.13, **not sent**: headroom 0) | **2400.35** | ask only |
| −0.05 (= −Q) | −0.264219 | 0.342154 (34.22 t) | 0.870592 (87.06 t) | **2399.66** | (2400.88, **not sent**) | bid only |
| +0.04 | +0.211375 | 0.817748 | 0.394998 | 2399.18 (qty 0.01, headroom exactly 0.01) | 2400.40 | both |

- Check one value: `2400.005 − 0.606373 = 2399.398627`, which rounds down to **2399.39**, 61.5 ticks below mid.
- Reservation price at q = +Q: `r = 2400.005 − 0.264219 = 2399.740781`.
- Model fill rate at q = 0: `λ = 4·e^(−10·0.606373) = 0.00930 /s` per side, about **33 fills per hour per side**.
  - Net edge per fill versus mid is `(δ − c)·Δ = 0.126·0.01 = 0.00126 USDT`.
  - Fee per fill is 0.0048 USDT, so fees are about 0.32 USDT per hour at that rate.
- At q = +Q the ask is 34 ticks out, inside the 48-tick fee. Unloading there loses about 14 ticks per ETH versus mid after fees. That is the skew paying for risk reduction. **Placeholder-only** (see the calibrated block below).

**Second test vector (fee mode "shift", same γ = 1):**
- `c2 = sqrt( 1/(2·4·0.01·10) · 2.719641 ) = 1.84379`
- `ω = 0.479386 USDT/ETH` (0.48 ticks per lot, 2.40 ticks at Q)
- `h = 0.480001 + 0.099950 + 0.002397 = 0.582348` (58.23 ticks)
- q = 0: bid 2399.42, ask 2400.59.
- The skew is `e^(kc/2) = e^(2.4) ≈ 11.0`× smaller than in exact mode (placeholder k; ≈ 3.66× when calibrated).

**Third test vector (γ from target skew, §6.4, exact mode):**
- `skew_at_Q_ticks = 25`, so `ω_target = 0.25/0.05 = 5.0` USDT/ETH and `c2_target = 19.2308`.
- Bisection gives **γ ≈ 0.8953 /USDT**. Then `c1 = 0.099955`, `h = 0.480001 + 0.099955 + 0.025 = 0.604956` (60.50 ticks).
- q = 0: bid 2399.40, ask 2400.61.
- q = +Q: δ_a = 0.354956, so the ask is 2400.36.
- Tolerance for tests: γ to ±1e-4, prices exact.

**With calibrated values (context, not a test vector):** σ 0.2501, A 0.3564, k 3.666, γ 0.2875 (target skew 25), mid ≈ 3535.7.
- `c` ≈ 70.7 ticks, `c1` ≈ 27.3 ticks, ω = 5.00 USDT/ETH, `h` ≈ 100.5 ticks.
- q = +Q: δ_a ≈ 75.5 ticks (**outside** the 70.7-tick fee, +4.8 ticks net per ETH), δ_b ≈ 125.5 ticks.
- `e^(kc/2)` ≈ 3.66 (not 11). The placeholder claims "reducing side inside the fee" and "≈ 11×" do not hold here.

---

## 11. Parameter table

`strategy.params` in `config/backtest/glft_smoke.yaml` (read by `GlftParams` in `src/mm/strategies/glft.py`). Everything lives in config; nothing is a magic number in code.

| Key | Symbol | Units | Default | How to set |
|---|---|---|---|---|
| `sigma` | σ | USDT/√s | 0.26 *(placeholder)* | §6.1, in-sample, frozen |
| `A` | A | 1/s | 4.0 *(placeholder)* | §6.2, in-sample, frozen |
| `k` | k | 1/USDT | 10.0 *(placeholder)* | §6.2, in-sample, frozen |
| `gamma` | γ | 1/USDT | 1.0 *(placeholder)* | §6.4 bisection, frozen |
| `xi_equals_gamma` | ξ | — | true | Fixed (ξ = γ). false means ξ = 0 (uses the ξ = 0 limit forms) |
| `skew_at_Q_ticks` | — | ticks | 25 | Owner choice, range 10–50. Used only by calibration |
| `fee_mode` | — | — | "exact" | "exact" or "shift" (§4.2) |
| `skew_off` | — | bool | false | Variant only (H3): ω = 0 in the `q·ω` term, same h |
| `maker_fee` | f_m | fraction | 0.0002 | Must equal `exchange.maker_fee` (unit test, §5) |
| `order_qty` | Δ | ETH | 0.01 | §6.3 |
| `max_inventory` | Q | ETH | 0.05 | §6.3. Also set `exchange.max_position` and `metrics.inventory_limit` to Q |
| `min_notional_usdt` | — | USDT | 20 | Must equal `exchange.min_notional` |
| `min_qty` | — | ETH | 0.001 | Exchange min qty (§1, §4.5) |
| `min_half_spread_ticks` | δ_min/θ | ticks | 10 | = `cal_delta_min_ticks` |
| `requote_ticks` | — | ticks | 5 | Fixed. Half the floor; about 5% of h |
| `urgent_requote_ticks` | — | ticks | 15 | Fixed |
| `min_requote_interval_ms` | — | ms | 1000 | ≥ the ~400 ms round trip. Equals the timer |
| `reject_cooldown_ms` | — | ms | 1000 | Fixed |
| `stale_quote_ms` | — | ms | 3000 | §7.1 |
| `gap_cooldown_ms` | — | ms | 5000 | §7.2 |
| `spread_pause_ticks` | — | ticks | 20 | §7.1 |
| `vol_pause_mult` / `vol_resume_mult` | — | × σ | 3.0 / 2.0 | §7.1 |
| `pause_resume_ms` | — | ms | 5000 | §7.1 |
| `vol_sample_ms` / `vol_ewma_halflife_s` | — | ms / s | 1000 / 60 | §7.3 |
| `vol_min_samples` | — | count | 60 | §7.3 |
| `max_drawdown_usdt` | — | USDT | 5.0 | §7.1. Scale with Q |
| `max_rejects_per_min` | — | count | 20 | §7.1 (trips at > 20) |
| `reject_window_s` | — | s | 60 | §7.1 |
| `unknown_order_grace_ms` | — | ms | 2000 | §7.1: a fill race (`unknown_order`, `notional_below_min`, `qty_below_filled`) explained by a report inside this window is not a storm reject. The last two are new in v1.2 |
| `inventory_breach_mult` | — | × Q | 1.5 | §7.1 |
| `flatten_on_kill` | — | bool | true | §7.1 |
| `flatten_slip_ticks` | — | ticks | 5 | §7.1, limit-IOC attempts only |
| `flatten_ioc_attempts` | — | count | 1 | §7.1: limit IOCs before switching to market. **New in v1.2** |
| `flatten_alert_ms` | — | ms | 10000 | §7.1: not flat this long after the kill → `flatten_failed`. **New in v1.2** |
| ~~`flatten_max_attempts`~~ | — | count | — | **Retired in v1.2** (no attempt cap) |
| `rate_orders_10s_max` | — | count / 10 s | 150 | §7.4 (50% of Binance 300) |
| `rate_orders_1m_max` | — | count / 60 s | 600 | §7.4 (50% of Binance 1,200) |
| `rate_ip_weight_1m_max` | — | weight / 60 s | 1200 | §7.4 (50% of Binance 2,400) |
| `max_modifies_per_order` | — | count | 9000 | §7.4 (Binance < 10,000) |
| `ufr_warn` | — | ratio | 0.98 | §7.4, report-only |
| `print_stats_on_stop` | — | bool | true | Diagnostics only: one summary line at the end of a run |
| **Calibration-only** | | | | |
| `cal_sigma_dt_ms` | Δt_σ | ms | 1000 | §6.1 |
| `cal_sigma_check_dt_ms` | — | ms | 10000 | §6.1 step 4 noise check |
| `cal_noise_tol` | — | fraction | 0.20 | §6.1 step 4: report if 1 s and 10 s σ differ by more |
| `cal_max_quote_silence_ms` | — | ms | 2000 | §6: drop intervals overlapping a longer quote silence |
| `cal_max_trade_silence_ms` | — | ms | 10000 | §6.2: drop windows overlapping an aggTrade id gap or a longer trade silence |
| `cal_lambda_dt_ms` | Δt_λ | ms | 500 | §6.2 (variants 100, 1000) |
| `cal_latency_ms` | L | ms | 400 | §6.2. Feed p50 + order p50 |
| `cal_delta_min_ticks` / `max` / `step` | — | ticks | 10 / 150 / 5 | §6.2 |
| `cal_min_hits` | — | count | 20 per side per bin | §6.2 |
| `cal_bootstrap` | — | resamples | 200 (hour blocks) | §6.2 |
| `cal_min_fit_end_ticks` | — | ticks | 60 | §6.2 step 9 gate |
| `cal_min_r2` | — | — | 0.9 | §6.2 step 9 gate (weighted R²) |
| `cal_max_k_side_diff` | — | fraction | 0.30 | §6.2 step 9 gate (bid vs ask k) |
| `cal_gamma_min` / `cal_gamma_max` | — | 1/USDT | 1e-4 / 1e3 | §6.4 bisection bracket |
| `cal_gamma_rel_tol` | — | relative | 1e-6 | §6.4 bisection tolerance |

- Decision f: all of the above keys added by the developer are accepted with the `GlftParams` defaults. The `rate_*`, `max_modifies_per_order` and `ufr_warn` keys (v1.1) are in the code.

**Engine YAML** (backtest README):
- `run.seed` (record it);
- `data.streams: [trades, quotes, funding]`;
- `periods.in_sample: 2024-03-29`, `out_of_sample: 2024-03-30`;
- `fill_model.type: quote_trade` (defaults as in `config.py`);
- `latency.timer_interval_ms: 1000`;
- `exchange` as in §1, plus `max_position: 0.05`;
- `metrics.inventory_limit: 0.05`.

---

## 12. Open questions and gaps
1. **Calibrated values do not exist yet.** All of σ, A, k and γ above are placeholders. The first deliverable is the calibration report (§6). If the gates in §6.2 step 9 fail, come back to me before backtesting.
2. **Binance USDⓈ-M order rate limits:** request-rate limits verified 2026-10-22 (§7.4); the smoke run is far below them. Still open: the **quantitative rules** (UFR VIP0 threshold, lock length, whether modifies count). The FAQ could not be loaded here; the owner should read it in a browser before paper/testnet. The backtest does not model rate limits (README bias 8), so the strategy-side guard is the only check.
2b. **Engine vs live, for the backtester.** Fixed: replaces re-queue by default; replaces use total size; `total ≤ filled` is cancelled, as on Binance (§4.6, N4). Still open: a GTX modify that would cross is `replace_rejected` (old order kept) in the engine but cancelled on Binance, which slightly flatters. Confirm both Binance behaviours on testnet.
2c. **UFR (validator N3):** UFR ≈ 1.0 in ~92% of minutes at today's churn. If the unverified rule (§7.4) binds, it blocks paper/testnet, not the backtest. Verify the rule first; any churn redesign is an IS-only owner decision.
3. **Data README is out of date.** `data/processed/README.md` lists quotes and aggTrades only for 2024-03-30 and 2026-10, but `binance_um/quotes` and `agg_trades` for **2024-03-29** exist, and the demo config uses them as in-sample. The data-engineer should update the README and run the quality checks for 03-29.
4. **One day in-sample, one day out-of-sample** is very little: one regime, a Friday vs a Saturday. Treat every number as anecdotal until the live recorder gives weeks of L1. Bootstrap CIs are mandatory.
5. Engine detail to confirm: trades still reach the fill model when the strategy subscribes only to `quote` and `funding` (§2).
6. The GLFT 2013 paper's own equation numbering was **not re-checked** by me. The formulas here are verified against Guéant (2017) eqs. 4.6–4.7, which reduce to the survey's Δ = 1 form.

---

## 13. Consistency with lessons

The spec follows lesson notation: `m` mid, `q` position (ETH, + long), PnL identity `Σ s_i·(m_i − p_i) + Σ q·Δm − Σ f_i − Σ q·mark·ρ` (L1 §4), markout `side × (mid at t+h − fill price)` (L1 §5), fee `δ − c` per fill (L1 §3), and perp ETHUSDT with tick 0.01, lot 0.001, min notional 20, maker 2 bp, taker 5 bp. Points of difference:

| # | Doc + section | What it says | What this spec does | Why |
|---|---|---|---|---|
| 1 | research survey §1.2 [Inference] "Adding fees" | Add `c` to both quotes (`δ → δ + c`); "exact if the fill rate is unchanged by the shift. It is not exact otherwise." | Shifts by `c` **and** replaces `A` with `A·e^(−kc)` in the skew coefficient (fee_mode "exact"). The survey's version is kept as variant "shift" | Within GLFT the fee is exactly a change of variable `δ̃ = δ − c`, which multiplies the intensity by `e^(−kc)` (Appendix A). The simple shift gets the half-spread nearly right but under-states the skew by `e^(kc/2)` (≈ 11× at the §10 placeholders, ≈ 3.66× calibrated). Under the default target-skew γ, both give the **same ω**; h differs through `c1` by ≈ 0.5 tick (placeholders) or ≈ 0.13 tick (calibrated), so prices can differ by one tick (§4.2) |
| 2 | L1 §3 "fee-aware GLFT quotes sit at least ~48 ticks from mid"; L0 §1 table "~48+ ticks from mid"; survey §1.2 "quotes sit ~48 ticks from mid" | Quotes are never closer to mid than the fee | At q = 0 both sides are ≈ 60 ticks out (consistent). With inventory, the **reducing** side *can* move inside the fee distance, down to the 10-tick floor (34 ticks at q = +Q with the §10 placeholders). With the calibrated values it does not: ≈ 75.5 ticks at +Q vs a 70.7-tick fee | That is what the GLFT skew does: it pays to unload risk. Forbidding quotes inside the fee would cap the skew. Lessons 00/01 already make this distinction, so this is now consistent |
| 3 | survey §1.2 formula (Δ = 1) | `δ_b ≈ c1 + ((2q+1)/2)·ω`, `ω = sqrt(σ²γ/(2kA)·(1+γ/k)^(1+k/γ))` | Uses the general-Δ form (Guéant 2017 eqs. 4.6–4.7) with q and Δ in ETH | Not a conflict: identical when q is counted in lots of size Δ and Δ = 1. **Warning:** plugging q in ETH into the Δ = 1 form while also using Δ = 1 ETH gives wrong numbers |
| 4 | survey §1.1 / L1 §8 (AS) | Reservation price `r = s − q·γ·σ²·(T−t)` | `r = m − q·ω` with `ω = σ·c2`, linear in σ, no horizon | A different model (stationary GLFT), not an error. Flagged so nobody "corrects" the σ vs σ² |
| 5 | L1 §2 and §7 "textbook assumes fills at distance δ from the *current* mid" | Textbook λ(δ) is measured against the current mid | Calibration measures distance against the mid seen ~400 ms earlier, over a 500 ms window | A deliberate departure, so the fitted λ describes what our latency-delayed quotes face. Lesson 3 or 6 may want to mention it |
| 6 | L0 §1, L1 §3 "48 ticks" | Fee ≈ 48 ticks per side (at mid 2,400) | Backtest data (2024-03) has ETH ≈ 3,500, so the fee ≈ 70 ticks | Same rule `c = f_m·m`; only the price differs. The worked example uses 2,400 to match the lessons |
| 6b | L0 §3 "How firms decide" | Width is set from data, "with a floor at fee + expected adverse selection" | The fee floor applies only to the position-opening side (both sides at q = 0). The reducing side may quote inside `c`, down to the 10-tick floor (§4.4) | GLFT prices the reduction in inventory risk above the fee. Unloading passively inside the fee (≈ −14 ticks per ETH at q = +Q, placeholders only) is still far cheaper than a taker exit (≈ −120 ticks). Not a contradiction if L0's floor is read as applying to quotes that open or add to a position. The tutor may want to say so |
| 7 | data/processed/README.md (not a lesson) | Quotes only for 2024-03-30 | Uses 2024-03-29 as in-sample | The files exist (§12 item 3) |
| 8 | backtest README "quote_trade" fill rules (not a lesson); spec v1 §6.2/§8 | v1 said calibration and engine share the trade-through rule | Calibration is through-only; base engine also fills on queue depletion and quote crosses (`require_trade_through: false`) | Known mismatch, stated in §6.2 and §8. It biases the H2 ratio up, so the observed ≈ 0.52 understates the shortfall (through-only ≈ 0.45) |
| 9 | L3 (`03-fill-probability.md`) step 5b table row, lines 199 and 241; `market-setup/market-data.md` line 48 | Trade gaps = aggTrade `first_id` ≠ previous `last_id + 1`; "5 id gaps, 6 steps masked → 172,776 valid"; "valid steps 172,782 → 172,776"; A 0.356368 → 0.356380 | Trade gaps = aggregate `trade_id` ≠ previous + 1, or silence > 10 s. 0 steps masked on 2024-03-29 | v1.2.1 fix (§6.2): raw-id skips are not lost trades. The tutor should update those lines |

No conflicts found with market-setup/ docs (venue, perp, fees, latency, fill rules, cancel assumptions) or with L0's decision.

---

## Appendix A. Why the fee is an exact change of variable (sketch) [Inference]

- **Model A** (CARA, Guéant 2017 §3): maximise `E[−exp(−γ(X_T + q_T·S_T))]`. Bid fills arrive at intensity `Λ(δ_b) = A·e^(−kδ_b)`, each for size Δ.
- Fee-free, a bid fill changes cash by `−(S − δ_b)·Δ`. With a maker fee `c` per ETH, it is `−(S − δ_b + c)·Δ = −(S − δ̃_b)·Δ` with `δ̃_b = δ_b − c`. The ask is symmetric.
- In `δ̃` the cash dynamics are the fee-free ones. The intensity is `A·e^(−kc)·e^(−kδ̃)`.
- The HJB equation and its solution are therefore the fee-free ones with `A → A·e^(−kc)`. The optimal `δ̃*` is GLFT's formula with that A, and `δ* = c + δ̃*`.
- **The trick relies on the exponential intensity** `Λ(δ) = A·e^(−kδ)`. Only then does `Λ(δ̃ + c) = (A·e^(−kc))·e^(−kδ̃)` remain a rescaled exponential. For a general Λ (Guéant 2017's general intensities), `Λ(δ̃ + c)` is a different function of δ̃, not just a rescaled A, so the GLFT formulas must be re-derived for it.
- This assumes `c` is constant. In reality `c = f_m·S` drifts with S, about 1% per day, and is recomputed live.
- I derived this; I did not take it from a paper. It is standard algebra, but I have not found it stated in GLFT or Guéant (2017).

---

## References
- Guéant, O., Lehalle, C.-A. & Fernandez-Tapia, J. (2013). *Dealing with the inventory risk: a solution to the market making problem.* Mathematics and Financial Economics 7(4), 477–507. https://arxiv.org/abs/1105.3115 (cited via the survey; equation numbers not re-checked)
- Guéant, O. (2017). *Optimal market making.* Applied Mathematical Finance 24(2), 112–154. https://arxiv.org/abs/1605.01862. Eqs. (4.6)–(4.7), the ξ definition (§3.4: ξ = γ in Model A, 0 in Model B; "risk aversion … applying to non-execution risk only"), and the accuracy remarks were checked on the arXiv HTML v5, 2026-10-22. The journal reference is from the survey; the arXiv page lists none.
- Avellaneda, M. & Stoikov, S. (2008). *High-frequency trading in a limit order book.* Quantitative Finance 8(3), 217–224. https://doi.org/10.1080/14697680701381228
- Practitioner, not peer-reviewed: nkaz001, *hftbacktest*, tutorial "GLFT Market Making Model and Grid Trading" (`compute_coeff`, 100 ms arrival-depth intensity measurement, fit on the first 70 ticks, 10-minute rolling window, ad hoc skew scaling `adj2`). https://hftbacktest.readthedocs.io/en/latest/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html (accessed 2026-10-22)
- Binance developer docs, USDⓈ-M Futures (all accessed 2026-10-22):
  - Live `GET https://fapi.binance.com/fapi/v1/exchangeInfo`, `rateLimits` array (REQUEST_WEIGHT 2400/1 min; ORDERS 1200/1 min; ORDERS 300/10 s).
  - General Info (limits per IP vs per account, 429/418, ban 2 min–3 days, rejected orders and headers): https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
  - New Order (1 on 10 s and 1 min order limits, 0 IP weight): https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api
  - Modify Order (same weights; < 10,000 modifies per order; re-queued; price and quantity both required; GTX that would cross is cancelled): https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Modify-Order
  - Futures Trading Quantitative Rules Indicators (`GET /fapi/v1/apiTradingStatus`): https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Futures-Trading-Quantitative-Rules-Indicators
  - Binance FAQ "Binance Futures Trading Quantitative Rules": https://www.binance.com/en/support/faq/binance-futures-trading-quantitative-rules-4f462ebe6ff445d4a170be7d9e897272 (**not read**: geo-redirected; UFR formula and 0.99 threshold taken from a search-engine summary only)
- Project docs: [research survey](../research/binance-eth-market-making-survey.md), [spot vs perp facts](../research/binance-eth-spot-vs-perp-facts.md), [backtest README](../src/mm/backtest/README.md), [data README](../data/processed/README.md), learnings L0, L1, market-setup/.
