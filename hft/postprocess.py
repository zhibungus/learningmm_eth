"""Turn the raw logs of a hftbacktest run into the comparison metrics (plain numpy, no numba).

Definitions match src/mm/backtest/README.md "Metrics glossary" as closely as hftbacktest's data allows:

* spread capture  = sum over fills of  side * (mid_before_fill - fill_px) * qty      (side +1 buy, -1 sell)
* net PnL         = final balance + position * final_mid - fees       (hftbacktest's balance excludes fees)
* gross PnL       = net PnL + fees
* inventory PnL   = gross PnL - spread capture                         (so the three always add up exactly)
* markout(h)      = side * (mid at fill_time + h - fill_px) per ETH, and in USDT = x qty
* fill rate       = orders that received any fill / new orders submitted.  (A requote by modify keeps the
                    same order id; a cancel + new counts as a new order.)

Time bases.  ``mid_log`` stores, for every polling step, the mid of OUR local book and the exchange
timestamp of the latest feed row we had received (``feed_exch_ts``).  The local book at local time T
describes the exchange at ~T - feed latency, so indexing the mid series by ``feed_exch_ts`` gives "the mid
as the exchange had it at time t".  Fill times are exchange times (order.exch_timestamp), so the lookup
is consistent: mid_before_fill = last mid with feed_exch_ts < fill exch_ts.
"""
from __future__ import annotations

import numpy as np

NS_PER_S = 1_000_000_000
MARKOUT_HORIZONS_S = (1, 10, 60)
SHARPE_BUCKET_S = 60                    # same default bucket as our engine's Sharpe
SECONDS_PER_YEAR = 365 * 24 * 3600      # annualisation base (perp trades 24/7)


def _valid_mid_series(mid_log: np.ndarray):
    """(feed_exch_ts, mid) with NaN-mid rows removed and time made non-decreasing."""
    ok = np.isfinite(mid_log["mid"]) & (mid_log["feed_exch_ts"] > 0)
    t = np.maximum.accumulate(mid_log["feed_exch_ts"][ok])
    return t, mid_log["mid"][ok]


def mid_at(t_series: np.ndarray, mid_series: np.ndarray, t: np.ndarray, *, strictly_before: bool) -> np.ndarray:
    """Last mid with time < t (or <= t).  NaN where no such row exists or t is past the end of the series."""
    side = "left" if strictly_before else "right"
    idx = np.searchsorted(t_series, t, side=side) - 1       # position of the last row before t
    out = np.full(len(t), np.nan)
    good = (idx >= 0) & (t <= t_series[-1] if len(t_series) else False)
    out[good] = mid_series[idx[good]]
    return out


