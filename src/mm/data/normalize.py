"""Turn source formats into the canonical schemas of ``schemas.py``.

Two families of sources:

* data.binance.vision CSV files  -> ``*_csv_to_frame`` functions (return DataFrames/LazyFrames)
* live websocket / REST JSON     -> ``parse_*`` functions (return event objects)

Millisecond Binance timestamps are converted to integer nanoseconds (x 1_000_000).
"""
# Postpones type-hint evaluation (see config.py).
from __future__ import annotations

from pathlib import Path
from typing import Any

# Polars primer: a DataFrame is an in-memory table; a LazyFrame is a recipe of operations that only
# runs when you call .collect(). Lazy lets polars optimise and stream big CSVs. Column work is
# written with EXPRESSIONS: pl.col("x") refers to a column, and arithmetic/casts on it describe
# a whole-column computation (no Python loop), e.g. pl.col("time") * 1_000_000.
import polars as pl

from . import schemas as S
from .events import DepthEvent, QuoteEvent, TradeEvent

NS_PER_MS = 1_000_000  # Binance gives milliseconds; the project standard is nanoseconds


def _first_line_is_header(path: Path) -> bool:
    """Archive CSVs have a header line in recent years; some very old files do not."""
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.readline().strip()
    # A data row starts with a digit (an id or timestamp); a header row starts with a letter.
    return not first[:1].isdigit()


# Read a CSV lazily with an explicit schema (column name -> dtype) so types never get guessed wrongly.
def _scan(path: Path, schema: dict[str, pl.DataType]) -> pl.LazyFrame:
    return pl.scan_csv(path, has_header=_first_line_is_header(path), schema=schema)


# Returns a polars EXPRESSION (not data): when(...).then(...).otherwise(...) is a column-wise if/else.
def _side_expr(buyer_maker: str = "is_buyer_maker") -> pl.Expr:
    # Binance's flag is "was the BUYER the maker?". If yes, the buyer was the resting (passive)
    # order, so the aggressor (taker) was the SELLER. This is the classic source of sign mix-ups.
    # is_buyer_maker=True -> the resting order was the buyer -> the aggressor SOLD.
    return pl.when(pl.col(buyer_maker)).then(pl.lit(S.SIDE_SELL)).otherwise(pl.lit(S.SIDE_BUY))


# ---------------------------------------------------------------- archive CSV
def agg_trades_csv_to_frame(path: Path) -> pl.LazyFrame:
    """``aggTrades`` CSV -> canonical trades. Sorted by (ts_ns, trade_id)."""
    schema = {"agg_trade_id": pl.Int64, "price": pl.Float64, "quantity": pl.Float64,
              "first_trade_id": pl.Int64, "last_trade_id": pl.Int64,
              "transact_time": pl.Int64, "is_buyer_maker": pl.Boolean}
    # .select(...) picks/creates output columns; .alias(name) names a computed column. Here we
    # rename the exchange's columns to our canonical names and convert ms to ns.
    lf = _scan(path, schema).select(
        (pl.col("transact_time") * NS_PER_MS).alias("ts_ns"),
        pl.col("agg_trade_id").alias("trade_id"),
        pl.col("first_trade_id").alias("first_id"),
        pl.col("last_trade_id").alias("last_id"),
        pl.col("price"), pl.col("quantity").alias("qty"),
        _side_expr().alias("side"),
    )
    return _finish(lf, "trades", ["ts_ns", "trade_id"])


def trades_csv_to_frame(path: Path) -> pl.LazyFrame:
    """Raw ``trades`` CSV -> canonical trades (first_id == last_id == trade_id)."""
    schema = {"id": pl.Int64, "price": pl.Float64, "qty": pl.Float64, "quote_qty": pl.Float64,
              "time": pl.Int64, "is_buyer_maker": pl.Boolean}
    lf = _scan(path, schema).select(
        (pl.col("time") * NS_PER_MS).alias("ts_ns"),
        pl.col("id").alias("trade_id"),
        pl.col("id").alias("first_id"),
        pl.col("id").alias("last_id"),
        pl.col("price"), pl.col("qty"),
        _side_expr().alias("side"),
    )
    return _finish(lf, "trades", ["ts_ns", "trade_id"])


def book_ticker_csv_to_frame(path: Path) -> pl.LazyFrame:
    """``bookTicker`` CSV -> canonical quotes.

    ``ts_ns`` = transaction_time, ``event_ts_ns`` = event_time. File order is kept for equal
    ``ts_ns`` (stable sort).
    """
    schema = {"update_id": pl.Int64, "best_bid_price": pl.Float64, "best_bid_qty": pl.Float64,
              "best_ask_price": pl.Float64, "best_ask_qty": pl.Float64,
              "transaction_time": pl.Int64, "event_time": pl.Int64}
    lf = _scan(path, schema).select(
        (pl.col("transaction_time") * NS_PER_MS).alias("ts_ns"),
        (pl.col("event_time") * NS_PER_MS).alias("event_ts_ns"),
        pl.col("update_id"),
        pl.col("best_bid_price").alias("bid_px"), pl.col("best_bid_qty").alias("bid_qty"),
        pl.col("best_ask_price").alias("ask_px"), pl.col("best_ask_qty").alias("ask_qty"),
    )
    return _finish(lf, "quotes", ["ts_ns"])


