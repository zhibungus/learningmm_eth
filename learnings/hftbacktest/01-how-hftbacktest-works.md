# 01. How hftbacktest works

*Written 2026-10-30, updated the same day (pending-order state, exchange models). Track: [hftbacktest/](README.md). Facts checked against installed 2.4.4 ([research §9](../../research/hftbacktest.md)). Our code: `hft/common.py`, `hft/glft_hft.py`, `hft/example_mm.py`.*

## Key ideas
- **Rust does the simulation. You write the strategy as a numba `@njit` loop** that calls methods on an `hbt` object.
- Data is one flat **event array**. Every row has **two clocks**: `exch_ts` (when the exchange did it) and `local_ts` (when we heard it).
- The **exchange side** replays rows at `exch_ts`. The **strategy side** sees them at `local_ts`. **Feed latency is in the data.** Order latency is a model.
- The strategy **pulls time forward** (`hbt.elapse(ns)`) and **polls** for fills. There are no callbacks. A just-sent order is `NONE` with `req = NEW`, and it is still **live**.
- Our queue position is **estimated** from L2 depth by a queue model. We use the all-or-nothing exchange, because partial fills crash in 2.4.4.
- Fees: **positive = cost**, negative = rebate. Watch out for the ROI depth's `best_bid_qty` bug.

---

## 1. The pieces

```
  .npz event files ──┐
  initial snapshot ──┤      Rust core (hftbacktest crate)
                     ├──>  ┌──────────────────────────────────────────┐
  BacktestAsset()    │     │ EXCHANGE processor   <- rows at exch_ts  │
   .tick_size ...  ──┘     │   book, queue model, fills, fees         │
                           │         ^ orders (+entry latency)        │
                           │         v responses (+resp latency)      │
                           │ LOCAL processor      <- rows at local_ts │
                           │   our view of the book, our order dict   │
                           └──────────────────────────────────────────┘
                                         ^ hbt (numba jitclass)
                           @njit def strategy(hbt, ...): while hbt.elapse(...) == 0: ...
```
- `BacktestAsset()` is a **builder**. Each method returns the asset, so calls chain (`hft/common.py` `build_asset`).
- `ROIVectorMarketDepthBacktest([asset])` turns the asset into `hbt`.
- `hbt` is a numba **jitclass**, so it can be used inside `@njit` code. Its calls go to Rust through C function pointers.

**Remember: you configure in Python, then hand `hbt` to a compiled loop.**

---

## 2. The event array

One row = 64 bytes (`event_dtype`):

| Field | Meaning |
|---|---|
| `ev` | event code OR'ed with flag bits |
| `exch_ts` | exchange time, ns |
| `local_ts` | our receive time on the **exchange clock**, ns |
| `px`, `qty` | price; quantity (for depth rows: the **new absolute size** at that price) |
| `order_id`, `ival`, `fval` | unused for L2 (0) |

### Flags as bits
Codes are small integers. Flags are single high bits. Combine them with `|` and test them with `&`.

| Name | Value | Kind |
|---|---|---|
| `EXCH_EVENT` | 2³¹ = 2,147,483,648 | flag: the exchange processor handles it |
| `LOCAL_EVENT` | 2³⁰ = 1,073,741,824 | flag: our (local) processor handles it |
| `BUY_EVENT` | 2²⁹ = 536,870,912 | flag: bid side / buyer-initiated trade |
| `SELL_EVENT` | 2²⁸ = 268,435,456 | flag: ask side / seller-initiated trade |
| `DEPTH_EVENT` | 1 | code |
| `TRADE_EVENT` | 2 | code |
| `DEPTH_CLEAR_EVENT` | 3 | code |
| `DEPTH_SNAPSHOT_EVENT` | 4 | code |

Worked example: a bid-side depth update seen by both processors.
```
ev = EXCH_EVENT | LOCAL_EVENT | BUY_EVENT | DEPTH_EVENT
   = 2,147,483,648 + 1,073,741,824 + 536,870,912 + 1 = 3,758,096,385
ev & 0xFF            -> 1      (code: DEPTH_EVENT)
ev & BUY_EVENT != 0  -> True   (bid side)
```
- On a **trade** row, `BUY_EVENT` means the **aggressor bought** (lifted the ask).
- **A processor ignores any row that lacks its flag.**

