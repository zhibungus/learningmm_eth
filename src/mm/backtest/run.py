"""Run a backtest from a YAML config:  ``python -m mm.backtest.run --config config/backtest/x.yaml``

What happens
    1. Load the config (defaults + your YAML), fix the seed.
    2. For every period in ``data.periods`` (e.g. in_sample, out_of_sample) replay the market data
       through a FRESH engine + strategy + simulated exchange (nothing carried over between periods).
    3. Unless disabled, repeat with every ``sensitivity.variants`` override (pessimistic fills,
       higher latency, both) and keep the headline numbers. Base and variants may run several seeds
       (``sensitivity.seeds``, per-variant ``seeds:``); the table shows mean and sd across seeds.
    4. Write ``reports/<YYYY-MM-DD>_<strategy>_<tag>/`` (config copy, config.json, metrics.json,
       fills.parquet, equity.parquet, PNG charts, summary.md).

Never tune parameters on the out_of_sample period; look at it once the parameters are frozen.
"""
# Lazy type hints (see strategy.py).
from __future__ import annotations

# argparse builds the command-line interface (--config, --seed ...) and the --help text.
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from mm.backtest.config import deep_merge, load_config
from mm.backtest.engine import build_engine
from mm.backtest.report import aggregate, code_manifest, file_manifest, git_info, headline, write_report
from mm.data.config import PROJECT_ROOT
from mm.data.loaders import find_files, replay, resolve_source


# Seconds since midnight from "HH:MM:SS" (periods may start/stop inside a day, e.g. one trading session).
def _hms(text: str) -> int:
    h, m, s = (int(x) for x in str(text).split(":"))
    return h * 3600 + m * 60 + s


# The exact nanosecond window of a period. start/end are whole UTC dates (inclusive); optional
# start_time / end_time ("HH:MM:SS", UTC) narrow it: [start 00:00 or start_time, end+1 day or end_time).
# Replay still loads whole days; events outside the window are skipped here.
def period_window_ns(period: dict) -> tuple[int, int]:
    def midnight(d) -> int:
        d = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
        return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000
    lo = midnight(period["start"]) + _hms(period.get("start_time", "00:00:00")) * 1_000_000_000
    hi = (midnight(period["end"]) + (_hms(period["end_time"]) if "end_time" in period else 86400) * 1_000_000_000)
    return lo, hi


# WARM START. A live recording has depth snapshots only at (re)connects, so a window that starts at
# 00:00 usually has no snapshot and the L2 book would begin empty. A period may therefore name how far
# back to start feeding the books (the strategy still only trades from the window start):
#     warmup_from:  "2026-10-23 19:06:24"   exact UTC start (pick the last snapshot before the window)
#     warmup_hours: 2                       or: this many hours before the window start
# Returns the warm-up start in ns, or None (no warm-up). It must lie before the window start (causality).
def warmup_start_ns(period: dict) -> int | None:
    lo, _ = period_window_ns(period)
    if "warmup_from" in period and "warmup_hours" in period:
        raise ValueError("give warmup_from or warmup_hours, not both")
    if "warmup_from" in period:
        w = period["warmup_from"]
        # YAML turns an unquoted 2026-10-23 19:06:24 into a datetime already; a quoted one is text.
        w = w if isinstance(w, dt.datetime) else dt.datetime.fromisoformat(str(w))
        w = w.replace(tzinfo=dt.timezone.utc) if w.tzinfo is None else w
        # Exact integer arithmetic: whole timedelta since the epoch (float timestamp() would round).
        delta = w - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        start = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
    elif period.get("warmup_hours"):
        start = lo - round(float(period["warmup_hours"]) * 3600) * 1_000_000_000
    else:
        return None
    if start >= lo:
        raise ValueError(f"warm-up start must be before the window start ({start} >= {lo})")
    return start


# First day to load: the window's start date, or the warm-up's start date if that is earlier.
def _first_day(period: dict, w: int | None) -> dt.date:
    s = period["start"]
    s = s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s))
    return s if w is None else min(s, dt.datetime.fromtimestamp(w / 1e9, dt.timezone.utc).date())


# Which processed input files feed this period (so the report can fingerprint them).
def period_files(cfg: dict, period: dict) -> list[Path]:
    d = cfg["data"]
    files: list[Path] = []
    first = _first_day(period, warmup_start_ns(period))           # includes the warm-up days
    for st in d["streams"]:
        # source may be one string or a per-stream dict such as {"depth": "live", "funding": "archive"};
        # resolve_source applies the SAME defaults replay() uses (e.g. depth is live-only), so we
        # fingerprint exactly the files replay will read.
        src = resolve_source(d["source"], st)
        files += find_files(st, first, period["end"], d["symbol"], src, d["trade_dataset"])
    # Live recordings also carry gap markers (the "gaps" dataset) that replay turns into GapEvents; they
    # change the result, so they belong in the manifest too.
    if any(resolve_source(d["source"], st) == "live" for st in d["streams"]):
        files += find_files("gaps", first, period["end"], d["symbol"], "live", d["trade_dataset"])
    return files


