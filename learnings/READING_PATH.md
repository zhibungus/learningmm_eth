# Reading path: the whole project, in order

## Key ideas
- One path through everything we built: lessons, then the spec section, the code and the report that show the same idea.
- Each step has: what to read, why, a rough time, and 1–2 self-check questions (answers on request).
- Tick a box when you finish a step. Tell the tutor chat and it ticks it for you.
- The path follows the pipeline order, so each stage builds on the one before.
- **The ending you are heading for:** the code works (implementation PASS), but the strategy loses money, mostly to adverse selection (strategy FAIL). Every stage shows a piece of why.

```
 research/         specs/            data/ + src/mm/data/    src/mm/strategies/     src/mm/backtest/        reviews/
 papers, facts ──► glft-perp.md ──►  download, record,   ──► glft.py           ──►  engine, fills,     ──►  validator
                   (math, params,    replay                  glft_calibrate.py      metrics, report         verdicts
                    risk limits)                                                     │
                                                                                     ▼
                                                              reports/<run>/  ──►  hft/ (independent cross-check)
 learnings/  explains every box, at any stage
```

How to read code here: open the file, jump to the named function, and read its comments first. You don't need every line.

---

## Stage 0. Market setup and choosing the strategy (done)
- [x] **0.1** [market-setup/venues-and-instruments.md](market-setup/venues-and-instruments.md) (+ skim [research/binance-eth-spot-vs-perp-facts.md](../research/binance-eth-spot-vs-perp-facts.md))
- [x] **0.2** [market-setup/market-data.md](market-setup/market-data.md)
- [x] **0.3** [market-setup/paper-trading-and-simulation.md](market-setup/paper-trading-and-simulation.md)
- [x] **0.4** [strategy-glft/00-choosing-the-strategy.md](strategy-glft/00-choosing-the-strategy.md)

## Stage 1. How a market maker earns and loses (you are here)
- [ ] **1.1** Lesson [strategy-glft/01-how-a-market-maker-earns-and-loses.md](strategy-glft/01-how-a-market-maker-earns-and-loses.md) · 40 min
  - Why: the 4-bucket PnL identity is the lens for every report later.
  - Check: Which bucket does adverse selection show up in? What does a negative 10 s markout mean?
- [ ] **1.2** Spec [§1 Objective](../specs/glft-perp.md) and [§3 Units](../specs/glft-perp.md) · 10 min
  - Why: one unit convention (USDT, ETH, ns UTC) is used in every file.
  - Check: What unit is a timestamp in, and why not seconds?
- [ ] **1.3** Code [src/mm/backtest/metrics.py](../src/mm/backtest/metrics.py): class `Account` (`on_fill`, `on_mid`, `equity`, `identity_gap`) · 20 min
  - Why: this is the PnL identity written as code.
  - Check: What should `identity_gap()` return if the accounting is right?
- [ ] **1.4** Report [reports/2026-10-23_glft_v1_2/summary.md](../reports/2026-10-23_glft_v1_2/summary.md), the Results table only · 10 min
  - Why: see the four buckets on a real run (in-sample: spread +2.13, fees −5.25, net −3.11 USDT).
  - Check: On this run, which bucket is the biggest loss? Would a zero fee make it profitable in-sample?

## Stage 2. Inventory risk and the reservation price
- [ ] **2.1** Lesson [strategy-glft/02-inventory-risk-and-reservation-price.md](strategy-glft/02-inventory-risk-and-reservation-price.md) · 45 min
  - Why: explains the skew, the core of GLFT.
  - Check: If q goes from 0 to +5 lots, which way do both quotes move, and why?
- [ ] **2.2** Research [research/binance-eth-market-making-survey.md](../research/binance-eth-market-making-survey.md) §1.1–1.2 (AS and GLFT) · 20 min
  - Why: where the model comes from, in the researcher's words.
  - Check: Why does GLFT fit a 24/7 perp better than AS?
- [ ] **2.3** Spec §4.1 GLFT closed form and §4.5 order size and inventory limit · 15 min
  - Check: What happens to the bid when q = +Q?
- [ ] **2.4** Code [src/mm/strategies/glft.py](../src/mm/strategies/glft.py): `GlftParams`, `xi_of`, `c2_skew`, `closed_form` · 25 min
  - Why: ω and the skew as code. Note the `@dataclass` explanation at the top.
  - Check: Which function returns the part of the quote that depends on inventory?
