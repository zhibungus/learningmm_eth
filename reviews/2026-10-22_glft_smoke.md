# Review: fee-aware GLFT market maker and its first smoke backtest (2026-10-22)

Reviewer: validator. Run reviewed: `reports/2026-10-22_glft_smoke/` (seed 42, IS 2024-03-29, OOS 2024-03-30, `--no-sensitivity`), with calibration `reports/2026-10-22_glft_perp_calibration/`.
Full suite: **152 passed** (`.venv\Scripts\python.exe -m pytest -q`). Nothing in `src/`, `tests/`, `specs/`, `config/`, `learnings/` or `research/` was edited.

What I ran (all in my scratch area, no `reports/` folder created):
* `probe.py`: replays each period through the real engine with a **logging-only** subclass of `GlftMarketMaker`. It reproduces the report to the cent (IS −3.1035, OOS −5.0545) and logs every action, order update, the kill and the flatten.
* `ioc_probe.py`: a 3-event `SimExchange` check of a non-marketable IOC.
* A check of the 1-second mid increments on 2024-03-29 (in-sample data only).

[CONFIRMED] = reproduced or measured. [SUSPICION] = from reading the code only.

## Verdict: **PASS WITH CAVEATS** (implementation), **not yet a test of the strategy**

* **The strategy code matches the spec.** Formulas, fee folding, rounding, sizing, requote rule, pauses and kills all match. I found no lookahead and no tuning on out-of-sample.
* **The negative result is believable.** The fill model does not flatter it. Fills are mostly real trade-throughs, priced at our own limit.
* **The run is a smoke test only.** Only `no_cross_fills` ran, so H3, H6 and H8 are untested. H2 is **falsified** on both days (the fit over-predicts fills by 2.3–3.6×).
* **One engine bug in the risk path** (F1). A non-marketable IOC rests in the book. Fix it before any paper run that relies on the kill switch.

## Key numbers from the probes

| | IS | OOS |
|---|---|---|
| Actions: replaces / new / cancel-all | 77,243 / 1,943 / 653 | 43,766 / 1,105 / 419 |
| Replaces driven by quote / timer / fill | 74,990 / 1,914 / 339 | 42,550 / 1,061 / 155 |
| `replace_rejected: unknown_order` (all came after a full fill of that order) | 560 (560) | 246 (246) |
| `post_only_would_cross` (new / replace) | 100 / 16 | 60 / 6 |
| Shrink-in-place replaces | 0 | 0 |
| Pauses: spread / stale / vol | 682 / 2 / 2 | 416 / 5 / 0 |
| Kill | none | drawdown, 19:16:46 UTC, q = +0.05 |
| Mean posted δ (ticks) | 101.6 / 100.3 | 100.1 / 100.2 |
| Model-predicted fills, Σ A·e^(−kδ_live)·dt over quoting time | 1,819 | 1,421 |
| Actual quote fills / ratio | 802 / **0.44** | 391 / **0.28** |
| Distance at pricing time → mid distance at fill, median (ticks) | 79.5 → 14.8 (through) | 81.5 → 17.0 |

---

## Findings, ranked by severity

### F1. HIGH: a non-marketable IOC rests in the book and fills as a maker [CONFIRMED, engine]
*Evidence:*
* `exchange.py:236-254`. The "cancel the IOC remainder" branch (line 244) is inside `if marketable:`.
* A non-marketable IOC skips that branch and reaches `on_new_resting` (line 252), so it rests like a GTC order.
* `ioc_probe.py`: true bid 3489.30, IOC sell 0.05 @ 3489.40. Result: `accepted`, the order rests, and a later buy print fills it as **maker**.

*What happened in this run (OOS):*
* At the kill the strategy saw a bid of 3489.45 and sent an IOC sell 0.05 @ 3489.40.
* By the time it arrived, the true bid was below 3489.40. The order rested and was filled 0.5 s later as a **maker** `through` fill, in 3 pieces (`fills.parquet`, client id `f1105`).
* So the report shows **0 taker fills**, and the flatten paid 2 bp instead of 5 bp.

*Bias:* the flatten looks cheaper than it is (small here, about 0.05 USDT). On Binance this IOC would expire unfilled and the retry would cost more.

