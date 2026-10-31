# Venues, instruments and choosing a coin

*Written 2026-10-19. Fees and rules checked that day (sources at end) or marked approximate. Code citations synced with 4b1640d on 2026-10-23.*

## Key ideas
- A venue's **maker fee** is the single biggest number in market-making economics.
- Perps have no expiry. **Funding** payments between longs and shorts keep them near spot.
- Perps add leverage, margin and liquidation. Liquidation uses **mark price**, not last trade.
- ETHUSDT is usually **1 tick wide**. Competition is about queue position and speed, not price.
- At the entry fee tier, a 1-tick round trip **loses about $0.95 per ETH** in fees.

---

## 1. What a venue is

```
 CEX (Binance, Bybit, OKX, Coinbase)     DEX with on-chain CLOB (Hyperliquid)
 company holds your money                you hold funds in your wallet
 fast, deep, familiar API                public on-chain activity
 risk: custody / counterparty            risk: contracts, bridge, validators, regulation
```
- AMM DEXs (e.g. Uniswap) have no order book. Market making there means pool liquidity, a different problem.

| Concept | What it is | Why you care |
|---|---|---|
| Matching engine | Matches orders by price, then time | Time priority creates **queue position** |
| Order book (CLOB) | Resting limit orders per price | What we quote into; local copy in [`book.py`](../../src/mm/data/book.py) `OrderBook` |
| Maker / taker | Maker rests and adds liquidity. Taker crosses the spread | MMs are makers, so the maker fee matters most |
| Fee tiers (VIP) | Fees fall with 30-day volume | A losing strategy at VIP0 can win at a high tier |
| Rebate | Negative maker fee | Usually top tiers or MM programs only |
| Regulation | Who may be served, where | Decides if you can open an account |
| API / co-location | REST, WebSocket, server next to engine | Latency decides if you can cancel a stale quote in time |

**The order book ladder (ETHUSDT snapshot, 2026-10-19):**
```
        price      size (ETH)
 ASK   2395.47        ...
 ASK   2395.46        ...
 ASK   2395.45      192.7   <- best ask
        ------------------  spread = 0.01 = 1 tick
 BID   2395.44      156.3   <- best bid
 BID   2395.43        ...
```

**Options shown (fees approximate, 2026-10-19). 1% = 100 bp, so 0.02% = 2 bp.**

| Venue | Product | Entry maker / taker | Notes |
|---|---|---|---|
| Binance | USDⓈ-M futures | **0.02% / 0.05%**, 10% off paying in BNB | Deepest. Free history. Not for US persons (Binance.US is a separate company without these futures) |
| Bybit / OKX | Perps | Similar magnitude (not re-checked) | Deep perps, weaker free history |
| Coinbase Advanced | Spot | ~**0.40% / 0.60%** lowest tier; tiers being restructured | US-regulated; maker ~20x Binance |
| Hyperliquid | Perps | **0.015% / 0.045%**; 0 then small rebates at high volume | Low fees, self-custody, newer risks |

**Remember: compare venues by maker fee in bp first.**

---

## 2. Spot vs perpetual futures

| | Spot | USDⓈ-M perp |
|---|---|---|
| You hold | Real ETH | A contract tracking ETH, settled in USDT |
| Shorting | Must own or borrow ETH | Free |
| Expiry | n/a | None |
| Extra mechanics | None (no leverage, no liquidation) | Funding, margin, liquidation, mark price |
| Binance ETHUSDT maker fee, VIP0 | **~10 bp** (approx) | **2 bp** |
| 1-tick round trip, 1 ETH at ~$2,400 | **-$4.79** | **-$0.95** |
| 24h volume (2026-10-19) | ~$0.52B | ~$8.18B (**~16x spot**) |
| Min order | 0.0001 ETH / 5 USDT | 0.001 ETH / 20 USDT |
| Free history (data.binance.vision) | Trades, aggTrades, klines only | Also bookTicker (to 2024-03-30), bookDepth, funding |
| Price discovery | Tends to follow | Tends to lead |

*Binance numbers: [spot vs perp facts](../../research/binance-eth-spot-vs-perp-facts.md) (fees marked approximate there).*

- USDⓈ-M = USD-stablecoin margined. Collateral and PnL in USDT, size in ETH.
- That matches our units ([`schemas.py`](../../src/mm/data/schemas.py) module docstring).

### Funding
- Paid every 8h for ETHUSDT: 00:00, 08:00, 16:00 UTC (API, 2026-10-19).
- Traders pay each other. The exchange keeps nothing.
- Payment = size x mark price x rate.

