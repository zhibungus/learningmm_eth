"""Tests for the fee-aware GLFT strategy (src/mm/strategies/glft.py) against specs/glft-perp.md.

Three layers
    1. pure formulas vs the HAND-COMPUTED worked example of spec §10 (m = 2400.005, gamma = 1, ...);
    2. strategy callbacks driven directly with a hand-built MarketState (sizing, requoting, risk);
    3. a small engine-level run on synthetic quote/trade events (zero latency so order is readable).
"""
# pytest runs every function named test_* in files named test_*.py; a test passes if it ends without an
# exception (a failing "assert" raises one).
#   pytest.approx(x, abs=tol / rel=tol): float comparison with a tolerance (floats are never exact after arithmetic).
#   pytest.raises(Err): the block must raise Err.
#   @pytest.mark.parametrize("a, b", [...]): run the same test once per row.
#   Fixtures (@pytest.fixture) share setup; this file uses plain helper functions instead.
import math
from dataclasses import replace      # copy a frozen dataclass with some fields changed

import pytest

from mm.backtest.config import DEFAULTS, deep_merge, load_config
from mm.backtest.engine import build_engine, resolve_strategy
from mm.backtest.strategy import (CancelAll, CancelOrder, Fill, MarketState, OrderUpdate, OrderView,
                                  PlaceOrder, ReplaceOrder)
from mm.data.config import PROJECT_ROOT
from mm.data.events import GapEvent, QuoteEvent, TradeEvent
from mm.strategies.glft import (GlftMarketMaker, GlftParams, ceil_to_tick, closed_form, compute_quotes,
                                floor_to_tick)
from mm.strategies.glft_calibrate import CalParams, solve_gamma

TICK = 0.01               # USDT
MID = 2400.005            # book 2400.00 / 2400.01 (spec §10)
NS = 1_000_000_000        # ns per second
CAL = CalParams()         # default gamma bracket for solve_gamma


# ------------------------------------------------------------------ helpers
# "**over" collects extra keyword arguments into a dict, so params(sigma=5) overrides one field;
# {**a, **b} merges two dicts (later keys win).
def params(**over) -> GlftParams:
    """Spec §10 inputs: sigma 0.26, A 4, k 10, gamma 1 (fixed for the test), exact mode."""
    return GlftParams(**{"gamma": 1.0, **over})


def quotes(q, p=None, mid=MID, bid=2400.00, ask=2400.01):
    return compute_quotes(p or params(), mid, q, bid, ask, TICK)


def make_state(bid=2400.00, ask=2400.01, pos=0.0, now=10 * NS) -> MarketState:
    st = MarketState(TICK, 0.001)
    st.bid_px, st.bid_qty, st.ask_px, st.ask_qty = bid, 5.0, ask, 5.0
    st._has_quotes = True
    st.position, st.now_ns, st.cash = pos, now, 10_000.0
    return st


def make_strategy(**over) -> GlftMarketMaker:
    s = GlftMarketMaker({"gamma": 1.0, "print_stats_on_stop": False, **over})
    return s


def qev(state, t_ns=None):
    return QuoteEvent(t_ns if t_ns is not None else state.now_ns, None, None, 0,
                      state.bid_px, 5.0, state.ask_px, 5.0)


def start(s, st):
    s.on_start(st)
    return s


# ------------------------------------------------------------------ 1. formulas vs spec §10
# GROUP 1: recompute the spec's hand-calculated worked example (m = 2400.005, gamma = 1, exact fee mode).
# If any number (fee c, c1, A_eff, c2, omega, h) or quote price differs, the code does not match the spec.
# Lessons: L1 §3 (fee per side), L1 §8 (reservation price); the GLFT formulas are not yet covered.
def test_worked_example_intermediate_numbers():
    cf = closed_form(params(), MID)
    assert cf.c == pytest.approx(0.480001, abs=1e-9)
    assert cf.c1 == pytest.approx(0.0999500, abs=1e-7)
    assert cf.a_use == pytest.approx(0.0329187, abs=1e-7)
    assert cf.c2 == pytest.approx(20.3245, abs=1e-4)
    assert cf.omega == pytest.approx(5.28437, abs=1e-5)
    assert cf.h == pytest.approx(0.606373, abs=1e-6)


# One row per inventory q: expected bid / ask (None = that side is not sent at that inventory).
@pytest.mark.parametrize("q, bid, ask", [
    (0.0, 2399.39, 2400.62),
    (0.05, None, 2400.35),        # bid at 2399.13 exists as a price but is not sent (headroom 0)
    (-0.05, 2399.66, None),
    (0.04, 2399.18, 2400.40),
])
def test_worked_example_quote_prices(q, bid, ask):
    t = quotes(q)
    if bid is not None:
        assert t.bid_px == pytest.approx(bid, abs=1e-9)
    if ask is not None:
        assert t.ask_px == pytest.approx(ask, abs=1e-9)


def test_worked_example_deltas_and_reservation_price():
    t = quotes(0.05)
    assert t.delta_b == pytest.approx(0.870592, abs=1e-6)
    assert t.delta_a == pytest.approx(0.342154, abs=1e-6)
    assert t.bid_px == pytest.approx(2399.13, abs=1e-9)           # the price exists, just not sent
    assert MID - 0.05 * t.cf.omega == pytest.approx(2399.740781, abs=1e-6)   # r = m - q*omega


# Second spec test vector: the naive 'shift by fee' mode (skew about e^(k*c/2) = 11x smaller).
def test_shift_mode_vector():
    cf = closed_form(params(fee_mode="shift"), MID)
    assert cf.c2 == pytest.approx(1.84379, abs=1e-5)
    # spec prints 0.479386 (= rounded c2 1.84379 * 0.26); exact value is 0.4793847: spec rounding only
    assert cf.omega == pytest.approx(0.479386, abs=5e-6)
    assert cf.h == pytest.approx(0.582348, abs=1e-6)
    t = quotes(0.0, params(fee_mode="shift"))
    assert (t.bid_px, t.ask_px) == (pytest.approx(2399.42), pytest.approx(2400.59))
    # skew is e^(k*c/2) ~ 11x smaller than in exact mode
    ratio = closed_form(params(), MID).omega / cf.omega
    assert ratio == pytest.approx(math.exp(10 * 0.480001 / 2), rel=1e-9)


