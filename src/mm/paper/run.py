"""Paper trader:  ``python -m mm.paper.run --config config/paper/demo_paper.yaml [--duration 60]``

Runs the SAME ``Strategy`` and the SAME simulated exchange / fill model as the backtest, but the
market events come from the live Binance websocket (``mm.data.live.LiveFeed``).

Safety: nothing is ever sent to Binance except public market-data subscriptions and the public REST
depth snapshot / time endpoints that ``LiveFeed`` already uses.  There are no API keys and no
order-entry code anywhere in this module.  "Orders" exist only inside the simulator.

Clock and latency (important for honesty)
    The engine runs on EXCHANGE timestamps (``ev.ts_ns``), never on the PC clock (which can be seconds
    off Binance's). Because of that, the real network delay the data suffered on its way to us is NOT
    part of the engine's world, so the simulated latencies must be added explicitly: paper trading uses
    the same measured defaults as the backtest (feed about 200 ms, order about 150 ms, plus jitter). With
    zero simulated latency the paper strategy would react at the exact exchange instant, which is more
    optimistic than any real setup.

Re-ordering (``reorder.py``)
    Trades and depth arrive on two websockets and can be out of exchange-time order. Events are held for
    ``paper.reorder_hold_ms`` and released sorted by ``(ts_ns, kind priority)`` exactly like replay, so
    the queue model does not count one trade twice.

Gaps
    If the feed drops or the book must be resynchronised (``GapEvent`` from the data layer, or a
    ``StatusEvent`` disconnected / book_gap / error) all simulated resting orders are cancelled, the
    book is cleared, and the strategy cannot place orders until the next book snapshot.

Outputs in ``reports/paper_<UTC timestamp>/``: the same files as a backtest report plus
``pnl_log.csv`` (periodic PnL lines) and ``fills_live.csv`` (every fill as it happened).
Ctrl+C stops cleanly and writes the final summary.
"""
# Lazy type hints (see mm/backtest/strategy.py).
from __future__ import annotations

import argparse
# asyncio: Python's framework for doing many waiting-heavy things (network reads, timers) in ONE
# thread. "async def" defines a coroutine (a function that can pause); "await x" pauses it until x is
# ready, letting other coroutines run meanwhile. asyncio.run(...) starts the event loop.
import asyncio
import csv
import datetime as dt
import logging
import sys
import time
from pathlib import Path

from mm.backtest.config import load_config, ms_to_ns
from mm.backtest.engine import build_engine
from mm.backtest.report import code_manifest, git_info, write_report
from mm.data.config import PROJECT_ROOT, load_config as load_data_config
from mm.data.events import StatusEvent
from mm.data.live import LiveFeed
from mm.paper.reorder import ReorderBuffer

log = logging.getLogger("mm.paper")
# Feed statuses after which our local book / queue estimates may be wrong, so resting orders are cancelled.
_DROP_STATUSES = ("disconnected", "book_gap", "error")


# Which feed does a drop status concern? LiveFeed writes "<endpoint>: <error>" in `detail` for
# disconnects/errors, where endpoint is "public" (book + quotes) or "market" (trades); a "book_gap" is a
# depth-sequence problem ("depth"). The engine treats "market" as a trades-only gap (book kept) and
# everything else as a book gap (wipe and wait for the snapshot); an unparseable detail gives None,
# which is also handled conservatively as a book gap.
def _status_stream(ev) -> str | None:
    if ev.status == "book_gap":
        return "depth"
    head, sep, _ = (ev.detail or "").partition(":")
    return head.strip() if sep and head.strip() in ("public", "market") else None


