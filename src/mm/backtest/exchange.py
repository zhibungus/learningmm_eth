"""The simulated exchange: order validation, post-only, matching against our own resting orders.

The engine hands this class (a) market events at EXCHANGE time and (b) strategy actions at their
ARRIVAL time (decision time + order latency).  It returns ``Fill`` and ``OrderUpdate`` objects that
the engine then delivers back to the strategy after the report latency.

Rules enforced here (the same ones Binance USD-M enforces):
* price must be a multiple of the tick size, qty a multiple of the lot size, qty >= min_qty,
  notional >= min_notional                                -> else rejected
* ``client_id`` must be unique among live orders          -> else rejected
* post-only orders that would execute immediately are REJECTED, not filled
* a marketable limit order / market order is a TAKER: it pays the spread (walks the available
  liquidity, see ``fills.py``) and the taker fee; whatever is left of a GTC limit rests as a maker
* an IOC or market order never rests: what is not filled on arrival is cancelled at once
* a replace (modify) goes to the back of the queue unless ``replace_keeps_priority``; see ``ReplaceOrder``
* a resting order only fills according to the chosen fill model (queue position etc.)
* optional hard risk limits (config ``exchange.max_position`` / ``max_order_notional``): an order that
  could push |position| past the limit (counting every open order too) is rejected
  ("risk_max_position"); so is one with too large a notional ("risk_max_order_notional")
* after a feed GAP the book is cleared and new orders are rejected ("awaiting_resync") until a fresh
  snapshot (depth models) or quote (quote model) has arrived

Not modelled: margin / liquidation, rate limits, self-trade prevention.
"""
# Lazy type hints (so "str | None" works everywhere); see strategy.py for the explanation.
from __future__ import annotations

import math
# Counter: a dict that counts things. counter["x"] += 1 works even if "x" was never seen before.
from collections import Counter

from mm.backtest.fills import FeeSchedule, FillModel, MarketView, Order
from mm.backtest.strategy import (BUY, SELL, CancelAll, CancelOrder, Fill, OrderUpdate, PlaceOrder,
                                  ReplaceOrder)


