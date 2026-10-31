"""The Strategy interface: the ONLY thing a strategy needs to know about the backtester.

The same ``Strategy`` object runs unchanged in the backtest (``mm.backtest.run``) and in the paper
trader (``mm.paper.run``).  A strategy never talks to an exchange or a clock directly.  The engine
calls its callbacks, and the strategy answers with a list of *actions* (place / cancel / replace).

Plain-language picture
----------------------
    market data -> [feed latency] -> your callback -> actions -> [order latency] -> exchange
    exchange fill -> [report latency] -> your on_fill

* Everything you see is **delayed** by the configured latencies, exactly like a real system.
* ``MarketState`` is what you know *now*.  It is updated by the events you subscribed to.
* ``state.position`` / ``state.cash`` are what the exchange has *reported to you* so far, so right
  after a fill happens they are stale by the report latency (just like live).
* Orders are identified by a ``client_id`` string that YOU choose (unique among live orders).
  ``Strategy.new_id()`` gives you a counter-based one.

Units (project-wide): prices in quote currency (USDT), sizes in base units (ETH), times are
integer nanoseconds UTC.  Sides are the strings ``"buy"`` and ``"sell"``.

A minimal strategy::

    class Quoter(Strategy):
        event_kinds = ("quote",)                       # which events I want (and build state from)
        def on_event(self, ev, state):
            if state.mid is None or "bid" in state.open_orders:
                return []
            px = state.round_price(state.mid - 0.05, "down")
            return [PlaceOrder("bid", "buy", 0.01, px, post_only=True)]
"""
# "from __future__ import annotations" makes type hints (the ": int" / "-> str" parts) be stored as
# text and not evaluated at import time. This lets us write "float | None" ("a float or None") and
# refer to classes defined further down the file.
from __future__ import annotations

# dataclass: a decorator (a function written as @name above a class that modifies the class). It
# auto-writes __init__, __repr__ and __eq__ from the annotated fields, so we only list the fields.
from dataclasses import dataclass
# Any = "no type restriction"; Sequence = "a list or tuple (something you can index and loop over)".
from typing import Any, Sequence

# numpy: fast arrays and random numbers. Imported "as np" by universal convention.
import numpy as np

from mm.data.book import OrderBook
from mm.data.events import Event

# Side names are plain strings used project-wide. "buy" = we purchase (our bid fills),
# "sell" = we sell (our ask fills). Constants avoid typos like "Buy" vs "buy".
BUY = "buy"
SELL = "sell"


