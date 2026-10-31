"""Accounting identity, PnL decomposition, determinism and an end-to-end run on synthetic parquet data."""
# pytest runs every test_* function; a failed "assert" fails the test. pytest.approx compares floats
# within a tiny tolerance (floating-point arithmetic is never exact).
import copy
import datetime as dt

import polars as pl
import pytest

from mm.backtest.config import DEFAULTS, deep_merge
from mm.backtest.engine import build_engine
from mm.backtest.metrics import Account
from mm.backtest.strategy import Fill, PlaceOrder, Strategy
from mm.data.events import FundingEvent, QuoteEvent

T0 = 1_700_000_000_000_000_000


# Shortcut to build a Fill record for the accounting tests ("ref" = mid just before the fill).
def _fill(side, px, qty, ref, fee=0.0):
    return Fill(T0, 1, "x", side, px, qty, "maker", fee, 0.0, ref)


# The accounting identity: cash + position*mid must always equal starting cash + realized +
# unrealized - fees - funding. If bookkeeping ever double-counts or drops money, this breaks. We walk
# through opening, adding, flipping long->short, and closing, with fees and a funding payment, and
# check after EVERY step. Also: net PnL = spread capture + inventory PnL - fees - funding.
def test_identity_through_open_flip_close_with_fees_and_funding():
    a = Account(1000.0)
    a.on_mid(100.0)
    steps = [("buy", 99.9, 2.0, 100.0), ("mid", 101.0), ("buy", 101.0, 1.0, 101.0), ("mid", 102.0),
             ("sell", 102.0, 5.0, 102.0),                      # closes 3.0 and flips to short 2.0
             ("mid", 100.5), ("fund", 0.0001, 100.5), ("buy", 100.5, 2.0, 100.5), ("mid", 99.0)]
    # Each step is a tuple; its first item says what kind of step it is. s[1], s[2]... index into the tuple.
    for s in steps:
        if s[0] == "mid":
            a.on_mid(s[1])
        elif s[0] == "fund":
            a.on_funding(s[1], s[2])
        else:
            a.on_fill(_fill(s[0], s[1], s[2], s[3], fee=0.01 * s[2]))
        assert a.identity_gap() == pytest.approx(0.0, abs=1e-9)
    assert a.position == pytest.approx(0.0)
    assert a.unrealized == 0.0
    # gross = spread capture + inventory pnl, net = gross - fees - funding
    net = a.equity - a.start_cash
    assert net == pytest.approx(a.spread_capture + a.inventory_pnl - a.fees - a.funding)
    assert net == pytest.approx(a.realized - a.fees - a.funding)


# A deliberately random strategy: 20% of the time it fires a market order (random side and size) using
# self.rng. It is not a trading idea; it just generates many fills so determinism and accounting can
# be tested over a long run. Market orders make every fill a taker fill.
class Churn(Strategy):
    """Buys and sells in a pattern driven by self.rng: exercises determinism of the seed plumbing."""
    name = "churn"
    event_kinds = ("quote",)

    def on_event(self, ev, state):
        if state.mid is None or self.rng.random() > 0.2:
            return []
        side = "buy" if self.rng.random() < 0.5 else "sell"
        return [PlaceOrder(self.new_id(), side, 0.01 * (1 + int(self.rng.integers(0, 3))), None)]  # market


# Build synthetic data: a random-walk quote stream (one tick wide spread) with a funding event every
# ~97 quotes. The data generator itself uses a FIXED seed (7) so tests never change between runs.
# "import numpy as np" sits inside the function only because this is the only place it is needed.
def _events(n=2000):
    import numpy as np
    r = np.random.default_rng(7)
    px, out = 2000.0, []
    for i in range(n):
        px = max(1.0, round(px + r.choice([-0.01, 0.0, 0.01]), 2))
        out.append(QuoteEvent(T0 + i * 50_000_000, None, None, i, px, 3.0, round(px + 0.01, 2), 3.0))
        if i % 97 == 0:
            out.append(FundingEvent(T0 + i * 50_000_000 + 1, 0.0001, px, 8))
    return out


# Run the whole engine on the synthetic data with latency jitter ON (so the seed really matters).
# Returns the engine plus the metrics, fills table and equity table.
def _run(seed):
    cfg = deep_merge(DEFAULTS, {"latency": {"feed_latency_ms": 5, "order_latency_ms": 10, "order_jitter_ms": 3, "feed_delay_quantiles_ms": None,
                                            "timer_interval_ms": 0}})
    eng = build_engine(cfg, seed=seed, strategy_cls=Churn)
    for e in _events():
        eng.on_market_event(e)
    m, fills, eq = eng.finish()
    return eng, m, fills, eq


# The identity from above, but over thousands of random events instead of a hand-made sequence.
# The polars expression pl.col("cash") + ... builds a computed column; .abs() takes absolute value;
# .drop_nans() skips rows where the mid was not yet known.
def test_accounting_identity_holds_over_a_whole_run():
    eng, m, fills, eq = _run(5)
    assert m["fills"]["count"] > 20
    assert eng.account.identity_gap() == pytest.approx(0.0, abs=1e-6)
    assert m["identity_gap"] == pytest.approx(0.0, abs=1e-6)
    p = m["pnl"]
    assert p["check_gross_minus_costs_minus_net"] == pytest.approx(0.0, abs=1e-6)
    # every sampled equity point obeys equity == cash + pos*mid
    chk = eq.with_columns((pl.col("cash") + pl.col("position") * pl.col("mid") - pl.col("equity")).abs().alias("gap"))
    assert chk["gap"].drop_nans().max() < 1e-6
    # all fills are takers (market orders) so the sum of fees matches the taker rate
    assert p["fees"] == pytest.approx(m["fills"]["traded_notional"] * 0.0005)


