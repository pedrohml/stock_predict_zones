#!/usr/bin/env python3
"""Backtest the baseline probabilities of rz.py on past dates. No Claude call.

    python backtest.py PETR4.SA NVDA --horizon 1-3m                    # the last three ended years
    python backtest.py NVDA --horizon 1-2w --start 2022-01-01 --end 2025-06-30 --step 5
    python backtest.py PETR4.SA --json                                 # one JSON object instead
    python backtest.py PETR4.SA --csv cases.csv                        # every forecast, one row each

For each ticker it downloads daily candles once (Yahoo Finance, split-adjusted, not
dividend-adjusted, like build_prompt.py) and builds weekly and monthly candles from them. Then,
for every `--step`-th trading day between `--start` and `--end`, it:

1. builds the input JSON as of that day's close with build_prompt.py's own code, from candles
   that end on that day (nothing later enters the input);
2. chooses zones from `rz.py levels` with the fixed rule in choose_zones();
3. runs `rz.py sim` on those zones (the same simulation and seed as a live run);
4. judges the result with `rz.outcome()` against the candles that followed, as
   score_projections.py judges a saved projection.

It reports how often events given a probability happened (reliability buckets) and Brier
scores, per kind of forecast, overall and per ticker. The zones are mechanical, not the ones
Claude would choose, and nothing is adjusted: this measures the baseline only. It describes
calibration. It says nothing about returns, and it is not advice.
"""
import argparse
import bisect
import csv
import inspect
import json
import logging
import sys
import textwrap
import time
from datetime import date, timedelta

import pandas as pd

import build_prompt
import score_projections as sp

HORIZONS = ("1-2w", "1-3m", "6-12m")   # daily or coarser: Yahoo keeps 15m candles for about 60 days
DEFAULT_STEP = {"1-2w": 5, "1-3m": 10, "6-12m": 21}   # trading days between two as-of dates
DEFAULT_YEARS = 3
AS_OF_TIME = (23, 0)   # each case is made after that day's close: session "closed"
SCENARIOS = ("breakdown", "breakout", "range", "whipsaw")

RULE = ("Zone rule (choose_zones): width = the mean of the two typical_zone_width numbers of "
        "`rz.py levels`. Candidates are the prices `levels` reports as evidence: pivot lows and highs, "
        "the lows and highs of the last three candles, the retracement levels and the two ends of the "
        "dominant swing; those below the price serve supports, those above it resistances. Walking outward from the price, a candidate within one width "
        "of the first member of the current cluster joins it; each cluster's level is the mean of "
        "its members, and its zone is one width centred there, cut at the price and pushed outward "
        "so it never overlaps the zone before it. The nearest clusters are kept. A side with too "
        "few clusters is filled with one-width zones, one width apart, beyond the last one.")

CAVEATS = [
    "This is the unadjusted baseline (rz.py sim) on mechanically chosen zones: not the zones or "
    "the adjusted probabilities of a real projection, and no events, dividends or earnings shocks.",
    "Cases overlap whenever the step is shorter than the horizon, and the entries of one case "
    "share one price path, so the forecasts are far from independent; n overstates the evidence.",
    "Tickers chosen today are survivors. Prices are split-adjusted to today's scale and not "
    "dividend-adjusted, so a large dividend shows up as a drop the simulation did not include.",
    "Calibration compares probabilities with events. It is not a measure of returns and does "
    "not show that the method works or fails.",
]


class BacktestError(Exception):
    """A ticker that cannot be backtested; the message is the reason."""


rz = sp.load_rz()
DEFAULT_PATHS = inspect.signature(rz.baseline).parameters["paths"].default


# ---- network -----------------------------------------------------------------

