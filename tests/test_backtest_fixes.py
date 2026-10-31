"""Regression tests for the validator's fixes (2026-10-19 review): same-millisecond ties, fill-size caps,
own-queue accounting, taker sweeps, risk limits, feed gaps, markout correctness, provenance, and the
paper trader's re-ordering. All on hand-built events; no network, no real data files.

pytest basics: functions named test_* are run; a failing ``assert`` fails the test;
``@pytest.mark.parametrize`` runs the same test body once per listed set of arguments;
``tmp_path`` is a fresh temporary folder pytest provides.
"""
import asyncio

import numpy as np
import polars as pl
import pytest

from mm.backtest.config import DEFAULTS, deep_merge
from mm.backtest.engine import build_engine
from mm.backtest.strategy import PlaceOrder, ReplaceOrder, Strategy
from mm.data.events import DepthEvent, FundingEvent, GapEvent, QuoteEvent, TradeEvent

# Reuse the helpers from the other test file (pytest puts the tests folder on sys.path).
from test_backtest_fills import depth, feed, make, ms, quote, trade


# ---------------------------------------------------------------- M1: same-millisecond ties
# Rule: ALL market events with the same ts_ns are processed before any order that arrives at that ts_ns.
# Setup: the order arrives at exactly 30 ms (decided at 1 ms, order latency 29 ms). Two through-prints
# happen at 30 ms. Old behaviour: the first print missed the order and the SECOND filled it, so the
# result depended on how many prints shared the millisecond. Now neither can fill it.
def test_order_arriving_in_a_millisecond_is_not_filled_by_that_milliseconds_prints():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))], latency={"order_latency_ms": 29})
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(30, 99.99, 0.05, "sell"), trade(30, 99.98, 0.05, "sell"),     # same ms as the arrival
               quote(31, 100.00, 5.0, 100.01, 5.0)])
    assert not s.fills
    assert "b" in eng.ex.resting                       # it arrived after those prints and now rests
    feed(eng, [trade(32, 99.99, 0.02, "sell")])         # a LATER print does fill it
    assert [f.qty for f in s.fills] == [pytest.approx(0.02)]


# ---------------------------------------------------------------- H4: quote-cross fills
# A quote that moves through our resting bid with no trade print means we were matched, but only up to the
# size displayed on the crossing side. Here 0.2 ETH is displayed on the (new) ask, our order is 0.5.
def _cross_setup(**fm):
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.5, 100.00))], fill_model=fm)
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               quote(2, 99.97, 1.0, 99.99, 0.2)])      # ask 99.99 <= our 100.00 bid, displayed 0.2
    return eng, s


def test_quote_cross_fill_is_capped_by_displayed_size_and_tagged():
    eng, s = _cross_setup()
    assert len(s.fills) == 1 and s.fills[0].qty == pytest.approx(0.2) and s.fills[0].price == 100.00
    assert eng.rec.fills[0][-1] == "cross"
    assert s.fills[0].ref_mid is None and s.fills[0].mechanism == ""    # L2: simulator internals hidden


def test_no_cross_fills_variant_and_conservative_mode_ignore_crosses():
    assert not _cross_setup(cross_fills=False)[1].fills
    assert not _cross_setup(require_trade_through=True)[1].fills


# ---------------------------------------------------------------- M4: our own orders in the queue
# Two of our orders (5 ETH then 1 ETH) at a bid with 1.0 ETH displayed. The displayed size never includes
# our orders, so the second order waits behind 1.0 public + 5.0 of our own. A 2.0 ETH sell print can fill
# only 1.0 (the first order's share of the public queue overrun) and nothing for the second.
def test_second_own_order_at_same_price_waits_behind_the_first():
    eng, s = make([(ms(1), PlaceOrder("o1", "buy", 5.0, 100.00)), (ms(1), PlaceOrder("o2", "buy", 1.0, 100.00))])
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 5.0), quote(1, 100.00, 1.0, 100.01, 5.0),
               trade(2, 100.00, 2.0, "sell")])
    assert [(f.client_id, f.qty) for f in s.fills] == [("o1", pytest.approx(1.0))]


