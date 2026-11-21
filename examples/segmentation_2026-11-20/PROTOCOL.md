# Segmentation of the setup backtest: protocol, fixed before any data was looked at (2026-11-20)

Data: the three picked setups of every case of the long backtest (76 tickers, 2005-2026), rules 5,
pessimistic ordering inside a candle; one CSV per horizon.

Outcome per setup: R per order (0 when unfilled), NET of an assumed round-trip cost of 0.20% of the
entry price (commission + spread + slippage), charged only on filled setups, in R: 0.002 x entry / risk.
The gross figure and the optimistic ordering are reported alongside, never used to choose.

Features, all known at the as-of close:
- from the setup: horizon, side, kind (setup), order, rank, quality, ratio, stated fill, stated
  win_if_filled, stated if_filled, risk as % of price, entry distance from price as % of price;
- from the market: index regime (trailing 12 months: bull / flat / bear);
- from the ticker: universe group; own trailing 3- and 12-month return; own 3-month realised daily
  volatility (annualised); close above or below its 200-day average.
Numeric features are cut into terciles computed on the fit part, per horizon.
Never used: anything after the as-of date (market_after, index_after, year of exit).

Search, on the FIT part only (part == "fit"), per horizon:
- every single feature value, and every pair of two different features' values;
- a segment counts only with at least 1,000 setups and at least 150 distinct as-of dates;
- statistic: mean net R per order; its standard error clusters setups by as-of month (overlap);
- robustness: positive net mean in each of three blocks of fit years (2005-2012, 2013-2019, 2020-2026,
  held-out years excluded by construction), and t >= 3 (a high bar for the hundreds of segments tried).

Freeze: among those passing, the five with the highest net mean R per order over all horizons
(at most two per horizon), written to FROZEN.json before the held-out part is read.

Judge, ONCE, on part == "held-out": net mean R per order of each frozen segment, its t, and the
same for its complement and for all setups of the horizon. A segment holds if its held-out net mean
is > 0 with t >= 2. Nothing is re-chosen after this step.

Caveats kept with any result: survivor tickers chosen in 2026, overlapping cases, mechanical zones,
no borrowing costs or dividends, an assumed cost level, pessimistic intra-day ordering. Analysis of
hypothetical setups, not a strategy or advice.

## Amendment (2026-11-20, after the first fit-part search, before any held-out data was read)
The universe group is removed from the features. The groups were chosen in 2026 with hindsight
(`original` = twelve tickers still listed that mostly rose), so membership is not known at the as-of
date, and the first search's winners were almost all `group = original`: survivorship, not a
situation. The first search's output is kept as FROZEN_v1_with_group.json and is not judged.
