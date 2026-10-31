"""Tests for the 2026-10-22 engine fixes from the GLFT review: IOC never rests, replace re-queues,
total-size replace, separate new/replace counts, strategy summary, markout bootstrap, data coverage.

Helpers (make, feed, quote, trade, ms) come from test_backtest_fills.py; latencies there are 0 unless a test sets them.
"""
import pytest

from mm.backtest.metrics import _bootstrap_ci
from mm.backtest.report import _strategy_summary_table
from mm.backtest.strategy import CancelOrder, PlaceOrder, ReplaceOrder, Strategy
from test_backtest_fills import feed, make, ms, quote, trade


# ---------------------------------------------------------------- 1. IOC never rests
# Bid 100.00 / ask 100.01. A sell IOC at 100.05 is NOT marketable (nobody bids that high). Binance expires
# it at once; the old engine left it resting, so the later buy print at 100.06 filled it as a maker.
def test_non_marketable_ioc_is_cancelled_and_never_rests():
    eng, s = make([(ms(1), PlaceOrder("i", "sell", 0.1, 100.05, tif="IOC"))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(3, 100.06, 1.0, "buy"), quote(4, 100.00, 5.0, 100.01, 5.0)])
    assert [(u.status, u.reason) for u in s.updates] == [("accepted", ""), ("canceled", "no_liquidity")]
    assert not eng.ex.resting and not s.fills


# A buy IOC at 100.01 against 0.2 ETH displayed: it takes the 0.2 at the touch, and the rest (0.3) is
# cancelled instead of resting at 100.01.
def test_partly_marketable_ioc_fills_then_cancels_remainder():
    eng, s = make([(ms(1), PlaceOrder("i", "buy", 0.5, 100.01, tif="IOC"))])
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 0.2), quote(1, 100.00, 5.0, 100.01, 0.2),
               trade(3, 100.02, 1.0, "sell"), quote(4, 100.00, 5.0, 100.01, 0.2)])
    assert sum(f.qty for f in s.fills) == pytest.approx(0.2) and all(f.liquidity == "taker" for f in s.fills)
    assert s.updates[-1].status == "canceled" and s.updates[-1].reason == "ioc_remainder"
    assert s.updates[-1].remaining == pytest.approx(0.3)
    assert not eng.ex.resting


# ---------------------------------------------------------------- 2a. replace re-queues (Binance) or keeps priority (config)
# Our bid joins behind 5.0 displayed. A 2.0 print leaves 3.0 ahead. Then the displayed size grows to 8.0
# (others queue behind us). Cutting our size: old rule keeps q_ahead 3.0; Binance rule goes to the back
# of the now 8.0-long queue.
def _size_cut(**exchange):
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.2, 100.00)), (ms(5), ReplaceOrder("b", qty=0.1))],
                  exchange=exchange)
    feed(eng, [quote(0, 100.00, 5.0, 100.01, 5.0), quote(1, 100.00, 5.0, 100.01, 5.0),
               trade(2, 100.00, 2.0, "sell"), quote(3, 100.00, 8.0, 100.01, 5.0),
               quote(4, 100.00, 8.0, 100.01, 5.0), quote(6, 100.00, 8.0, 100.01, 5.0),
               quote(7, 100.00, 8.0, 100.01, 5.0)])
    return eng.ex.resting["b"], s


def test_replace_size_cut_goes_to_back_of_queue_by_default():
    o, s = _size_cut()
    assert o.remaining == pytest.approx(0.1) and o.q_ahead == pytest.approx(8.0)
    assert [u.status for u in s.updates] == ["accepted", "replaced"]


def test_replace_size_cut_keeps_priority_when_configured():
    o, s = _size_cut(replace_keeps_priority=True)
    assert o.remaining == pytest.approx(0.1) and o.q_ahead == pytest.approx(3.0)
    assert s.updates[-1].reason == "kept_priority"


