# Example run

One complete, real run of the pipeline for PETR4.SA, with data as of 2026-11-18 15:15 (São Paulo), price 42.77 BRL. The last weekly candle was still forming.

| File | What it is |
|---|---|
| `petr4_input.json` | Step 1 output: candles from Yahoo Finance plus the request |
| `petr4_projection.json` | Step 2 output: the `reversal-zones-projection` skill run by Claude on that input |
| `petr4.png`, `petr4_dark.png` | Step 3 output, light and dark themes |

Commands that produced them (from the repository root):

```bash
uv run python build_prompt.py PETR4.SA --horizon 1-3m --max-rows 52 --research none --language en -o examples/petr4_input.json
# step 2: Claude followed .claude/skills/reversal-zones-projection/SKILL.md and saved examples/petr4_projection.json
uv run python .claude/skills/reversal-zones-projection/scripts/rz.py check examples/petr4_projection.json   # prints OK
uv run python plot_projection.py examples/petr4_input.json examples/petr4_projection.json -o examples/petr4.png --lang en
uv run python plot_projection.py examples/petr4_input.json examples/petr4_projection.json -o examples/petr4_dark.png --lang en --theme dark
```

The projection used `research: none`: no web searches, so `drivers` is empty and the 21 Dec 2026 ex-dividend, whose amount is not in the input, is not part of the simulation.

Re-running step 1 fetches newer candles, so it will not reproduce `petr4_input.json`. This is a sample of the output format, not a forecast: the probabilities have **not been backtested**, and nothing here is investment advice.