# ====================================================================== actions (strategy -> exchange)
# Actions are plain "message" objects. The strategy never calls the exchange; it returns these and the
# engine delivers them after the order latency. Why? Because in real trading your order takes time to
# reach the exchange, and the market can move (or fill you) meanwhile; the backtest must feel that too.
#
# @dataclass(frozen=True, slots=True):
#   frozen=True -> instances cannot be modified after creation (safe to share, like a sent message);
#   slots=True  -> uses less memory and is faster (matters with millions of objects).
# A field written "price: float | None = None" means: optional, defaults to None.
@dataclass(frozen=True, slots=True)
class PlaceOrder:
    """Send a new order.

    ``price=None`` means a market order.  ``post_only=True`` (Binance "GTX"): the exchange REJECTS
    the order if it would execute immediately as a taker, instead of paying the spread.
    ``tif``: "GTC" (rest until cancelled) or "IOC" (execute what you can now, cancel the rest).
    """
    client_id: str
    side: str
    qty: float
    price: float | None = None
    post_only: bool = False
    tif: str = "GTC"


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Ask the exchange to cancel a live order.  NOT instant: it can still fill before the request
    arrives (that is the whole point of modelling order latency)."""
    client_id: str


@dataclass(frozen=True, slots=True)
class ReplaceOrder:
    """Amend a live order (one atomic request, like Binance order modify).

    Like Binance, every replace puts the order at the back of the queue (config
    ``exchange.replace_keeps_priority: true`` restores the old rule: a same-price size cut keeps its
    place).  If the new order would be rejected (e.g. post-only would cross) the OLD order stays live
    and you get a ``replace_rejected`` update.

    Two ways to say the new size (give at most one, else ValueError):
      * ``qty``       the new REMAINING size.  If a fill you have not heard about yet arrives first,
                      the order ends up bigger in total than you intended.
      * ``total_qty`` the new TOTAL (original) size, like Binance's modify ``quantity``.  The exchange
                      uses total_qty minus what is already filled, so a fill still in flight cannot push
                      your inventory past the limit.  If total_qty is not above the filled amount the
                      order is CANCELLED (``OrderUpdate`` status "canceled", reason ``qty_below_filled``), as in the Binance docs.
    """
    client_id: str
    price: float | None = None  # None = keep price
    qty: float | None = None    # None = keep the remaining qty
    total_qty: float | None = None

    # Runs right after the dataclass __init__; used here only to refuse the ambiguous combination.
    def __post_init__(self) -> None:
        if self.qty is not None and self.total_qty is not None:
            raise ValueError("ReplaceOrder: give qty (new remaining) or total_qty (new total), not both")


@dataclass(frozen=True, slots=True)
class CancelAll:
    """Cancel every live order (optionally only one side)."""
    side: str | None = None


# Type aliases: "A | B" means "either A or B". These names are only for readability in type hints.
Action = PlaceOrder | CancelOrder | ReplaceOrder | CancelAll
Actions = Sequence[Action] | None


# ====================================================================== reports (exchange -> strategy)
# These are the messages coming BACK: fills (our order traded) and order status news. They reach the
# strategy after a delay, just like exchange execution reports over a real network.
@dataclass(frozen=True, slots=True)
class Fill:
    """An execution of one of your orders.  ``ts_ns`` is the exchange time of the fill.

    ``liquidity``: "maker" (your resting order was hit) or "taker" (you crossed the spread).
    ``fee`` is in quote currency (positive = you paid, negative = rebate).
    ``leaves_qty`` is what is still open on that order after this fill (0 = fully filled).
    ``ref_mid`` is the market mid just before the event that caused the fill (used for accounting) and
    ``mechanism`` is WHY the simulator filled us ("queue", "through", "cross" or "taker"). Both are
    simulator internals that a real exchange never sends, so the engine blanks them (None / "") in the
    copy it hands to the strategy; only the books and the report tables see them.
    """
    ts_ns: int
    order_id: int
    client_id: str
    side: str
    price: float
    qty: float
    liquidity: str
    fee: float
    leaves_qty: float
    ref_mid: float | None = None
    mechanism: str = ""


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Order lifecycle news.  ``status``:

    accepted | rejected | canceled | cancel_rejected | replaced | replace_rejected
    (a completely filled order is announced by the last ``Fill`` having ``leaves_qty == 0``).
    ``reason`` explains rejections, e.g. "post_only_would_cross", "price_not_on_tick",
    "qty_below_min", "qty_below_filled", "unknown_order", "duplicate_client_id", "ioc_remainder", "connection_lost".
    """
    ts_ns: int
    client_id: str
    status: str
    reason: str = ""
    remaining: float = 0.0


# No frozen=True here: the engine updates this record in place as the order's status changes.
@dataclass(slots=True)
class OrderView:
    """The strategy's own view of one of its orders (``state.open_orders[client_id]``)."""
    client_id: str
    side: str
    price: float | None
    qty: float
    remaining: float
    status: str  # "pending_new" | "open" | "pending_cancel"


