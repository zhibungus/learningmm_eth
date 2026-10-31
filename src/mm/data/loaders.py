"""Load processed market data and replay it as one time-ordered event stream.

=====================================================================================
REPLAY CONTRACT (what the backtester can rely on)
=====================================================================================

    from mm.data.loaders import replay
    for ev in replay("2026-10-17", "2026-10-18", symbol="ETHUSDT",
                     streams=("trades", "depth", "funding")):
        ...

``replay(start, end, symbol=None, streams=..., source="archive", trade_dataset="agg_trades",
         strict=True, cfg=None) -> Iterator[Event]``

``source`` is either one string for every stream (``"archive"`` | ``"live"``) or a dict choosing
a source per stream, e.g. ``source={"depth": "live", "funding": "archive"}``. Streams missing from
the dict default to "live" for depth and "archive" for everything else.

* ``start`` / ``end``: dates (``datetime.date`` or ``"YYYY-MM-DD"``), UTC, **both inclusive**.
  The window is ``[start 00:00:00, end+1day 00:00:00)`` in ``ts_ns``.
* ``streams``: any subset of ``"trades"``, ``"quotes"``, ``"depth"``, ``"funding"``.
* ``source``: ``"archive"`` (data.binance.vision) or ``"live"`` (our own recorder output), as a
  string for all streams or a ``{stream: source}`` dict (see above).
* Every yielded object is one of the frozen dataclasses in ``mm.data.events``:

    TradeEvent   ts_ns, event_ts_ns, recv_ts_ns, trade_id, first_id, last_id, price, qty, side
                 side = aggressor: "buy" | "sell"
    QuoteEvent   ts_ns, event_ts_ns, recv_ts_ns, update_id, bid_px, bid_qty, ask_px, ask_qty
    DepthEvent   ts_ns, event_ts_ns, recv_ts_ns, is_snapshot, first_update_id, final_update_id,
                 prev_final_update_id, bids=((px, qty), ...), asks=((px, qty), ...)
                 One DepthEvent == one exchange message (all its levels applied atomically).
                 qty == 0 deletes the level; is_snapshot=True replaces the whole book.
                 Apply with ``mm.data.book.OrderBook.apply(ev)``.
    FundingEvent ts_ns, rate, mark_price (may be None), interval_hours
    GapEvent     ts_ns, reason ("session_start" | "resync" | "disconnect"), stream (str | None)
                 ``kind == "gap"``. Means "the live feed was interrupted; your book is stale".

  ``ev.kind`` is "gap" | "trade" | "quote" | "depth" | "funding".

* GAP MARKERS (only when some stream comes from the live recorder): a GapEvent is delivered
  IMMEDIATELY BEFORE every snapshot DepthEvent (``is_snapshot=True``), with the same ``ts_ns``.
  Reasons: "session_start" = first snapshot of a recording session, "resync" = the recorder saw a
  sequence gap and rebuilt the book. Extra "disconnect" markers (stream = "public"/"market") are
  delivered at the exchange time of the last data before a websocket dropped. Markers persisted by
  the recorder (``gaps`` dataset) are used when present; for older recordings without markers a
  GapEvent is synthesised before each snapshot (first one "session_start", later ones "resync").
  Consumers should drop their book / hold quotes on any GapEvent until the next snapshot.

* ORDERING (deterministic): events are sorted by
      (ts_ns, kind priority, order within the stored file)
  where ``ts_ns`` is exchange matching-engine time (see ``schemas.py``) and the kind priority comes
  from ``config/data.yaml: replay.kind_priority`` (default gap < funding < trade < depth < quote).
  So at an identical timestamp a trade is delivered BEFORE the book update it caused. Calling
  replay twice on the same files gives the identical sequence.
* ``ts_ns`` is when the thing happened at the exchange, NOT when your strategy could have known
  it. The backtester must add its own latency model (``recv_ts_ns`` is populated only for
  live-recorded data; archive data has none).
* Memory: files are read one at a time and turned into events lazily, so a multi-day replay does
  not hold everything in RAM.
* ``strict=True`` raises ``ReplayOrderError`` if a stream's timestamps go backwards between
  files (data problem). ``strict=False`` silently keeps going.
* Missing dates are NOT an error here (use ``quality.report`` to see gaps). If a requested stream
  has no files at all, a ``FileNotFoundError`` is raised so you never replay empty by accident.

DataFrame loaders (sorted by ts_ns, canonical schemas from ``schemas.py``):
    load_trades / load_quotes / load_depth / load_funding (start, end, symbol=None, ...)
"""
# Postpones type-hint evaluation (see config.py).
from __future__ import annotations

