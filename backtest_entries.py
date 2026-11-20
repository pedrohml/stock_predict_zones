#!/usr/bin/env python3
"""Backtest the ranked trade setups on past dates: stated odds against what the price did. No Claude.

    python backtest_entries.py PETR4.SA NVDA --horizon 1-3m             # the last three ended years
    python backtest_entries.py NVDA SPY --horizon 1-2w 1-3m 6-12m        # several horizons, one report
    python backtest_entries.py PETR4.SA --optimistic                     # the other bound of the ordering
    python backtest_entries.py PETR4.SA --json                           # one JSON object instead
    python backtest_entries.py PETR4.SA --csv setups.csv                 # every setup, one row each
    python backtest_entries.py --universe backtest_universe.txt --start 2005-01-01 --jobs 8 \
        --horizon 1-2w 1-3m 6-12m --by-year --by-regime --by-part --subset original   # the long run

For each ticker, horizon and as-of date (backtest.py's grid: `--start/--end/--step`, `--method`)
it does what a live `zones TICKER --engine mechanical` run does after that day's close, with the
same code: the input from backtest.input_as_of(), the projection from mechanical.build(), the
three setups from trade_entries.build(). Then it walks the DAILY candles after the as-of day, up
to the setups' valid_until (the horizon's last close), and plays each setup by the rules in
walk(): fill, then stop, target or expiry. The result is in units of the setup's own risk (R).

The report compares the stated `fill`, `win_if_filled` and edge with the observed fill rate,
win rate and mean R, overall and by side, kind, order type, quality, rank and ticker, per
horizon, with Brier scores, reliability buckets, a bootstrap over cases and the per-ticker
spread. It checks the calibration of the stated odds of hypothetical setups. It is not a
strategy, says nothing about returns you could have had, and is not advice.

`--universe FILE` reads the tickers from a versioned universe file (backtest_universe.txt) in
named groups; `--group` picks groups, `--subset` adds a full section for a group. `--by-year`,
`--by-regime` (the market index's trailing 12-month return at the as-of date, and its move over
the horizon) and `--by-part` (the fixed fit / held-out split, PARTS) add compact tables per
period; `--part` restricts the report to one part. `--jobs N` runs the cases in N processes;
the output is identical to a serial run.
"""
import argparse
import bisect
import csv
import itertools
import json
import multiprocessing
import random
import re
import statistics
import sys
import textwrap
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

import pandas as pd

import backtest
import build_prompt
import mechanical
import score_projections as sp
import trade_entries

rz = backtest.rz
BOOTSTRAP = 2000          # resamples of whole cases
BOOTSTRAP_SEED = 20261119
GROUPS = (("side", "side"), ("setup", "kind"), ("order", "order"), ("quality", "quality"), ("rank", "rank"))
QUALITY_ORDER = ("favourable", "marginal", "unfavourable")
CHUNK = 50                # as-of dates per task with --jobs: small enough to keep every process busy

# Market regimes (--by-regime). Each ticker is matched with its market's index: B3 symbols
# (".SA") with the Ibovespa, everything else with the S&P 500 (price indices, like the candles).
# The regime is the index's return over the 365 days before the as-of date, known at the time:
# bear at or below -REGIME_BAND, bull at or above +REGIME_BAND, flat between. "index up/down"
# is the index's move from the as-of close to the close of valid_until: known only afterwards,
# it explains errors and is not a forecast.
INDEX_BY_SUFFIX = {".SA": "^BVSP"}
DEFAULT_INDEX = "^GSPC"
REGIME_BAND = 0.10
TRAILING_DAYS = 365
REGIMES = ("bull", "flat", "bear")

# The fixed fit / held-out split (--by-part, --part) for judging later rule changes: fit any
# new parameter on "fit" cases only and score it once on "held-out". A case belongs to a part
# only when its whole window, as-of date to valid_until, lies in that part's calendar years;
# a case that straddles the two is "embargo" and used by neither. The held-out years were
# fixed before any long-run result was looked at: two blocks with a crash, a rally, a long
# Brazilian bear market and a recovery (2008-2009, 2014-2016), all before Nov 2017, the
# earliest as-of date any earlier tuning of the drift or the setup rules looked at.
HELD_OUT_YEARS = (2008, 2009, 2014, 2015, 2016)
PARTS = ("fit", "held-out", "embargo")

RULES = [
    "The setups are made after the as-of day's close; the walk starts with the next daily candle "
    "(that day's own candle is never used) and ends with the candle of valid_until, the "
    "horizon's last close. Daily candles are used whatever the horizon's close basis.",
    "Fill: a limit order fills when the candle reaches its entry (a long's low at or below it, a "
    "short's high at or above it); a stop order fills when the candle reaches its entry in the "
    "order's direction (a long's high at or above it, a short's low at or below it). A touch is "
    "enough. When the open is already past the entry, the order fills at the open (better than "
    "the entry for a limit, worse for a stop). A market order fills at the next candle's open, "
    "not at the as-of close it was priced at. An unfilled order stays in place until "
    "valid_until, even if the price runs to the target first.",
    "After the fill: the stop or the target closes the setup at its price; an open past the "
    "stop or the target closes it at that open (a gap through the stop can lose more than 1R). "
    "Still open after the last candle: closed at that candle's close (expiry).",
    "Ordering inside a candle is unknown. On the fill candle, a level the price must have "
    "crossed after the entry (the entry lies between the open and the level) is hit after "
    "the fill; any other level the candle reaches may have come before it. When the candle "
    "allows more than one result (stop or target, stop or still open, target or still open), "
    "the default is the pessimistic one (stop first, target not yet); --optimistic takes the "
    "other. Each setup is played both ways, and 'ambiguous' counts those whose result differs.",
    "R = (exit - fill price) / risk for a long, (fill price - exit) / risk for a short, where "
    "risk is the setup's own entry-to-stop distance. An order that never fills is 0R. A win is "
    "an exit at the target.",
]

