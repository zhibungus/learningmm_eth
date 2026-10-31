# hftbacktest track: index and decisions

*Written 2026-10-30. A second, independent simulator for the GLFT strategy. Read [market-setup/market-data.md](../market-setup/market-data.md) (clocks, snapshot + diff) and [strategy-glft/](../strategy-glft/00-choosing-the-strategy.md) lessons 1–5 first.*

## Key ideas
- **hftbacktest** (2.4.4) is an open-source Rust simulator. You drive it from a numba `@njit` loop.
- We use it as a **cross-check** of our own engine, not a replacement. Same data, same strategy maths, different simulator.
- The hard work was **data**: turning our recordings into its two-clock event files, with a repaired PC clock.
- It lives in `hft/`, with its own venv `.venv-hft`. Nothing in `src/mm/` changed.
- **Result:** fill counts differ (mostly because the port has no risk pauses), but **both simulators show negative markouts and losses**. Adverse selection is confirmed independently ([05](05-first-results.md)).

## Notes (read in this order)
1. [01-how-hftbacktest-works.md](01-how-hftbacktest-works.md): event array and flags, the two clocks, EXCH vs LOCAL processors, depth, the `elapse` loop, latency, queue models (worked example), fees, the ROI bug.
2. [02-vs-our-engine.md](02-vs-our-engine.md): side-by-side with `src/mm/backtest/`, and why an independent cross-check is worth it.
3. [03-clocks-and-our-data.md](03-clocks-and-our-data.md): the converter, the PC-clock problem and the robust offset, snapshots, gaps, hourly files.
4. [04-how-to-run.md](04-how-to-run.md): venv, convert, check, run, variants, common errors.
5. [05-first-results.md](05-first-results.md): hour 10, full days 10-24 and 10-25 side by side with our engine, why they differ, what agrees, and lessons from the bugs.

Sources: [`research/hftbacktest.md`](../../research/hftbacktest.md), [`hft/README.md`](../../hft/README.md), `hft/results/COMPARISON_INPUT.md`, the commented code in `hft/`.

---

## Decisions log

Seven decisions made on **2026-10-30**. For each: the options, how they differ, the choice and why, and how firms usually decide.

### 2026-10-30 (a) Separate venv `.venv-hft` + `requirements-hft.txt`

| Option | Effect |
|---|---|
| Add hftbacktest to `.venv` / `requirements.txt` | One env. But main pins **numpy 2.5.4**, while hftbacktest needs **numpy < 2.3** (via numba). pip cannot satisfy both. |
| Downgrade the main env's numpy | Changes the numbers and behaviour of every existing run and test. |
| **Separate venv** | Two envs to maintain. The main project is untouched. |

- **Chosen: separate venv.** Installed with `--only-binary=:all:` on Python 3.13.14: hftbacktest 2.4.4, numba 0.68.1, numpy 2.2.6, polars 2.1.0.
- Why: the cross-check must not disturb the reference engine. A version clash is a hard blocker, not a preference.
- Firms: one locked environment **per service or tool** (containers, conda envs, or a monorepo build tool that pins versions). A shared "one big env" breaks as soon as two tools pin different numpy. **Isolate tools whose pins clash.**

### 2026-10-30 (b) Our own converter, not `hftbacktest.data.utils.binancefutures.convert`

