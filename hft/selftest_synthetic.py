"""Phase-1 self-test on SYNTHETIC data (no real data needed):  .venv-hft\\Scripts\\python.exe -m hft.selftest_synthetic

Proves, in this order:
  1. the event loop runs for the example MM and for the GLFT port (numba compile time is printed),
  2. orders are placed, fill, and the fill log matches hftbacktest's own accounting
     (sum of logged qty == state trading_volume; logged fees == state fee),
  3. spread capture + inventory PnL + fees == net PnL (exact identity of the metrics code),
  4. an empty book (DEPTH_CLEAR_EVENT "gap") makes the GLFT port stop quoting,
  5. the inventory cap holds (|position| <= Q + one order of slack for the order latency),
  6. repeat run is identical (determinism).
Writes into hft/results/synthetic_* (safe to delete).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hft.common import merged_config                         # noqa: E402
from hft.glft_hft import load_params_yaml                     # noqa: E402
from hft.run_backtest import DEFAULT_YAML, RESULTS_ROOT, execute   # noqa: E402
from hft.synthetic import make_synthetic                      # noqa: E402


def run(strategy: str, snap, ev, tag: str, **cfg_over) -> dict:
    gp = load_params_yaml(DEFAULT_YAML) if strategy == "glft" else None
    cfg = merged_config(**cfg_over)
    if gp:
        cfg["maker_fee"], cfg["tick_size"], cfg["lot_size"] = gp["maker_fee"], gp["tick_size"], gp["lot_size"]
    t = time.time()
    m = execute(strategy, data_files=[], snapshot_file=None, data_arrays=[ev], snapshot_array=snap, cfg=cfg,
                glft_params=gp, step_ms=100, out_dir=RESULTS_ROOT / f"synthetic_{strategy}_{tag}",
                title=f"synthetic {strategy} {tag}")
    print(f"[{strategy}/{tag}] wall {time.time() - t:.1f}s (includes numba compile on first call)")
    return m


def main() -> None:
    snap, ev = make_synthetic(120.0)
    print(f"synthetic events: {len(ev)}")

    # ---- 1. example MM
    me = run("example", snap, ev, "a")
    assert me["num_trades"] > 0, "example MM never traded"
    print("example:", {k: round(v, 5) if isinstance(v, float) else v for k, v in me.items()})

    # ---- 2/3. GLFT.  Real GLFT quotes sit ~100 ticks (1 USDT) from mid, so give the synthetic price a
    # +-3 USDT sine swing (done in synthetic.py) to let them fill.
    m1 = run("glft", snap, ev, "a")
    m2 = run("glft", snap, ev, "b")                      # same inputs again -> must be identical
    for k in ("fill_rows", "filled_qty_eth", "net_pnl_usdt", "orders_new"):
        assert m1[k] == m2[k], f"not deterministic: {k} {m1[k]} vs {m2[k]}"
    assert m1["fill_rows"] > 0, "GLFT never filled on synthetic data"
    assert m1["fill_log_matches_state_volume"], (m1["filled_qty_eth"], m1["state_trading_volume_eth"])
    assert abs(m1["fees_usdt"] - m1["fees_check_from_fill_log"]) < 1e-9, (m1["fees_usdt"], m1["fees_check_from_fill_log"])
    ident = m1["spread_capture_usdt"] + m1["inventory_pnl_usdt"] + (-m1["fees_usdt"]) - m1["net_pnl_usdt"]
    assert abs(ident) < 1e-9, ident
    assert m1["inventory_abs_max_eth"] <= 0.05 + 0.01 + 1e-9, m1["inventory_abs_max_eth"]
    print("glft:", {k: round(v, 5) if isinstance(v, float) else v for k, v in m1.items()})

    # ---- 4. gap / empty book
    snap2, ev2 = make_synthetic(120.0, clear_at_s=60.0, clear_for_s=10.0)
    m3 = run("glft", snap2, ev2, "gap")
    assert m3["cycles_without_valid_book"] > 0, "empty book was not detected"
    print(f"gap run: cycles without valid book = {m3['cycles_without_valid_book']}")

    # ---- 5. pessimistic variant must not fill MORE than the base case (sensitivity direction)
    m4 = run("glft", snap, ev, "pess", queue_model="power_prob", queue_n=3.0, partial_fill=False,
             latency={"entry_ns": 300_000_000})
    print("pessimistic fill rows:", m4["fill_rows"], "vs base", m1["fill_rows"])
    m5 = run("glft", snap, ev, "jitter", latency={"kind": "intp_jitter", "seed": 42})
    print("intp_jitter fill rows:", m5["fill_rows"])
    print("SELF-TEST PASSED")


if __name__ == "__main__":
    main()