```
 rate > 0 (perp above spot)            rate < 0 (perp below spot)
   LONGS ---- pay ----> SHORTS           SHORTS ---- pay ----> LONGS
   pushes perp down toward spot          pushes perp up toward spot
```

- Formula (secondary source, simplified): `funding = premium + clamp(interest - premium, -0.05%, +0.05%)`, `interest = 0.01%/8h`.
- Capped per symbol. Calm markets sit near **+0.01% per 8h**.

**Real example: 2026-10-19 08:00 UTC, rate 0.00009885 (~0.99 bp), mark 2429.46.**
- Short 2 ETH: **receive** 2 x 2429.46 x 0.00009885 = **$0.48**.
- Long 2 ETH: **pay $0.48**.
- Held all year: 0.00009885 x 3 x 365 ≈ **10.8%/yr** of notional.

- A flat MM barely pays funding.
- Inventory held across a funding time is a real cost or income. It can justify skewing quotes before funding.
- That is why funding is in our data ([`data.yaml`](../../config/data.yaml) `archive.default_datasets`, `FundingEvent` in [`events.py`](../../src/mm/data/events.py)).

**Remember: positive funding means longs pay shorts.**

### Leverage, margin, liquidation
- Initial margin: collateral to open. 10x on 1 ETH at $2,400 = **$240**.
- Maintenance margin: minimum to keep it. Rises with size ("leverage brackets").
- Liquidation: equity < maintenance, so the exchange closes you at a bad price.

*Simplified (approximate, ignores fees and exact formula):* long 1 ETH at $2,400, $240 margin, ~0.5% maintenance.
`240 + (P - 2400) = 0.005 P  ->  P ≈ $2,171` (a ~9.5% drop). ETH has done that in hours.

**Remember: keep leverage low enough that a gap move cannot liquidate inventory.**

### Mark vs last price, and basis
- Last price: latest trade. Our fills happen here.
- Mark price: smoothed fair value from a multi-exchange spot index plus basis. Used for unrealised PnL and **liquidation**, so one manipulative print cannot liquidate people.
- Backtests should value inventory at mid or mark, not last trade.
- **Basis = perp price - spot index.** Small thanks to funding, wider in fast markets. Matters if we hedge with spot.

### Why crypto MM lives on perps
1. Volume: perps usually several times spot on major CEXs (approximate, varies).
2. Lower maker fees at the same tier.
3. Free shorting, so both sides are symmetric.
4. Margin, so less cash per unit of notional.
- Costs: funding, liquidation risk, more moving parts.

---

## 3. Choosing a coin

ETHUSDT perp (exchangeInfo, 2026-10-19): **tick 0.01**, lot 0.001 ETH, min notional 20 USDT.

- Tick in bp: `0.01 / 2395.45 ≈ 0.042 bp`.
- Tiny tick vs fair spread means the book sits **1 tick wide** (2395.44 / 2395.45, ~156 / 193 ETH at the top).
- You cannot improve on 1 tick. You compete on **queue position and speed**.
- BTCUSDT plays the same game, even harder.

### 1-tick spread vs fees (1 ETH each side, one round trip)

| | Per ETH |
|---|---|
| Spread captured (1 tick) | **+$0.01** |
| Maker fee VIP0: 2 bp x $2,395 x 2 | **-$0.96** |
| With BNB discount (1.8 bp) | -$0.86 |
| **Net at VIP0** | **≈ -$0.95** |

- Break-even on fees alone: ~4 bp ≈ **96 ticks** of captured spread, before adverse selection.
- Coinbase (~40 bp maker): ~ **-$19** per round trip.
- Hyperliquid base (1.5 bp): **-$0.72**.
- Hypothetical **-0.5 bp rebate** (illustrative, not a Binance rate): **+$0.24** plus $0.01 spread.

What this means for us:
- Touch-quoting at retail tiers loses money by construction.
- Edge must come from elsewhere: quote wider in high volatility, a short-term signal, or simply measuring how far from break-even we are.
- The spec must state its fee assumption.
- ETHUSDT is still a good learning market.

**Remember: at retail fees, fee tier matters more than spread.**
 
### Other coin traits

| Trait | Tells you |
|---|---|
| Spread in ticks | Queue game (1 tick) or pricing game (many) |
| Depth | Queue length; how far a big taker moves price |
| Volatility | Inventory risk. Avellaneda-Stoikov: half-spread grows with σ and γ; reservation price shifts by ~q·γ·σ²·(T - t) |

- **Adverse selection**: informed takers hit you just before price moves against you. Such flow is **toxic**.
- BTC/ETH: mixed flow; some informed flow from fast traders watching other venues.
- Mid-cap alts (e.g. SOL): wider bp spreads, but more jump risk (listings, news, liquidation cascades) and thinner books.
- **BTC often moves first** (ms to seconds). MMs use BTC and other venues as a lead signal to pull or skew quotes. Future feature for us; needs BTC data.

