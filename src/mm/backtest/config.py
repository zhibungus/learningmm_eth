"""Backtest / paper-trading configuration: defaults, YAML loading, unit conversion.

Every number that influences a result lives in the YAML file (``config/backtest/*.yaml``) or in
``DEFAULTS`` below -- never as a magic number in code.  A YAML file only needs to contain what it
wants to CHANGE; everything else comes from ``DEFAULTS``.  The fully resolved config is written to
``reports/<run>/config.json`` so a run can always be reproduced.

Units in the YAML: latencies in milliseconds, durations in seconds, prices in USDT, sizes in ETH.
Inside the code everything is integer nanoseconds.
"""
# Lazy type hints so "str | Path" style annotations work (see strategy.py).
from __future__ import annotations

# copy.deepcopy duplicates nested dicts fully, so editing a copy never alters the original.
import copy
# Path: an object for file paths (works on Windows and Linux); easier than joining strings.
from pathlib import Path
from typing import Any

# YAML: a human-friendly config file format; PyYAML turns it into Python dicts/lists.
import yaml

# Every tunable number lives here (project rule: no magic numbers in code). A user's YAML only has to
# list what differs. Latency is modelled (feed + order + report) because in real trading information
# and orders take time to travel: a quote you see is already old, and an order you send lands later,
# when the market may have moved or filled the order you were trying to cancel.
DEFAULTS: dict[str, Any] = {
    "run": {
        "tag": "run",
        "seed": 12345,                    # every run records its seed; used for strategy rng + jitter
        "out_dir": "reports",
    },
    "data": {
        "symbol": "ETHUSDT",
        "source": "archive",              # "archive" (data.binance.vision) or "live" (our recordings)
        "streams": ["trades", "quotes", "funding"],
        "trade_dataset": "agg_trades",
        # Calibrate on in_sample, report out_of_sample UNTOUCHED.  Dates inclusive, UTC.
        # Each period is simulated independently from flat (no position carried over).
        "periods": {},
    },
    "strategy": {"name": "demo_quoter", "params": {}},
    "latency": {
        # Defaults are MEASURED on the owner's PC with `python -m mm.data.latency` on 2026-10-19:
        # one-way market-data delay p50 239 / p90 296 / p99 772 ms (recv time minus exchange event time E,
        # clock offset removed), E-T p50 0 / p99 3 ms (live), REST round trip p50 224 ms. Suggested
        # values from that tool: feed 240 (+~59 jitter), report 240, order ~132 (+jitter).
        # A colocated/VPS setup is available as the "colocated" sensitivity variant below.
        "feed_latency_ms": 240.0,         # exchange publish time (E) -> strategy sees it (used when no table below)
        "order_latency_ms": 132.0,        # strategy decision -> order reaches the matching engine
        "report_latency_ms": 240.0,       # fill/ack -> strategy (None = same as feed latency)
        "order_jitter_ms": 59.0,          # +U(0, x) ms on order latency (uses the seeded rng)
        "feed_jitter_ms": 59.0,           # +U(0, x) ms on feed latency (only used when the table below is None)
        # EMPIRICAL feed-delay distribution (exchange publish time E -> our receipt), as (probability, ms)
        # points measured with `python -m mm.data.latency` on 2026-10-19: p50 239 / p90 296 / p99 772 ms,
        # never below ~130 ms. The 0.0 and 1.0 end points are assumptions (floor from the validator's
        # measurement, ceiling a round guess for the worst spike). ONE delay is drawn per window of
        # feed_delay_knot_ms and shared by its events, so delays come in slow/fast stretches like real
        # bursts; event ORDER is kept (a late event holds back the ones behind it). Effective percentiles
        # match the table: p50 within 5%, p99 within 15%, p90 ~20-25% HIGH (backlog drain), checked by a test. When this table is set,
        # feed_latency_ms / feed_jitter_ms are ignored; set it to None for a fixed delay.
        "feed_delay_quantiles_ms": [[0.0, 130.0], [0.5, 239.0], [0.9, 296.0], [0.99, 772.0], [1.0, 1500.0]],
        "feed_delay_knot_ms": 2000.0,
        # Archive trades carry no publish time E, only the match time T. Binance publishes about this
        # long after T (median E-T of the archive bookTicker was 6-7 ms), so add it when E is missing.
        "no_event_ts_extra_ms": 7.0,
        "timer_interval_ms": 1000.0,      # on_timer period (None/0 = no timer)
        # Zero latency leaks same-millisecond information (a strategy could react to print #1 and be
        # filled by print #2 of the same ms), so latencies below this are refused unless the test-only
        # flag allow_zero_latency is set.
        "min_latency_ms": 1.0,
        "allow_zero_latency": False,
    },
    "fill_model": {
        "type": "quote_trade",            # "quote_trade" (top of book) or "l2" (depth)
        "require_trade_through": False,   # True = pessimistic: trades AT our price never fill us
        "cancel_assumption": "proportional",   # proportional | all_behind (pessimistic) | all_ahead
        "initial_queue_factor": 1.0,      # >1 = start further back in the queue (pessimistic)
        # Fill SIZE caps: a trade-through fill is limited to the size of that print, a quote-cross fill
        # to the displayed quantity. False = old behaviour (whole order fills), kept only for comparison.
        "cap_fills_by_volume": True,
        "cross_fills": True,              # False = a quote crossing our price (no print) never fills us
        "sweep_penalty_ticks": 1,         # quote_trade taker: ticks of extra slippage per extra "top size" tier
        "sweep_max_tiers": 5,             # quote_trade taker: liquidity assumed beyond the touch = this many tiers
    },
    "exchange": {
        "tick_size": 0.01,
        "lot_size": 0.001,
        "min_qty": 0.001,
        "min_notional": 20.0,             # USDT; check exchangeInfo for the current value
        "maker_fee": 0.0002,              # Binance USD-M VIP0
        "taker_fee": 0.0005,
        # Exchange-side hard risk limits (None = off). Orders that could take |position| past
        # max_position (counting all open orders) or exceed max_order_notional are rejected.
        "max_position": None,             # ETH, absolute
        "max_order_notional": None,       # USDT per order
        # GapEvent.stream values that mean "the TRADES socket dropped" (live feed endpoint name "market").
        # Such a gap cancels our orders (we may have missed prints) but keeps the book and resumes at once.
        # Any other stream ("depth", "public", None/unknown) wipes the book and waits for a snapshot/quote.
        "gap_trade_streams": ["market"],
        # Binance re-queues every modify (docs: "modified orders will be reordered in the match queue"),
        # so by default a replace always goes to the back. True = a same-price size cut keeps its place
        # (optimistic; the old engine rule).
        "replace_keeps_priority": False,
    },
    "account": {"starting_cash": 10000.0},
    "metrics": {
        "equity_bucket_s": 1.0,           # equity/inventory sampling grid
        "sharpe_bucket_s": 60.0,
        "markout_horizons_s": [1, 10, 60],
        "headline_markout_s": 10,         # horizon shown in the sensitivity tables (must be in the list above)
        "mid_sample_ms": 10.0,            # resolution of the stored mid series (for markouts)
        "inventory_limit": None,          # base units; used for "time at limit" (set to the strategy cap)
        "inventory_histogram_bins": 10,
        "bootstrap_n": 1000,              # resamples for the markout confidence interval (0 = off)
        "coverage_gap_s": 60.0,           # silence between events longer than this is not counted as covered
    },
    "sensitivity": {
        "enabled": True,
        # Seeds for the BASE row of the sensitivity table (run.seed is always included and stays the
        # headline run). None = just run.seed. A variant gets its own seeds with a "seeds:" key inside
        # it; without one it runs once, at run.seed.
        "seeds": None,
        # Each variant = overrides merged on top of the base config.  Reported next to the base run.
        "variants": {
            "colocated": {"latency": {"feed_latency_ms": 10.0, "order_latency_ms": 20.0, "report_latency_ms": 10.0,
                                      "feed_delay_quantiles_ms": None,
                                      "order_jitter_ms": 0.0, "feed_jitter_ms": 0.0}},
            "pessimistic_fills": {"fill_model": {"require_trade_through": True,
                                                 "cancel_assumption": "all_behind",
                                                 "initial_queue_factor": 1.5}},
            "no_cross_fills": {"fill_model": {"cross_fills": False}},
            # heavier tail: the same median but 0.5-3 s lag spikes ten times as often
            "latency_spikes": {"latency": {"feed_delay_quantiles_ms": [[0.0, 130.0], [0.5, 239.0], [0.9, 400.0],
                                                                       [0.95, 900.0], [0.99, 1800.0], [1.0, 3000.0]]}},
            "higher_latency": {"latency": {"feed_delay_quantiles_ms": [[0.0, 260.0], [0.5, 478.0], [0.9, 592.0],
                                                                       [0.99, 1544.0], [1.0, 3000.0]],
                                           "feed_latency_ms": 480.0, "order_latency_ms": 264.0,
                                           "report_latency_ms": 480.0}},
            "pessimistic_all": {"fill_model": {"require_trade_through": True,
                                               "cancel_assumption": "all_behind",
                                               "initial_queue_factor": 1.5, "cross_fills": False},
                                "latency": {"feed_delay_quantiles_ms": [[0.0, 260.0], [0.5, 478.0], [0.9, 592.0],
                                                                       [0.99, 1544.0], [1.0, 3000.0]],
                                            "feed_latency_ms": 480.0, "order_latency_ms": 264.0,
                                           "report_latency_ms": 480.0}},
        },
    },
    "paper": {
        "log_interval_s": 10.0,           # periodic PnL line
        "heartbeat_s": 1.0,               # engine clock tick when the feed is quiet
        # Live trades and depth come from two websockets and can arrive out of exchange-time order.
        # Events are held this long (real time) and released sorted like a replay would sort them.
        "reorder_hold_ms": 250.0,
        "reorder_poll_ms": 50.0,          # how often the buffer is checked for releasable events
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    """Return a new dict: ``base`` with ``over`` merged in recursively."""
    # Nested dicts are merged key by key (so overriding latency.order_latency_ms keeps the other
    # latency settings); anything else, including lists, is replaced wholesale. "(over or {})" guards
    # against None. ".items()" yields (key, value) pairs.
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("params",):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | Path) -> dict:
    """Read a YAML run config and fill in defaults."""
    # "with open(...) as fh:" is a context manager: it guarantees the file is closed when the block
    # ends, even if an error happens. utf-8-sig also tolerates the invisible BOM Windows editors add.
    with open(path, "r", encoding="utf-8-sig") as fh:
        user = yaml.safe_load(fh) or {}
    cfg = deep_merge(DEFAULTS, user)
    validate(cfg)
    return cfg


# Fail early with a clear message instead of producing a silently meaningless backtest, e.g.
# asking for a queue-based fill model when the data has no quotes/depth to build a queue from.
def validate(cfg: dict) -> None:
    validate_latency(cfg)
    # no_cross_fills is a REQUIRED sensitivity row (rule: if net PnL is better WITH cross fills than
    # without, the result is suspect), so a config may not switch it off.
    sens = cfg["sensitivity"]
    if "no_cross_fills" not in sens["variants"]:     # required even if the other variants are disabled
        raise ValueError("sensitivity.variants must include no_cross_fills (required row)")
    ft = cfg["fill_model"]["type"]
    if ft not in ("quote_trade", "l2"):
        raise ValueError(f"fill_model.type must be quote_trade or l2, got {ft!r}")
    streams = set(cfg["data"]["streams"])
    need = "quotes" if ft == "quote_trade" else "depth"
    if need not in streams:
        raise ValueError(f"fill_model.type={ft} needs the '{need}' stream in data.streams "
                         f"(have {sorted(streams)}). Trades-only data cannot support a passive fill model.")
    if "trades" not in streams:
        raise ValueError("data.streams must include 'trades' (needed for fills)")


# Zero-latency guard. If the strategy reacts instantly (0 ms) to a print and its order also lands
# instantly, it can trade against the NEXT print of the very same millisecond, i.e. it acts on
# information it could not have had. So every latency must be at least min_latency_ms, unless the
# explicit test-only flag allow_zero_latency is set (unit tests use it to keep event order simple).
def validate_latency(cfg: dict) -> None:
    lat = cfg["latency"]
    # The empirical delay table must be a proper distribution: probabilities rising from 0 to 1 and
    # delays never decreasing (otherwise inverse-CDF sampling would be meaningless).
    tbl = lat.get("feed_delay_quantiles_ms")
    if tbl:
        ps, ds = [p for p, _ in tbl], [d for _, d in tbl]
        if ps[0] != 0.0 or ps[-1] != 1.0 or ps != sorted(ps) or ds != sorted(ds):
            raise ValueError("latency.feed_delay_quantiles_ms must go from probability 0.0 to 1.0 with "
                             "non-decreasing probabilities and delays")
    if lat.get("allow_zero_latency"):
        return
    floor = lat["min_latency_ms"]
    if tbl and tbl[0][1] < floor:
        raise ValueError(f"latency.feed_delay_quantiles_ms starts below min_latency_ms={floor}")
    for key in ("feed_latency_ms", "order_latency_ms", "report_latency_ms"):
        v = lat.get(key)
        if v is not None and v < floor:
            raise ValueError(f"latency.{key}={v} is below latency.min_latency_ms={floor}; zero latency leaks "
                             "same-millisecond information (set latency.allow_zero_latency only in tests)")


# Config uses milliseconds (easy to read); the code uses integer nanoseconds (exact, no float drift).
# "not ms" is True for None and 0, both meaning "no latency".
def ms_to_ns(ms: float | None) -> int:
    return 0 if not ms else int(round(float(ms) * 1_000_000))
