"""Warm start: events before the window build the books but never reach the strategy or the metrics."""
import copy
import datetime as dt

import polars as pl
import pytest

from mm.backtest import run as runmod
from mm.backtest.config import DEFAULTS, deep_merge
from mm.backtest.engine import build_engine
from mm.backtest.strategy import Strategy
from mm.data import loaders
from mm.data import schemas as S
from mm.data.config import load_config as load_data_cfg
from mm.data.config import processed_dir
from mm.data.events import DepthEvent, GapEvent, QuoteEvent, TradeEvent

T0 = 1_700_000_000_000_000_000
SEC = 10**9


def _snap(ts, bid_q=5.0):
    return DepthEvent(ts, None, None, True, 1, 1, None, ((2000.00, bid_q),), ((2000.01, 5.0),))


def _diff(ts, bid_q, fu=2):
    return DepthEvent(ts, None, None, False, fu, fu, fu - 1, ((2000.00, bid_q),), ())


# A strategy that only records what reaches it (a stand-in for "the strategy must not see warm-up").
class Spy(Strategy):
    name = "spy"
    event_kinds = ("quote", "trade", "depth")

    def on_start(self, state):
        self.seen = []
        return []

    def on_event(self, ev, state):
        self.seen.append(ev.ts_ns)
        return []


def _l2_cfg():
    return deep_merge(DEFAULTS, {"fill_model": {"type": "l2"}, "latency": {"timer_interval_ms": 0}})


def test_warm_up_builds_the_books_and_touches_nothing_else():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    eng.warm_up(GapEvent(T0, "session_start", "depth"))
    eng.warm_up(_snap(T0 + SEC))
    eng.warm_up(_diff(T0 + 2 * SEC, 7.0))
    eng.warm_up(TradeEvent(T0 + 3 * SEC, None, None, 1, 1, 1, 2000.0, 1.0, "buy"))
    eng.warm_up(QuoteEvent(T0 + 3 * SEC, None, None, 1, 2000.0, 7.0, 2000.01, 5.0))
    eng.check_warm_start()                                    # synced: does not raise
    assert eng.ex.mkt.book.bids[2000.0] == 7.0                # exchange book is warm
    assert eng.ex.mkt.mid == pytest.approx(2000.005)
    assert eng.state.bid_px == 2000.0                         # strategy's price picture too
    # ...but nothing else moved: no strategy start, clock, recorder, fills, orders, account
    assert not eng._started and eng.n_events == 0 and eng.now == 0 and not eng._heap
    assert eng.rec.first_ts is None and eng.account.last_mid is None
    assert eng.ex.stats["submitted"] == 0 and not hasattr(eng.strategy, "seen")
    # a window event now starts the engine normally, on top of the warm book
    eng.on_market_event(_diff(T0 + 10 * SEC, 6.0, fu=3))
    assert eng._started and eng.ex.mkt.book.bids[2000.0] == 6.0 and eng.rec.first_ts == T0 + 10 * SEC


def test_queue_position_comes_from_the_warm_book():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    eng.warm_up(GapEvent(T0, "session_start", "depth"))
    eng.warm_up(_snap(T0 + SEC, bid_q=9.0))
    eng.check_warm_start()
    from mm.backtest.fills import Order
    o = Order(1, "x", "buy", 2000.0, 0.1, 0.1, True, "GTC", T0)
    eng.ex.model.on_new_resting(o, eng.ex.mkt)
    assert o.q_ahead == 9.0                                   # an empty book would give 0 here


def test_warm_up_without_a_snapshot_or_after_an_unrepaired_gap_is_refused():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    eng.warm_up(_diff(T0, 7.0))                               # diffs only: a partial book
    with pytest.raises(RuntimeError, match="no valid depth book"):
        eng.check_warm_start()
    eng.warm_up(_snap(T0 + SEC))
    eng.check_warm_start()
    eng.warm_up(GapEvent(T0 + 2 * SEC, "disconnect", "public"))   # book socket dropped, no snapshot yet
    assert eng.ex.mkt.bid_px is None and eng.state.bid_px is None
    with pytest.raises(RuntimeError, match="no valid depth book"):
        eng.check_warm_start()
    eng.warm_up(GapEvent(T0 + 3 * SEC, "session_start", "depth"))
    eng.warm_up(_snap(T0 + 3 * SEC))                          # resynced
    eng.check_warm_start()


