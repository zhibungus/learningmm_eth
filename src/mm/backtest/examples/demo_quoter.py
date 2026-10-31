"""DEMO QUOTER -- NOT A REAL STRATEGY.  It exists only to smoke-test the backtest/paper plumbing.

It quotes a fixed distance either side of the mid, refreshes on a timer, and stops quoting one side
when the inventory cap is hit.  There is no alpha, no adverse-selection control, no spread model:
any PnL it shows is meaningless.  Real strategies are built from ``specs/`` by the quant-developer
in ``src/mm/strategies/``.

It does show the shape of a strategy: subclass ``Strategy``, declare ``event_kinds``, return actions
from callbacks, read everything from ``state``.

Parameters (``strategy.params`` in the YAML)
    half_spread_ticks   distance from mid to each quote, in ticks             (default 2)
    order_qty           size of each quote in ETH                             (default 0.05)
    max_position        inventory cap in ETH; no bid above it, no ask below -it (default 0.5)
    requote_ticks       move the quote when it is this many ticks off target  (default 1)
"""
# Lazy type hints (see mm/backtest/strategy.py).
from __future__ import annotations

from mm.backtest.strategy import CancelOrder, MarketState, PlaceOrder, ReplaceOrder, Strategy


# "class DemoQuoter(Strategy)" = inheritance: DemoQuoter is a Strategy and overrides only the callback
# it needs (on_timer). A market maker's basic job: keep a buy order (bid) below and a sell order (ask)
# above fair value, earn the gap when both fill, and limit how much inventory piles up on one side.
class DemoQuoter(Strategy):
    """Fixed-spread symmetric quoter.  FOR PLUMBING TESTS ONLY."""

    name = "demo_quoter"
    event_kinds = ("quote",)   # we only need the book top; everything else is timer-driven

    def __init__(self, params=None, rng=None) -> None:
        # super().__init__ runs the parent class's setup first. Then read our settings from the
        # params dict; dict.get(key, default) returns the default when the key is absent, so the
        # strategy works even with an empty config (defaults are for the demo only).
        super().__init__(params, rng)
        p = self.params
        self.half_ticks = p.get("half_spread_ticks", 2)
        self.qty = p.get("order_qty", 0.05)
        self.max_pos = p.get("max_position", 0.5)
        self.requote_ticks = p.get("requote_ticks", 1)
        self._ids: dict[str, str] = {}   # side -> client_id of our live order

    # Called every timer interval (1 s by default). Each round, for each side:
    #   * no order live  -> place one at mid +/- half spread, if inventory allows;
    #   * order is live  -> cancel it if inventory cap forbids it, or move (replace) it if the target moved;
    #   * order pending  -> do nothing and wait for the exchange's answer.
    # Rounding the bid DOWN and the ask UP keeps quotes on the tick grid without tightening them.
    # post_only=True guarantees we are a maker: if the order would cross the book it is rejected
    # rather than filled as an expensive taker.
    def on_timer(self, now_ns: int, state: MarketState):
        mid = state.mid
        if mid is None:
            return None
        tick = state.tick_size
        actions = []
        for side in ("buy", "sell"):
            if side == "buy":
                target = state.round_price(mid - self.half_ticks * tick, "down")
                allowed = state.position + self.qty <= self.max_pos + 1e-9
            else:
                target = state.round_price(mid + self.half_ticks * tick, "up")
                allowed = state.position - self.qty >= -self.max_pos - 1e-9
            # Inventory cap: stop buying when long near the limit, stop selling when short near it.
            # (position is signed: + long, - short.) Look up our live order for this side, if any.
            cid = self._ids.get(side)
            view = state.open_orders.get(cid) if cid else None
            if view is None:
                if allowed:
                    cid = self.new_id(side[0])
                    self._ids[side] = cid
                    actions.append(PlaceOrder(cid, side, self.qty, target, post_only=True))
            elif view.status == "open":
                if not allowed:
                    actions.append(CancelOrder(cid))
                elif abs(view.price - target) >= self.requote_ticks * tick - 1e-9:
                    actions.append(ReplaceOrder(cid, price=target))
        return actions
