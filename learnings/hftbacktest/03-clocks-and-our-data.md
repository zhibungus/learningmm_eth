# 03. Clocks and our data: the converter

*Written 2026-10-30. Track: [hftbacktest/](README.md). Code: `hft/convert.py` (`Inputs._build_robust`, `make_local`, `build_chunk`). Docs: [`hft/README.md`](../../hft/README.md). Numbers: `data/hft/ETHUSDT/2026-10-24/manifest.json`. Background: [market-data.md §3–4](../market-setup/market-data.md).*

## Key ideas
- Each recorded message becomes event rows with **`exch_ts` = Binance T** and **`local_ts` = our receive time translated to the exchange clock**.
- Our PC clock is **wrong and moving**: drifts ~−55 ms/h and **jumps** up to ~1 s when Windows re-syncs.
- The recorder's REST offset readings are **too noisy** (±300 ms for an hour at a time). Using them gave a feed latency p50 of ~645 ms, which is false.
- **Robust method:** the **floor** of `recv − E` per minute tracks the clock; cut at jumps, smooth, then pin the level with L0 ≈ **306 ms**. Result: depth latency p50 **309 ms**.
- Snapshots and gaps both **clear the book**; hourly files keep **EXCH sorted by `exch_ts`, LOCAL by `local_ts`**.

---

## 1. Mapping: our recordings → event rows

| hftbacktest | From our parquet |
|---|---|
| `exch_ts` | `ts_ns` (transaction time **T**) |
| `local_ts` | `recv_ts_ns − offset(t)`; if ≤ `exch_ts` → `exch_ts + 1 ms`; then running max per stream |
| depth diff | `DEPTH_EVENT \| BUY` (bid) or `\| SELL` (ask); qty = new absolute size, 0 deletes |
| snapshot message | (a) clear both sides fully, (b) per-side `DEPTH_CLEAR` at the farthest price, (c) `DEPTH_SNAPSHOT_EVENT` rows |
| aggTrade | `TRADE_EVENT \| BUY` if aggressor = buy, else `\| SELL` |
| public/depth stream gap | whole-book clear: `DEPTH_CLEAR\|BUY` px 0.0 + `DEPTH_CLEAR\|SELL` px 1e9 |
| trade stream gap | no clear (only trades missing); listed in `gaps.json` |
| bookTicker | **not converted** (v1); reference only |

Day 2026-10-24 in numbers:

| Count | Value |
|---|---|
| depth rows / messages | 67.6 M / 838 k |
| aggTrade rows | 882 k |
| event rows written (24 files) | ≈ 75.7 M |
| snapshots / gap clears / gaps listed | 3 / 3 / 7 |
| rows clamped to `exch + 1 ms` | depth 100,243 (0.15%), trades 2,053 (0.23%) |
| rows raised by the running max | depth 338, trades 0 |
| update-id chain breaks | 0 |

**Remember: `local_ts` is not our PC time; it is our PC time corrected onto Binance's clock.**

---

## 2. The two clocks and why ours is wrong

```
 Binance clock:  T (match) ──> E (event sent) ──── network ────> arrives
 our PC clock:                                                  recv_ts (PC time, offset by o(t))

 recv_ts − E  =  o(t)  +  one-way delay
                 ^ clock error      ^ physics + queues, never below a floor
```
- `o(t)` drifts about **−55 ms per hour** and **jumps** when Windows re-syncs. On 2026-10-24 the offset used ranged from **−1,365 ms to +147 ms**.
- The recorder asks Binance's REST `serverTime` every 5 minutes. That reading assumes the request and response take equal time. On a home link they don't, and the probe itself stalls the recorder.
- Result: of 701 readings, the implied level has p10 / p50 / p90 = **272 / 306 / 720 ms**. Individual readings are off by hundreds of ms.
- `--offset-mode measured` (as-of join of these readings) gave hour-10 depth latency **p50 ≈ 645 ms**. Our engine's measured median is 239 ms, so that is wrong.

**Remember: you cannot measure one-way latency without a trusted clock on both ends.**

---

## 3. The robust floor method

Idea: the **fastest** messages in each minute all have about the minimum delay. So the low end ("floor") of `recv − E` moves only when the **clock** moves.

```
 g = recv_ts − E  (ms), one dot per message
  1000 |  .     .   .        .                 .      .   .
   800 |  ..  . .. ...  . .. . .      .     .. .. . . .. ..
   600 | ...:.:..:.:...:.:..:.:..   .:.:.  ..:.:..:.:..:.:..
   450 | ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾-__   |                 <- floor (1-min p1),
   300 |                                    ‾‾-_|  ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾     drifting down
   150 |                                        |                    jump up (Windows re-sync)
       +----------------------------------------+---------------------> recv time
         drift ≈ −55 ms/h                    cut series here
```

Steps (`Inputs._build_robust`):
1. Per **1-minute** bin (by receive time): the **1st percentile** of `g = recv − E` over depth + trades (minimum if < 200 rows). That is the floor.
2. **Jumps**: floor changes > **100 ms** between neighbouring minutes. Normal drift is ~1 ms/min. Cut the series there.
3. **Smooth**: rolling median of **15** bins, only inside a segment (never across a jump).
4. **Level L0**: median over REST readings of (smoothed floor − reading). Noisy readings, robust median. Day value **L0 = 305.9 ms** (390 of 701 readings near a data bin). `hft/README.md` quotes ~299 ms; the value shifts a little with the set of readings used.
5. `offset(t) = smoothed_floor(t) − L0`, so `local = recv − offset`.

