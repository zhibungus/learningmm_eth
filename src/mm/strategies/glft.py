"""Fee-aware GLFT market maker for the Binance USD-M ETHUSDT perpetual.

Implements ``specs/glft-perp.md`` (v1.2).  

What the strategy does, in one picture (spec Key ideas; L1 §3, §8)::

     price
       ^        ask = ceil( m + h - q*omega )       h = c + c1 + (Delta/2)*omega
       |        m (mid)                              c = maker fee per ETH = f_m * m
       |        bid = floor( m - h - q*omega )       omega = sigma * c2 = skew per ETH held
       |  long (q > 0): BOTH quotes move DOWN -> the ask fills sooner, the bid buys less

Layout of this file
    1. GlftParams       every tunable number, with units (config -> frozen dataclass)
    2. closed-form maths pure functions (no engine objects), unit-tested against spec §10
    3. helpers          pause gate, quote bundle
    4. GlftMarketMaker  the ONE Strategy subclass the engine's loader finds (keep it the only one!)

Symbols (same as the lessons): m mid [USDT]; q inventory [ETH, + long]; s_i = +1 bought / -1 sold;
delta = distance of a quote from mid [USDT]; spread captured = s_i * (m_i - p_i) (L1 §4).
"""
# Lazy type hints: "float | None" (= a float or None) is kept as text at import time.
from __future__ import annotations

import math
# deque: list with cheap removal at the left end. Counter: dict that counts (c["x"] += 1 works for new keys).
from collections import Counter, deque
# @dataclass writes __init__/__repr__/__eq__ from the annotated fields; fields() lists them.
from dataclasses import dataclass, fields
# Any = "no type restriction" in a type hint.
from typing import Any

import numpy as np

from mm.backtest.strategy import (BUY, SELL, CancelAll, CancelOrder, Fill, MarketState, OrderUpdate,
                                  PlaceOrder, ReplaceOrder, Strategy)

# Unit conversions (not tunable): nanoseconds per millisecond / second.
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
# Float-noise guard (same 1e-9 nudge as MarketState.round_price): 99.99999999 ticks == 100.
_EPS = 1e-9
# Binance's rate-limit windows (spec §7.4) are exchange facts, not parameters: 10 s, 1 min, 10 min (UFR).
_WIN_10S_NS, _WIN_1M_NS, _WIN_UFR_NS = 10 * NS_PER_S, 60 * NS_PER_S, 600 * NS_PER_S


