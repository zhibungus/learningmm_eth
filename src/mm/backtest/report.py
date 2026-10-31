"""Write a run folder: config copy, metrics.json, parquet files, charts, plain-language summary.md.

Used by both ``mm.backtest.run`` and ``mm.paper.run`` so the two produce identical report layouts.
"""
# Lazy type hints (see strategy.py).
from __future__ import annotations

# json: write/read the metrics file. Path: file-path objects (the "/" operator joins path pieces).
import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

# Nanoseconds per second; timestamps are integer ns, so dividing by this gives seconds.
NS = 1_000_000_000


# Format a number for the markdown tables. f"{x:,.2f}" means: thousands separators, 2 decimals.
# The nested braces {nd} let the number of decimals be a variable. None becomes "n/a".
def _f(x, nd=2, suffix=""):
    if x is None:
        return "n/a"
    return f"{x:,.{nd}f}{suffix}"


def _pct(x):
    return _f(None if x is None else 100 * x, 1, "%")


# "mean [low, high]" for the size-weighted markout per ETH, plus a flag when the interval includes 0.
def _ci_cell(mk: dict) -> str:
    ci = mk.get("avg_markout_per_eth_ci95")
    if ci is None:
        return "n/a"
    return f'{mk["avg_markout_per_eth"]:+.4f} [{ci[0]:+.4f}, {ci[1]:+.4f}]' + (" (includes 0)" if ci[0] <= 0 <= ci[1] else "")


# Flatten nested dicts to "a.b.c" -> number, keeping numbers only, so a strategy's summary() of any
# shape can be shown as one table (rows = keys, columns = periods).
def _flat_numbers(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat_numbers(v, f"{prefix}{k}."))
        elif isinstance(v, (int, float)):       # bool counts as int in Python
            out[f"{prefix}{k}"] = v
    return out