CAVEATS = [
    "Mechanical zones and unadjusted probabilities (mechanical.py), as a live `--engine mechanical` "
    "run makes them: not Claude's zones or adjustments. The inputs carry no events.",
    "No commissions, spread, slippage, borrowing costs or dividends; prices are split-adjusted and "
    "not dividend-adjusted, so a short does not pay the dividend and a long does not receive it.",
    "The ordering inside a daily candle is unknown and counted pessimistically by default "
    "(--optimistic gives the other bound). Touch fills are generous for limit orders.",
    "Tickers chosen today are survivors, in what was mostly a rising market for them.",
    "Cases overlap whenever the step is shorter than the horizon, the three setups of a case share "
    "one price path, and tickers move together; the bootstrap resamples whole cases and still "
    "understates the uncertainty.",
    "This checks the calibration of the stated odds of hypothetical setups. It is not a strategy, "
    "not a claim about returns, and not advice.",
]


# ---- one setup on the candles that followed --------------------------------------------

def setup_of(entry):
    """The parts of one entry of trade_entries.build() that the walk and the report read."""
    return {"rank": entry["rank"], "id": entry["id"], "side": entry["side"], "setup": entry["setup"],
            "order": entry["entry"]["order"], "entry": entry["entry"]["price"], "stop": entry["stop"]["price"],
            "target": entry["target"]["price"], "risk": entry["payoff"]["risk"], "ratio": entry["payoff"]["ratio"],
            "fill": entry["odds"]["fill"], "win_if_filled": entry["odds"]["win_if_filled"],
            "if_filled": entry["edge"]["if_filled"], "per_order": entry["edge"]["per_order"],
            "quality": entry["quality"]}


def walk(s, candles, optimistic=False):
    """Play setup `s` (setup_of()) on daily `candles` [[date, open, high, low, close], ...], the
    ones after the as-of day up to valid_until, by the rules in RULES. `optimistic` resolves a
    candle that allows two results in the setup's favour, else against it. -> {"filled",
    "fill_date", "fill_price", "gap_fill", "touch_only", "exit", "exit_date", "exit_price", "r",
    "won"}; exit is "target", "stop", "expiry" or None (never filled)."""
    long = s["side"] == "long"
    entry, stop, target, risk = s["entry"], s["stop"], s["target"], s["risk"]
    out = {"filled": False, "fill_date": None, "fill_price": None, "gap_fill": False, "touch_only": False,
           "exit": None, "exit_date": None, "exit_price": None, "r": 0.0, "won": False}
    if not candles:
        return out

    def reaches_stop(high, low):
        return low <= stop if long else high >= stop

    def reaches_target(high, low):
        return high >= target if long else low <= target

    def close(kind, day, price):
        out.update(exit=kind, exit_date=day, exit_price=price, won=kind == "target",
                   r=round(((price - out["fill_price"]) if long else (out["fill_price"] - price)) / risk, 4))
        return out

    def from_open(day, open_, high, low):
        """A candle wholly after the fill: a gap past a level closes at the open, else the
        levels it reaches. -> "stop", "target" or None (still open)."""
        if (open_ <= stop) if long else (open_ >= stop):
            return close("stop", day, open_)
        if (open_ >= target) if long else (open_ <= target):
            return close("target", day, open_)
        hit_stop, hit_target = reaches_stop(high, low), reaches_target(high, low)
        if hit_stop and hit_target:
            return close("target", day, target) if optimistic else close("stop", day, stop)
        if hit_stop:
            return close("stop", day, stop)
        if hit_target:
            return close("target", day, target)
        return None

    start = None
    for i, (day, open_, high, low, _) in enumerate(candles):
        order = s["order"]
        if order == "market":
            fill, gap = open_, True
        elif (order == "limit") == long:        # long limit or short stop: filled on the way down
            if low > entry:
                continue
            fill, gap = min(open_, entry), open_ <= entry
        else:                                   # long stop or short limit: filled on the way up
            if high < entry:
                continue
            fill, gap = max(open_, entry), open_ >= entry
        out.update(filled=True, fill_date=day, fill_price=fill, gap_fill=gap and order != "market",
                   touch_only=not gap and (low == entry if (order == "limit") == long else high == entry))
        if gap:   # the whole candle comes after the fill
            if from_open(day, open_, high, low):
                return out
        else:     # filled inside the candle: what it reached may have come before the entry
            after = lambda level: min(open_, level) <= entry <= max(open_, level)
            hit_stop, hit_target = reaches_stop(high, low), reaches_target(high, low)
            if hit_stop and hit_target:
                return close("target", day, target) if optimistic else close("stop", day, stop)
            if hit_stop and (after(stop) or not optimistic):
                return close("stop", day, stop)
            if hit_target and (after(target) or optimistic):
                return close("target", day, target)
        start = i + 1
        break
    if start is None:
        return out
    for day, open_, high, low, _ in candles[start:]:
        if from_open(day, open_, high, low):
            return out
    return close("expiry", candles[-1][0], candles[-1][4])


# ---- one case and the whole run ----------------------------------------------------------

STALE_DAYS = 60          # daily candles up to the as-of date that stale() looks at
STALE_SHARE = 0.2        # more flat candles than this share: stale quotes, the case is skipped
STALE_REASON = "stale quotes (over 20% of the daily candles have no range)"


def stale(rows):
    """True when more than STALE_SHARE of the daily `rows` have high == low: Yahoo repeats the
    last price on days without trades in some old B3 rows, and a case built or walked on them
    says nothing. Checked on the STALE_DAYS candles up to the as-of date and on the walk's."""
    return bool(rows) and sum(1 for row in rows if row[2] == row[3]) > STALE_SHARE * len(rows)


def failure(where, error):
    """The skip reason of a case the live code could not make: a reason the report counts and
    prints, so the failure is visible instead of ending a long run."""
    return f"{where} failed ({type(error).__name__}: {error})"


def longest_period(horizon):
    """The longest series a live run of `horizon` asks Yahoo for, as Yahoo names it ("10y")."""
    preset = build_prompt.PRESETS[horizon]
    return max((entry[1] for entry in (preset["main"], preset["context"]) if entry),
               key=lambda period: pd.Timestamp(2000, 1, 1) + backtest.period_offset(period))


def history_start(horizon, day):
    """The first day a live run's longest series covers, as of `day`: a ticker whose daily
    candles start later has less history than a live run would see, and the case is skipped
    (the presets assume a mature listing; backtest.input_as_of() would build shorter series)."""
    return (pd.Timestamp(day) - backtest.period_offset(longest_period(horizon))).date()