# Third spec vector: gamma is solved so that skew at max inventory equals 25 ticks (spec §6.4), by bisection.
def test_target_skew_gamma_vector():
    p = params()
    target_omega = 25 * TICK / 0.05                                  # 5.0 USDT/ETH
    g = solve_gamma(p, CAL, target_omega, MID)
    assert g == pytest.approx(0.8953, abs=1e-4)
    pg = replace(p, gamma=g)                                         # same params with the solved gamma
    cf = closed_form(pg, MID)
    assert cf.omega == pytest.approx(5.0, rel=1e-5)
    assert cf.c1 == pytest.approx(0.099955, abs=1e-6)
    assert cf.h == pytest.approx(0.604956, abs=1e-6)
    assert cf.c2 == pytest.approx(19.2308, abs=1e-3)
    t0 = compute_quotes(pg, MID, 0.0, 2400.00, 2400.01, TICK)
    tq = compute_quotes(pg, MID, 0.05, 2400.00, 2400.01, TICK)
    assert (t0.bid_px, t0.ask_px) == (pytest.approx(2399.40), pytest.approx(2400.61))
    assert tq.ask_px == pytest.approx(2400.36)
    assert tq.delta_a == pytest.approx(0.354956, abs=1e-6)


def test_target_skew_same_omega_in_both_fee_modes_but_not_identical_quotes():
    # Under target-skew gamma both fee modes hit the same omega, but gamma (and so c1) differs:
    # ~0.895 (exact) vs ~103.5 (shift). So do NOT assert identical quotes.
    ge = solve_gamma(params(), CAL, 5.0, MID)
    gs = solve_gamma(params(fee_mode="shift"), CAL, 5.0, MID)
    assert gs == pytest.approx(103.5, rel=0.01)
    ce, cs = closed_form(params(gamma=ge), MID), closed_form(params(fee_mode="shift", gamma=gs), MID)
    assert ce.omega == pytest.approx(cs.omega, rel=1e-5)
    assert ce.c1 == pytest.approx(0.09996, abs=2e-5) and cs.c1 == pytest.approx(0.0952, abs=1e-4)
    assert (ce.h - cs.h) / TICK == pytest.approx(0.5, abs=0.15)


# pytest.raises: asking for an impossible skew must fail loudly, not return a wrong gamma.
def test_solve_gamma_out_of_bracket_is_an_error():
    with pytest.raises(ValueError):
        solve_gamma(params(), CAL, 1e9, MID)


# The xi = 0 forms are a limit of the xi = gamma forms; they should agree to a tiny fraction of a tick.
def test_xi_zero_limit_is_close_to_xi_equals_gamma():
    a = closed_form(params(), MID)
    b = closed_form(params(xi_equals_gamma=False), MID)
    assert b.c1 == pytest.approx(0.1, abs=1e-12)                    # 1/k
    assert abs(a.h - b.h) < 0.0005                                  # "within 0.01 tick" scale (spec §4.1)


# ------------------------------------------------------------------ inventory skew and fee effect
# GROUP 2: a long position must move BOTH quotes down (ask nearer mid to sell sooner, bid further away to
# buy less; L1 §8). A higher fee widens the half-spread; zero fee gives c = 0 and A_eff = A.
def test_long_inventory_lowers_both_quotes_short_raises_both():
    flat, long_, short = quotes(0.0), quotes(0.03), quotes(-0.03)
    assert long_.bid_px < flat.bid_px and long_.ask_px < flat.ask_px
    assert short.bid_px > flat.bid_px and short.ask_px > flat.ask_px
    # reservation price moves by exactly q*omega: the bid distance grows, the ask distance shrinks
    assert long_.delta_b > flat.delta_b > long_.delta_a


def test_higher_maker_fee_widens_half_spread_zero_fee_has_c_zero():
    lo, hi = closed_form(params(maker_fee=0.0001), MID), closed_form(params(maker_fee=0.0004), MID)
    assert hi.h > lo.h
    assert hi.c == pytest.approx(2 * 0.0004 * MID / 2)
    zero = closed_form(params(maker_fee=0.0), MID)
    assert zero.c == 0.0 and zero.a_use == pytest.approx(4.0)       # A_eff = A when c = 0
    assert zero.h == pytest.approx(zero.c1 + 0.005 * zero.omega)


# The fee distance is not a floor for the inventory-reducing side; only delta_min is (spec §4.4).
def test_reducing_side_may_quote_inside_the_fee_but_not_inside_delta_min():
    t = quotes(0.05)
    assert t.delta_a < t.cf.c                                        # 34 ticks < 48 ticks fee
    huge = params(sigma=50.0)                                        # extreme volatility
    th = compute_quotes(huge, MID, 0.05, 2400.00, 2400.01, TICK)
    assert th.delta_a == pytest.approx(10 * TICK)                    # floor delta_min binds


def test_skew_off_variant_ignores_inventory():
    p = params(skew_off=True)
    assert quotes(0.05, p).bid_px == quotes(0.0, p).bid_px


# ------------------------------------------------------------------ tick rounding and no-cross
# GROUP 3: bids round DOWN and asks UP (never tighter than the formula); quotes never reach the touch
# we last saw (our view is ~240 ms old).
def test_rounding_is_away_from_mid_and_matches_marketstate():
    st = make_state()
    for x in (2399.398627, 2399.39, 2400.611373, 2400.62, 2400.0000000001, 2399.9999999999):
        assert floor_to_tick(x, TICK) == st.round_price(x, "down")
        assert ceil_to_tick(x, TICK) == st.round_price(x, "up")
    assert floor_to_tick(2399.398627, TICK) == 2399.39 and ceil_to_tick(2400.611373, TICK) == 2400.62


def test_no_cross_guard_clamps_to_one_tick_inside_the_touch():
    # our mid is stale: the market fell, best ask is now 2399.30 -> bid must be <= 2399.29
    t = compute_quotes(params(), MID, 0.0, 2399.29, 2399.30, TICK)
    assert t.bid_px == pytest.approx(2399.29)
    # and the other way: best bid jumped to 2401.00 -> ask must be >= 2401.01
    t = compute_quotes(params(), MID, 0.0, 2401.00, 2401.01, TICK)
    assert t.ask_px == pytest.approx(2401.01)


# ------------------------------------------------------------------ 2. strategy callbacks
# GROUP 4: callbacks driven directly with a hand-built MarketState (no engine). Sizing and inventory limit
# (spec §4.5): at +Q only the ask, at -Q only the bid; in-flight orders count; the reducing side keeps full size.
def test_flat_places_both_sides_post_only_full_size():
    st = make_state()
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    assert [type(a) for a in acts] == [PlaceOrder, PlaceOrder]
    bid, ask = acts
    assert (bid.side, bid.price, bid.qty, bid.post_only, bid.tif) == ("buy", 2399.39, 0.01, True, "GTC")
    assert (ask.side, ask.price, ask.qty) == ("sell", 2400.62, 0.01)


