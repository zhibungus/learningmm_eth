"""Accounting and performance metrics.

``Account``   the TRUE books of the simulated account: position, cash, realized/unrealized PnL
              (average-cost method), fees, funding.  Marked to the exchange mid.
``Recorder``  collects time series during a run (equity samples on a fixed time grid, mid prices,
              fills) with small memory use.
``compute_metrics``  turns a finished run into plain numbers + two DataFrames (fills, equity).

The accounting identity (tested in ``tests/test_backtest_accounting.py``)::

    cash + position * mid  ==  starting_cash + realized_pnl + unrealized_pnl - fees - funding

PnL split used in the reports (exact, they add up to gross trading PnL):

    spread capture   per fill: side * (mid_before_fill - fill_price) * qty
                     "how far from the mid did I trade" -- positive for a maker that buys below mid.
    inventory PnL    sum over time of position * (change in mid)
                     "what did holding inventory earn" -- this is where adverse selection shows up.
    gross trading PnL = spread capture + inventory PnL
    net PnL           = gross - fees - funding

Markouts (adverse selection): for each fill and each horizon h (1s, 10s, 60s)
    markout_h  = side * (mid(t + h) - fill_price)    [quote currency per 1 unit, includes the edge]
    move_h     = side * (mid(t + h) - mid(t))        [pure price drift after the fill]
Negative ``move_h`` on average = the market moves against you right after you trade (adverse
selection).  Fills whose t + h is beyond the end of the data are excluded from that horizon.

Sharpe: PnL (in quote currency) is sampled on a fixed time bucket (default 60 s), then
mean / std * sqrt(buckets per year).  Perpetual futures have no natural capital base so returns are
absolute PnL increments, not percentages; compare runs, do not quote it as a fund Sharpe.
"""
# Lazy type hints (see strategy.py).
from __future__ import annotations

import math
# array: a compact typed list. array("d") stores raw 64-bit floats, array("q") 64-bit ints: far less
# memory than a Python list of millions of numbers, and numpy can wrap it without copying.
from array import array

import numpy as np
# polars: fast DataFrame library (tables). Used for the fills/equity tables we write as parquet.
import polars as pl

from mm.backtest.strategy import Fill

# Nanoseconds per second (underscores in numbers are just visual separators). Project time unit is ns.
NS = 1_000_000_000
# Seconds in a year, used to annualise the Sharpe ratio.
YEAR_S = 365.25 * 86400


