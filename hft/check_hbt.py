"""Sanity check: load converted files in hftbacktest and compare its book with our recorded bookTicker.

    .venv-hft\\Scripts\\python.exe hft\\check_hbt.py --date 2026-10-24 --start 10:00 --end 11:00

What it does
------------
1. Builds a backtest of the converted hourly files (initial snapshot = the *_snapshot.npz for --start).
2. Steps the clock in 100 ms increments (`hbt.elapse`) and counts, at every step:
     - crossed book (best_bid >= best_ask), and NaN best prices (empty side).
3. At every k-th step it stores hftbacktest's best bid/ask. Afterwards it compares them with the best
   bid/ask from the bookTicker stream that we recorded. bookTicker is NOT fed to hftbacktest; it is only
   the independent reference here.
Differences are expected to be small but non-zero: depth updates are batched per 100 ms while
bookTicker is real time, and the two streams reach us over different latencies.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

# hftbacktest classes; the builder pattern `BacktestAsset().data(...).tick_size(...)` returns the asset
# itself after each call, so calls can be chained on one line.
from hftbacktest import BacktestAsset, ROIVectorMarketDepthBacktest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so `import convert` finds hft/convert.py
from convert import PROJECT, Inputs, make_local, utc_ns, NS_PER_H, NS_PER_MS   # noqa: E402

TICK, LOT = 0.01, 0.001
STEP_NS = 100_000_000          # 100 ms clock steps
ENTRY_LAT_NS = 5_000_000       # constant order latencies: placeholder values, no orders are sent here
RESP_LAT_NS = 5_000_000
ROI_PCT = 0.25                 # region of interest: +-25 % around the median mid price
SAMPLE_EVERY = 10              # compare with bookTicker every 10th step (= every 1 s)
WINDOW_NS = 300 * NS_PER_MS    # "recently seen" window for the lenient comparison


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", default="00:00")
    ap.add_argument("--end", default="24:00")
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--offset-mode", default="robust", choices=["robust", "measured"])
    args = ap.parse_args()
    W0, W1 = utc_ns(args.date, args.start), utc_ns(args.date, args.end)
    d = PROJECT / "data" / "hft" / args.symbol / args.date
    import datetime as dt
    files = [str(d / f"{args.symbol}_{dt.datetime.fromtimestamp(h / 1e9, dt.timezone.utc):%Y-%m-%d_%H}.npz")
             for h in range(W0, W1, NS_PER_H)]
    snap = str(d / f"{args.symbol}_{args.date}_{args.start.split(':')[0]}_snapshot.npz")

    # ---- reference quotes (bookTicker) for the window, converted to local-on-exchange-clock time ----
    # same offset mode and horizon as the conversion run, so quote times are mapped identically
    inp = Inputs(args.symbol, mode=args.offset_mode, horizon_ns=W1)
    # find the quote part files that can overlap the window (names carry the first exchange ts)
    qparts = []
    for p in (inp.proc / "quotes" / args.symbol).glob("*/part-*.parquet"):
        s = p.name.split("-")[1]
        if len(s) == 19 and W0 - 3600 * 10**9 <= int(s) < W1:
            qparts.append(p)
    q = (pl.scan_parquet(sorted(qparts, key=lambda p: p.name.split("-")[1]))
         .filter((pl.col("ts_ns") >= W0 - 60 * 10**9) & (pl.col("ts_ns") < W1))
         .select("ts_ns", "recv_ts_ns", "bid_px", "ask_px").collect())
    q_ex, q_rv = q["ts_ns"].to_numpy(), q["recv_ts_ns"].to_numpy()
    q_loc, _ = make_local(inp, q_ex, q_rv, None, None)
    q_bid, q_ask = q["bid_px"].to_numpy(), q["ask_px"].to_numpy()
    mid = float(np.median((q_bid + q_ask) / 2))
    roi_lb, roi_ub = mid * (1 - ROI_PCT), mid * (1 + ROI_PCT)
    print(f"reference quotes {q.height:,}; median mid {mid:.2f}; ROI [{roi_lb:.0f}, {roi_ub:.0f}]")

    # ---- backtest: asset configuration (chained builder calls) ----
    asset = (BacktestAsset()
             .data(files)
             .initial_snapshot(snap)
             .linear_asset(1.0)                               # linear contract: PnL = size * price change
             .constant_order_latency(ENTRY_LAT_NS, RESP_LAT_NS)
             .risk_adverse_queue_model()
             .no_partial_fill_exchange()
             .trading_value_fee_model(0.0002, 0.0005)         # maker 2 bp, taker 5 bp
             .tick_size(TICK).lot_size(LOT)
             .roi_lb(roi_lb).roi_ub(roi_ub))
    hbt = ROIVectorMarketDepthBacktest([asset])

    n = crossed = nan = 0
    samp_t, samp_b, samp_a = [], [], []
    t0 = time.time()
    # elapse(ns) advances the simulated clock; it returns 0 while data remains and non-zero at the end.
    while hbt.elapse(STEP_NS) == 0:
        depth = hbt.depth(0)
        bb, ba = depth.best_bid, depth.best_ask
        n += 1
        if np.isnan(bb) or np.isnan(ba):
            nan += 1
        elif bb >= ba:
            crossed += 1
        if n % SAMPLE_EVERY == 0:
            samp_t.append(hbt.current_timestamp)
            samp_b.append(bb)
            samp_a.append(ba)
    hbt.close()
    print(f"stepped {n:,} x 100 ms in {time.time() - t0:.0f}s: crossed={crossed} nan_best={nan}")

    # ---- compare with bookTicker, as of the same LOCAL time ----
    st, sb, sa = np.array(samp_t), np.array(samp_b), np.array(samp_a)
    ok = ~np.isnan(sb) & ~np.isnan(sa) & (st >= W0 + 60 * 10**9)
    st, sb, sa = st[ok], sb[ok], sa[ok]
    # as-of: last quote seen at or before st (searchsorted needs q_loc sorted: apply a running max first)
    q_sorted = np.maximum.accumulate(q_loc)
    idx = np.searchsorted(q_sorted, st, side="right") - 1
    db = (sb - q_bid[idx]) / TICK                             # difference in ticks (hbt minus bookTicker)
    da = (sa - q_ask[idx]) / TICK
    # lenient: does hbt's best equal ANY quote the ticker showed in the previous 300 ms?
    lo_idx = np.searchsorted(q_sorted, st - WINDOW_NS, side="right")
    hit_b = hit_a = 0
    for k in range(len(st)):
        a, b = max(lo_idx[k] - 1, 0), idx[k] + 1
        hit_b += bool(np.any(np.isclose(q_bid[a:b], sb[k], atol=TICK / 2)))
        hit_a += bool(np.any(np.isclose(q_ask[a:b], sa[k], atol=TICK / 2)))
    res = {
        "samples": int(len(st)),
        "exact_match_bid": float(np.mean(db == 0)), "exact_match_ask": float(np.mean(da == 0)),
        "within_300ms_window_bid": hit_b / len(st), "within_300ms_window_ask": hit_a / len(st),
        "abs_diff_ticks_bid_pcts_50_90_99_max": np.percentile(np.abs(db), [50, 90, 99, 100]).tolist(),
        "abs_diff_ticks_ask_pcts_50_90_99_max": np.percentile(np.abs(da), [50, 90, 99, 100]).tolist(),
        "mean_signed_diff_ticks_bid": float(db.mean()), "mean_signed_diff_ticks_ask": float(da.mean()),
        "steps": n, "crossed": crossed, "nan_best": nan,
    }
    print(json.dumps(res, indent=1))
    (d / f"check_{args.start.replace(':', '')}_{args.end.replace(':', '')}.json").write_text(
        json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
