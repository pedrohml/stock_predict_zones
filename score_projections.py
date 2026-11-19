#!/usr/bin/env python3
"""Compare the saved projections with what the price did afterwards.

    python score_projections.py                       # everything under projections/
    python score_projections.py --ticker PETR4.SA     # one ticker
    python score_projections.py projections/PETR4.SA/2026-11-18T1515_1-3m   # one saved folder
    python score_projections.py --json                # the same, for a program

Each saved projection is judged with the definitions of the simulation that produced its
probabilities (`outcome()` in the skill's rz.py). Projections whose horizon has ended then go
into a calibration summary: forecast probability against observed frequency, overall and per
model (from the provenance.json that `zones` saves with each projection; older entries have
none and count as "unrecorded"). This describes how the probabilities compared with events.
It says nothing about returns, and it is not advice.
"""
import argparse
import importlib.util
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RZ_PATH = Path(__file__).resolve().parent / ".claude" / "skills" / "reversal-zones-projection" / "scripts" / "rz.py"

# Weekly and monthly candles are built from daily ones: one download per ticker then serves
# every daily-or-coarser horizon, and the candle dates do not depend on how Yahoo aligns weeks.
YAHOO_INTERVAL = {"1D": "1d", "1W": "1d", "1M": "1d", "15m": "15m", "1h": "1h"}
LOOKBACK_DAYS = {"1d": 45, "15m": 4, "1h": 4}   # history before as_of: the forming candle and one closed one
MISMATCH = 0.01    # realised and saved close of the same candle may differ by this share
KINDS = ("reach", "turn", "scenario")
UNINFORMED = {"reach": 0.5, "turn": 0.5, "scenario": 0.25}
UNRECORDED = "unrecorded"   # the model of a projection saved without provenance.json

CAVEATS = [
    "Small samples are anecdote, not evidence: one projection adds about twenty forecasts at once.",
    "Forecasts are not independent: the entries of one projection are judged on the same price "
    "path, and overlapping horizons of one ticker share most of it.",
    "Calibration compares probabilities with events. It is not a measure of returns and does "
    "not show that the method works or fails.",
]


class Unscorable(Exception):
    """A projection that cannot be compared with prices; the message is the reason."""