# ---------------------------------------------------------------- M6: taker sweeps
# Quote data shows only the touch. A market buy bigger than the displayed 0.2 ETH pays a growing price:
# tiers as big as the touch, each one tick (config) worse. Liquidity already used by an earlier taker is
# remembered until the next quote, and beyond sweep_max_tiers nothing is available.
def test_quote_model_taker_sweeps_tiers_and_remembers_consumption():
    eng, s = make([(ms(1), PlaceOrder("t1", "buy", 0.5)), (ms(1), PlaceOrder("t2", "buy", 0.1))])
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 0.2), quote(1, 100.00, 1.0, 100.01, 0.2)])
    got = [(f.client_id, round(f.price, 2), round(f.qty, 3)) for f in s.fills]
    assert got == [("t1", 100.01, 0.2), ("t1", 100.02, 0.2), ("t1", 100.03, 0.1),
                   ("t2", 100.03, 0.1)]                    # t2 cannot reuse what t1 consumed


def test_quote_model_taker_is_limited_to_max_tiers():
    eng, s = make([(ms(1), PlaceOrder("t", "buy", 3.0))], fill_model={"sweep_max_tiers": 2})
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 0.2), quote(1, 100.00, 1.0, 100.01, 0.2)])
    assert sum(f.qty for f in s.fills) == pytest.approx(0.6)            # touch + 2 tiers of 0.2
    assert s.updates[-1].status == "canceled" and s.updates[-1].reason == "ioc_remainder"


def test_l2_second_taker_sees_a_thinner_book():
    eng, s = make([(ms(3), PlaceOrder("t1", "buy", 0.7)), (ms(3), PlaceOrder("t2", "buy", 0.7))],
                  fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 1.0)], [(100.01, 1.0), (100.02, 5.0)], snap=True),
               depth(1, [(99.99, 3.0)], []), depth(2, [(99.99, 3.1)], []), depth(3, [(99.99, 3.2)], [])])
    got = [(f.client_id, f.price, round(f.qty, 3)) for f in s.fills]
    assert got == [("t1", 100.01, 0.7), ("t2", 100.01, 0.3), ("t2", 100.02, 0.4)]


# ---------------------------------------------------------------- M7: exchange-side risk limits
def test_exchange_risk_limits_reject_orders():
    script = [(ms(1), PlaceOrder("ok", "buy", 0.2, 99.99)),
              (ms(1), PlaceOrder("too_much", "buy", 0.2, 99.98)),      # open buys would reach 0.4 > 0.3
              (ms(1), PlaceOrder("big", "buy", 1.0, 99.99)),           # notional ~100 > 50
              (ms(1), PlaceOrder("sell_ok", "sell", 0.2, 100.05))]
    eng, s = make(script, exchange={"max_position": 0.3, "max_order_notional": 50.0})
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)])
    status = {u.client_id: (u.status, u.reason) for u in s.updates}
    assert status["ok"][0] == "accepted" and status["sell_ok"][0] == "accepted"
    assert status["too_much"] == ("rejected", "risk_max_position")
    assert status["big"] == ("rejected", "risk_max_order_notional")


# ---------------------------------------------------------------- M3: feed gaps
# A GapEvent (the feed lost messages) must: cancel everything, forget the book/prices (strategy side
# too), and reject new orders until the NEXT SNAPSHOT has been applied (depth model).
def test_gap_cancels_orders_clears_state_and_blocks_quoting_until_snapshot():
    script = [(ms(2), PlaceOrder("b", "buy", 0.1, 100.00)),
              (ms(7), PlaceOrder("c", "buy", 0.1, 100.00)),            # sent while still desynchronised
              (ms(9), PlaceOrder("d", "buy", 0.1, 100.00))]            # sent after the snapshot
    eng, s = make(script, fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 4.0)], [(100.01, 3.0)], snap=True), depth(1, [(99.99, 1.0)], []),
               depth(2, [(99.99, 2.0)], [])])
    assert "b" in eng.ex.resting
    feed(eng, [GapEvent(ts_ns=ms(5), reason="disconnect", stream=None)])
    assert not eng.ex.resting and eng.ex.awaiting_resync
    assert eng.state.bid_px is None and eng.state.mid is None             # strategy sees no prices
    assert any(u.client_id == "b" and u.status == "canceled" and u.reason == "gap" for u in s.updates)
    feed(eng, [depth(6, [(99.99, 2.5)], []), depth(7, [(99.99, 2.6)], []),
               depth(8, [(100.00, 4.0)], [(100.01, 3.0)], snap=True), depth(9, [(99.99, 2.7)], [])])
    status = {u.client_id: (u.status, u.reason) for u in s.updates if u.client_id in ("c", "d")}
    assert status["c"] == ("rejected", "awaiting_resync")
    assert status["d"][0] == "accepted" and not eng.ex.awaiting_resync


