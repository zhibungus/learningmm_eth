# Processed datasets: sources, coverage, quality

Generated 2026-10-19, updated 2026-10-30 (funding to 2026-10-30, live QC for 2026-10-24 and 10-25). Venue Binance USD-M futures, symbol ETHUSDT. Schemas: `src/mm/data/schemas.py`.
All prices USDT, sizes ETH, timestamps integer ns UTC.

## Sources and terms

| Dataset (processed folder) | Source | Dates in this repo | Rows |
|---|---|---|---|
| `binance_um/agg_trades` | https://data.binance.vision/data/futures/um/daily/aggTrades | 2024-03-29, 2024-03-30, 2026-10-17, 2026-10-18 | 921,565 + 680,664 + 578,540 + 898,438 |
| `binance_um/trades` (raw trades) | .../daily/trades | 2026-10-17, 2026-10-18 | 2,068,678 + 3,350,349 |
| `binance_um/quotes` (bookTicker) | .../daily/bookTicker | 2024-03-29, 2024-03-30 | 7,981,165 + 5,996,993 |
| `binance_um/funding` | .../monthly/fundingRate (2024-03, 2026-09) and REST `/fapi/v1/fundingRate` (2026-10, not yet on archive; re-fetched 2026-10-30 with `python -m mm.data.download --start 2026-10-20 --end 2026-10-29 --datasets fundingRate --funding-marks`, which rewrites the whole month file) | 2024-03-01 .. 2024-03-31, 2026-09-01 .. 2026-10-30 16:00 | 93 + 90 + 90 |
| `binance_um_live/{trades,quotes,depth}` | our recorder, wss://fstream.binance.com (public + market endpoints) + REST `/fapi/v1/depth` | 2026-10-19, five short test sessions (14:23, 14:26, 14:55, 17:22, 19:59 UTC; 7 to 40 s each) | 2,166 trades, 74,794 quotes, 157,387 depth rows (944 messages, 5 snapshots) |
| inance_um_live/{trades,quotes,depth,gaps} (continuous recording) | same recorder, long run | 2026-10-23 (partial), **2026-10-24 and 2026-10-25 (full days, QC'd below)**, 2026-10-26 (partial; 10-23 and 10-26 not QC'd) | 10-24: 881,685 trades, 22,914,306 quotes, 67,628,680 depth rows (837,731 messages, 3 snapshots); 10-25: 656,628 trades, 20,684,765 quotes, 60,759,014 depth rows (844,499 messages, 0 snapshots) |

Terms: data.binance.vision and the Binance public APIs are free public data published by Binance; use is
subject to Binance's Terms of Use (do not redistribute the files; confirm before sharing outside this project).
Every archive zip was verified against its `.CHECKSUM` (SHA-256). Hashes are in `data/raw/binance_um/download_log.jsonl`.
No API keys are used or needed.

## Availability findings (verified against the S3 bucket listing)

* `bookTicker` for ETHUSDT: daily files exist only for **2023-05-16 .. 2024-03-30** (320 contiguous days; monthly files 2023-05 .. 2024-04). Nothing newer. It is not a recent-data source.
* `bookDepth` exists for recent dates but is a percent-depth aggregate (not L2 updates); not used.
* **No free historical L2 depth updates** for futures on data.binance.vision. Depth history = whatever the live recorder accumulates.
* `fundingRate` is monthly only and published after month end. The archive CSV has no mark price. `--funding-marks` fills it from REST (done for 2026-09 and 2026-10; REST-sourced rows have `interval_hours` null).
* `aggTrades` and `trades` have files for every day sampled (2019-12-31 .. 2026-10-18 for aggTrades).

## Timestamps: which clock?

* `ts_ns` everywhere = exchange matching-engine time (trade time / `T`). Archive trades only have this.
* `event_ts_ns` (`E`) exists for archive bookTicker and live data. Archive bookTicker: E - T median 6-7 ms, p99 17-19 ms (2024-03-29/30). Live: 0-1 ms.
* `recv_ts_ns` (local clock) exists only for live data. In the live test our PC clock was ~5.1-5.4 s AHEAD of Binance (REST `/fapi/v1/time`), so raw `recv - ts` shows ~5.4 s. The recorder logs `clock_offset_ms` to `data/raw/binance_um_live/ETHUSDT/status.jsonl`; subtract it to get true latency, and sync the Windows clock before long recordings.

## Quality results

Run with `python -m mm.data.quality`.