def test_trades_socket_gap_does_not_unsync_the_book():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    eng.warm_up(_snap(T0))
    eng.warm_up(GapEvent(T0 + SEC, "disconnect", "market"))
    eng.check_warm_start()
    assert eng.ex.mkt.book.bids[2000.0] == 5.0


def test_warm_up_after_start_is_an_error():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    eng.on_market_event(_snap(T0))
    with pytest.raises(RuntimeError):
        eng.warm_up(_diff(T0 + SEC, 1.0))


def test_quote_models_need_no_snapshot():
    eng = build_engine(deep_merge(DEFAULTS, {"latency": {"timer_interval_ms": 0}}), seed=1, strategy_cls=Spy)
    eng.warm_up(QuoteEvent(T0, None, None, 1, 2000.0, 1.0, 2000.01, 1.0))
    eng.check_warm_start()                                    # default fill model is quote_trade: no depth needed
    assert eng.ex.mkt.has_quote is False or eng.ex.mkt.mid is not None


# ---- period settings ---------------------------------------------------------------------------
def test_warmup_start_ns_options():
    base = {"start": "2026-10-24", "end": "2026-10-24"}
    lo, _ = runmod.period_window_ns(base)
    assert runmod.warmup_start_ns(base) is None
    assert runmod.warmup_start_ns({**base, "warmup_hours": 2}) == lo - 7200 * SEC
    want = int(dt.datetime(2026, 10, 23, 19, 6, 24, tzinfo=dt.timezone.utc).timestamp()) * SEC
    assert runmod.warmup_start_ns({**base, "warmup_from": "2026-10-23 19:06:24"}) == want
    assert runmod.warmup_start_ns({**base, "warmup_from": dt.datetime(2026, 10, 23, 19, 6, 24)}) == want   # YAML datetime
    # a fraction of a second >= .5 must be counted once (it used to be counted twice, 1 s late)
    frac = runmod.warmup_start_ns({**base, "warmup_from": "2026-10-23 19:06:24.874"})
    assert frac == want + 874_000_000
    assert runmod.warmup_start_ns({**base, "warmup_from": "2026-10-23 19:06:24.250"}) == want + 250_000_000
    with pytest.raises(ValueError):
        runmod.warmup_start_ns({**base, "warmup_from": "2026-10-23 19:00:00", "warmup_hours": 1})
    with pytest.raises(ValueError):                           # causality: warm-up cannot reach into the window
        runmod.warmup_start_ns({**base, "warmup_from": "2026-10-24 00:00:00"})


# ---- end to end through run_period with real loaders on synthetic live files -------------------
def _depth_rows(msgs):
    rows = []
    for ts, snap, fu, lu, pu, bq in msgs:
        rows.append((ts, None, None, "bid", 2000.0, bq, snap, fu, lu, pu))
        rows.append((ts, None, None, "ask", 2000.01, 5.0, snap, fu, lu, pu))
    return pl.DataFrame(rows, schema=S.DEPTH_SCHEMA, orient="row")


@pytest.fixture
def live_two_days(tmp_path, monkeypatch):
    """Day 1 has a snapshot at 23:00 and a diff at 23:30. Day 2 has diffs only (like a live 00:00)."""
    d1, d2 = dt.date(2026, 10, 17), dt.date(2026, 10, 18)
    mid1 = int(dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc).timestamp()) * SEC
    mid2 = mid1 + 86400 * SEC
    dcfg = copy.deepcopy(load_data_cfg())
    dcfg["paths"]["processed_dir"] = str(tmp_path / "proc")
    dcfg["paths"]["raw_dir"] = str(tmp_path / "raw")

    def put(folder, day, df):
        p = processed_dir(dcfg, live=True) / folder / "ETHUSDT" / day.isoformat()
        p.mkdir(parents=True, exist_ok=True)
        df.write_parquet(p / "part-0.parquet")

    h = 3600 * SEC
    put("depth", d1, _depth_rows([(mid1 + 23 * h, True, 10, 10, None, 5.0), (mid1 + 23 * h + 1800 * SEC, False, 11, 11, 10, 7.0)]))
    put("depth", d2, _depth_rows([(mid2 + 600 * SEC, False, 12, 12, 11, 3.0)]))
    trades = lambda tss: S.conform(pl.DataFrame({"ts_ns": tss, "trade_id": list(range(len(tss))), "first_id": list(range(len(tss))),
                                                 "last_id": list(range(len(tss))), "price": [2000.0] * len(tss),
                                                 "qty": [1.0] * len(tss), "side": ["sell"] * len(tss)}), "trades")
    put("trades", d1, trades([mid1 + 23 * h + 600 * SEC]))
    put("trades", d2, trades([mid2 + 1200 * SEC]))
    real = loaders.replay
    monkeypatch.setattr(runmod, "replay", lambda *a, **k: real(*a, cfg=dcfg, **k))
    made = []

    def fake_build(cfg, seed):
        made.append(build_engine(cfg, seed, strategy_cls=Spy))
        return made[-1]
    monkeypatch.setattr(runmod, "build_engine", fake_build)
    cfg = deep_merge(DEFAULTS, {"fill_model": {"type": "l2"}, "latency": {"timer_interval_ms": 0},
                                "data": {"source": "live", "streams": ["trades", "depth"]}})
    return cfg, made, mid2


