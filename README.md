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

> **Read this first.** The probabilities are internally consistent but have **not been backtested**. Nobody has yet checked whether a zone given a 20% chance of holding actually holds about 20% of the time. Treat the output as a structured way to look at a chart, not as a forecast you can rely on. Nothing here is investment advice.

## Contents

- [Setup](#setup)
- [Quick start](#quick-start)
- [Step 1: build_prompt.py](#step-1-build_promptpy)
- [Step 2: the reversal-zones-projection skill](#step-2-the-reversal-zones-projection-skill)
- [Step 3: plot_projection.py](#step-3-plot_projectionpy)
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

`uv sync` installs the four declared dependencies: `yfinance`, `matplotlib`, `numpy` and `pandas`.

Tests: `uv run pytest` runs the offline suite in `tests/` (no network, a few seconds). It does not exercise the Yahoo Finance download.

Step 2 needs Claude with the skill in `.claude/skills/reversal-zones-projection/`. Claude Code picks it up automatically when run in this repository; other Claude apps need it installed. See [Installing the skill](#installing-the-skill).

## Quick start

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
uv run python plot_projection.py examples/petr4_input.json examples/petr4_projection.json -o chart.png --lang en
```

## Step 1: build_prompt.py

Downloads candles from Yahoo Finance and writes one JSON holding the data and the request.

```bash
uv run python build_prompt.py TICKER [options]
```

`TICKER` is a Yahoo symbol, for example `NVDA`, `PETR4.SA` or `BTC-USD`. Without `-o` the JSON goes to standard output.

### Options

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
| `--language` | `en` | Language of free-text values in the projection (`pt`, `en`, ...). |
| `--links` | `decisive_only` | Links in the prose: `none`, `decisive_only`, `all`. |
| `--decimals N` | `2` | Price rounding. |
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
  "reply": { "shape": "table", "max_words": 200, "language": "en", "links": "decisive_only" }
}
```

| Field | Notes |
|---|---|
| `now.session` | `open`, `closed`, `pre_market` or `after_hours`, from Yahoo's trading-period data. |
| `data.columns` | Shared by every series. Rows are compact arrays, oldest first, to keep the file small. |
| `series[].last_row` | `forming` when the newest candle is still being built, otherwise `closed`. A forming candle is not a signal yet. |
| `data.split_adjusted` | Prices are adjusted for splits but **not for dividends**, which matches what most charting platforms show. |
| `events` | Earnings and ex-dividend dates inside the horizon, from Yahoo's calendar, plus anything passed with `--event`. An empty list means none are known. |
| `want.probability` | `close_in_zone`: judged on candle closes. `touched_once`: judged on intra-candle extremes. |

## Step 2: the reversal-zones-projection skill

The skill lives in `.claude/skills/reversal-zones-projection/`. `SKILL.md` tells Claude how to turn an input JSON into a projection JSON, and `scripts/rz.py` is the helper that does the arithmetic.

### Installing the skill

In Claude Code there is nothing to install: the skill sits in `.claude/skills/`, so it is available whenever Claude Code runs in this repository.

For other Claude apps, add the `.claude/skills/reversal-zones-projection` folder (with its `scripts/` subfolder) as a custom skill. The copy in this repository is the source of truth; editing it does not change a skill that is already installed elsewhere.

### Using it

Paste or attach an input JSON. The skill triggers on `"task": "reversal_zones"` and replies with the projection in one `json` code block. Save that block as a file.

### What the skill does

1. Runs its helper on the input to extract facts: trend, dominant swing, retracement levels, volatility, pivot highs and lows, gaps.
2. Chooses zones. Each zone needs at least two independent pieces of evidence taken from the rows, and every evidence string quotes exact prices so the zone can be audited.
3. Researches as far as `research` allows: the cause of the latest move and scheduled events inside the horizon.
4. Simulates future closes to get baseline probabilities (details [below](#how-the-probabilities-are-produced)).
5. Adjusts the baseline by a small, capped amount and records what it moved in `method`.
6. Validates the result (probabilities add up, zones do not overlap, scenarios sum to 1, every driver has a source) before replying.

It does not give buy, sell or hold calls, stops, targets or position sizes.

### Output: the projection JSON

Abridged example:

```json
{
  "task": "reversal_zones",
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
uv run python $RZ sim input.json zones.json      # baseline probabilities
uv run python $RZ check projection.json          # validate a projection
```

`zones.json` lists the zones nearest first, plus optional scheduled shocks:

```json
{
  "supports": [[43.2, 44.6], [40.6, 42.2], [37.4, 38.6]],
  "resistances": [[45.3, 47.1], [47.1, 49.6], [50.2, 52.7]],
  "shocks": [{ "date": "2026-12-21", "drop": 1.19 }, { "date": "2026-11-17", "jump": 0.08 }]
}
```

## Step 3: plot_projection.py

Draws the candles, the zones and the probabilities in one figure.

```bash
uv run python plot_projection.py INPUT.json PROJECTION.json [options]
```

Both files are needed: the projection does not contain candles.

| Option | Default | Meaning |
|---|---|---|
| `-o FILE` | `projection.png` | Output image. The extension picks the format: `png`, `svg`, `pdf` or `jpg`. |
| `--theme` | `light` | `light` or `dark`. |
| `--lang` | `pt` | Language of the chart's own labels: `pt` or `en`. |
| `--candles N` | automatic | How many past candles to show. The default is four times the horizon, with a minimum of 40. |
| `--dpi N` | `160` | Resolution for raster formats. |

### Reading the chart

- **Main panel.** Candles for the series the projection was judged on. Rising candles are hollow and falling candles are filled, so colour is left for the zones. Supports are blue and resistances are red, faint over the history and stronger over the shaded horizon. A dashed line and a boxed number mark the current price. Diamonds on the time axis mark dated events.
- **Probability panel.** One bar per zone on the same price scale. The filled part is `turn` (reverses here) and the outline is `reach` (gets here).
- **Scenario bar.** The four scenarios as one segmented bar, with the rule for each written underneath.
- **Notes.** Drivers with their direction, events, the confidence level and its caveats.

`--lang` only translates the chart's own labels. Text that comes from the projection (drivers, caveats, event names) stays in the language the projection was written in, which is set by `--language` in step 1.

## How the probabilities are produced

The helper simulates the future closes of the main series and counts how often each zone is reached or broken.

| Ingredient | Choice |
|---|---|
| Paths | 200,000, with a fixed random seed so identical inputs give identical baselines |
| Returns | Student-t with 5 degrees of freedom (fat tails), no drift |
| Volatility | Starts at the average of the last 13 and last 26 candles, decays linearly to the average of the last 52 and the whole series |
| Number of steps | 10 daily closes for `1-2w`, 13 weekly for `1-3m`, 12 monthly for `6-12m`, and the rest of the session for `today` |
| First step | Shortened when the newest candle is still forming |
| Scheduled shocks | `drop`: a cash dividend subtracted from the ex-date on. `jump`: an extra move on an event date, such as an options-implied earnings move |
| Measure | Closes for `close_in_zone`, intra-candle extremes for `touched_once` |

The simulation knows nothing about chart levels. The skill may therefore move a little probability, at most 0.10 in total on each side, typically from a tail into a zone with strong evidence. Every such move is written into `method`.

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

- **Uncalibrated.** No backtest has been run. This is the most important gap.
- **Yahoo Finance is unofficial.** Quotes can be delayed about 15 minutes on some exchanges and requests are sometimes rate-limited. One failed request currently ends the run.
- **Not dividend-adjusted.** For heavy dividend payers, old levels sit higher than a total-return chart would show.
- **Holidays.** A week ending on a holiday Friday is labelled `forming` until the weekend.
- **24-hour markets.** The session and week-end logic assumes a Monday-to-Friday exchange, so crypto tickers get wrong `forming` labels.
- **Zone choice is judgment.** Two runs on the same input can pick slightly different zone edges. The baseline probabilities for a given set of zones are reproducible.
- **`--shape json` is missing.** The skill accepts `"shape": "json"` (projection only, no prose), but `build_prompt.py` does not offer it yet. Edit the field by hand if needed.

## Roadmap

1. Backtest the baseline probabilities on past dates to measure calibration.
2. Add `json` to `--shape`.
3. Carry event sizes as numbers (dividend amount, implied move) so the skill does not have to search for them.
4. State `dividend_adjusted` explicitly in the input.
5. Retry on Yahoo failures and guard against duplicated or malformed rows.

## Project layout

```
stock_predict_zones/
├── build_prompt.py                 step 1: Yahoo Finance to input JSON
├── plot_projection.py              step 3: input + projection to image
├── .claude/skills/reversal-zones-projection/
│   ├── SKILL.md                    step 2: the skill
│   └── scripts/rz.py               its helper: levels, simulation, validation
├── examples/                       one complete run: input, projection and charts
├── pyproject.toml                  dependencies and Python version
├── uv.lock
└── README.md
```

## Disclaimer

This project produces probabilistic estimates for study and discussion. It is not investment advice and not a recommendation to buy or sell any security.
