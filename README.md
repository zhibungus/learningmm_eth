# An introduction to market making on Binance 

A market making strategy for the Binance ETHUSDT perpetual future to learn the process from start to finish: research, spec, data, code, backtest and independent review, done with Claude.

The strategy is a **fee-aware GLFT market maker** (Guéant–Lehalle–Fernandez-Tapia). It posts a bid and an ask around the mid-price and shifts both quotes against its inventory. The quote distances come from a closed-form formula calibrated on market data.

> **Main lesson: Negative results** On every test day, the strategy lost money after fees. The main cause is adverse selection (toxic flow). ([hftbacktest](https://github.com/nkaz001/hftbacktest)) confirms this.
>
> This is a learning project, and real money was never used.

## Results at a glance

| Test | Data | Net PnL (USDT), in-sample / out-of-sample | Verdict |
|---|---|---|---|
| [v1.2, archive](reports/2026-10-23_glft_v1_2/summary.md) | Binance public archive, best bid/ask + trades, 2024-03-29 / 30 | −3.11 / −5.09 | [Implementation PASS, strategy FAIL](reviews/2026-10-23_glft_v1_2.md) |
| [Live L2](reports/2026-10-30_glft_live/summary.md) | Full order book I recorded myself, 2026-10-24 / 25 | −2.93 / −1.63 | [Implementation PASS, strategy FAIL](reviews/2026-10-30_glft_live.md) |
| [hftbacktest cross-check](hft/results/COMPARISON_INPUT.md) | Same live days, open-source simulator | −9.68 / −4.86 | Price moves against our fills in both simulators |

Where the money goes on the live days (in-sample):

| Spread capture | Inventory PnL | Fees | Net |
|---|---|---|---|
| +0.82 | −2.52 | −1.23 | −2.93 |

- **Spread capture** is the edge from buying below the mid and selling above it. It is small and positive.
- **Inventory PnL** is the loss from holding the position while the price moves. It is negative, and the price moves against us within about 10 seconds of a fill.
- **Fees** come on top of that loss.

The volume involved is tiny: about 300 fills per day, at 0.01 ETH each.

Other findings:
- **The fitted fill model is too optimistic.** It predicts about 6–7 times more fills than the simulated exchange gives (spec hypothesis H2: 0.16 / 0.14, where 1.0 would mean it is right).
- **Inventory skew works.** Turning it off makes the strategy hold close to its position limit for about 40–50% of the time.
- **Being faster (colocation) alone doesn't turn the result clearly positive.**

The full list of hypotheses is in [specs/glft-perp.md §9](specs/glft-perp.md).

## How it was built

```
research/ ──► specs/ ──► data/ ──► src/mm/strategies/ ──► src/mm/backtest/ ──► reports/ ──► reviews/
 (papers)    (math,     (download,  (strategy + tests)     (event-driven         (one folder   (look for bias,
              params,    record,                            engine, fills,        per run)      overfitting,
              limits)    clean)                             latency, fees)                      bad assumptions)
```

Each stage reads the previous stage's files and doesn't redo another stage's work. The project rules are in [CLAUDE.md](CLAUDE.md). A strategy isn't done until it passes review.

## Where to start reading

| If you want… | Read |
|---|---|
| The strategy's math and rules | [specs/glft-perp.md](specs/glft-perp.md) |
| The strategy code | [src/mm/strategies/glft.py](src/mm/strategies/glft.py) and [glft_calibrate.py](src/mm/strategies/glft_calibrate.py) |
| How the backtester works | [src/mm/backtest/README.md](src/mm/backtest/README.md) |
| The final result and its limits | [reports/2026-10-30_glft_live/summary.md](reports/2026-10-30_glft_live/summary.md) and [its review](reviews/2026-10-30_glft_live.md) |
| The second-simulator check | [hft/README.md](hft/README.md) and [learnings/hftbacktest/](learnings/hftbacktest/) |

The code is commented for a learner: each block says what it does and why, including Python syntax and the market meaning.

## Repository map

```
research/         Literature notes: Binance market making survey, spot vs perp facts, hftbacktest
specs/            glft-perp.md, the strategy specification (formulas, parameters, risk limits, hypotheses)
config/           YAML configs: data, backtests, paper trading, calibrated parameters
data/             Only READMEs are in git. Market data is downloaded or recorded locally (see below)
src/mm/
  data/           Archive downloader, live L2 recorder, order-book sync, loaders, quality checks, latency
  strategies/     GLFT market maker + calibration (σ, A, k, γ)
  backtest/       Event-driven engine: simulated exchange, fill models (quote/trade and L2 queue), metrics, reports
  paper/          Paper trading on the live feed (simulated fills, no orders sent)
tests/            pytest suite (216 tests)
reports/          One folder per backtest or calibration run: summary.md, metrics, charts, exact config
reviews/          Validator reports, one per run
hft/              Port of the strategy to hftbacktest, data converter and comparison results
```

## Running it

You need Python 3.11+. The commands are written for Windows PowerShell; on macOS/Linux use `.venv/bin/python`.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pytest -q
```

**Get data.** None is in the repo, because it is large and comes from Binance.

```powershell
# Public archive days used by the v1.2 run (best bid/ask + trades + funding)
.venv\Scripts\python.exe -m mm.data.download --datasets bookTicker aggTrades fundingRate --start 2024-03-29 --end 2024-03-30
# Or record your own full order book from the public websocket (Ctrl+C to stop)
.venv\Scripts\python.exe -m mm.data.record
```

**Calibrate and backtest:**

```powershell
.venv\Scripts\python.exe -m mm.strategies.glft_calibrate --config config\backtest\glft_smoke.yaml
.venv\Scripts\python.exe -m mm.backtest.run --config config\backtest\glft_smoke.yaml --workers 4
```

Each run writes `reports/<date>_<strategy>_<tag>/`. Every run uses a fixed random seed, and `config.json` records the code hash, the input-file hashes and the seed.

The live-L2 run (`config/backtest/glft_live.yaml`) needs the 2026-10-23 to 25 recordings. Those aren't public, so you'd have to record your own days and update the dates. [data/README.md](data/README.md) has more data commands. The hftbacktest track uses its own environment ([requirements-hft.txt](requirements-hft.txt), [hft/README.md](hft/README.md)).

## Main limitations

- **Little data:** two archive days and two recorded live days (a Saturday and a Sunday, which are usually quieter). That is enough to show the sign and the mechanism, but not to rank parameter variants.
- **Estimated queue position:** the simulator estimates our place in the queue from order-book changes. It has no market impact and doesn't model margin or liquidation.
- **Assumed latency:** latency comes from a table measured on one home PC, and the recording PC's clock drifted.

Each report ends with a section called "Assumptions that most limit realism". The reviews list every known issue.

## License

[MIT](LICENSE). The market data belongs to Binance and is not included.