import datetime as dt
import heapq  # heapq.merge lazily merges several already-sorted iterators into one sorted stream
from pathlib import Path
from typing import Iterable, Iterator

import polars as pl
import pyarrow.parquet as pq  # reads parquet footers (metadata) without loading the data

from . import schemas as S
from .config import load_config, processed_dir
from .events import DepthEvent, Event, FundingEvent, GapEvent, QuoteEvent, TradeEvent

# Type alias: anywhere a date is expected we accept a date object or an 'YYYY-MM-DD' string.
DateLike = dt.date | str
_NS_PER_DAY = 86_400 * 1_000_000_000
STREAMS = ("trades", "quotes", "depth", "funding")

# A source spec is one string for all streams, or a dict {stream name: "archive" | "live"}.
SourceSpec = str | dict[str, str]
SOURCES = ("archive", "live")


def resolve_source(source: SourceSpec, stream: str) -> str:
    """Which source (archive/live) a given stream should be read from."""
    if isinstance(source, str):
        return source
    # dict.get(key, default): depth only exists live, everything else defaults to the archive.
    return source.get(stream, "live" if stream == "depth" else "archive")


# A custom exception class so callers can catch this specific data problem.
class ReplayOrderError(RuntimeError):
    """A stream's timestamps decreased between files."""


# ---------------------------------------------------------------- file discovery
def _to_date(d: DateLike) -> dt.date:
    return d if isinstance(d, dt.date) else dt.date.fromisoformat(d)


# Midnight UTC of a date, as integer nanoseconds since 1970 (the project's time unit).
def _date_ns(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000


def _period_overlaps(label: str, start: dt.date, end: dt.date) -> bool:
    """Does a folder named 'YYYY-MM-DD' or 'YYYY-MM' overlap [start, end]?"""
    if len(label) == 10:
        d = dt.date.fromisoformat(label)
        return start <= d <= end
    y, m = int(label[:4]), int(label[5:7])
    first = dt.date(y, m, 1)
    last = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1) - dt.timedelta(days=1)
    return first <= end and last >= start


def _folder_for(stream: str, source: str, trade_dataset: str) -> str:
    """Folder name under the processed dir. Only trades differ: the archive has two datasets."""
    if stream == "trades" and source == "archive":
        return trade_dataset
    return stream


def find_files(stream: str, start: DateLike, end: DateLike, symbol: str | None = None,
               source: str = "archive", trade_dataset: str = "agg_trades",
               cfg: dict | None = None) -> list[Path]:
    """Parquet files of ``stream`` whose folder date overlaps [start, end], in time order."""
    # Layout on disk: <processed>/<stream folder>/<SYMBOL>/<period folder>/*.parquet. We pick the
    # period folders (named by date or month) that overlap the request, so we never open files
    # for dates that aren't needed. sorted() gives chronological order because ISO dates sort
    # alphabetically in time order.
    cfg = cfg or load_config()
    symbol = symbol or cfg["symbol"]
    s, e = _to_date(start), _to_date(end)
    root = processed_dir(cfg, live=(source == "live")) / _folder_for(stream, source, trade_dataset) / symbol
    if not root.exists():
        return []
    files: list[Path] = []
    # Live recordings made before the exchange-time foldering fix may have the last seconds of a
    # day in the NEXT day's folder, so look one day wider on each side; the ts_ns window filter in
    # _read_window trims anything outside the requested dates anyway.
    if source == "live":
        s, e = s - dt.timedelta(days=1), e + dt.timedelta(days=1)
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if _period_overlaps(sub.name, s, e):
            files += sorted(sub.glob("*.parquet"))
    if source == "live":
        # Recorder part files are named after the PC clock, which can be seconds off the exchange
        # clock, so name order is NOT time order around midnight (and old recordings may sit in
        # the neighbouring day's folder). Order live files by the earliest EXCHANGE timestamp
        # they contain instead; ties fall back to the path so the order stays deterministic.
        files.sort(key=lambda f: (_file_min_ts(f), str(f)))
    return files


