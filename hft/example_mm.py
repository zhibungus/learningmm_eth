"""hftbacktest's own style of simple market maker, adapted to our tick/lot.  Learning example only.

SOURCE.  Pattern from the hftbacktest documentation tutorials "Getting Started" and "High-Frequency Grid
Trading" (hftbacktest.readthedocs.io): an ``@njit`` strategy function with a
``while hbt.elapse(...) == 0`` loop, ``recorder.record(hbt)``, a scan of ``hbt.orders(0)`` to cancel stale
orders and submit missing ones.  I could not fetch those pages from this sandbox, so the strategy is
rewritten from memory of that pattern; EVERY API call used here was checked against the installed 2.4.4
source (binding.py / order.py / recorder.py), not against the docs.

STRATEGY (deliberately naive, nothing to do with GLFT)
    reservation = mid - skew_ticks * tick * (position / max_position)       (long -> quote lower)
    bid = floor_tick(reservation - half_spread), ask = ceil_tick(reservation + half_spread)
    never post through the touch (bid <= best bid, ask >= best ask); quote a side only if one more
    order_qty would not push |position| past max_position.  One order per side, GTX (post-only) LIMIT.
    Every ``step`` of local time: cancel orders whose price is no longer the target, submit missing ones.

Run:  .venv-hft\\Scripts\\python.exe -m hft.run_backtest --strategy example --synthetic --tag demo
"""
from __future__ import annotations

import math

import numpy as np
from numba import njit

from hftbacktest import BUY, GTX, LIMIT, NEW, Recorder
from hftbacktest.order import PARTIALLY_FILLED

NS_PER_MS = 1_000_000

# Strategy settings of the example (a "recipe", edit freely).  Units: ticks, ETH.
EXAMPLE_PARAMS = {
    "half_spread_ticks": 10.0,   # distance of each quote from the reservation price
    "skew_ticks": 20.0,          # extra shift (ticks) when position == +/- max_position
    "order_qty": 0.01,           # ETH per order
    "max_position": 0.05,        # ETH
    "step_ms": 1000,             # how often we re-evaluate (local time)
    "record_every": 1,           # record into the Recorder every N steps
}
EPS = 1e-9


@njit
def simple_mm(hbt, recorder, half_spread_ticks, skew_ticks, order_qty, max_position, tick, step_ns, record_every):
    """The strategy loop.  ``@njit`` = compile to machine code at first call (types come from the arguments).

    Everything inside must be numba-friendly: scalars, numpy arrays, and the ``hbt`` / ``recorder`` jitclass
    objects.  No dicts of strings, no f-strings, no Python classes."""
    next_id = 1                      # order ids must be unique; a counter is the simplest source
    step = 0
    while hbt.elapse(step_ns) == 0:      # 0 = time advanced; 1 = end of data
        step += 1
        if step % record_every == 0:
            recorder.record(hbt)
        hbt.clear_inactive_orders(0)      # forget filled / cancelled orders so the dict stays small
        depth = hbt.depth(0)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        if not (best_bid == best_bid and best_ask == best_ask) or best_bid <= 0.0 or best_ask <= best_bid:
            continue                      # NaN (empty side) or crossed book: skip this step
        mid = (best_bid + best_ask) * 0.5
        pos = hbt.position(0)
        reservation = mid - skew_ticks * tick * (pos / max_position)
        bid_px = min(math.floor((reservation - half_spread_ticks * tick) / tick + EPS) * tick, best_bid)
        ask_px = max(math.ceil((reservation + half_spread_ticks * tick) / tick - EPS) * tick, best_ask)
        bid_px = round(bid_px, 10)        # clean float noise (e.g. 2499.3900000000003)
        ask_px = round(ask_px, 10)
        want_bid = pos + order_qty <= max_position + EPS
        want_ask = pos - order_qty >= -max_position - EPS

        have_bid = False
        have_ask = False
        vals = hbt.orders(0).values()     # numba cannot iterate a dict-like with ``for``; use has_next()/get()
        while vals.has_next():
            o = vals.get()
            # status NONE (0) = submitted but not yet acknowledged: still live (skipping it made the first
            # version stack duplicate orders while acks were in flight)
            if o.status != NEW and o.status != PARTIALLY_FILLED and o.status != 0:
                continue                  # not live any more
            is_bid = o.side == BUY
            target = bid_px if is_bid else ask_px
            wanted = want_bid if is_bid else want_ask
            if wanted and abs(o.price - target) < tick * 0.5:
                keep = True               # already at the target price
            else:
                keep = False
                if o.cancellable:         # active and no request in flight
                    hbt.cancel(0, o.order_id, False)
            # A mismatched order still occupies its side until the cancel is confirmed, so do not stack another.
            if is_bid:
                have_bid = True
            else:
                have_ask = True
        if want_bid and not have_bid:
            hbt.submit_buy_order(0, next_id, bid_px, order_qty, GTX, LIMIT, False)
            next_id += 1
        if want_ask and not have_ask:
            hbt.submit_sell_order(0, next_id, ask_px, order_qty, GTX, LIMIT, False)
            next_id += 1
    return step


def run_example(hbt, params: dict | None = None, *, max_steps: int, tick: float = 0.01):
    """Python wrapper: build the Recorder, run the njit loop, close the backtester.

    Returns {"recorder": record_dtype array (timestamp, price=mid, position, balance, fee, num_trades,
    trading_volume, trading_value), "steps": int}."""
    p = dict(EXAMPLE_PARAMS)
    p.update(params or {})
    step = int(p["step_ms"] * NS_PER_MS)
    rec = Recorder(1, max_steps // int(p["record_every"]) + 10)   # raises IndexError inside njit if too small
    steps = simple_mm(hbt, rec.recorder, p["half_spread_ticks"], p["skew_ticks"], p["order_qty"],
                      p["max_position"], tick, step, int(p["record_every"]))
    hbt.close()                       # always close: releases the Rust side and flushes
    return {"recorder": rec.get(0).copy(), "steps": int(steps), "params": p}