def run_case(ticker, horizon, data, day, today, method=None, optimistic=False):
    """One past date -> {"ticker", "horizon", "as_of", "price", "valid_until", "setups": [...]}
    with each setup's stated odds and both walks, or a `skipped` reason."""
    record = {"ticker": ticker, "horizon": horizon, "as_of": day.isoformat()}
    if not data["rows"] or data["rows"][0][0] > history_start(horizon, day).isoformat():
        return {**record, "skipped": f"less history than a live run requests ({longest_period(horizon)} "
                                     "of candles before the as-of date)"}
    at = bisect.bisect_right([row[0] for row in data["rows"]], day.isoformat())
    if stale(data["rows"][max(0, at - STALE_DAYS):at]):
        return {**record, "skipped": STALE_REASON}
    try:
        d = backtest.input_as_of(ticker, horizon, data, day)
    except ValueError as error:
        if "nonexistent time" not in str(error):
            raise
        # input_as_of() starts a series at local midnight, which did not exist on the days
        # Brazil's daylight saving time began (until 2018); such a date is skipped
        return {**record, "skipped": "a series would start at a local midnight that did not exist (DST)"}
    main = rz.unpack(d)[1]
    keep = build_prompt.PRESETS[horizon]["main"][2]
    if len(main["rows"]) < keep:
        return {**record, "skipped": f"less history than a live run sees ({keep} {main['candles']} candles)"}
    try:
        proj = mechanical.build(d, method=method)
    except mechanical.MechanicalError as reason:
        return {**record, "skipped": str(reason).split(":")[0]}
    except (ArithmeticError, ValueError) as error:
        # e.g. rz.facts() divides by the dominant swing's range, zero when every candle of the
        # main series has one price; counted under its own reason, never silently
        return {**record, "skipped": failure("mechanical.build()", error)}
    try:
        reply = trade_entries.build(d, proj)
    except (ArithmeticError, ValueError) as error:
        # e.g. a short whose target zone reaches zero on a sub-penny price: math.log(0)
        return {**record, "skipped": failure("trade_entries.build()", error)}
    if "error" in reply:
        return {**record, "skipped": reply["error"]}
    valid_until = reply["valid_until"]
    if date.fromisoformat(valid_until) >= today:
        return {**record, "skipped": "horizon not ended yet"}
    places = trade_entries.places(d)
    candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in data["rows"]
               if day.isoformat() < row[0] <= valid_until]
    if not candles:
        return {**record, "skipped": "no daily candle after the as-of day"}
    if stale([row for row in data["rows"] if day.isoformat() < row[0] <= valid_until]):
        return {**record, "skipped": STALE_REASON}
    setups = []
    for entry in reply["entries"]:
        s = setup_of(entry)
        chosen, other = walk(s, candles, optimistic), walk(s, candles, not optimistic)
        setups.append({**s, **chosen, "r_other": other["r"], "exit_other": other["exit"],
                       "ambiguous": (chosen["exit"], chosen["r"]) != (other["exit"], other["r"])})
    record.update(price=d["now"]["price"], valid_until=valid_until, candles=len(candles),
                  confidence=reply["confidence"]["level"], setups=setups)
    return record


def cases(task):
    """The cases of one ticker and horizon on some as-of dates: run_case() for each, in order.
    A top-level function so that --jobs can run it in another process."""
    ticker, horizon, data, days, today, method, optimistic = task
    return [run_case(ticker, horizon, data, day, today, method, optimistic) for day in days]


def cut(data, first=None, last=None):
    """`data` with only the daily rows from `first` to `last` (dates, either None: no limit), a
    universe file's TICKER@FIRST..LAST for a symbol whose other rows are not usable (broken
    early prices, sub-penny quotes after a failure). Rows before `first` count as no history,
    so the first as-of dates come once a live run's series fit after it; no as-of date comes
    after `last`, and the walk of a case whose horizon runs past it ends with the last row: a
    setup still open is closed at that close, as at expiry."""
    low, high = first.isoformat() if first else "", last.isoformat() if last else "9999"
    return {**data, "rows": [row for row in data["rows"] if low <= row[0] <= high]}


def run(tickers, horizons, start=None, end=None, step=None, today=None, progress=True, method=None,
        optimistic=False, jobs=1, spans=None, regimes=False, universe=None, view=None):
    """The whole backtest -> {"settings", "records", "summary"}. Per horizon, the as-of grid of
    backtest.py (its default range and step unless given). `jobs` > 1 runs the cases in that
    many processes; downloads stay in this one and the records come back in the serial order,
    so the result is the same. `spans` maps a ticker to its first and last usable day, either
    None (cut()); `regimes` downloads the market indices and labels each case (label_regimes());
    `universe` is read_universe()'s result, kept in the settings; `view` goes to summarise()."""
    method = rz.sim_method(method)
    spans = {t.upper(): span for t, span in (spans or {}).items()}
    records, failed, ranges, units = [], {}, {}, []
    pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn")) if jobs > 1 else None
    try:
        for horizon in horizons:
            first_start, first_end = backtest.default_range(horizon, today or date.today())
            h_start, h_end, h_step = start or first_start, end or first_end, step or backtest.DEFAULT_STEP[horizon]
            if h_start > h_end:
                raise backtest.BacktestError(f"--start {h_start} is after --end {h_end}")
            ranges[horizon] = {"start": h_start.isoformat(), "end": h_end.isoformat(), "step_trading_days": h_step}
            for ticker in tickers:
                ticker = ticker.upper()
                clock = time.monotonic()
                try:
                    data = backtest.download(ticker, h_start - timedelta(days=backtest.lookback(horizon)))
                except backtest.BacktestError as reason:
                    failed[f"{ticker} {horizon}"] = str(reason)
                    backtest.say(f"{ticker} {horizon}: not backtested: {reason}", progress)
                    continue
                if ticker in spans:
                    data = cut(data, *spans[ticker])
                ticker_today = today or sp.today_in(data["tz"])
                days = backtest.as_of_days(data["rows"], h_start, h_end, h_step)
                tasks = [(ticker, horizon, data, days[i:i + CHUNK], ticker_today, method, optimistic)
                         for i in range(0, len(days), CHUNK)]
                parts = [pool.submit(cases, task) for task in tasks] if pool else [cases(task) for task in tasks]
                units.append((ticker, horizon, len(days), parts, clock))
                if not pool:
                    collect(units.pop(), records, progress)
        while units:
            collect(units.pop(0), records, progress)
    finally:
        if pool:
            pool.shutdown(cancel_futures=True)
    for record in records:
        if "skipped" not in record:
            record["part"] = part_of(record["as_of"], record["valid_until"])
    settings = {"tickers": [t.upper() for t in tickers], "horizons": list(horizons), "ranges": ranges,
                "sim_method": method, "default_sim_method": rz.SIM_METHOD, "setup_rules": trade_entries.RULES,
                "ordering": "optimistic" if optimistic else "pessimistic", "rules": RULES, "failed": failed}
    if spans:
        settings["spans"] = {t: [day.isoformat() if day else None for day in span] for t, span in spans.items()}
    if universe:
        settings["universe"] = {key: universe[key] for key in ("file", "version", "groups")}
    if regimes:
        settings["regimes"] = label_regimes(records, failed, progress)
    view = dict(view or {})
    if universe and view.get("groups") is None:
        view["groups"] = {name: g["tickers"] for name, g in universe["groups"].items()}
    if view.get("part"):
        settings["part"] = view["part"]
    if view.get("periods"):
        settings["periods"] = list(view["periods"])
    return {"settings": settings, "records": records, "summary": summarise(records, horizons, **view)}