| | Theirs | **Ours (`hft/convert.py`)** |
|---|---|---|
| Input | gzip text, one line = `<local ns> <ws JSON>` (the author's collector) | our processed **parquet** (depth, trades, gaps, quotes) |
| Trades | raw `trade` stream, `X == 'MARKET'` | **aggTrade** (what we record) |
| `exch_ts` | `T` | `T` (`ts_ns`) |
| `local_ts` | line prefix as is (PC clock) | `recv − offset(t)`, **robust** clock fix |
| Gaps | none | whole-book clear + `gaps.json` |

- **Chosen: our own.** Theirs cannot read our files and does not handle aggTrade. Writing a reader into their format would still leave the clock problem.
- It still calls their `correct_event_order` and `validate_event_order`, so the ordering logic is theirs.
- Firms: they own the **normaliser** (raw feed → internal schema) and adapt each tool at the edge. **Data adapters are always in-house; the core library can be bought.**

### 2026-10-30 (c) Robust clock offset, not raw REST readings
- Options:
  1. Raw REST `clock_offset_ms` readings, as-of joined (`--offset-mode measured`).
  2. `correct_local_timestamp` (one constant shift).
  3. **Robust floor method** (`--offset-mode robust`, the default).
- How they differ:
  - (1) is noisy by ±300 ms for an hour at a time. At hour 10 it gave a feed latency of **p50 ≈ 645 ms**, which is impossible for this link.
  - (2) fixes only negative latencies, uniformly, and ignores drift and jumps.
  - (3) tracks drift and jumps from the data itself and gives depth p50 **309 ms**.
- **Chosen: robust.** Hour-10 results made with (1) were rerun and the old numbers dropped. Details and limits in [03 §3](03-clocks-and-our-data.md).
- Firms: they **don't estimate**. They discipline the clock with PTP/GPS and timestamp on the network card, in a colocated rack. Our method is a home-PC substitute. **Fix the clock at the source when you can; repair it in the data when you can't.**

### 2026-10-30 (d) Gaps → whole-book clear; strategy cancels and waits 5 s, no flatten
- Options:
  1. Ignore gaps (the book goes stale silently).
  2. Clear the book, **cancel all, and wait `gap_cooldown_ms` = 5 s after the book is valid again**.
  3. Also flatten inventory with a taker order.
- How they differ:
  - (1) quotes against a book that no longer exists, so its fills are fiction.
  - (3) pays the taker fee + spread on every disconnect. With Q = 0.05 ETH, the inventory risk over a few minutes is tiny.
- **Chosen: (2).** The converter writes `DEPTH_CLEAR` (bid px 0.0, ask px 1e9) at the gap. `glft_hft.py` treats every `gaps.json` interval as "no valid book". This is the same choice as our engine's paper trader.
- Firms: **stale data = pull quotes** is universal (a stale-feed kill switch). Whether to flatten depends on position size and hedge venues; small books usually wait for the resync.

### 2026-10-30 (e) GLFT port: same closed form + inventory cap only; cancel+new for size changes
- Options:
  1. Run hftbacktest's own GLFT tutorial.
  2. **Port our `compute_quotes` exactly**, with frozen params.
  3. Port everything, including all risk overlays.
- How they differ:
  - (1) differs in units, fee term, a ×0.05 skew fudge factor, a grid of 20 levels and rolling recalibration ([research §6](../../research/hftbacktest.md)), so any disagreement would mix strategy and simulator.
  - (3) is a lot of numba code.
- **Chosen: (2)**, keeping the headroom/inventory cap, the requote rule, the reject cooldown and the gap pause. No kill switch, vol/spread pause, stale-quote check, drawdown stop or min-notional check.
- Requotes: `hbt.modify` for **price-only** changes. When the size must change: **cancel, then a new order on the next cycle**. A probe showed that a qty change via `modify` was not honoured by the partial-fill exchange (the local qty changed, but the exchange filled the old size).
- `check_glft_math.py` shows the njit maths matches `glft.py` to 1e-9.
- **Known cost, seen in the results:** without the spread/vol pause the port kept quoting through the 10-24 crash, where 771 of its 1,188 fills fall ([05 §3](05-first-results.md)). For a cleaner comparison, port the spread pause or exclude crash hours.
- Firms: when validating a simulator they hold the strategy **byte-for-byte fixed** and change one thing. **Change one variable at a time, and know which strategy features you dropped.**

### 2026-10-30 (f) Skip bookTicker in v1
- Options:
  1. Feed only depth + trades.
  2. Also add bookTicker as `DEPTH_BBO_EVENT` rows.
- How they differ: bookTicker is real-time BBO, while depth is batched every 100 ms. Mixing both into one book needs rules for which one wins, because they arrive over different latencies.
- **Chosen: (1).** bookTicker is used only as an independent reference in `check_hbt.py` and by the offset estimator. A v2 could add BBO rows.
- Firms: they consume the **fastest** feed for the touch and merge feeds using sequence numbers. That is a real engineering project. **Start with one consistent source.**

### 2026-10-30 (g) NoPartialFillExchange as the default (partial fill crashes in 2.4.4)
- Options:
  1. **PartialFillExchange**: closest to our engine, which allows partial fills.
  2. **NoPartialFillExchange**: an order fills in full or not at all.
- How they differ:
  - (1) splits a fill when a trade at our price is smaller than our remaining size. In 2.4.4 on our data, `hbt.elapse` returns **error 14** after **25–60%** of a day and stays stuck, with any queue model, latency or requote mode. Such runs are marked `*_INCOMPLETE`.
  - (2) completes every day.
- **Chosen: (2).** The flag is now `--partial-fill` (opt-in), and `common.py` has `partial_fill: False`.
- Why the cost is small here: our size is Δ = 0.01 ETH, while the average trade is ~0.78 ETH, and our fills are mostly sweeps through the level. A single print almost always fills the whole order anyway.
- Lesson: check `run_complete` / `elapse_end_code` on every run. A run that silently stops looks like a quiet market.
- Firms: when a tool has a known defect, they pin the version, document the workaround next to the config, and re-test on upgrade. **Prefer a slightly less faithful model that completes over a faithful one that silently dies.**

---

## Check your understanding
1. Why can't `requirements.txt` simply list `hftbacktest==2.4.4`?
2. Which decision most explains why hftbacktest filled 4× more than our engine on 10-24: (d), (e) or (g)? Why?
3. Why does switching off partial fills barely matter for a 0.01 ETH quote ~75 ticks from mid?

*Answers on request.*
