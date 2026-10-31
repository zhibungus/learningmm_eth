"""A small re-ordering buffer for live events.

Why it exists
-------------
The live feed uses two separate websocket connections (trades on one, depth + quotes on the other), so
messages can ARRIVE in a different order from the one the exchange produced them in. In the owner's
recordings about 21% of trades arrived AFTER the depth message that already reflected them. A replay
sorts events by ``(ts_ns, kind priority)`` (a trade comes before the book update it caused); if the
paper trader processed events in arrival order instead, a depth drop would be read as a cancellation
and then the same trade would be subtracted from our queue AGAIN, moving us up the queue too fast.

The fix: hold every event for a short, bounded time (``paper.reorder_hold_ms``), then release the
ready ones sorted by the SAME rule replay uses. A late message that misses the hold window is still
processed (the engine clamps its time), it just cannot be re-ordered any more.
"""
from __future__ import annotations

# heapq keeps a list ordered so the smallest item comes out first (a "priority queue").
import heapq
from typing import Any


class ReorderBuffer:
    """Hold events, release them sorted by (ts_ns, kind priority, arrival order).

    ``priority`` maps an event ``kind`` ("funding", "trade", "depth", "quote", ...) to a number; lower
    is delivered first at an identical ``ts_ns``. Use ``config/data.yaml: replay.kind_priority``.
    A "gap" marker sorts before everything at its timestamp.
    """

    def __init__(self, hold_ns: int, priority: dict[str, int]) -> None:
        self.hold_ns = hold_ns
        self.priority = priority
        self._heap: list[tuple] = []     # items: (ts_ns, kind_priority, seq, arrival_ns, event)
        self._seq = 0                    # arrival counter: the final tie-break, keeps order stable

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, ev: Any, arrival_ns: int) -> None:
        """Add an event that arrived at local monotonic time ``arrival_ns``."""
        self._seq += 1
        # gap sorts first (-1); unknown kinds sort last (a large number) instead of crashing.
        prio = -1 if ev.kind == "gap" else self.priority.get(ev.kind, 99)
        heapq.heappush(self._heap, (ev.ts_ns, prio, self._seq, arrival_ns, ev))

    def min_ts(self) -> int | None:
        """Smallest exchange timestamp currently held (None if empty)."""
        return self._heap[0][0] if self._heap else None

    def pop_ready(self, now_ns: int) -> list[Any]:
        """Release events in sorted order, but only those that have been held at least ``hold_ns``.

        The heap is ordered by exchange time, not by arrival, so we scan from the smallest and stop
        at the first event that is still too young: releasing a LATER-ts event while an EARLIER-ts one
        is still waiting would defeat the purpose of sorting.
        """
        out = []
        while self._heap and now_ns - self._heap[0][3] >= self.hold_ns:
            out.append(heapq.heappop(self._heap)[4])
        return out

    def pop_all(self) -> list[Any]:
        """Release everything now, sorted (used at shutdown and before handling a disconnect)."""
        out = []
        while self._heap:
            out.append(heapq.heappop(self._heap)[4])
        return out
