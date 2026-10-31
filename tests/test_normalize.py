"""Normalization of archive CSVs and live JSON payloads."""
# pytest primer: functions named test_* are run automatically; an `assert` that fails = test fails.
# `tmp_path` is a built-in pytest FIXTURE: an argument that pytest fills in with a fresh temporary
# folder, so tests can write sample CSV files without touching real data.
import polars as pl

from mm.data import normalize as N
from mm.data import schemas as S
from mm.data.events import trades_to_frame, depth_to_frame, quotes_to_frame


def test_agg_trades_csv(tmp_path):
    # Archive CSV -> canonical schema: ms->ns conversion, sort by (ts, id), and the aggressor-side
    # rule (is_buyer_maker=True means the buyer was resting, so the taker SOLD).
    p = tmp_path / "a.csv"
    p.write_text("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
                 "11,2420.8,0.5,100,102,1792022400007,false\n"
                 "10,2420.7,1.5,99,99,1792022400007,true\n")
    df = N.agg_trades_csv_to_frame(p).collect()
    assert list(df.columns) == list(S.TRADES_SCHEMA)
    assert dict(df.schema) == S.TRADES_SCHEMA
    assert df["ts_ns"].to_list() == [1792022400007 * 10**6] * 2
    assert df["trade_id"].to_list() == [10, 11]                 # sorted by (ts, id)
    assert df["side"].to_list() == ["sell", "buy"]              # is_buyer_maker=True -> aggressor sold
    assert df["recv_ts_ns"].null_count() == 2


def test_trades_csv_and_headerless(tmp_path):
    # Very old archive files have no header line; the reader must cope.
    p = tmp_path / "t.csv"
    p.write_text("5,10.0,2.0,20.0,1000,true\n6,10.5,1.0,10.5,1001,false\n")  # no header
    df = N.trades_csv_to_frame(p).collect()
    assert df.height == 2 and df["first_id"].to_list() == [5, 6] == df["last_id"].to_list()


def test_book_ticker_csv(tmp_path):
    # Quotes: ts_ns comes from transaction_time, event_ts_ns from event_time.
    p = tmp_path / "b.csv"
    p.write_text("update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time\n"
                 "1,3512.46,8.1,3512.47,7.9,1711756800019,1711756800026\n")
    df = N.book_ticker_csv_to_frame(p).collect()
    r = df.row(0, named=True)
    assert r["ts_ns"] == 1711756800019 * 10**6 and r["event_ts_ns"] == 1711756800026 * 10**6
    assert (r["bid_px"], r["ask_px"], r["bid_qty"], r["ask_qty"]) == (3512.46, 3512.47, 8.1, 7.9)


def test_funding_csv_and_rest(tmp_path):
    # The archive funding CSV has no mark price (null); the REST format does.
    p = tmp_path / "f.csv"
    p.write_text("calc_time,funding_interval_hours,last_funding_rate\n1788220800005,8,0.00005716\n1788249600000,8,9.2E-7\n")
    df = N.funding_csv_to_frame(p).collect()
    assert df["rate"].to_list() == [0.00005716, 9.2e-7] and df["mark_price"].null_count() == 2
    rest = N.funding_rest_to_frame([{"fundingTime": 1792396800004, "fundingRate": "0.0001", "markPrice": "2429.4"}])
    assert rest["mark_price"][0] == 2429.4 and rest["ts_ns"][0] == 1792396800004 * 10**6


def test_live_parsers():
    # Binance websocket JSON uses short keys (E,T,a,p,q,m,U,u,pu,b,a...) and string numbers.
    t = N.parse_agg_trade({"e": "aggTrade", "E": 1000, "T": 999, "a": 7, "p": "2400.5", "q": "0.3",
                           "f": 70, "l": 72, "m": False}, recv_ts_ns=1_100_000_000)
    assert (t.ts_ns, t.event_ts_ns, t.recv_ts_ns, t.side) == (999_000_000, 1_000_000_000, 1_100_000_000, "buy")
    assert (t.trade_id, t.first_id, t.last_id, t.price, t.qty) == (7, 70, 72, 2400.5, 0.3)
    q = N.parse_book_ticker({"e": "bookTicker", "u": 5, "E": 1000, "T": 998, "s": "ETHUSDT",
                             "b": "1.0", "B": "2", "a": "1.1", "A": "3"}, 5)
    assert (q.ts_ns, q.bid_px, q.ask_qty, q.update_id) == (998_000_000, 1.0, 3.0, 5)
    d = N.parse_depth_diff({"e": "depthUpdate", "E": 1000, "T": 999, "U": 10, "u": 12, "pu": 9,
                            "b": [["1.0", "0"]], "a": [["1.1", "2"]]}, 5)
    assert (d.first_update_id, d.final_update_id, d.prev_final_update_id) == (10, 12, 9)
    assert d.bids == ((1.0, 0.0),) and d.asks == ((1.1, 2.0),) and not d.is_snapshot
    s = N.parse_depth_snapshot({"lastUpdateId": 77, "E": 1000, "T": 999, "bids": [["1", "1"]], "asks": []}, 5)
    assert s.is_snapshot and s.final_update_id == 77 and s.prev_final_update_id is None


def test_events_to_frames_roundtrip_schema():
    # Events converted to tables must match the canonical schemas exactly (one row per depth level).
    t = N.parse_agg_trade({"E": 1, "T": 1, "a": 1, "p": "1", "q": "1", "f": 1, "l": 1, "m": True}, 2)
    assert dict(trades_to_frame([t]).schema) == S.TRADES_SCHEMA
    q = N.parse_book_ticker({"u": 1, "E": 1, "b": "1", "B": "1", "a": "2", "A": "1"}, 2)
    assert dict(quotes_to_frame([q]).schema) == S.QUOTES_SCHEMA
    d = N.parse_depth_diff({"E": 1, "U": 1, "u": 2, "pu": 0, "b": [["1", "1"], ["0.9", "0"]], "a": [["2", "1"]]}, 2)
    df = depth_to_frame([d])
    assert dict(df.schema) == S.DEPTH_SCHEMA and df.height == 3
    assert df["side"].to_list() == ["bid", "bid", "ask"]


def test_conform_adds_missing_columns():
    # conform() fills absent columns with nulls and fixes column order.
    df = S.conform(pl.DataFrame({"ts_ns": [1], "price": [1.0]}), "trades")
    assert list(df.columns) == list(S.TRADES_SCHEMA) and df["side"].null_count() == 1
