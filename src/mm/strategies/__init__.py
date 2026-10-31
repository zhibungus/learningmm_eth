"""Strategies built from the specs in ``specs/``.  One module per strategy (see CLAUDE.md).

The backtest engine loads ``strategy: {name: glft}`` by importing ``mm.strategies.glft`` and looking
for exactly one ``Strategy`` subclass in that module (``mm.backtest.engine.resolve_strategy``).
"""
