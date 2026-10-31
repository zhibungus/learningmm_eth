# hftbacktest: what it is, how to feed it, and how its GLFT differs from ours

Purpose: use `hftbacktest` as an **independent cross-check** of our own engine (`src/mm/backtest/README.md`, "Build vs reuse") for the fee-aware GLFT strategy (`specs/glft-perp.md` v1.2, `src/mm/strategies/glft.py`).
Status: desk research only, done 2026-10-30. **Nothing here has been installed or run.** Another session should check the symbol list in §8 against an installed copy.

## Key ideas

- Latest release: **hftbacktest 2.4.4** (PyPI, 2025-12-10). Git tags `py-v2.4.4` and `rust-v0.9.4` were made the same day. There is no newer release as of 2026-10-30.
- **Rust core with a thin Python layer.** You write the strategy as a numba `@njit` function that drives an `hbt` object.
- **Windows wheels exist** (`win_amd64`, cp311–cp314). In practice only **Python 3.11–3.13** work, because the package needs `numba~=0.61`, and numba 0.61 has no cp314 wheels.
- **It cannot go into our current `.venv`.** That venv pins `numpy==2.5.4`, but hftbacktest needs `numpy>=2.0,<2.3`. Use a separate venv.
- Data is one flat **event array** (`event_dtype`). Each row carries both clocks (`exch_ts`, `local_ts`). Feed latency is **in the data**, not in a model.
- Depth rows carry the **absolute** quantity at a level, and a quantity of 0 deletes the level.
- Set the **queue model explicitly.** The builder default is `LogProbQueueModel2`, not the conservative `RiskAdverse` model.
- Its GLFT tutorial uses the same `c1`/`c2` algebra as our §4.1. Everything around it differs: tick units, no fee term, hand-set γ, skew scaled down 20× by a fudge factor, rolling recalibration, a 20-level grid, and a different A/k estimator (§6).

Evidence tags used below:

| Tag | Meaning |
|---|---|
| **[Src]** | Read in the source at tag `py-v2.4.4` (Python) or `rust-v0.9.4` (Rust core) |
| **[Docs]** | Read on readthedocs `latest`. Some pages may predate 2.4.4 |
| **[Mem]** | From memory. Not verified in this session |
| **[Inf]** | My own inference |

---

## 1. Version, packaging, Python/Rust split