# ---------------------------------------------------------------- 2b. total-size replace
def test_replace_cannot_give_both_qty_and_total_qty():
    with pytest.raises(ValueError):
        ReplaceOrder("b", qty=0.1, total_qty=0.2)
    assert ReplaceOrder("b", None, 0.1).qty == 0.1          # old positional use still works


# 0.3 placed, 0.1 filled -> 0.2 left. total_qty=0.25 means 0.25 in all, i.e. 0.15 remaining.
def _partial_then(action, **over):
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.3, 100.00)), (ms(5), action)], **over)
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 100.00, 0.1, "sell"), quote(4, 100.00, 0.0, 100.01, 5.0),
               quote(6, 100.00, 0.0, 100.01, 5.0), quote(7, 100.00, 0.0, 100.01, 5.0)])
    return eng, s


def test_total_qty_replace_subtracts_what_is_already_filled():
    eng, s = _partial_then(ReplaceOrder("b", total_qty=0.25))
    assert eng.ex.resting["b"].remaining == pytest.approx(0.15)
    assert eng.ex.resting["b"].qty == pytest.approx(0.25)    # TOTAL size = filled 0.1 + remaining 0.15
    assert s.updates[-1].status == "replaced" and s.updates[-1].remaining == pytest.approx(0.15)


def test_total_qty_at_or_below_filled_cancels_the_order():
    # Binance Modify Order docs: new total <= filled quantity -> the order is cancelled (not tested on testnet)
    eng, s = _partial_then(ReplaceOrder("b", total_qty=0.1))
    assert s.updates[-1].status == "canceled" and s.updates[-1].reason == "qty_below_filled"
    assert "b" not in eng.ex.resting


# The point of total_qty: the strategy wants 0.3 ETH in total and sends the replace BEFORE it has heard
# about a 0.1 fill (order 10 ms, report 50 ms). qty=0.3 would leave 0.4 ETH of exposure; total_qty=0.3 leaves 0.3.
def _fill_in_flight(action):
    eng, s = make([(ms(1), PlaceOrder("b", "buy", 0.3, 100.00)), (ms(12), action)],
                  latency={"order_latency_ms": 10, "report_latency_ms": 50})
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               quote(12, 100.00, 0.0, 100.01, 5.0), trade(15, 100.00, 0.1, "sell"),   # fills 0.1 before the replace arrives (22 ms)
               quote(30, 100.00, 0.0, 100.01, 5.0), quote(31, 100.00, 0.0, 100.01, 5.0)])
    return eng, s


def test_replace_remaining_qty_overshoots_when_a_fill_is_in_flight():
    eng, s = _fill_in_flight(ReplaceOrder("b", qty=0.3))
    assert eng.account.position + eng.ex.resting["b"].remaining == pytest.approx(0.4)   # the problem


def test_replace_total_qty_does_not_overshoot_when_a_fill_is_in_flight():
    eng, s = _fill_in_flight(ReplaceOrder("b", total_qty=0.3))
    assert eng.account.position + eng.ex.resting["b"].remaining == pytest.approx(0.3)


# ---------------------------------------------------------------- 3b. order counts
def test_new_orders_and_replaces_are_counted_apart():
    eng, s = make([(ms(1), PlaceOrder("a", "buy", 0.1, 99.95)), (ms(1), PlaceOrder("b", "buy", 0.1, 100.00)),
                   (ms(4), ReplaceOrder("b", price=99.98)), (ms(4), ReplaceOrder("b", price=99.97)),
                   (ms(4), ReplaceOrder("zz", price=99.97))])
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               quote(4, 100.00, 0.0, 100.01, 5.0), trade(6, 99.96, 1.0, "sell"), quote(8, 100.00, 0.0, 100.01, 5.0)])
    m, _, _ = eng.finish()
    o = m["orders"]
    assert (o["accepted"], o["rejected"]) == (2, 0)                  # replaces are not "orders"
    assert (o["replace_requests"], o["replaced"], o["replace_rejected"]) == (3, 2, 1)
    assert o["replace_reject_reasons"] == {"unknown_order": 1}
    # "b" was replaced twice, then filled: ONE of the two new orders had a fill ...
    assert o["fill_ratio_orders"] == pytest.approx(1 / 2)
    # ... but only 1 of the 4 accepted actions (2 new + 2 replaces) ended in a fill
    assert o["fill_ratio_per_action"] == pytest.approx(1 / 4)


