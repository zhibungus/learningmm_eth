"""Replay ordering, determinism, depth grouping, date windows, and quality checks."""
# pytest primer: test_* functions run automatically. A FIXTURE (function decorated with
# @pytest.fixture) builds reusable test input; any test that names it as an argument receives its
# return value. `tmp_path` is pytest's built-in temporary folder fixture.
import copy
import datetime as dt

import polars as pl
import pytest

from mm.data import loaders as L
from mm.data import schemas as S
from mm.data import quality as Q
from mm.data.config import load_config
from mm.data.events import DepthEvent, FundingEvent, QuoteEvent, TradeEvent

D0 = dt.date(2026, 10, 17)
T0 = int(dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc).timestamp()) * 10**9


# Helper: write a DataFrame as a parquet file at <root>/<folder>/<symbol>/<period>/<part>.parquet,
# mimicking the layout the real downloader produces.
def _write(root, folder, symbol, period, df, part="part-0"):
    d = root / folder / symbol / period
    d.mkdir(parents=True, exist_ok=True)
    df.write_parquet(d / f"{part}.parquet")


# Builds a tiny fake data set in a temp folder with deliberately colliding timestamps, and returns
# a config pointing at it. deepcopy avoids modifying the real loaded config.
@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["processed_dir"] = str(tmp_path)  # absolute path replaces the project root
    c["paths"]["archive_name"] = "arc"
    root = tmp_path / "arc"
    sym = "ETHUSDT"
    trades = pl.DataFrame({
        "ts_ns": [T0 + 100, T0 + 300, T0 + 300], "trade_id": [1, 2, 3], "first_id": [1, 2, 3],
        "last_id": [1, 2, 3], "price": [100.0, 100.0, 101.0], "qty": [1.0, 1.0, 2.0],
        "side": ["buy", "sell", "buy"]})
    quotes = pl.DataFrame({
        "ts_ns": [T0 + 100, T0 + 300], "update_id": [1, 2], "bid_px": [99.0, 99.5], "bid_qty": [1.0, 1.0],
        "ask_px": [100.0, 100.0], "ask_qty": [1.0, 1.0]})
    depth_rows = []
    for ts, snap, fu, lu, pu, lv in [(T0 + 100, True, 5, 5, None, [("bid", 99.0, 1.0), ("ask", 100.0, 1.0)]),
                                     (T0 + 300, False, 6, 7, 5, [("bid", 99.0, 0.0), ("bid", 98.0, 2.0), ("ask", 100.5, 1.0)])]:
        for side, px, q in lv:
            depth_rows.append((ts, None, None, side, px, q, snap, fu, lu, pu))
    depth = pl.DataFrame(depth_rows, schema=S.DEPTH_SCHEMA, orient="row")
    funding = pl.DataFrame({"ts_ns": [T0 + 300], "rate": [0.0001], "mark_price": [100.0], "interval_hours": [8]})
    _write(root, "agg_trades", sym, "2026-10-17", S.conform(trades, "trades"))
    _write(root, "quotes", sym, "2026-10-17", S.conform(quotes, "quotes"))
    _write(root, "depth", sym, "2026-10-17", depth)
    _write(root, "funding", sym, "2026-10", S.conform(funding, "funding"))
    return c


def test_tie_break_order_is_deterministic(cfg):
    # At identical timestamps events must come out in a fixed order (funding, trade, depth, quote)
    # and the same on every run: backtests must be reproducible.
    evs = list(L.replay(D0, D0, cfg=cfg, streams=("trades", "quotes", "depth", "funding")))
    # at T0+100: trade, depth(snapshot), quote ; at T0+300: funding, trade, trade, depth, quote
    kinds = [(e.ts_ns - T0, e.kind) for e in evs]
    assert kinds == [(100, "trade"), (100, "depth"), (100, "quote"),
                     (300, "funding"), (300, "trade"), (300, "trade"), (300, "depth"), (300, "quote")]
    assert [e.trade_id for e in evs if isinstance(e, TradeEvent)] == [1, 2, 3]  # file order kept
    again = list(L.replay(D0, D0, cfg=cfg))
    assert again == evs


