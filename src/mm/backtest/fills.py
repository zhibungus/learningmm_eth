"""Fill models: decide when (and how much of) a resting order is executed.

This is the part of a market-making backtest that decides whether the results mean anything, so
every assumption is spelled out here and again in ``src/mm/backtest/README.md``.

Vocabulary
----------
* **Resting order** -- a limit order sitting in the book waiting for someone to trade with it.
* **Queue ahead** (``Order.q_ahead``) -- how much PUBLIC quantity (ETH) is in front of us at our
  price. Orders at one price fill first-come-first-served, so we only get filled after everything
  ahead of us has traded or been cancelled. Our OWN earlier orders at the same price are also ahead
  of us, but they are tracked separately (``_own_ahead``) because the public book never shows them.
* **Trade-through** -- a trade prints at a price strictly BETTER (for the aggressor) than our
  order, e.g. someone sells at 99.99 while we rest a bid at 100.00. Then everything at 100.00
  must have been consumed, including us. We are certainly filled, but only up to the size of that
  print (``cap_fills_by_volume``): a 0.001 ETH print cannot fill a 10 ETH order.
* **Trade at our price** -- a trade prints exactly at our price. We only get a share if it is
  larger than the queue ahead of us.
* **Quote cross** -- the public best bid/ask moves through our price with no print (data lag).
  A real book cannot stay crossed, so we were matched, but only up to the displayed quantity.

Every execution is tagged with its **mechanism**: ``queue`` (at-price, after the queue), ``through``,
``cross`` or ``taker``. Reports show the mix so you can see which assumption a result leans on.

Two models share one engine (``QueueFillModel``) and differ only in what they can *see*:

``QuoteTradeFillModel`` (config ``fill_model.type: quote_trade``)
    Data: trades + best bid/ask quotes (the free ``bookTicker`` archive, 2023-05..2024-03).
    * On arrival at the touch we start BEHIND the displayed quantity (x ``initial_queue_factor``).
    * Inside the spread (better than the touch) we are first in line (queue 0).
    * Deeper than the touch we cannot see the queue; we assume it is infinite, so we only fill on
      trade-through, until the touch reaches our price (then everything displayed is ahead).
    * Each trade at our price decrements the queue (before it can fill us).
    * When the displayed quantity at the touch drops by MORE than the trades we saw, the extra
      drop is a cancellation (see ``cancel_assumption``).

``L2FillModel`` (config ``fill_model.type: l2``)
    Data: trades + full depth updates (only from our own live recordings).
    * Same rules, but the queue at ANY level is known from the book, not just the touch.

``cancel_assumption`` -- who cancelled when the queue shrinks without trades?
    "proportional" (default): cancellations are spread evenly over the queue, so the share that
        was ahead of us is ``q_ahead / displayed_qty``.  Neutral.
    "all_behind": cancellations never help us (our queue only shrinks through trades).  Pessimistic.
    "all_ahead":  every cancellation was in front of us.  Optimistic.

``require_trade_through`` (conservative switch)
    If true, a trade AT our price never fills us and quote crosses are ignored, only a strict
    trade-through does. Pessimistic: real exchanges do fill us once the queue ahead is consumed.

``cross_fills: false`` turns off only the quote-cross mechanism (the "no_cross_fills" sensitivity).

Also modelled: taker executions that pay the spread (``taker_executions``), and fees
(``FeeSchedule``). Exchange-side rules (post-only rejection, tick/lot checks, risk limits) live in
``exchange.py``.

Known biases (details in the README)
    * Our own orders never change the public data (no market impact, no feedback).
    * A trade that prints "at our price" may have matched against orders that were *behind* us in
      reality if our queue estimate is wrong; queue position is an estimate, not a fact.
    * A quote-cross can fill us again on each later crossed quote (displayed size is not remembered).
    * Hidden / iceberg quantity and self-trade prevention are not modelled.
"""
# Makes type hints lazy text, so "float | None" and forward references work (see strategy.py).
from __future__ import annotations