| Item | Value | Source |
|---|---|---|
| Latest PyPI version | **2.4.4**, uploaded 2025-12-10 | [Src] [PyPI](https://pypi.org/project/hftbacktest/), [PyPI JSON](https://pypi.org/pypi/hftbacktest/json), [tags](https://github.com/nkaz001/hftbacktest/tags) |
| Recent history | 2.4.0 (2025-08-03), 2.4.1 (08-11), 2.4.2 (08-24), 2.4.3 (09-30), 2.4.4 (12-10). 2.0.0 shipped 2024-08-14 and 1.8.4 on 2024-06-10 | tags page, PyPI JSON |
| `Requires-Python` | `>=3.11` (classifiers list 3.11–3.13) | [pyproject.toml](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/pyproject.toml) |
| `requires_dist` | `numpy<2.3,>=2.0`, `numba~=0.61`, `polars`, `matplotlib`, `holoviews`, extra `databento` | PyPI JSON |
| Build | maturin. The extension module is `hftbacktest._hftbacktest` (Rust, pyo3) | pyproject.toml |
| Windows wheels | `cp311`, `cp312`, `cp313`, `cp314` × `win_amd64`. No win32 or arm64 wheels | PyPI files list |
| Other wheels | manylinux_2_28 and musllinux_1_2 (x86_64, aarch64, armv7l), macOS x86_64 and arm64, free-threaded cp313t/cp314t, PyPy 3.11 | PyPI files list |

**Python 3.14 does not work in practice.** numba 0.61.2 ships wheels only for cp310–cp313 and itself needs `numpy<2.3` ([numba JSON](https://pypi.org/pypi/numba/0.61.2/json)). [Inf] So `pip install hftbacktest` on 3.14 should fail to resolve numba, even though a cp314 hftbacktest wheel exists.

**How the work is split between Rust and Python** [Src]

- **Rust crate `hftbacktest`** ([`hftbacktest/src`](https://github.com/nkaz001/hftbacktest/tree/rust-v0.9.4/hftbacktest/src)). It does all the simulation: depth, exchange and local processors, queue, latency and fee models.
- **Rust pyo3 layer** ([`py-hftbacktest/src/lib.rs`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/src/lib.rs)). It provides the `BacktestAsset` builder plus `build_hashmap_backtest` and `build_roivec_backtest`.
- **Python layer** ([`hftbacktest/binding.py`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/hftbacktest/binding.py)). numba `jitclass` wrappers call the Rust C ABI through `ctypes` function pointers. That is why `hbt` can be used **inside `@njit`**.
- **Strategy code.** You write a Python function decorated with `@njit` that takes `hbt` (and optionally `recorder.recorder`).

---

## 2. Data format

### 2.1 `event_dtype` [Src: [`types.py`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/hftbacktest/types.py)]

A numpy structured array, `align=True`, 64 bytes per row:

| Field | dtype | Meaning |
|---|---|---|
| `ev` | u8 | Event code (low bits) OR'ed with flag bits (below) |
| `exch_ts` | i8 | Exchange timestamp. ns by convention (2.x moved from µs to ns, "not strictly enforced" [Docs migration2]) |
| `local_ts` | i8 | Time our machine received the event, ns |
| `px` | f8 | Price |
| `qty` | f8 | Quantity. For depth rows this is the **absolute** quantity at the level (§2.3) |
| `order_id` | u8 | Only used for L3 (market-by-order) data |
| `ival` | i8 | Reserved |
| `fval` | f8 | Reserved |

### 2.2 Event codes and flags [Src: `types.py`]

| Constant | Value | Kind |
|---|---|---|
| `DEPTH_EVENT` | 1 | code |
| `TRADE_EVENT` | 2 | code |
| `DEPTH_CLEAR_EVENT` | 3 | code |
| `DEPTH_SNAPSHOT_EVENT` | 4 | code |
| `DEPTH_BBO_EVENT` | 5 | code |
| `ADD_ORDER_EVENT` / `CANCEL_ORDER_EVENT` / `MODIFY_ORDER_EVENT` / `FILL_EVENT` | 10 / 11 / 12 / 13 | code (L3) |
| `EXCH_EVENT` | `1 << 31` | flag: the exchange processor handles this row at `exch_ts` |
| `LOCAL_EVENT` | `1 << 30` | flag: the local (strategy-side) processor handles this row at `local_ts` |
| `BUY_EVENT` | `1 << 29` | flag: bid side for depth rows, **buyer-initiated** for trade rows |
| `SELL_EVENT` | `1 << 28` | flag: ask side for depth rows, **seller-initiated** for trade rows |
| `ALL_ASSETS` | -1 | used for asset arguments |
| `UNTIL_END_OF_DATA` | `2**63-1` | used for timeouts |
| `WAIT_ORDER_RESPONSE_NONE` / `_ANY` | -1 / -2 | marked "todo" in the source |

- **Codes are small integers, not bits.** Combine one code with flags using bitwise OR, for example `ev = EXCH_EVENT | LOCAL_EVENT | DEPTH_EVENT | BUY_EVENT`.
- Test a flag with `ev & BUY_EVENT == BUY_EVENT`, as the tutorial does. Recover the code with `ev & 0xFF` [Inf].
- **A row is ignored by a processor unless it carries that processor's flag.** v1 used `ts = -1` to mark invalid rows instead [Docs [migration2](https://hftbacktest.readthedocs.io/en/latest/migration2.html)].

### 2.3 Depth semantics [Src: Rust [`hashmapmarketdepth.rs`](https://github.com/nkaz001/hftbacktest/blob/rust-v0.9.4/hftbacktest/src/depth/hashmapmarketdepth.rs), [`proc/local.rs`](https://github.com/nkaz001/hftbacktest/blob/rust-v0.9.4/hftbacktest/src/backtest/proc/local.rs)]

- **`DEPTH_EVENT` replaces the level.** The quantity is stored as given (`*entry = qty`).
  - If `round(qty/lot_size) == 0`, the level is **removed**, and the best bid or ask is rescanned.
  - This matches Binance `depthUpdate`, which sends absolute quantities with 0 meaning delete.
- **`DEPTH_CLEAR_EVENT | BUY_EVENT` with `px = p`** removes bid levels from the best bid **down to `p`, inclusive**.
  - `SELL_EVENT` mirrors this upward on the ask side.
  - A non-finite `px` clears the whole side.
  - `DEPTH_CLEAR_EVENT` with no side flag clears both sides.
- **`DEPTH_SNAPSHOT_EVENT` rows in the stream** are applied **like depth updates**: the same branch, `update_bid_depth`/`update_ask_depth`.
  - The usual pattern is a `DEPTH_CLEAR_EVENT` down to the deepest snapshot price, followed by one `DEPTH_SNAPSHOT_EVENT` per level. The Binance converter does exactly this (§2.5).
- **`initial_snapshot(...)`** on the builder loads a separate array or `.npz` via `apply_snapshot`, which rebuilds the book from scratch.
  - BUY rows become bids and SELL rows become asks. Quantities are assigned, not summed.
  - Zero-quantity rows are **not** filtered here [Src].
  - Binance futures runs 24/7, so each day's file needs the previous day's end-of-day snapshot. Build it with `hftbacktest.data.utils.snapshot.create_last_snapshot([...files], tick_size=..., lot_size=..., output_snapshot_filename=..., initial_snapshot=...)` [Docs [Data Preparation](https://hftbacktest.readthedocs.io/en/latest/tutorials/Data%20Preparation.html). I did not read the signature in source].
- **Trades** (`LOCAL_EVENT | TRADE_EVENT`) are kept for `hbt.last_trades()` **only if `last_trades_capacity > 0`** [Src local.rs].

### 2.4 Ordering and the two clocks

**Rules** [Docs [data.html](https://hftbacktest.readthedocs.io/en/latest/data.html)] [Src validation]

1. Rows flagged `EXCH_EVENT` must be non-decreasing in `exch_ts`.
2. Rows flagged `LOCAL_EVENT` must be non-decreasing in `local_ts`.
3. Feed latency `local_ts − exch_ts` must be **positive**.

The two clocks are **not** required to be sorted together. A row can sit at different places in the two orders, which is why it gets duplicated (below).

**Utilities** in `hftbacktest.data` [Src: [`data/validation.py`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/hftbacktest/data/validation.py)]:

| Function | What it does |
|---|---|
| `correct_local_timestamp(data, base_latency)` (`@njit`, in place) | Computes `min_lat = min(local_ts − exch_ts)`. **Only if `min_lat < 0`**, adds `−min_lat + base_latency` to **every** `local_ts`. The minimum latency then becomes `base_latency`. (The docstring formula has the sign the other way round. The code shifts forward.) |
| `correct_event_order(data, sorted_exch_index, sorted_local_index)` | Pass `argsort(exch_ts, kind='mergesort')` and `argsort(local_ts, kind='mergesort')`. It two-pointer merges both orders into a buffer of size `2·len(data)`. A row that is at the head of both orders is written **once** with `EXCH_EVENT \| LOCAL_EVENT`. Otherwise it is written **twice**, once with `EXCH_EVENT` and once with `LOCAL_EVENT`. It asserts that `ev`, `px` and `qty` match on merged rows |
| `validate_event_order(data)` | Plain Python. Raises `ValueError('exchange events are out of order.')` or `('local events are out of order.')`. Ties are allowed. It does not check the two clocks against each other |

### 2.5 Binance USD-M converter [Src: [`data/utils/binancefutures.py`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/hftbacktest/data/utils/binancefutures.py)]

```
convert(input_filename, output_filename=None, opt='' | 'm' | 't' | 'mt',
        base_latency=0, combined_stream=True, buffer_size=100_000_000) -> ndarray[event_dtype]
```

- **Input format.** A gzip text file in which each line is `<19-digit local ns timestamp> <websocket JSON>`. This is the format of the author's Data Collector. The docstring example shows 16-digit values that would not parse; the code slices `line[:19]`.
- **The two clocks.**
  - `exch_ts = T × 1_000_000`. `T` is the transaction time in ms; `E` (event time) is commented out.
  - `local_ts` = the line prefix, used as is.
- **`depthUpdate`.** Each `b` row becomes `DEPTH_EVENT|BUY_EVENT` and each `a` row becomes `DEPTH_EVENT|SELL_EVENT`, with absolute quantities.
- **`trade` (raw trade stream).** Rows are kept only where `X == 'MARKET'`. Side is `SELL_EVENT if m else BUY_EVENT`, which is the **initiator** side.
  - **`aggTrade` is not handled.** Our recorder's trade stream would need our own converter [Inf].
- **REST snapshot** (a line with no `data` key, so only when `combined_stream=True`):
  1. `DEPTH_CLEAR_EVENT|BUY_EVENT` at `px = bids[-1][0]`, then one `DEPTH_SNAPSHOT_EVENT|BUY_EVENT` per bid.
  2. The same for asks.
- **`opt`.**
  - `'m'` adds `markPriceUpdate` rows with codes 100 (index), 101 (mark price) and 102 (funding rate).
  - `'t'` adds `bookTicker` rows with codes 103 (bid) and 104 (ask).
  - These are plain integer codes. Flags are added later by `correct_event_order`.
- **After parsing**, the converter runs `correct_local_timestamp(base_latency)`, then `correct_event_order` (mergesort indices), then `validate_event_order`. It then optionally runs `np.savez_compressed(output_filename, data=...)`. The `.npz` key is **`data`**.

---

## 3. Builder and backtest objects [Src: `lib.rs` @ py-v2.4.4, [`__init__.py`](https://github.com/nkaz001/hftbacktest/blob/py-v2.4.4/py-hftbacktest/hftbacktest/__init__.py)]

Every `BacktestAsset()` method returns `self`, so calls can be chained.

| Method | Args | Notes |
|---|---|---|
| `data(x)` | `str \| list[str] \| ndarray \| list[ndarray]` | `.npz` paths or event arrays |
| `add_file(path)` / `add_data(arr)` | | lower level |
| `initial_snapshot(x)` | path or event array | |
| `linear_asset(contract_size)` / `inverse_asset(contract_size)` | float | default `linear_asset(1.0)` |
| `constant_order_latency(entry_ns, resp_ns)` | int, int | **`constant_latency(...)` is a deprecated alias** that raises a `DeprecationWarning`. The Getting Started docs still use the old name |
| `intp_order_latency(data, latency_offset=0)` | paths or ndarray | see §4 |
| `latency_offset(ns)` | int | shifts **feed** latency (doc comment: "useful for cross-site data") |
| `risk_adverse_queue_model()` | | |
| `log_prob_queue_model()` / `log_prob_queue_model2()` | | **default is `LogProbQueueModel2`** |
| `power_prob_queue_model(n)` / `power_prob_queue_model2(n)` / `power_prob_queue_model3(n)` | float | |
| `l3_fifo_queue_model()` | | L3 data only |
| `no_partial_fill_exchange()` (default) / `partial_fill_exchange()` | | |
| `trading_value_fee_model(maker, taker)` | fractions of notional; **negative = rebate** | default `(0, 0)` |
| `trading_qty_fee_model(maker, taker)` / `flat_per_trade_fee_model(maker, taker)` | | |
| `tick_size(x)` / `lot_size(x)` | float | |
| `last_trades_capacity(n)` | int | default 0, which means **no** trades are stored |
| `roi_lb(px)` / `roi_ub(px)` | float | used only by the ROI depth |
| `parallel_load(bool)` | | default `True` (background loading, uses more memory) |

**The two backtest types** (both are `fn(list[BacktestAsset])`, one asset per list entry):

- **`HashMapMarketDepthBacktest`** keeps depth in a hash map (any price range).
  - `depth.snapshot()` / `snapshot_free(arr)` are available.
- **`ROIVectorMarketDepthBacktest`** keeps a dense array between `roi_lb` and `roi_ub`, which is faster.
  - It exposes `bid_depth`, `ask_depth` (indexed by `price_tick − roi_lb_tick`), `roi_lb_tick` and `roi_ub_tick`.
  - **Bug [Src binding.py]:** `ROIVectorMarketDepth.best_bid_qty` and `best_ask_qty` call `roivecdepth_best_bid` and `_ask`, so they return the **price**. Use `bid_qty_at_tick(best_bid_tick)` instead.

---

## 4. Latency and queue models

### 4.1 Latency [Docs [latency_models](https://hftbacktest.readthedocs.io/en/latest/latency_models.html)] [Src enum names]

- **Feed latency is not a model.** It is `local_ts − exch_ts` of each data row. You can shift it globally with `latency_offset`.
- **`ConstantLatency{entry_latency, resp_latency}`** (ns).
  - Entry: local to exchange.
  - Response: exchange to local.
- **`IntpOrderLatency`** linearly interpolates measured order round trips.
  - Rows are structured as `req_ts, exch_ts, resp_ts, _padding`, all `i8` ns, with `_padding = 0`.
  - Measure them live by sending unexecutable orders regularly.
- **Generating order latency from feed latency** (tutorial [Order Latency Data](https://hftbacktest.readthedocs.io/en/latest/tutorials/Order%20Latency%20Data.html), notebook code, not a library function):
  - `entry = mul_entry·feed_lat + offset_entry`
  - `resp = mul_resp·feed_lat + offset_resp`
  - `exch_ts = local_ts + entry`
  - `resp_ts = exch_ts + resp`
  - The example uses `mul_entry=4`, `mul_resp=3`. Output is saved with `np.savez_compressed(..., data=...)`.

### 4.2 Exchange (fill) models [Docs [order_fill](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)]

- **`NoPartialFillExchange`**: all-or-nothing. A resting buy fills if any of these happens:
  - the best ask ≤ our price (cross);
  - a sell trade prints **below** our price (trade-through);
  - a sell trade prints **at** our price **and** our queue-ahead estimate has reached zero.
- **Taker orders** fill fully at the best price whatever their size.
- **`PartialFillExchange`**: the same full-fill conditions, plus a partial fill (up to the remaining trade quantity) when we are at the front of the queue and the trade is at our price.
- **GTX that would cross** is not accepted. The order ends `EXPIRED` [Docs Getting Started].

### 4.3 Queue models [Src: [`models/queue.rs`](https://github.com/nkaz001/hftbacktest/blob/rust-v0.9.4/hftbacktest/src/backtest/models/queue.rs)]

The data is market-by-price, so our place in the queue must be estimated. `front` is the quantity ahead of us and `back` the quantity behind. At order arrival, `front` = the level quantity.

- **RiskAdverse** (the code spells it "Adverse"; it means risk-*averse*): the most pessimistic model.
  - A trade at our price does `front −= trade_qty`.
  - A depth change does `front = min(front, new_level_qty)`, so cancellations are assumed to come from **behind** us unless the level falls below `front`.
  - We fill when `round(−front/lot) > 0`, that is, once more volume has traded than was ahead of us.
- **Prob models.** On a level **decrease** `chg` (trades already counted are excluded), a fraction `p` is assumed to have come from behind us:
  - `front' = min(front − (1−p)·chg + min(back − p·chg, 0), new_qty)`, with `back = prev_qty − front`.

| Builder call | `p = prob(front, back)` | Intuition |
|---|---|---|
| `power_prob_queue_model(n)` | `back^n / (back^n + front^n)` | Larger n means a cancel is more likely to come from the side with more volume. The tutorials use n = 2 or 3 |
| `power_prob_queue_model2(n)` | `back^n / (back+front)^n` | |
| `power_prob_queue_model3(n)` | `1 − (front/(front+back))^n` | |
| `log_prob_queue_model()` | `ln(1+back) / (ln(1+back) + ln(1+front))` | Depends on absolute size, not only the ratio (docs) |
| `log_prob_queue_model2()` (**default**) | `ln(1+back) / ln(1+back+front)` | |
| `l3_fifo_queue_model()` | exact FIFO from order IDs | needs L3 data. Binance public data is L2 |

- **Docs constraint** (with x = our relative position, 0 at the head and 1 at the tail): f(0) = 0 and f(1) = 1.
- [Inf] **For a cross-check, bracket the result:** `risk_adverse` as the lower bound, then `power_prob(2)` and `power_prob(3)`. Fill rates outside our engine's range point at the queue assumption.

---

## 5. Strategy loop API [Src: `binding.py`, `order.py`, `state.py`, `recorder.py`, `stats/`]

**`hbt` methods** (`ROIVectorMarketDepthBacktest_` and `HashMapMarketDepthBacktest_` are the same apart from `depth()`). None of them take default arguments, so pass every argument.

| Call | Returns / meaning |
|---|---|
| `elapse(ns)` | `0` ok, `1` end of data. Moves time forward by `ns` |
| `elapse_bt(ns)` | same, backtest-only |
| `wait_next_feed(include_order_resp: bool, timeout_ns)` | `0` timeout, `1` end of data, `2` feed received, `3` order response (if included) |
| `wait_order_response(asset_no, order_id, timeout_ns)` | `0` response or timeout, `1` end of data |
| `submit_buy_order(asset_no, order_id, price, qty, time_in_force, order_type, wait)` | `0` ok, `1` end of data (if `wait`). `submit_sell_order` is the same. There is **no** generic `submit_order` |
| `modify(asset_no, order_id, price, qty, wait)` / `cancel(asset_no, order_id, wait)` | `0` ok |
| `clear_inactive_orders(asset_no)` | drops FILLED, CANCELED and EXPIRED orders from `orders()` |
| `depth(asset_no)` | `.best_bid`, `.best_ask`, `.best_bid_tick`, `.best_ask_tick`, `.best_bid_qty`, `.best_ask_qty` (ROI bug, §3), `.tick_size`, `.lot_size`, `.bid_qty_at_tick(t)`, `.ask_qty_at_tick(t)` |
| `position(asset_no)` | float |
| `state_values(asset_no)` | `.position`, `.balance`, `.fee`, `.num_trades`, `.trading_volume`, `.trading_value` |
| `orders(asset_no)` | `OrderDict`: `.get(id)`, `id in orders`, `len()`, `.values()` |
| `orders(asset_no).values()` | iterator. Loop with `while it.has_next(): o = it.get()` (`has_next` advances). `__next__` is disabled because numba cannot use it |
| `last_trades(asset_no)` / `clear_last_trades(asset_no)` | event array of trades since the last clear. Needs `last_trades_capacity > 0` |
| `current_timestamp`, `num_assets` | properties |
| `feed_latency(asset_no)` → `(exch_ts, local_ts)` / `order_latency(asset_no)` → `(req, exch, resp)` | latest observed values, or `None` |
| `close()` | `0` ok. **Required.** The object cannot be reused afterwards [Docs] |

**`Order` objects** (from `order.py`):

- Properties: `order_id`, `price`, `price_tick`, `qty`, `leaves_qty`, `exec_qty`, `exec_price`, `exec_price_tick`, `side`, `status`, `req`, `time_in_force`, `order_type`, `exch_timestamp`, `local_timestamp`, `cancellable` (status NEW or PARTIALLY_FILLED and no request pending).
- **`Order.tick_size` appears to return `price_tick`** (a bug, read in source via fetch). Do not use it.

**Constants:**

| Group | Values |
|---|---|
| Side | `BUY=1`, `SELL=-1` |
| Status | `NONE=0`, `NEW=1`, `EXPIRED=2`, `FILLED=3`, `CANCELED=4`, `PARTIALLY_FILLED=5`, `REJECTED=6` |
| Time in force | `GTC=0`, `GTX=1` (post-only), `FOK=2`, `IOC=3` |
| Order type | `LIMIT=0`, `MARKET=1` |

Only `NONE`, `NEW`, `EXPIRED`, `FILLED`, `CANCELED`, `GTC`, `GTX`, `LIMIT`, `MARKET`, `BUY` and `SELL` are re-exported from the top-level package. Import the rest from `hftbacktest.order`.

**`Recorder` and stats:**

- **`Recorder(num_assets, record_size)`.**
  - Pass `recorder.recorder` (a jitclass) into the `@njit` function and call `.record(hbt)` there. It raises `IndexError` when the buffer is full.
  - Afterwards call `recorder.get(asset_no)` or `recorder.to_npz(path)`.
- **`hftbacktest.stats`.**
  - `LinearAssetRecord(data)` (or `InverseAssetRecord`) `.contract_size(x).resample('10s').stats(metrics=None, book_size=...)` returns a `Stats`.
  - `Stats` has `.summary()` (polars DataFrame) and `.plot(price_as_ret=False, backend='matplotlib')`.
  - Metrics include `SR`, `Sortino`, `Ret`, `MaxDrawdown`, `DailyNumberOfTrades`, `DailyTradingValue`, `ReturnOverMDD`, `ReturnOverTrade`, `MaxPositionValue`, and others.

**Minimal skeleton.** This shows the API shape only and is not our strategy. File names are placeholders. Defaults are from the [Getting Started](https://hftbacktest.readthedocs.io/en/latest/tutorials/Getting%20Started.html) and GLFT tutorials, adjusted to 2.4.4 names.

```python
import numpy as np
from numba import njit
from hftbacktest import (BacktestAsset, ROIVectorMarketDepthBacktest, Recorder,
                         GTX, LIMIT, BUY, SELL)

asset = (BacktestAsset()
         .data(['ethusdt_20261024.npz'])              # event_dtype arrays, key 'data'
         .initial_snapshot('ethusdt_20261023_eod.npz')
         .linear_asset(1.0)
         .constant_order_latency(160_000_000, 160_000_000)   # ns; placeholder
         .risk_adverse_queue_model()                  # set explicitly (default is LogProb2)
         .no_partial_fill_exchange()
         .trading_value_fee_model(0.0002, 0.0005)     # maker, taker fractions; check vs our YAML
         .tick_size(0.01).lot_size(0.001)
         .roi_lb(0.0).roi_ub(10_000.0)
         .last_trades_capacity(10_000))
hbt = ROIVectorMarketDepthBacktest([asset])
rec = Recorder(1, 2_000_000)

@njit
def run(hbt, rec):
    oid = 0
    while hbt.elapse(100_000_000) == 0:               # 100 ms; 0 = OK, 1 = end of data
        hbt.clear_inactive_orders(0)
        d = hbt.depth(0)
        it = hbt.orders(0).values()
        while it.has_next():                          # cancel everything still working
            o = it.get()
            if o.cancellable:
                hbt.cancel(0, o.order_id, False)
        mid = (d.best_bid + d.best_ask) / 2.0
        oid += 1
        hbt.submit_buy_order(0, oid, d.best_bid, 0.01, GTX, LIMIT, False)
        oid += 1
        hbt.submit_sell_order(0, oid, d.best_ask, 0.01, GTX, LIMIT, False)
        hbt.clear_last_trades(0)
        rec.record(hbt)
    return True

run(hbt, rec.recorder)
hbt.close()
rec.to_npz('run.npz')
```

---

## 6. The GLFT tutorial vs our spec §4

Sources:

- [Docs] [GLFT Market Making Model and Grid Trading](https://hftbacktest.readthedocs.io/en/latest/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html)
- [Docs] [Market Making with Alpha – Order Book Imbalance](https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html)
- Ours: `specs/glft-perp.md` §4 and §6, and `glft.py` `closed_form` / `compute_quotes`

**Tutorial code (quoted):**

```
c1 = 1/(ξΔ) · ln(1 + ξΔ/k)
c2 = sqrt( γ/(2AΔk) · (1 + ξΔ/k)^(k/(ξΔ)+1) )          called with ξ = γ
half_spread_tick = (c1 + Δ/2 · c2 · σ) · adj1           adj1 = 1
skew             = c2 · σ · adj2                         adj2 = 0.05
r_tick   = mid_tick − skew · position
bid_tick = min(round(r_tick − half_spread_tick), best_bid_tick)
ask_tick = max(round(r_tick + half_spread_tick), best_ask_tick)
```

**Comparison:**

| Aspect | hftbacktest tutorial | Ours (spec v1.2 / `glft.py`) |
|---|---|---|
| c1, c2 algebra | Guéant–Lehalle–Fernandez-Tapia (GLFT), ξ = γ | **Identical** (§4.1) |
| Units | **ticks**. σ in ticks/√s, A in 1/s, k per tick, Δ = 1 (one order unit) | USDT. σ in USDT/√s, k per USDT, Δ = 0.01 ETH, q in ETH |
| Fee | Not in the formula. Only the simulator charges it (`trading_value_fee_model(-0.00005, 0.0007)`, a maker **rebate**) | **In the formula**: `h = c + c1 + (Δ/2)ω` with `c = f_m·m`, and `A_eff = A·e^(−kc)` in c2 ("exact" mode) |
| Skew | `c2·σ·0.05`. A hand-set factor shrinks the GLFT skew **20×** | `ω = σ·c2` unscaled. γ is solved so that `Q·ω = 25 ticks` (§6.4) |
| γ | hand-set 0.05 | solved from the target skew, then frozen |
| Rounding and clamping | round to nearest. Bid ≤ best bid and ask ≥ best ask, so it joins the touch at most | Bid rounds **down**, ask rounds **up**. Floor `δ_min = 10` ticks. No-cross guard against the **opposite** touch (`bid ≤ best_ask − θ`) |
| A, k | Every 100 ms, record the **max** trade distance from mid ("arrival depth"). λ(δ) = count of intervals reaching ≥ δ ÷ 600 s. **OLS** of ln λ on δ over the first 70 bins (the bins are actually half-ticks, a bug the tutorial itself notes). **Rolling** 10 min window, refit every 5 s | Through-only hit test of size Δ with a **400 ms latency shift**, 500 ms grid, 5-tick bins 10–150, **WLS**, sanity gates, fitted **once** in-sample and frozen (§6.2) |
| σ | `nanstd` of 100 ms mid changes over 10 min × √10, in ticks/√s, rolling | RMS of 1 s mid changes over the whole in-sample day, no demeaning, frozen (§6.1) |
| Orders | **Grid**: 20 levels per side, spacing `max(round(half_spread_tick)·tick, tick)`. Order ID = price tick. Each 100 ms, cancel orders that are off-grid and add missing ones. `order_qty = 1`, `max_position = 20` | One order per side, Q = 5Δ, headroom sizing, requote thresholds and min interval, replace keeps total size (§4.5–4.6) |
| Requote | Every 100 ms step, cancel + new (no `modify`) | `ReplaceOrder` with a churn guard |
| Simulator setup | `intp_order_latency` (live-measured), `power_prob_queue_model(2.0)`, no partial fill, tick 0.01, lot 0.001, ETHUSDT Oct-2022 | ours: see `config/backtest/*.yaml` |

**The "with alpha" tutorial:**

- `fair = mid + c1·α`, where α is the z-scored order-book imbalance within ±2.5% of mid over a 1 h window.
- `r = fair − skew·(position/order_qty)`.
- `half_spread` and `skew` are **hand-set constants**, for example 80 and 3.5 ticks on BTC. There is no GLFT here.
- It uses `power_prob_queue_model(2)`, and `3` for the 2025 run.

[Inf] **What this means for the cross-check.** Do not run the tutorial's strategy as the comparison. Port **our** `compute_quotes` logic (with frozen σ, A, k, γ and our fee term) into an `@njit` loop. That way any disagreement comes from the simulator (queue, latency, fill model), not from a different strategy. One gap cannot be closed: our requote logic relies on modify keeping total size, and hftbacktest's `modify` semantics against `executedQty` were **not checked** here.

---

## 7. Gotchas

- **1.x → 2.x** [Docs migration2]:
  - `elapse` and `submit_*` now return **int codes** (0 = OK) instead of `True`. `while hbt.elapse(x):` silently exits at once.
  - Data moved from a columnar float array to the `event_dtype` structured array. The side column was replaced by flags, invalid `-1` timestamps by EXCH/LOCAL flags, and µs by ns.
  - `hftbacktest.data.utils.migration2` converts old files.
  - [Mem, unverified] 1.x used a single `HftBacktest(data, tick_size, lot_size, maker_fee, taker_fee, order_latency, asset_type, ...)` constructor with `hbt.best_bid`, `hbt.position` and calls without `asset_no`. **Any blog post or notebook in that style is outdated.** 2.x uses `BacktestAsset` + `*MarketDepthBacktest([assets])`, and every call takes `asset_no`.
- **Within 2.x:** `constant_latency` is deprecated in favour of `constant_order_latency` [Src]. The Getting Started docs still show the old name.
- **`hbt.close()`** must be called. The object is single-use.
- **Default queue model is `LogProbQueueModel2`**, which is optimistic compared with `RiskAdverse`. Always set it explicitly.
- **`last_trades_capacity` defaults to 0**, so `last_trades()` is always empty unless you set it.
- **ROI bounds:** prices outside `[roi_lb, roi_ub]` are not tracked in the ROI depth. Set a wide range (ETH: 0–10,000 at tick 0.01 is about 1M levels, roughly 16 MB) [Inf], or use HashMap.
- **ROI `best_bid_qty` and `best_ask_qty` return prices** (bug, §3).
- **numba restrictions:**
  - Strategy functions must be `@njit`. No Python dicts or lists of objects, no f-strings, no `try`/`except` on most paths. The GLFT tutorial uses `numba.typed.Dict` for the grid.
  - Iterate orders with `has_next()`/`get()`, not `for`.
  - Pass `recorder.recorder`, not `recorder`.
  - The first call compiles, so expect seconds of JIT time.
- **Windows:**
  - Wheels exist for cp311–cp314 `win_amd64`. Use **Python 3.11–3.13** (numba 0.61 has no 3.14).
  - Our `.venv` is Python 3.13.14 (fine), but `requirements.txt` pins **`numpy==2.5.4`**, which conflicts with `numpy<2.3`. **Use a separate venv** (for example `.venv-hbt`).
  - `polars` is unpinned upstream, and our pin is `polars==2.0.0`. Whether `hftbacktest.stats` works with polars 2.x is **unverified**.
- **Binance data:**
  - The converter expects the author's collector format (local ns prefix, `trade` not `aggTrade`, REST snapshot lines) and uses `T` as `exch_ts`, not `E`.
  - **For our recorder's files, we need our own converter** to `event_dtype`. It must then call `correct_local_timestamp`, `correct_event_order` and `validate_event_order`, in that order.
- **`correct_local_timestamp` only shifts when some latency is negative**, and the shift is uniform. It hides clock skew rather than fixing it.

---

## 8. Symbols to verify against an installed 2.4.4

```
hftbacktest.__version__ (if present)            hftbacktest.BacktestAsset
hftbacktest.HashMapMarketDepthBacktest          hftbacktest.ROIVectorMarketDepthBacktest
hftbacktest.Recorder                            hftbacktest.ALL_ASSETS
hftbacktest.{DEPTH_EVENT, TRADE_EVENT, DEPTH_CLEAR_EVENT, DEPTH_SNAPSHOT_EVENT, DEPTH_BBO_EVENT,
             ADD_ORDER_EVENT, CANCEL_ORDER_EVENT, MODIFY_ORDER_EVENT, FILL_EVENT,
             EXCH_EVENT, LOCAL_EVENT, BUY_EVENT, SELL_EVENT}
hftbacktest.{BUY, SELL, NONE, NEW, EXPIRED, FILLED, CANCELED, GTC, GTX, LIMIT, MARKET}
hftbacktest.order.{PARTIALLY_FILLED, REJECTED, FOK, IOC, UNSUPPORTED}
hftbacktest.types.{event_dtype, state_values_dtype, order_dtype, record_dtype, UNTIL_END_OF_DATA}
hftbacktest.binding.event_dtype
BacktestAsset().{data, add_file, add_data, initial_snapshot, linear_asset, inverse_asset,
  constant_latency, constant_order_latency, intp_order_latency, latency_offset,
  risk_adverse_queue_model, log_prob_queue_model, log_prob_queue_model2,
  power_prob_queue_model, power_prob_queue_model2, power_prob_queue_model3, l3_fifo_queue_model,
  no_partial_fill_exchange, partial_fill_exchange,
  trading_value_fee_model, trading_qty_fee_model, flat_per_trade_fee_model,
  tick_size, lot_size, last_trades_capacity, roi_lb, roi_ub, parallel_load}
hbt.{elapse, elapse_bt, wait_next_feed, wait_order_response, submit_buy_order, submit_sell_order,
  modify, cancel, clear_inactive_orders, depth, position, state_values, orders, last_trades,
  clear_last_trades, feed_latency, order_latency, close, current_timestamp, num_assets}
depth.{best_bid, best_ask, best_bid_tick, best_ask_tick, best_bid_qty, best_ask_qty, tick_size,
  lot_size, bid_qty_at_tick, ask_qty_at_tick, bid_depth, ask_depth, roi_lb_tick, roi_ub_tick}  # last 4 ROI only
hftbacktest.data.{correct_local_timestamp, correct_event_order, validate_event_order}  # check export path
hftbacktest.data.utils.binancefutures.convert
hftbacktest.data.utils.snapshot.create_last_snapshot
hftbacktest.data.utils.migration2
hftbacktest.stats.{LinearAssetRecord, InverseAssetRecord, Stats, SR, Sortino, Ret, MaxDrawdown,
  ReturnOverMDD, ReturnOverTrade, DailyNumberOfTrades, DailyTradingValue, MaxPositionValue}
```

Specific checks to run:

1. `ROIVectorMarketDepth.best_bid_qty == best_bid`. If true, the bug is confirmed.
2. The default queue model, using a toy dataset.
3. `pip install` on Python 3.13 in a clean venv resolves numpy < 2.3.

## References

- nkaz001, *hftbacktest* (MIT). Repo: https://github.com/nkaz001/hftbacktest. Tag `py-v2.4.4`, source tree https://github.com/nkaz001/hftbacktest/tree/py-v2.4.4/py-hftbacktest, Rust core at https://github.com/nkaz001/hftbacktest/tree/rust-v0.9.4/hftbacktest
- PyPI: https://pypi.org/project/hftbacktest/ (2.4.4) and numba 0.61.2 metadata https://pypi.org/pypi/numba/0.61.2/json
- Docs (latest): https://hftbacktest.readthedocs.io/en/latest/ (pages: `data.html`, `latency_models.html`, `order_fill.html`, `migration2.html`, and the tutorials linked inline)
- Guéant, O. (2017). *Optimal market making.* Applied Mathematical Finance. arXiv:1605.01862. This is the GLFT closed form used by both implementations (see our spec §4.1 and its references).

## 9. Verified against the installed package (2026-10-30)

Checked in `.venv-hft` (Windows 11, Python 3.13.14, `hftbacktest==2.4.4`, `numba==0.68.1`, `numpy==2.2.6`; pins in `requirements-hft.txt`).

- Install worked from Windows wheels (`--only-binary=:all:`). `pip check`: no broken requirements.
- `numba~=0.61` is a "compatible release" pin. It means `>=0.61, <1.0`, not `0.61.*`, so numba 0.68.1 is allowed.
- `hftbacktest.__version__` == `2.4.4`.
- **Every symbol in §8 exists**: module constants, `order.*`, `types.*`, all `BacktestAsset` methods, all `hbt` methods, `hftbacktest.data.{correct_local_timestamp, correct_event_order, validate_event_order}`, `data.utils.snapshot.create_last_snapshot` and all `stats.*` classes.
- **Bug confirmed**: `binding.py` `ROIVectorMarketDepth.best_bid_qty` / `best_ask_qty` call `roivecdepth_best_bid` / `_best_ask`, so they return the PRICE. Workaround: `depth.bid_qty_at_tick(depth.best_bid_tick)` and `depth.ask_qty_at_tick(depth.best_ask_tick)`. The `HashMapMarketDepth` versions are correct.