def test_at_max_long_only_the_ask_is_quoted_and_ask_is_inside_fee():
    st = make_state(pos=0.05)
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    assert len(acts) == 1 and acts[0].side == "sell" and acts[0].price == 2400.35


def test_at_max_short_only_the_bid_is_quoted():
    st = make_state(pos=-0.05)
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    assert len(acts) == 1 and acts[0].side == "buy" and acts[0].price == 2399.66


def test_headroom_exactly_one_order_and_partial_headroom():
    st = make_state(pos=0.04)
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    bid = [a for a in acts if a.side == "buy"][0]
    assert bid.qty == 0.01 and bid.price == 2399.18
    # q = 0.045: headroom 0.005 ETH is below the 20 USDT minimum notional (0.005*2399 = 12) -> no bid
    st2 = make_state(pos=0.045)
    s2 = start(make_strategy(), st2)
    assert [a.side for a in s2.on_event(qev(st2), st2)] == ["sell"]


def test_in_flight_orders_count_against_headroom():
    st = make_state(pos=0.04)
    st.open_orders["x1"] = OrderView("x1", "buy", 2390.0, 0.01, 0.01, "pending_new")
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    assert all(a.side == "sell" for a in acts)          # 0.04 + 0.01 in flight = Q: no second bid


def test_beyond_limit_reducing_side_still_quotes_full_size():
    st = make_state(pos=0.06)                            # |q| > Q (e.g. after a lagging fill report)
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    assert len(acts) == 1 and acts[0].side == "sell" and acts[0].qty == 0.01


def test_extreme_volatility_does_not_send_nonpositive_bid():
    st = make_state(pos=0.04)
    s = start(make_strategy(sigma=5000.0), st)           # skew so large the bid price would be <= 0
    assert compute_quotes(s.p, MID, 0.04, 2400.00, 2400.01, TICK).bid_px < 0
    acts = s.on_event(qev(st), st)
    assert acts and all(a.price > 0 and a.side == "sell" for a in acts)


def _live(st, s, acts):
    """Pretend the engine accepted our placements: register them as open orders."""
    for a in acts:
        st.open_orders[a.client_id] = OrderView(a.client_id, a.side, a.price, a.qty, a.qty, "open")
        s.on_order_update(OrderUpdate(st.now_ns, a.client_id, "accepted", "", a.qty), st)


# Requote rule (spec §4.6): move a live order only if the target is >= 5 ticks away AND >= 1 s since the
# last action, unless urgent (>= 15 ticks too close to mid). Limits churn under ~400 ms latency.
def test_requote_needs_five_ticks_and_one_second_unless_urgent():
    st = make_state()
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    _live(st, s, acts)
    bid_id = [a for a in acts if a.side == "buy"][0].client_id
    # mid up 3 ticks: target moves 3 ticks only -> keep (queue priority is kept)
    st.bid_px, st.ask_px, st.now_ns = 2400.03, 2400.04, 12 * NS
    assert s.on_event(qev(st), st) == []
    # mid up 6 ticks and > 1 s since the last action -> replace both with full qty
    st.bid_px, st.ask_px, st.now_ns = 2400.06, 2400.07, 12 * NS
    acts = s.on_event(qev(st), st)
    assert {type(a) for a in acts} == {ReplaceOrder} and all(a.total_qty == 0.01 for a in acts)  # nothing filled yet: total = r*
    # too soon after the last action and not urgent -> nothing (unanswered replace = in flight anyway)
    cids = [a.client_id for a in acts]
    for cid in cids:
        s.on_order_update(OrderUpdate(st.now_ns, cid, "replaced", "", 0.01), st)
        st.open_orders[cid].price = [a.price for a in acts if a.client_id == cid][0]
    st.bid_px, st.ask_px, st.now_ns = 2400.12, 2400.13, 12 * NS + 100_000_000
    assert s.on_event(qev(st), st) == []
    # price drop of >= 15 ticks makes the BID too close to mid -> urgent replace despite the interval
    st.bid_px, st.ask_px, st.now_ns = 2399.90, 2399.91, 12 * NS + 200_000_000
    acts = s.on_event(qev(st), st)
    assert any(isinstance(a, ReplaceOrder) and a.client_id == bid_id for a in acts)


def test_fill_triggers_immediate_requote_ignoring_interval():
    st = make_state()
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    _live(st, s, acts)
    ask_id = [a for a in acts if a.side == "sell"][0].client_id
    bid = [a for a in acts if a.side == "buy"][0]
    # our bid fills completely: position +0.01 reported, view removed (as the engine does)
    st.position = 0.01
    del st.open_orders[bid.client_id]
    st.now_ns += 100_000_000                              # only 100 ms after placing
    out = s.on_fill(Fill(st.now_ns, 1, bid.client_id, "buy", bid.price, 0.01, "maker", 0.0, 0.0), st)
    kinds = {(type(a).__name__, getattr(a, "side", None)) for a in out}
    assert ("PlaceOrder", "buy") in kinds                 # new bid (lower, because q > 0)
    new_bid = [a for a in out if isinstance(a, PlaceOrder)][0]
    assert new_bid.price < bid.price
    assert any(isinstance(a, ReplaceOrder) and a.client_id == ask_id for a in out)   # ask moved down


def test_desired_none_cancels_once():
    st = make_state(pos=0.0)
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    _live(st, s, acts)
    bid_id = [a for a in acts if a.side == "buy"][0].client_id
    st.position, st.now_ns = 0.05, st.now_ns + 2 * NS                     # now at the long limit
    acts = s.on_event(qev(st), st)
    assert [a for a in acts if isinstance(a, CancelOrder)] == [CancelOrder(bid_id)]
    st.open_orders[bid_id].status = "pending_cancel"
    assert not [a for a in s.on_event(qev(st), st) if isinstance(a, CancelOrder)]   # not again


def test_reject_puts_side_in_cooldown():
    st = make_state()
    s = start(make_strategy(), st)
    acts = s.on_event(qev(st), st)
    bid = [a for a in acts if a.side == "buy"][0]
    st.open_orders.pop(bid.client_id, None)                # the engine drops rejected orders
    s.on_order_update(OrderUpdate(st.now_ns, bid.client_id, "rejected", "post_only_would_cross"), st)
    st.now_ns += 500_000_000
    assert all(a.side != "buy" for a in s.on_event(qev(st), st) if isinstance(a, PlaceOrder))
    st.now_ns += 600_000_000                               # past the 1000 ms cooldown
    assert any(a.side == "buy" for a in s.on_event(qev(st), st) if isinstance(a, PlaceOrder))


