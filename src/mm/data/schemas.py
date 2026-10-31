"""Canonical (normalized) schemas for every dataset in the project.

Conventions, identical everywhere:

* Prices are in quote currency (USDT). Sizes are in base units (ETH).
* Every timestamp is an ``Int64`` count of **nanoseconds since the Unix epoch, UTC**.
* Binance only gives millisecond precision, so archive timestamps are multiples of 1_000_000.

Three different clocks exist; columns say which one they carry:

``ts_ns``        Exchange **matching-engine time** ("T" / ``transaction_time`` / trade time).
                 This is the primary time axis. It is present in every dataset, archive and live,
                 so all streams are comparable and replay order matches what happened at the exchange.
``event_ts_ns``  Exchange **message/event time** ("E"). When the exchange built the message; usually
                 a few ms after ``ts_ns``. Null for archive trades (the file has only trade time).
``recv_ts_ns``   **Local receive time** (``time.time_ns()`` when our websocket client got the
                 message). Only available for live recordings; null for archive data. Use it, not
                 ``ts_ns``, to model the latency your own system would have seen.

Rule of thumb for a backtest: a strategy can only act on information at
``ts_ns + feed_latency``; never at ``ts_ns`` itself.

Side conventions
    trades.side  : aggressor side, "buy" (taker bought, lifted the ask) or "sell" (taker sold).
                   Binance flag ``is_buyer_maker=True`` means the aggressor SOLD.
    depth.side   : "bid" or "ask".
"""
# Postpones type-hint evaluation (see config.py).
from __future__ import annotations

import polars as pl

# A SCHEMA below is just a dict {column name: polars data type}. Passing it when building or reading
# a table forces every file to have identical columns and types, which is what lets the loaders and
# the backtester trust the data. Int64 = 64-bit integer (big enough for ns timestamps),
# Float64 = double precision decimal, String = text, Boolean = True/False.

# Constants (UPPER_CASE by convention) so the strings "buy"/"sell"/"bid"/"ask" are spelled once.
SIDE_BUY = "buy"
SIDE_SELL = "sell"
BID = "bid"
ASK = "ask"

#: One row per print. For aggTrades, one row is a group of fills at the same price/time/side.
TRADES_SCHEMA: dict[str, pl.DataType] = {
    "ts_ns": pl.Int64,        # trade time (matching engine), ns UTC
    "event_ts_ns": pl.Int64,  # exchange event time; null in archive
    "recv_ts_ns": pl.Int64,   # local receive time; null in archive
    "trade_id": pl.Int64,     # aggTrades: agg_trade_id; trades: trade id
    "first_id": pl.Int64,     # first underlying trade id (== trade_id for raw trades)
    "last_id": pl.Int64,      # last underlying trade id  (== trade_id for raw trades)
    "price": pl.Float64,      # USDT
    "qty": pl.Float64,        # ETH
    "side": pl.String,        # aggressor side: "buy" | "sell"
}

#: Best bid/offer updates (Binance "bookTicker").
QUOTES_SCHEMA: dict[str, pl.DataType] = {
    "ts_ns": pl.Int64,        # transaction time (T), ns UTC
    "event_ts_ns": pl.Int64,  # event time (E)
    "recv_ts_ns": pl.Int64,   # local receive time; null in archive
    "update_id": pl.Int64,    # exchange order-book update id (u)
    "bid_px": pl.Float64,
    "bid_qty": pl.Float64,
    "ask_px": pl.Float64,
    "ask_qty": pl.Float64,
}

#: L2 order-book updates, one row per price level change. Rows belonging to the same exchange
#: message share ``final_update_id`` and ``is_snapshot`` and are contiguous in storage.
DEPTH_SCHEMA: dict[str, pl.DataType] = {
    "ts_ns": pl.Int64,                  # transaction time (T). Snapshots: aligned to first applied diff
    "event_ts_ns": pl.Int64,            # event time (E) (snapshots: REST response E)
    "recv_ts_ns": pl.Int64,             # local receive time
    "side": pl.String,                  # "bid" | "ask"
    "price": pl.Float64,
    "qty": pl.Float64,                  # NEW absolute quantity at this price; 0 = delete the level
    "is_snapshot": pl.Boolean,          # True: full-book snapshot row (replace book); False: delta
    "first_update_id": pl.Int64,        # U (snapshots: lastUpdateId)
    "final_update_id": pl.Int64,        # u (snapshots: lastUpdateId)
    "prev_final_update_id": pl.Int64,   # pu; null for snapshots
}

#: Funding settlements (every 8h for ETHUSDT historically; interval is a column).
FUNDING_SCHEMA: dict[str, pl.DataType] = {
    "ts_ns": pl.Int64,                  # funding time (calc_time), ns UTC
    "rate": pl.Float64,                 # fraction per interval, e.g. 0.0001 = 1 bp. Longs pay if > 0
    "mark_price": pl.Float64,           # USDT; null when the source has none (archive CSV does not)
    "interval_hours": pl.Int64,
}

#: Feed-gap markers written by the live recorder (see events.GapEvent). ts_ns is EXCHANGE time.
GAPS_SCHEMA: dict[str, pl.DataType] = {
    "ts_ns": pl.Int64,        # exchange-time estimate of the gap (snapshot-aligned for resync/session_start)
    "reason": pl.String,      # "session_start" | "resync" | "disconnect"
    "stream": pl.String,      # "depth" for book markers; endpoint name ("public"/"market") for disconnects
}

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "trades": TRADES_SCHEMA,
    "quotes": QUOTES_SCHEMA,
    "depth": DEPTH_SCHEMA,
    "funding": FUNDING_SCHEMA,
    "gaps": GAPS_SCHEMA,
}


# `dict[str, pl.DataType]` in the hints above: a dict with string keys and polars-type values.
def empty(name: str) -> pl.DataFrame:
    """An empty DataFrame with the canonical schema ``name``."""
    return pl.DataFrame(schema=SCHEMAS[name])


def conform(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """Cast to the canonical dtypes, add missing (nullable) columns, order the columns.

    Extra columns are dropped. Raises if a non-nullable-by-nature column such as ``ts_ns``
    is missing.
    """
    schema = SCHEMAS[name]
    # Every dataset is time-indexed; refuse data that has no timestamp column.
    if "ts_ns" not in df.columns:
        raise ValueError(f"{name}: 'ts_ns' column is required")
    # Build one polars expression per output column: cast existing columns to the canonical dtype,
    # or create an all-null column (pl.lit(None, dtype=...)) if the source lacks it.
    exprs = []
    for col, dtype in schema.items():
        if col in df.columns:
            exprs.append(pl.col(col).cast(dtype))
        else:
            exprs.append(pl.lit(None, dtype=dtype).alias(col))
    return df.select(exprs)
