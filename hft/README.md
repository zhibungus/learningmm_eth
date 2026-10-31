# hft/ - converting our live Binance recordings for hftbacktest 2.4.4

Separate learning track. Nothing here touches `src/mm/`. Reads `data/processed/binance_um_live/` and
`data/raw/binance_um_live/ETHUSDT/status.jsonl` (read-only); writes only to `data/hft/` (git-ignored).

| File | Purpose |
|---|---|
| `convert.py` | processed parquet -> hourly hftbacktest `.npz` event files + initial snapshot + `gaps.json` + `manifest.json` |
| `check_hbt.py` | loads the files in hftbacktest, counts crossed/NaN books, compares its best bid/ask with our recorded bookTicker |

## Setup (own venv, Python 3.13)

```
python -m venv .venv-hft
.venv-hft\Scripts\python.exe -m pip install -r requirements-hft.txt     # hftbacktest 2.4.4, numpy, numba, polars, pyarrow
```

## Run

```
set PYTHONIOENCODING=utf-8
.venv-hft\Scripts\python.exe hft\convert.py --date 2026-10-24 --start 10:00 --end 11:00   # one hour (~2-3 min, mostly the setup scan)
.venv-hft\Scripts\python.exe hft\convert.py --date 2026-10-24                             # whole day, 24 hourly files
.venv-hft\Scripts\python.exe hft\check_hbt.py --date 2026-10-24 --start 10:00 --end 11:00
```
Options: `--offset-mode robust|measured` (default robust), `--symbol`, `--tick 0.01`, `--lot 0.001`.
Output: `data/hft/ETHUSDT/<date>/ETHUSDT_<date>_<HH>.npz`, `..._<HH>_snapshot.npz` (book at the window start; pass it to
`BacktestAsset.initial_snapshot`), `gaps.json`, `manifest.json`. Load several consecutive hours with `.data([file1, file2, ...])`.
Keep `roi_lb/roi_ub` wide (e.g. +-25 % of the day's mid) if you use `ROIVectorMarketDepthBacktest`.

## Mapping

| hftbacktest field | Source |
|---|---|
| `exch_ts` | `ts_ns` (exchange transaction time T) |
| `local_ts` | `recv_ts_ns - offset(t)` = our receive time on the exchange clock (see "Clock offset"); rows with `local <= exch` -> `exch + 1 ms` (counted); then forced non-decreasing per stream (depth, trades) with a running max (counted) |
| depth diff row | `DEPTH_EVENT \| BUY_EVENT` (bid) / `\| SELL_EVENT` (ask), px, qty = new absolute size (0 deletes) |
| snapshot message (`is_snapshot` rows sharing `final_update_id`) | (a) full-book clear of both sides, (b) per side `DEPTH_CLEAR_EVENT` with px = farthest price of that side, (c) `DEPTH_SNAPSHOT_EVENT \| side` rows in recorded order |
| aggTrade | `TRADE_EVENT \| BUY_EVENT` if aggressor == "buy" else `\| SELL_EVENT` |
| public-stream gap (`disconnect`/`resync`) | at the gap time a whole-book clear: `DEPTH_CLEAR_EVENT\|BUY` px=0.0 and `DEPTH_CLEAR_EVENT\|SELL` px=1e9. Verified by experiment: the clear removes every bid >= px / every ask <= px |
| market-stream (trades) disconnect | no clear (only trades missing); listed in `gaps.json` |
| bookTicker (quotes) | NOT converted in v1: hftbacktest's book is driven by depth, and mixing BBO events into it needs care. A v2 could add `DEPTH_BBO_EVENT` rows. Quotes are used only as an independent reference in `check_hbt.py` and by the offset estimator |

Event order. Within a file, EXCH copies are sorted by `exch_ts` and LOCAL copies by `local_ts` (`correct_event_order`, then
`validate_event_order`, and continuity is checked across files). Hour H's file holds the EXCH copy of every event with exch_ts in H and the
LOCAL copy of every event with local_ts in H (we read 60 s of the previous hour for that). Ties at equal timestamps: clears first, then
trades, then depth rows; inside a kind the recorded order is kept (stable sorts). Reason: a trade is the cause of the depth change stamped
with the same millisecond, so it is replayed first.

## Clock offset (important)

`recv_ts_ns` is our PC clock; the PC clock drifts (about -55 ms/hour) and jumps by up to ~1 s when Windows re-syncs. The recorder's REST
`clock_offset_ms` readings are noisy (+-300 ms for an hour at a time), so a plain as-of join of them (`--offset-mode measured`) gave a depth
latency p50 of ~645 ms for 2026-10-24 10:00, which is wrong. Default `robust` mode (`Inputs._build_robust`):

1. per 1-minute bin (by recv time) the 1st percentile of `recv_ts - event_ts` over depth + trades (min if < 200 rows) = the "floor";
2. clock jumps = floor change > 100 ms between adjacent bins; the series is cut there;
3. rolling median (15 bins) inside each segment;
4. level `L0` = median over all readings of (smoothed floor - reading) (~299 ms) = the minimum one-way delay;
5. `offset(t) = smooth floor(t) - L0`.

Caveat: a real change of the delay floor lasting longer than ~15 min is read as clock drift. A few "jumps" are not clock jumps but
reconnect minutes with few/backlogged rows; they only cut the series (harmless). The manifest lists all jumps and the latency percentiles
under both modes.

## Known quirks

* The recorder's 5-minute REST probe stalls its event loop: every ~5 min a burst of messages arrives up to ~3 s late (visible as the latency max).
* The initial snapshot is built by replaying depth from the last recorded snapshot before the window start (can be several hours back).