# ------------------------------------------------------------------ risk limits (spec §7)
# GROUP 5 (spec §7): spread pause, stale quotes, gap cooldown, drawdown kill + taker flatten, inventory
# breach, reject storm, volatility spike. The strategy must stop quoting when it cannot trust its inputs.
def test_wide_spread_pauses_and_cancels_then_resumes_after_five_seconds():
    st = make_state()
    s = start(make_strategy(), st)
    _live(st, s, s.on_event(qev(st), st))
    st.bid_px, st.ask_px, st.now_ns = 2400.00, 2400.30, st.now_ns + NS    # 30-tick spread > 20
    assert s.on_event(qev(st), st) == [CancelAll()]
    assert s.on_event(qev(st), st) is None                                 # CancelAll only once
    st.bid_px, st.ask_px = 2400.00, 2400.01
    for _ in range(3):                                                     # ok, but < 5 s continuously
        st.now_ns += NS
        assert not s.on_event(qev(st), st)
    st.now_ns += 3 * NS
    assert s.stats["pauses"]["spread"] == 1
    st.open_orders.clear()
    assert [a for a in (s.on_event(qev(st), st) or []) if isinstance(a, PlaceOrder)]   # quoting again


def test_stale_quotes_pause_on_timer_and_gap_cooldown_after_fresh_quote():
    st = make_state()
    s = start(make_strategy(), st)
    _live(st, s, s.on_event(qev(st, t_ns=st.now_ns), st))
    st.now_ns += 4 * NS                                                    # > 3000 ms without a quote
    assert s.on_timer(st.now_ns, st) == [CancelAll()]
    st.open_orders.clear()
    st.now_ns += NS
    assert not s.on_event(qev(st, t_ns=st.now_ns), st)                     # fresh quote, but cooldown
    st.now_ns += 6 * NS
    assert [a for a in s.on_event(qev(st, t_ns=st.now_ns), st) if isinstance(a, PlaceOrder)]


def test_gap_clears_orders_and_blocks_quoting_for_cooldown():
    st = make_state()
    s = start(make_strategy(), st)
    s.on_event(qev(st), st)
    s.on_gap(GapEvent(st.now_ns, "resync", "depth"), st)
    st.open_orders.clear()
    assert s._inflight == set()
    st.now_ns += NS
    assert not s.on_event(qev(st), st)
    st.now_ns += 5 * NS + 1
    assert [a for a in s.on_event(qev(st), st) if isinstance(a, PlaceOrder)]


def long_state(**over):
    """Start flat with 10,000 cash, then 'buy' 0.05 ETH at 2400.005 so equity is unchanged."""
    st = make_state()
    s = start(make_strategy(**over), st)
    st.position, st.cash = 0.05, st.cash - 0.05 * MID
    return st, s


def test_drawdown_kills_cancels_and_flattens_with_taker_ioc():
    st, s = long_state(max_drawdown_usdt=5.0)
    st.bid_px, st.ask_px = 2290.00, 2290.01                # -110 * 0.05 = -5.5 -> equity drop > 5
    st.now_ns += NS
    acts = s.on_timer(st.now_ns, st)
    assert acts[0] == CancelAll()
    fl = acts[1]
    assert (fl.side, fl.qty, fl.tif, fl.post_only) == ("sell", 0.05, "IOC", False)
    assert fl.price == pytest.approx(2290.00 - 5 * TICK)
    assert s.stats["kills"]["drawdown"] == 1
    assert s.on_event(qev(st), st) is None                 # killed: no more quoting


def _exits(acts):
    return [a for a in (acts or []) if isinstance(a, PlaceOrder)]


def test_flatten_escalates_from_limit_ioc_to_market_and_never_stops_until_flat():
    st, s = long_state()
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    first = _exits(s.on_timer(st.now_ns, st))              # kill: CancelAll + one limit IOC
    assert len(first) == 1 and first[0].price == pytest.approx(2290.00 - 5 * TICK) and first[0].tif == "IOC"
    last = first[0]
    for _ in range(12):                                    # every exit "fails" (no_liquidity): there is no cap
        s.on_order_update(OrderUpdate(st.now_ns, last.client_id, "canceled", "no_liquidity", 0.05), st)
        st.now_ns += NS
        nxt = _exits(s.on_timer(st.now_ns, st))
        assert len(nxt) == 1 and nxt[0].price is None and nxt[0].tif == "IOC" and nxt[0].qty == 0.05   # market
        last = nxt[0]
    assert s.stats["flatten_orders"] == 13
    st.position = 0.0                                      # finally flat: nothing more is sent
    s.on_order_update(OrderUpdate(st.now_ns, last.client_id, "canceled", "ioc_remainder", 0.0), st)
    st.now_ns += NS
    assert _exits(s.on_timer(st.now_ns, st)) == [] and s.summary()["flat_after_kill_ms"] is not None


def test_flatten_failed_alert_after_flatten_alert_ms_but_exit_continues():
    st, s = long_state(flatten_alert_ms=5000)
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    last = _exits(s.on_timer(st.now_ns, st))[0]
    for i in range(8):                                     # 8 s of failing exits
        s.on_order_update(OrderUpdate(st.now_ns, last.client_id, "canceled", "no_liquidity", 0.05), st)
        st.now_ns += NS
        last = _exits(s.on_timer(st.now_ns, st))[0]        # still sending market orders
    summ = s.summary()
    assert summ["flatten_failed"]["q"] == 0.05 and summ["flatten_failed_ticks"] >= 3
    assert summ["flatten_failed"]["after_ms"] >= 5000


def test_flatten_waits_through_a_gap_and_resumes():
    st, s = long_state()
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    fl = _exits(s.on_timer(st.now_ns, st))[0]
    s.on_gap(GapEvent(st.now_ns, "resync", "depth"), st)   # the engine cancelled the exit; prices are gone
    st.bid_px = st.ask_px = None
    s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, "canceled", "connection_lost", 0.05), st)
    st.now_ns += NS
    assert _exits(s.on_timer(st.now_ns, st)) == []         # waits: no prices
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    assert len(_exits(s.on_timer(st.now_ns, st))) == 1     # resumes at the first tick after resync


