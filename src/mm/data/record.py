"""Live recorder: saves the ``LiveFeed`` to disk so we accumulate our own L2 history.

Output (both rotate every ``live.rotation.max_seconds`` / ``max_rows`` and at UTC midnight):

* normalized parquet (what ``loaders.replay(source="live")`` reads)::

      data/processed/binance_um_live/<trades|quotes|depth|gaps>/<SYMBOL>/<YYYY-MM-DD>/part-<HHMMSS>-<n>.parquet

  ``<YYYY-MM-DD>`` is the UTC date of the EXCHANGE timestamp of the rows (not the PC clock, which
  can be seconds off), so each row lands in the folder of the day it really happened. A buffer
  that spans midnight is split into two files. ``gaps`` holds the replayable GapEvent markers.

* untouched websocket JSON, one ``<recv_ts_ns>\\t<json>`` line per frame, gzip::

      data/raw/binance_um_live/<SYMBOL>/<YYYY-MM-DD>/ws-<HHMMSS>.jsonl.gz
      (raw folder date = receive time minus the measured clock offset, i.e. also exchange-time UTC)
      data/raw/binance_um_live/<SYMBOL>/status.jsonl     (connects, gaps, resyncs, ...)

Run (project root, venv active)::

    python -m mm.data.record                    # until Ctrl+C
    python -m mm.data.record --duration 60      # 60 seconds, then exit cleanly
    python -m mm.data.record --no-raw           # skip the raw JSON copy

Ctrl+C flushes all buffers before exiting, so no data in memory is lost.
Measured disk use for ETHUSDT (Oct 2026): ~2.5 GB/day parquet + ~2 GB/day raw JSON (--no-raw to skip).
"""
# Postpones type-hint evaluation so `X | None` style hints work everywhere.
from __future__ import annotations

import argparse  # builds the command-line interface (--duration etc.)
import asyncio  # cooperative multitasking; `async`/`await` code (see live.py for a primer)
import datetime as dt
import gzip  # compress the raw JSON as we write it
import json
import logging
import time
from pathlib import Path
from typing import Callable

import polars as pl

from . import events as E
from .config import load_config, processed_dir, raw_dir
from .live import LiveFeed

log = logging.getLogger("mm.record")
NS_PER_DAY = 86_400 * 1_000_000_000


# A leading underscore in a name is a convention meaning "internal helper, not public API".
class _Bucket:
    """Buffers events of one dataset and writes them as parquet part files."""

    # `to_frame` is a FUNCTION passed in as a value (e.g. events.trades_to_frame): each bucket is told
    # how to turn its own kind of event into a table. `Callable` is the type hint for that.
    def __init__(self, dataset: str, to_frame: Callable, root: Path, symbol: str, max_rows: int):
        # Tuple assignment: several names assigned in one line.
        self.dataset, self.to_frame, self.root = dataset, to_frame, root
        self.symbol, self.max_rows = symbol, max_rows
        self.buf: list = []
        self.n_files = 0
        self.rows_written = 0

    # Events are only collected in memory here; writing one tiny file per event would be very slow.
    def add(self, ev) -> None:
        self.buf.append(ev)

    def full(self) -> bool:
        return len(self.buf) >= self.max_rows

    def flush(self, stamp: dt.datetime) -> None:
        """Write everything buffered to a new parquet file (name uses the window start `stamp`)."""
        if not self.buf:
            return
        # Events -> table. Then build the folder <root>/<dataset>/<SYMBOL>/<YYYY-MM-DD>/ .
        df = self.to_frame(self.buf)
        self.n_files += 1
        # File the rows by the UTC date of their EXCHANGE timestamp (ts_ns), not the PC clock.
        # Integer-dividing ns by ns-per-day gives "days since 1970"; partition_by splits the table
        # into one piece per day (usually just one; two if the buffer crosses midnight).
        day_col = pl.col("ts_ns") // NS_PER_DAY
        for part in df.with_columns(day_col.alias("_day")).partition_by("_day", maintain_order=True):
            day = dt.datetime.fromtimestamp(part["_day"][0] * 86400, dt.timezone.utc)
            d = self.root / self.dataset / self.symbol / day.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            # Name = part-<earliest EXCHANGE ts_ns in the file>-<counter>. The zero padding makes
            # alphabetical order equal time order, and unlike a PC-clock name it can never sort
            # a file holding the day's last seconds before the files that hold earlier data.
            first_ts = int(part["ts_ns"].min())
            final = d / f"part-{first_ts:019d}-{self.n_files:04d}.parquet"
            # Write to a temporary name first, then rename (atomic), so a crash or a concurrent
            # reader never sees a half-written .parquet file. zstd = compact, fast compression.
            tmp = final.with_suffix(".tmp")
            part.drop("_day").write_parquet(tmp, compression="zstd")
            tmp.replace(final)
        self.rows_written += df.height
        self.buf = []