Why E and not T: E − T is Binance's own batching delay (depth is sent every 100 ms). Using E keeps that out of the clock estimate. `exch_ts` is still T, so the reported depth latency includes the batching.

### Worked example
```
 message:   E = 10:00:00.200 (Binance)   recv = 10:00:00.900 (PC)      g = 700 ms
 this minute's smoothed floor = 450 ms   L0 = 306 ms
 offset    = 450 − 306 = 144 ms          (PC is 144 ms ahead of Binance)
 local_ts  = 0.900 − 0.144 = 10:00:00.756 on Binance's clock
 latency vs E = 556 ms   (a slow message: it sat 250 ms above the floor)
 a floor message (g = 450) would get exactly 306 ms = L0
```

### What the manifest shows for 2026-10-24
- **9 jumps** (several on 2026-10-23 19:03–19:06 during a reconnect; these just cut the series).
- Biggest real jump: **13:23**, floor −1,062 → +123 ms, i.e. the PC clock stepped by ~1.2 s.
- Feed latency after repair: depth p50 / p90 / p99 = **309 / 327 / 1,270 ms**; trades **337 / 482 / 2,754 ms**. Max 25.9 s (depth), 16.2 s (trades) around reconnects.

### Limits (be honest)
- The **absolute** level rests on L0 from the REST median. If REST is biased (asymmetric path), every latency is off by the same amount.
- A real change in the delay floor lasting > ~15 min is read as clock drift and removed.
- The clamp (`local ≤ exch` → `exch + 1 ms`) touches 0.15% of depth rows: those are moments the repaired clock is still a little wrong.

**Remember: trust the floor for the shape (drift, jumps); use the REST median only for the level.**

---

## 4. Snapshots vs diffs

```
 diff:      DEPTH_EVENT|BUY 2365.10 6.5          -> set one level
 snapshot:  CLEAR bid px 0.0, CLEAR ask px 1e9   -> book empty (removes stale far levels too)
            CLEAR|BUY at lowest snapshot bid     -> hftbacktest's own convention
            SNAPSHOT|BUY rows ... SNAPSHOT|SELL rows   -> book rebuilt (1,000 levels per side)
```
- A live recording has snapshots only at **(re)connects**: 3 on 2026-10-24.
- So the 00:00 book was built by **replaying 15.2 M events** from the snapshot at 2026-10-23 19:06:24.874 up to midnight → a 27,103-row initial snapshot (`create_last_snapshot`).
- Same idea as our engine's warm-up (`warmup_from`).

**Remember: diffs need a starting book; a snapshot is the only safe starting point.**

---

## 5. Gaps

- **Public (depth) stream disconnect or resync** → whole-book clear at the gap time. The book is empty until the next snapshot. The strategy sees NaN prices and stops quoting.
- **Market (trade) stream disconnect** → no clear (book still valid); listed in `gaps.json`.
- Example: the 2026-10-24 network outage. Hour 22's last event is 22:59:32.557, hour 23's first is 23:10:24.184: **~652 s** of nothing.
- The GLFT port also reads `gaps.json` and treats **every** listed interval as "no valid book": cancel all, wait 5 s after valid again, no flatten ([README (d)](README.md#decisions-log)).

**Remember: a gap means "the book is unknown", so the honest move is to empty it, not to keep the last one.**

---

## 6. Hourly files and event order

Rules hftbacktest checks (`validate_event_order`):
1. EXCH rows non-decreasing in `exch_ts`.
2. LOCAL rows non-decreasing in `local_ts`.
3. These also hold **across** consecutive files (our converter checks).

```
 exchange time:     09:59:00 ........ 10:00:00 ............................ 11:00:00
 read for file _10: [── 60 s lookback ──|──────────── hour 10 ────────────)
 file _10 holds:                        EXCH copies with exch_ts  in [10:00, 11:00)
                                        LOCAL copies with local_ts in [10:00, 11:00)
 example event:  exch 09:59:59.800 → its EXCH copy is in file _09
                 local 10:00:00.110 → its LOCAL copy is in file _10
```
- The 60 s lookback must exceed the maximum feed latency (converter fails if latency > 55 s).
- **Ties at the same timestamp**: clears first, then trades, then depth rows; recorded order within a kind. A trade is the cause of the depth change stamped with the same ms, so it goes first.
- Load several hours with `.data([file_10, file_11, ...])`. A missing hour is an error, because the clock would run over a stale book.

**Remember: one event can live in two files, its EXCH copy in one and its LOCAL copy in the next.**

---

## Decisions log
- **2026-10-30.** Own converter (b), robust clock offset (c), gaps as whole-book clears (d), no bookTicker (f). Full write-ups in [README.md Decisions log](README.md#decisions-log).

---

## Check your understanding
1. In one minute, the smoothed floor of `recv − E` is 420 ms and L0 = 306 ms. A message has recv − E = 900 ms. What is its feed latency (vs E) after repair?
2. Why does the method use the 1st percentile per minute rather than the median?
3. A depth event has `exch_ts` 10:59:59.900 and `local_ts` 11:00:00.250. Which hourly file(s) contain it, and with which flags?

*Answers on request.*
