"""Shared builders for the hftbacktest (2.4.4) learning track.

Everything that is the SAME for every hftbacktest run lives here, so the example strategy, the GLFT
strategy and the self-test all build their simulated exchange identically:

    build_asset(...)  ->  a configured ``BacktestAsset`` (data files, tick/lot, fees, queue model,
                          exchange model, latency model)
    describe_asset_config(...) -> the same choices as a plain dict, for config.json

Verified against the INSTALLED source (.venv-hft/Lib/site-packages/hftbacktest/__init__.py plus a
runtime ``dir()`` of the Rust class, whose methods have no .pyi stub):

    linear_asset(contract_size)                 USDT-margined perp: PnL = qty * price change
    tick_size(x) / lot_size(x)
    trading_value_fee_model(maker, taker)       fee = rate * price * qty; a NEGATIVE rate is a rebate
    risk_adverse_queue_model()                  an order only moves up the queue when trades hit it
    power_prob_queue_model(n), power_prob_queue_model2(n), power_prob_queue_model3(n)
    log_prob_queue_model(), log_prob_queue_model2()
    l3_fifo_queue_model()                       needs Level-3 (per-order) data, not our L2 data
    partial_fill_exchange() / no_partial_fill_exchange()
    constant_order_latency(entry, resp)         both in the data's time unit (ns)
    intp_order_latency(array_or_files, offset)  linear interpolation of a (req_ts, exch_ts, resp_ts) table
    roi_lb(px) / roi_ub(px)                     range of interest, in PRICE (not ticks)
    last_trades_capacity(n)                     0 = the strategy cannot read ``hbt.last_trades``
    initial_snapshot(file_or_array)
    data(list_of_files_or_arrays)

Project units (CLAUDE.md): prices in USDT, sizes in ETH, timestamps integer nanoseconds UTC.

Latency choice (recorded in config.json by run_backtest.py)
-----------------------------------------------------------
* FEED latency is not a parameter: hftbacktest replays each event at its ``local_ts`` (when WE
  received it) while the exchange processes it at ``exch_ts``.  Our recorder stored both, so the real
  ~240 ms median feed delay is already in the data.
* ORDER latency default = constant: entry 160 ms (~mean of our engine's 132 ms + uniform 0-59 ms jitter
  = 161.5 ms; rounded) and response 240 ms (our engine's report latency), total round trip 400 ms.
* Option ``kind="intp_jitter"``: a latency table on a fixed grid where the entry latency is
  132 ms + U(0, 59) ms with a fixed seed.  hftbacktest INTERPOLATES linearly between table rows by
  request time, so the jitter is smooth between grid points (grid default 100 ms), not an independent
  draw per order.  That is close to, but not identical with, our engine's per-order jitter.
"""
# Lazy type hints: "float | None" stays as text at import time (works on any Python 3.9+ too).
from __future__ import annotations

import numpy as np

# Event flags (EXCH_EVENT = "exchange processes this row", LOCAL_EVENT = "we receive this row") and the
# numeric codes of the event kinds.  All are plain integers exported by the package.
from hftbacktest import (BacktestAsset, BUY_EVENT, DEPTH_SNAPSHOT_EVENT, SELL_EVENT)
from hftbacktest.data.utils.feed_order_latency import order_latency_dtype

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000

# ------------------------------------------------------------------------------------------
# Defaults.  They live in ONE dict so they can be dumped into config.json (no magic numbers in the
# code below).  Sources: tick/lot = config/backtest/glft_live.yaml `exchange:`; fees likewise;
# latency = src/mm/backtest/README.md and spec glft-perp.md section 4.6 (~400 ms round trip).
# ------------------------------------------------------------------------------------------
DEFAULTS: dict = {
    "contract_size": 1.0,         # linear perp quoted in USDT, size in ETH: 1 contract = 1 ETH
    "tick_size": 0.01,            # USDT
    "lot_size": 0.001,            # ETH
    "maker_fee": 0.0002,          # fraction of traded value, paid (positive = cost)
    "taker_fee": 0.0005,
    "queue_model": "risk_adverse",  # risk_adverse | power_prob | power_prob2 | power_prob3 | log_prob | log_prob2
    "queue_n": 2.0,               # exponent n of the power_prob* models (ignored by the others)
    # False = NoPartialFillExchange.  PartialFillExchange (our engine allows partial fills) is the nicer match but
    # in hftbacktest 2.4.4 it makes ``hbt.elapse`` return error 14 after 25-60% of a day on our data (any queue
    # model, latency or requote mode) and the engine then stays stuck, so it cannot complete a day.
    "partial_fill": False,
    "roi_frac": 0.5,              # ROI = [px*(1-frac), px*(1+frac)] around the snapshot mid
    "last_trades_capacity": 0,    # >0 only if the strategy reads hbt.last_trades(); costs memory
    "latency": {
        "kind": "constant",       # constant | intp_jitter
        "entry_ns": 160 * NS_PER_MS,   # order entry latency, local -> exchange
        "resp_ns": 240 * NS_PER_MS,    # response latency, exchange -> local
        # only used by kind == "intp_jitter":
        "entry_base_ns": 132 * NS_PER_MS,
        "entry_jitter_ns": 59 * NS_PER_MS,   # entry = base + U(0, jitter)
        "grid_ns": 100 * NS_PER_MS,          # one table row per grid step
        "seed": 42,
    },
}