def download(ticker, start):
    """Daily candles since `start` -> {"rows", "tz", "currency", "venue"}. rows are [date, open,
    high, low, close], unrounded, in exchange time. The only function that touches the network."""
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    try:
        handle = yf.Ticker(ticker)
        frame = handle.history(start=start.isoformat(), interval="1d", auto_adjust=False, actions=False)
        meta = handle.get_history_metadata() or {}
    except Exception as error:
        raise BacktestError(f"could not fetch {ticker} from Yahoo ({type(error).__name__}: {error})"[:200])
    if frame is None or frame.empty:
        raise BacktestError(f"no daily candles for {ticker} from Yahoo since {start} "
                            "(unknown symbol or network failure)")
    tz = meta.get("exchangeTimezoneName") or "America/New_York"
    frame = frame.tz_convert(tz) if frame.index.tz is not None else frame.tz_localize(tz)
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    rows = [[when.strftime("%Y-%m-%d")] + [float(candle[c]) for c in ("Open", "High", "Low", "Close")]
            for when, candle in frame.iterrows()]
    return {"rows": rows, "tz": tz, "currency": meta.get("currency") or "unsure",
            "venue": meta.get("fullExchangeName") or meta.get("exchangeName") or "unsure"}


# ---- the input as of a past day ------------------------------------------------

def period_offset(period):
    """A Yahoo period ("5d", "6mo", "2y") -> a pandas offset."""
    for suffix, unit in (("mo", "months"), ("d", "days"), ("y", "years")):
        if period.endswith(suffix):
            return pd.DateOffset(**{unit: int(period[:-len(suffix)])})
    raise ValueError(f"unknown period {period}")


def lookback(horizon):
    """How far before the first as-of date the download must start."""
    preset = build_prompt.PRESETS[horizon]
    spans = [period_offset(entry[1]) for entry in (preset["main"], preset["context"]) if entry]
    first = date(2000, 1, 1)
    return max((first - (pd.Timestamp(first) - span).date()).days for span in spans) + 40


def frame_of(rows, interval, tz):
    """[date, open, high, low, close] daily rows -> the candles of `interval` as a frame like
    Yahoo's: one row per day, week (from its Monday) or month (from its 1st), in exchange time."""
    basis = {"1d": "1D", "1wk": "1W", "1mo": "1M"}[interval]
    candles = sp.aggregate(rows, basis)
    index = pd.DatetimeIndex([pd.Timestamp(row[0]) for row in candles]).tz_localize(tz)
    return pd.DataFrame([row[1:] for row in candles], index=index, columns=["Open", "High", "Low", "Close"])


def input_as_of(ticker, horizon, data, day, supports=3, resistances=3):
    """The input JSON build_prompt.py would have written after the close of `day`, from the
    downloaded daily rows that end on that day. Rows after `day` are never read."""
    rows = data["rows"][:bisect.bisect_right([row[0] for row in data["rows"]], day.isoformat())]
    if not rows or rows[-1][0] != day.isoformat():
        raise ValueError(f"no candle on {day}")
    args = build_prompt.arguments().parse_args(
        [ticker, "--horizon", horizon, "--research", "none",
         "--supports", str(supports), "--resistances", str(resistances)])
    tz = data["tz"]
    now = pd.Timestamp(day.year, day.month, day.day, *AS_OF_TIME, tz=tz)
    session = "closed"

    def candles_of(interval, period):
        since = (now - period_offset(period)).normalize()
        recent = [row for row in rows if row[0] >= (since - pd.Timedelta(days=40)).strftime("%Y-%m-%d")]
        frame = frame_of(recent, interval, tz)
        return frame[frame.index >= since]

    series = build_prompt.make_all_series(args, candles_of, now, session, name=ticker)
    meta = {"exchangeTimezoneName": tz, "currency": data["currency"], "fullExchangeName": data["venue"]}
    return build_prompt.assemble(args, meta, now, session, rows[-1][4], series, events=[])


# ---- zones -------------------------------------------------------------------

