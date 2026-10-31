"""GLFT market maker as a numba ``@njit`` function for hftbacktest 2.4.4.

Same formulas and parameter VALUES as our own strategy ``src/mm/strategies/glft.py`` (spec
``specs/glft-perp.md`` sections 4.1, 4.2, 4.4, 4.5, 4.6), so the two engines can be compared on the same
data.  Parameters are loaded at runtime (read-only) from ``config/backtest/glft_live.yaml``
(``strategy.params`` + ``exchange`` + ``latency.timer_interval_ms``); nothing is hard-coded here.

Usage
-----
    from hft.glft_hft import load_params_yaml, run_glft
    params = load_params_yaml("config/backtest/glft_live.yaml")
    res = run_glft(asset, params, max_cycles=...)       # asset from hft.common.build_asset

or through ``python -m hft.run_backtest --strategy glft ...`` (the normal way).

What is the same as our engine and what is not (v1, read this before comparing numbers)
---------------------------------------------------------------------------------------
SAME : closed form (c, A_use, c1, c2, omega, h), delta_min floor, tick rounding (bid down / ask up),
       no-cross guard against the last seen touch, headroom rule (do not quote a side that would push
       |q| past Q; size min(Delta, headroom) rounded DOWN to the lot), requote rule (keep the live order
       unless |new - live| >= requote_ticks, with min_requote_interval and the urgent override),
       post-only (GTX) limit orders, quote cycle every ``timer_interval_ms`` of local time, an extra
       immediate cycle after a fill (spec section 5 on_fill, ignores min_requote_interval).
DIFFERENT (simplifications, documented rather than hidden):
  1. Requote = ``hbt.modify(price)`` for an UNTOUCHED order (same qty), cancel + new otherwise.  hftbacktest
     does support ``modify`` in the backtester, but a QTY change through modify was NOT honoured by the
     partial-fill exchange in our probe (local qty changed, exchange filled the old size), so size changes
     (shrink to headroom, partially filled orders) use cancel, then a new order on a later cycle.
     Our engine's ReplaceOrder sets total size; there is no exact equivalent.
  2. No risk kill-switch, vol pause, spread pause, stale-quote check, drawdown stop or order-rate guard.
     Only: inventory cap, empty/crossed/NaN book -> cancel and do not quote, reject cooldown.
  3. Min-notional (20 USDT) is not checked; only qty >= min_qty (0.001).
  4. Fills are detected by polling the order dictionary every ``step_ns`` (default 100 ms), not from a
     callback: hftbacktest has no on_fill hook.  The fill TIME is the order's ``exch_timestamp`` (the
     exchange time of the fill), the fill price is ``exec_price`` (a maker fill is at our limit price).
     Several partial fills of one order between two polls are merged into one row (price is the order's).
  5. Quote cycle runs on a timer only (plus after fills), not on every quote event.

Numba restrictions you will meet below (CLAUDE.md: explain where they bite)
---------------------------------------------------------------------------
* ``@njit`` compiles a function to machine code the FIRST time it is called with a given set of argument
  TYPES (it infers them from the actual arguments).  That first call is slow (seconds); later calls are fast.
* Inside ``@njit`` there are no Python objects: no dict/list of mixed types, no classes (except numba
  ``jitclass`` like hftbacktest's ``hbt``), no f-strings or ``.format``, no exceptions with formatted
  messages, and ``print`` only accepts simple values.  So parameters travel as a float64 numpy ARRAY
  indexed by module-level integer constants (P_SIGMA, ...), and results are written into preallocated
  numpy arrays that the caller reads after the run.
* ``hbt`` must be the object returned by ``ROIVectorMarketDepthBacktest(...)`` (a numba jitclass);
  plain Python wrappers cannot be passed in.
* Module-level constants are read at COMPILE time and frozen; changing them later has no effect until
  the function is recompiled.
* A ``None`` check (``if o is None``) is how numba handles "maybe missing" values (Optional types).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import yaml
from numba import njit

from hftbacktest import BUY, SELL, GTX, LIMIT, FILLED, CANCELED, NEW, Recorder
from hftbacktest.order import EXPIRED, PARTIALLY_FILLED, REJECTED

NS_PER_MS = 1_000_000

# ====================================================================================== parameters
# Index map of the float64 parameter array P (numba cannot take a dict, so a named index is the next best
# thing).  Order is arbitrary but must match PARAM_NAMES in ``params_to_array``.
(P_SIGMA, P_A, P_K, P_GAMMA, P_XI_EQ_GAMMA, P_FEE_EXACT, P_SKEW_OFF, P_MAKER_FEE, P_QTY, P_QMAX,
 P_MIN_QTY, P_MIN_HALF, P_REQ, P_URG, P_MIN_INT_NS, P_COOLDOWN_NS, P_TICK, P_LOT, P_GAP_CD_NS, P_CANCEL_NEW, N_PARAMS) = range(21)

# Float-noise guard, same value as ``_EPS`` in src/mm/strategies/glft.py (99.99999999 ticks == 100).
EPS = 1e-9
# A freshly submitted order has status NONE (0) with req = NEW until the exchange's ack arrives (verified by
# tracing order ids).  It is NOT terminal: treating it as such made the first version forget the order and
# quote a second one (order leak, exposure above Q, up to 22 live bids).  We call that state PENDING_NEW.
PENDING_NEW = 0
# Rounding digits of the final price, same as ``round(..., 10)`` in glft.py's floor_to_tick/ceil_to_tick.
PRICE_DIGITS = 10

# Indices into the integer counter array returned by the run.
(S_NEW, S_CANCEL, S_MODIFY, S_EXPIRED, S_CYCLES, S_BOOK_SKIP, S_FILL_ROWS, S_SUBMIT_ERR, S_END_CODE, S_ERRORS, S_FIRST_ERR_CODE, S_FIRST_ERR_STEP, N_STATS) = range(13)

# Structured (record) dtypes for the logs.  int64 columns keep exact nanoseconds (float64 would lose
# precision above 2**53, and timestamps are ~1.7e18).
MID_DTYPE = np.dtype([("local_ts", "i8"), ("feed_exch_ts", "i8"), ("mid", "f8"), ("position", "f8"),
                      ("balance", "f8"), ("fee", "f8"), ("n_live_bid", "i8"), ("n_live_ask", "i8")], align=True)
FILL_DTYPE = np.dtype([("exch_ts", "i8"), ("poll_ts", "i8"), ("order_id", "i8"), ("side", "i8"),
                       ("px", "f8"), ("qty", "f8")], align=True)


# One row per quote cycle that was allowed to quote: the TARGET prices (before the requote rule decides
# whether to move the live order).  Used to check that quotes sit >= min_half_spread_ticks from mid.
QUOTE_DTYPE = np.dtype([("ts", "i8"), ("mid", "f8"), ("bid", "f8"), ("ask", "f8"), ("position", "f8")], align=True)


def load_params_yaml(path: str | Path) -> dict:
    """Read the strategy settings from a backtest YAML (READ-ONLY use of our config).

    Returns a flat dict with the glft params plus tick/lot and the timer interval, and ``source``."""
    with open(path, "r", encoding="utf-8") as f:       # context manager: closes the file automatically
        cfg = yaml.safe_load(f)                         # safe_load: plain data only, resolves the &anchors
    p = dict(cfg["strategy"]["params"])
    p = {k: v for k, v in p.items() if not k.startswith("cal_")}   # calibration-only keys are not needed
    p["tick_size"] = cfg["exchange"]["tick_size"]
    p["lot_size"] = cfg["exchange"]["lot_size"]
    p["timer_interval_ms"] = cfg["latency"]["timer_interval_ms"]
    p["seed"] = cfg.get("run", {}).get("seed")
    p["source"] = str(path)
    return p


def params_to_array(p: dict) -> np.ndarray:
    """Pack the dict into the float64 array P that the njit code reads (booleans become 1.0 / 0.0)."""
    P = np.zeros(N_PARAMS, dtype=np.float64)
    P[P_SIGMA], P[P_A], P[P_K], P[P_GAMMA] = p["sigma"], p["A"], p["k"], p["gamma"]
    P[P_XI_EQ_GAMMA] = 1.0 if p["xi_equals_gamma"] else 0.0
    if p["fee_mode"] not in ("exact", "shift"):
        raise ValueError("fee_mode must be 'exact' or 'shift'")
    P[P_FEE_EXACT] = 1.0 if p["fee_mode"] == "exact" else 0.0
    P[P_SKEW_OFF] = 1.0 if p["skew_off"] else 0.0
    P[P_MAKER_FEE], P[P_QTY], P[P_QMAX] = p["maker_fee"], p["order_qty"], p["max_inventory"]
    P[P_MIN_QTY], P[P_MIN_HALF] = p["min_qty"], p["min_half_spread_ticks"]
    P[P_REQ], P[P_URG] = p["requote_ticks"], p["urgent_requote_ticks"]
    P[P_MIN_INT_NS] = p["min_requote_interval_ms"] * NS_PER_MS
    P[P_COOLDOWN_NS] = p["reject_cooldown_ms"] * NS_PER_MS
    P[P_TICK], P[P_LOT] = p["tick_size"], p["lot_size"]
    P[P_GAP_CD_NS] = p["gap_cooldown_ms"] * NS_PER_MS
    P[P_CANCEL_NEW] = 1.0 if p.get("requote_mode", "modify") == "cancel_new" else 0.0   # see side_cycle
    return P


# ================================================================================= pure maths (njit)
# These mirror glft.py function by function; check_glft_math.py proves they agree to 1e-9.
@njit
def fee_per_eth(P, mid):
    """c = f_m * m, the maker fee per ETH in USDT (spec 4.4 step 1)."""
    return P[P_MAKER_FEE] * mid


@njit
def xi_of(P):
    """xi = gamma (Model A) or 0 (spec 4.1)."""
    return P[P_GAMMA] if P[P_XI_EQ_GAMMA] > 0.5 else 0.0


@njit
def a_use(P, c):
    """Fill-intensity scale: A*exp(-k c) in exact fee mode, A in shift mode (spec 4.2)."""
    return P[P_A] * math.exp(-P[P_K] * c) if P[P_FEE_EXACT] > 0.5 else P[P_A]


@njit
def c1_static(P):
    """c1 = ln(1 + xi*Delta/k) / (xi*Delta); the xi -> 0 limit is 1/k (spec 4.1)."""
    xi = xi_of(P)
    if xi == 0.0:
        return 1.0 / P[P_K]
    return math.log1p(xi * P[P_QTY] / P[P_K]) / (xi * P[P_QTY])


@njit
def c2_skew(P, a):
    """c2 = sqrt( gamma/(2 a Delta k) * (1 + xi Delta/k)^(k/(xi Delta) + 1) ); power term = e when xi = 0."""
    xi = xi_of(P)
    if xi == 0.0:
        power = math.e
    else:
        x = xi * P[P_QTY] / P[P_K]
        # exp((1/x + 1) * ln(1+x)) instead of (1+x)**(1/x+1): same value, no overflow for tiny x.
        power = math.exp((1.0 / x + 1.0) * math.log1p(x))
    return math.sqrt(P[P_GAMMA] / (2.0 * a * P[P_QTY] * P[P_K]) * power)


@njit
def closed_form(P, mid):
    """Spec 4.4 steps 1-4.  Returns the tuple (c, A_use, c1, c2, omega, h)."""
    c = fee_per_eth(P, mid)
    a = a_use(P, c)
    c1 = c1_static(P)
    c2 = c2_skew(P, a)
    omega = P[P_SIGMA] * c2
    h = c + c1 + 0.5 * P[P_QTY] * omega
    return c, a, c1, c2, omega, h


@njit
def floor_to_tick(x, tick):
    """Round DOWN to the tick grid (bids).  ``round(v, 10)`` mirrors glft.py's float clean-up."""
    return round(math.floor(x / tick + EPS) * tick, PRICE_DIGITS)