def collect(unit, records, progress):
    """Append one ticker and horizon's records, in as-of order, and say how it went."""
    ticker, horizon, count, parts, clock = unit
    mine = [record for part in parts for record in (part if isinstance(part, list) else part.result())]
    records += mine
    used = sum(1 for r in mine if "skipped" not in r)
    backtest.say(f"{ticker} {horizon}: {count} dates, {used} with setups, {count - used} skipped "
                 f"({time.monotonic() - clock:.0f}s)", progress)


# ---- universe, periods, regimes ------------------------------------------------------------

class UniverseError(ValueError):
    """A universe file that cannot be used; the message says why."""


GROUP_LINE = re.compile(r"^\[([a-z0-9][a-z0-9_-]*)\]\s*(.*)$")
TICKER = re.compile(r"^([A-Z0-9^][A-Z0-9.^=-]*)(?:@(\S+))?$")


def read_universe(path):
    """A universe file -> {"file", "version", "groups": {name: {"reason", "tickers"}}, "tickers",
    "spans"}. The format: `#` starts a comment; one `version: N` line; `[name] reason` starts a
    group; any other line lists tickers separated by spaces, each optionally followed by
    @FIRST..LAST (dates YYYY-MM-DD, either side may be left out; a bare @LAST is ..LAST): use its
    rows in that range only (cut(); the range applies wherever the ticker is listed). A ticker
    may be in several groups; "tickers" is every ticker once, in file order; "spans" maps a
    ticker to (first, last), None for an open end."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise UniverseError(f"cannot read the universe file {path}: {error.strerror or error}")
    version, groups, current, spans = None, {}, None, {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        where = f"{path}, line {number}"
        if line.lower().startswith("version:"):
            if version is not None:
                raise UniverseError(f"{where}: a second version line")
            version = line.split(":", 1)[1].strip()
            if not version:
                raise UniverseError(f"{where}: the version is empty")
            continue
        header = GROUP_LINE.match(line)
        if header:
            name, reason = header.groups()
            if name in groups:
                raise UniverseError(f"{where}: group [{name}] appears twice")
            if not reason:
                raise UniverseError(f"{where}: group [{name}] needs a reason after its name")
            groups[name] = current = {"reason": reason, "tickers": []}
            continue
        if line.startswith("["):
            raise UniverseError(f"{where}: a group name is lowercase letters, digits, - or _: {line}")
        if current is None:
            raise UniverseError(f"{where}: tickers before the first [group] line")
        for word in line.split():
            found = TICKER.match(word.upper())
            if not found:
                raise UniverseError(f"{where}: not a ticker: {word}")
            ticker, span = found.groups()
            if span:
                ends = span.split("..") if ".." in span else ["", span]
                try:
                    span = tuple(date.fromisoformat(end) if end else None for end in ends)
                except ValueError:
                    raise UniverseError(f"{where}: not a date or FIRST..LAST range (YYYY-MM-DD): {word}")
                if len(span) != 2 or span == (None, None) or (all(span) and span[0] > span[1]):
                    raise UniverseError(f"{where}: not a FIRST..LAST range: {word}")
                if spans.get(ticker, span) != span:
                    raise UniverseError(f"{where}: {ticker} has two different @ranges")
                spans[ticker] = span
            if ticker not in current["tickers"]:
                current["tickers"].append(ticker)
    if version is None:
        raise UniverseError(f"{path}: no 'version: N' line")
    if not groups:
        raise UniverseError(f"{path}: no [group] line")
    for name, group in groups.items():
        if not group["tickers"]:
            raise UniverseError(f"{path}: group [{name}] has no tickers")
    tickers = list(dict.fromkeys(t for group in groups.values() for t in group["tickers"]))
    return {"file": str(path), "version": version, "groups": groups, "tickers": tickers, "spans": spans}


def part_of(as_of, valid_until):
    """The fit / held-out part of a case (see HELD_OUT_YEARS): "held-out" when every calendar
    year from the as-of date to valid_until is held out, "fit" when none is, else "embargo"."""
    held = [year in HELD_OUT_YEARS for year in range(int(as_of[:4]), int(valid_until[:4]) + 1)]
    return "held-out" if all(held) else "fit" if not any(held) else "embargo"


def index_of(ticker):
    """The market index a ticker's regime is read from."""
    for suffix, index in INDEX_BY_SUFFIX.items():
        if ticker.upper().endswith(suffix):
            return index
    return DEFAULT_INDEX


def close_on(rows, dates, day):
    """The close of the last row on or before `day` (an ISO date), None before the first row."""
    at = bisect.bisect_right(dates, day) - 1
    return rows[at][4] if at >= 0 else None


