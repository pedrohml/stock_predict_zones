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
uv sync                                                                      # Python 3.12+ (.python-version); also installs the `zones` command
uv run zones PETR4.SA --horizon 1-3m                                         # all steps: Yahoo, headless `claude -p`, rz.py check, chart, into runs/TICKER/TIME_HORIZON/
uv run python run_pipeline.py PETR4.SA --horizon 1-3m                        # the same thing; `zones` is run_pipeline.main
uv tool install --editable .                                                 # optional: `zones` in any directory, still running this checkout
uv run python build_prompt.py PETR4.SA --horizon 1-3m -o petr4_input.json    # step 1, hits Yahoo Finance
uv run python plot_projection.py petr4_input.json petr4_projection.json -o petr4.png   # step 3
uv run pytest                                                                # all tests, offline, a few seconds
uv run pytest tests/test_rz.py::test_sim_output_structure                    # a single test
```

The tests in `tests/` never touch the network: they cover the pure functions of `build_prompt.py`, `rz.py`, a plot smoke test, `run_pipeline.py` with Yahoo and Claude stubbed, and the cross-file contracts below (`tests/test_contracts.py`). `tests/fixtures/input.json` is synthetic; regenerate it with `uv run python tests/fixtures/make_input.py`. There is no linter or CI, and nothing tests the Yahoo download itself, so still run the scripts end to end after changing them.

Step 2 is the `reversal-zones-projection` project skill: given an input JSON, Claude follows `.claude/skills/reversal-zones-projection/SKILL.md`. Its helper can also be run directly:

```bash
RZ=.claude/skills/reversal-zones-projection/scripts/rz.py
uv run python $RZ levels input.json              # facts for choosing zones
uv run python $RZ sim    input.json zones.json   # baseline probabilities
uv run python $RZ check  output.json             # validate a projection; prints OK or the violations
uv run python $RZ save   input.json output.json  # keep the pair under projections/TICKER/TIME_HORIZON/
uv run python $RZ list                           # saved projections, oldest first, open or ended
```

Use `rz.py check` on any projection JSON you write or edit by hand.

The skill saves every projection by default (`rz.py save`, its last workflow step) so past projections can be scored later. `projections/` is gitignored, relative to the working directory (override with `RZ_PROJECTIONS_DIR` or a trailing folder argument), and append-only: `save` never overwrites and nothing should edit or delete an earlier entry.

`examples/` holds a ready-made input/projection pair (`petr4_input.json`, `petr4_projection.json`) for trying `plot_projection.py` and `rz.py check` without Yahoo or step 2.

## Architecture

- **`build_prompt.py`** (step 1): `PRESETS` maps each horizon to a main series, an optional coarser context series, the `close_basis` and the probability meaning. The context series is filtered to candles that start *before* the main series, so the two never overlap. `render()` uses placeholder substitution to keep each candle row and event on one line inside otherwise indented JSON.
- **`.claude/skills/reversal-zones-projection/`** (step 2): `SKILL.md` is the prompt that defines the workflow, the zone-selection rules, the limits on adjusting simulated probabilities and the exact output schema. `scripts/rz.py` is its helper (Monte Carlo with a fixed seed, so the same input and zones give the same baseline).
- **`plot_projection.py`** (step 3): one `build()` function that draws five matplotlib axes (candles + zones, probability bars sharing the price axis, scenario bar, notes, side panel). `THEMES` and `TEXT` hold all colours and translated strings, including each language's number separators and intraday tick format. Labels are placed by measuring text with the figure's renderer (`extent()`, `spread()`, `share_lines()`), not by fixed offsets; `tests/test_plot_layout.py` pins that with text boxes, never pixels.
- **`run_pipeline.py`** (all steps): a thin orchestrator that shares no code with the others. It runs the two scripts and `rz.py check` as subprocesses and step 2 as `claude -p` (`claude_command()` holds the exact flags and the tool allowlist, `call_claude()` is the only place that starts Claude). Step 2 asks for `--output-format stream-json --verbose` (one JSON event per line): `call_claude()` drains stdout and stderr in two threads and enforces the timeout itself, `Progress` maps tool calls to the skill's stages (`STAGES`, one stderr line each, heartbeat after a silence, nothing with `--quiet`) and skips every event or line it does not know, and `claude_result()` finds the final `result` event, from which `summary()` prints duration, turns and cost (a field Claude Code does not report is left out, never guessed). A new helper subcommand or work-file name in the skill needs a matching entry in `STAGES`/`Progress.stage()` to show up. Options it does not declare go to `build_prompt.py` untouched, so a new step-1 flag needs no change here; plot options are forwarded only when given, so step 3 keeps its own defaults. Output: without `--out-dir`, `run_folder()` makes a new `runs/TICKER/TIME_HORIZON/` under the current directory (named like `rz.py save` names its folders, from the input's `asset.ticker`, `now.time` and `horizon`, with `_2`, `_3` ... when taken) holding `input.json`, `projection.json` and `chart.png`, with files given by `--input`/`--projection` copied in so every folder is complete; plotting again with `--chart` makes no folder. With `--out-dir` the files go exactly there as `STEM_*` (stem from the ticker or `--name`) and given files are not copied. Nothing is created before the input and the given projection have loaded, and `build_prompt.py` writes into a throwaway folder. `runs/` is disposable output; `projections/` stays the append-only record, and the two are never merged. `--open` calls `open_chart()` after a successful run (`start_viewer()` is the only place that starts a viewer; failure is one warning). When the input's `research` is not `none` its prompt (`claude_prompt()`) adds research rules on top of the skill's: separate searches for the cause and for the events, and every driver `source` and event size must come from a page opened with WebFetch, because drivers written from the search summary alone did not match their sources in real runs. It tells Claude to skip the skill's save step and runs `rz.py save` itself after `check` (not with `--projection` or `--no-save`), so each run is kept exactly once; a failed save only warns. The tests stub `run` (for Yahoo) and `call_claude` (or replace the `claude` binary with a script that replays `tests/fixtures/claude_stream.jsonl`, a cut-down recording of one real run), and point `RZ_PROJECTIONS_DIR` at a temp folder so they never write into the repo's `projections/`. A test that runs `main()` without `--out-dir` must first `chdir` to a temp folder (the autouse fixture in `tests/test_run_pipeline_outputs.py`), or it writes `runs/` into the repo, and must stub `start_viewer` before passing `--open`.

### Contracts that span files

The three pieces share no code, only JSON conventions. Changing one side silently breaks another:

- **Main series selection** is implemented twice, identically: the series whose `candles` equals `close_basis`, falling back to the last series (`load()` in `rz.py`, `build()` in `plot_projection.py`). `build_prompt.py` guarantees this by inserting the context series *first*.
- **Horizon and candle labels**: adding a horizon or interval means touching `PRESETS`/`LABEL` in `build_prompt.py`, `HORIZON_DAYS`/`CANDLE_DAYS`/`schedule()` in `rz.py`, the horizon tables in `SKILL.md`, and `TEXT[*]["labels"]` in `plot_projection.py`.
- **`last_row: "forming"`** shortens the first simulation step in `rz.py` and removes one future slot in the plot.
- **Validation**: `plot_projection.py` `preflight()` runs before `build()` and reports every problem at once (exit 1, no traceback). `validate()` covers what `build()` reads, so a new `[...]` access in `build()` needs a matching check there. The projection's rules are not duplicated: the plot loads `rz.py` by path (`RZ_PATH`) and calls `problems(proj, complete=False)`; `rz.py check`/`save` call the same function with `complete=True`. Rule violations block the plot unless `--no-check`; without `rz.py` the plot checks structure only and says so.
- **Tails** are zone entries with a `null` edge and ids `below`/`above`; the plot separates them from real zones by testing for `None`.
- **Scenario thresholds**: the plot recovers the middle-zone prices by regex from the scenario `low`/`high` strings (`"below 40.6"`), whose format comes from `rz.py sim`. Keep a number in those strings.
- **Swing markers** are drawn only if `structure.swing.*[1]` exactly equals a row's `start` string.
- **Schema keys, ids and scenario names stay English** regardless of language; only free-text values are translated.

### Things that are easy to get wrong

- One language setting: `build_prompt.py --language` (default `en`, free text) is stored as `reply.language`, sets the language of the projection's free text, and `plot_projection.py` follows it for the chart's own labels (`chart_language()`: primary subtag, so `pt-BR` -> `pt`; no `TEXT` entry or no `reply` -> `FALLBACK_LANG`). `--lang` has no default of its own: it is only an override, and it translates the labels, not the projection's text. A new chart language is one new `TEXT` entry, nothing else.
- Prices are split-adjusted but deliberately **not** dividend-adjusted (`auto_adjust=False`); the `drop` shock in `rz.py` and the chart footer depend on that.
- The project is installable only as an editable install, and is still not a package directory: the code is the three root scripts. `pyproject.toml` has a `hatchling` build system and one entry point, `zones = "run_pipeline:main"`, whose wheel holds `run_pipeline.py` alone (`uv_build` cannot build a flat module). `run_pipeline.py` finds the other scripts and the skill next to its own file (`ROOT`) and runs Claude Code there, so `uv sync` and `uv tool install --editable .` work, and a non-editable install stops with a message saying so. Do not move the scripts without changing `ROOT`.
- The skill is a project skill, so Claude Code in this repo always uses the current files. A copy installed elsewhere (claude.ai, `~/.claude/skills/`) does not follow edits made here.
- `rz.py` must not be rewritten per run or have its seed changed: reproducible baselines are a design requirement.
- Session and week-end logic assumes a Monday-to-Friday exchange, so crypto tickers get wrong `forming` labels (listed under Known limits in the README).

## Domain constraint

One deliberate exception, kept separate: `prompts/trade_entries.md` (reply schema in `prompts/trade_entries.schema.json`) is an opt-in prompt template that turns one input + projection into three ranked hypothetical setups with entry, stop and target. It is not wired into the skill, the chart or `run_pipeline.py`, and it must stay that way unless the user asks. Its rules are mechanical (prices from zone edges, odds from `reach`/`turn`); it reads the projection's `supports`/`resistances` (`zone`, `reach`, `turn`), `structure.volatility.assumed`, `horizon.closes`/`last`, `events` and `confidence`, so a change to those fields in `SKILL.md` needs a matching change there. It still carries no position size and never raises the confidence level.

The output is analysis, not advice: the skill never emits buy/sell/hold calls, stops, targets or position sizes, and `confidence.level` is only ever `low` or `medium`. The probabilities are not backtested. Preserve these constraints and the disclaimers when editing the skill, the chart footer or the README.
