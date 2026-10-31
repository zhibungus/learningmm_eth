"""Data quality checks on processed data, and a report builder.

Each ``check_*`` function takes one canonical DataFrame (see ``schemas.py``) and returns a flat dict
of counts and examples (small). ``report`` runs them day by day (to bound memory) and
``to_markdown`` renders the result.

What is checked
    trades   rows, time range, duplicate trade_ids, id gaps (missing ids), timestamps that go
             backwards when ordered by id, silences longer than ``quality.trade_gap_s``,
             non-positive price/qty, estimated tick size, side balance
    quotes   crossed (bid > ask) and locked (bid == ask) quotes, non-positive prices/qty,
             update_id going backwards or repeated, silences longer than ``quality.quote_gap_s``,
             event_ts earlier than ts, estimated tick size
    depth    messages/snapshots, update-id chain breaks (pu != previous u), crossed/locked book
             after each message (by replaying into ``OrderBook``), timestamps going backwards,
             receive-latency percentiles (recv_ts_ns - ts_ns) when available
    funding  duplicates, spacing vs interval, rate range

Run::

    python -m mm.data.quality --start 2026-10-17 --end 2026-10-18 [--source archive|live]
        [--streams trades quotes depth funding] [--out report.md]
"""
# Postpones type-hint evaluation (see config.py).
from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Any

import polars as pl

from .book import OrderBook
from .config import load_config
from .events import DepthEvent
from . import loaders as L

NS = 1_000_000_000  # nanoseconds per second, to convert ns differences to seconds


# Helper: integer ns -> human-readable ISO timestamp for the report.
def _iso(ns: int | None) -> str | None:
    return None if ns is None else dt.datetime.fromtimestamp(ns / NS, dt.timezone.utc).isoformat(timespec="milliseconds")


def _examples(df: pl.DataFrame, n: int) -> list[dict]:
    return df.head(n).to_dicts()


# Tick size = the smallest allowed price step. We estimate it as the smallest positive gap between
# distinct observed prices (rounded to 8 decimals to hide float noise).
def _tick(prices: pl.Series) -> float | None:
    """Smallest positive price step seen (a decent tick-size estimate)."""
    u = prices.unique().sort()
    if u.len() < 2:
        return None
    d = u.diff().drop_nulls().round(8)
    d = d.filter(d > 0)
    return None if d.is_empty() else float(d.min())


# Find silences: sort the timestamps, take the difference between neighbours (Series.diff()),
# and keep the differences above the limit. Long silences can mean an outage in the data feed.
def _gaps(ts: pl.Series, limit_s: float, n: int) -> tuple[int, list[dict]]:
    s = ts.sort()
    gaps = pl.DataFrame({"start": s, "gap_s": s.diff() / NS}).filter(pl.col("gap_s") > limit_s)
    ex = [{"after": _iso(r["start"] - int(r["gap_s"] * NS)), "gap_s": round(r["gap_s"], 3)}
          for r in gaps.head(n).to_dicts()]
    return gaps.height, ex