def choose_zones(facts, supports=3, resistances=3, places=2):
    """The baseline's zones from the output of `rz.py levels` (rz.facts()). See RULE. Deterministic,
    and the only place the rule lives. -> {"supports": [[low, high], ...], "resistances": [...]},
    nearest first, which is what `rz.py sim` reads."""
    price = float(facts["price"])
    tick = 10 ** -places
    width = max(round(sum(facts["typical_zone_width"]) / 2, places), tick)

    candidates = ([p for _, p in facts["pivot_lows"] + facts["pivot_highs"]]
                  + [p for _, p in facts["recent_lows"] + facts["recent_highs"]]
                  + list(facts["retracements"].values()) + [facts["swing"]["low"][0], facts["swing"]["high"][0]])
    candidates = sorted(float(p) for p in candidates)

    def side(count, is_support):
        sign = -1 if is_support else 1           # outward from the price
        levels = sorted((p for p in candidates if (p < price if is_support else p > price)),
                        key=lambda p: abs(p - price))
        clusters = []
        for level in levels:
            if clusters and abs(level - clusters[-1][0]) <= width:
                clusters[-1].append(level)
            else:
                clusters.append([level])
        beyond = lambda x, y: sign * (x - y) > 0     # x lies further from the price than y
        zones, edge = [], price                  # `edge`: the far edge of the last zone so far
        for members in clusters[:count]:
            centre = sum(members) / len(members)
            near, far = centre - sign * width / 2, centre + sign * width / 2
            if not zones:
                near = near if beyond(near, price) else price          # cut at the price
            elif not beyond(near, edge) and near != edge:
                near, far = edge, edge + sign * width                  # pushed past the zone before
            zones.append(sorted([round(float(near), places), round(float(far), places)]))
            edge = zones[-1][0] if is_support else zones[-1][1]
        while len(zones) < count:                # too few clusters: volatility steps beyond the last zone
            near = edge + sign * width
            far = near + sign * width
            if far <= 0:
                break
            zones.append(sorted([round(near, places), round(far, places)]))
            edge = zones[-1][0] if is_support else zones[-1][1]
        return zones

    return {"supports": side(supports, True), "resistances": side(resistances, False)}


def projection(d, zones, out):
    """The parts of a projection that rz.outcome() and rz.problems() read, from the simulation's
    output `out` copied verbatim (as SKILL.md says when nothing is adjusted), with the tails."""
    o = {"asset": dict(d["asset"]), "as_of": {"time": d["now"]["time"], "price": d["now"]["price"]},
         "horizon": out["horizon"], "scenarios": out["scenarios"]}
    for name in ("supports", "resistances"):
        support = name == "supports"
        real = zones[name]
        edge = real[-1][0] if support else real[-1][1]
        boxes = [list(z) for z in real] + [[None, edge] if support else [edge, None]]
        o[name] = [{"id": entry["id"], "zone": box, "reach": entry["reach"], "turn": entry["turn"],
                    "evidence": ["backtest zone rule"]} for entry, box in zip(out[name], boxes)]
    return o


# ---- one case and the whole run ----------------------------------------------

def run_case(ticker, horizon, data, day, realised, today, paths=DEFAULT_PATHS):
    """One past date -> a record with the zones, the forecasts and the outcome, or a
    `skipped` reason."""
    record = {"ticker": ticker, "as_of": day.isoformat()}
    d = input_as_of(ticker, horizon, data, day)
    loaded = rz.unpack(d)
    main = loaded[1]
    keep = build_prompt.PRESETS[horizon]["main"][2]
    if len(main["rows"]) < keep:
        return {**record, "skipped": f"less history than a live run sees ({keep} {main['candles']} candles)"}
    zones = choose_zones(rz.facts(*loaded), d["want"]["supports"], d["want"]["resistances"])
    if not zones["supports"] or not zones["resistances"]:
        return {**record, "skipped": "the zone rule found no zone on one side"}
    out = rz.baseline(loaded, zones, paths)
    o = projection(d, zones, out)
    bad = rz.problems(o, complete=False)
    if bad:
        return {**record, "skipped": "the zone rule made an invalid projection: " + "; ".join(bad)}
    record.update(price=d["now"]["price"], last=o["horizon"]["last"], zones=zones)
    if date.fromisoformat(o["horizon"]["last"]) >= today:
        return {**record, "skipped": "horizon not ended yet"}
    try:
        result = rz.outcome(d, o, realised, today)
    except ValueError as reason:
        return {**record, "skipped": str(reason)}
    if result["closes"]["counted"] < result["closes"]["scheduled"] / 2:
        return {**record, "skipped": "fewer than half of the scheduled candles exist"}
    record.update(result)
    return record


def as_of_days(rows, start, end, step):
    """Every `step`-th trading day (a day with a candle) from `start` to `end`."""
    days = [date.fromisoformat(row[0]) for row in rows if start.isoformat() <= row[0] <= end.isoformat()]
    return days[::step]


def default_range(horizon, today):
    end = today - timedelta(days=rz.HORIZON_DAYS[horizon] + 7)
    return end.replace(year=end.year - DEFAULT_YEARS), end