def merged_config(**overrides) -> dict:
    """Return DEFAULTS with ``overrides`` applied (the 'latency' dict is merged key by key).

    ``**overrides`` collects keyword arguments into a dict, so callers write
    ``merged_config(queue_model="log_prob2", latency={"entry_ns": 200_000_000})``."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}   # deep-ish copy
    for key, value in overrides.items():
        if key not in cfg:
            raise ValueError(f"unknown config key {key!r}; valid keys: {sorted(cfg)}")
        if isinstance(value, dict):
            cfg[key].update(value)          # merge dicts (latency) instead of replacing them
        else:
            cfg[key] = value
    return cfg


# ------------------------------------------------------------------------------------------
# Data helpers
# ------------------------------------------------------------------------------------------
def load_events(path: str) -> np.ndarray:
    """Load one hftbacktest event file (.npz with key 'data') into a structured numpy array."""
    return np.load(path)["data"]


def data_time_range(data_files: list[str]) -> tuple[int, int]:
    """(first local_ts, last local_ts) in ns over the files.  Assumes files are in time order, so only
    the first and last file are opened.  Rows with local_ts <= 0 (exchange-only rows) are ignored."""
    def _min_max(path: str) -> tuple[int, int]:
        ts = load_events(path)["local_ts"]
        ts = ts[ts > 0]                     # boolean-mask indexing: keep only the True positions
        return int(ts.min()), int(ts.max())
    first = _min_max(data_files[0])[0]
    last = _min_max(data_files[-1])[1]
    return first, last


def feed_latency_stats(data_files: list[str]) -> dict:
    """Realised FEED latency (local_ts - exch_ts) in ms over rows that carry both flags.

    Percentiles come from every 10th row of each file (plenty for p99)."""
    from hftbacktest import EXCH_EVENT, LOCAL_EVENT
    chunks = []
    for f in data_files:
        a = load_events(f)
        both = (a["ev"] & (EXCH_EVENT | LOCAL_EVENT)) == (EXCH_EVENT | LOCAL_EVENT)
        chunks.append(((a["local_ts"] - a["exch_ts"])[both][::10]) / 1e6)
    x = np.concatenate(chunks)
    return {"n_sampled": int(len(x)), "min_ms": float(x.min()), "p50_ms": float(np.percentile(x, 50)),
            "p90_ms": float(np.percentile(x, 90)), "p99_ms": float(np.percentile(x, 99)), "max_ms": float(x.max())}


def mid_from_snapshot(snapshot) -> float:
    """Rough mid price from an initial-snapshot array or file: highest bid and lowest ask.

    A snapshot is a set of rows with ev = DEPTH_SNAPSHOT_EVENT | (BUY_EVENT for bids, SELL_EVENT for asks),
    ``px`` the level price and ``qty`` the size there.  ``&`` below is a bitwise AND used to test flags."""
    arr = load_events(snapshot) if isinstance(snapshot, str) else snapshot
    ev = arr["ev"]
    is_bid = (ev & BUY_EVENT) != 0
    is_ask = (ev & SELL_EVENT) != 0
    live = arr["qty"] > 0
    best_bid = arr["px"][is_bid & live].max()
    best_ask = arr["px"][is_ask & live].min()
    return float((best_bid + best_ask) / 2.0)


def make_jitter_latency_table(start_ts: int, end_ts: int, entry_base_ns: int, entry_jitter_ns: int,
                              resp_ns: int, grid_ns: int, seed: int) -> np.ndarray:
    """Build the (req_ts, exch_ts, resp_ts) table for ``intp_order_latency``.

    One row per ``grid_ns`` of request time; entry latency = base + U(0, jitter) drawn with a FIXED seed
    (reproducible, CLAUDE.md rule); response latency constant.  Columns come from
    ``order_latency_dtype`` (req_ts, exch_ts, resp_ts, _padding; all int64)."""
    # np.arange(a, b, step) = a, a+step, ... (< b).  We pad the end by 10 s so late orders still interpolate.
    req_ts = np.arange(start_ts, end_ts + 10 * NS_PER_S, grid_ns, dtype=np.int64)
    rng = np.random.default_rng(seed)       # numpy's modern random generator, seeded
    entry = entry_base_ns + rng.uniform(0.0, entry_jitter_ns, size=len(req_ts))
    table = np.zeros(len(req_ts), dtype=order_latency_dtype)   # zeros: the padding column stays 0
    table["req_ts"] = req_ts
    table["exch_ts"] = req_ts + entry.astype(np.int64)         # time the exchange sees the order
    table["resp_ts"] = table["exch_ts"] + int(resp_ns)         # time we see the answer
    return table


# ------------------------------------------------------------------------------------------
# The builder
# ------------------------------------------------------------------------------------------
def build_asset(data_files: list[str], snapshot_file: str | None = None, *, cfg: dict | None = None,
                data_arrays: list[np.ndarray] | None = None, snapshot_array: np.ndarray | None = None,
                roi_lb: float | None = None, roi_ub: float | None = None):
    """Return a configured ``BacktestAsset`` (not yet a backtester; see ``ROIVectorMarketDepthBacktest``).

    data_files     hourly event files in time order (paths).  Pass [] and ``data_arrays`` for in-memory data
                   (used by the synthetic self-test).
    snapshot_file  initial-book file (events at the first timestamp); None = start with an empty book.
    cfg            result of :func:`merged_config` (None = all defaults).
    roi_lb/roi_ub  price bounds for the ROI depth; default = snapshot mid * (1 -/+ roi_frac).  Prices outside
                   the ROI are silently ignored by the depth, so make it wide.
    """
    cfg = cfg or merged_config()
    asset = BacktestAsset()        # the Rust builder; each method below mutates it and returns it

    # ---- feed data: files (streamed from disk, loaded in a background thread) or in-memory arrays.
    # The Rust side keeps a pointer into numpy memory for arrays, so callers must keep them alive
    # until the backtest is closed (we return nothing that owns them; the caller holds the list).
    if data_files:
        asset.data(list(data_files))
    for arr in (data_arrays or []):
        asset.add_data(arr)

    # ---- initial book
    if snapshot_file is not None:
        asset.initial_snapshot(snapshot_file)
    elif snapshot_array is not None:
        asset.initial_snapshot(snapshot_array)

    # ---- instrument: linear (USDT-margined) perp, tick and lot
    asset.linear_asset(cfg["contract_size"]).tick_size(cfg["tick_size"]).lot_size(cfg["lot_size"])

    # ---- fees.  trading_value_fee_model(maker, taker): rate on traded value, positive = we PAY
    # (docs: "A negative fee represents rebates").  Maker first, taker second.
    asset.trading_value_fee_model(cfg["maker_fee"], cfg["taker_fee"])

    # ---- queue-position model: how an order of ours advances in the queue at its price level.
    qm = cfg["queue_model"]
    if qm == "risk_adverse":
        asset.risk_adverse_queue_model()
    elif qm == "power_prob":
        asset.power_prob_queue_model(cfg["queue_n"])
    elif qm == "power_prob2":
        asset.power_prob_queue_model2(cfg["queue_n"])
    elif qm == "power_prob3":
        asset.power_prob_queue_model3(cfg["queue_n"])
    elif qm == "log_prob":
        asset.log_prob_queue_model()
    elif qm == "log_prob2":
        asset.log_prob_queue_model2()
    else:
        raise ValueError(f"unknown queue_model {qm!r}")

    # ---- exchange model: partial fills or all-or-nothing.
    if cfg["partial_fill"]:
        asset.partial_fill_exchange()
    else:
        asset.no_partial_fill_exchange()

    # ---- order latency
    lat = cfg["latency"]
    if lat["kind"] == "constant":
        asset.constant_order_latency(int(lat["entry_ns"]), int(lat["resp_ns"]))
    elif lat["kind"] == "intp_jitter":
        if data_arrays and not data_files:
            ts = np.concatenate([a["local_ts"] for a in data_arrays])
            ts = ts[ts > 0]
            start, end = int(ts.min()), int(ts.max())
        else:
            start, end = data_time_range(list(data_files))
        table = make_jitter_latency_table(start, end, int(lat["entry_base_ns"]), int(lat["entry_jitter_ns"]),
                                          int(lat["resp_ns"]), int(lat["grid_ns"]), int(lat["seed"]))
        asset.intp_order_latency(table)     # copied by the Rust side (it takes pointer + length)
    else:
        raise ValueError(f"unknown latency kind {lat['kind']!r}")

    # ---- range of interest (the "ROIVector" depth stores only prices inside [lb, ub])
    if roi_lb is None or roi_ub is None:
        if snapshot_file is None and snapshot_array is None:
            raise ValueError("give roi_lb/roi_ub or a snapshot to derive them from")
        mid = mid_from_snapshot(snapshot_file if snapshot_file is not None else snapshot_array)
        roi_lb = mid * (1.0 - cfg["roi_frac"]) if roi_lb is None else roi_lb
        roi_ub = mid * (1.0 + cfg["roi_frac"]) if roi_ub is None else roi_ub
    asset.roi_lb(float(roi_lb)).roi_ub(float(roi_ub))

    # ---- last-trades buffer (0 = disabled).  Our strategies read fills from the order dict instead.
    asset.last_trades_capacity(int(cfg["last_trades_capacity"]))
    return asset


def describe_asset_config(cfg: dict, data_files: list[str], snapshot_file: str | None,
                          roi: tuple[float, float] | None = None) -> dict:
    """Plain-dict description of the exchange simulation (goes into config.json)."""
    return {"hftbacktest_version": "2.4.4", "exchange": cfg, "data_files": list(data_files),
            "snapshot_file": snapshot_file, "roi_price": list(roi) if roi else None,
            "feed_latency": "taken from the data (local_ts - exch_ts)"}
