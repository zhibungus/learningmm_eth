"""Convert our processed live Binance recordings into hftbacktest 2.4.4 event files.

This is a SEPARATE learning track from src/mm/. It only READS data/processed/ and data/raw/ and only
WRITES under data/hft/. Run it with the dedicated venv (see hft/README.md):

    .venv-hft\\Scripts\\python.exe hft\\convert.py --date 2026-10-24 --start 10:00 --end 11:00

Output (per day):  data/hft/<SYMBOL>/<date>/<SYMBOL>_<date>_<HH>.npz          one file per UTC hour
                   data/hft/<SYMBOL>/<date>/<SYMBOL>_<date>_<HH>_snapshot.npz initial book at the window start
                   data/hft/<SYMBOL>/<date>/gaps.json, manifest.json

Background: what hftbacktest wants
----------------------------------
A numpy *structured array* (like a table whose rows have typed named fields) with dtype ``event_dtype``:
    ev       bit flags: event kind (DEPTH_EVENT, TRADE_EVENT, ...) | side (BUY_EVENT/SELL_EVENT)
             | which clock the row belongs to (EXCH_EVENT = exchange-side copy, LOCAL_EVENT = our side)
    exch_ts  exchange time, integer ns       local_ts  OUR receive time on the exchange clock, ns
    px, qty  price and quantity (for depth rows: qty is the NEW absolute size at px, 0 = level deleted)
    order_id, ival, fval  unused here (0)
Every real event is stored as an EXCH copy (it happens at the exchange at exch_ts) and a LOCAL copy
(we learn about it at local_ts). The EXCH copies must be sorted by exch_ts and the LOCAL copies by
local_ts. ``correct_event_order`` (from hftbacktest) builds such a file from two sort orders.

"Bit flags": integers used as a bag of yes/no switches. ``A | B`` (bitwise OR) turns both switches on,
``x & B`` tests/keeps switch B, ``x & ~B`` turns B off. e.g. DEPTH_EVENT | BUY_EVENT = "bid-side depth update".
"""
# Lets us write type hints like `int | None` on any Python 3.x (hints are just notes for the reader).
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

# These imports come from the installed hftbacktest package (the .venv-hft environment).
from hftbacktest.data.utils.snapshot import create_last_snapshot
from hftbacktest.data.validation import correct_event_order, validate_event_order
from hftbacktest.types import (
    BUY_EVENT, DEPTH_CLEAR_EVENT, DEPTH_EVENT, DEPTH_SNAPSHOT_EVENT, EXCH_EVENT, LOCAL_EVENT,
    SELL_EVENT, TRADE_EVENT, event_dtype,
)

# ----------------------------------------------------------------------------------------------
# Constants (every tunable number lives here, none are buried in the code below).
# ----------------------------------------------------------------------------------------------
PROJECT = Path(__file__).resolve().parent.parent   # .../test folder  (this file is in hft/)
NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000
NS_PER_H = 3600 * NS_PER_S

CLAMP_NS = 1 * NS_PER_MS          # local_ts <= exch_ts rows are moved to exch_ts + 1 ms
LOOKBACK_S = 60                   # extra seconds read BEFORE an hour: events from the previous hour whose
                                  # local copy lands in this hour (feed latency); must exceed max latency
ROBUST_BIN_S = 60               # robust clock offset: bin width (seconds, by recv time)
ROBUST_MIN_ROWS = 200             # bins with fewer rows use the minimum instead of the 1st percentile
ROBUST_SMOOTH_BINS = 15           # rolling-median width (bins) of the floor, inside one segment
JUMP_MS = 100                     # a floor change bigger than this between adjacent bins = clock jump
HIST_CAP_MS = 120_000            # latency histogram: 1 ms bins up to 2 min (bigger values go to the last bin)
# Price that clears a WHOLE side. hftbacktest's DEPTH_CLEAR_EVENT(px) on the bid side removes every bid
# level with price >= px, on the ask side every ask level with price <= px (verified by experiment, see
# hft/README.md). So bid px=0.0 and ask px=1e9 clear everything.
CLEAR_ALL_BID_PX = 0.0
CLEAR_ALL_ASK_PX = 1.0e9

# Tie-break "rank" for events that share the same timestamp (smaller = earlier). Documented in README:
#   0 book clears (gap / snapshot start) -> 1 trades -> 2 depth rows (diffs, snapshot rows)
# Rationale: a book reset must precede everything; a trade (the cause) is replayed before the depth
# update (the effect) stamped with the same millisecond. Within a kind the original recorded order is kept.
RANK_CLEAR, RANK_TRADE, RANK_DEPTH = 0, 1, 2