class Recorder:
    """Subscribes to a ``LiveFeed`` and persists everything it emits."""

    # Why files rotate: one endless file could never be read until the program ends and a crash
    # would lose everything. Small time-boxed files are safe, resumable and easy to load by date.

    def __init__(self, feed: LiveFeed, record_raw: bool | None = None) -> None:
        # Settings come from config/data.yaml (via the feed) so nothing is hard-coded here.
        self.feed, self.cfg = feed, feed.cfg
        rot = self.cfg["live"]["rotation"]
        self.max_seconds, self.flush_raw_s = rot["max_seconds"], rot["flush_raw_seconds"]
        self.record_raw = self.cfg["live"]["record_raw_json"] if record_raw is None else record_raw
        sym = feed.symbol
        proc, self.raw_root = processed_dir(self.cfg, live=True), raw_dir(self.cfg, live=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        # One buffer per event kind. `lambda` is a one-line anonymous function; mk(...) is just a
        # shortcut for building a _Bucket with the shared arguments filled in.
        mk = lambda name, fn: _Bucket(name, fn, proc, sym, rot["max_rows"])
        self.buckets = {
            "trade": mk("trades", E.trades_to_frame),
            "quote": mk("quotes", E.quotes_to_frame),
            "depth": mk("depth", E.depth_to_frame),
            # Replayable feed-gap markers (see events.GapEvent); tiny, but vital for backtests.
            "gap": mk("gaps", E.gaps_to_frame),
        }
        self._raw_date: dt.date | None = None  # exchange-time date of the open raw file
        self._window_start = dt.datetime.now(dt.timezone.utc)
        self._raw_fh: gzip.GzipFile | None = None
        self._last_raw_flush = time.monotonic()
        self._status_path = self.raw_root / sym / "status.jsonl"
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        # Register for events. The feed calls on_event for every normalized event, and on_raw
        # (if enabled) for every untouched websocket frame.
        feed.subscribe(self.on_event)
        if self.record_raw:
            feed.on_raw = self.on_raw

    # --- callbacks
    def on_event(self, ev) -> None:
        # Status events (connected, gap, resync...) go to a small log, not to parquet.
        # `with open(..., "a")` is a context manager: appends a line and closes the file safely.
        if ev.kind == "status":
            with open(self._status_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_ns": ev.ts_ns, "status": ev.status, "detail": ev.detail}) + "\n")
            return
        # Market data: rotate files if the window is over, buffer the event in its bucket,
        # and flush early if the bucket got big (bounds memory use).
        self._maybe_rotate()
        b = self.buckets[ev.kind]
        b.add(ev)
        if b.full():
            b.flush(dt.datetime.now(dt.timezone.utc))

    def on_raw(self, recv_ts_ns: int, text: str) -> None:
        """Append one raw websocket frame as `<recv_ts_ns><TAB><json>` to a gzip file (our untouched copy)."""
        # Exchange-time date of this frame = receive time minus the measured clock offset. When it
        # changes (midnight UTC on the exchange clock) close the file so a new date folder starts.
        exch_ns = recv_ts_ns - int(self.feed.clock_offset_ms * 1_000_000)
        day = dt.datetime.fromtimestamp(exch_ns / 1e9, dt.timezone.utc).date()
        if self._raw_fh is not None and day != self._raw_date:
            self._raw_fh.close()
            self._raw_fh = None
        if self._raw_fh is None:
            self._raw_date = day
            d = self.raw_root / self.feed.symbol / day.isoformat()
            d.mkdir(parents=True, exist_ok=True)
            name = f"ws-{self._window_start.strftime('%H%M%S')}.jsonl.gz"
            self._raw_fh = gzip.open(d / name, "ab")
        # "ab" = append binary; gzip needs bytes so the f-string text is .encode()d. \t is a TAB.
        self._raw_fh.write(f"{recv_ts_ns}\t{text}\n".encode("utf-8"))
        # Flush to disk every few seconds so a crash loses at most that much raw data.
        if time.monotonic() - self._last_raw_flush > self.flush_raw_s:
            self._raw_fh.flush()
            self._last_raw_flush = time.monotonic()

    # --- rotation
    def _maybe_rotate(self) -> None:
        # Start a new window when the UTC date changes (so each file belongs to one date folder)
        # or when the window has lasted max_seconds.
        now = dt.datetime.now(dt.timezone.utc)
        if (now.date() != self._window_start.date()
                or (now - self._window_start).total_seconds() >= self.max_seconds):
            self.rotate(now)

    def rotate(self, now: dt.datetime | None = None) -> None:
        """Write buffers and start a new window (new raw file, new parquet parts)."""
        now = now or dt.datetime.now(dt.timezone.utc)
        for b in self.buckets.values():
            b.flush(self._window_start)
        if self._raw_fh is not None:
            self._raw_fh.close()
            self._raw_fh = None
        self._window_start = now

    def close(self) -> None:
        """Flush everything to disk; call on shutdown so buffered data is not lost."""
        self.rotate()

    # --- main loop
    async def run(self, duration_s: float | None = None, report_every_s: float = 10.0) -> None:
        # `async def`/`await`: see the primer in live.py. create_task starts the feed concurrently
        # while this method keeps ticking once per second (rotation, progress log, duration check).
        feed_task = asyncio.create_task(self.feed.run())
        t0 = time.monotonic()
        try:
            while not feed_task.done():
                await asyncio.sleep(1.0)
                self._maybe_rotate()  # time-based rotation even when a stream is quiet
                el = time.monotonic() - t0
                if int(el) % int(report_every_s) == 0:
                    c = self.feed.counts
                    log.info("t=%3.0fs state=%s trades=%d quotes=%d depth_msgs=%d reconnects=%d gaps=%d",
                             el, self.feed.sync.state.value, c["trade"], c["quote"], c["depth"],
                             self.feed.reconnects, self.feed.sync.gaps)
                if duration_s is not None and el >= duration_s:
                    break
        finally:
            # `finally` always runs: on timeout, error AND Ctrl+C. Stop the feed, then write out
            # whatever is still buffered so a clean shutdown loses nothing.
            feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
            self.close()
            log.info("recorder closed; rows written: %s",
                     {k: b.rows_written for k, b in self.buckets.items()})


def main(argv: list[str] | None = None) -> int:
    # Command-line interface: argparse turns `--duration 60` into args.duration == 60.0.
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Record Binance USD-M live depth/trades/quotes.")
    ap.add_argument("--symbol", default=cfg["symbol"])
    ap.add_argument("--duration", type=float, default=None, help="seconds to record (default: forever)")
    ap.add_argument("--no-raw", action="store_true", help="do not keep the raw JSON frames")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    rec = Recorder(LiveFeed(args.symbol, cfg), record_raw=not args.no_raw)
    # asyncio.run starts the event loop and runs our coroutine to completion. On Ctrl+C Python
    # cancels it (running the `finally` above) and then raises KeyboardInterrupt here.
    try:
        asyncio.run(rec.run(args.duration))
    except KeyboardInterrupt:
        # asyncio.run cancelled rec.run(), whose `finally` already flushed everything.
        log.info("Ctrl+C: stopped cleanly")
    return 0


# Runs main() only when executed as a script (`python -m mm.data.record`), not when imported.
if __name__ == "__main__":
    raise SystemExit(main())
