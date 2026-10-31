"""Measure real feed/order latency from live recordings and suggest backtest latency settings.

Why this exists: a backtest must delay what the strategy sees (feed latency) and when its orders
reach the exchange (order latency). Guessing these is dangerous; too-small values make a market
maker look better than it can be. This tool measures them for THIS computer and network.

Three clocks are involved (see ``schemas.py``):

    ts_ns        exchange matching-engine time ("T")
    event_ts_ns  exchange message-send time ("E")        E - T  = exchange-side publishing delay
    recv_ts_ns   our PC clock when the frame arrived     recv - E = network delay + clock error

The PC clock is NOT the exchange clock; it was seconds off in our recordings and it DRIFTS. The
recorder therefore measures ``clock_offset_ms`` (PC minus exchange, from a REST /fapi/v1/time round
trip) at the start of every session and every ``live.clock_check_interval_s`` seconds, and logs it
to ``status.jsonl``. This tool matches each recorded frame to the nearest offset measurement in
time, rather than assuming one constant offset:

    one_way_feed_delay = (recv_ts_ns - offset) - event_ts_ns      (E -> arrival at our PC)
    total_feed_delay   = (recv_ts_ns - offset) - ts_ns            (T -> arrival; what a backtest
                                                                   adds to ts_ns)

Caveat: the offset estimate assumes the request and response legs of the REST call take equal
time, so every delay here has an uncertainty of about +-(REST round trip / 4). Read the numbers as
"right to within a few tens of ms", not exact.

Optionally (--rtt N) it times N public ``GET /fapi/v1/time`` calls (no API key, no orders) to
estimate order round-trip time. A real order also spends time in Binance's gateway and matching
engine, which cannot be measured without an account, so a configurable extra
(``latency.order_gateway_extra_ms`` in config/data.yaml) is added to the order estimate.

Run::

    python -m mm.data.latency --start 2026-10-19 --end 2026-10-19
    python -m mm.data.latency --start 2026-10-19 --end 2026-10-19 --rtt 10
"""
# Postpones type-hint evaluation (see config.py).
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import polars as pl
import requests

from . import loaders as L
from .config import load_config, raw_dir

NS_PER_MS = 1_000_000
PERCENTILES = (0.5, 0.9, 0.99)  # p50, p90, p99


# ---------------------------------------------------------------- clock offsets
def load_clock_offsets(cfg: dict, symbol: str | None = None) -> pl.DataFrame:
    """Read every ``clock_offset_ms`` measurement from the recorder's status.jsonl.

    Returns a table with columns ``local_ts_ns`` (PC clock when it was measured) and
    ``offset_ms`` (PC minus exchange, positive = PC clock ahead), sorted by time. Empty if the
    file does not exist or has no measurements.
    """
    symbol = symbol or cfg["symbol"]
    path = raw_dir(cfg, live=True) / symbol / "status.jsonl"
    rows: list[tuple[int, float]] = []
    if path.exists():
        # Each line is one JSON object, e.g. {"ts_ns": ..., "status": "clock_offset_ms", "detail": "5145.7"}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("status") == "clock_offset_ms":
                    rows.append((int(rec["ts_ns"]), float(rec["detail"])))
    return pl.DataFrame(rows, schema={"local_ts_ns": pl.Int64, "offset_ms": pl.Float64},
                        orient="row").sort("local_ts_ns")


# ---------------------------------------------------------------- statistics (pure functions)
def percentiles(values: pl.Series) -> dict[str, float | int | None]:
    """p50/p90/p99, min, max and count of a numeric series (None values if it is empty)."""
    values = values.drop_nulls()
    if values.is_empty():
        return {"n": 0, "p50": None, "p90": None, "p99": None, "min": None, "max": None}
    # Series.quantile(q) returns the q-th quantile (0.5 = median); "linear" interpolates between
    # neighbouring values instead of snapping to one of them.
    q = lambda x: values.quantile(x, interpolation="linear")
    return {"n": values.len(), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99),
            "min": values.min(), "max": values.max()}


