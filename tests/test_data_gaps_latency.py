"""Tests for gap markers, per-stream replay sources, exchange-time date folders and latency stats.

pytest primer: functions named test_* run automatically; a failing `assert` fails the test.
`tmp_path` is a built-in fixture giving each test its own empty temporary folder.
"""
import copy
import datetime as dt
import json

import polars as pl
import pytest

from mm.data import events as EV
from mm.data import latency as LAT
from mm.data import loaders as L
from mm.data import normalize as N
from mm.data import quality as Q
from mm.data import schemas as S
from mm.data.config import load_config, processed_dir, raw_dir
from mm.data.events import GapEvent, TradeEvent
from mm.data.live import LiveFeed
from mm.data.record import Recorder, _Bucket

SYM = "ETHUSDT"
DAY = dt.date(2026, 10, 17)
T0 = int(dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc).timestamp()) * 10**9
MS = 10**6


@pytest.fixture
def cfg(tmp_path):
    """A config whose raw/processed folders live in a throw-away temp dir."""
    c = copy.deepcopy(load_config())
    c["paths"]["processed_dir"] = str(tmp_path / "proc")   # absolute path replaces the project root
    c["paths"]["raw_dir"] = str(tmp_path / "raw")
    return c


# ---- helpers that build tiny stored datasets -----------------------------------------------
def write_part(cfg, folder, live, df, period="2026-10-17", name="part-0"):
    root = processed_dir(cfg, live=live) / folder / SYM / period
    root.mkdir(parents=True, exist_ok=True)
    df.write_parquet(root / f"{name}.parquet")


def depth_frame(msgs):
    """msgs: list of (ts_ns, is_snapshot, first_id, last_id, prev_id)."""
    rows = []
    for ts, snap, fu, lu, pu in msgs:
        rows.append((ts, None, None, "bid", 99.0, 1.0, snap, fu, lu, pu))
        rows.append((ts, None, None, "ask", 100.0, 1.0, snap, fu, lu, pu))
    return pl.DataFrame(rows, schema=S.DEPTH_SCHEMA, orient="row")


def trade_frame(tss):
    return S.conform(pl.DataFrame({
        "ts_ns": tss, "trade_id": list(range(len(tss))), "first_id": list(range(len(tss))),
        "last_id": list(range(len(tss))), "price": [100.0] * len(tss), "qty": [1.0] * len(tss),
        "side": ["buy"] * len(tss)}), "trades")


# ---- GapEvent basics ---------------------------------------------------------------------------
def test_gap_event_fields_and_priority():
    g = GapEvent(ts_ns=5, reason="resync", stream="depth")
    assert (g.ts_ns, g.kind, g.reason, g.stream) == (5, "gap", "resync", "depth")
    assert GapEvent(1, "disconnect").stream is None           # stream is optional
    with pytest.raises(Exception):
        g.reason = "x"                                          # frozen dataclass
    pr = load_config()["replay"]["kind_priority"]
    assert pr["gap"] < pr["funding"] < pr["trade"] < pr["depth"] < pr["quote"]
    assert dict(EV.gaps_to_frame([g]).schema) == S.GAPS_SCHEMA


# ---- LiveFeed emits the same markers -------------------------------------------------------
def dmsg(U, u, pu, T=1000):
    return json.dumps({"stream": "x", "data": {"e": "depthUpdate", "E": T, "T": T, "U": U, "u": u,
                                               "pu": pu, "b": [["99", "1"]], "a": [["100", "1"]]}})


def snapshot(L_, ts=900):
    return N.parse_depth_snapshot({"lastUpdateId": L_, "E": ts, "T": ts,
                                   "bids": [["98", "2"]], "asks": [["101", "2"]]}, 1)


