"""The event-driven engine: one clock, one ordered queue of things that happen.

The same ``Engine`` object is driven by the backtest (``run.py`` pushes replayed events in) and by
the paper trader (``mm.paper.run`` pushes live events in).  It never looks at a wall clock: time
only advances when an event (or a heartbeat) tells it to.  That is what makes backtests
reproducible and what keeps causality honest.

How time works (see the README for the picture)
-----------------------------------------------
Market events arrive in exchange-time order.  For each event at exchange time ``t``:

1. everything scheduled strictly before ``t`` is executed first (order arrivals, deliveries ...);
2. the SIMULATED EXCHANGE processes the event at ``t`` (queues move, resting orders may fill);
3. the event is scheduled for delivery to the STRATEGY at ``max(T, E) + feed_latency`` where ``T`` is
   the exchange match time (``ts_ns``) and ``E`` the time Binance PUBLISHED the message
   (``event_ts_ns``; you cannot receive a message before it was published). When ``E`` is missing
   (archive trades) a configured small extra delay is added to ``T`` instead.

When the strategy answers with actions at its local time ``u``, they are scheduled to reach the
exchange at ``u + order_latency``.  Fills / acks the exchange produces are scheduled to reach the
strategy at ``exchange_time + report_latency``.  Because an action is processed at its arrival time,
a cancel that is "in flight" loses the race against a fill that happens first.

Ties (same ``ts_ns``): ALL market events with timestamp ``t`` are processed by the exchange BEFORE any
order that arrives at ``t``. Scheduled items due exactly at ``t`` only run when the first event with a
later timestamp arrives (or at ``flush()`` / ``finish()``). So an order arriving at 30.000 ms sees every
print of that millisecond already done, and none of them can fill it: conservative and independent of
how many prints happened to share the millisecond.

Gaps: a ``GapEvent`` (feed lost messages) makes the exchange cancel everything, forget its book and
reject new orders until the next snapshot; the strategy's prices are cleared and ``on_gap`` is called.

All times are integer nanoseconds UTC.
"""
# Lazy type hints (see strategy.py).
from __future__ import annotations

# heapq: a "priority queue". heappush adds an item; heappop always returns the SMALLEST item first.
# We store tuples starting with the time, so the earliest scheduled thing always comes out first.
import heapq
# importlib / inspect: load a module by name at runtime and look inside it (to find strategy classes).
import importlib
import inspect
import json
from dataclasses import replace
from typing import Any, Iterable

import numpy as np

from mm.backtest.config import ms_to_ns, validate_latency
from mm.backtest.exchange import SimExchange
from mm.backtest.fills import FeeSchedule, make_fill_model
from mm.backtest.metrics import Account, Recorder, compute_metrics
from mm.backtest.strategy import (Action, CancelAll, CancelOrder, Fill, MarketState, OrderView,
                                  PlaceOrder, ReplaceOrder, Strategy)
from mm.data.events import GapEvent

# Kinds of scheduled item (small integers are cheap to compare). Reading them:
#  _ARRIVE  an order action reaches the exchange     _DELIVER a market event reaches the strategy
#  _REPORT  a fill/ack reaches the strategy          _TIMER   the strategy's periodic wake-up
_ARRIVE, _DELIVER, _REPORT, _TIMER = 0, 1, 2, 3

#: short names -> "module:Class".  Real strategies from specs/ get registered here (or are found
#: automatically as ``mm.strategies.<name>`` containing exactly one Strategy subclass).
STRATEGY_REGISTRY: dict[str, str] = {
    "demo_quoter": "mm.backtest.examples.demo_quoter:DemoQuoter",
}


