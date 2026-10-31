# Backtest, paper trading and live: simulating fills honestly

*Written 2026-10-19. Synced with code 4b1640d on 2026-10-23 (see §5).*

## Key ideas
- Three stages: **backtest → paper → live**. Each catches errors the previous one misses.
- The hardest thing to simulate in MM is **fills**. Simulators are almost always too optimistic.
- Local simulator tests the **strategy**. Testnet tests the **order-handling code**.
- Run backtests under more than one fill rule. **Profitable only under the optimistic rule means not profitable.**
- Only real fills can calibrate a simulator.

---

## 1. The three stages

```
  BACKTEST  ──────────>  PAPER TRADING  ──────────>  LIVE (small size)
  historical data        live feed, real time        live feed
  simulated fills        simulated fills             real fills
  fast, many regimes     real latency, plumbing      the only ground truth
  cost: free             cost: free                  cost: real money
```

| Stage | Catches | Misses |
|---|---|---|
| Backtest | Logic bugs, parameter sensitivity, rough PnL, many regimes | Data/fill-model errors; look-ahead bias |
| Paper | Feed plumbing (reconnects, resyncs, gaps), our real latency, keeping up in real time, today's market | Real queue, real fills, impact, rejects |
| Live (small) | Everything: fills, fees, rejects, rate limits, margin | Behaviour at bigger size |

- Analogy: backtest = flight sim with recorded weather. Paper = sim with today's weather. Live = flying.
- Paper data is forward data, so it cannot be overfit in advance.
- But paper uses the **same fill model** as the backtest. Its optimism carries straight through.

**Remember: paper trading does not fix an optimistic fill model.**

---

## 2. Local simulator vs testnet

|            | Local simulator on live feed (chosen)          | Binance futures testnet                                                                               |
| ---------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Needs keys | No                                             | Yes                                                                                                   |
| Market     | Real spreads, volatility, toxic flow           | Thin, artificial book; odd prices and jumps                                                           |
| Fills      | Our fill model                                 | Testnet matching; says little about real fills                                                        |
| Tests      | Strategy behaviour; backtest-vs-live agreement | Auth, acks, rejects (bad tick, min notional, margin), rate limits, cancel-replace, position reporting |
| Blind to   | Real queue, exchange behaviour                 | Real strategy PnL                                                                                     |

- Simulator uses the same streams as the recorder ([`data.yaml`](../../config/data.yaml) `live.endpoints`).
- Same fill model as the backtest, so results are directly comparable.

**Remember: before going live you want both. Simulator for strategy, testnet for plumbing.**

---

## 3. Why simulated fills are optimistic
The errors point the same way, so they do not cancel.

| # | Cause | Effect |
|---|---|---|
| 1 | Queue position guessed (see [market-data.md §7](market-data.md)) | Fill sooner, more often |
| 2 | Adverse selection under-counted | "Touch = fill" gives fills on harmless flow too |
| 3 | Latency too small | You "pull" quotes that really would have been hit. Act at `ts_ns + latency` ([`schemas.py`](../../src/mm/data/schemas.py) module docstring) |
| 4 | You are invisible | Your order joins no queue; nobody reacts; you take no one's liquidity |
| 5 | No rejects or outages | Live has rate limits, rejects, maintenance, disconnects |
| 6 | Fees/funding forgotten | VIP0 maker on ETHUSDT ≈ **96 ticks** per round trip ([venues-and-instruments.md](venues-and-instruments.md)). Assuming a rebate tier we lack turns losers into "winners" |

### Fill rules (our bid at 2395.44)
```
 optimistic  <──────────────────────────────────────>  conservative
 TOUCH                  QUEUE MODEL                    TRADE-THROUGH
 any trade @ 2395.44    trades @ 2395.44 > queue ahead a trade @ <= 2395.43
 assumes you're first   depends on cancel assumption   price went through you,
                        (honest middle)                so you must have filled
```