def label_regimes(records, failed, progress):
    """Add "regime" (bull, flat, bear: the index's return over the TRAILING_DAYS before the
    as-of date) and "market_after" (index up or down from the as-of close to valid_until's) to
    every case with setups, from each ticker's index (index_of()), downloaded once through
    backtest.download(). A case without index data gets "unknown". -> the settings' record."""
    used = [r for r in records if "skipped" not in r]
    indices = {}
    for index in sorted({index_of(r["ticker"]) for r in used}):
        first = min(date.fromisoformat(r["as_of"]) for r in used if index_of(r["ticker"]) == index)
        try:
            data = backtest.download(index, first - timedelta(days=TRAILING_DAYS + 30))
        except backtest.BacktestError as reason:
            failed[f"{index} (regime index)"] = str(reason)
            backtest.say(f"{index}: no regime labels: {reason}", progress)
            continue
        indices[index] = (data["rows"], [row[0] for row in data["rows"]])
    for r in used:
        r["regime"] = r["market_after"] = "unknown"
        if index_of(r["ticker"]) not in indices:
            continue
        rows, dates = indices[index_of(r["ticker"])]
        back = (date.fromisoformat(r["as_of"]) - timedelta(days=TRAILING_DAYS)).isoformat()
        now, before, after = (close_on(rows, dates, r["as_of"]), close_on(rows, dates, back),
                              close_on(rows, dates, r["valid_until"]))
        if now and before and dates[0] <= back:
            trailing = now / before - 1
            r["index_trailing"] = round(trailing, 4)
            r["regime"] = "bull" if trailing >= REGIME_BAND else "bear" if trailing <= -REGIME_BAND else "flat"
        if now and after:
            r["index_after"] = round(after / now - 1, 4)
            r["market_after"] = "index up" if after >= now else "index down"
    return {"index": {**{f"*{suffix}": index for suffix, index in INDEX_BY_SUFFIX.items()}, "other": DEFAULT_INDEX},
            "band": REGIME_BAND, "trailing_days": TRAILING_DAYS}


# ---- summary -------------------------------------------------------------------------------

