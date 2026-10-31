# Binance ETH: spot vs USDⓈ-M perp, verified market facts

*Researched 2026-10-19 by quant-researcher. Live API values were read around 17:15 UTC that day.*
*Labels: **[API]** = read from Binance's public API that day. **[Docs]** = official Binance docs. **[Approx]** = secondary source or could not be checked on an official page.*

## Key ideas
- **Spot is about 5x worse on fees at the entry tier.** VIP0 spot maker is ~10 bp vs 2 bp on the perp **[Approx]**. A 1-tick round trip on spot loses about **$4.80 per ETH**, vs **$0.96** on the perp.
- **The perp trades ~16x more than spot.** 24h quote volume was $8.18B on the perp vs $0.52B on spot **[API]**.
- **Same tick (0.01) on both, and both sit 1 tick wide.** Spot allows smaller orders: 0.0001 ETH step and 5 USDT minimum, vs 0.001 ETH and 20 USDT on the perp **[API]**.
- **Spot's free history is thinner.** It has trades, aggTrades and klines only. There is **no bookTicker or bookDepth** for spot on data.binance.vision **[API listing]**.
- **Our pipeline is perp-only.** Spot needs a new downloader path, new REST/WebSocket URLs, a *different* depth-sync rule (no `pu`), and **microsecond** archive timestamps from 2025-01-01.
- **Niche finding:** **ETH/U** spot (U is a stablecoin) has had a **0 maker fee for all users since 2026-01-27**, "until further notice". This comes from secondary reports **[Approx]** and its current status is unverified. The book is thin and was quoted ~40 ticks wide **[API]**.

---

## 1. Fees

| | Spot ETHUSDT | USDⓈ-M perp ETHUSDT |
|---|---|---|
| VIP0 maker / taker | **0.10% / 0.10%** (10 / 10 bp) **[Approx]** | **0.02% / 0.05%** (2 / 5 bp) (see learnings doc) |
| BNB discount | **25% off** → 0.075% / 0.075% **[Approx]** | 10% off → 1.8 / 4.5 bp |
| VIP tiers | Fees fall with 30-day spot+margin volume and BNB balance. Example seen: VIP1 0.09% maker. **Exact thresholds not verified** | Similar ladder (not re-checked) |
| Maker rebate programs | Spot Liquidity Provider / "Spot Maker" program. Max rebate **0.8 bp** (June 2025 press release). Sept 2024: Tier 1 needed **≥ $25M weekly maker volume or ≥ 0.05% maker share**, and the top tier's rebate went from -1 bp to -0.8 bp **[Approx]** | USDⓈ-M LP program exists (terms not checked) |
| 0-fee promos | **ETH/U: 0 maker for all users from 2026-01-27** **[Approx, status unverified]**. Other USDC zero-fee promos were for VIP2-9 and LPs only, and not for ETH as far as found. The FDUSD zero-fee promo ended 2024 | None found for ETHUSDT |

I could not load the official fee page (binance.com/en/fee/schedule renders with JavaScript). The 0.10% VIP0 figure matches Binance's own 2022 FAQ example ("regular user 0.1%") and several third-party guides. **The developer should confirm it with the authenticated endpoint `GET /api/v3/account/commission` once an account exists.**

**Rebate programs are out of reach for us.** $25M of maker volume per week is about 10,000 ETH per week.

### 1-tick round trip, 1 ETH, mid ≈ $2,400

| Venue / tier | Fee per side | Round-trip fee | + 1-tick spread | Net |
|---|---|---|---|---|
| Spot VIP0 | 10 bp = $2.40 | -$4.80 | +$0.01 | **-$4.79** |
| Spot VIP0 + BNB | 7.5 bp = $1.80 | -$3.60 | +$0.01 | **-$3.59** |
| Perp VIP0 | 2 bp = $0.48 | -$0.96 | +$0.01 | **-$0.95** |
| Spot ETH/U promo (0 maker) | $0 | $0 | +$0.01 per tick captured | **+spread, minus adverse selection** |

Break-even on spot VIP0 needs ~20 bp ≈ **480 ticks** of captured spread per round trip. Passive touch-quoting on ETHUSDT spot at retail fees is not viable. The only spot cases worth studying are **0-maker-fee pairs**, or spot used purely as a **signal**.

---

## 2. Contract specs and liquidity (snapshot 2026-10-19 ~17:15 UTC) [API]