- [ ] **2.5** Tests [tests/test_glft.py](../tests/test_glft.py): `test_long_inventory_lowers_both_quotes_short_raises_both`, `test_at_max_long_only_the_ask_is_quoted_and_ask_is_inside_fee` · 10 min
  - Check: Why is it OK for the ask at max long to sit inside the fee?

## Stage 3. Fill probability `λ(δ) = A·e^(−kδ)`
- [ ] **3.1** Lesson [strategy-glft/03-fill-probability.md](strategy-glft/03-fill-probability.md) · 40 min
  - Check: If k doubles, what happens to fills 10 ticks out?
- [ ] **3.2** Spec §6.2 A and k · 15 min
  - Check: Why measure distance from the mid seen 400 ms earlier?
- [ ] **3.3** Code [src/mm/strategies/glft_calibrate.py](../src/mm/strategies/glft_calibrate.py): `lambda_curve`, `wls_fit`, `fit_curve` · 30 min
  - Why: how the curve is measured from trades and fitted.
  - Check: Why fit on log λ with weights, not plain least squares on λ?
- [ ] **3.4** Data [reports/2026-10-22_glft_perp_calibration/calibration.json](../reports/2026-10-22_glft_perp_calibration/calibration.json) · 10 min
  - Check: Find A and k. Do they match the params at the top of the v1_2 summary?

## Stage 4. The optimal spread, ticks and requoting
- [ ] **4.1** Lesson [strategy-glft/04-optimal-spread-glft-closed-form.md](strategy-glft/04-optimal-spread-glft-closed-form.md) · 60 min
  - Why: the full quote recipe, and the spec §10 example by hand.
  - Check: Why floor the bid and ceil the ask? What does the 10-tick floor protect against?
- [ ] **4.2** Spec §4.4 Final prices, §4.6 Requote rule, §10 Worked example · 25 min
  - Check: Why not requote on every tiny mid move, given ~400 ms latency?
- [ ] **4.3** Code `glft.py`: `c1_static`, `floor_to_tick`, `ceil_to_tick`, `compute_quotes`; then `solve_gamma` in `glft_calibrate.py` · 30 min
  - Check: In `compute_quotes`, where is the no-cross guard?
- [ ] **4.4** Tests `test_glft.py`: `test_worked_example_*`, `test_no_cross_guard_*`, `test_requote_needs_five_ticks_and_one_second_unless_urgent` · 15 min
  - Why: the §10 hand numbers become automatic checks.
  - Check: What would break if someone changed the rounding direction?

## Stage 5. Fees and break-even
- [ ] **5.1** Lesson [strategy-glft/05-fees-and-break-even.md](strategy-glft/05-fees-and-break-even.md) · 45 min
  - Check: What is the difference between "exact" and "shift" fee modes?
- [ ] **5.2** Spec §4.2, §4.3 (funding excluded) and Appendix A · 20 min
  - Check: Why is folding the fee in an exact change of variable?
- [ ] **5.3** Code `glft.py`: `fee_per_eth`, `a_use`; tests `test_shift_mode_vector`, `test_higher_maker_fee_widens_half_spread_zero_fee_has_c_zero` · 15 min
  - Check: At zero fee, what is c?

## Stage 6. Calibration end to end (no lesson yet; planned lesson 6)
- [ ] **6.1** Spec §6 intro, §6.1 σ, §6.3 Δ and Q, §6.4 γ · 20 min
  - Why: which numbers are fitted, which are chosen, and why they are frozen before out-of-sample.
  - Check: Why must calibration use in-sample data only?
- [ ] **6.2** Code `glft_calibrate.py`: `estimate_sigma`, `calibrate`, `write_outputs`, `main` · 30 min
  - Check: Where does the output YAML go, and which backtest config reads it?
- [ ] **6.3** Config [config/strategies/glft_calibrated.yaml](../config/strategies/glft_calibrated.yaml) vs [glft_live_calibrated.yaml](../config/strategies/glft_live_calibrated.yaml) · 10 min
  - Check: How did σ, A, k and γ change between the 2024 archive and our 2026 live data?

## Stage 7. The strategy as a running program
- [ ] **7.1** Spec §5 lifecycle and §7 risk limits, gaps, vol pause, rate limits · 30 min
  - Check: What triggers the kill switch, and what does it do after?