def test_flatten_residual_below_min_notional_stops_the_exit():
    st, s = long_state()
    st.position = 0.005                                    # 0.005 ETH is under the 20 USDT minimum
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    fl = _exits(s._kill("drawdown", True, st))[0]
    s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, "rejected", "notional_below_min"), st)
    st.now_ns += NS
    assert _exits(s.on_timer(st.now_ns, st)) == []         # stops, does not retry forever
    assert s.summary()["flatten_residual"] == pytest.approx(0.005)


def test_inventory_breach_kills_and_reject_storm_kills_without_flatten():
    st = make_state(pos=0.08)                              # > 1.5 * Q = 0.075
    s = start(make_strategy(), st)
    out = s.on_timer(st.now_ns + NS, st)
    assert s.stats["kills"]["inventory_breach"] == 1 and any(isinstance(a, PlaceOrder) for a in out)
    st2 = make_state()
    s2 = start(make_strategy(max_rejects_per_min=3), st2)
    res = None
    for i in range(4):
        res = s2.on_order_update(OrderUpdate(st2.now_ns + i, f"z{i}", "rejected", "whatever"), st2)
    assert res == [CancelAll()] and s2.stats["kills"]["reject_storm"] == 1
    # awaiting_resync rejects do not count
    s3 = start(make_strategy(max_rejects_per_min=1), make_state())
    for i in range(5):
        assert s3.on_order_update(OrderUpdate(1, f"z{i}", "rejected", "awaiting_resync"), make_state()) is None


def test_vol_spike_pauses_then_resumes():
    st = make_state()
    s = start(make_strategy(vol_min_samples=5, sigma=0.26), st)
    t = st.now_ns
    px = 2400.0
    for i in range(10):                                    # calm: 0.01 per second -> sigma_live ~ 0.01
        t += NS
        st.now_ns, st.bid_px, st.ask_px = t, px, px + 0.01
        s.on_event(qev(st, t), st)
        s.on_timer(t, st)
        px += 0.01
    assert not s._vol_gate.active
    for i in range(10):                                    # violent: +/-3 USDT per second
        t += NS
        px += 3.0 * (-1) ** i
        st.now_ns, st.bid_px, st.ask_px = t, px, px + 0.01
        s.on_event(qev(st, t), st)
        s.on_timer(t, st)
    assert s._vol_gate.active and s.stats["pauses"]["vol"] == 1


# ------------------------------------------------------------------ 3. loader, config, engine
# GROUP 6: the engine's loader needs exactly one Strategy subclass; unknown params are errors; the smoke
# YAML agrees with itself; a synthetic zero-latency run proves it plugs into the real engine.
def test_engine_loader_finds_exactly_one_strategy():
    assert resolve_strategy("glft") is GlftMarketMaker
    assert GlftMarketMaker.event_kinds == ("quote", "funding")


def test_unknown_param_is_rejected():
    with pytest.raises(ValueError):
        GlftMarketMaker({"sigmaa": 1.0})
    with pytest.raises(ValueError):                 # a misspelled cal_ key is caught by CalParams
        CalParams.from_dict({"cal_min_hitz": 1})


def test_smoke_config_is_consistent_with_exchange_section():
    cfg = load_config(PROJECT_ROOT / "config" / "backtest" / "glft_smoke.yaml")
    p = GlftParams.from_dict(cfg["strategy"]["params"])     # cal_ keys in the same YAML section are skipped
    assert CalParams.from_dict(cfg["strategy"]["params"]).cal_min_hits == 20
    assert p.maker_fee == cfg["exchange"]["maker_fee"]
    assert p.min_notional_usdt == cfg["exchange"]["min_notional"]
    assert cfg["exchange"]["max_position"] == p.max_inventory == cfg["metrics"]["inventory_limit"]
    assert cfg["strategy"]["name"] == "glft"
    assert {"trades", "quotes", "funding"} <= set(cfg["data"]["streams"])


# Build a real engine with ZERO latency (allowed only with the test-only flag) so event order is easy to read.
def _engine(report_ms=0, timer_ms=0, order_ms=0, **params):
    cfg = deep_merge(DEFAULTS, {
        "latency": {"feed_latency_ms": 0, "order_latency_ms": order_ms, "report_latency_ms": report_ms,
                    "timer_interval_ms": timer_ms, "allow_zero_latency": True, "order_jitter_ms": 0,
                    "feed_jitter_ms": 0, "feed_delay_quantiles_ms": None, "no_event_ts_extra_ms": 0},
        "exchange": {"max_position": 0.05},
        "strategy": {"name": "glft", "params": {"gamma": 1.0, "print_stats_on_stop": False, **params}}})
    return build_engine(cfg, seed=1)


T0 = 1_700_000_000_000_000_000


def _q(ms, bid, ask):
    return QuoteEvent(T0 + ms * 1_000_000, None, None, 0, bid, 5.0, ask, 5.0)


def _t(ms, px, qty, side):
    return TradeEvent(T0 + ms * 1_000_000, None, None, 0, 0, 0, px, qty, side)


def test_engine_run_quotes_fills_and_skews():
    eng = _engine()
    for ms in range(0, 4):
        eng.on_market_event(_q(ms, 2400.00, 2400.01))
    eng.flush()
    rest = {o.side: o.price for o in eng.ex.resting.values()}
    assert rest == {"buy": 2399.39, "sell": 2400.62}                       # spec §10, q = 0
    # a sell print through our bid fills it (trade-through, capped by print size)
    eng.on_market_event(_t(10, 2399.30, 0.01, "sell"))
    for ms in range(11, 16):
        eng.on_market_event(_q(ms, 2400.00, 2400.01))
    eng.flush()
    assert eng.state.position == pytest.approx(0.01)
    prices = {o.side: o.price for o in eng.ex.resting.values()}
    assert prices["sell"] < 2400.62 and prices["buy"] < 2399.39            # long: both quotes lower
    assert abs(eng.account.position) <= 0.05 + 1e-12
    assert eng.ex.reject_reasons.get("risk_max_position", 0) == 0




# ------------------------------------------------------------------ 4. spec v1.1: flatten, unknown_order, rate guard
# GROUP 7: flatten stays in flight until a FINAL state; explained unknown_order rejects never feed the
# reject storm; the rate guard throttles places/replaces but never cancels; shrink-in-place; partial fills.
def _kill_with_flatten():
    """Long 0.05, then a price drop kills on drawdown; returns (state, strategy, flatten order)."""
    st, s = long_state(max_drawdown_usdt=5.0)
    st.bid_px, st.ask_px = 2290.00, 2290.01
    st.now_ns += NS
    acts = s.on_timer(st.now_ns, st)
    return st, s, [a for a in acts if isinstance(a, PlaceOrder)][0]


