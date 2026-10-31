"""Live Binance USD-M futures feed: depth@100ms + aggTrade + bookTicker, with a correct local book.

``LiveFeed`` connects to the combined websocket stream, keeps an in-memory order book in sync using
the documented snapshot + diff procedure (see ``book.py``), reconnects with exponential backoff,
and publishes **normalized events** (``mm.data.events``) in two ways:

1. async iterator (for a paper trader)::

       feed = LiveFeed()
       async for ev in feed.stream():           # TradeEvent / QuoteEvent / DepthEvent / StatusEvent
           ...                                  # feed.book is the live OrderBook

2. callbacks (sync functions)::

       feed.subscribe(lambda ev: ...)
       await feed.run()                         # runs until cancelled

``DepthEvent``s are only emitted once they are verified: first a snapshot (``is_snapshot=True``),
then gap-free deltas. After a sequence gap or a reconnect a *new* snapshot DepthEvent is emitted
(preceded by a ``StatusEvent`` with status "book_gap" / "disconnected"); consumers must replace
their book when they see ``is_snapshot=True``.

``recv_ts_ns`` on every event is the local ``time.time_ns()`` taken as soon as the websocket frame
was received. Compare with ``ts_ns`` (exchange time) to measure feed latency + clock offset.

``GapEvent``s (see events.py) are also emitted, with the same semantics as in ``loaders.replay``:
one immediately BEFORE the first snapshot DepthEvent of a connection (reason "session_start"),
a "resync" marker emitted AT DETECTION of a sequence gap (stamped with the exchange time of the
diff that revealed it, i.e. earlier than the repairing snapshot), plus a "disconnect" marker when
a websocket drops. Their ``ts_ns`` is exchange time (the snapshot's aligned ts, or the last exchange
timestamp seen for a disconnect), so live and replay look alike. StatusEvents are still emitted.

``on_raw``: optional ``callable(recv_ts_ns, raw_text)`` called for every websocket frame, used by
the recorder to keep the untouched JSON.

Beginner note on async: this module uses ``asyncio``. An ``async def`` function is a "coroutine":
it can PAUSE at an ``await`` so other work (here: the other websocket connection) runs while this
one waits for the network. Nothing runs in parallel on several CPU cores; the program just
switches between tasks whenever one is waiting. That is ideal for network-bound code.
"""
from __future__ import annotations

import asyncio  # Python's built-in cooperative-multitasking library
import json
import logging
import time
from typing import AsyncIterator, Callable  # type-hint helpers: "an async iterator of ...", "a function taking ..."

import requests  # simple blocking HTTP client (used for the REST snapshot / time calls)
from websockets.asyncio.client import connect  # async websocket client
from websockets.exceptions import ConnectionClosed

from . import normalize as N
from .book import BookSynchronizer
from .config import load_config
from .events import DepthEvent, Event, GapEvent, StatusEvent

# A named logger; the recorder configures how its messages are printed.
log = logging.getLogger("mm.live")


# Our own exception type. Subclassing RuntimeError lets us `except StaleFeed` specifically.
class StaleFeed(RuntimeError):
    """No websocket message for ``live.stale_timeout_s`` seconds."""