# ====================================================================== market state
class MarketState:
    """What the strategy knows at the moment of a callback.  Read it, never write it.

    Market fields are only filled for event kinds in ``Strategy.event_kinds``.
    """

    # __init__ runs when you create the object (MarketState(0.01, 0.001)). "self" is the object being
    # built; "-> None" says it returns nothing. Everything below starts empty because no data has
    # arrived yet. tick_size = smallest allowed price step; lot_size = smallest allowed quantity step.
    def __init__(self, tick_size: float, lot_size: float) -> None:
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.now_ns: int = 0
        # best bid / ask as the STRATEGY sees them (already delayed by feed latency)
        self.bid_px: float | None = None
        self.bid_qty: float | None = None
        self.ask_px: float | None = None
        self.ask_qty: float | None = None
        self.last_trade_px: float | None = None
        self.last_trade_side: str | None = None
        self.last_funding_rate: float | None = None
        self.book: OrderBook | None = None  # L2 book if DepthEvents are subscribed, else None
        # account as REPORTED to the strategy (delayed by report latency)
        self.position: float = 0.0
        self.cash: float = 0.0
        self.open_orders: dict[str, OrderView] = {}
        self._has_quotes = False
        # GapEvent.stream values that only concern the trades feed (set by build_engine from config).
        self.trade_gap_streams: frozenset = frozenset({"market"})

    # ---- derived
    # @property makes a method readable like an attribute: write state.mid, not state.mid().
    # Mid price = halfway between best bid and best ask; the usual "fair price" proxy in market making.
    @property
    def mid(self) -> float | None:
        """Mid of best bid/ask; falls back to the last trade price when no quote is known."""
        if self.bid_px is not None and self.ask_px is not None:
            return 0.5 * (self.bid_px + self.ask_px)
        return self.last_trade_px

    @property
    def spread(self) -> float | None:
        if self.bid_px is None or self.ask_px is None:
            return None
        return self.ask_px - self.bid_px

    @property
    def equity(self) -> float | None:
        m = self.mid
        return None if m is None else self.cash + self.position * m

    # ---- helpers so strategies never submit off-tick prices
    def round_price(self, px: float, mode: str = "nearest") -> float:
        """Round to the tick grid.  mode: "nearest" | "down" | "up"."""
        # Work in whole ticks: n is "how many ticks" the price is. A dict maps the mode name to the
        # rounded tick count (the 1e-9 nudges guard against float error like 99.99999999 ticks).
        # Then multiply back and round to 10 decimals to remove float noise (0.1+0.2 != 0.3 problem).
        n = px / self.tick_size
        k = {"nearest": round(n), "down": np.floor(n + 1e-9), "up": np.ceil(n - 1e-9)}[mode]
        return round(float(k) * self.tick_size, 10)

    def round_qty(self, qty: float) -> float:
        """Round DOWN to the lot size."""
        return round(float(np.floor(qty / self.lot_size + 1e-9)) * self.lot_size, 10)

    # ---- engine-side update (called by the engine, not by strategies)
    # A leading underscore (_apply) is Python's "internal, don't call this from outside" convention.
    # This folds one market event into the strategy's picture of the market.
    def _apply(self, ev: Event) -> None:
        k = ev.kind
        if k == "quote":
            self.bid_px, self.bid_qty, self.ask_px, self.ask_qty = ev.bid_px, ev.bid_qty, ev.ask_px, ev.ask_qty
            self._has_quotes = True
        elif k == "trade":
            self.last_trade_px, self.last_trade_side = ev.price, ev.side
        elif k == "depth":
            if self.book is None:
                self.book = OrderBook()
            self.book.apply(ev)
            if not self._has_quotes:  # a quote stream (if subscribed) wins over the book for the BBO
                # best_bid is (price, qty) or None. "b if b else (None, None)" is a one-line
                # if/else; assigning a 2-tuple to two names at once is called tuple unpacking.
                b, a = self.book.best_bid, self.book.best_ask
                self.bid_px, self.bid_qty = (b if b else (None, None))
                self.ask_px, self.ask_qty = (a if a else (None, None))
        elif k == "funding":
            self.last_funding_rate = ev.rate
            # Funding is really paid in cash by the exchange. Mirror it in the strategy's cash using the
            # position the strategy currently believes it has (it may lag the true one by the report
            # latency, so this is an approximation).
            px = ev.mark_price if ev.mark_price is not None else self.mid
            if px is not None:
                self.cash -= self.position * px * ev.rate
        elif k == "gap":
            # The feed lost messages. A gap on the TRADES socket (stream in trade_gap_streams) only
            # invalidates the last trade price. Any other gap (book/quote socket, or unknown stream) means
            # the book and quotes may be wrong, so forget them; the strategy sees no prices (mid is None)
            # until fresh data arrives after the resync.
            self.last_trade_px = None
            if getattr(ev, "stream", None) not in self.trade_gap_streams:
                self.bid_px = self.bid_qty = self.ask_px = self.ask_qty = None
                self.book = None
                self._has_quotes = False