# ====================================================================== account
# The Account is the "true books": what we really own and owe, independent of what the strategy has
# been told yet. Money is tracked in quote currency (USDT) with one subtle perp-futures point:
# there is no coin delivery; buying adds to `position` and subtracts notional from `cash`.
class Account:
    """True position / cash bookkeeping.  All amounts in quote currency (USDT)."""

    def __init__(self, starting_cash: float) -> None:
        self.start_cash = starting_cash
        self.cash = starting_cash
        self.position = 0.0
        self.avg_entry = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.funding = 0.0          # positive = we paid
        self.spread_capture = 0.0
        self.inventory_pnl = 0.0
        self.last_mid: float | None = None
        self.volume = 0.0           # traded base qty
        self.notional = 0.0

    # ---- marks
    def on_mid(self, mid: float) -> None:
        """Mark to a new mid.  Accumulates inventory PnL = position * change in mid."""
        # "Marking to market" = revaluing what we hold at the current price. Holding +2 ETH while the
        # mid rises $1 earns $2 of inventory PnL; this is where adverse selection (the market moving
        # against the side we just traded) shows up.
        if self.last_mid is not None:
            self.inventory_pnl += self.position * (mid - self.last_mid)
        self.last_mid = mid

    # @property: use as account.unrealized (no brackets). Unrealized PnL = paper gain/loss on the open
    # position versus its average entry price; realized = locked in by closing.
    @property
    def unrealized(self) -> float:
        if self.last_mid is None or self.position == 0.0:
            return 0.0
        return self.position * (self.last_mid - self.avg_entry)

    @property
    def equity(self) -> float:
        return self.cash + (self.position * self.last_mid if self.last_mid is not None else 0.0)

    # ---- events
    def on_fill(self, f: Fill) -> None:
        # s = signed quantity: + when we buy, - when we sell, so one formula serves both directions.
        # Spread capture: buying BELOW the mid (px < ref) gives a positive number, i.e. the edge we
        # earned by quoting away from fair value. Cash falls when buying, rises when selling; fees
        # always reduce cash (or add to it for a rebate).
        s = f.qty if f.side == "buy" else -f.qty
        ref = f.ref_mid if f.ref_mid is not None else (self.last_mid if self.last_mid is not None else f.price)
        self.spread_capture += s * (ref - f.price)
        self.cash -= s * f.price + f.fee
        self.fees += f.fee
        self.volume += f.qty
        self.notional += f.qty * f.price
        # Average-cost bookkeeping. Adding to a position blends the entry price; reducing realizes PnL
        # on the closed part; a flip (long to short) realizes everything and the remainder opens a
        # new position at the fill price.
        pos = self.position
        if pos == 0.0 or (pos > 0) == (s > 0):  # opening / adding
            new = pos + s
            self.avg_entry = (self.avg_entry * abs(pos) + f.price * abs(s)) / abs(new)
            self.position = new
        else:                                    # reducing, closing or flipping
            closed = min(abs(s), abs(pos))
            self.realized += closed * (f.price - self.avg_entry) * (1 if pos > 0 else -1)
            new = pos + s
            if abs(new) < 1e-12:
                new = 0.0
                self.avg_entry = 0.0
            elif (new > 0) != (pos > 0):         # flipped: remainder opened at the fill price
                self.avg_entry = f.price
            self.position = new

    def on_funding(self, rate: float, mark_price: float | None) -> float:
        """Funding settlement: longs pay ``position * mark * rate`` (shorts receive when rate > 0)."""
        # Perpetual futures never expire, so a periodic "funding" payment (every 8h on Binance) ties
        # the perp price to spot: when the rate is positive, longs pay shorts; when negative, the reverse.
        # We charge it on the position held at the settlement instant, valued at the mark price.
        px = mark_price if mark_price is not None else self.last_mid
        if px is None or self.position == 0.0:
            return 0.0
        pay = self.position * px * rate
        self.cash -= pay
        self.funding += pay
        return pay

    def identity_gap(self) -> float:
        """cash + pos*mid - (start + realized + unrealized - fees - funding).  Should be ~0."""
        return self.equity - (self.start_cash + self.realized + self.unrealized - self.fees - self.funding)