| | Spot ETHUSDT | Perp ETHUSDT |
|---|---|---|
| Tick | 0.01 USDT (~0.042 bp) | 0.01 USDT |
| Lot step / min qty | 0.0001 / 0.0001 ETH | 0.001 / 0.001 ETH |
| Min notional | 5 USDT (`NOTIONAL` filter) | 20 USDT |
| Max open orders | 200 per symbol (`MAX_NUM_ORDERS`) | not checked |
| Top of book | 2399.04 x 39.9 / 2399.05 x 14.7 | 2401.52 x 213.2 / 2401.53 x 5.4 |
| 24h volume | 214.7k ETH, $515M, 3.65M trades | 3.41M ETH, $8.18B, 4.37M trades |
| Avg trade size | **~0.06 ETH** | **~0.78 ETH** |

- Both books are **1 tick wide**, so both are queue games.
- Spot trades are ~13x smaller on average. My inference: spot flow is more retail-like. That *might* mean less toxic flow per fill, but this is untested.
- Shorting on spot: you sell ETH you hold, or borrow it via Binance Margin (`isMarginTradingAllowed: true`). Borrowing charges hourly interest **[Approx, rate not checked]**. A spot MM therefore needs a **starting ETH inventory**: it must hold ETH to quote the ask.
- Spot order features **[API]**: `LIMIT_MAKER` (post-only), `amendAllowed: true` (change an order without cancel/replace), `pegInstructionsAllowed: true` (pegged orders), `cancelReplaceAllowed`. Default self-trade prevention is `EXPIRE_MAKER`.
- Perp spread vs spot at this snapshot: perp ~2401.5 vs spot ~2399.0, and index 2402.07 vs mark 2401.42. The basis is a few bp and moves. Last funding was 0.0000668 (0.67 bp per 8h) and the next is 2026-10-20 00:00 UTC **[API]**.

### Other spot ETH books (24h, same snapshot) [API]

| Symbol | Tick | 24h volume | Trades | Top of book | Note |
|---|---|---|---|---|---|
| ETHUSDT | 0.01 | $516M | 3.66M | 1 tick | Main book |
| ETHUSDC | 0.01 | $189M | 291k | 1 tick | |
| ETHUSD1 | not checked | $130M | 318k | not checked | |
| ETHU | 0.01 | **$6.4M** | 59k | **2400.70 / 2401.10 (40 ticks ≈ 1.7 bp)**, ~0.003 / 0.42 ETH | **0-maker promo (unverified)**. Thin |
| ETHFDUSD | not checked | $9.6M | 43k | 1 tick, < 1 ETH | |
| ETHBTC | **0.00001 BTC ≈ 3.4 bp** | 4,861 ETH (~$11.7M) | 18.6k | 1 tick, 1 / 61 ETH | Big tick in bp terms |
| U/USDT | 0.0001 (1 bp) | ~$19.6M | 24k | 1.0003 / 1.0004 | Converts U to USDT |

---

## 3. Free data

### data.binance.vision (listing checked 2026-10-19)

| Dataset | Spot | Perp (um) |
|---|---|---|
| trades, aggTrades | **Yes**, ETHUSDT daily up to 2026-10-18 | Yes, up to 2026-10-18 |
| klines | Yes | Yes, plus `indexPriceKlines`, `markPriceKlines`, `premiumIndexKlines` |
| bookTicker (L1) | **No** | Yes, but ends 2024-03-30 |
| bookDepth | **No** | Yes (aggregated bands, not L2) |
| metrics | No | Yes (daily, up to 2026-10-18; open interest and ratio data, format not inspected) |
| fundingRate | n/a | Yes (monthly) |