def test_gap_with_quote_model_resumes_on_next_quote():
    eng, s = make([(ms(8), PlaceOrder("d", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), GapEvent(ts_ns=ms(5), reason="resync", stream=None)])
    assert eng.ex.awaiting_resync
    feed(eng, [quote(7, 100.00, 5.0, 100.01, 5.0), quote(8, 100.00, 5.0, 100.01, 5.0)])
    assert [u.status for u in s.updates if u.client_id == "d"] == ["accepted"]


# ---------------------------------------------------------------- L2: strategy never sees simulator internals
# (covered above for the cross fill: ref_mid None, mechanism ""). L4: funding also moves the strategy's cash.
def test_funding_reduces_the_strategys_cash_view():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 1.0, 100.00))])
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 99.99, 1.0, "sell"), quote(3, 100.00, 0.0, 100.01, 5.0)])
    assert eng.state.position == pytest.approx(1.0)
    cash0 = eng.state.cash
    feed(eng, [FundingEvent(ms(10), 0.0001, 2000.0, 8), quote(11, 100.00, 0.0, 100.01, 5.0)])
    assert eng.state.cash == pytest.approx(cash0 - 1.0 * 2000.0 * 0.0001)


# L8: a replace still in flight when the order fully fills must not leave a stale entry behind.
def test_pending_replace_cleared_when_order_fills():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00)), (ms(55), ReplaceOrder("b", price=99.99))],
                  latency={"order_latency_ms": 50})
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               quote(55, 100.00, 0.0, 100.01, 5.0), trade(60, 99.99, 5.0, "sell")])
    assert not eng.ex.resting and "b" not in eng._pending_replace


# ---------------------------------------------------------------- markouts (L1) with real value checks
# Buy filled at 100.00 (mid before 100.005); one second later the mid is 100.11. Markout vs fill price is
# side*(mid_after - fill_price) = +0.11 for a buy, -0.09 for a sell filled at 100.02 (sign flips with
# side). The 10 s horizon is beyond the end of the data so it must NOT appear (no lookahead / no clipping).
@pytest.mark.parametrize("side,px,print_px,print_side,expected,expected_move", [
    ("buy", 100.00, 99.99, "sell", 0.11, 100.11 - 100.005),
    ("sell", 100.02, 100.03, "buy", -(100.11 - 100.02), -(100.11 - 100.005))])
def test_markout_values_have_the_right_sign_and_horizon(side, px, print_px, print_side, expected, expected_move):
    eng, s = make([(ms(1), PlaceOrder("o", side, 0.1, px))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(2, print_px, 5.0, print_side),
               quote(1002, 100.10, 5.0, 100.12, 5.0), quote(1500, 100.10, 5.0, 100.12, 5.0)])
    m, fills, _ = eng.finish()
    k = m["markouts"]["1s"]
    assert k["n_fills"] == 1
    assert k["avg_markout_per_eth"] == pytest.approx(expected)
    assert k["avg_move_per_eth"] == pytest.approx(expected_move)
    assert "10s" not in m["markouts"]


# L1 directly: three mid updates, the last two in the same 10 ms slot. A lookup at 22 ms must return the
# mid that was valid THEN (100), not the 25 ms update (102) that merely shares the slot.
def test_mid_series_is_labelled_with_last_update_time_so_lookups_cannot_see_the_future():
    from mm.backtest.metrics import Account, Recorder, _fills_frame
    rec = Recorder(Account(1000.0), 1_000_000_000, 10_000_000)
    rec.on_mid(ms(0), 100.0)
    rec.on_mid(ms(20), 101.0)
    rec.on_mid(ms(25), 102.0)
    rec.last_ts = ms(100)
    rec.fills.append((ms(0), 1, "x", "buy", 99.0, 1.0, "maker", 0.0, 0.0, 100.0, 1.0, 1.0, "through"))
    df = _fills_frame(rec, [0.022])                      # target = fill time + 22 ms
    assert df["markout_0.022s"][0] == pytest.approx(100.0 - 99.0)
    df = _fills_frame(rec, [0.030])                      # at 30 ms the 25 ms update is legitimately known
    assert df["markout_0.03s"][0] == pytest.approx(102.0 - 99.0)