# ====================================================================== recorder
# Recorder: a notebook the engine writes into while running. Millions of events happen but we only
# keep a time-sampled equity curve (one row per bucket), a thinned mid-price series (needed later to
# look up "mid 10 seconds after this fill"), and every fill.
class Recorder:
    """Collects series during a run.  Called by the engine."""

    def __init__(self, account: Account, equity_bucket_ns: int, mid_sample_ns: int,
                 coverage_gap_ns: int = 60 * NS) -> None:
        self.account = account
        # Data coverage: time between consecutive market events, minus any silence longer than
        # coverage_gap_ns. Tells how many hours the data REALLY covers (a calendar window can be half empty).
        self.coverage_gap_ns = coverage_gap_ns
        self._prev_event: int | None = None
        self.covered_ns = 0
        self.gaps: list[tuple[int, int]] = []       # (start_ns, end_ns) of silences longer than the threshold
        self.bucket = equity_bucket_ns
        self.mid_sample = mid_sample_ns
        self._next: int | None = None
        self.eq_ts = array("q")
        self.eq_equity = array("d")
        self.eq_pos = array("d")
        self.eq_mid = array("d")
        self.eq_cash = array("d")
        self.eq_realized = array("d")
        self.eq_unreal = array("d")
        self.eq_fees = array("d")
        self.eq_funding = array("d")
        self.mid_ts = array("q")
        self.mid_px = array("d")
        self._mid_slot = -1
        self.fills: list[tuple] = []
        self.first_ts: int | None = None
        self.last_ts: int | None = None

    def sample_until(self, t: int) -> None:
        """Record equity at every grid time <= t (state as of just before the current event)."""
        # Sampling on a fixed grid (e.g. every 1 s) rather than per event makes Sharpe and drawdown
        # independent of how busy the market was. "-(-t // b) * b" is the integer ceiling trick:
        # negating before floor-division rounds UP, giving the first grid time >= t.
        # The while-loop forward-fills grid points across quiet gaps with the unchanged state.
        if self.first_ts is None:
            self.first_ts = t
            self._next = -(-t // self.bucket) * self.bucket  # first grid point >= t
        self.last_ts = t
        a = self.account
        while t >= self._next:
            self._row(self._next, a)
            self._next += self.bucket

    def on_event_time(self, t: int) -> None:
        """Called once per market event: extend the covered time, or log a long silence as a gap."""
        p = self._prev_event
        if p is not None and t > p:
            if t - p > self.coverage_gap_ns:
                self.gaps.append((p, t))
            else:
                self.covered_ns += t - p
        self._prev_event = t

    def _row(self, ts: int, a: Account) -> None:
        self.eq_ts.append(ts)
        self.eq_equity.append(a.equity)
        self.eq_pos.append(a.position)
        self.eq_mid.append(a.last_mid if a.last_mid is not None else math.nan)
        self.eq_cash.append(a.cash)
        self.eq_realized.append(a.realized)
        self.eq_unreal.append(a.unrealized)
        self.eq_fees.append(a.fees)
        self.eq_funding.append(a.funding)

    def finish(self, t: int) -> None:
        self.sample_until(t)
        self.last_ts = t

    def on_mid(self, t: int, mid: float) -> None:
        # Thin the mid series: "//" is floor division, so all events within the same short time slot
        # share a slot number; we keep only the latest mid per slot to save memory.
        # IMPORTANT: each stored point is labelled with the time of the LAST update it contains (not the
        # slot start). Labelling with the slot start would let a markout lookup at "t + 10 s" read a mid
        # that was only set up to one slot (10 ms) LATER: a small lookahead. With true labels, "latest
        # point at or before the target time" can never contain the future.
        slot = t // self.mid_sample
        if slot == self._mid_slot:
            self.mid_ts[-1] = t
            self.mid_px[-1] = mid
        else:
            self._mid_slot = slot
            self.mid_ts.append(t)
            self.mid_px.append(mid)

    def on_fill(self, f: Fill, position_after: float) -> None:
        ref = f.ref_mid if f.ref_mid is not None else math.nan
        s = 1.0 if f.side == "buy" else -1.0
        self.fills.append((f.ts_ns, f.order_id, f.client_id, f.side, f.price, f.qty, f.liquidity, f.fee,
                           f.leaves_qty, ref, position_after, s * (ref - f.price) * f.qty, f.mechanism))


# ====================================================================== metrics
FILL_COLUMNS = ["ts_ns", "order_id", "client_id", "side", "price", "qty", "liquidity", "fee",
                "leaves_qty", "ref_mid", "position_after", "spread_capture", "mechanism"]


# Build the fills table and add MARKOUT columns. A markout asks: "h seconds after I traded, where is
# the mid compared with my fill price?" If we bought and the mid then falls, the fill was "toxic":
# informed traders picked us off (adverse selection). Averaging markouts over all fills measures it.
# Negative = bad for us. We use the mid from the recorded series, never data after the run ended.
def _fills_frame(rec: Recorder, horizons_s: list[float]) -> pl.DataFrame:
    schema = {"ts_ns": pl.Int64, "order_id": pl.Int64, "client_id": pl.String, "side": pl.String,
              "price": pl.Float64, "qty": pl.Float64, "liquidity": pl.String, "fee": pl.Float64,
              "leaves_qty": pl.Float64, "ref_mid": pl.Float64, "position_after": pl.Float64,
              "spread_capture": pl.Float64, "mechanism": pl.String}
    df = pl.DataFrame(rec.fills, schema=schema, orient="row")
    if df.is_empty() or len(rec.mid_ts) == 0:
        for h in horizons_s:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"markout_{h:g}s"),
                                 pl.lit(None, dtype=pl.Float64).alias(f"move_{h:g}s"))
        return df
    # np.frombuffer views the compact array as a numpy array without copying. Then everything below
    # is "vectorised": one numpy operation handles all fills at once instead of a Python loop.
    # np.where(cond, a, b) picks a where cond is true else b (+1/-1 sign for buy/sell, NaN if the
    # horizon is past the end of data). np.searchsorted finds, for each target time, the position in
    # the sorted time array; minus 1 gives "the latest mid at or before that time".
    mts = np.frombuffer(rec.mid_ts, dtype=np.int64)
    mpx = np.frombuffer(rec.mid_px, dtype=np.float64)
    ts = df["ts_ns"].to_numpy()
    sgn = np.where(df["side"].to_numpy() == "buy", 1.0, -1.0)
    px = df["price"].to_numpy()
    ref = df["ref_mid"].to_numpy()
    end = rec.last_ts if rec.last_ts is not None else int(mts[-1])
    cols = []
    for h in horizons_s:
        tgt = ts + int(h * NS)
        # idx = index of the latest stored mid at or before the target time; -1 means "none yet", which
        # must give NaN rather than being clipped onto a later point (that would read the future).
        raw = np.searchsorted(mts, tgt, side="right") - 1
        idx = np.clip(raw, 0, len(mts) - 1)
        mid_h = np.where((tgt <= end) & (raw >= 0), mpx[idx], np.nan)
        cols.append(pl.Series(f"markout_{h:g}s", sgn * (mid_h - px)))
        cols.append(pl.Series(f"move_{h:g}s", sgn * (mid_h - ref)))
    return df.with_columns(cols)


