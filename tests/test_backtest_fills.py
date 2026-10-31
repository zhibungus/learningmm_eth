"""Fill-model / exchange unit tests on hand-built event sequences (no files, no network).

Prices are on a 0.01 tick, sizes on a 0.001 lot.  Times are in ns; the helper ``ms`` converts.
Latencies are 0 unless a test is about latency, so the order of events is easy to read.
"""
# pytest finds every function named test_* in files named test_*.py and runs it. A test passes if it
# finishes without an exception; a failing "assert" raises one. pytest.approx(x) compares floats with
# a tiny tolerance, since floating-point math is never exactly equal (0.1 + 0.2 != 0.3).
import pytest

from mm.backtest.config import DEFAULTS, deep_merge
from mm.backtest.engine import build_engine
from mm.backtest.strategy import (CancelOrder, PlaceOrder, ReplaceOrder, Strategy)
from mm.data.events import DepthEvent, FundingEvent, QuoteEvent, TradeEvent

# A fixed starting time in nanoseconds (about Nov 2023). Tests only need RELATIVE times.
T0 = 1_700_000_000_000_000_000


# Helpers that build market events so each test reads like a short story. ms(5) = "5 milliseconds
# after T0" in nanoseconds. A quote is the best bid/ask with sizes; a trade is an aggressor ("buy" lifts
# the ask, "sell" hits the bid) printing at a price; a depth event updates book levels (snap=True
# replaces the whole book). The Nones are timestamps/ids the tests do not care about.
def ms(x):
    return T0 + int(x * 1_000_000)


def quote(t, bid, bq, ask, aq):
    return QuoteEvent(ms(t), None, None, 0, bid, bq, ask, aq)


def trade(t, px, qty, side):
    return TradeEvent(ms(t), None, None, 0, 0, 0, px, qty, side)


def depth(t, bids, asks, snap=False):
    return DepthEvent(ms(t), None, None, snap, 0, 0, None, tuple(bids), tuple(asks))


# A fake strategy for tests: instead of thinking, it sends pre-written orders. The script is a list of
# (time, action) pairs; an action goes out when the first event at or after its time reaches the
# strategy. It also records every fill and order update it hears about, so tests can assert on them.
class Scripted(Strategy):
    """Plays a fixed list of (timestamp_ms, action) when the first event after that time arrives."""
    name = "scripted"
    event_kinds = ("quote", "trade", "depth")

    def __init__(self, params=None, rng=None):
        super().__init__(params, rng)
        self.script = list(self.params.get("script", []))
        self.fills, self.updates = [], []

    def on_event(self, ev, state):
        # "while ... pop(0)": send every scripted action whose time has come (pop(0) removes and
        # returns the first list item).
        out = []
        while self.script and self.script[0][0] <= ev.ts_ns:
            out.append(self.script.pop(0)[1])
        return out

    def on_fill(self, fill, state):
        self.fills.append(fill)

    def on_order_update(self, upd, state):
        self.updates.append(upd)


# Build an engine with ZERO latency (so event order is easy to reason about) and the Scripted
# strategy. "**over" collects extra keyword arguments into a dict, e.g. make(script, latency={...}),
# which are merged over the defaults to tweak a single test. min_notional=0 lets tiny test orders pass.
def make(script, **over):
    # allow_zero_latency is the explicit TEST-ONLY opt-in (real runs refuse zero latency because it leaks
    # same-millisecond information); jitter and the archive E-delay are off so timing is exact.
    cfg = deep_merge(DEFAULTS, {"latency": {"feed_latency_ms": 0, "order_latency_ms": 0, "report_latency_ms": 0,
                                            "timer_interval_ms": 0, "allow_zero_latency": True,
                                            "order_jitter_ms": 0, "feed_jitter_ms": 0,
                                            "feed_delay_quantiles_ms": None,   # fixed delay, not the empirical table
                                            "no_event_ts_extra_ms": 0},
                                "exchange": {"min_notional": 0.0}})
    cfg = deep_merge(cfg, over)
    cfg["strategy"]["params"] = {"script": script}
    eng = build_engine(cfg, seed=1, strategy_cls=Scripted)
    return eng, eng.strategy


# Push a list of events through the engine in order (what a replay does with real data). Items due
# exactly at the last timestamp only run when a LATER event arrives (tie rule), so flush() at the end
# lets a test look at the result without feeding a dummy event.
def feed(eng, events):
    for e in events:
        eng.on_market_event(e)
    eng.flush()