**Remember: one integer carries kind + side + which processor(s) see it.**

---

## 3. Two clocks, two processors

A trade happens at the exchange at 10:00:00.000. We receive it 310 ms later and react.

```
 exchange clock ───────────────────────────────────────────────────────────>
  .000             .310              .400           .560           .800
   │                │                 │              │              │
   trade happens    we see it         we send        order reaches  we see the
   EXCH processor   LOCAL processor   an order       the exchange   ack / fill
   (exch_ts)        (local_ts)                       (+160 entry)   (+240 resp)
   │<─ feed 310 ms ─>│                 │<───── order round trip 400 ms ─────>│
```
- The exchange processor moves the "true" book and matches our resting orders at `exch_ts`.
- The local processor updates **our view** at `local_ts`. Our strategy only ever sees that view.
- So between `.000` and `.310` the market has moved but we don't know it yet. That gap is the **feed latency**.
- From `.000` to `.560` our old order is still exposed. Total "blind + slow" time here: **560 ms**.
- A real event is stored **once**, with both flags, if it sorts to the same place in both orders. Otherwise it is stored **twice**: an EXCH copy and a LOCAL copy (`correct_event_order`). In our hour-10 file about **3.5%** of events are split (2.32 M rows for 2.24 M events).

**Remember: the exchange acts on `exch_ts`; you react on `local_ts`. The difference is your blind spot.**

---

## 4. Depth: diffs, snapshots, clears

| Row | Effect on the book |
|---|---|
| `DEPTH_EVENT \| BUY/SELL` at px, qty | **Set** level px to qty. qty 0 deletes the level. (Binance `depthUpdate` sends absolute sizes.) |
| `DEPTH_CLEAR_EVENT \| BUY` at px | Delete all bids **≥ px** (from the best bid down to px). `\| SELL`: all asks ≤ px. |
| `DEPTH_SNAPSHOT_EVENT \| side` | Applied like a depth update. Used after a clear to rebuild the book. |
| `initial_snapshot(file)` | The book at the start, built from scratch. |

Worked example (bid side):
```
 before          row                              after
 2365.10  4.0    DEPTH_EVENT|BUY 2365.10 qty 6.5  2365.10  6.5   (set, not +6.5)
 2365.09  2.0    DEPTH_EVENT|BUY 2365.09 qty 0    (deleted)
```
- Our converter clears **a whole side** with bid px 0.0 / ask px 1e9 (every bid ≥ 0, every ask ≤ 1e9). See [03](03-clocks-and-our-data.md).

**Remember: depth rows say "the level is now X", never "add X".**

---

## 5. The strategy loop

There are no callbacks. The strategy advances time and then looks around.

```
 @njit
 def glft_mm(hbt, ...):
     while hbt.elapse(100 ms) == 0:      # 0 = OK, 1 = end of data, other = error
         process_orders(...)             # poll hbt.orders(0): new fills? rejects?
         depth = hbt.depth(0)            # our (delayed) view of the book
         ...gap check...
         if fill_found or every 10th step:   # quote cycle every 1 s, or right after a fill
             compute_quotes(...)
             hbt.modify / hbt.cancel / hbt.submit_buy_order / submit_sell_order
```
(`hft/glft_hft.py` `glft_mm`.)

| Call | What it does |
|---|---|
| `elapse(ns)` | processes all events up to now + ns. Returns 0, or 1 at the end of the data |
| `wait_next_feed(include_resp, timeout)` | jumps to the next feed row (returns 2) or order response (returns 3). Event-driven style |
| `submit_buy_order(asset, id, px, qty, GTX, LIMIT, wait)` | new order. GTX = post-only |
| `modify(asset, id, px, qty, wait)` / `cancel(asset, id, wait)` | change / remove |
| `orders(0).values()` | iterate with `while it.has_next(): o = it.get()` |
| `clear_inactive_orders(0)` | drops FILLED / CANCELED / EXPIRED orders |

### Order states you must track yourself

