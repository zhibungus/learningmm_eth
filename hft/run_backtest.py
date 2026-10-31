"""CLI: run the GLFT port or the example market maker on hftbacktest and write a result folder.

    # real data: hourly files made by hft/convert.py, inclusive hour range (UTC)
    .venv-hft\\Scripts\\python.exe -m hft.run_backtest --strategy glft --start 2026-10-24T00 --end 2026-10-24T23 --tag full_base

Named variants of the comparison (risk_adverse queue and the NoPartialFill exchange unless stated):
    base          no flags                       order latency entry 160 ms / response 240 ms
    pess_latency  --entry-ms 300                 only the latency is more pessimistic
    queue_prob3   --queue-model power_prob --queue-n 3
                  power_prob(3) also advances our queue position on cancels, so it is MORE optimistic than
                  risk_adverse (trades only).  Use it as a comparison point towards our engine's
                  cancel_assumption=proportional, NOT as a pessimistic case.
    --partial-fill selects PartialFillExchange.  In 2.4.4 it makes hbt.elapse return error 14 after 25-60% of
                  a day on our data, so the run is INCOMPLETE: exit code 2 and a banner in summary.md.
    --latency-kind intp_jitter   seeded 132 + U(0,59) ms entry-latency table (seed = --seed)
    --requote-mode cancel_new    cancel then place a new order instead of modify (default: modify)
    # synthetic smoke test (no data needed)
    .venv-hft\\Scripts\\python.exe -m hft.run_backtest --strategy glft --synthetic --seconds 120 --tag synth

Output: hft/results/<first data date>_<strategy>_<tag>/
    config.json   everything needed to reproduce: strategy params (with the YAML they came from), exchange model
                  (queue model, partial fills, fees, latency), data files, seed
    metrics.json  comparison metrics (see hft/postprocess.py for definitions)
    fills.csv     one row per fill: exch_ts, poll_ts, order_id, side, px, qty, mid_before, markouts
    summary.md    short human summary + the assumptions that limit realism
    equity.png    equity curve + inventory + mid
Run from the project root (the folder that contains CLAUDE.md).  A full day takes about 15-25 s.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # so ``import hft.xxx`` works however the script is started

from hftbacktest import ROIVectorMarketDepthBacktest                       # noqa: E402

from hft import postprocess                                                   # noqa: E402
from hft.common import (build_asset, data_time_range, describe_asset_config, feed_latency_stats,   # noqa: E402
                        merged_config)
from hft.example_mm import EXAMPLE_PARAMS, run_example                        # noqa: E402
from hft.glft_hft import load_params_yaml, run_glft                           # noqa: E402

DEFAULT_YAML = ROOT / "config" / "backtest" / "glft_live.yaml"
DATA_ROOT = ROOT / "data" / "hft"
RESULTS_ROOT = ROOT / "hft" / "results"
NS_PER_S = 1_000_000_000


def hour_files(symbol: str, start: str, end: str, data_root: Path = DATA_ROOT, allow_missing: bool = False):
    """(event files, first snapshot file) for the inclusive hour range ``YYYY-MM-DDTHH`` .. ``YYYY-MM-DDTHH``.

    File naming (written by hft/convert.py): <root>/<symbol>/<date>/<symbol>_<date>_<HH>.npz and
    ..._<HH>_snapshot.npz.  A missing hour is an error unless allow_missing: hftbacktest would keep the clock
    running through the hole on a stale book."""
    t = datetime.strptime(start, "%Y-%m-%dT%H")
    t_end = datetime.strptime(end, "%Y-%m-%dT%H")
    files, snap, missing = [], None, []
    while t <= t_end:
        d, hh = t.strftime("%Y-%m-%d"), t.strftime("%H")
        f = data_root / symbol / d / f"{symbol}_{d}_{hh}.npz"
        if f.exists():
            files.append(str(f))
            if snap is None:
                s = data_root / symbol / d / f"{symbol}_{d}_{hh}_snapshot.npz"
                snap = str(s) if s.exists() else None
        else:
            missing.append(str(f))
        t += timedelta(hours=1)
    if missing and not allow_missing:
        raise FileNotFoundError(f"{len(missing)} hourly files missing, first: {missing[0]} (use --allow-missing)")
    if not files:
        raise FileNotFoundError("no data files in range")
    return files, snap


def load_gaps(symbol_dir: Path, files: list[str]) -> np.ndarray:
    """Read gaps.json of every date folder touched by ``files``; return an int64 (n, 2) array of
    [start, end] intervals in LOCAL nanoseconds (the strategy clock).

    gaps.json (data-engineer): {"gaps": [{start, end, start_local_ns?, end_local_ns?, clears_book, ...}]}.
    start/end are exchange-clock ns; *_local_ns are the receive times.  We use the local fields when
    present, else start/end.  EVERY listed interval is treated like an empty book (cancel, no quoting, then
    gap_cooldown_ms), including trade-stream outages that do not clear the book."""
    out = []
    for d in sorted({Path(f).parent for f in files}):
        g = d / "gaps.json"
        if g.exists():
            for item in json.loads(g.read_text(encoding="utf-8")).get("gaps", []):
                s = item.get("start_local_ns", item.get("start"))
                e = item.get("end_local_ns", item.get("end"))
                out.append((int(s), int(e)))
    return np.array(out, dtype=np.int64).reshape(-1, 2)


def plot_run(out_dir: Path, res: dict, title: str) -> None:
    """equity.png: net equity, inventory and mid on a shared time axis (downsampled to <= 4000 points)."""
    import matplotlib
    matplotlib.use("Agg")                  # no display needed: draw straight to a PNG file
    import matplotlib.pyplot as plt
    t, eq = res["_t_s"] / 3600.0, res["_equity"]
    step = max(1, len(t) // 4000)
    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 7))
    axes[0].plot(t[::step], eq[::step]); axes[0].set_ylabel("net equity [USDT]"); axes[0].set_title(title)
    pos = res["_pos"] if "_pos" in res else res["mid_log"]["position"]
    axes[1].plot(t[::step], pos[::step], drawstyle="steps-post"); axes[1].set_ylabel("inventory [ETH]")
    if "_mid_ff" in res:
        axes[2].plot(t[::step], res["_mid_ff"][::step], color="grey")
    axes[2].set_ylabel("mid [USDT]"); axes[2].set_xlabel("hours since start")
    for ax in axes:
        ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(out_dir / "equity.png", dpi=110); plt.close(fig)


def write_fills_csv(path: Path, res: dict, metrics_inputs: tuple) -> None:
    """fills.csv with the derived columns (mid_before and markouts) so a reader needs no code."""
    fills, mid_log = res["fill_log"], res["mid_log"]
    t_ser, m_ser = postprocess._valid_mid_series(mid_log)
    mid_before = postprocess.mid_at(t_ser, m_ser, fills["exch_ts"], strictly_before=True)
    cols = {h: postprocess.mid_at(t_ser, m_ser, fills["exch_ts"] + h * NS_PER_S, strictly_before=False)
            for h in postprocess.MARKOUT_HORIZONS_S}
    with open(path, "w", newline="", encoding="utf-8") as f:      # newline="" is what the csv module expects
        w = csv.writer(f)
        w.writerow(["exch_ts_ns", "poll_ts_ns", "order_id", "side", "px", "qty", "mid_before"]
                   + [f"markout_{h}s" for h in cols])
        for i, r in enumerate(fills):
            s = float(r["side"])
            w.writerow([int(r["exch_ts"]), int(r["poll_ts"]), int(r["order_id"]), int(r["side"]), float(r["px"]),
                        float(r["qty"]), float(mid_before[i])] + [s * (cols[h][i] - float(r["px"])) for h in cols])


def execute(strategy: str, *, data_files, snapshot_file, data_arrays=None, snapshot_array=None, cfg: dict,
            glft_params: dict | None, step_ms: int, out_dir: Path, title: str, extra_config: dict | None = None,
            gaps: np.ndarray | None = None) -> dict:
    """Build the asset, run the strategy, compute metrics and write all outputs.  Returns the metrics dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # ---- time span -> log capacities
    if data_arrays and not data_files:
        ts = np.concatenate([a["local_ts"] for a in data_arrays]); ts = ts[ts > 0]
        t_first, t_last = int(ts.min()), int(ts.max())
    else:
        t_first, t_last = data_time_range(list(data_files))
    max_steps = int((t_last - t_first) // (step_ms * 1_000_000)) + 20_000

    asset = build_asset(list(data_files), snapshot_file, cfg=cfg, data_arrays=data_arrays,
                        snapshot_array=snapshot_array)
    hbt = ROIVectorMarketDepthBacktest([asset])
    t0 = time.time()
    if strategy == "glft":
        res = run_glft(hbt, glft_params, max_steps=max_steps, step_ms=step_ms, gaps=gaps)
        maker_fee = cfg["maker_fee"]
        m = postprocess.compute_metrics(res, glft_params, maker_fee, glft_params["max_inventory"], cfg["lot_size"])
        write_fills_csv(out_dir / "fills.csv", res, ())
        strat_cfg = {k: v for k, v in glft_params.items()}
    else:
        res = run_example(hbt, {"step_ms": step_ms}, max_steps=max_steps, tick=cfg["tick_size"])
        m = postprocess.basic_from_recorder(res["recorder"])
        res.update({k: m[k] for k in ("_equity", "_t_s", "_pos")})
        strat_cfg = res["params"]
    m["wall_clock_s_incl_compile"] = round(time.time() - t0, 2)
    m["data_span_s"] = (t_last - t_first) / NS_PER_S
    if data_files:
        m["feed_latency_ms"] = feed_latency_stats(list(data_files))   # realised, from the data
    m["gap_intervals_used"] = 0 if gaps is None else int(len(gaps))

    # ---- config.json (everything to reproduce)
    config = {"strategy": strategy, "strategy_params": strat_cfg, "step_ms": step_ms,
              "seed": (glft_params or {}).get("seed", 42),
              "seed_note": "strategy is deterministic; the seed only drives the optional intp_jitter latency table",
              "asset": describe_asset_config(cfg, list(data_files), snapshot_file),
              "synthetic": bool(data_arrays), **(extra_config or {})}
    # Paths are written relative to the project root, so results show nothing about this PC.
    text = json.dumps(config, indent=2, default=str).replace(json.dumps(str(ROOT / "x")[:-1])[1:-1], "")
    (out_dir / "config.json").write_text(text, encoding="utf-8")
    # ---- fail loudly if the run did not cover the data (engine error, full recorder, ...)
    covered = m.get("duration_s", m.get("records", 0))
    if strategy == "glft":
        m["coverage_frac"] = m.get("duration_s", 0.0) / max(m["data_span_s"], 1e-9)
    else:
        m["coverage_frac"] = (res["_t_s"][-1] / max(m["data_span_s"], 1e-9)) if len(res["_t_s"]) else 0.0
    m["run_complete"] = bool(m["coverage_frac"] >= 0.99 and m.get("elapse_end_code", 1) == 1)
    clean = {k: v for k, v in m.items() if not k.startswith("_")}
    (out_dir / "metrics.json").write_text(json.dumps(clean, indent=2, default=float), encoding="utf-8")
    plot_run(out_dir, res, title)      # res carries _equity/_t_s (added by postprocess) for both strategies
    write_summary(out_dir, strategy, clean, config, title)
    return clean


def write_summary(out_dir: Path, strategy: str, m: dict, config: dict, title: str) -> None:
    """summary.md: key numbers + the assumptions that limit realism."""
    ex = config["asset"]["exchange"]
    lat = ex["latency"]
    lines = [f"# {title}", ""]
    if not m.get("run_complete", True):
        lines += ["**INCOMPLETE RUN: covered only {:.1%} of the data span (elapse end code {}). Numbers below are NOT comparable.**".format(m.get("coverage_frac", 0), m.get("elapse_end_code")), ""]
    lines += [
             f"Strategy `{strategy}` on hftbacktest 2.4.4 (queue model `{ex['queue_model']}`, "
             f"{'partial' if ex['partial_fill'] else 'no-partial'} fills, order latency "
             f"{lat['kind']} entry {lat['entry_ns']/1e6:.0f} ms / resp {lat['resp_ns']/1e6:.0f} ms).", "",
             "## Results", ""]
    for k, v in m.items():
        lines.append(f"- {k}: {v:.6g}" if isinstance(v, float) else f"- {k}: {v}")
    lines += ["", "## Assumptions that most limit realism", "",
              "- Feed latency is the REAL recorded local-exchange timestamp gap; order latency is a model "
              "(constant or interpolated jitter), not measured.",
              "- Queue position uses the chosen probabilistic/risk-averse model on L2 depth; true queue priority is unknown.",
              "- Your own orders never move the market (no impact), and fills against your resting order happen at your price.",
              "- Fill times/prices come from polling the order dictionary every step; several partial fills inside one step are merged.",
              "- GLFT port: no kill-switch / vol pause / spread pause / stale-quote guard / min-notional check (see hft/glft_hft.py docstring).",
              "- Requotes use modify (price only) or cancel+new, which differs from our engine's ReplaceOrder.",
              "- One sample period; no confidence intervals. Research and education, not financial advice."]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=["glft", "example"], default="glft")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--start", help="first hour, YYYY-MM-DDTHH (UTC)")
    ap.add_argument("--end", help="last hour inclusive, YYYY-MM-DDTHH (UTC)")
    ap.add_argument("--allow-missing", action="store_true", help="skip missing hourly files (clock still runs!)")
    ap.add_argument("--data-root", default=str(DATA_ROOT))
    ap.add_argument("--config", default=str(DEFAULT_YAML), help="backtest YAML to read GLFT params from")
    ap.add_argument("--queue-model", default="risk_adverse")
    ap.add_argument("--queue-n", type=float, default=2.0, help="exponent for power_prob* models")
    ap.add_argument("--partial-fill", action="store_true",
                    help="use PartialFillExchange (makes hbt.elapse fail with error 14 on our data)")
    ap.add_argument("--latency-kind", choices=["constant", "intp_jitter"], default="constant")
    ap.add_argument("--entry-ms", type=float, help="constant entry latency (default 160)")
    ap.add_argument("--resp-ms", type=float, help="response latency (default 240)")
    ap.add_argument("--requote-mode", choices=["modify", "cancel_new"], default="modify")
    ap.add_argument("--seed", type=int, default=42, help="seed of the intp_jitter latency table")
    ap.add_argument("--step-ms", type=int, default=100, help="polling step; GLFT quote cycle = timer_interval_ms")
    ap.add_argument("--out-root", default=str(RESULTS_ROOT))
    ap.add_argument("--synthetic", action="store_true", help="use hft.synthetic data instead of files")
    ap.add_argument("--seconds", type=float, default=120.0, help="synthetic duration")
    a = ap.parse_args(argv)

    lat: dict = {"kind": a.latency_kind, "seed": a.seed}
    if a.entry_ms is not None:
        lat["entry_ns"] = int(a.entry_ms * 1e6)
    if a.resp_ms is not None:
        lat["resp_ns"] = int(a.resp_ms * 1e6)
    cfg = merged_config(queue_model=a.queue_model, queue_n=a.queue_n, partial_fill=a.partial_fill, latency=lat)
    gp = load_params_yaml(a.config) if a.strategy == "glft" else None
    if gp:
        gp["requote_mode"] = a.requote_mode
    if gp:   # tick / lot / fee must be the same in the strategy and the simulated exchange
        cfg["tick_size"], cfg["lot_size"], cfg["maker_fee"] = gp["tick_size"], gp["lot_size"], gp["maker_fee"]

    gaps_arr = None
    if a.synthetic:
        from hft.synthetic import make_synthetic
        snap, ev = make_synthetic(a.seconds)
        files, snap_file, arrays, snap_arr, date = [], None, [ev], snap, "synthetic"
    else:
        if not (a.start and a.end):
            ap.error("--start and --end are required unless --synthetic")
        files, snap_file = hour_files(a.symbol, a.start, a.end, Path(a.data_root), a.allow_missing)
        arrays, snap_arr, date = None, None, a.start[:10]
        gaps_arr = load_gaps(Path(files[0]).parent.parent, files)
    out_dir = Path(a.out_root) / f"{date}_{a.strategy}_{a.tag}"
    title = f"{a.strategy} / {a.tag} / {a.start or 'synthetic'} .. {a.end or ''}"
    m = execute(a.strategy, data_files=files, snapshot_file=snap_file, data_arrays=arrays, snapshot_array=snap_arr,
                cfg=cfg, glft_params=gp, step_ms=a.step_ms, out_dir=out_dir, title=title,
                extra_config={"cli": vars(a)}, gaps=gaps_arr)
    print(json.dumps(m, indent=2, default=float))
    print("written:", out_dir)
    if not m.get("run_complete", True):       # fail loudly: summary.md also carries a banner
        print("ERROR: INCOMPLETE RUN, coverage", m.get("coverage_frac"), "elapse_end_code", m.get("elapse_end_code"), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