- [ ] **7.2** Code [src/mm/backtest/strategy.py](../src/mm/backtest/strategy.py): `PlaceOrder`, `ReplaceOrder`, `Fill`, `MarketState`, `Strategy` · 25 min
  - Why: the contract between any strategy and the engine.
  - Check: Why does a strategy return actions instead of calling the exchange?
- [ ] **7.3** Code `glft.py`: class `GlftMarketMaker`: `on_timer`, `on_fill`, `on_order_update`, `on_gap`, and `_PauseGate` · 45 min
  - Check: Why does a fill trigger an immediate requote?
- [ ] **7.4** Tests `test_glft.py`: `test_gap_clears_orders_*`, `test_drawdown_kills_*`, `test_flatten_escalates_*` · 15 min
  - Check: Why must flattening never stop until flat? (Hint: review v1_1 N1.)

## Stage 8. The data layer
- [ ] **8.1** Code [src/mm/data/events.py](../src/mm/data/events.py) and [schemas.py](../src/mm/data/schemas.py) module docstring (the three clocks) · 20 min
  - Check: Which timestamp may a backtest strategy act on, and which would be lookahead?
- [ ] **8.2** Code [src/mm/data/book.py](../src/mm/data/book.py): `OrderBook`, `BookSynchronizer` (`on_diff`, `on_snapshot`, `_try_sync`); test [tests/test_book_sync.py](../tests/test_book_sync.py) · 30 min
  - Why: snapshot + diff sync from the market-data lesson, in code.
  - Check: What happens when an update ID is missing?
- [ ] **8.3** Code [src/mm/data/download.py](../src/mm/data/download.py) `main` (skim), [record.py](../src/mm/data/record.py) `Recorder`, [loaders.py](../src/mm/data/loaders.py) `replay` · 30 min
  - Check: How does `replay` merge several streams into one time order?

## Stage 9. The backtest engine
- [ ] **9.1** [src/mm/backtest/README.md](../src/mm/backtest/README.md): picture, fill models, known biases · 25 min
  - Check: Name two reasons simulated fills are too optimistic.
- [ ] **9.2** Code [engine.py](../src/mm/backtest/engine.py): `on_market_event`, `_schedule_delivery`, `_run_until`, `_submit` · 35 min
  - Why: how latency is applied in both directions.
  - Check: When does the strategy see an event published at time t?
- [ ] **9.3** Code [exchange.py](../src/mm/backtest/exchange.py): `on_action`, `_place`, `_replace`, `_risk_check` · 25 min
  - Check: When is a post-only order rejected?
- [ ] **9.4** Code [fills.py](../src/mm/backtest/fills.py): `QueueFillModel._on_trade`, `QuoteTradeFillModel`, `L2FillModel`; tests in [test_backtest_fills.py](../tests/test_backtest_fills.py) · 40 min
  - Check: Where does our order start in the queue, and what moves it forward?
- [ ] **9.5** Review [reviews/2026-10-19_infrastructure_review.md](../reviews/2026-10-19_infrastructure_review.md) H1–H4, then the Verdict of [_final.md](../reviews/2026-10-19_infrastructure_final.md) · 25 min
  - Why: the bugs a skeptic found before any strategy ran.
  - Check: Why was H2 (seeing quotes before Binance published them) a lookahead bug?

## Stage 10. Metrics, reports and how a run is driven
- [ ] **10.1** Code `metrics.py`: `Recorder`, `_markout_metrics`, `compute_metrics`; README "Metrics glossary" · 30 min
  - Check: How is a 10 s markout computed for one fill?
- [ ] **10.2** Code [run.py](../src/mm/backtest/run.py): `run_period`, `seeds_for`, `run_all`; [report.py](../src/mm/backtest/report.py): `headline`, `write_report` · 25 min
  - Check: Why run several seeds, and where is each seed recorded?
- [ ] **10.3** Config [config/backtest/glft_smoke.yaml](../config/backtest/glft_smoke.yaml) vs [glft_live.yaml](../config/backtest/glft_live.yaml) · 10 min
  - Check: What differs: data source, fill model, warm-up?

## Stage 11. The story of the runs (2024 archive data)
- [ ] **11.1** Spec §9 Hypotheses (H1–H8) · 15 min
  - Why: each run tests these. Know them before reading results.
  - Check: What result would falsify H2?
