# Market making on Binance ETH: literature survey

*Written 2026-10-19 by quant-researcher. Facts about Binance (fees, ticks, data) are in [binance-eth-spot-vs-perp-facts.md](binance-eth-spot-vs-perp-facts.md).*
*Tags: **[Paper]** = what the paper shows. **[Inference]** = my reading or extrapolation, not tested. **[Niche]** = less standard idea.*

## Key ideas
- **Classic models** (Avellaneda–Stoikov, GLFT) give a clean rule:
  - Shift your quotes against your inventory.
  - Widen with volatility and risk aversion.
  - They assume Poisson fills and constant volatility. Neither holds in crypto.
  - **None of them include fees.** You must add the fee yourself.
- **On a 1-tick-wide book**, you cannot choose a price inside the spread, so the classic models matter less. What decides your PnL is **when to be in the queue and when to pull**. Signals and adverse selection matter more than the spread formula.
- **The best-documented short-term signals** are order-book imbalance / microprice and order-flow imbalance (OFI). The crypto evidence is real but older (BitMEX).
- **Price discovery tends to happen in the perps**, not spot (BitMEX-era papers). The claim that "BTC leads ETH at milliseconds" is widely believed by practitioners, but **I found no peer-reviewed high-frequency paper** confirming it for Binance. We can test it with our own data.
- **The RL market-making evidence on crypto is weak**: short test windows, simple fill models, no live results.
- **At VIP0, edge has to come from somewhere other than spread capture**:
  - avoiding toxic fills (signals, pulling quotes),
  - 0-fee promo pairs,
  - or measuring the gap honestly.

```
              where PnL comes from for a passive quote
  spread captured   +  rebate/(-fee)   -  adverse selection   -  inventory drift
  (~0 on 1-tick)      (-2 bp perp VIP0)    (signals reduce it)    (skew reduces it)
```

---

## 1. Optimal quoting models

### 1.1 Avellaneda–Stoikov (AS)
**[Paper]** One dealer quotes a bid and an ask around a mid price that moves as Brownian motion. Fills arrive as Poisson processes whose rate decays with distance from mid. The dealer maximises exponential (CARA) utility of wealth at time T.

- Reservation price: `r = s - q·γ·σ²·(T - t)`
- Total spread: `δ_a + δ_b = γ·σ²·(T - t) + (2/γ)·ln(1 + γ/k)`, centred on `r`
- Fill intensity: `λ(δ) = A·exp(-k·δ)`

| Symbol | Meaning | Units |
|---|---|---|
| `s` | mid price | USDT |
| `q` | inventory (+ long) | ETH, or lots in the paper |
| `γ` | risk aversion | 1/USDT |
| `σ` | volatility of mid | USDT/√s |
| `T - t` | time to horizon | s |
| `δ_a, δ_b` | ask/bid distance from `r` | USDT |
| `A` | fill rate at zero distance | 1/s |
| `k` | decay of fill rate with distance | 1/USDT |

**Weaknesses:**
- A finite horizon T is artificial for a 24/7 market.
- The quotes depend on `(T - t)`, so the spread collapses as the horizon nears.
- There is no inventory limit, no fee, and no adverse selection.
- Fill rate depends only on distance, not on queue position.

> **Qualifier (2026-10-22):** this AS reservation price (skew ∝ σ²·(T−t)) is **not** what [`specs/glft-perp.md`](../specs/glft-perp.md) §4.1 uses. The spec uses stationary GLFT, `r = m − q·ω` with `ω ∝ σ·√γ` (linear in σ, no horizon). Different model, not an error in either; see spec §13 row 4.

**Citation:** Avellaneda, M. & Stoikov, S. (2008). *High-frequency trading in a limit order book.* Quantitative Finance 8(3), 217–224. https://doi.org/10.1080/14697680701381228