def compute_metrics(res: dict, params: dict, maker_fee: float, inventory_limit: float, lot: float) -> dict:
    """All comparison numbers for one GLFT run.  ``res`` is the dict returned by glft_hft.run_glft."""
    mid_log, fills, stats = res["mid_log"], res["fill_log"], res["stats"]
    t_ser, m_ser = _valid_mid_series(mid_log)
    m: dict = {}

    # ---- fills
    n_rows = len(fills)
    side = fills["side"].astype(float)
    px, qty = fills["px"], fills["qty"]
    mid_before = mid_at(t_ser, m_ser, fills["exch_ts"], strictly_before=True)
    spread_capture = float(np.nansum(side * (mid_before - px) * qty))
    m["fill_rows"] = int(n_rows)
    m["filled_qty_eth"] = float(qty.sum())
    m["filled_notional_usdt"] = float((px * qty).sum())
    orders_new = int(stats[0])                       # S_NEW
    m["orders_new"] = orders_new
    m["orders_with_fill"] = int(len(np.unique(fills["order_id"])))
    m["fill_rate_orders"] = m["orders_with_fill"] / orders_new if orders_new else float("nan")
    m["fill_rows_per_new_order"] = n_rows / orders_new if orders_new else float("nan")
    m["cancels"], m["modifies"] = int(stats[1]), int(stats[2])
    m["post_only_rejects"], m["quote_cycles"], m["cycles_without_valid_book"] = int(stats[3]), int(stats[4]), int(stats[5])
    m["submit_errors"] = int(stats[7])
    m["elapse_end_code"] = int(stats[8])           # 1 = reached end of data (normal); other = engine error, run cut short

    # ---- quote distances (target quotes of every allowed cycle), in ticks from the mid
    ql = res.get("quote_log")
    if ql is not None and len(ql):
        tk = params["tick_size"]
        db, da = (ql["mid"] - ql["bid"]) / tk, (ql["ask"] - ql["mid"]) / tk
        m["quote_bid_dist_ticks_min_p50_max"] = [float(db.min()), float(np.median(db)), float(db.max())]
        m["quote_ask_dist_ticks_min_p50_max"] = [float(da.min()), float(np.median(da)), float(da.max())]
        m["quote_cycles_with_dist_below_floor"] = int(((db < params["min_half_spread_ticks"] - 1e-6)
                                                       | (da < params["min_half_spread_ticks"] - 1e-6)).sum())
    if n_rows:
        dist = side * (mid_before - px) / params["tick_size"]    # >0: we bought below / sold above mid
        m["fill_dist_ticks_min_p50_max"] = [float(np.nanmin(dist)), float(np.nanmedian(dist)), float(np.nanmax(dist))]

    # ---- PnL
    if len(mid_log):
        last = mid_log[-1]
        last_mid = m_ser[-1] if len(m_ser) else float("nan")      # last VALID mid (book may be empty at the end)
        fee_state = float(last["fee"])
        net = float(last["balance"] + last["position"] * last_mid - fee_state)
        m["final_position_eth"] = float(last["position"])
        m["final_mid"] = float(last_mid)
    else:
        fee_state, net = 0.0, 0.0
    m["fees_usdt"] = fee_state
    m["fees_check_from_fill_log"] = float((maker_fee * px * qty).sum())   # should equal fees_usdt (all fills are maker)
    m["net_pnl_usdt"] = net
    m["gross_pnl_usdt"] = net + fee_state
    m["spread_capture_usdt"] = spread_capture
    m["inventory_pnl_usdt"] = m["gross_pnl_usdt"] - spread_capture

    # ---- markouts
    for h in MARKOUT_HORIZONS_S:
        mh = mid_at(t_ser, m_ser, fills["exch_ts"] + h * NS_PER_S, strictly_before=False)
        mo = side * (mh - px)                        # USDT per ETH
        ok = np.isfinite(mo)
        m[f"markout_{h}s_per_eth_mean"] = float(np.average(mo[ok], weights=qty[ok])) if ok.any() else float("nan")
        m[f"markout_{h}s_total_usdt"] = float((mo[ok] * qty[ok]).sum())
        m[f"markout_{h}s_n"] = int(ok.sum())
    m["spread_capture_per_eth_mean"] = float(np.nansum(side * (mid_before - px) * qty) / qty.sum()) if n_rows else float("nan")

    # ---- inventory (time-weighted over polling steps)
    if len(mid_log) > 1:
        pos = mid_log["position"]
        dt = np.diff(mid_log["local_ts"]).astype(float)
        w = dt / dt.sum()
        p_ = pos[:-1]
        mean = float((p_ * w).sum())
        m["inventory_mean_eth"] = mean
        m["inventory_std_eth"] = float(np.sqrt(((p_ - mean) ** 2 * w).sum()))
        m["inventory_abs_max_eth"] = float(np.abs(pos).max())
        m["time_at_limit_frac"] = float(w[np.abs(p_) >= inventory_limit - lot / 2].sum())
        m["max_live_orders_bid"] = int(mid_log["n_live_bid"].max())
        m["max_live_orders_ask"] = int(mid_log["n_live_ask"].max())
        m["steps_with_more_than_one_live_order_on_a_side"] = int(((mid_log["n_live_bid"] > 1) | (mid_log["n_live_ask"] > 1)).sum())
        m["duration_s"] = float((mid_log["local_ts"][-1] - mid_log["local_ts"][0]) / NS_PER_S)
        # ---- equity curve, drawdown, Sharpe
        mid_ff = _ffill(mid_log["mid"])
        equity = mid_log["balance"] + pos * mid_ff - mid_log["fee"]
        t_s = (mid_log["local_ts"] - mid_log["local_ts"][0]) / NS_PER_S
        m["max_drawdown_usdt"] = float((np.maximum.accumulate(equity) - equity).max())
        bucket = (t_s // SHARPE_BUCKET_S).astype(int)
        last_idx = np.r_[np.nonzero(np.diff(bucket))[0], len(bucket) - 1]
        eq_b = equity[last_idx]
        inc = np.diff(eq_b)
        m["sharpe_annualised_60s"] = (float(inc.mean() / inc.std() * np.sqrt(SECONDS_PER_YEAR / SHARPE_BUCKET_S))
                                      if len(inc) > 2 and inc.std() > 0 else float("nan"))
        res["_equity"], res["_t_s"], res["_mid_ff"] = equity, t_s, mid_ff
    # ---- consistency with hftbacktest's own accounting
    rec = res.get("recorder")
    if rec is not None and len(rec):
        m["state_num_trades"] = int(rec["num_trades"][-1])
        m["state_trading_volume_eth"] = float(rec["trading_volume"][-1])
        m["fill_log_matches_state_volume"] = bool(abs(m["state_trading_volume_eth"] - m["filled_qty_eth"]) < 1e-9 + 1e-6 * m["filled_qty_eth"])
    return m


def _ffill(x: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs (an empty book keeps the last known mid for marking inventory)."""
    out = x.copy()
    idx = np.where(np.isfinite(out), np.arange(len(out)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = out[idx]
    first = np.nonzero(np.isfinite(x))[0]
    if len(first):
        out[: first[0]] = x[first[0]]
    return out


def basic_from_recorder(rec: np.ndarray) -> dict:
    """Minimal metrics for a Recorder-only run (the example strategy): no fill log, no markouts."""
    if len(rec) == 0:
        return {}
    mid = _ffill(rec["price"])
    equity = rec["balance"] + rec["position"] * mid - rec["fee"]
    return {"net_pnl_usdt": float(equity[-1]), "fees_usdt": float(rec["fee"][-1]),
            "num_trades": int(rec["num_trades"][-1]), "trading_volume_eth": float(rec["trading_volume"][-1]),
            "final_position_eth": float(rec["position"][-1]), "abs_position_max_eth": float(np.abs(rec["position"]).max()),
            "max_drawdown_usdt": float((np.maximum.accumulate(equity) - equity).max()), "records": int(len(rec)),
            "_equity": equity, "_t_s": (rec["timestamp"] - rec["timestamp"][0]) / NS_PER_S, "_pos": rec["position"]}