# ---------------------------------------------------------------- 3a. strategy summary
class WithSummary(Strategy):
    event_kinds = ("quote",)

    def summary(self):
        return {"pauses": {"spread": 3, "vol": 0}, "kills": {}, "killed": False, "name": "x"}


def test_strategy_summary_is_stored_in_metrics_and_shown_as_table():
    eng, _ = make([])
    eng.strategy = WithSummary()
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 1.0)])
    m, _, _ = eng.finish()
    assert m["strategy_summary"]["pauses"] == {"spread": 3, "vol": 0}
    table = _strategy_summary_table({"in_sample": {"metrics": m}, "out_of_sample": {"metrics": {}}})
    assert "| pauses.spread | 3 |" in table and "| killed | False |" in table
    assert "name" not in table                                         # text fields are skipped, numbers only


def test_strategy_without_summary_adds_nothing():
    eng, _ = make([])
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 1.0)])
    assert "strategy_summary" not in eng.finish()[0]


# ---------------------------------------------------------------- 3c. markout bootstrap
def test_bootstrap_ci_is_seeded_and_brackets_the_mean():
    import numpy as np
    v = np.array([-0.2, 0.1, 0.05, -0.1, 0.3, -0.05, 0.0, 0.2])
    w = np.ones_like(v)
    a, b = _bootstrap_ci(v, w, 500, seed=7), _bootstrap_ci(v, w, 500, seed=7)
    assert a == b and a[0] <= v.mean() <= a[1]
    assert _bootstrap_ci(v[:1], w[:1], 500, 7) is None                # one fill: no interval


def test_markout_ci_appears_in_metrics():
    eng, s = make([(ms(1), PlaceOrder("a", "buy", 0.1, 100.00)), (ms(1), PlaceOrder("b", "buy", 0.1, 100.00))])
    feed(eng, [quote(0, 100.00, 0.0, 100.01, 5.0), quote(1, 100.00, 0.0, 100.01, 5.0),
               trade(2, 100.00, 0.1, "sell"), trade(3, 100.00, 0.1, "sell"),
               quote(20_000, 100.10, 1.0, 100.11, 1.0)])
    m, _, _ = eng.finish()
    mk = m["markouts"]["10s"]
    assert mk["n_fills"] == 2
    lo, hi = mk["avg_markout_per_eth_ci95"]
    assert lo <= mk["avg_markout_per_eth"] <= hi


# ---------------------------------------------------------------- 3d. data coverage
def test_coverage_excludes_silences_longer_than_threshold():
    eng, s = make([])
    # events at 0 s, 10 s, 20 s, then a 1000 s silence, then 1010 s: covered = 30 s, one gap of 980 s
    feed(eng, [quote(0, 100.00, 1.0, 100.01, 1.0), quote(10_000, 100.00, 1.0, 100.01, 1.0),
               quote(20_000, 100.00, 1.0, 100.01, 1.0), quote(1_000_000 + 20_000, 100.00, 1.0, 100.01, 1.0),
               quote(1_000_000 + 30_000, 100.00, 1.0, 100.01, 1.0)])
    cov = eng.finish()[0]["coverage"]
    assert cov["n_gaps"] == 1 and cov["covered_hours"] * 3600 == pytest.approx(30.0)
    assert cov["longest_gaps"][0]["seconds"] == pytest.approx(1000.0)