# Determinism: reproducible research requires that the same seed gives bit-identical results, and a
# different seed (different jitter and random orders) gives something different.
def test_same_seed_gives_identical_results_and_different_seed_differs():
    # "_" names a return value we deliberately ignore (here, the engine object).
    _, m1, f1, e1 = _run(11)
    _, m2, f2, e2 = _run(11)
    _, m3, f3, _ = _run(12)
    assert f1.equals(f2) and e1.equals(e2) and m1 == m2
    assert not f1.equals(f3)


# Markouts compare each fill with the mid price N seconds LATER, a measure of adverse selection. Here
# we only check that the columns exist and are filled for fills that have enough data after them.
def test_markouts_use_future_mid():
    eng, m, fills, _ = _run(5)
    assert "1s" in m["markouts"] and m["markouts"]["1s"]["n_fills"] > 0
    row = fills.row(0, named=True)
    assert row["markout_1s"] is not None


# End to end. "tmp_path" and "monkeypatch" are pytest FIXTURES: pytest sees the parameter names and
# supplies them automatically. tmp_path is a fresh temporary folder (deleted afterwards);
# monkeypatch lets a test temporarily replace something (here the replay function, so it reads our
# synthetic files instead of the real data folder) and undoes it when the test ends.
def test_end_to_end_backtest_on_synthetic_parquet(tmp_path, monkeypatch):
    """Full path: parquet files -> replay -> engine -> report folder."""
    from mm.backtest.config import load_config
    from mm.backtest import run as runmod
    from mm.data import loaders
    from mm.data import schemas as S
    from mm.data.config import load_config as load_data_cfg

    day = dt.date(2024, 3, 29)
    t0 = int(dt.datetime(2024, 3, 29, tzinfo=dt.timezone.utc).timestamp()) * 10**9
    dcfg = copy.deepcopy(load_data_cfg())
    dcfg["paths"]["processed_dir"] = str(tmp_path)
    dcfg["paths"]["archive_name"] = "arc"
    root = tmp_path / "arc"

    def put(folder, df):
        d = root / folder / "ETHUSDT" / day.isoformat()
        d.mkdir(parents=True)
        df.write_parquet(d / "part-0.parquet")

    n = 4000
    ts = [t0 + i * 100_000_000 for i in range(n)]
    px = [2000.0 + 0.01 * ((i // 7) % 5) for i in range(n)]
    put("quotes", S.conform(pl.DataFrame({"ts_ns": ts, "update_id": list(range(n)), "bid_px": px, "bid_qty": [2.0] * n,
                                "ask_px": [p + 0.01 for p in px], "ask_qty": [2.0] * n}), "quotes"))
    put("agg_trades", S.conform(pl.DataFrame({"ts_ns": [t + 1 for t in ts], "trade_id": list(range(n)),
                                    "first_id": list(range(n)), "last_id": list(range(n)),
                                    "price": [p - 0.01 * (i % 3) for i, p in enumerate(px)], "qty": [1.0] * n,
                                    "side": ["sell" if i % 2 else "buy" for i in range(n)]}), "trades"))
    put("funding", S.conform(pl.DataFrame({"ts_ns": [t0 + 5 * 10**9], "rate": [0.0001], "mark_price": [2000.0],
                                 "interval_hours": [8]}), "funding"))
    # Wrap the real replay so it always receives our temporary data config. "lambda *a, **k: f(*a, ...)"
    # is a one-line function that accepts any positional (*a) and keyword (**k) arguments and passes
    # them through unchanged.
    real_replay = loaders.replay
    monkeypatch.setattr(runmod, "replay", lambda *a, **k: real_replay(*a, cfg=dcfg, **k))

    ycfg = tmp_path / "c.yaml"
    ycfg.write_text("run: {tag: t, seed: 3}\ndata:\n  periods:\n    in_sample: {start: 2024-03-29, end: 2024-03-29}\n"
                    "    out_of_sample: {start: 2024-03-29, end: 2024-03-29}\n"
                    "strategy: {name: demo_quoter, params: {half_spread_ticks: 1, order_qty: 0.05}}\n"
                    "metrics: {inventory_limit: 0.5}\nsensitivity: {enabled: true, variants: {slow: "
                    "{latency: {order_latency_ms: 200}}}}\n", encoding="utf-8")
    cfg = load_config(ycfg)
    out = runmod.run_all(cfg, ycfg.read_text(), out_root=tmp_path / "reports")
    for f in ("config.yaml", "config.json", "metrics.json", "fills.parquet", "equity.parquet", "summary.md",
              "equity_inventory.png", "markouts.png"):
        assert (out / f).exists(), f
    import json
    m = json.loads((out / "metrics.json").read_text())
    assert m["seed"] == 3 and set(m["periods"]) == {"in_sample", "out_of_sample"} and "slow" in m["sensitivity"]
    assert m["periods"]["in_sample"]["fills"]["count"] > 0
    assert m["periods"]["in_sample"] == m["periods"]["out_of_sample"]   # same data + seed -> same result
    assert abs(m["periods"]["in_sample"]["identity_gap"]) < 1e-6


# Edge case: no market data at all must still yield valid (empty) metrics, not a crash.
def test_empty_run_produces_metrics_without_crashing():
    cfg = deep_merge(DEFAULTS, {})
    eng = build_engine(cfg, seed=1, strategy_cls=Churn)
    m, fills, eq = eng.finish()
    assert m["fills"]["count"] == 0 and fills.height == 0 and m["pnl"]["total_net"] == 0.0