### 1.2 Guéant–Lehalle–Fernandez-Tapia (GLFT)
**[Paper]** Same setup as AS, but with an inventory limit `|q| ≤ Q` and a long-horizon (stationary) solution. That removes the `(T - t)` problem. The paper's closed-form approximation, with order size Δ = 1:

```
δ_b*(q) ≈ (1/γ)·ln(1 + γ/k) + ((2q + 1)/2) · ω
δ_a*(q) ≈ (1/γ)·ln(1 + γ/k) - ((2q - 1)/2) · ω
ω = sqrt( (σ²·γ) / (2·k·A) · (1 + γ/k)^(1 + k/γ) )
```

Here `δ_b`, `δ_a` are measured from the **mid** `s`. The other symbols are as in AS.
- The first term is a constant half-spread.
- The second term is the inventory skew. It grows linearly in `q` with slope `ω`.

**[Inference] Adding fees.** With maker fee `c` (in USDT per ETH, i.e. fee rate × price), a fill at distance δ earns `δ - c`. The simplest fix is to add `c` to both quotes: `δ → δ + c`. That is exact if the fill rate is unchanged by the shift. It is not exact otherwise.
- With `c ≈ 0.48 USDT` per ETH (2 bp perp), the quotes sit ~48 ticks from mid. They will rarely fill, given a 1-tick book.
- This is the honest result: **at VIP0 the "optimal" passive quote is mostly "far away"**.

> **Qualifier (2026-10-22), see [`specs/glft-perp.md`](../specs/glft-perp.md) §4.2, §4.4, §13, Appendix A:**
> - The shift above is only the spec's `"shift"` variant. Under exponential intensity the fee is an exact change of variable `δ̃ = δ − c`, so the fee problem is fee-free GLFT with `A → A·e^(−kc)`. The spec's default `"exact"` mode therefore also uses `A·e^(−kc)` in `ω`, which scales the skew by `e^(kc/2)` (≈11× at k = 10/USDT, c = 0.48). I checked this algebra; it holds up to O(f_m) terms (Binance charges the fee on fill price, not mid). The rescaling-of-A form is specific to `A·e^(−kδ)`.
> - "~48 ticks from mid" holds **at zero inventory** only. With inventory, the reducing side can quote inside the fee distance (34 ticks at q = +Q in spec §10), down to the spec's 10-tick floor. That is the skew paying to shed risk.

**Weaknesses:** the same Poisson and constant-volatility assumptions as AS, and no queue.
- `A` and `k` are fitted to how fill rate falls with distance. On a 1-tick book, almost all fills happen at distance 0–1 tick, so the fit is ill-posed near the touch.
- Practitioners fit `A`, `k` from trade-through rates at several depths. The hftbacktest tutorials do this on Binance perps. That is a practitioner source, not peer-reviewed.

**Citations:**
- Guéant, O., Lehalle, C.-A. & Fernandez-Tapia, J. (2013). *Dealing with the inventory risk: a solution to the market making problem.* Mathematics and Financial Economics 7(4), 477–507. https://arxiv.org/abs/1105.3115
- Guéant, O. (2017). *Optimal market making.* Applied Mathematical Finance 24(2), 112–154. https://arxiv.org/abs/1605.01862 (general intensities, multi-asset, closed forms)
- Practitioner: nkaz001, *hftbacktest* (GLFT, queue and imbalance tutorials on Binance futures data). https://github.com/nkaz001/hftbacktest

### 1.3 Extensions: book, alpha signals, hedging
- **Cartea–Jaimungal–Penalva book** **[Paper]**: the standard textbook. It covers AS/GLFT-type models, inventory penalties, adverse selection, and short-term alpha. Running inventory penalty: `-φ·∫q² dt`.
  - Cartea, Á., Jaimungal, S. & Penalva, J. (2015). *Algorithmic and High-Frequency Trading.* Cambridge University Press.