def test_live_feed_emits_gap_before_snapshot_and_on_resync_and_disconnect():
    feed = LiveFeed()
    got = []
    feed.subscribe(got.append)
    feed.handle_message(dmsg(1, 10, 0, T=1000), 1)
    feed._emit_depth(feed.sync.on_snapshot(snapshot(5)))
    kinds = [e.kind for e in got if e.kind in ("gap", "depth")]
    assert kinds == ["gap", "depth", "depth"]                   # gap, snapshot, first diff
    first_gap = next(e for e in got if e.kind == "gap")
    assert (first_gap.reason, first_gap.stream) == ("session_start", "depth")
    snap = next(e for e in got if e.kind == "depth")
    assert first_gap.ts_ns == snap.ts_ns                        # same timestamp => sorts right before it

    feed.handle_message(dmsg(20, 21, 19, T=2000), 2)            # pu mismatch => sequence gap
    assert feed.sync.needs_snapshot
    feed._emit_depth(feed.sync.on_snapshot(snapshot(20, ts=1500)))
    gaps = [e for e in got if e.kind == "gap"]
    assert [g.reason for g in gaps] == ["session_start", "resync"]
    assert any(getattr(e, "status", "") == "book_gap" for e in got)   # StatusEvent still emitted

    feed._emit_disconnect("market")
    d = [e for e in got if e.kind == "gap"][-1]
    assert (d.reason, d.stream) == ("disconnect", "market")
    assert d.ts_ns == feed._last_ts_ns + 1                          # stamped with last EXCHANGE time seen


# ---- recorder persistence + exchange-time date folders -------------------------------------
def test_bucket_files_by_exchange_date_not_pc_date(tmp_path):
    midnight = T0 + 86_400 * 10**9
    evs = [TradeEvent(midnight - 1 * MS, None, None, 1, 1, 1, 100.0, 1.0, "buy"),
           TradeEvent(midnight + 1 * MS, None, None, 2, 2, 2, 100.0, 1.0, "sell")]
    b = _Bucket("trades", EV.trades_to_frame, tmp_path, SYM, 1000)
    for e in evs:
        b.add(e)
    # The "stamp" (PC window start) is deliberately a different date: it must only name the file.
    b.flush(dt.datetime(2030, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc))
    d1 = tmp_path / "trades" / SYM / "2026-10-17"
    d2 = tmp_path / "trades" / SYM / "2026-10-18"
    assert pl.read_parquet(next(d1.glob("*.parquet")))["trade_id"].to_list() == [1]
    assert pl.read_parquet(next(d2.glob("*.parquet")))["trade_id"].to_list() == [2]
    assert not (tmp_path / "trades" / SYM / "2030-01-01").exists()
    assert b.rows_written == 2


def test_recorder_persists_gaps_and_replay_uses_them(cfg):
    feed = LiveFeed(cfg=cfg)
    rec = Recorder(feed, record_raw=False)
    ts = T0 + 10**9
    t_ms = ts // MS
    feed.handle_message(dmsg(1, 10, 0, T=t_ms), 1)
    feed._emit_depth(feed.sync.on_snapshot(snapshot(5, ts=t_ms - 5)))
    feed.handle_message(dmsg(11, 12, 10, T=t_ms + 100), 2)
    feed._emit_disconnect("public")
    rec.close()
    g = L.load_gaps(DAY, DAY, SYM, "live", cfg)
    assert g["reason"].to_list() == ["session_start", "disconnect"]
    evs = list(L.replay(DAY, DAY, SYM, streams=["depth"], source="live", cfg=cfg))
    kinds = [(e.kind, getattr(e, "reason", None) or getattr(e, "is_snapshot", None)) for e in evs]
    # persisted session_start immediately before the snapshot, no duplicate synthesised marker,
    # the buffered diff that straddles the snapshot, the next diff, then the disconnect marker
    assert kinds == [("gap", "session_start"), ("depth", True), ("depth", False), ("depth", False),
                     ("gap", "disconnect")]


# ---- replay: synthesised gaps for recordings without markers -------------------------------
def test_replay_synthesises_gaps_for_old_recordings(cfg):
    msgs = [(T0 + 100, True, 5, 5, None), (T0 + 200, False, 6, 7, 5),
            (T0 + 900, True, 50, 50, None), (T0 + 950, False, 51, 52, 49)]
    write_part(cfg, "depth", True, depth_frame(msgs))
    write_part(cfg, "trades", True, trade_frame([T0 + 100, T0 + 900]))   # same ts as the snapshots
    evs = list(L.replay(DAY, DAY, SYM, streams=["trades", "depth"], source="live", cfg=cfg))
    seq = [(e.kind, getattr(e, "reason", None)) for e in evs]
    assert seq == [("gap", "session_start"), ("trade", None), ("depth", None), ("depth", None),
                   ("gap", "resync"), ("trade", None), ("depth", None), ("depth", None)]
    gaps = [e for e in evs if e.kind == "gap"]
    snaps = [e for e in evs if e.kind == "depth" and e.is_snapshot]
    assert [g.ts_ns for g in gaps] == [s.ts_ns for s in snaps] and all(g.stream == "depth" for g in gaps)