@njit
def ceil_to_tick(x, tick):
    """Round UP to the tick grid (asks)."""
    return round(math.ceil(x / tick - EPS) * tick, PRICE_DIGITS)


@njit
def compute_quotes(P, mid, q, best_bid, best_ask):
    """Spec 4.4 steps 1-7.  Returns (bid_px, ask_px, delta_b, delta_a, omega, h).

    best_bid / best_ask must be finite (the caller skips the cycle on an empty book; glft.py allows None)."""
    tick = P[P_TICK]
    c, a, c1, c2, omega, h = closed_form(P, mid)
    skew = 0.0 if P[P_SKEW_OFF] > 0.5 else q * omega        # long (q>0) pushes both quotes down
    d_min = P[P_MIN_HALF] * tick                              # step 5 floor
    delta_b = max(h + skew, d_min)
    delta_a = max(h - skew, d_min)
    bid = floor_to_tick(mid - delta_b, tick)                  # step 6
    ask = ceil_to_tick(mid + delta_a, tick)
    bid = min(bid, round(best_ask - tick, PRICE_DIGITS))      # step 7: never at/through the last seen touch
    ask = max(ask, round(best_bid + tick, PRICE_DIGITS))
    return bid, ask, delta_b, delta_a, omega, h


@njit
def floor_to_lot(x, lot):
    """Round a quantity DOWN to the lot (state.round_qty in our engine)."""
    return round(math.floor(x / lot + EPS) * lot, PRICE_DIGITS)