# ---------------------------------------------------------------- queue position
# Queue position: orders at the same price fill first-come-first-served. If 5 ETH is already bid at
# 100.00 when we join, 5 ETH of selling must hit that price before we get anything.
def test_queue_starts_behind_displayed_and_decrements_with_trades():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0),
               quote(1, 100.00, 5.0, 100.01, 5.0),          # strategy sees this and sends the order
               trade(2, 100.00, 3.0, "sell")])               # eats 3 of the 5 ahead
    order = eng.ex.resting["b"]
    assert order.q_ahead == pytest.approx(2.0)
    assert not s.fills
    feed(eng, [trade(3, 100.00, 2.05, "sell")])              # clears the last 2.0, 0.05 reaches us
    assert len(s.fills) == 1
    f = s.fills[0]
    assert f.qty == pytest.approx(0.05) and f.price == 100.00 and f.liquidity == "maker"
    assert f.leaves_qty == pytest.approx(0.05)               # partial fill
    feed(eng, [trade(4, 100.00, 1.0, "sell")])
    assert s.fills[-1].leaves_qty == 0.0
    assert "b" not in eng.ex.resting


# Trade-through: a sell prints at 99.99, BELOW our 100.00 bid, so our level was consumed and we are
# certainly filled, BUT only up to the size of that print: a 0.001 ETH print cannot fill a 0.1 ETH
# order (the old model gave the whole order, an overfill the validator reproduced).
def test_trade_through_fill_is_capped_by_print_size():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 50.0, 100.01, 5.0), quote(1, 100.00, 50.0, 100.01, 5.0),
               trade(2, 99.99, 0.001, "sell")])
    assert len(s.fills) == 1 and s.fills[0].qty == pytest.approx(0.001) and s.fills[0].price == 100.00
    assert eng.ex.resting["b"].remaining == pytest.approx(0.099)        # the rest is still resting
    assert eng.rec.fills[0][-1] == "through"                            # mechanism recorded
    feed(eng, [trade(3, 99.99, 1.0, "sell")])                           # a big print fills the rest
    assert s.fills[-1].leaves_qty == 0.0 and sum(f.qty for f in s.fills) == pytest.approx(0.1)


# Legacy switch: cap_fills_by_volume=False restores the whole-order fill (kept only for comparison).
def test_uncapped_legacy_mode_fills_whole_order():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))], fill_model={"cap_fills_by_volume": False})
    feed(eng, [quote(0, 100.00, 50.0, 100.01, 5.0), quote(1, 100.00, 50.0, 100.01, 5.0),
               trade(2, 99.99, 0.001, "sell")])
    assert s.fills[0].qty == pytest.approx(0.1)

# Conservative (pessimistic) setting: a trade exactly at our price never fills us; only a strict
# trade-through does. Used in sensitivity runs to test how much results depend on queue luck.
def test_conservative_mode_requires_strict_trade_through():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))],
                  fill_model={"require_trade_through": True})
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 5.0), quote(1, 100.00, 1.0, 100.01, 5.0),
               trade(2, 100.00, 5.0, "sell")])               # queue exhausted but not through
    assert not s.fills
    feed(eng, [trade(3, 99.99, 0.01, "sell")])
    assert len(s.fills) == 1


def test_wrong_aggressor_side_does_not_fill():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 100.00, 5.0, "buy"), trade(3, 99.00, 5.0, "buy")])
    assert not s.fills


# Quoting INSIDE the spread (better than the best price) puts us alone at a new price level, so
# we are first in line: the very first trade at our price fills us.
def test_inside_spread_order_is_first_in_line():
    eng, s = make([(ms(1), PlaceOrder("a", "sell", 0.1, 100.02, post_only=True))])
    feed(eng, [quote(0, 100.00, 5.0, 100.04, 5.0), quote(1, 100.00, 5.0, 100.04, 5.0)])
    assert eng.ex.resting["a"].q_ahead == 0.0
    feed(eng, [trade(2, 100.02, 0.03, "buy")])
    assert s.fills[0].qty == pytest.approx(0.03)