# ====================================================================================== 1. parameters
# Spec §11 parameter table. frozen=True: values cannot change after creation. Each field is
# "name: type = default" (the spec default); units are in the comment.
@dataclass(frozen=True)
class GlftParams:
    # ---- model inputs (spec §6: calibrated in-sample, then frozen)
    sigma: float = 0.26                  # sigma, USDT per sqrt(second)  (PLACEHOLDER, spec §6.1)
    A: float = 4.0                       # A, fills per second at distance 0 (PLACEHOLDER, §6.2)
    k: float = 10.0                      # k, 1/USDT, decay of fill rate with distance (PLACEHOLDER)
    gamma: float = 1.0                   # gamma, 1/USDT, risk aversion (PLACEHOLDER, §6.4)
    xi_equals_gamma: bool = True         # xi = gamma (True) or xi = 0 limit forms (False), §4.1
    skew_at_Q_ticks: float = 25.0        # target skew at max inventory, ticks; calibration only, §6.4
    fee_mode: str = "exact"              # "exact" or "shift", spec §4.2
    skew_off: bool = False               # sensitivity variant only (spec §9 H3): force omega=0 in q*omega
    # ---- exchange rules / sizing (spec §1, §4.5)
    maker_fee: float = 0.0002            # f_m, fraction of notional; must equal exchange.maker_fee
    order_qty: float = 0.01              # Delta, ETH
    max_inventory: float = 0.05          # Q, ETH
    min_notional_usdt: float = 20.0      # USDT; must equal exchange.min_notional
    min_qty: float = 0.001               # ETH (spec §4.5 quotes it but its table omits the key)
    min_half_spread_ticks: float = 10.0  # delta_min / theta, ticks
    # ---- requote rule (spec §4.6)
    requote_ticks: float = 5.0
    urgent_requote_ticks: float = 15.0
    min_requote_interval_ms: float = 1000.0
    reject_cooldown_ms: float = 1000.0
    # ---- risk limits (spec §7)
    stale_quote_ms: float = 3000.0
    gap_cooldown_ms: float = 5000.0
    spread_pause_ticks: float = 20.0
    vol_pause_mult: float = 3.0          # pause when sigma_live > mult * sigma
    vol_resume_mult: float = 2.0         # resume when sigma_live < mult * sigma ...
    pause_resume_ms: float = 5000.0      # ... continuously for this long
    vol_sample_ms: float = 1000.0
    vol_ewma_halflife_s: float = 60.0
    vol_min_samples: int = 60            # spec §7.3 "valid only after 60 samples"
    max_drawdown_usdt: float = 5.0
    max_rejects_per_min: int = 20
    reject_window_s: float = 60.0        # the "per min" window of the reject-storm rule
    unknown_order_grace_ms: float = 2000.0   # unknown_order reject explained by a final report within this
    inventory_breach_mult: float = 1.5   # kill when |q| > mult * Q
    flatten_on_kill: bool = True
    flatten_slip_ticks: float = 5.0
    flatten_ioc_attempts: int = 1        # limit IOCs before the exit switches to market orders (spec §7.1)
    flatten_alert_ms: float = 10000.0    # not flat this long after the kill -> flatten_failed
    # ---- exchange order-rate guard (spec §7.4): 50% of Binance's limits, never tuning knobs
    rate_orders_10s_max: int = 150       # sent places + replaces per rolling 10 s (Binance 300)
    rate_orders_1m_max: int = 600        # per rolling 60 s (Binance 1200)
    rate_ip_weight_1m_max: int = 1200    # cancels, weight 1 each, per rolling 60 s (Binance 2400)
    max_modifies_per_order: int = 9000   # Binance allows < 10,000 modifies of one order
    ufr_warn: float = 0.98               # report-only: rolling 10 min unfilled ratio
    print_stats_on_stop: bool = True     # one summary line at the end of a run (not in the spec)
    # (The cal_* calibration-only keys live in CalParams in glft_calibrate.py; same YAML section.)

    def __post_init__(self) -> None:
        # Runs right after __init__: reject nonsense early.
        if self.fee_mode not in ("exact", "shift"):
            raise ValueError(f"fee_mode must be 'exact' or 'shift', got {self.fee_mode!r}")
        for name in ("sigma", "A", "k", "gamma", "order_qty", "max_inventory"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0, got {getattr(self, name)}")
        if self.vol_resume_mult > self.vol_pause_mult:
            raise ValueError("vol_resume_mult must be <= vol_pause_mult")

    # @classmethod: receives the class (cls) instead of an object; an alternative constructor.
    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "GlftParams":
        """Build from the YAML ``strategy.params`` dict; a misspelled key is an error, not ignored."""
        # Keys starting with "cal_" belong to CalParams (same YAML section), so they are skipped here.
        # Simplest option: no second YAML section, and CalParams still rejects misspelled cal_ keys.
        d = {k: v for k, v in (d or {}).items() if not k.startswith("cal_")}   # dict comprehension
        unknown = set(d) - {f.name for f in fields(cls)}                         # set difference
        if unknown:
            raise ValueError(f"unknown glft params: {sorted(unknown)}")
        return cls(**d)                              # **d unpacks the dict into keyword arguments


# ============================================================================== 2. closed-form maths
# Everything here is a plain function of numbers, so tests can check it against the hand-computed
# worked example (spec §10) without building an engine.
def fee_per_eth(maker_fee: float, mid: float) -> float:
    """c = f_m * m, the maker fee per ETH in USDT (spec §4.4 step 1; L1 §3 "fee per side")."""
    return maker_fee * mid


def xi_of(p: GlftParams) -> float:
    """xi, the non-execution risk aversion: xi = gamma (Model A) or 0 (spec §4.1; not yet covered in lessons)."""
    return p.gamma if p.xi_equals_gamma else 0.0


def a_use(p: GlftParams, c: float) -> float:
    """Fill-intensity scale to put in the closed form (spec §4.2, Appendix A).

    exact: A_eff = A * exp(-k*c)  (the fee is a change of variable delta~ = delta - c)
    shift: A itself (the survey's simple "+fee" shift).  Fee folding is not yet covered in lessons (planned L5)."""
    # Conditional expression: "X if condition else Y" is a one-line if/else that gives a value.
    return p.A * math.exp(-p.k * c) if p.fee_mode == "exact" else p.A


def c1_static(p: GlftParams) -> float:
    """c1 = (1/(xi*Delta)) * ln(1 + xi*Delta/k)  [USDT]; the xi -> 0 limit is 1/k (spec §4.1)."""
    xi = xi_of(p)
    if xi == 0.0:
        return 1.0 / p.k
    # log1p(x) = ln(1+x), accurate for small x (xi*Delta/k is about 0.001 here).
    return math.log1p(xi * p.order_qty / p.k) / (xi * p.order_qty)


def c2_skew(p: GlftParams, a: float) -> float:
    """c2 = sqrt( gamma/(2*a*Delta*k) * (1+xi*Delta/k)^(k/(xi*Delta)+1) )  [sqrt(s)/ETH] (spec §4.1).

    For xi = 0 the power term becomes e.  ``a`` is A_use from :func:`a_use`."""
    g = p.gamma
    xi = xi_of(p)
    if xi == 0.0:
        power = math.e
    else:
        x = xi * p.order_qty / p.k
        # (1+x)^(1/x + 1) written as exp((1/x+1)*ln(1+x)) so a tiny x cannot overflow.
        power = math.exp((1.0 / x + 1.0) * math.log1p(x))
    return math.sqrt(g / (2.0 * a * p.order_qty * p.k) * power)


@dataclass(frozen=True)
class ClosedForm:
    """The numbers of spec §4.4 steps 1-4 (everything that does not depend on q)."""
    c: float        # fee per ETH [USDT]
    a_use: float    # A_use [1/s]
    c1: float       # static half-spread term [USDT]
    c2: float       # skew coefficient [sqrt(s)/ETH]
    omega: float    # skew per ETH of inventory [USDT/ETH] = sigma * c2
    h: float        # half-spread around the reservation price [USDT] = c + c1 + (Delta/2)*omega


def closed_form(p: GlftParams, mid: float) -> ClosedForm:
    """Spec §4.4 steps 1-4 at the given mid."""
    c = fee_per_eth(p.maker_fee, mid)                      # step 1
    a = a_use(p, c)                                         # step 2
    c1 = c1_static(p)                                       # step 3
    c2 = c2_skew(p, a)
    omega = p.sigma * c2
    h = c + c1 + 0.5 * p.order_qty * omega                  # step 4
    return ClosedForm(c, a, c1, c2, omega, h)


# ---- tick rounding (same arithmetic as MarketState.round_price in backtest/strategy.py)
def floor_to_tick(x: float, tick: float) -> float:
    """Round DOWN to the tick grid (used for bids, so rounding never tightens the quote)."""
    return round(float(np.floor(x / tick + _EPS)) * tick, 10)


def ceil_to_tick(x: float, tick: float) -> float:
    """Round UP to the tick grid (used for asks)."""
    return round(float(np.ceil(x / tick - _EPS)) * tick, 10)


@dataclass(frozen=True)
class QuoteTargets:
    """Output of :func:`compute_quotes` (spec §4.4)."""
    cf: ClosedForm
    delta_b: float   # distance of the bid below mid [USDT]
    delta_a: float   # distance of the ask above mid [USDT]
    bid_px: float    # final bid price (tick-rounded, no-cross clamped)
    ask_px: float


def compute_quotes(p: GlftParams, mid: float, q: float, best_bid: float | None,
                   best_ask: float | None, tick: float) -> QuoteTargets:
    """Spec §4.4 steps 1-7: the two target prices for inventory q [ETH] at mid [USDT]."""
    cf = closed_form(p, mid)
    skew = 0.0 if p.skew_off else q * cf.omega              # q*omega: long (q>0) pushes both quotes down
    d_min = p.min_half_spread_ticks * tick                  # delta_min floor (step 5)
    # Step 5. Long: delta_b grows (bid further away), delta_a shrinks (ask nearer mid).  The fee
    # distance c is NOT a floor for the inventory-reducing side; only delta_min is (spec §4.4).
    delta_b = max(cf.h + skew, d_min)
    delta_a = max(cf.h - skew, d_min)
    bid = floor_to_tick(mid - delta_b, tick)                # step 6: bid rounds DOWN
    ask = ceil_to_tick(mid + delta_a, tick)                 # ask rounds UP
    # Step 7: our view of the book is ~240 ms old, so never quote at/through the touch we last saw.
    if best_ask is not None:
        bid = min(bid, round(best_ask - tick, 10))
    if best_bid is not None:
        ask = max(ask, round(best_bid + tick, 10))
    return QuoteTargets(cf, delta_b, delta_a, bid, ask)


# =================================================================================== 3. small helpers
# A leading underscore (_PauseGate) marks "internal to this module".
class _PauseGate:
    """A pause with hysteresis: trips on a 'bad' condition, clears only after a 'clear' condition has
    held CONTINUOUSLY for ``resume_ns`` (spec §7.1 "Resume" column).  A plain class (not a Strategy), so
    the engine's loader is not confused."""

    def __init__(self, resume_ns: int) -> None:
        self.resume_ns = resume_ns
        self.active = False
        self.ok_since_ns: int | None = None

    def reset(self) -> None:
        self.active, self.ok_since_ns = False, None

    def update(self, now_ns: int, trip: bool, clear: bool) -> bool:
        """Feed one observation; returns True only on the moment the pause starts."""
        if not self.active:
            if trip:
                self.active, self.ok_since_ns = True, None
                return True
            return False
        if clear:
            if self.ok_since_ns is None:
                self.ok_since_ns = now_ns
            if now_ns - self.ok_since_ns >= self.resume_ns:
                self.active, self.ok_since_ns = False, None
        else:
            self.ok_since_ns = None
        return False


class _Rolling:
    """Sum of values added in the last ``window_ns`` (a sliding window): order counts, weights, quantities."""

    def __init__(self, window_ns: int) -> None:
        self.window_ns = window_ns
        self.items: deque[tuple[int, float]] = deque()    # (time, value), oldest on the left
        self.sum = 0.0

    def add(self, now_ns: int, value: float = 1.0) -> None:
        self.items.append((now_ns, value))
        self.sum += value

    def total(self, now_ns: int) -> float:
        while self.items and self.items[0][0] < now_ns - self.window_ns:    # drop what left the window
            self.sum -= self.items.popleft()[1]
        if not self.items:
            # Empty window: reset to exactly 0. Adding then subtracting sizes like 0.01 leaves float
            # residue (e.g. -1e-18), and a ratio of two residues is nonsense (it gave UFR = 1.85).
            self.sum = 0.0
        return self.sum


def _is_flatten(client_id: str) -> bool:
    """Flatten orders get ids "f1", "f2", ... (quote orders are "b..." / "a...")."""
    return client_id.startswith("f")


# ================================================================================ 4. the strategy
# "(Strategy)" = inheritance: we override only the callbacks we need; the rest do nothing.
class GlftMarketMaker(Strategy):
    """Fee-aware GLFT quoter with inventory skew, requote throttling and a kill switch (spec §4-§7).

    Engine call order for one run: __init__ -> on_start -> (on_event / on_timer / on_fill /
    on_order_update / on_gap in time order) -> on_stop.
    """

    name = "glft"
    # Spec §2: quotes drive everything; funding is delivered anyway so state.cash mirrors funding.
    # Trades are NOT subscribed: engine.on_market_event() feeds EVERY event to the simulated exchange
    # (fill model) whatever event_kinds says; event_kinds only controls what reaches on_event.
    event_kinds = ("quote", "funding")

    def __init__(self, params=None, rng=None) -> None:
        super().__init__(params, rng)
        self.p = GlftParams.from_dict(self.params)
        p = self.p
        # ---- time constants converted once to integer nanoseconds (project convention)
        self._stale_ns = int(p.stale_quote_ms * NS_PER_MS)
        self._gap_cd_ns = int(p.gap_cooldown_ms * NS_PER_MS)
        self._requote_gap_ns = int(p.min_requote_interval_ms * NS_PER_MS)
        self._reject_cd_ns = int(p.reject_cooldown_ms * NS_PER_MS)
        self._vol_dt_ns = int(p.vol_sample_ms * NS_PER_MS)
        # ---- risk state
        self._killed: str | None = None            # cause of the kill, or None
        self._kill_flatten = False
        self._kill_ns = 0                          # local time of the kill
        self._alert_ns = int(p.flatten_alert_ms * NS_PER_MS)
        self._flatten_residual: float | None = None   # |q| left when a below-min-notional exit was rejected
        self._equity0 = 0.0
        self._peak_equity = 0.0
        self._last_quote_ts_ns: int | None = None  # exchange time of the latest quote we saw
        self._stale_paused = False
        self._gap_wait = False                     # True from on_gap until the next fresh quote
        self._cooldown_until_ns = 0                # no quoting before this local time
        resume_ns = int(p.pause_resume_ms * NS_PER_MS)
        self._spread_gate = _PauseGate(resume_ns)
        self._vol_gate = _PauseGate(resume_ns)
        self._reset_vol()
        self._rejects = _Rolling(int(p.reject_window_s * NS_PER_S))   # rejects that count toward the storm
        self._grace_ns = int(p.unknown_order_grace_ms * NS_PER_MS)
        # Fill races (spec §7.1): a replace/cancel reject that just means "a fill we had not heard about yet".
        self._report_ns: dict[str, int] = {}         # order id -> time of its last fill / cancel report
        self._pending_races: dict[str, int] = {}     # race rejects still waiting for such a report
        # Rate guard (spec §7.4). Paper/live only, NOT modelled here: HTTP 429 back-off, 418 ban -> kill,
        # polling /fapi/v1/apiTradingStatus (isLocked -> kill), cross-checking the X-MBX-* count headers.
        self._sent_10s, self._sent_1m = _Rolling(_WIN_10S_NS), _Rolling(_WIN_1M_NS)   # places + replaces
        self._weight_1m = _Rolling(_WIN_1M_NS)       # cancels (IP weight 1 each)
        self._modifies: dict[str, int] = {}          # order id -> replaces sent so far
        self._filled: dict[str, float] = {}          # order id -> qty of it filled that we have been told of
        self._ufr_placed, self._ufr_filled = _Rolling(_WIN_UFR_NS), _Rolling(_WIN_UFR_NS)   # qty, for UFR
        self._last_ufr_ns: int | None = None
        # ---- order bookkeeping (spec §4.6)
        self._quote_ids: dict[str, str] = {}       # client_id of our quote orders -> side
        # In flight = action sent, exchange has not answered yet (we see it ~400 ms late).
        self._inflight: set[str] = set()           # a set holds each id once; "x in s" is fast
        self._side_cooldown_until: dict[str, int] = {BUY: 0, SELL: 0}
        self._last_action_ns: dict[str, int | None] = {BUY: None, SELL: None}
        self._cancel_all_sent = False
        # ---- report counters (spec §9 "required report fields")
        self.stats: dict[str, Any] = {
            "pauses": Counter(), "kills": Counter(), "rejects": Counter(), "fills": Counter(),
            "placed": Counter(), "replaced": Counter(), "cancelled": Counter(),
            "delta_ticks_sum": Counter(), "delta_ticks_n": Counter(),
            "ns_with_order": Counter(), "ns_observed": 0, "flatten_orders": 0,
            "h2_predicted_fills": 0.0,   # spec §9 H2: sum of A*exp(-k*delta)*dt over live quotes
            "rate_throttled": 0, "modify_cap_cancels": 0, "race_explained": 0,
            "race_unexplained": 0, "ufr_minutes_above": 0, "ufr_max": 0.0,
            "flat_after_kill_ms": None, "flatten_failed": None, "flatten_failed_ticks": 0,
        }
        self._last_timer_ns: int | None = None

    # ------------------------------------------------------------------------------ vol estimator
    def _reset_vol(self) -> None:
        """Spec §7.2 step 2 / §7.3: (re)start the live volatility estimator."""
        self._vol_prev_mid: float | None = None
        self._vol_last_ns: int | None = None
        self._vol_num = 0.0        # exponentially weighted sum of squared increments / dt
        self._vol_den = 0.0        # sum of the weights (bias correction, so early values are not low)
        self._vol_n = 0

    def _vol_sample(self, now_ns: int, mid: float | None) -> None:
        """Spec §7.3: every vol_sample_ms take x = mid change; sigma_live^2 <- EWMA of x^2/dt."""
        if mid is None:
            self._reset_vol()
            return
        if self._vol_last_ns is not None and now_ns - self._vol_last_ns < self._vol_dt_ns:
            return
        if self._vol_prev_mid is not None and self._vol_last_ns is not None:
            dt = (now_ns - self._vol_last_ns) / NS_PER_S
            x = mid - self._vol_prev_mid
            alpha = 1.0 - 2.0 ** (-dt / self.p.vol_ewma_halflife_s)    # half-life -> smoothing weight
            self._vol_num = (1 - alpha) * self._vol_num + alpha * x * x / dt
            self._vol_den = (1 - alpha) * self._vol_den + alpha
            self._vol_n += 1
        self._vol_prev_mid, self._vol_last_ns = mid, now_ns

    # @property: read sigma_live like an attribute (no parentheses); it is computed on demand.
    @property
    def sigma_live(self) -> float | None:
        """Live volatility estimate [USDT/sqrt(s)], or None before vol_min_samples samples."""
        if self._vol_n < self.p.vol_min_samples or self._vol_den <= 0:
            return None
        return math.sqrt(self._vol_num / self._vol_den)

    # -------------------------------------------------------------------------------- callbacks
    def on_start(self, state: MarketState):
        """Spec §5: remember starting equity (position is 0) and the running peak."""
        self._equity0 = state.cash
        self._peak_equity = state.cash
        return None

    def on_event(self, ev, state: MarketState):
        """Spec §5: on a quote update the spread gate, then run the quote cycle."""
        if ev.kind != "quote" or self._killed:
            return None               # funding: only state.cash changes (spec §4.3)
        now = state.now_ns
        self._last_quote_ts_ns = ev.ts_ns
        # A fresh quote ends a stale pause / gap wait, after which the cooldown clock starts (§7.1, §7.2).
        if self._stale_paused or self._gap_wait:
            self._stale_paused = self._gap_wait = False
            self._cooldown_until_ns = now + self._gap_cd_ns
        if state.bid_px is not None and state.ask_px is not None:
            wide = round((state.ask_px - state.bid_px) / state.tick_size)   # spread in ticks
            ok = wide <= self.p.spread_pause_ticks
            if self._spread_gate.update(now, trip=not ok, clear=ok):
                self.stats["pauses"]["spread"] += 1
        return self._quote_cycle(state, now, force=False)

    def on_timer(self, now_ns: int, state: MarketState):
        """Spec §5: staleness, drawdown, vol gate, then a quote cycle (catches throttled requotes)."""
        if self._killed:
            # Killed: keep exiting every tick until flat (if flattening), else do nothing.
            return self._flatten_orders(state) if self._kill_flatten else None
        # time accounting for "fraction of time quoting each side" (sampled once per timer tick)
        if self._last_timer_ns is not None:
            dt = now_ns - self._last_timer_ns
            self.stats["ns_observed"] += dt
            for side in (BUY, SELL):
                live = self._live_order(state, side)
                if live is not None:
                    self.stats["ns_with_order"][side] += dt
                    # H2: fills the fitted model expects for this quote over this tick, at its real
                    # distance from the mid (A per second, k per USDT; dt is in ns).
                    if state.mid is not None:
                        self.stats["h2_predicted_fills"] += (
                            self.p.A * math.exp(-self.p.k * abs(state.mid - live.price)) * dt / NS_PER_S)
        self._last_timer_ns = now_ns
        # stale quotes (spec §7.1): exchange time of the last quote vs our local clock
        if (self._last_quote_ts_ns is not None and not self._stale_paused
                and now_ns - self._last_quote_ts_ns > self._stale_ns):
            self._stale_paused = True
            self._reset_vol()
            self.stats["pauses"]["stale"] += 1
        kill = self._risk_checks(state, now_ns)
        if kill:
            return kill
        kill = self._expire_races(state)
        if kill:
            return kill
        self._ufr_tick(now_ns)
        # volatility spike gate (spec §7.1 / §7.3)
        if not self._stale_paused and not self._gap_wait:
            self._vol_sample(now_ns, state.mid)
        sl = self.sigma_live
        if sl is None:
            trip, clear = False, True
        else:
            trip = sl > self.p.vol_pause_mult * self.p.sigma
            clear = sl < self.p.vol_resume_mult * self.p.sigma
        if self._vol_gate.update(now_ns, trip, clear):
            self.stats["pauses"]["vol"] += 1
        return self._quote_cycle(state, now_ns, force=False)

    def on_fill(self, fill: Fill, state: MarketState):
        """Spec §5: q changed, so re-run the quote cycle immediately, ignoring the requote interval."""
        self.stats["fills"][fill.side] += 1
        self._ufr_filled.add(state.now_ns, fill.qty)
        self._filled[fill.client_id] = self._filled.get(fill.client_id, 0.0) + fill.qty
        self._order_reported(fill.client_id, state.now_ns)
        if fill.leaves_qty <= _EPS:                             # order completely done: a final state
            self._inflight.discard(fill.client_id)
            self._quote_ids.pop(fill.client_id, None)
            self._filled.pop(fill.client_id, None)
            self._modifies.pop(fill.client_id, None)
        kill = self._risk_checks(state, state.now_ns)
        if kill:
            return kill
        if self._killed:
            return None
        return self._quote_cycle(state, state.now_ns, force=True)

    def on_order_update(self, upd: OrderUpdate, state: MarketState):
        """Spec §4.6, §7.1: keep the in-flight flags and local book, then handle rejects."""
        cid, now = upd.client_id, state.now_ns
        # A flatten order stays in flight until a FINAL state (full fill, canceled, rejected): an "accepted"
        # IOC is not done yet, and a second flatten could flip the position.
        if not (_is_flatten(cid) and upd.status == "accepted"):
            self._inflight.discard(cid)
        side = self._quote_ids.get(cid)
        if upd.status in ("rejected", "canceled"):
            self._quote_ids.pop(cid, None)
            self._modifies.pop(cid, None)
            self._filled.pop(cid, None)
        if upd.status == "canceled":      # also the engine's answer to a replace with total <= filled
            self._order_reported(cid, now)
        if upd.status not in ("rejected", "replace_rejected", "cancel_rejected"):
            return None
        self.stats["rejects"][upd.reason] += 1
        if _is_flatten(cid) and upd.reason == "notional_below_min":
            self._flatten_residual = abs(state.position)       # leftover below min notional: stop exiting
        kind = self._classify_reject(upd)
        if kind == "ignore":
            return None
        if kind == "race":                # no cooldown: the next cycle re-sizes, re-places or cancels
            seen = self._report_ns.get(cid)
            if seen is not None and now - seen <= self._grace_ns:
                self.stats["race_explained"] += 1
            else:
                self._pending_races[cid] = now         # counts later if no report explains it in time
            return None
        if side is not None:
            self._side_cooldown_until[side] = now + self._reject_cd_ns
        return self._note_reject(state)

    @staticmethod                          # a method that does not use self: just a function kept in the class
    def _classify_reject(upd: OrderUpdate) -> str:
        """Spec §7.1: "ignore" (expected during a gap), "race" (a fill we had not heard about yet), or "count"."""
        if upd.reason == "awaiting_resync":
            return "ignore"
        if upd.status == "replace_rejected" and upd.reason in ("notional_below_min", "qty_below_filled"):
            return "race"
        if upd.status != "rejected" and upd.reason == "unknown_order":     # replace/cancel of a gone order
            return "race"
        return "count"

    def _order_reported(self, cid: str, now_ns: int) -> None:
        """A fill or cancel report arrived: remember when, and explain a race reject that was waiting for it."""
        self._report_ns[cid] = now_ns
        if self._pending_races.pop(cid, None) is not None:
            self.stats["race_explained"] += 1

    def _note_reject(self, state: MarketState):
        """Count one reject toward the storm (spec §7.1: trips at MORE than max_rejects_per_min)."""
        self._rejects.add(state.now_ns)
        if self._rejects.total(state.now_ns) > self.p.max_rejects_per_min and not self._killed:
            return self._kill("reject_storm", False, state)       # probably a bug: no flatten
        return None

    def _expire_races(self, state: MarketState):
        """Timer: race rejects with no report after the grace period are real storm rejects."""
        now = state.now_ns
        self._report_ns = {c: t for c, t in self._report_ns.items() if now - t <= self._grace_ns}   # dict comprehension
        kill = None
        for cid, t in list(self._pending_races.items()):        # list(...) copy: we delete while looping
            if now - t > self._grace_ns:
                del self._pending_races[cid]
                self.stats["race_unexplained"] += 1
                killed_now = self._note_reject(state)
                kill = killed_now or kill
        return kill
    def _ufr_tick(self, now_ns: int) -> None:
        """Report-only (spec §7.4): once a minute, UFR = 1 - filled qty / placed qty over 10 min; count minutes above ufr_warn."""
        if self._last_ufr_ns is not None and now_ns - self._last_ufr_ns < _WIN_1M_NS:
            return
        self._last_ufr_ns = now_ns
        placed = self._ufr_placed.total(now_ns)
        if placed > 0:
            ufr = 1.0 - self._ufr_filled.total(now_ns) / placed
            self.stats["ufr_max"] = max(self.stats["ufr_max"], ufr)
            if ufr > self.p.ufr_warn:
                self.stats["ufr_minutes_above"] += 1

    def on_gap(self, ev, state: MarketState):
        """Spec §7.2: the engine already cancelled our orders; forget them and wait for a fresh quote."""
        self._inflight.clear()
        self._quote_ids.clear()
        self._modifies.clear()
        self._filled.clear()
        self._reset_vol()
        self._spread_gate.reset()
        self._last_quote_ts_ns = None
        self._gap_wait = True
        self._cancel_all_sent = True        # nothing left to cancel
        self.stats["pauses"]["gap"] += 1
        return None

    def on_stop(self, state: MarketState) -> None:
        if self.p.print_stats_on_stop:
            # f-string: f"...{expr}..." inserts the value of expr into the text.
            print(f"[glft] {self.summary()}", flush=True)

    def summary(self) -> dict[str, Any]:
        """Report fields of spec §9 (counts by cause, mean posted delta, time quoting each side)."""
        s = self.stats
        obs = s["ns_observed"] or 1
        return {
            "pauses": dict(s["pauses"]), "kills": dict(s["kills"]), "rejects": dict(s["rejects"]),
            "fills": dict(s["fills"]), "placed": dict(s["placed"]), "replaced": dict(s["replaced"]),
            "mean_posted_delta_ticks": {side: s["delta_ticks_sum"][side] / s["delta_ticks_n"][side]
                                        for side in (BUY, SELL) if s["delta_ticks_n"][side]},
            "frac_time_quoting": {side: s["ns_with_order"][side] / obs for side in (BUY, SELL)},
            "killed": self._killed, "flatten_orders": s["flatten_orders"],
            "rate_throttled": s["rate_throttled"], "modify_cap_cancels": s["modify_cap_cancels"],
            "race_explained": s["race_explained"], "race_unexplained": s["race_unexplained"],
            "flat_after_kill_ms": s["flat_after_kill_ms"], "flatten_failed": s["flatten_failed"],
            "flatten_failed_ticks": s["flatten_failed_ticks"], "flatten_residual": self._flatten_residual,
            "ufr_max": s["ufr_max"], "ufr_minutes_above": s["ufr_minutes_above"],
            "h2_predicted_fills": s["h2_predicted_fills"],
        }

    # ----------------------------------------------------------------------------- risk (spec §7)
    def _risk_checks(self, state: MarketState, now_ns: int):
        """Inventory breach and drawdown; returns kill actions or None."""
        if self._killed:
            return None
        q = state.position
        if abs(q) > self.p.inventory_breach_mult * self.p.max_inventory + _EPS:
            return self._kill("inventory_breach", True, state)
        eq = state.equity
        if eq is not None:
            self._peak_equity = max(self._peak_equity, eq)
            if self._peak_equity - eq > self.p.max_drawdown_usdt:
                return self._kill("drawdown", True, state)
        return None

    def _kill(self, cause: str, flatten: bool, state: MarketState):
        """Stop quoting for the rest of the period; cancel everything; optionally flatten (spec §7.1)."""
        self._killed = cause
        self._kill_flatten = flatten and self.p.flatten_on_kill
        self._kill_ns = state.now_ns
        self.stats["kills"][cause] += 1
        self._weight_1m.add(state.now_ns)
        actions = [CancelAll()]
        if self._kill_flatten:                       # first exit order now; the timer sends the next ones
            actions += self._flatten_orders(state) or []     # "X or []": [] when X is None, so += is safe
        return actions

    def _flatten_orders(self, state: MarketState):
        """Kill-switch exit (spec §7.1): flatten_ioc_attempts limit IOCs at touch +/- slip, then MARKET orders.

        Called at the kill and on every timer tick. One order at a time (the next only once the previous is
        final), sized from the q we see, and no attempt cap: we never stop while not flat."""
        p, q, now = self.p, state.position, state.now_ns
        if abs(q) < p.min_qty - _EPS:                # flat
            if self.stats["flat_after_kill_ms"] is None:
                self.stats["flat_after_kill_ms"] = (now - self._kill_ns) / NS_PER_MS
            return None
        if self._flatten_residual is not None:       # a leftover below min notional was rejected: stop
            return None
        if now - self._kill_ns >= self._alert_ns:    # still not flat: the "look now" state
            self._flatten_alert(q, now)
        # any(cond for x in items): generator expression, True if cond holds for one item
        # (here: a flatten order, id starting with "f", is still in flight).
        if (any(_is_flatten(cid) for cid in self._inflight)
                or state.bid_px is None or state.ask_px is None):      # no prices = gap/resync: wait
            return None
        qty = state.round_qty(abs(q))
        if qty < p.min_qty - _EPS:
            return None
        # No budget check for an exit (the budget is only 50% of Binance's limit and exits are few), but it
        # is still counted in the windows.
        self._record_sent(now, qty)
        side = SELL if q > 0 else BUY
        cid = self.new_id("f")
        self._inflight.add(cid)
        n_sent = self.stats["flatten_orders"]
        self.stats["flatten_orders"] += 1
        if n_sent < p.flatten_ioc_attempts:          # cheap try first: limit IOC at the touch +/- slip
            slip = p.flatten_slip_ticks * state.tick_size
            px = state.round_price(state.bid_px - slip if side == SELL else state.ask_px + slip)
            return [PlaceOrder(cid, side, qty, px, post_only=False, tif="IOC")]
        # Then a MARKET order (price=None): the engine fills it as a taker and never lets it rest.
        return [PlaceOrder(cid, side, qty, None, post_only=False, tif="IOC")]

    def _flatten_alert(self, q: float, now_ns: int) -> None:
        """State flatten_failed (spec §7.1): record it once, count every tick; the exit keeps going."""
        self.stats["flatten_failed_ticks"] += 1
        if self.stats["flatten_failed"] is None:
            self.stats["flatten_failed"] = {"after_ms": (now_ns - self._kill_ns) / NS_PER_MS, "q": q,
                                            "orders": self.stats["flatten_orders"]}
            if self.p.print_stats_on_stop:
                print(f"[glft] FLATTEN FAILED: q={q} still open {self.p.flatten_alert_ms:.0f} ms after the kill", flush=True)
    # ---------------------------------------------------------------------------- quote cycle
    def _blocked(self, state: MarketState, now_ns: int) -> bool:
        """Spec §5.3: no quoting while paused, in a gap cooldown, or without a mid."""
        return (self._stale_paused or self._gap_wait or now_ns < self._cooldown_until_ns
                or self._spread_gate.active or self._vol_gate.active
                or state.bid_px is None or state.ask_px is None)

    def _live_order(self, state: MarketState, side: str):
        """Our live quote order on ``side`` (any status), or None."""
        for v in state.open_orders.values():
            if v.side == side and self._quote_ids.get(v.client_id) == side:
                return v
        return None

    def _exposure(self, state: MarketState, side: str, exclude: str | None) -> float:
        """Sum of remaining size of every live order on ``side`` (pending_new, open, pending_cancel)."""
        return sum(v.remaining for v in state.open_orders.values()
                   if v.side == side and v.client_id != exclude)

    def _quote_cycle(self, state: MarketState, now_ns: int, force: bool):
        """Spec §5.3: compute the target quotes and apply the requote rule per side."""
        if self._killed:
            return None
        if self._blocked(state, now_ns):
            # ensure no live orders: CancelAll once per blocked episode
            if not self._cancel_all_sent and any(v.client_id in self._quote_ids
                                                 for v in state.open_orders.values()):
                self._cancel_all_sent = True
                self._weight_1m.add(now_ns)
                return [CancelAll()]
            return None
        self._cancel_all_sent = False
        mid, q = state.mid, state.position
        t = compute_quotes(self.p, mid, q, state.bid_px, state.ask_px, state.tick_size)
        actions = []
        for side in (BUY, SELL):
            a = self._side_action(state, side, t, mid, q, now_ns, force)
            if a is not None:
                actions.append(a)
        return actions

    def _desired(self, state: MarketState, side: str, t: QuoteTargets, q: float, exclude: str | None):
        """Spec §4.5: (price, qty) we want on this side, or None if it must not quote."""
        p = self.p
        px = t.bid_px if side == BUY else t.ask_px
        if px < state.tick_size - _EPS:                  # a huge skew can push a bid to <= 0
            return None
        others = self._exposure(state, side, exclude)
        # headroom = how much more we may buy/sell without |q| ever exceeding Q, counting in-flight orders
        headroom = (p.max_inventory - q - others) if side == BUY else (p.max_inventory + q - others)
        if headroom <= 0:
            return None
        qty = state.round_qty(min(p.order_qty, headroom))        # rounds DOWN to the lot
        if qty < p.min_qty - _EPS or qty * px < p.min_notional_usdt - _EPS:
            return None
        return px, qty

    def _side_action(self, state, side, t: QuoteTargets, mid, q, now_ns, force):
        """Spec §4.6: the requote table for one side; returns one action or None."""
        p, tick = self.p, state.tick_size
        live = self._live_order(state, side)
        cid = live.client_id if live is not None else None
        want = self._desired(state, side, t, q, cid)
        if live is None:
            if want is None or now_ns < self._side_cooldown_until[side]:
                return None
            px, qty = want
            if not self._budget_ok(now_ns):
                self.stats["rate_throttled"] += 1
                return None
            new = self.new_id("b" if side == BUY else "a")
            self._quote_ids[new] = side
            self._inflight.add(new)
            self._last_action_ns[side] = now_ns
            self._record_sent(now_ns, qty)
            self._note_quote(side, px, mid, tick, "placed")
            return PlaceOrder(new, side, qty, px, post_only=True, tif="GTC")
        if cid in self._inflight:
            return None                                   # an action on it is still in flight
        if want is None:
            return self._cancel(cid, side, now_ns)
        px, qty = want
        if now_ns < self._side_cooldown_until[side]:
            return None
        diff_ticks = round((px - live.price) / tick)      # + means target is above the live price
        # Urgent (spec §4.6): the live order sits too close to mid compared with the target.
        #   bid: px_live - px* >= urgent;   ask: px* - px_live >= urgent.
        too_close_ticks = -diff_ticks if side == BUY else diff_ticks
        urgent = too_close_ticks >= p.urgent_requote_ticks - _EPS
        last = self._last_action_ns[side]
        interval_ok = force or last is None or now_ns - last >= self._requote_gap_ns
        if abs(diff_ticks) >= p.requote_ticks - _EPS and (interval_ok or urgent):
            return self._replace(cid, side, px, qty, now_ns, (mid, tick))       # always pass qty (spec §4.6)
        # Not in the spec's table: if the live remainder is larger than today's headroom allows,
        # shrink it at the same price so |q| cannot exceed Q (the engine re-queues every replace, like Binance).
        if live.remaining > qty + _EPS:
            return self._replace(cid, side, live.price, qty, now_ns, None)
        return None

    # ---- rate guard (spec §7.4)
    def _budget_ok(self, now_ns: int) -> bool:
        """May we send one more place/replace? Cancels never ask: they are always allowed."""
        p = self.p
        return (self._sent_10s.total(now_ns) < p.rate_orders_10s_max
                and self._sent_1m.total(now_ns) < p.rate_orders_1m_max
                and self._weight_1m.total(now_ns) < p.rate_ip_weight_1m_max)

    def _record_sent(self, now_ns: int, qty: float) -> None:
        """Every place/replace counts (also flatten orders, and ones the exchange later rejects)."""
        self._sent_10s.add(now_ns)
        self._sent_1m.add(now_ns)
        self._ufr_placed.add(now_ns, qty)

    def _cancel(self, cid: str, side: str, now_ns: int):
        self._inflight.add(cid)
        self._weight_1m.add(now_ns)
        self.stats["cancelled"][side] += 1
        return CancelOrder(cid)

    def _replace(self, cid: str, side: str, price: float, qty: float, now_ns: int, note):
        """Send a replace, unless the order hit its modify cap (then cancel; the next cycle places a new one)
        or the rate budget is used up (then send nothing). ``note`` = (mid, tick) to log the posted distance.

        ``qty`` is the REMAINING size we want (spec §4.6). We send the order's TOTAL size = filled we know
        of + qty (Binance modify semantics): the exchange subtracts any fill we have not heard about yet,
        so |q| stays within Q (validator F5)."""
        if self._modifies.get(cid, 0) >= self.p.max_modifies_per_order:
            self.stats["modify_cap_cancels"] += 1
            return self._cancel(cid, side, now_ns)
        if not self._budget_ok(now_ns):
            self.stats["rate_throttled"] += 1
            return None
        self._inflight.add(cid)
        self._last_action_ns[side] = now_ns
        self._modifies[cid] = self._modifies.get(cid, 0) + 1
        self._record_sent(now_ns, qty)
        if note is not None:
            self._note_quote(side, price, note[0], note[1], "replaced")
        return ReplaceOrder(cid, price=price, total_qty=round(self._filled.get(cid, 0.0) + qty, 10))

    def _note_quote(self, side: str, px: float, mid: float, tick: float, what: str) -> None:
        self.stats[what][side] += 1
        self.stats["delta_ticks_sum"][side] += abs(mid - px) / tick
        self.stats["delta_ticks_n"][side] += 1