**Example. One minute: 30 ETH trades at 2395.44, 40 ETH was ahead when we joined, nothing trades below.**
- Touch: filled.
- Queue model: not filled (30 < 40).
- Trade-through: not filled.
- Touch invents a fill. Across thousands of quotes that flips a backtest from loss to profit.

**Remember: report both queue-model and trade-through results.**

### What our engine does (code 4b1640d)

| Rule | Engine behaviour | Where |
|---|---|---|
| Base fill rule | Queue model (proportional cancels) **+** trade-through **+** quote cross, each fill capped by print / displayed size | `fills.py` `QueueFillModel` |
| Conservative row | `pessimistic_fills`: trade-through only, cancels all behind, queue × 1.5 | `config.py` `DEFAULTS["sensitivity"]` |
| Deeper than the touch | Queue unknown → infinite → fills only on trade-through | `QuoteTradeFillModel.on_new_resting` |
| Replace (modify) | **Loses priority**: back of the queue, as on Binance (`exchange.replace_keeps_priority: false`) | `exchange.py` `SimExchange._replace` |
| Replace size | `ReplaceOrder.total_qty` = new **total** size; if total ≤ already filled, the order is **cancelled** | `SimExchange._replace` |
| IOC / market order | **Never rests.** Fill what you can as taker, cancel the rest (`no_liquidity` / `ioc_remainder`) | `SimExchange._place` |
| Randomness | Separate RNG streams for feed delay, order jitter and strategy, so variants share latency paths | `engine.py` `build_engine` |
| Seeds | Base and strategy-param variants run on 5 seeds; seed sd of GLFT base net PnL ≈ 0.36 USDT in-sample | `sensitivity.seeds`; [review](../../reviews/2026-10-22_glft_v1_1.md) N2 |

- **Not modelled:** margin/liquidation, exchange rate limits, self-trade prevention (`exchange.py` docstring).
- GLFT v1.1 in-sample fills: 698 through, 81 cross, 23 queue of 802. Under `pessimistic_fills` the loss barely changes (−3.59 vs −3.10), so it is not a fill-model artefact.

### Calibrate and measure
- Later, at tiny live size: log real fills next to what the simulator would have done. The gap (fill rate, markout) is the simulator's bias.
- **Markout** now: after each simulated fill, track mid move at 1 s, 10 s, 60 s. Consistently negative = adverse selection. Visible even in simulation.

---

## 4. How professional firms choose
Varies by firm. Common patterns:
- **Go small-live quickly.** Simulated fills are unreliable, so small live size is the real test. Its cost buys real information.
- **Calibrate simulators to own fills**, per venue and symbol, re-checked regularly.
- **Testnets are for integration**: new venues, API changes. Not strategy.
- **Shadow mode**: the new strategy computes quotes alongside the live system. They are compared with what traded, nothing is sent.
- **Kill switches before size**: position limits, loss limits, cancel-all on disconnect, all tested first.

**Remember: with no live fills yet, treat every paper PnL as an upper bound with unknown error bars.**

---

## 5. Decisions log
- **2026-10-19. Paper trading mode: local fill simulator on the real live Binance feed** (not testnet). *Defaulted by the lead; owner can change it.* Why: no keys, real market, same fill model as backtest so results compare. Accepted: fills stay simulated and probably optimistic; order handling (rejects, rate limits, margin) untested. Before live: add a testnet stage for plumbing, then tiny live size to calibrate fills.
- **2026-10-23. Sync with code 4b1640d (no owner decision).** Added "What our engine does" in §3: replaces lose priority, `total_qty` modify semantics (total ≤ filled cancels), IOC/market never rests, separate RNG streams, multi-seed runs. Line-number citations replaced by file/key names.

---

## Check your understanding
1. Our ask rests at 2395.45 with 25 ETH ahead. In a minute, 20 ETH trades at 2395.45 and 1 ETH prints at 2395.46. Which of touch, queue model and trade-through say we filled?
2. Why can testnet confirm a "below minimum notional" reject is handled, yet say almost nothing about PnL?
3. A backtest wins under touch and loses under trade-through. What does that tell you, and what next?

*Answers on request.*
