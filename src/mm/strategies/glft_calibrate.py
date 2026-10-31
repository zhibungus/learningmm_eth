"""Offline calibration of the GLFT strategy: sigma, A, k, then gamma (spec §6).

Run:  .venv\\Scripts\\python.exe -m mm.strategies.glft_calibrate --config config\\backtest\\glft_smoke.yaml

What it does (all on the ``in_sample`` period ONLY; ``out_of_sample`` is never opened)
    1. sigma  : realised volatility of the mid on a 1 s grid (spec §6.1; lesson 01 §6 uses sigma ~ 0.26)
    2. A, k   : how often a resting order of size Delta at distance delta from the mid would have been
                traded through, fitted to lambda(delta) = A * exp(-k * delta)   (spec §6.2)
                (fill-rate fitting: not yet covered in lessons, planned L3 and L6)
    3. gamma  : solved by bisection so the skew at max inventory equals ``skew_at_Q_ticks`` (spec §6.4)
    4. stop conditions (spec §6.2 step 9): reported loudly, exit code 2, never silently ignored.
Outputs: a printed summary, ``config/strategies/glft_calibrated.yaml`` and a diagnostics folder
``reports/<date>_glft_perp_calibration/`` (json + fit table).  Paste the numbers into the backtest
YAML yourself: calibration never edits the backtest config.

This module deliberately contains NO ``Strategy`` subclass, so the engine's loader is not confused.
"""
# Lazy type hints: "dict[int, float]" (a dict with int keys, float values) is only text at import time.
from __future__ import annotations

import argparse                      # command-line flags (--config) and --help
import csv                           # writes the fit table as a .csv file
import datetime as dt                # "as dt" = short alias; UTC date for the report folder name
import json                          # readable diagnostics file
import math
import sys                           # sys.exit(code): exit code 0 = ok, 2 = a stop condition tripped
# @dataclass (a decorator) writes __init__/__repr__/__eq__ from the annotated fields ("name: type").
# fields() lists them; replace() copies a frozen dataclass with some fields changed.
from dataclasses import dataclass, fields, replace
from pathlib import Path             # file-path object; "/" joins pieces: Path("reports") / "x.csv"

import numpy as np                   # fast arrays: one day has ~8 million quotes
import polars as pl                  # DataFrame (table) library; reads the parquet data files
import yaml                          # PyYAML: the project's config format

# load_config fills YAML defaults; period_window_ns gives a period's exact [start, end) in ns; the strategy's
# own maths is reused so calibration and trading use identical formulas (spec §4).
from mm.backtest.config import load_config
from mm.backtest.run import period_window_ns
from mm.data.config import PROJECT_ROOT
from mm.data.loaders import load_quotes, load_trades, resolve_source
from mm.strategies.glft import GlftParams, closed_form, fee_per_eth

# Unit conversions (not tunable). Project convention: timestamps are integer nanoseconds UTC.
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
NS_PER_HOUR = 3600 * NS_PER_S
_EPS = 1e-9          # float-noise guard when converting prices to whole ticks (as in glft.floor_to_tick)


# ================================================================================== parameters
# The calibration-only settings (spec §6, §11). They sit in the SAME strategy.params YAML section as the
# strategy's keys (GlftParams.from_dict skips "cal_" keys), so one YAML feeds both.
@dataclass(frozen=True)
class CalParams:
    cal_sigma_dt_ms: float = 1000.0
    cal_sigma_check_dt_ms: float = 10000.0
    cal_noise_tol: float = 0.20          # report if 1 s and 10 s sigma differ by more than this
    cal_lambda_dt_ms: float = 500.0
    cal_latency_ms: float = 400.0        # feed ~240 ms + order ~160 ms
    cal_delta_min_ticks: float = 10.0
    cal_delta_max_ticks: float = 150.0
    cal_delta_step_ticks: float = 5.0
    cal_min_hits: int = 20               # per side per bin
    cal_bootstrap: int = 200             # hour-block resamples
    cal_max_quote_silence_ms: float = 2000.0
    cal_max_trade_silence_ms: float = 10000.0   # trade silence above this = outage, not a quiet market
    cal_min_fit_end_ticks: float = 60.0  # gate: fit range must reach at least this far
    cal_min_r2: float = 0.9              # gate: weighted R^2
    cal_max_k_side_diff: float = 0.30    # gate: relative difference of bid-side and ask-side k
    cal_gamma_min: float = 1e-4          # bisection bracket for gamma, 1/USDT
    cal_gamma_max: float = 1e3
    cal_gamma_rel_tol: float = 1e-6

    # @classmethod: receives the class (cls); an alternative constructor.
    @classmethod
    def from_dict(cls, d: dict | None) -> "CalParams":
        """Pick the ``cal_*`` keys out of strategy.params; a misspelled cal_ key is an error."""
        cal = {k: v for k, v in (d or {}).items() if k.startswith("cal_")}     # dict comprehension
        unknown = set(cal) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown calibration params: {sorted(unknown)}")
        return cls(**cal)                # **cal unpacks the dict into keyword arguments