def _n_flatten(s, st, k):
    """Run the timer k seconds later; count the flatten orders it sends."""
    st.now_ns += k * NS
    return sum(isinstance(a, PlaceOrder) for a in (s.on_timer(st.now_ns, st) or []))


def test_flatten_stays_in_flight_after_accepted_until_final_state():
    st, s, fl = _kill_with_flatten()
    s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, "accepted", "", fl.qty), st)
    assert _n_flatten(s, st, 1) == 0                  # accepted is NOT final: no second flatten
    # partly filled (0.02 of 0.05); position report arrives, the order is still not final
    st.position = 0.03
    s.on_fill(Fill(st.now_ns, 1, fl.client_id, "sell", fl.price, 0.02, "taker", 0.0, 0.03), st)
    assert _n_flatten(s, st, 1) == 0
    # the IOC remainder is cancelled: now final, so the retry flattens the REMAINING 0.03 only
    s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, "canceled", "ioc_remainder", 0.03), st)
    st.now_ns += NS
    retry = [a for a in s.on_timer(st.now_ns, st) if isinstance(a, PlaceOrder)]
    assert len(retry) == 1 and retry[0].qty == 0.03 and retry[0].tif == "IOC"


def test_flatten_final_states_release_the_guard():
    for final, why in (("canceled", "no_liquidity"), ("rejected", "awaiting_resync")):   # unfilled / refused
        st, s, fl = _kill_with_flatten()
        s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, "accepted", "", fl.qty), st)
        s.on_order_update(OrderUpdate(st.now_ns, fl.client_id, final, why), st)
        assert _n_flatten(s, st, 1) == 1
    st, s, fl = _kill_with_flatten()                  # a full fill ends it too (position now flat)
    st.position = 0.0
    s.on_fill(Fill(st.now_ns, 1, fl.client_id, "sell", fl.price, 0.05, "taker", 0.0, 0.0), st)
    assert _n_flatten(s, st, 1) == 0 and s._inflight == set()


def _unknown(s, st, cid, t_ns):
    st.now_ns = t_ns
    return s.on_order_update(OrderUpdate(t_ns, cid, "replace_rejected", "unknown_order"), st)


def test_unknown_order_after_final_report_is_explained_and_never_kills():
    st = make_state()
    s = start(make_strategy(max_rejects_per_min=3), st)
    t0 = st.now_ns
    for i in range(30):                               # 30 races: fill report first, reject 200 ms later
        t = st.now_ns = t0 + i * NS
        s.on_fill(Fill(t, i, f"x{i}", "buy", 2399.0, 0.01, "maker", 0.0, 0.0), st)
        assert _unknown(s, st, f"x{i}", t + 200_000_000) is None
    assert s.stats["race_explained"] == 30 and not s._killed
    assert s.stats["rejects"]["unknown_order"] == 30  # still counted and reported


def test_unknown_order_explained_by_a_later_final_report_within_grace():
    st = make_state()
    s = start(make_strategy(), st)
    t = st.now_ns
    _unknown(s, st, "x1", t)
    s.on_order_update(OrderUpdate(t + NS, "x1", "canceled", "user"), st)    # 1 s later, inside 2 s
    st.now_ns = t + 5 * NS
    s.on_timer(st.now_ns, st)
    assert s.stats["race_explained"] == 1 and s.stats["race_unexplained"] == 0


def test_unexplained_unknown_orders_count_toward_the_storm_after_the_grace_period():
    st = make_state()
    s = start(make_strategy(max_rejects_per_min=2), st)
    t = st.now_ns
    for i in range(3):
        _unknown(s, st, f"x{i}", t + i * 1000)       # no final report for any of them
    st.now_ns = t + NS                                # inside the grace period: nothing counted yet
    assert CancelAll() not in (s.on_timer(st.now_ns, st) or [])
    st.now_ns = t + 3 * NS                            # grace (2 s) is over: 3 unexplained > 2
    assert CancelAll() in s.on_timer(st.now_ns, st)
    assert s.stats["kills"]["reject_storm"] == 1 and s.stats["race_unexplained"] == 3


def _two_live(**over):
    st = make_state()
    s = start(make_strategy(**over), st)
    acts = s.on_event(qev(st), st)
    _live(st, s, acts)
    return st, s, acts


def test_rate_guard_throttles_replace_but_not_cancel():
    st, s, acts = _two_live(rate_orders_10s_max=2)    # the two places used the whole 10 s budget
    st.bid_px, st.ask_px, st.now_ns = 2400.30, 2400.31, 12 * NS           # target moved: wants 2 replaces
    assert s.on_event(qev(st), st) == [] and s.stats["rate_throttled"] == 2
    st.position = 0.05                                                    # at the limit: bid must go
    out = s.on_event(qev(st), st)
    assert [type(a) for a in out] == [CancelOrder]                        # cancel allowed at budget
    st.now_ns = 30 * NS                                                   # 10 s window has emptied
    st.position = 0.0
    st.open_orders.clear()
    assert [type(a) for a in s.on_event(qev(st), st)] == [PlaceOrder, PlaceOrder]


def test_rate_guard_ip_weight_budget_blocks_new_orders_only():
    st, s, acts = _two_live(rate_ip_weight_1m_max=1)
    st.position, st.now_ns = 0.05, 12 * NS
    out = s.on_event(qev(st), st)                     # cancel of the bid uses the whole weight budget
    assert [type(a) for a in out] == [CancelOrder]
    st.bid_px, st.ask_px, st.now_ns = 2400.40, 2400.41, 14 * NS
    assert s.on_event(qev(st), st) == [] and s.stats["rate_throttled"] >= 1   # replace of the ask blocked


def test_modify_cap_cancels_instead_of_replacing():
    st, s, acts = _two_live(max_modifies_per_order=1)
    ask = [a for a in acts if a.side == "sell"][0]
    st.bid_px, st.ask_px, st.now_ns = 2400.30, 2400.31, 12 * NS
    out = s.on_event(qev(st), st)                     # first modify is allowed
    rep = [a for a in out if a.client_id == ask.client_id][0]
    assert isinstance(rep, ReplaceOrder)
    s.on_order_update(OrderUpdate(st.now_ns, ask.client_id, "replaced", "", 0.01), st)
    st.open_orders[ask.client_id].price = rep.price
    st.bid_px, st.ask_px, st.now_ns = 2400.60, 2400.61, 14 * NS
    out = s.on_event(qev(st), st)                     # cap reached: cancel, a new order follows
    assert CancelOrder(ask.client_id) in out and s.stats["modify_cap_cancels"] >= 1


