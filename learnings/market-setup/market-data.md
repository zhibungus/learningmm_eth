# Market data: levels, streams, clocks and honest backtests

*Written 2026-10-19. Synced with code 4b1640d on 2026-10-23 (see §9).*

## Key ideas
- The key backtest question is **"when would my resting order have filled?"** More book detail means a more honest answer.
- Binance gives trades, L1 and L2. It does **not** give L3 (individual orders).
- L2 is rebuilt from a snapshot plus diffs. **Any sequence gap means throw the book away and resync.**
- A strategy may act only at `ts_ns + latency`, never at `ts_ns`.
- Free Binance L1 history (`bookTicker`) **ends 2024-03-30**. Recent L1 and all L2 come from our own recorder.
- Even with L2, queue position depends on a cancel assumption. Different assumptions give different fills.

---

## 1. Data levels

| Level                | You see                                          | Binance USDⓈ-M source                                |
| -------------------- | ------------------------------------------------ | ---------------------------------------------------- |
| Trades               | Price, size, aggressor side, time                | `aggTrade` stream; `aggTrades`, `trades` archive     |
| L1                   | Best bid/ask price and size                      | `bookTicker` stream and archive (see section 5)      |
| L2 (market-by-price) | Total size at each price                         | `depth@100ms` diffs + REST `/fapi/v1/depth` snapshot |
| L3 (market-by-order) | Each order: ID, add, cancel, fill, arrival order | **Not offered**                                      |

**The same moment at each level:**
```
 L3   bid 2395.44: [A 4.0][B 5.0][C 1.0]        ask 2395.45: [D 2.0][E 6.0]
 L2   bid 2395.44: 10.0   2395.43: 7.5 ...      ask 2395.45: 8.0   2395.46: 3.1 ...
 L1   bid 2395.44 x 10.0                        ask 2395.45 x 8.0
 Trade  2.0 @ 2395.45, aggressor = buy   (ask lifted; D is gone)
```
- With L3, a 3 ETH cancel shows *which* order left, so you know if it was ahead of you.
- L3 exists elsewhere: e.g. Coinbase Exchange "full" channel, many equity/futures exchanges. On Hyperliquid it can in principle be rebuilt from chain data.

**Remember: Binance is L2 at best, so queue position is always an estimate.**

---

## 2. Trades vs aggTrades
- **trades**: one row per taker-vs-one-maker match.
- **aggTrades**: one row per taker order per price. `first_id`/`last_id` give the trade-ID range ([`schemas.py`](../../src/mm/data/schemas.py) `TRADES_SCHEMA`).

```
 Taker buys 6 ETH @ 2395.45, sweeping D (2) and E (4 of 6)
 trades:    id 101  2.0  |  id 102  4.0        -> 2 rows
 aggTrades: 6.0, first_id 101, last_id 102     -> 1 row
```
- aggTrades is enough for MM and much smaller. The per-maker split is useless without L3 anyway.
- Each aggTrade row also has its own **aggregate `trade_id`**, counting 1, 2, 3, … Missing rows show up as a break: `trade_id` ≠ previous + 1. GLFT calibration masks those windows, plus any trade silence over 10 s (`glft_calibrate.py` `trade_gap_ranges`; 0 gaps on 2024-03-29, 1 real outage of 653 s in our 2026-10-24 recording).
- **Trap (we fell in it):** don't check `first_id` against previous `last_id + 1`. Those are *raw* trade ids, and Binance skips some, so they jump even when nothing is missing. The old rule flagged 5 false "gaps" on 2024-03-29 (spec v1.2.1 §6.2).
- `is_buyer_maker = True` means the **taker sold**. Our schema stores explicit aggressor `side` ([`schemas.py`](../../src/mm/data/schemas.py) module docstring, "Side conventions").

**Remember: flipping the aggressor side flips every order-flow signal.**

---

## 3. Snapshot + diff sync

- Streaming the whole book is too much data. Binance sends **diffs** (changed levels) plus a REST **snapshot**.
- IDs per diff: `U` = first update, `u` = final update, `pu` = previous message's `u`.
- Implemented in [`book.py`](../../src/mm/data/book.py) (procedure in the module docstring) and class `BookSynchronizer`.

```
 time ->
 WS diffs:  [U=990 u=995]  [U=996 u=1003]  [U=1004 u=1010 pu=1003]  [U=1015 u=1020 pu=1012]
                 |               |                   |                        |
 REST snapshot L=1000            |                   |                        |
                 v               v                   v                        v
              DROP           FIRST APPLY          APPLY                  GAP! pu 1012 != 1010
            (u < L)       996 <= 1000 <= 1003   (pu == prev u)          clear book, new snapshot
```

