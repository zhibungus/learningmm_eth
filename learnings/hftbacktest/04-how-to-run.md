# 04. How to run it

*Written 2026-10-30, updated the same day (no-partial-fill default, renamed variants). Track: [hftbacktest/](README.md). Commands from `hft/README.md`, `hft/convert.py`, `hft/check_hbt.py`, `hft/run_backtest.py`. Run everything from the project root (the folder with `CLAUDE.md`).*

## Key ideas
- Own venv: **`.venv-hft`** on Python 3.11–3.13, installed from **`requirements-hft.txt`** with wheels only.
- Pipeline: **convert** (parquet → hourly `.npz`) → **check** (book sane?) → **run** (example or GLFT) → read `hft/results/<run>/`.
- Variants are CLI flags. The default exchange is **NoPartialFill**, because `--partial-fill` crashes in 2.4.4.
- Expect a few seconds of **numba compile** on the first call. Set **`PYTHONIOENCODING=utf-8`** on Windows.
- A full day of GLFT runs in **~24 s**.

---

## 1. Pipeline at a glance

```
 data/processed/binance_um_live/  ─┐
 data/raw/.../status.jsonl        ─┤  hft/convert.py   ┌─> data/hft/ETHUSDT/<date>/ETHUSDT_<date>_<HH>.npz
                                   └─────────────────> ├─> ..._<HH>_snapshot.npz   (book at window start)
                                                       ├─> gaps.json, manifest.json
                                                       │
                       hft/check_hbt.py  <─────────────┤  (crossed/NaN book? vs recorded bookTicker)
                                                       │
     hft/run_backtest.py --strategy glft|example  <────┘
                                   └──> hft/results/<date>_<strategy>_<tag>/
                                          config.json  metrics.json  fills.csv  summary.md  equity.png
```

---

## 2. One-time setup