**aggTrades 2024-03-29 / 2024-03-30**: no duplicate ids, no id gaps, no non-positive price/qty, no silence over 60 s (largest 5.3 s / 7.9 s), tick size 0.01, buy share of volume 48.6% on 03-29.

**aggTrades 2026-10-17 / 2026-10-18**: no duplicate ids, no id gaps, timestamps monotonic, no silence over 60 s (largest 10.2 s / 9.8 s), no non-positive price/qty, tick size 0.01, buy share of volume 47.2% / 49.1%.

**raw trades 2026-10-17 / 2026-10-18**: no duplicate ids, timestamps monotonic, but **10,445 and 13,388 trade ids are missing** (10,162 / 13,068 gaps). The same ids are skipped between consecutive aggTrades (9,951 on 10-17), and total quantity matches aggTrades to 1.4e-7 relative. So these are ids Binance never publishes, not download loss. Prefer aggTrades (smaller, same information at ms resolution).

**bookTicker 2024-03-29 / 2024-03-30**: 7,981,165 / 5,996,993 updates; crossed 0, locked 0, non-positive 0, update_id never backwards or repeated, event time never before trade time, largest silence 4.21 s / 2.04 s, median spread 0.01 (1 tick), tick size 0.01 (`python -m mm.data.quality --start 2024-03-29 --end 2024-03-30 --streams quotes trades`).

**funding**: 2024-03 has 93 rows (31 days x 3; the quality tool over 03-29..03-30 sees 6 rows, 8 h spacing, no duplicates, rates 2.4e-4 .. 4.5e-4, no mark price); 2026-09 has 90 rows (8 h spacing, no gaps, no duplicates), 2026-10 has 90 rows through 2026-10-30 16:00 (30 rows for 10-20..10-29 = 10 days x 3, no gaps, no duplicates, no null marks, all with `interval_hours` null). Spacing is 8 h +/- 7 ms: REST `fundingTime` carries 0-7 ms of jitter after the hour. Over 10-20..10-29: rate -3e-6 .. 9.7e-5, mark 2,226 .. 2,511 (one row per 8 h; 10-24: 1.6e-5, 3.4e-5, -3e-6 with marks 2386.54, 2374.06, 2305.34; 10-25: 3.7e-5, 7.8e-5, 5.6e-5 with marks 2357.67, 2346.88, 2343.33). `source: {funding: archive}` resolves to this folder: `resolve_source` gives archive for funding, live for depth/quotes/trades, and `find_files("funding","2026-10-20","2026-10-29")` returns the single `2026-10/part-0.parquet`; `load_funding` returns the 30 rows. Rates in [-1.8e-05, 6.6e-05] over the 10-17..10-18 window.

**live (five short sessions, 2026-10-19)**: depth chain breaks 0, crossed books 0, locked 0, non-monotonic ts 0, trade ids contiguous within each session. The four "silences" (160 s to 9,431 s) and the 4 trade-id gaps are the pauses between sessions, not feed gaps. Book depth after sync: about 2,000-2,170 levels per side (the diff stream carries more than the 1000-level snapshot).

**live 2026-10-24 (full day, `python -m mm.data.quality --source live --start 2026-10-24 --end 2026-10-24 --streams trades quotes depth`)**
* Books: crossed 0, locked 0, non-positive qty 0, depth `update_id` chain breaks 0, timestamps never backwards, quote `update_id` never backwards or repeated, event time never before trade time. Median spread 0.01 (1 tick), tick 0.01.
* Trades: 881,685, duplicates 0, one aggTrade id gap (ids 3128719953 -> 3128724252, 4,298 aggTrades lost) which is exactly the outage below. `first_id`/`last_id` are present and populated.
* **Outage 22:59:32.5 .. 23:10:26.2 UTC (653 s).** Last trade 22:59:32.349, last quote .557, last depth .523; first trade back 23:10:24.184, first quote 23:10:25.065, depth resumes with a fresh REST snapshot at 23:10:26.214 (silences 651.8 s trades, 652.5 s quotes, 653.7 s depth). status.jsonl: both sockets `ConnectionClosedError` at 22:59:32 PC time, then `getaddrinfo failed` (DNS) retries 23:10:17-23:10:20, reconnect 23:10:24/25, `book_synced` 23:10:26. So it was a network/DNS outage on the PC, not a Binance gap. **Yes, it is marked** in `binance_um_live/gaps/ETHUSDT/2026-10-24`: `disconnect` markers for `public` and `market` at ts 22:59:32.557000001 (files part-...0006 and ...0007, duplicated by the reconnect retries) and a `session_start` (stream `depth`) marker at 23:10:26.214. Treat (22:59:32.557, 23:10:26.214] as no data; the id gap and the three silences coincide with it.
* Two shorter public-stream drops, also marked (`disconnect public` + `session_start depth`): quotes silent 15:10:03.9 -> 15:10:23.4 (19.6 s; depth resumes 15:10:28.5 after 24.7 s) and 15:30:02.8 -> 15:30:21.4 (18.5 s; depth resumes 15:30:24.2 after 21.4 s). The disconnect marker (15:10:23.1, 15:30:18.6 PC clock) is stamped when the client noticed the dead socket, which is up to ~19 s after the last message, so the true hole starts at the last quote, not at the marker. aggTrade (market socket) was unaffected. Depth chain breaks stay 0 because each drop is followed by a new snapshot.
* Silences between 5 s and 10 s on trades: 25 (quiet market, real). Longer than 10 s: only the outage.
* Depth: recv minus exchange time p50 -289 ms, p99 4,862 ms: dominated by the PC clock offset (below), not network delay.