```
 submit ──> NONE, req=NEW ──(ack after ~entry+resp)──> NEW ──> PARTIALLY_FILLED ──> FILLED
            ^ "pending new": LIVE!                       └──> CANCELED / EXPIRED (GTX would cross)
```
- A just-sent order is `status NONE (0)` with `req = NEW` until its ack arrives (~400 ms here).
- **Treat it as live.** The first port treated it as finished, forgot it and quoted again. Result: up to **22 live bids** and inventory above Q (`PENDING_NEW` in `glft_hft.py` fixes it).
- `o.req != 0` means some action on the order is still in flight. The port then does nothing with that order.

### Fills by polling
- Each order holds `qty`, `leaves_qty`, `status`, `exec_price` and `exch_timestamp`. Filled so far = `qty − leaves_qty`. A new fill = that minus what we logged last time (`process_orders`).
- With a 100 ms step, two fills in one step become **one row**, and we react up to 100 ms late.
- `wait=False` everywhere: we don't block for the response. We see the result on a later poll.
- 2.x returns **int codes**. The 1.x style `while hbt.elapse(x):` exits at once. Check the final code: an error code (e.g. 14) means the run did **not** finish.

**Remember: you own the clock and the order bookkeeping. Nothing happens until you call `elapse`, and nothing is "gone" until the exchange says so.**

---

## 6. Latency

| Kind | Where it comes from | Ours |
|---|---|---|
| **Feed** (exchange → us) | `local_ts − exch_ts` of every row. **Data, not a parameter** | real recordings: depth p50 **309 ms**, p99 1,270 ms (robust conversion, 2026-10-24) |
| **Order entry** (us → exchange) | `constant_order_latency(entry, resp)` or `intp_order_latency(table)` | constant **160 ms** (300 ms in `pess_latency`) |
| **Order response** (exchange → us) | same | constant **240 ms** |

- 160 ms ≈ the mean of our engine's 132 ms + U(0, 59) ms jitter (161.5, rounded). The round trip of 400 ms matches the calibration's `cal_latency_ms`.
- `intp_order_latency` interpolates a `(req_ts, exch_ts, resp_ts)` table linearly. Our `--latency-kind intp_jitter` builds one on a 100 ms grid with seeded U(0, 59) ms jitter. Between grid points the jitter is smooth, not an independent draw per order.
- `latency_offset(ns)` can shift all feed latency at once. It is the only feed-latency knob.
- Professional firms measure order latency live, by sending orders that can't fill and timing the acks. We don't have that yet.

**Remember: feed latency is measured (in the data); order latency is assumed (a model).**

---

## 7. Queue models and exchange models

L2 data shows the **total** size at each price, not who is where. The queue model guesses our position.
- `front` = size ahead of us. At arrival, `front` = the whole level (we join the back).
- `back` = size behind us = level − front.
- **Trade at our price**: it comes from the front (FIFO). `front −= trade_qty` in **every** model.
- **Cancel** (the level shrinks without a trade): did it come from ahead of us or behind? This is where the models differ.
- We fill once `front < 0`, i.e. more has traded at our price than was ahead of us. A **trade-through** (a print beyond our price) or a **cross** (best ask ≤ our bid) fills us regardless of the queue.

| Model (builder) | Share of a cancel assumed to come from **behind** us, `p` |
|---|---|
| `risk_adverse_queue_model()` | all of it, unless the level drops below `front`: `front = min(front, level)` |
| `power_prob_queue_model(n)` | `back^n / (back^n + front^n)` |
| `log_prob_queue_model2()` (**the default!**) | `ln(1+back) / ln(1+back+front)` |

For the prob models: `front' = front − (1−p)·chg` (capped at the new level size).

### Worked example
We join a bid at 2365.00 that holds **30 ETH**. Then 20 ETH more joins behind us.
```
 2365.00 bid level:  [ 30 ETH ahead ][ us 0.01 ][ 20 ETH behind ]     front = 30, back = 20
```
**Case A: 10 ETH is cancelled** (level 50 → 40, no trade).