def load_rz():
    """The skill's helper, which holds the outcome definitions and is not on the import path."""
    spec = importlib.util.spec_from_file_location("rz", RZ_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def today_in(tz):
    """Today's date at the exchange."""
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return date.today()


CACHE = True   # download() reuses finished candles from data_source's cache; --no-cache turns it off


def download(ticker, interval, start, tz, cache=None):
    """Candles from Yahoo since `start` -> (rows, splits). rows are [start, open, high, low,
    close] in exchange time, unrounded; splits are [date, ratio]. Prices are split-adjusted and
    not dividend-adjusted, as in build_prompt.py. Raises Unscorable with a one-line reason.
    The request goes through data_source (retries, timeout, shared rate limit, row checks);
    finished daily candles come from its cache unless `cache` (None: CACHE) is false. The
    latest candles are always downloaded, so an open projection is judged on current prices."""
    import pandas as pd
    import yfinance as yf
    import data_source
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # it logs what we report ourselves
    try:
        frame, report = data_source.candles(yf.Ticker(ticker), ticker, interval, start=start.isoformat(),
                                            actions=True, cache=CACHE if cache is None else cache,
                                            what=f"{ticker} {interval}")
    except data_source.DataError as error:
        raise Unscorable(str(error)[:300])
    except Exception as error:
        raise Unscorable(f"could not fetch {ticker} from Yahoo ({type(error).__name__}: {error})"[:200])
    if frame is None or frame.empty:
        hint = "; Yahoo keeps intraday candles for about 60 days" if interval != "1d" else ""
        raise Unscorable(f"no {interval} candles for {ticker} from Yahoo since {start} "
                         f"(unknown symbol or network failure{hint})")
    frame = frame.tz_convert(tz) if frame.index.tz is not None else frame.tz_localize(tz)
    stamp = "%Y-%m-%d" if interval == "1d" else "%Y-%m-%dT%H:%M"
    splits = []
    for when, ratio in report["splits"]:     # read before the row checks, cached ones included
        when = pd.Timestamp(when)
        when = when.tz_convert(tz) if when.tzinfo is not None else when.tz_localize(tz)
        splits.append([when.strftime("%Y-%m-%d"), float(ratio)])
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    rows = [[when.strftime(stamp)] + [float(candle[c]) for c in ("Open", "High", "Low", "Close")]
            for when, candle in frame.iterrows()]
    return rows, splits


class Prices:
    """Downloads each (ticker, interval) once per run, from the earliest date any projection needs."""

    def __init__(self):
        self.start, self.cache = {}, {}

    def need(self, ticker, interval, start):
        key = (ticker, interval)
        self.start[key] = min(start, self.start.get(key, start))

    def get(self, ticker, interval, start, tz):
        key = (ticker, interval)
        if key not in self.cache:
            try:
                self.cache[key] = download(ticker, interval, self.start.get(key, start), tz)
            except Unscorable as reason:
                self.cache[key] = reason
        if isinstance(self.cache[key], Unscorable):
            raise self.cache[key]
        return self.cache[key]


def aggregate(rows, basis):
    """Daily rows -> weekly (labelled by the week's Monday) or monthly (by the 1st) candles."""
    if basis == "1D":
        return [list(row) for row in rows]
    out = []
    for start, open_, high, low, close in rows:
        day = date.fromisoformat(start)
        label = (day - timedelta(days=day.weekday()) if basis == "1W" else day.replace(day=1)).isoformat()
        if out and out[-1][0] == label:
            out[-1][2:] = [max(out[-1][2], high), min(out[-1][3], low), close]
        else:
            out.append([label, open_, high, low, close])
    return out


def decimals(main):
    """The number of decimals the input's prices were rounded to."""
    return max((len(repr(float(x)).split(".")[1]) if "e" not in repr(float(x)) else 6
                for row in main["rows"][-30:] for x in row[1:5]), default=2)


def plan(rz, d):
    """(ticker, Yahoo interval, first date to fetch, timezone, basis) for one saved input."""
    basis = rz.main_series(d)["candles"]
    if basis not in YAHOO_INTERVAL:
        raise Unscorable(f"no price source for {basis} candles")
    interval = YAHOO_INTERVAL[basis]
    start = date.fromisoformat(d["now"]["time"][:10]) - timedelta(days=LOOKBACK_DAYS[interval])
    return d["asset"]["ticker"].upper(), interval, start, d["data"].get("timezone") or "America/New_York", basis


def read_input(folder):
    path = os.path.join(folder, "input.json")
    if not os.path.isfile(path):
        raise Unscorable("input.json is missing from the saved folder")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def made_by(folder):
    """{"model", "rules", "commit", "dirty"} from the saved provenance.json, as far as it says;
    None when the folder has none (saved before provenance existed, or by the skill alone)."""
    try:
        with open(os.path.join(folder, "provenance.json"), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    claude = (record.get("projection") or {}).get("claude") or {}
    rules = record.get("rules") or {}
    git = rules.get("git") or {}
    used = claude.get("models_used") or []
    out = {"model": claude.get("model") or (used[0] if used else None), "rules": rules.get("version"),
           "commit": git.get("commit"), "dirty": git.get("dirty")}
    return {key: value for key, value in out.items() if value is not None}


def score_one(rz, folder, o, prices):
    """One record per saved projection: the outcome, or `not_scored` with the reason."""
    record = {"folder": folder}
    origin = made_by(folder)
    if origin:
        record["made_by"] = origin
    try:
        record.update(ticker=o["asset"]["ticker"], horizon=o["horizon"]["label"], as_of=o["as_of"]["time"],
                      price=o["as_of"]["price"], last=o["horizon"]["last"])
        d = read_input(folder)
        ticker, interval, start, tz, basis = plan(rz, d)
        main = rz.main_series(d)
        rows, splits = prices.get(ticker, interval, start, tz)
        as_of = d["now"]["time"][:10]
        later = [f"{when} ({ratio:g} for 1)" for when, ratio in splits if when > as_of]
        if later:
            raise Unscorable(f"stock split after the projection, on {', '.join(later)}: "
                             "its zone prices are on the old scale")
        places = decimals(main)
        rows = [[row[0]] + [round(x, places) for x in row[1:]] for row in aggregate(rows, basis)]
        check_scale(rz, main, basis, rows)
        out = rz.outcome(d, o, rows, today_in(tz))
        seen, wanted = out["closes"]["counted"], out["closes"]["scheduled"]
        if out["final"] and seen < wanted / 2:
            raise Unscorable(f"only {seen} of the {wanted} scheduled candles were found")
        out["notes"] = notes(out, main)
        record.update(out)
    except Unscorable as reason:
        record["not_scored"] = str(reason)
    except ValueError as reason:            # rz.outcome: nothing to judge yet, which is not a failure
        record.update(not_scored=str(reason), waiting=True)
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        record["not_scored"] = f"malformed saved files ({type(error).__name__}: {error})"
    return record


def check_scale(rz, main, basis, rows):
    """The newest candle that was already closed in the input must have the same close in the
    realised series. If not, the two are on different price scales and zones cannot be compared."""
    closed = main["rows"][:-1] if main["last_row"] == "forming" else main["rows"]
    if not closed:
        return
    start, saved = closed[-1][0], closed[-1][4]
    daily = basis in rz.CANDLE_DAYS
    same = [row for row in rows
            if (rz.closes_on(basis, row[0]) == rz.closes_on(basis, start) if daily else row[0] == start)]
    if same and saved and abs(same[0][4] / saved - 1) > MISMATCH:
        raise Unscorable(f"realised prices do not match the input's (candle {start} closed at {saved} in "
                         f"the input, {same[0][4]} now): rescaled since, perhaps by a split")


def notes(out, main):
    found = []
    seen, wanted = out["closes"]["counted"], out["closes"]["scheduled"]
    what = "candles" if out["measure"] == "touch" else "closes"
    if out["final"] and seen < wanted:
        found.append(f"{seen} of the {wanted} scheduled {what} exist (market holidays or missing data); "
                     "judged on those")
    if seen > wanted:
        found.append(f"{seen} {what} fall between the projection and horizon.last, {wanted} were scheduled; "
                     "all are counted")
    if out["measure"] == "touch" and main["last_row"] == "forming":
        found.append("the first candle began before the projection: its high or low counts only if it "
                     "is a new extreme, otherwise its close is used")
    return found


# ---- calibration -------------------------------------------------------------

def forecasts(record):
    """(kind, forecast probability, happened) for every probability in one scored projection.
    A nearest zone the price started inside has reach 1 by definition, so that is not a forecast."""
    found = []
    for side in ("supports", "resistances"):
        for entry in record[side]:
            if not entry["forced"]:
                found.append(("reach", entry["reach"], entry["reached"]))
            found.append(("turn", entry["turn"], entry["turned"]))
    found += [("scenario", s["p"], s["happened"]) for s in record["scenarios"]]
    return found


def brier(pairs):
    return sum((p - bool(y)) ** 2 for p, y in pairs) / len(pairs)


def buckets(pairs, width=10):
    """Forecasts grouped by probability: [0, 10%), [10%, 20%), ... [90%, 100%]. Empty ones are left out."""
    groups = {}
    for p, y in pairs:
        index = min(int(round(p * 100, 6) // width), 100 // width - 1)
        groups.setdefault(index, []).append((p, bool(y)))
    return [{"from": index * width, "to": (index + 1) * width, "n": len(group),
             "mean_forecast": round(sum(p for p, _ in group) / len(group), 3),
             "observed": round(sum(y for _, y in group) / len(group), 3)}
            for index, group in sorted(groups.items())]


def model_of(record):
    return (record.get("made_by") or {}).get("model") or UNRECORDED


def by_model(ended):
    """Brier score per model and kind: enough to tell models apart, if the samples allow."""
    out = {}
    for model in sorted({model_of(r) for r in ended}):
        mine = [r for r in ended if model_of(r) == model]
        kinds = {}
        for kind in KINDS:
            pairs = [(p, y) for r in mine for k, p, y in forecasts(r) if k == kind]
            if pairs:
                kinds[kind] = {"n": len(pairs), "brier": round(brier(pairs), 4)}
        out[model] = {"projections": len(mine), "kinds": kinds}
    return out


def calibration(records):
    """Summary over the scored projections whose horizon has ended."""
    ended = [r for r in records if r.get("final")]
    out = {"projections": len(ended), "tickers": len({r["ticker"].upper() for r in ended}),
           "forecasts": 0, "caveats": CAVEATS, "kinds": {}, "by_model": by_model(ended)}
    for kind in KINDS:
        pairs = [(p, y) for r in ended for k, p, y in forecasts(r) if k == kind]
        if not pairs:
            continue
        rate = sum(bool(y) for _, y in pairs) / len(pairs)
        out["forecasts"] += len(pairs)
        out["kinds"][kind] = {
            "n": len(pairs), "brier": round(brier(pairs), 4), "base_rate": round(rate, 3),
            # two reference forecasts: the same number every time
            "brier_uninformed": round(brier([(UNINFORMED[kind], y) for _, y in pairs]), 4),
            "brier_base_rate": round(brier([(rate, y) for _, y in pairs]), 4),
            "buckets": buckets(pairs)}
    return out


# ---- report ------------------------------------------------------------------

def money(x):
    return "open" if x is None else f"{x:g}"


def yes(flag):
    return "-" if flag is None else "yes" if flag else "no"


def origin_line(r):
    """'  made by MODEL · rules VERSION · commit ABCDEF1 (dirty)', or None without provenance."""
    origin = r.get("made_by")
    if not origin:
        return None
    parts = [f"made by {origin.get('model', 'an unrecorded model')}"]
    if origin.get("rules"):
        parts.append(f"rules {origin['rules']}")
    if origin.get("commit"):
        parts.append(f"commit {origin['commit'][:7]}" + (" (dirty)" if origin.get("dirty") else ""))
    return "  " + " · ".join(parts)


def render_projection(r):
    head = f"{r.get('ticker', '?')} {r.get('horizon', '?')}  as of {r.get('as_of', '?')} at {r.get('price', '?')}"
    origin = [origin_line(r)] if origin_line(r) else []
    if "not_scored" in r:
        label = "NOT SCORED YET" if r.get("waiting") else "NOT SCORED"
        return [head, *origin, f"  {label}: {r['not_scored']}", f"  {r['folder']}"]
    final = r["final"]
    so_far = "" if final else " so far"
    what = "candles" if r["measure"] == "touch" else "closes"
    lines = [head + (f"  ENDED {r['last']}" if final else f"  OPEN until {r['last']}: PROVISIONAL, so far"),
             *origin,
             f"  {r['closes']['counted']} of {r['closes']['scheduled']} {what} ({r['measure']} measure), "
             f"{r['window'][0]} to {r['window'][1]}; low{so_far} {r['low'][0]:g} ({r['low'][1]}), "
             f"high{so_far} {r['high'][0]:g} ({r['high'][1]})",
             f"  {'id':<6} {'zone':<16} {'reach':>5}  {'reached' + so_far:<21} {'broken' + so_far:<13} "
             f"{'turn':>5}  {'turned' + so_far}"]
    for side in ("resistances", "supports"):
        entries = r[side][::-1] if side == "resistances" else r[side]
        for e in entries:
            zone = f"{money(e['zone'][0])} - {money(e['zone'][1])}"
            reached = "yes (started inside)" if e["forced"] else yes(e["reached"])
            lines.append(f"  {e['id']:<6} {zone:<16} {e['reach']:>5.2f}  {reached:<21} {yes(e['broken']):<13} "
                         f"{e['turn']:>5.2f}  {yes(e['turned'])}")
    odds = ", ".join(f"{s['name']} {s['p']:.2f}" for s in r["scenarios"])
    lines.append(f"  scenario{so_far}: {r['scenario']}  (forecast: {odds})")
    if not final:
        lines.append("  Reached and broken are final once they say yes; turned and the scenario can still change.")
    lines += [f"  note: {note}" for note in r["notes"]]
    lines.append(f"  {r['folder']}")
    return lines


def render_calibration(c, open_count, failed):
    lines = ["CALIBRATION, ended projections only",
             f"  {c['projections']} projection(s) of {c['tickers']} ticker(s), {c['forecasts']} forecast probabilities"
             + (f"; {open_count} open and {failed} not scored are left out" if open_count or failed else "")]
    if not c["projections"]:
        return lines + ["  No horizon has ended yet, so there is nothing to summarise."]
    lines += [f"  {text}" for text in c["caveats"]]
    for kind, k in c["kinds"].items():
        lines += ["", f"  {kind}: {k['n']} forecasts, {k['base_rate']:.0%} happened",
                  f"    Brier score {k['brier']:.3f} (0 is perfect); always saying {UNINFORMED[kind]:.0%} would "
                  f"score {k['brier_uninformed']:.3f}, always saying this sample's own rate {k['brier_base_rate']:.3f}",
                  f"    {'forecast':<9} {'n':>4} {'mean forecast':>14} {'observed':>9}"]
        for b in k["buckets"]:
            lines.append(f"    {str(b['from']) + '-' + str(b['to']) + '%':<9} {b['n']:>4} "
                         f"{b['mean_forecast']:>14.2f} {b['observed']:>9.2f}")
    models = c.get("by_model") or {}
    if models:
        lines += ["", "  by model (Brier score, n forecasts; tiny samples say nothing):"]
        for model, m in models.items():
            scores = ", ".join(f"{kind} {k['brier']:.3f} (n {k['n']})" for kind, k in m["kinds"].items())
            lines.append(f"    {model}: {m['projections']} projection(s); {scores}")
    return lines


def run(folder=None, ticker=None):
    """Score everything saved under `folder` -> {"projections": [...], "calibration": {...}}."""
    rz = load_rz()
    found = rz.saved(folder)
    if ticker:
        found = [(here, o) for here, o in found
                 if str(o.get("asset", {}).get("ticker", "")).upper() == ticker.upper()]
    prices = Prices()
    for here, _ in found:     # so each ticker is downloaded once, from the earliest date needed
        try:
            symbol, interval, start, _, _ = plan(rz, read_input(here))
            prices.need(symbol, interval, start)
        except Exception:
            pass              # score_one reports it
    records = [score_one(rz, here, o, prices) for here, o in found]
    return {"projections": records, "calibration": calibration(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", nargs="?",
                        help="the folder of saved projections, or one saved projection inside it "
                             "(default: $RZ_PROJECTIONS_DIR, else ./projections)")
    parser.add_argument("--ticker", help="only projections of this ticker, e.g. PETR4.SA")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the tables")
    parser.add_argument("--no-cache", action="store_true",
                        help="download every candle again instead of reusing finished ones from the "
                             "local candle cache (README: Data sources)")
    args = parser.parse_args(argv)
    global CACHE
    CACHE = not args.no_cache

    report = run(args.folder, args.ticker)
    records = report["projections"]
    if not records:
        where = args.folder or os.environ.get("RZ_PROJECTIONS_DIR") or "projections"
        print(f"No saved projections found under {where}" + (f" for {args.ticker}" if args.ticker else "") + ".",
              file=sys.stderr)
        return 1
    scored = [r for r in records if "not_scored" not in r]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for record in records:
            print("\n".join(render_projection(record)) + "\n")
        open_count = sum(1 for r in scored if not r["final"])
        print("\n".join(render_calibration(report["calibration"], open_count, len(records) - len(scored))))
    failed = [r for r in records if "not_scored" in r and not r.get("waiting")]
    return 1 if failed and not scored else 0


if __name__ == "__main__":
    sys.exit(main())