Steps:
1. Buffer diffs.
2. Get snapshot, `lastUpdateId = L`.
3. Drop diffs with `u < L`.
4. First applied diff needs `U <= L <= u`. If the earliest has `U > L`, the snapshot is stale; refetch (`BookSynchronizer._try_sync`).
5. Then require `pu == previous u`, else resync (`BookSynchronizer._apply_synced`).
6. Quantities are **absolute**. `qty = 0` deletes the level (`OrderBook.apply` / `OrderBook._set`).

- One missed diff can leave a **ghost level** forever, and every later fill decision is wrong.
- Health checks: `OrderBook.is_crossed()`, and the `gaps` / `resyncs` counters on `BookSynchronizer`.
- The snapshot has 1000 levels per side ([`data.yaml`](../../config/data.yaml) `rest.depth_limit`). Deletes for unknown far levels are normal and harmless near the touch.

**Remember: gap means resync, never "apply anyway".**

---

## 4. Three clocks ([`schemas.py`](../../src/mm/data/schemas.py) module docstring)

```
 08:00:00.100   ts_ns         matching engine: event happened
 08:00:00.103   event_ts_ns   exchange built the message
 08:00:00.141   recv_ts_ns    our machine received it (live only)
                \___________ ~40 ms: your quote is stale here ___________/
```
- Rule: act only at `ts_ns + feed_latency` (same docstring, "Rule of thumb"). Your order then also needs time to reach the exchange.
- Archive: `ts_ns` only, ms precision (same docstring). Latency must be assumed.
- Live recordings: real `recv_ts_ns`, so latency can be measured and fed back.
- Keep the clock NTP-synced to trust `recv_ts_ns - ts_ns`.

**Remember: never react at `ts_ns`.**

---

## 5. Why historical L2 is rarely free, and what the free archive has
- L2 diffs for ETHUSDT can be thousands of changes per second in busy periods, adding up to gigabytes per day.
- Exchanges do not archive it free. Vendors (Tardis.dev, Kaiko, others) charge; prices vary by plan.

**data.binance.vision, ETHUSDT USDⓈ-M (checked 2026-10-19):**

| Dataset                 | Latest file    | Note                                                                                                                                  |
| ----------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `aggTrades` (daily)     | 2026-10-18     | Current, next-day                                                                                                                     |
| `fundingRate` (monthly) | 2026-09        | Current, after month end                                                                                                              |
| `bookTicker` (daily)    | **2024-03-30** | **Free L1 stops here**                                                                                                                |
| `bookDepth` (daily)     | 2026-10-18     | Aggregated depth (as I understand it: cumulative size in % bands, coarse intervals). **Not** price-level L2. Verify format before use |

- Recent backtests need L1 from our recorder (`bookTicker` on the `/public` endpoint, [`data.yaml`](../../config/data.yaml) `live.endpoints.public`), or L1 derived from our L2.
- Or backtest the pre-April-2024 period and accept the market has changed.
- Flagged to data-engineer and researcher.

**Remember: "free top-of-book history" only goes up to 2024-03-30.**

---

## 6. Recording our own data
Configured in [`data.yaml`](../../config/data.yaml) `live` section. Binance now routes futures streams through two endpoints (legacy URLs retired 2026-04-23), one connection each.

```
 /public  depth@100ms ──> BookSynchronizer ──┐
          (REST /fapi/v1/depth snapshot ──^ on start and on every resync)
          bookTicker ────────────────────────┼──> rotating Parquet (+ raw JSON, gzip)
 /market  aggTrade ──────────────────────────┘
```
- Only depth needs syncing. Trades and quotes are written as they arrive.
- History starts the day you start. Six months of L2 needs six months of uptime.
- Gaps happen (disconnects, sleep). The recorder reconnects and resyncs. Gaps must be visible so the backtester skips them. Quality settings: [`data.yaml`](../../config/data.yaml) `quality` section.
- Keep raw JSON (`record_raw_json: true`). You can re-process after a parsing bug.
- `depth@100ms` merges changes within each 100 ms. A level that appears and vanishes inside one window is never seen.
- Home recording measures home latency, far worse than a server in the exchange's region.

**Remember: start the recorder early and keep it running.**

---

## 7. What each level lets a backtest model honestly