*Second risk [SUSPICION]:*
* `glft.py:470` clears the in-flight flag on `accepted`. The retry guard at `glft.py:548-549` then sees no flatten in flight.
* So a resting "IOC" plus the next timer's retry can **double-flatten** and flip the position.
* This run escaped only because the fill report arrived (206.88 s) before the next timer (207.0 s).

*Fix (backtester):* cancel any IOC remainder whether or not it was marketable, with reason `ioc_expired`. Add a test.
*Fix (quant-developer):* treat a flatten order as in flight until it reaches a terminal state: fill with `leaves_qty == 0`, `canceled`, or `rejected`. Do not clear it on `accepted`.

### F2. HIGH: H2 is falsified, so the fitted λ(δ) over-predicts our fills by 2.3–3.6× [CONFIRMED]
*Evidence:* the table above. My prediction integrates the calibrated λ at the **live** distance of each resting quote over the time it rested. Realised/predicted = **0.44 IS** and **0.28 OOS**. Spec §9 says H2 is falsified outside [0.5, 2]. The caller's simpler 774/side/day vs about 400 gives about 0.5, which is borderline. Both say the same thing.

*Likely drivers [SUSPICION, not decomposed]:*
1. **The calibration re-pegs every 500 ms; the strategy does not.** The calibration places a fresh order at δ every 500 ms (`glft_calibrate.py:252-271`). The strategy keeps an order until its target is ≥ 5 ticks off. When the mid runs at it, it chases away (urgent replace), and it cannot act while a replace is in flight (`glft.py:639-640`).
2. **Selection.** 682 / 416 spread pauses cancel all quotes for at least 5 s, exactly in the busy moments after jumps. Those are the windows that carry most calibration hits. A day-average λ then over-predicts fills for the time we actually quote.
3. **A is grid-dependent.** Δt = 100 / 500 / 1000 ms gives A = 0.57 / 0.36 / 0.26 (k changes only 15%). λ̂ is really a "hit probability inside a latency window", not a stationary Poisson rate. The spec gate only checks k, so it passed. A should be treated as soft.

*Bias:* the model's h and ω assume about 2× more fills than happen. Inventory then unwinds more slowly than GLFT assumes, so the skew is too weak (see F6). It does not flatter PnL.

*Fix (backtester):* report H2 by default as realised / Σλ(δ_live)·dt, with a bootstrap CI.
*Fix (quant-researcher):*
* Decide whether calibration should mimic the requote rule, or exclude paused windows.
* Add an A-stability gate.
* Decide all of this on IS only.

### F3. MEDIUM: the smoke run does not test the spec's hypotheses, and the report omits the strategy's own fields [CONFIRMED]
* `--no-sensitivity` ran only `no_cross_fills`. `skew_off`, `fee_shift`, `gamma_low/high`, `pessimistic_fills`, `higher_latency` and `latency_spikes` did not run. So H3, H6 and H8 cannot be judged. H5 has no bootstrap CI. H1 ("recompute every quote from logged inputs") was not done.
* `GlftMarketMaker.summary()` (`glft.py:504-515`) is only **printed** (`glft.py:499-502`). Pauses, kills by cause, rejects, mean posted δ and time quoting per side are all required by spec §9. **None of them appear in `summary.md` or `metrics.json`.** The OOS drawdown kill is not mentioned anywhere in the report.
* The OOS "per day" numbers cover only 19.3 h, because quoting stopped at the 19:16 kill.

*Fix (backtester):* collect `strategy.summary()` (if present) into `metrics.json` and `summary.md`. Then run the full sensitivity block plus the spec §9 variants on IS and OOS.

### F4. MEDIUM: the reject-storm kill counts benign `unknown_order` races [CONFIRMED counts; kill risk SUSPICION]
* `glft.py:473-484` counts `rejected`, `replace_rejected` and `cancel_rejected` for every reason except `awaiting_resync`. That follows spec §7.1 literally ("any reason").
* **All** 560 (IS) and 246 (OOS) `unknown_order` rejects are replaces that crossed a fill we had not heard about yet. They are harmless, and the spec itself (§4.6) says they mean "already filled".
* The 116 / 66 `post_only_would_cross` rejects are also expected. They are placements priced from a stale view just after a jump.
* Both kinds cluster in fast markets. A burst of more than 20 in 60 s would kill a healthy strategy. That has not happened yet.