# Smallest ts_ns stored in a parquet file. Parquet keeps per-column min/max statistics in the file
# footer, so this normally reads a few bytes instead of the whole file; if a file has no
# statistics we fall back to scanning the column.
def _file_min_ts(path: Path) -> int:
    meta = pq.ParquetFile(path).metadata
    col = meta.schema.names.index("ts_ns")
    mins = []
    for rg in range(meta.num_row_groups):
        st = meta.row_group(rg).column(col).statistics
        if st is None or not st.has_min_max:
            return int(pl.read_parquet(path, columns=["ts_ns"])["ts_ns"].min() or 0)
        mins.append(st.min)
    return int(min(mins)) if mins else 0


# Read one parquet file, keep rows inside [lo_ns, hi_ns) and sort by time. The sort is STABLE
# (maintain_order=True): equal timestamps keep file order, so replay is deterministic.
# filter(...) takes a polars expression: the `&` combines two column comparisons.
def _read_window(path: Path, lo_ns: int, hi_ns: int) -> pl.DataFrame:
    df = pl.read_parquet(path).filter((pl.col("ts_ns") >= lo_ns) & (pl.col("ts_ns") < hi_ns))
    return df.sort("ts_ns", maintain_order=True)


# Shared implementation behind load_trades/load_quotes/...: gather the files, window them, concat.
def _load(stream: str, start: DateLike, end: DateLike, symbol: str | None, source: str,
          trade_dataset: str, cfg: dict | None) -> pl.DataFrame:
    cfg = cfg or load_config()
    lo, hi = _date_ns(_to_date(start)), _date_ns(_to_date(end)) + _NS_PER_DAY
    files = find_files(stream, start, end, symbol, source, trade_dataset, cfg)
    if not files:
        return S.empty(stream)
    frames = [_read_window(f, lo, hi) for f in files]
    return pl.concat(frames).sort("ts_ns", maintain_order=True)


# ---- Public DataFrame loaders: easy for analysis in notebooks. For backtesting use replay().
def load_trades(start: DateLike, end: DateLike, symbol: str | None = None, source: str = "archive",
                trade_dataset: str = "agg_trades", cfg: dict | None = None) -> pl.DataFrame:
    """Trades in [start, end] (dates inclusive) as a DataFrame (``schemas.TRADES_SCHEMA``).

    ``trade_dataset``: "agg_trades" or "trades" (archive only; live always records aggTrade).
    """
    return _load("trades", start, end, symbol, source, trade_dataset, cfg)


def load_quotes(start: DateLike, end: DateLike, symbol: str | None = None, source: str = "archive",
                cfg: dict | None = None) -> pl.DataFrame:
    """Top-of-book quotes (``schemas.QUOTES_SCHEMA``)."""
    return _load("quotes", start, end, symbol, source, "agg_trades", cfg)


def load_depth(start: DateLike, end: DateLike, symbol: str | None = None, source: str = "live",
               cfg: dict | None = None) -> pl.DataFrame:
    """L2 depth rows (``schemas.DEPTH_SCHEMA``). Only live-recorded data exists."""
    return _load("depth", start, end, symbol, source, "agg_trades", cfg)


def load_gaps(start: DateLike, end: DateLike, symbol: str | None = None, source: str = "live",
              cfg: dict | None = None) -> pl.DataFrame:
    """Gap/resync markers written by the live recorder (``schemas.GAPS_SCHEMA``)."""
    return _load("gaps", start, end, symbol, source, "agg_trades", cfg)


def load_funding(start: DateLike, end: DateLike, symbol: str | None = None,
                 source: str = "archive", cfg: dict | None = None) -> pl.DataFrame:
    """Funding settlements (``schemas.FUNDING_SCHEMA``)."""
    return _load("funding", start, end, symbol, source, "agg_trades", cfg)