def test_deeper_than_touch_only_fills_on_trade_through():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 99.98))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(2, 99.98, 100.0, "sell")])              # at our price but queue unknown (infinite)
    assert not s.fills
    feed(eng, [trade(3, 99.97, 0.01, "sell")])
    assert len(s.fills) == 1


# When displayed size shrinks with no matching trades, somebody CANCELLED. We cannot see whether they
# were ahead of or behind us, so the assumption decides how far we move up the queue. Setup: we join
# behind 10 displayed (q_ahead 10), 20 more join behind us (30 displayed), then 15 cancel (15 displayed).
# proportional: our share ahead was 10/30, so 15 * 1/3 = 5 left ahead of us -> q_ahead 5.
# all_behind:   none of the 15 were ahead of us -> q_ahead stays 10 (capped at the 15 displayed).
# all_ahead:    all 15 were ahead of us -> q_ahead 10 - min(15, 10) = 0.
# The three values differ, so each assumption is actually exercised (the old test asserted 6.0 for two).
@pytest.mark.parametrize("assumption,expected", [("proportional", 5.0), ("all_behind", 10.0), ("all_ahead", 0.0)])
def test_cancel_assumptions_give_different_queue_positions(assumption, expected):
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))], fill_model={"cancel_assumption": assumption})
    feed(eng, [quote(0, 100.00, 10.0, 100.01, 5.0), quote(1, 100.00, 10.0, 100.01, 5.0),
               quote(2, 100.00, 30.0, 100.01, 5.0),          # +20 joined behind us (q_ahead stays 10)
               quote(3, 100.00, 15.0, 100.01, 5.0)])         # -15 cancelled with no trades
    assert eng.ex.resting["b"].q_ahead == pytest.approx(expected)

def test_trades_explain_display_drop_so_no_extra_cancel():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 10.0, 100.01, 5.0), quote(1, 100.00, 10.0, 100.01, 5.0),
               trade(2, 100.00, 4.0, "sell"),                # q_ahead 10 -> 6
               quote(2, 100.00, 6.0, 100.01, 5.0)])          # the display drop is the trade, not a cancel
    assert eng.ex.resting["b"].q_ahead == pytest.approx(6.0)


# ---------------------------------------------------------------- L2 model
# L2 = full order-book depth. The queue at our price is read directly from the book instead of being
# guessed from the top of book only. fill_model={"type": "l2"} switches the model for these tests.
def test_l2_queue_from_book_and_depth_updates():
    eng, s = make([(ms(2), PlaceOrder("b", "buy", 0.1, 100.00))],
                  fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 4.0), (99.99, 9.0)], [(100.01, 3.0)], snap=True),
               depth(1, [], []), depth(2, [], [])])
    o = eng.ex.resting["b"]
    assert o.q_ahead == pytest.approx(4.0)
    feed(eng, [depth(3, [(100.00, 2.0)], [])])                # 2 cancelled, none traded -> ahead 4 -> 2
    assert o.q_ahead == pytest.approx(2.0)
    feed(eng, [trade(4, 100.00, 2.5, "sell")])
    assert len(s.fills) == 1 and s.fills[0].qty == pytest.approx(0.1)   # 2.0 ahead, trade of 2.5 reaches us


def test_l2_fill_amount_after_queue():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))],
                  fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 1.0)], [(100.01, 3.0)], snap=True), depth(1, [], []),
               trade(2, 100.00, 1.04, "sell")])
    assert s.fills[0].qty == pytest.approx(0.04)


def test_l2_taker_walks_the_book():
    eng, s = make([(ms(1), PlaceOrder("t", "buy", 3.0, None))],
                  fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 1.0)], [(100.01, 1.0), (100.02, 1.5), (100.05, 10.0)], snap=True),
               depth(1, [], [])])
    got = [(f.price, f.qty) for f in s.fills]
    assert got == [(100.01, 1.0), (100.02, 1.5), (100.05, 0.5)]
    assert all(f.liquidity == "taker" for f in s.fills)


# ---------------------------------------------------------------- exchange rules
# Post-only = "maker or nothing". A buy at 100.01 when the ask is 100.01 would execute immediately as
# a taker, so the exchange rejects it rather than letting us pay the higher taker fee.
def test_post_only_rejected_when_crossing():
    eng, s = make([(ms(1), PlaceOrder("p", "buy", 0.1, 100.01, post_only=True))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)])
    assert [u.status for u in s.updates] == ["rejected"] and s.updates[0].reason == "post_only_would_cross"
    assert not s.fills and not eng.ex.resting