def add_delays(df: pl.DataFrame, offsets: pl.DataFrame) -> pl.DataFrame:
    """Add delay columns (ms) to rows that have ts_ns, event_ts_ns, recv_ts_ns.

    Each row is matched to the NEAREST-in-time clock-offset measurement (join_asof), so a drifting
    PC clock is handled per measurement instead of with one constant. With no measurements the
    offset is taken as 0 and ``offset_ms`` is null (the caller should warn).
    """
    df = df.filter(pl.col("recv_ts_ns").is_not_null()).sort("recv_ts_ns")
    if offsets.is_empty():
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("offset_ms"))
        off_ns = pl.lit(0)
    else:
        # join_asof: for every row find the offset measured closest in time to the frame's arrival.
        df = df.join_asof(offsets.rename({"local_ts_ns": "recv_ts_ns"}), on="recv_ts_ns",
                          strategy="nearest")
        off_ns = pl.col("offset_ms") * NS_PER_MS
    exch_recv = pl.col("recv_ts_ns") - off_ns  # arrival time translated to the exchange clock
    return df.with_columns(
        ((exch_recv - pl.col("event_ts_ns")) / NS_PER_MS).alias("one_way_ms"),  # E -> arrival
        ((exch_recv - pl.col("ts_ns")) / NS_PER_MS).alias("total_ms"),          # T -> arrival
        ((pl.col("event_ts_ns") - pl.col("ts_ns")) / NS_PER_MS).alias("e_minus_t_ms"),
    )


def latency_stats(frames: dict[str, pl.DataFrame], offsets: pl.DataFrame) -> dict:
    """Per-stream and pooled distributions. ``frames`` maps stream name -> table with the 3 clocks."""
    out: dict = {"streams": {}, "offset_measurements": offsets.height}
    if not offsets.is_empty():
        out["offset_ms_min_max"] = [offsets["offset_ms"].min(), offsets["offset_ms"].max()]
    pooled = []
    for name, df in frames.items():
        d = add_delays(df, offsets)
        if d.is_empty():
            continue
        pooled.append(d.select("one_way_ms", "total_ms", "e_minus_t_ms"))
        out["streams"][name] = {col: percentiles(d[col]) for col in ("one_way_ms", "total_ms", "e_minus_t_ms")}
    if pooled:
        p = pl.concat(pooled)
        out["pooled"] = {col: percentiles(p[col]) for col in ("one_way_ms", "total_ms", "e_minus_t_ms")}
    return out


# ---------------------------------------------------------------- order round trip
def measure_rtt(cfg: dict, n: int) -> list[float]:
    """Time ``n`` public GET /fapi/v1/time round trips in ms (connection reused, first call dropped).

    Reusing one session means TCP/TLS setup is paid once, like a long-lived order connection.
    """
    r = cfg["rest"]
    url = r["base_url"] + r["time_path"]
    out: list[float] = []
    with requests.Session() as sess:  # context manager closes the connection afterwards
        sess.get(url, timeout=r["timeout_s"])  # warm-up: pays connection setup, not recorded
        for _ in range(n):
            t0 = time.perf_counter()
            sess.get(url, timeout=r["timeout_s"]).raise_for_status()
            out.append((time.perf_counter() - t0) * 1000.0)
    return out


def suggest(stats: dict, rtt_ms: list[float] | None, gateway_extra_ms: float) -> dict[str, float]:
    """Turn measurements into values for the backtest ``latency:`` config block.

    feed_latency_ms     median T->arrival delay (the backtest adds latency to the match time T)
    feed_jitter_ms      p90 - p50 of that delay (the backtest adds U(0, jitter))
    report_latency_ms   fills/acks come back over a similar network path, so = feed latency
    order_latency_ms    half the REST round trip (one-way) + gateway extra; needs --rtt
    order_jitter_ms     half of (p90 - p50) of the round trip
    """
    pooled = stats.get("pooled", {}).get("total_ms")
    sug: dict[str, float] = {}
    if pooled and pooled["p50"] is not None:
        sug["feed_latency_ms"] = round(pooled["p50"])
        sug["feed_jitter_ms"] = round(max(pooled["p90"] - pooled["p50"], 0.0))
        sug["report_latency_ms"] = sug["feed_latency_ms"]
    if rtt_ms:
        # Same interpolated percentiles as everywhere else (one outlier among few samples would
        # otherwise become the "p90").
        rs = percentiles(pl.Series(rtt_ms, dtype=pl.Float64))
        p50, p90 = rs["p50"], rs["p90"]
        sug["order_latency_ms"] = round(p50 / 2 + gateway_extra_ms)
        sug["order_jitter_ms"] = round(max(p90 - p50, 0.0) / 2)
    return sug