# numpy needs the flag constants as unsigned 64-bit integers to combine them with the 'ev' column (u8).
EX = np.uint64(EXCH_EVENT)
LO = np.uint64(LOCAL_EVENT)


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    """SHA-256 fingerprint of a file (reads in 8 MB blocks so big files do not fill memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:                     # `with` = context manager: closes the file for us
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):   # lambda = tiny anonymous function
            h.update(block)
    return h.hexdigest()


def utc_ns(date: str, hhmm: str) -> int:
    """'2026-10-24', '10:30' -> integer nanoseconds since 1970 UTC. '24:00' means next day 00:00."""
    h, m = (int(x) for x in hhmm.split(":"))        # generator expression unpacked into two names
    base = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(base.timestamp()) * NS_PER_S + (h * 3600 + m * 60) * NS_PER_S


def iso(ns: int) -> str:
    """ns -> readable UTC string, for logs and JSON."""
    return dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# ----------------------------------------------------------------------------------------------
# Input access
# ----------------------------------------------------------------------------------------------
class Inputs:
    """Knows where the processed parquet parts and the clock-offset log are, and reads time windows."""

    def __init__(self, symbol: str, mode: str = "robust", horizon_ns: int = 1 << 62):
        self.symbol, self.mode, self.horizon_ns = symbol, mode, horizon_ns
        if mode not in ("robust", "measured"):
            raise ValueError(f"unknown offset mode {mode}")
        self.proc = PROJECT / "data" / "processed" / "binance_um_live"
        self.raw = PROJECT / "data" / "raw" / "binance_um_live" / symbol
        # Part files are named part-<first exchange ts_ns, 19 digits>-<n>.parquet. Older test files used
        # a short HHMMSS name; we ignore those (only the 19-digit names are chronological).
        self.parts = {k: self._list(k) for k in ("depth", "trades", "gaps", "quotes")}
        self.files_read: set[Path] = set()            # remembered for the manifest (SHA-256 + row counts)
        self._load_offsets()
        self._build_robust()                           # always built: used by 'robust', and reported for both

    def _list(self, dataset: str) -> list[tuple[int, Path]]:
        """All part files of a dataset as [(first_ts_ns, path)] sorted by time."""
        out = []
        for p in (self.proc / dataset / self.symbol).glob("*/part-*.parquet"):
            first = p.name.split("-")[1]
            if len(first) == 19:                       # 19-digit ns timestamp => new naming scheme
                out.append((int(first), p))
        # Sort by (first ts, file name) so the n-counter breaks ties; lists compare element by element.
        return sorted(out, key=lambda t: (t[0], t[1].name))

    def _load_offsets(self) -> None:
        """Read the clock-offset log. Rows look like {"ts_ns":..., "status":"clock_offset_ms", "detail":"-1394.2"}.

        offset = (our PC clock) - (Binance clock). Positive => our PC is AHEAD, so the exchange-clock
        time of a local timestamp is recv - offset (this is exactly what record.py line 158 does).
        These REST-based readings are noisy (+-300 ms for an hour at a time), see _build_robust.
        """
        path = self.raw / "status.jsonl"
        ts, off = [], []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:           # the recorder may be mid-write on the last line
                    continue
                if row.get("status") == "clock_offset_ms":
                    ts.append(int(row["ts_ns"]))
                    off.append(int(round(float(row["detail"]) * NS_PER_MS)))   # ms -> ns, integer
        order = np.argsort(np.asarray(ts), kind="stable")  # argsort = the indices that would sort the array
        self.off_ts = np.asarray(ts, dtype=np.int64)[order]
        self.off_ns = np.asarray(off, dtype=np.int64)[order]
        self.status_path = path

    def _build_robust(self) -> None:
        """Robust clock offset = smooth 'floor' of the receive delay minus a constant level L0.

        Idea. For each message  g = recv_ts - event_ts = clock_offset(t) + one_way_delay.  The delay is
        never below some physical minimum (~200 ms here), so the LOW end ("floor") of g inside a short
        window follows the clock offset (drift, and the jumps when Windows re-syncs the PC clock),
        while busy moments only push g UP. We use E (event time, not T) so the exchange's own
        batching delay (T -> E) is not mistaken for clock offset.
        1. per 1-minute bin (by recv time) take the 1st percentile of g over depth + trades pooled
           (the minimum if the bin has < ROBUST_MIN_ROWS rows);
        2. a clock JUMP is a change of the floor > JUMP_MS between consecutive bins (normal drift
           is ~1 ms/min). Jumps cut the series into segments;
        3. smooth the floor with a rolling median of ROBUST_SMOOTH_BINS bins, only INSIDE a segment;
        4. L0 = median over all REST readings of (smoothed floor at the reading time - the reading).
           The REST readings are individually noisy but their median pins down the absolute level
           (= the minimum one-way delay), which one-way timestamps alone cannot give;
        5. offset_robust(t) = smoothed_floor(t) - L0 (linear inside a segment, a step at a jump).
        Limitation: any real change of the delay FLOOR slower than ~15 min is read as clock drift.
        """
        per_file = []
        for ds in ("depth", "trades"):
            for _, f in self.parts[ds]:
                df = pl.read_parquet(f, columns=["recv_ts_ns", "event_ts_ns"])
                if df.height == 0:
                    continue
                # `g` is recv - event time in ns (float so quantile works); `bin` = minute number of recv time
                per_file.append(df.select(
                    bin=pl.col("recv_ts_ns") // (ROBUST_BIN_S * NS_PER_S),
                    g=(pl.col("recv_ts_ns") - pl.col("event_ts_ns")).cast(pl.Float64),
                ).group_by("bin").agg(q=pl.col("g").quantile(0.01), mn=pl.col("g").min(), n=pl.len()))
        # A bin can be split over several files (and over depth/trades): combine with a row-weighted
        # mean of the percentiles; the min of the minima for bins with too few rows.
        b = (pl.concat(per_file)
             .group_by("bin").agg(qw=(pl.col("q") * pl.col("n")).sum(), mn=pl.col("mn").min(), n=pl.col("n").sum())
             .with_columns(floor=pl.when(pl.col("n") >= ROBUST_MIN_ROWS).then(pl.col("qw") / pl.col("n")).otherwise(pl.col("mn")))
             .sort("bin"))
        bw = ROBUST_BIN_S * NS_PER_S
        start = b["bin"].to_numpy().astype(np.int64) * bw         # bin start (recv ns)
        floor = b["floor"].to_numpy()
        # --- jumps: where the floor moves by more than JUMP_MS from one bin to the next ---
        jump_at = np.flatnonzero(np.abs(np.diff(floor)) > JUMP_MS * NS_PER_MS) + 1   # index of first bin after
        seg_of_bin = np.zeros(len(floor), dtype=np.int64)
        seg_of_bin[jump_at] = 1
        seg_of_bin = np.cumsum(seg_of_bin)                         # segment number of every bin
        smooth = np.empty_like(floor)
        half = ROBUST_SMOOTH_BINS // 2
        for sgm in np.unique(seg_of_bin):
            idx = np.flatnonzero(seg_of_bin == sgm)
            fl = floor[idx]
            # centred rolling median whose window shrinks at the segment edges (so it never crosses a jump)
            smooth[idx] = [np.median(fl[max(0, i - half):i + half + 1]) for i in range(len(fl))]
        self.rb_start, self.rb_seg, self.rb_smooth = start, seg_of_bin, smooth
        self.rb_centre = start + bw // 2
        # --- level L0 from the readings ---
        use = (self.off_ts <= self.horizon_ns)
        near = np.abs(self.rb_centre[np.clip(np.searchsorted(self.rb_centre, self.off_ts), 0, len(start) - 1)]
                      - self.off_ts) < 2 * bw                      # a bin within 2 minutes of the reading
        use &= near
        implied = self._floor_at(self.off_ts[use]) - self.off_ns[use]
        self.L0_ns = float(np.median(implied))
        self.jumps = [{"at_recv_utc": iso(int(start[i])), "floor_before_ms": float(floor[i - 1] / 1e6),
                       "floor_after_ms": float(floor[i] / 1e6)} for i in jump_at]
        self.offset_info = {
            "mode": self.mode, "readings": int(len(self.off_ts)), "readings_used_for_L0": int(use.sum()),
            "L0_ms": self.L0_ns / 1e6, "implied_L0_ms_p10_p50_p90": [float(x) / 1e6 for x in np.percentile(implied, [10, 50, 90])],
            "bin_s": ROBUST_BIN_S, "jump_threshold_ms": JUMP_MS, "smooth_bins": ROBUST_SMOOTH_BINS,
            "n_jumps": int(len(jump_at)), "jumps": self.jumps}

    def _floor_at(self, recv_ns: np.ndarray) -> np.ndarray:
        """Smoothed floor (ns) at the given receive times: linear between bin centres inside a segment,
        and a STEP at segment boundaries (a time before the first bin of a segment belongs to the previous one)."""
        out = np.empty(len(recv_ns))
        # segment of each time = segment of the last bin that starts at or before it
        bi = np.clip(np.searchsorted(self.rb_start, recv_ns, side="right") - 1, 0, len(self.rb_start) - 1)
        seg = self.rb_seg[bi]
        for sgm in np.unique(seg):
            m = seg == sgm
            sel = self.rb_seg == sgm
            # np.interp(x, xs, ys) = piecewise-linear interpolation (flat outside the data range)
            out[m] = np.interp(recv_ns[m], self.rb_centre[sel], self.rb_smooth[sel])
        return out

    def offset_for(self, recv_ns: np.ndarray, mode: str | None = None) -> np.ndarray:
        """Clock offset (our clock minus exchange clock, ns) at each receive time, for `mode` (default self.mode)."""
        mode = mode or self.mode
        if mode == "robust":
            return np.rint(self._floor_at(recv_ns) - self.L0_ns).astype(np.int64)
        # 'measured': AS-OF JOIN with the REST readings: for every receive time, the latest reading at or
        # before it. np.searchsorted(a, x, 'right') = number of readings with ts <= x; minus 1 = index of
        # the latest one. Before the first reading that gives -1, which we clip to 0 (= use the first).
        idx = np.searchsorted(self.off_ts, recv_ns, side="right") - 1
        np.maximum(idx, 0, out=idx)
        return self.off_ns[idx]
    def select(self, dataset: str, lo: int, hi: int) -> list[Path]:
        """Part files that may hold rows with exchange ts in [lo, hi). A file covers [its start, next start)."""
        parts = self.parts[dataset]
        starts = [s for s, _ in parts]
        keep = []
        for k, (s, p) in enumerate(parts):
            nxt = starts[k + 1] if k + 1 < len(parts) else 1 << 62
            if s < hi and nxt >= lo:
                keep.append(p)
        return keep

    def read(self, dataset: str, lo: int, hi: int, columns: list[str]) -> pl.DataFrame:
        """Rows of `dataset` with lo <= ts_ns < hi, in recorded (storage) order."""
        files = self.select(dataset, lo, hi)
        if not files:
            return pl.DataFrame({c: [] for c in columns})
        self.files_read.update(files)
        # scan_parquet = lazy reader: nothing is loaded until .collect(); .filter() is pushed down so only
        # matching row groups are read. The `pl.col(...)` expressions describe columns without looping.
        lf = pl.scan_parquet([str(f) for f in files]).filter(
            (pl.col("ts_ns") >= lo) & (pl.col("ts_ns") < hi)).select(columns)
        return lf.collect()


# ----------------------------------------------------------------------------------------------
# Local timestamps
# ----------------------------------------------------------------------------------------------
def make_local(inp: Inputs, exch: np.ndarray, recv: np.ndarray, stats: dict | None, in_win: np.ndarray | None):
    """Our receive time expressed on the exchange clock, repaired.

    1. local = recv - clock_offset (as-of).                      [recv is our PC clock]
    2. rows with local <= exch (impossible: we cannot hear it before it happens) -> exch + 1 ms.  (clamp)
    3. running maximum (np.maximum.accumulate): the offset readings change in small steps every 5 minutes,
       and a downward step would make a later message look EARLIER than the one before it. For a book that
       is replayed message by message that would be harmful, so local is forced to be non-decreasing
       within one stream (a stream = depth, or trades). Rows where this changed something are counted.
    `stats` (a dict of counters) and `in_win` (bool mask: rows that belong to the hour, not to the
    lookback margin) are optional: used to count only rows of the hour being written.
    """
    off = inp.offset_for(recv)
    local = recv - off
    clamped = local <= exch
    local = np.where(clamped, exch + CLAMP_NS, local)
    mono = np.maximum.accumulate(local) if len(local) else local
    raised = mono != local
    if stats is not None and in_win is not None:
        stats["clamped"] += int((clamped & in_win).sum())
        stats["monotone_fixed"] += int((raised & in_win).sum())
        if in_win.any():
            stats["off_min"] = min(stats["off_min"], int(off[in_win].min()))
            stats["off_max"] = max(stats["off_max"], int(off[in_win].max()))
    return mono, off


# ----------------------------------------------------------------------------------------------
# Gaps
# ----------------------------------------------------------------------------------------------
def load_gaps(inp: Inputs) -> list[dict]:
    """All gap markers (tiny files), de-duplicated and sorted by time. ts_ns is EXCHANGE time."""
    files = [p for _, p in inp.parts["gaps"]]
    if not files:
        return []
    inp.files_read.update(files)
    df = pl.read_parquet(files).unique(subset=["ts_ns", "reason", "stream"]).sort("ts_ns", "reason", "stream")
    return df.to_dicts()                          # list of {"ts_ns":..., "reason":..., "stream":...}


def resolve_gaps(inp: Inputs, gaps: list[dict], lo: int, hi: int) -> tuple[list[dict], list[dict]]:
    """Turn raw gap markers into (a) sidecar records and (b) book-clear events, for gaps in [lo, hi).

    A *book gap* is: public/depth stream `disconnect` or `resync` (the book we hold is no longer true
    until the next snapshot). `session_start` rows are the snapshot that ENDS a book gap (or starts
    a session); the snapshot itself is turned into events by build_chunk, so no clear is made here.
    A `market` stream disconnect only loses trades: it is listed in gaps.json but does not clear the book.
    """
    records, clears = [], []
    snaps = sorted(g["ts_ns"] for g in gaps if g["reason"] in ("session_start", "resync") and g["stream"] == "depth")

    def depth_local_at(ts: int, before: bool) -> int | None:
        """Local time (exchange clock) of the last depth row before ts (before=True) / first row at-or-after ts."""
        win = (ts - 30 * NS_PER_S, ts) if before else (ts, ts + 30 * NS_PER_S)
        df = inp.read("depth", win[0] - LOOKBACK_S * NS_PER_S if before else win[0], win[1],
                      ["ts_ns", "recv_ts_ns"])
        if df.height == 0:
            return None
        e, r = df["ts_ns"].to_numpy(), df["recv_ts_ns"].to_numpy()
        loc, _ = make_local(inp, e, r, None, None)
        return int(loc[-1] if before else loc[0])

    for g in gaps:
        ts, reason, stream = g["ts_ns"], g["reason"], g["stream"]
        if not (lo <= ts < hi):
            continue
        rec = {"reason": reason, "stream": stream, "start_exch_ns": ts, "start_local_ns": None,
               "end_exch_ns": None, "end_local_ns": None, "clears_book": False}
        if reason == "disconnect" and stream == "public":
            # end of the gap = the first snapshot after the disconnect
            end = next((s for s in snaps if s > ts), None)
            last_local = depth_local_at(ts, before=True)
            # We only notice a disconnect after the fact; the earliest consistent local time is just
            # after the last depth message we actually received (and after the exchange gap time).
            start_local = max(ts, last_local or ts) + CLAMP_NS
            rec.update(start_local_ns=start_local, end_exch_ns=end, clears_book=True)
            if end is not None:
                rec["end_local_ns"] = depth_local_at(end, before=False)
            clears.append((ts, start_local))
        elif reason == "disconnect":                       # market stream: trades only, no book clear
            nxt = inp.read("trades", ts, ts + 3600 * NS_PER_S, ["ts_ns", "recv_ts_ns"]).head(1)
            rec["start_local_ns"] = ts + CLAMP_NS
            if nxt.height:
                e, r = nxt["ts_ns"].to_numpy(), nxt["recv_ts_ns"].to_numpy()
                rec["end_exch_ns"] = int(e[0])
                rec["end_local_ns"] = int(make_local(inp, e, r, None, None)[0][0])
        else:                                              # session_start / resync: marks a snapshot
            rec["start_local_ns"] = depth_local_at(ts, before=False)
            rec["end_exch_ns"], rec["end_local_ns"] = ts, rec["start_local_ns"]
        rec["duration_s"] = None if rec["end_exch_ns"] is None else (rec["end_exch_ns"] - ts) / 1e9
        records.append(rec)
    return records, clears


# ----------------------------------------------------------------------------------------------
# Building one chunk of events
# ----------------------------------------------------------------------------------------------
def new_stats() -> dict:
    """Counters that accumulate across the hours of one run (they end up in manifest.json)."""
    return {
        "depth_rows": 0, "depth_messages": 0, "trade_rows": 0, "snapshots": 0, "gap_clears": 0,
        "clamped_depth": 0, "clamped_trades": 0, "monotone_fixed_depth": 0, "monotone_fixed_trades": 0,
        "dup_trade_ids": 0, "depth_chain_breaks": 0, "depth_ts_backwards": 0, "depth_recv_backwards": 0,
        "off_min": 1 << 62, "off_max": -(1 << 62),
        "hist_depth": np.zeros(HIST_CAP_MS + 1, dtype=np.int64),    # message latency histogram (1 ms bins)
        "hist_trades": np.zeros(HIST_CAP_MS + 1, dtype=np.int64),
        "lat_max_depth_ns": 0, "lat_max_trades_ns": 0,
    }


def build_chunk(inp: Inputs, lo: int, hi: int, read_lo: int, gap_clears: list[tuple[int, int]],
                with_trades: bool, st: dict, count: bool) -> np.ndarray:
    """Events for the window [lo, hi): EXCH copies with exch_ts in the window, LOCAL copies with local_ts in it.

    read_lo <= lo is how far back we read so that earlier events whose LOCAL copy lands inside the window
    are found. Returns the finished, correctly ordered structured array (not yet saved).
    `count` = update the quality counters (True for real output hours, False for the snapshot prefix).
    """
    # ---- 1. read the raw rows (polars DataFrames; columns become numpy arrays with .to_numpy()) ----
    d = inp.read("depth", read_lo, hi, ["ts_ns", "recv_ts_ns", "side", "price", "qty", "is_snapshot",
                                         "final_update_id", "prev_final_update_id"])
    t = (inp.read("trades", read_lo, hi, ["ts_ns", "recv_ts_ns", "trade_id", "price", "qty", "side"])
         if with_trades else None)

    parts_ev, parts_ex, parts_lo, parts_px, parts_q, parts_rank, parts_seq = [], [], [], [], [], [], []

    def add(ev, ex, loc, px, q, rank, seq):
        """Collect one block of event columns (all numpy arrays of equal length)."""
        parts_ev.append(ev); parts_ex.append(ex); parts_lo.append(loc); parts_px.append(px)
        parts_q.append(q); parts_rank.append(rank); parts_seq.append(seq)

    # ---- 2. depth rows ----
    if d.height:
        ex = d["ts_ns"].to_numpy()
        rv = d["recv_ts_ns"].to_numpy()
        in_win = (ex >= lo) & (ex < hi)                    # rows of the hour itself (not the lookback margin)
        loc, _ = make_local(inp, ex, rv, _Sub(st, "depth"), in_win if count else None)
        is_bid = (d["side"] == "bid").to_numpy()
        snap = d["is_snapshot"].to_numpy()
        px = d["price"].to_numpy()
        qty = d["qty"].to_numpy()
        n = len(ex)
        i = np.arange(n, dtype=np.int64)                   # original row number = tie-break "seq"
        # Event code: DIFF rows -> DEPTH_EVENT, snapshot rows -> DEPTH_SNAPSHOT_EVENT; OR the side flag in.
        kind = np.where(snap, DEPTH_SNAPSHOT_EVENT, DEPTH_EVENT).astype(np.uint64)
        side_flag = np.where(is_bid, BUY_EVENT, SELL_EVENT).astype(np.uint64)
        add(kind | side_flag, ex, loc, px, qty, np.full(n, RANK_DEPTH), 2 * i)

        # Snapshot messages: rows with is_snapshot sharing a final_update_id (= lastUpdateId).
        if snap.any():
            fid = d["final_update_id"].to_numpy()
            s_idx = np.flatnonzero(snap)
            # first row index of each snapshot message
            msg_first = s_idx[np.r_[True, fid[s_idx][1:] != fid[s_idx][:-1]]]
            for f in msg_first:
                members = s_idx[fid[s_idx] == fid[f]]
                # (a) FULL clear of both sides at the message time: a snapshot means the old book is void.
                #     (extra safety on top of the per-side clear below: removes stale levels FARTHER than
                #     the snapshot's 1000 levels, which the per-side clear would leave behind.)
                add(np.array([DEPTH_CLEAR_EVENT | BUY_EVENT, DEPTH_CLEAR_EVENT | SELL_EVENT], dtype=np.uint64),
                    np.full(2, ex[f]), np.full(2, loc[f]), np.array([CLEAR_ALL_BID_PX, CLEAR_ALL_ASK_PX]),
                    np.zeros(2), np.full(2, RANK_CLEAR), np.array([2 * f - 3, 2 * f - 2]))
                # (b) hftbacktest's own convention: per side, clear up to the snapshot's FARTHEST price
                #     (lowest bid / highest ask), placed right before that side's first snapshot row.
                for bid_side in (True, False):
                    m = members[is_bid[members] == bid_side]
                    if len(m) == 0:
                        continue
                    far = px[m].min() if bid_side else px[m].max()
                    add(np.array([DEPTH_CLEAR_EVENT | (BUY_EVENT if bid_side else SELL_EVENT)], dtype=np.uint64),
                        ex[m[:1]], loc[m[:1]], np.array([far]), np.zeros(1), np.full(1, RANK_DEPTH),
                        np.array([2 * m[0] - 1]))
                if count and lo <= ex[f] < hi:
                    st["snapshots"] += 1

        if count:
            # --- quality counters over the hour's own rows ---
            fid = d["final_update_id"].to_numpy()
            new_msg = np.r_[True, (fid[1:] != fid[:-1]) | (snap[1:] != snap[:-1])]   # first row of each message
            lat = loc - ex
            sel = new_msg & in_win
            st["depth_rows"] += int(in_win.sum())
            st["depth_messages"] += int(sel.sum())
            if sel.any():
                st["hist_depth"] += np.bincount(np.minimum(lat[sel] // NS_PER_MS, HIST_CAP_MS),
                                                minlength=HIST_CAP_MS + 1)
                st["lat_max_depth_ns"] = max(st["lat_max_depth_ns"], int(lat[sel].max()))
            st["depth_ts_backwards"] += int((np.diff(ex)[in_win[1:]] < 0).sum())
            st["depth_recv_backwards"] += int((np.diff(rv)[in_win[1:]] < 0).sum())
            # update-id chain: a diff message's `pu` must equal the previous message's final id,
            # except right after a snapshot (it starts a new chain).
            mi = np.flatnonzero(new_msg)
            pu = d["prev_final_update_id"].to_numpy()[mi]
            prev_u = np.r_[0, fid[mi][:-1]]
            ok_start = ~snap[mi] & ~np.r_[True, snap[mi][:-1]] & in_win[mi]
            ok_start[0] = False                             # first message: no predecessor in this read
            st["depth_chain_breaks"] += int((ok_start & (pu != prev_u)).sum())

    # ---- 3. trades ----
    if t is not None and t.height:
        ex = t["ts_ns"].to_numpy()
        rv = t["recv_ts_ns"].to_numpy()
        in_win = (ex >= lo) & (ex < hi)
        loc, _ = make_local(inp, ex, rv, _Sub(st, "trades"), in_win if count else None)
        # TRADE_EVENT | BUY_EVENT when the aggressor BOUGHT (lifted the ask), SELL_EVENT when it sold.
        flag = np.where((t["side"] == "buy").to_numpy(), BUY_EVENT, SELL_EVENT).astype(np.uint64)
        n = len(ex)
        add(np.uint64(TRADE_EVENT) | flag, ex, loc, t["price"].to_numpy(), t["qty"].to_numpy(),
            np.full(n, RANK_TRADE), 2 * np.arange(n, dtype=np.int64))
        if count:
            st["trade_rows"] += int(in_win.sum())
            lat = loc - ex
            if in_win.any():
                st["hist_trades"] += np.bincount(np.minimum(lat[in_win] // NS_PER_MS, HIST_CAP_MS),
                                                 minlength=HIST_CAP_MS + 1)
                st["lat_max_trades_ns"] = max(st["lat_max_trades_ns"], int(lat[in_win].max()))
            ids = t["trade_id"].to_numpy()
            st["dup_trade_ids"] += int((np.diff(ids)[in_win[1:]] == 0).sum())

    # ---- 4. whole-book clears for book gaps (public stream disconnect/resync) ----
    for k, (g_ex, g_loc) in enumerate(gap_clears):
        if read_lo <= g_ex < hi:
            add(np.array([DEPTH_CLEAR_EVENT | BUY_EVENT, DEPTH_CLEAR_EVENT | SELL_EVENT], dtype=np.uint64),
                np.full(2, g_ex), np.full(2, g_loc), np.array([CLEAR_ALL_BID_PX, CLEAR_ALL_ASK_PX]),
                np.zeros(2), np.full(2, RANK_CLEAR), np.array([-(10 ** 9) + 2 * k, -(10 ** 9) + 2 * k + 1]))
            if count and lo <= g_ex < hi:
                st["gap_clears"] += 1

    if not parts_ev:
        return np.zeros(0, dtype=event_dtype)

    # ---- 5. assemble one structured array ----
    cat = np.concatenate
    n = sum(len(a) for a in parts_ev)
    arr = np.zeros(n, dtype=event_dtype)                    # all fields start at 0 (order_id, ival, fval stay 0)
    arr["ev"] = cat(parts_ev)
    arr["exch_ts"] = cat(parts_ex)
    arr["local_ts"] = cat(parts_lo)
    arr["px"] = cat(parts_px)
    arr["qty"] = cat(parts_q)
    rank, seq = cat(parts_rank), cat(parts_seq)

    # ---- 6. two stable sort orders ----
    # np.lexsort sorts by the LAST key first, then the previous ones to break ties, and is stable:
    # primary key = timestamp, then rank (clears < trades < depth), then the original row order.
    # It returns *indices* (argsort style), not sorted values.
    by_exch = np.lexsort((seq, rank, arr["exch_ts"]))
    by_local = np.lexsort((seq, rank, arr["local_ts"]))
    out = correct_event_order(arr, by_exch, by_local)       # splits/merges rows into EXCH / LOCAL copies

    # ---- 7. keep only the copies that belong to this window ----
    has_e = (out["ev"] & EX) != 0
    has_l = (out["ev"] & LO) != 0
    in_e = (out["exch_ts"] >= lo) & (out["exch_ts"] < hi)
    in_l = (out["local_ts"] >= lo) & (out["local_ts"] < hi)
    # `& ~flag` switches a flag off. (~ is bitwise NOT.) A row flagged EXCH|LOCAL whose local time is in
    # the next hour becomes an EXCH-only row here; its LOCAL copy is written by the next hour's file.
    out["ev"][has_e & ~in_e] &= ~EX
    out["ev"][has_l & ~in_l] &= ~LO
    out = out[(out["ev"] & (EX | LO)) != 0]
    return out


def alt_hist(inp: Inputs, recv: np.ndarray, exch: np.ndarray) -> np.ndarray:
    """Latency histogram (1 ms bins) of the same rows under the OTHER offset mode (for the manifest only).
    No clamping/repair here: negative latencies are floored at 0 to fit the histogram."""
    other = "measured" if inp.mode == "robust" else "robust"
    lat = recv - inp.offset_for(recv, other) - exch
    return np.bincount(np.clip(lat // NS_PER_MS, 0, HIST_CAP_MS), minlength=HIST_CAP_MS + 1)


class _Sub:
    """Tiny adapter so make_local can count into 'clamped_depth' / 'clamped_trades' style keys."""

    def __init__(self, st: dict, name: str):
        self.st, self.name = st, name

    def __getitem__(self, key: str):
        return self.st[self._k(key)]

    def __setitem__(self, key: str, value) -> None:
        self.st[self._k(key)] = value

    def _k(self, key: str) -> str:
        # off_min/off_max are shared; clamped/monotone_fixed are per stream.
        return key if key.startswith("off_") else f"{key}_{self.name}"


# ----------------------------------------------------------------------------------------------
# Main program
# ----------------------------------------------------------------------------------------------
def percentiles_ms(hist: np.ndarray, qs=(50, 90, 99)) -> dict:
    """Percentiles from a 1 ms-bin histogram (value = lower edge of the bin, so +-1 ms)."""
    total = int(hist.sum())
    if total == 0:
        return {f"p{q}": None for q in qs}
    cum = np.cumsum(hist)
    return {f"p{q}": int(np.searchsorted(cum, np.ceil(total * q / 100.0))) for q in qs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Convert live recordings to hftbacktest event files.")
    ap.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--start", default="00:00", help="window start HH:00 (whole hours)")
    ap.add_argument("--end", default="24:00", help="window end HH:00 (exclusive; 24:00 = midnight)")
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--tick", type=float, default=0.01)
    ap.add_argument("--lot", type=float, default=0.001)
    ap.add_argument("--out", default=str(PROJECT / "data" / "hft"))
    ap.add_argument("--offset-mode", default="robust", choices=["robust", "measured"],
                    help="how recv time is mapped to the exchange clock: robust (default; see Inputs._build_robust) "
                         "or measured (plain as-of join of the REST clock_offset_ms readings)")
    args = ap.parse_args(argv)

    t_run = time.time()
    W0, W1 = utc_ns(args.date, args.start), utc_ns(args.date, args.end)
    if W0 % NS_PER_H or W1 % NS_PER_H or W1 <= W0:
        sys.exit("--start/--end must be whole hours with start < end")
    out_dir = Path(args.out) / args.symbol / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    inp = Inputs(args.symbol, mode=args.offset_mode, horizon_ns=W1)
    print("clock offsets:", {k: v for k, v in inp.offset_info.items() if k != "jumps"})
    gaps_all = load_gaps(inp)
    st = new_stats()

    # ---- gaps overlapping the whole needed range (prefix included, so look from 2 days before) ----
    records, clears = resolve_gaps(inp, gaps_all, W0 - 48 * NS_PER_H, W1)
    # keep gaps that overlap the window (started before it but still open at W0 count too)
    window_records = [r for r in records if r["start_exch_ns"] < W1
                      and (r["end_exch_ns"] is None or r["end_exch_ns"] >= W0)]

    # ---- initial snapshot: replay depth from the last snapshot before W0, up to W0 ----
    first_hh = args.start.split(":")[0]
    snap_path = out_dir / f"{args.symbol}_{args.date}_{first_hh}_snapshot.npz"
    snap_ts = None
    for s_start, p in reversed([x for x in inp.parts["depth"] if x[0] < W0]):
        df = pl.read_parquet(p, columns=["ts_ns", "is_snapshot"]).filter(
            pl.col("is_snapshot") & (pl.col("ts_ns") <= W0))
        if df.height:
            snap_ts = int(df["ts_ns"].max())
            break
    prefix_info = {}
    if snap_ts is None:
        print("no snapshot before the window start: the first snapshot inside the window seeds the book")
    else:
        tmp = out_dir / "_prefix_tmp"                       # temporary hourly files, deleted afterwards
        tmp.mkdir(exist_ok=True)
        edges = [snap_ts]
        e = (snap_ts // NS_PER_H + 1) * NS_PER_H
        while e < W0:
            edges.append(e)
            e += NS_PER_H
        edges.append(W0)
        files = []
        n_pre = 0
        for a, b in zip(edges[:-1], edges[1:]):             # zip pairs consecutive edges into (a, b) intervals
            # first prefix chunk starts exactly at the snapshot, so nothing of the previous session is read
            r_lo = a if a == snap_ts else a - LOOKBACK_S * NS_PER_S
            ev = build_chunk(inp, a, b, r_lo, [c for c in clears], False, st, False)
            f = tmp / f"pre_{len(files):03d}.npz"
            np.savez_compressed(f, data=ev)
            n_pre += len(ev)
            files.append(str(f))
        snap = create_last_snapshot(files, args.tick, args.lot, output_snapshot_filename=str(snap_path))
        shutil.rmtree(tmp)
        prefix_info = {"snapshot_ts": iso(snap_ts), "prefix_events": n_pre, "snapshot_rows": int(len(snap)),
                       "snapshot_file": snap_path.name}
        print(f"initial snapshot from {iso(snap_ts)} replayed to {iso(W0)}: {len(snap)} rows -> {snap_path.name}")

    # ---- the hourly files ----
    outputs = []
    prev_last_e = prev_last_l = None
    for hs in range(W0, W1, NS_PER_H):
        t0 = time.time()
        he = hs + NS_PER_H
        hh = dt.datetime.fromtimestamp(hs / 1e9, dt.timezone.utc)
        path = out_dir / f"{args.symbol}_{hh.strftime('%Y-%m-%d_%H')}.npz"
        ev = build_chunk(inp, hs, he, hs - LOOKBACK_S * NS_PER_S, clears, True, st, True)
        # Both order rules must hold inside the file ...
        validate_event_order(ev)
        e_ts = ev["exch_ts"][(ev["ev"] & EX) != 0]
        l_ts = ev["local_ts"][(ev["ev"] & LO) != 0]
        # ... and continue across files: this file must not start before the previous one ended.
        if prev_last_e is not None and len(e_ts) and e_ts[0] < prev_last_e:
            raise ValueError(f"exchange events go backwards across files at {path.name}")
        if prev_last_l is not None and len(l_ts) and l_ts[0] < prev_last_l:
            raise ValueError(f"local events go backwards across files at {path.name}")
        if len(e_ts):
            prev_last_e = int(e_ts[-1])
        if len(l_ts):
            prev_last_l = int(l_ts[-1])
        # the lookback margin must have been large enough: no event with latency near it
        if st["lat_max_depth_ns"] > (LOOKBACK_S - 5) * NS_PER_S:
            raise ValueError("feed latency exceeds the lookback margin; raise LOOKBACK_S")
        np.savez_compressed(path, data=ev)
        outputs.append({"file": path.name, "rows": int(len(ev)), "exch_rows": int(len(e_ts)),
                        "local_rows": int(len(l_ts)), "bytes": path.stat().st_size,
                        "first_exch": iso(int(e_ts[0])) if len(e_ts) else None,
                        "last_exch": iso(int(e_ts[-1])) if len(e_ts) else None,
                        "seconds": round(time.time() - t0, 1)})
        print(f"{path.name}: {len(ev):,} rows ({path.stat().st_size / 1e6:.0f} MB) in {time.time() - t0:.0f}s", flush=True)
        del ev

    # ---- sidecars ----
    (out_dir / "gaps.json").write_text(json.dumps(
        {"note": "start/end are EXCHANGE-clock ns; *_local_ns are our receive time on the exchange clock. "
                 "clears_book=true: the order book is invalid from start until the snapshot at end; pause quoting.",
         "gaps": window_records}, indent=1), encoding="utf-8")

    in_files = sorted(inp.files_read | {inp.status_path})
    manifest = {
        "symbol": args.symbol, "date": args.date, "window": [iso(W0), iso(W1)],
        "tick_size": args.tick, "lot_size": args.lot,
        "initial_snapshot": prefix_info or None,
        "outputs": outputs,
        "counts": {k: int(v) for k, v in st.items() if isinstance(v, (int, np.integer)) and not k.startswith("off_")
                   and not k.startswith("lat_max")},
        "clock_offset_readings": inp.offset_info,
        "clock_offset_ms_range_used": [st["off_min"] / 1e6, st["off_max"] / 1e6],
        "feed_latency_ms": {
            "definition": "local_ts - exch_ts after clamp/monotone repair; depth per MESSAGE, trades per row; 1 ms bins",
            "depth": {**percentiles_ms(st["hist_depth"]), "max": st["lat_max_depth_ns"] / 1e6},
            "trades": {**percentiles_ms(st["hist_trades"]), "max": st["lat_max_trades_ns"] / 1e6},
        },
        "clamp_rule": "rows with local_ts <= exch_ts moved to exch_ts + 1 ms",
        "gaps_in_window": len(window_records),
        "inputs": [{"path": str(p.relative_to(PROJECT)).replace("\\", "/"),
                    "rows": (None if p.suffix != ".parquet" else pl.scan_parquet(p).select(pl.len()).collect().item()),
                    "sha256": sha256_of(p)} for p in in_files],
        "elapsed_s": round(time.time() - t_run, 1),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"done in {time.time() - t_run:.0f}s; manifest.json, gaps.json written to {out_dir}")
    return 0


# Run main() only when this file is executed directly (not when imported by another script).
if __name__ == "__main__":
    raise SystemExit(main())