def _strategy_summary_table(periods: dict) -> str:
    names = [n for n, p in periods.items() if p["metrics"].get("strategy_summary")]
    if not names:
        return ""
    flat = {n: _flat_numbers(periods[n]["metrics"]["strategy_summary"]) for n in names}
    keys = list(dict.fromkeys(k for n in names for k in flat[n]))     # all keys, first-seen order
    def cell(v):
        return "" if v is None else (str(v) if isinstance(v, (bool, int)) else f"{v:.4g}")
    rows = ["| strategy summary | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    rows += [f"| {k} | " + " | ".join(cell(flat[n].get(k)) for n in names) + " |" for k in keys]
    return "\n".join(rows)


# A handful of headline numbers per run; the sensitivity table compares these across variants.
def headline(m: dict, horizon_s: float = 10) -> dict:
    """The handful of numbers shown in comparison tables. ``horizon_s`` picks which markout horizon is
    shown (config ``metrics.headline_markout_s``)."""
    mk = m.get("markouts", {})
    k10 = mk.get(f"{horizon_s:g}s", {})
    return {
        "net_pnl": m["pnl"]["total_net"], "spread_capture": m["pnl"]["spread_capture"],
        "inventory_pnl": m["pnl"]["inventory_pnl"], "fees": m["pnl"]["fees"], "funding": m["pnl"]["funding"],
        "fills": m["fills"]["count"], "maker_share_of_volume": m["fills"]["maker_share_of_volume"],
        "fill_ratio_orders": m["orders"]["fill_ratio_orders"],
        "sharpe": m["sharpe"].get("annualized"), "max_drawdown": m["max_drawdown"]["abs"],
        "avg_move_headline_bps": k10.get("avg_move_bps"), "final_position": m["final_position"],
        "filled_qty": m["fills"]["maker_volume"] + m["fills"]["taker_volume"],
        # inventory control (spec H3/H8) and whether the strategy's kill switch fired (None = no summary)
        "mean_abs_q": m["inventory"].get("mean_abs"), "time_at_limit_pct": m["inventory"].get("time_at_limit_pct"),
        "killed": bool(m["strategy_summary"]["killed"]) if "killed" in (m.get("strategy_summary") or {}) else None,
        # per mechanism: [filled qty, spread capture]; filled quantity matters because capped fills split one
        # order into several records, so the fill COUNT alone can rise while less size is filled.
        "mechanisms": {k: [v["qty"], v.get("spread_capture", 0.0)] for k, v in m["fills"].get("by_mechanism", {}).items()},
    }


# Several seeds of one (variant, period) -> mean and standard deviation per headline number.
# per_seed maps seed -> headline() dict. A different seed gives different latency jitter, so the spread
# across seeds is the noise level you must beat before calling a difference between variants real.
# sd needs 2+ seeds (ddof=1 = the sample standard deviation); with one seed it is None.
_NUM_KEYS = ("net_pnl", "spread_capture", "inventory_pnl", "fees", "funding", "fills", "filled_qty",
             "maker_share_of_volume", "fill_ratio_orders", "sharpe", "max_drawdown", "avg_move_headline_bps",
             "final_position", "mean_abs_q", "time_at_limit_pct")


def aggregate(per_seed: dict) -> dict:
    mean, sd = {}, {}
    for k in _NUM_KEYS:
        vals = [h[k] for h in per_seed.values() if h.get(k) is not None]
        mean[k] = float(np.mean(vals)) if vals else None
        sd[k] = float(np.std(vals, ddof=1)) if len(vals) > 1 else None
    # [qty, spread capture] per fill mechanism, averaged element by element
    mech = [h["mechanisms"] for h in per_seed.values()]
    mean["mechanisms"] = {k: [float(np.mean([m[k][i] for m in mech])) for i in (0, 1)]
                          for k in dict.fromkeys(k for m in mech for k in m) if all(k in m for m in mech)}
    flags = [h["killed"] for h in per_seed.values() if h.get("killed") is not None]
    return {"n": len(per_seed), "seeds": list(per_seed), "per_seed": per_seed, "mean": mean, "sd": sd,
            "kills": sum(flags) if flags else None, "kills_of": len(flags)}


def _plots(out: Path, periods: dict[str, dict], start_cash: float) -> list[str]:
    # matplotlib is imported inside the function so it is only loaded when charts are drawn.
    # "Agg" is a backend that draws straight to PNG files without opening a window (works headless).
    # Charts: equity curve (is it making money, how bumpy), inventory (how much risk we carried),
    # mid price (what the market did), and markouts (are fills followed by adverse moves?).
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = list(periods)
    files = []
    fig, axes = plt.subplots(3, len(names), figsize=(7 * len(names), 9), squeeze=False)
    for j, name in enumerate(names):
        eq: pl.DataFrame = periods[name]["equity"]
        ax0, ax1, ax2 = axes[0][j], axes[1][j], axes[2][j]
        if eq.height:
            # Thin to roughly 4000 points so the chart is quick to draw ("[::step]" = every step-th row).
            # datetime64[s] converts seconds-since-epoch into dates matplotlib can label.
            step = max(1, eq.height // 4000)
            e = eq[::step]
            t = (e["ts_ns"].to_numpy() / NS).astype("datetime64[s]")
            ax0.plot(t, e["equity"].to_numpy() - start_cash, lw=0.9, label="net PnL")
            ax0.plot(t, e["fees"].to_numpy() * -1, lw=0.7, color="tab:red", alpha=0.6, label="- cumulative fees")
            ax0.legend(loc="best", fontsize=8)
            ax1.plot(t, e["position"].to_numpy(), lw=0.8, color="tab:green")
            ax2.plot(t, e["mid"].to_numpy(), lw=0.7, color="gray")
        ax0.set_title(f"{name}: equity curve (USDT, vs start)")
        ax1.set_title("inventory (ETH)")
        ax2.set_title("mid price")
        for a in (ax0, ax1, ax2):
            a.grid(alpha=0.3)
            a.tick_params(axis="x", labelrotation=30)
    fig.tight_layout()
    fig.savefig(out / "equity_inventory.png", dpi=110)
    plt.close(fig)
    files.append("equity_inventory.png")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for name in names:
        mk = periods[name]["metrics"].get("markouts", {})
        if not mk:
            continue
        hs = sorted(mk, key=lambda s: float(s[:-1]))
        x = [0.0] + [float(h[:-1]) for h in hs]
        axes[0].plot(x, [0.0] + [mk[h]["avg_markout_bps"] for h in hs], marker="o", label=name)
        axes[1].plot(x, [0.0] + [mk[h]["avg_move_bps"] for h in hs], marker="o", label=name)
    axes[0].set_title("avg markout vs fill price (bps)\n(= edge kept after h seconds)")
    axes[1].set_title("avg mid move vs mid at fill (bps), sign = in our favour\n(negative = adverse selection)")
    for a in axes:
        a.set_xlabel("seconds after fill")
        a.axhline(0, color="k", lw=0.5)
        a.grid(alpha=0.3)
        a.legend()
    fig.tight_layout()
    fig.savefig(out / "markouts.png", dpi=110)
    plt.close(fig)
    files.append("markouts.png")
    return files


# Build a markdown table: one row per metric, one column per period (in-sample, out-of-sample).
# Each row is (label, function). A "lambda m: ..." is a tiny anonymous function taking the metrics dict
# m; "lambda m, h=h:" binds the loop variable h NOW (default argument) so every lambda keeps its own h.
def _period_table(periods: dict[str, dict], horizons: list) -> str:
    names = list(periods)
    rows = [("Net PnL (USDT)", lambda m: _f(m["pnl"]["total_net"])),
            ("  spread capture", lambda m: _f(m["pnl"]["spread_capture"])),
            ("  inventory PnL", lambda m: _f(m["pnl"]["inventory_pnl"])),
            ("  fees", lambda m: _f(-m["pnl"]["fees"])),
            ("  funding", lambda m: _f(-m["pnl"]["funding"])),
            ("Fills (maker / taker)", lambda m: f'{m["fills"]["count"]} ({m["fills"]["maker_count"]} / {m["fills"]["taker_count"]})'),
            ("Traded notional (USDT)", lambda m: _f(m["fills"]["traded_notional"], 0)),
            ("New orders accepted / rejected", lambda m: f'{m["orders"]["accepted"]} / {m["orders"]["rejected"]}'),
            ("Replaces accepted / rejected", lambda m: f'{m["orders"]["replaced"]} / {m["orders"]["replace_rejected"]}'),
            ("Fill ratio (new orders with a fill)", lambda m: _pct(m["orders"]["fill_ratio_orders"])),
            ("Fill ratio per action (new orders + replaces)", lambda m: _pct(m["orders"]["fill_ratio_per_action"])),
            ("Fill ratio (qty filled / qty accepted)", lambda m: _f(None if m["orders"]["fill_ratio_qty"] is None else 100 * m["orders"]["fill_ratio_qty"], 1, "%")),
            ("Sharpe (annualised, 60s PnL buckets)", lambda m: _f(m["sharpe"].get("annualized"))),
            ("Max drawdown (USDT)", lambda m: _f(m["max_drawdown"]["abs"])),
            ("Mean abs inventory (ETH)", lambda m: _f(m["inventory"].get("mean_abs"), 3)),
            ("Max abs inventory (ETH)", lambda m: _f(m["inventory"].get("max_abs"), 3)),
            ("Time at inventory limit", lambda m: _f(m["inventory"].get("time_at_limit_pct"), 1, "%")),
            ("Final position (ETH)", lambda m: _f(m["final_position"], 3)),
            # Which mechanism produced the fills: see fills.py. "cross"/"through" are the most generous.
            ("Fills by mechanism (queue/through/cross/taker)",
             lambda m: "/".join(str(m["fills"].get("by_mechanism", {}).get(k, {}).get("count", 0))
                                for k in ("queue", "through", "cross", "taker"))),
            ("Net PnL if final inventory exited at the touch",
             lambda m: _f(m["pnl"].get("net_if_liquidated_at_touch", m["pnl"]["total_net"]))),
            ("t-stat of mean PnL per bucket", lambda m: _f(m["sharpe"].get("t_stat_mean_pnl_per_bucket"))),
            ("Risk-limit rejects", lambda m: str(m["orders"].get("risk_rejects", 0)))]
    # Horizons come from config (metrics.markout_horizons_s), formatted exactly as metrics.py keys them.
    for hs in horizons:
        h = f"{hs:g}s"
        rows.append((f"Mid move {h} after fill (bps, + = favourable)",
                     lambda m, h=h: _f(m["markouts"].get(h, {}).get("avg_move_bps"), 3)))
        rows.append((f"Markout {h} per ETH (USDT, 95% bootstrap CI)",
                     lambda m, h=h: _ci_cell(m["markouts"].get(h, {}))))
    out = ["| metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for label, fn in rows:
        out.append(f"| {label} | " + " | ".join(fn(periods[n]["metrics"]) for n in names) + " |")
    return "\n".join(out)


# "mean (sd)" for one number of an aggregate(); just the mean when there is a single seed (no sd).
def _ms(agg: dict, key: str, nd: int = 2, scale: float = 1.0, suffix: str = "") -> str:
    m, s = agg["mean"][key], agg["sd"][key]
    if m is None:
        return "n/a"
    return _f(scale * m, nd, suffix) + ("" if s is None else f" ({_f(abs(scale) * s, nd)})")


# One markdown row of the sensitivity table. Cells show the mean over seeds, with the sd in brackets.
def _sens_row(name: str, agg: dict) -> str:
    kills = "n/a" if agg["kills"] is None else f'{agg["kills"]}/{agg["kills_of"]}'
    return (f'| {name} | {agg["n"]} | {_ms(agg, "net_pnl")} | {_ms(agg, "spread_capture")} | '
            f'{_ms(agg, "inventory_pnl")} | {_ms(agg, "fees", scale=-1)} | {_ms(agg, "fills", 0)} | '
            f'{_ms(agg, "filled_qty", 1)} | {_ms(agg, "maker_share_of_volume", 0, 100, "%")} | '
            f'{_ms(agg, "avg_move_headline_bps", 3)} | {kills} | {_ms(agg, "time_at_limit_pct", 1, 1, "%")} | '
            f'{_ms(agg, "mean_abs_q", 3)} | {_mech_cell(agg["mean"])} |')


# N6: cross fills are adverse in this data, so "better WITH cross fills than without" is suspect. But two
# runs differ by seed noise alone, so warn only when the gap is bigger than the base net PnL's sd across
# seeds. With a single seed the noise is unknown: still warn, and say so. Returns the text or None.
def cross_fill_warning(period: str, base: dict, no_cross: dict) -> str | None:
    gap = base["mean"]["net_pnl"] - no_cross["mean"]["net_pnl"]
    sd = base["sd"]["net_pnl"]
    if gap <= 0 or (sd is not None and gap <= sd):
        return None
    note = ("single seed, noise not estimated" if sd is None
            else f"gap {gap:.2f} exceeds the base net PnL sd {sd:.2f} over {base['n']} seeds")
    return (f"> WARNING ({period}): net PnL is better WITH cross fills than without (`no_cross_fills`); "
            f"{note}. Treat this result as suspect.")


# Sensitivity = same strategy under harsher assumptions (pessimistic fills, higher latency). If profit
# vanishes here, the base result depended on optimistic fill/latency assumptions. Each row is the mean
# (sd) over that variant's seeds. "kills" = runs where the strategy's kill switch fired / seeds.
def _sens_table(base: dict[str, dict], sens: dict[str, dict[str, dict]], horizon_s: float) -> str:
    out = []
    for n, p in base.items():
        out.append(f"\n**{n}**\n")
        out.append(f"| variant | seeds | net PnL | spread capture | inventory PnL | fees | fills | filled qty (ETH) | "
                   f"maker share | {horizon_s:g}s move (bps) | kills | time at limit | mean abs q (ETH) | "
                   "qty / spread capture: queue; through; cross; taker |")
        out.append("|" + "---|" * 14)
        base_agg = p.get("seed_stats") or aggregate({0: headline(p["metrics"], horizon_s)})
        out.append(_sens_row("base", base_agg))
        out += [_sens_row(v, per[n]) for v, per in sens.items()]
    out.append("\nCells are mean (sd across seeds); the sd is blank with one seed. The base row averages the base "
               "seeds; the headline run (first table, charts) is the run.seed run alone.")
    # Sign-off rule: net PnL BETTER with cross fills on than off is suspect (cross fills are adverse here).
    for n, p in base.items():
        nc = sens.get("no_cross_fills", {}).get(n)
        base_agg = p.get("seed_stats") or aggregate({0: headline(p["metrics"], horizon_s)})
        warn = cross_fill_warning(n, base_agg, nc) if nc is not None else None
        if warn:
            out.append("\n" + warn)
    return "\n".join(out)

# "qty / spread capture" per fill mechanism as one table cell, e.g. "0.0 / +0.00; 691.3 / +23.74; ...".
# A generator expression inside "; ".join(...) builds the pieces without making a list first.
def _mech_cell(h: dict) -> str:
    mech = h["mechanisms"]
    return "; ".join(f"{mech.get(k, [0.0, 0.0])[0]:.1f} / {mech.get(k, [0.0, 0.0])[1]:+.2f}"
                     for k in ("queue", "through", "cross", "taker"))


# ---------------------------------------------------------------------------------------- provenance
# A report must say WHICH code and WHICH data produced it, otherwise it cannot be reproduced (a later
# recording session can silently change what a date range contains). We record the git commit (and
# whether files were modified since), and for every input parquet file its size, row count and SHA-256
# hash (a fingerprint of the exact bytes: any change to the file changes the hash). We never commit.
def git_info(root: Path) -> dict:
    """{"commit": <hash> or "uncommitted", "dirty": bool}; never raises (no git installed is fine)."""
    import subprocess

    def run(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--", "src", "config", "tests")
    return {"commit": commit or "uncommitted", "dirty": bool(status) if status is not None else True}


# Which CODE actually ran? The git commit does not say when files are untracked or modified, so we also
# hash every project source file that Python has imported in this process (sys.modules maps module
# names to module objects; __file__ is where each came from) and combine the hashes into one fingerprint.
# Anyone can recompute it later from the same files. Nothing is committed.
def code_manifest(root: Path) -> dict:
    """{"combined_sha256": ..., "files": [{"path", "sha256"}]} for loaded modules under <root>/src/mm."""
    import hashlib
    import sys

    src = (root / "src" / "mm").resolve()
    files = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f or not str(f).endswith(".py"):
            continue
        p = Path(f).resolve()
        if src in p.parents:
            files.append(p)
    entries = [{"path": str(p.relative_to(root.resolve())), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in sorted(set(files))]
    combined = hashlib.sha256("".join(e["path"] + e["sha256"] for e in entries).encode()).hexdigest()
    return {"combined_sha256": combined, "files": entries}


def file_manifest(files: list[Path], root: Path) -> list[dict]:
    """Path (relative to the project), bytes, rows and SHA-256 of each input file."""
    import hashlib

    out = []
    for f in files:
        h = hashlib.sha256()
        with open(f, "rb") as fh:                      # read in 1 MB chunks so big files do not fill RAM
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            rel = str(f)
        out.append({"path": rel, "bytes": f.stat().st_size,
                    "rows": int(pl.scan_parquet(f).select(pl.len()).collect().item()), "sha256": h.hexdigest()})
    return out


def write_report(out: Path, cfg: dict, raw_config_text: str | None, periods: dict[str, dict],
                 sens: dict[str, dict[str, dict]], meta: dict[str, Any], title: str) -> Path:
    """Write everything.  ``periods[name] = {"metrics", "fills", "equity", "info"}``."""
    # Everything needed to reproduce and judge a run goes in one folder: the config (incl. seed), the
    # numbers (metrics.json), the raw tables (parquet = compact columnar files), charts and a summary.
    # mkdir(parents=True, exist_ok=True): create the folder and parents, no error if it exists.
    # "default=str" in json.dumps turns objects JSON cannot store (like dates) into text.
    out.mkdir(parents=True, exist_ok=True)
    if raw_config_text is not None:
        (out / "config.yaml").write_text(raw_config_text, encoding="utf-8")
    resolved = {"config": cfg, "meta": meta}
    (out / "config.json").write_text(json.dumps(resolved, indent=2, default=str), encoding="utf-8")
    metrics = {"run_id": out.name, "seed": cfg["run"]["seed"],
               "periods": {n: p["metrics"] for n, p in periods.items()},
               "period_info": {n: p.get("info", {}) for n, p in periods.items()},
               # per-seed headline numbers plus mean/sd: the base seeds, then every variant (see aggregate())
               "base_seeds": {n: p["seed_stats"] for n, p in periods.items() if "seed_stats" in p},
               "sensitivity": sens}
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    # List comprehensions tag each period's table with a constant "period" column (pl.lit = literal
    # value, .alias = column name), then pl.concat stacks them into one table. how="diagonal" tolerates
    # tables whose columns differ slightly.
    fr = [p["fills"].with_columns(pl.lit(n).alias("period")) for n, p in periods.items()]
    er = [p["equity"].with_columns(pl.lit(n).alias("period")) for n, p in periods.items()]
    pl.concat(fr, how="diagonal").write_parquet(out / "fills.parquet")
    pl.concat(er, how="diagonal").write_parquet(out / "equity.parquet")
    plots = _plots(out, periods, cfg["account"]["starting_cash"])
    (out / "summary.md").write_text(_summary(out.name, cfg, periods, sens, meta, plots, title), encoding="utf-8")
    return out


# How many data outages "What was run" names per period.
MAX_OUTAGES_SHOWN = 5


# Nanoseconds since the epoch -> "YYYY-MM-DD HH:MM:SS.mmm" in UTC ("n/a" when missing).
def _utc(ns: int | None) -> str:
    if ns is None:
        return "n/a"
    t = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ns // 1000)
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


# Spec H2: does the fitted fill-rate model A*exp(-k*delta) predict how often we really fill?
# ratio = realised fills / sum of the model's expected fills over every live quote's real distance and
# resting time (the strategy accumulates the denominator as "h2_predicted_fills"). Spec bound: [0.5, 2].
# "through-only" counts only trade-through fills, which is what the calibration measured.
def _h2_text(periods: dict) -> str:
    cells = []
    for n, p in periods.items():
        pred = (p["metrics"].get("strategy_summary") or {}).get("h2_predicted_fills")
        if not pred:
            continue
        f = p["metrics"]["fills"]
        thr = f.get("by_mechanism", {}).get("through", {}).get("count", 0)
        cells.append(f"{n} {f['count'] / pred:.3f} (through-only {thr / pred:.3f}; "
                     f"{f['count']} fills vs {pred:.0f} predicted)")
    if not cells:
        return ""
    return ("**Spec H2 (fitted fill rate vs realised): realised / predicted fills** = " + "; ".join(cells) +
            ". The hypothesis holds only inside [0.5, 2]; far below 0.5 means the model promises many more "
            "fills than a resting quote really gets.")


# Realism wording per fill model. Quote runs only see the top of the book; L2 runs replay full depth
# from our own live recording. Same numbering as the list in the report.
_REALISM_QUOTE = {
    "queue": "1. **Queue position is an estimate.** We start behind the displayed quantity and shrink the queue "
             "by trades at our price; cancellations are split proportionally (see config). With only top-of-book "
             "data we cannot see queues deeper than the touch, so those orders fill only on a trade-through.",
    "sizes": "3b. **Fill sizes are capped** (trade-through by print size, quote cross by displayed size), but a "
             "crossed quote can fill again on each later crossed quote. The 'Fills by mechanism' row shows how "
             "much of the result rests on through/cross fills; `no_cross_fills` tests it.",
    "data": "4. **bookTicker archive ends 2024-03-30**; results on that period say little about today's market. "
            "Recent dates have trades only and cannot support a passive fill model.",
    "taker": "5. **Taker fills (quote model)** assume liquidity beyond the touch comes in tiers as big as the touch, "
             "each a configured number of ticks worse; L2 data walks the real book.",
}
_REALISM_L2 = {
    "queue": "1. **Queue position is an estimate, from depth diffs.** The full order book (all recorded levels) "
             "comes from our own live recording, so we join behind the real displayed queue at our price. The "
             "queue then shrinks by trades at our price and by depth decreases that are not explained by trades; "
             "the feed shows only size changes, so how many of those cancels were ahead of us is assumed "
             "(`cancel_assumption`). Depth updates arrive in 100 ms batches, so order inside a batch is unknown.",
    "sizes": "3b. **Fill sizes are capped** (trade-through by print size, at-price fills by the queue left after "
             "the queue ahead). Our own size is tiny against the queues, so we do not model our effect on them. "
             "The 'Fills by mechanism' row shows how much rests on through/cross fills; `no_cross_fills` tests it.",
    "data": "4. **Only the days we recorded ourselves.** A few live days (see 'What was run') cannot show "
            "day-to-day or weekday/weekend variation, so one in-sample and one out-of-sample day support the sign "
            "and the mechanism, not a ranking of variants. Recorded outages are replayed as gaps (book cleared, "
            "orders cancelled), but anything the recorder missed is invisible. Receive times on the recording "
            "PC are not used; exchange timestamps are.",
    "taker": "5. **Taker fills walk the real recorded book** level by level, with no impact on it, so large "
             "sweeps are not penalised beyond what the book showed.",
}


# Assemble summary.md as a list of lines (L) and join with newlines at the end. "L += [...]" appends
# several lines at once. The text is deliberately plain-language and ends with the assumptions that
# most limit realism, so no reader mistakes a backtest for a promise.
def _summary(run_id: str, cfg: dict, periods: dict, sens: dict, meta: dict, plots: list[str], title: str) -> str:
    fm, lat, ex = cfg["fill_model"], cfg["latency"], cfg["exchange"]
    L = [f"# {title}: {run_id}", ""]
    L.append(f"Strategy `{cfg['strategy']['name']}`, params `{json.dumps(cfg['strategy'].get('params', {}))}`. "
             f"Seed {cfg['run']['seed']}. Symbol {cfg['data']['symbol']} (Binance USD-M perpetual). "
             "Research and education only; a backtest does not promise live results.")
    L.append("")
    if cfg["strategy"]["name"] == "demo_quoter":
        L += ["> **The demo quoter is a plumbing test, not a strategy. Its PnL means nothing.**", ""]
    L += ["## What was run", ""]
    for n, p in periods.items():
        info = p.get("info", {})
        cov = p["metrics"].get("coverage") or {}
        L.append(f"* **{n}**: {info.get('start')} to {info.get('end')} (UTC, inclusive), "
                 f"{info.get('events', 0):,} market events replayed. First to last event "
                 f"{p['metrics']['duration_hours']:.1f} h"
                 + (f", of which **{cov['covered_hours']:.1f} h actually covered by events** "
                    f"({cov['n_gaps']} silences over {cov['gap_threshold_s']:g} s removed, "
                    f"{cov['silent_hours']:.1f} h)." if cov else "."))
        # Warm-up (events before the window that only built the books) and the data outages inside the period.
        if info.get("warmup_from_ns") is not None:
            L.append(f"  * Warm-up: books fed from {_utc(info['warmup_from_ns'])} UTC, "
                     f"{info.get('warmup_events', 0):,} events replayed but not traded; last depth snapshot "
                     f"{_utc(info.get('warmup_last_snapshot_ns'))} UTC.")
        if cov and cov.get("longest_gaps"):
            outages = ", ".join(f"{_utc(g['start_ns'])} to {_utc(g['end_ns'])} UTC ({g['seconds']:.0f} s)"
                                for g in cov["longest_gaps"][:MAX_OUTAGES_SHOWN])
            L.append(f"  * Data outages (silences over {cov['gap_threshold_s']:g} s; the longest "
                     f"{min(len(cov['longest_gaps']), MAX_OUTAGES_SHOWN)}): {outages}.")
    for note in meta.get("notes", []):
        L.append(f"* {note}")
    L += ["", f"Fill model: `{fm['type']}`"
          f" (trade-through required: {fm['require_trade_through']}, cancellations: {fm['cancel_assumption']}, "
          f"queue factor: {fm['initial_queue_factor']}). "
          f"Latency: market data "
          + (f"drawn from table {lat['feed_delay_quantiles_ms']} (probability, ms)"
             if lat.get("feed_delay_quantiles_ms") else f"{lat['feed_latency_ms']} ms")
          + f", orders {lat['order_latency_ms']} ms, "
          f"reports {lat['report_latency_ms'] if lat['report_latency_ms'] is not None else lat['feed_latency_ms']} ms. "
          f"Fees: maker {ex['maker_fee']}, taker {ex['taker_fee']} (fractions of notional). "
          f"Tick {ex['tick_size']}, lot {ex['lot_size']}.", ""]
    L += ["## Results", "",
          "In-sample is where you may calibrate parameters; out-of-sample must be looked at only once, "
          "after parameters are frozen (never tune on it).", "",
          _period_table(periods, cfg["metrics"]["markout_horizons_s"]), ""]
    L += ["How to read it: **spread capture** is what you earn by trading away from the mid; "
          "**inventory PnL** is what the position you were holding earned as the mid moved (adverse selection "
          "shows up as negative inventory PnL and negative 'mid move after fill'). "
          "The two add up to gross trading PnL; subtract fees and funding for net.", ""]
    table = _strategy_summary_table(periods)
    if table:
        L += ["## Strategy's own counters (its `summary()`)", "",
              "Pauses, kills, rejects by reason etc., as counted by the strategy itself. "
              "Full detail in `metrics.json` under `periods.<period>.strategy_summary`.", "", table, ""]
    h2 = _h2_text(periods)
    if h2:
        L += [h2, ""]
    L += ["Order counts: 'new orders' are fresh placements; 'replaces' are modifies of a live order "
          "(each re-queues the order at the back unless `exchange.replace_keeps_priority`). The fill ratio "
          "is taken over new orders; the per-action ratio over new orders plus replaces. The markout "
          "interval is a seeded bootstrap over fills (fills cluster in time, so it is optimistically narrow).", ""]
    if "no_cross_fills" not in sens:
        # Cannot happen through run.py (it always runs the row); guards hand-built reports.
        L += ["> **WARNING: the required `no_cross_fills` sensitivity row is MISSING. Do not trust this result "
              "until it has been run.**", ""]
    if sens:
        L += ["## Sensitivity (does the result survive worse assumptions?)", "",
              "Same data and strategy, harsher fills and/or higher latency. If PnL collapses here, "
              "the base result depended on optimistic assumptions. `colocated` is the old fast-network "
              "setting (10/20 ms) for comparison, NOT the base case.", "",
              _sens_table(periods, sens, cfg["metrics"]["headline_markout_s"]), ""]
    L += ["## Charts", ""] + [f"* `{p}`" for p in plots] + [""]
    # Provenance: which code and which input files produced these numbers (details in config.json).
    git = meta.get("git")
    if git:
        code = meta.get("code") or {}
        L += ["## Provenance", "",
              f"* source files that ran: {len(code.get('files', []))} modules, combined SHA-256 "
              f"`{code.get('combined_sha256', 'n/a')}` (the git commit alone does not identify untracked code)",
              f"* git commit: `{git.get('commit')}`" + (" (working tree has uncommitted changes)" if git.get("dirty") else ""),
              f"* data manifest: {sum(len(v) for v in meta.get('manifest', {}).values())} input files with SHA-256 "
              "hashes and row counts are listed in `config.json` under `meta.manifest`.", ""]
    realism = _REALISM_L2 if fm["type"] == "l2" else _REALISM_QUOTE
    L += ["## Assumptions that most limit realism", "", realism["queue"],
          "2. **No market impact.** Our orders never change the recorded market, and nobody reacts to us.",
          "3. **Feed delay is drawn from an empirical table** (p50/p90/p99 measured on the owner's PC with "
          "`mm.data.latency`, smoothly varying so slow stretches come in bursts); order latency is a base value "
          "plus uniform jitter. The strategy sees market events no earlier than their "
          "publish time E. Timestamps are millisecond resolution, so sub-ms ordering is unknowable and "
          "same-millisecond events are processed before any order arriving in that millisecond.",
          realism["sizes"], realism["data"], realism["taker"],
          "6. **Not modelled**: margin/liquidation, rate limits, hidden/iceberg orders, self-trade prevention, "
          "our own order connection going down (feed gaps in the data are replayed), fee tier changes, "
          "funding forecasts.",
          "7. **Each period starts flat** and ends with whatever inventory is left (marked at mid, not liquidated).",
          "8. **Replaces re-queue** (Binance reorders modified orders); `exchange.replace_keeps_priority: true` "
          "is the optimistic old rule. IOC orders never rest. A replace whose total_qty is already filled "
          "cancels the order (per the Binance docs; not checked on testnet).",
          ""]
    return "\n".join(L)