@njit
def last_feed_exch_ts(hbt):
    """Exchange timestamp of the latest feed row received, 0 if none yet.

    ``hbt.feed_latency(0)`` returns an Optional tuple (exch_ts, local_ts) or None.  Numba cannot index an
    Optional directly (``fl[0]`` fails to compile: "No definition for lowering static_getitem(Optional...)"),
    but it CAN unpack it after an explicit None test, which is what this helper does."""
    fl = hbt.feed_latency(0)
    if fl is None:
        return 0
    exch_ts, local_ts = fl
    return exch_ts


# ============================================================================ order handling (njit)
@njit
def process_orders(hbt, now, seen_cum, seen_final, live_id, cooldown_until, cooldown_ns,
                   fill_log, n_fill, stats, expo):
    """Sweep the order dictionary: log new fills, count rejects, then drop finished orders.

    hftbacktest has no fill callback.  Instead each Order holds ``qty``, ``leaves_qty`` (what is still
    open) and ``status``.  Cumulative filled = qty - leaves_qty (or qty if FILLED).  New fill qty = that
    minus what we logged before (``seen_cum``).  Returns the new number of fill rows and the count of
    fills found in THIS sweep (used to trigger an immediate quote cycle)."""
    expo[0] = 0.0
    expo[1] = 0.0
    expo[2] = 0.0
    expo[3] = 0.0
    orders = hbt.orders(0)
    vals = orders.values()                    # numba cannot use ``for o in orders``; use has_next()/get()
    found = 0
    while vals.has_next():
        o = vals.get()
        oid = o.order_id
        st = o.status
        if st == FILLED:
            cum = o.qty
        elif st == NEW or st == PARTIALLY_FILLED or st == CANCELED:
            cum = o.qty - o.leaves_qty
        else:
            cum = 0.0
        if cum > seen_cum[oid] + 1e-12 and n_fill < len(fill_log):
            fill_log[n_fill].exch_ts = o.exch_timestamp
            fill_log[n_fill].poll_ts = now
            fill_log[n_fill].order_id = oid
            fill_log[n_fill].side = o.side
            fill_log[n_fill].px = o.exec_price
            fill_log[n_fill].qty = cum - seen_cum[oid]
            seen_cum[oid] = cum
            n_fill += 1
            found += 1
        # Exposure of every NON-TERMINAL order (spec 4.5): count and open quantity per side.  An order with a
        # cancel in flight still counts, because it can fill before the cancel reaches the exchange.
        if st == NEW or st == PARTIALLY_FILLED or st == PENDING_NEW:
            k = 0 if o.side == BUY else 1
            expo[k] += 1.0
            expo[2 + k] += (o.qty if st == PENDING_NEW else o.leaves_qty)
        # A post-only order that would have crossed is EXPIRED (or REJECTED) by the exchange: count it once
        # and keep that side quiet for reject_cooldown_ms (spec 4.6 "Rejects").
        if (st == EXPIRED or st == REJECTED) and seen_final[oid] == 0:
            seen_final[oid] = 1
            stats[S_EXPIRED] += 1
            s = 0 if o.side == BUY else 1
            cooldown_until[s] = now + cooldown_ns
    # Remove FILLED / CANCELED / EXPIRED orders from the local dict (keeps the dict tiny and fast).
    hbt.clear_inactive_orders(0)
    return n_fill, found