*Developer interpretation judged:* faithful to the spec, but the spec rule is wrong.
*Fix (quant-researcher):* exclude `unknown_order`, or count it separately with its own limit.
*Fix (quant-developer):* implement whatever the spec decides, and add a test.

### F5. MEDIUM: replacing with `qty = qty*` after an unseen partial fill can push |q| past Q [SUSPICION, not observed]
*How it can happen:*
* Spec §4.6 says "always pass qty=qty*", and the code does (`glft.py:659`).
* The engine re-places the full new qty (`exchange.py:261-276`).
* If the live order was partly filled less than about 370 ms earlier, the true exposure becomes q_true + qty*, which is more than Q by up to that partial amount.

*In this run:*
* The exchange backstop `max_position` would reject it as `risk_max_position`. There were 0 such rejects.
* Partial fills are rare (64 of 802 fills belong to multi-fill orders).

*Paper/live:* no such backstop exists there, so |q| could overshoot Q by less than Δ. That is still well below the 1.5·Q breach.

*Other |q| paths I checked:*
* No second order per side can be placed: `_live_order` gates new placements.
* Flatten sized from a stale q can overshoot through zero by at most Δ.
* The F1 double-flatten.
* None of them reaches 1.5·Q.

*Fix (quant-researcher):* decide the rule, e.g. cap qty* at `live.remaining` unless the side is reducing, or accept the overshoot ≤ Δ and document it. Also check Binance's modify-order quantity semantics before paper trading (spec §12).

### F6. MEDIUM (finding about the model, not a bug): fills come from jumps, and the skew is weak next to h [CONFIRMED]
* **Jumps.** In-sample 1 s mid increments have median |x| = **0 ticks**, std 25 ticks, kurtosis **27**. The top 1% of seconds carry 32% of the variance. σ is a jump number, not a Brownian one, and GLFT's constant-σ, no-adverse-selection assumptions fail exactly where the fills happen (spec §8).
* **Weak skew.** At q = Q the skew is 25 ticks against h ≈ 100 ticks, so the reducing quote is still about 75 ticks out. OOS spent 3.5% of the time at the limit, and the drawdown kill fired while stuck at +Q in a down-move.
* H3 (`skew_off`) was not run, so we cannot yet say whether the skew helps at all.

*Fix (quant-researcher):* decide whether the target skew should be fixed relative to h (it is an owner choice: do not tune it on OOS). Discuss a jump-robust σ (e.g. bipower variation) for the spec.

### F7. LOW: report labels inflate "orders" [CONFIRMED, backtester]
* `exchange.py:194,233`: `_place` is reused for replaces, so "Orders accepted 78,510" includes 76,667 replaces.
* "Fill ratio 0.9%" therefore divides by replaces.

*Fix (backtester):* report new orders and replaces separately, and compute fill ratio over new orders.

### F8. LOW: naming and placement differ from spec [CONFIRMED]
* The spec names `src/mm/strategies/glft_perp.py` and `config/backtest/glft_perp.yaml` (§1, §6, §11). The code uses `glft.py` and `glft_smoke.yaml`.
* Spec §5 says "on_event (quote): run the risk checks". The code runs the drawdown and breach checks on the timer and on fills only (`glft.py:435`, `459`). That is a 1 s granularity and acceptable.

*Fix (quant-researcher):* update the spec to match.

---

## Answers to the specific questions

**1. Lookahead / leakage: none found.**
* **Calibration reads in-sample only** [CONFIRMED]. `calibrate()` loads only `periods["in_sample"]` (`glft_calibrate.py:446-453`) and refuses an overlapping OOS. The grid mid is the last quote at or before t (`mid_on_grid`, line 158-167). The windows lie strictly after t + L (line 268-271). σ, A, k, γ and the mean mid all come from 2024-03-29.
* **The strategy uses only `state` and its own past** [CONFIRMED]:
  * The engine strips `ref_mid` and `mechanism` from fills before the strategy sees them (`engine.py:224`).
  * The strategy never reads trades.
  * Staleness compares local time with the exchange time of the last *delivered* quote.