def test_run_period_warm_start_end_to_end(live_two_days):
    cfg, made, mid2 = live_two_days
    period = {"start": "2026-10-18", "end": "2026-10-18", "warmup_from": "2026-10-17 23:00:00"}
    m, fills, eq, info = runmod.run_period(cfg, period, seed=1, verbose=False)
    eng = made[0]
    assert eng.ex.mkt.book.bids[2000.0] == 3.0                # day-1 snapshot + diff + day-2 diff, in order
    assert info["warmup_events"] > 0 and info["events"] == 2      # window events only: a diff and a trade
    assert info["warmup_last_snapshot_ns"] is not None
    assert min(eng.strategy.seen) >= mid2                     # the strategy never saw a warm-up event
    assert info["first_ts_ns"] >= mid2                        # and the metrics start at the window


def test_depth_model_rejects_orders_until_the_first_snapshot():
    eng = build_engine(_l2_cfg(), seed=1, strategy_cls=Spy)
    assert eng.ex.awaiting_resync                             # a depth model starts un-synced
    eng.ex.on_market_event(_diff(T0, 7.0), T0)                # a diff alone does not sync it
    assert eng.ex.awaiting_resync
    eng.ex.on_market_event(_snap(T0 + SEC), T0 + SEC)
    assert not eng.ex.awaiting_resync


def test_quote_model_starts_ready():
    eng = build_engine(deep_merge(DEFAULTS, {"latency": {"timer_interval_ms": 0}}), seed=1, strategy_cls=Spy)
    assert not eng.ex.awaiting_resync


def test_run_period_without_warm_up_waits_for_a_snapshot_and_too_short_warm_up_is_refused(live_two_days):
    cfg, made, mid2 = live_two_days
    plain = {"start": "2026-10-18", "end": "2026-10-18"}
    info = runmod.run_period(cfg, plain, seed=1, verbose=False)[3]
    assert info["warmup_events"] == 0 and info["warmup_from_ns"] is None   # no warm-up: only the lone day-2 diff
    assert made[0].ex.awaiting_resync                         # ...and no snapshot came, so orders stay rejected
    short = {"start": "2026-10-18", "end": "2026-10-18", "warmup_hours": 0.5}   # 23:30..00:00 holds only a diff
    with pytest.raises(RuntimeError, match="no valid depth book"):
        runmod.run_period(cfg, short, seed=1, verbose=False)
    ok = {"start": "2026-10-18", "end": "2026-10-18", "warmup_hours": 1}        # reaches the 23:00 snapshot
    info = runmod.run_period(cfg, ok, seed=1, verbose=False)[3]
    assert info["warmup_events"] > 0 and info["warmup_last_snapshot_ns"] is not None


def test_replay_from_ns_starts_inside_the_first_day(live_two_days):
    cfg, made, mid2 = live_two_days
    mid1 = mid2 - 86400 * SEC
    kw = dict(symbol="ETHUSDT", streams=["trades", "depth"], source="live")
    allv = [e.ts_ns for e in runmod.replay("2026-10-17", "2026-10-18", **kw)]
    cut = mid1 + 23 * 3600 * SEC + 900 * SEC                  # 23:15: after the snapshot and the first trade
    late = [e.ts_ns for e in runmod.replay("2026-10-17", "2026-10-18", from_ns=cut, **kw)]
    assert late == [t for t in allv if t >= cut] and 0 < len(late) < len(allv)
