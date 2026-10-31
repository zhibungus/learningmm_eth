"""Unit check: the numba GLFT maths in hft/glft_hft.py must equal src/mm/strategies/glft.py to 1e-9.

Run from the project root with the hftbacktest venv:
    .venv-hft\\Scripts\\python.exe hft\\check_glft_math.py

It imports OUR pure functions (``glft.closed_form``, ``glft.compute_quotes``) and compares them with the
njit versions on a grid of (mid, q), for three parameter sets:
  * the spec section 10 worked example (placeholders, gamma = 1), also against the hand-checked numbers,
  * the calibrated live set from config/backtest/glft_live.yaml,
  * the "shift" fee mode and xi = 0 variants of the live set.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Make ``mm`` (in src/) and ``hft`` importable without installing anything.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mm.strategies import glft as ref                      # our reference implementation (pure Python)
from hft import glft_hft as nb                              # the numba port

TOL = 1e-9
TICK = 0.01


def to_dict(p: "ref.GlftParams", tick: float = TICK, lot: float = 0.001) -> dict:
    """GlftParams (dataclass) -> the flat dict that glft_hft.params_to_array expects."""
    d = {f: getattr(p, f) for f in p.__dataclass_fields__}
    d.update(tick_size=tick, lot_size=lot, timer_interval_ms=1000)
    return d


def compare(label: str, p: "ref.GlftParams", mids, qs) -> float:
    """Max abs difference over the grid for every output; raises AssertionError above TOL."""
    P = nb.params_to_array(to_dict(p))
    worst = 0.0
    for mid in mids:
        # reference closed form (dataclass) vs numba tuple (c, a, c1, c2, omega, h)
        cf = ref.closed_form(p, mid)
        c, a, c1, c2, omega, h = nb.closed_form(P, mid)
        worst = max(worst, abs(cf.c - c), abs(cf.a_use - a), abs(cf.c1 - c1), abs(cf.c2 - c2),
                    abs(cf.omega - omega), abs(cf.h - h))
        for q in qs:
            # book: touch one tick around mid (best_bid/ask only enter through the no-cross guard)
            bb = round(np.floor(mid / TICK) * TICK, 10)
            ba = round(bb + TICK, 10)
            qt = ref.compute_quotes(p, mid, q, bb, ba, TICK)
            bid, ask, db, da, om, hh = nb.compute_quotes(P, mid, q, bb, ba)
            # prices are on a 0.01 grid, so they must match EXACTLY (not just to 1e-9)
            assert abs(qt.bid_px - bid) < TOL and abs(qt.ask_px - ask) < TOL, (label, mid, q, qt, bid, ask)
            worst = max(worst, abs(qt.delta_b - db), abs(qt.delta_a - da), abs(qt.bid_px - bid),
                        abs(qt.ask_px - ask))
    assert worst < TOL, f"{label}: max diff {worst:.3e} > {TOL}"
    print(f"OK  {label:<34} grid {len(mids)}x{len(qs)}  max|diff| = {worst:.2e}")
    return worst


def main() -> None:
    qs = np.round(np.arange(-0.08, 0.0801, 0.01), 10)           # beyond +-Q = 0.05 on purpose
    mids = np.round(np.arange(1800.005, 3600.0, 123.455), 3)

    # ---- 1. spec section 10 hand-checked example (placeholders, gamma = xi = 1)
    ex = ref.GlftParams(sigma=0.26, A=4.0, k=10.0, gamma=1.0, xi_equals_gamma=True, fee_mode="exact")
    compare("spec s10 worked example", ex, np.array([2400.005]), qs)
    P = nb.params_to_array(to_dict(ex))
    c, a, c1, c2, omega, h = nb.closed_form(P, 2400.005)
    # hand numbers from the spec (6 significant digits as printed there)
    for name, got, want, tol in (("c", c, 0.480001, 1e-6), ("c1", c1, 0.0999500, 1e-7), ("A_eff", a, 0.0329187, 1e-7),
                                 ("c2", c2, 20.3245, 1e-4), ("omega", omega, 5.28437, 1e-5), ("h", h, 0.606373, 1e-6)):
        assert abs(got - want) < tol, (name, got, want)
    print("OK  spec s10 hand numbers (c, c1, A_eff, c2, omega, h) reproduced")
    for q, want_bid, want_ask in ((0.0, 2399.39, 2400.62), (0.05, None, 2400.35), (-0.05, 2399.66, None),
                                  (0.04, 2399.18, 2400.40)):
        bid, ask, *_ = nb.compute_quotes(P, 2400.005, q, 2400.00, 2400.01)
        if want_bid is not None:
            assert abs(bid - want_bid) < 1e-9, (q, bid, want_bid)
        if want_ask is not None:
            assert abs(ask - want_ask) < 1e-9, (q, ask, want_ask)
    print("OK  spec s10 quote table (q = 0, +-Q, +0.04) reproduced exactly")

    # ---- 2. calibrated live parameters, read from the YAML through our own loader
    live = nb.load_params_yaml(ROOT / "config" / "backtest" / "glft_live.yaml")
    live_p = ref.GlftParams.from_dict({k: v for k, v in live.items()
                                       if k in ref.GlftParams.__dataclass_fields__})
    compare("live calibrated (exact, xi=gamma)", live_p, mids, qs)
    compare("live, fee_mode=shift", replace(live_p, fee_mode="shift"), mids, qs)
    compare("live, xi=0 limit", replace(live_p, xi_equals_gamma=False), mids, qs)
    compare("live, skew_off", replace(live_p, skew_off=True), mids, qs)
    print(f"live params loaded from {live['source']}: sigma={live['sigma']} A={live['A']} k={live['k']} gamma={live['gamma']}")
    print("ALL GLFT MATH CHECKS PASSED")


if __name__ == "__main__":
    main()