# ---------------------------------------------------------------- determinism with passive orders
# The earlier determinism test only used market orders. Here the DEMO quoter's resting limit orders (queue,
# through, replace, jitter) are exercised: same seed => identical fills, different seed => different timing.
def _market_events(n=1200, seed=7):
    r = np.random.default_rng(seed)
    px, out = 2000.0, []
    base = 1_700_000_000_000_000_000
    for i in range(n):
        t = base + i * 50_000_000
        px = max(1.0, round(px + r.choice([-0.02, -0.01, 0.0, 0.01, 0.02]), 2))
        out.append(QuoteEvent(t, t + 2_000_000, None, i, px, 3.0, round(px + 0.01, 2), 3.0))
        k = int(r.integers(0, 4))
        if r.random() < 0.5:
            out.append(TradeEvent(t + 1_000_000, None, None, i, i, i, round(px - 0.01 * k, 2), 0.5, "sell"))
        else:
            out.append(TradeEvent(t + 1_000_000, None, None, i, i, i, round(px + 0.01 + 0.01 * k, 2), 0.5, "buy"))
    return out


def _run_demo(seed):
    cfg = deep_merge(DEFAULTS, {"latency": {"timer_interval_ms": 200},
                                "strategy": {"params": {"half_spread_ticks": 1, "order_qty": 0.05}}})
    eng = build_engine(cfg, seed=seed)
    for e in _market_events():
        eng.on_market_event(e)
    return eng.finish()


def test_determinism_with_resting_limit_orders():
    m1, f1, e1 = _run_demo(3)
    m2, f2, e2 = _run_demo(3)
    m3, f3, e3 = _run_demo(4)
    assert m1["fills"]["count"] > 5
    assert f1.equals(f2) and e1.equals(e2) and m1 == m2
    assert f1["ts_ns"].to_list() != f3["ts_ns"].to_list()               # jitter draws differ with the seed


# ---------------------------------------------------------------- M5: provenance and period windows
def test_file_manifest_has_hash_and_row_count(tmp_path):
    from mm.backtest.report import file_manifest, git_info
    p = tmp_path / "a.parquet"
    pl.DataFrame({"x": [1, 2, 3]}).write_parquet(p)
    (rec,) = file_manifest([p], tmp_path)
    assert rec["rows"] == 3 and len(rec["sha256"]) == 64 and rec["path"] == "a.parquet"
    g = git_info(tmp_path)                                               # not a repo: must not crash
    assert "commit" in g and isinstance(g["dirty"], bool)


def test_intraday_period_window():
    from mm.backtest.run import period_window_ns
    lo, hi = period_window_ns({"start": "2024-03-29", "end": "2024-03-29",
                               "start_time": "06:00:00", "end_time": "07:30:00"})
    assert (hi - lo) == 5400 * 1_000_000_000
    lo, hi = period_window_ns({"start": "2024-03-29", "end": "2024-03-30"})
    assert (hi - lo) == 2 * 86400 * 1_000_000_000                        # whole days, end inclusive


def test_demo_configs_use_measured_not_zero_latency():
    from mm.backtest.config import load_config
    for path in ("config/backtest/demo_smoke.yaml", "config/backtest/demo_l2_live.yaml", "config/paper/demo_paper.yaml"):
        lat = load_config(path)["latency"]
        assert lat["feed_latency_ms"] >= 100 and lat["order_latency_ms"] >= 50     # H1 / H3


# ---------------------------------------------------------------- M2: the paper trader's re-ordering
from mm.paper.reorder import ReorderBuffer


def test_reorder_buffer_sorts_by_time_then_kind_priority_and_respects_hold():
    prio = {"funding": 1, "trade": 2, "depth": 3, "quote": 4}
    buf = ReorderBuffer(hold_ns=100, priority=prio)
    d = DepthEvent(ms(2), None, None, False, 0, 0, None, (), ())
    t = trade(2, 100.0, 1.0, "sell")
    g = GapEvent(ts_ns=ms(2), reason="resync", stream=None)
    early = trade(1, 100.0, 1.0, "sell")
    for ev, arr in ((d, 0), (t, 10), (g, 20), (early, 30)):            # arrival order: depth, trade, gap, early
        buf.push(ev, arr)
    assert buf.pop_ready(50) == []                                      # nothing has waited 100 yet... (head age 30)
    out = buf.pop_ready(130)
    assert [e.kind for e in out] == ["trade", "gap", "trade", "depth"]  # early trade, then gap, trade, depth
    assert out[0] is early and len(buf) == 0