# ---------------------------------------------------------------- frame -> events
# Each converter is a GENERATOR (a function with `yield`): it produces one event at a time on
# demand instead of building a giant list, so memory stays small. iter_rows() yields each table
# row as a tuple, and `TradeEvent(*r)` unpacks that tuple into the constructor's arguments.
def _trade_events(df: pl.DataFrame) -> Iterator[TradeEvent]:
    for r in df.select(list(S.TRADES_SCHEMA)).iter_rows():
        yield TradeEvent(*r)


def _quote_events(df: pl.DataFrame) -> Iterator[QuoteEvent]:
    for r in df.select(list(S.QUOTES_SCHEMA)).iter_rows():
        yield QuoteEvent(*r)


def _funding_events(df: pl.DataFrame) -> Iterator[FundingEvent]:
    for r in df.select(list(S.FUNDING_SCHEMA)).iter_rows():
        yield FundingEvent(*r)


def _gap_events(df: pl.DataFrame) -> Iterator[GapEvent]:
    for r in df.select(list(S.GAPS_SCHEMA)).iter_rows():
        yield GapEvent(*r)


def _depth_events(df: pl.DataFrame) -> Iterator[DepthEvent]:
    """Group consecutive level rows of the same message into one DepthEvent."""
    # A depth message is stored as several rows (one per price level) that share the same
    # (ts, snapshot flag, first id, last id). Walk the rows in order, and whenever that key changes,
    # emit the finished message and start a new one.
    cur_key = None
    bids: list = []
    asks: list = []
    head: tuple = ()
    for (ts, ets, rts, side, px, q, snap, fu, lu, pu) in df.select(list(S.DEPTH_SCHEMA)).iter_rows():
        key = (ts, snap, fu, lu)
        if key != cur_key:
            if cur_key is not None:
                yield DepthEvent(*head, tuple(bids), tuple(asks))
            cur_key, bids, asks = key, [], []
            head = (ts, ets, rts, snap, fu, lu, pu)
        (bids if side == S.BID else asks).append((px, q))
    if cur_key is not None:
        yield DepthEvent(*head, tuple(bids), tuple(asks))


# Dispatch table: stream name -> converter function.
_CONVERTERS = {"trades": _trade_events, "quotes": _quote_events,
               "depth": _depth_events, "funding": _funding_events, "gaps": _gap_events}


# One stream's events in time order, file by file (only one file is in memory at a time).
def _stream_events(stream: str, files: list[Path], lo: int, hi: int, strict: bool) -> Iterator[Event]:
    conv = _CONVERTERS[stream]
    last_ts = None
    for f in files:
        df = _read_window(f, lo, hi)
        if df.is_empty():
            continue
        first = df["ts_ns"][0]
        if strict and last_ts is not None and first < last_ts:
            raise ReplayOrderError(f"{stream}: {f} starts at {first} < previous file end {last_ts}")
        last_ts = df["ts_ns"][-1]
        yield from conv(df)
        del df  # free this file's table before the next one is read, to keep peak memory low