@njit
def side_cycle(hbt, P, s, now, desired_px, desired_qty, ignore_interval, live_id, last_action,
               cooldown_until, next_id, seen_cum, stats):
    """One side (s = 0 bid, 1 ask) of the quote cycle, spec 4.6.  Returns the updated next_id.

    desired_qty <= 0 means "this side must not quote" (headroom rule or invalid book)."""
    tick, lot = P[P_TICK], P[P_LOT]
    sgn = 1.0 if s == 0 else -1.0          # +1 for the bid (BUY), -1 for the ask (SELL)
    want = desired_qty >= P[P_MIN_QTY] - EPS
    oid = live_id[s]
    orders = hbt.orders(0)
    live = False
    if oid != 0:
        o = orders.get(oid)
        if o is None:
            live_id[s] = 0                  # already cleared: it finished (filled / cancelled / expired)
        elif o.status == NEW or o.status == PARTIALLY_FILLED or o.status == PENDING_NEW:
            live = True
            if o.req != 0:                  # an action on it is still in flight: do nothing (spec 4.6)
                return next_id
            if not want:
                if hbt.cancel(0, oid, False) == 0:
                    stats[S_CANCEL] += 1
                    last_action[s] = now
                return next_id
            # Order is larger than the headroom allows, or already partly filled: size cannot be changed
            # reliably with modify (see module docstring, point 1), so cancel; a new order follows later.
            if o.leaves_qty > desired_qty + lot * 0.5 or o.leaves_qty < o.qty - lot * 0.5:
                if hbt.cancel(0, oid, False) == 0:
                    stats[S_CANCEL] += 1
                    last_action[s] = now
                return next_id
            dist = (desired_px - o.price) * sgn          # >0: desired is closer to mid for a bid
            far = abs(desired_px - o.price) >= P[P_REQ] * tick - EPS
            urgent = (-dist) >= P[P_URG] * tick - EPS    # live order is too close to mid / the market
            if far and (ignore_interval or urgent or now - last_action[s] >= P[P_MIN_INT_NS]):
                if P[P_CANCEL_NEW] > 0.5:
                    # cancel now; the replacement order is placed by a later cycle once the cancel is confirmed
                    if hbt.cancel(0, oid, False) == 0:
                        stats[S_CANCEL] += 1
                        last_action[s] = now
                elif hbt.modify(0, oid, desired_px, o.qty, False) == 0:
                    stats[S_MODIFY] += 1
                    last_action[s] = now
            return next_id
        else:
            live_id[s] = 0                  # terminal status that has not been swept yet
    if (not live) and want and now >= cooldown_until[s] and next_id < len(seen_cum):
        new_id = next_id
        if s == 0:
            r = hbt.submit_buy_order(0, new_id, desired_px, desired_qty, GTX, LIMIT, False)
        else:
            r = hbt.submit_sell_order(0, new_id, desired_px, desired_qty, GTX, LIMIT, False)
        if r == 0:
            live_id[s] = new_id
            last_action[s] = now
            stats[S_NEW] += 1
            next_id += 1
        else:
            stats[S_SUBMIT_ERR] += 1
    return next_id