def test_paper_trader_reorders_events_and_handles_gap(tmp_path, monkeypatch):
    """Fake feed delivers a depth message BEFORE the trade it reflects; the paper trader must hand the
    engine the trade first (replay order), then react to a gap marker."""
    import mm.paper.run as paper
    seen, holder = [], {}
    real_build = paper.build_engine

    def spy_build(cfg, **kw):
        eng = real_build(cfg, **kw)
        holder["eng"] = eng
        orig = eng.on_market_event
        eng.on_market_event = lambda ev: (seen.append((ev.kind, ev.ts_ns)), orig(ev))[1]
        return eng
    monkeypatch.setattr(paper, "build_engine", spy_build)

    snap = DepthEvent(ms(0), None, None, True, 1, 1, None, ((100.00, 4.0),), ((100.01, 3.0),))
    depth_msg = DepthEvent(ms(5), None, None, False, 2, 2, 1, ((100.00, 2.0),), ())
    trade_msg = TradeEvent(ms(5), None, None, 1, 1, 1, 100.00, 2.0, "sell")
    gap = GapEvent(ts_ns=ms(9), reason="disconnect", stream=None)

    class FakeFeed:
        async def stream(self):
            for ev in (snap, depth_msg, trade_msg, gap):        # depth arrives before the trade
                await asyncio.sleep(0.005)
                yield ev
            await asyncio.sleep(0.4)                            # let the hold expire, then end

    cfg = deep_merge(DEFAULTS, {
        "run": {"out_dir": str(tmp_path)},
        "data": {"source": "live", "streams": ["trades", "quotes", "depth"]},
        "fill_model": {"type": "l2"},
        "latency": {"feed_latency_ms": 1, "order_latency_ms": 1, "report_latency_ms": 1,
                    "order_jitter_ms": 0, "feed_jitter_ms": 0, "timer_interval_ms": 0,
                    "feed_delay_quantiles_ms": None},
        "paper": {"reorder_hold_ms": 100, "reorder_poll_ms": 10, "heartbeat_s": 0.05, "log_interval_s": 60}})
    out = asyncio.run(paper.run_paper(cfg, None, None, tmp_path, feed=FakeFeed()))
    # snapshot first, then at ts=5 the trade BEFORE the depth update (replay order), then the gap
    assert [k for k, _ in seen] == ["depth", "trade", "depth", "gap"]
    assert seen[1] == ("trade", ms(5)) and seen[2] == ("depth", ms(5))   # trade BEFORE the depth it caused
    assert holder["eng"].ex.awaiting_resync                                # gap blocked quoting
    for f in ("summary.md", "metrics.json", "fills.parquet", "pnl_log.csv", "fills_live.csv"):
        assert (out / f).exists(), f


# ================================================================ re-review fixes (R1, R2, R3, R5, M5)
from mm.data.events import StatusEvent


# ---------------------------------------------------------------- R1: gaps depend on WHICH feed broke
# GapEvent.stream tells us. "market" = the TRADES websocket dropped: the book (a different socket) is
# still valid, so keep it and keep quoting (orders are cancelled because prints may have been missed).
# "public" / "depth" / None = the book socket (or unknown): wipe the book and wait for a snapshot.
def _l2_engine_with_resting_order():
    eng, s = make([(ms(2), PlaceOrder("b", "buy", 0.1, 100.00)), (ms(7), PlaceOrder("c", "buy", 0.1, 100.00))],
                  fill_model={"type": "l2"}, data={"streams": ["trades", "depth"]})
    feed(eng, [depth(0, [(100.00, 4.0), (99.99, 9.0)], [(100.01, 3.0), (100.02, 5.0)], snap=True),
               depth(1, [(99.99, 9.5)], []), depth(2, [(99.99, 9.6)], [])])
    assert "b" in eng.ex.resting
    return eng, s