def test_archive_replay_has_no_gap_events(cfg):
    write_part(cfg, "agg_trades", False, trade_frame([T0 + 1, T0 + 2]))
    evs = list(L.replay(DAY, DAY, SYM, streams=["trades"], source="archive", cfg=cfg))
    assert [e.kind for e in evs] == ["trade", "trade"]


# ---- replay: per-stream sources ----------------------------------------------------------
def test_per_stream_source_dict_and_plain_string(cfg):
    write_part(cfg, "depth", True, depth_frame([(T0 + 100, True, 5, 5, None)]))
    fund = S.conform(pl.DataFrame({"ts_ns": [T0 + 50], "rate": [0.0001], "mark_price": [100.0],
                                   "interval_hours": [8]}), "funding")
    write_part(cfg, "funding", False, fund, period="2026-10")
    mixed = list(L.replay(DAY, DAY, SYM, streams=["depth", "funding"],
                          source={"depth": "live", "funding": "archive"}, cfg=cfg))
    assert [e.kind for e in mixed] == ["funding", "gap", "depth"]
    # unspecified streams default: depth -> live, others -> archive
    dflt = list(L.replay(DAY, DAY, SYM, streams=["depth", "funding"], source={}, cfg=cfg))
    assert [e.kind for e in dflt] == ["funding", "gap", "depth"]
    # plain string still means "this source for every stream": funding has no live files
    with pytest.raises(FileNotFoundError):
        list(L.replay(DAY, DAY, SYM, streams=["depth", "funding"], source="live", cfg=cfg))
    assert [e.kind for e in L.replay(DAY, DAY, SYM, streams=["depth"], source="live", cfg=cfg)] == ["gap", "depth"]
    with pytest.raises(ValueError):
        list(L.replay(DAY, DAY, SYM, streams=["depth"], source={"bogus": "live"}, cfg=cfg))
    with pytest.raises(ValueError):
        list(L.replay(DAY, DAY, SYM, streams=["depth"], source={"depth": "nowhere"}, cfg=cfg))


def test_live_folder_off_by_one_day_still_found(cfg):
    # An OLD recording: the last seconds of 2026-10-17 sit in the 2026-10-18 folder (PC clock ahead).
    write_part(cfg, "trades", True, trade_frame([T0 + 86_399 * 10**9]), period="2026-10-18")
    assert L.load_trades(DAY, DAY, SYM, "live", cfg=cfg).height == 1


# ---- latency tool on synthetic data ------------------------------------------------------
def make_frame(n, offset_ms, one_way_ms, e_minus_t_ms=2, start_ms=1_000_000):
    """n frames: exchange time T, published at T+2ms, arriving one_way_ms later by the exchange clock;
    the PC clock is offset_ms ahead of the exchange clock."""
    rows = []
    for i in range(n):
        t = (start_ms + i * 10) * MS
        e = t + e_minus_t_ms * MS
        recv = e + int(one_way_ms * MS) + int(offset_ms * MS)
        rows.append((t, e, recv))
    return pl.DataFrame(rows, schema={"ts_ns": pl.Int64, "event_ts_ns": pl.Int64, "recv_ts_ns": pl.Int64},
                        orient="row")


def test_latency_stats_recover_known_delay_and_per_session_offset():
    # Two sessions with DIFFERENT clock offsets (drift): 5000 ms then 5200 ms. A constant-offset
    # assumption would report a 200 ms error for the second one.
    s1 = make_frame(100, 5000, 150, start_ms=1_000_000)
    s2 = make_frame(100, 5200, 150, start_ms=2_000_000)
    df = pl.concat([s1, s2])
    offsets = pl.DataFrame({"local_ts_ns": [s1["recv_ts_ns"][0], s2["recv_ts_ns"][0]],
                            "offset_ms": [5000.0, 5200.0]})
    st = LAT.latency_stats({"quotes": df}, offsets)
    one_way = st["streams"]["quotes"]["one_way_ms"]
    assert one_way["n"] == 200
    assert one_way["p50"] == pytest.approx(150.0) and one_way["p99"] == pytest.approx(150.0)
    assert st["streams"]["quotes"]["e_minus_t_ms"]["p50"] == pytest.approx(2.0)
    assert st["streams"]["quotes"]["total_ms"]["p50"] == pytest.approx(152.0)
    assert st["offset_ms_min_max"] == [5000.0, 5200.0]
    # Wrong (constant) offset would be off by 200 ms for the second session:
    wrong = LAT.latency_stats({"quotes": df}, offsets.head(1))["streams"]["quotes"]["one_way_ms"]
    assert wrong["max"] == pytest.approx(350.0)


