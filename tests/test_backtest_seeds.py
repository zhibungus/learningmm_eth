"""Random streams (common random numbers), multi-seed aggregation and the noise-aware cross-fill warning."""
import pytest

from mm.backtest.config import DEFAULTS, deep_merge
from mm.backtest.engine import build_engine
from mm.backtest.report import aggregate, cross_fill_warning
from mm.backtest.strategy import PlaceOrder, Strategy
from mm.data.events import QuoteEvent

T0 = 1_700_000_000_000_000_000


# Two toy strategies that see the same quotes. Both record how late each quote reached them (the feed
# delay). Quiet sends nothing; Chatty sends a far-away resting order on every quote (so it draws an
# order-latency jitter number every time).
class Quiet(Strategy):
    name = "quiet"
    event_kinds = ("quote",)

    def on_start(self, state):
        self.seen = []
        return []

    def on_event(self, ev, state):
        self.seen.append(state.now_ns - ev.ts_ns)
        return []


class Chatty(Quiet):
    name = "chatty"

    def on_event(self, ev, state):
        super().on_event(ev, state)
        return [PlaceOrder(self.new_id(), "buy", 0.1, 1000.0)]


def _delays(strategy_cls, seed):
    cfg = deep_merge(DEFAULTS, {"latency": {"timer_interval_ms": 0}})     # default: empirical feed-delay table
    eng = build_engine(cfg, seed=seed, strategy_cls=strategy_cls)
    for i in range(3000):                                                  # 150 s = many 2 s delay windows
        eng.on_market_event(QuoteEvent(T0 + i * 50_000_000, None, None, i, 2000.0, 3.0, 2000.01, 3.0))
    eng.finish()
    return eng.strategy.seen


def test_feed_delay_path_does_not_depend_on_how_many_orders_a_strategy_sends():
    quiet, chatty = _delays(Quiet, 5), _delays(Chatty, 5)
    assert len(quiet) > 1000 and len(set(quiet)) > 5      # the delay really varies (not a constant)
    assert quiet == chatty                                 # ...and is identical for both strategies
    assert _delays(Quiet, 6) != quiet                      # a different seed gives a different path


# ---------------------------------------------------------------- aggregation across seeds
def _h(net, killed=False, q=0.01):
    return {"net_pnl": net, "fills": 10, "killed": killed, "mean_abs_q": q, "time_at_limit_pct": 1.0,
            "mechanisms": {"queue": [1.0, 0.5]}}


def test_aggregate_gives_mean_sd_kills_and_keeps_per_seed_values():
    a = aggregate({42: _h(-3.0), 1: _h(-5.0, killed=True), 2: _h(-4.0)})
    assert a["n"] == 3 and a["seeds"] == [42, 1, 2]
    assert a["mean"]["net_pnl"] == pytest.approx(-4.0) and a["sd"]["net_pnl"] == pytest.approx(1.0)
    assert (a["kills"], a["kills_of"]) == (1, 3)
    assert a["per_seed"][42]["net_pnl"] == -3.0
    assert a["mean"]["mechanisms"]["queue"] == [1.0, 0.5]
    one = aggregate({42: _h(-3.0)})
    assert one["sd"]["net_pnl"] is None and one["mean"]["sharpe"] is None      # one seed: no sd; missing key: None


def test_aggregate_kills_unknown_without_a_strategy_summary():
    assert aggregate({1: _h(0.0, killed=None)})["kills"] is None


# ---------------------------------------------------------------- N6: cross-fill warning with a noise band
def test_cross_fill_warning_needs_gap_larger_than_seed_noise():
    base = aggregate({1: _h(-3.0), 2: _h(-3.5), 3: _h(-2.5)})                # mean -3.0, sd 0.5
    assert cross_fill_warning("p", base, aggregate({1: _h(-3.2)})) is None   # gap 0.2 < sd 0.5: noise
    assert cross_fill_warning("p", base, aggregate({1: _h(-2.0)})) is None   # base is worse: fine
    w = cross_fill_warning("p", base, aggregate({1: _h(-4.0)}))              # gap 1.0 > sd 0.5
    assert w and "WARNING" in w and "single seed" not in w


def test_cross_fill_warning_with_one_seed_warns_and_says_noise_is_unknown():
    w = cross_fill_warning("p", aggregate({1: _h(-3.0)}), aggregate({1: _h(-3.1)}))
    assert w and "single seed, noise not estimated" in w


# ---------------------------------------------------------------- orchestration of the seed jobs
def test_seeds_for_defaults_and_overrides():
    from mm.backtest.run import seeds_for
    cfg = deep_merge(DEFAULTS, {"run": {"seed": 42}, "sensitivity": {"seeds": [1, 42, 2]}})
    variants = {"a": {}, "b": {"seeds": [7, 8, 7]}}
    assert seeds_for(cfg, variants, True) == {"base": [42, 1, 2], "a": [42], "b": [7, 8]}
    assert seeds_for(cfg, variants, False)["base"] == [42]                   # sensitivity off: base runs once


def test_run_all_runs_one_job_per_variant_period_seed(tmp_path, monkeypatch):
    import mm.backtest.run as runmod
    ran = []

    def fake_run_period(cfg, period, seed, verbose=True, manifest=False):
        ran.append((cfg["fill_model"]["cross_fills"], seed, manifest))
        eng = build_engine(deep_merge(DEFAULTS, {}), seed=1)
        m, fills, eq = eng.finish()
        return m, fills, eq, {"start": "x", "end": "y", "events": 0, "wall_seconds": 0.0}
    captured = {}
    monkeypatch.setattr(runmod, "run_period", fake_run_period)
    monkeypatch.setattr(runmod, "write_report",
                        lambda out, cfg, raw, res, sens, meta, title: captured.update(res=res, sens=sens, meta=meta) or out)
    cfg = deep_merge(DEFAULTS, {"run": {"seed": 42}, "data": {"periods": {"p": {"start": "2024-03-29", "end": "2024-03-29"}}},
                                "sensitivity": {"seeds": [42, 1], "variants": {"no_cross_fills": {"seeds": [42, 1, 2]}}}})
    cfg["sensitivity"]["variants"] = {"no_cross_fills": cfg["sensitivity"]["variants"]["no_cross_fills"]}
    runmod.run_all(cfg, None, out_root=tmp_path)
    assert ran == [(True, 42, True), (True, 1, False), (False, 42, False), (False, 1, False), (False, 2, False)]
    assert captured["meta"]["replay_jobs"] == 5
    assert captured["res"]["p"]["seed_stats"]["n"] == 2 and captured["sens"]["no_cross_fills"]["p"]["n"] == 3
