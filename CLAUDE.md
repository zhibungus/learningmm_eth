# Quant Market Making Lab

A research-to-backtest pipeline for quantitative market making strategies, built
with a team of specialist subagents (defined in `.claude/agents/`). The owner is
learning as they go, so clarity matters as much as performance.

## Folder layout

```
research/      Literature notes and paper summaries (quant-researcher)
specs/         Strategy specifications: math, parameters, risk limits (quant-researcher)
data/
  raw/         Downloaded market data, never edited by hand (data-engineer)
  processed/   Cleaned, normalized data (data-engineer)
src/mm/
  data/        Loaders and schemas
  strategies/  One module per strategy (quant-developer)
  backtest/    Engine, fill models, metrics (backtester)
tests/         Unit tests (quant-developer, backtester)
reports/       Backtest results, one folder per run (backtester)
reviews/       Validation reports (validator)
learnings/     Explanations, study notes and decision write-ups for the owner (tutor)
```

## Pipeline

1. **Research**: `quant-researcher` studies the idea and writes `research/<topic>.md`, then `specs/<strategy>.md`.
2. **Data**: `data-engineer` gets and cleans the data the spec needs.
3. **Implement**: `quant-developer` builds the strategy from the spec, with tests.
4. **Backtest**: `backtester` runs it with realistic fills and costs, then writes `reports/<run>/`.
5. **Validate**: `validator` looks for bias, overfitting and unrealistic assumptions. A strategy isn't "done" until it passes.
6. **Learn**: `tutor` explains anything on request, at any stage. Whenever the owner makes a decision, `tutor` writes it up in `learnings/`: the options, how they differ, the choice and why, and how firms decide. Related topics share one doc. A separate concept gets its own.

Each stage reads the previous stage's files. Agents don't redo another agent's work. They report gaps back instead.

## Conventions

- Python 3.11+, with a virtual environment in `.venv/`. Dependencies go in `requirements.txt`.
- Prefer `numpy`/`pandas`/`polars`. Use `numba` only where profiling shows a need.
- Use the same units everywhere: prices in quote currency, sizes in base units, timestamps as integer nanoseconds UTC.
- Keep every parameter in config/spec, never as a magic number in code.
- Fix a random seed for each backtest run and record it in the report.
- Write Python for a learner. Above each logical chunk, add a comment saying what it does and why. Explain any non-obvious Python syntax where it first appears in a file (decorators, dataclasses, type hints, comprehensions, `async`/`await`, generators/`yield`, context managers, polars expressions, `*args`/`**kwargs`, etc.). Explain the domain meaning too, not just the mechanics. Comments must never change behaviour.
- This is research and education, not financial advice. Backtest results don't promise live performance.
