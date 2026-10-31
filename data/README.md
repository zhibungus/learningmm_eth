# Data layer quick guide

Venue: Binance USD-M perpetual futures. Symbol: ETHUSDT. Code: `src/mm/data/`. Config: `config/data.yaml`.
Quality results and dataset facts: `data/processed/README.md`.

## Folders

```
data/raw/binance_um/<dataset>/ETHUSDT/*.zip(+.CHECKSUM)   untouched downloads (data.binance.vision)
data/raw/binance_um_live/ETHUSDT/<date>/ws-*.jsonl.gz     untouched live websocket frames
data/processed/binance_um/<agg_trades|trades|quotes|funding>/ETHUSDT/<date>/part-0.parquet
data/processed/binance_um_live/<trades|quotes|depth>/ETHUSDT/<date>/part-*.parquet
```

## Setup (once)

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .          # makes `mm` importable
```

## Commands (project root)

```powershell
# what exists on the archive?
.venv\Scripts\python.exe -m mm.data.download --list-availability --datasets bookTicker aggTrades fundingRate
# download + verify checksum + normalize
.venv\Scripts\python.exe -m mm.data.download --datasets aggTrades fundingRate --start 2026-10-17 --end 2026-10-18 --funding-marks
.venv\Scripts\python.exe -m mm.data.download --datasets bookTicker --start 2024-03-30 --end 2024-03-30
# record live L2 + trades + quotes (Ctrl+C to stop; --duration 60 for a test)
.venv\Scripts\python.exe -m mm.data.record
# quality report
.venv\Scripts\python.exe -m mm.data.quality --start 2026-10-17 --end 2026-10-18 --streams trades funding
.venv\Scripts\python.exe -m mm.data.quality --start 2026-10-19 --end 2026-10-19 --source live --streams trades quotes depth
# measure feed latency from recordings (+ --rtt N public REST round trips for order latency)
.venv\Scripts\python.exe -m mm.data.latency --start 2026-10-19 --end 2026-10-19 --rtt 10
# tests
.venv\Scripts\python.exe -m pytest
```

## Use in Python

```python
from mm.data.loaders import replay, load_trades
from mm.data.book import OrderBook

book = OrderBook()
for ev in replay("2026-10-17", "2026-10-18", streams=("trades", "funding")):
    if ev.kind == "depth":
        book.apply(ev)
```

The full replay contract (event fields, ordering, tie-breaks) is documented at the top of
`src/mm/data/loaders.py`. Live: `from mm.data.live import LiveFeed; async for ev in LiveFeed().stream(): ...`.

## Gap markers, sources, latency (live recordings)

* `GapEvent(ts_ns, reason, stream)` (`kind == "gap"`) says "the live feed was interrupted, your
  book is stale". `reason` is `"session_start"`, `"resync"` or `"disconnect"`. `replay(source="live")`
  delivers one immediately before every snapshot `DepthEvent` (same `ts_ns`; gap sorts first at equal
  timestamps: gap < funding < trade < depth < quote). A "resync" marker is stamped at the exchange
  time the sequence gap was DETECTED (earlier than the repairing snapshot, which keeps its own
  `ts_ns`), so a backtest stops trusting its book right away. Synthesised markers for old
  recordings can only use the snapshot time (the detection time was never stored). The recorder stores them in
  `data/processed/binance_um_live/gaps/`; recordings made before this existed get one synthesised
  before each snapshot. `LiveFeed` yields the same events (and still yields `StatusEvent`s).
* `replay(..., source={"depth": "live", "funding": "archive"})` mixes sources per stream. A plain
  string still applies to all streams. Unlisted streams default to live for depth, archive otherwise.
* Recorder part files are named `part-<first exchange ts_ns>-<n>` and loaders order live files by
  the smallest `ts_ns` inside them (old PC-clock names still work), so replay order is correct
  across midnight even though the PC clock is a few seconds fast.
* The recorder files rows by the UTC date of their EXCHANGE timestamp (not the PC clock).
  Recordings made earlier may have the last seconds of a day in the next day's folder; loaders look
  one day either side to compensate.
* `python -m mm.data.latency` turns recorded `recv_ts_ns`, `event_ts_ns`, `ts_ns` and the
  per-session `clock_offset_ms` (re-measured every `live.clock_check_interval_s`) into delay
  percentiles and prints values for the backtest `latency:` block. Your PC clock drifts, so never
  reuse an old offset.

## Things to remember

* `ts_ns` is exchange matching-engine time. Strategies must add their own latency.
* There is NO free historical L2 depth. Depth history only exists from when you start the recorder.
  Free historical quotes (`bookTicker`) stop at 2024-03-30, so recent days have trades only.
* Prices in USDT, sizes in ETH, timestamps are integer nanoseconds UTC.