- **Spot archive timestamps are in microseconds from 2025-01-01** ([binance-public-data README](https://github.com/binance/binance-public-data/)). Earlier files are milliseconds. A loader that assumes ms will be off by 1000x.
- Free spot L1/L2 **history does not exist**. A spot backtest of quotes needs our own recorder, or a paid vendor.

### Live WebSocket, spot ([spot stream docs](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)) [Docs]
- Base URL `wss://stream.binance.com:9443` (or `:443`). Connections last at most 24h. The server pings every 20 s. Each connection may send at most **5 messages per second**, and may carry at most 1024 streams.
- Streams: `<symbol>@depth@100ms` (diffs), `@bookTicker` (real time), `@aggTrade`, `@trade`. Add `timeUnit=MICROSECOND` to the URL to get µs timestamps.
- Snapshot: `GET https://api.binance.com/api/v3/depth?symbol=ETHUSDT&limit=5000`.
- **The sync rule differs from futures.** Spot diffs have **no `pu`** field.
  1. Drop events with `u <= lastUpdateId`.
  2. The first event applied must satisfy `U <= lastUpdateId <= u`.
  3. After that, if an event's `U > book_update_id + 1`, there is a gap: discard the book and resync.
  4. If the snapshot's `lastUpdateId < U` of the first buffered event, refetch the snapshot.
- Our `BookSynchronizer` checks `pu`, so it would need a spot mode.

### Perp extras relevant to the ideas (not in our pipeline yet)
- `<symbol>@markPrice` stream: mark price, index (spot-basket) price, funding rate, next funding time.
- `<symbol>@forceOrder`: liquidation orders, at most **one per symbol per 1000 ms** ([Docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)). It is a sample, not every liquidation. Docs and changelog disagree on whether it shows the "latest" or the "largest" one. I could not find it in the free archive, so it must be recorded live.
- Funding can settle **more often than every 8h** when the rate hits its cap (secondary sources mention dynamic intervals from 2025) **[Approx]**. Read `fundingIntervalHours` from the API rather than hard-coding 8h.

---

## 4. Spot vs perp for market making

| Factor | Spot ETHUSDT | Perp ETHUSDT | Winner for us |
|---|---|---|---|
| Maker fee VIP0 | ~10 bp | 2 bp | **Perp (5x)** |
| Volume / fills | $0.5B/day | $8.2B/day | **Perp** |
| Top-of-book depth | tens of ETH | hundreds of ETH | Perp is deeper; spot has shorter queues |
| Min order | 0.0001 ETH / 5 USDT | 0.001 ETH / 20 USDT | **Spot** (cheaper live tests) |
| Shorting | Need ETH inventory or margin loan | Free | **Perp** |
| Leverage / liquidation | None (cash) | Yes | **Spot** (simpler, no liquidation) |
| Funding | None | Every 8h (or faster at cap) | Spot is simpler; perp adds a signal and a cost |
| Free L1 history | None | To 2024-03-30 | **Perp** |
| Our pipeline | Not built | Built | **Perp** |
| Price discovery | Tends to follow derivatives (papers below) | Tends to lead | Perp leads, so spot quotes are the ones that go stale |
| 0-fee promo pairs | ETH/U (unverified) | None found | **Spot** (niche) |

**Bottom line:**
- For a learner at VIP0, the perp is better on almost every axis: fees, volume, data, and our existing code.
- Spot matters in three cases:
  1. As a **signal**, via the spot index and basis.
  2. As a **hedge leg**.
  3. On **0-maker-fee promo pairs**.
- The owner's earlier decision (perp) and the new "I assume spot" conflict. **This needs an explicit choice.** The tutor should log it.

---

## Sources (all accessed 2026-10-19)
- Binance spot API: `https://api.binance.com/api/v3/exchangeInfo?symbol=ETHUSDT` (also ETHBTC, ETHU), `/api/v3/ticker/bookTicker`, `/api/v3/ticker/24hr` (ETHUSDT, ETHUSDC, ETHFDUSD, ETHBTC, ETHU, ETHUSD1, UUSDT)
- Binance futures API: `https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=ETHUSDT`, `/fapi/v1/ticker/bookTicker`, `/fapi/v1/premiumIndex`
- Archive listings: `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/daily/` and `.../data/futures/um/daily/`
- [binance-public-data README](https://github.com/binance/binance-public-data/) (spot µs timestamps from 2025)
- [Binance spot WebSocket streams](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)
- [Binance USDⓈ-M liquidation order streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams)
- Spot fee (VIP0 0.1%, BNB 25%) **[Approx]**: [Binance FAQ 2022 (example only, marked possibly outdated)](https://www.binance.com/en/support/faq/detail/e85d6e703b874674840122196b89780a), [CryptoPotato](https://cryptopotato.com/binance-fees/)
- Spot LP program **[Approx]**: [Binance press release via AAP, 2025-06-04](https://aapnews.aap.com.au/news/cision20250604AE02912), [The Block, 2025](https://www.theblock.co/post/356887/binance-altcoin-spot-liquidity-program), [Blockchain.news, 2024-08-26](https://blockchain.news/news/binance-updates-spot-liquidity-provider-program)
- Zero-fee promos **[Approx]**: ETH/U 0 maker from 2026-01-27 (secondary coverage, e.g. [bloomingbit](https://en.bloomingbit.io/feed/news/104723)); FDUSD promo end: [The Block](https://theblock.co/post/300748/fdusd-trading-dominance-persists-despite-binance-ending-zero-fee-promotion); USDC VIP2-9 promo: [Binance announcements (Telegram mirror)](https://tx.me/s/binance_announcements?after=8043)
- Dynamic funding interval **[Approx]**: [CoinCub](https://coincub.com/?p=14476)