# ====================================================================== the interface
# A "base class": real strategies write "class MyStrategy(Strategy):" and override only the methods
# they need (inheritance). Every method here does nothing by default, so a strategy that ignores
# fills simply does not define on_fill. (An ABC/Protocol would force every method; we chose a plain
# base class because most strategies only need two or three callbacks.)
class Strategy:
    """Base class for every strategy.  Override the callbacks you need; all default to "do nothing".

    The engine builds it as ``cls(params=<dict from config>, rng=<seeded numpy Generator>)``.
    Use ONLY ``self.rng`` for randomness so that runs are reproducible from the seed.
    Every callback may return a list of actions (or ``None``).

    Class attributes
        name         short identifier written into reports
        event_kinds  which market events to deliver to ``on_event`` and to build ``state`` from:
                     any subset of ("quote", "trade", "depth", "funding").  Subscribing to fewer
                     kinds makes backtests faster.
    """

    name: str = "strategy"
    event_kinds: tuple[str, ...] = ("quote", "trade", "depth", "funding")

    def __init__(self, params: dict[str, Any] | None = None, rng: np.random.Generator | None = None) -> None:
        # "params or {}" uses an empty dict if params is None; dict(...) makes our own copy so the
        # caller's config cannot be changed by accident.
        # rng = random number generator. Seeded by the engine so every run is repeatable.
        self.params = dict(params or {})
        self.rng = rng if rng is not None else np.random.default_rng(0)
        self._id_counter = 0

    def new_id(self, prefix: str = "o") -> str:
        """A unique client order id: 'o1', 'o2', ..."""
        self._id_counter += 1
        return f"{prefix}{self._id_counter}"

    # ---- callbacks -------------------------------------------------------------------------
    def on_start(self, state: MarketState) -> Actions:
        """Called once, before the first event is delivered."""
        return None

    def on_event(self, ev: Event, state: MarketState) -> Actions:
        """A market event reached you (``state`` already includes it).  ``ev.ts_ns`` is the EXCHANGE
        time; ``state.now_ns`` is your (later) local time."""
        return None

    def on_gap(self, ev, state: MarketState) -> Actions:
        """The market-data feed lost messages (``ev.reason``). The engine has already cancelled all your
        resting orders, cleared ``state``'s prices, and will REJECT new orders until the book has been
        re-synchronised. Override to reset any internal model state that depended on the old book."""
        return None

    def on_fill(self, fill: Fill, state: MarketState) -> Actions:
        """One of your orders traded (the report reached you; ``state.position`` already updated)."""
        return None

    def on_order_update(self, upd: OrderUpdate, state: MarketState) -> Actions:
        """Accepted / rejected / canceled ... news about an order."""
        return None

    def on_timer(self, now_ns: int, state: MarketState) -> Actions:
        """Periodic wake-up every ``engine.timer_interval_ms`` (if configured)."""
        return None

    def on_stop(self, state: MarketState) -> None:
        """End of data / shutdown.  Nothing can be sent any more."""
        return None