# ---------------------------------------------------------------- per-dataset checks
def check_trades(df: pl.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    q = (cfg or load_config())["quality"]
    # Checks below mostly use polars column expressions such as `(series > 1).sum()`, which count
    # how many rows break a rule. `Any` in the hint just means "values of mixed types".
    out: dict[str, Any] = {"rows": df.height}
    if df.is_empty():
        return out
    by_id = df.sort("trade_id")
    out["first_ts"], out["last_ts"] = _iso(df["ts_ns"].min()), _iso(df["ts_ns"].max())
    out["duplicate_trade_ids"] = int(df["trade_id"].is_duplicated().sum())
    # Trade ids are consecutive integers. A jump bigger than 1 means missing trades.
    d_id = by_id["trade_id"].diff().drop_nulls()
    out["id_gaps"] = int((d_id > 1).sum())
    out["missing_ids"] = int((d_id.filter(d_id > 1) - 1).sum())
    out["ts_backwards_by_id"] = int((by_id["ts_ns"].diff().drop_nulls() < 0).sum())
    out["nonpositive_price_or_qty"] = int(((df["price"] <= 0) | (df["qty"] <= 0)).sum())
    out["silences_over_limit"], out["silence_examples"] = _gaps(df["ts_ns"], q["trade_gap_s"], q["max_examples"])
    out["largest_silence_s"] = round(float((df.sort("ts_ns")["ts_ns"].diff().max() or 0) / NS), 3)
    out["est_tick_size"] = _tick(df["price"])
    out["buy_share_by_qty"] = round(float(df.filter(pl.col("side") == "buy")["qty"].sum() / df["qty"].sum()), 4)
    out["bad_side_values"] = int((~df["side"].is_in(["buy", "sell"])).sum())
    return out


def check_quotes(df: pl.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    q = (cfg or load_config())["quality"]
    out: dict[str, Any] = {"rows": df.height}
    if df.is_empty():
        return out
    out["first_ts"], out["last_ts"] = _iso(df["ts_ns"].min()), _iso(df["ts_ns"].max())
    # Crossed = best bid above best ask: impossible in a real book, so it flags corrupt data.
    # Locked = bid equals ask (zero spread).
    crossed = df.filter(pl.col("bid_px") > pl.col("ask_px"))
    out["crossed"] = crossed.height
    out["crossed_examples"] = _examples(crossed.select("ts_ns", "bid_px", "ask_px"), q["max_examples"])
    out["locked"] = int((df["bid_px"] == df["ask_px"]).sum())
    out["nonpositive_price_or_qty"] = int(((df["bid_px"] <= 0) | (df["ask_px"] <= 0)
                                           | (df["bid_qty"] <= 0) | (df["ask_qty"] <= 0)).sum())
    uid = df["update_id"]  # df is sorted by ts_ns (stable): update_id should be non-decreasing
    uid_step = uid.diff().drop_nulls()
    out["update_id_backwards"] = int((uid_step < 0).sum())
    out["update_id_repeated"] = int((uid_step == 0).sum())
    out["silences_over_limit"], out["silence_examples"] = _gaps(df["ts_ns"], q["quote_gap_s"], q["max_examples"])
    out["largest_silence_s"] = round(float((df["ts_ns"].diff().max() or 0) / NS), 3)
    if df["event_ts_ns"].null_count() < df.height:
        out["event_before_trans_time"] = int((df["event_ts_ns"] < df["ts_ns"]).sum())
        lag = ((df["event_ts_ns"] - df["ts_ns"]) / 1e6).drop_nulls()
        out["event_minus_trans_ms_p50_p99"] = [float(lag.quantile(0.5)), float(lag.quantile(0.99))]
    out["spread_median"] = float((df["ask_px"] - df["bid_px"]).median())
    out["est_tick_size"] = _tick(df["bid_px"].head(2_000_000))
    return out


def check_depth(df: pl.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    """Replay depth rows into an ``OrderBook`` and verify sequence + book sanity."""
    q = (cfg or load_config())["quality"]
    out: dict[str, Any] = {"rows": df.height}
    if df.is_empty():
        return out
    out["first_ts"], out["last_ts"] = _iso(df["ts_ns"].min()), _iso(df["ts_ns"].max())
    # Rebuild the book message by message (like a backtest would) and test it after each one.
    book = OrderBook()
    msgs = snaps = chain_breaks = crossed = locked = ts_back = 0
    prev_u = None
    after_snap = False
    last_ts = None
    examples: list[dict] = []
    # pu must equal the previous message's u (see book.py). The first diff after a snapshot is
    # exempt because it is allowed to straddle the snapshot point.
    for ev in L._depth_events(df):
        msgs += 1
        if last_ts is not None and ev.ts_ns < last_ts:
            ts_back += 1
        last_ts = ev.ts_ns
        if ev.is_snapshot:
            snaps += 1
            after_snap = True
        else:
            if prev_u is not None and not after_snap and ev.prev_final_update_id != prev_u:
                chain_breaks += 1
                if len(examples) < q["max_examples"]:
                    examples.append({"ts": _iso(ev.ts_ns), "pu": ev.prev_final_update_id, "prev_u": prev_u})
            after_snap = False
        prev_u = ev.final_update_id
        book.apply(ev)
        crossed += book.is_crossed()
        locked += book.is_locked()
    out.update(messages=msgs, snapshots=snaps, update_id_chain_breaks=chain_breaks,
               chain_break_examples=examples, crossed_after_msg=crossed, locked_after_msg=locked,
               ts_backwards=ts_back, final_levels={"bids": len(book.bids), "asks": len(book.asks)})
    out["nonpositive_qty_rows"] = int((df["qty"] < 0).sum())
    if df["recv_ts_ns"].null_count() < df.height:
        lat = ((df["recv_ts_ns"] - df["ts_ns"]) / 1e6).drop_nulls()
        out["recv_minus_exchange_ms_p50_p99"] = [round(float(lat.quantile(0.5)), 1), round(float(lat.quantile(0.99)), 1)]
    return out


def check_funding(df: pl.DataFrame, cfg: dict | None = None) -> dict[str, Any]:
    q = (cfg or load_config())["quality"]
    out: dict[str, Any] = {"rows": df.height}
    if df.is_empty():
        return out
    df = df.sort("ts_ns")
    out["first_ts"], out["last_ts"] = _iso(df["ts_ns"].min()), _iso(df["ts_ns"].max())
    out["duplicates"] = int(df["ts_ns"].is_duplicated().sum())
    gap_h = (df["ts_ns"].diff() / NS / 3600).drop_nulls()
    iv = df["interval_hours"].drop_nulls()
    expected = float(iv.mode()[0]) if iv.len() else 8.0
    out["interval_hours_seen"] = sorted(set(iv.to_list())) if iv.len() else "unknown (REST source)"
    out["spacing_over_limit"] = int((gap_h > expected * q["funding_gap_hours_factor"]).sum())
    # Partial-month flag: if the newest funding row is more than ~1.5 intervals before the end of
    # its month, the file for the current month stops early (the archive only publishes full
    # months, so a REST-filled current month is partial by nature). Don't mistake it for a full month.
    last = dt.datetime.fromtimestamp(df["ts_ns"].max() / NS, dt.timezone.utc)
    month_end = dt.datetime(last.year + (last.month == 12), 1 if last.month == 12 else last.month + 1,
                            1, tzinfo=dt.timezone.utc)
    left_h = (month_end - last).total_seconds() / 3600
    out["partial_final_month"] = left_h > expected * q["funding_gap_hours_factor"]
    out["rate_min_max"] = [float(df["rate"].min()), float(df["rate"].max())]
    out["mark_price_present"] = f"{df.height - df['mark_price'].null_count()}/{df.height}"
    return out


# Dispatch table: stream name -> check function.
_CHECKS = {"trades": check_trades, "quotes": check_quotes, "depth": check_depth, "funding": check_funding}


# ---------------------------------------------------------------- report
def report(start, end, symbol: str | None = None, streams=("trades", "quotes", "depth", "funding"),
           source: str = "archive", trade_dataset: str = "agg_trades", cfg: dict | None = None) -> dict:
    """Run checks per UTC day (funding: whole range) and return ``{stream: {day: result}}``.

    Also lists dates in the range that have NO file for each stream (``missing_days``).
    """
    # Process one UTC day at a time so a month of data never has to sit in memory at once.
    cfg = cfg or load_config()
    s, e = L._to_date(start), L._to_date(end)
    days = [s + dt.timedelta(days=i) for i in range((e - s).days + 1)]
    res: dict[str, Any] = {"symbol": symbol or cfg["symbol"], "source": source,
                           "start": s.isoformat(), "end": e.isoformat(), "streams": {}}
    for st in streams:
        entry: dict[str, Any] = {"days": {}, "missing_days": []}
        if st == "funding":
            df = L._load(st, s, e, symbol, source, trade_dataset, cfg)
            entry["days"][f"{s}..{e}"] = check_funding(df, cfg)
        else:
            for d in days:
                if st == "trades":
                    df = L.load_trades(d, d, symbol, source, trade_dataset, cfg)
                elif st == "quotes":
                    df = L.load_quotes(d, d, symbol, source, cfg)
                elif st == "depth":
                    df = L.load_depth(d, d, symbol, source, cfg)
                else:
                    raise KeyError(st)
                if df.is_empty():
                    entry["missing_days"].append(d.isoformat())
                    continue
                entry["days"][d.isoformat()] = _CHECKS[st](df, cfg)
        res["streams"][st] = entry
    return res


# Turn the nested result dict into readable Markdown (skipping empty values).
def to_markdown(rep: dict) -> str:
    lines = [f"### Quality report: {rep['symbol']} {rep['start']}..{rep['end']} (source={rep['source']})", ""]
    for st, entry in rep["streams"].items():
        lines.append(f"**{st}**" + (f" - no data for: {', '.join(entry['missing_days'])}" if entry["missing_days"] else ""))
        for day, r in entry["days"].items():
            lines.append(f"- `{day}`")
            for k, v in r.items():
                if v in ([], None):
                    continue
                lines.append(f"    - {k}: {v}")
        lines.append("")
    return "\n".join(lines)


# Command-line entry point (argparse parses `--start ... --end ...`).
def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Data quality report on processed data.")
    ap.add_argument("--symbol", default=cfg["symbol"])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--source", default="archive", choices=["archive", "live"])
    ap.add_argument("--streams", nargs="+", default=["trades", "quotes", "depth", "funding"])
    ap.add_argument("--trade-dataset", default="agg_trades", choices=["agg_trades", "trades"])
    ap.add_argument("--out", help="write markdown here as well")
    a = ap.parse_args(argv)
    md = to_markdown(report(a.start, a.end, a.symbol, a.streams, a.source, a.trade_dataset, cfg))
    print(md)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(md)
    return 0


# Run main() only when executed as a script, not when imported.
if __name__ == "__main__":
    sys.exit(main())
