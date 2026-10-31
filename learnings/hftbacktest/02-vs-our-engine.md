# 02. hftbacktest vs our engine

*Written 2026-10-30, updated the same day (no-partial-fill default, renamed variants, full-day run time). Track: [hftbacktest/](README.md). Our engine: [`src/mm/backtest/README.md`](../../src/mm/backtest/README.md). Port: `hft/glft_hft.py` (its docstring lists every difference). Results: [05](05-first-results.md).*

## Key ideas
- Same strategy maths, same data, **different simulator**. That is what makes it a cross-check.
- Biggest design gap: **our engine calls the strategy** (callbacks). **hftbacktest is called by the strategy** (`elapse` + polling).
- hftbacktest is **much faster**: a full day of GLFT in **~24 s**, against **12–15 min** for our engine.
- The realism knobs differ: **recorded** feed latency vs our drawn table; **risk_adverse, all-or-nothing** fills vs our **proportional, partial** fills; **modify / cancel+new** vs our `ReplaceOrder(total_qty)`.
- The port drops most risk overlays. In a crash that difference dominates everything else ([05 §3](05-first-results.md)).

---

## 1. Side by side

| Topic | Our engine (`src/mm/backtest/`) | hftbacktest port (`hft/`) |
|---|---|---|
| **Who drives the clock** | Engine. It pushes each event into `on_event`, `on_timer`, `on_fill`, `on_gap`, `on_order_update` | Strategy. It calls `hbt.elapse(100 ms)`, then looks at the book and its orders |
| **Language** | plain Python classes | numba `@njit` function + Rust core |
| **Speed** | live day ≈ **12–15 min** per process (~35k events/s, l2 model; `glft_live.yaml` comment) | full day **24–25 s**; hour 10: 14 s incl. a few s of compile |
| **Data prep** | reads processed parquet directly via `mm.data` | offline conversion to `.npz` (`hft/convert.py`): **262 s** for the whole day |
| **Depth** | L2 book from snapshot + diffs; warm-up from the last snapshot (`warmup_from`) | ROI vector depth; initial snapshot file replayed from the last snapshot (2026-10-23 19:06:24) |
| **Queue model** | `l2`, `cancel_assumption: proportional` (= power_prob n=1, my reading) | `risk_adverse` (base); `power_prob(3)` in the `queue_prob3` variant (**more** optimistic) |
| **Partial fills** | yes | **no**: NoPartialFillExchange. PartialFill crashes in 2.4.4 (error 14) |
| **Fill detection** | `on_fill` callback, exact time | poll every 100 ms; partial fills inside one step are merged |
| **Replace** | `ReplaceOrder(total_qty)`: one request, **back of queue** (Binance) | `modify(px, same qty)` for price moves; **cancel, then new** for size changes |
| **Feed latency** | **drawn**: p50 239 / p90 296 / p99 772 ms, one draw per 2 s window | **recorded**: depth p50 309 / p90 327 / p99 1,270 ms; trades 337 / 482 / 2,754 ms |
| **Order latency** | 132 ms + U(0, 59) ms per order; reports 240 ms | constant 160 / 240 ms (300 ms entry in `pess_latency`) |
| **Randomness** | seeded (feed draws, jitter, strategy rng) | deterministic with constant latency; the seed is used only for `intp_jitter` |
| **Gaps** | `GapEvent` → `on_gap`; book dropped until the next snapshot | `DEPTH_CLEAR` rows in the data + the strategy reads `gaps.json`, cancels, waits 5 s |
| **Risk overlays** | kill switch, vol/spread pause, stale-quote check, drawdown stop, rate guard | inventory cap, reject cooldown and gap pause **only** |
| **Metrics** | `metrics.py`: fills by mechanism, bootstrap CIs, multiple seeds, sensitivity grid | `postprocess.py`: same formulas (spread capture, markouts, PnL identity), no CIs, plus run-completion and live-order counters |

**Remember: if the two disagree, this table is the list of suspects.**

---

## 2. Who drives the clock

```
 OUR ENGINE (push)                              HFTBACKTEST (pull)
 ┌────────────┐  event  ┌──────────┐           ┌──────────┐ elapse(100ms) ┌────────────┐
 │  engine    │ ──────> │ strategy │           │ strategy │ ────────────> │ Rust core  │
 │  (clock,   │ <────── │ on_event │           │  @njit   │ <──────────── │ (clock,    │
 │  latency)  │ actions │ on_fill  │           │  loop    │  0 / 1        │  latency)  │
 └────────────┘         └──────────┘           └──────────┘ orders(),depth()└──────────┘
```
- Push: the strategy reacts to **every** event, instantly in simulated time. Fill handling is exact.
- Pull: the strategy wakes on **its own** schedule. That is cheap, but it learns about a fill up to one step late.
- Pull also means **you** must track order states. A just-sent order is `NONE` with `req = NEW` until it is acknowledged. Forgetting that leaked up to 22 live orders in the first port ([05 §5](05-first-results.md)).
- Our GLFT port imitates `on_fill` by running an extra quote cycle in the step right after it finds a fill.