def run_period(cfg: dict, period: dict, seed: int, verbose: bool = True, manifest: bool = False):
    """Replay one period (a dict with start, end and optional start_time/end_time) through a fresh
    engine.  Returns (metrics, fills, equity, info)."""
    # A fresh engine per period means no position or state leaks from one period into the next, so
    # in-sample and out-of-sample are independent tests. replay() is a GENERATOR: it yields events one
    # at a time (lazily) instead of loading days of data into memory. The loop feeds them to the engine
    # in time order, which is the whole backtest. "n % 2_000_000 == 0" prints progress every 2M events.
    eng = build_engine(cfg, seed)
    d = cfg["data"]
    lo, hi = period_window_ns(period)
    wlo = warmup_start_ns(period)         # None = no warm-up
    t0 = time.time()
    n = 0
    # With a warm-up, replay starts on the warm-up day (and at the warm-up time, via from_ns). Events
    # before the window only warm the books (eng.warm_up); from the window start they are traded.
    first_day = _first_day(period, wlo)
    for ev in replay(first_day, period["end"], symbol=d["symbol"], streams=d["streams"],
                     source=d["source"], trade_dataset=d["trade_dataset"], from_ns=wlo):
        if ev.ts_ns < lo:
            if wlo is not None and ev.ts_ns >= wlo:
                eng.warm_up(ev)
            continue
        if n == 0 and wlo is not None:
            eng.check_warm_start()        # the first in-window event: is the book synced?
        if ev.ts_ns >= hi:
            break
        eng.on_market_event(ev)
        n += 1
        if verbose and n % 2_000_000 == 0:
            print(f"    ... {n:,} events ({time.time() - t0:.0f}s)", flush=True)
    metrics, fills, eq = eng.finish()
    info = {"start": str(period["start"]), "end": str(period["end"]),
            "warmup_from_ns": wlo, "warmup_events": eng.warm_events,
            "warmup_last_snapshot_ns": eng.warm_last_snapshot_ns,
            "start_time": period.get("start_time", "00:00:00"), "end_time": period.get("end_time", "24:00:00"),
            "window_ns": [lo, hi], "events": n, "wall_seconds": round(time.time() - t0, 1),
            "first_ts_ns": eng.rec.first_ts, "last_ts_ns": eng.rec.last_ts}
    if manifest:
        info["files"] = file_manifest(period_files(cfg, period), PROJECT_ROOT)
    return metrics, fills, eq, info


# One replay job, run in a worker process when --workers > 1. It must live at module level so the
# worker can import it. The headline run keeps everything (fills, equity, file manifest); every other
# job only needs its metrics. Each job builds its own engine from its seed, so the result does not
# depend on which process runs it or in what order.
def _job(cfg: dict, rng: dict, seed: int, headline_run: bool, verbose: bool):
    m, fills, eq, info = run_period(cfg, rng, seed, verbose=verbose, manifest=headline_run)
    return (m, fills, eq, info) if headline_run else (m, None, None, info)


# Which seeds each row of the sensitivity table runs. The base row uses sensitivity.seeds; a variant uses
# its own "seeds:" key, else just run.seed. run.seed is always in the base list and runs first (it is the
# headline run). dict.fromkeys(list) drops duplicates but keeps the order.
def seeds_for(cfg: dict, variants: dict, full: bool) -> dict[str, list[int]]:
    run_seed = cfg["run"]["seed"]
    extra = (cfg["sensitivity"].get("seeds") or []) if full else []
    out = {"base": list(dict.fromkeys([run_seed, *extra]))}
    for vname, over in variants.items():
        out[vname] = list(dict.fromkeys(over.get("seeds") or [run_seed]))
    return out