def _equity_frame(rec: Recorder) -> pl.DataFrame:
    return pl.DataFrame({
        "ts_ns": np.frombuffer(rec.eq_ts, dtype=np.int64), "equity": np.frombuffer(rec.eq_equity, dtype=np.float64),
        "position": np.frombuffer(rec.eq_pos, dtype=np.float64), "mid": np.frombuffer(rec.eq_mid, dtype=np.float64),
        "cash": np.frombuffer(rec.eq_cash, dtype=np.float64),
        "realized": np.frombuffer(rec.eq_realized, dtype=np.float64),
        "unrealized": np.frombuffer(rec.eq_unreal, dtype=np.float64),
        "fees": np.frombuffer(rec.eq_fees, dtype=np.float64),
        "funding": np.frombuffer(rec.eq_funding, dtype=np.float64)})


def _pnl_metrics(account: Account) -> dict:
    """Net PnL and its split: spread capture + inventory PnL = gross; gross - fees - funding = net."""
    net = account.equity - account.start_cash
    gross = account.spread_capture + account.inventory_pnl
    return {
        "total_net": net,
        "gross_trading": gross,
        "spread_capture": account.spread_capture,
        "inventory_pnl": account.inventory_pnl,
        "fees": account.fees,
        "funding": account.funding,
        "realized": account.realized,
        "unrealized": account.unrealized,
        "check_gross_minus_costs_minus_net": gross - account.fees - account.funding - net,
    }


def _fill_metrics(fills: pl.DataFrame, account: Account) -> dict:
    n = fills.height
    maker = fills.filter(pl.col("liquidity") == "maker")
    taker = fills.filter(pl.col("liquidity") == "taker")
    # Which fill mechanism produced the fills: queue (at-price after the queue), through (price traded
    # through us), cross (quote crossed us, no print) or taker. The mix shows which assumption a result
    # leans on: a strategy living on "cross"/"through" fills depends on the most generous mechanisms.
    by_mech = {}
    if n:
        # spread capture is split per mechanism too: it is an ATTRIBUTION number (fill price vs the mid just
        # before the event), so a "cross" line showing positive capture is not edge, see the report notes.
        for mech, cnt, qty, sc in fills.group_by("mechanism").agg(
                pl.len(), pl.col("qty").sum(), pl.col("spread_capture").sum()).iter_rows():
            by_mech[mech] = {"count": cnt, "qty": float(qty), "spread_capture": float(sc)}
    return {
        "by_mechanism": by_mech,
        "count": n, "maker_count": maker.height, "taker_count": taker.height,
        "maker_volume": float(maker["qty"].sum()) if maker.height else 0.0,
        "taker_volume": float(taker["qty"].sum()) if taker.height else 0.0,
        "traded_notional": account.notional,
        "maker_share_of_volume": (float(maker["qty"].sum()) / float(fills["qty"].sum())) if n else None,
    }


def _order_metrics(exchange, account: Account) -> dict:
    # "submitted / accepted / rejected / reject_reasons" count NEW orders only. Replaces (modifies) are
    # counted apart: a quoter that re-prices every second sends ~50x more replaces than new orders, and
    # lumping them together made the fill ratio meaningless.
    st = exchange.stats
    accepted, replaced = st.get("accepted", 0), st.get("replaced", 0)
    all_reasons = exchange.reject_reasons + exchange.replace_reject_reasons
    return {
        "submitted": st.get("submitted", 0), "accepted": accepted, "rejected": st.get("rejected", 0),
        "reject_reasons": dict(exchange.reject_reasons),
        "replace_requests": st.get("replace_requests", 0), "replaced": replaced,
        "replace_rejected": st.get("replace_rejected", 0),
        "replace_reject_reasons": dict(exchange.replace_reject_reasons),
        "risk_rejects": sum(v for k, v in all_reasons.items() if k.startswith("risk_")),
        "canceled": st.get("canceled", 0),
        # share of NEW orders that got at least one fill (a replaced order counts once, under its first id)
        "fill_ratio_orders": (exchange.orders_with_fills / accepted) if accepted else None,
        # per action: order incarnations with a fill / (new orders + replaces accepted)
        "fill_ratio_per_action": (exchange.incarnations_with_fills / (accepted + replaced))
                                 if accepted + replaced else None,
        "fill_ratio_qty": (account.volume / exchange.accepted_qty) if exchange.accepted_qty else None,
    }