- [ ] **11.2** Report [2026-10-22_glft_smoke](../reports/2026-10-22_glft_smoke/summary.md) + review [2026-10-22_glft_smoke.md](../reviews/2026-10-22_glft_smoke.md) (Verdict, F1, F2, F6) · 30 min
  - Check: The model over-predicts our fills by 2.3–3.6×. What does that mean for the quotes?
- [ ] **11.3** Report [2026-10-22_glft_v1_1](../reports/2026-10-22_glft_v1_1/summary.md) + review [v1_1](../reviews/2026-10-22_glft_v1_1.md) (N1, N2, N3) · 30 min
  - Check: Why are variant comparisons confounded by random latency paths?
- [ ] **11.4** Report [2026-10-23_glft_v1_2](../reports/2026-10-23_glft_v1_2/summary.md) (sensitivity, charts) + review [v1_2](../reviews/2026-10-23_glft_v1_2.md) · 30 min
  - Check: Which hypotheses held? Why did PnL still lose?

## Stage 12. Our own live L2 data (2026-10-24/25)
- [ ] **12.1** Report [2026-10-30_glft_live](../reports/2026-10-30_glft_live/summary.md) incl. warm-up and outages; `markouts.png` · 30 min
  - Check: Gross PnL is negative before fees on both days. Why does that rule out "find a cheaper fee tier"?
- [ ] **12.2** Review [2026-10-30_glft_live.md](../reviews/2026-10-30_glft_live.md): Verdict, §2 L2 realism, §4 Interpretation, §7 Follow-up · 30 min
  - Check: What does "H2 realised / model = 0.16" mean in plain words?

## Stage 13. Independent cross-check with hftbacktest
- [ ] **13.1** [hftbacktest/README.md](hftbacktest/README.md) + [01-how-hftbacktest-works.md](hftbacktest/01-how-hftbacktest-works.md) (skim [research/hftbacktest.md](../research/hftbacktest.md) as reference) · 50 min
  - Check: What are the two clocks in hftbacktest, and which processor uses which?
- [ ] **13.2** [02-vs-our-engine.md](hftbacktest/02-vs-our-engine.md) + [03-clocks-and-our-data.md](hftbacktest/03-clocks-and-our-data.md); [hft/README.md](../hft/README.md) · 40 min
  - Check: Why did our PC clock need the robust floor method?
- [ ] **13.3** Code [hft/glft_hft.py](../hft/glft_hft.py): `compute_quotes` (compare to `glft.py`), `side_cycle`, `glft_mm` · 30 min
  - Check: What does the port leave out that our strategy has?
- [ ] **13.4** [04-how-to-run.md](hftbacktest/04-how-to-run.md) (skim) + [05-first-results.md](hftbacktest/05-first-results.md) + [hft/results/COMPARISON_INPUT.md](../hft/results/COMPARISON_INPUT.md) · 40 min
  - Check: Fill counts differ between the engines, but markouts agree. Why is the agreement the important part?

## Stage 14. Wrap-up: what we learned and what would come next
- [ ] **14.1** Spec §8 Assumptions that rarely hold, §12 Open questions · 15 min
- [ ] **14.2** Research survey §1.3 and §2 (signals: imbalance, microprice, OFI, toxicity) + lesson 00 §3 "how firms stack" · 30 min
  - Check: Which signal would you add first against adverse selection, and why?
- [ ] **14.3** Explain the whole project back in 5 sentences (to the tutor, or in a note) · 15 min
  - Check: Why does a correct implementation of a famous model still lose here?

## Optional side trips
- [ ] [tooling/git-basics.md](tooling/git-basics.md): looking at old versions of any file.
- [ ] The demo quoter: [src/mm/backtest/examples/demo_quoter.py](../src/mm/backtest/examples/demo_quoter.py) and [reports/2026-10-19_demo_quoter_smoke/](../reports/2026-10-19_demo_quoter_smoke/summary.md): the simplest strategy the engine ran first.
- [ ] Paper trading: [src/mm/data/live.py](../src/mm/data/live.py) `LiveFeed` and one `reports/paper_*` folder.

Total: about 25–30 hours at a steady pace. Stages 1–5 are the theory; 6–10 the machinery; 11–13 the evidence.