* **The 400 ms offset matches the engine.** The engine's median is feed 239 ms + order 132 ms ≈ 371 ms, so L = 400 is a fair constant. It ignores the delay tail (p99 772 ms, max 1.5 s).
* **"Strictly through" matches the engine's `through` rule** (`fills.py:263,268`: `kp < ko` / `kp > ko`). The calibration leaves out the engine's at-price `queue` fills (23 / 5) and `cross` fills (81 / 34), about 13% of fills, all with tiny spread capture. It also needs ≥ Δ inside one window, while the engine gives partial fills. Both effects are small next to F2.

**2. Spec fidelity.**
* **These match the spec and tests:**
  * fee folding, exact (`a_use`) vs shift;
  * c1, c2 with ξ = γ and the ξ = 0 limit forms;
  * ω = σ·c2 and h = c + c1 + (Δ/2)·ω;
  * the δ_min floor;
  * rounding away from mid;
  * the no-cross clamp against the last seen touch;
  * post-only GTC;
  * headroom counting all live orders except the one being re-priced;
  * the min-notional check;
  * the requote table, with urgent and force-on-fill;
  * pause hysteresis;
  * the gap cooldown;
  * the drawdown and breach kill with flatten.

  The tests check all three §10 vectors exactly. Mean posted δ ≈ 100.1–101.6 ticks matches the derived h = 100.5.
* **The developer's interpretations, judged:**
  * *Oversized live order shrunk in place* (`glft.py:660-665`). **Good**: it keeps queue priority and only ever reduces exposure. It never fired in this run, and no test covers it (add one).
  * *Reject storm counts all rejects except awaiting_resync*. **Faithful, but the spec should change** (F4).
  * *Bias-corrected EWMA* (`glft.py:375-390`). **Better than the spec.** A plain EWMA started at 0 would read σ about √2 too low at sample 60 and delay vol pauses. The researcher should adopt it in §7.3.
  * *Where each check runs.* Stale, drawdown and vol run on the timer, the spread gate on quotes, breach and drawdown also on fills. **Acceptable** (F8).
  * *Cooldown also blocks replaces, including urgent ones* (`glft.py:646`). Reasonable, but it also blocks the shrink path during a cooldown. Minor.

**3. Why about 30 ticks of capture when quotes are posted about 100 ticks out** [CONFIRMED]
* Spread capture is measured against the **mid just before the trade print that filled us** (`exchange.py:87`).
* Through fills: the median mid-to-price distance at fill is 15–17 ticks (p10 1.5, p90 about 100). The order was priced about 80 ticks from the mid the strategy saw.
* So **the mid has usually walked most of the way to our quote before the print**. The requote loop is too slow to escape: a 240 ms feed, 132 ms orders, nothing new sent while a replace is in flight, and price moving in jumps (F6).
* The p90 ≈ 100 cases are true sweeps. They earn full capture, then lose it to the −50-tick 1 s markout.
* **Accounting is neutral.** The capture/inventory split moves PnL between buckets, and gross is unaffected.
* **The fill model is, if anything, slightly conservative:**
  * fills are at our limit price;
  * a strict print-through is required;
  * the queue at deep levels is infinite;
  * the cross-fill effect is tiny (+0.02 USDT).

  The engine's "better WITH cross fills" warning on IS (−3.10 vs −3.47) comes from a different inventory path, not from cross-fill capture.
* One unmodelled pessimism [SUSPICION]: real feed delays spike *during* sweeps. The engine's delays are independent of activity, so reality is probably worse.

**4. Churn of about 55 actions/min: expected, not a bug** [CONFIRMED]
* 94% of actions are quote-driven replaces. Each one moves the price, because shrink replaces = 0.
* In-sample, the mid moves ≥ 5 ticks in 35% of seconds. With a 5-tick threshold and at most about 1 replace per second per side, that predicts about 42–60 replaces/min. The run shows about 53.
* No replace loops.
* The 560 / 246 `unknown_order` rejects are all races with a full fill. The 116 / 66 `post_only_would_cross` are stale-view placements after jumps. Neither is a bug.
* A 5-tick threshold is about 5% of h, so each replace changes λ by only about 17%. A larger threshold would cut churn a lot. That is a researcher decision, to be made on IS only, not here. This is within the spec's ≤ 150/min budget. Binance rate limits are still unverified (spec §12).