@njit
def glft_mm(hbt, recorder, P, step_ns, cycle_every, mid_log, fill_log, quote_log, gaps, seen_cum, seen_final, stats,
            dbg):
    """The strategy loop.  Runs until the data ends.  Returns (n_mid_rows, n_fill_rows).

    Every ``step_ns`` of local time: process fills, log the mid.  Every ``cycle_every`` steps (or right
    after a fill) run the quote cycle.  ``hbt.elapse(ns)`` advances the simulated clock; it returns 0
    while there is data left and 1 at the end."""
    tick, lot, Q, delta = P[P_TICK], P[P_LOT], P[P_QMAX], P[P_QTY]
    live_id = np.zeros(2, dtype=np.int64)            # live order id per side (0 = none); 0=bid, 1=ask
    last_action = np.full(2, -(1 << 62), dtype=np.int64)   # local time of our last place/modify per side
    cooldown_until = np.zeros(2, dtype=np.int64)
    next_id = 1
    n_mid = 0
    n_fill = 0
    n_quote = 0
    expo = np.zeros(4, dtype=np.float64)              # [live buys, live sells, open buy qty, open sell qty]
    valid_since = -1                                  # local time since which the book has been valid (-1 = not valid)
    step = 0
    while True:
        rc = hbt.elapse(step_ns)
        if rc != 0:                       # 1 = end of data; other codes = engine errors (unrecoverable, see docstring)
            stats[S_END_CODE] = rc
            stats[S_FIRST_ERR_STEP] = step
            # Error exit: dump the open orders (id, status, req, side, price, qty, leaves, exch_ts, local_ts, now)
            # so the cause can be read off after the run.
            vals_dbg = hbt.orders(0).values()
            k = 0
            while vals_dbg.has_next() and k < len(dbg):
                od = vals_dbg.get()
                dbg[k, 0] = od.order_id
                dbg[k, 1] = od.status
                dbg[k, 2] = od.req
                dbg[k, 3] = od.side
                dbg[k, 4] = od.price
                dbg[k, 5] = od.qty
                dbg[k, 6] = od.leaves_qty
                dbg[k, 7] = od.exch_timestamp
                dbg[k, 8] = od.local_timestamp
                dbg[k, 9] = hbt.current_timestamp
                k += 1
            break
        if rc != 0:                       # engine error: count it, remember the first, and TRY TO CONTINUE
            stats[S_ERRORS] += 1
            if stats[S_ERRORS] == 1:
                stats[S_FIRST_ERR_CODE] = rc
                stats[S_FIRST_ERR_STEP] = step
        step += 1
        now = hbt.current_timestamp
        # 1) fills and rejects since the last step
        n_fill, found = process_orders(hbt, now, seen_cum, seen_final, live_id, cooldown_until,
                                       int(P[P_COOLDOWN_NS]), fill_log, n_fill, stats, expo)
        stats[S_FILL_ROWS] = n_fill
        # 2) market snapshot
        depth = hbt.depth(0)
        bb = depth.best_bid
        ba = depth.best_ask
        valid = (bb == bb) and (ba == ba) and bb > 0.0 and ba > bb       # x == x is False only for NaN
        mid = (bb + ba) * 0.5 if valid else np.nan
        pos = hbt.position(0)
        sv = hbt.state_values(0)
        feed_exch = last_feed_exch_ts(hbt)  # exchange time of the newest feed row we have received
        if n_mid < len(mid_log):
            mid_log[n_mid].local_ts = now
            mid_log[n_mid].feed_exch_ts = feed_exch
            mid_log[n_mid].mid = mid
            mid_log[n_mid].position = pos
            mid_log[n_mid].balance = sv.balance
            mid_log[n_mid].fee = sv.fee
            mid_log[n_mid].n_live_bid = int(expo[0])
            mid_log[n_mid].n_live_ask = int(expo[1])
            n_mid += 1
        # 3) gap handling (spec 7.2, owner decision): book empty/NaN/crossed, or inside a gaps.json interval
        #    -> cancel ALL live orders (no flatten) and stay out until the book has been valid for
        #    gap_cooldown_ms continuously.  Checked every step so the cancel is not delayed to the next cycle.
        in_gap = False
        for gi in range(len(gaps)):
            if now >= gaps[gi, 0] and now <= gaps[gi, 1]:
                in_gap = True
        valid = valid and (not in_gap)
        if not valid:
            valid_since = -1
            for s in range(2):
                next_id = side_cycle(hbt, P, s, now, 0.0, 0.0, True, live_id, last_action,
                                     cooldown_until, next_id, seen_cum, stats)
        elif valid_since < 0:
            valid_since = now
        allowed = valid and (now - valid_since >= P[P_GAP_CD_NS])
        # 4) quote cycle
        regular = found > 0 or step % cycle_every == 0
        # cancel+new requoting: as soon as a side is empty again, refill it without waiting for the next
        # 1 s cycle (not counted as a regular cycle, not recorded).
        refill = allowed and (live_id[0] == 0 or live_id[1] == 0)
        if regular or refill:
            if regular:
                stats[S_CYCLES] += 1
                recorder.record(hbt)
            ignore_interval = found > 0
            if allowed:
                bid_px, ask_px, db, da, omega, h = compute_quotes(P, mid, pos, bb, ba)
                # headroom rule (spec 4.5): our own live order is excluded because it is the one being re-priced
                own_b = 0.0
                own_a = 0.0
                if live_id[0] != 0:
                    ob = hbt.orders(0).get(live_id[0])
                    if ob is not None:
                        own_b = ob.qty if ob.status == PENDING_NEW else ob.leaves_qty
                if live_id[1] != 0:
                    oa = hbt.orders(0).get(live_id[1])
                    if oa is not None:
                        own_a = oa.qty if oa.status == PENDING_NEW else oa.leaves_qty
                # Q - q - (open buy qty of all OTHER non-terminal buy orders); likewise for sells
                head_b = Q - pos - (expo[2] - own_b)
                head_a = Q + pos - (expo[3] - own_a)
                qty_b = floor_to_lot(min(delta, head_b), lot) if head_b > 0.0 else 0.0
                qty_a = floor_to_lot(min(delta, head_a), lot) if head_a > 0.0 else 0.0
                next_id = side_cycle(hbt, P, 0, now, bid_px, qty_b, ignore_interval, live_id, last_action,
                                     cooldown_until, next_id, seen_cum, stats)
                next_id = side_cycle(hbt, P, 1, now, ask_px, qty_a, ignore_interval, live_id, last_action,
                                     cooldown_until, next_id, seen_cum, stats)
                if regular and n_quote < len(quote_log):          # remember the TARGET quotes for the sanity checks
                    quote_log[n_quote].ts = now
                    quote_log[n_quote].mid = mid
                    quote_log[n_quote].bid = bid_px
                    quote_log[n_quote].ask = ask_px
                    quote_log[n_quote].position = pos
                    n_quote += 1
            else:
                # Not allowed to quote (empty/NaN/crossed book, inside a gaps.json interval, or still in the
                # gap_cooldown_ms after one).  Live orders were already cancelled above, every step.
                stats[S_BOOK_SKIP] += 1
    return n_mid, n_fill, n_quote