def test_latency_percentiles_and_suggestions():
    vals = pl.Series(list(range(1, 101)), dtype=pl.Float64)
    p = LAT.percentiles(vals)
    assert p["p50"] == pytest.approx(50.5) and p["p90"] == pytest.approx(90.1) and p["max"] == 100
    assert LAT.percentiles(pl.Series([], dtype=pl.Float64))["n"] == 0
    stats = {"pooled": {"total_ms": {"p50": 240.0, "p90": 300.0}}}
    sug = LAT.suggest(stats, [200.0, 220.0, 240.0, 260.0, 300.0], gateway_extra_ms=20)
    assert sug["feed_latency_ms"] == 240 and sug["feed_jitter_ms"] == 60
    assert sug["report_latency_ms"] == 240
    assert sug["order_latency_ms"] == 140        # median 240 / 2 + 20 gateway
    assert sug["order_jitter_ms"] == 22        # (interpolated p90 284 - p50 240) / 2
    assert "order_latency_ms" not in LAT.suggest(stats, None, 20)


def test_latency_without_offsets_is_flagged():
    st = LAT.latency_stats({"q": make_frame(5, 0, 10)}, pl.DataFrame(
        schema={"local_ts_ns": pl.Int64, "offset_ms": pl.Float64}))
    assert st["offset_measurements"] == 0
    assert "WARNING" in LAT.format_report(st, None, {})


