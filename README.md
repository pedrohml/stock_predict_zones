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

> **Read this first.** The probabilities are internally consistent, but only their unadjusted baseline has been backtested, on twelve tickers and mechanically chosen zones (see [Backtest of the baseline](#backtest-of-the-baseline)). The numbers a real projection shows, with Claude's zones and adjustments, have not been checked against outcomes. Treat the output as a structured way to look at a chart, not as a forecast you can rely on. Nothing here is investment advice.

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

Step 2 needs Claude with the skill in `.claude/skills/reversal-zones-projection/`. Claude Code picks it up automatically when run in this repository; other Claude apps need it installed. See [Installing the skill](#installing-the-skill).

## Quick start

One command, from ticker to chart and ranked trade setups (needs [Claude Code](https://claude.com/claude-code) installed and signed in):

```bash
uv run zones PETR4.SA --open          # plain `zones PETR4.SA --open` after the global install
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
| `model`, `timeout`, `quiet`, `entries_by_claude` | Claude Code's own, `1800`, `false`, `false` | `--model`, `--timeout`, `--quiet`, `--entries-by-claude` |
| `entries`, `save` | `true`, `true` | `false` is `--no-entries`, `--no-save` |
| `theme`, `lang`, `candles`, `dpi` | [step 3](#step-3-plot_projectionpy)'s own | the chart options; `lang` overrides `language` for the chart's labels only |
| `open` | `false` | `--open` |
| `summary` | `true` | `--summary` (`--no-summary` for one run) |
| `runs_dir` | `runs/` in the current directory | `--runs-dir DIR` |
| `history_dir` | `$RZ_PROJECTIONS_DIR`, else `projections/` in the current directory | `--history-dir DIR` |
| `cache_dir` | `$ZONES_CACHE_DIR`, else `$XDG_CACHE_HOME/zones`, else `~/.cache/zones` | `--cache-dir DIR`; the [candle cache](#the-candle-cache) and the shared rate-limit file |

What belongs to one run stays on the command line: the ticker, `--event`, `--prices`, `--no-cache`, `--input`, `--projection`, `--name`, `--chart`, `--out-dir`. There are no per-ticker settings.

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
| `zones plot WHAT [plot options]` | Draws the chart of a run again. | `plot_projection.py` |
| `zones show WHAT` | Prints the summary of a run. | `show_run.py` |
| `zones entries WHAT` | Computes the [trade setups](#ranked-trade-setups) of a run again and checks them. | `trade_entries.py build`, then `check` |
| `zones backtest TICKER... [options]` | [Backtests the baseline probabilities](#backtest-of-the-baseline) on past dates. Downloads from Yahoo Finance; no Claude call. | `backtest.py` |
| `zones batch FILE\|TICKER... [options]` | One pipeline run per ticker of a watchlist, a few at a time, with a cost cap and a `summary.csv`. See [Batch runs](#batch-runs). | the pipeline, once per ticker |
| `zones cache [info\|clear\|path] [TICKER...]` | The [candle cache](#the-candle-cache): what it holds, delete it (all or some tickers), or print its folder. | `data_source.py` |

```bash
zones list                                   # every saved projection
zones score PETR4.SA --json                  # one ticker, as JSON
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

**Cost.** The cost of a run is the sum of what Claude Code reported for its calls, as the run printed it (to the cent). A run that made no Claude Code call (it failed at step 1) cost nothing. A call that reported no cost, or never finished (a timeout), makes the run's cost unknown: the table shows `?`, `summary.csv` leaves `cost_usd` empty and says why in `cost_note`, and the cap does not count it. Nothing is estimated.

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

By default the step makes no Claude Code call. Every price, payoff and probability follows from the projection by fixed rules, so `trade_entries.py build` computes them, and writes the sentences around them (`anchor`, `basis`, `why`, `watch`, the limits) from fixed wording in the language of `reply.language` (English or Portuguese; any other language gets English). It takes under a second and costs nothing.

With `--entries-by-claude`, Claude writes those sentences instead, for this projection in particular:

1. `trade_entries.py prompt` fills `prompts/trade_entries.md` with the input and the projection.
2. `claude -p` answers with one JSON object. This call gets no tools at all and runs outside the repository; in three real runs it took two to three minutes and reported a cost of $0.50 to $0.70.
3. `trade_entries.py check` validates the reply against `prompts/trade_entries.schema.json` and recomputes every price, payoff and probability from the projection. A reply that disagrees is kept for inspection, reported, and the run exits 1; the input, projection and chart are unaffected.

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

- **Prices come from zone edges only.** A *reversal* enters at a zone's near edge and targets the first zone on the opposite side. A *continuation* enters where a zone breaks and targets the far edge of the next one. Stops sit a quarter of one candle's typical move beyond the zone.
- **Prices keep the candles' precision.** Stops, risk, reward and the units are rounded to the most decimals the input's rows show (at least two), so an asset priced around 0.04 written with `--decimals 5` gets the same setups as one priced at 40. A setup whose risk still rounds to zero is discarded; when none is left the file holds `{"task": "trade_entries", "error": "no setup passes the rules for this projection"}`.
- **Odds come from the projection.** `fill` and `win_if_filled` are computed from `reach` and `turn` by fixed formulas, and each entry's `basis` shows the numbers used.
- **Ranking** is by `edge.per_order`: the expected result in units of risk, counting orders that never fill as zero.

Read the limits before using the output:

- `win_if_filled` is a ceiling. The projection knows whether a zone held or broke by the end of the horizon, not whether the stop was touched before the target.
- The formulas are a heuristic layered on probabilities that are themselves not backtested. Continuation setups get no distance discount, so they tend to rank above reversals.
- There is no position size or leverage, and a setup marked `favourable` is not a recommendation.

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
2. **Zones.** Three supports and three resistances, chosen by a fixed rule from the output of `rz.py levels` (`choose_zones()`, the only place the rule lives): the zone width is the mean of the two `typical_zone_width` numbers; the candidates are the prices `levels` reports as evidence (pivot lows and highs, the lows and highs of the last three candles, the retracement levels, the two ends of the dominant swing); walking outward from the price, a candidate within one width of the first member of the current cluster joins it, and each cluster becomes one zone of that width centred on the mean of its members, cut at the price and pushed outward so it never overlaps the zone before it; the nearest clusters are kept; a side with too few clusters is filled with one-width zones, one width apart. It is not what Claude would choose: it is a reproducible baseline.
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

- **Mostly uncalibrated.** `backtest.py` checks the unadjusted baseline on past dates, with mechanically chosen zones, no events and twelve tickers ([Backtest of the baseline](#backtest-of-the-baseline)).
- **One drift for every asset, measured in a rising market.** Method 2's 5% a year was chosen on 2021-2026 data from twelve names that are still listed today; it helped there and in 2017-2021, but lost on cases made in 2018, and it ignores dividend yield (the prices are not dividend-adjusted, so a heavy payer's price drifts less). Long-horizon scenarios are still worse than the base rate, and trending names still get too few breakouts. The probabilities of a real projection, with Claude's zones and adjustments, have not been checked against a meaningful number of outcomes: `score_projections.py` can compare saved projections with later prices, but the history is days old. This is the most important gap.
- **Scoring a touch on a forming candle.** For `touched_once`, a finished candle does not say whether its high or low came before or after the projection. For the candle that was forming, the scorer counts an extreme only if it is new, otherwise it uses the close, so a touch inside that candle's earlier range can be missed. Everything else is judged exactly as the simulation counts it.
- **Pre-market and first-of-month projections.** When the newest candle is closed but a new one has not started (a daily projection made before the open, a monthly one before the month's first session), `horizon.first` skips the candle about to begin. The scorer counts it anyway, since it comes after the projection, and notes that there are more closes than scheduled.
- **Yahoo Finance is unofficial.** Quotes can be delayed about 15 minutes on some exchanges. Rate limits and server errors are [retried](#yahoo-finance-retries-timeout-rate-limit) for up to two minutes; past that the run stops. Each process's first request pays yfinance's handshake, about 10 s here.
- **The cache trusts old candles once checked.** Each use compares the five newest cached candles with Yahoo, which catches a split; a revision of older candles only is not noticed until `zones cache clear` or `--no-cache`. The rate limit spaces this project's calls on one machine, not other programs' requests or several machines.
- **CSV data is taken as given.** The row checks catch malformed rows, not wrong or dividend-adjusted prices, and a file has no trading hours, so `now.session` is `unsure`.
- **Not dividend-adjusted.** For heavy dividend payers, old levels sit higher than a total-return chart would show.
- **Holidays.** A week ending on a holiday Friday is labelled `forming` until the weekend.
- **24-hour markets.** The session and week-end logic assumes a Monday-to-Friday exchange, so crypto tickers get wrong `forming` labels.
- **Zone choice is judgment.** Two runs on the same input can pick slightly different zone edges. The baseline probabilities for a given set of zones are reproducible.
- **`--shape json` is missing.** The skill accepts `"shape": "json"` (projection only, no prose), but `build_prompt.py` does not offer it yet. Edit the field by hand if needed.

## Roadmap

1. Extend the backtest (`backtest.py`, which covers the unadjusted baseline): scheduled dividends as `drop` shocks, more tickers and sectors, and scoring the unadjusted simulation next to the skill's adjusted numbers for saved projections.
2. Re-check sim method 2 when a falling market has ended inside the backtest window, and try a drift net of each asset's dividend yield. Revisit a calibration layer only with enough history that its fitted maps stop tracking one period's average rise.
3. Add `json` to `--shape`.
4. Carry event sizes as numbers (dividend amount, implied move) so the skill does not have to search for them.
5. State `dividend_adjusted` explicitly in the input.
6. Data: done are retries, a timeout, a shared rate limit, a cache of finished candles, row checks and CSV import (`data_source.py`). Left: another data vendor behind the same interface, corporate actions in the CSV format (split dates, so unadjusted files can be adjusted here), and an occasional full check of older cached candles.

## Project layout

```
stock_predict_zones/
├── build_prompt.py                 step 1: Yahoo Finance to input JSON
├── plot_projection.py              step 3: input + projection to image
├── run_pipeline.py                 the `zones` command: all steps in one (step 2 through headless Claude Code), and list, runs, score, plot, show, entries, backtest, batch, cache
├── score_projections.py            saved projections against later prices, and the calibration summary
├── backtest.py                     the baseline probabilities on past dates: mechanical zones, rz.py sim, rz.outcome
├── trade_entries.py                trade setups: computes them, or fills the prompt and checks the reply
├── show_run.py                     one run as a text summary in the terminal
├── batch_runs.py                   `zones batch`: the pipeline over a watchlist, with summary.csv
├── data_source.py                  every price request: retries, timeout, shared rate limit, candle cache, row checks, CSV import
├── .claude/skills/reversal-zones-projection/
│   ├── SKILL.md                    step 2: the skill
│   └── scripts/rz.py               its helper: levels, simulation, validation, saving, outcomes
├── examples/                       one complete run: input, projection and charts
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