def run(tickers, horizon, start=None, end=None, step=None, paths=DEFAULT_PATHS, today=None, progress=True):
    """The whole backtest -> {"settings", "records", "summary"}."""
    step = step or DEFAULT_STEP[horizon]
    first_start, first_end = default_range(horizon, today or date.today())
    start, end = start or first_start, end or first_end
    if start > end:
        raise BacktestError(f"--start {start} is after --end {end}")
    records, failed = [], {}
    for ticker in tickers:
        ticker = ticker.upper()
        clock = time.monotonic()
        try:
            data = download(ticker, start - timedelta(days=lookback(horizon)))
        except BacktestError as reason:
            failed[ticker] = str(reason)
            say(f"{ticker}: not backtested: {reason}", progress)
            continue
        ticker_today = today or sp.today_in(data["tz"])
        basis = build_prompt.PRESETS[horizon]["close_basis"]
        places = build_prompt.arguments().get_default("decimals")
        realised = [[row[0]] + [round(x, places) for x in row[1:]] for row in sp.aggregate(data["rows"], basis)]
        days = as_of_days(data["rows"], start, end, step)
        mine = [run_case(ticker, horizon, data, day, realised, ticker_today, paths) for day in days]
        records += mine
        used = sum(1 for r in mine if "skipped" not in r)
        say(f"{ticker}: {len(days)} dates, {used} scored, {len(days) - used} skipped "
            f"({time.monotonic() - clock:.0f}s)", progress)
    settings = {"tickers": [t.upper() for t in tickers], "horizon": horizon, "start": start.isoformat(),
                "end": end.isoformat(), "step_trading_days": step, "paths": paths,
                "default_paths": DEFAULT_PATHS, "rule": RULE, "failed": failed}
    return {"settings": settings, "records": records, "summary": summarise(records)}


def say(text, progress):
    if progress:
        print(text, file=sys.stderr)


# ---- summary -----------------------------------------------------------------

def by_scenario(scored):
    """Reliability and Brier score of each scenario on its own, with sp's buckets and brier()."""
    out = {}
    for name in SCENARIOS:
        pairs = [(s["p"], s["happened"]) for r in scored for s in r["scenarios"] if s["name"] == name]
        if not pairs:
            continue
        rate = sum(bool(y) for _, y in pairs) / len(pairs)
        out[name] = {"n": len(pairs), "brier": round(sp.brier(pairs), 4), "base_rate": round(rate, 3),
                     "brier_base_rate": round(sp.brier([(rate, y) for _, y in pairs]), 4),
                     "buckets": sp.buckets(pairs)}
    return out


def scope(records):
    scored = [r for r in records if "skipped" not in r]
    c = sp.calibration(scored)
    c.pop("caveats", None)
    c["cases"] = c.pop("projections")
    c["scenarios"] = by_scenario(scored)
    return c


def summarise(records):
    skipped = {}
    for r in records:
        if "skipped" in r:
            reason = r["skipped"].split(":")[0] if r["skipped"].startswith("the zone rule made") else r["skipped"]
            skipped[reason] = skipped.get(reason, 0) + 1
    tickers = sorted({r["ticker"] for r in records})
    return {"cases": sum(1 for r in records if "skipped" not in r), "skipped": skipped,
            "caveats": CAVEATS, "overall": scope(records),
            "per_ticker": {t: scope([r for r in records if r["ticker"] == t]) for t in tickers}}


# ---- report ------------------------------------------------------------------

def table(name, k, uninformed=None):
    lines = [f"  {name}: {k['n']} forecasts, {k['base_rate']:.0%} happened; Brier {k['brier']:.3f}"
             + (f", always {uninformed:.0%} {k['brier_uninformed']:.3f}" if uninformed is not None else "")
             + f", always the base rate {k['brier_base_rate']:.3f}",
             f"    {'forecast':<9} {'n':>5} {'mean forecast':>14} {'observed':>9}"]
    for b in k["buckets"]:
        lines.append(f"    {str(b['from']) + '-' + str(b['to']) + '%':<9} {b['n']:>5} "
                     f"{b['mean_forecast']:>14.2f} {b['observed']:>9.2f}")
    return lines