**live 2026-10-25 (full day)**: trades 656,628, duplicates 0, id gaps 0, largest trade silence 9.95 s; quotes 20,684,765 with crossed 0, locked 0, `update_id` problems 0, largest silence 0.895 s; depth 60,759,014 rows, 844,499 messages, chain breaks 0, crossed 0, locked 0, ts backwards 0, largest silence under 1 s. No gap markers, no disconnects. **0 depth snapshots on this day**: the book is a continuation of the sync done at 23:10:26 on 10-24, so a replay of 10-25 alone starts with an empty book. Load from 10-24 (or the last snapshot before the window) to warm start the book.

**Trade ids: aggregate vs raw.** `quality` checks `trade_id` (the aggregate id, consecutive by definition). The older GLFT calibration counted breaks in `first_id != previous last_id + 1`. Those are raw trade ids, and Binance never publishes some raw ids, so they jump by 1 with nothing lost: archive 2024-03-29 has exactly 5 such breaks (each skipping 1 raw id) and 0 aggregate-id breaks; live 10-24 has 12,882 and 10-25 has 11,218 raw breaks against 1 and 0 aggregate breaks. `quality` was right; the calibration (spec v1.2 -> v1.2.1) was fixed to use `trade_id`. The raw skips are the same phenomenon documented for `trades` above.

**PC clock offset.** `status.jsonl` logs `clock_offset_ms` every 5 min (PC minus Binance server, via REST `/time`, so negative = PC behind). It wanders: 10-23 +1,829 .. -976 ms, 10-24 +160 .. -2,564 ms, 10-25 +105 .. -1,317 ms, 10-26 -1,283 .. -1,396 ms. It drifts about -1 s per 6-12 h and resets (to about +100 ms) when the PC re-syncs. It affects only `recv_ts_ns` (and recv-based latency stats such as the depth p99 above); correct with `recv - offset` (`mm.data.latency`). `ts_ns` and `event_ts_ns` are Binance's clock, so replay order, day folders (exchange-time foldering), the quality checks and the backtest (nothing in `src/mm/backtest` or `src/mm/strategies` reads `recv_ts_ns`) are unaffected. Gap-marker timestamps are exchange time (last message + 1 ns); the `ts_ns` in status.jsonl is PC clock, so it is off by the offset at that moment.

## Known limitations

* Millisecond timestamp resolution: many events share a `ts_ns`. Replay tie-breaks deterministically (see `loaders.py`) but the true intra-millisecond order is unknown.
* Tick size (0.01) and lot size were checked only on the sampled days; not tested across the full history for changes or symbol renames.
* Archive bookTicker is 2024-03-29/30 and has matching aggTrades for the same two days (the GLFT backtest in-sample set). The 2026-10 aggTrades/trades have no matching archive quotes, so quotes and trades cannot be combined for those days. Use the live recorder for matching quotes + depth + trades.
* Depth messages that carry no price levels would not survive the flattened row format; Binance does not send such messages in practice.
* Binance split its futures websocket into `/public` (depth, bookTicker) and `/market` (aggTrade) endpoints; legacy URLs were retired 2026-04-23. The recorder runs two connections, so a reconnect of one does not trigger a book resync for the other.