# Taker = takes liquidity by crossing the spread: buys at the ask, pays the taker fee (5 bps here)
# and starts "behind" the mid, so its spread capture is negative (it paid half the spread).
def test_marketable_limit_is_taker_and_pays_taker_fee():
    eng, s = make([(ms(1), PlaceOrder("t", "buy", 0.1, 100.01))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)])
    f = s.fills[0]
    assert f.liquidity == "taker" and f.price == 100.01
    assert f.fee == pytest.approx(100.01 * 0.1 * 0.0005)
    # paid the spread: spread capture vs the pre-trade mid (100.005) is negative
    assert eng.account.spread_capture == pytest.approx(0.1 * (100.005 - 100.01))


# Some venues pay makers a rebate (negative fee) to attract resting liquidity; a negative rate
# must therefore produce a negative fee and reduce total fees paid.
def test_maker_fee_and_rebate():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 1.0, 100.00))], exchange={"maker_fee": -0.0001})
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 100.00, 1.0, "sell")])
    assert s.fills[0].fee == pytest.approx(-100.0 * 1.0 * 0.0001)    # rebate
    assert eng.account.fees < 0


def test_tick_and_lot_enforced():
    eng, s = make([(ms(1), PlaceOrder("a", "buy", 0.1, 100.005)),
                   (ms(1), PlaceOrder("b", "buy", 0.0005, 100.00)),
                   (ms(1), PlaceOrder("c", "buy", 0.1234, 100.00)),
                   (ms(1), PlaceOrder("d", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 5.0, 100.02, 5.0), quote(1, 100.00, 5.0, 100.02, 5.0)])
    reasons = {u.client_id: (u.status, u.reason) for u in s.updates}
    assert reasons["a"] == ("rejected", "price_not_on_tick")
    assert reasons["b"][0] == "rejected" and reasons["c"] == ("rejected", "qty_not_on_lot")
    assert reasons["d"][0] == "accepted"


def test_duplicate_client_id_rejected():
    eng, s = make([(ms(1), PlaceOrder("x", "buy", 0.1, 99.99)), (ms(1), PlaceOrder("x", "buy", 0.1, 99.98))])
    feed(eng, [quote(0, 100.00, 5.0, 100.02, 5.0), quote(1, 100.00, 5.0, 100.02, 5.0)])
    assert [u.reason for u in s.updates if u.status == "rejected"] == ["duplicate_client_id"]


# ---------------------------------------------------------------- latency
# Why model latency? Real orders take tens of ms to reach the exchange. A cancel is only effective on
# ARRIVAL, so if the order trades first the cancel arrives too late ("cancel_rejected"). Ignoring this
# makes a market maker look far safer than it is.
def test_cancel_in_flight_loses_race_against_fill():
    # order latency 50 ms: cancel decided at t=11 arrives at t=61; a trade at t=30 fills first.
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00)), (ms(11), CancelOrder("b"))],
                  latency={"order_latency_ms": 50})
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               quote(11, 100.00, 0.0, 100.01, 5.0),
               trade(60, 100.00, 5.0, "sell"),               # order live from t=51; cancel not yet arrived
               quote(70, 100.00, 0.0, 100.01, 5.0)])
    assert len(s.fills) == 1
    assert [u.status for u in s.updates] == ["accepted", "cancel_rejected"]


def test_order_not_live_before_arrival():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))], latency={"order_latency_ms": 50})
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(30, 99.00, 5.0, "sell"),                # would fill it, but it has not arrived yet
               quote(60, 100.00, 0.0, 100.01, 5.0)])
    assert not s.fills


