# Learnings: study guide index

Each doc opens with Key ideas and ends with a dated Decisions log and check questions (answers on request).

**Start here:** [READING_PATH.md](READING_PATH.md): one ordered path through the lessons, spec, code, reports and reviews, with checkboxes for progress.

## market-setup/ (read in this order)
1. [venues-and-instruments.md](market-setup/venues-and-instruments.md): venues and fees, spot vs perps (funding, margin, mark price), choosing ETHUSDT, and why 1-tick quoting at VIP0 loses money.
2. [market-data.md](market-setup/market-data.md): trades/L1/L2/L3, snapshot + diff sync, the three clocks, what the free archive really has, and queue-position modelling.
3. [paper-trading-and-simulation.md](market-setup/paper-trading-and-simulation.md): backtest → paper → live, simulator vs testnet, why simulated fills are optimistic, and fill rules.

## strategy-glft/ (read in this order; read market-setup/ first)
Teaching track for the fee-aware GLFT baseline on ETHUSDT perp. Market structure is introduced lesson by lesson.
0. [00-choosing-the-strategy.md](strategy-glft/00-choosing-the-strategy.md): the 6-idea menu, why GLFT first, accepted downsides, how firms stack base quoter + signals + overlays, and the full curriculum.
1. [01-how-a-market-maker-earns-and-loses.md](strategy-glft/01-how-a-market-maker-earns-and-loses.md): order types and takers, one full round trip with fees, the 4-bucket PnL identity, adverse selection and markouts, stuck inventory.
2. [02-inventory-risk-and-reservation-price.md](strategy-glft/02-inventory-risk-and-reservation-price.md): why inventory variance grows as q²σ²t, the Avellaneda–Stoikov reservation price, stationary GLFT `r = m − q·ω`, and why Q exists (margin, liquidation).
3. [03-fill-probability.md](strategy-glft/03-fill-probability.md): fill rate vs distance `λ(δ) = A·e^(−kδ)`, the calibrated A and k, the 400 ms-lagged mid, bursty flow and the queue.
4. [04-optimal-spread-glft-closed-form.md](strategy-glft/04-optimal-spread-glft-closed-form.md): GLFT's closed form (c1, ω, h), solving γ from a target skew, tick rounding, the 10-tick floor, requote rules, and the spec §10 example by hand.
5. [05-fees-and-break-even.md](strategy-glft/05-fees-and-break-even.md): "exact" vs "shift" fee modes, which side the fee floor binds on, break-even fee, VIP tiers and rebates, and why funding is left out of v1.
6. *Planned:* calibrating σ, A, k from our data.
7. *Planned:* PnL decomposition and what the backtest must measure.
8. *Planned:* weaknesses, and the extensions from ideas 2 and 3.

## hftbacktest/ (read in this order; read market-setup/ and strategy-glft/ 1–5 first)
Independent cross-check of our engine with the open-source hftbacktest 2.4.4 simulator. [README.md](hftbacktest/README.md) holds the track's index and its 2026-10-30 decisions (separate venv, own converter, robust clock, gap handling, GLFT port, no bookTicker, no-partial-fill default).
1. [01-how-hftbacktest-works.md](hftbacktest/01-how-hftbacktest-works.md): event array and flag bits, the two clocks, EXCH vs LOCAL processors, depth, the `elapse` loop and order states, latency, queue and exchange models with a worked example, fees, the ROI bug.
2. [02-vs-our-engine.md](hftbacktest/02-vs-our-engine.md): side-by-side with our engine (clock, numba, speed, queue, replace, latency, gaps, metrics) and why a cross-check is worth it.
3. [03-clocks-and-our-data.md](hftbacktest/03-clocks-and-our-data.md): the converter mapping, the drifting PC clock and the robust floor method, snapshots, gaps, hourly files and event order.
4. [04-how-to-run.md](hftbacktest/04-how-to-run.md): `.venv-hft`, convert, check, run, variants, result files, common errors.
5. [05-first-results.md](hftbacktest/05-first-results.md): hour 10 and full days 10-24 / 10-25 vs our engine; why fill counts differ, why markouts agree (adverse selection confirmed), and lessons from the bugs.

## tooling/
1. [git-basics.md](tooling/git-basics.md): what the repo and a commit are, the commit chain and HEAD, read-only vs file-changing commands for old versions, why not to `switch --detach` during another session, and the add → commit workflow.
