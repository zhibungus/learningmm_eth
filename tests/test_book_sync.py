"""BookSynchronizer / OrderBook with synthetic messages (no network)."""
# pytest primer: any function named test_* in a file named test_*.py is run automatically; a test
# passes if it finishes without an `assert` failing. `python -m pytest` finds and runs them all.
# These tests use hand-made (synthetic) messages so no network is needed.
from mm.data.book import BookSynchronizer, OrderBook, SyncState
from mm.data.events import DepthEvent


# Helper that builds a depth DIFF message. U/u = first/last update id covered, pu = previous
# message's u. `bids=()` is a default argument; `ts=1_000` the underscore is just a readable 1000.
# See book.py for why these ids let us detect missing messages.
def diff(U, u, pu, bids=(), asks=(), ts=1_000):
    return DepthEvent(ts_ns=ts, event_ts_ns=ts, recv_ts_ns=ts, is_snapshot=False,
                      first_update_id=U, final_update_id=u, prev_final_update_id=pu,
                      bids=tuple(bids), asks=tuple(asks))


# Helper that builds a REST SNAPSHOT: the whole book as of update id L (lastUpdateId).
def snap(L, bids, asks, ts=500):
    return DepthEvent(ts_ns=ts, event_ts_ns=ts, recv_ts_ns=ts, is_snapshot=True,
                      first_update_id=L, final_update_id=L, prev_final_update_id=None,
                      bids=tuple(bids), asks=tuple(asks))


def test_orderbook_apply_delete_and_best():
    # Deleting the best bid (qty 0) must promote the next level; crossing prices must be detected.
    b = OrderBook()
    b.apply(snap(1, [(100.0, 1), (99.0, 2)], [(101.0, 3), (102.0, 4)]))
    assert b.best_bid == (100.0, 1) and b.best_ask == (101.0, 3)
    b.apply(diff(2, 2, 1, bids=[(100.0, 0)], asks=[(100.5, 7)]))
    assert b.best_bid == (99.0, 2) and b.best_ask == (100.5, 7)
    assert not b.is_crossed() and not b.is_locked()
    b.apply(diff(3, 3, 2, bids=[(100.5, 1)]))
    assert b.is_locked()
    b.apply(diff(4, 4, 3, bids=[(101.0, 1)]))
    assert b.is_crossed()
    assert b.top(1) == ([(101.0, 1)], [(100.5, 7)])


def test_snapshot_replaces_book():
    # A snapshot is the full truth, so old levels (100.0) must vanish.
    b = OrderBook()
    b.apply(snap(1, [(100.0, 1)], [(101.0, 1)]))
    b.apply(snap(9, [(50.0, 1)], [(51.0, 1)]))
    assert b.best_bid == (50.0, 1) and 100.0 not in b.bids


def test_happy_path_drops_old_and_uses_straddling_event():
    # Walk through Binance's procedure: events with u < L are dropped (already in the snapshot);
    # the first used event must satisfy U <= L <= u (it straddles the snapshot point).
    s = BookSynchronizer()
    # buffered before the snapshot arrives; L = 100
    assert s.on_diff(diff(80, 90, 79, bids=[(1.0, 1)])) == []
    assert s.on_diff(diff(91, 99, 90, bids=[(2.0, 1)])) == []
    assert s.on_diff(diff(100, 105, 99, bids=[(99.0, 5)])) == []   # U<=L<=u -> first used
    assert s.needs_snapshot
    out = s.on_snapshot(snap(100, [(98.0, 1)], [(101.0, 1)]))
    assert [e.is_snapshot for e in out] == [True, False]
    assert out[1].final_update_id == 105
    assert out[0].ts_ns == out[1].ts_ns            # snapshot aligned to first applied diff
    assert s.state is SyncState.SYNCED and s.last_update_id == 105
    assert s.book.best_bid == (99.0, 5)            # old events dropped, straddling one applied
    out = s.on_diff(diff(106, 108, 105, asks=[(100.0, 2)]))
    assert len(out) == 1 and s.book.best_ask == (100.0, 2)


def test_snapshot_first_then_diff_arrives():
    # Same procedure when the snapshot arrives BEFORE any diff has been buffered.
    s = BookSynchronizer()
    assert s.on_snapshot(snap(100, [(98.0, 1)], [(101.0, 1)])) == []
    assert s.state is SyncState.SYNCING
    assert s.on_diff(diff(50, 99, 49)) == []                       # u < L dropped
    out = s.on_diff(diff(98, 101, 97, bids=[(98.5, 3)]))           # straddles L
    assert len(out) == 2 and out[0].is_snapshot
    assert s.state is SyncState.SYNCED


def test_stale_snapshot_requests_new_one():
    # If the oldest buffered diff starts after L (U > L), updates in between are unknown, so the
    # snapshot is too old and a fresher one is needed.
    s = BookSynchronizer()
    s.on_diff(diff(200, 210, 199))
    out = s.on_snapshot(snap(100, [], []))        # first remaining U=200 > L=100
    assert out == [] and s.needs_snapshot and s.stale_snapshots == 1
    out = s.on_snapshot(snap(205, [(1.0, 1)], [(2.0, 1)]))
    assert len(out) == 2 and s.state is SyncState.SYNCED


def test_gap_triggers_resync_and_recovers():
    # pu != previous u means a message was lost: the book must be discarded and rebuilt.
    s = BookSynchronizer()
    s.on_diff(diff(1, 10, 0))
    s.on_snapshot(snap(5, [(1.0, 1)], [(2.0, 1)]))
    assert s.state is SyncState.SYNCED
    assert len(s.on_diff(diff(11, 12, 10, bids=[(1.5, 1)]))) == 1
    out = s.on_diff(diff(14, 15, 13, bids=[(1.6, 1)]))   # pu=13 != 12 -> gap
    assert out == [] and s.needs_snapshot and s.gaps == 1 and s.resyncs == 1
    assert not s.book.bids                                # book discarded
    out = s.on_snapshot(snap(14, [(1.0, 1)], [(2.0, 1)]))  # buffered gap event straddles L
    assert [e.is_snapshot for e in out] == [True, False]
    assert s.state is SyncState.SYNCED and s.last_update_id == 15


def test_duplicate_old_event_ignored_when_synced():
    # An already-seen update (u <= last u) is harmless and must not count as a gap.
    s = BookSynchronizer()
    s.on_diff(diff(1, 10, 0))
    s.on_snapshot(snap(5, [], []))
    assert s.on_diff(diff(1, 10, 0)) == []
    assert s.state is SyncState.SYNCED and s.gaps == 0


def test_gap_inside_buffer_forces_resync():
    # A hole inside the diffs buffered before the snapshot also forces a resync.
    s = BookSynchronizer()
    s.on_diff(diff(1, 10, 0))
    s.on_diff(diff(11, 12, 10))
    s.on_diff(diff(20, 25, 19))            # gap
    out = s.on_snapshot(snap(5, [(1.0, 1)], [(2.0, 1)]))
    assert out == [] and s.needs_snapshot and s.gaps == 1


def test_reset_after_reconnect():
    # After a websocket reconnect everything is forgotten, since messages may have been missed.
    s = BookSynchronizer()
    s.on_diff(diff(1, 10, 0))
    s.on_snapshot(snap(5, [(1.0, 1)], [(2.0, 1)]))
    s.reset()
    assert s.needs_snapshot and not s.book.bids and s.last_update_id is None