# ================================================================================== data container
@dataclass
class MarketArrays:
    """One period's data as numpy arrays (all times: int64 nanoseconds). Hints like np.ndarray are documentation only."""
    lo_ns: int
    hi_ns: int
    q_ts: np.ndarray        # quote exchange times, sorted
    q_mid: np.ndarray       # (bid + ask) / 2 at each quote
    t_ts: np.ndarray        # trade times
    t_px_tick: np.ndarray   # trade price in WHOLE ticks (int): exact comparisons, no float noise
    t_qty: np.ndarray       # trade size, ETH
    t_is_buy: np.ndarray    # True when the AGGRESSOR bought (schema field `side`; never from is_buyer_maker)
    t_id: np.ndarray        # aggregate trade id of each print; a jump between prints = missing aggTrades
    tick: float             # tick size, USDT (0.01)


def load_period_arrays(cfg: dict, period: dict, tick: float) -> MarketArrays:
    """Read quotes + aggTrades of ONE period. Callers pass in_sample, so out_of_sample is never opened (spec §6)."""
    d = cfg["data"]
    lo, hi = period_window_ns(period)
    q = load_quotes(period["start"], period["end"], d["symbol"], resolve_source(d["source"], "quotes"))
    t = load_trades(period["start"], period["end"], d["symbol"], resolve_source(d["source"], "trades"),
                    d["trade_dataset"])
    # polars: pl.col("ts_ns") is that column; comparisons give boolean columns, "&" combines them,
    # .filter(mask) keeps rows where the mask is True. Here: keep events inside [lo, hi), sorted by time.
    inside = (pl.col("ts_ns") >= lo) & (pl.col("ts_ns") < hi)
    q, t = q.filter(inside).sort("ts_ns"), t.filter(inside).sort("ts_ns")
    # .to_numpy() turns a polars column into a numpy array. Mid = (bid + ask) / 2 (L1 §2). np.rint rounds to
    # the nearest whole number (= price in ticks); .astype(np.int64) stores it as integers.
    return MarketArrays(
        lo, hi, q["ts_ns"].to_numpy(), 0.5 * (q["bid_px"].to_numpy() + q["ask_px"].to_numpy()),
        t["ts_ns"].to_numpy(), np.rint(t["price"].to_numpy() / tick).astype(np.int64),
        t["qty"].to_numpy(), (t["side"] == "buy").to_numpy(),
        t["trade_id"].to_numpy(), tick)


# ====================================================================== grid helpers (no look-ahead)
def trade_gap_ranges(m: MarketArrays, max_silence_ns: int):
    """Time ranges (start, end] between consecutive aggTrades that are NOT trustworthy (spec §6.2):
    a break in the aggregate trade_id (not previous + 1: aggTrades went missing) or a silence above
    max_silence_ns (an outage; shorter quiet spells are real market information, a true "no hit").
    The raw first_id/last_id are NOT used: Binance skips raw ids, so they jump with nothing missing.
    Returns (starts, ends, number of id gaps, number of long silences)."""
    id_gap = np.diff(m.t_id) != 1
    silent = np.diff(m.t_ts) > max_silence_ns
    bad = id_gap | silent                    # "|" = OR on boolean arrays
    return m.t_ts[:-1][bad], m.t_ts[1:][bad], int(id_gap.sum()), int(silent.sum())



def silence_ranges(q_ts: np.ndarray, max_silence_ns: int) -> tuple[np.ndarray, np.ndarray]:
    """Time ranges (start, end] with no quote for longer than max_silence_ns: data holes, not real stillness."""
    gap = np.diff(q_ts)                      # np.diff: differences of neighbours = time between quotes
    bad = gap > max_silence_ns               # comparing an array with a number gives a boolean mask
    # q_ts[:-1] = all but the last element; arr[mask] keeps the elements where the mask is True.
    return q_ts[:-1][bad], q_ts[1:][bad]


