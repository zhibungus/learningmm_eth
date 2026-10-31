"""Tiny SYNTHETIC market data in hftbacktest's event format, for self-tests (no real data needed).

The price follows a slow sine wave plus noise, so the book keeps moving through our resting quotes
and orders actually fill.  Everything is deterministic given the seed.

Event row layout (``hftbacktest.event_dtype``, verified in types.py):
    ev       flags + kind: DEPTH_EVENT / TRADE_EVENT / DEPTH_CLEAR_EVENT, | BUY_EVENT or SELL_EVENT,
             | EXCH_EVENT (exchange processes it at exch_ts) | LOCAL_EVENT (we receive it at local_ts)
    exch_ts  exchange timestamp, ns            local_ts  our receive timestamp, ns
    px, qty  price [USDT], new TOTAL size at that level [ETH] (qty 0 deletes the level) or trade size
    order_id, ival, fval  unused for L2 data (0)
For depth rows BUY_EVENT = bid side, SELL_EVENT = ask side.  For trade rows BUY_EVENT means the
trade INITIATOR was a buyer (it lifted the ask), SELL_EVENT that a seller hit the bid.
"""
from __future__ import annotations

import numpy as np

from hftbacktest import (BUY_EVENT, DEPTH_CLEAR_EVENT, DEPTH_EVENT, DEPTH_SNAPSHOT_EVENT, EXCH_EVENT,
                         LOCAL_EVENT, SELL_EVENT, TRADE_EVENT, event_dtype)
from hftbacktest.data.validation import validate_event_order

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000

# Scenario constants of the synthetic world (a "fixture", not strategy parameters).
SYNTH = {
    "t0_ns": 1_700_000_000 * NS_PER_S,   # arbitrary start (Nov 2023); only differences matter
    "tick": 0.01,
    "levels": 8,                         # book depth per side
    "level_qty": 3.0,                    # ETH resting at each level
    "update_ms": 100,                    # book refresh period
    "feed_latency_ns": 240 * NS_PER_MS,  # local_ts - exch_ts, like our real median
    "sine_amp": 3.0,                     # USDT swing of the mid
    "sine_period_s": 20.0,
    "noise": 0.05,                       # USDT std of random wiggle
    "trade_qty": 0.5,                    # ETH per synthetic trade print
}


def _book(mid: float, tick: float, levels: int, qty: float):
    """Bid and ask level dicts {price_tick: qty} around ``mid`` (best bid just below, best ask just above)."""
    mid_tick = int(np.floor(mid / tick))
    bids = {mid_tick - i: qty for i in range(levels)}          # dict comprehension
    asks = {mid_tick + 1 + i: qty for i in range(levels)}
    return bids, asks


def _row(ev, ts, px, qty):
    """One event tuple in event_dtype column order (feed latency is added to local_ts)."""
    return (ev | EXCH_EVENT | LOCAL_EVENT, ts, ts + SYNTH["feed_latency_ns"], px, qty, 0, 0, 0.0)


def make_synthetic(seconds: float = 120.0, seed: int = 7, start_mid: float = 2500.005,
                   clear_at_s: float | None = None, clear_for_s: float = 3.0):
    """Return (snapshot_array, events_array).

    clear_at_s  if given, emit DEPTH_CLEAR_EVENT rows at that time and rebuild the book ``clear_for_s``
                later: tests that the strategy stops quoting on an empty book (like a recording gap)."""
    rng = np.random.default_rng(seed)
    tick, levels, lq = SYNTH["tick"], SYNTH["levels"], SYNTH["level_qty"]
    t0 = SYNTH["t0_ns"]
    step_ns = SYNTH["update_ms"] * NS_PER_MS

    # ---- initial snapshot: rows with ev = DEPTH_SNAPSHOT_EVENT | side, timestamps 0 (mimics depth.snapshot()).
    bids, asks = _book(start_mid, tick, levels, lq)
    snap = [(DEPTH_SNAPSHOT_EVENT | BUY_EVENT, 0, 0, round(p * tick, 10), q, 0, 0, 0.0) for p, q in bids.items()]
    snap += [(DEPTH_SNAPSHOT_EVENT | SELL_EVENT, 0, 0, round(p * tick, 10), q, 0, 0, 0.0) for p, q in asks.items()]
    snapshot = np.array(snap, dtype=event_dtype)

    rows: list = []
    cur_bids, cur_asks = dict(bids), dict(asks)
    prev_mid = start_mid
    n_steps = int(seconds * NS_PER_S // step_ns)
    cleared = False
    for i in range(1, n_steps + 1):
        ts = t0 + i * step_ns
        t_s = i * step_ns / NS_PER_S
        # ---- optional recording gap: clear both sides, then (later) rebuild from a fresh book.
        if clear_at_s is not None and not cleared and t_s >= clear_at_s:
            rows.append(_row(DEPTH_CLEAR_EVENT | BUY_EVENT, ts, 0.0, 0.0))
            rows.append(_row(DEPTH_CLEAR_EVENT | SELL_EVENT, ts, 0.0, 0.0))
            cur_bids, cur_asks = {}, {}
            cleared = True
        if cleared and clear_at_s is not None and not cur_bids and t_s < clear_at_s + clear_for_s:
            continue                                   # book stays empty during the "gap"
        mid = start_mid + SYNTH["sine_amp"] * np.sin(2 * np.pi * t_s / SYNTH["sine_period_s"]) \
            + SYNTH["noise"] * rng.standard_normal()
        new_bids, new_asks = _book(mid, tick, levels, lq)
        # ---- trades: if the mid moved by >= 1 tick, print trades through the levels it crossed.
        if cur_bids and cur_asks:
            if mid < prev_mid - tick:        # price fell: sellers hit our old bids from best down to new best
                for p in sorted(cur_bids, reverse=True):
                    if p > max(new_bids):
                        rows.append(_row(TRADE_EVENT | SELL_EVENT, ts, round(p * tick, 10), SYNTH["trade_qty"]))
            elif mid > prev_mid + tick:      # price rose: buyers lift old asks up to the new best
                for p in sorted(cur_asks):
                    if p < min(new_asks):
                        rows.append(_row(TRADE_EVENT | BUY_EVENT, ts, round(p * tick, 10), SYNTH["trade_qty"]))
        # ---- depth diff: delete vanished levels (qty 0), add/refresh the new ones.
        for side_flag, old, new in ((BUY_EVENT, cur_bids, new_bids), (SELL_EVENT, cur_asks, new_asks)):
            for p in old:
                if p not in new:
                    rows.append(_row(DEPTH_EVENT | side_flag, ts, round(p * tick, 10), 0.0))
            for p, q in new.items():
                if old.get(p) != q:
                    rows.append(_row(DEPTH_EVENT | side_flag, ts, round(p * tick, 10), q))
        cur_bids, cur_asks, prev_mid = new_bids, new_asks, mid

    events = np.array(rows, dtype=event_dtype)
    validate_event_order(events)       # raises ValueError if exch_ts / local_ts go backwards
    return snapshot, events
