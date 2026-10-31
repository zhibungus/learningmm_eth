"""Local L2 order book and Binance's snapshot + diff synchronisation procedure.

``OrderBook``          a plain price -> qty book for both sides.
``BookSynchronizer``   a pure state machine (no network, no clock) that turns raw diff messages and
                       a REST snapshot into a correct, gap-free stream of ``DepthEvent``s. It is
                       what the live recorder uses, and it is unit-tested with synthetic messages.

Binance USD-M futures procedure (https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly):

 1. Open the diff stream and buffer events.
 2. GET /fapi/v1/depth?limit=1000 -> ``lastUpdateId`` (call it L).
 3. Drop buffered events with ``u < L``.
 4. The first event that is used must satisfy ``U <= L <= u``. If the first remaining event has
    ``U > L`` the snapshot is too old -> fetch a new one.
 5. Each following event must have ``pu`` equal to the previous event's ``u``. Otherwise there is
    a gap -> throw the book away and start again from step 2.
 6. Quantity 0 means remove the price level; any other qty is the new absolute size.

Why all this? The websocket only sends CHANGES (diffs), and the full book only comes from a REST
call that takes time. Messages keep flowing during that call, so we must line the two up exactly:
every diff carries an id range (``U`` = first update id, ``u`` = last update id) and ``pu`` (the
previous message's ``u``). The snapshot carries ``lastUpdateId`` = L, "the book as of update L".
 * ``u < L``: that diff is already contained in the snapshot, so applying it would be wrong.
 * ``U <= L <= u``: this diff straddles the snapshot point, so nothing between the snapshot and
   the stream is missing. This is the proof the two sources join without a hole.
 * ``pu == previous u``: a chain check. If one websocket message is lost, ``pu`` no longer
   matches and we know our book is silently wrong, so we rebuild it from a fresh snapshot.
A wrong book is dangerous for market making: you would quote against liquidity that is not there.
"""
# Postpones type-hint evaluation (lets us write `float | None`).
from __future__ import annotations

import enum  # Enum = a fixed set of named constants (used for the sync states)
# dataclasses.replace(obj, field=new) returns a COPY of a frozen dataclass with some fields changed
# (frozen objects cannot be modified in place).
from dataclasses import replace

from .events import DepthEvent


