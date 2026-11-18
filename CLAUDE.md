# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A three-step pipeline that turns price candles into support/resistance zones with probabilities and plots them. Each step exchanges plain JSON:

```
build_prompt.py  ──input.json──►  reversal-zones-projection skill (Claude)  ──projection.json──►  plot_projection.py
```

`README.md` is the full user-facing reference (CLI options, both JSON formats, glossary, known limits, roadmap). Keep it in sync when changing a CLI flag or a JSON field.

## Commands

```bash
uv sync                                                                      # Python 3.12+ (.python-version)
uv run python build_prompt.py PETR4.SA --horizon 1-3m -o petr4_input.json    # step 1, hits Yahoo Finance
uv run python plot_projection.py petr4_input.json petr4_projection.json -o petr4.png   # step 3
uv run pytest                                                                # all tests, offline, a few seconds
uv run pytest tests/test_rz.py::test_sim_output_structure                    # a single test
```

The tests in `tests/` never touch the network: they cover the pure functions of `build_prompt.py`, `rz.py`, a plot smoke test and the cross-file contracts below (`tests/test_contracts.py`). `tests/fixtures/input.json` is synthetic; regenerate it with `uv run python tests/fixtures/make_input.py`. There is no linter or CI, and nothing tests the Yahoo download itself, so still run the scripts end to end after changing them.

Step 2 is the `reversal-zones-projection` project skill: given an input JSON, Claude follows `.claude/skills/reversal-zones-projection/SKILL.md`. Its helper can also be run directly:

```bash
RZ=.claude/skills/reversal-zones-projection/scripts/rz.py
uv run python $RZ levels input.json              # facts for choosing zones
uv run python $RZ sim    input.json zones.json   # baseline probabilities
uv run python $RZ check  output.json             # validate a projection; prints OK or the violations
```

Use `rz.py check` on any projection JSON you write or edit by hand.

`examples/` holds a ready-made input/projection pair (`petr4_input.json`, `petr4_projection.json`) for trying `plot_projection.py` and `rz.py check` without Yahoo or step 2.

## Architecture

- **`build_prompt.py`** (step 1): `PRESETS` maps each horizon to a main series, an optional coarser context series, the `close_basis` and the probability meaning. The context series is filtered to candles that start *before* the main series, so the two never overlap. `render()` uses placeholder substitution to keep each candle row and event on one line inside otherwise indented JSON.
- **`.claude/skills/reversal-zones-projection/`** (step 2): `SKILL.md` is the prompt that defines the workflow, the zone-selection rules, the limits on adjusting simulated probabilities and the exact output schema. `scripts/rz.py` is its helper (Monte Carlo with a fixed seed, so the same input and zones give the same baseline).
- **`plot_projection.py`** (step 3): one `build()` function that draws five matplotlib axes (candles + zones, probability bars sharing the price axis, scenario bar, notes, side panel). `THEMES` and `TEXT` hold all colours and translated strings.

### Contracts that span files

The three pieces share no code, only JSON conventions. Changing one side silently breaks another:

- **Main series selection** is implemented twice, identically: the series whose `candles` equals `close_basis`, falling back to the last series (`load()` in `rz.py`, `build()` in `plot_projection.py`). `build_prompt.py` guarantees this by inserting the context series *first*.
- **Horizon and candle labels**: adding a horizon or interval means touching `PRESETS`/`LABEL` in `build_prompt.py`, `HORIZON_DAYS`/`CANDLE_DAYS`/`schedule()` in `rz.py`, the horizon tables in `SKILL.md`, and `TEXT[*]["labels"]` in `plot_projection.py`.
- **`last_row: "forming"`** shortens the first simulation step in `rz.py` and removes one future slot in the plot.
- **Tails** are zone entries with a `null` edge and ids `below`/`above`; the plot separates them from real zones by testing for `None`.
- **Scenario thresholds**: the plot recovers the middle-zone prices by regex from the scenario `low`/`high` strings (`"below 40.6"`), whose format comes from `rz.py sim`. Keep a number in those strings.
- **Swing markers** are drawn only if `structure.swing.*[1]` exactly equals a row's `start` string.
- **Schema keys, ids and scenario names stay English** regardless of language; only free-text values are translated.

### Things that are easy to get wrong

- Two unrelated language settings: `build_prompt.py --language` (default `en`) sets the language of the projection's free text; `plot_projection.py --lang` (default `pt`) only translates the chart's own labels.
- Prices are split-adjusted but deliberately **not** dividend-adjusted (`auto_adjust=False`); the `drop` shock in `rz.py` and the chart footer depend on that.
- The project is not a package: `pyproject.toml` has no build system or entry points, only dependencies. The code is the two root scripts, run with `uv run python`.
- The skill is a project skill, so Claude Code in this repo always uses the current files. A copy installed elsewhere (claude.ai, `~/.claude/skills/`) does not follow edits made here.
- `rz.py` must not be rewritten per run or have its seed changed: reproducible baselines are a design requirement.
- Session and week-end logic assumes a Monday-to-Friday exchange, so crypto tickers get wrong `forming` labels (listed under Known limits in the README).

## Domain constraint

The output is analysis, not advice: the skill never emits buy/sell/hold calls, stops, targets or position sizes, and `confidence.level` is only ever `low` or `medium`. The probabilities are not backtested. Preserve these constraints and the disclaimers when editing the skill, the chart footer or the README.