def test_load_clock_offsets_reads_status_file(cfg):
    d = raw_dir(cfg, live=True) / SYM
    d.mkdir(parents=True)
    lines = [{"ts_ns": 20, "status": "clock_offset_ms", "detail": "5131.1"},
             {"ts_ns": 5, "status": "connected", "detail": "x"},
             {"ts_ns": 10, "status": "clock_offset_ms", "detail": "5145.7"}]
    (d / "status.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    off = LAT.load_clock_offsets(cfg, SYM)
    assert off["local_ts_ns"].to_list() == [10, 20] and off["offset_ms"].to_list() == [5145.7, 5131.1]


def test_funding_partial_month_is_flagged():
    def fund(days):
        ts = [int(dt.datetime(2026, 10, d, 16, tzinfo=dt.timezone.utc).timestamp()) * 10**9 for d in days]
        return S.conform(pl.DataFrame({"ts_ns": ts, "rate": [0.0001] * len(ts), "interval_hours": [8] * len(ts)}), "funding")
    assert Q.check_funding(fund([1, 2, 3]))["partial_final_month"] is True
    assert Q.check_funding(fund([30, 31]))["partial_final_month"] is False   # 16:00 on the 31st = the last settlement of the month



# ---- resync markers are stamped at DETECTION time ---------------------------------------
def test_live_feed_resync_marker_emitted_at_detection_not_at_snapshot():
    feed = LiveFeed()
    got = []
    feed.subscribe(got.append)
    feed.handle_message(dmsg(1, 10, 0, T=1000), 1)
    feed._emit_depth(feed.sync.on_snapshot(snapshot(5)))
    n_before = len(got)
    feed.handle_message(dmsg(20, 21, 19, T=2000), 2)          # gap revealed by the diff at T=2000 ms
    new = got[n_before:]
    resync = [e for e in new if e.kind == "gap"]
    assert len(resync) == 1 and resync[0].reason == "resync" and resync[0].stream == "depth"
    assert resync[0].ts_ns == 2000 * MS                         # detection time, exchange clock
    # the repairing snapshot arrives later (ts aligned to the first applied diff) with NO extra marker
    n_mid = len(got)
    feed._emit_depth(feed.sync.on_snapshot(snapshot(20, ts=1500)))
    after = got[n_mid:]
    assert [e.kind for e in after].count("gap") == 0
    assert any(e.kind == "depth" and e.is_snapshot for e in after)


def test_replay_emits_persisted_resync_at_detection_time_and_snapshot_at_its_own(cfg):
    msgs = [(T0 + 100, True, 5, 5, None), (T0 + 200, False, 6, 7, 5),
            (T0 + 900, True, 50, 50, None), (T0 + 950, False, 51, 52, 49)]
    write_part(cfg, "depth", True, depth_frame(msgs))
    write_part(cfg, "trades", True, trade_frame([T0 + 600]))
    gaps = pl.DataFrame({"ts_ns": [T0 + 100, T0 + 500], "reason": ["session_start", "resync"],
                         "stream": ["depth", "depth"]}, schema=S.GAPS_SCHEMA)
    write_part(cfg, "gaps", True, gaps)
    evs = list(L.replay(DAY, DAY, SYM, streams=["trades", "depth"], source="live", cfg=cfg))
    seq = [(e.kind, e.ts_ns - T0, getattr(e, "reason", None)) for e in evs]
    assert seq == [("gap", 100, "session_start"), ("depth", 100, None), ("depth", 200, None),
                   ("gap", 500, "resync"),              # at DETECTION time, before the snapshot
                   ("trade", 600, None),               # an order placed now would rest on a stale book
                   ("depth", 900, None), ("depth", 950, None)]   # snapshot at ITS OWN ts, no 2nd marker


# ---- midnight file ordering (PC clock ahead of the exchange) ----------------------------
def _trade_events(ids, base_ns):
    return [TradeEvent(base_ns + i * MS, None, None, i, i, i, 100.0, 1.0, "buy") for i in ids]


def test_midnight_pc_clock_ahead_files_replay_in_exchange_order(cfg):
    proc = processed_dir(cfg, live=True)
    day_end = T0 + 86_400 * 10**9                    # exchange midnight at the end of DAY
    # Window 1: PC window start 23:50:05 (stamp), data around exchange 23:50.
    b1 = _Bucket("trades", EV.trades_to_frame, proc, SYM, 10_000)
    for e in _trade_events(range(3), day_end - 600 * 10**9):
        b1.add(e)
    b1.flush(dt.datetime(2026, 10, 17, 23, 50, 5, tzinfo=dt.timezone.utc))
    # Window 2: the PC clock is ~5 s fast, so the window "starts at PC midnight" (stamp 00:00:00 of
    # the NEXT day) while its first rows are exchange time 23:59:55, still DAY.
    b2 = _Bucket("trades", EV.trades_to_frame, proc, SYM, 10_000)
    for e in _trade_events(range(10, 13), day_end - 5 * 10**9):
        b2.add(e)
    b2.flush(dt.datetime(2026, 10, 18, 0, 0, 0, tzinfo=dt.timezone.utc))
    folder = proc / "trades" / SYM / "2026-10-17"
    assert len(list(folder.glob("*.parquet"))) == 2
    # strict replay of that day must NOT raise, and time must never go backwards
    ts = [e.ts_ns for e in L.replay(DAY, DAY, SYM, streams=["trades"], source="live", strict=True, cfg=cfg)]
    assert len(ts) == 6 and ts == sorted(ts)
    assert [e.trade_id for e in L.replay(DAY, DAY, SYM, streams=["trades"], source="live", cfg=cfg)] == [0, 1, 2, 10, 11, 12]


def test_old_style_pc_clock_file_names_still_sort_by_exchange_time(cfg):
    # Recordings made before the naming fix: the file named part-000000 (PC midnight) holds the
    # LATEST data, yet name order puts it first. find_files must order by min exchange ts instead.
    late = trade_frame([T0 + 86_399 * 10**9, T0 + 86_399 * 10**9 + 500 * MS])
    early = trade_frame([T0 + 1000 * 10**9, T0 + 1001 * 10**9])
    write_part(cfg, "trades", True, late, name="part-000000-0001")
    write_part(cfg, "trades", True, early, name="part-235000-0001")
    files = L.find_files("trades", DAY, DAY, SYM, "live", cfg=cfg)
    assert [f.name for f in files] == ["part-235000-0001.parquet", "part-000000-0001.parquet"]
    ts = [e.ts_ns for e in L.replay(DAY, DAY, SYM, streams=["trades"], source="live", strict=True, cfg=cfg)]
    assert ts == sorted(ts) and len(ts) == 4

