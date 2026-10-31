"""In-memory event objects shared by the loaders (replay), the live feed and the backtester.

Every event has ``ts_ns`` (exchange matching-engine time, ns UTC), ``event_ts_ns`` and
``recv_ts_ns`` (see ``schemas.py``; the latter two may be ``None``) and a class attribute ``kind``.
Events are immutable and slotted so millions of them are cheap.
"""
# Postpones evaluation of type hints (see config.py for the explanation).
from __future__ import annotations

# A "dataclass" is a class whose __init__, __repr__ and __eq__ are generated automatically from
# the annotated fields below, so we don't write boilerplate.
from dataclasses import dataclass

import polars as pl  # DataFrame library used for the parquet-friendly conversions at the bottom

from . import schemas as S  # `.` means "from this same package"

# A type alias: a Level is a (price, quantity) pair, i.e. one rung of the order book ladder.
Level = tuple[float, float]


# `@dataclass(...)` is a DECORATOR: a function applied to the class right below it to modify it.
#   frozen=True -> fields cannot be changed after creation (events are facts; they must not mutate,
#                  and frozen objects are also safe to share between parts of the program)
#   slots=True  -> store fields in a compact fixed layout instead of a per-object dict, saving
#                  memory; important when a replay creates millions of events.
@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A public trade print (aggregated trade). ``side`` is the aggressor: "buy" or "sell".

    The AGGRESSOR is the order that crossed the spread and took liquidity (a market order, or a
    limit order priced through the book). "buy" means a buyer lifted a resting ask; "sell" means a
    seller hit a resting bid. The resting order is the "maker". Market makers are makers.
    """
    # `name: type` lines declare the fields. `int | None` means "an int, or None if unknown".
    ts_ns: int
    event_ts_ns: int | None
    recv_ts_ns: int | None
    trade_id: int
    first_id: int
    last_id: int
    price: float
    qty: float
    side: str
    # No annotation -> this is a plain class attribute (shared label), not a dataclass field.
    kind = "trade"


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    """New best bid / best ask."""
    ts_ns: int
    event_ts_ns: int | None
    recv_ts_ns: int | None
    update_id: int
    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float
    kind = "quote"


@dataclass(frozen=True, slots=True)
class DepthEvent:
    """One exchange order-book message.

    ``is_snapshot=True``: ``bids``/``asks`` are the full book; discard the old book first.
    ``is_snapshot=False``: ``bids``/``asks`` are changed levels; ``qty == 0`` removes the level,
    any other qty is the new absolute size at that price.
    """
    ts_ns: int
    event_ts_ns: int | None
    recv_ts_ns: int | None
    is_snapshot: bool
    # U / u / pu in Binance's docs: first and last update id covered by this message, and the last
    # id of the PREVIOUS message. They let us prove no message was lost (see book.py).
    first_update_id: int
    final_update_id: int
    prev_final_update_id: int | None
    # `tuple[Level, ...]` = a tuple of any length whose items are Levels. Tuples (unlike lists)
    # are immutable, which suits a frozen event.
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    kind = "depth"


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """A funding settlement. ``rate`` is a fraction (0.0001 = 1 bp); longs pay when positive."""
    ts_ns: int
    rate: float
    mark_price: float | None
    interval_hours: int | None
    kind = "funding"


@dataclass(frozen=True, slots=True)
class GapEvent:
    """A marker saying "the order book / feed was interrupted here; everything you know is stale".

    ``ts_ns``   exchange time (ns UTC), aligned so that it sorts immediately before the snapshot
                DepthEvent that repairs the gap.
    ``reason``  "session_start" (first snapshot of a recording session), "resync" (a sequence gap
                was detected and the book was rebuilt) or "disconnect" (a websocket dropped).
    ``stream``  which feed: "depth" for session_start/resync, the endpoint name ("public" =
                depth+bookTicker, "market" = aggTrade) for disconnects; None if unknown.

    Consumers should treat a GapEvent as "drop your book and cancel/hold quotes until the next
    snapshot DepthEvent (is_snapshot=True) arrives". replay() and LiveFeed emit identical markers.
    """
    ts_ns: int
    reason: str
    stream: str | None = None
    kind = "gap"


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """Live-feed housekeeping (connected, disconnected, book_resync, ...). Not stored in replay."""
    ts_ns: int
    recv_ts_ns: int
    status: str
    detail: str = ""  # a default value: callers may omit it
    kind = "status"


# A union type: an Event is any ONE of these four classes. Handy in type hints.
Event = TradeEvent | QuoteEvent | DepthEvent | FundingEvent | GapEvent


# ---------------------------------------------------------------- events -> DataFrames
# These helpers turn a list of event objects into a table (DataFrame) so they can be saved as
# parquet. They are the inverse of the converters in loaders.py.
def trades_to_frame(events: list[TradeEvent]) -> pl.DataFrame:
    # List comprehension: builds a list by evaluating the expression once per event `e`.
    # Each row is a tuple of values in the same order as the schema's columns.
    rows = [(e.ts_ns, e.event_ts_ns, e.recv_ts_ns, e.trade_id, e.first_id, e.last_id,
             e.price, e.qty, e.side) for e in events]
    # orient="row": each tuple is one row (not one column). The schema fixes the dtypes, which also
    # keeps the table typed correctly when the list is empty.
    return pl.DataFrame(rows, schema=S.TRADES_SCHEMA, orient="row")


def quotes_to_frame(events: list[QuoteEvent]) -> pl.DataFrame:
    rows = [(e.ts_ns, e.event_ts_ns, e.recv_ts_ns, e.update_id, e.bid_px, e.bid_qty,
             e.ask_px, e.ask_qty) for e in events]
    return pl.DataFrame(rows, schema=S.QUOTES_SCHEMA, orient="row")


def depth_to_frame(events: list[DepthEvent]) -> pl.DataFrame:
    """Flatten depth messages to one row per level (messages stay contiguous)."""
    rows = []
    for e in events:
        # Columns shared by every level of this message, then the per-level part is inserted
        # between them. Tuples are joined with `+`.
        head = (e.ts_ns, e.event_ts_ns, e.recv_ts_ns)
        tail = (e.is_snapshot, e.first_update_id, e.final_update_id, e.prev_final_update_id)
        # `for px, q in e.bids` unpacks each (price, qty) pair into two variables.
        for px, q in e.bids:
            rows.append(head + (S.BID, px, q) + tail)
        for px, q in e.asks:
            rows.append(head + (S.ASK, px, q) + tail)
    return pl.DataFrame(rows, schema=S.DEPTH_SCHEMA, orient="row")


def funding_to_frame(events: list[FundingEvent]) -> pl.DataFrame:
    rows = [(e.ts_ns, e.rate, e.mark_price, e.interval_hours) for e in events]
    return pl.DataFrame(rows, schema=S.FUNDING_SCHEMA, orient="row")


def gaps_to_frame(events: list[GapEvent]) -> pl.DataFrame:
    """GapEvents -> table (schemas.GAPS_SCHEMA), used by the recorder."""
    rows = [(e.ts_ns, e.reason, e.stream) for e in events]
    return pl.DataFrame(rows, schema=S.GAPS_SCHEMA, orient="row")