def funding_csv_to_frame(path: Path) -> pl.LazyFrame:
    """``fundingRate`` monthly CSV -> canonical funding (mark_price is null: not in the file)."""
    schema = {"calc_time": pl.Int64, "funding_interval_hours": pl.Int64,
              "last_funding_rate": pl.Float64}
    lf = _scan(path, schema).select(
        (pl.col("calc_time") * NS_PER_MS).alias("ts_ns"),
        pl.col("last_funding_rate").alias("rate"),
        pl.col("funding_interval_hours").alias("interval_hours"),
    )
    return _finish(lf, "funding", ["ts_ns"])


def funding_rest_to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Rows of GET /fapi/v1/fundingRate -> canonical funding (includes markPrice)."""
    df = pl.DataFrame({
        "ts_ns": [int(r["fundingTime"]) * NS_PER_MS for r in rows],
        "rate": [float(r["fundingRate"]) for r in rows],
        "mark_price": [float(r["markPrice"]) if r.get("markPrice") not in (None, "") else None
                       for r in rows],
    }, schema_overrides={"ts_ns": pl.Int64, "rate": pl.Float64, "mark_price": pl.Float64})
    return S.conform(df, "funding").sort("ts_ns")


# Final step shared by all CSV parsers: force the canonical dtypes/column order (filling missing
# columns such as recv_ts_ns with nulls) and sort by time. maintain_order=True makes the sort
# STABLE: rows with equal keys keep their original order, which keeps replay deterministic.
def _finish(lf: pl.LazyFrame, name: str, sort_by: list[str]) -> pl.LazyFrame:
    present = lf.collect_schema().names()
    cols = [pl.col(c).cast(dt) if c in present else pl.lit(None, dtype=dt).alias(c)
            for c, dt in S.SCHEMAS[name].items()]
    return lf.select(cols).sort(sort_by, maintain_order=True)


# ---------------------------------------------------------------- live JSON
# Binance sends prices/quantities as STRINGS (to avoid float rounding in JSON); convert to floats.
# Example input: [["2395.54", "120.869"], ...] -> ((2395.54, 120.869), ...). The generator
# expression inside tuple(...) builds the result lazily; `for p, q in raw` unpacks each pair.
def _levels(raw: list[list[str]]) -> tuple[tuple[float, float], ...]:
    return tuple((float(p), float(q)) for p, q in raw)


# The parse_* functions read the short single-letter keys of Binance's websocket JSON:
# E = event time (ms), T = trade/transaction time (ms), a = aggregate trade id, p = price,
# q = quantity, f/l = first/last underlying trade id, m = buyer-is-maker flag.
def parse_agg_trade(m: dict[str, Any], recv_ts_ns: int) -> TradeEvent:
    """Websocket ``aggTrade`` payload (the ``data`` part of a combined-stream message)."""
    return TradeEvent(
        ts_ns=int(m["T"]) * NS_PER_MS, event_ts_ns=int(m["E"]) * NS_PER_MS, recv_ts_ns=recv_ts_ns,
        trade_id=int(m["a"]), first_id=int(m["f"]), last_id=int(m["l"]),
        price=float(m["p"]), qty=float(m["q"]),
        # m=True -> buyer was the maker -> aggressor sold (see _side_expr above).
        side=S.SIDE_SELL if m["m"] else S.SIDE_BUY,
    )


# bookTicker keys: u = update id, b/B = best bid price/qty, a/A = best ask price/qty.
# `m.get("T", m["E"])` means "use T if present, else fall back to E".
def parse_book_ticker(m: dict[str, Any], recv_ts_ns: int) -> QuoteEvent:
    """Websocket ``bookTicker`` payload."""
    return QuoteEvent(
        ts_ns=int(m.get("T", m["E"])) * NS_PER_MS, event_ts_ns=int(m["E"]) * NS_PER_MS,
        recv_ts_ns=recv_ts_ns, update_id=int(m["u"]),
        bid_px=float(m["b"]), bid_qty=float(m["B"]), ask_px=float(m["a"]), ask_qty=float(m["A"]),
    )


# depthUpdate keys: U = first update id, u = final update id, pu = previous message's u,
# b = bid levels changed, a = ask levels changed. See book.py for why U/u/pu matter.
def parse_depth_diff(m: dict[str, Any], recv_ts_ns: int) -> DepthEvent:
    """Websocket ``depthUpdate`` payload (fields U, u, pu, b, a, E, T)."""
    return DepthEvent(
        ts_ns=int(m.get("T", m["E"])) * NS_PER_MS, event_ts_ns=int(m["E"]) * NS_PER_MS,
        recv_ts_ns=recv_ts_ns, is_snapshot=False,
        first_update_id=int(m["U"]), final_update_id=int(m["u"]),
        prev_final_update_id=int(m["pu"]),
        bids=_levels(m["b"]), asks=_levels(m["a"]),
    )


# The REST snapshot has lastUpdateId (the book as of that update id) plus full bids/asks lists.
def parse_depth_snapshot(m: dict[str, Any], recv_ts_ns: int) -> DepthEvent:
    """REST ``/fapi/v1/depth`` response."""
    lid = int(m["lastUpdateId"])
    return DepthEvent(
        ts_ns=int(m.get("T", m.get("E", 0))) * NS_PER_MS,
        event_ts_ns=int(m["E"]) * NS_PER_MS if "E" in m else None,
        recv_ts_ns=recv_ts_ns, is_snapshot=True,
        first_update_id=lid, final_update_id=lid, prev_final_update_id=None,
        bids=_levels(m["bids"]), asks=_levels(m["asks"]),
    )