def test_trades_socket_gap_keeps_the_book_and_resumes_quoting_at_once():
    eng, s = _l2_engine_with_resting_order()
    feed(eng, [GapEvent(ts_ns=ms(5), reason="disconnect", stream="market")])
    assert not eng.ex.resting                                  # orders cancelled (we may have missed prints)
    assert any(u.client_id == "b" and u.status == "canceled" and u.reason == "gap" for u in s.updates)
    assert not eng.ex.awaiting_resync                          # NOT blocked
    assert eng.ex.mkt.book.asks == {100.01: 3.0, 100.02: 5.0}  # book intact, asks still there
    assert eng.state.bid_px == 100.00 and eng.state.ask_px == 100.01   # strategy still sees prices
    feed(eng, [depth(6, [(99.99, 9.7)], []), depth(7, [(99.99, 9.8)], []), depth(8, [(99.99, 9.9)], [])])
    assert [u.status for u in s.updates if u.client_id == "c"] == ["accepted"]   # quoting resumed, no snapshot


@pytest.mark.parametrize("stream", ["public", "depth", None])
def test_book_or_unknown_gap_wipes_the_book_and_waits_for_a_snapshot(stream):
    eng, s = _l2_engine_with_resting_order()
    feed(eng, [GapEvent(ts_ns=ms(5), reason="disconnect", stream=stream)])
    assert eng.ex.awaiting_resync and not eng.ex.resting and not eng.ex.mkt.book.bids
    assert eng.state.bid_px is None
    feed(eng, [depth(6, [(99.99, 9.7)], []), depth(7, [(99.99, 9.8)], []), depth(8, [(99.99, 9.9)], [])])
    assert [(u.status, u.reason) for u in s.updates if u.client_id == "c"] == [("rejected", "awaiting_resync")]
    feed(eng, [depth(9, [(100.00, 4.0)], [(100.01, 3.0)], snap=True)])      # the snapshot lifts the block
    assert not eng.ex.awaiting_resync


# The same through the paper trader's path: a fake feed with a trades-socket gap (GapEvent AND the
# StatusEvent LiveFeed also emits) must leave the book usable; a "public" status must block until snapshot.
def _paper_run(tmp_path, monkeypatch, events_after_snapshot):
    import mm.paper.run as paper
    holder = {}
    real_build = paper.build_engine
    monkeypatch.setattr(paper, "build_engine", lambda cfg, **kw: holder.setdefault("eng", real_build(cfg, **kw)))
    snap = DepthEvent(ms(0), None, None, True, 1, 1, None, ((100.00, 4.0),), ((100.01, 3.0), (100.02, 5.0)))

    class FakeFeed:
        async def stream(self):
            yield snap
            for ev in events_after_snapshot:
                await asyncio.sleep(0.02)
                yield ev
            await asyncio.sleep(0.4)

    cfg = deep_merge(DEFAULTS, {
        "run": {"out_dir": str(tmp_path)}, "data": {"source": "live", "streams": ["trades", "quotes", "depth"]},
        "fill_model": {"type": "l2"},
        "latency": {"feed_latency_ms": 1, "order_latency_ms": 1, "report_latency_ms": 1, "order_jitter_ms": 0,
                    "feed_jitter_ms": 0, "timer_interval_ms": 0, "feed_delay_quantiles_ms": None},
        "paper": {"reorder_hold_ms": 50, "reorder_poll_ms": 10, "heartbeat_s": 0.05, "log_interval_s": 60}})
    asyncio.run(paper.run_paper(cfg, None, None, tmp_path, feed=FakeFeed()))
    return holder["eng"]


def test_paper_trades_socket_gap_keeps_the_book(tmp_path, monkeypatch):
    eng = _paper_run(tmp_path, monkeypatch, [
        StatusEvent(ms(1), ms(1), "disconnected", "market: ConnectionClosed"),
        GapEvent(ts_ns=ms(1) + 1, reason="disconnect", stream="market")])
    assert not eng.ex.awaiting_resync and eng.ex.mkt.book.asks == {100.01: 3.0, 100.02: 5.0}


@pytest.mark.parametrize("detail,stream", [("public: ConnectionClosed", "public"), ("garbage", None)])
def test_paper_book_socket_or_unparseable_disconnect_blocks_until_snapshot(tmp_path, monkeypatch, detail, stream):
    eng = _paper_run(tmp_path, monkeypatch, [
        StatusEvent(ms(1), ms(1), "disconnected", detail),
        GapEvent(ts_ns=ms(1) + 1, reason="disconnect", stream=stream)])
    assert eng.ex.awaiting_resync and not eng.ex.mkt.book.bids