**Remember: push = exact but slow in Python; pull = coarse but fast in compiled code.**

---

## 3. numba in practice

`@njit` compiles the function on its first call. The restrictions we hit in `glft_hft.py`:

| Can't | Did instead |
|---|---|
| pass a `dict` of params | a float64 array `P`, indexed by constants `P_SIGMA`, `P_A`, … |
| `for o in orders` | `it = orders.values(); while it.has_next(): o = it.get()` |
| index an Optional: `hbt.feed_latency(0)[0]` | `if fl is None: ...`, then unpack `exch_ts, local_ts = fl` |
| f-strings, rich exceptions, Python lists of objects | preallocated numpy record arrays (`MID_DTYPE`, `FILL_DTYPE`) |
| change a module constant after compiling | constants are frozen at compile time |

- The first call pays **seconds** of compile time. Every later call is fast.

**Remember: numba code looks like Python but only allows numbers, arrays and jitclasses.**

---

## 4. Replace semantics

| | Our engine | Binance | hftbacktest port |
|---|---|---|---|
| Move price | `ReplaceOrder`, back of queue | modify, **re-queued** | `modify(px, same qty)` |
| Change size | `total_qty` = new **total**; remaining = total − filled | modify `quantity` = new total | **cancel**, then a new order next cycle |
| Size cut keeps priority? | no (`replace_keeps_priority: false`) | no | n/a (new order) |

- Why cancel+new: a probe showed that `modify` with a new qty changed the **local** order, but the exchange side **filled the old size**. That would break the inventory cap.
- Cost: between the cancel and the next cycle (up to 1 s), that side has **no quote**.
- `--requote-mode cancel_new` uses cancel+new for price moves too.
- Whether hftbacktest's `modify` keeps queue priority was **not checked**. At ~75 ticks deep it matters little.
- Consequence: "new orders" counts different things on each side, so **fill rates are not comparable** (13% vs 71–86%).

**Remember: same intention, different mechanics. Never compare order-based ratios across the two.**

---

## 5. Latency: drawn vs recorded

```
 feed latency (ms)     p50     p90     p99
 our engine table      239     296     772     drawn per 2 s window (measured 2026-10-19)
 recorded, depth       309     327   1,270     robust conversion of 2026-10-24
 recorded, trades      337     482   2,754
```
- Recorded latency is **higher and has a fatter tail**. Expect hftbacktest quotes to be staler, so somewhat **more adverse fills**. `pess_latency` confirmed this direction: more fills, bigger loss.
- The recorded level depends on the clock repair (L0 ≈ 306 ms, [03 §3](03-clocks-and-our-data.md)). A ±30 ms error in L0 shifts every latency by the same amount.
- Trades show more latency than depth for two reasons: depth rows are batched every 100 ms, and the 5-minute REST probe stalls the recorder (bursts up to ~3 s late).

**Remember: recorded latency includes real bursts; a drawn table only imitates them.**

---

## 6. Why an independent cross-check

- One simulator can be **wrong in ways its own tests can't see**. A test checks that the code does what we meant, not that what we meant is right.
- Two independent codebases making **the same assumption** (e.g. "trades come from the front of the queue") should agree. Agreement is evidence that there is no bug.
- Disagreement **localises** the problem. Change one knob at a time:

```
 ours (proportional, drawn latency) ─┐
                                     ├─ compare fills, markouts
 hbt (risk_adverse, recorded lat.) ──┘
        │ differ? → exclude crash hours (risk overlays) → queue variant → latency variant → port details
```
- What we got ([05](05-first-results.md)): fill counts differ (mostly the missing risk overlays), but **markouts and the sign of PnL agree** on both days.
- What it can't do: neither simulator knows the true queue or our market impact. **Only live fills can test that.**
- Firms call this **effective challenge** (model risk management): an independent re-implementation, plus reconciliation of simulated against live fills.

**Remember: agreement is evidence, not proof; disagreement tells you where to look.**

---

## Decisions log
- **2026-10-30.** See [README.md Decisions log](README.md#decisions-log). Relevant here: (a) separate venv, (d) gap handling, (e) GLFT port with cancel+new for size changes, (g) NoPartialFill as the default.

---

## Check your understanding
1. Our GLFT port finds a fill on a poll, 80 ms after it happened. What does our engine do differently at that moment, and could it change the next quote?
2. Recorded feed p99 is 1,270 ms against 772 ms in our table. Which engine would you expect to show worse 1 s markouts, and why?
3. Our engine allows partial fills and hftbacktest (as run) does not. With Δ = 0.01 ETH and an average trade of ~0.78 ETH, how much should that matter?

*Answers on request.*