| Data        | Can model                                                                  | Must assume                                                                       |
| ----------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Trades only | Price traded *through* your level (fill if a trade prints strictly better) | Spread, queue, the whole book                                                     |
| L1 + trades | Queue at the touch, reduced by trades there                                | Cancels ahead vs behind; anything off the touch                                   |
| L2 + trades | Queues near the touch, imbalance, slippage                                 | Cancel split: from behind (pessimistic), from ahead (optimistic), or proportional |
| L3 + trades | Exact queue                                                                | Only others' reactions to you, and latency                                        |

**Worked queue example (L2). Your bid: 2 ETH at 2395.44.**
```
 join:          [ 10 ahead ][ YOU 2 ]              ahead 10, behind 0
 +4 join:       [ 10 ahead ][ YOU 2 ][ 4 behind ]
 trade 4:       [ 6 ahead  ][ YOU 2 ][ 4 behind ]  trades eat from the front
 2 cancelled (no trade):
   from behind: [ 6 ahead  ][ YOU 2 ][ 2 behind ]  pessimistic
   proportional:[ 4.8 ahead][ YOU 2 ][ 3.2 behind] 6/(6+4) = 60% of 2 = 1.2 ahead
   from ahead:  [ 4 ahead  ][ YOU 2 ][ 4 behind ]  optimistic
 6 ETH sell hits:
   pessimistic  -> you get 0
   proportional -> you get 1.2
   optimistic   -> you get 2
```
- Same data, three fills, one assumption.
- With trades only you guess everything, and guesses tend to be optimistic.
- **A modify loses your place.** Binance re-queues every modified order, and so does our engine (`exchange.replace_keeps_priority: false`). After a replace you are back at "join" with everything displayed ahead.

**Remember: report which cancel assumption a backtest uses.**

---

## 8. How professional firms choose
Varies by firm. Common patterns:
- **Record everything themselves**, next to each venue, full depth, precise (often hardware) timestamps. Only they see their own orders in context.
- **Also buy vendor data** to backfill, cover new venues and cross-check recorders.
- **Prefer L3 where it exists.** Elsewhere, queue models calibrated against their **own real fills**.
- **Measure latency constantly.** Backtests use a measured distribution, not one number.
- **Data quality is a team's full-time job**: gaps, sequencing, clock sync, reprocessing.

**Remember: our queue model stays uncalibrated until we have real fills, and our latency is home/VPS latency.**

---

## 9. Decisions log
- **2026-10-19. Data: free historical trades and top-of-book now, plus a live L2 recorder building our own depth history.** Over trades only (too little for fills), L2 only (months of waiting), paid Tardis.dev L2 (cost; can revisit). Accepted: until enough L2 exists, long-history backtests can only model queue at the touch, approximately.
- **2026-10-19. Finding:** free `bookTicker` archive for ETHUSDT USDⓈ-M ends **2024-03-30**. Recent L1 must come from our recorder or be derived from L2. `aggTrades` and `fundingRate` are current. Raised with data-engineer and researcher; decision unchanged.
- **2026-10-23. Sync with code 4b1640d (no owner decision).** Line-number citations replaced by class/function/config-key names. §6: recorder diagram now shows the two Binance endpoints (`/public`, `/market`) from `data.yaml`; `live.quote_stream` no longer exists. §2: aggTrade id gaps are now masked in calibration. §7: a modify loses queue priority (Binance and engine).
- **2026-10-30. Gap-mask fix (spec v1.2.1 §6.2, no owner decision).** §2: the gap check uses aggregate `trade_id` continuity, not raw `first_id`/`last_id` (Binance skips raw ids). The 2026-10-23 entry's "id gaps are masked" referred to the old, wrong rule.

---

## Check your understanding
1. A diff arrives with `pu = 5012` but the last applied `u = 5009`. What happened, what must the synchroniser do, and why is "apply it anyway" dangerous?
2. Your bid joins a level showing 8 ETH; then 2 ETH joins behind you. 3 ETH trades at your price, then the level (excluding you) shrinks 2 ETH with no trade. How much is ahead under "cancels from behind" and under "cancels from ahead"? Which flatters a backtest, and why?
3. Why can't the strategy react at `ts_ns`, and what do our live recordings give that the archive cannot?

*Answers on request.*

---

## Sources (checked 2026-10-19)
- [Binance: manage a local order book](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)
- Archive listings: `s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/...` (aggTrades, bookTicker, bookDepth, fundingRate)
- [binance-public-data README](https://github.com/binance/binance-public-data/)
- `!bookTicker` all-symbols stream throttled to 5 s from 2023-12-20 (per-symbol unaffected): [Binance change log](https://developers.binance.com/docs/derivatives/change-log)