class LiveFeed:
    def __init__(self, symbol: str | None = None, cfg: dict | None = None) -> None:
        # `cfg or load_config()`: use the passed config, else read config/data.yaml.
        self.cfg = cfg or load_config()
        self.symbol = (symbol or self.cfg["symbol"]).upper()
        live = self.cfg["live"]
        # The synchronizer owns the local order book and the Binance snapshot+diff state machine.
        self.sync = BookSynchronizer(max_buffer=live["max_sync_buffer"])
        # `Callable[[int, str], None] | None`: either None or a function taking (int, str).
        self.on_raw: Callable[[int, str], None] | None = None
        # Consumers: plain callback functions, and per-iterator asyncio queues (see stream()).
        self._callbacks: list[Callable[[Event], None]] = []
        self._queues: list[asyncio.Queue] = []
        self.counts = {"trade": 0, "quote": 0, "depth": 0, "status": 0, "gap": 0}
        self.reconnects = 0
        # True until the "session_start" marker for this connection has been emitted with its first
        # snapshot. After a detected sequence gap the "resync" marker is emitted AT DETECTION
        # instead, so the later repairing snapshot needs no marker of its own.
        self._snapshot_needs_marker = True
        # Newest exchange timestamp seen on any stream: used to stamp "disconnect" gap markers in
        # exchange time (we have no exchange clock when a socket dies).
        self._last_ts_ns: int | None = None
        # Latest measured (local clock - exchange clock) in ms; 0.0 until measured.
        self.clock_offset_ms = 0.0

    # ------------------------------------------------------------------ public API
    # `@property` lets callers write `feed.book` (no parentheses) as if it were an attribute,
    # while it is really computed by this method.
    @property
    def book(self):
        """The live ``OrderBook`` (valid only while ``sync.state`` is SYNCED)."""
        return self.sync.book

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._callbacks.append(callback)

    def url(self, endpoint: str) -> str:
        """Combined-stream URL for one endpoint group ("public" or "market") from the config."""
        ep = self.cfg["live"]["endpoints"][endpoint]
        sym = self.symbol.lower()  # Binance stream names are lower-case, e.g. "ethusdt@aggTrade"
        # f-strings: f"...{expr}..." inserts the value of expr. The generator expression inside
        # join(...) builds "ethusdt@depth@100ms", "ethusdt@bookTicker", ... joined with "/".
        return f"{ep['url']}?streams=" + "/".join(f"{sym}@{n}" for n in ep["streams"])

    # `async def` + `yield` = an ASYNC GENERATOR. The caller consumes it with `async for ev in
    # feed.stream():`; each `yield` hands one event to the caller and then pauses here until the
    # caller asks for the next one.
    async def stream(self) -> AsyncIterator[Event]:
        """Async iterator of events. Starts the connection on first iteration; stops on close."""
        # An asyncio.Queue is a thread-safe-ish mailbox: _emit() puts events in, we take them out.
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        # create_task schedules run() to execute "in the background" on the event loop.
        task = asyncio.create_task(self.run())
        # When run() finishes (error or cancel) drop the task itself into the queue as a signal,
        # so the loop below wakes up instead of waiting forever.
        task.add_done_callback(lambda t: q.put_nowait(t))  # lambda = tiny anonymous function
        try:
            while True:
                item = await q.get()  # pause here until something is in the queue
                if isinstance(item, asyncio.Task):  # run() ended
                    if not item.cancelled() and item.exception():
                        raise item.exception()
                    return
                yield item
        finally:
            # `finally` runs however the loop ends (consumer stopped early, error, Ctrl+C):
            # stop the background task and unregister our queue.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._queues.remove(q)

    async def run(self) -> None:
        """Run every endpoint connection (each reconnects independently) until cancelled."""
        # `*( ... for n in ...)` unpacks the generator into separate arguments of gather().
        # gather runs all the coroutines concurrently: one connection for "public", one for "market".
        await asyncio.gather(*(self._run_endpoint(n) for n in self.cfg["live"]["endpoints"]))

    async def _run_endpoint(self, endpoint: str) -> None:
        # Reconnect loop for ONE websocket connection. Websockets drop for many reasons (network
        # blip, Binance's 24h connection limit), so we simply reconnect forever.
        live = self.cfg["live"]
        backoff = live["reconnect_min_s"]
        while True:
            started = time.monotonic()  # monotonic clock: unaffected by system clock changes
            try:
                await self._session(endpoint)
            except asyncio.CancelledError:
                # Cancellation = the user pressed Ctrl+C / shut us down. Never swallow it.
                raise
            except (ConnectionClosed, OSError, StaleFeed, asyncio.TimeoutError) as exc:
                # Expected network trouble: record it and fall through to the reconnect.
                self._status("disconnected", f"{endpoint}: {type(exc).__name__}: {exc}")
                self._emit_disconnect(endpoint)
            except Exception as exc:  # unexpected: log, keep the recorder alive
                log.exception("unexpected feed error")
                self._status("error", f"{endpoint}: {type(exc).__name__}: {exc}")
                self._emit_disconnect(endpoint)
            self.reconnects += 1
            if time.monotonic() - started > live["backoff_reset_s"]:  # it was healthy for a while: reset backoff
                backoff = live["reconnect_min_s"]
            await asyncio.sleep(backoff)  # wait without blocking other tasks
            # Exponential backoff: 1s, 2s, 4s ... capped, so we don't hammer the server.
            backoff = min(backoff * 2, live["reconnect_max_s"])

    # ------------------------------------------------------------------ internals
    def _emit(self, ev: Event | StatusEvent) -> None:
        """Deliver one event to every subscriber (callbacks and async-iterator queues)."""
        self.counts[ev.kind] += 1
        # Track the newest exchange time for market events (StatusEvent.ts_ns is the local clock,
        # so it is excluded).
        if ev.kind not in ("status", "gap") and (self._last_ts_ns is None or ev.ts_ns > self._last_ts_ns):
            self._last_ts_ns = ev.ts_ns
        for cb in self._callbacks:
            cb(ev)
        for q in self._queues:
            q.put_nowait(ev)  # put without waiting (the queue is unbounded)

    def _emit_disconnect(self, endpoint: str) -> None:
        """Publish a "disconnect" GapEvent stamped with the last exchange time we saw."""
        # If nothing was ever received, estimate exchange time from the local clock minus offset.
        ts = self._last_ts_ns
        if ts is None:
            ts = time.time_ns() - int(self.clock_offset_ms * 1_000_000)
        # +1 ns: at an equal timestamp a gap sorts BEFORE data, but a disconnect happened AFTER the
        # last data we saw, so nudge it just past that data.
        self._emit(GapEvent(ts_ns=ts + 1, reason="disconnect", stream=endpoint))

    def _status(self, status: str, detail: str = "") -> None:
        """Publish a housekeeping StatusEvent (connected, gap, ...), stamped with the local clock."""
        now = time.time_ns()
        log.info("status %s %s", status, detail)
        self._emit(StatusEvent(ts_ns=now, recv_ts_ns=now, status=status, detail=detail))

    def handle_message(self, raw: str | bytes, recv_ts_ns: int) -> None:
        """Parse one websocket frame, update the book, emit normalized events. Sync + testable."""
        # Keep the untouched text first (the recorder stores it as our "raw" copy).
        if self.on_raw is not None:
            self.on_raw(recv_ts_ns, raw if isinstance(raw, str) else raw.decode("utf-8"))
        msg = json.loads(raw)
        # Combined streams wrap the payload as {"stream": ..., "data": {...}}; unwrap if present.
        data = msg.get("data", msg)
        etype = data.get("e")  # "e" = event type, tells us which kind of message this is
        if etype == "aggTrade":
            self._emit(N.parse_agg_trade(data, recv_ts_ns))
        elif etype == "bookTicker":
            self._emit(N.parse_book_ticker(data, recv_ts_ns))
        elif etype == "depthUpdate":
            # Depth diffs must pass through the synchronizer, which checks the U/u/pu sequence
            # and only returns events that are safe to use. A rise in `gaps` means the sequence
            # broke and a new snapshot will be requested.
            gaps_before = self.sync.gaps
            diff = N.parse_depth_diff(data, recv_ts_ns)
            self._emit_depth(self.sync.on_diff(diff))
            if self.sync.gaps != gaps_before:
                self._status("book_gap", f"sequence gap near u={data.get('u')}; resyncing")
                # Emit the "resync" marker NOW, stamped with the exchange time of the diff that
                # revealed the gap (detection time), not when the repairing snapshot finally
                # arrives 0.2-1 s later. Consumers must stop trusting their book from this
                # moment; replay of the recording reproduces exactly this timing.
                self._emit(GapEvent(ts_ns=diff.ts_ns, reason="resync", stream="depth"))
                self._snapshot_needs_marker = False  # the marker above already covers the snapshot

    def _clock_offset_blocking(self) -> float:
        """Local-minus-exchange clock offset in ms (positive = our clock is ahead), using
        the midpoint of a REST /time round trip. Subtract it from recv_ts_ns - ts_ns to get
        true feed latency."""
        # "blocking" = a normal synchronous function that waits on the network. We run such
        # functions in a worker thread (asyncio.to_thread) so they don't freeze the event loop.
        r = self.cfg["rest"]
        t0 = time.time_ns()
        srv = requests.get(r["base_url"] + r["time_path"], timeout=r["timeout_s"]).json()["serverTime"]
        t1 = time.time_ns()
        # The server stamped its time somewhere between t0 and t1; assume the midpoint.
        off = ((t0 + t1) / 2 - srv * 1_000_000) / 1e6
        self.clock_offset_ms = off
        return off

    def _emit_depth(self, events: list[DepthEvent]) -> None:
        for ev in events:
            if ev.is_snapshot:
                # A snapshot being emitted means the book just (re)synchronized. For a new session
                # the "session_start" GapEvent goes first and shares the snapshot's ts_ns, so
                # replay orders them identically. After a sequence gap the "resync" marker was
                # already emitted at detection time (see handle_message), so nothing is added.
                if self._snapshot_needs_marker:
                    self._emit(GapEvent(ts_ns=ev.ts_ns, reason="session_start", stream="depth"))
                self._snapshot_needs_marker = False
                self._status("book_synced", f"lastUpdateId={ev.final_update_id}")
            self._emit(ev)

    def _fetch_snapshot_blocking(self) -> DepthEvent:
        """REST call for a full order-book snapshot (blocking; run it via asyncio.to_thread)."""
        r = self.cfg["rest"]
        resp = requests.get(r["base_url"] + r["depth_path"], timeout=r["timeout_s"],
                            params={"symbol": self.symbol, "limit": r["depth_limit"]})
        recv = time.time_ns()
        resp.raise_for_status()  # turn HTTP errors (e.g. 429 rate limit) into exceptions
        return N.parse_depth_snapshot(resp.json(), recv)

    async def _check_clock(self) -> None:
        """Measure and log the clock offset (informational: a failure must not drop the feed)."""
        try:
            # to_thread runs the blocking requests call in a worker thread.
            off = await asyncio.to_thread(self._clock_offset_blocking)
            self._status("clock_offset_ms", f"{off:.1f}")  # :.1f = 1 decimal place
        except Exception as exc:
            self._status("clock_offset_failed", repr(exc))

    def _on_snapshot_done(self, task: asyncio.Task) -> None:
        """Hand a finished REST snapshot request to the synchronizer and publish what it releases."""
        if task.exception() is not None:
            self._status("snapshot_error", repr(task.exception()))
            return
        stale_before = self.sync.stale_snapshots
        events = self.sync.on_snapshot(task.result())
        self._emit_depth(events)
        if not events and self.sync.stale_snapshots != stale_before:
            self._status("snapshot_stale", "snapshot older than stream; refetching")

    async def _session(self, endpoint: str) -> None:
        """One websocket connection from connect until it fails (the caller reconnects)."""
        live = self.cfg["live"]
        # Only the connection that carries depth streams owns the order book.
        needs_book = any(x.startswith("depth") for x in live["endpoints"][endpoint]["streams"])
        if needs_book:
            self.sync.reset()  # a new connection means a fresh book: old state may have gaps
            self._snapshot_needs_marker = True  # a fresh connection starts a new session
        url = self.url(endpoint)
        snap_task: asyncio.Task | None = None  # the in-flight REST snapshot request, if any
        next_clock_check = time.monotonic() + live["clock_check_interval_s"]
        next_snap_at = 0.0  # don't request snapshots more often than snapshot_retry_s
        # `async with` = context manager for async resources: opens the websocket and guarantees
        # it is closed at the end of the block.
        async with connect(url, max_size=None, open_timeout=live["open_timeout_s"],
                                 ping_interval=live["ping_interval_s"]) as ws:
            self._status("connected", url)
            if needs_book:
                await self._check_clock()
            try:
                while True:
                    # Binance's procedure: the stream is already open and buffering diffs (inside
                    # the synchronizer) while we ask REST for a snapshot; (re)start a snapshot
                    # request whenever the book needs one
                    if needs_book and self.sync.needs_snapshot and snap_task is None and time.monotonic() >= next_snap_at:
                        snap_task = asyncio.create_task(asyncio.to_thread(self._fetch_snapshot_blocking))
                        next_snap_at = time.monotonic() + live["snapshot_retry_s"]
                    # The PC clock drifts against the exchange, so re-measure the offset regularly
                    # (each result is logged as a "clock_offset_ms" status; the latency tool uses
                    # them). The REST call runs in a worker thread so the loop is not frozen.
                    if needs_book and time.monotonic() >= next_clock_check:
                        next_clock_check = time.monotonic() + live["clock_check_interval_s"]
                        await self._check_clock()
                    # When the snapshot request has finished, hand its result to the synchronizer.
                    if snap_task is not None and snap_task.done():
                        t, snap_task = snap_task, None  # tuple swap: take the task, clear the slot
                        self._on_snapshot_done(t)
                    try:
                        # wait_for adds a timeout to the await: if no frame arrives for
                        # stale_timeout_s the connection is probably dead, so we reconnect.
                        raw = await asyncio.wait_for(ws.recv(), live["stale_timeout_s"])
                    except asyncio.TimeoutError:
                        raise StaleFeed(f"no message for {live['stale_timeout_s']}s") from None
                    # Stamp local receive time immediately: this is recv_ts_ns.
                    self.handle_message(raw, time.time_ns())
            finally:
                if snap_task is not None:
                    snap_task.cancel()