```powershell
python -m venv .venv-hft                      # Python 3.13 here; 3.14 fails (no numba wheels)
.venv-hft\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-hft.txt
.venv-hft\Scripts\python.exe -c "import hftbacktest; print(hftbacktest.__version__)"   # 2.4.4
```
- The pins that matter: `hftbacktest==2.4.4`, `numba==0.68.1`, `numpy==2.2.6`, `polars==2.1.0`, `pyarrow`, `PyYAML`.
- `--only-binary=:all:` = use prebuilt wheels and never compile from source, so no Rust toolchain is needed.
- Never install these into `.venv` (numpy clash, [README (a)](README.md#decisions-log)).

**Remember: two envs: `.venv` for `src/mm/`, `.venv-hft` for `hft/`.**

---

## 3. Convert

```powershell
$env:PYTHONIOENCODING = "utf-8"              # PowerShell   (cmd.exe: set PYTHONIOENCODING=utf-8)
.venv-hft\Scripts\python.exe hft\convert.py --date 2026-10-24 --start 10:00 --end 11:00   # one hour, ~2-3 min
.venv-hft\Scripts\python.exe hft\convert.py --date 2026-10-24                             # whole day: 262 s
```

| Flag | Default | Meaning |
|---|---|---|
| `--date` | required | UTC date |
| `--start` / `--end` | `00:00` / `24:00` | whole hours; end exclusive |
| `--offset-mode` | `robust` | or `measured` (raw REST readings; for comparison only) |
| `--symbol`, `--tick`, `--lot` | ETHUSDT, 0.01, 0.001 | |
| `--out` | `data/hft` | output root |

- Most of a one-hour run is spent on two things: the setup scan (the robust offset reads every part file) and the initial-snapshot replay.
- Check `manifest.json` afterwards: latency percentiles, clamp counts, jumps, gaps.

---

## 4. Check

```powershell
.venv-hft\Scripts\python.exe hft\check_hbt.py --date 2026-10-24 --start 10:00 --end 11:00
```
- It steps the book in 100 ms increments and counts crossed and NaN books. Every 1 s it compares best bid/ask with our recorded bookTicker.
- Small differences are expected: depth is batched every 100 ms, while bookTicker is real time.
- Optional: `.venv-hft\Scripts\python.exe hft\check_glft_math.py` checks the njit maths against `glft.py` to 1e-9. `.venv-hft\Scripts\python.exe -m hft.selftest_synthetic` needs no data.

---

## 5. Run

```powershell
# hftbacktest-style example market maker, hour 10
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy example --start 2026-10-24T10 --end 2026-10-24T10 --tag h10
# GLFT port, one hour
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy glft --start 2026-10-24T10 --end 2026-10-24T10 --tag h10_base
# the full-day comparison variants
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy glft --start 2026-10-24T00 --end 2026-10-24T23 --tag full_base
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy glft --start 2026-10-24T00 --end 2026-10-24T23 --tag full_pess_latency --entry-ms 300
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy glft --start 2026-10-24T00 --end 2026-10-24T23 --tag full_queue_prob3 `
    --queue-model power_prob --queue-n 3
# no data at all
.venv-hft\Scripts\python.exe -m hft.run_backtest --strategy glft --synthetic --seconds 120 --tag synth
```
(The backtick `` ` `` continues a line in PowerShell.)

| Flag | Default | Meaning |
|---|---|---|
| `--strategy` | `glft` | or `example` |
| `--start` / `--end` | — | hours `YYYY-MM-DDTHH`, **end inclusive** (unlike convert) |
| `--tag` | `run` | suffix of the result folder |
| `--config` | `config/backtest/glft_live.yaml` | GLFT params, tick/lot/fee (read only) |
| `--queue-model` | `risk_adverse` | `power_prob`, `power_prob2`, `power_prob3`, `log_prob`, `log_prob2` |
| `--queue-n` | 2.0 | exponent for `power_prob*` |
| `--partial-fill` | off (= NoPartialFill) | PartialFillExchange: **crashes in 2.4.4** (error 14), run marked INCOMPLETE |
| `--requote-mode` | `modify` | or `cancel_new` (cancel, then a new order) |
| `--latency-kind` | `constant` | or `intp_jitter` |
| `--entry-ms` / `--resp-ms` | 160 / 240 | order latency |
| `--seed` | 42 | only drives the `intp_jitter` table |
| `--step-ms` | 100 | polling step; quote cycle = `timer_interval_ms` (1,000) |
| `--allow-missing` | off | skip missing hours (the clock still runs over them!) |
| `--synthetic`, `--seconds` | off, 120 | generated data |

### The variants

| Variant | Queue | Exchange | Entry / resp | Direction |
|---|---|---|---|---|
| `base` | risk_adverse | NoPartialFill | 160 / 240 ms | reference |
| `pess_latency` | risk_adverse | NoPartialFill | **300** / 240 ms | pessimistic on latency only |
| `queue_prob3` | **power_prob(3)** | NoPartialFill | 160 / 240 ms | **more optimistic** on queue (advances on cancels) |

- One knob per variant. An earlier "pess" variant mixed a worse latency with a more optimistic queue, and so pointed in two directions at once ([05 §5](05-first-results.md)).

### Where results go
`hft/results/<first date>_<strategy>_<tag>/`:

| File | Content |
|---|---|
| `config.json` | everything needed to reproduce: params + YAML path, exchange model, data files, seed, CLI |
| `metrics.json` | comparison metrics (`hft/postprocess.py`), including `run_complete`, `elapse_end_code`, `max_live_orders_*` |
| `fills.csv` | GLFT only: one row per fill with `mid_before` and 1/10/60 s markouts |
| `summary.md` | the numbers + the assumptions that limit realism (with a banner if INCOMPLETE) |
| `equity.png` | equity, inventory, mid |

**Remember: check `run_complete` and `max_live_orders_*` before reading any PnL.**

---

## 6. Common errors

| Symptom | Cause | Fix |
|---|---|---|
| First run pauses for seconds | numba compiling the `@njit` functions | wait; this happens once per process |
| `UnicodeEncodeError` when printing | Windows console code page | `$env:PYTHONIOENCODING="utf-8"` |
| Run stops part-way, `elapse` returns 14, folder `*_INCOMPLETE` | `--partial-fill` in 2.4.4 | drop `--partial-fill` |
| More than 1 live order per side, inventory above Q | treating a pending order (`NONE`, `req=NEW`) as finished | count it as live (fixed in `glft_hft.py`) |
| `IndexError` inside the loop | `Recorder` buffer full | size it ≥ steps ÷ record interval (the runners already add slack) |
| `FileNotFoundError: N hourly files missing` | hour not converted | convert it, or use `--allow-missing` (dangerous) |
| `ValueError: ... events are out of order` / `go backwards across files` | event-order rule broken (converter bug or hand-edited files) | reconvert the range in one run; report it if it repeats |
| `feed latency exceeds the lookback margin` | a message arrived > 55 s late | raise `LOOKBACK_S` in `convert.py` |
| numba: `No definition for lowering static_getitem(Optional...)` | indexing an Optional, e.g. `hbt.feed_latency(0)[0]` | test `is None` first, then unpack |
| Loop exits immediately | 1.x style `while hbt.elapse(x):` | `while hbt.elapse(x) == 0:` |
| Book empty or odd prices | ROI too narrow | widen `roi_frac`; check the snapshot mid |
| `pip` can't resolve numba | Python 3.14 | use 3.11–3.13 |
| Weird `best_bid_qty` | ROI bug (returns the price) | `bid_qty_at_tick(best_bid_tick)` |

**Remember: when something looks odd, read `manifest.json`, `config.json` and `metrics.json` before the code.**

---

## Decisions log
- **2026-10-30.** Setup follows decision (a), a separate venv. The variants reflect (e).
- **2026-10-30.** NoPartialFill is the default and `--no-partial-fill` was replaced by `--partial-fill` (decision (g)). Variants renamed to `pess_latency` / `queue_prob3`. See [README.md Decisions log](README.md#decisions-log).

---

## Check your understanding
1. `convert.py --start 10:00 --end 11:00` and `run_backtest --start 2026-10-24T10 --end 2026-10-24T10` cover the same hour. Why do the `--end` values differ?
2. You run the GLFT twice in one Python process. Which run pays the compile time?
3. Why is `queue_prob3` not a pessimistic variant, and which variant would you call pessimistic?

*Answers on request.*