def test_flatten_ignores_the_rate_budget_but_is_counted():
    st, s = long_state(rate_orders_10s_max=1)
    s._record_sent(st.now_ns, 0.01)                   # the 10 s budget is already used up
    assert not s._budget_ok(st.now_ns)
    st.bid_px, st.ask_px = 2290.00, 2290.01
    acts = s.on_timer(st.now_ns + NS, st)             # drawdown kill: the flatten goes out anyway
    assert any(isinstance(a, PlaceOrder) and a.tif == "IOC" for a in acts)
    assert s.stats["rate_throttled"] == 0 and s._sent_10s.total(st.now_ns + NS) == 2   # but it is counted


def test_ufr_is_reported_but_never_throttles():
    st, s, acts = _two_live()                         # 0.02 ETH placed, nothing filled
    s.on_timer(st.now_ns + 61 * NS, st)
    summ = s.summary()
    assert summ["ufr_max"] == pytest.approx(1.0) and summ["ufr_minutes_above"] == 1
    assert summ["rate_throttled"] == 0


def test_rolling_sum_is_exactly_zero_once_the_window_empties():
    # Float residue from adding/subtracting 0.01-sized values must not survive an empty window
    # (a residue/residue ratio once reported UFR = 1.85).
    from mm.strategies.glft import _Rolling
    r = _Rolling(10)
    for i, v in enumerate([0.01, 0.007, 0.003, 0.011, 0.009]):
        r.add(i, v)
    assert r.total(100) == 0.0


def test_shrink_in_place_when_headroom_falls_below_the_live_size():
    st, s, acts = _two_live(sigma=0.001)              # tiny sigma: the skew barely moves the price
    bid = [a for a in acts if a.side == "buy"][0]
    st.position, st.now_ns = 0.0405, 12 * NS          # headroom 0.0095 -> desired qty 0.009 < live 0.01
    out = [a for a in s.on_event(qev(st), st) if getattr(a, "client_id", None) == bid.client_id]
    assert out == [ReplaceOrder(bid.client_id, price=bid.price, total_qty=0.009)]


def test_partial_fill_then_replace_sends_total_size_filled_plus_remaining():
    st, s, acts = _two_live()
    ask = [a for a in acts if a.side == "sell"][0]
    # the ask is partly filled (0.004 of 0.01): position and remaining as the engine would report them
    st.position, st.cash = -0.004, st.cash + 0.004 * ask.price            # sold 0.004 at our ask
    st.open_orders[ask.client_id].remaining = 0.006
    s.on_fill(Fill(st.now_ns, 1, ask.client_id, "sell", ask.price, 0.004, "maker", 0.0, 0.006), st)
    st.bid_px, st.ask_px, st.now_ns = 2399.80, 2399.81, 14 * NS      # price target moves > 5 ticks
    out = [a for a in s.on_event(qev(st), st) if getattr(a, "client_id", None) == ask.client_id]
    assert len(out) == 1 and isinstance(out[0], ReplaceOrder)
    assert out[0].total_qty == pytest.approx(0.004 + 0.01) and out[0].qty is None   # filled we know + r*

def test_engine_replace_after_unseen_partial_fill_keeps_exposure_within_total():
    # The fill report is 300 ms late. Between the fill and its report the strategy replaces the bid; with
    # total_qty = filled-we-know (0) + r* (0.01) the exchange subtracts the unseen 0.001 fill (validator F5).
    eng = _engine(report_ms=300)
    for ms in range(0, 4):
        eng.on_market_event(_q(ms, 2400.00, 2400.01))
    eng.on_market_event(_t(10, 2399.30, 0.001, "sell"))                   # bid fills 0.001, unreported
    eng.on_market_event(_q(50, 2399.50, 2399.51))                         # mid -50 ticks: urgent replace
    eng.on_market_event(_q(60, 2399.50, 2399.51))
    bid = [o for o in eng.ex.resting.values() if o.side == "buy"][0]
    assert bid.remaining == pytest.approx(0.009) and bid.qty == pytest.approx(0.01)
    for ms in (400, 401, 402):                                            # reports arrive, strategy catches up
        eng.on_market_event(_q(ms, 2399.50, 2399.51))
    eng.flush()
    assert eng.state.position == pytest.approx(0.001)
    assert eng.ex.position + sum(o.remaining for o in eng.ex.resting.values() if o.side == "buy") <= 0.05
    assert eng.ex.reject_reasons.get("risk_max_position", 0) == 0
    assert not eng.ex.replace_reject_reasons


def test_engine_kill_flattens_as_taker_and_nothing_rests():
    eng = _engine(timer_ms=1000, max_drawdown_usdt=0.05)
    for ms in range(0, 4):
        eng.on_market_event(_q(ms, 2400.00, 2400.01))
    eng.on_market_event(_t(10, 2399.30, 0.01, "sell"))                    # bid fills 0.01: long
    for ms in (400, 500):
        eng.on_market_event(_q(ms, 2390.00, 2390.01))                     # price falls 10 USDT
    for ms in (1100, 1200, 1300, 2300, 2400):                             # timer fires: drawdown kill
        eng.on_market_event(_q(ms, 2390.00, 2390.01))
    eng.flush()
    assert eng.strategy._killed == "drawdown"
    assert not eng.ex.resting                                             # the IOC never rests
    assert eng.ex.position == pytest.approx(0.0)                          # flat again
    assert eng.ex.stats["fills_taker"] == 1

# ------------------------------------------------------------------ 5. spec v1.2: fill races (N4) and the exit (N1)
def _race(s, st, cid, reason, t_ns, status="replace_rejected"):
    st.now_ns = t_ns
    return s.on_order_update(OrderUpdate(t_ns, cid, status, reason), st)


@pytest.mark.parametrize("reason", ["notional_below_min", "qty_below_filled"])
def test_replace_rejects_that_are_fill_races_are_explained_by_a_fill_and_get_no_cooldown(reason):
    st, s, acts = _two_live(max_rejects_per_min=1)
    bid = [a for a in acts if a.side == "buy"][0]
    t0 = st.now_ns
    for i in range(4):                                 # a partial fill, then the reject 200 ms later
        s.on_fill(Fill(t0 + i * NS, i, bid.client_id, "buy", bid.price, 0.001, "maker", 0.0, 0.009), st)
        assert _race(s, st, bid.client_id, reason, t0 + i * NS + 200_000_000) is None
    assert s.stats["race_explained"] == 4 and not s._killed and s._side_cooldown_until["buy"] == 0
    st.now_ns = t0 + 10 * NS                           # reject first, the fill report 1 s later (inside grace)
    _race(s, st, "y1", reason, st.now_ns)
    s.on_fill(Fill(st.now_ns + NS, 9, "y1", "buy", bid.price, 0.001, "maker", 0.0, 0.009), st)
    assert s.stats["race_explained"] == 5 and not s._pending_races