def mean(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def stats(setups):
    """Stated against observed for a group of setups (rows of flat())."""
    n = len(setups)
    filled = [s for s in setups if s["filled"]]
    out = {"setups": n, "cases": len({(s["ticker"], s["as_of"]) for s in setups}),
           "fill_stated": mean(s["fill"] for s in setups), "fill_observed": mean(s["filled"] for s in setups),
           "fills": len(filled),
           "win_stated": mean(s["win_if_filled"] for s in filled), "win_observed": mean(s["won"] for s in filled),
           "expired": mean(s["exit"] == "expiry" for s in filled),
           "edge_fill_stated": mean(s["if_filled"] for s in filled), "r_per_fill": mean(s["r"] for s in filled),
           "edge_order_stated": mean(s["per_order"] for s in setups), "r_per_order": mean(s["r"] for s in setups),
           "r_per_order_other": mean(s["r_other"] for s in setups),
           "ambiguous": mean(s["ambiguous"] for s in setups)}
    if n:
        pairs = [(s["fill"], s["filled"]) for s in setups]
        out["brier_fill"] = round(sp.brier(pairs), 4)
        out["brier_fill_base_rate"] = round(sp.brier([(out["fill_observed"], y) for _, y in pairs]), 4)
    if filled:
        pairs = [(s["win_if_filled"], s["won"]) for s in filled]
        out["brier_win"] = round(sp.brier(pairs), 4)
        out["brier_win_base_rate"] = round(sp.brier([(out["win_observed"], y) for _, y in pairs]), 4)
    return out


def bootstrap(setups, rounds=BOOTSTRAP, seed=BOOTSTRAP_SEED):
    """90% intervals of the observed rates and mean R, resampling whole cases (a case's three
    setups stay together). Cases overlap in time, so these intervals are too narrow."""
    cases = {}
    for s in setups:
        cases.setdefault((s["ticker"], s["as_of"]), []).append(s)
    # Each case packed once: setups, fills, wins, and the R values in setup order, so a round
    # adds the same numbers in the same order as summing over the resampled setups would.
    groups = [(len(g), sum(s["filled"] for s in g), sum(s["won"] for s in g if s["filled"]),
               tuple(s["r"] for s in g), tuple(s["r"] for s in g if s["filled"])) for g in cases.values()]
    if len(groups) < 2:
        return {}
    rng = random.Random(seed)
    choice, chain = rng.choice, itertools.chain.from_iterable
    draws = {"fill_observed": [], "win_observed": [], "r_per_fill": [], "r_per_order": []}
    for _ in range(rounds):
        sample = [choice(groups) for _ in groups]
        n, fills = sum(g[0] for g in sample), sum(g[1] for g in sample)
        draws["fill_observed"].append(fills / n)
        draws["r_per_order"].append(sum(chain(g[3] for g in sample)) / n)
        if fills:
            draws["win_observed"].append(sum(g[2] for g in sample) / fills)
            draws["r_per_fill"].append(sum(chain(g[4] for g in sample)) / fills)
    out = {}
    for key, values in draws.items():
        if values:
            values.sort()
            out[key] = [round(values[int(0.05 * (len(values) - 1))], 4), round(values[int(0.95 * (len(values) - 1))], 4)]
    return out


def spread(setups):
    """Per-ticker mean R per order and win rate: lowest, median, highest, and how many tickers
    had a positive mean R."""
    per = {}
    for s in setups:
        per.setdefault(s["ticker"], []).append(s)
    r = [sum(x["r"] for x in v) / len(v) for v in per.values()]
    if not r:
        return {}
    return {"tickers": len(r), "r_per_order_min": round(min(r), 4), "r_per_order_median": round(statistics.median(r), 4),
            "r_per_order_max": round(max(r), 4), "tickers_positive": sum(x > 0 for x in r)}


def flat(records):
    """Every setup of every case as one row, with the case's ticker, horizon and dates."""
    return [{"ticker": r["ticker"], "horizon": r["horizon"], "as_of": r["as_of"], "price": r["price"],
             "valid_until": r["valid_until"], **{key: r[key] for key in LABELS if key in r}, **s}
            for r in records if "skipped" not in r for s in r["setups"]]


LABELS = ("part", "regime", "index_trailing", "market_after", "index_after")   # added to a case by run()
COMPACT = (("all", lambda s: True), ("long", lambda s: s["side"] == "long"),
           ("short", lambda s: s["side"] == "short"), ("continuation", lambda s: s["setup"] == "continuation"),
           ("reversal", lambda s: s["setup"] == "reversal"),
           ("long continuation", lambda s: s["side"] == "long" and s["setup"] == "continuation"),
           ("short continuation", lambda s: s["side"] == "short" and s["setup"] == "continuation"))
PERIODS = ("year", "regime", "part")   # --by-year, --by-regime, --by-part
PERIOD_KEYS = {"year": (("year", lambda s: s["as_of"][:4], None),),
               "regime": (("regime", lambda s: s.get("regime", "unknown"), REGIMES + ("unknown",)),
                          ("market_after", lambda s: s.get("market_after", "unknown"),
                           ("index up", "index down", "unknown"))),
               "part": (("part", lambda s: s["part"], PARTS),)}


def compact(setups):
    """stats() of a group of setups, and of its long, short, continuation, reversal and long and
    short continuation setups: the rows of the period and group tables."""
    return {name: stats([s for s in setups if test(s)]) for name, test in COMPACT}


def split(setups, key, order=None):
    """{value: compact()} of the setups grouped by key(setup), in `order` (else sorted); values
    with no setup are left out."""
    values = {key(s) for s in setups}
    ordered = [v for v in order if v in values] if order else sorted(values)
    return {v: compact([s for s in setups if key(s) == v]) for v in ordered}


def scope(setups, periods=(), groups=None):
    """Everything the report says about one set of setups. `periods` adds the tables of
    PERIOD_KEYS; `groups` ({name: tickers}) adds one compact row per group."""
    out = {"overall": stats(setups), "bootstrap_90": bootstrap(setups), "ticker_spread": spread(setups),
           "fill_buckets": sp.buckets([(s["fill"], s["filled"]) for s in setups]),
           "win_buckets": sp.buckets([(s["win_if_filled"], s["won"]) for s in setups if s["filled"]]),
           "exits": {kind: sum(1 for s in setups if s["exit"] == kind) for kind in ("target", "stop", "expiry")}}
    out["exits"]["unfilled"] = sum(1 for s in setups if not s["filled"])
    for key, name in GROUPS:
        values = sorted({s[key] for s in setups},
                        key=lambda v: QUALITY_ORDER.index(v) if v in QUALITY_ORDER else str(v))
        out[f"by_{name}"] = {str(v): stats([s for s in setups if s[key] == v]) for v in values}
    out["by_side_and_kind"] = {f"{side} {kind}": stats(group) for side in ("long", "short")
                               for kind in ("reversal", "continuation")
                               if (group := [s for s in setups if s["side"] == side and s["setup"] == kind])}
    out["by_ticker"] = {t: stats([s for s in setups if s["ticker"] == t]) for t in sorted({s["ticker"] for s in setups})}
    for period in periods:
        for name, key, order in PERIOD_KEYS[period]:
            out[f"by_{name}"] = split(setups, key, order)
    if groups:
        out["by_group"] = {"all": compact(setups)} | {
            name: compact([s for s in setups if s["ticker"] in set(tickers)]) for name, tickers in groups.items()}
    return out


def summarise(records, horizons, periods=(), groups=None, subsets=(), part=None):
    """The report's numbers. `periods` and `groups` go to scope(); `subsets` names groups that
    get a whole report of their own; `part` keeps only the cases of that part (part_of())."""
    skipped = {}
    for r in records:
        if "skipped" in r:
            key = f"{r['horizon']}: {r['skipped']}"
            skipped[key] = skipped.get(key, 0) + 1
    if part:
        records = [r for r in records if "skipped" not in r and r["part"] == part]
    rows = flat(records)
    out = {"cases": sum(1 for r in records if "skipped" not in r), "setups": len(rows), "skipped": skipped,
           "caveats": CAVEATS,
           "per_horizon": {h: scope([s for s in rows if s["horizon"] == h], periods, groups) for h in horizons
                           if any(s["horizon"] == h for s in rows)}}
    if subsets:
        out["subsets"] = {}
        for name in subsets:
            tickers = set((groups or {})[name])
            mine = [s for s in rows if s["ticker"] in tickers]
            out["subsets"][name] = {
                "tickers": sorted(tickers), "cases": len({(s["ticker"], s["horizon"], s["as_of"]) for s in mine}),
                "setups": len(mine),
                "per_horizon": {h: scope([s for s in mine if s["horizon"] == h], periods) for h in horizons
                                if any(s["horizon"] == h for s in mine)}}
    return out


# ---- report ----------------------------------------------------------------------------------

def num(value, form="{:.2f}"):
    return "-" if value is None else form.format(value)


def pct(value):
    return "-" if value is None else f"{value:.0%}"


HEAD = (f"    {'group':<22} {'setups':>6} {'fill st/obs':>12} {'win st/obs':>12} {'R/fill st/obs':>14} "
        f"{'R/order st/obs':>15} {'ambig':>6}")


def row(name, k):
    return (f"    {name:<22} {k['setups']:>6} {pct(k['fill_stated']) + '/' + pct(k['fill_observed']):>12} "
            f"{pct(k['win_stated']) + '/' + pct(k['win_observed']):>12} "
            f"{num(k['edge_fill_stated'], '{:+.2f}') + '/' + num(k['r_per_fill'], '{:+.2f}'):>14} "
            f"{num(k['edge_order_stated'], '{:+.2f}') + '/' + num(k['r_per_order'], '{:+.2f}'):>15} "
            f"{pct(k['ambiguous']):>6}")


def render_scope(horizon, c, ordering):
    o, b, sd = c["overall"], c["bootstrap_90"], c["ticker_spread"]
    other = "optimistic" if ordering == "pessimistic" else "pessimistic"
    lines = [f"HORIZON {horizon}: {o['cases']} case(s), {o['setups']} setups, {o['fills']} filled; exits: "
             + ", ".join(f"{n} {kind}" for kind, n in c["exits"].items()),
             "  st = stated by the setup (mean), obs = observed. win is among filled setups; R/fill is the mean "
             "result of filled setups, R/order counts unfilled ones as 0R.",
             HEAD, row("all", o)]
    for name in ("side", "kind", "side_and_kind", "order", "quality", "rank"):
        lines.append(f"    by {name.replace('_', ' ')}:")
        lines += [row(f"  {key}", k) for key, k in c[f"by_{name}"].items()]
    lines.append("    by ticker:")
    lines += [row(f"  {key}", k) for key, k in c["by_ticker"].items()]
    lines.append(f"  Brier, fill: {num(o.get('brier_fill'), '{:.3f}')} (always the observed rate "
                 f"{num(o.get('brier_fill_base_rate'), '{:.3f}')}); win: {num(o.get('brier_win'), '{:.3f}')} "
                 f"(always the observed rate {num(o.get('brier_win_base_rate'), '{:.3f}')}). 0 is perfect.")
    if b:
        lines.append("  Bootstrap over cases, 90%: fill " + "-".join(pct(x) for x in b["fill_observed"])
                     + (", win " + "-".join(pct(x) for x in b["win_observed"]) if "win_observed" in b else "")
                     + (", R/fill " + " to ".join(num(x, '{:+.2f}') for x in b["r_per_fill"]) if "r_per_fill" in b else "")
                     + ", R/order " + " to ".join(num(x, '{:+.2f}') for x in b["r_per_order"]))
    if sd:
        lines.append(f"  Per ticker, R/order: lowest {sd['r_per_order_min']:+.2f}, median {sd['r_per_order_median']:+.2f}, "
                     f"highest {sd['r_per_order_max']:+.2f}; positive in {sd['tickers_positive']} of {sd['tickers']} tickers.")
    lines.append(f"  Ambiguous candles changed {pct(o['ambiguous'])} of the setups; with the {other} ordering "
                 f"R/order would be {num(o['r_per_order_other'], '{:+.2f}')}.")
    for title, key in (("fill", "fill_buckets"), ("win if filled", "win_buckets")):
        lines.append(f"  Reliability, {title}:  " + "  ".join(
            f"{bucket['from']}-{bucket['to']}%: n {bucket['n']}, {bucket['mean_forecast']:.2f}->{bucket['observed']:.2f}"
            for bucket in c[key]))
    tables = [(key, title) for key, title in TABLES if key in c]
    if tables:
        lines += ["  R/o = R per order (st/obs) of the long, short and short continuation setups; cont fill = fill "
                  "of the continuations;", "  Brier m/c = Brier score of the stated odds / of always forecasting "
                  "the group's own observed rate.", COMPACT_HEAD]
        for key, title in tables:
            lines.append(f"    {title}:")
            lines += [compact_row(f"  {value}", k) for value, k in c[key].items()]
    return lines


TABLES = (("by_year", "by year of the as-of date"), ("by_regime", "by regime: index's trailing 12-month return"),
          ("by_market_after", "by the index's move over the horizon (known only afterwards)"),
          ("by_part", "by part of the fixed fit / held-out split"), ("by_group", "by universe group"))
COMPACT_HEAD = (f"    {'group':<22} {'setups':>6} {'fill st/obs':>12} {'win st/obs':>12} {'R/order st/obs':>15} "
                f"{'long R/o':>12} {'short R/o':>12} {'s.cont R/o':>12} {'cont fill':>10} {'Brier fill m/c':>15} "
                f"{'Brier win m/c':>14}")


def compact_row(name, k):
    """One row of a period or group table (compact())."""
    a = k["all"]

    def r_pair(group):
        g = k[group]
        return "-" if not g["setups"] else f"{num(g['edge_order_stated'], '{:+.2f}')}/{num(g['r_per_order'], '{:+.2f}')}"

    def brier(g, kind):
        return f"{num(g.get(f'brier_{kind}'), '{:.3f}')}/{num(g.get(f'brier_{kind}_base_rate'), '{:.3f}')}"

    cont = k["continuation"]
    return (f"    {name:<22} {a['setups']:>6} {pct(a['fill_stated']) + '/' + pct(a['fill_observed']):>12} "
            f"{pct(a['win_stated']) + '/' + pct(a['win_observed']):>12} "
            f"{num(a['edge_order_stated'], '{:+.2f}') + '/' + num(a['r_per_order'], '{:+.2f}'):>15} "
            f"{r_pair('long'):>12} {r_pair('short'):>12} {r_pair('short continuation'):>12} "
            f"{(pct(cont['fill_stated']) + '/' + pct(cont['fill_observed'])) if cont['setups'] else '-':>10} "
            f"{brier(a, 'fill'):>15} {brier(a, 'win'):>14}")


def render(report):
    s, summary = report["settings"], report["summary"]
    lines = ["BACKTEST of the trade setups (mechanical engine, trade_entries.py), hypothetical, for calibration",
             f"  {', '.join(s['tickers'])}; horizons {', '.join(s['horizons'])}; sim method {s['sim_method']}"
             + ("" if s["sim_method"] == s["default_sim_method"] else
                f" (rz.py sim uses method {s['default_sim_method']} by default)")
             + f"; ordering inside a candle: {s['ordering']}; setup rules {s.get('setup_rules', 1)}"]
    for horizon, r in s["ranges"].items():
        lines.append(f"  {horizon}: as-of dates {r['start']} to {r['end']}, every {r['step_trading_days']} trading day(s)")
    lines.append(f"  {summary['cases']} case(s), {summary['setups']} setups"
                 + ("; skipped: " + ", ".join(f"{n} {why}" for why, n in summary["skipped"].items())
                    if summary["skipped"] else ""))
    for name, reason in s["failed"].items():
        lines.append(f"  {name}: not backtested: {reason}")
    for text in context(s):
        lines += textwrap.wrap(text, 100, initial_indent="  ", subsequent_indent="    ")
    lines.append("  Rules:")
    for text in s["rules"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    lines.append("  Caveats:")
    for text in summary["caveats"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    for horizon, c in summary["per_horizon"].items():
        lines += [""] + render_scope(horizon, c, s["ordering"])
    for name, sub in summary.get("subsets", {}).items():
        lines += ["", "=" * 100, f"SUBSET [{name}]: {', '.join(sub['tickers'])}; {sub['cases']} case(s), "
                  f"{sub['setups']} setups. The same tables for these tickers only.", "=" * 100]
        for horizon, c in sub["per_horizon"].items():
            lines += [""] + render_scope(horizon, c, s["ordering"])
    return "\n".join(lines)


def context(s):
    """The header lines the universe, the split, the regimes and the cut tickers add."""
    out = []
    if "universe" in s:
        u = s["universe"]
        out.append(f"Universe {u['file']}, version {u['version']}: "
                   + "; ".join(f"[{name}] {len(g['tickers'])}" for name, g in u["groups"].items())
                   + f" ({len(s['tickers'])} tickers). Yahoo serves no history for most delisted or acquired "
                   "symbols, so survivor bias is smaller than with today's names alone, not gone (the file lists "
                   "what was tried).")
    if s.get("spans"):
        out.append("Rows used in a date range only (the others are unusable; before the range counts as no "
                   "history, and a setup still open at the range's end is closed at its last close): "
                   + ", ".join(f"{t} {first or ''}..{last or ''}" for t, (first, last) in s["spans"].items()) + ".")
    if "regimes" in s:
        g = s["regimes"]
        out.append(f"Regime: the index's return over the {g['trailing_days']} days before the as-of date, bull at or "
                   f"above +{g['band']:.0%}, bear at or below -{g['band']:.0%}, flat between; index: "
                   + ", ".join(f"{k} {v}" for k, v in g["index"].items()) + ".")
    if "part" in s.get("periods", []) or s.get("part"):
        out.append("Fit / held-out split: held-out = cases whose whole window (as-of date to valid_until) lies in "
                   + ", ".join(map(str, HELD_OUT_YEARS)) + "; fit = cases whose window touches none of those years; "
                   "embargo = the rest, used by neither.")
    if s.get("part"):
        out.append(f"Only the {s['part']} part is reported (the skipped counts are for all dates).")
    return out


CSV_FIELDS = ["ticker", "horizon", "as_of", "price", "valid_until", "rank", "id", "side", "setup", "order",
              "quality", "entry", "stop", "target", "risk", "ratio", "fill", "win_if_filled", "if_filled",
              "per_order", "filled", "fill_date", "fill_price", "gap_fill", "touch_only", "exit", "exit_date",
              "exit_price", "r", "won", "ambiguous", "exit_other", "r_other", *LABELS]


def write_csv(path, records):
    """One row per setup of every case."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.DictWriter(handle, CSV_FIELDS, extrasaction="ignore")
        out.writeheader()
        for s in flat(records):
            out.writerow(s)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tickers", nargs="*", metavar="TICKER",
                        help="Yahoo symbols, e.g. PETR4.SA NVDA (or --universe, or both)")
    parser.add_argument("--universe", metavar="FILE",
                        help="read the tickers from a universe file (e.g. backtest_universe.txt): named groups, "
                             "a version, TICKER@DATE for a symbol usable up to a day")
    parser.add_argument("--group", action="append", metavar="NAME",
                        help="with --universe: only the tickers of this group (repeatable)")
    parser.add_argument("--subset", action="append", metavar="NAME",
                        help="with --universe: also report this group's tickers on their own (repeatable)")
    parser.add_argument("--by-year", action="store_true", help="add a table per year of the as-of date")
    parser.add_argument("--by-regime", action="store_true",
                        help="add tables by market regime (downloads ^GSPC and ^BVSP): the index's trailing "
                             f"12-month return (+/-{REGIME_BAND:.0%}) and its move over the horizon")
    parser.add_argument("--by-part", action="store_true",
                        help="add a table by part of the fixed fit / held-out split (held-out years "
                             + ", ".join(map(str, HELD_OUT_YEARS)) + ")")
    parser.add_argument("--part", choices=PARTS[:2],
                        help="report only the cases of this part of the fit / held-out split")
    parser.add_argument("--jobs", type=int, default=1, metavar="N",
                        help="run the cases in N processes (default: 1); the output is the same")
    parser.add_argument("--horizon", nargs="+", choices=backtest.HORIZONS, default=["1-3m"],
                        help="one or more horizons (default: 1-3m)")
    parser.add_argument("--start", type=date.fromisoformat,
                        help=f"first as-of date (default: {backtest.DEFAULT_YEARS} years before --end)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="last as-of date (default: today minus the horizon, so every horizon has ended)")
    parser.add_argument("--step", type=int, metavar="N",
                        help="trading days between two as-of dates (default: "
                             + ", ".join(f"{h} {n}" for h, n in backtest.DEFAULT_STEP.items()) + ")")
    parser.add_argument("--method", type=int, choices=sorted(rz.SIM_METHODS), default=rz.SIM_METHOD,
                        help=f"sim method version of the projection (default: {rz.SIM_METHOD}, as a live run)")
    parser.add_argument("--optimistic", action="store_true",
                        help="resolve a candle that allows two results in the setup's favour (default: against it)")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the tables")
    parser.add_argument("--csv", metavar="FILE", help="also write every setup of every case to FILE")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    parser.add_argument("--no-cache", action="store_true",
                        help="download every candle again instead of reusing the local candle cache")
    args = parser.parse_args(argv)
    if args.step is not None and args.step < 1:
        parser.error("--step must be at least 1")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if (args.group or args.subset) and not args.universe:
        parser.error("--group and --subset need --universe")
    universe, tickers = None, [t.upper() for t in args.tickers]
    if args.universe:
        try:
            universe = read_universe(args.universe)
        except UniverseError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        unknown = sorted(set(args.group or []) - set(universe["groups"])) + \
            sorted(set(args.subset or []) - set(universe["groups"]))
        if unknown:
            parser.error(f"no such group in {args.universe}: {', '.join(unknown)} "
                         f"(groups: {', '.join(universe['groups'])})")
        names = list(dict.fromkeys(args.group or universe["groups"]))
        if set(args.subset or []) - set(names):
            parser.error("--subset must name a group that is run (see --group)")
        groups = {name: universe["groups"][name]["tickers"] for name in names}
        tickers = list(dict.fromkeys(tickers + [t for name in names for t in groups[name]]))
    if not tickers:
        parser.error("give at least one TICKER or --universe FILE")
    backtest.CACHE = not args.no_cache
    horizons = list(dict.fromkeys(args.horizon))
    periods = [name for name, on in zip(PERIODS, (args.by_year, args.by_regime, args.by_part)) if on]
    view = {"periods": periods, "subsets": list(dict.fromkeys(args.subset or [])), "part": args.part,
            "groups": groups if universe else None}
    spans = {t: span for t, span in (universe["spans"] if universe else {}).items() if t in tickers}
    try:
        report = run(tickers, horizons, args.start, args.end, args.step, progress=not args.quiet,
                     method=args.method, optimistic=args.optimistic, jobs=args.jobs, spans=spans,
                     regimes=args.by_regime,
                     universe=universe, view=view)
    except backtest.BacktestError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if args.csv:
        write_csv(args.csv, report["records"])
        backtest.say(f"Wrote {args.csv}", not args.quiet)
    if args.json:
        print(json.dumps({"settings": report["settings"], **report["summary"]}, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report["summary"]["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