---

## 4. How professional firms choose
Varies a lot by firm. Common patterns, not rules.

- **Volume and fees first.** MM programs give low or negative maker fees for obligations (max spread, min size, min uptime). Terms are private.
- **Many venues and symbols.** Diversifies risk. Inventory from one venue is hedged on another.
- **Latency and co-location.** Servers near the engine. Binance is widely reported (not officially) to run in AWS Tokyo. Some venues sell co-location, some do not.
- **Counterparty risk: the FTX lesson.** FTX, a top-3 venue, collapsed in Nov 2022 after misuse of customer funds; MMs lost access to their money. Firms now cap per-venue balances, sweep profits often, use off-exchange settlement where possible. Binance itself had a 2023 US settlement with regulators and the DOJ.
- **Legal entity and licensing.** UK's FCA banned retail crypto derivatives in 2021. US persons cannot use Binance.com. Firms set up suitable entities; individuals usually cannot.
- **Product.** Perps dominate. Spot is made too, often hedged with perps.
- **Coin.** Large caps for steady volume; alts for wider spreads with stricter risk limits.

**Remember: we lack MM fees, co-location and multi-venue hedging. A backtest assuming them would mislead us.**

---

## 5. Decisions log
- **2026-10-19. Venue: Binance.** Over Bybit/OKX, Coinbase Advanced, Hyperliquid. Why: deepest liquidity, best free history (data.binance.vision), free WebSockets. Accepted: not for US persons and some others (check your jurisdiction before going live), CEX custody risk, VIP0 fees make touch-quoting fee-negative.
- **2026-10-19. Market type: USDⓈ-M perpetual futures** (not spot). Why: deeper books, lower maker fees, free shorting. Accepted: must model funding, leverage, margin, liquidation, mark vs last price.
- **2026-10-19. Pair: ETHUSDT.** Over BTCUSDT (harder queue game) and alts like SOL (more adverse selection, jump risk). Verified: tick 0.01, lot 0.001, min notional 20, funding every 8h. Open issue for researcher: 1-tick round trip at VIP0 loses ~$0.95/ETH; spec must state fee assumption and edge source.
- **2026-10-19. Reconfirmed: perp ETHUSDT, not spot ETH/USDT.** The owner briefly assumed spot, then chose the perp again "because most market makers trade perp". Verified numbers ([facts](../../research/binance-eth-spot-vs-perp-facts.md)): VIP0 maker ~10 bp spot vs 2 bp perp (approx); 1-tick round trip -$4.80/ETH vs -$0.96; spot volume ~1/16 of perp; free spot history is trades only, so no quote backtest. Spot would have given no leverage or liquidation and smaller min orders (5 vs 20 USDT). Accepted: perp risks stay. Spot stays useful as a **lagging market** to watch (perp leads, spot follows) and a possible **hedge leg**, as firms do.
- **2026-10-23. Sync with code 4b1640d (no owner decision).** Line-number citations replaced by class/config-key names (`OrderBook`, `FundingEvent`, `archive.default_datasets`). No content changed.

---

## Check your understanding
1. Funding is +0.03% and you are **short 5 ETH** at mark $2,500. Pay or receive, and how much? Why care if you are usually flat?
2. ETHUSDT is 1 tick wide at $2,400 and maker is 2 bp. Roughly how many ticks of captured spread cover a round trip's fees? Where must a retail strategy's edge come from?
3. Why liquidate on **mark price** rather than last trade? What should our backtest use to value inventory?

*Answers on request.*

---

## Sources (checked 2026-10-19)
- Binance fees, BNB discount: [Binance support](https://www.binance.com/fr/support/articles/360033544231), [Finder](https://www.finder.com/cryptocurrency/trading/binance-futures-fees)
- Binance spot vs perp facts (fees, volume, filters, archive listing): [research note](../../research/binance-eth-spot-vs-perp-facts.md)
- Live API: `fapi/v1/exchangeInfo` (filters), `fapi/v1/fundingRate?symbol=ETHUSDT`, `fapi/v1/ticker/bookTicker?symbol=ETHUSDT`
- Funding formula (secondary): [TradingView notes](https://kr.tradingview.com/script/9oo2FQD5-Binance-Funding-Rate-Inverted)
- Coinbase: [NerdWallet](https://www.nerdwallet.com/investing/reviews/coinbase), [Coinbase Advanced](https://www.coinbase.com/en-gb/advanced-markets)
- Hyperliquid: [docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees), [Dwellir](https://www.dwellir.com/blog/hyperliquid-fees-explained)