# ======================================================================================== Python wrapper
def run_glft(hbt, params: dict, *, max_steps: int, step_ms: int = 100, recorder_size: int | None = None,
             gaps: np.ndarray | None = None):
    """Run the GLFT loop on a built backtester and return the raw logs.

    hbt        ``ROIVectorMarketDepthBacktest([asset])`` (closed here with ``hbt.close()``).
    max_steps  capacity of the per-step mid log; a run that needs more rows silently stops logging,
               so size it as duration / step + slack.
    step_ms    polling / mid-log period (the quote cycle period is params['timer_interval_ms']).
    Returns dict(mid_log, fill_log, stats, recorder, P)."""
    P = params_to_array(params)
    step_ns = int(step_ms * NS_PER_MS)
    cycle_every = max(1, int(round(params["timer_interval_ms"] / step_ms)))
    rec_size = recorder_size or (max_steps // cycle_every + 10_000)
    recorder = Recorder(1, rec_size)
    mid_log = np.zeros(max_steps, dtype=MID_DTYPE)
    fill_log = np.zeros(max(10_000, max_steps), dtype=FILL_DTYPE)   # generous: at most 1 fill row per step
    max_orders = 4 * (max_steps // cycle_every) + 100_000
    seen_cum = np.zeros(max_orders, dtype=np.float64)
    seen_final = np.zeros(max_orders, dtype=np.int8)
    stats = np.zeros(N_STATS, dtype=np.int64)
    quote_log = np.zeros(max_steps // cycle_every + 10_000, dtype=QUOTE_DTYPE)
    # gaps: int64 array (n, 2) of [start_local_ns, end_local_ns] intervals from gaps.json (may be empty)
    gaps = np.zeros((0, 2), dtype=np.int64) if gaps is None else np.ascontiguousarray(gaps, dtype=np.int64)
    dbg = np.zeros((16, 10), dtype=np.float64)       # open orders at an engine error exit (see glft_mm)
    n_mid, n_fill, n_quote = glft_mm(hbt, recorder.recorder, P, step_ns, cycle_every, mid_log, fill_log,
                                     quote_log, gaps, seen_cum, seen_final, stats, dbg)
    hbt.close()
    return {"mid_log": mid_log[:n_mid], "fill_log": fill_log[:n_fill], "quote_log": quote_log[:n_quote], "dbg": dbg, "stats": stats,
            "recorder": recorder.get(0).copy(), "P": P, "cycle_every": cycle_every, "step_ns": step_ns}