# abc = "abstract base classes": a way to declare "every fill model MUST provide these methods".
import abc
import math
# @dataclass auto-writes __init__ etc. from the listed fields. "field" allows per-field options.
from dataclasses import dataclass, field
# Iterable = anything you can loop over with "for" (list, tuple, generator ...).
from typing import Iterable

from mm.data.book import OrderBook
from mm.data.events import QuoteEvent, TradeEvent

# Infinity. We use it for "queue ahead is unknown / unreachable": inf minus anything is still inf,
# so such an order can never be reached by trades at its price and only fills on a trade-through.
INF = math.inf


# ====================================================================== fees
# Exchange fees. Market makers "make" liquidity (resting orders) and usually pay a LOWER fee than
# takers (who cross the spread). Some venues even pay a rebate (negative fee). Fees are a fraction of
# notional (price * qty): 0.0002 = 2 basis points. At tiny spreads fees decide if a strategy is viable.
# frozen=True: immutable; slots=True: compact memory.
@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee rates as fractions of notional.  Negative = rebate.  Binance USD-M VIP0: 0.0002 / 0.0005."""
    maker: float = 0.0002
    taker: float = 0.0005

    def fee(self, liquidity: str, price: float, qty: float) -> float:
        # Conditional expression: pick the maker rate if we provided liquidity, else the taker rate.
        return price * qty * (self.maker if liquidity == "maker" else self.taker)


# ====================================================================== order + execution records
# Not frozen: the fill model updates q_ahead etc. on the same object as the market evolves.
# The queue fields are the heart of the simulation: where are we in line at our price level?
@dataclass(slots=True)
class Order:
    """An order resting on the simulated exchange."""
    order_id: int
    client_id: str
    side: str
    price: float | None
    qty: float
    remaining: float
    post_only: bool
    tif: str
    arrival_ns: int
    q_ahead: float = 0.0              # PUBLIC quantity in front of us at our price (INF = unknown/deep)
    level_qty: float | None = None    # displayed qty at our price at the last observation
    traded_since_obs: float = 0.0     # volume that traded at our price since that observation


# A fill model returns these ("order X trades qty Y at price Z because of <mechanism>"); the exchange
# then turns each into a Fill with fees. This separation keeps the model about WHEN we fill, not money.
@dataclass(slots=True)
class Execution:
    """A passive (maker) execution produced by a fill model."""
    order: Order
    price: float
    qty: float
    mechanism: str = "queue"          # "queue" | "through" | "cross"


# ====================================================================== the exchange's view of the market
# The exchange's own, undelayed picture of the market. Contrast with MarketState (strategy.py), which
# is the strategy's DELAYED picture. Keeping both separate is how we model latency honestly.
class MarketView:
    """The TRUE market (no latency) as the simulated exchange sees it."""

    def __init__(self) -> None:
        self.bid_px: float | None = None
        self.bid_qty: float | None = None
        self.ask_px: float | None = None
        self.ask_qty: float | None = None
        self.last_px: float | None = None
        self.book: OrderBook | None = None
        # Liquidity our own taker orders already used since the last public update, keyed by
        # ("bid"|"ask", price). Without this a second taker order could "use" the same displayed size
        # again, because the public data never shows our consumption.
        self.consumed: dict[tuple[str, float], float] = {}

    @property
    def mid(self) -> float | None:
        if self.bid_px is not None and self.ask_px is not None:
            return 0.5 * (self.bid_px + self.ask_px)
        return self.last_px

    @property
    def has_quote(self) -> bool:
        return self.bid_px is not None and self.ask_px is not None

    def set_quote(self, ev: QuoteEvent) -> None:
        self.bid_px, self.bid_qty, self.ask_px, self.ask_qty = ev.bid_px, ev.bid_qty, ev.ask_px, ev.ask_qty
        self.consumed.clear()   # a fresh public quote replaces whatever we consumed

    def set_from_book(self) -> None:
        # Copy best bid/ask out of the L2 book. "b if b else (None, None)": if the side is empty use
        # (None, None); assigning a pair to two names is "tuple unpacking".
        b, a = self.book.best_bid, self.book.best_ask
        self.bid_px, self.bid_qty = b if b else (None, None)
        self.ask_px, self.ask_qty = a if a else (None, None)

    def clear(self) -> None:
        """Forget all public prices (feed gap): until a fresh snapshot/quote arrives there is no market."""
        self.bid_px = self.bid_qty = self.ask_px = self.ask_qty = None
        self.last_px = None
        if self.book is not None:
            self.book.clear()
        self.consumed.clear()


# ====================================================================== base model
# abc.ABC + @abc.abstractmethod: a class with abstract methods cannot be instantiated directly; a
# subclass must implement them all. This is the contract between SimExchange and any fill model.
class FillModel(abc.ABC):
    """Interface used by ``SimExchange``."""

    #: event kinds this model reads (the exchange feeds it nothing else)
    consumes: tuple[str, ...] = ("trade",)

    @abc.abstractmethod
    def apply_event(self, ev, mkt: MarketView, orders: Iterable[Order]) -> list[Execution]:
        """Update ``mkt`` with the event, update queue estimates, return maker executions."""

    @abc.abstractmethod
    def on_new_resting(self, order: Order, mkt: MarketView) -> None:
        """Set ``order.q_ahead`` (the PUBLIC queue ahead) for an order that just started resting."""

    @abc.abstractmethod
    def taker_executions(self, side: str, limit_px: float | None, qty: float,
                         mkt: MarketView) -> list[tuple[float, float]]:
        """Executions [(price, qty), ...] for an aggressive order (walks the available liquidity)."""

    def reset_episodes(self) -> None:
        """Hook: forget any per-episode memory after a feed gap (default: nothing to forget)."""


# Shared machinery. Orders at one price fill first-come-first-served (price-time priority), so the key
# question is "how much volume is ahead of me?" (queue position). It shrinks when trades hit that price
# or when people ahead cancel. We cannot see individual orders, so we estimate it.
class QueueFillModel(FillModel):
    """Queue-position machinery shared by the quote+trade and the L2 models."""

    def __init__(self, tick_size: float, require_trade_through: bool = False,
                 cancel_assumption: str = "proportional", initial_queue_factor: float = 1.0,
                 sweep_penalty_ticks: int = 1, sweep_max_tiers: int = 5,
                 cap_fills_by_volume: bool = True, cross_fills: bool = True) -> None:
        if cancel_assumption not in ("proportional", "all_behind", "all_ahead"):
            raise ValueError(f"unknown cancel_assumption {cancel_assumption!r}")
        if initial_queue_factor < 0:
            raise ValueError("initial_queue_factor must be >= 0")
        # Store settings on the object. _inv = 1/tick lets us convert prices to whole-tick integers
        # with one multiplication (floats like 100.01 are inexact, so comparing prices with == is risky).
        self.tick = tick_size
        self._inv = 1.0 / tick_size
        self.require_trade_through = require_trade_through
        self.cancel_assumption = cancel_assumption
        self.factor = initial_queue_factor
        self.sweep_penalty_ticks = sweep_penalty_ticks
        self.sweep_max_tiers = sweep_max_tiers
        self.cap = cap_fills_by_volume
        self.cross_fills = cross_fills
        # per side: None (not crossing) or {price level: (last displayed size, size still available to fill us)}
        self._episode: dict[str, dict | None] = {"buy": None, "sell": None}

    def _k(self, px: float) -> int:
        """Price as an integer number of ticks (avoids float equality problems)."""
        return round(px * self._inv)

    # ------------------------------------------------------------------ our own queue
    def _own_ahead(self, o: Order, orders: Iterable[Order]) -> float:
        """Remaining size of OUR OTHER earlier orders at the same side and price.

        The public book never contains our simulated orders, so they must be added on top of the public
        queue (``q_ahead``). Computing it on demand (rather than storing it) keeps it right when an
        earlier order of ours fills or is cancelled. ``sum(<generator>)`` adds without building a list."""
        ko = self._k(o.price)
        return sum(x.remaining for x in orders
                   if x is not o and x.side == o.side and x.order_id < o.order_id and self._k(x.price) == ko)

    # ------------------------------------------------------------------ trades
    def _on_trade(self, ev: TradeEvent, orders: Iterable[Order]) -> list[Execution]:
        # A trade print is an aggressor (taker) hitting resting orders on the OTHER side: a "sell"
        # aggressor hits bids, a "buy" aggressor lifts asks. For each of our orders compare prices in ticks.
        # "through" = the trade printed at a price worse for the resting side than ours, so our level
        # must have been fully consumed (a sure fill, but only up to the print size when capped).
        # "at_price" = same price: only a share of the trade reaches us, after the queue ahead is eaten.
        out: list[Execution] = []
        kp = self._k(ev.price)
        pool = ev.qty                      # print volume still available to hand out to through-fills
        orders = sorted(orders, key=lambda x: x.order_id)   # oldest order first (time priority)
        for o in orders:
            ko = self._k(o.price)
            if o.side == "buy":
                if ev.side != "sell":
                    continue                      # a buy aggressor lifts asks, it cannot hit our bid
                through = kp < ko
            else:
                if ev.side != "buy":
                    continue
                through = kp > ko
            at_price = kp == ko
            if through:
                if self.cap:
                    q = min(o.remaining, pool)    # a tiny print cannot fill a big order
                    pool -= q
                else:
                    q = o.remaining               # legacy behaviour: the whole order fills
                if q > 0:
                    out.append(Execution(o, o.price, q, "through"))
            elif at_price:
                o.traded_since_obs += ev.qty
                excess = ev.qty - o.q_ahead       # volume left after the PUBLIC queue ahead of us
                if excess <= 0:                   # the trade only eats the public queue ahead of us
                    o.q_ahead -= ev.qty
                else:                             # public queue exhausted
                    o.q_ahead = 0.0
                    reach = excess - self._own_ahead(o, orders)   # our earlier orders are served first
                    if reach > 0 and not self.require_trade_through:
                        out.append(Execution(o, o.price, min(o.remaining, reach), "queue"))
        return out

    # ------------------------------------------------------------------ quote/book crossing us
    def _cross_fills(self, orders: Iterable[Order], bid_px, bid_qty, ask_px, ask_qty) -> tuple[list[Execution], set]:
        """Orders the public book now trades through (without a trade print, e.g. data lag).

        Example: our bid is 100.00 and the best ask is now 99.99. A real exchange would already have
        matched us (a book can never stay crossed), so we count it as a fill, but only up to the size
        displayed on the crossing side (shared between our orders). Returns (executions, ids of all
        crossed orders) so the caller does not also treat those orders as merely "observed".
        """
        out: list[Execution] = []
        hit: set = set()
        if self.require_trade_through or not self.cross_fills:
            return out, hit
        ordered = sorted(orders, key=lambda x: x.order_id)
        # Which of our orders are crossed on each side (buy orders are crossed by the ask, sell by the bid)?
        crossed_ids = {"buy": [], "sell": []}
        for o in ordered:
            if o.side == "buy":
                crossed = ask_px is not None and self._k(ask_px) <= self._k(o.price)
            else:
                crossed = bid_px is not None and self._k(bid_px) >= self._k(o.price)
            if crossed:
                crossed_ids[o.side].append(o)
                hit.add(o.order_id)
        # CROSSING EPISODE memory (R2). The same displayed size can show up in many consecutive quotes;
        # without memory every quote would refill the order from a "fresh" pool and a 2 ETH bid could be
        # filled by a 0.5 ETH ask flickering between 0.5 and 0.4. Per side we keep (level, last displayed
        # size, size still available to us): the available size only GROWS when the displayed size grows,
        # shrinks with the display, and the whole memory is dropped when the quote stops crossing us.
        levels = {"buy": (ask_px, ask_qty or 0.0), "sell": (bid_px, bid_qty or 0.0)}
        for side in ("buy", "sell"):
            if not crossed_ids[side]:
                self._episode[side] = None                    # no longer crossing: forget
                continue
            px, disp = levels[side]
            # The memory is kept PER PRICE LEVEL for the whole episode (not just the current level): if the
            # crossing ask flips 99.99 -> 99.98 -> 99.99, the size we already took at 99.99 must not become
            # available again just because the quote came back to it (the validator's two-level probe).
            ep = self._episode[side]
            if ep is None:
                ep = self._episode[side] = {}
            if px not in ep:                                  # first time this level crosses us in the episode
                avail = disp
            else:
                prev_disp, avail = ep[px]
                avail = avail + (disp - prev_disp) if disp > prev_disp else min(avail, disp)
            for o in crossed_ids[side]:
                if self.cap:
                    q = min(o.remaining, avail)
                    avail -= q
                else:
                    q = o.remaining
                if q > 0:
                    out.append(Execution(o, o.price, q, "cross"))
            ep[px] = (disp, avail)
        return out, hit

    def reset_episodes(self) -> None:
        """Forget crossing-episode memory (called on a feed gap, when old displayed sizes mean nothing)."""
        self._episode = {"buy": None, "sell": None}

    # ------------------------------------------------------------------ displayed quantity changed
    def _observe(self, o: Order, new_qty: float) -> None:
        """We now see ``new_qty`` displayed at our price.  Update the queue estimate."""
        # Idea: displayed size at our price changed from `old` to `new_qty`. A DROP is either trades
        # (already subtracted from our queue when the trade event came) or cancellations (unseen).
        # We only treat the unexplained part as cancellations and assign a share of it to "ahead of us"
        # per cancel_assumption. Finally the PUBLIC queue ahead can never exceed what is displayed
        # (our own orders are not displayed and are added separately by _own_ahead).
        old = o.level_qty
        cap = new_qty * self.factor
        if old is None:                           # first look at this level since we joined/lost it
            o.q_ahead = min(o.q_ahead, cap)
        else:
            if new_qty < old:
                cancelled = max(0.0, (old - new_qty) - o.traded_since_obs)  # drop not explained by trades
                if cancelled > 0 and o.q_ahead > 0:
                    if self.cancel_assumption == "proportional":
                        share = min(1.0, o.q_ahead / old) if old > 0 else 0.0
                        o.q_ahead -= cancelled * share
                    elif self.cancel_assumption == "all_ahead":
                        o.q_ahead -= min(cancelled, o.q_ahead)
                    # "all_behind": nothing changes for us
            o.q_ahead = max(0.0, min(o.q_ahead, cap))
        o.level_qty = new_qty
        o.traded_since_obs = 0.0

    def _unknown_level(self, o: Order, queue: float) -> None:
        o.level_qty = None
        o.traded_since_obs = 0.0
        o.q_ahead = queue


# Shared by both models' taker_executions: walk price levels from best to worst and take what is
# displayed there, minus what our earlier takers already used (mkt.consumed). Stops when the order is
# filled or the next level is worse than the limit price. Returns [(price, qty), ...].
def _take_liquidity(levels: Iterable[tuple[float, float]], side: str, limit_px: float | None,
                    qty: float, mkt: MarketView) -> list[tuple[float, float]]:
    book_side = "ask" if side == "buy" else "bid"     # the side of the book we hit
    sgn = 1 if side == "buy" else -1
    out: list[tuple[float, float]] = []
    left = qty
    for px, size in levels:
        if limit_px is not None and sgn * (px - limit_px) > 1e-9:
            break
        avail = size - mkt.consumed.get((book_side, px), 0.0)
        take = min(left, max(0.0, avail))
        if take > 0:
            out.append((px, take))
            mkt.consumed[(book_side, px)] = mkt.consumed.get((book_side, px), 0.0) + take
            left -= take
        if left <= 1e-12:
            break
    return out


class QuoteTradeFillModel(QueueFillModel):
    """Trades + top-of-book quotes.  See the module docstring."""

    consumes = ("trade", "quote")

    def apply_event(self, ev, mkt: MarketView, orders: Iterable[Order]) -> list[Execution]:
        if ev.kind == "trade":
            mkt.last_px = ev.price
            return self._on_trade(ev, orders)
        # Quote update: refresh the best bid/ask, then for each resting order decide whether it was
        # crossed (fill) or what it can learn about its queue from the displayed size at the touch.
        mkt.set_quote(ev)
        out, hit = self._cross_fills(orders, ev.bid_px, ev.bid_qty, ev.ask_px, ev.ask_qty)
        for o in orders:
            if o.order_id in hit:
                continue
            # "touch" = best price on our side. rel > 0: we are priced better than the touch (inside
            # the spread, first in line); rel == 0: at the touch; rel < 0: deeper (queue invisible).
            touch = ev.bid_px if o.side == "buy" else ev.ask_px
            touch_qty = ev.bid_qty if o.side == "buy" else ev.ask_qty
            rel = self._k(o.price) - self._k(touch)
            if o.side == "sell":
                rel = -rel                        # rel > 0: we are more aggressive than the touch
            if rel == 0:
                self._observe(o, touch_qty)
            elif rel > 0:                         # inside the spread: nobody PUBLIC is ahead of us
                self._unknown_level(o, 0.0)
            else:                                 # deeper than the touch: queue not visible
                o.level_qty = None
        return out

    def on_new_resting(self, order: Order, mkt: MarketView) -> None:
        if order.side == "buy":
            touch, tq = mkt.bid_px, mkt.bid_qty
        else:
            touch, tq = mkt.ask_px, mkt.ask_qty
        if touch is None:
            self._unknown_level(order, INF)
            return
        rel = self._k(order.price) - self._k(touch)
        if order.side == "sell":
            rel = -rel
        if rel == 0:
            order.level_qty = tq
            order.traded_since_obs = 0.0
            order.q_ahead = tq * self.factor
        elif rel > 0:
            self._unknown_level(order, 0.0)
        else:
            self._unknown_level(order, INF)

    def taker_executions(self, side: str, limit_px: float | None, qty: float,
                         mkt: MarketView) -> list[tuple[float, float]]:
        """Top-of-book only. We know the displayed size at the touch; beyond it we assume liquidity
        comes in ``sweep_max_tiers`` further tiers, each as big as the touch and each
        ``sweep_penalty_ticks`` worse than the previous one. So a bigger order pays a growing price
        (and beyond the last tier gets nothing: the rest is cancelled). Size we already used since the
        last quote is remembered in ``mkt.consumed`` so repeated takers cannot reuse the same liquidity."""
        if not mkt.has_quote:
            return []
        px, size0 = (mkt.ask_px, mkt.ask_qty) if side == "buy" else (mkt.bid_px, mkt.bid_qty)
        # sgn turns buy/sell into +1/-1 so one formula handles both sides ("worse price" = further
        # up for a buyer, further down for a seller). Crossing the spread means paying the other side's price.
        sgn = 1 if side == "buy" else -1
        # Synthetic levels: tier 0 is the touch, each further tier is one penalty step worse.
        # (A generator expression: produces the levels lazily, one per loop step.)
        levels = ((round(px + sgn * tier * self.sweep_penalty_ticks * self.tick, 10), size0)
                  for tier in range(self.sweep_max_tiers + 1))
        return _take_liquidity(levels, side, limit_px, qty, mkt)


class L2FillModel(QueueFillModel):
    """Trades + full depth.  See the module docstring."""

    consumes = ("trade", "depth")

    def apply_event(self, ev, mkt: MarketView, orders: Iterable[Order]) -> list[Execution]:
        if ev.kind == "trade":
            mkt.last_px = ev.price
            return self._on_trade(ev, orders)
        # Apply the depth update to our copy of the book first, then check each resting order.
        # {px for px, _ in ev.bids} is a "set comprehension": the set of prices that changed in this
        # message (the "_" ignores the quantity). Sets make "is this price in it?" very fast.
        if mkt.book is None:
            mkt.book = OrderBook()
        mkt.book.apply(ev)
        mkt.set_from_book()
        mkt.consumed.clear()
        snap = ev.is_snapshot
        changed_b = {px for px, _ in ev.bids}
        changed_a = {px for px, _ in ev.asks}
        book = mkt.book
        out, hit = self._cross_fills(orders, mkt.bid_px, mkt.bid_qty, mkt.ask_px, mkt.ask_qty)
        for o in orders:
            if o.order_id in hit:
                continue
            if o.side == "buy":
                if snap or o.price in changed_b:
                    self._observe(o, book.bids.get(o.price, 0.0))
            elif snap or o.price in changed_a:
                self._observe(o, book.asks.get(o.price, 0.0))
        return out

    def on_new_resting(self, order: Order, mkt: MarketView) -> None:
        if mkt.book is None:
            self._unknown_level(order, INF)
            return
        side = mkt.book.bids if order.side == "buy" else mkt.book.asks
        q = side.get(order.price, 0.0)
        order.level_qty = q
        order.traded_since_obs = 0.0
        order.q_ahead = q * self.factor

    def taker_executions(self, side: str, limit_px: float | None, qty: float,
                         mkt: MarketView) -> list[tuple[float, float]]:
        """Walk the real book level by level. Size we already took since the last depth update is
        remembered (``mkt.consumed``), so a second taker order sees a thinner book (no market impact
        beyond that on later data)."""
        if mkt.book is None:
            return []
        # A buyer consumes asks from the lowest price up; a seller consumes bids from the highest down.
        # .items() gives (price, qty) pairs; sorted() orders them. We stop when the order is filled or
        # the next level is beyond our limit price.
        levels = sorted(mkt.book.asks.items()) if side == "buy" else sorted(mkt.book.bids.items(), reverse=True)
        return _take_liquidity(levels, side, limit_px, qty, mkt)


def make_fill_model(cfg: dict, tick_size: float) -> FillModel:
    """Build the model chosen in ``cfg["fill_model"]``."""
    # Factory: turn the config dict into the right model object. dict(cfg) copies so pop() does not
    # alter the caller's config. "**common" below unpacks a dict into keyword arguments:
    # f(**{"a": 1}) is the same as f(a=1).
    fm = dict(cfg)
    typ = fm.pop("type")
    common = dict(require_trade_through=fm.get("require_trade_through", False),
                  cancel_assumption=fm.get("cancel_assumption", "proportional"),
                  initial_queue_factor=fm.get("initial_queue_factor", 1.0),
                  cap_fills_by_volume=fm.get("cap_fills_by_volume", True),
                  cross_fills=fm.get("cross_fills", True))
    if typ == "quote_trade":
        return QuoteTradeFillModel(tick_size, sweep_penalty_ticks=fm.get("sweep_penalty_ticks", 1),
                                   sweep_max_tiers=fm.get("sweep_max_tiers", 5), **common)
    if typ == "l2":
        return L2FillModel(tick_size, **common)
    raise ValueError(f"unknown fill_model.type {typ!r}; choose 'quote_trade' or 'l2'")