class OrderBook:
    """Price -> quantity maps for bids and asks with cached best prices.

    BIDS are resting buy orders (the best bid is the HIGHEST price); ASKS are resting sell orders
    (the best ask is the LOWEST price). Each dict maps price -> total size resting at that price.
    """

    def __init__(self) -> None:
        # `dict[float, float]` is a type hint: keys are prices, values are quantities.
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        # Cache the best prices so reading the top of book is O(1) instead of scanning the dict.
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self._best_bid = self._best_ask = None  # chained assignment: both set to None

    def apply(self, ev: DepthEvent) -> None:
        """Apply a snapshot (replaces the book) or a delta (qty 0 deletes a level)."""
        # A snapshot is the whole truth, so throw the old book away first.
        if ev.is_snapshot:
            self.clear()
        for px, q in ev.bids:  # `px, q` unpacks each (price, qty) pair
            self._set(self.bids, px, q, is_bid=True)
        for px, q in ev.asks:
            self._set(self.asks, px, q, is_bid=False)

    def _set(self, side: dict[float, float], px: float, q: float, is_bid: bool) -> None:
        """Set or delete one price level and keep the cached best price up to date."""
        if q > 0:
            # New absolute size at this price (NOT an increment).
            side[px] = q
            if is_bid:
                if self._best_bid is None or px > self._best_bid:
                    self._best_bid = px
            elif self._best_ask is None or px < self._best_ask:
                self._best_ask = px
        else:
            # qty 0 means the level is gone. pop(px, None) deletes it and doesn't fail if absent.
            side.pop(px, None)
            # If we just removed the best level, the new best must be found by scanning:
            # max(side) = highest remaining bid, min(side) = lowest remaining ask.
            if is_bid and px == self._best_bid:
                self._best_bid = max(side) if side else None
            elif not is_bid and px == self._best_ask:
                self._best_ask = min(side) if side else None

    # `@property` makes best_bid readable as an attribute (book.best_bid) though it is computed.
    @property
    def best_bid(self) -> tuple[float, float] | None:
        return None if self._best_bid is None else (self._best_bid, self.bids[self._best_bid])

    @property
    def best_ask(self) -> tuple[float, float] | None:
        return None if self._best_ask is None else (self._best_ask, self.asks[self._best_ask])

    def is_crossed(self) -> bool:
        """best bid strictly above best ask (should never happen in a correct book)."""
        return (self._best_bid is not None and self._best_ask is not None
                and self._best_bid > self._best_ask)

    def is_locked(self) -> bool:
        """best bid equals best ask (zero spread; rare and brief on a healthy book)."""
        return (self._best_bid is not None and self._best_ask is not None
                and self._best_bid == self._best_ask)

    def top(self, n: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Best ``n`` levels per side: (bids high->low, asks low->high)."""
        bids = sorted(self.bids.items(), reverse=True)[:n]  # [:n] = slice: first n items
        asks = sorted(self.asks.items())[:n]
        return bids, asks

    def mid(self) -> float | None:
        """Mid price = average of best bid and best ask (a common fair-value proxy)."""
        if self._best_bid is None or self._best_ask is None:
            return None
        return 0.5 * (self._best_bid + self._best_ask)


class SyncState(enum.Enum):
    """Where the synchronizer is in Binance's procedure."""
    WAIT_SNAPSHOT = "wait_snapshot"  # buffering diffs; a snapshot is needed
    SYNCING = "syncing"              # have a snapshot; waiting for the first valid diff
    SYNCED = "synced"                # book is live and sequence-checked


class BookSynchronizer:
    """Feeds ``DepthEvent`` diffs and snapshots in, gets verified ``DepthEvent``s out.

    Usage::

        sync = BookSynchronizer()
        out = sync.on_diff(diff)            # buffered while waiting -> []
        if sync.needs_snapshot: ...fetch REST, build a snapshot DepthEvent...
        out = sync.on_snapshot(snapshot)    # returns [snapshot, first diff, ...] once valid

    The emitted snapshot has its ``ts_ns`` set to the ``ts_ns`` of the first applied diff, so that
    sorting by ``ts_ns`` (stable) always places the snapshot before the diffs that follow it.

    Counters (``gaps``, ``stale_snapshots``, ``resyncs``) are exposed for monitoring.
    """

    def __init__(self, max_buffer: int = 20000) -> None:
        self.book = OrderBook()
        self.state = SyncState.WAIT_SNAPSHOT
        self.max_buffer = max_buffer  # cap on buffered diffs so memory can't grow without bound
        self._buf: list[DepthEvent] = []              # diffs received while not yet synced
        self._snapshot: DepthEvent | None = None      # the snapshot we are trying to join to
        self._last_u: int | None = None               # `u` of the last applied diff (chain check)
        self.gaps = 0
        self.stale_snapshots = 0
        self.resyncs = 0  # number of times we (re)entered WAIT_SNAPSHOT after being synced

    # ------------------------------------------------------------------ properties
    @property
    def needs_snapshot(self) -> bool:
        return self.state is SyncState.WAIT_SNAPSHOT

    @property
    def last_update_id(self) -> int | None:
        return self._last_u

    def reset(self) -> None:
        """Forget everything (call after a websocket reconnect)."""
        self.book.clear()
        self.state = SyncState.WAIT_SNAPSHOT
        self._buf.clear()
        self._snapshot = None
        self._last_u = None

    # ------------------------------------------------------------------ inputs
    def on_diff(self, d: DepthEvent) -> list[DepthEvent]:
        """Handle one diff message. Returns the events that are now safe to publish."""
        if self.state is SyncState.WAIT_SNAPSHOT:
            # No snapshot yet: just remember the diff for later (step 1).
            self._buffer(d)
            return []
        if self.state is SyncState.SYNCING:
            # We have a snapshot but haven't found the diff that joins to it yet.
            self._buffer(d)
            return self._try_sync()
        return self._apply_synced(d)

    def on_snapshot(self, snap: DepthEvent) -> list[DepthEvent]:
        """Provide a REST snapshot (``final_update_id`` = lastUpdateId)."""
        if self.state is SyncState.SYNCED:
            return []  # late/duplicate snapshot, ignore
        self._snapshot = snap
        self.state = SyncState.SYNCING
        return self._try_sync()

    # ------------------------------------------------------------------ internals
    def _buffer(self, d: DepthEvent) -> None:
        self._buf.append(d)
        if len(self._buf) > self.max_buffer:
            # `del lst[:k]` removes the first k items: keep only the newest max_buffer diffs.
            del self._buf[: len(self._buf) - self.max_buffer]

    def _go_wait(self, keep: list[DepthEvent]) -> None:
        """Return to WAIT_SNAPSHOT, keeping `keep` as the start of the new buffer."""
        self.state = SyncState.WAIT_SNAPSHOT
        self._snapshot = None
        self._buf = list(keep)  # list(...) makes a copy so we don't share the caller's list

    def _try_sync(self) -> list[DepthEvent]:
        """Steps 3-5: line the buffered diffs up against the snapshot."""
        snap = self._snapshot
        assert snap is not None  # `assert` documents (and checks) an assumption
        L = snap.final_update_id  # lastUpdateId of the snapshot
        # Step 3: a list comprehension that keeps only diffs NOT already inside the snapshot.
        self._buf = [d for d in self._buf if d.final_update_id >= L]
        if not self._buf:
            return []  # keep waiting for the first diff
        first = self._buf[0]
        if first.first_update_id > L:  # step 4: snapshot too old
            # The earliest usable diff starts AFTER the snapshot point: updates between L and U
            # are missing, so the snapshot cannot be joined. Fetch a newer one.
            self.stale_snapshots += 1
            self._go_wait(self._buf)
            return []
        out: list[DepthEvent] = []
        self.book.clear()
        # replace() copies the frozen snapshot with a new ts_ns. Using the first diff's timestamp
        # guarantees the snapshot sorts BEFORE the diffs after it when data is replayed by time.
        out_snap = replace(snap, ts_ns=first.ts_ns)
        self.book.apply(out_snap)
        out.append(out_snap)
        self._last_u = None
        # Tuple swap: take the pending diffs and leave an empty buffer behind.
        pending, self._buf = self._buf, []
        self.state = SyncState.SYNCED
        # enumerate() yields (index, item) pairs so we can treat the first diff specially.
        for i, d in enumerate(pending):
            if i == 0:  # first event: U <= L <= u, no pu check
                self.book.apply(d)
                self._last_u = d.final_update_id
                out.append(d)
                continue
            res = self._apply_synced(d)
            if self.state is not SyncState.SYNCED:
                return []  # chain broke while draining the buffer: discard, resync from scratch
            out.extend(res)
        return out

    def _apply_synced(self, d: DepthEvent) -> list[DepthEvent]:
        """Step 5: apply one diff to a synced book, verifying the pu chain."""
        assert self._last_u is not None
        if d.final_update_id <= self._last_u:
            return []  # duplicate / already covered
        if d.prev_final_update_id != self._last_u:  # step 5: gap
            # A message was lost (pu doesn't match the last u we saw). The book can no longer be
            # trusted: discard it and wait for a new snapshot, keeping this diff for the re-join.
            self.gaps += 1
            self.resyncs += 1
            self.book.clear()
            self._last_u = None
            self._go_wait([d])
            return []
        self.book.apply(d)
        self._last_u = d.final_update_id
        return [d]