def test_event_types_and_depth_grouping(cfg):
    # Several parquet rows (one per price level) must be regrouped into ONE DepthEvent per message.
    evs = list(L.replay("2026-10-17", "2026-10-17", cfg=cfg, streams=["depth"]))
    assert len(evs) == 2 and all(isinstance(e, DepthEvent) for e in evs)
    assert evs[0].is_snapshot and evs[0].bids == ((99.0, 1.0),) and evs[0].asks == ((100.0, 1.0),)
    assert evs[1].bids == ((99.0, 0.0), (98.0, 2.0)) and evs[1].prev_final_update_id == 5
    q = next(iter(L.replay(D0, D0, cfg=cfg, streams=["quotes"])))
    assert isinstance(q, QuoteEvent) and q.event_ts_ns is None
    f = next(iter(L.replay(D0, D0, cfg=cfg, streams=["funding"])))
    assert isinstance(f, FundingEvent) and f.rate == 0.0001


def test_date_window_and_missing_stream(cfg):
    # Dates outside the data give empty results; replaying a stream with no files is an error
    # (so you never backtest on silently empty data); bad stream names are rejected.
    # `with pytest.raises(X):` passes only if the block raises X.
    assert L.load_trades("2026-10-18", "2026-10-18", cfg=cfg).is_empty()
    assert L.load_trades(D0, D0, cfg=cfg).height == 3
    with pytest.raises(FileNotFoundError):
        list(L.replay("2026-10-18", "2026-10-18", cfg=cfg, streams=["trades"]))
    with pytest.raises(ValueError):
        list(L.replay(D0, D0, cfg=cfg, streams=["bogus"]))


def test_unsorted_file_is_sorted_and_cross_file_disorder_detected(cfg, tmp_path):
    # A second file that starts earlier than the first one ended is a data problem:
    # strict mode raises, non-strict mode carries on.
    root = tmp_path / "arc"
    late = S.conform(pl.DataFrame({"ts_ns": [T0 + 60, T0 + 50], "trade_id": [11, 10], "price": [1.0, 1.0],
                                   "qty": [1.0, 1.0], "side": ["buy", "buy"]}), "trades")
    _write(root, "agg_trades", "ETHUSDT", "2026-10-17", late, part="part-1")
    ts = [e.ts_ns for e in L.replay(D0, D0, cfg=cfg, streams=["trades"], strict=False)]
    assert len(ts) == 5  # each file is sorted on its own; strict=False tolerates the overlap
    with pytest.raises(L.ReplayOrderError):
        list(L.replay(D0, D0, cfg=cfg, streams=["trades"], strict=True))


def test_quality_checks_detect_problems():
    # Feed deliberately broken data and check that the quality functions count the problems.
    t = S.conform(pl.DataFrame({
        "ts_ns": [10**9, 2 * 10**9, 500 * 10**9, 501 * 10**9], "trade_id": [1, 2, 2, 5],
        "price": [1.0, 1.0, 1.0, 1.0], "qty": [1.0] * 4, "side": ["buy"] * 4}), "trades")
    r = Q.check_trades(t)
    assert r["duplicate_trade_ids"] == 2 and r["id_gaps"] == 1 and r["missing_ids"] == 2
    assert r["silences_over_limit"] == 1
    q = S.conform(pl.DataFrame({"ts_ns": [1, 2, 3], "update_id": [5, 4, 4], "bid_px": [1.0, 2.0, 1.0],
                                "bid_qty": [1.0] * 3, "ask_px": [1.5, 1.5, 1.0], "ask_qty": [1.0] * 3}), "quotes")
    r = Q.check_quotes(q)
    assert r["crossed"] == 1 and r["locked"] == 1 and r["update_id_backwards"] == 1 and r["update_id_repeated"] == 1


def test_quality_depth_chain_break_and_crossed():
    # Replays depth rows into an OrderBook: expects one pu-chain break and one crossed book.
    rows = []
    for ts, snap, fu, lu, pu, lv in [
        (1, True, 5, 5, None, [("bid", 99.0, 1.0), ("ask", 100.0, 1.0)]),
        (2, False, 6, 7, 3, [("bid", 98.0, 1.0)]),            # first diff after snapshot: pu not checked
        (3, False, 8, 9, 7, [("bid", 101.0, 1.0)]),           # crosses the book
        (4, False, 12, 13, 11, [("bid", 101.0, 0.0)])]:       # chain break (pu 11 != 9)
        for side, px, q in lv:
            rows.append((ts, None, None, side, px, q, snap, fu, lu, pu))
    r = Q.check_depth(pl.DataFrame(rows, schema=S.DEPTH_SCHEMA, orient="row"))
    assert r["snapshots"] == 1 and r["messages"] == 4
    assert r["update_id_chain_breaks"] == 1 and r["crossed_after_msg"] == 1