# SimExchange plays the role of Binance's matching engine, but only for OUR orders: the rest of the
# market is replayed from data. It owns the resting orders and asks the fill model when they trade.
class SimExchange:
    def __init__(self, model: FillModel, fees: FeeSchedule, tick_size: float, lot_size: float,
                 min_qty: float, min_notional: float, max_position: float | None = None,
                 max_order_notional: float | None = None,
                 gap_trade_streams: tuple[str, ...] = ("market",),
                 replace_keeps_priority: bool = False) -> None:
        self.model = model
        # False (Binance-like): every modify re-queues the order at the back. True: a same-price size
        # cut keeps its place in the queue (the old, more optimistic rule).
        self.replace_keeps_priority = replace_keeps_priority
        self.fees = fees
        self.tick = tick_size
        self.lot = lot_size
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.max_position = max_position
        self.max_order_notional = max_order_notional
        # GapEvent.stream values meaning "only the TRADES feed dropped" (see on_book_lost).
        self.gap_trade_streams = frozenset(gap_trade_streams)
        # The exchange tracks the true position itself (from the fills it creates) for the risk check.
        self.position = 0.0
        # True after a feed gap until the data has resynchronised; orders are rejected meanwhile.
        # A depth model also starts in this state: its book is built from diffs, which are only
        # meaningful on top of a full snapshot, so no order is accepted before the first snapshot.
        # (Quote models need no snapshot and start ready.)
        self.awaiting_resync = "depth" in model.consumes
        self.mkt = MarketView()
        # resting maps client_id -> Order for orders currently live in the (simulated) book.
        self.resting: dict[str, Order] = {}
        self._next_oid = 0
        # Statistics for the report (fill ratio, reject reasons ...).
        self.stats: Counter = Counter()
        # Counting rules: "submitted/accepted/rejected" and reject_reasons are for NEW orders only;
        # replaces are counted apart ("replace_requests/replaced/replace_rejected", replace_reject_reasons),
        # so the fill ratio is not diluted by thousands of re-quotes.
        self.reject_reasons: Counter = Counter()
        self.replace_reject_reasons: Counter = Counter()
        # A replace re-places the order under a NEW order_id (back of the queue). _root maps such an id to
        # the id of the original order, so "orders with a fill" counts each original order once.
        self._root: dict[int, int] = {}
        self._filled_orders: set[int] = set()   # every order_id (incl. replaced incarnations) with a fill
        self._filled_roots: set[int] = set()    # original orders with a fill
        self.submitted_qty = 0.0
        self.accepted_qty = 0.0

    # ------------------------------------------------------------------ market side
    # @property: read as exchange.mid (no parentheses).
    @property
    def mid(self) -> float | None:
        return self.mkt.mid

    def on_market_event(self, ev, t: int) -> list[Fill]:
        """Apply one market event at exchange time ``t``; return the maker fills it caused."""
        # Events the chosen model does not read (e.g. quotes when using the L2 model) are skipped.
        if ev.kind not in self.model.consumes:
            return []
        # Resync detection: a depth model is trustworthy again after a full snapshot; the quote model
        # (no book to rebuild) after the next quote. The event is still applied below as normal.
        if self.awaiting_resync and ((ev.kind == "depth" and ev.is_snapshot) or
                                     (ev.kind == "quote" and "depth" not in self.model.consumes)):
            self.awaiting_resync = False
        # Remember the mid before the event: a fill is measured against the price level that existed
        # when we traded, so adverse moves AFTER it show up as inventory PnL, not as spread capture.
        # tuple(...values()) takes a snapshot, since fills can remove orders while we iterate.
        ref = self.mkt.mid  # mid BEFORE this event (used for spread-capture accounting)
        orders = tuple(self.resting.values()) if self.resting else ()
        # The model says WHICH orders trade; _execute turns each into a Fill (with fee), or None
        # if the amount rounds to zero lots.
        execs = self.model.apply_event(ev, self.mkt, orders)
        fills: list[Fill] = []
        for x in execs:
            f = self._execute(x.order, x.price, x.qty, "maker", t, ref, x.mechanism)
            if f is not None:
                fills.append(f)
        return fills

    # A feed gap. What we must forget depends on WHICH feed broke (GapEvent.stream):
    #  * the TRADES socket ("market"): we may have missed prints, so our resting orders can no longer be
    #    trusted (they might already have been filled) and are cancelled, and the last trade price is
    #    dropped. But the book comes from a different socket that kept running, so it is KEPT and quoting
    #    may resume at once. (Wiping it would leave no snapshot to rebuild from: the validator's R1.)
    #  * anything else ("depth", "public", or None/unknown = be conservative): the book / quotes are
    #    stale, so cancel everything, forget all public prices and refuse orders until a fresh snapshot
    #    (depth model) or quote (quote model) arrives.
    def on_book_lost(self, t: int, reason: str = "connection_lost", stream: str | None = None) -> list[OrderUpdate]:
        """Handle a feed gap; returns the cancellations."""
        self.model.reset_episodes()
        if stream in self.gap_trade_streams:
            self.mkt.last_px = None
        else:
            self.awaiting_resync = True
            self.mkt.clear()
        return self._cancel_many(list(self.resting.values()), t, reason)

    # ------------------------------------------------------------------ strategy side
    def on_action(self, action, t: int) -> list[Fill | OrderUpdate]:
        """Process one action arriving at the exchange at time ``t``."""
        # isinstance(x, Class) asks "is x an instance of Class?"; used to dispatch on the action type.
        # "list[Fill | OrderUpdate]" in the signature = a list whose items are either type.
        if isinstance(action, PlaceOrder):
            return self._place(action, t)
        if isinstance(action, CancelOrder):
            o = self.resting.pop(action.client_id, None)
            if o is None:
                self.stats["cancel_rejected"] += 1
                return [OrderUpdate(t, action.client_id, "cancel_rejected", "unknown_order")]
            self.stats["canceled"] += 1
            return [OrderUpdate(t, o.client_id, "canceled", "user", o.remaining)]
        if isinstance(action, ReplaceOrder):
            return self._replace(action, t)
        if isinstance(action, CancelAll):
            # List comprehension: build a list by filtering (here: all orders, or only one side).
            return self._cancel_many([o for o in self.resting.values()
                                      if action.side is None or o.side == action.side], t, "user")
        raise TypeError(f"unknown action {action!r}")

    # ------------------------------------------------------------------ internals
    def _cancel_many(self, orders: list[Order], t: int, reason: str) -> list[OrderUpdate]:
        out = []
        for o in orders:
            self.resting.pop(o.client_id, None)
            self.stats["canceled"] += 1
            out.append(OrderUpdate(t, o.client_id, "canceled", reason, o.remaining))
        return out

    # Is x a whole multiple of step? Floats are inexact (0.1/0.01 may be 10.000000000000002), so we
    # allow a tiny tolerance instead of demanding exact equality.
    def _on_grid(self, x: float, step: float) -> bool:
        n = x / step
        return abs(n - round(n)) < 1e-6

    # Exchange-side sanity checks. Returns a reject reason, or None if the order is fine.
    # Binance rejects off-tick prices, off-lot sizes and tiny notionals, so we do too.
    def _validate(self, a: PlaceOrder) -> str | None:
        if a.side not in (BUY, SELL):
            return "bad_side"
        if a.tif not in ("GTC", "IOC"):
            return "bad_tif"
        if a.price is not None and (a.price <= 0 or not self._on_grid(a.price, self.tick)):
            return "price_not_on_tick"
        if a.qty <= 0 or not self._on_grid(a.qty, self.lot):
            return "qty_not_on_lot"
        if a.qty < self.min_qty - 1e-12:
            return "qty_below_min"
        if a.post_only and (a.price is None or a.tif == "IOC"):
            return "post_only_needs_gtc_limit"
        ref = a.price if a.price is not None else self.mkt.mid
        if ref is not None and ref * a.qty < self.min_notional - 1e-9:
            return "notional_below_min"
        return None

    # Hard risk limits enforced by the exchange simulator itself (a safety net under the strategy's own,
    # DELAYED view of its position). Worst case = every open order on that side fills at once, e.g.
    # in a flash move. Position is signed: + long, - short. Limits come from config (None = off).
    def _risk_check(self, a: PlaceOrder) -> str | None:
        px = a.price if a.price is not None else self.mkt.mid
        if self.max_order_notional is not None and px is not None and px * a.qty > self.max_order_notional + 1e-9:
            return "risk_max_order_notional"
        if self.max_position is not None:
            if a.side == BUY:
                open_buys = sum(o.remaining for o in self.resting.values() if o.side == BUY)
                if self.position + open_buys + a.qty > self.max_position + 1e-9:
                    return "risk_max_position"
            else:
                open_sells = sum(o.remaining for o in self.resting.values() if o.side == SELL)
                if self.position - open_sells - a.qty < -self.max_position - 1e-9:
                    return "risk_max_position"
        return None

    # ``old`` is given only for a replace: the order being replaced (already taken out of ``resting``).
    # A replace reuses this method; only the status names and the counters differ.
    def _place(self, a: PlaceOrder, t: int, old: Order | None = None) -> list[Fill | OrderUpdate]:
        replacing = old is not None
        ok_status, bad_status = ("replaced", "replace_rejected") if replacing else ("accepted", "rejected")
        if not replacing:
            self.stats["submitted"] += 1
            self.submitted_qty += a.qty

        # A function defined inside a function ("closure"): it can read the outer variables (a, t,
        # bad_status), which saves repeating the same three lines at every reject point below.
        def reject(reason: str):
            self.stats["replace_rejected" if replacing else "rejected"] += 1
            (self.replace_reject_reasons if replacing else self.reject_reasons)[reason] += 1
            return [OrderUpdate(t, a.client_id, bad_status, reason)]

        reason = self._validate(a)
        if reason is None and a.client_id in self.resting:
            reason = "duplicate_client_id"
        # After a feed gap the book is stale/cleared: refuse to quote until it has resynchronised.
        if reason is None and self.awaiting_resync:
            reason = "awaiting_resync"
        if reason is None and not self.mkt.has_quote:
            reason = "no_market_data"
        if reason is None:
            reason = self._risk_check(a)
        if reason is not None:
            return reject(reason)

        # "Marketable" = would match immediately against the opposite side (a buy at or above the best
        # ask, a sell at or below the best bid, or a market order). That makes us a TAKER and we pay
        # the spread. POST-ONLY orders exist to guarantee we are a maker: if they would cross, the
        # exchange rejects them instead, so we never accidentally pay the taker fee.
        mkt = self.mkt
        if a.price is None:
            marketable = True
        elif a.side == BUY:
            marketable = a.price >= mkt.ask_px - 1e-9
        else:
            marketable = a.price <= mkt.bid_px + 1e-9
        if a.post_only and marketable:
            return reject("post_only_would_cross")

        self._next_oid += 1
        # order.qty is the order's TOTAL size (filled so far + remaining), so filled = qty - remaining.
        # A replace carries over the old order's filled part, and its root id for the fill ratio.
        total = a.qty + (old.qty - old.remaining if replacing else 0.0)
        order = Order(self._next_oid, a.client_id, a.side, a.price, round(total, 10), a.qty,
                      a.post_only, a.tif, t)
        if replacing:
            self._root[order.order_id] = self._root.get(old.order_id, old.order_id)
            self.stats["replaced"] += 1
            self.accepted_qty += order.qty - old.qty
        else:
            self.stats["accepted"] += 1
            self.accepted_qty += a.qty
        out: list[Fill | OrderUpdate] = [OrderUpdate(t, a.client_id, ok_status, "", a.qty)]
        if marketable:
            ref = mkt.mid
            for px, q in self.model.taker_executions(a.side, a.price, a.qty, mkt):
                f = self._execute(order, px, q, "taker", t, ref, "taker")
                if f is not None:
                    out.append(f)
            if order.remaining <= 1e-12:
                return out
        # A market order or an IOC NEVER rests (Binance cancels the unfilled part at once), whether or not
        # it was marketable: fill what we can as taker above, cancel the rest here.
        if a.price is None or a.tif == "IOC":
            self.stats["canceled"] += 1
            out.append(OrderUpdate(t, a.client_id, "canceled",
                                   "no_liquidity" if order.remaining >= a.qty - 1e-12 else "ioc_remainder",
                                   order.remaining))
            return out
        # The rest (or all) of the order now rests in the book. The fill model sets our PUBLIC queue
        # position; our own earlier orders at the same price are accounted for separately, on demand.
        self.model.on_new_resting(order, mkt)
        self.resting[order.client_id] = order
        return out

    def _replace(self, a: ReplaceOrder, t: int) -> list[Fill | OrderUpdate]:
        self.stats["replace_requests"] += 1

        def reject(reason: str, remaining: float = 0.0):
            self.stats["replace_rejected"] += 1
            self.replace_reject_reasons[reason] += 1
            return [OrderUpdate(t, a.client_id, "replace_rejected", reason, remaining)]

        old = self.resting.get(a.client_id)
        if old is None:
            return reject("unknown_order")
        price = old.price if a.price is None else a.price
        filled = round(old.qty - old.remaining, 10)
        if a.total_qty is not None:
            # Binance semantics: total_qty is the NEW TOTAL size, so what is left = total - already filled.
            # If fills we have not heard about already reach it, the order is CANCELLED and its remainder
            # leaves the book (Binance Modify Order docs; not checked on testnet).
            qty = round(a.total_qty - filled, 10)
            if qty < self.lot / 2:
                return self._cancel_many([old], t, "qty_below_filled")
        else:
            qty = old.remaining if a.qty is None else a.qty
        if qty <= 0 or not self._on_grid(qty, self.lot):
            return reject("qty_not_on_lot", old.remaining)
        if self.replace_keeps_priority and price == old.price and qty <= old.remaining + 1e-12:
            # Old rule (config): a same-price size cut keeps its place in the queue.
            self.stats["replaced"] += 1
            self.accepted_qty += qty - old.remaining
            old.remaining = qty
            old.qty = round(filled + qty, 10)
            return [OrderUpdate(t, a.client_id, "replaced", "kept_priority", qty)]
        # Binance re-queues every modify: cancel + re-place under the same client id, at the BACK of the
        # queue. If the new order is rejected (e.g. post-only would cross) the old one stays.
        del self.resting[a.client_id]
        out = self._place(PlaceOrder(a.client_id, old.side, qty, price, old.post_only, old.tif), t, old=old)
        if any(isinstance(u, OrderUpdate) and u.status == "replace_rejected" for u in out):
            self.resting[a.client_id] = old  # the amendment failed: old order stays
        return out

    def _execute(self, o: Order, price: float, qty: float, liquidity: str, t: int,
                 ref_mid: float | None, mechanism: str = "") -> Fill | None:
        # Cap by what is left, then round DOWN to whole lots (floor) so float noise cannot create
        # fills like 0.0499999 ETH. The 1e-6 nudge stops floor() from dropping a lot due to noise.
        qty = min(qty, o.remaining)
        qty = math.floor(qty / self.lot + 1e-6) * self.lot  # whole lots only
        qty = round(qty, 10)
        if qty <= 0:
            return None
        o.remaining = round(o.remaining - qty, 10)
        if o.remaining <= 1e-12:
            o.remaining = 0.0
            self.resting.pop(o.client_id, None)
        self._filled_orders.add(o.order_id)
        self._filled_roots.add(self._root.get(o.order_id, o.order_id))
        self.stats["fills_" + liquidity] += 1
        self.position += qty if o.side == BUY else -qty   # true position, for the risk check
        return Fill(t, o.order_id, o.client_id, o.side, price, qty, liquidity,
                    self.fees.fee(liquidity, price, qty), o.remaining, ref_mid, mechanism)

    # How many original orders got at least one fill (a set holds each id once). Used for fill ratio.
    @property
    def orders_with_fills(self) -> int:
        return len(self._filled_roots)

    # Same, but a replaced order counts once per incarnation (= per accepted action). Per-action ratio.
    @property
    def incarnations_with_fills(self) -> int:
        return len(self._filled_orders)