- **Cartea–Jaimungal–Ricci** **[Paper]**: adds short-term drift (order-flow momentum) and adverse selection. The MM skews quotes in the direction of the expected drift.
  - Cartea, Á., Jaimungal, S. & Ricci, J. (2014). *Buy low, sell high: a high frequency trading perspective.* SIAM J. Financial Mathematics 5(1), 415–444.
- **Market making with alpha signals** **[Paper]**:
  - Buy market orders push mid up for a short time, and sell market orders push it down.
  - The MM uses that "alpha" signal to cut adverse selection, take directional positions, and manage inventory.
  - The model was calibrated to Nasdaq data.
  - Cartea, Á. & Wang, Y. (2020). *Market making with alpha signals.* International Journal of Theoretical and Applied Finance 23(3), 2050016. https://doi.org/10.1142/S0219024920500168
- **Hedging on another venue** **[Paper]**: a dealer *internalises* (holds) inventory within a band and *externalises* (hedges on another market, paying impact) outside it.
  - The setting is FX (USDCNH), but the structure maps directly to "quote spot, hedge with perp" **[Inference]**.
  - Barzykin, A., Bergault, P. & Guéant, O. (2021–2023). *Algorithmic market making in dealer markets with hedging and market impact.* arXiv:2106.06974 (journal version reported as Mathematical Finance; I could not confirm it on the publisher's site). https://arxiv.org/abs/2106.06974
- **Earliest inventory model:** Ho, T. & Stoll, H. (1981). *Optimal dealer pricing under transactions and return uncertainty.* Journal of Financial Economics 9(1), 47–73.

**Assumptions that rarely hold in crypto (flag for any spec):**
- Poisson arrivals: crypto flow is bursty and self-exciting.
- Constant σ: ETH volatility clusters and jumps.
- Mid is a martingale: perp and BTC lead-lag says it is partly predictable.
- Fills don't depend on queue position.
- No fees.

---

## 2. Microstructure signals for quoting

### 2.1 Order-book imbalance and the microprice
**[Paper]** Define `I = Q_b / (Q_b + Q_a)`, where `Q_b`, `Q_a` are the best bid and ask sizes (ETH).

- The weighted mid `P_b·(1 - I) + P_a·I` is a common heuristic. It is noisy and can jump.
- Stoikov's **microprice** is `P_micro = mid + g(I, S)`:
  - `g` is the expected limit of future mid changes.
  - It is estimated from a discrete Markov chain on (imbalance bucket, spread).
  - It is meant to be a martingale, and it beat mid and weighted mid at forecasting short-horizon mid changes in the paper's US equity data.
- Gould & Bonart: queue imbalance at the touch predicts the direction of the next mid move, especially for large-tick stocks.

**[Inference] Use on ETHUSDT** (1 tick wide, so it behaves like a large-tick asset):
- Imbalance is the natural first signal.
- If `I` is high (bid much bigger than ask), the next move is more likely up. Sitting on the ask is then toxic, so pull or skew the ask.

**Weaknesses:**
- Spoofing and flickering size at the touch.
- On the perp the top level is often hundreds of ETH, and the signal can be dominated by one large order.
- The signal decays fast, so latency eats it.

**Citations:**
- Stoikov, S. (2018). *The micro-price: a high-frequency estimator of future prices.* Quantitative Finance 18(12), 1959–1966. https://ssrn.com/abstract=2970694
- Gould, M. D. & Bonart, J. (2016). *Queue imbalance as a one-tick-ahead price predictor in a limit order book.* Market Microstructure and Liquidity 2(2). https://arxiv.org/abs/1512.03492

### 2.2 Order-flow imbalance (OFI)
**[Paper]** OFI over an interval sums signed changes at the best quotes. Between book events n-1 and n:
```
e_n = 1{P_b,n ≥ P_b,n-1}·Q_b,n - 1{P_b,n ≤ P_b,n-1}·Q_b,n-1
    - 1{P_a,n ≤ P_a,n-1}·Q_a,n + 1{P_a,n ≥ P_a,n-1}·Q_a,n-1
OFI_k = Σ e_n over interval k ;   ΔP_k ≈ β·OFI_k + ε,   β ∝ 1 / depth
```
- Price changes over short intervals are roughly linear in OFI, and the slope is inversely proportional to depth (US equities).
- Kolm et al. (2023): deep nets on multi-level OFI forecast returns over several short horizons. They report OFI inputs work better than raw book states.
- Crypto (Silantyev 2019, BitMEX XBTUSD perp): **trade-flow imbalance** (signed taker volume) explains contemporaneous price changes better than book OFI. The paper attributes this to thin, rarely updated books. Those books were thinner than today's Binance books.

**Weaknesses:**
- OFI's well-documented result is *contemporaneous* (explains the same interval). Its predictive power is much smaller.
- The crypto evidence is old.

**Citations:**
- Cont, R., Kukanov, A. & Stoikov, S. (2014). *The price impact of order book events.* Journal of Financial Econometrics 12(1), 47–88. https://arxiv.org/abs/1011.6402
- Kolm, P. N., Turiel, J. & Westray, N. (2023). *Deep order flow imbalance: extracting alpha at multiple horizons from the limit order book.* Mathematical Finance 33(4).
- Silantyev, E. (2019). *Order flow analysis of cryptocurrency markets.* Digital Finance 1, 191–218. https://doi.org/10.1007/s42521-019-00007-w

### 2.3 Queue position, tick size and latency
**[Paper]**
- **Moallemi & Yuan:** an early queue position is worth money. Front-of-queue orders fill more often and face less adverse selection than back-of-queue ones. They estimated values of roughly the size of a half-spread (US equities). This is a working paper; I did not open the full PDF.
- **Dayri & Rosenbaum:** on large-tick assets (spread almost always 1 tick), the useful quantities are queue sizes and an "implicit spread", not the quoted spread.
- **Huang, Lehalle & Rosenbaum:** the queue-reactive model. Arrival, cancel and trade rates at each level depend on current queue sizes. It is a realistic simulator for backtesting fills.
- **Lehalle & Mounjid:** imbalance helps limit-order placement, but its value **falls with latency**. If you cannot cancel in time, predicting toxic flow does not help.

**[Inference]**
- On ETHUSDT (both books 1 tick wide) these are the most relevant papers.
- From home we are slow, and the perp top level is long. We will usually be **at the back of the queue**, which is the worst place by Moallemi & Yuan.
- Spot queues are shorter (tens of ETH), so the queue is easier to reach. But the fees are 5x higher.

**Citations:**
- Moallemi, C. C. & Yuan, K. (2017). *A model for queue position valuation in a limit order book.* Working paper, Columbia Business School. https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf
- Dayri, K. & Rosenbaum, M. (2015). *Large tick assets: implicit spread and optimal tick size.* Market Microstructure and Liquidity 1(1).
- Huang, W., Lehalle, C.-A. & Rosenbaum, M. (2015). *Simulating and analyzing order book data: the queue-reactive model.* Journal of the American Statistical Association 110(509), 107–122.
- Lehalle, C.-A. & Mounjid, O. (2017). *Limit order strategic placement with adverse selection risk and the role of latency.* Market Microstructure and Liquidity. https://arxiv.org/abs/1610.00261

### 2.4 Adverse selection and toxicity
**[Paper]**
- **Glosten–Milgrom:** spreads exist because some takers are informed. **Kyle:** price impact is proportional to order flow.
- **VPIN** (Easley, López de Prado & O'Hara):
  - Split trades into equal-volume buckets.
  - Classify each bucket's volume as buy or sell using bulk-volume classification.
  - `VPIN = mean over last n buckets of |V_buy - V_sell| / V_bucket`.
  - They argued it rose before the 2010 Flash Crash.
- **Critique** (Andersen & Bondarenko): VPIN's apparent predictive power comes largely from the classification scheme and from mechanical correlation with volume and volatility. Given those, it adds little.

**[Inference]**
- For us, a simpler and more direct toxicity measure is **markouts**: mid price change at +100 ms, +1 s, +10 s after each of our fills, signed by our side.
- This is standard practitioner practice rather than a single paper.
- Binance aggTrades already give the true aggressor side, so bulk classification is unnecessary. VPIN's main estimation problem disappears, but the critique about volume and volatility remains.

**Citations:**
- Glosten, L. & Milgrom, P. (1985). *Bid, ask and transaction prices in a specialist market with heterogeneously informed traders.* Journal of Financial Economics 14(1), 71–100.
- Kyle, A. S. (1985). *Continuous auctions and insider trading.* Econometrica 53(6), 1315–1335.
- Easley, D., López de Prado, M. & O'Hara, M. (2012). *Flow toxicity and liquidity in a high-frequency world.* Review of Financial Studies 25(5), 1457–1493.
- Andersen, T. G. & Bondarenko, O. (2014). *VPIN and the flash crash.* Journal of Financial Markets 17, 1–46.

---

## 3. Crypto-specific evidence

| Topic | What the paper shows [Paper] | Relevance [Inference] |
|---|---|---|
| Cross-exchange arbitrage | Large, persistent price gaps across exchanges and countries in 2017–18. They were bigger across countries than within one, and co-moved | Today's Binance spot vs perp gaps are a few bp, much smaller. Cross-venue lead-lag still exists |
| BTC price discovery | Unregulated derivatives (BitMEX, Huobi, OKEx perps and futures) led price discovery. CME and US spot reacted | Perps lead. On Binance the perp has ~16x spot volume |
| ETH price discovery | The BitMEX ETH perp dominated price discovery over major spot exchanges (minute data). Spot volume had hour-of-day and day-of-week patterns | **Spot quotes are the stale ones.** Use perp moves to pull spot quotes |
| CEX vs DEX | Binance generally leads Uniswap v2 for ETH (2024 events) | Confirms CEX leads. Not Binance spot vs perp |
| Perp pricing | No-arbitrage perp price, and bounds under trading costs. Perp–spot deviations are larger than in FX and co-move across coins; an implied arbitrage had high Sharpe ratios | Basis and funding are a structured, partly predictable quantity |
| Liquidation cascades | One study of Hyperliquid's L2 archive around the 2025-10-10 cascade found the median pooled spread reached 9.4 bp (p95 87.5 bp), with recovery within the day. Single event and venue; authors not verified | Spread blow-outs are concentrated, so a regime filter (pull or widen) is plausible |
| BTC leads ETH at high frequency | **No verified peer-reviewed paper found.** A CBS master's thesis (2019) found cross-exchange BTC lags up to 15 s | Practitioner belief. **Testable with our data** once we record BTCUSDT |
| Funding-time effects | **No academic paper found** on intraday price or flow patterns around Binance funding times | Plausible (positioning before the snapshot) but **unverified**. Testable from free archive data |

**Citations:**
- Makarov, I. & Schoar, A. (2020). *Trading and arbitrage in cryptocurrency markets.* Journal of Financial Economics 135(2), 293–319.
- Alexander, C. & Heck, D. F. (2020). *Price discovery in Bitcoin: the impact of unregulated markets.* Journal of Financial Stability 50. https://sussex.figshare.com/articles/journal_contribution/Price_discovery_in_Bitcoin_the_impact_of_unregulated_markets/23308262
- Alexander, C., Choi, J., Massie, H. R. A. & Sohn, S. (2020). *Price discovery and microstructure in ether spot and derivative markets.* International Review of Financial Analysis 71, 101506. https://doi.org/10.1016/j.irfa.2020.101506
- Alexander, C., Choi, J., Park, H. & Sohn, S. (2020). *BitMEX bitcoin derivatives: price discovery, informational efficiency, and hedging effectiveness.* Journal of Futures Markets 40(1), 23–43.
- Plazuelo Pascual, J., Tardon Rubio, C., Toro Cebada, J. & Hernando Veciana, A. (2025). *Price discovery in cryptocurrency markets.* arXiv:2506.08718. https://arxiv.org/abs/2506.08718 (preprint)
- He, S., Manela, A., Ross, O. & von Wachter, V. (2022, rev. 2024). *Fundamentals of perpetual futures.* arXiv:2212.06888. https://arxiv.org/abs/2212.06888 (preprint; one author discloses an industry role)
- Liquidation-cascade study: *Journal of FinTech and Digital Assets* (2026), Hyperliquid L2 panel. https://journals.lincoln.ac.nz/index.php/JFDA/article/view/1395 (**I saw only the abstract and could not verify the authors or title. Cite with care.**)
- Eross, A., McGroarty, F., Urquhart, A. & Wolfe, S. (2019). *The intraday dynamics of bitcoin.* Research in International Business and Finance 49, 71–81. (Intraday patterns. I did not re-check details in this session.)

---

## 4. Reinforcement-learning market making (weak evidence)

| Paper | Data | Result [Paper] | Caveats [Inference] |
|---|---|---|---|
| Spooner et al. (2018) | Equities / futures LOB | Temporal-difference agents beat simple baselines | Not crypto. Simplified fills |
| Sadighian (2019, 2020) | Crypto LOB. The 2020 paper used BitMEX: 8 days of training, 30 days of testing | Policy-gradient agents were profitable vs baselines. Event-based steps beat time-based steps | Very short samples, one regime, simulated fills, no fees or queue realism stated as central |
| Falces Marin et al. (2022) | 30 days of BTC-USD L2 | Deep RL tuning AS's γ and skew beat a genetic-algorithm-tuned AS on Sharpe and Sortino | Some large drawdowns. One month of data |
| Gašperov et al. (2021) survey | n/a | Reviews RL market-making papers | Notes the field's reliance on simulators |

**[Inference] Verdict:** RL is not a good first project here.
- The evidence uses short windows and simulated fills.
- The hard parts are the fill and queue model and fees, not the policy class.
- RL also overfits easily, which the validator would flag.
- One reasonable use later: **RL to tune 1–2 parameters of a model-based strategy**, as in Falces Marin.

**Citations:**
- Spooner, T., Fearnley, J., Savani, R. & Koukorinis, A. (2018). *Market making via reinforcement learning.* AAMAS 2018. https://arxiv.org/abs/1804.04216
- Sadighian, J. (2019). *Deep reinforcement learning in cryptocurrency market making.* arXiv:1911.08647. https://arxiv.org/abs/1911.08647
- Sadighian, J. (2020). *Extending deep reinforcement learning frameworks in cryptocurrency market making.* arXiv:2004.06985. https://arxiv.org/abs/2004.06985
- Falces Marin, J., Díaz Pardo de Vera, D. & Lopez Gonzalo, E. (2022). *A reinforcement learning approach to improve the performance of the Avellaneda-Stoikov market-making algorithm.* PLOS ONE 17(12), e0277042. https://doi.org/10.1371/journal.pone.0277042
- Gašperov, B., Begušić, S., Posedel Šimović, P. & Kostanjčar, Z. (2021). *Reinforcement learning approaches to optimal market making.* Mathematics 9(21), 2689.

---

## 5. Niche ideas

All of this section is **[Inference]** built on the papers above unless marked. Each is labelled **[Niche]**.

| # | Idea | Mechanism | Spot? | Evidence |
|---|---|---|---|---|
| N1 | **0-maker-fee promo pair MM** (ETH/U spot) **[Niche]** | Fee is 0, the book was ~40 ticks wide and thin, so spread capture is actually possible. Price off the ETHUSDT perp/spot microprice. Convert U↔USDT via U/USDT (1 bp tick) | Spot only. **Helps** | Promo confirmed by a secondary source (2026-01-27, "until further notice"). Current status unverified. $6.4M/day volume |
| N2 | **Quote spot, hedge on perp** **[Niche]** | Internalise spot fills within an inventory band and hedge outside it with perp taker orders (Barzykin et al. structure). Basis risk is small | Spot. **Hurts at VIP0** (10 bp maker + 5 bp taker hedge). Only viable with N1-style 0-fee spot | Paper is FX; crypto mapping untested |
| N3 | **Basis- and funding-aware reservation price (perp)** **[Niche]** | Add to the AS/GLFT reservation price: (a) the perp's deviation from the spot index, (b) the expected funding cost of holding `q` through the next funding time: `-q·ρ·mark·1{funding before exit}`, where ρ is the predicted funding rate | Neutral. Uses spot only as an index signal | He et al. show structured basis. Funding-time flow effects not verified |
| N4 | **Liquidation-cascade regime filter** **[Niche]** | Watch `forceOrder` rate, mark–last gap and realised volatility. Above a threshold, pull or widen quotes, or quote only the side that buys the forced selling once it eases | Perp-only signal (spot has no public liquidation feed). Spot neutral | One-event study (Hyperliquid). Mechanism standard |
| N5 | **Lead-lag stale-quote guard** | Cancel or skew our quotes when the leader moves first (the perp for spot quotes; BTCUSDT perp for ETH quotes) | **Helps** if quoting spot (spot follows). On the perp, the leader is BTC or other venues | Perp leads spot (papers). BTC→ETH at ms is practitioner belief only |
| N6 | **ETH/BTC cross pair** **[Niche]** | Tick is ~3.4 bp, so it is a pricing game, not a queue game. Price it from ETHUSDT/BTCUSDT (triangular) | Spot only | Thin (~$12M/day). At VIP0 a 1-tick spread (3.4 bp) still does not cover 20 bp of fees. Only viable with a 0-fee promo or high tier |
| N7 | **Stablecoin cross-book** **[Niche]** | Same ETH on USDT, USDC, USD1, U and FDUSD books. The thin books lag ETHUSDT; quote them off the main book's microprice | Spot only | Mechanism obvious. Fees decide viability (only U had 0 maker) |
| N8 | **Time-of-day / funding-time scheduling** **[Niche]** | Only quote in hours where measured markouts beat fees; skip funding-snapshot windows and major macro releases | Both | Hour-of-day volume patterns in ETH (Alexander et al. 2020). Funding-time effects unverified |

---

## 6. Gaps and open questions for the owner and other agents
- **Spot vs perp needs an explicit decision.** The logged decision is perp. Spot is attractive only via 0-fee pairs (N1/N7) or as a signal.
- **Spot data work (data-engineer):**
  - new archive path `data/spot`, REST `api.binance.com`, WS `stream.binance.com:9443`;
  - spot depth sync without `pu`;
  - **µs timestamps in spot archive from 2025-01-01**;
  - no free spot L1/L2 history at all, so recording must start early.
- **Perp extras not yet recorded:**
  - `@markPrice` (index/mark/funding);
  - `@forceOrder` (liquidations; live only, throttled to 1 per second per symbol);
  - BTCUSDT (for lead-lag).
  - The archive has 1-minute `indexPriceKlines` / `markPriceKlines` / `premiumIndexKlines` and `metrics` for coarse history.
- **Fee confirmation:** verify spot VIP0 fees and ETH/U promo status via `GET /api/v3/account/commission?symbol=ETHU` once an account exists.
- **I could not verify:** official spot VIP tier thresholds; current Spot LP program terms; ETH/U promo still active; authors of the liquidation-cascade paper; any Binance-specific high-frequency BTC→ETH lead-lag paper.