def test_status_stream_parser():
    from mm.paper.run import _status_stream
    assert _status_stream(StatusEvent(0, 0, "disconnected", "market: x")) == "market"
    assert _status_stream(StatusEvent(0, 0, "error", "public: boom")) == "public"
    assert _status_stream(StatusEvent(0, 0, "book_gap", "sequence gap")) == "depth"
    assert _status_stream(StatusEvent(0, 0, "disconnected", "no endpoint here")) is None


# ---------------------------------------------------------------- R2: crossing-episode memory
# A 2.0 ETH bid, the market sits BELOW it for many quotes, and only the displayed ask size flickers between
# 0.5 and 0.4 ETH. Old behaviour refilled from the displayed size every quote (total 2.0 ETH, no trade).
# Now the available size only grows when the displayed size grows, and resets when the quote stops crossing.
def test_repeated_crossed_quotes_cannot_refill_the_same_liquidity():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 2.0, 100.00))])
    evs = [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)]
    for i in range(10):
        evs.append(quote(2 + i, 99.97, 1.0, 99.99, 0.5 if i % 2 == 0 else 0.4))
    feed(eng, evs)
    first = sum(f.qty for f in s.fills)
    assert first == pytest.approx(0.5 + 0.1 * 4)           # 0.5, then only the +0.1 growth each time it grows
    assert first < 1.0
    feed(eng, [quote(20, 100.00, 5.0, 100.01, 5.0), quote(21, 99.97, 1.0, 99.99, 0.5)])   # stops crossing, then a NEW episode
    assert sum(f.qty for f in s.fills) == pytest.approx(first + 0.5)


# ---------------------------------------------------------------- R3: simulated feed delay matches the measured shape
# Draw the delay for 600k archive-style quotes (E == T) and compare the effective percentiles of
# "time the strategy saw it minus event time" with the configured table (p50 239 / p90 296 / p99 772 ms,
# mm.data.latency). Tolerances: p50 5%, p99 15%, p90 30% (known limit: the ordering rule drains a backlog after each slow\n# window, which stretches spikes and lifts p90 by ~20-25%; p90 is the conservative direction).
def test_simulated_feed_delay_percentiles_match_the_configured_table():
    seen = []

    class Spy(Strategy):
        event_kinds = ("quote",)

        def on_event(self, ev, state):
            seen.append((state.now_ns - ev.ts_ns) / 1e6)
    cfg = deep_merge(DEFAULTS, {"latency": {"timer_interval_ms": 0}})
    tbl = {p: d for p, d in cfg["latency"]["feed_delay_quantiles_ms"]}
    eng = build_engine(cfg, seed=5, strategy_cls=Spy)
    base = 1_700_000_000_000_000_000
    # one quote per 100 ms for 60,000 s = 30,000 two-second delay windows (enough tail windows for a stable p99)
    for i in range(600_000):
        t = base + i * 100_000_000
        eng.on_market_event(QuoteEvent(t, t, None, i, 100.0, 1.0, 100.01, 1.0))
    eng.finish()
    p50, p90, p99 = np.percentile(seen, [50, 90, 99])
    assert p50 == pytest.approx(tbl[0.5], rel=0.05)
    assert p90 == pytest.approx(tbl[0.9], rel=0.30)
    assert p99 == pytest.approx(tbl[0.99], rel=0.15)
    assert min(seen) >= tbl[0.0] - 1                       # never faster than the measured floor


def test_latency_table_must_be_a_valid_distribution():
    from mm.backtest.config import validate_latency
    bad = deep_merge(DEFAULTS, {"latency": {"feed_delay_quantiles_ms": [[0.0, 100.0], [0.5, 300.0], [0.9, 200.0], [1.0, 900.0]]}})
    with pytest.raises(ValueError, match="non-decreasing"):
        validate_latency(bad)


# ---------------------------------------------------------------- required sensitivity row + table cell
def test_no_cross_fills_is_a_required_sensitivity_row():
    from mm.backtest.config import validate
    cfg = deep_merge(DEFAULTS, {})
    cfg["sensitivity"]["variants"].pop("no_cross_fills")
    with pytest.raises(ValueError, match="no_cross_fills"):
        validate(cfg)