def resolve_strategy(name: str) -> type[Strategy]:
    """Find a Strategy class by registry name, ``"pkg.module:Class"``, or ``mm.strategies.<name>``."""
    # Lookup order: short registry name -> "module:Class" path -> a module in mm.strategies.
    # dict.get(key, default) returns the default if the key is missing. getattr(obj, "name") fetches
    # an attribute by a name held in a variable. f"...{x}" is an f-string (inserts x into the text).
    target = STRATEGY_REGISTRY.get(name, name)
    if ":" in target:
        mod, cls = target.split(":", 1)
        return getattr(importlib.import_module(mod), cls)
    mod = importlib.import_module(f"mm.strategies.{target}")
    if hasattr(mod, "STRATEGY"):
        return mod.STRATEGY
    # List comprehension that filters: keep classes defined in this module that subclass Strategy.
    found = [c for _, c in inspect.getmembers(mod, inspect.isclass)
             if issubclass(c, Strategy) and c is not Strategy and c.__module__ == mod.__name__]
    if len(found) != 1:
        raise ValueError(f"mm.strategies.{target}: expected exactly one Strategy subclass or a STRATEGY "
                         f"attribute, found {[c.__name__ for c in found]}")
    return found[0]


# The Engine owns the CLOCK. Everything that "will happen later" (an order arriving after latency, a
# delayed market update, a timer) is put on one heap keyed by time and executed in time order. That
# single ordered queue is what makes the simulation causal: nothing can influence anything earlier.
class Engine:
    """Glue between market events, the strategy, the simulated exchange and the books."""

    # Class-level default; build_engine() replaces it with the real metrics settings for each instance.
    cfg_metrics: dict = {}
    seed: int = 0     # run seed (build_engine sets it); seeds the markout bootstrap

    def __init__(self, cfg: dict, strategy: Strategy, exchange: SimExchange, account: Account,
                 recorder: Recorder, state: MarketState, feed_rng: np.random.Generator,
                 order_rng: np.random.Generator, on_fill_hook=None) -> None:
        # Convert latency settings from config (ms) into ns once, up front.
        # report latency = delay before the strategy learns of a fill/ack (None means "same as feed").
        lat = cfg["latency"]
        self.feed_ns = ms_to_ns(lat["feed_latency_ms"])
        self.order_ns = ms_to_ns(lat["order_latency_ms"])
        rl = lat.get("report_latency_ms")
        self.report_ns = self.feed_ns if rl is None else ms_to_ns(rl)
        self.order_jit = ms_to_ns(lat.get("order_jitter_ms"))
        self.feed_jit = ms_to_ns(lat.get("feed_jitter_ms"))
        self.timer_ns = ms_to_ns(lat.get("timer_interval_ms"))
        # Extra delay for events that have no publish time E (archive trades): see module docstring.
        self.no_e_ns = ms_to_ns(lat.get("no_event_ts_extra_ms"))
        # Empirical feed-delay table (see config.py): inverse-CDF sampling points in ns. None = use the
        # fixed feed_latency_ms + uniform jitter instead.
        tbl = lat.get("feed_delay_quantiles_ms")
        self._dly_p = np.array([p for p, _ in tbl], dtype=float) if tbl else None
        self._dly_ns = np.array([ms_to_ns(d) for _, d in tbl], dtype=float) if tbl else None
        self._knot_ns = ms_to_ns(lat.get("feed_delay_knot_ms")) or 1
        self._seg = None          # (end of the current window, delay drawn for it)
        self.strategy = strategy
        self.kinds = frozenset(strategy.event_kinds)
        self.ex = exchange
        self.account = account
        self.rec = recorder
        self.state = state
        # Two separate random streams (common random numbers): the feed-delay path must not change
        # just because a strategy variant sent a different number of orders (each order draws jitter).
        self.feed_rng = feed_rng
        self.order_rng = order_rng
        self.on_fill_hook = on_fill_hook   # optional callback(fill) -- the paper trader logs with it
        self.now = 0
        self._heap: list[tuple] = []
        self._seq = 0
        self._started = False
        self._last_arrival = 0
        self._last_delivery = 0
        self._pending_replace: dict[str, ReplaceOrder] = {}
        self.n_events = 0
        # Warm-up bookkeeping (see warm_up): events fed, is the book synced, time of the last snapshot.
        self.warm_events = 0
        self.warm_book_synced = False
        self.warm_last_snapshot_ns: int | None = None

    # ------------------------------------------------------------------ scheduling
    # Schedule something for `time`. The heap item is a tuple (time, seq, kind, payload); Python compares
    # tuples element by element, so ordering is by time, then by seq (a running counter that breaks ties
    # in the order things were scheduled and stops Python ever trying to compare the payloads).
    def _push(self, time: int, kind: int, payload: Any) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (time, self._seq, kind, payload))

    # Pop and perform every scheduled item due before time t (or at t when inclusive). h[0] is the
    # earliest item. Each kind does its own thing: an order reaches the exchange, a market event reaches
    # the strategy (it may answer with new actions), a report reaches the strategy, or a timer fires.
    # "time, _, kind, payload = ..." unpacks the tuple; "_" marks a value we do not need.
    def _run_until(self, t: int, inclusive: bool) -> None:
        h = self._heap
        while h and (h[0][0] <= t if inclusive else h[0][0] < t):
            time, _, kind, payload = heapq.heappop(h)
            self.now = time
            if kind == _ARRIVE:
                self._process_outputs(self.ex.on_action(payload, time), time)
            elif kind == _DELIVER:
                # State is always updated (also for funding / gap, which it needs for cash and book
                # hygiene); the strategy callback only fires for subscribed kinds, gaps via on_gap.
                st = self.state
                st.now_ns = time
                st._apply(payload)
                if payload.kind == "gap":
                    self._submit(self.strategy.on_gap(payload, st), time)
                elif payload.kind in self.kinds:
                    self._submit(self.strategy.on_event(payload, st), time)
            elif kind == _REPORT:
                self._on_report(payload, time)
            else:  # _TIMER
                self.state.now_ns = time
                self._submit(self.strategy.on_timer(time, self.state), time)
                self._push(time + self.timer_ns, _TIMER, None)

    # ------------------------------------------------------------------ strategy -> exchange
    # The strategy has decided something at local time `time`. Each action is scheduled to ARRIVE at the
    # exchange after the order latency. Meanwhile the strategy's own order list is updated immediately
    # (status "pending") because it knows what it sent, though not yet what the exchange did.
    def _submit(self, actions: Iterable[Action] | None, time: int) -> None:
        if not actions:
            return
        st = self.state
        for a in actions:
            if isinstance(a, PlaceOrder):
                st.open_orders[a.client_id] = OrderView(a.client_id, a.side, a.price, a.qty, a.qty, "pending_new")
            elif isinstance(a, CancelOrder):
                v = st.open_orders.get(a.client_id)
                if v is not None:
                    v.status = "pending_cancel"
            elif isinstance(a, ReplaceOrder):
                self._pending_replace[a.client_id] = a
            elif not isinstance(a, CancelAll):
                raise TypeError(f"strategy returned a non-action: {a!r}")
            # Optional random jitter (a few extra ms, drawn from the order stream so runs repeat).
            # max(...) keeps arrivals in the order sent: one connection delivers messages in order.
            lat = self.order_ns + (int(self.order_rng.integers(0, self.order_jit + 1)) if self.order_jit else 0)
            arrival = max(time + lat, self._last_arrival)   # one FIFO connection to the exchange
            self._last_arrival = arrival
            self._push(arrival, _ARRIVE, a)

    # ------------------------------------------------------------------ exchange -> strategy
    # The exchange produced fills / order updates at time t. Fills hit our TRUE books immediately (the
    # money moved at that instant) while the strategy hears about them only after the report latency.
    def _process_outputs(self, outs: list, t: int) -> None:
        for o in outs:
            if isinstance(o, Fill):
                self.account.on_fill(o)
                self.rec.on_fill(o, self.account.position)
                if self.on_fill_hook is not None:
                    self.on_fill_hook(o, self.account)
                # The strategy gets a COPY without simulator internals (the true mid and why we were
                # filled): a real exchange sends neither, and ref_mid would leak information whenever
                # the report latency is shorter than the feed latency. replace() = copy with changes.
                self._push(t + self.report_ns, _REPORT, replace(o, ref_mid=None, mechanism=""))
                continue
            self._push(t + self.report_ns, _REPORT, o)

    # A fill or order update finally reaches the strategy: update the strategy's view of position,
    # cash and open orders, then call its callback (which may return more actions).
    def _on_report(self, o, time: int) -> None:
        st = self.state
        st.now_ns = time
        if isinstance(o, Fill):
            s = o.qty if o.side == "buy" else -o.qty
            st.position += s
            st.cash -= s * o.price + o.fee
            v = st.open_orders.get(o.client_id)
            if v is not None:
                v.remaining = o.leaves_qty
                if o.leaves_qty <= 1e-12:
                    del st.open_orders[o.client_id]
                    self._pending_replace.pop(o.client_id, None)   # a fully filled order cannot be replaced
                elif v.status == "pending_new":
                    v.status = "open"
            self._submit(self.strategy.on_fill(o, st), time)
            return
        v = st.open_orders.get(o.client_id)
        if o.status == "accepted":
            if v is not None:
                v.status = "open"
                v.remaining = o.remaining
        elif o.status in ("rejected", "canceled"):
            st.open_orders.pop(o.client_id, None)
            self._pending_replace.pop(o.client_id, None)
        elif o.status == "cancel_rejected":
            if v is not None and v.status == "pending_cancel":
                v.status = "open"
        elif o.status == "replaced":
            r = self._pending_replace.pop(o.client_id, None)
            if v is not None:
                v.status = "open"
                v.remaining = o.remaining
                if r is not None and r.price is not None:
                    v.price = r.price
        elif o.status == "replace_rejected":
            self._pending_replace.pop(o.client_id, None)
        self._submit(self.strategy.on_order_update(o, st), time)

    # ------------------------------------------------------------------ inputs
    def _start(self, t: int) -> None:
        self._started = True
        self.now = t
        self.state.now_ns = t
        self._submit(self.strategy.on_start(self.state), t)
        if self.timer_ns > 0:
            self._push(t + self.timer_ns, _TIMER, None)

    def warm_up(self, ev) -> None:
        """Feed one event BEFORE the window: it only builds the books, nothing else happens.

        Why: a live day has no depth snapshot at 00:00, so without a warm start the L2 book would begin
        empty. Warm-up events go to the exchange's book/market view (no orders exist yet, so no fills)
        and to the strategy's MarketState (prices), but NOT to the strategy callbacks, the clock, the
        account, the recorder or the latency queue. Call it before the first ``on_market_event``.
        Causal by construction: it is only ever given events older than the window start.
        """
        if self._started:
            raise RuntimeError("warm_up() must come before the first on_market_event()")
        self.warm_events += 1
        if ev.kind == "gap":
            # Same book hygiene as a real gap: forget the book; it is trustworthy again only after a snapshot.
            self.ex.on_book_lost(ev.ts_ns, "gap", ev.stream)
            self.state._apply(ev)
            if ev.stream not in self.state.trade_gap_streams:
                self.warm_book_synced = False
            return
        self.ex.on_market_event(ev, ev.ts_ns)          # nothing rests yet, so this only updates the book
        if ev.kind in self.kinds or ev.kind == "funding":
            self.state._apply(ev)
        if ev.kind == "depth" and ev.is_snapshot:
            self.warm_book_synced = True
            self.warm_last_snapshot_ns = ev.ts_ns

    def check_warm_start(self) -> None:
        """After the warm-up: a depth fill model needs a book synced from a snapshot, else stop loudly."""
        if "depth" in self.ex.model.consumes and not self.warm_book_synced:
            raise RuntimeError(
                f"warm-up ({self.warm_events:,} events) ended with no valid depth book: no snapshot after "
                "the last gap. Start the warm-up earlier (period warmup_from / warmup_hours must reach a "
                "snapshot) or move the window.")

    def on_market_event(self, ev) -> None:
        """Feed one market event (exchange time order).  Out-of-order timestamps are clamped to
        the engine clock (live feeds from two websockets can interleave by a few ms)."""
        # The main entry point, called once per market event, in this order: (1) run everything
        # scheduled earlier, (2) record equity, (3) let the exchange process the event (our orders may
        # fill), (4) funding: pay/receive on open positions, (5) re-mark the account to the new mid,
        # (6) schedule delivery to the strategy after feed latency. `t` is clamped so time never goes backwards.
        t = ev.ts_ns if ev.ts_ns > self.now else self.now
        if not self._started:
            self._start(t)
        self._run_until(t, inclusive=False)
        self.now = t
        self.rec.sample_until(t)
        self.rec.on_event_time(t)
        self.n_events += 1
        ex = self.ex
        # A gap marker: the exchange cancels every order and forgets its book; the strategy is told
        # after the feed latency.
        if ev.kind == "gap":
            self._process_outputs(ex.on_book_lost(t, "gap", ev.stream), t)
            self._schedule_delivery(ev, t)
            return
        outs = ex.on_market_event(ev, t)
        if outs:
            self._process_outputs(outs, t)
        if ev.kind == "funding":
            self.account.on_funding(ev.rate, ev.mark_price)
        mid = ex.mid
        if mid is not None and mid != self.account.last_mid:
            self.account.on_mid(mid)
            self.rec.on_mid(t, mid)
        if ev.kind in self.kinds or ev.kind == "funding":
            self._schedule_delivery(ev, t)
        # NOTE: items due exactly at t are deliberately NOT run here (see "Ties" in the module docstring).

    # When does the strategy get to see this event? Not before it was PUBLISHED: use the later of the
    # match time T (ts_ns) and the publish time E (event_ts_ns), then add feed latency and jitter.
    # Events without an E field at all (funding, gap) use T alone; events whose E is None (archive
    # trades) use T plus the configured typical publish delay.
    def _schedule_delivery(self, ev, t: int) -> None:
        base = t
        if hasattr(ev, "event_ts_ns"):
            e = ev.event_ts_ns
            base = max(t, e) if e is not None else t + self.no_e_ns
        if self._dly_p is not None:
            d = base + self._sample_delay(t)
        else:
            d = base + self.feed_ns
            if self.feed_jit:
                d += int(self.feed_rng.integers(0, self.feed_jit + 1))
        d = max(d, self._last_delivery)           # events stay in the order they happened
        self._last_delivery = d
        self._push(d, _DELIVER, ev)

    # Feed delay at exchange time t from the EMPIRICAL distribution. Real delays are not independent per
    # message: they come in slow and fast stretches (bursts, e.g. 0.5-1 s lag spikes). So we draw ONE delay
    # per window of feed_delay_knot_ms by inverse-CDF sampling (np.interp maps a uniform random number u in
    # [0, 1] through the measured (probability, delay) points) and use it for every event in that window.
    # The caller then keeps events in order (delivery = max(this, previous delivery)): after a slow window
    # the backlog drains in a burst, exactly what a real websocket does. Drawing a fresh delay per EVENT
    # instead would, together with the ordering rule, push almost every delay to the top of the range
    # (the validator's R3); with ~2 s windows the effective p50/p99 match the table closely and p90 is a
    # little high (the backlog drain makes spikes last slightly longer). tests/test_backtest_fixes.py checks it.
    def _sample_delay(self, t: int) -> int:
        if self._seg is None or t >= self._seg[0]:
            u = self.feed_rng.random()
            self._seg = (t + self._knot_ns, int(np.interp(u, self._dly_p, self._dly_ns)))
        return self._seg[1]

    def on_book_lost(self, t: int | None = None, reason: str = "feed_lost", stream: str | None = None) -> None:
        """Live feed dropped / book resynced: queue estimates are unreliable. Handled exactly like a
        ``GapEvent`` from the data layer: cancel resting orders, forget the book, block quoting until the
        next snapshot, tell the strategy (the paper trader uses this for ``StatusEvent`` disconnects)."""
        t = self.now if t is None else max(t, self.now)
        self.on_market_event(GapEvent(ts_ns=t, reason=reason, stream=stream))

    def flush(self) -> None:
        """Run everything due up to and including the current clock time (used at the end of a batch of
        events, and by tests). Normal operation never needs it."""
        self._run_until(self.now, inclusive=True)

    def advance(self, t: int) -> None:
        """Move the clock forward with no market event (paper-trading heartbeat)."""
        if not self._started or t <= self.now:
            return
        # inclusive=False keeps the tie rule: things due exactly at t wait until the market events of
        # that timestamp have been processed (they have not arrived yet in a live setting).
        self._run_until(t, inclusive=False)
        self.now = t
        self.rec.sample_until(t)

    def finish(self) -> tuple[dict, Any, Any]:
        """End of data: drain what is due, notify the strategy, compute metrics."""
        if self._started:
            self._run_until(self.now, inclusive=True)
            self.rec.finish(self.now)
        self.strategy.on_stop(self.state)
        metrics, fills, eq = compute_metrics(self.rec, self.account, self.ex, self.cfg_metrics, self.seed)
        # A strategy may offer summary() -> JSON-able dict (pauses, kills, rejects ...): keep it in the
        # report. The json round trip makes sure only plain JSON types end up in metrics.json.
        summary = getattr(self.strategy, "summary", None)
        if callable(summary):
            metrics["strategy_summary"] = json.loads(json.dumps(summary(), default=str))
        # Optional realism check: what would the final inventory be worth if we had to exit at the
        # touch (sell at the bid / buy at the ask) and pay the taker fee? Inventory is otherwise marked
        # at mid for free, which flatters a strategy that ends the period holding risk.
        a, mk = self.account, self.ex.mkt
        exit_px = mk.bid_px if a.position > 0 else mk.ask_px
        if a.position != 0 and exit_px is not None:
            liq = a.cash + a.position * exit_px - abs(a.position) * exit_px * self.ex.fees.taker
            metrics["pnl"]["net_if_liquidated_at_touch"] = liq - a.start_cash
        return metrics, fills, eq