def run_all(cfg: dict, raw_text: str | None = None, sensitivity: bool | None = None,
            out_root: Path | None = None, workers: int = 1) -> Path:
    # Orchestration: base run on every period, then sensitivity variants (each = the base config with
    # a few overrides merged in, e.g. higher latency), then write the report folder.
    # Rule: tune parameters only on in_sample; out_of_sample is looked at once at the end.
    periods_cfg = cfg["data"]["periods"]
    if not periods_cfg:
        raise SystemExit("config data.periods is empty: give at least in_sample and out_of_sample dates")
    seed = cfg["run"]["seed"]
    print(f"seed={seed}  strategy={cfg['strategy']['name']}  fill_model={cfg['fill_model']['type']}", flush=True)

    # The no_cross_fills row is REQUIRED in every report (rule: net PnL better WITH cross fills than
    # without makes a result suspect), so it runs even when the other variants are switched off
    # (sensitivity.enabled false or --no-sensitivity). Only the extra variants are optional.
    all_variants = cfg["sensitivity"]["variants"]
    full = cfg["sensitivity"]["enabled"] if sensitivity is None else sensitivity
    to_run = all_variants if full else {"no_cross_fills": all_variants["no_cross_fills"]}
    seed_plan = seeds_for(cfg, to_run, full)

    # The list of every replay to do: (variant, period, seed, config). One job per (variant incl. base,
    # period, seed). The headline job (base, run.seed) comes first for each period.
    jobs = []
    for vname in seed_plan:
        # "seeds" only picks seeds; the other keys are config overrides merged on top of the base config.
        vcfg = cfg if vname == "base" else deep_merge(cfg, {k: v for k, v in to_run[vname].items() if k != "seeds"})
        jobs += [(vname, name, s, vcfg) for name in periods_cfg for s in seed_plan[vname]]

    # Run the jobs: one after another (workers=1, prints progress inside each replay), or several at
    # once in separate processes (each replay is independent, so this only saves wall time).
    # ProcessPoolExecutor = a pool of Python worker processes; submit() starts a job and returns a
    # "future" whose .result() waits for that job's return value.
    outputs: dict[tuple[str, str, int], tuple] = {}
    is_headline = lambda vname, s: vname == "base" and s == seed      # keeps fills/equity/manifest
    if workers <= 1:
        for vname, name, s, c in jobs:
            print(f"[{vname}] period {name} seed {s}", flush=True)
            outputs[(vname, name, s)] = _job(c, periods_cfg[name], s, is_headline(vname, s), True)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        print(f"running {len(jobs)} replays on {workers} worker processes", flush=True)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            # A dict comprehension mapping each future back to its (variant, period, seed) label.
            futures = {pool.submit(_job, c, periods_cfg[name], s, is_headline(vname, s), False): (vname, name, s)
                       for vname, name, s, c in jobs}
            for fut in as_completed(futures):           # yields each job as soon as it finishes
                outputs[futures[fut]] = fut.result()
                print(f"    done {futures[fut]}  ({len(outputs)}/{len(jobs)})", flush=True)

    # Assemble the results in the fixed config order, so the report is the same however the jobs ran.
    # stats(v, name) = mean/sd/per-seed numbers of one variant in one period.
    hz = cfg["metrics"]["headline_markout_s"]
    def stats(vname: str, name: str) -> dict:
        return aggregate({s: headline(outputs[(vname, name, s)][0], hz) for s in seed_plan[vname]})
    results: dict[str, dict] = {}
    for name in periods_cfg:
        m, fills, eq, info = outputs[("base", name, seed)]
        results[name] = {"metrics": m, "fills": fills, "equity": eq, "info": info, "seed_stats": stats("base", name)}
        print(f"[base] {name}: net pnl {m['pnl']['total_net']:.2f}  fills {m['fills']['count']}  "
              f"({info['events']:,} events, {info['wall_seconds']}s)", flush=True)
    sens = {v: {name: stats(v, name) for name in periods_cfg} for v in to_run}
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    run_id = f"{day}_{cfg['strategy']['name']}_{cfg['run']['tag']}"
    root = out_root or (PROJECT_ROOT / cfg["run"]["out_dir"])
    meta = {"written_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "python": sys.version.split()[0],
            # Recorded as a module command (no absolute paths, so the report shows nothing about this PC).
            "command": " ".join(["python -m mm.backtest.run", *sys.argv[1:]]),
            "seeds": seed_plan, "replay_jobs": len(jobs),
            # Provenance: exact code version and exact input files (with hashes) behind these numbers.
            "git": git_info(PROJECT_ROOT),
            # hash of the project source files that actually ran (git alone misses untracked/modified code)
            "code": code_manifest(PROJECT_ROOT),
            "manifest": {n: r["info"].get("files", []) for n, r in results.items()},
            "periods": {n: r["info"] for n, r in results.items()}}
    out = write_report(root / run_id, cfg, raw_text, results, sens, meta, "Backtest report")
    print(f"report written to {out}", flush=True)
    return out


# Command-line entry point: parse flags, load the YAML, apply optional overrides, run.
# argv=None makes argparse read the real command line; tests can pass a list instead.
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="YAML file, e.g. config/backtest/demo_smoke.yaml")
    ap.add_argument("--no-sensitivity", action="store_true", help="skip the pessimistic/latency variants")
    ap.add_argument("--seed", type=int, default=None, help="override run.seed")
    ap.add_argument("--tag", default=None, help="override run.tag")
    ap.add_argument("--workers", type=int, default=1,
                    help="replays to run in parallel processes (results are identical; ~0.8 GB RAM each)")
    a = ap.parse_args(argv)
    path = Path(a.config)
    raw = path.read_text(encoding="utf-8")
    cfg = load_config(path)
    if a.seed is not None:
        cfg["run"]["seed"] = a.seed
    if a.tag:
        cfg["run"]["tag"] = a.tag
    run_all(cfg, raw, sensitivity=False if a.no_sensitivity else None, workers=a.workers)


# Runs main() only when executed as "python -m mm.backtest.run", not when imported by another file.
if __name__ == "__main__":
    main()