| Model | p (from behind) | front after |
|---|---|---|
| risk_adverse | 1 | min(30, 40) = **30.0** (no progress) |
| power_prob(2) | 400 / (400 + 900) = 0.31 | 30 − 0.69 × 10 = **23.1** |
| power_prob(3) | 8,000 / 35,000 = 0.23 | 30 − 0.77 × 10 = **22.3** |
| log_prob2 | ln 21 / ln 51 = 0.77 | 30 − 0.23 × 10 = **27.7** |

**Case B: a 10 ETH sell trade prints at 2365.00** (level 50 → 40).
- In every model: **front = 30 − 10 = 20**. The depth drop that follows is not counted twice.
- We fill after more than another 20 ETH trades at our price, or as soon as anything prints below 2365.00.

Notes:
- Every prob model advances us on cancels, so each one is **more optimistic** than risk_adverse.
- Our engine's `cancel_assumption: proportional` (share ahead = front/level) equals **power_prob with n = 1** *(my reading)*: p = 20/50 = 0.4, so front → 24.0.
- Our GLFT quotes sit ~75 ticks deep, and fills there are **sweeps** (trade-throughs). On 10-24, risk_adverse gave 1,188 fills and power_prob(3) gave 1,190 ([05](05-first-results.md)).

### Exchange models
| Builder | Behaviour | Status for us |
|---|---|---|
| `no_partial_fill_exchange()` | the order fills **in full** when a fill condition is met | **our default** |
| `partial_fill_exchange()` | fills up to the traded quantity when we are at the front | 2.4.4: `elapse` returns **error 14** after 25–60% of a day on our data. Unusable |

**Remember: trades always move you up the queue; the model only decides how much cancels help. Set the queue model explicitly; the default is not the most pessimistic one.**

---

## 8. Fees

`trading_value_fee_model(maker, taker)`: fee = rate × price × qty. **Positive = we pay**, negative = rebate.
- We use **0.0002 / 0.0005** (Binance USD-M VIP0).
- One maker fill: 0.01 ETH × 2,366 × 0.0002 = **0.00473 USDT**. Seven fills ≈ 0.033 USDT (hour-10 run: `fees_usdt` 0.0331).
- `state_values(0).balance` **excludes** fees; `.fee` holds them. Net = balance + position × mid − fee (`hft/postprocess.py`).

**Remember: a positive fee rate is a cost, and hftbacktest keeps it apart from the balance.**

---

## 9. ROI depth and its bug

- `ROIVectorMarketDepthBacktest` stores the book in a dense array between `roi_lb` and `roi_ub` (**prices**, not ticks). It is fast. Prices outside the range are **silently dropped**.
- We use ±50% around the snapshot mid (`roi_frac: 0.5`). `check_hbt.py` uses ±25%.
- **Bug (confirmed in 2.4.4):** `depth.best_bid_qty` / `best_ask_qty` return the **price**.
  ```
  best_bid = 2366.55  ->  best_bid_qty also = 2366.55   (wrong)
  use: depth.bid_qty_at_tick(depth.best_bid_tick)
  ```
- Our strategies read only `best_bid` / `best_ask`, so they are not affected. `HashMapMarketDepthBacktest` is correct.

**Remember: use a wide ROI, and never trust `best_*_qty` on the ROI depth.**

---

## Decisions log
- **2026-10-30.** Note written. The related decisions are in the [README.md Decisions log](README.md#decisions-log): (e) the GLFT port (`modify` only for price changes, cancel+new for size changes), (f) the book is built from depth only, with no bookTicker, and (g) NoPartialFillExchange is the default.
- The queue models used (risk_adverse in base, power_prob(3) in the `queue_prob3` comparison variant) are runner settings, not owner decisions.

---

## Check your understanding
1. A row has `ev = LOCAL_EVENT | TRADE_EVENT | SELL_EVENT`. Which processor sees it, and what happened in the market?
2. In the queue example, start again from front 30 / back 20. A 25 ETH cancel hits the level (50 → 25). What is `front` under risk_adverse? Under power_prob(2)?
3. Your strategy submits a bid, and on the next 100 ms poll the order shows `status NONE, req NEW`. Should the next quote cycle submit another bid? Why not?

*Answers on request.*