# ====================================================================== construction
def build_engine(cfg: dict, seed: int | None = None, strategy_cls: type[Strategy] | None = None,
                 on_fill_hook=None) -> Engine:
    """Create a fresh engine (fresh strategy, exchange, account) from a resolved config.

    ``seed`` is split into three independent random streams: feed delay, order/report jitter, strategy.
    """
    # Wire everything together. A fixed seed makes the random generators produce the same numbers every
    # run, so results are reproducible. SeedSequence(seed).spawn(3) makes three statistically independent
    # child seeds, so no source's draws shift when another source draws more or fewer numbers.
    validate_latency(cfg)      # refuse zero latency (same-millisecond leakage) unless a test opted in
    seed = cfg["run"]["seed"] if seed is None else seed
    feed_ss, order_ss, strat_ss = np.random.SeedSequence(seed).spawn(3)
    ex_cfg = cfg["exchange"]
    model = make_fill_model(cfg["fill_model"], ex_cfg["tick_size"])
    exchange = SimExchange(model, FeeSchedule(ex_cfg["maker_fee"], ex_cfg["taker_fee"]),
                           ex_cfg["tick_size"], ex_cfg["lot_size"], ex_cfg["min_qty"], ex_cfg["min_notional"],
                           ex_cfg.get("max_position"), ex_cfg.get("max_order_notional"),
                           tuple(ex_cfg.get("gap_trade_streams", ("market",))),
                           ex_cfg.get("replace_keeps_priority", False))
    account = Account(cfg["account"]["starting_cash"])
    m = cfg["metrics"]
    recorder = Recorder(account, ms_to_ns(m["equity_bucket_s"] * 1000), ms_to_ns(m["mid_sample_ms"]),
                        ms_to_ns(m.get("coverage_gap_s", 60.0) * 1000))
    cls = strategy_cls or resolve_strategy(cfg["strategy"]["name"])
    strategy = cls(params=cfg["strategy"].get("params", {}), rng=np.random.default_rng(strat_ss))
    state = MarketState(ex_cfg["tick_size"], ex_cfg["lot_size"])
    state.cash = account.start_cash
    state.trade_gap_streams = frozenset(ex_cfg.get("gap_trade_streams", ("market",)))
    eng = Engine(cfg, strategy, exchange, account, recorder, state,
                 np.random.default_rng(feed_ss), np.random.default_rng(order_ss), on_fill_hook=on_fill_hook)
    eng.cfg_metrics = m
    eng.seed = seed
    return eng