def _sharpe_metrics(equity: np.ndarray, bucket_ns: int, mcfg: dict) -> dict:
    """Sharpe = mean PnL per bucket / its standard deviation, scaled by sqrt(buckets per year).
    It rewards steady gains over lumpy ones. ``equity[::k]`` = every k-th element; np.diff = increments."""
    k = max(1, int(round(mcfg["sharpe_bucket_s"] * NS / bucket_ns)))
    r = np.diff(equity[::k])
    if len(r) >= 3 and r.std(ddof=1) > 0:
        per_year = YEAR_S / mcfg["sharpe_bucket_s"]
        # t-statistic of the mean PnL per bucket (mean / standard error). |t| < 2 means "cannot be
        # told apart from zero"; buckets are autocorrelated, so treat it as optimistic.
        tstat = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))
        return {"annualized": float(r.mean() / r.std(ddof=1) * math.sqrt(per_year)),
                "t_stat_mean_pnl_per_bucket": tstat,
                "bucket_s": mcfg["sharpe_bucket_s"], "n_buckets": int(len(r)),
                "note": "PnL-increment Sharpe; not comparable to a fund Sharpe"}
    return {"annualized": None, "bucket_s": mcfg["sharpe_bucket_s"], "n_buckets": int(len(r))}


def _daily_pnl(eq: pl.DataFrame, start_cash: float) -> dict:
    """PnL per UTC calendar day: equity at each day's last sample minus the previous day's (the first
    day starts from the starting cash). Per-minute Sharpe on a short run is noisy; daily PnL is the
    unit people actually compare. np.diff(days) != 0 marks the samples where the day changes."""
    e = eq["equity"].to_numpy()
    days = eq["ts_ns"].to_numpy() // (86400 * NS)
    ends = np.append(np.nonzero(np.diff(days))[0], len(e) - 1)
    prev, daily = start_cash, {}
    for i in ends:
        label = (np.datetime64(int(days[i]), "D")).astype(str)
        daily[str(label)] = float(e[i] - prev)
        prev = e[i]
    return daily


def _inventory_metrics(pos: np.ndarray, mcfg: dict) -> dict:
    """Distribution of the position over time (one sample per equity bucket), incl. time at the limit."""
    lim = mcfg.get("inventory_limit")
    inv = {"mean": float(pos.mean()), "std": float(pos.std()), "mean_abs": float(np.abs(pos).mean()),
           "max_abs": float(np.abs(pos).max()),
           "p05": float(np.percentile(pos, 5)), "p50": float(np.percentile(pos, 50)),
           "p95": float(np.percentile(pos, 95))}
    if lim:
        inv["limit"] = lim
        inv["time_at_limit_pct"] = float(100 * np.mean(np.abs(pos) >= lim - 1e-9))
    # histogram bins come from config (bins + 1 edges); the range is +/- the inventory limit
    edges = np.linspace(-(lim or max(1e-9, np.abs(pos).max())), (lim or max(1e-9, np.abs(pos).max())),
                        int(mcfg.get("inventory_histogram_bins", 10)) + 1)
    cnt, _ = np.histogram(np.clip(pos, edges[0], edges[-1]), bins=edges)
    inv["histogram"] = {"bin_edges": [float(x) for x in edges], "time_share": [float(c / len(pos)) for c in cnt]}
    return inv


# Bootstrap = re-draw the fills WITH replacement many times and recompute the size-weighted mean each
# time; the 2.5th and 97.5th percentiles of those means are a 95% confidence interval. Seeded, so the
# same run gives the same interval. Fills are not independent (they cluster in fast markets), so
# treat the interval as optimistically narrow.
def _bootstrap_ci(values: np.ndarray, weights: np.ndarray, n_boot: int, seed: int) -> list[float] | None:
    n = len(values)
    if n < 2 or n_boot <= 0:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, n)
        means[i] = np.average(values[idx], weights=weights[idx])
    lo, hi = np.percentile(means, [2.5, 97.5])
    return [float(lo), float(hi)]