# Paper trading = real live market data, pretend orders. It exists to check that the strategy behaves
# sensibly on today's market before any real money is risked, and to catch bugs a historical replay
# cannot show. The strategy and fill model are exactly those of the backtest.
# "async def" makes this a coroutine; it is awaited by asyncio.run in main().
# `feed` can be injected (anything with an async-generator ``stream()``); tests use a fake feed.
async def run_paper(cfg: dict, raw_text: str | None, duration_s: float | None = None,
                    out_root: Path | None = None, feed=None) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = (out_root or PROJECT_ROOT / cfg["run"]["out_dir"]) / f"paper_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    # Output files are opened by hand (not with "with") because they stay open for the whole run and
    # are closed in the finally block below. csv.writer formats rows as comma-separated lines.
    fills_f = open(out / "fills_live.csv", "w", newline="", encoding="utf-8")
    pnl_f = open(out / "pnl_log.csv", "w", newline="", encoding="utf-8")
    fills_w, pnl_w = csv.writer(fills_f), csv.writer(pnl_f)
    fills_w.writerow(["exchange_ts_ns", "side", "price", "qty", "liquidity", "mechanism", "fee", "position_after"])
    pnl_w.writerow(["utc", "exchange_ts_ns", "mid", "position", "net_pnl", "spread_capture",
                    "inventory_pnl", "fees", "funding", "fills"])

    # Functions defined inside run_paper ("closures") can use its local variables (eng, live, files).
    # on_fill is a "hook": the engine calls it every time a simulated fill happens.
    def on_fill(f, acct):
        fills_w.writerow([f.ts_ns, f.side, f.price, f.qty, f.liquidity, f.mechanism, f"{f.fee:.6f}",
                          f"{acct.position:.4f}"])
        fills_f.flush()
        print(f"FILL {f.side:4s} {f.qty:g} @ {f.price:.2f} ({f.liquidity}/{f.mechanism}) pos={acct.position:+.3f}", flush=True)

    # Same engine factory as the backtest; LiveFeed connects to Binance's PUBLIC websocket only.
    # `live` is a small dict of mutable bookkeeping shared by the closures below (a closure can mutate
    # a dict even though it cannot rebind an outer variable).
    eng = build_engine(cfg, on_fill_hook=on_fill)
    feed = feed if feed is not None else LiveFeed(cfg["data"]["symbol"])
    live = {"last_ts": None, "last_mono": time.monotonic(), "events": 0, "drops": 0}
    t_start = time.monotonic()
    pcfg = cfg["paper"]
    # The buffer sorts like replay does: kind priority comes from the data layer's replay config
    # (read only). `hold` is how long (real time) an event waits so a slightly late partner can overtake it.
    hold_ns = ms_to_ns(pcfg["reorder_hold_ms"])
    buf = ReorderBuffer(hold_ns, load_data_config()["replay"]["kind_priority"])

    def release(events) -> None:
        """Hand sorted events to the engine and remember the latest exchange time seen."""
        for e in events:
            live["events"] += 1
            live["last_ts"] = max(e.ts_ns, live["last_ts"] or 0)
            live["last_mono"] = time.monotonic()
            eng.on_market_event(e)

    def log_pnl() -> None:
        a = eng.account
        if a.last_mid is None:
            return
        row = [dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), eng.now, f"{a.last_mid:.2f}",
               f"{a.position:.4f}", f"{a.equity - a.start_cash:.4f}", f"{a.spread_capture:.4f}",
               f"{a.inventory_pnl:.4f}", f"{a.fees:.4f}", f"{a.funding:.4f}", len(eng.rec.fills)]
        pnl_w.writerow(row)
        pnl_f.flush()
        print(f"[{row[0]}] mid {row[2]}  pos {row[3]}  net {row[4]}  fees {row[7]}  fills {row[9]}", flush=True)

    # Background task. Every poll interval it (1) releases events whose hold time is over, in sorted
    # order, and (2) once per heartbeat advances the engine clock when the feed is quiet (so timers and
    # order latencies still progress). The clock estimate is the last released exchange timestamp plus
    # the real time elapsed since; it never passes an event still waiting in the buffer. We never use the PC
    # clock directly because it can be seconds off Binance's. (3) It prints a PnL line periodically.
    # "await asyncio.sleep(n)" pauses THIS coroutine for n seconds while others keep running.
    async def pump() -> None:
        poll = pcfg["reorder_poll_ms"] / 1000.0
        last_hb = last_log = time.monotonic()
        while True:
            await asyncio.sleep(poll)
            release(buf.pop_ready(time.monotonic_ns()))
            now = time.monotonic()
            if now - last_hb >= pcfg["heartbeat_s"] and live["last_ts"] is not None:
                last_hb = now
                # Exchange "now" is estimated as the last released exchange time plus the real time since.
                # (An earlier version also added the hold and feed delay, which put the clock ~450 ms
                # AHEAD of the data: the next real event then arrived "in the past" and was clamped.)
                est = live["last_ts"] + int((now - live["last_mono"]) * 1e9)
                oldest = buf.min_ts()
                eng.advance(est if oldest is None else min(est, oldest))
            if now - last_log >= pcfg["log_interval_s"]:
                last_log = now
                log_pnl()

    # Main loop: "async for" is a for-loop over an ASYNC generator: each step awaits the next live
    # event instead of blocking. Status messages (disconnects etc.) are not market data; on a drop we
    # first release everything buffered (older events happened BEFORE the drop), then treat it as a gap:
    # cancel simulated orders and block quoting until the book resynchronises.
    async def consume() -> None:
        async for ev in feed.stream():
            if isinstance(ev, StatusEvent):
                print(f"status: {ev.status} {ev.detail[:80]}", flush=True)
                if ev.status in _DROP_STATUSES:
                    live["drops"] += 1
                    release(buf.pop_all())
                    eng.on_book_lost(live["last_ts"], ev.status, _status_stream(ev))
                continue
            buf.push(ev, time.monotonic_ns())   # real GapEvents are buffered and sorted like any event

    # create_task starts pump() running concurrently in the background and returns a handle so we
    # can cancel it later.
    tick = asyncio.create_task(pump())
    print(f"paper trading {cfg['data']['symbol']} with strategy {cfg['strategy']['name']} "
          f"(Ctrl+C to stop{'' if duration_s is None else f', auto-stop after {duration_s:g}s'})", flush=True)
    # try / except / finally: whatever happens (timeout, Ctrl+C, a crash) the "finally" block runs,
    # so files are closed and the final report is written. Ctrl+C makes asyncio cancel this task,
    # which shows up as CancelledError; we print a note and re-raise it so shutdown continues normally.
    # asyncio.wait_for(x, n) runs x but cancels it after n seconds (used for --duration).
    try:
        if duration_s is None:
            await consume()
        else:
            try:
                await asyncio.wait_for(consume(), duration_s)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        print("\nstopping (Ctrl+C) ...", flush=True)
        raise
    finally:
        # Clean shutdown: stop the background task, process what is still buffered (sorted), log a last
        # PnL line, compute metrics, close files and write the same kind of report as a backtest
        # (marked as a paper run).
        tick.cancel()
        release(buf.pop_all())
        log_pnl()
        metrics, fills, eq = eng.finish()
        fills_f.close()
        pnl_f.close()
        info = {"start": f"live, started {stamp}", "end": "stopped " + dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ"),
                "events": live["events"], "wall_seconds": round(time.monotonic() - t_start, 1),
                "feed_drops": live["drops"]}
        lat = cfg["latency"]
        meta = {"mode": "paper", "command": " ".join(["python -m mm.paper.run", *sys.argv[1:]]), "feed_drops": live["drops"],
                "git": git_info(PROJECT_ROOT), "code": code_manifest(PROJECT_ROOT),
                "notes": ["PAPER RUN: live public data, simulated exchange; no real orders were sent.",
                          f"Engine clock = exchange time; simulated latency feed {lat['feed_latency_ms']} ms, "
                          f"order {lat['order_latency_ms']} ms (+ jitter), events re-ordered with a "
                          f"{pcfg['reorder_hold_ms']} ms hold.",
                          f"Wall time {info['wall_seconds']} s, {live['events']:,} events, "
                          f"{live['drops']} feed drops/resyncs (resting orders are cancelled on each)."]}
        write_report(out, cfg, raw_text, {"paper": {"metrics": metrics, "fills": fills, "equity": eq, "info": info}},
                     {}, meta, "Paper-trading report")
        m = metrics
        print(f"\nFINAL: net pnl {m['pnl']['total_net']:.4f} USDT, fills {m['fills']['count']}, "
              f"position {m['final_position']:+.3f}; report in {out}", flush=True)
    return out


# Command-line entry. asyncio.run(coroutine) creates the event loop, runs run_paper to completion and
# closes the loop. Python turns Ctrl+C into KeyboardInterrupt after the cancellation cleanup has run,
# so we swallow it quietly here.
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (default: until Ctrl+C)")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)
    path = Path(a.config)
    cfg = load_config(path)
    if a.seed is not None:
        cfg["run"]["seed"] = a.seed
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run_paper(cfg, path.read_text(encoding="utf-8-sig"), a.duration))
    except KeyboardInterrupt:
        pass


# Only run main() when executed directly ("python -m mm.paper.run"), not when imported.
if __name__ == "__main__":
    main()