def test_sensitivity_cell_shows_qty_and_spread_capture_per_mechanism():
    from mm.backtest.report import _mech_cell
    cell = _mech_cell({"mechanisms": {"through": [691.3, 23.74], "cross": [340.1, 2.68]}})
    assert cell == "0.0 / +0.00; 691.3 / +23.74; 340.1 / +2.68; 0.0 / +0.00"


# ---------------------------------------------------------------- R5: paper advance keeps the tie rule
def test_advance_does_not_run_arrivals_due_exactly_now():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.1, 100.00))], latency={"order_latency_ms": 29})
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)])   # arrival at 30
    eng.advance(ms(30))
    assert "b" not in eng.ex.resting                       # market events at 30 ms may still be coming
    eng.advance(ms(31))
    assert "b" in eng.ex.resting


# ---------------------------------------------------------------- M5: manifest completeness + code hash
def test_manifest_covers_depth_gap_markers_and_resolved_sources(monkeypatch):
    import mm.backtest.run as runmod
    calls = []
    monkeypatch.setattr(runmod, "find_files", lambda st, a, b, sym, src, td: calls.append((st, src)) or [])
    cfg = deep_merge(DEFAULTS, {"fill_model": {"type": "l2"},
                                "data": {"streams": ["trades", "depth", "funding"],
                                         "source": {"funding": "archive"}}})   # depth NOT listed: defaults to live
    runmod.period_files(cfg, {"start": "2026-10-19", "end": "2026-10-19"})
    assert ("depth", "live") in calls and ("gaps", "live") in calls and ("funding", "archive") in calls


def test_code_manifest_hashes_the_source_files_that_ran():
    from mm.backtest.report import code_manifest
    from mm.data.config import PROJECT_ROOT
    m = code_manifest(PROJECT_ROOT)
    paths = {e["path"].replace("\\", "/") for e in m["files"]}
    assert "src/mm/backtest/engine.py" in paths and "src/mm/backtest/fills.py" in paths
    assert len(m["combined_sha256"]) == 64 and code_manifest(PROJECT_ROOT)["combined_sha256"] == m["combined_sha256"]


# R2 two-level flip: the crossing ask alternates between 99.99 and 99.98, each showing 0.5 ETH. Remembering
# only the CURRENT level refilled the order every time the quote flipped (the probe filled all 2.0 ETH).
# With memory per level for the whole episode each level can give us its 0.5 ETH once: 1.0 in total.
def test_ask_flipping_between_two_levels_cannot_refill_consumed_liquidity():
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 2.0, 100.00))])
    evs = [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0)]
    for i in range(12):
        evs.append(quote(2 + i, 99.96, 1.0, 99.99 if i % 2 == 0 else 99.98, 0.5))
    feed(eng, evs)
    assert sum(f.qty for f in s.fills) == pytest.approx(1.0)


# The required no_cross_fills row still runs when the other sensitivity variants are off (disabled in the
# config or --no-sensitivity). run_period is stubbed with an empty engine run and write_report captures
# what would be written, so this checks only the orchestration in run_all.
@pytest.mark.parametrize("flag", [False, None])
def test_no_cross_fills_row_runs_even_without_full_sensitivity(tmp_path, monkeypatch, flag):
    import mm.backtest.run as runmod
    ran = []

    def fake_run_period(cfg, period, seed, verbose=True, manifest=False):
        ran.append(cfg["fill_model"]["cross_fills"])
        eng = build_engine(deep_merge(DEFAULTS, {}), seed=1)
        m, fills, eq = eng.finish()
        return m, fills, eq, {"start": "x", "end": "y", "events": 0, "wall_seconds": 0.0}
    captured = {}
    monkeypatch.setattr(runmod, "run_period", fake_run_period)
    monkeypatch.setattr(runmod, "write_report", lambda out, cfg, raw, res, sens, meta, title: captured.update(sens=sens) or out)
    cfg = deep_merge(DEFAULTS, {"data": {"periods": {"p": {"start": "2024-03-29", "end": "2024-03-29"}}},
                                "sensitivity": {"enabled": False}})
    runmod.run_all(cfg, None, sensitivity=flag, out_root=tmp_path)
    assert list(captured["sens"]) == ["no_cross_fills"]
    assert ran == [True, False]                      # base (cross on) then the no-cross variant
