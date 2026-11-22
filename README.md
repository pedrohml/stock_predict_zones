# stock_predict_zones

Turn price candles into **support and resistance zones with probabilities**, and plot the result.

The project is a three-step pipeline. Each step reads and writes plain JSON, so any step can be inspected, edited or replaced.

```
 Yahoo Finance            Claude + skill                     matplotlib
      │                        │                                 │
      ▼                        ▼                                 ▼
build_prompt.py  ──►  reversal-zones-projection  ──►  plot_projection.py
                input.json                 projection.json              chart.png / .svg / .pdf
```

| Step | Tool | Input | Output |
|---|---|---|---|
| 1. Collect | `build_prompt.py` | a ticker and a horizon | `input.json`: candles plus the request |
| 2. Project | `reversal-zones-projection` skill, run by Claude | `input.json` | `projection.json`: zones, probabilities, scenarios |
| 3. Plot | `plot_projection.py` | both JSON files | an image |

![Example chart: PETR4.SA reversal zones, 1 to 3 month horizon](examples/petr4.png)

*Example output from the files in [`examples/`](examples/), using data as of 18 Nov 2026. It shows what the pipeline produces; it is not a forecast.*

> **Read this first.** The probabilities are internally consistent, but only their unadjusted baseline has been backtested, on twelve tickers and mechanically chosen zones (see [Backtest of the baseline](#backtest-of-the-baseline)). Claude's own numbers were compared with that baseline on 88 past cases ([Does Claude's adjustment help?](#does-claudes-adjustment-help)): without web research it left the baseline unchanged every time, and its zones scored no better than the mechanical ones beyond noise. Runs with research have not been checked against outcomes. Treat the output as a structured way to look at a chart, not as a forecast you can rely on. Nothing here is investment advice.

## Contents

- [Setup](#setup)
- [Quick start](#quick-start)
- [Your defaults](#your-defaults)
- [Commands](#commands)
- [Batch runs](#batch-runs)
- [Step 1: build_prompt.py](#step-1-build_promptpy)
- [Data sources: Yahoo, the cache, CSV files](#data-sources-yahoo-the-cache-csv-files)
- [Step 2: the reversal-zones-projection skill](#step-2-the-reversal-zones-projection-skill)
- [Step 3: plot_projection.py](#step-3-plot_projectionpy)
- [Ranked trade setups](#ranked-trade-setups)
- [Backtest of the baseline](#backtest-of-the-baseline)
- [Backtest of the trade setups](#backtest-of-the-trade-setups)
- [Scoring the saved setups](#scoring-the-saved-setups)
- [Does Claude's adjustment help?](#does-claudes-adjustment-help)
- [Engines: claude or mechanical](#engines-claude-or-mechanical)
- [How the probabilities are produced](#how-the-probabilities-are-produced)
- [Glossary](#glossary)
- [Known limits](#known-limits)
- [Roadmap](#roadmap)
- [Project layout](#project-layout)

## Setup

Requirements: Python 3.12 or newer (see `.python-version`) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/pedrohml/stock_predict_zones.git
cd stock_predict_zones
uv sync
```

`uv sync` installs the four declared dependencies (`yfinance`, `matplotlib`, `numpy` and `pandas`) and the `zones` command, which is `run_pipeline.py`. Inside the repository run it as `uv run zones`.

To have `zones` in any directory, install it once from the repository folder:

```bash
uv tool install --editable .    # undo with: uv tool uninstall stock-predict-zones
```

The install must be editable. `zones` keeps running the scripts and the skill of this checkout, so edits and `git pull` take effect at once, and the folder must stay where it is. If the shell does not find `zones` afterwards, run `uv tool update-shell` and open a new terminal. Output folders and relative paths such as `--input` and `--out-dir` are taken from the directory you are in, not from the repository.

Tests: `uv run pytest` runs the offline suite in `tests/` (no network, about two minutes). Yahoo is replaced by fakes, including simulated rate limits, server errors and hung requests; the real download is not exercised.

Step 2 needs Claude with the skill in `.claude/skills/reversal-zones-projection/`. Claude Code picks it up automatically when run in this repository; other Claude apps need it installed. See [Installing the skill](#installing-the-skill). With `--engine mechanical` step 2 needs neither Claude nor Claude Code ([Engines](#engines-claude-or-mechanical)).

## Quick start

One command, from ticker to chart and ranked trade setups (needs [Claude Code](https://claude.com/claude-code) installed and signed in):

```bash
uv run zones PETR4.SA --open          # plain `zones PETR4.SA --open` after the global install
```

For a free, fast scan with no Claude call at all, add `--engine mechanical`: the zones and probabilities come from fixed rules in about a second, so a whole watchlist takes seconds per ticker and costs nothing. See [Engines: claude or mechanical](#engines-claude-or-mechanical) for what it gives up.

```bash
uv run zones PETR4.SA --engine mechanical      # no Claude Code needed; no web research
```

It runs the three steps below in order, validates the projection with `rz.py check` before plotting, then computes three ranked trade setups (see [Ranked trade setups](#ranked-trade-setups)). It prints a summary of the result and then where the files are and, with `--open`, shows the chart in your default image viewer. `--horizon` and the other step-1 options work here too (the default horizon is `1-3m`). Step 2 takes about 2 to 6 minutes (longest with `--research deep`) and is the only Claude Code call; the trade-setups step is computed in under a second. Progress goes to standard error. Pass `--no-entries` to stop at the chart. `uv run python run_pipeline.py ...` is the same command. To stop typing the same options on every run, see [Your defaults](#your-defaults).

The run ends with what it found, so no file has to be opened to see it (this is the committed example, `examples/petr4_*.json`):

```text
PETR4.SA  42.77 BRL · 18 Nov 2026 15:15 · 1–3 months to 12 Feb 2027 · trend down

Zones            range  strength  reach  turn  ░ reach █ turn
  above    above 52.71              17%   17%  ███
  R3     50.24 – 52.71  moderate    31%   14%  ███░░░
  R2     47.11 – 49.60  strong      57%   26%  █████░░░░░░
  R1     43.23 – 45.69  strong      80%   23%  █████░░░░░░░░░░░
  ── price 42.77 ───────────────────────────────────────────────
  S1     40.65 – 42.52  strong      85%   18%  ████░░░░░░░░░░░░░
  S2     37.40 – 38.80  strong      67%   28%  ██████░░░░░░░
  S3     35.04 – 36.85  moderate    39%   16%  ███░░░░░
  below    below 35.04              23%   23%  █████

Scenarios
  Breakdown  37%  ███████  below 37.40
  Breakout   29%  ██████   above 49.60
  Range      32%  ██████   neither
  Whipsaw     2%           both

Hypothetical setups · valid until 12 Feb 2027
  #  id                     side   order  entry   stop  target
  1  short-S1-continuation  short  stop   40.65  43.22   37.40
  2  long-S2-reversal       long   limit  38.80  36.70   43.23
  3  long-R1-continuation   long   stop   45.69  42.53   49.60
  #  reward/risk  fill  win_if_filled  edge per order  quality
  1         1.26   67%            58%            0.21  favourable
  2         2.11   67%            38%            0.12  marginal
  3         1.24   57%            54%            0.12  favourable
  Hypothetical setups for study, derived mechanically from uncalibrated
  probabilities. Not a recommendation to buy or sell.

Events
  21 Dec 2026 ex-dividend: price drops by the payout on the ex-date; the amount…
Confidence: low
  • S1's floor is 5% below the price, less than one week's move at the recent 8…
  • 21 Dec ex-dividend: amount unknown, so the drop is not simulated

input:      runs/PETR4.SA/2026-11-18T1515_1-3m/input.json
projection: runs/PETR4.SA/2026-11-18T1515_1-3m/projection.json
chart:      runs/PETR4.SA/2026-11-18T1515_1-3m/chart.png
entries:    runs/PETR4.SA/2026-11-18T1515_1-3m/entries.json
saved:      projections/PETR4.SA/2026-11-18T1515_1-3m
```

The summary reads top to bottom like the chart: resistances above the current price, supports below, each with its `reach` and `turn` (see the [Glossary](#glossary)); then the scenarios, the hypothetical trade setups with the note of their file, the events inside the horizon, the confidence level with its first limits, and the drivers. Every number and sentence comes from the JSON files, which hold the rest (evidence, sources, the reasoning of each setup). Its labels follow the chart's language, the columns fold into a second block on a narrow terminal, and colour is used only when the output is a terminal (never when piped or with `NO_COLOR` set). `--no-summary` leaves only the file paths. `show_run.py` prints the same for any run, old or new:

```bash
uv run python show_run.py runs/PETR4.SA/2026-11-18T1515_1-3m           # a run folder, or one under projections/
uv run python show_run.py examples/petr4_input.json examples/petr4_projection.json [ENTRIES.json]
```

It takes `--lang CODE` (default: the input's `reply.language`), `--width N` (default: the terminal's width, at most 100; 80 when piped) and `--no-color`.

Afterwards the same command lists, scores, redraws and recomputes what the runs left behind (`zones list`, `zones score`, `zones plot PETR4.SA`, ...): see [Commands](#commands).

Each run gets a new folder under the current directory (or under `runs_dir`, see [Your defaults](#your-defaults)):

```
runs/
└── PETR4.SA/
    ├── 2026-11-18T1515_1-3m/
    │   ├── input.json
    │   ├── projection.json
    │   ├── chart.png
    │   └── entries.json
    └── 2026-11-18T1515_1-3m_2/     the same input run a second time
```

The timestamp is the input's `now.time` (the last candle, in the exchange's local time), followed by the horizon, so folders sort by date. Nothing is overwritten: a name already taken gets `_2`, `_3`, ... A run that fails in step 2 leaves its folder with `input.json` only, ready for `--input`. `runs/` is in `.gitignore` and is only this command's output: delete any of it at any time. The record used for scoring later is the separate [`projections/`](#saved-projections) folder.

With `--out-dir DIR` the files go exactly into `DIR` instead, named after the ticker, lowercased and without the exchange suffix (`petr4_input.json`, `petr4_projection.json`, `petr4.png`, `petr4_entries.json`), and the next run of that ticker replaces them. `--out-dir .` is the behaviour of earlier versions.

While Claude works, each stage of the skill gets one line with the time elapsed, and the step ends with the duration, the number of turns and the cost:

```text
[2/5] Projection: Claude Code runs the skill (research: none). This can take several minutes; timeout 1800 s
      reading the skill's instructions                    0:02
      reading the input                                   0:04
      measuring the candles (rz.py levels)                0:08
      ... still running                                   0:39
      choosing zones                                      0:58
      simulating price paths (rz.py sim)                  1:02
      writing the projection                              2:30
      validating (rz.py check)                            2:33
      done in 2:39 · 9 turns · $0.80
```

A stage that happens again says so (`adjusting the zones`, `simulating again (rz.py sim, run 2)`). With web research each search shows its query and each page its host, never the content. `... still running` appears after 30 seconds without a new line. Turns and cost are the figures Claude Code reports for the run, and either is left out when it reports none. `--quiet` drops the stage lines and keeps the step headers, errors and the last line.

| Option | Default | Meaning |
|---|---|---|
| `TICKER` and any `build_prompt.py` option | | Passed to step 1 untouched (`--horizon`, `--research`, `--event`, `--language`, ...). See [Step 1](#step-1-build_promptpy). |
| `--out-dir DIR` | a new `runs/TICKER/TIME_HORIZON/` | Put the files exactly in `DIR`, as `STEM_input.json`, `STEM_projection.json`, `STEM.png` and `STEM_entries.json`. Files given with `--input` and `--projection` stay where they are. |
| `--name STEM` | none in a run folder, the ticker with `--out-dir` | File name stem. In a run folder it turns `input.json`, `projection.json`, `chart.png` into `STEM_input.json`, `STEM_projection.json`, `STEM.png`. |
| `--input FILE` | none | Use an existing input JSON and skip step 1 (no download). The run folder gets a copy. |
| `--projection FILE` | none | Use an existing projection JSON and skip step 2. Needs `--input`. The run folder gets a copy. |
| `--chart FILE` | `chart.png` in the run folder, `STEM.png` with `--out-dir` | Chart file. The extension picks the format. |
| `--open` | off | Open the chart in the default viewer when the run succeeds. A viewer that cannot be started is only a warning. |
| `--no-summary` | off | Print only the file paths, without the summary of the result before them. A summary that cannot be made is only a warning. |
| `--engine NAME` | `claude` | Who makes the projection in step 2: `claude` (the skill through Claude Code: paid, minutes, can do web research) or `mechanical` (`mechanical.py`: fixed rules, no Claude Code call, about a second, no research). See [Engines](#engines-claude-or-mechanical). |
| `--model NAME` | Claude Code's own | Model for the headless run. |
| `--timeout SECONDS` | `1800` | Give up on each Claude Code call after this long. |
| `--no-entries` | off | Stop at the chart: skip the trade-setups step. |
| `--entries-by-claude` | off | Have Claude write the sentences of the trade setups: a second Claude Code call of a few minutes. The numbers are the same. |
| `--quiet` | off | No line per stage during step 2. Headers, errors and the duration and cost line stay. |
| `--theme`, `--lang`, `--candles`, `--dpi` | step 3's own | Passed to `plot_projection.py` only when given. |
| `--no-save` | off | Do not keep the new projection under `projections/` (see [Saved projections](#saved-projections)). |
| `--runs-dir DIR` | `runs/` in the current directory | Where the run folders are made. |
| `--history-dir DIR` | `$RZ_PROJECTIONS_DIR`, else `projections/` in the current directory | Where new projections are kept. |
| `--show-config`, `--init-config`, `--no-config` | | Print the settings and their sources; write a template config file; ignore the config files. See [Your defaults](#your-defaults). |

Every default in this table except the per-run files (`--out-dir`, `--name`, `--input`, `--projection`, `--chart`) can be changed once in a config file: see [Your defaults](#your-defaults). A switch that a file turns on has a `--no-` form for one run (`--no-open`, `--no-quiet`, `--no-volume`, ...), and `--entries` and `--save` undo a file's `entries = false` and `save = false`.

```bash
RUN=runs/PETR4.SA/2026-11-18T1515_1-3m
uv run zones --input $RUN/input.json                                         # project and plot again, no download
uv run zones --input $RUN/input.json --projection $RUN/projection.json       # plot again only
uv run zones --input $RUN/input.json --projection $RUN/projection.json --chart dark.svg --theme dark
```

The first two make a new, complete run folder each (`..._2`, then `..._3`), so the earlier chart stays. The third writes only `dark.svg`: plotting again into a named `--chart` creates no folder. To redraw the chart of a run in place, without a new folder, use `zones plot PETR4.SA` (see [Commands](#commands)).

With `--engine mechanical`, step 2 prints instead:

```text
[2/5] Projection: mechanical engine, no Claude Code call (backtest.py's zone rule, unadjusted rz.py sim)
      made in 1.1 s · no Claude Code call · $0
```

Step 2 runs `claude -p` in this repository without bypassing permissions. Claude gets only the skill, the helper script and a throwaway work folder to write in (it can also read files of this repository), plus web search and page fetching when `--research` is not `none`; anything else is denied.

With research on (the default, `one_search`), Claude opens each page before citing it. That is the skill's own rule (see [What the skill does](#what-the-skill-does)) and the wrapper's prompt repeats it: a driver states only what its `source` page says, and a dividend amount or implied move read from an opened page goes into the simulation as a shock. Pages that refuse to load (some sites answer 403) are skipped, so the drivers come from the pages that did open.

The same thing by hand, one step at a time:

```bash
# 1. Candles and request for Petrobras, 1 to 3 month horizon
uv run python build_prompt.py PETR4.SA --horizon 1-3m -o petr4_input.json

# 2. Give petr4_input.json to Claude (paste or attach it).
#    The skill replies with the projection JSON. Save it as petr4_projection.json.

# 3. Chart
uv run python plot_projection.py petr4_input.json petr4_projection.json -o petr4.png
```

To try step 3 without Claude, use the ready-made pair in [`examples/`](examples/):

```bash
uv run python plot_projection.py examples/petr4_input.json examples/petr4_projection.json -o chart.png
```

## Your defaults

Preferences you would otherwise type on every run can live in a file. With this one, plain `zones PETR4.SA` asks for a projection in Portuguese with no web research, uses the `sonnet` model, opens the chart, and keeps its folders in one place whatever directory you run it from:

```toml
# ~/.config/zones/config.toml
language = "pt"            # the projection's text and the chart's labels
research = "none"
model = "sonnet"
open = true

runs_dir = "~/zones/runs"                # one folder per run
history_dir = "~/zones/projections"      # the saved projections, the record used for scoring
```

```bash
zones --init-config     # write a template with every setting commented out (never replaces a file)
zones --show-config     # the files found, and every setting with its value and where it comes from
zones PETR4.SA --no-config          # ignore the files for this run
```

**Files.** The user file is `$XDG_CONFIG_HOME/zones/config.toml`, by default `~/.config/zones/config.toml` on every platform (macOS and Windows included, so there is one path to remember). `ZONES_CONFIG=FILE` names another file instead. A `zones.toml` in the current directory, and only there (parent folders are not searched), is the project file: handy for a folder of US tickers that wants `language = "en"`. A file that does not exist is skipped. A run that used a file says so in its first line, with the settings taken from it:

```text
Config: ~/.config/zones/config.toml (language, research, model, open, runs_dir, history_dir)
```

**Precedence.** Command line > environment variable > project file (`./zones.toml`) > user file > built-in default. Two settings have an environment variable: `history_dir` (`RZ_PROJECTIONS_DIR`) and `cache_dir` (`ZONES_CACHE_DIR`).

**Settings.** A key is the option's name with underscores (`--max-rows` is `max_rows`); every key is top-level, without `[tables]`.

| Key | Built-in default | Same as |
|---|---|---|
| `language`, `horizon`, `research`, `supports`, `resistances`, `volume`, `max_rows`, `shape`, `max_words`, `links`, `decimals` | see [Step 1](#options) | the `build_prompt.py` option of that name |
| `engine` | `"claude"` | `--engine`; `"mechanical"` makes every run free and Claude-free unless `--engine claude` is typed |
| `model`, `timeout`, `quiet`, `entries_by_claude` | Claude Code's own, `1800`, `false`, `false` | `--model`, `--timeout`, `--quiet`, `--entries-by-claude` |
| `entries`, `save` | `true`, `true` | `false` is `--no-entries`, `--no-save` |
| `theme`, `lang`, `candles`, `dpi` | [step 3](#step-3-plot_projectionpy)'s own | the chart options; `lang` overrides `language` for the chart's labels only |
| `open` | `false` | `--open` |
| `summary` | `true` | `--summary` (`--no-summary` for one run) |
| `runs_dir` | `runs/` in the current directory | `--runs-dir DIR` |
| `history_dir` | `$RZ_PROJECTIONS_DIR`, else `projections/` in the current directory | `--history-dir DIR` |
| `cache_dir` | `$ZONES_CACHE_DIR`, else `$XDG_CACHE_HOME/zones`, else `~/.cache/zones` | `--cache-dir DIR`; the [candle cache](#the-candle-cache) and the shared rate-limit file |

What belongs to one run stays on the command line: the ticker, `--event`, `--prices`, `--no-cache`, `--input`, `--projection`, `--name`, `--chart`, `--out-dir`. There are no per-ticker settings.

For a daily scan with no Claude cost, put `engine = "mechanical"` in the file and type `--engine claude` for the runs that should get the skill and its research.

**One run differently.** Whatever is typed wins, e.g. `zones NVDA --language en --research deep`. A switch that a file turns on has an off form for one run: `--no-open`, `--no-quiet`, `--no-entries-by-claude`, `--no-volume`; one that a file turns off has an on form: `--entries`, `--save`, `--summary`.

**The two folders.** `runs_dir` is where the run folders are made (`runs_dir/TICKER/TIME_HORIZON/`), instead of `runs/` under the current directory; `--out-dir` still places one run's files exactly. `history_dir` is where each new projection is kept for [scoring](#scoring-the-saved-projections): `zones` passes it to `rz.py save` as `RZ_PROJECTIONS_DIR`, unless that variable is already set. The [commands](#commands) (`zones list`, `zones runs`, `zones score`, a ticker as WHAT) look in the same two folders, and `zones plot`/`zones show` use the file's chart settings (`theme`, `lang`, `candles`, `dpi`, `open`) unless typed. `rz.py` and `score_projections.py` run by hand do not read the config file: give them the folder, or export `RZ_PROJECTIONS_DIR`. In a file, `~` and `$VARIABLES` expand and a relative path starts at the file's own folder, not at the current directory, so the file means the same wherever you run `zones`.

**Mistakes.** A key the command does not know, a value of the wrong type or outside its choices, or a file that is not valid TOML stops the run before anything is downloaded, with one line naming the file and the key:

```text
Error: /home/you/.config/zones/config.toml: unknown setting 'langauge' (did you mean 'language'?). `zones --show-config` lists them all.
```

## Commands

`zones` is one command for the run and for what comes after it. Inside the repository write `uv run zones ...`; after the global install, plain `zones ...` in any directory.

| Command | What it does | Runs |
|---|---|---|
| `zones TICKER [options]` | The pipeline of [Quick start](#quick-start). `zones run TICKER` is the same. | all steps |
| `zones list [TICKER]` | The [saved projections](#saved-projections), newest last, each `open` or `ended`. | `rz.py list` |
| `zones runs [TICKER]` | The run folders under `runs/` in the current directory, newest last, with the files each one holds. | nothing else |
| `zones score [TICKER \| FOLDER]` | Scores the saved projections against later prices. Downloads from Yahoo Finance. | `score_projections.py` |
| `zones score-entries [TICKER \| FOLDER] [options]` | [Scores the saved trade setups](#scoring-the-saved-setups) (`entries.json`) against later prices: stated odds against what happened. Downloads from Yahoo Finance. | `score_entries.py` |
| `zones plot WHAT [plot options]` | Draws the chart of a run again. | `plot_projection.py` |
| `zones show WHAT` | Prints the summary of a run. | `show_run.py` |
| `zones entries WHAT` | Computes the [trade setups](#ranked-trade-setups) of a run again and checks them. | `trade_entries.py build`, then `check` |
| `zones backtest TICKER... [options]` | [Backtests the baseline probabilities](#backtest-of-the-baseline) on past dates. Downloads from Yahoo Finance; no Claude call. | `backtest.py` |
| `zones backtest-entries TICKER... [options]` | [Backtests the trade setups](#backtest-of-the-trade-setups) of the mechanical engine on past dates: stated odds against what the price did. Downloads from Yahoo Finance; no Claude call. | `backtest_entries.py` |
| `zones batch FILE\|TICKER... [options]` | One pipeline run per ticker of a watchlist, a few at a time, with a cost cap and a `summary.csv`. See [Batch runs](#batch-runs). | the pipeline, once per ticker |
| `zones cache [info\|clear\|path] [TICKER...]` | The [candle cache](#the-candle-cache): what it holds, delete it (all or some tickers), or print its folder. | `data_source.py` |

```bash
zones list                                   # every saved projection
zones score PETR4.SA --json                  # one ticker, as JSON
zones score-entries --runs                   # the saved setups, and those of runs/ here
zones plot PETR4.SA                          # redraw the latest run's chart.png
zones plot nvda --theme dark --lang pt -o nvda_dark.png
zones plot PETR4.SA --horizon 1-2w --open    # the latest 1-2w run, then open the chart
zones entries runs/PETR4.SA/2026-11-18T1515_1-3m
zones plot --help                            # each command has its own help
```

**Command or ticker.** The first argument is a command only when it is exactly one of the lowercase words above. Anything else goes to the pipeline as before, so `zones nvda` still runs the pipeline for NVDA (step 1 upper-cases the ticker), and so do `zones --input FILE` and `zones --help`. For a ticker spelled like a command, write it in capitals (`zones LIST`) or put it after `run` or `--` (`zones run list`, `zones -- list`).

**WHAT** names one run, and comes first, before the options:

- A folder that holds `input.json` and `projection.json`: a run folder or a saved projection. A folder of `--out-dir` files (`petr4_input.json`, `petr4_projection.json`) works too; if it holds several pairs, pick one with `--name STEM`. A folder with run folders inside it, such as `runs/PETR4.SA`, means the latest of them.
- A ticker, in any letter case: its latest run under `runs/` in the current directory or, when there is none, its latest saved projection (`$RZ_PROJECTIONS_DIR`, else `./projections`). `--horizon H` narrows it to one horizon. "Latest" is read from the folder names (the time, then `_2`, `_3`, ...), and the folder chosen is printed.
- When nothing matches, the error names both places it looked. An existing folder wins over a ticker of the same name.

**Options are passed on.** Options a command does not declare go to the script it runs: `zones score --json`, `zones plot NVDA --theme dark --dpi 200 --no-check`, `zones show NVDA --lang pt`. The exit code is the script's. Relative paths are taken from the directory you are in.

**Where `plot` and `entries` write.** For a run folder they replace the folder's own `chart.png` or `entries.json`: `runs/` is working output and both files follow from the pair. For a saved projection they write `TICKER_FOLDER.png` or `TICKER_FOLDER_entries.json` (for example `PETR4.SA_2026-11-18T1515_1-3m.png`) in the current directory, because the saved history is append-only and nothing is added to it. `-o FILE` chooses the file in either case, and a file inside the saved history is refused. `entries` builds and checks the setups before replacing anything.

`zones list` and `zones runs` show two different things: `projections/` is the record kept for scoring, `runs/` is this command's output in the current directory.

## Batch runs

`zones batch` runs the pipeline once per ticker of a watchlist, for covering many tickers in one go:

```bash
zones batch watchlist.txt                                  # every ticker in the file
zones batch watchlist.txt NVDA AAPL --research none --jobs 3 --max-cost 5
zones batch --retry-failed runs/batch/2026-11-19T153725    # only what failed or was skipped there
```

```text
# watchlist.txt: one ticker per line, optionally with a horizon; # starts a comment
PETR4.SA
VALE3.SA 1-2w      # this one weekly
NVDA
```

**What runs.** Each ticker is an ordinary `zones run TICKER [options]` in a process of its own, so a ticker that fails (no data from Yahoo, a Claude Code timeout, a failed `rz.py check`) never stops the others, and its run folder lands exactly where a single run puts it, under `runs_dir`. Files and tickers come first; a word that names an existing file is a watchlist, any other word is a ticker, and a ticker listed twice with the same horizon runs once. Every option `zones batch` does not declare goes to each run as if typed there (`--horizon`, `--research`, `--model`, `--timeout`, `--no-entries` ...), and the [config files](#your-defaults) apply as they do to one run. A horizon on a watchlist line wins over `--horizon` for that ticker. The options that name one run's files (`--input`, `--projection`, `--out-dir`, `--name`, `--chart`) are refused, and each run gets `--no-open` unless you type `--open`. `--prices DIR` works when the folder holds one `TICKER.csv` or `TICKER/` per ticker ([CSV files](#csv-files-your-own-data)); one CSV file for several tickers is refused.

| Option | Default | Meaning |
|---|---|---|
| `--jobs N`, `-j N` | `2` | Runs at the same time. Each run mostly waits on Claude Code, so two roughly halve the wall time; more runs at once mean more Claude Code sessions sharing your usage limits. Yahoo requests are spaced across all the runs by the [shared rate limit](#yahoo-finance-retries-timeout-rate-limit), so more jobs do not burst Yahoo. |
| `--max-cost USD` | none | Once the costs Claude Code reported add up to this, start no new run: the ones running finish, the rest are listed as `skipped`. With `--jobs N` the total can pass the cap by what the last N-1 runs cost. |
| `--retry-failed BATCH_FOLDER` | none | Run again the tickers whose row in that batch's `summary.csv` is not `ok`, each with the horizon its row shows. Options are not remembered: type them again. A new batch folder is made. |
| `--runs-dir DIR` | `runs_dir` | Where the batch folder goes, and passed on to each run. |

**Progress.** One line per ticker on standard error when it starts and when it ends, with its status, duration and cost; the stage lines of each run go to its log instead. After a minute without a line, one line names the runs still going. The end is a table on standard output:

```text
Batch of 3 tickers, 3 at a time: runs/batch/2026-11-19T153736
  [1/3] NODATA1   started
  [2/3] SLOWX     started
  [3/3] ITUB4.SA  started
  [1/3] NODATA1   failed   0:00     $0.00  build_prompt.py failed: No price data found for NODATA1
  [3/3] ITUB4.SA  ok       0:01     $0.75  runs/ITUB4.SA/2026-11-17T1736_1-3m
  [2/3] SLOWX     failed   0:04    cost ?  Claude Code did not finish within 4 s (raise it with --timeout).
ticker    status  price  support (reach/turn)   resistance (reach/turn)  top scenario   conf.  cost   time
--------  ------  -----  ---------------------  -----------------------  -------------  -----  -----  ----
NODATA1   failed  -      -                      -                        -              -      $0.00  0:00
SLOWX     failed  -      -                      -                        -              -      ?      0:04
ITUB4.SA  ok      44.06  43.2-44.6 (1.00/0.20)  45.3-47.1 (0.69/0.18)    breakdown 0.5  low    $0.75  0:01

NODATA1: failed: build_prompt.py failed: No price data found for NODATA1  (log: runs/batch/2026-11-19T153736/logs/NODATA1.log)
SLOWX: failed: Claude Code did not finish within 4 s (raise it with --timeout).  (log: runs/batch/2026-11-19T153736/logs/SLOWX.log)

3 tickers: 1 ok, 2 failed, 0 skipped; reported cost $0.75, plus an unknown cost for SLOWX.
summary:    runs/batch/2026-11-19T153736/summary.csv
Analysis for study, not investment advice.
```

**A free batch.** `zones batch watchlist.txt --engine mechanical --jobs 3` passes the engine to every run, so no run calls Claude: each row shows `$0.00` with `cost_note` "no Claude Code call", and the time per ticker is mostly the Yahoo download (about 15 s each with an empty candle cache; six US and B3 tickers took 34 s at `--jobs 3`). `engine = "mechanical"` in a config file does the same.

**Cost.** The cost of a run is the sum of what Claude Code reported for its calls, as the run printed it (to the cent). A run that made no Claude Code call (the mechanical engine, or a run that failed at step 1) cost nothing. A call that reported no cost, or never finished (a timeout), makes the run's cost unknown: the table shows `?`, `summary.csv` leaves `cost_usd` empty and says why in `cost_note`, and the cap does not count it. Nothing is estimated.

**The batch folder**, `RUNS_DIR/batch/TIME/`, holds `logs/TICKER.log` (everything one run printed) and `summary.csv`, rewritten after every run so a batch cut short still has one. Its columns: `ticker`, `horizon`, `status` (`ok`, `failed` or `skipped`), `run_folder`, `as_of`, `price`, `currency`; the nearest support and resistance with both edges (the open tails are left out) as `support_from`, `support_to`, `support_reach`, `support_turn` and the same for `resistance_`; `top_scenario` and `top_scenario_p` (the most likely scenario), `confidence`, `cost_usd`, `cost_note`, `duration_s`, `error` (the run's own `Error:` line) and `log`. A run that failed after making its folder still points at it, with whatever its files hold.

**Exit code.** `0` when every ticker succeeded, `1` when any failed or was skipped, `130` after Ctrl-C (the runs going are stopped, and the summary is written all the same). Like a single run, each row is analysis for study, not investment advice.

## Step 1: build_prompt.py

Downloads candles from Yahoo Finance (or reads them from [CSV files](#csv-files-your-own-data)) and writes one JSON holding the data and the request.

```bash
uv run python build_prompt.py TICKER [options]
```

`TICKER` is a Yahoo symbol, for example `NVDA`, `PETR4.SA` or `BTC-USD`. Without `-o` the JSON goes to standard output.

### Options

When the step runs through `zones`, a [config file](#your-defaults) can set any of these except `--event` and `-o`.

| Option | Default | Meaning |
|---|---|---|
| `--horizon` | `1-3m` | `today`, `1-2w`, `1-3m` or `6-12m`. Picks the candle sizes and what "probability" means (table below). |
| `--max-rows N` | preset | Cap the rows of the main series to shrink the file. `52` is enough for most uses. |
| `--volume` | off | Add a volume column to every row. |
| `--event 'DATE: TEXT'` | none | Add or refine an event, e.g. `--event "2026-11-17: Q3 earnings, after close"`. Repeatable. Replaces Yahoo's entry for the same date. |
| `--supports N` | `3` | How many support zones to ask for. |
| `--resistances N` | `3` | How many resistance zones to ask for. |
| `--research` | `one_search` | How much web research the skill may do: `none`, `one_search`, `deep`. |
| `--shape` | `table` | Shape of the prose that follows the projection: `table` or `list`. |
| `--max-words N` | `200` | Word limit for that prose. |
| `--language` | `en` | Language of the whole result: the free-text values in the projection and, in step 3, the chart's labels. The chart has labels for `pt` and `en`. |
| `--links` | `decisive_only` | Links in the prose: `none`, `decisive_only`, `all`. |
| `--decimals N` | `2` | Price rounding. |
| `--prices FILE.csv\|DIR` | none | Read the candles from [CSV files](#csv-files-your-own-data) instead of Yahoo: no network at all. |
| `--no-cache` | off | Download every candle again instead of reusing finished ones from the [candle cache](#the-candle-cache). |
| `-o FILE` | stdout | Write to a file. |

### Horizon presets

The horizon decides which series are embedded. The **main** series is what levels are read from. The **context** series is coarser and covers only the history *before* the main series starts, so the two never repeat each other.

| `--horizon` | Main series | Context series | Close basis | Probability |
|---|---|---|---|---|
| `today` | 15-minute, last 5 sessions (up to 140 rows) | daily, up to 40 rows | `15m` | `touched_once` |
| `1-2w` | daily, up to 90 rows | weekly, up to 52 rows | `1D` | `close_in_zone` |
| `1-3m` | weekly, up to 104 rows | monthly, up to 36 rows | `1W` | `close_in_zone` |
| `6-12m` | monthly, up to 120 rows | none | `1M` | `close_in_zone` |

### Output: the input JSON

Abridged example (real files have many more rows):

```json
{
  "task": "reversal_zones",
  "schema_version": 1,
  "asset": { "ticker": "PETR4.SA", "venue": "São Paulo", "currency": "BRL" },
  "now": { "time": "2026-11-17T17:36-03:00", "price": 44.06, "session": "after_hours" },
  "horizon": "1-3m",
  "data": {
    "timezone": "America/Sao_Paulo",
    "split_adjusted": true,
    "columns": ["start", "open", "high", "low", "close"],
    "series": [
      { "candles": "1M", "last_row": "closed",
        "rows": [["2024-10-01", 36.01, 38.38, 35.34, 35.91],
                 ["2024-11-01", 36.2, 39.86, 35.03, 38.9]] },
      { "candles": "1W", "last_row": "forming",
        "rows": [["2026-11-09", 44.6, 45.69, 43.23, 45.3],
                 ["2026-11-16", 45.0, 45.39, 43.65, 44.06]] }
    ]
  },
  "events": [{ "date": "2026-12-21", "what": "ex-dividend" }],
  "want": { "supports": 3, "resistances": 3, "probability": "close_in_zone", "close_basis": "1W" },
  "research": "one_search",
  "reply": { "shape": "table", "max_words": 200, "language": "en", "links": "decisive_only" },
  "provenance": {
    "source": "Yahoo Finance (yfinance)",
    "library": "yfinance 1.8.0",
    "downloaded_at": "2026-11-17T20:36:02Z",
    "auto_adjust": false,
    "series": [
      { "candles": "1M", "rows": 36, "first": "2021-12-01", "last": "2024-11-01", "interval": "1mo", "period": "5y", "max_rows": 36 },
      { "candles": "1W", "rows": 104, "first": "2024-11-25", "last": "2026-11-16", "interval": "1wk", "period": "2y", "max_rows": 104 }
    ],
    "data_sha256": "cd64501d971e80bccb606a6df414c5ceeb77852f48c1428e92e1839c43e5ceb3"
  }
}
```

| Field | Notes |
|---|---|
| `schema_version` | Version of this input format, `1`. Inputs written before it existed have no such key (and no `provenance`); every script still reads them. |
| `now.session` | `open`, `closed`, `pre_market` or `after_hours`, from Yahoo's trading-period data. |
| `data.columns` | Shared by every series. Rows are compact arrays, oldest first, to keep the file small. |
| `series[].last_row` | `forming` when the newest candle is still being built, otherwise `closed`. A forming candle is not a signal yet. |
| `data.split_adjusted` | Prices are adjusted for splits but **not for dividends**, which matches what most charting platforms show. |
| `events` | Earnings and ex-dividend dates inside the horizon, from Yahoo's calendar, plus anything passed with `--event`. An empty list means none are known. |
| `want.probability` | `close_in_zone`: judged on candle closes. `touched_once`: judged on intra-candle extremes. |
| `provenance` | Where the candles came from, for the record: the source and library version, when they were downloaded (UTC), `auto_adjust: false` (split-adjusted, not dividend-adjusted), and per series (in the order of `data.series`) the rows kept, the first and last row's `start`, and what was requested from Yahoo. A series entry also has `checks` when the [row checks](#row-checks) dropped, fixed or flagged rows, `cache` when finished candles came from the [cache](#the-candle-cache) (how many of the downloaded rows, when they were first downloaded, when they were checked against Yahoo again), and `note` when a stale cache was replaced; none of these keys means the candles were downloaded in this run and needed nothing. From [CSV files](#csv-files-your-own-data), `source` names them, `files` lists each with its sha256, `read_at` replaces `downloaded_at`, `adjustment` replaces `auto_adjust`, and a series built from daily rows says `built_from: "1d"`. `data_sha256` is the sha256 of `data` in canonical JSON (sorted keys, no spaces), so an edited candle shows: `build_prompt.data_hash(json.load(f)["data"])` must equal it. The skill ignores this block. |

## Data sources: Yahoo, the cache, CSV files

Every price request of `build_prompt.py`, `backtest.py` and `score_projections.py` goes through `data_source.py`. It retries what can be retried, keeps finished candles in a local cache, checks every row, and can read your own CSV files instead.

### Yahoo Finance: retries, timeout, rate limit

| | What happens |
|---|---|
| **Transient failure** (HTTP 429, 5xx, a timeout, a dropped connection, Yahoo's "will be right back") | Retried up to 4 times, after 2, 4, 8, 16 s (5, 10, 20, 30 s after a 429), each wait cut to a random 50-100% so parallel runs do not retry in step, and never past 120 s in all. A warning line names each retry; after the last one the run stops with one line: `Yahoo kept failing for NVDA 1wk (YFRateLimitError: ...); gave up after 5 attempt(s) in 63 s`. |
| **Permanent failure** (unknown symbol, delisted, 404, no candles in the range) | Not retried. The script prints its usual message at once, e.g. `No data for 'ZZZZ'. Check the Yahoo symbol.` yfinance is told to raise its errors instead of returning an empty table, so a timeout is never mistaken for an unknown symbol. |
| **Timeout** | Each attempt is abandoned after `ZONES_YAHOO_TIMEOUT` seconds (default 30), even when Yahoo keeps the connection open, and counts as a transient failure. |
| **Rate limit** | Requests from all processes on the machine start at least `ZONES_YAHOO_SPACING` seconds apart (default 0.5). The next free slot is kept in `yahoo.next` in the cache folder, read and moved on under an operating-system file lock (`fcntl` on macOS and Linux, `msvcrt` on Windows), which the system releases if a process dies, so nothing goes stale. A process reserves its slot, lets go of the lock and then waits, so `zones batch --jobs N` spreads its requests out. If the file cannot be used, requests are not delayed. |

The spacing counts the scripts' own calls; one yfinance call can make a few HTTP requests (cookie, timezone, data). All Yahoo calls of a process run on one reused worker thread, because yfinance's HTTP session redoes its handshake (about 5 s, measured) on every new thread. The first request of every process still pays that handshake once, which is most of step 1's time.

### The candle cache

**Where.** `$ZONES_CACHE_DIR`, else `$XDG_CACHE_HOME/zones`, else `~/.cache/zones` on every platform; `cache_dir` in a [config file](#your-defaults) or `zones --cache-dir DIR` sets it for `zones` and everything it starts. One JSON file per key under `candles/TICKER/`: `INTERVAL-split.json` (split-adjusted, not dividend-adjusted), with `-actions` for score_projections.py's requests, which also keep the split dates.

**What is cached.** Only finished daily, weekly and monthly candles: a candle is stored once it ended at least a day before the download. Intraday (15-minute, hourly) candles are never cached, and the first request of step 1, which reads the session and the latest price, is never cached.

**How it is used.** When the cache covers the requested range, Yahoo is still asked, but only for the candles since the fifth-newest cached one. Those five must match the cache to within a millionth (prices, not volume, which Yahoo revises): a split rescales the whole history, so a mismatch discards the cache and downloads the full range again, with a warning and a `note` in the provenance. Everything from that fresh answer is used as it came, the forming candle and today's session included, so a live run never shows a stale price. Checked against Yahoo, the result equals a full download for all live horizons, a backtest and a score run. With a `period` request, rows older than the period are cut by date from now, so the oldest row of a series shorter than its row cap could differ from Yahoo's own cut by one candle.

**Speed.** The cache saves payload, not requests: each series still costs one request. A 13-year daily download took about 1.0 s, its cached form 0.5 s. Step 1 is dominated by yfinance's first handshake either way.

```bash
zones cache                  # where it is and what it holds (rows, dates, last checked)
zones cache clear            # delete it; `zones cache clear PETR4.SA` deletes one ticker
zones PETR4.SA --no-cache    # one run without it (also backtest.py and score_projections.py --no-cache)
```

### Row checks

Every frame, from Yahoo, the cache or a CSV file, is checked before it is used. Each action becomes one warning line and, in step 1, an entry in the series' `provenance.checks` (`issue`, `action`, `rows`, the first affected `start`). The checks cover all the rows received, including rows later cut by the period or the row cap.

| Issue | Action |
|---|---|
| rows out of time order | sorted |
| a missing open, high, low or close | row dropped (Yahoo gives such rows now and then) |
| the same time twice | the earlier rows dropped, the last kept |
| a price of zero or less | row dropped |
| high below low | high and low swapped |
| open or close outside high-low | high and low widened to include them |
| a gap longer than expected (10 days for daily candles, 22 for weekly, 65 for monthly, 5 for intraday) | kept, reported |
| a close-to-close move of 1.8x or more within 4% of a whole ratio (2:1, 3:1, 1:2 ...) | kept, reported: it may be an unadjusted split, or a real move; check the data |
| more than 20% of the rows dropped (and more than 3) | the data is not used: the run stops |

A missing volume is left as it is (written as 0 with `--volume`).

### CSV files: your own data

`--prices` reads licensed or your own candles instead of Yahoo, with no network at all: `build_prompt.py PETR4.SA --prices data/petr4.csv`, `zones PETR4.SA --prices data/`, `zones batch watchlist.txt --prices data/`.

**Format.** A header row, then one candle per row, in any order of time. Columns in any order and letter case: a time column named `date`, `datetime`, `time`, `timestamp` or `start`, then `open`, `high`, `low`, `close`, and optionally `volume`; other columns (an adjusted close, say) are ignored. Comma, semicolon or tab separated, a decimal point, no thousands separators; lines starting with `#` are skipped. Times are ISO dates (`2026-11-17`) or date-times (`2026-11-17 10:15`, `2026-11-17T10:15-03:00`); a time without an offset is exchange time. A weekly row is labelled by its week's Monday, a monthly row by its 1st. Prices should be split-adjusted and not dividend-adjusted, like Yahoo's in this project; nothing can check that.

```text
date,open,high,low,close,volume
2026-11-13,44.60,45.69,43.23,45.30,31250000
2026-11-16,45.10,45.80,44.90,45.52,28400000
```

**Files.** `--prices` takes a file or a folder. In a folder, for ticker `T`, the first of these that exists is used: a subfolder `T/` with one file per interval, a file `T.csv`, or files named by interval in the folder itself. A file's interval comes from the last word of its name (`1d`, `daily`, `1wk`, `weekly`, `1mo`, `monthly`, `15m`, `1h`: `1d.csv`, `petr4_1wk.csv`), else from the spacing of its rows. Weekly and monthly candles missing from the files are built from daily ones; `today` needs a 15-minute file.

**Metadata.** `FILE.json` beside a single `FILE.csv`, or `meta.json` in the folder: `{"timezone": "America/Sao_Paulo", "currency": "BRL", "venue": "B3"}`. Without it the timezone is UTC and currency and venue are `unsure`, with a warning.

**What changes in the input.** The session is `unsure` (files carry no trading hours), the price is the main series' last close, periods are counted back from the newest row, and events are only those given with `--event`. A warning says when the newest candle is more than a week old. The provenance names the files and their sha256 ([Output](#output-the-input-json)). Checked against Yahoo: daily candles exported to CSV give the same `1-2w` input as the live download, except one weekly open where Yahoo's weekly candle does not start at the first daily open.

## Step 2: the reversal-zones-projection skill

The skill lives in `.claude/skills/reversal-zones-projection/`. `SKILL.md` tells Claude how to turn an input JSON into a projection JSON, and `scripts/rz.py` is the helper that does the arithmetic.

### Installing the skill

In Claude Code there is nothing to install: the skill sits in `.claude/skills/`, so it is available whenever Claude Code runs in this repository.

For other Claude apps, add the `.claude/skills/reversal-zones-projection` folder (with its `scripts/` subfolder) as a custom skill. The copy in this repository is the source of truth; editing it does not change a skill that is already installed elsewhere.

### Using it

Paste or attach an input JSON. The skill triggers on `"task": "reversal_zones"` and replies with the projection in one `json` code block. Save that block as a file.

The skill also keeps every projection it makes, together with its input, under `projections/` (see [Saved projections](#saved-projections)).

### What the skill does

1. Runs its helper on the input to extract facts: trend, dominant swing, retracement levels, volatility, pivot highs and lows, gaps.
2. Chooses zones. Each zone needs at least two independent pieces of evidence taken from the rows, and every evidence string quotes exact prices so the zone can be audited.
3. Researches as far as `research` allows: the cause of the latest move and scheduled events inside the horizon. It opens every page it cites, so a driver states only what its `source` page says and an event's date and size are read from a page, never from a search summary or from memory. `one_search` is two searches (cause, events) plus those pages; `deep` adds follow-up searches and a second page for claims that rest on one. What it cannot confirm is left out and named in `confidence.limits`; where Claude has no web tools, the projection is made without research and says so.
4. Simulates future closes to get baseline probabilities (details [below](#how-the-probabilities-are-produced)).
5. Adjusts the baseline by a small, capped amount and records what it moved in `method`.
6. Validates the result (probabilities add up, zones do not overlap, scenarios sum to 1, every driver has a source) before replying.
7. Saves the input and the projection under `projections/`.

It does not give buy, sell or hold calls, stops, targets or position sizes.

### Output: the projection JSON

Abridged example:

```json
{
  "task": "reversal_zones",
  "schema_version": 1,
  "asset": { "ticker": "PETR4.SA", "currency": "BRL" },
  "as_of": { "time": "2026-11-17T17:36-03:00", "price": 44.06 },
  "horizon": { "label": "1-3m", "close_basis": "1W", "closes": 13, "first": "2026-11-20", "last": "2027-02-12" },
  "structure": {
    "trend": "down",
    "swing": { "low": [28.86, "2025-06-09"], "high": [59.79, "2026-10-19"] },
    "retraced": 0.51,
    "from_high": -0.263,
    "volatility": { "per": "1W", "last_8": 0.085, "last_52": 0.05, "assumed": [0.063, 0.046] }
  },
  "supports": [
    { "id": "S1", "zone": [43.2, 44.6], "strength": "strong", "reach": 1.00, "turn": 0.20,
      "evidence": ["Nov 2026 weekly lows 43.23 and 43.65", "50% retracement 44.33"] },
    { "id": "below", "zone": [null, 37.4], "reach": 0.32, "turn": 0.32,
      "evidence": ["next reference 35.48, the 78.6% retracement"] }
  ],
  "resistances": [
    { "id": "R1", "zone": [45.3, 47.1], "strength": "moderate", "reach": 0.69, "turn": 0.18,
      "evidence": ["weekly closes of 45.30 on 2 and 9 Nov"] },
    { "id": "above", "zone": [52.7, null], "reach": 0.14, "turn": 0.14,
      "evidence": ["open gap 54.23-56.91"] }
  ],
  "scenarios": [
    { "name": "breakdown", "p": 0.50, "low": "below 40.6", "high": "49.6 or lower" },
    { "name": "breakout", "p": 0.25, "low": "40.6 or higher", "high": "above 49.6" },
    { "name": "range", "p": 0.19, "low": "40.6 or higher", "high": "49.6 or lower" },
    { "name": "whipsaw", "p": 0.06, "low": "below 40.6", "high": "above 49.6" }
  ],
  "events": [{ "date": "2026-12-21", "what": "ex-dividend", "effect": "price drops by the payout", "certainty": "medium" }],
  "drivers": [{ "what": "Brent fell from about US$118 to about US$98", "pull": "down", "source": "https://..." }],
  "confidence": { "level": "low", "limits": ["S1's floor is 2% below the price"] },
  "method": "200,000 simulated paths of 13 weekly closes ..."
}
```

| Field | Meaning |
|---|---|
| `schema_version` | Version of this projection format, `1` (separate from the input's). `rz.py check` accepts a projection without it, as every projection made before it has, and rejects any other value. Which model made the projection is not in this file: see [Provenance](#provenance). |
| `zone` | `[from, to]`. `null` marks the open end of a tail entry (`below` or `above`). |
| `reach` | Chance the zone comes into play within the horizon. |
| `turn` | Chance the horizon's most extreme close is made in this zone: it is reached and does not break. This is the reversal probability. |
| `strength` | `strong`, `moderate` or `weak`, from how much independent evidence supports the zone. It is not a probability. |
| `scenarios` | Four outcomes that sum to 1, defined by whether the middle support and the middle resistance break. |
| `events[].certainty` | `high` when confirmed by the company or exchange, `medium` from a data feed, `low` when inferred from past years. |
| `confidence.level` | `low` or `medium`. Never `high`: chart levels have weak predictive power. |

On each side, `turn` values add up to the `reach` of the nearest zone, and each `reach` equals the sum of `turn` from that entry outward.

### Running the helper by hand

The helper, `.claude/skills/reversal-zones-projection/scripts/rz.py`, can be used without Claude:

```bash
RZ=.claude/skills/reversal-zones-projection/scripts/rz.py
uv run python $RZ levels input.json              # facts for choosing zones
uv run python $RZ sim input.json zones.json      # baseline probabilities (--method 1: the original, without drift)
uv run python $RZ check projection.json          # validate a projection
uv run python $RZ save input.json projection.json   # keep the pair under projections/
uv run python $RZ list                           # the saved projections, oldest first
uv run python $RZ score input.json projection.json candles.json   # what the price did, from a file of later candles
```

`zones.json` lists the zones nearest first, plus optional scheduled shocks:

```json
{
  "supports": [[43.2, 44.6], [40.6, 42.2], [37.4, 38.6]],
  "resistances": [[45.3, 47.1], [47.1, 49.6], [50.2, 52.7]],
  "shocks": [{ "date": "2026-12-21", "drop": 1.19 }, { "date": "2026-11-17", "jump": 0.08 }]
}
```

### Saved projections

Every projection is kept by default, so that a batch of past projections can later be compared with what the price did. The skill runs `rz.py save` as its last step; you can run it yourself for a projection made elsewhere.

```
projections/
└── PETR4.SA/
    ├── 2026-11-18T1515_1-3m/
    │   ├── input.json          the candles and request the projection was made from
    │   ├── projection.json     the projection, exactly as produced
    │   ├── entries.json        the trade setups, when `zones` made them
    │   └── provenance.json     which model, rules and data made it, when `zones` made it
    └── 2026-11-18T1515_1-3m_2/ the same input projected a second time
```

- The timestamp is the projection's `as_of.time`, in the exchange's local time, followed by the horizon.
- Nothing is overwritten. Projecting the same input again adds a `_2`, `_3`, ... folder.
- `save` refuses a projection that fails `check`, or whose ticker or time does not match the input.
- `zones list` (which runs `rz.py list`) prints one line per saved projection with its horizon's last date and whether that date has passed (`ended`) or not (`open`). Only `ended` projections can be scored in full.
- `run_pipeline.py` (`zones`) saves each projection it makes the same way, once it has passed `check`, and prints the folder. It does not save when `--projection` reuses an existing file or `--no-save` is given. Its own `runs/` folders use the same names but are a different thing: working output that can be deleted, while `projections/` is the record.
- The folder is `projections/` in the directory the command runs from. Set `RZ_PROJECTIONS_DIR`, or pass a folder as the last argument, to keep them elsewhere. For `zones`, `history_dir` in a [config file](#your-defaults) gives the history one home whatever directory you run it from.
- `projections/` is in `.gitignore`: the history is local to your machine. Remove that line if you want to commit it.
- `rz.py save` itself keeps only the pair; `zones` adds `entries.json` and `provenance.json` to the folder it just saved, in the same run. Entries saved before provenance existed, or by the skill outside `zones`, have no `provenance.json`; everything that reads the history (`list`, `score`, `show`) works without it.

### Provenance

Every `zones` run that makes a projection writes `provenance.json` beside its files (`STEM_provenance.json` with `--out-dir`) and into the saved folder. It answers which model and which rules produced the result, from which data, when, and at what cost:

```json
{
  "schema_version": 1,
  "kind": "reversal_zones_provenance",
  "written_at": "2026-11-19T18:47:09Z",
  "subject": { "ticker": "PETR4.SA", "as_of": "2026-11-18T15:15-03:00", "horizon": "1-3m" },
  "input": {
    "sha256": "c8472cd1...",
    "schema_version": 1,
    "data": { "source": "Yahoo Finance (yfinance)", "downloaded_at": "2026-11-18T18:15:40Z", "...": "the input's provenance" }
  },
  "projection": {
    "sha256": "599209fe...",
    "made_by": "claude-code",
    "schema_version": 1,
    "claude": {
      "requested_model": "opus",
      "model": "claude-opus-4-8",
      "claude_code_version": "2.1.323",
      "duration_ms": 137366, "duration_api_ms": 130722, "num_turns": 8, "total_cost_usd": 0.750123,
      "usage": { "input_tokens": 14, "cache_creation_input_tokens": 37579, "cache_read_input_tokens": 210576, "output_tokens": 10759 },
      "wall_seconds": 140.0
    }
  },
  "rules": {
    "version": "8ada605feb46",
    "sim_method": 2,
    "git": { "commit": "e6723b6f3f7d0f82fb30a10884cf17f3736b2d13", "dirty": false },
    "files": { "SKILL.md": "3f451f13...", "rz.py": "ba4be519...", "build_prompt.py": "2548a8c1...",
               "trade_entries.py": "17498a76...", "trade_entries.md": "6a625630...", "trade_entries.schema.json": "142cd64d..." }
  },
  "entries": { "sha256": "d17fb6f5...", "made_by": "trade_entries.py" }
}
```

| Part | Where it comes from |
|---|---|
| `input.data` | The input's own `provenance`, written by `build_prompt.py` at download time (absent for older inputs). |
| `input.sha256`, `projection.sha256`, `entries.sha256` | sha256 of the files exactly as written in the run folder. |
| `projection.claude` | Claude Code's own event stream: `model`, `claude_code_version` and `session_id` from the `system` init event; `num_turns`, `total_cost_usd`, `duration_ms`, `duration_api_ms` and `usage` from the final `result` event; `models_used` and `model_usage` from the result's `modelUsage` (else the models named on the assistant messages). A field Claude Code does not report is left out, never guessed. `requested_model` is what `--model` asked for, and `wall_seconds` what `zones` measured. |
| `projection.mechanical` | With `--engine mechanical`, in place of `claude` (and `made_by` is `"mechanical"`): `engine` (`mechanical.py`), `zone_rule`, `sim_method` and `wall_seconds`. The `rules` files are then `mechanical.py`, `backtest.py` and the others below except `SKILL.md`, so the `version` differs from a Claude run's. |
| `rules` | The checkout that ran: the sha256 of each rule file, a short `version` (the first 12 hex digits of a hash over those) that changes whenever any of them does, `sim_method` (the version `rz.py sim` uses by default there), and the git commit with `dirty` set when tracked files differ from it (untracked files such as `runs/` are not counted). Without git, `git` is left out. |
| `entries` | Added when the setups are written: by `trade_entries.py`, or by Claude Code with its own `claude` block (`--entries-by-claude`). |

Why it lives there: the data half is known at download time, so `build_prompt.py` writes it into the input. The run half cannot come from Claude, which does not reliably know its model id or cost, so `zones` reads it from Claude Code's events instead of asking Claude to fill fields in the projection. A run that reuses a projection (`--projection`) writes no provenance, since it did not make it; it copies the file beside the given projection into a new run folder when there is one, and never overwrites one.

### Scoring the saved projections

`score_projections.py` compares every saved projection with what the price did afterwards, then summarises how the forecast probabilities compared with the events.

```bash
uv run zones score                      # everything under projections/
uv run zones score PETR4.SA             # one ticker
uv run zones score projections/PETR4.SA/2026-11-18T1515_1-3m   # one saved folder
uv run zones score --json               # one JSON object instead of the tables
```

Each projection's line names the model and rules that made it (`made by claude-opus-4-8 · rules 8ada605feb46 · commit e6723b6`), read from its `provenance.json`, and the calibration summary ends with a Brier score per model and kind, so models can be compared once the samples allow. Projections saved without provenance count as `unrecorded`.

`zones score` runs the script with the same arguments; called directly it is `uv run python score_projections.py [FOLDER] [--ticker T] [--json]`, from the repository folder.

It downloads the later candles from Yahoo Finance once per ticker (split-adjusted, not dividend-adjusted, like step 1) and judges each projection with the simulation's own definitions (`outcome()` in `rz.py`, which shares its code with `sim`):

- **Measure.** Closes for `close_in_zone`, intra-candle lows and highs for `touched_once`.
- **Reached, broken, turned.** The nearest zone is reached when the lowest close (highest, for resistances) gets to its near edge, and always if the price started inside it. Every later entry, the tail included, is reached when the zone before it broke: strictly beyond its far edge. `turned` is true for the one entry per side where the horizon's extreme was made (reached and not broken), and for none if the nearest zone was never reached.
- **Scenario.** Exactly one of breakdown, breakout, range and whipsaw, from the far edges of the middle zones.
- **Which candles.** The input's last row if it was still forming (its close is the first close of the horizon), otherwise the candle after it, up to `horizon.last`. Weekly and monthly candles are built from daily ones. Intraday, the horizon is one session: the one in progress, or the next one if the market was shut.
- **Holidays.** Fewer candles than `horizon.closes` are scored as they are, with a note. An ended projection with less than half of its candles is not scored.
- **Open horizons.** The result is provisional and labelled `so far`. Only closed candles count for the close measure. A zone already reached or broken stays so; `turned` and the scenario can still change.
- **Not scored.** A stock split after the projection (its zone prices are on the old scale), realised prices that do not match the input's last closed candle, a failed download, or damaged saved files. Each is listed with its reason and the rest are still scored.

Only projections whose horizon has **ended** enter the calibration summary. Every `reach`, `turn` and scenario `p` is put in a 10-point bucket; per bucket the table shows the number of forecasts, their mean and how often the event happened. A `reach` of 1 for a zone the price started inside is left out, since it is true by definition. Each kind also gets a Brier score (mean squared difference between forecast and outcome, 0 is perfect) next to two reference forecasts: always 50% (25% for scenarios), and always the sample's own rate.

```
  reach: 8 forecasts, 38% happened
    Brier score 0.120 (0 is perfect); always saying 50% would score 0.250, always saying this sample's own rate 0.234
    forecast     n  mean forecast  observed
    10-20%       2           0.16      0.00
    30-40%       1           0.39      0.00
    ...
```

Read it with care. One projection contributes about twenty probabilities that are all judged on the same price path, and overlapping horizons of one ticker share most of that path, so the forecasts are far from independent. With a handful of projections the table is anecdote, not evidence. It describes calibration only: it is not a measure of returns and does not show that the method works or fails.

`rz.py score input.json projection.json candles.json` is the same judgement without the network: `candles.json` is a list of `[start, open, high, low, close]` rows of the close basis.

## Step 3: plot_projection.py

Draws the candles, the zones and the probabilities in one figure.

```bash
uv run python plot_projection.py INPUT.json PROJECTION.json [options]
```

Both files are needed: the projection does not contain candles. For a run made by `zones`, `zones plot PETR4.SA [options]` finds the two files and takes the same options (see [Commands](#commands)).

| Option | Default | Meaning |
|---|---|---|
| `-o FILE` | `projection.png` | Output image. The extension picks the format: `png`, `svg`, `pdf` or `jpg`. |
| `--theme` | `light` | `light` or `dark`. |
| `--lang` | from the input | Override for the language of the chart's own labels: `pt` or `en`. Without it the chart uses `reply.language` of the input, i.e. `--language` from step 1. |
| `--candles N` | automatic | How many past candles to show. The default is four times the horizon, with a minimum of 40. |
| `--dpi N` | `160` | Resolution for raster formats, from 1 to 1200. |
| `--no-check` | off | Plot a projection that fails the rules of `rz.py check`, printing them as warnings. |

Through `zones`, `theme`, `lang`, `candles` and `dpi` can be set once in a [config file](#your-defaults).

### Validation

Both files are checked before anything is drawn. If something is wrong, the script prints every problem it found on stderr, one per line with the field's name, and exits with status 1 without writing an image:

```
Cannot plot, 2 problems:
  projection.as_of.price: missing, expected a number
  projection.supports[1].zone: expected [low, high] as two numbers, or one number and one null for a tail, got [42.2, "x"]
```

There are two levels:

- **Structure** (always blocks): unreadable or invalid JSON, swapped files, missing or mistyped fields, probabilities outside 0 to 1, unparseable dates, no zone with both edges, an output path that cannot be written. `structure`, `scenarios`, `events`, `drivers` and `confidence` may be left out; if present they must be well formed.
- **Rules** (block unless `--no-check`): the same rules as `rz.py check` (reach and turn add up, zones in order, scenarios sum to 1 and so on), plus `horizon.close_basis` naming a series the input has. A missing `scenarios` or `confidence` section is not counted here, so a draft can be plotted; `rz.py check` itself still demands them.

The rules are read from the skill's `rz.py`. If `plot_projection.py` is copied somewhere without it, only the structure is checked and a one-line note says so.

### Reading the chart

- **Main panel.** Candles for the series the projection was judged on. Rising candles are hollow and falling candles are filled, so colour is left for the zones. Supports are blue and resistances are red, faint over the history and stronger over the shaded horizon. A dashed line and a boxed number mark the current price. Diamonds on the time axis mark dated events.
- **Probability panel.** One bar per zone on the same price scale. The filled part is `turn` (reverses here) and the outline is `reach` (gets here).
- **Scenario bar.** The four scenarios as one segmented bar, with the rule for each written underneath.
- **Notes.** Drivers with their direction, events, the confidence level and its caveats. Long caveats and event names are cut with an ellipsis so that the block always fits.

The language is chosen once, with `--language` in step 1. It is stored in the input as `reply.language`; the projection's text (drivers, caveats, event names) is written in it and the chart's own labels, month names, date formats and number separators (`1,234.50` or `1.234,50`) follow it. Region variants are accepted (`pt-BR`, `en_US`). If the chart has no labels for the language (say `es`), or the input has no `reply` block, the labels are in English; in the first case a note on standard error says so.

`--lang` overrides the labels only: text that comes from the projection stays in the language it was written in.

## Ranked trade setups

The last step of `run_pipeline.py` writes three hypothetical trade setups, each with an entry, a stop and a target, ranked best first, to `entries.json` in the run folder. It runs by default; `--no-entries` skips it. The setups are derived from the projection only, so they inherit every limit of it.

By default the step makes no Claude Code call. Every price and payoff follows from the projection's zone edges, and every probability from simulated price paths of the input, by fixed rules, so `trade_entries.py build` computes them (about a second), and writes the sentences around them (`anchor`, `basis`, `why`, `watch`, the limits) from fixed wording in the language of `reply.language` (English or Portuguese; any other language gets English). It costs nothing.

With `--entries-by-claude`, Claude writes those sentences instead, for this projection in particular:

1. `trade_entries.py prompt` fills `prompts/trade_entries.md` with the input, the projection and every valid setup with its computed numbers (Claude has no tools in this call, so it cannot count simulated paths; it chooses, ranks and writes).
2. `claude -p` answers with one JSON object. This call gets no tools at all and runs outside the repository; in three real runs it took two to three minutes and reported a cost of $0.50 to $0.70.
3. `trade_entries.py check` validates the reply against `prompts/trade_entries.schema.json` and recomputes every price, payoff and probability from the input and the projection. A reply that disagrees is kept for inspection, reported, and the run exits 1; the input, projection and chart are unaffected.

The numbers are identical either way: both paths are checked by the same `trade_entries.py check`.

When a new projection is saved under `projections/`, the setups are saved next to it as `entries.json`.

`zones entries PETR4.SA` computes and checks the setups of an existing run again (see [Commands](#commands)). The parts also run by hand, for example on the committed example:

```bash
uv run python trade_entries.py build  examples/petr4_input.json examples/petr4_projection.json -o entries.json
uv run python trade_entries.py prompt examples/petr4_input.json examples/petr4_projection.json > prompt.txt
uv run python trade_entries.py check  examples/petr4_input.json examples/petr4_projection.json reply.json
uv run python trade_entries.py expected examples/petr4_input.json examples/petr4_projection.json   # every valid setup, best first
```

How the setups are built:

- **Prices come from zone edges only**, the zones of the projection (in a mechanical run, the [zone rule](#a-stronger-zone-rule) of rules version 6: 1.5 zone widths, at least two candidate prices per cluster). A *reversal* enters at a zone's near edge and targets the first zone on the opposite side. A *continuation* enters where a zone breaks and targets the far edge of the next one. Stops sit a quarter of one candle's typical move beyond the zone.
- **Prices keep the candles' precision.** Stops, risk, reward and the units are rounded to the most decimals the input's rows show (at least two), so an asset priced around 0.04 written with `--decimals 5` gets the same setups as one priced at 40. A setup whose risk still rounds to zero, or whose entry, stop or target rounds to zero or below (a zone near zero on a collapsed stock), is discarded; when none is left the file holds `{"task": "trade_entries", "error": "no setup passes the rules for this projection"}`.
- **Odds are counted on simulated paths** (since rules version 2; the file now says `"rules": 6`, see the correction below). The paths are those of the projection's own simulation: `rz.walk()` with its fixed seed, its drift (sim method 2) and no shocks, the first 20,000 of the 200,000 paths `rz.py sim` draws. Between two simulated closes, `trade_entries.py` fills in the trading days with a Brownian bridge (five a week, 21 a month; the closes stay exactly as simulated) and gives every day a high and a low, drawn as the extremes of a Brownian bridge between its two points with 0.65 times the simulation's volatility (`SPREAD`). These draws use their own fixed seed, so the same files always give the same numbers and `rz.py`'s results are untouched. Each setup is then played on every path by the backtest's own rules (`backtest_entries.walk()`, pessimistic ordering): `fill` is the share of paths whose entry is reached by `valid_until`; `win_if_filled` the share of the filled paths that reach the target before the stop, a day touching both counting as the stop; `edge.if_filled` the mean result of the filled paths in units of risk (-1 at the stop, the ratio at the target, the last close when still open); `edge.per_order = fill × edge.if_filled`. Each entry's `basis` gives the shares that end at the target, at the stop and still open.
- **At `1-2w` and `1-3m` the path counts are corrected by the backtest** (since rules version 5; refitted in version 6 on the current zones, [below](#a-stronger-zone-rule)). For each side and kind, except market orders (left as counted), `fill` and `win_if_filled` are moved by a fixed shift in log-odds and `edge.if_filled` by a fixed number of R, all fitted on the fit part of the [long setup backtest](#an-empirical-correction-of-the-odds-per-horizon-side-and-kind) over every valid setup (`CORRECTION` in `trade_entries.py`), and adopted at those two horizons only after a held-out test fixed in advance. Continuations get lower edges (−0.09 to −0.21 R), reversals higher (+0.15 to +0.16 R at `1-2w`), most orders slightly higher fills; ranking and `quality` use the corrected numbers, and the 5% minimum fill is still judged on the paths' own count. `odds.basis` says "corrected by the backtest's measured error", and `confidence.limits` says what the correction is: an average over many past setups, not knowledge of this one, with that backtest's limits. At `6-12m` the correction failed the held-out test and nothing is corrected.
- **Why the paths, and what they cannot know.** Rules version 1 took `fill` from `reach` and `win_if_filled` from `turn / reach` (reversals) or a ratio of reaches (continuations). Those measure where the horizon's extreme close lands, not whether the target comes before a stop just past the zone, and they ignored touches inside a candle; the backtest found them far too optimistic. Counting on paths fixes the order of events and gives continuations their distance penalty without an ad-hoc factor: a far target is reached less often before the stop. The paths know the volatility and the drift, and nothing about the zones: a zone that holds or breaks more often than chance is not in these odds. They describe the distances of a setup.
- **Ranking** is by `edge.per_order` (then `fill`, then `id`), whatever its sign. A setup whose order fills on fewer than 5% of the paths is discarded: its edge per order rounds to zero, and it would otherwise rank above every setup with a negative edge. The three kept are the best three with distinct entry prices, whatever their side (rules version 3). When all of them are long, or all short, `confidence.limits` says so plainly: they ranked highest by the stated edge per order, the list is not balanced by side, and a one-sided list is not a forecast of direction or a recommendation. Rules version 2 swapped the third for the best setup of the other side whenever the best three shared one; the [setup backtest](#rules-version-3-no-side-quota) found that swapped-in setup the weakest of the three, so version 3 dropped the quota.
- **`quality`** (since rules version 3; bands of rules version 6) compares `edge.if_filled` with the model's own measured error at the input's horizon: ±0.17 R at `1-2w`, ±0.05 R at `1-3m`, ±0.32 R at `6-12m`, and the widest of those, ±0.32 R, at `today`, which the backtest cannot cover. `favourable` means the edge is at or above the band, `unfavourable` at or below minus the band, and `marginal` that it lies inside: on what the backtest measured, the edge cannot be told from zero. Each band is how far the stated mean edge of the picked setups missed the observed mean, under whichever ordering inside a candle missed by more: at `1-2w` and `1-3m` with the corrected odds on the fit part of the [long run](#an-empirical-correction-of-the-odds-per-horizon-side-and-kind), at `6-12m` (uncorrected, as in rules 3) on the first two years of the three-year backtest ([below](#rules-version-3-quality-against-the-models-measured-error)); version 3's bands were 0.35, 0.09 and 0.32, version 5's 0.29, 0.09 and 0.32. At `6-12m` nearly every setup is therefore `marginal`: the stated edges are small next to that error, and on the held-out year the label did not separate outcomes ([below](#rules-version-3-quality-against-the-models-measured-error)). `confidence.limits` names the band of the run. The label describes the simulated edge against its error; it is not a grade of the setup and not a recommendation.
- **When no setup has a positive edge**, the file still holds the three best (most often the case at `1-2w`, where the stops are tight and a day touching both levels counts against the setup), and `confidence.limits` says plainly that on the simulated paths every setup loses on average. Three always, rather than only the ones that pass, because a negative edge is the finding: it keeps the schema, the backtest and the comparison between runs unchanged. When no setup is valid at all, the file is the `no setup passes the rules` error, as before.
- **Paths with volatility clustering and fat tails were tried and not adopted.** A rules version 4 that drew the days from a GARCH(1,1) model with Student-t returns, at the same mean volatility, was fitted on the first two years of the setup backtest and judged on the held-out year: it lowered every stated fill, including the breakout fills that were already too low, and its Brier scores were not better ([below](#rules-version-4-clustering-and-fat-tails-not-adopted)). Version 4 was never shipped; the next version is 5.
- **Older files.** An `entries.json` written before 19 Nov 2026 has no `rules` key, one written earlier on 19 Nov 2026 says `"rules": 2`, one written until 20 Nov 2026 says `"rules": 3`, and one written until 22 Nov 2026 says `"rules": 5`; `trade_entries.py check` says which earlier version wrote it instead of listing every number, `zones show` still displays it as written, and `zones entries` computes it again. Version 2 also labelled `quality` with one band of ±0.05 R at every horizon; version 3 stated the uncorrected path counts; version 5 priced the setups on the zones of the earlier zone rule (one width, single-candidate clusters).

Read the limits before using the output:

- **The stated odds are backtested out of sample, and roughly calibrated on average, not per setup.** On the held-out years (2008-2009, 2014-2016) of the 76-ticker long run, the corrected odds of rules 6 missed the observed R per order of the picks by 0.002 R at `1-2w` and 0.03 R at `1-3m`, 0.13 R at `6-12m` (uncorrected), with the Brier scores of `fill` and `win_if_filled` better than the constant forecast at all three ([below](#a-stronger-zone-rule)). Those of rules 5 missed the observed R per order of the picks by 0.01 R at `1-2w` and 0.03 R at `1-3m` (rules 3: 0.06 and 0.10), and beat the constant forecast by Brier score for `fill` and `win_if_filled`; at `6-12m`, uncorrected, the miss was 0.16 R in those years. The earlier, three-year result for rules 2 and 3: On the held-out year of the [setup backtest](#backtest-of-the-trade-setups), `win_if_filled` beat the constant forecast by Brier score at `1-2w` and `1-3m` and lost slightly to it at `6-12m`; the stated edge per order was within about 0.1 R of the observed mean at `1-2w` and `1-3m`, and 0.2 R too low at `6-12m`, where the held-out year was a strong rise that the simulation's 5% drift does not contain.
- The edge is near zero by construction: on random paths a stop and a target have no edge beyond the drift, the time left at expiry and the pessimistic ordering. A positive `edge.per_order` mostly says "long, in a simulation with an upward drift"; it is not evidence that the setup works.
- There is no position size or leverage, no costs, and a setup marked `favourable` is not a recommendation.

## Backtest of the baseline

`backtest.py` measures how well the simulation's own probabilities (`rz.py sim`, before any adjustment by the skill) matched what happened, on many past dates at once. It makes no Claude call.

```bash
uv run python backtest.py PETR4.SA NVDA --horizon 1-3m              # the last three ended years
uv run python backtest.py NVDA --horizon 1-2w --start 2022-01-01 --end 2025-06-30 --step 5
uv run python backtest.py PETR4.SA --json                           # one JSON object instead of the tables
uv run python backtest.py PETR4.SA --csv cases.csv                  # every forecast of every case, one row each
uv run zones backtest PETR4.SA NVDA --horizon 1-2w                  # the same, through zones
```

| Option | Default | Meaning |
|---|---|---|
| `--horizon` | `1-3m` | `1-2w`, `1-3m` or `6-12m`. `today` is not offered: Yahoo keeps 15-minute candles for about 60 days. |
| `--start`, `--end` | three years ending one horizon (plus a week) before today | The range of as-of dates, `YYYY-MM-DD`. A case whose horizon has not ended is skipped. |
| `--step N` | `5` for `1-2w`, `10` for `1-3m`, `21` for `6-12m` | Trading days between two as-of dates. |
| `--paths N` | `200000`, as `rz.py sim` | Simulated paths per case. Fewer is faster; the report then says the numbers differ from a live run's. |
| `--method N` | `2`, as `rz.py sim` | Sim method version: `1` is the original simulation without drift, `2` adds the 5% a year drift ([below](#sim-method-2-a-small-drift-chosen-out-of-sample)). The report names the method. |
| `--json` | off | One JSON object (settings, skipped cases, the summaries) instead of the tables. |
| `--csv FILE` | none | Also write every forecast: ticker, as-of date, price, entry, zone, forecast, happened. |
| `--quiet` | off | No progress lines on standard error. |
| `--no-cache` | off | Download every candle again instead of reusing finished ones from the [candle cache](#the-candle-cache). |

For each ticker it downloads daily candles once (split-adjusted, not dividend-adjusted, like step 1) and builds weekly and monthly candles from them. Then for every as-of date, after that day's close:

1. **Input.** The input JSON is built by `build_prompt.py`'s own code (`make_all_series()` and `assemble()`, the same presets, row counts, `close_basis` and `last_row` labels) from the candles that end on that day. Nothing after the as-of day enters the input. There are no events, `research` is `none`, and the session is `closed`.
2. **Zones.** Three supports and three resistances, chosen by a fixed rule from the output of `rz.py levels` (`choose_zones()`, the only place the rule lives): the zone width is 1.5 times the mean of the two `typical_zone_width` numbers (`WIDTH_FACTOR`); the candidates are the prices `levels` reports as evidence (pivot lows and highs, the lows and highs of the last three candles, the retracement levels, the two ends of the dominant swing); walking outward from the price, a candidate within one width of the first member of the current cluster joins it, and a cluster of fewer than two candidates is dropped (`MIN_MEMBERS`); each remaining cluster becomes one zone of that width centred on the mean of its members, cut at the price and pushed outward so it never overlaps the zone before it; the nearest clusters are kept; a side with too few clusters is filled with one-width zones, one width apart. It is not what Claude would choose: it is a reproducible baseline. Until 22 Nov 2026 the width was one mean and a single candidate made a zone; the change was chosen for the trade setups and checked on this backtest too ([below](#a-stronger-zone-rule)), so the tables of this section, made with the earlier rule, are kept as they were.
3. **Probabilities.** `rz.py sim` on those zones, with its fixed seed, and no shocks, under the sim method `--method` names (default: the one `rz.py sim` uses).
4. **Outcome.** `rz.outcome()` against the candles that followed, exactly as `score_projections.py` judges a saved projection; a case with fewer than half of its scheduled closes is skipped.

The report is the calibration summary of `score_projections.py` (the same buckets, Brier scores and reference forecasts) for `reach`, `turn` and the four scenarios together, plus each scenario on its own, overall and per ticker. A case is not skipped silently: the reasons are counted at the top.

The first run of the backtest (19 Nov 2026; PETR4.SA, NVDA and VALE3.SA; sim method 1, which had no drift) found `reach` well calibrated (Brier 0.206 against 0.249 for always forecasting the sample's own rate) and `turn` calibrated but barely better than the base rate. The scenarios were the weak part: a ticker in a long rise got too few breakouts (NVDA `1-3m`: breakout forecast about 40%, observed 57%), and NVDA `6-12m` scored worse than the base rate. That led to sim method 2, below.

Read it with the same care as the scoring of saved projections, and more:

- It measures the baseline only, on mechanical zones. The zones and adjusted probabilities of a real projection are not tested here.
- No events: earnings and ex-dividend dates are not simulated, while the realised prices contain them. A large dividend shows up as a drop the simulation did not include.
- Cases overlap whenever the step is shorter than the horizon, and every entry of one case is judged on the same path, so the number of forecasts overstates the evidence by a wide margin.
- Tickers chosen today are survivors, and the prices are on today's split-adjusted scale.
- It describes calibration. It is not a measure of returns, does not show that the method works, and is not advice.

### Sim method 2: a small drift, chosen out of sample

`rz.py sim` now uses **sim method 2**: the same simulation, same seed and same random numbers, plus a deterministic drift under which the expected price grows 5% a year (none on intraday candles). `--method 1` gives the original simulation, in `rz.py sim` and in `backtest.py`, and every `sim` output names its method in `sim_method`, so baselines made before and after can be told apart and reproduced.

How it was chosen. Twelve liquid names, six in the US and six on B3: NVDA, AAPL, MSFT, SPY, JPM, XOM, PETR4.SA, VALE3.SA, ITUB4.SA, BBDC4.SA, ABEV3.SA, WEGE3.SA. Four years of as-of dates per horizon (the default steps), 4,188 cases with mechanical zones. Two remedies were compared with the original:

1. **Drift in the simulation.** Two kinds: a trend estimate from the main series (mean, median or least-squares slope of the log closes over the last 26 or 52 candles or all of them, shrunk by 0.25, 0.5 or 1), and a fixed rate (2.5%, 5%, 7.5%, 10% or 15% a year).
2. **A calibration layer after the simulation**, fitted per horizon on backtest cases: a monotone map (Platt-style, or isotonic) of every `reach`, per side or pooled, with `turn` and the scenarios then rederived the way SKILL.md rederives them after an adjustment.

Everything was fitted and chosen on earlier dates and scored on later ones, twice: fit on as-of years 1-2 and score year 3, then fit on years 1-3 and score year 4. A fit case had to have its whole horizon end before the first scored as-of date, so no outcome seen in fitting falls in the scored period. The choice was the one with the lowest mean of the `reach`, `turn` and scenario Brier scores on the fit cases, over the three horizons.

Brier scores on the two scored years together (20,000 paths per case; lower is better):

| Horizon, scored cases | Original (method 1) | Fixed drift 5% (method 2) | Trend drift | Calibration layer | Method 2 + calibration |
|---|---|---|---|---|---|
| `1-2w`, 1,194: `reach` / `turn` / scenarios | 0.2041 / 0.1605 / 0.1677 | 0.2033 / 0.1603 / 0.1672 | 0.2059 / 0.1609 / 0.1689 | 0.2025 / 0.1600 / 0.1669 | 0.2026 / 0.1599 / 0.1667 |
| `1-3m`, 600 | 0.2057 / 0.1653 / 0.1686 | 0.2020 / 0.1637 / 0.1661 | 0.2056 / 0.1652 / 0.1688 | 0.1992 / 0.1626 / 0.1648 | 0.1996 / 0.1626 / 0.1651 |
| `6-12m`, 288 | 0.2181 / 0.1651 / 0.1688 | 0.1965 / 0.1574 / 0.1546 | 0.2057 / 0.1603 / 0.1612 | 0.1907 / 0.1540 / 0.1505 | 0.1870 / 0.1522 / 0.1482 |

What this says, and why only the fixed drift was shipped:

- **The trend of the candles does not help.** No trend variant beat the fixed rates on the fit years (in the first split the best trend variant was no drift at all), and on the scored years trend drift made `1-2w` worse in 9 of 12 tickers, did nothing at `1-3m`, and helped at `6-12m` by about half as much as the fixed rate. Extrapolating momentum is not the fix for the NVDA breakouts.
- **A fixed 5% a year helps a little, consistently.** It was the first split's choice (the second chose 10%). Against the original, scored on later dates: better at every horizon, in 10 to 11 of the 12 tickers, and never worse than the original by more than 0.0001 in any split and horizon, for `reach`, `turn` or the scenarios together. A ticker-cluster bootstrap of the difference (90% interval, `reach`): `1-2w` -0.0007 [-0.0011, -0.0004], `1-3m` -0.0037 [-0.0051, -0.0023], `6-12m` -0.0215 [-0.0314, -0.0127]. As a check on a different regime, the same comparison on as-of dates from Nov 2017 to Oct 2021 (the 2018 sell-offs and the 2020 crash) also favoured 5% at every horizon, though it lost on cases made in 2018 at `1-3m` and `6-12m`. 10% did better in the most recent year and worse in 2018, so the smaller rate was kept.
- **The calibration layer was not shipped.** Pooled it scored slightly better than the drift, but it was not consistent: its `reach` was worse than the original's in two of the six split-and-horizon cells, and adding it on top of method 2 was worse than method 2 alone in three of six. The fitted maps show why: they mostly learn the average rise of these twelve surviving names in 2021-2026 (at `6-12m` they turn a 50% resistance `reach` into about 69% and a 50% support `reach` into about 29%), well beyond the 5% rate, which is a property of the sample more than of the method. A table like that would mislead in a falling market and would break the rule that `sim` reports what its own paths do.
- **Where neither helps.** At `1-2w` all the differences are small (under 0.002). The individual scenarios at `6-12m` are still worse than always forecasting their own rate, and `breakdown` at `1-3m` too: one horizon-long path per case, a dozen overlapping cases per ticker and year, and two strong years, so the breakouts of trending names are still under-forecast (NVDA `6-12m` breakout: 35% under method 1, 39% under method 2, observed 75%).

The same comparison with the real tool, at 200,000 paths, on the scored window (the last two years of as-of dates; `backtest.py ... --method 1` and `--method 2`; Brier score, then always forecasting the sample's own rate):

| Horizon, as-of dates, cases | `reach` 1 → 2 (base) | `turn` 1 → 2 (base) | Scenarios 1 → 2 (base) | `breakout` 1 → 2 (base) | `breakdown` 1 → 2 (base) |
|---|---|---|---|---|---|
| `1-2w`, 29 Oct 2024 to 29 Oct 2026, 1,206 | 0.2042 → 0.2035 (0.249) | 0.1608 → 0.1606 (0.168) | 0.1666 → 0.1662 (0.188) | 0.225 → 0.225 (0.232) | 0.213 → 0.212 (0.215) |
| `1-3m`, 13 Aug 2024 to 13 Aug 2026, 606 | 0.2021 → 0.1981 (0.248) | 0.1680 → 0.1665 (0.175) | 0.1731 → 0.1705 (0.188) | 0.248 → 0.244 (0.247) | 0.213 → 0.206 (0.203) |
| `6-12m`, 12 Nov 2023 to 12 Nov 2025, 288 | 0.2197 → 0.1980 (0.248) | 0.1650 → 0.1575 (0.170) | 0.1690 → 0.1546 (0.188) | 0.291 → 0.261 (0.238) | 0.197 → 0.171 (0.152) |

Method 2 was better for 9 of the 12 tickers at `1-2w` (not MSFT, SPY, ABEV3.SA), 11 at `1-3m` (not MSFT) and 11 at `6-12m` (not PETR4.SA). Reliability for `reach` at `6-12m` moved closer to the diagonal (forecasts around 25%, 45% and 74% came true 38%, 52% and 69% of the time under method 1, and 23%, 49% and 75% under method 2); breakdowns are still over-forecast and breakouts under-forecast at the long horizons.

To repeat it: `uv run python backtest.py NVDA AAPL MSFT SPY JPM XOM PETR4.SA VALE3.SA ITUB4.SA BBDC4.SA ABEV3.SA WEGE3.SA --horizon 1-3m --start 2024-08-13 --end 2026-08-13 --method 1` and again with `--method 2` (Yahoo's history may have been revised since). The split-by-date search over drift variants and the calibration layer was a one-off study and is not part of the repository. These are descriptions of one sample in one mostly rising market, not properties of the method.

## Backtest of the trade setups

`backtest_entries.py` checks the [trade setups](#ranked-trade-setups) themselves: on many past dates it makes the three setups exactly as a live `zones TICKER --engine mechanical` run would, then plays them on the daily candles that followed and compares the stated `fill`, `win_if_filled` and edge with what happened. It makes no Claude call and costs nothing beyond the Yahoo downloads.

```bash
uv run python backtest_entries.py PETR4.SA NVDA --horizon 1-3m                    # the last three ended years
uv run python backtest_entries.py NVDA SPY --horizon 1-2w 1-3m 6-12m               # several horizons in one report
uv run python backtest_entries.py PETR4.SA --optimistic                            # the other bound of the ordering
uv run python backtest_entries.py PETR4.SA --json                                  # one JSON object instead of the tables
uv run python backtest_entries.py PETR4.SA --csv setups.csv                        # every setup, one row each
uv run zones backtest-entries PETR4.SA NVDA --horizon 1-2w                         # the same, through zones
uv run python backtest_entries.py --universe backtest_universe.txt --horizon 1-2w 1-3m 6-12m --start 2005-01-03 \
    --by-year --by-regime --by-part --subset original --jobs 10                    # the long run (below)
```

| Option | Default | Meaning |
|---|---|---|
| `--horizon H [H ...]` | `1-3m` | One or more of `1-2w`, `1-3m`, `6-12m`; each gets its own section. |
| `--start`, `--end`, `--step N` | as [`backtest.py`](#backtest-of-the-baseline) | The as-of grid, per horizon: three years ending one horizon (plus a week) before today, every 5, 10 or 21 trading days. |
| `--method N` | `2`, as a live run | Sim method of the projection (`mechanical.build(d, method=N)`). |
| `--optimistic` | off | Resolve a candle whose order is unknown in the setup's favour instead of against it. |
| `--universe FILE` | none | Read the tickers from a [universe file](#the-universe-file) (tickers given on the command line are added). The report names the file and its version and adds a table by group. |
| `--group NAME` | every group | With `--universe`: only this group's tickers (repeatable), e.g. `--group original` for the twelve tickers of the earlier reports. |
| `--subset NAME` | none | With `--universe`: repeat the whole report for this group's tickers alone (repeatable). |
| `--by-year` | off | Add a compact table per calendar year of the as-of date. |
| `--by-regime` | off | Add tables by market regime: the index's return over the 365 days before the as-of date (bull at or above +10%, bear at or below -10%, flat between; known at the time), and the index's move from the as-of close to `valid_until` (known only afterwards). The index is the Ibovespa (`^BVSP`) for `.SA` symbols and the S&P 500 (`^GSPC`) for the rest, downloaded once through `data_source`. |
| `--by-part` | off | Add a table by part of the [fixed fit / held-out split](#the-fit--held-out-split). |
| `--part fit\|held-out` | both | Report only the cases of one part. |
| `--jobs N` | `1` | Run the cases in N processes. Downloads stay in the main process (the cache and the shared rate limit apply as always) and the results come back in serial order, so the output is byte for byte that of `--jobs 1`. |
| `--json`, `--csv FILE`, `--quiet`, `--no-cache` | off | As in `backtest.py`. The CSV has one row per setup: its stated numbers, fill, exit, R, and the other ordering's exit and R, plus the case's part and, with `--by-regime`, its regime and index moves. |

**History.** A date is skipped ("less history than a live run requests") when the ticker's daily candles start after the first day of the longest series a live run of that horizon asks Yahoo for: 2 years before the as-of date at `1-2w`, 5 years at `1-3m`, 10 years at `6-12m`. The presets assume a mature listing; with less history `input_as_of()` would build shorter series than any live run of an established name sees. So the as-of dates of a young listing start later, and at `6-12m` most B3 names (Yahoo's B3 history starts around 2000) only from about 2010. A B3 date whose series would start on a day Brazil's daylight saving time began (no local midnight that day, until 2018) is skipped too, with its own reason: `backtest.input_as_of()` cannot build it.

**What it does, per ticker, horizon and as-of date** (after that day's close):

1. The input from `backtest.input_as_of()` (no events, `research: none`), the projection from `mechanical.build()`, the setups from `trade_entries.build()`. Nothing is reimplemented: a change to the zone rule, the sim method or the setup rules changes these results.
2. The setups are walked on **daily** candles after the as-of day up to their `valid_until` (the horizon's last close), even when the horizon's close basis is weekly or monthly; finer candles say more about the order of events.

**Fill and exit rules.**

- The as-of day's own candle is never used: the setups are made after its close.
- A `limit` order fills when the candle reaches the entry (a long's low at or below it, a short's high at or above it). A `stop` order fills when the candle reaches the entry in the order's direction. A touch is enough, which is generous for limit orders (about 2% of fills were exact touches). When the open is already past the entry, the order fills **at the open**: better than the entry for a limit, worse for a stop. A `market` order would fill at the next open, not at the as-of close it was priced at (none occurred in the run below). An unfilled order stays until `valid_until`, even if the price reaches the target first.
- After the fill, the stop or the target closes the setup at its price. An open past the stop or the target closes it at that open, so a gap through the stop can lose more than 1R. A setup still open after the last candle is closed at that candle's close (expiry).
- **Ordering inside a candle is unknown.** On the fill candle, a level the price must have crossed after the entry (the entry lies between the open and that level) is hit after the fill; anything else the candle reached may have come before. When a candle allows two results (stop or target, stop or still open, target or still open), the default counts the **pessimistic** one; `--optimistic` takes the other. Every setup is walked both ways, and the report says how many results differ (`ambig`) and what the mean R would be under the other ordering.
- **R** is the result in units of the setup's own risk (entry to stop), measured from the actual fill price. An unfilled order is 0R. A win is an exit at the target.

**The report**, per horizon: stated against observed for fill, win among fills, R per fill (`edge.if_filled`) and R per order (`edge.per_order`), overall and by side, kind (reversal, continuation), side and kind, order type, quality, rank and ticker; Brier scores of `fill` and `win_if_filled` next to always forecasting the observed rate; reliability buckets; a 90% bootstrap that resamples whole cases (fixed seed); and the spread of mean R across tickers. The rules and the caveats are printed with every report.

### Rules version 2: odds from simulated paths, judged on a held-out year

The odds of [rules version 2](#ranked-trade-setups) have two free parameters, the width of the simulated daily high and low (`SPREAD`) and the `quality` band (`EDGE_BAND`); the 5% minimum fill was set without looking at outcomes, after a live run ranked a setup that filled on 2% of the paths first. Both free parameters were chosen on the first two years of as-of dates only: cases whose `valid_until` falls before the first as-of date of the third year (29 Oct 2025 at `1-2w`, 13 Aug 2025 at `1-3m`, 12 Nov 2024 at `6-12m`), every valid setup of those cases (26,843 candidates over the three years, from the same twelve tickers), scored by the sum of the Brier scores of `fill` and `win_if_filled` per horizon and kind. `SPREAD` 0.65 had the lowest sum (0.5, 0.65, 0.8 and 1.0 were tried, with and without a 0.85 or 0.92 scale on the whole simulated move; 1.0 is the simulation's own volatility, and the fit years preferred narrower days, mostly because a wider day touches the stop on the fill day too often). A fixed day-to-day band around the closes (the Broadie-Glasserman continuity correction, 0.58 volatilities) was tried first and dropped: it puts both the high and the low out at once, so nearly every `1-2w` continuation was stopped on its fill day. `EDGE_BAND` 0.05 (replaced by a band per horizon in [rules version 3](#rules-version-3-quality-against-the-models-measured-error)) is about the spread of the stated edges; on the fit years the three labels ordered the observed R per order at every horizon (`1-2w` +0.52 / +0.05 / -0.02 with 11, 92 and 3,446 setups; `1-3m` +0.03 / -0.02 / -0.10; `6-12m` +0.26 / -0.03 / -0.42).

Then the third year was run once with each version of the rules (`--start` the dates above; rules 1 with the code of commit c793ee5). Reports: [`examples/backtest_entries_2026-11-19_rules2_heldout.txt`](examples/backtest_entries_2026-11-19_rules2_heldout.txt) and, for all three years, [`examples/backtest_entries_2026-11-19_rules2.txt`](examples/backtest_entries_2026-11-19_rules2.txt). Stated / observed, pessimistic ordering; "opt." is the observed R per order under the optimistic ordering, the other bound:

| Held-out year, setups | Rules | Fill st/obs | Win st/obs | R per order st/obs (opt.) | Brier fill (constant) | Brier win (constant) |
|---|---|---|---|---|---|---|
| `1-2w`, 29 Oct 2025 to 29 Oct 2026, 1,821 | 1 | 56% / 64% | 59% / 32% | +0.23 / -0.18 (+0.03) | 0.216 (0.230) | 0.287 (0.218) |
| | **2** | 45% / 50% | 23% / 28% | -0.07 / +0.01 (+0.11) | 0.218 (0.250) | **0.190 (0.201)** |
| `1-3m`, 13 Aug 2025 to 13 Aug 2026, 936 | 1 | 62% / 73% | 64% / 42% | +0.33 / -0.02 (+0.04) | 0.191 (0.195) | 0.275 (0.244) |
| | **2** | 65% / 73% | 43% / 42% | +0.04 / -0.04 (+0.03) | 0.181 (0.196) | **0.228 (0.244)** |
| `6-12m`, 12 Nov 2024 to 12 Nov 2025, 432 | 1 | 62% / 75% | 68% / 54% | +0.37 / +0.18 (+0.18) | 0.186 (0.188) | 0.269 (0.249) |
| | **2** | 67% / 73% | 47% / 58% | +0.05 / +0.24 (+0.24) | 0.164 (0.196) | 0.250 (0.244) |

By kind, side and rank on the held-out year, rules 2 (R per order stated / observed; setups in brackets):

| Horizon | Reversal | Continuation | Long | Short | Rank 1 | Rank 2 | Rank 3 |
|---|---|---|---|---|---|---|---|
| `1-2w` | -0.07 / +0.02 (1,761) | -0.10 / -0.22 (60) | -0.07 / +0.03 | -0.07 / -0.02 | -0.04 / +0.08 | -0.07 / +0.01 | -0.10 / -0.05 |
| `1-3m` | +0.02 / +0.20 (26) | +0.04 / -0.05 (910) | +0.05 / +0.05 | +0.01 / -0.21 | +0.07 / +0.12 | +0.03 / -0.06 | +0.00 / -0.19 |
| `6-12m` | +0.07 / +0.52 (26) | +0.05 / +0.22 (406) | +0.09 / +0.39 | -0.02 / -0.07 | +0.10 / +0.46 | +0.07 / +0.33 | -0.02 / -0.07 |

The same with rules 1, for comparison: reversals +0.24 / +0.03, +0.39 / +0.02, +0.52 / +0.17; continuations +0.22 / -0.25, +0.32 / -0.03, +0.35 / +0.19; shorts +0.21 / -0.21, +0.30 / -0.23, +0.26 / -0.19; rank 1 +0.33 / -0.20, +0.48 / -0.07, +0.54 / +0.32 and rank 3 +0.14 / -0.14, +0.22 / -0.04, +0.22 / -0.05 (`1-2w`, `1-3m`, `6-12m`). Win rates of reversals: 36% / 17%, 38% / 16%, 37% / 33% under rules 1; 23% / 28%, 38% / 42%, 45% / 71% under rules 2.

`quality` on the held-out year, rules 2 (R per order observed, favourable / marginal / unfavourable): `1-2w` +0.59 / +0.29 / +0.00 (4, 67 and 1,750 setups), `1-3m` +0.03 / -0.12 / -0.12, `6-12m` +0.39 / -0.02 / -0.20.

What changed, and where it still falls short:

- **`win_if_filled` is no longer a ceiling.** Rules 1 stated 59-68% against 32-54% observed; rules 2 states 23%, 43% and 47% against 28%, 42% and 58%. Its Brier score now beats the constant forecast at `1-2w` and `1-3m` (it was worse at every horizon), and at `6-12m` it is still slightly worse than the constant (0.250 against 0.244), though better than rules 1's 0.269. Reliability over all three years: at `1-3m`, forecasts of 35%, 44% and 55% came true 33%, 38% and 59% of the time.
- **The stated edge moved to about zero, and so did the observed one, except at `6-12m`.** The gap between stated and observed R per order on the held-out year shrank from 0.41 to 0.08 R at `1-2w` and from 0.35 to 0.08 R at `1-3m`; the target was ±0.05 R, so it is missed at both, narrowly: the stated values (-0.07 and +0.04) lie just outside the 90% bootstrap intervals of the observed means (-0.03 to +0.05 and -0.10 to +0.01), intervals that are themselves too narrow because cases overlap. At `1-2w` the stated edge (-0.07) lies below even the pessimistic bound: the simulated days touch both levels more often than real ones. At `6-12m` the gap stayed at 0.19 R, now with the stated edge too low: the held-out year was a strong rise (long setups +0.39 R observed), which a 5% a year drift does not contain. That is the limit of a zone-blind random walk, not something a tuned parameter would fix out of sample.
- **The mix changed.** Ranked by the path edge, `1-2w` now keeps reversals (97% of picks, mostly long), whose wide stops survive daily noise, instead of continuations with stops one daily move away; `1-3m` and `6-12m` still keep mostly continuations, now with a distance penalty from the paths. Short setups still lost at `1-3m` and `6-12m`; they are stated close to zero now (+0.01, -0.02) instead of +0.30.
- **Ranking now means something on the held-out year.** Rank 1 beat rank 3 at every horizon (+0.08 against -0.05, +0.12 against -0.19, +0.46 against -0.07 R per order); under rules 1 it did only at `6-12m`.
- **`quality` separates where the edges spread.** On the held-out year the three labels are ordered at `1-2w` and `6-12m`, and at `1-3m` `marginal` and `unfavourable` did equally badly. At `1-2w` 96% of the picks are `unfavourable`, because nearly every setup there has a negative edge on the paths: the label then says that, and little else. Rules version 3 replaced the band (below).

### Rules version 3: quality against the model's measured error

Rules version 2's `quality` claimed a precision the model does not have. Its ±0.05 R band is smaller than the model's own error: on the first two years, the stated mean edge per fill of the picked setups missed the observed mean by 0.13 R at `1-2w` and `6-12m`, and at `1-2w` by 0.35 R under the optimistic ordering. So at `1-2w` 96% of the picks were called `unfavourable` for a stated edge of about -0.16 R, while their filled setups observed -0.04 R per fill under the pessimistic ordering and +0.18 R under the optimistic one.

Rules version 3 keeps the field and its three values and changes the band: one per horizon, equal to the model's measured error there. On the first two years of as-of dates, with version 3's picks (no side quota, next section), the band is the larger of |mean stated `edge.if_filled` - mean observed R per fill| of the picked setups under the pessimistic and under the optimistic ordering, rounded up to 0.01: `1-2w` 0.35 (0.13 pessimistic, 0.35 optimistic), `1-3m` 0.09 (0.09, 0.00), `6-12m` 0.32 (0.32, 0.32). With version 2's picks the same measure gave 0.06 at `1-3m` and 0.13 at `6-12m`: the long setups that now replace the swapped-in shorts are the ones the paths underestimate most. A horizon without a backtest (`today`) gets the widest. An edge inside the band is `marginal`: it cannot be told from zero.

Weighed and not taken: a band from the Monte Carlo error (tiny with 20,000 paths, so it would claim more precision, not less); bands from the spread of the error across tickers or side and kind (0.1 to 0.6 R depending on the grouping, and at `6-12m` driven by one strong rise in NVDA; with cases that overlap in time, mostly sampling noise); a bias correction of the stated edge (it would learn the rising market of these survivor tickers, as the calibration layer of the baseline did); and a relative label (top, middle, bottom of the horizon), which `rank` already gives and which would call a setup with a negative stated edge `favourable`. The bands were fixed before the held-out year was looked at.

The held-out year with all of version 3 (`--start` 29 Oct 2025 at `1-2w`, 13 Aug 2025 at `1-3m`, 12 Nov 2024 at `6-12m`; report [`examples/backtest_entries_2026-11-19_rules3_heldout.txt`](examples/backtest_entries_2026-11-19_rules3_heldout.txt)). Observed R per order, pessimistic ordering, setups in brackets:

| Horizon | Rules | favourable | marginal | unfavourable |
|---|---|---|---|---|
| `1-2w` | 2 (±0.05) | +0.59 (4) | +0.29 (67) | -0.00 (1,750) |
| | **3 (±0.35)** | none | **+0.01 (1,813)** | -0.30 (8) |
| `1-3m` | 2 (±0.05) | +0.03 (475) | -0.12 (450) | -0.12 (11) |
| | **3 (±0.09)** | **-0.06 (241)** | **+0.02 (695)** | none |
| `6-12m` | 2 (±0.05) | +0.39 (281) | -0.02 (123) | -0.20 (28) |
| | **3 (±0.32)** | +4.85 (1) | **+0.27 (431)** | none |

What it shows, and what it does not:

- **The label no longer claims a precision the model lacks, and that is nearly all it does.** Almost every setup is now `marginal`: at `1-2w` and `6-12m` all but a handful. That is the finding: next to the model's measured error, the stated edges cannot be told from zero. Under version 2 the same `1-2w` setups were called `unfavourable` and observed about zero.
- **It does not separate outcomes.** At `1-3m`, the one horizon where the band leaves two sizeable groups, `favourable` did worse than `marginal` on the held-out year (-0.06 against +0.02 R per order), and over all three years about the same (-0.02 against -0.03). Read `quality` as "outside or inside the model's error", not as a grade.
- **`marginal` is not neutral.** At `6-12m` it observed +0.27 R in a strong rise. "Cannot be told from zero" is a statement about the model's error, not a forecast of zero.
- Over all three years (report [`examples/backtest_entries_2026-11-19_rules3.txt`](examples/backtest_entries_2026-11-19_rules3.txt)): `1-2w` marginal -0.00 (5,371), unfavourable -0.16 (50); `1-3m` favourable -0.02 (674), marginal -0.03 (2,047); `6-12m` marginal +0.21 (1,295), favourable 1 setup.

The bands come from one sample of twelve survivor tickers in a mostly rising market, and from the picks of the current `pick()`; a change to the ranking or to the odds changes the picked setups and so the measured error, and needs the bands measured again.

### Rules version 3: no side quota

Rules version 2 kept the best three by edge per order but, when all three were on one side, swapped the third for the best setup of the other side. In the three-year report above that swapped-in setup was the weakest of the three: at `6-12m` the 432 rank-3 setups were exactly the 432 short continuations (stated -0.01 R per order, observed -0.13), and at `1-3m` rank 3 observed -0.10. Version 3 keeps the best three with distinct entry prices, whatever their side, and adds a line to `confidence.limits` when they share one: they ranked highest on the simulated paths, the list is not balanced by side, and a one-sided list is not a forecast of direction or a recommendation. The odds of each setup are unchanged; only the choice of the third differs.

Three rules were compared on every valid setup of every case (the same twelve tickers, grid and candles as the report above), choosing on the first two years only (cases whose `valid_until` falls before the held-out dates of the rules 2 section): (a) the best three regardless of side; (b) the best three, dropping any whose edge per order is more than 0.05, 0.1, 0.15 or 0.2 R below the best's; (c) the version 2 swap only when the other side's edge is within 0.01, 0.02, 0.03, 0.05 or 0.1 R of the third's. Summed over the three horizons, the observed R per case on the fit years was +0.34 for version 2, +0.72 for (a), +0.72 for (c) at 0.01 and lower for every other margin, and +0.67 to +0.72 for (b), the higher the wider the gap (at 0.2 R it almost never drops a setup and is (a)). (a) was chosen: it ties the best, has no parameter, and keeps three setups whenever three are valid, so the schema, the backtest and comparisons between runs are unchanged. On the fit years it was better at `1-2w` (rank 3 -0.01 to -0.00) and `6-12m` (-0.11 to +0.29) and slightly worse at `1-3m` (-0.05 to -0.08), within noise.

Held-out year, then all three years, version 2 before / version 3 after (R per order stated / observed, setups in brackets). Rank 1 and rank 2 never change; the held-out cases are those of the three-year grid with an as-of date on or after the held-out start, so they differ slightly from the separate held-out run of the rules 2 section. Version 3's three-year report, with its quality bands too: [`examples/backtest_entries_2026-11-19_rules3.txt`](examples/backtest_entries_2026-11-19_rules3.txt).

| Horizon, cases | Rules | All | Rank 3 | Long | Short | One-sided lists |
|---|---|---|---|---|---|---|
| `1-2w` held-out, 600 | 2 | -0.07 / +0.01 | -0.10 / -0.09 | -0.07 / +0.02 (1,147) | -0.08 / -0.00 (653) | 0 |
| | **3** | -0.07 / +0.02 | -0.10 / -0.07 | -0.07 / +0.03 (1,172) | -0.08 / +0.00 (628) | 25 |
| `1-3m` held-out, 300 | 2 | +0.04 / -0.11 | +0.00 / -0.15 | +0.05 / -0.06 (592) | +0.01 / -0.20 (308) | 0 |
| | **3** | +0.04 / -0.07 | +0.01 / -0.04 | +0.05 / -0.04 (683) | +0.01 / -0.15 (217) | 91 |
| `6-12m` held-out, 144 | 2 | +0.05 / +0.25 | -0.02 / -0.09 | +0.09 / +0.42 (288) | -0.02 / -0.09 (144) | 0 |
| | **3** | +0.07 / +0.29 | +0.05 / +0.05 | +0.07 / +0.29 (432) | none | 144 |
| `1-2w` three years, 1,807 | 2 | -0.07 / -0.01 | -0.10 / -0.04 | -0.07 / +0.01 | -0.08 / -0.04 | 0 |
| | **3** | -0.07 / -0.00 | -0.10 / -0.03 | -0.07 / +0.01 | -0.08 / -0.04 | 59 |
| `1-3m` three years, 907 | 2 | +0.04 / -0.04 | +0.00 / -0.10 | +0.05 / +0.01 | +0.01 / -0.14 | 0 |
| | **3** | +0.04 / -0.03 | +0.01 / -0.07 | +0.05 / +0.01 | +0.01 / -0.14 | 254 |
| `6-12m` three years, 432 | 2 | +0.05 / +0.13 | -0.01 / -0.13 | +0.08 / +0.26 | -0.01 / -0.13 | 0 |
| | **3** | +0.07 / +0.21 | +0.04 / +0.12 | +0.07 / +0.21 | +0.01 / +0.00 (2) | 430 |

On the held-out year rank 3 improved at every horizon (-0.09 to -0.07, -0.15 to -0.04, -0.09 to +0.05 R per order observed), and so did the mean of all three. What it costs: at `6-12m` nearly every list is now all long (430 of 432 cases), and at `1-3m` 28%. That follows from the ranking, not from a view of the market: the simulation's 5% a year drift gives longs the higher edge on the paths, the more so the longer the horizon. That is why a one-sided list says so in its limits. Short setups that are still kept did no better than before (`1-3m` -0.15 observed on the held-out year). The same caveats apply as to the whole backtest: survivor tickers in a mostly rising market, overlapping cases, no costs; the held-out `6-12m` year was a strong rise, which flatters any rule that keeps more longs.

### The long run: 2005-2026, 76 tickers, rules version 3 unchanged

The sections above rest on three years of as-of dates (2022-2026) for twelve tickers that are still listed and mostly rose. To see whether the remaining errors (long setups at `6-12m` stated +0.08 R per order and observed +0.25; short continuations at `1-3m` stated +0.01 and observed -0.14) belong to the rules or to that market, the same code was run on twenty years of as-of dates and a broader universe, with no rule changed:

```bash
uv run python backtest_entries.py --universe backtest_universe.txt --horizon 1-2w 1-3m 6-12m --start 2005-01-03 \
    --by-year --by-regime --by-part --subset original --jobs 12
```

Report: [`examples/backtest_entries_2026-11-19_rules3_long.txt`](examples/backtest_entries_2026-11-19_rules3_long.txt) (universe version 1, sim method 2, pessimistic ordering, the default steps; as-of dates from 3 Jan 2005 to 30 Oct 2026 at `1-2w`, 14 Aug 2026 at `1-3m`, 13 Nov 2025 at `6-12m`). **122,540 cases, 367,610 setups.** It took 42 minutes with `--jobs 12` on a 15-core laptop (7.8 hours of CPU, so about 8 hours serially; the candles came from the cache); the three-year, twelve-ticker report takes 2 minutes with `--jobs 10` against 8 serially, and came out byte for byte identical to the committed one either way.

#### The universe file

[`backtest_universe.txt`](backtest_universe.txt) (version 1) lists 76 Yahoo symbols in named groups, each with its reason: `original` (the twelve tickers of every earlier report), `us-large` (12 US large caps across sectors), `us-fell-hard` (16 US large and mid caps with crashes or long declines: GE, C, AIG, INTC, BA, T, F, XRX, LUMN, KSS, M, VFC, CCL, SLB, CLF, AAL), `us-collapsed` (FNMA, FMCC, MBI, GNW: down 90% or more in 2008, still quoted), `us-delisted` (FRCB and BIGGQ, failed and still served up to their failure), `br-large` (12 B3 large caps) and `br-fell-hard` (18 B3 names that fell hard or for years, from steel and retail to Americanas, IRB, PDG, Light, Rossi). A line `TICKER@FIRST..LAST` restricts a symbol to its usable rows. Raise its `version:` with any change and name it with any result.

**Survivor bias is smaller, not gone.** Yahoo serves no history for most delisted, acquired or renamed-away symbols: of the failures and takeovers tried (Lehman, Washington Mutual, old GM, AMR, J.C. Penney, Rite Aid, Yellow, Sears, SVB, Bed Bath & Beyond, Credit Suisse, Chesapeake, Walgreens, US Steel, Nordstrom, Foot Locker, Hanesbrands, DISH, Activision, Twitter; on B3 OGX, MMX, Gol, Azul, Cielo, Natura &Co, BRF, JBS, Marfrig, Saraiva) none is served. So the losers here are the ones still quoted; listings that ended at zero are mostly missing, and every name was chosen in 2026 knowing how it did. Some large B3 losers (Gafisa, Oi, Braskem, GPA) were left out because Yahoo's rows for them are broken (unadjusted splits, adjusted prices thousands of times today's), which adds to the bias. The file lists every symbol tried and every cut.

**Data checks.** Every day-over-day close ratio beyond x1.6 or /1.6 since 1995 was looked at, and the long blocks of stale B3 quotes (one price repeated for weeks or years) were found by counting candles with no range. Real moves are kept (the 2008 collapses, FRCB's last weeks, the Ibovespa's -14% day of 26 Oct 2026); broken stretches are cut with `@` (for example SUZB3 before 2018, flat for ten years, and WEGE3 before mid-2005, which leaves the earlier twelve-ticker reports untouched). In addition the backtest skips any case with more than 20% candles without range in the 60 days before the as-of date or in its walk (`stale quotes`: 852, 263 and 35 cases at the three horizons). Two further skip reasons are failures of the live code, counted rather than hidden: a B3 date whose series would start at a local midnight that did not exist (Brazil's daylight saving time, 100 cases; `backtest.input_as_of()` cannot build it), and `trade_entries.build()` raising `ValueError: math domain error` (10, 72 and 256 cases), which happens on sub-real prices where a short's target zone reaches zero and `odds()` takes its logarithm. That last one is a bug in `trade_entries.py` that a live run on such a ticker would hit too; it was left for the rules' owner, and the cases are skipped.

#### The fit / held-out split

For any later change to the drift, the volatility or the setup rules, the long run is split once and for all (`HELD_OUT_YEARS` in `backtest_entries.py`): **held-out = cases whose whole window, as-of date to `valid_until`, lies in 2008-2009 or 2014-2016; fit = cases whose window touches none of those years; embargo = the rest, used by neither.** The held-out years hold a crash (2008), a rally (2009), a long Brazilian bear market and a commodity crash (2014-2015) and a recovery (2016), and all lie before Nov 2017, the earliest as-of date any earlier tuning (the sim drift, `SPREAD`, `EDGE_BAND`, the quality bands, the side quota) looked at. The fit part still contains the 2011, 2018, 2020 and 2022 sell-offs. The years were fixed before any long-run result was seen. Fit a parameter with `--part fit`, then run `--part held-out` once; `--by-part` shows both. The cases per part: `1-2w` 57,328 fit / 16,327 held-out / 434 embargo, `1-3m` 26,469 / 6,930 / 1,438, `6-12m` 9,903 / 1,763 / 1,948. The held-out numbers below were looked at for this description only; nothing was fitted on them.

#### What the current rules look like on it

Stated / observed, pessimistic ordering; R is per order (unfilled orders count 0R); "opt." the observed R per order under the optimistic ordering; Brier of the stated odds, then of always forecasting the group's own observed rate.

| Horizon | Tickers, setups | Fill st/obs | Win st/obs | R/order st/obs (opt.) | Brier fill | Brier win | R/order > 0 in |
|---|---|---|---|---|---|---|---|
| `1-2w` | all 76, 222,263 | 45% / 49% | 24% / 28% | -0.07 / -0.01 (+0.12) | 0.208 (0.250) | 0.186 (0.201) | 27 of 76 |
| | original 12, 37,908 | 46% / 48% | 24% / 29% | -0.07 / -0.00 | 0.207 (0.250) | 0.190 (0.204) | 5 of 12 |
| `1-3m` | all 76, 104,506 | 66% / 68% | 43% / 39% | +0.03 / -0.05 (+0.03) | 0.197 (0.218) | 0.230 (0.238) | 13 of 76 |
| | original 12, 18,372 | 65% / 69% | 43% / 42% | +0.03 / -0.00 | 0.192 (0.215) | 0.234 (0.244) | 3 of 12 |
| `6-12m` | all 74, 40,841 | 70% / 72% | 46% / 46% | +0.06 / +0.05 (+0.07) | 0.186 (0.204) | 0.238 (0.248) | 49 of 74 |
| | original 12, 7,422 | 70% / 74% | 46% / 51% | +0.07 / +0.15 | 0.177 (0.194) | 0.243 (0.250) | 11 of 12 |

By side and kind (R per order stated / observed, setups in brackets; "orig." is the original twelve over the same twenty years):

| Horizon | Long reversal | Long continuation | Short reversal | Short continuation |
|---|---|---|---|---|
| `1-2w` | -0.07 / +0.01 (133,484) | -0.09 / -0.22 (5,846) | -0.08 / -0.02 (78,384) | -0.07 / -0.19 (4,549) |
| `1-3m` | +0.01 / +0.01 (9,642) | +0.05 / -0.04 (59,957); orig. +0.05 / +0.03 | -0.03 / -0.11 (368) | +0.02 / -0.09 (34,539); orig. +0.02 / -0.13 |
| `6-12m` | +0.04 / +0.07 (13,152) | +0.08 / +0.05 (26,409); orig. +0.09 / +0.16 | -0.01 / +0.11 (71) | +0.01 / -0.08 (1,209) |

By regime and part, all 76 tickers (R per order observed: all / long / short continuation; continuation fill stated / observed):

| Horizon | Bull (index +10% or more over the past year) | Bear (-10% or less) | Index rose over the horizon | Index fell over the horizon | Fit | Held-out |
|---|---|---|---|---|---|---|
| `1-2w` | -0.01 / -0.00 / -0.20; 59% / 63% | -0.01 / +0.01 / -0.12; 53% / 53% | +0.02 / +0.10 / -0.20 | -0.06 / -0.14 / -0.18 | -0.01 / +0.00 / -0.21 | -0.01 / -0.01 / -0.15 |
| `1-3m` | -0.05 / -0.03 / -0.11; 68% / 69% | -0.08 / -0.04 / -0.12; 64% / 67% | -0.03 / +0.04 / -0.15 | -0.11 / -0.18 / +0.01 | -0.05 / -0.02 / -0.10 | -0.07 / -0.07 / -0.06 |
| `6-12m` | +0.06 / +0.07 / -0.05; 71% / 72% | +0.04 / +0.05 / -0.24; 67% / 76% | +0.09 / +0.10 / -0.12 | -0.09 / -0.10 / +0.13 | +0.07 / +0.07 / -0.06 | -0.09 / -0.09 / -0.11 |

Long setups at `6-12m` by year of the as-of date (observed R per order; stated +0.05 to +0.09 every year): 2005 +0.02, 2006 +0.32, 2007 -0.05, **2008 -0.26**, 2009 +0.21, 2010 +0.11, 2011 -0.03, 2012 +0.10, 2013 +0.09, 2014 +0.04, **2015 -0.12**, 2016 +0.21, 2017 +0.22, 2018 -0.05, 2019 +0.02, 2020 +0.09, 2021 -0.02, 2022 -0.05, 2023 +0.13, 2024 +0.04, 2025 +0.18.

What it shows:

- **The `6-12m` long excess was the sample, not the rules.** Over 76 tickers and twenty years the long setups at `6-12m` are calibrated on average (stated +0.06, observed +0.06; the long continuations +0.08 / +0.05). The original twelve over the same twenty years still observe +0.15 (+0.24 for their as-of dates from 2023): it is a property of those surviving names, most of all in 2023-2025. Around the mean, the result swings with the market path: -0.26 R per order for as-of dates in 2008, -0.12 in 2015, +0.32 in 2006, and -0.09 on the held-out part against +0.07 on the fit part. A 5% a year drift is right on average and says nothing about which year it is. The trailing regime known at the as-of date barely separates the outcomes (bull +0.07, bear +0.05); the index's move afterwards does (+0.10 against -0.10). Nothing in the rules can use that.
- **The short continuation loss holds in every regime.** At `1-3m` short continuations were stated +0.02 and observed -0.09 over all 76 tickers (-0.13 for the original twelve): -0.10 for as-of dates up to 2022 and -0.07 from 2023, -0.11 in bull regimes, -0.06 flat and -0.12 in bear regimes, and -0.06 on the held-out part. Only when the index actually fell over the horizon did they break even (+0.01). At `1-2w` they observed -0.19 (stated -0.07) and at `6-12m` -0.08 (stated +0.01, few cases), in bull and bear regimes alike. So this is a bias of the setups against the drift-free part of the path, not of the 2023-2026 rise: it survives bear markets, falling indices at the as-of date and the losers. The long continuations at `1-2w` lose as much (-0.22 against -0.09): breakout stops one daily move away are hit more than the paths say, on both sides.
- **The continuation fill under-statement is small and regime-independent, except in bear markets at `6-12m`.** Continuation stop orders filled 2-4 points more often than stated over the whole universe (`1-2w` 58% / 61%, `1-3m` 66% / 68%, `6-12m` 71% / 73%), the same in bull and flat regimes; the larger gap of the earlier report (61-64% stated against 71-78% at rules 1) belongs to the original twelve, whose continuations still fill 4-8 points more often than stated. In bear regimes the `1-2w` gap vanishes (53% / 53%) and the `6-12m` one grows (67% / 76%).
- **`1-3m` is the one horizon whose stated edge is too high everywhere.** Stated +0.03, observed -0.05 R per order, in all three regimes, in 21 of 22 years (not 2010) and on both parts; under the optimistic ordering +0.03. Its `win_if_filled` is stated 43% and observed 39%. Only 13 of 76 tickers had a positive mean R per order there.
- **`1-2w` stated edges are too low, as in the three-year report** (stated -0.07, observed -0.01 pessimistic and +0.12 optimistic), stable from year to year.
- **The stated odds still beat the constant forecast everywhere it matters.** Brier of `fill` is better than always forecasting the observed rate at every horizon and in every year of the `1-2w` and `1-3m` tables; `win_if_filled` beats it at all three horizons over the whole sample, but not in every year: at `6-12m` it was worse for as-of dates in 2006, 2007, 2008 and 2015, at `1-3m` in 2008.
- **The universe changes the conclusions, the years less so.** Comparing the original twelve with the whole universe over the same twenty years moves the `6-12m` long result from +0.15 to +0.06 and the `1-3m` result from -0.00 to -0.05; comparing 2005-2022 with 2023-2026 within one universe moves it much less, except for the original twelve at `6-12m`. By group, R per order at `1-3m` / `6-12m`: original -0.00 / +0.14, US large caps -0.01 / +0.08, US fell hard -0.06 / +0.03, US collapsed -0.09 / -0.09, B3 large caps -0.13 / +0.06, B3 fell hard -0.06 / -0.03.

The same caveats as the whole backtest apply, more strongly: the cases overlap heavily (a `6-12m` case every month for a year-long horizon), the three setups of a case share one path, the tickers move together, so the bootstrap intervals in the report are far too narrow; no costs, borrowing or dividends; mechanical zones and unadjusted probabilities. It checks the calibration of the stated odds of hypothetical setups and is not a strategy, a claim about returns or advice.

### Rules version 4: clustering and fat tails, not adopted

The held-out errors of rules 3 look like those a constant-volatility, thin-tailed path model would make: breakout (continuation) fills stated too low (`1-3m` 66% against 76%, `6-12m` 74% against 92%), reversal fills too high at `6-12m` (67% against 51%), simulated `1-2w` days that touch both the stop and the target more often than real ones, and a `6-12m` win Brier no better than the constant. The hypothesis tested: real prices have calm and volatile stretches (volatility clustering) and fat-tailed daily returns, and paths with both would move these odds in the right directions. It was **not better out of sample, so the rules stay version 3**: `"rules": 3`, `paths()`, `SPREAD` 0.65 and the bands above are unchanged. The candidate is kept as `trade_entries.garch_paths()`, which the rules never call, so the evaluation can be repeated.

**The candidate model** (`garch_paths()`): the same schedule as `paths()` (the projection's closes, its volatility per candle from `rz.vol()`'s `assumed`, its 5% a year drift), but the trading days are drawn directly instead of bridging `rz.walk()`'s closes. Each day's log return is `sigma × sqrt(h) × z + drift − h × sigma² / 2`, with `sigma` and `drift` the day's share of the candle's, `z` a Student-t with 5 degrees of freedom scaled to unit variance (fat tails), and `h` a GARCH(1,1) factor per path, `h' = (1 − α − β) + (α z² + β) h`, starting at 1 (a large move raises the next days' volatility, and the effect fades by β a day). The mean of `h` stays 1, so the mean variance of the paths equals the projection's: only how it is spread over paths and days changes. Daily highs and lows are Brownian-bridge extremes as before, with `SPREAD` times that day's own volatility. It draws from its own generator (seed 8); `rz.walk()` is called with one path for its schedule only, so its seed, its draws and every `rz.py`, `mechanical.py` and `backtest.py` output stay byte-identical (`tests/test_path_models.py` pins them by hash). It costs about the same as `paths()`: 0.19 s against 0.14 s for a `6-12m` input, 0.05 s against 0.04 s at `1-3m`.

**Parameters, fixed globally and fitted on data before the held-out years only.** The inputs at `1-3m` and `6-12m` hold weekly or monthly candles, so a daily volatility model cannot be estimated per input; the parameters are one pooled fit for all inputs. A GARCH(1,1) with Student-t days and variance targeting, fitted by maximum likelihood on a grid to the daily log returns of the twelve tickers from 2012 to 11 Nov 2024 (the earliest held-out start), gave α 0.08, β 0.90 and 5 degrees of freedom (per ticker, α 0.05 to 0.16 and β 0.81 to 0.93). Both parts mattered for the fit of daily returns: the log-likelihood rose by about 2,500 from clustering and about 1,900 from the t tails. `SPREAD` was then chosen again under the new model on the fit years (cases whose `valid_until` falls before 29 Oct 2025 at `1-2w`, 13 Aug 2025 at `1-3m`, 12 Nov 2024 at `6-12m`), by the rules 2 criterion: the sum of the Brier scores of `fill` and `win_if_filled` per horizon and kind over every valid setup.

| Fit years, every valid setup | SPREAD 0.5 | 0.6 | 0.65 | 0.7 | 0.8 | 1.0 |
|---|---|---|---|---|---|---|
| Rules 3, `paths()` | 2.483 | 2.476 | **2.475** | 2.476 | 2.484 | |
| GARCH(1,1), t(5) days (the candidate) | 2.497 | 2.488 | 2.485 | **2.484** | 2.487 | 2.512 |
| No clustering, t(5) days | | | 2.493 | | 2.499 | |
| GARCH(1,1), normal days | | | 2.485 | | 2.487 | |
| No clustering, normal days | | | 2.489 | | | |

Already on the fit years no variant beat rules 3 by this measure; the candidate (GARCH, t days, `SPREAD` 0.70) was the best of the new ones and the one judged on the held-out year. Its quality bands, re-measured by their method (the larger of |mean stated `edge.if_filled` − mean observed R per fill| of the picked setups under the two orderings, fit years, rounded up to 0.01): `1-2w` 0.30 (0.08 pessimistic, 0.30 optimistic), `1-3m` 0.05 (0.05, 0.04), `6-12m` 0.26 (0.26, 0.26).

**The held-out year**, run once (`--start` 29 Oct 2025, 13 Aug 2025, 12 Nov 2024, the same twelve tickers; reports [`examples/backtest_entries_2026-11-19_rules4_heldout.txt`](examples/backtest_entries_2026-11-19_rules4_heldout.txt) and, for all three years, [`examples/backtest_entries_2026-11-19_rules4.txt`](examples/backtest_entries_2026-11-19_rules4.txt); rules 3 is [`examples/backtest_entries_2026-11-19_rules3_heldout.txt`](examples/backtest_entries_2026-11-19_rules3_heldout.txt)). The picked setups, stated / observed, pessimistic ordering, "opt." the observed R per order under the optimistic ordering; the gap is |stated − observed| R per order:

| Held-out year, setups | Rules | Fill st/obs | Win st/obs | R per order st/obs (opt.) | Gap | Brier fill (constant) | Brier win (constant) |
|---|---|---|---|---|---|---|---|
| `1-2w`, 1,821 | 3 | 46% / 50% | 24% / 28% | -0.07 / +0.01 (+0.12) | 0.081 | **0.217** (0.250) | 0.190 (0.202) |
| | 4 candidate | 43% / 49% | 23% / 28% | -0.06 / +0.02 (+0.12) | 0.080 | 0.221 (0.250) | **0.189** (0.201) |
| `1-3m`, 936 | 3 | 65% / 73% | 43% / 44% | +0.04 / -0.00 (+0.06) | 0.038 | 0.178 (0.195) | **0.230** (0.246) |
| | 4 candidate | 65% / 72% | 40% / 43% | +0.02 / +0.01 (+0.07) | **0.015** | **0.173** (0.201) | 0.236 (0.244) |
| `6-12m`, 432 | 3 | 72% / 78% | 47% / 59% | +0.07 / +0.28 (+0.28) | 0.211 | 0.165 (0.172) | 0.248 (0.242) |
| | 4 candidate | 66% / 76% | 46% / 58% | +0.07 / +0.26 (+0.26) | **0.186** | **0.164** (0.183) | **0.244** (0.244) |

Every valid setup of the held-out cases, not only the three picked (about 5,300, 2,700 and 1,200): mean stated fill 55% → 54% against 60% observed at `1-2w`, 62% → 60% against 67% at `1-3m`, 66% → 64% against 69% at `6-12m`; Brier of `fill` 0.211 → 0.212, 0.190 → 0.195, 0.183 → 0.185 and of `win_if_filled` 0.209 → 0.210, 0.217 → 0.217, 0.207 → 0.208: worse at every horizon, slightly.

By kind, side and rank on the held-out year (fill and R per order, stated / observed; setups in brackets):

| Horizon | Rules | Reversal | Continuation | Long reversal | Short reversal | Long continuation | Short continuation | Rank 1 / 2 / 3, R per order |
|---|---|---|---|---|---|---|---|---|
| `1-2w` | 3 | 45%/50%, -0.07/+0.02 (1,759) | 64%/61%, -0.10/-0.17 (62) | -0.06/+0.04 (1,128) | -0.07/-0.02 (631) | -0.10/-0.18 (46) | -0.08/-0.14 (16) | +0.08 / +0.01 / -0.05 |
| | 4 | 43%/49%, -0.06/+0.02 (1,818) | 11%/0%, -0.05/+0.00 (3) | -0.06/+0.04 (1,066) | -0.06/-0.00 (752) | none | -0.05/+0.00 (3) | +0.07 / -0.01 / -0.00 |
| `1-3m` | 3 | 58%/58%, +0.01/+0.08 (122) | 66%/76%, +0.04/-0.01 (814) | +0.01/+0.08 (122) | none | +0.05/+0.04 (593) | +0.01/-0.14 (221) | +0.12 / -0.06 / -0.06 |
| | 4 | 49%/46%, +0.00/+0.17 (198) | 70%/79%, +0.03/-0.04 (738) | +0.00/+0.18 (188) | -0.01/+0.06 (10) | +0.04/+0.05 (506) | +0.01/-0.23 (232) | +0.04 / -0.01 / -0.01 |
| `6-12m` | 3 | 67%/51%, +0.05/+0.12 (152) | 74%/92%, +0.09/+0.37 (280) | +0.05/+0.12 (152) | none | +0.09/+0.37 (280) | none | +0.46 / +0.33 / +0.07 |
| | 4 | 55%/45%, +0.03/+0.06 (139) | 71%/91%, +0.10/+0.35 (293) | +0.03/+0.06 (139) | none | +0.10/+0.38 (285) | +0.01/-0.44 (8) | +0.45 / +0.30 / +0.03 |

`quality` on the held-out year (observed R per order, favourable / marginal / unfavourable, setups in brackets): rules 3 (±0.35, ±0.09, ±0.32) `1-2w` none / +0.01 (1,813) / -0.30 (8), `1-3m` -0.06 (241) / +0.02 (695) / none, `6-12m` +4.85 (1) / +0.27 (431) / none; candidate (±0.30, ±0.05, ±0.26) `1-2w` none / +0.02 (1,818) / +0.00 (3), `1-3m` +0.05 (264) / -0.02 (655) / +0.29 (17), `6-12m` +1.09 (11) / +0.24 (421) / none.

What it shows:

- **Not better where it matters, so not adopted.** The rule for adoption was: better out of sample on the Brier scores of `fill` and `win_if_filled` and on the R-per-order gap. The candidate narrowed the gap at `1-3m` (0.038 to 0.015) and `6-12m` (0.211 to 0.186) and tied at `1-2w`, but its `fill` Brier was worse at `1-2w`, its `win_if_filled` Brier worse at `1-3m`, and over every valid setup both Brier scores were worse at every horizon. The narrower gaps come largely from different picks (at `1-2w` the 62 continuations of rules 3 disappear, at `1-3m` more reversals are kept), not from better odds for a given setup.
- **The hypothesis has the wrong sign for fills.** At the same mean variance, fat tails and clustering put more of the paths' mass near the starting price and a little more far out. Most fill levels lie within about one horizon move, so the candidate states *lower* fills for every kind: over every valid setup of the held-out year, continuations at `1-3m` went from 65% to 63% stated (70% observed), the opposite of what the under-statement needed. Its picked `6-12m` reversals look better (55%/45% against 67%/51%) only because different, fewer reversals are picked.
- **The `1-2w` both-levels problem is not a tail problem.** Stated win at `1-2w` is still 23% against 28% observed, and the two orderings still span about 0.10 R (+0.02 to +0.12): calmer typical days with occasional large ones do not make real daily candles touch both levels less often in the simulation.
- **`6-12m` win Brier reached the constant (0.244 against 0.244), not better than it.** The rules 3 errors at `6-12m` and in long continuations are directional: the held-out years rose more than the 5% a year drift (long continuations observed 92% fills), and short continuations kept losing (`1-3m` -0.23 R per order under the candidate). A symmetric change to the shape of volatility cannot fix a drift error; that is the limit of zone-blind random paths, already named in [Known limits](#known-limits).
- The fit-year comparison, the bands and the held-out tables above come from a harness that ran `trade_entries.setups()`, `pick()` and `backtest_entries.walk()` on cached cases of the same grid; it reproduced the rules 3 held-out report exactly (same 1,821, 936 and 432 setups and the same numbers), and the candidate's held-out numbers equal the backtest reports. The same caveats apply as to the whole backtest: twelve survivor tickers in a mostly rising market, overlapping cases, no costs.

**On the long universe too.** The candidate was run again on the 76 tickers and twenty years of [the long run](#the-long-run-2005-2026-76-tickers-rules-version-3-unchanged), with the same command and dates (report: [`examples/backtest_entries_2026-11-19_rules4_long.txt`](examples/backtest_entries_2026-11-19_rules4_long.txt)). Brier scores are given with the constant forecast's in brackets, because the two rule sets pick different setups:

| Horizon | Rules | R/order st/obs | Brier fill | Brier win | Held-out Brier fill | Continuation fill st/obs (setups) | Short continuation R/order |
|---|---|---|---|---|---|---|---|
| `1-2w` | 3 | -0.07 / -0.01 | 0.208 (0.250) | 0.186 (0.201) | 0.207 (0.250) | 58% / 61% (10,395) | -0.07 / -0.19 |
| | 4 | -0.06 / -0.00 | 0.212 (0.249) | 0.184 (0.199) | 0.211 (0.249) | 29% / 33% (2,377) | -0.05 / -0.16 |
| `1-3m` | 3 | +0.03 / -0.05 | 0.197 (0.218) | 0.230 (0.238) | 0.206 (0.223) | 66% / 68% (94,496) | +0.02 / -0.09 |
| | 4 | +0.02 / -0.05 | 0.185 (0.207) | 0.228 (0.238) | 0.193 (0.212) | 69% / 73% (89,497) | +0.01 / -0.10 |
| `6-12m` | 3 | +0.06 / +0.05 | 0.186 (0.204) | 0.238 (0.248) | 0.184 (0.195) | 71% / 73% (27,618) | +0.01 / -0.08 |
| | 4 | +0.07 / +0.05 | 0.191 (0.211) | 0.235 (0.246) | 0.204 (0.201) | 71% / 74% (30,260) | +0.02 / -0.03 |

The verdict stands. The candidate is slightly better at `1-3m` (both Brier scores closer to zero relative to the constant, on fit and held-out years) and slightly better for `win_if_filled` everywhere. It is worse for `fill` at `1-2w`, and at `6-12m` on the held-out years its `fill` Brier is worse than the constant forecast (0.204 against 0.201), where rules 3 beat it. The R-per-order gap at `1-3m` barely narrows (0.08 to 0.07), the continuation fill under-statement widens there (66%/68% to 69%/73%), and the short continuation loss at `1-3m` is unchanged. Again the remaining errors are directional, not a matter of the shape of volatility.

### An empirical correction of the odds, per horizon, side and kind

The long run shows errors that keep their sign across years, regimes and parts: short continuations lose everywhere (`1-3m` stated +0.02 R per order, observed -0.09), long continuations are overstated at `1-3m` and `1-2w`, reversals are about calibrated or understated, and the `1-3m` stated edge is too high in 21 of 22 years. The zone-blind paths seem to under-represent what happens at zones, and a different path shape did not help ([rules 4](#rules-version-4-clustering-and-fat-tails-not-adopted)). The hypothesis tested here: a small correction of the stated odds, measured on past outcomes per horizon, side and kind, makes them better calibrated out of sample.

**Adoption criterion, written down before the held-out part was evaluated, and not changed afterwards.** The candidate (the correction and the quality bands re-measured under it) is fitted on the fit part of the long universe only. It is then run once on `--part held-out` of the same long run (76 tickers, the same as-of grid), and compared, per horizon, with rules 3 on the same held-out cases; the picked setups, pessimistic ordering. The correction is adopted at a horizon only if all three hold there:

1. **The R gap shrinks:** |mean stated `edge.per_order` − mean observed R per order| is smaller than rules 3's.
2. **Brier no worse:** for `fill` and for `win_if_filled` separately, the candidate's Brier score minus the Brier score of the constant forecast (always the group's own observed rate) is no more than 0.002 above the same difference for rules 3. Both raw scores and both constants are reported.
3. **The original twelve are not made worse on the R gap:** on the held-out cases of the original twelve tickers alone, the candidate's gap is no larger than rules 3's.

A horizon that fails any of them keeps rules 3 (its correction is zero); another horizon may still adopt. If no horizon passes, the rules stay version 3 and the candidate is written up as not adopted.

**The candidate, fixed on the fit part before the held-out part was evaluated:** per horizon, side and kind, a log-odds shift of `fill` and of `win_if_filled` and an additive shift of `edge.if_filled` (`CORRECTION` in `trade_entries.py`), with the quality bands re-measured under it on the fit part: `1-2w` 0.26, `1-3m` 0.09, `6-12m` 0.02.

**Result: adopted at `1-2w` and `1-3m`, not at `6-12m`. The rules are now version 5** (`"rules": 5`; version 4 was the GARCH candidate above, never shipped). At `6-12m` nothing is corrected and the band stays 0.32, so its setups are those of rules 3.

**How it was fitted.** A harness walked every valid setup of every case of the long run (`trade_entries.setups()`, all candidates, 1,049,364 in 122,876 cases over the three horizons, not only the three picked, so the fit does not depend on the picks it changes), recording the raw path odds and both walks. It reproduced `backtest_entries.py` exactly (the rules 3 report of a ticker, and the candidate's picks and numbers on 377 cases run through the code). On the fit part only, for each horizon, side and kind (reversal, continuation), under the pessimistic ordering:

- `fill`: a shift x in log-odds such that the mean of the shifted path fills equals the observed fill rate of the group's limit and stop orders (a market order's fill stays 1);
- `win_if_filled`: the same among the filled setups, against the observed win rate;
- `edge.if_filled`: + (mean observed R per fill − mean stated R per fill) of the group;
- each shrunk toward zero by n / (n + 100), n the cases in the group (over 26,000 at the two adopted horizons, 10,000 at `6-12m`, so the shrinkage barely acts), and rounded to 0.01. `edge.per_order` is the corrected `fill` × the corrected `edge.if_filled`; `detail.loss` and `detail.open` are scaled to fill what the corrected win leaves; the 5% minimum fill is judged on the paths' own count, before the correction; the ranking and `quality` use the corrected numbers.

Splitting reversals by order type (market or limit) was not done: market orders are 3% of the reversals. A variant that shifted only `edge.if_filled` was compared with the three-shift one on the fit part, by three-fold cross-validation over blocks of years: about the same R gap, and the three shifts gave better Brier scores at `1-2w` and `1-3m` (and they keep `fill` and `win_if_filled` from staying known-biased next to a corrected edge). The same cross-validation already showed `6-12m` as the weak horizon (R gap of the picks 0.006 under rules 3, 0.020 with the correction).

| Shifts fitted (fill / win in log-odds, edge in R) | Long reversal | Long continuation | Short reversal | Short continuation |
|---|---|---|---|---|
| `1-2w` (adopted) | +0.16 / +0.26 / +0.18 | +0.23 / −0.06 / −0.09 | +0.22 / +0.22 / +0.17 | +0.15 / −0.15 / −0.15 |
| `1-3m` (adopted) | +0.01 / +0.11 / +0.07 | +0.24 / −0.13 / −0.11 | +0.23 / −0.01 / +0.02 | −0.01 / −0.24 / −0.19 |
| `6-12m` (not adopted) | −0.04 / +0.08 / +0.06 | +0.18 / +0.02 / −0.01 | +0.19 / −0.09 / −0.02 | −0.06 / −0.03 / −0.07 |

They say what the long run said: continuations are overstated (their wins less often than the paths say, their edge 0.1-0.2 R lower) and reversals understated (0.17-0.18 R at `1-2w`), while stop and limit orders fill a little more often than on the paths. The quality bands, by their method (the larger of |mean stated `edge.if_filled` − mean observed R per fill| of the picked setups under the two orderings, fit part, rounded up to 0.01): `1-2w` 0.26 (0.02 pessimistic, 0.25 optimistic), `1-3m` 0.09 (0.00, 0.08); with market orders left uncorrected (below) they were measured again: `1-2w` 0.29 (0.00, 0.29), `1-3m` 0.09 (0.00, 0.09), and those are the bands shipped. Rules 3 measured the same way on the long fit part: 0.40, 0.13 and 0.03.

**Fit part** (in sample; 93,919 cases), the picked setups, stated / observed, pessimistic ordering, "opt." the observed R per order under the optimistic ordering; Brier with the constant forecast in brackets:

| Horizon, setups | Rules | Fill st/obs | Win st/obs | R/order st/obs (opt.) | Gap | Brier fill | Brier win | Original 12 gap |
|---|---|---|---|---|---|---|---|---|
| `1-2w`, 171,998 | 3 | 45% / 49% | 24% / 28% | −0.07 / −0.01 (+0.12) | 0.060 | 0.209 (0.250) | 0.186 (0.201) | 0.068 |
| | candidate | 54% / 54% | 32% / 32% | +0.01 / +0.00 (+0.15) | 0.009 | 0.202 (0.249) | 0.202 (0.217) | 0.003 |
| `1-3m`, 79,513 | 3 | 66% / 69% | 43% / 40% | +0.03 / −0.05 (+0.03) | 0.082 | 0.194 (0.216) | 0.229 (0.239) | 0.022 |
| | candidate | 63% / 62% | 38% / 38% | +0.01 / +0.01 (+0.07) | 0.000 | 0.200 (0.235) | 0.219 (0.236) | 0.050 |
| `6-12m`, 30,234 | 3 | 70% / 71% | 46% / 47% | +0.06 / +0.07 (+0.08) | 0.006 | 0.187 (0.206) | 0.238 (0.249) | 0.105 |
| | candidate | 68% / 68% | 45% / 44% | +0.07 / +0.07 (+0.08) | 0.008 | 0.191 (0.217) | 0.234 (0.247) | 0.096 |

**Held-out part, run once** (25,086 cases; 2008-2009 and 2014-2016), the same columns, then the three tests of the criterion:

| Horizon, setups | Rules | Fill st/obs | Win st/obs | R/order st/obs (opt.) | Gap | Brier fill | Brier win | Original 12 gap |
|---|---|---|---|---|---|---|---|---|
| `1-2w`, 48,993 | 3 | 45% / 48% | 24% / 28% | −0.07 / −0.01 (+0.12) | 0.063 | 0.207 (0.250) | 0.187 (0.201) | 0.075 |
| | candidate | 55% / 54% | 32% / 32% | +0.01 / +0.00 (+0.15) | **0.010** | 0.200 (0.248) | 0.201 (0.217) | **0.015** |
| `1-3m`, 20,889 | 3 | 64% / 66% | 43% / 38% | +0.04 / −0.07 (+0.01) | 0.102 | 0.206 (0.223) | 0.232 (0.235) | 0.103 |
| | candidate | 60% / 62% | 37% / 36% | +0.01 / −0.02 (+0.03) | **0.032** | 0.207 (0.235) | 0.214 (0.230) | **0.011** |
| `6-12m`, 5,376 | 3 | 70% / 73% | 45% / 38% | +0.07 / −0.09 (−0.06) | 0.165 | 0.185 (0.196) | 0.232 (0.235) | 0.208 |
| | candidate | 70% / 75% | 44% / 36% | +0.09 / −0.10 (−0.07) | 0.187 | 0.187 (0.186) | 0.228 (0.230) | 0.214 |

| Horizon | 1. Gap shrinks | 2. Brier − constant, fill / win (rules 3 → candidate) | 3. Original 12 not worse | Decision |
|---|---|---|---|---|
| `1-2w` | 0.063 → 0.010, yes | −0.042 → −0.049 / −0.014 → −0.016, yes | 0.075 → 0.015, yes | **adopted** |
| `1-3m` | 0.102 → 0.032, yes | −0.017 → −0.028 / −0.004 → −0.016, yes | 0.103 → 0.011, yes | **adopted** |
| `6-12m` | 0.165 → 0.187, no | −0.011 → +0.001 / −0.004 → −0.002, no | 0.208 → 0.214, no | not adopted |

By side and kind on the held-out part (R per order stated / observed, setups in brackets):

| Horizon | Rules | Long reversal | Long continuation | Short reversal | Short continuation |
|---|---|---|---|---|---|
| `1-2w` | 3 | −0.07 / −0.00 (29,054) | −0.09 / −0.24 (1,278) | −0.08 / +0.01 (17,463) | −0.07 / −0.15 (1,198) |
| | 5 | +0.02 / +0.00 (35,097) | −0.16 / +0.58 (16) | +0.00 / +0.00 (13,866) | −0.15 / −0.53 (14) |
| `1-3m` | 3 | +0.01 / −0.02 (1,648) | +0.05 / −0.08 (12,026) | −0.01 / −0.14 (106) | +0.02 / −0.06 (7,109) |
| | 5 | +0.02 / +0.00 (14,794) | −0.00 / −0.06 (4,867) | −0.03 / −0.09 (1,088) | −0.03 / −0.01 (140) |

`quality` and rank on the held-out part (observed R per order; setups in brackets): `1-2w` rules 3 (±0.35) marginal −0.01 (48,686), unfavourable −0.12 (307); rules 5 (±0.26) favourable −0.52 (48), marginal +0.00 (48,928), unfavourable +0.07 (17). `1-3m` rules 3 (±0.09) favourable −0.06 (4,713), marginal −0.07 (16,147), unfavourable −0.19 (29); rules 5 (±0.09) favourable −0.01 (1,557), marginal −0.02 (19,095), unfavourable −0.05 (237). Rank 1 / 2 / 3 at `1-3m`: rules 3 −0.07 / −0.08 / −0.04, rules 5 +0.01 / −0.01 / −0.06; at `1-2w` −0.01 / +0.00 / −0.02 and +0.00 / −0.00 / +0.00.

What it shows, and what it does not:

- **At `1-2w` and `1-3m` the stated edge is now about right on average, out of sample.** The R-per-order gap of the picks fell from 0.06 to 0.01 and from 0.10 to 0.03, on all 76 tickers and on the original twelve, and both Brier scores improved relative to the constant forecast. Most of it comes from the picks changing, not only from better numbers for a given setup: at `1-3m` the list moves from long and short continuations (rules 3: 92% of the picks) to long reversals (71%), and at `1-2w` the continuations nearly vanish. The short continuations that lost everywhere are now ranked low by their own corrected edge, and rarely kept.
- **It is a correction of averages per group, not of a setup.** Within a group the shift is the same for every setup; nothing new is known about a particular zone. `quality` still separates little: at `1-3m` favourable, marginal and unfavourable are now ordered (−0.01, −0.02, −0.05) but close, and at `1-2w` almost everything is `marginal`. Its raw Brier score of `fill` at `1-3m` is about equal to rules 3's (0.207 against 0.206) and better only against the constant, because the mix of picks changed.
- **Market orders are not corrected.** The group shift of a reversal was fitted mostly on limit orders (97% of the reversals), and in the first version of rules 5 a market order (the price already inside the zone) got the same win and edge shift without a fill shift. Over the whole long run that overstated it: `1-2w` market reversals stated +0.15 R per order against −0.07 observed (10,836 setups; rules 3 stated −0.02), every `1-2w` setup labelled `favourable` was one of them (279 setups, observed −0.16), and at `1-3m` +0.06 against −0.05 (2,906). This was found after the held-out evaluation, so it was not fitted: the correction was simply withdrawn from market orders, which are left as counted (as in rules 3), and the bands re-measured on the fit part. Over the whole long run with that change: `1-2w` market reversals stated +0.02 against −0.08 observed (3,751 picked), `favourable` with the 0.29 band only 4 setups (too few to judge), rank 1 +0.02 / +0.01, held-out R per order +0.00 / +0.00 and its `fill` Brier 0.206 (constant 0.249); `1-3m` market +0.01 / −0.04 (2,089), `favourable` +0.05 / +0.03, held-out +0.01 / −0.02, `fill` Brier 0.209 (0.236), `win` 0.214 (0.230). Market orders keep the slight overstatement they had under rules 3; a correction split by order type is the first thing a later version should fit, and judge on held-out data again.
- **At `6-12m` it failed every test.** Its held-out cases lie in 2008-2009 and 2014-2016, years in which long setups there lost (observed −0.09 R per order against +0.07 stated); a shift fitted on the fit part's average makes the longs look a little better still, and nothing measured per side and kind can say which year it is. That is the drift error named in the long-run section, not something a constant per group fixes.
- **The correction learns the fit part's market.** It carries every limit of the backtest it came from: survivor tickers chosen in 2026, no costs, borrowing or dividends, mechanical zones and unadjusted probabilities, a pessimistic order of events inside a day (under the optimistic ordering the corrected `1-2w` picks observe +0.15 R per order), and cases that overlap heavily. A setup file says when its odds were corrected, in `odds.basis` and in `confidence.limits`.

The long run under rules 5, with the same command and dates as rules 3's: [`examples/backtest_entries_2026-11-19_rules5_long.txt`](examples/backtest_entries_2026-11-19_rules5_long.txt) (one section per horizon, each run with its own `--end`, so the as-of grid is exactly rules 3's). Its fit and held-out rows equal the tables above. Over all parts and 76 tickers, R per order stated / observed: `1-2w` +0.01 / +0.00 (rules 3: −0.07 / −0.01), `1-3m` +0.01 / +0.01 (rules 3: +0.03 / −0.05), `6-12m` +0.06 / +0.05 (unchanged); Brier of `fill` and `win_if_filled` 0.201 (constant 0.249) and 0.202 (0.217) at `1-2w`, 0.202 (0.235) and 0.218 (0.234) at `1-3m`. There are 10, 70 and 251 more cases than in the rules 3 long report: that report skipped 10, 72 and 256 cases with `math domain error`, before the fix that discards a setup priced at zero or below.

### Result of 19 Nov 2026, rules version 1

Twelve tickers (AAPL, BBDC4.SA, ITUB4.SA, JPM, KO, MSFT, NVDA, PETR4.SA, SPY, VALE3.SA, WEGE3.SA, XOM), the default three-year ranges and steps, sim method 2, pessimistic ordering: **3,146 cases, 9,438 setups**, none skipped. The full report is in [`examples/backtest_entries_2026-11-19.txt`](examples/backtest_entries_2026-11-19.txt), kept for comparison; the numbers below are for rules version 1, which these results led to replace. "st/obs" is the stated mean, then the observed value; "R/order (optimistic)" is the same mean with the other ordering.

| Horizon, as-of dates, setups | Fill st/obs | Win if filled st/obs | R per fill st/obs | R per order st/obs | R/order (optimistic) | Ambiguous | 90% bootstrap, R/order |
|---|---|---|---|---|---|---|---|
| `1-2w`, 29 Oct 2023 to 29 Oct 2026, 5,421 | 57% / 62% | 60% / 30% | +0.42 / -0.32 | +0.25 / -0.20 | +0.04 | 17% | -0.22 to -0.18 |
| `1-3m`, 13 Aug 2023 to 13 Aug 2026, 2,721 | 62% / 72% | 65% / 43% | +0.52 / -0.03 | +0.34 / -0.02 | +0.04 | 3% | -0.05 to +0.01 |
| `6-12m`, 12 Nov 2022 to 12 Nov 2025, 1,296 | 61% / 71% | 65% / 47% | +0.52 / +0.10 | +0.33 / +0.07 | +0.08 | 0% | +0.03 to +0.12 |

By kind (all orders of a kind share one order type: reversals are `limit`, continuations `stop`; no `market` order occurred):

| Horizon | Kind, setups | Fill st/obs | Win st/obs | R per order st/obs |
|---|---|---|---|---|
| `1-2w` | continuation, 4,103 | 61% / 71% | 64% / 33% (53% optimistic) | +0.24 / -0.24 |
| `1-2w` | reversal, 1,318 | 46% / 33% | 37% / 11% | +0.28 / -0.05 |
| `1-3m` | continuation, 2,362 | 64% / 78% | 67% / 45% | +0.33 / -0.03 |
| `1-3m` | reversal, 359 | 52% / 35% | 38% / 18% | +0.40 / +0.02 |
| `6-12m` | continuation, 1,140 | 63% / 77% | 67% / 49% | +0.32 / +0.08 |
| `6-12m` | reversal, 156 | 47% / 28% | 35% / 14% | +0.40 / -0.00 |

By side: long setups did better than short ones at every horizon (R per order, long / short: `1-2w` -0.16 / -0.24, `1-3m` +0.04 / -0.13, `6-12m` +0.22 / -0.19).

By rank (R per order st/obs): `1-2w` #1 +0.36/-0.17, #2 +0.23/-0.21, #3 +0.16/-0.20; `1-3m` #1 +0.48/-0.02, #2 +0.31/+0.02, #3 +0.22/-0.05; `6-12m` #1 +0.49/+0.19, #2 +0.30/+0.10, #3 +0.20/-0.06.

By quality: 95% of all setups were `favourable` (5,155 of 5,421 at `1-2w`, 2,686 of 2,721 at `1-3m`, 1,265 of 1,296 at `6-12m`). `marginal` setups did worse than `favourable` ones (R per order -0.23, -0.20 and -0.27), but there were few of them, and almost no `unfavourable` ones.

Brier scores (0 is perfect; then always forecasting the observed rate): fill 0.215 (0.236), 0.193 (0.201), 0.195 (0.206); **win 0.296 (0.210), 0.282 (0.245), 0.274 (0.249)**, worse than the constant forecast at every horizon. Reliability of `win_if_filled` at `1-3m`: forecasts of 45%, 56%, 65% and 75% came true 27%, 37%, 40% and 52% of the time. Per ticker, R per order was positive in 0 of 12 tickers at `1-2w`, 5 of 12 at `1-3m` and 8 of 12 at `6-12m`.

**How to read it.**

- **`fill` is roughly calibrated, too low for continuations and too high for reversals.** Breakout stop orders filled more often than the projection's `reach` says (71-78% against 61-64%), reversal limit orders less often (28-35% against 46-52%). Fill is the one stated number that beats the constant forecast.
- **`win_if_filled` is much too high, as the template warns, and more than a ceiling would explain.** Observed wins are about half the stated value at `1-2w` and two thirds at `1-3m` and `6-12m`. The optimistic ordering narrows the gap only at `1-2w` (48% against 60%). Reversals are the worst: the zone "holds" in the projection's sense (the horizon's extreme close is made there), but the target is the first zone on the other side, which the price seldom reaches before the stop just beyond the zone, so 11-18% win against 35-38% stated.
- **The stated edge did not appear.** Every horizon states about +0.25 to +0.34 R per order; observed: -0.20 R at `1-2w` (still +0.04 under the optimistic ordering), about zero at `1-3m`, +0.07 R at `6-12m`. At `6-12m` the positive part is long setups in a rising market of survivor tickers; short setups lost there too.
- **The ranking is weakly informative.** Rank 1 has the highest stated edge, but only at `6-12m` did it do clearly better than ranks 2 and 3. `quality` separates almost nothing, because nearly everything is `favourable`.
- **Ambiguity matters at `1-2w`.** A daily candle often spans the whole zone between a continuation's entry and stop, so 22% of the `1-2w` continuation results depend on the unknown order inside a candle; the true value lies between the two bounds. At `1-3m` and `6-12m` the stops are wider than a day's range and the bounds nearly agree.

**Caveats** (printed with every report): mechanical zones and unadjusted probabilities, not Claude's; no events in the inputs; no commissions, spread, slippage, borrowing costs or dividends (prices are split-adjusted, not dividend-adjusted); the unknown ordering inside a daily candle, counted pessimistically by default; survivor tickers in a mostly rising market; cases that overlap in time, three setups per case on one path, and tickers that move together, so the bootstrap intervals are too narrow. This checks the calibration of the stated odds of hypothetical setups. It is not a strategy, not a claim about returns anyone could have had, and not advice.

To repeat it: `uv run python backtest_entries.py AAPL BBDC4.SA ITUB4.SA JPM KO MSFT NVDA PETR4.SA SPY VALE3.SA WEGE3.SA XOM --horizon 1-2w 1-3m 6-12m` (Yahoo's history may have been revised since, and the default ranges move with today's date; pass `--start`/`--end` for the dates above).

### Segmenting the setups: no situation held

Calibrated odds say what a setup is worth on average; the next question was whether some situation, known at the as-of date, does better than that average after costs. The search followed a protocol written before any data was read ([`examples/segmentation_2026-11-20/PROTOCOL.md`](examples/segmentation_2026-11-20/PROTOCOL.md); script, frozen segments and output in the same folder):

- **Data:** the three picked setups of every case of the long run (76 tickers, 2005-2026, rules 5, pessimistic ordering), from `backtest_entries.py --csv`.
- **Outcome:** R per order net of an assumed round-trip cost of 0.20% of the entry price, charged on filled setups, in R (0.002 x entry / risk).
- **Features, all known at the as-of close:** side, kind, order, rank, quality, ratio, stated fill, win and edge, risk and entry distance as % of price, the index's trailing 12-month regime, the ticker's own 3- and 12-month return, 3-month volatility, and close above or below its 200-day average. Numeric features were cut into terciles on the fit part.
- **Search, fit part only:** every value and every pair of values (about 2,700 segments), each with at least 1,000 setups on 150 dates. A segment passed only with a positive net mean in each of three blocks of fit years and t >= 3, its error clustered by as-of month.
- **Freeze, then judge once:** the best five passing (at most two per horizon) were written to `FROZEN.json`, then judged once on the held-out part. A segment held if net > 0 with t >= 2 there.
- **One amendment before the held-out part was read:** the universe group was dropped as a feature. Its first search was won almost entirely by `group = original`, the twelve tickers chosen in 2026 because they are still listed and mostly rose. That is survivorship, not a situation; that search is kept as `FROZEN_v1_with_group.json` and was never judged.

| Horizon | Fit part, net R per order (gross) | Segments tried / passing | Held-out, net R per order (gross) |
|---|---|---|---|
| `1-2w` | -0.06 (+0.01) | 878 / 0 | -0.06 (+0.00) |
| `1-3m` | -0.02 (+0.01) | 978 / 0 | -0.05 (-0.02) |
| `6-12m` | +0.04 (+0.07) | 857 / 230 | -0.12 (-0.09) |

The two frozen `6-12m` segments, net +0.12 and +0.11 R per order on the fit part, did not hold:

| Segment | Held-out setups | Held-out net (gross) | t |
|---|---|---|---|
| stated edge in its middle tercile, and a 12-month return in its top tercile | 349 | -0.02 (+0.01) | -0.3 |
| ratio and stated fill both in their top terciles | 538 | -0.17 (-0.13) | -2.8 |

What it shows:

- **At `1-2w` and `1-3m` the cost alone turns an even result into a loss.** Their stops are close, so 0.20% of the price is a large share of 1R.
- **At `6-12m` the passing segments were the fit years' rising market, cut different ways.** The held-out years (2008, 2009, 2014-2016) include falling markets, and every segment fell with them.
- **The setups' value is the calibration of their odds, not an edge.** No situation tried here beat the average out of sample.

The usual limits apply, more strongly: survivor tickers chosen in 2026, overlapping cases, mechanical zones, an assumed cost level, no borrowing costs or dividends, pessimistic ordering inside a day. It is a search for a calibration pattern in hypothetical setups, not a strategy, and its negative result is not advice either.

### A stronger zone rule

The setups take their prices from the zone edges, and the zones come from one fixed rule (`backtest.clustered_zones()`, [above](#backtest-of-the-baseline)) that treats every cluster of candidate prices alike: one stray pivot makes a zone as readily as five touches. The hypothesis tested here: zones with stronger evidence (more members in the cluster, at least one pivot, recent pivots) or of another width make reversals more reliable and targets better placed, so that the setups' mean R per order net of costs rises while their odds stay calibrated. Volume at a level cannot be tested: the backtest builds its inputs from daily rows without volume, so no candidate uses it.

**Adoption criterion, written down on 21 Nov 2026 before any held-out evaluation, and not changed afterwards.** One candidate rule is chosen and frozen on the fit part of the long run, with the setups' `CORRECTION` refitted and `EDGE_BAND` re-measured under it on the fit part by the methods of [the correction](#an-empirical-correction-of-the-odds-per-horizon-side-and-kind). It is then run once on the held-out part of the same long run (76 tickers, universe version 1, `--start 2005-01-03`, `--end` 30 Oct 2026, 14 Aug 2026 and 13 Nov 2025 per horizon, the default steps) and compared with the current rule under rules 5 as shipped (the current zones, `CORRECTION` and `EDGE_BAND`), on the cases for which both make setups; the three picked setups of each case, pessimistic ordering. Net R of a setup is its R minus, when it filled, an assumed round-trip cost of 0.20% of the entry price in R (0.002 × entry / risk), as in [the segmentation](#segmenting-the-setups-no-situation-held).

1. **Net R per order rises** (at `1-2w` and `1-3m`): the candidate's mean net R per order is at least 0.02 above the current rule's, and the difference has t ≥ 2. Its standard error clusters by as-of month: per month, the sum over each rule's picks of (net R − that rule's mean) / that rule's number of picks, candidate minus current; the error is the square root of the sum of the squared monthly totals.
2. **The R gap is kept** (every horizon): |mean stated `edge.per_order` − mean observed R per order| (gross) is no more than 0.01 above the current rule's.
3. **Brier is kept** (every horizon): for `fill` and for `win_if_filled` separately, the candidate's Brier score minus that of the constant forecast (always the group's own observed rate) is no more than 0.002 above the same difference for the current rule. Both raw scores and both constants are reported.
4. **The projection baseline is kept** (every horizon), because the zone rule also feeds `backtest.py` and every mechanical projection: on the twelve tickers of [the baseline backtest](#sim-method-2-a-small-drift-chosen-out-of-sample) (NVDA, AAPL, MSFT, SPY, JPM, XOM, PETR4.SA, VALE3.SA, ITUB4.SA, BBDC4.SA, ABEV3.SA, WEGE3.SA), the default dates and steps on the day of the run, sim method 2 and 200,000 paths, the Brier scores of `reach` and of `turn` are each no more than 0.002 above the current rule's. These dates lie after the held-out years, so this test uses no held-out data and may be run before the candidate is frozen.

The zone rule is one rule for all horizons, so it is adopted only if tests 1-4 all pass at `1-2w` and at `1-3m`, and tests 2-4 pass at `6-12m` (whose setups are not corrected and which only must not get worse). Otherwise the rule stays as it is and the candidate is written up as not adopted. **This is the third use of the held-out part** (after the rules 5 correction and the segmentation), so its result is weaker evidence than the first; to keep it from becoming a search, exactly one candidate is evaluated on it, once, and the frozen candidate (its parameters, refitted `CORRECTION` and bands) is committed before that run.

**Result: adopted. The zone rule is now 1.5 widths and at least two candidates per cluster, and the setup rules are version 6** (`"rules": 6`: the same rules as 5 on the new zones, with `CORRECTION` refitted and the bands 0.17 at `1-2w`, 0.05 at `1-3m`, 0.32 at `6-12m` unchanged). It passed every test at every horizon. **What it improves is narrow:** the gross R per order barely moves; the net result rises because a stop 1.5 times as far away makes the same 0.20% cost a smaller share of 1R. The net result is still negative at `1-2w` and `1-3m`. None of the evidence-strength filters tried (a pivot required, two members, the five nearest clusters ranked by members, recent pivots only) raised the R per order on its own; what they did was make the odds better calibrated against the constant forecast. Script, protocol, frozen candidate and every table: [`examples/zone_rule_2026-11-21/`](examples/zone_rule_2026-11-21/).

**How the candidate was chosen, on the fit part only** ([`PROTOCOL.md`](examples/zone_rule_2026-11-21/PROTOCOL.md)). A harness (`harness.py`) built every fit-part case of the long-run grid once, with twelve candidate rules side by side: every valid setup of `trade_entries.setups()` with its raw path odds, and both walks. With the current rule and the shipped `CORRECTION` it reproduced `backtest_entries.run_case()` exactly (140 of 140 cases checked), and refitting the correction on the current rule reproduced the shipped `CORRECTION` and the 1-2w band 0.29 exactly. For each candidate the correction was refitted and the bands re-measured by the methods of rules 5; the four tests were then computed on the fit part (test 4 on its own dates). The choice rule, written before the held-out run: among the candidates passing every test on the fit part, the one whose smallest margin (each test's margin divided by its tolerance) is largest. Two passed, width 1.5 with two members (smallest margin +0.32) and width 2 with two members (+0.16, its `1-3m` win Brier within 0.0003 of failing), so the first was frozen ([`FROZEN.json`](examples/zone_rule_2026-11-21/FROZEN.json), committed before the held-out run).

Fit part, every candidate against the current rule: net R per order difference (t), then the change of the R gap, of Brier minus the constant for `fill` / `win`, and of `backtest.py`'s Brier for `reach` / `turn`; `*` marks a failed test ([`fit_part.txt`](examples/zone_rule_2026-11-21/fit_part.txt)):

| Rule | `1-2w` | `1-3m` | `6-12m` | Passes |
|---|---|---|---|---|
| width 1.5 | +0.030 (12.4), +0.001, +0.0001 / −0.0073, −0.020 / −0.002 | +0.025 (6.1), −0.001, +0.0018 / +0.0030*, −0.016 / +0.0036* | +0.020 (3.6), +0.011*, −0.004 / −0.000, −0.020 / +0.001 | no |
| width 2 | +0.044 (14.0), −0.000, +0.0060* / −0.0087, −0.045 / −0.010 | +0.035 (6.4), −0.002, +0.0036* / +0.0043*, −0.035 / −0.002 | +0.018 (2.2), +0.006, −0.009 / −0.001, −0.041 / −0.009 | no |
| two members | −0.001 (−0.3)*, +0.003, −0.019 / −0.005, −0.016 / −0.011 | +0.005 (1.3)*, −0.002, −0.016 / −0.005, −0.013 / −0.008 | +0.011, +0.001, −0.010 / −0.009, −0.007 / −0.006 | no |
| pivot required | +0.003 (1.0)*, +0.002, −0.025 / −0.004, −0.022 / −0.025 | +0.017 (3.8)*, −0.001, −0.020 / −0.005, −0.012 / −0.019 | +0.013, +0.004, −0.013 / −0.008, −0.018 / −0.019 | no |
| last 10 pivots | +0.000 (1.1)*, all within 0.001 | +0.000 (0.0)*, all within 0.001 | −0.001, all within 0.001 | no |
| 3 of the nearest 5 by members | +0.000 (0.2)*, +0.003, −0.021 / −0.004, −0.013 / −0.007 | +0.004 (1.3)*, −0.002, −0.019 / −0.004, −0.013 / −0.005 | +0.009, +0.001, −0.009 / −0.006, −0.014 / −0.005 | no |
| **width 1.5, two members** | +0.027 (9.9), +0.001, −0.0097 / −0.0099, −0.032 / −0.008 | +0.027 (5.8), −0.000, −0.0038 / −0.0003, −0.025 / −0.002 | +0.021, +0.007, −0.011 / −0.008, −0.025 / −0.003 | **yes** |
| width 1.5, pivot required | +0.025 (8.9), −0.000, −0.024 / −0.007, −0.039 / −0.022 | +0.027 (5.7), −0.002, −0.008 / −0.000, −0.029 / −0.015 | +0.030, +0.018*, −0.015 / −0.006, −0.036 / −0.016 | no |
| width 2, pivot required | +0.038 (12.2), +0.001, −0.018 / −0.009, −0.059 / −0.024 | +0.037 (6.6), −0.001, −0.007 / +0.002, −0.047 / −0.018 | +0.026, +0.013*, −0.019 / −0.006, −0.053 / −0.021 | no |
| width 1.5, 3 of 5 by members | +0.025 (8.9), +0.000, −0.015 / −0.009, −0.035 / −0.010 | +0.023 (4.7), −0.001, −0.010 / −0.001, −0.031 / −0.004 | +0.023, +0.010*, −0.015 / −0.005, −0.032 / −0.007 | no |
| width 2, two members | +0.041 (12.1), −0.000, +0.0005 / −0.0087, −0.053 / −0.014 | +0.037 (6.3), −0.000, −0.0005 / +0.0017, −0.042 / −0.006 | +0.023, +0.008, −0.013 / −0.005, −0.044 / −0.011 | yes |

**The held-out part, run once** (`held_out.txt`, `judge.json`): the cases both rules made setups for, their three picks, pessimistic ordering. Net R is after the 0.20% cost; "opt." the gross R per order under the optimistic ordering; zones per projection counts the zones built from candidates (of six), and the width is the mean zone width as a share of the price.

| Horizon, setups | Rule | Fill st/obs | Win st/obs | R/order st / obs (opt.) | Net R/order | Gap | Brier fill (constant) | Brier win (constant) | Zones, width |
|---|---|---|---|---|---|---|---|---|---|
| `1-2w`, 48,993 | current (rules 5) | 53% / 52% | 30% / 30% | +0.005 / +0.004 (+0.148) | −0.059 | 0.001 | 0.206 (0.249) | 0.197 (0.211) | 5.41, 1.95% |
| 48,979 | new (rules 6) | 40% / 40% | 27% / 27% | +0.004 / +0.002 (+0.063) | −0.034 | 0.002 | 0.188 (0.239) | 0.177 (0.198) | 5.08, 2.91% |
| `1-3m`, 20,886 | current | 60% / 62% | 37% / 36% | +0.012 / −0.018 (+0.034) | −0.052 | 0.031 | 0.209 (0.236) | 0.214 (0.229) | 5.44, 4.23% |
| 20,844 | new | 55% / 56% | 35% / 34% | +0.031 / +0.001 (+0.033) | −0.022 | 0.030 | 0.216 (0.247) | 0.207 (0.225) | 5.11, 6.33% |
| `6-12m`, 5,367 | current | 70% / 73% | 45% / 38% | +0.073 / −0.092 (−0.063) | −0.118 | 0.165 | 0.185 (0.196) | 0.232 (0.235) | 5.46, 6.03% |
| 5,344 | new | 61% / 64% | 40% / 36% | +0.081 / −0.054 (−0.045) | −0.070 | 0.134 | 0.208 (0.229) | 0.221 (0.229) | 5.18, 9.02% |

| Horizon | 1. Net R per order (t) | 2. Gap | 3. Brier − constant, fill / win | 4. `backtest.py` Brier, `reach` / `turn` | Decision |
|---|---|---|---|---|---|
| `1-2w` | +0.026 (4.6), pass | 0.001 → 0.002, pass | −0.043 → −0.052 / −0.013 → −0.021, pass | 0.203 → 0.171 / 0.159 → 0.151, pass | pass |
| `1-3m` | +0.030 (3.3), pass | 0.031 → 0.030, pass | −0.026 → −0.031 / −0.016 → −0.018, pass | 0.202 → 0.176 / 0.165 → 0.163, pass | pass |
| `6-12m` | (+0.047, t 2.3; not a test) | 0.165 → 0.134, pass | −0.011 → −0.021 / −0.003 → −0.008, pass | 0.199 → 0.174 / 0.162 → 0.160, pass | pass |

Test 4 ran `backtest.py`'s cases (1,806, 906 and 432 on the twelve tickers, as-of dates up to 21 Nov 2026 less one horizon) with both rules on the same day. Its constants moved too, since the zones lie further out: `reach` against always forecasting its own rate 0.203 (0.249) → 0.171 (0.232) at `1-2w`, 0.202 (0.248) → 0.176 (0.246) at `1-3m`, 0.199 (0.250) → 0.174 (0.241) at `6-12m`; `turn` 0.159 (0.167) → 0.151 (0.165), 0.165 (0.174) → 0.163 (0.175), 0.162 (0.171) → 0.160 (0.171); the four scenarios together 0.165 → 0.146, 0.163 → 0.158, 0.160 → 0.161. The shipped code's own `backtest.py` reports under the new rule, one day later (the default dates moved by a day): [`backtest_baseline_2026-11-22_rules6.txt`](examples/zone_rule_2026-11-21/backtest_baseline_2026-11-22_rules6.txt), `reach` 0.168, 0.178, 0.171 and `turn` 0.151, 0.159, 0.158 at the three horizons.

The `CORRECTION` refitted on the new zones (fill / win in log-odds, edge in R): `1-2w` long reversal +0.17 / +0.37 / +0.16, long continuation +0.20 / −0.15 / −0.16, short reversal +0.19 / +0.32 / +0.15, short continuation +0.16 / −0.13 / −0.21; `1-3m` +0.05 / +0.18 / +0.08, +0.22 / −0.12 / −0.09, +0.19 / +0.02 / +0.02, +0.02 / −0.11 / −0.17. The bands by the rules 5 method on the fit part: `1-2w` 0.17 (0.003 pessimistic, 0.16 optimistic), `1-3m` 0.05 (0.004, 0.05); `6-12m` measured 0.04 there but, uncorrected as under rules 5, keeps its band 0.32 (its held-out gap is 0.13).

The long run under rules 6, with the same command and dates as rules 5's: [`examples/backtest_entries_2026-11-19_rules6_zones_long.txt`](examples/backtest_entries_2026-11-19_rules6_zones_long.txt); its fit and held-out rows equal the harness's (the same setups and Brier scores). Over all parts and 76 tickers, gross R per order stated / observed: `1-2w` +0.00 / +0.00 (rules 5: +0.01 / +0.01), `1-3m` +0.03 / +0.02 (+0.01 / +0.01), `6-12m` +0.07 / +0.07 (+0.06 / +0.05); Brier of `fill` and `win_if_filled` with the constant in brackets 0.187 (0.238) and 0.176 (0.198) at `1-2w` (rules 5: 0.208 (0.250), 0.197 (0.211)), 0.210 (0.247) and 0.212 (0.229) at `1-3m` (0.203 (0.236), 0.217 (0.234)), 0.208 (0.236) and 0.225 (0.243) at `6-12m` (0.187 (0.204), 0.238 (0.248)). Short continuations still lose (`1-2w` −0.17, `1-3m` −0.04, `6-12m` −0.05 R per order observed).

What it shows, and what it does not:

- **The net gain is a cost effect.** Gross R per order on the held-out part: `1-2w` +0.004 → +0.002, `1-3m` −0.018 → +0.001, `6-12m` −0.092 → −0.054; net −0.059 → −0.034, −0.052 → −0.022, −0.118 → −0.070. At `1-2w` all of the improvement is the cost, which is charged per fill in units of a risk now about 1.5 times as large; at `1-3m` about a third of it is gross. The hypothesis that stronger evidence makes reversals more reliable is not what the data show: requiring two members or a pivot, or ranking by members, did not raise R per order at `1-2w` and raised it by at most 0.02 at `1-3m` on the fit part.
- **The odds became better calibrated against the constant.** Wider zones of two or more candidates fill less often (`1-2w` 52% → 40%), and the stated fill and win separate fills from non-fills and wins from losses better than before, relative to the constant forecast, at all three horizons; the two orderings inside a candle also agree more (`1-2w` optimistic +0.148 against pessimistic +0.004 before, +0.063 against +0.002 now), because a stop further away is touched on the fill candle less often.
- **Fewer zones come from the rows.** About 5.1 of 6 zones per projection now have candidates behind them (5.4 before); the rest are one-width fillers, named as such in the projection's evidence and limits.
- **It is still not an edge.** Net of the assumed cost every horizon loses on the held-out years, as on the fit part at `1-2w`; the zones are wider, and the setups fill less often. This is the third use of the held-out years, after the correction and the segmentation, so the result is weaker evidence than a first look would be. The usual limits apply: survivor tickers chosen in 2026, overlapping cases, mechanical zones and unadjusted probabilities, an assumed cost level, no borrowing costs or dividends, pessimistic ordering inside a day. It is a calibration study of hypothetical setups, not a strategy or advice.
- **Claude's projections are not changed.** The skill still chooses its own zones (SKILL.md's widths between the two `typical_zone_width` numbers); the new rule is used by the mechanical engine, `backtest.py` and the setup backtest. The new width, 1.125 times the larger `typical_zone_width`, lies just above that range.

## Scoring the saved setups

The [setup backtest](#backtest-of-the-trade-setups) replays today's rules on past dates. `score_entries.py` checks the setups that real runs actually wrote, before the prices existed: the forward, out-of-sample check. Every run keeps its `entries.json` in the run folder and, next to the saved projection, in `projections/`.

```bash
uv run zones score-entries                      # every entries.json under projections/
uv run zones score-entries PETR4.SA             # one ticker
uv run zones score-entries projections/PETR4.SA/2026-11-18T1515_1-3m   # one saved folder
uv run zones score-entries --runs               # also the run folders under ./runs (--runs DIR for another)
uv run zones score-entries --optimistic         # the other bound of the ordering inside a candle
uv run zones score-entries --json               # one JSON object instead of the tables
```

Called directly it is `uv run python score_entries.py [FOLDER] [--ticker T] [--runs [DIR]] [--optimistic] [--json] [--no-cache]`. It is a command of its own rather than a mode of `zones score` because it scores another file with other rules and options (`--optimistic`, `--runs`) and reports per setup, not per probability.

- **The walk is the backtest's.** Each setup is played on the daily candles after its as-of day, up to its `valid_until`, by `backtest_entries.walk()` itself: touch fills, gaps at the open, market orders at the next open, the stop or target at its price, expiry at the last close, and the pessimistic ordering inside a candle unless `--optimistic`. Every setup is played both ways, and the other result is shown when a candle allowed two. The as-of day's own candle is never used, so a run made during a session leaves the rest of that session out.
- **Candles.** Daily candles once per ticker through `data_source.py` (the same download as `zones score`: retries, shared rate limit, cache of finished candles, `--no-cache` to skip it), rounded to the input's precision. A split after the as-of day, or prices that no longer match the input's last closed candle, mark the run as not scored.
- **Open and ended.** A setup is ended once its `valid_until` is past at the exchange. Before that it is shown as `OPEN ... PROVISIONAL`: a stop or target already hit is final, a filled setup still running has exit `open` and its R at the latest close, and an unfilled order may still fill. Only ended setups enter the summary. A `today` horizon cannot be scored this way (it ends on its as-of day).
- **Per setup.** Rank, side, kind, order, entry, stop and target; the stated `fill`, `win_if_filled` and `edge.per_order`; then the fill day, the exit (`target`, `stop`, `expiry`, `open` or none) and the result in R.
- **Summary, per rules version.** Files without a `rules` key are rules version 1 (odds from `reach` and `turn` by formulas), `"rules": 2` counts them on simulated paths; their odds mean different things and are never pooled. For each version: stated against observed fill, win and R per fill and per order, Brier scores of `fill` and `win_if_filled` next to the constant forecast at the observed rate, reliability buckets, a bootstrap over runs, and splits by horizon, side, kind, order, quality, rank, ticker and author. The author comes from the `entries` block of `provenance.json`: `computed` (`trade_entries.py build`), `claude` with its model (`--entries-by-claude`), or `unrecorded`.
- **Missing files.** A saved projection without `entries.json` (a run with `--no-entries`, or older than the step) is counted, not listed; an unreadable file, one that holds the `no setup passes` error, or a damaged one is listed with its reason and the rest are still scored. A run folder and its saved copy hold the same file, which is scored once.

```
PETR4.SA 1-3m  as of 2026-11-17T17:36-03:00 at 44.06  ENDED 2027-02-12
  setups computed by trade_entries.py; rules 2
  63 daily candle(s) after the as-of day, the last 2027-02-12
  # side  kind         order      entry      stop    target  fill  win R/ord  filled     exit         R  other
  1 long  reversal     limit         43        42        46  0.60 0.40 +0.36  2026-11-18 target   +3.00  same
  2 short reversal     limit         47        48        45  0.30 0.50 +0.15  2026-12-01 stop     -1.00  target +2.00
  3 long  continuation stop          50        49        52  0.10 0.40 +0.02  no         -        +0.00  same
```

(an example from the tests, on made-up candles.) Read it as the backtest's report and more so: it is a calibration check of the stated odds of hypothetical setups, with no commissions, spread, slippage or dividends; the three setups of a run share one price path, runs of one ticker overlap in time and the tickers are the ones you chose, so a few dozen setups are anecdote. It is not a strategy, not a claim about returns, and not advice.

## Does Claude's adjustment help?

Step 2 costs money (about $0.20 per run with Sonnet, more with Opus or web research), while the baseline it starts from (`rz.py sim`) is free. `value_test.py` measures whether the paid step earns its cost: it runs the real step 2 on past dates and scores Claude's numbers against the baseline on the **same zones**, so the comparison isolates Claude's adjustment.

```bash
uv run python value_test.py NVDA PETR4.SA SPY --out value_runs/sonnet_1-2w --max-cost 20 --jobs 4
uv run python value_test.py --out value_runs/sonnet_1-2w --max-cost 25     # resume: more cases, earlier runs not paid again
uv run python value_test.py --out value_runs/sonnet_1-2w --report-only     # score what is there, start no run
```

For each ticker and every `--step`-th trading day (default 10, about one `1-2w` horizon) it:

1. builds the input as of that day's close with `backtest.input_as_of()`: nothing after the day, `research: none` and no events, so Claude has no web tool and no later news. The input's `now.time` is in the past; Claude Code also tells the model today's date, and nothing else is said beyond the input;
2. runs step 2 through the pipeline (`run_pipeline.py --input FILE --no-entries --no-save --model MODEL --out-dir CASE`), which also runs `rz.py check`. Nothing is written to `projections/`;
3. recomputes the unadjusted baseline (`rz.baseline()`) on the zone edges Claude chose;
4. runs `backtest.py`'s mechanical zones and their baseline on the same input, which separates the choice of zones from the adjustment;
5. judges all three with `rz.outcome()` against the candles that followed, exactly as the backtest does.

The report gives the Brier score per kind (`reach`, `turn`, the four scenarios) for each version, the paired difference per case (mean over cases, with a 95% bootstrap interval that resamples whole cases, and how many cases each side won), and how often and how far Claude moved the numbers.

**Look-ahead.** A model has seen prices up to its training cutoff, so an as-of date before it leaks the outcome. `--start` must be after the cutoff (default: the first day after it), taken from Anthropic's [models overview](https://platform.claude.com/docs/en/about-claude/models/overview) ("Training data cutoff"; `TRAINING_CUTOFF` in the script), and a run whose model, as Claude Code reports it in `provenance.json`, has no known cutoff or a later one is not scored. A case is scored only when its horizon has ended.

**Budget.** The cost of each run is Claude Code's own `total_cost_usd` (from the run's provenance; else the cost on its summary line). A run starts only while the total spent in `--out`, plus a reserve for each run in flight and one more, stays within `--max-cost` (default $20); the reserve is the highest cost seen so far ($0.60 before the first). Everything goes into `--out` (`cases/TICKER/DATE/` with the input, projection, provenance, chart, `run.json`, `scores.json`; `data/` with the candles; `summary.json` and `report.txt`), and a case with a `run.json` is never run again, failed or not.

| Option | Default | Meaning |
|---|---|---|
| `TICKER ...` | the folder's own | Yahoo symbols. None: resume or report the tickers already in `--out`. |
| `--out DIR` | required | Where everything goes; resumes there. |
| `--horizon` | `1-2w` | `1-2w`, `1-3m` or `6-12m`. Longer horizons leave few dates after a recent cutoff. |
| `--start`, `--end` | the day after the cutoff; today minus the horizon and a week | Range of as-of dates. |
| `--step N` | `10` | Trading days between two as-of dates. |
| `--model` | `sonnet` | Model of step 2. |
| `--cutoff YYYY-MM` | from `TRAINING_CUTOFF` | The model's training data cutoff, for a model not in the table. |
| `--max-cost USD` | `20` | Total Claude Code cost allowed in `--out`, earlier runs included. |
| `--jobs N` | `1` | Runs of step 2 at the same time. |
| `--paths N` | `200000` | Simulated paths of the recomputed baselines. |
| `--report-only`, `--json`, `--quiet` | off | No new run; JSON output; no progress lines. |

### Result of 19 Nov 2026

Model `sonnet`, which Claude Code resolved to `claude-sonnet-5-5` in every run (training data cutoff June 2026). Horizon `1-2w`, as-of dates every 10 trading days from 1 Jul to 23 Oct 2026, twelve tickers (AAPL, BBDC4.SA, ITUB4.SA, JPM, KO, MSFT, NVDA, PETR4.SA, SPY, VALE3.SA, WEGE3.SA, XOM). 88 runs, all passed `rz.py check` and all were scored; 20 planned cases were left unrun by the budget. **Total cost: $19.85** (about $0.22 and 30-40 seconds per run). The full report is in [`examples/value_test_sonnet_1-2w_2026-11-19.txt`](examples/value_test_sonnet_1-2w_2026-11-19.txt).

Brier score, pooled over forecasts (lower is better):

| Kind | Forecasts | Claude | Baseline, same zones | Mechanical zones |
|---|---|---|---|---|
| `reach` | 625 | 0.198 | 0.198 | 0.206 |
| `turn` | 655 | 0.163 | 0.163 | 0.168 |
| Scenarios (all four) | 352 | 0.174 | 0.174 | 0.176 |

Paired difference per case (mean over 88 cases, 95% bootstrap interval; negative means the first is better):

| Kind | Claude minus baseline, same zones | Claude's zones minus mechanical zones |
|---|---|---|
| `reach` | 0.000 [0.000, 0.000] | -0.006 [-0.018, 0.007], 46 cases better, 42 worse |
| `turn` | 0.000 [0.000, 0.000] | -0.002 [-0.015, 0.010], 45 better, 43 worse |
| Scenarios | 0.000 [0.000, 0.000] | -0.002 [-0.020, 0.016], 49 better, 39 worse |

**What it shows.** With no research and no events, Sonnet 5.5 did not adjust the baseline at all: in all 88 cases every `reach`, `turn` and scenario equals the simulation's on its zones, and its `method` says why (the baseline is flat, chart levels predict little, no catalyst). So in this setting the paid step adds nothing to the probabilities beyond the choice of zones, and the adjustment's value cannot be measured because there is none. Claude's zones scored slightly better than the mechanical rule's, but well within the noise: every interval contains zero. Claude also returned fewer zones than requested on one side in 33 of 88 cases. The paid step's other output (evidence strings, strength labels, confidence limits) is not scored here.

Read it with the same care as the backtest: 88 cases over four months, overlapping dates across tickers that move together, survivor tickers, no events. It says nothing about runs with web research (`one_search`, `deep`), where catalysts are what the skill allows it to adjust for; that needs as-of dates after the cutoff and news as of those dates, which `value_test.py` does not provide. It describes calibration, not returns, and is not advice.

## Engines: claude or mechanical

Step 2 has two engines. `--engine claude` (the default) runs the skill through Claude Code. `--engine mechanical` runs `mechanical.py` instead, which builds the whole projection by fixed rules, with no Claude Code call:

| Part of the projection | `claude` (the skill) | `mechanical` (`mechanical.py`) |
|---|---|---|
| Zones | Chosen by Claude from `rz.py levels`, following SKILL.md | `backtest.py`'s zone rule (`clustered_zones()`, the one place it lives; [Backtest](#backtest-of-the-baseline), step 2) on the same facts, with the input's `want.supports`/`want.resistances` and prices at the input's precision |
| Probabilities | `rz.py sim`, then adjusted by at most 0.10 per side | `rz.py sim` with the default sim method, copied unadjusted, no shocks |
| Evidence | Written by Claude from the rows | The pivots, last-three-candle extremes, retracements and swing ends that formed each zone, with prices and candles; a zone that only fills a side with too few clusters says it has no fact behind it |
| Strength | Claude's reading of SKILL.md's rule | `strong` for three or more kinds of evidence (pivots, recent extremes, retracements, swing ends) or three tested candles; `moderate` for two; `weak` for one or none |
| Events | From the input, plus what research found | The input's events inside the horizon, passed through and not simulated (the input carries no sizes) |
| Drivers, research | Web research when `research` is not `none` | None: `drivers` is `[]` |
| Confidence | `low` or `medium` | Always `low`, with limits saying the numbers are mechanical and unadjusted, and naming events, filled zones and the nearest zone at risk |
| Cost, time | About $0.20-0.60 and one to six minutes | $0 and about a second (the run's time is the Yahoo download) |

Both write the same schema and pass the same `rz.py check`, then the run continues identically: saved history, chart, trade setups, summary and `provenance.json`. A mechanical projection's free text follows `reply.language` (English and Portuguese; other languages get English); the same input always gives the same file, byte for byte. Its `provenance.json` has `made_by: "mechanical"`, a `mechanical` block instead of `claude`, and a rules version over `mechanical.py`, `backtest.py`, `rz.py` and the other rule files (not `SKILL.md`), so `zones score` groups these projections as `mechanical`, apart from every model.

**Research.** The mechanical engine searches nothing. Without `--input` it builds the input with `--research none`, so the input records what was done; if a research level was typed or set in a config file it prints one warning and continues. A given `--input` that asks for research gets the same warning, and the projection's `confidence.limits` says the research was not done.

**When to use which.** The [value test](#does-claudes-adjustment-help) found that without web research Claude never changed the simulated probabilities (88 of 88 runs, Sonnet 5.5, `1-2w`), and its zones scored no better than the mechanical rule beyond noise. So for a watchlist scan with `research: none`, the mechanical engine gives the same kind of numbers for free, in a second, reproducibly. The paid engine still writes evidence and limits by reading the chart, and only it can do web research; whether research runs improve the probabilities has **not been measured** (`value_test.py` cannot give Claude news as of a past date). Use `claude` when you want the reading and the research, `mechanical` for breadth and daily scans. Neither is advice, and neither engine's probabilities are calibrated beyond what the [backtest](#backtest-of-the-baseline) describes.

```bash
uv run zones NVDA --engine mechanical --language pt
uv run python mechanical.py runs/NVDA/2026-11-19T1528_1-3m/input.json -o projection.json   # step 2 alone
```

## How the probabilities are produced

The helper simulates the future closes of the main series and counts how often each zone is reached or broken.

| Ingredient | Choice |
|---|---|
| Paths | 200,000, with a fixed random seed so identical inputs give identical baselines |
| Returns | Student-t with 5 degrees of freedom (fat tails), plus a fixed drift: the expected price grows 5% a year (none on intraday candles). The drift is added after the random draws, so the draws are the same as without it |
| Method version | `sim_method` in the output: 2 since Nov 2026, chosen out of sample ([Backtest of the baseline](#sim-method-2-a-small-drift-chosen-out-of-sample)); 1, the same without drift, stays available with `rz.py sim ... --method 1` |
| Volatility | Starts at the average of the last 13 and last 26 candles, decays linearly to the average of the last 52 and the whole series |
| Number of steps | 10 daily closes for `1-2w`, 13 weekly for `1-3m`, 12 monthly for `6-12m`, and the rest of the session for `today` |
| First step | Shortened when the newest candle is still forming |
| Scheduled shocks | `drop`: a cash dividend subtracted from the ex-date on. `jump`: an extra move on an event date, such as an options-implied earnings move |
| Measure | Closes for `close_in_zone`, intra-candle extremes for `touched_once` |

The simulation knows nothing about chart levels. The skill may therefore move a little probability, at most 0.10 in total on each side, typically from a tail into a zone with strong evidence. Every such move is written into `method`, which also names the sim method. The skill does not move probability for the general rise of prices (the drift already covers it) or for the recent trend alone (extrapolating it made the backtest worse).

## Glossary

| Term | Meaning |
|---|---|
| Zone | A price band where several independent facts from the candles coincide. |
| Near edge, far edge | The side of a zone closest to the current price, and the side furthest from it. |
| Reached | Price gets to the zone's near edge. |
| Broken | Price goes beyond the zone's far edge. |
| Tail | The open-ended entry beyond the last zone on a side (`below`, `above`). |
| Hold | `turn / reach`: the chance a zone holds once it has been reached. |
| Middle zone | The second zone on a side, or the only one. Scenarios are defined on it. |
| Breakdown | The middle support breaks and the middle resistance does not. |
| Breakout | The middle resistance breaks and the middle support does not. |
| Range | Neither breaks. |
| Whipsaw | Both break within the horizon. |

## Known limits

- **Mostly uncalibrated.** `backtest.py` checks the unadjusted baseline on past dates, with mechanically chosen zones, no events and twelve tickers ([Backtest of the baseline](#backtest-of-the-baseline)). `value_test.py` scored 88 real step-2 runs (Sonnet 5.5, `1-2w`, research `none`) on dates after the model's training cutoff: Claude never adjusted the baseline, and its zones were not measurably better than the mechanical rule's ([Does Claude's adjustment help?](#does-claudes-adjustment-help)). Projections made with web research, other models and longer horizons have not been checked against a meaningful number of outcomes: `score_projections.py` can compare saved projections with later prices, but the history is days old. This is the most important gap.
- **One drift for every asset, measured in a rising market.** Method 2's 5% a year was chosen on 2021-2026 data from twelve names that are still listed today; it helped there and in 2017-2021, but lost on cases made in 2018, and it ignores dividend yield (the prices are not dividend-adjusted, so a heavy payer's price drifts less). Long-horizon scenarios are still worse than the base rate, and trending names still get too few breakouts.
- **The trade setups' odds know nothing of the zones.** Since rules version 2 they are counted on the simulation's random paths, so they are about the distances of a setup, with the simulation's volatility and its 5% a year drift. On a held-out year of the [setup backtest](#backtest-of-the-trade-setups) the stated win rate beat a constant forecast at `1-2w` and `1-3m` but not at `6-12m`, and the stated edge per order missed the observed mean by 0.08 R at `1-2w` and `1-3m` and by 0.19 R at `6-12m` (too low, in a strong rise), against a target of 0.05 R. At `1-2w` the simulated days touch both stop and target more often than real days, so the stated edge sits below even the pessimistic bound. The width of the simulated daily range is one number chosen on two years of twelve survivor tickers. The paths keep a constant volatility schedule and Gaussian days; a version with volatility clustering and fat tails was tried and was not better out of sample, because the remaining errors are mostly directional (a rising held-out market against a 5% a year drift), which the shape of volatility does not change ([rules version 4, not adopted](#rules-version-4-clustering-and-fat-tails-not-adopted)). The measurement uses the mechanical engine only, with no costs, and the ordering inside a daily candle is unknown.
- **Few dates can test a model.** Only as-of dates after a model's training cutoff are fair, so `value_test.py` has a few months of dates for the current models, and almost none for `6-12m`. Its intervals stay wide until time passes.
- **Scoring a touch on a forming candle.** For `touched_once`, a finished candle does not say whether its high or low came before or after the projection. For the candle that was forming, the scorer counts an extreme only if it is new, otherwise it uses the close, so a touch inside that candle's earlier range can be missed. Everything else is judged exactly as the simulation counts it.
- **Pre-market and first-of-month projections.** When the newest candle is closed but a new one has not started (a daily projection made before the open, a monthly one before the month's first session), `horizon.first` skips the candle about to begin. The scorer counts it anyway, since it comes after the projection, and notes that there are more closes than scheduled.
- **Yahoo Finance is unofficial.** Quotes can be delayed about 15 minutes on some exchanges. Rate limits and server errors are [retried](#yahoo-finance-retries-timeout-rate-limit) for up to two minutes; past that the run stops. Each process's first request pays yfinance's handshake, about 10 s here.
- **The cache trusts old candles once checked.** Each use compares the five newest cached candles with Yahoo, which catches a split; a revision of older candles only is not noticed until `zones cache clear` or `--no-cache`. The rate limit spaces this project's calls on one machine, not other programs' requests or several machines.
- **CSV data is taken as given.** The row checks catch malformed rows, not wrong or dividend-adjusted prices, and a file has no trading hours, so `now.session` is `unsure`.
- **Not dividend-adjusted.** For heavy dividend payers, old levels sit higher than a total-return chart would show.
- **Holidays.** A week ending on a holiday Friday is labelled `forming` until the weekend.
- **24-hour markets.** The session and week-end logic assumes a Monday-to-Friday exchange, so crypto tickers get wrong `forming` labels.
- **Zone choice is judgment.** Two Claude runs on the same input can pick slightly different zone edges. The baseline probabilities for a given set of zones are reproducible, and `--engine mechanical` reproduces the whole projection, at the price of a fixed rule that reads no context and no news.
- **`--shape json` is missing.** The skill accepts `"shape": "json"` (projection only, no prose), but `build_prompt.py` does not offer it yet. Edit the field by hand if needed.

## Roadmap

1. Extend the backtest (`backtest.py`, which covers the unadjusted baseline): scheduled dividends as `drop` shocks, more tickers and sectors, and scoring the unadjusted simulation next to the skill's adjusted numbers for saved projections.
2. Re-check sim method 2 when a falling market has ended inside the backtest window, and try a drift net of each asset's dividend yield. Revisit a calibration layer only with enough history that its fitted maps stop tracking one period's average rise.
3. Measure the paid step where it can adjust: `value_test.py` with events or research as of the past date (a news archive cut at the as-of day), and with more dates as they pass the cutoff. Without research Sonnet 5.5 never adjusted; `--engine mechanical` is now the free path for such runs. Score saved mechanical projections against Claude's as the history grows (`zones score` groups them apart).
4. Trade setups (rules version 2 counts the odds on simulated paths): check the simulated daily range against real daily ranges per ticker instead of one `SPREAD`, which would close most of the `1-2w` gap; judge again when a held-out year is not a strong rise; and try the zones themselves in the odds (paths conditioned on a zone holding, measured as `backtest.py` measures `turn`) only if the backtest shows the zones add anything beyond distance. Any such change is a new rules number, a change to the template and `setups()`/`pick()` together, and judged out of sample. Run `zones score-entries` as the saved setups' horizons end: the first forward check of the stated odds, per rules version.
5. Add `json` to `--shape`.
6. Carry event sizes as numbers (dividend amount, implied move) so the skill does not have to search for them.
7. State `dividend_adjusted` explicitly in the input.
8. Data: done are retries, a timeout, a shared rate limit, a cache of finished candles, row checks and CSV import (`data_source.py`). Left: another data vendor behind the same interface, corporate actions in the CSV format (split dates, so unadjusted files can be adjusted here), and an occasional full check of older cached candles.

## Project layout

```
stock_predict_zones/
├── build_prompt.py                 step 1: Yahoo Finance to input JSON
├── plot_projection.py              step 3: input + projection to image
├── run_pipeline.py                 the `zones` command: all steps in one (step 2 through headless Claude Code), and list, runs, score, score-entries, plot, show, entries, backtest, backtest-entries, batch, cache
├── score_projections.py            saved projections against later prices, and the calibration summary
├── backtest.py                     the baseline probabilities on past dates: mechanical zones, rz.py sim, rz.outcome
├── backtest_entries.py             the trade setups on past dates: mechanical engine, trade_entries.py, a walk on daily candles
├── score_entries.py                the saved trade setups (entries.json) against later prices, with backtest_entries.walk()
├── mechanical.py                   step 2 without Claude (--engine mechanical): backtest.py's zones, unadjusted rz.py sim
├── value_test.py                   paid: Claude's step 2 against the baseline on the same zones, after the model's cutoff
├── trade_entries.py                trade setups: prices from zone edges, odds on simulated paths; fills the prompt and checks the reply
├── show_run.py                     one run as a text summary in the terminal
├── batch_runs.py                   `zones batch`: the pipeline over a watchlist, with summary.csv
├── data_source.py                  every price request: retries, timeout, shared rate limit, candle cache, row checks, CSV import
├── .claude/skills/reversal-zones-projection/
│   ├── SKILL.md                    step 2: the skill
│   └── scripts/rz.py               its helper: levels, simulation, validation, saving, outcomes
├── examples/                       one complete run: input, projection and charts; reports of the value test and the setup backtest; segmentation_2026-11-20/ (the segment search); zone_rule_2026-11-21/ (the zone-rule study)
├── prompts/
│   ├── trade_entries.md            prompt: three ranked trade setups from one run
│   └── trade_entries.schema.json   JSON Schema of its reply
├── projections/                    every projection made, with its input and provenance (local, not committed)
├── runs/                           one folder per `zones` run: input, projection, chart, setups, provenance; runs/batch/ for batches (local, not committed)
├── pyproject.toml                  dependencies, Python version and the `zones` command
├── uv.lock
└── README.md
```

## Disclaimer

This project produces probabilistic estimates for study and discussion. It is not investment advice and not a recommendation to buy or sell any security.