def render_scope(title, c):
    lines = [title, f"  {c['cases']} case(s), {c['forecasts']} forecast probabilities (Brier: 0 is perfect)"]
    if not c["cases"]:
        return lines + ["  Nothing was scored."]
    for kind, k in c["kinds"].items():
        lines += table(kind if kind != "scenario" else "scenario (all four)", k, sp.UNINFORMED[kind])
    for name, k in c["scenarios"].items():
        lines += table(f"scenario {name}", k)
    return lines


def render(report):
    s, summary = report["settings"], report["summary"]
    lines = [f"BACKTEST of the baseline probabilities, horizon {s['horizon']}",
             f"  {', '.join(s['tickers'])}; as-of dates {s['start']} to {s['end']}, every "
             f"{s['step_trading_days']} trading day(s), after the close",
             f"  {summary['cases']} case(s) scored"
             + (f"; skipped: " + ", ".join(f"{n} {why}" for why, n in summary["skipped"].items())
                if summary["skipped"] else "")]
    if s["paths"] != s["default_paths"]:
        lines.append(f"  NOTE: {s['paths']:,} simulated paths per case, not the {s['default_paths']:,} of "
                     "rz.py sim: the probabilities differ slightly from a live run's.")
    for ticker, reason in s["failed"].items():
        lines.append(f"  {ticker}: not backtested: {reason}")
    for text in [s["rule"]] + summary["caveats"]:
        lines += textwrap.wrap(text, 100, initial_indent="  ", subsequent_indent="    ")
    lines.append("")
    lines += render_scope("OVERALL", summary["overall"])
    if len(summary["per_ticker"]) > 1:
        for ticker, c in summary["per_ticker"].items():
            lines += [""] + render_scope(ticker, c)
    return "\n".join(lines)


CSV_FIELDS = ["ticker", "as_of", "price", "horizon_last", "kind", "id", "zone_low", "zone_high",
              "forecast", "happened", "forced"]


def write_csv(path, records):
    """One row per forecast of every scored case."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.DictWriter(handle, CSV_FIELDS)
        out.writeheader()
        for r in records:
            if "skipped" in r:
                continue
            base = {"ticker": r["ticker"], "as_of": r["as_of"], "price": r["price"], "horizon_last": r["last"]}
            for side in ("supports", "resistances"):
                for e in r[side]:
                    zone = {"id": e["id"], "zone_low": e["zone"][0], "zone_high": e["zone"][1], "forced": e["forced"]}
                    out.writerow({**base, **zone, "kind": "reach", "forecast": e["reach"], "happened": e["reached"]})
                    out.writerow({**base, **zone, "kind": "turn", "forecast": e["turn"], "happened": e["turned"]})
            for sc in r["scenarios"]:
                out.writerow({**base, "kind": "scenario", "id": sc["name"], "forecast": sc["p"],
                              "happened": sc["happened"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tickers", nargs="+", metavar="TICKER", help="Yahoo symbols, e.g. PETR4.SA NVDA")
    parser.add_argument("--horizon", choices=HORIZONS, default="1-3m")
    parser.add_argument("--start", type=date.fromisoformat,
                        help=f"first as-of date (default: {DEFAULT_YEARS} years before --end)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="last as-of date (default: today minus the horizon, so every horizon has ended)")
    parser.add_argument("--step", type=int, metavar="N",
                        help="trading days between two as-of dates (default: "
                             + ", ".join(f"{h} {n}" for h, n in DEFAULT_STEP.items()) + ")")
    parser.add_argument("--paths", type=int, default=DEFAULT_PATHS,
                        help=f"simulated paths per case (default: {DEFAULT_PATHS:,}, as rz.py sim)")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the tables")
    parser.add_argument("--csv", metavar="FILE", help="also write every forecast of every case to FILE")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    args = parser.parse_args(argv)
    if args.step is not None and args.step < 1 or args.paths < 1000:
        parser.error("--step must be at least 1 and --paths at least 1000")

    try:
        report = run(args.tickers, args.horizon, args.start, args.end, args.step, args.paths,
                     progress=not args.quiet)
    except BacktestError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if args.csv:
        write_csv(args.csv, report["records"])
        say(f"Wrote {args.csv}", not args.quiet)
    if args.json:
        print(json.dumps({"settings": report["settings"], **report["summary"]}, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report["summary"]["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