def _markout_metrics(fills: pl.DataFrame, horizons: list, n_boot: int = 0, seed: int = 0) -> dict:
    # Summaries weighted by fill size (np.average with weights) so big fills count more. Dividing by
    # price and multiplying by 1e4 converts to basis points (1 bp = 0.01%), comparable across prices.
    # The "move" columns are relative to the mid at the fill (pure drift); "markout" includes the edge.
    mk: dict = {}
    for h in horizons:
        key = f"{h:g}s"
        if fills.height and f"markout_{key}" in fills.columns:
            sub = fills.filter(pl.col(f"markout_{key}").is_not_null() & pl.col(f"markout_{key}").is_not_nan())
            if sub.height:
                w = sub["qty"].to_numpy()
                mko = sub[f"markout_{key}"].to_numpy()
                mv = sub[f"move_{key}"].to_numpy()
                pxs = sub["price"].to_numpy()
                mk[key] = {
                    "n_fills": int(sub.height),
                    "avg_markout_per_eth": float(np.average(mko, weights=w)),
                    "avg_markout_per_eth_ci95": _bootstrap_ci(mko, w, n_boot, seed),
                    "avg_markout_bps": float(np.average(mko / pxs, weights=w) * 1e4),
                    "avg_move_per_eth": float(np.average(mv, weights=w)),
                    "avg_move_bps": float(np.average(mv / pxs, weights=w) * 1e4),
                    "total_markout_pnl": float(np.sum(mko * w)),
                    "pct_fills_adverse": float(100 * np.mean(mv < 0)),
                }
                for liq in ("maker", "taker"):
                    s2 = sub.filter(pl.col("liquidity") == liq)
                    if s2.height:
                        mk[key][f"avg_move_bps_{liq}"] = float(np.average(
                            s2[f"move_{key}"].to_numpy() / s2["price"].to_numpy(), weights=s2["qty"].to_numpy()) * 1e4)
    return mk


def _coverage(rec: Recorder) -> dict:
    """Hours the data really covers: first to last EVENT minus silences longer than the threshold."""
    big = sorted(rec.gaps, key=lambda g: g[0] - g[1])[:10]    # the 10 longest silences
    return {"gap_threshold_s": rec.coverage_gap_ns / NS,
            "covered_hours": rec.covered_ns / NS / 3600,
            "silent_hours": sum(b - a for a, b in rec.gaps) / NS / 3600,
            "n_gaps": len(rec.gaps),
            "longest_gaps": [{"start_ns": a, "end_ns": b, "seconds": (b - a) / NS} for a, b in big]}


def compute_metrics(rec: Recorder, account: Account, exchange, mcfg: dict,
                    seed: int = 0) -> tuple[dict, pl.DataFrame, pl.DataFrame]:
    """Return (metrics dict, fills DataFrame, equity DataFrame).  ``mcfg`` is ``cfg["metrics"]``;
    ``seed`` (run.seed) seeds the markout bootstrap."""
    horizons = list(mcfg["markout_horizons_s"])
    fills = _fills_frame(rec, horizons)
    eq = _equity_frame(rec)
    start = account.start_cash
    m: dict = {}
    dur_s = ((rec.last_ts - rec.first_ts) / NS) if rec.first_ts is not None else 0.0
    m["duration_hours"] = dur_s / 3600
    m["coverage"] = _coverage(rec)
    m["pnl"] = _pnl_metrics(account)
    m["final_position"] = account.position
    m["fills"] = _fill_metrics(fills, account)
    m["orders"] = _order_metrics(exchange, account)

    # Equity-curve statistics. Drawdown = how far equity sits below its previous peak
    # (np.maximum.accumulate is a running maximum).
    if eq.height >= 2:
        e = eq["equity"].to_numpy()
        dd = np.maximum.accumulate(e) - e
        m["max_drawdown"] = {"abs": float(dd.max()), "pct_of_start_cash": float(dd.max() / start)}
        m["sharpe"] = _sharpe_metrics(e, rec.bucket, mcfg)
        m["daily_pnl"] = _daily_pnl(eq, start)
        m["inventory"] = _inventory_metrics(eq["position"].to_numpy(), mcfg)
    else:
        m["max_drawdown"] = {"abs": 0.0, "pct_of_start_cash": 0.0}
        m["sharpe"] = {"annualized": None}
        m["inventory"] = {}

    m["markouts"] = _markout_metrics(fills, horizons, int(mcfg.get("bootstrap_n", 0)), seed)
    m["identity_gap"] = account.identity_gap()
    return _clean(m), fills, eq


# Recursively replace NaN/inf (not valid JSON) with None so metrics.json is always readable.
# Recursion = a function calling itself on nested dicts/lists.
def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o