def replay(start: DateLike, end: DateLike, symbol: str | None = None,
           streams: Iterable[str] = ("trades", "quotes", "depth", "funding"),
           source: SourceSpec = "archive", trade_dataset: str = "agg_trades",
           strict: bool = True, cfg: dict | None = None, from_ns: int | None = None) -> Iterator[Event]:
    """Merge the requested streams into one deterministic, time-ordered event iterator.

    ``source`` may be a string or a per-stream dict (see the module docstring), and GapEvents are
    emitted for live data. See the module docstring for the full contract.
    ``from_ns`` (optional): start inside the first day, at this ``ts_ns``, so a backtest warm-up does not
    build millions of events just to throw them away. Default: the start of day ``start``.
    """
    # Validate input first. `set(a) - set(b)` is set difference: names in `a` not allowed in `b`.
    cfg = cfg or load_config()
    streams = tuple(streams)
    bad = set(streams) - set(STREAMS)
    if bad:
        raise ValueError(f"unknown streams {sorted(bad)}; choose from {STREAMS}")
    if isinstance(source, dict):
        unknown = set(source) - set(STREAMS)
        if unknown:
            raise ValueError(f"unknown streams in source dict: {sorted(unknown)}")
    if not {resolve_source(source, st) for st in streams} <= set(SOURCES):
        raise ValueError(f"source values must be in {SOURCES}")
    s, e = _to_date(start), _to_date(end)
    lo, hi = _date_ns(s), _date_ns(e) + _NS_PER_DAY
    if from_ns is not None:
        lo = max(lo, from_ns)
    prio = cfg["replay"]["kind_priority"]
    kind_of = {"trades": "trade", "quotes": "quote", "depth": "depth", "funding": "funding",
               "gaps": "gap"}

    # A nested function (closure) that can see `lo`, `hi`, `strict`, `prio` from replay() above.
    # It tags each event with a SORT KEY (ts_ns, kind priority, running counter); heapq.merge
    # compares only these keys. Equal timestamps are therefore resolved by kind priority first
    # (config replay.kind_priority), then by file order, never randomly.
    def keyed(stream: str, files: list[Path]):
        p = prio[kind_of[stream]]
        for seq, ev in enumerate(_stream_events(stream, files, lo, hi, strict)):
            yield (ev.ts_ns, p, seq), ev

    any_live = any(resolve_source(source, st) == "live" for st in streams)
    depth_live = "depth" in streams and resolve_source(source, "depth") == "live"

    # Persisted gap markers (written by the recorder). They are small, so read them all now.
    # We remember the timestamps of the book markers (session_start/resync) so we know which
    # snapshots already have a marker and which ones need a synthesised one.
    markers: list[GapEvent] = []
    if any_live:
        gfiles = find_files("gaps", s, e, symbol, "live", trade_dataset, cfg)
        markers = list(_stream_events("gaps", gfiles, lo, hi, False))
    # Sorted timestamps of the persisted BOOK markers. A "resync" marker is stamped at the time the
    # sequence gap was DETECTED, which is earlier than the repairing snapshot, so a marker no longer
    # shares its snapshot's timestamp: instead each snapshot "consumes" every not-yet-used book
    # marker with ts <= the snapshot's ts (see keyed_depth).
    book_marker_ts = sorted(m.ts_ns for m in markers if m.reason in ("session_start", "resync"))

    # Depth stream with synthesised markers: before each snapshot that no persisted marker covers,
    # yield a GapEvent key with the gap priority (lower than depth). NOTE: a synthesised marker can
    # only use the SNAPSHOT's timestamp, because old recordings never stored when the gap was
    # detected; so for those, orders rest on a stale book until the snapshot time. Recordings with
    # persisted markers do not have this limitation.
    def keyed_depth(files: list[Path]):
        p_depth, p_gap = prio["depth"], prio["gap"]
        seq, first_snapshot = 0, True
        used = 0  # how many entries of book_marker_ts earlier snapshots have already consumed
        for ev in _stream_events("depth", files, lo, hi, strict):
            if ev.is_snapshot:
                # Advance past every persisted marker at or before this snapshot (a loop is a
                # simple way to count them since the list is sorted).
                covered = False
                while used < len(book_marker_ts) and book_marker_ts[used] <= ev.ts_ns:
                    used += 1
                    covered = True
                if not covered:
                    reason = "session_start" if first_snapshot else "resync"
                    yield (ev.ts_ns, p_gap, seq), GapEvent(ev.ts_ns, reason, "depth")
                    seq += 1
                first_snapshot = False
            yield (ev.ts_ns, p_depth, seq), ev
            seq += 1

    def keyed_markers():
        p_gap = prio["gap"]
        # Markers are stored in write order; sort by time (stable) so the merge input is sorted.
        for seq, m in enumerate(sorted(markers, key=lambda g: g.ts_ns)):
            yield (m.ts_ns, p_gap, seq), m

    # One keyed iterator per requested stream; merge() pulls the smallest key across all of them
    # each step, so only one event per stream is held in memory.
    iters = []
    for st in streams:
        src = resolve_source(source, st)
        files = find_files(st, s, e, symbol, src, trade_dataset, cfg)
        if not files:
            raise FileNotFoundError(f"no processed '{st}' files for {symbol or cfg['symbol']} "
                                    f"{s}..{e} (source={src})")
        iters.append(keyed_depth(files) if (st == "depth" and depth_live) else keyed(st, files))
    if markers:
        iters.append(keyed_markers())
    # `key=lambda kv: kv[0]` tells merge to compare the (key, event) pairs by the key only.
    for _, ev in heapq.merge(*iters, key=lambda kv: kv[0]):
        yield ev
