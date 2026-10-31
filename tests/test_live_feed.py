"""LiveFeed message handling with synthetic websocket frames (no network)."""
# pytest primer: test_* functions run automatically; failed `assert` = failed test.
# These tests call LiveFeed.handle_message() directly with fake websocket text frames, so the
# parsing/routing/sync logic is tested without any network connection.
import json

from mm.data.live import LiveFeed
from mm.data.events import DepthEvent, QuoteEvent, TradeEvent
from mm.data.book import SyncState
from mm.data import normalize as N


# Wrap a payload the way Binance's combined stream does: {"stream": ..., "data": {...}}.
def frame(data):
    return json.dumps({"stream": "x", "data": data})


# Build a fake depthUpdate frame. U/u/pu are the update-id fields used to verify the sequence
# (see book.py); b/a are lists of [price, qty] strings, as Binance sends them.
def dmsg(U, u, pu, bids=(), asks=(), T=1000):
    return frame({"e": "depthUpdate", "E": T, "T": T, "s": "ETHUSDT", "U": U, "u": u, "pu": pu,
                  "b": [[str(p), str(q)] for p, q in bids], "a": [[str(p), str(q)] for p, q in asks]})


def test_routing_and_book_sync_through_handle_message():
    # Trades and quotes pass straight through; depth is held back until a snapshot joins it, and a
    # sequence gap produces a book_gap status. `lambda` below = a tiny inline function.
    feed = LiveFeed()
    got = []
    feed.subscribe(got.append)
    raw = []
    feed.on_raw = lambda ts, txt: raw.append((ts, txt))

    feed.handle_message(dmsg(1, 10, 0, bids=[(99, 1)]), 1)           # buffered
    feed.handle_message(frame({"e": "aggTrade", "E": 5, "T": 5, "a": 1, "p": "100", "q": "1",
                               "f": 1, "l": 1, "m": True}), 2)
    feed.handle_message(frame({"e": "bookTicker", "E": 5, "T": 5, "u": 3, "b": "99", "B": "1",
                               "a": "100", "A": "1"}), 3)
    assert [type(e) for e in got] == [TradeEvent, QuoteEvent]       # nothing from depth yet
    assert feed.sync.needs_snapshot

    snap = N.parse_depth_snapshot({"lastUpdateId": 5, "E": 900, "T": 900,
                                   "bids": [["98", "2"]], "asks": [["101", "2"]]}, 4)
    for ev in feed.sync.on_snapshot(snap):
        feed._emit_depth([ev])
    depth = [e for e in got if isinstance(e, DepthEvent)]
    assert [e.is_snapshot for e in depth] == [True, False]
    assert feed.sync.state is SyncState.SYNCED and feed.book.best_bid == (99.0, 1.0)

    feed.handle_message(dmsg(11, 12, 10, asks=[(100.5, 3)]), 5)
    assert feed.book.best_ask == (100.5, 3.0)
    assert len(raw) == 4 and raw[0][0] == 1

    # a gap: status event + book needs a new snapshot
    feed.handle_message(dmsg(20, 21, 19), 6)
    assert feed.sync.needs_snapshot and feed.sync.gaps == 1
    assert any(getattr(e, "status", "") == "book_gap" for e in got)
    assert feed.counts["trade"] == 1 and feed.counts["quote"] == 1


def test_urls_come_from_config():
    # Endpoints/streams are read from config/data.yaml, never hard-coded in the code.
    feed = LiveFeed(symbol="ethusdt")
    assert feed.url("public").endswith("?streams=ethusdt@depth@100ms/ethusdt@bookTicker")
    assert feed.url("market").endswith("?streams=ethusdt@aggTrade")
