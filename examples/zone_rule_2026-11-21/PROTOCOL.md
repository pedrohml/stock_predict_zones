# A stronger zone rule: protocol (21-22 Nov 2026)

The adoption criterion is in the README ("A stronger zone rule"), committed (62a58c8) before any
candidate was run. This file records how the candidate was chosen, before the held-out part was read.

## Data
The as-of grid of the long setup backtest: 76 tickers (backtest_universe.txt, version 1),
`--start 2005-01-03`, `--end` 2026-10-30 / 2026-08-14 / 2025-11-13 at 1-2w / 1-3m / 6-12m, the
default steps. `harness.py collect fit DATA` builds only the cases of the fit part (the part is
computed from the schedule before the case is built). Per case: every valid setup of
`trade_entries.setups()` under each candidate zone rule, raw path odds, both walks. Base rule +
the shipped CORRECTION reproduce `backtest_entries.run_case()` exactly (140 of 140 cases checked,
three tickers, all horizons), and refitting the correction on the base rule reproduces the shipped
`CORRECTION` and the 1-2w band 0.29 exactly.

## Candidates (harness.RULES)
Width multiplier (1, 1.5, 2: the zone width, the clustering distance and the filler spacing),
minimum cluster members (2), a pivot or swing end required in the cluster, only the last 10
pivots per side, and the 3 clusters with most members among the nearest 5; a few combinations.
Twelve rules. Volume is not in the backtest's rows, so it was not a candidate.

## Choice, fit part only
For each candidate, `CORRECTION` refitted (1-2w, 1-3m) and the bands re-measured by the README's
methods (`analyse.py screen`), then the criterion's tests 1-3 computed on the fit part
(`margins.py`) and test 4 by `calib.py` (dates after the held-out years). Rule for the choice,
written before the held-out run: among the candidates passing all four tests on the fit part, the
one whose smallest margin, each test's margin divided by its tolerance (0.02 for test 1, 0.01 for
test 2, 0.002 for tests 3 and 4), is largest. The choice is frozen in FROZEN.json and committed
with the code before `harness.py collect held-out` is run, once.

## Frozen (22 Nov 2026, before the held-out run)
`fit_part.txt` is the fit-part table of every candidate (tests 1-4 and the smallest margin). Two
passed all four tests on the fit part: `w1.5_m2` (smallest margin +0.32) and `w2_m2` (+0.16), so
the frozen candidate is **`w1.5_m2`: zones 1.5 times the current width, and a cluster needs at
least two members**. FROZEN.json holds it with its refitted CORRECTION (1-2w, 1-3m) and bands
(1-2w 0.17, 1-3m 0.05; 6-12m keeps 0.32, not corrected, as under rules 5). The held-out run is
`harness.py collect held-out DATA` then `analyse.py judge DATA`, once.

## Held-out result and adoption (22 Nov 2026)
`held_out.txt` / `judge.json`: every test passed at every horizon, so the rule was adopted
(backtest.WIDTH_FACTOR 1.5, MIN_MEMBERS 2; setup rules 6 with FROZEN.json's CORRECTION and bands).
Since then `backtest.clustered_zones` is the adopted rule, so `calib.py` replaces it by
`harness.zones()` for every rule name, "base" (the earlier rule) included, and `harness.zones()`
under `w1.5_m2` equals the shipped rule.