def test_unexplained_replace_race_counts_toward_the_storm_after_the_grace_period():
    st, s, _ = _two_live(max_rejects_per_min=1)
    t0 = st.now_ns
    for i in range(2):
        _race(s, st, f"z{i}", "qty_below_filled", t0 + i)         # no fill report for either
    st.now_ns = t0 + 3 * NS
    assert CancelAll() in s.on_timer(st.now_ns, st)               # 2 unexplained > 1 -> kill
    assert s.stats["race_unexplained"] == 2 and s.stats["kills"]["reject_storm"] == 1


def test_a_rejected_place_for_min_notional_is_not_a_race_and_counts_at_once():
    st = make_state()
    s = start(make_strategy(max_rejects_per_min=0), st)
    res = s.on_order_update(OrderUpdate(st.now_ns, "z1", "rejected", "notional_below_min"), st)
    assert res == [CancelAll()] and s.stats["race_explained"] == 0


def test_engine_cancel_for_total_below_filled_leaves_the_side_empty_and_it_is_replaced():
    st, s, acts = _two_live()
    bid = [a for a in acts if a.side == "buy"][0]
    s._inflight.add(bid.client_id)                     # a replace of the bid is in flight ...
    st.open_orders.pop(bid.client_id)                  # ... and the engine answers: canceled, qty_below_filled
    assert s.on_order_update(OrderUpdate(st.now_ns, bid.client_id, "canceled", "qty_below_filled", 0.0), st) is None
    assert s._side_cooldown_until["buy"] == 0 and bid.client_id not in s._quote_ids
    st.now_ns += 2 * NS
    again = [a for a in s.on_event(qev(st), st) if isinstance(a, PlaceOrder) and a.side == "buy"]
    assert len(again) == 1 and again[0].client_id != bid.client_id


def test_replace_rejected_qty_below_filled_keeps_the_old_order_and_the_cycle_continues():
    st, s, acts = _two_live()
    bid = [a for a in acts if a.side == "buy"][0]
    s._inflight.add(bid.client_id)
    _race(s, st, bid.client_id, "qty_below_filled", st.now_ns)     # old order stays live (engine rule)
    assert bid.client_id in st.open_orders and bid.client_id not in s._inflight
    st.now_ns += 2 * NS
    assert s.on_event(qev(st), st) is not None         # no exception, quote cycle runs on


def test_engine_exit_escalates_from_a_stale_limit_ioc_to_a_market_order():
    # Order latency 100 ms: the touch we saw is stale on arrival (the bid fell 10 ticks), so the limit IOC
    # finds nothing (engine: canceled, no_liquidity). The next timer tick sends a MARKET order that fills.
    eng = _engine(timer_ms=1000, order_ms=100, max_drawdown_usdt=0.05)
    for ms in range(0, 4):
        eng.on_market_event(_q(ms, 2400.00, 2400.01))
    eng.on_market_event(_t(150, 2399.30, 0.01, "sell"))                   # bid fills 0.01: long
    eng.on_market_event(_q(400, 2390.00, 2390.01))                        # price falls: drawdown
    eng.on_market_event(_q(1050, 2389.80, 2389.81))                       # timer at 1000 kills; IOC arrives 1100
    for ms in (1200, 1300, 2100, 2200, 2300, 3100, 3200):
        eng.on_market_event(_q(ms, 2389.80, 2389.81))
    eng.flush()
    assert eng.strategy._killed == "drawdown"
    assert eng.strategy.stats["flatten_orders"] == 2                      # limit IOC (expired) + market
    assert not eng.ex.resting and eng.ex.position == pytest.approx(0.0)
    assert eng.ex.stats["fills_taker"] == 1

# ------------------------------------------------------------------ 6. calibration: trade-stream gap mask (spec §6.2)
def _arrays_from_trades(monkeypatch, trade_id, first_id, ts_s):
    """Run the real loader on tiny hand-made frames (no files): quotes are irrelevant here."""
    import polars as pl
    from mm.strategies import glft_calibrate as gc
    n = len(trade_id)
    trades = pl.DataFrame({"ts_ns": [int(t * 1e9) for t in ts_s], "event_ts_ns": [None] * n, "recv_ts_ns": [None] * n,
                           "trade_id": trade_id, "first_id": first_id, "last_id": [f + 1 for f in first_id],
                           "price": [100.0] * n, "qty": [1.0] * n, "side": ["buy"] * n})
    quotes = pl.DataFrame({"ts_ns": [0, int(1e9)], "event_ts_ns": [None, None], "recv_ts_ns": [None, None],
                           "update_id": [1, 2], "bid_px": [99.0, 99.0], "bid_qty": [1.0, 1.0],
                           "ask_px": [101.0, 101.0], "ask_qty": [1.0, 1.0]})
    monkeypatch.setattr(gc, "load_trades", lambda *a, **k: trades)
    monkeypatch.setattr(gc, "load_quotes", lambda *a, **k: quotes)
    cfg = {"data": {"symbol": "X", "source": "archive", "trade_dataset": "agg_trades"}}
    period = {"start": "1970-01-01", "end": "1970-01-01"}
    return gc, gc.load_period_arrays(cfg, period, 0.01)


def test_raw_id_jumps_with_contiguous_aggregate_ids_are_not_a_trade_gap(monkeypatch):
    # first_id jumps by 5 and 7 between prints (Binance skips raw ids), but trade_id is 10, 11, 12, 13
    gc, m = _arrays_from_trades(monkeypatch, [10, 11, 12, 13], [100, 105, 112, 113], [1, 2, 3, 4])
    s, e, n_id, n_silent = gc.trade_gap_ranges(m, int(10e9))
    assert n_id == 0 and n_silent == 0 and len(s) == 0


def test_aggregate_id_jump_and_long_silence_are_trade_gaps(monkeypatch):
    gc, m = _arrays_from_trades(monkeypatch, [10, 11, 20, 21, 22], [1, 2, 3, 4, 5], [1, 2, 3, 4, 20])
    s, e, n_id, n_silent = gc.trade_gap_ranges(m, int(10e9))
    assert n_id == 1 and n_silent == 1                                   # 11 -> 20 jumps; 4 s -> 20 s is a 16 s silence
    assert list(s) == [int(2e9), int(4e9)] and list(e) == [int(3e9), int(20e9)]