def overlaps_gap(a: np.ndarray, b: np.ndarray, bad_s: np.ndarray, bad_e: np.ndarray) -> np.ndarray:
    """For each interval (a, b]: does it overlap any bad range (s, e]?  (ranges sorted and disjoint)"""
    if len(bad_s) == 0:
        return np.zeros(len(a), dtype=bool)  # np.zeros(n, dtype=bool) = n False values
    # np.searchsorted(sorted, values, side="right"): binary search for the insertion point of each value;
    # "right" puts equal values AFTER ("left" would put them before). So idx = first bad range ending after a.
    idx = np.searchsorted(bad_e, a, side="right")
    ok = idx < len(bad_e)                    # idx == len means "no later bad range"
    out = np.zeros(len(a), dtype=bool)
    # Overlap <=> that range also starts before b. out[ok] = ... writes only where ok is True.
    out[ok] = bad_s[idx[ok]] < b[ok]
    return out


def mid_on_grid(m: MarketArrays, grid: np.ndarray) -> np.ndarray:
    """Mid of the last quote at or before each grid time (NaN before the first quote): never the future."""
    i = np.searchsorted(m.q_ts, grid, side="right") - 1   # last quote with ts <= t; -1 = none yet
    out = np.full(len(grid), np.nan)         # np.full(n, v): n copies of v; NaN = "no valid value"
    out[i >= 0] = m.q_mid[i[i >= 0]]         # fancy indexing: pick elements by an integer array
    return out


# ================================================================================== 1. sigma
@dataclass
class SigmaResult:
    sigma: float               # USDT / sqrt(s)
    n_increments: int
    hourly: dict[int, float]   # hour of day -> sigma
    mean_mid: float            # mean of the valid grid mids (used for the fee c when solving gamma)