# Feed latency AND publish time (H2). Binance stamps each message with T (when the trade/quote happened
# inside the matching engine) and E (when it PUBLISHED the message). You cannot receive it before E, so
# the strategy must see it at max(T, E) + feed latency, not T + latency. When E is missing (archive
# trades) a configured typical publish delay is added to T instead. "Spy" is a throwaway subclass that
# records (event time T, time the strategy saw it).
def test_strategy_sees_events_after_publish_time_plus_latency():
    seen = []

    class Spy(Scripted):
        def on_event(self, ev, state):
            seen.append((ev.kind, ev.ts_ns, state.now_ns))
            return []
    cfg = deep_merge(DEFAULTS, {"latency": {"feed_latency_ms": 25, "order_latency_ms": 5, "report_latency_ms": 5,
                                            "timer_interval_ms": 0, "order_jitter_ms": 0, "feed_jitter_ms": 0,
                                            "feed_delay_quantiles_ms": None, "no_event_ts_extra_ms": 7}})
    eng = build_engine(cfg, seed=1, strategy_cls=Spy)
    q_slow = QuoteEvent(ms(0), ms(40), None, 0, 100.00, 1.0, 100.01, 1.0)      # published 40 ms after T
    q_fast = QuoteEvent(ms(100), ms(100), None, 1, 100.00, 1.0, 100.01, 1.0)   # E == T
    t_arch = trade(200, 100.0, 1.0, "buy")                                     # archive trade: E is None
    q_early = QuoteEvent(ms(300), ms(290), None, 2, 100.00, 1.0, 100.01, 1.0)  # E < T (clock noise): use T
    feed(eng, [q_slow, q_fast, t_arch, q_early, quote(400, 100.00, 1.0, 100.01, 1.0)])   # last one flushes
    eng.finish()
    assert seen[0] == ("quote", ms(0), ms(65))        # max(0, 40) + 25, NOT 0 + 25
    assert seen[1] == ("quote", ms(100), ms(125))
    assert seen[2] == ("trade", ms(200), ms(232))     # T + 7 ms assumed publish delay + 25
    assert seen[3] == ("quote", ms(300), ms(325))


# Zero latency lets a strategy react to print #1 and be filled by print #2 of the SAME millisecond, so
# the config validator refuses it unless the test-only flag is set (L3).
def test_zero_latency_is_refused_without_the_test_flag():
    from mm.backtest.config import validate_latency
    cfg = deep_merge(DEFAULTS, {"latency": {"feed_latency_ms": 0}})
    with pytest.raises(ValueError, match="same-millisecond"):
        validate_latency(cfg)
    validate_latency(deep_merge(cfg, {"latency": {"allow_zero_latency": True}}))   # explicit opt-in works

# ---------------------------------------------------------------- replace
# Amending: shrinking an order at the same price keeps its place in the queue; moving the price sends
# it to the back at the new level (here deeper than the touch, so its queue becomes unknown/infinite).
def test_replace_size_reduction_keeps_queue_price_change_loses_it():
    # replace_keeps_priority=True is the optimistic old rule; the default (Binance-like) re-queues every
    # replace, see test_backtest_engine_fixes.py.
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.2, 100.00)), (ms(3), ReplaceOrder("b", qty=0.1)),
                   (ms(5), ReplaceOrder("b", price=99.99))], exchange={"replace_keeps_priority": True})
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(2, 100.00, 2.0, "sell"), quote(3, 100.00, 3.0, 100.01, 5.0),
               quote(4, 100.00, 3.0, 100.01, 5.0)])
    o = eng.ex.resting["b"]
    assert o.remaining == pytest.approx(0.1) and o.q_ahead == pytest.approx(3.0)   # kept priority
    feed(eng, [quote(5, 100.00, 3.0, 100.01, 5.0), quote(6, 100.00, 3.0, 100.01, 5.0)])
    o = eng.ex.resting["b"]
    assert o.price == 99.99 and o.q_ahead == float("inf")                          # new, deeper level


# ---------------------------------------------------------------- funding
# Funding: perpetual futures have no expiry, so every 8 hours the side that is "too popular" pays the
# other. With a positive rate, longs pay shorts position * mark price * rate; a flat position pays nothing.
def test_funding_charged_to_open_position():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 1.0, 100.00))])
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 100.00, 1.0, "sell")])
    assert eng.account.position == 1.0
    cash0 = eng.account.cash
    feed(eng, [FundingEvent(ms(10), 0.0001, 2000.0, 8)])
    assert eng.account.cash == pytest.approx(cash0 - 1.0 * 2000.0 * 0.0001)      # long pays
    assert eng.account.funding == pytest.approx(0.2)


def test_funding_without_position_is_free_and_short_receives():
    eng, s = make([])
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 1.0), FundingEvent(ms(5), 0.0001, 100.0, 8)])
    assert eng.account.funding == 0.0
    eng.account.position = -2.0
    assert eng.account.on_funding(0.0001, 100.0) == pytest.approx(-0.02)