# ---------------------------------------------------------------- report
def _fmt(d: dict) -> str:
    if not d or d["n"] == 0:
        return "no data"
    return (f"n={d['n']:,}  p50={d['p50']:.1f}  p90={d['p90']:.1f}  p99={d['p99']:.1f}  "
            f"min={d['min']:.1f}  max={d['max']:.1f}")


def format_report(stats: dict, rtt_ms: list[float] | None, sug: dict[str, float]) -> str:
    lines = ["Latency report (milliseconds)", ""]
    n_off = stats["offset_measurements"]
    if n_off == 0:
        lines.append("WARNING: no clock_offset_ms measurements in status.jsonl; offset assumed 0, "
                     "so one-way delays below are meaningless (they include the PC clock error).")
    else:
        lo, hi = stats["offset_ms_min_max"]
        lines.append(f"Clock offset (PC minus exchange): {n_off} measurements, range {lo:.1f} .. {hi:.1f} ms "
                     f"(drift {hi - lo:.1f} ms). Each frame uses its nearest measurement.")
    lines.append("")
    for name, st in stats["streams"].items():
        lines += [f"[{name}]",
                  f"  one-way E->arrival : {_fmt(st['one_way_ms'])}",
                  f"  total   T->arrival : {_fmt(st['total_ms'])}",
                  f"  E - T (exchange)   : {_fmt(st['e_minus_t_ms'])}"]
    if "pooled" in stats:
        pl_ = stats["pooled"]
        lines += ["[all streams pooled]",
                  f"  one-way E->arrival : {_fmt(pl_['one_way_ms'])}",
                  f"  total   T->arrival : {_fmt(pl_['total_ms'])}",
                  f"  E - T (exchange)   : {_fmt(pl_['e_minus_t_ms'])}"]
    if rtt_ms:
        s = sorted(rtt_ms)
        lines += ["", f"REST /time round trip ({len(s)} calls): p50={statistics.median(s):.1f} "
                      f"min={s[0]:.1f} max={s[-1]:.1f}"]
    lines += ["", "Suggested backtest latency config (paste into the `latency:` block):"]
    if sug:
        lines += [f"  {k}: {v}" for k, v in sug.items()]
    else:
        lines.append("  (no data to suggest from)")
    if not rtt_ms:
        lines.append("  (order_latency_ms / order_jitter_ms need --rtt N)")
    lines.append("Note: delays are accurate to roughly +-(REST round trip / 4) because of the symmetric-path "
                 "assumption in the clock offset; use p90/p99 for pessimistic runs.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Measure feed/order latency from live recordings.")
    ap.add_argument("--symbol", default=cfg["symbol"])
    ap.add_argument("--start", required=True, help="first date YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="last date YYYY-MM-DD (UTC), inclusive")
    ap.add_argument("--rtt", type=int, default=0, help="also time N public REST round trips")
    a = ap.parse_args(argv)

    # Load the rows that carry all three clocks. Depth snapshots are skipped: their timestamps
    # are re-aligned and recv time is the REST response, not a websocket frame.
    depth = L.load_depth(a.start, a.end, a.symbol, "live", cfg)
    depth = depth.filter(~pl.col("is_snapshot")).unique(subset=["final_update_id"], keep="first")
    frames = {
        "trades": L.load_trades(a.start, a.end, a.symbol, "live", cfg=cfg),
        "quotes": L.load_quotes(a.start, a.end, a.symbol, "live", cfg),
        "depth": depth,
    }
    offsets = load_clock_offsets(cfg, a.symbol)
    stats = latency_stats(frames, offsets)
    rtt = measure_rtt(cfg, a.rtt) if a.rtt > 0 else None
    sug = suggest(stats, rtt, cfg["latency"]["order_gateway_extra_ms"])
    print(format_report(stats, rtt, sug))
    return 0


# Run main() only when executed as a script, not when imported.
if __name__ == "__main__":
    raise SystemExit(main())