def estimate_sigma(m: MarketArrays, dt_ms: float, max_silence_ms: float) -> SigmaResult:
    """Spec §6.1: sigma^2 = sum(x_i^2) / (N * dt), x_i = mid change over one grid step, no demeaning.

    Why: a random walk's variance grows linearly with time, so E[x^2] = sigma^2 * dt (L1 §6)."""
    step = int(dt_ms * NS_PER_MS)
    grid = np.arange(m.lo_ns, m.hi_ns + 1, step, dtype=np.int64)   # int64 keeps ns exact (float would not)
    mids = mid_on_grid(m, grid)
    x = np.diff(mids)                                              # x_i = m_i - m_{i-1}
    bs, be = silence_ranges(m.q_ts, int(max_silence_ms * NS_PER_MS))
    # np.isfinite is False for NaN; "~" is NOT. Keep increments with a mid at both ends and no data hole inside.
    good = np.isfinite(x) & ~overlaps_gap(grid[:-1], grid[1:], bs, be)
    dt_s = dt_ms / 1000.0
    n = int(good.sum())                                            # summing booleans counts the Trues
    sigma = math.sqrt(float(np.sum(x[good] ** 2)) / (n * dt_s))
    # "//" = floor division: ns since the period start // ns per hour = hour number.
    hours = ((grid[:-1] - m.lo_ns) // NS_PER_HOUR).astype(int)     # hour in which each step STARTS
    hourly = {}                      # shows the constant-sigma assumption failing (spec §8)
    for h in np.unique(hours[good]):                               # np.unique: sorted distinct values
        sel = good & (hours == h)
        hourly[int(h)] = math.sqrt(float(np.sum(x[sel] ** 2)) / (int(sel.sum()) * dt_s))
    return SigmaResult(sigma, n, hourly, float(np.nanmean(mids)))


# ================================================================================== 2. lambda(delta)
@dataclass
class LambdaCurve:
    """Hit counts per distance bin: the raw material of lambda(delta) = A*exp(-k*delta) (spec §6.2; L1 §2, §7).

    lambda(delta) = "how often per second does the market trade through an order resting delta from the mid?"."""
    delta_ticks: np.ndarray    # bin distances in ticks (10, 15, ..., 150)
    hits_bid: np.ndarray       # grid steps where a bid at that distance would have been hit
    hits_ask: np.ndarray
    n_valid: int               # grid steps with a usable mid and an intact window
    dt_s: float                # window length, seconds
    hour_hits_bid: np.ndarray  # shape (hours, bins), for the bootstrap
    hour_hits_ask: np.ndarray
    hour_valid: np.ndarray     # shape (hours,)
    n_steps: int = 0           # all grid steps
    n_trade_masked: int = 0    # steps dropped only because their window overlaps a trade-stream gap/outage


def lambda_curve(m: MarketArrays, p: GlftParams, cal: CalParams, dt_ms: float | None = None) -> LambdaCurve:
    """Count, per distance bin, how often an order of size Delta placed delta from the mid seen at grid time
    t_i would have been traded through in the window (t_i + L, t_i + L + dt] (spec §6.2 steps 1-6).

    Why the shift L = 400 ms: a quote decided from the mid we saw reaches the exchange ~400 ms later
    (feed + order latency), so that is the window it actually faces (spec §13 item 5)."""
    dt_ms = cal.cal_lambda_dt_ms if dt_ms is None else dt_ms
    step, lat = int(dt_ms * NS_PER_MS), int(cal.cal_latency_ms * NS_PER_MS)
    tick = m.tick
    # Bins 10, 15, ..., 150 ticks (np.arange excludes its stop, so extend it by half a step).
    deltas = np.arange(cal.cal_delta_min_ticks, cal.cal_delta_max_ticks + 0.5 * cal.cal_delta_step_ticks,
                       cal.cal_delta_step_ticks)
    grid = np.arange(m.lo_ns, m.hi_ns - lat - step + 1, step, dtype=np.int64)   # t_i with a full window
    mids = mid_on_grid(m, grid)
    bs, be = silence_ranges(m.q_ts, int(cal.cal_max_quote_silence_ms * NS_PER_MS))
    # A step counts only with a mid and a window free of data holes; n_valid = valid.sum() is the time base.
    valid = np.isfinite(mids) & ~overlaps_gap(grid + lat, grid + lat + step, bs, be)
    # A missing trade would read as "no hit" and bias lambda down, so windows overlapping a trade-stream
    # gap are dropped from BOTH the time base and the hits (spec §6.2).
    ts_, te_, _, _ = trade_gap_ranges(m, int(cal.cal_max_trade_silence_ms * NS_PER_MS))
    trade_bad = overlaps_gap(grid + lat, grid + lat + step, ts_, te_)
    n_trade_masked = int((valid & trade_bad).sum())
    valid = valid & ~trade_bad

    # Window index of each trade: time in (t_i + L, t_i + L + dt] -> i = (x - 1) // step.
    x = m.t_ts - m.lo_ns - lat
    sel = x >= 1
    win = (x[sel] - 1) // step
    df = pl.DataFrame({"w": win, "px": m.t_px_tick[sel], "qty": m.t_qty[sel], "buy": m.t_is_buy[sel]})
    n_win = len(grid)

    # A bid is hit iff the size of SELL prints at prices STRICTLY below it reaches Delta ("strictly through":
    # a print exactly at our price only eats the queue ahead of us, so it is not a certain fill; backtest
    # README, spec §6.2 step 5). Equivalent: bid > p*, where p* is the price at which the running sell size,
    # walking up from the lowest price, first reaches Delta. Asks are the mirror image (buy prints, walking down).
    def p_star(is_buy: bool) -> np.ndarray:         # a def inside a def can read the outer variables
        d = df.filter(pl.col("buy") == is_buy).filter(pl.col("w") < n_win)
        d = d.sort(["w", "px"], descending=[False, is_buy])          # per window: price ascending (sells) / descending (buys)
        # .cum_sum().over("w") = running total restarted in each window; .alias names the new column.
        d = d.with_columns(pl.col("qty").cum_sum().over("w").alias("cum"))
        # Keep rows that reached Delta (1e-12 forgives float noise); .unique keeps the first per window = p*.
        d = d.filter(pl.col("cum") >= p.order_qty - 1e-12).unique(subset="w", keep="first", maintain_order=True)
        out = np.full(n_win, -np.inf if is_buy else np.inf)         # inf = Delta never reached: never hit
        out[d["w"].to_numpy()] = d["px"].to_numpy()
        return out

    ps_bid, ps_ask = p_star(False), p_star(True)
    # [:, None] adds an axis so N grid steps x J bins combine into an N x J table ("broadcasting").
    mm = np.where(valid, mids, 0.0)[:, None]        # np.where(cond, a, b): a where cond else b
    dd = (deltas * tick)[None, :]                   # distances in USDT
    b_tick = np.floor((mm - dd) / tick + _EPS)      # hypothetical bid in ticks, rounded DOWN (spec §4.4)
    a_tick = np.ceil((mm + dd) / tick - _EPS)       # hypothetical ask, rounded UP
    # Bins are nested (a hit at 50 ticks is also a hit at 45), so they are not independent (hence the bootstrap).
    hit_b = (b_tick > ps_bid[:, None]) & valid[:, None]
    hit_a = (a_tick < ps_ask[:, None]) & valid[:, None]
    hours = ((grid - m.lo_ns) // NS_PER_HOUR).astype(int)
    n_h = int(hours.max()) + 1
    # List comprehension: per hour h, sum the Trues down the rows (axis=0) = hits per bin in that hour.
    hb = np.array([hit_b[hours == h].sum(axis=0) for h in range(n_h)])
    ha = np.array([hit_a[hours == h].sum(axis=0) for h in range(n_h)])
    hv = np.array([int(valid[hours == h].sum()) for h in range(n_h)])
    return LambdaCurve(deltas, hb.sum(axis=0), ha.sum(axis=0), int(valid.sum()), dt_ms / 1000.0, hb, ha, hv,
                       len(grid), n_trade_masked)


@dataclass
class Fit:
    """One exponential fit; ok=False means it could not be fitted. float("nan") = not-a-number."""
    ok: bool
    A: float = float("nan")
    k: float = float("nan")
    r2: float = float("nan")
    n_bins: int = 0
    range_end_ticks: float = float("nan")


def wls_fit(x: np.ndarray, lam: np.ndarray, w: np.ndarray) -> Fit:
    """Weighted least squares of ln(lambda) on x = delta [USDT]:  ln lam = ln A - k*x  (spec §6.2 step 8).

    Log-linear: taking the log turns A*exp(-k*delta) into a straight line (slope -k, intercept ln A), so the
    huge near-touch rates do not dominate. Weights = hit counts: ln(rate) has variance ~ 1/hits, so
    weighting by hits trusts well-measured bins more. R^2 = share of the weighted variance the line explains."""
    if len(x) < 2 or np.any(lam <= 0):
        return Fit(False)
    y = np.log(lam)
    sw = w.sum()
    xb, yb = (w * x).sum() / sw, (w * y).sum() / sw       # weighted means
    sxx = (w * (x - xb) ** 2).sum()
    if sxx <= 0:
        return Fit(False)
    slope = (w * (x - xb) * (y - yb)).sum() / sxx          # = -k
    icpt = yb - slope * xb                                 # = ln A
    sse = (w * (y - (icpt + slope * x)) ** 2).sum()        # weighted residuals
    sst = (w * (y - yb) ** 2).sum()                        # weighted total variation
    return Fit(True, math.exp(icpt), -slope, 1.0 - sse / sst if sst > 0 else float("nan"), len(x), float("nan"))


def fit_curve(c: LambdaCurve, hb: np.ndarray, ha: np.ndarray, n_valid: int, cal: CalParams, tick: float):
    """Spec §6.2 steps 6-8 on given hit counts: weighted fit, plain OLS, per-side fits.

    rate = hits / (n_valid * dt): hits divided by the time we watched = average fills per second."""
    d = c.delta_ticks * tick
    lam_b, lam_a = hb / (n_valid * c.dt_s), ha / (n_valid * c.dt_s)
    lam = 0.5 * (lam_b + lam_a)                  # the model is symmetric: average the two sides
    w = (hb + ha).astype(float)
    keep = w >= 2 * cal.cal_min_hits             # sparse far bins are too noisy; kept bins form a prefix
    comb = wls_fit(d[keep], lam[keep], w[keep])
    if comb.ok:
        comb.range_end_ticks = float(c.delta_ticks[keep].max())
    ols = wls_fit(d[keep], lam[keep], np.ones(int(keep.sum())))   # unweighted (hftbacktest-style) for comparison
    kb, ka = hb >= cal.cal_min_hits, ha >= cal.cal_min_hits       # per-side consistency check
    side_b = wls_fit(d[kb], lam_b[kb], hb[kb].astype(float))
    side_a = wls_fit(d[ka], lam_a[ka], ha[ka].astype(float))
    return comb, ols, side_b, side_a, lam, keep


def bootstrap(c: LambdaCurve, cal: CalParams, tick: float, seed: int):
    """Hour-block bootstrap (spec §6.2 caveat). Nested bins and correlated seconds make the fit's own error
    bars too small, so resample whole HOURS with replacement, refit, and read a 95% interval off the
    2.5% / 97.5% percentiles of A and k. Hours are the blocks because activity clusters within hours."""
    rng = np.random.default_rng(seed)            # seeded: same seed, same resamples (CLAUDE.md)
    n_h = len(c.hour_valid)
    As, ks = [], []
    for _ in range(cal.cal_bootstrap):           # "_" = loop variable not used
        idx = rng.integers(0, n_h, n_h)          # n_h random hours, repeats allowed
        nv = int(c.hour_valid[idx].sum())
        if nv == 0:
            continue
        f, *_ = fit_curve(c, c.hour_hits_bid[idx].sum(axis=0), c.hour_hits_ask[idx].sum(axis=0), nv, cal, tick)
        if f.ok:                                 # "f, *_ = ..." keeps the first item, discards the rest
            As.append(f.A)
            ks.append(f.k)
    pct = lambda v: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] if v else None   # lambda = tiny function
    return {"A_ci95": pct(As), "k_ci95": pct(ks), "n_ok": len(As)}


# ================================================================================== 3. gamma
def solve_gamma(p: GlftParams, cal: CalParams, target_omega: float, mid: float) -> float:
    """Spec §6.4: find gamma so that omega(gamma) == target_omega [USDT/ETH], by bisection.

    omega increases with gamma, so halve a bracket [lo, hi] until it is tight. Halving is done in log space
    (geometric mean) because the bracket spans seven orders of magnitude. Risk aversion: L1 §8 teaser;
    not yet covered in lessons (planned L2, L4)."""
    lo, hi = cal.cal_gamma_min, cal.cal_gamma_max
    omega = lambda g: closed_form(replace(p, gamma=g), mid).omega   # omega for a trial gamma
    if not (omega(lo) <= target_omega <= omega(hi)):
        raise ValueError(f"target omega {target_omega:g} is outside the bracket [{lo:g}, {hi:g}] "
                         "for gamma; widen cal_gamma_min/max or change skew_at_Q_ticks")
    while (hi - lo) > cal.cal_gamma_rel_tol * lo:
        mid_g = math.sqrt(lo * hi)
        if omega(mid_g) < target_omega:
            lo = mid_g
        else:
            hi = mid_g
    return math.sqrt(lo * hi)


# ================================================================================== main routine
def calibrate(cfg: dict, verbose: bool = True) -> dict:
    """The whole procedure in the order of spec §6: sigma -> (A, k) -> stop-condition gates -> gamma."""
    p = GlftParams.from_dict(cfg["strategy"]["params"])
    cal = CalParams.from_dict(cfg["strategy"]["params"])
    tick = cfg["exchange"]["tick_size"]
    periods = cfg["data"]["periods"]
    if "in_sample" not in periods:
        raise SystemExit("config has no data.periods.in_sample: nothing to calibrate on")
    ins = periods["in_sample"]
    # Guard: refuse if in_sample touches out_of_sample (no peeking at the test set).
    if "out_of_sample" in periods:
        oos = periods["out_of_sample"]
        if not (str(ins["end"]) < str(oos["start"]) or str(oos["end"]) < str(ins["start"])):
            raise SystemExit("in_sample overlaps out_of_sample: refusing to calibrate")
    m = load_period_arrays(cfg, ins, tick)
    if verbose:
        print(f"in_sample {ins['start']}..{ins['end']}: {len(m.q_ts):,} quotes, {len(m.t_ts):,} trades", flush=True)

    # sigma on a 1 s grid (used) and a 10 s grid (noise check: a big gap means bid-ask bounce; spec §6.1 step 4).
    s1 = estimate_sigma(m, cal.cal_sigma_dt_ms, cal.cal_max_quote_silence_ms)
    s10 = estimate_sigma(m, cal.cal_sigma_check_dt_ms, cal.cal_max_quote_silence_ms)
    noise_rel = abs(s1.sigma - s10.sigma) / s1.sigma

    curve = lambda_curve(m, p, cal)
    _, _, id_gaps, long_silences = trade_gap_ranges(m, int(cal.cal_max_trade_silence_ms * NS_PER_MS))
    comb, ols, fb, fa, lam, keep = fit_curve(curve, curve.hits_bid, curve.hits_ask, curve.n_valid, cal, tick)
    seed = cfg["run"]["seed"]
    boot = bootstrap(curve, cal, tick, seed)
    # Grid-size sensitivity (spec §9 H2), information only: if k moves a lot the fit depends on the grid.
    variants = {}
    for ms in (100.0, 1000.0):
        cv = lambda_curve(m, p, cal, ms)
        f, *_ = fit_curve(cv, cv.hits_bid, cv.hits_ask, cv.n_valid, cal, tick)
        variants[f"dt_{int(ms)}ms"] = {"A": f.A, "k": f.k, "r2": f.r2, "range_end_ticks": f.range_end_ticks}

    # Stop conditions (spec §6.2 step 9): reported, exit code 2; we never quietly backtest a bad fit.
    # (In f-strings, {x:.3g} = 3 significant digits and {x:.0%} = percent.)
    stops: list[str] = []
    if not comb.ok:
        stops.append("fit failed: fewer than 2 distance bins have enough hits")
    else:
        if comb.k <= 0:
            stops.append(f"k <= 0 (k={comb.k:.4g})")
        if comb.range_end_ticks < cal.cal_min_fit_end_ticks:
            stops.append(f"fit range ends at {comb.range_end_ticks:g} ticks < {cal.cal_min_fit_end_ticks:g}")
        if not comb.r2 >= cal.cal_min_r2:
            stops.append(f"weighted R^2 {comb.r2:.3f} < {cal.cal_min_r2}")
        if fb.ok and fa.ok:
            rel = abs(fb.k - fa.k) / (0.5 * (fb.k + fa.k))
            if rel > cal.cal_max_k_side_diff:
                stops.append(f"per-side k differ by {rel:.0%} (bid {fb.k:.3g}, ask {fa.k:.3g}) > {cal.cal_max_k_side_diff:.0%}")
        else:
            stops.append("per-side fit failed (a side has too few bins with enough hits)")

    # gamma is not guessed: solve it so the skew at max inventory Q equals skew_at_Q_ticks (spec §6.4),
    # with the fee c taken at the mean in-sample mid. Only meaningful when the fit is usable.
    result_gamma, gamma_variants = None, {}
    if comb.ok and comb.k > 0:
        p_cal = GlftParams.from_dict({**cfg["strategy"]["params"], "sigma": s1.sigma, "A": comb.A, "k": comb.k})
        for label, ticks in (("base", p.skew_at_Q_ticks), ("skew_10", 10.0), ("skew_50", 50.0)):
            target_omega = ticks * tick / p.max_inventory
            try:
                gamma_variants[label] = solve_gamma(p_cal, cal, target_omega, s1.mean_mid)
            except ValueError as e:
                stops.append(f"gamma solve failed for skew {ticks:g} ticks: {e}")
        result_gamma = gamma_variants.get("base")
    out = {
        "status": "STOP" if stops else "ok",
        "stop_conditions": stops,
        "params": {"sigma": s1.sigma, "A": comb.A, "k": comb.k, "gamma": result_gamma},
        "gamma_for_variants": gamma_variants,
        "seed": seed, "in_sample": {"start": str(ins["start"]), "end": str(ins["end"])},
        "diagnostics": {
            "n_quotes": int(len(m.q_ts)), "n_trades": int(len(m.t_ts)),
            "sigma_1s": s1.sigma, "sigma_10s": s10.sigma, "sigma_noise_rel_diff": noise_rel,
            "sigma_noise_flag": noise_rel > cal.cal_noise_tol, "sigma_hourly": s1.hourly,
            "mean_mid": s1.mean_mid, "fee_per_eth_at_mean_mid": fee_per_eth(p.maker_fee, s1.mean_mid),
            "lambda_n_valid_steps": curve.n_valid, "lambda_dt_ms": cal.cal_lambda_dt_ms,
            "trade_gaps": {"id_gaps": id_gaps, "long_silences": long_silences,
                           "steps_masked": curve.n_trade_masked,
                           "masked_fraction": curve.n_trade_masked / curve.n_steps},
            "fit_weighted": comb.__dict__, "fit_ols": ols.__dict__,
            "fit_bid_side": fb.__dict__, "fit_ask_side": fa.__dict__,
            "bootstrap_hour_blocks": boot, "grid_variants": variants,
        },
    }
    if result_gamma is not None:
        # Derived numbers for the summary: fee c, c1, c2, omega, half-spread h, model fill rate A*exp(-k*h) (L1 §2).
        p_final = GlftParams.from_dict({**cfg["strategy"]["params"], **out["params"]})
        cf = closed_form(p_final, s1.mean_mid)
        out["derived"] = {"c_usdt": cf.c, "c_ticks": cf.c / tick, "c1": cf.c1, "c2": cf.c2, "omega_usdt_per_eth": cf.omega,
                          "omega_ticks_per_lot": cf.omega * p.order_qty / tick, "h_usdt": cf.h,
                          "h_ticks": cf.h / tick, "A_eff": cf.a_use,
                          "lambda_at_h_per_s": comb.A * math.exp(-comb.k * cf.h)}
    # Per-bin table for the CSV (zip walks several lists in parallel).
    out["_table"] = [{"delta_ticks": float(d), "hits_bid": int(b), "hits_ask": int(a), "lambda_per_s": float(l),
                      "used_in_fit": bool(k)} for d, b, a, l, k in
                     zip(curve.delta_ticks, curve.hits_bid, curve.hits_ask, lam, keep)]
    return out


def print_summary(r: dict) -> None:
    """Human-readable summary of the result dict."""
    d, par = r["diagnostics"], r["params"]
    f = d["fit_weighted"]
    print("\n=== GLFT calibration (in_sample only) ===")
    print(f"sigma  1 s = {d['sigma_1s']:.5f} USDT/sqrt(s)   10 s = {d['sigma_10s']:.5f}   "
          f"diff {d['sigma_noise_rel_diff']:.1%}" + ("   <-- NOISE FLAG (>20%)" if d["sigma_noise_flag"] else ""))
    hr = d["sigma_hourly"]
    print("hourly sigma: " + " ".join(f"{h:02d}:{v:.3f}" for h, v in sorted(hr.items())))
    print(f"mean mid {d['mean_mid']:.2f}  ->  fee c = {d['fee_per_eth_at_mean_mid']:.4f} USDT "
          f"({d['fee_per_eth_at_mean_mid'] / 0.01:.1f} ticks)")
    print(f"lambda fit (WLS, {d['lambda_n_valid_steps']:,} valid steps): A = {f['A']:.4g} /s  k = {f['k']:.4g} /USDT "
          f"({f['k'] * 0.01:.4g} /tick)  R^2 = {f['r2']:.3f}  bins = {f['n_bins']}  range ends {f['range_end_ticks']} ticks")
    g = d["trade_gaps"]
    print(f"  trade-stream gaps: {g['id_gaps']} id gaps, {g['long_silences']} silences > limit; "
          f"{g['steps_masked']} grid steps masked ({g['masked_fraction']:.4%} of time)")
    o = d["fit_ols"]
    print(f"  OLS unweighted : A = {o['A']:.4g}  k = {o['k']:.4g}  R^2 = {o['r2']:.3f}")
    print(f"  bid side only  : A = {d['fit_bid_side']['A']:.4g}  k = {d['fit_bid_side']['k']:.4g}")
    print(f"  ask side only  : A = {d['fit_ask_side']['A']:.4g}  k = {d['fit_ask_side']['k']:.4g}")
    b = d["bootstrap_hour_blocks"]
    print(f"  bootstrap (hour blocks, {b['n_ok']} ok): A 95% CI {b['A_ci95']}  k 95% CI {b['k_ci95']}")
    for name, v in d["grid_variants"].items():
        print(f"  grid variant {name}: A = {v['A']:.4g}  k = {v['k']:.4g}  R^2 = {v['r2']:.3f}")
    if par["gamma"] is not None:
        print(f"gamma (skew at Q = target): {par['gamma']:.5g} /USDT   variants: "
              + ", ".join(f"{k}={v:.4g}" for k, v in r["gamma_for_variants"].items()))
        dv = r["derived"]
        print(f"omega = {dv['omega_usdt_per_eth']:.4f} USDT/ETH = {dv['omega_ticks_per_lot']:.3f} ticks per lot;  "
              f"c1 = {dv['c1']:.4f}  h = {dv['h_usdt']:.4f} USDT ({dv['h_ticks']:.1f} ticks);  "
              f"model fill rate at h: {dv['lambda_at_h_per_s']:.4g} /s per side")
    print("\nSTATUS: " + r["status"])
    for s in r["stop_conditions"]:
        print("  STOP CONDITION: " + s)
    if r["stop_conditions"]:
        print("  -> do NOT backtest on these numbers; send them back to the quant-researcher (spec §12 item 1).")


def write_outputs(r: dict, out_yaml: Path, report_dir: Path, config_path: str) -> None:
    """Write the calibrated parameters (YAML) and diagnostics (JSON, CSV)."""
    out_yaml.parent.mkdir(parents=True, exist_ok=True)     # create the folder(s); fine if they exist
    report_dir.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in r.items() if k != "_table"}
    body["source"] = {"config": config_path, "written_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "note": "paste params into strategy.params of the backtest YAML; "
                              "if status is STOP the numbers are NOT usable"}
    # "with open(...) as fh" closes the file at the end of the block, even after an error. The json round trip
    # turns numpy numbers into plain Python ones so yaml can store them.
    with open(out_yaml, "w", encoding="utf-8") as fh:
        yaml.safe_dump(json.loads(json.dumps(body, default=float)), fh, sort_keys=False)
    (report_dir / "calibration.json").write_text(json.dumps(body, indent=2, default=float), encoding="utf-8")
    with open(report_dir / "fit_table.csv", "w", newline="", encoding="utf-8") as fh:   # newline="": csv module on Windows
        w = csv.DictWriter(fh, fieldnames=list(r["_table"][0]))
        w.writeheader()
        w.writerows(r["_table"])


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. argv=None makes argparse read the real command line (tests may pass a list)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="backtest YAML (its in_sample period is used)")
    ap.add_argument("--out", default="config/strategies/glft_calibrated.yaml", help="calibrated params YAML")
    ap.add_argument("--report-dir", default=None, help="diagnostics folder (default reports/<date>_glft_perp_calibration)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    r = calibrate(cfg)
    print_summary(r)
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    rep = Path(a.report_dir) if a.report_dir else PROJECT_ROOT / "reports" / f"{day}_glft_perp_calibration"
    out = Path(a.out) if Path(a.out).is_absolute() else PROJECT_ROOT / a.out
    write_outputs(r, out, rep, a.config)
    print(f"\nwrote {out}\nwrote {rep}")
    return 2 if r["status"] == "STOP" else 0      # 0 ok, 2 = a stop condition tripped


# True only when run as `python -m mm.strategies.glft_calibrate`, not when imported.
if __name__ == "__main__":
    sys.exit(main())