**5. Risk.**
* **The OOS kill behaved as specified, apart from F1.** At a timer tick the strategy-view drawdown was 5.016 > 5. Peak equity never rose above the start, so this is the cumulative day loss. Position was +0.05.
* The strategy then sent CancelAll and one IOC sell for 0.05 at bid − 5 ticks. Quoting stopped for the remaining 4.7 h. The final drawdown was 5.054.
* There was no reject storm, inventory breach or vol pause that day.
* **|q| never exceeded Q** (max 0.050, 0 risk rejects). Possible paths: F5 (less than Δ, live only), F1 (double flatten), and a stale-q flatten overshooting by at most Δ.

**6. Overfitting: nothing was tuned on OOS** [CONFIRMED as far as provenance shows]
* Calibration was written at 13:57 UTC and the single backtest at 14:19 UTC. The YAML params equal `calibration.json` to 10 digits. No other GLFT report folders exist.
* All non-fitted parameters are spec defaults chosen before the data.
* I cannot prove no earlier runs were deleted. Committing the code and report would settle provenance.

**7. Simplicity (quant-developer; behaviour-preserving only)**
* `glft.py`
  * Line 33 imports `replace`, which is unused; its comment explains an unused function. Remove both.
  * The optional `gamma=` override is threaded through `xi_of`, `c1_static`, `c2_skew`, `closed_form` and `compute_quotes` (lines 145-203, 245) only for calibration. Have `solve_gamma` use `dataclasses.replace(p, gamma=g)`, so every function reads `p.gamma`.
  * `GlftParams` carries 17 calibration-only fields (lines 95-112). Move them to a small dataclass in `glft_calibrate.py`. Note this changes which YAML keys `from_dict` accepts, so coordinate with the backtester.
  * `stats["fills_signed_qty"]` (line 453-455) is never reported. Drop it and `s_i`.
  * `floor_to_tick` / `ceil_to_tick` could use `math.floor` / `math.ceil` instead of numpy.
  * Trim the over-long comments at lines 298-299, 337-339, 342-343, 350, 546-547 and 581-582 to one short line each.
* `glft_calibrate.py`
  * Almost every line has a comment. The imports alone take about 45 lines (18-62). Several comments say the same thing twice: 102-106, 308-314 (inline repeats), 386, 470-472, 478-480, 498-500. Cut it by about 40% and keep one guiding comment per chunk.
  * Line 533: `{**cfg[...], **{k: v for k, v in out["params"].items()}}` can be `{**cfg[...], **out["params"]}`.
  * `fit_curve` returns a 6-tuple. A small result dataclass would read better (optional).
* `test_glft.py`
  * Lines 11-16 repeat the docstring's reading tips (8-9).
  * `import math` sits inside a test (line 118).
  * `_live_reset` (line 308) can be `cids`.
  * Missing tests: the `unknown_order` path, the shrink-in-place path, a partial fill then a replace, and a flatten that is accepted but not filled (F1).

## Fixes by owner

| # | Fix | Owner |
|---|---|---|
| F1 | Cancel any IOC remainder whether or not it was marketable; test it | backtester |
| F1 | Keep a flatten order in flight until it reaches a terminal state | quant-developer |
| F2 | Report the H2 ratio by default | backtester |
| F2 | Rethink the calibration-vs-requote mismatch and the paused windows; add an A-stability gate (IS only) | quant-researcher |
| F3 | Put `strategy.summary()` into the report; run the full sensitivity block plus the §9 variants | backtester |
| F4 | Exclude or separately cap `unknown_order` in the reject storm | quant-researcher, then quant-developer |
| F5 | Decide the replace-qty rule after partial fills; check Binance modify semantics | quant-researcher, then quant-developer |
| F6 | Decide skew at Q relative to h, and the jump-robust σ question | quant-researcher (owner decision; tutor writes it up) |
| F7 | Separate new orders from replaces in the report | backtester |
| F8 | Align spec file names; adopt the bias-corrected EWMA and the check placement in the spec | quant-researcher |
| 7 | Simplify code, comments and tests as listed | quant-developer |
