#!/usr/bin/env python3
"""Does Claude's adjustment beat the free baseline? Paid: every case is one run of step 2.

    python value_test.py NVDA PETR4.SA --start 2026-07-01 --end 2026-11-04 --out value_runs
    python value_test.py NVDA --out value_runs --max-cost 5 --jobs 3
    python value_test.py --out value_runs --report-only          # score what is there, no new run

For each ticker and past as-of date (every `--step`-th trading day from `--start` to `--end`):

1. the input as of that day's close, built by backtest.input_as_of() (research none, no events),
   so Claude gets no web tool and nothing after the as-of day;
2. step 2 through the real pipeline: `run_pipeline.py --input FILE --no-entries --no-save
   --model MODEL --out-dir CASE`, which runs the skill headless and checks the projection;
3. the unadjusted baseline on the zones Claude chose (rz.baseline() on its zone edges), so
   the two differ only by Claude's adjustment;
4. backtest.py's mechanical zones and their baseline (backtest.run_case()), which separates
   the choice of zones from the adjustment;
5. all three judged by rz.outcome() against the candles that followed, as the backtest does.

The report gives Brier scores per kind (reach, turn, scenario) for each version, the paired
difference per case (Claude minus the baseline on the same zones) with a bootstrap interval
over cases, and how often and how far Claude moved the numbers.

Look-ahead: a model has seen prices up to its training cutoff. An as-of date on or before
`--cutoff` (default: the training data cutoff of the model `sonnet` resolves to, from
Anthropic's models overview) is refused, and a run whose model (read from Claude Code's own
events in provenance.json) has no known cutoff, or a later one, is not scored.

Budget: every run's cost (Claude Code's own total_cost_usd, from provenance.json; else the
cost line it printed) goes into the case folder. A run is started only while the spent total,
plus one reserve per run in flight, plus one more reserve, stays within `--max-cost`. The
reserve is the highest cost seen so far (FIRST_RESERVE before the first). Runs already in
`--out` are never paid for again, failed ones included.

This measures calibration. It is not a measure of returns and not advice.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import backtest as bt
import build_prompt
import score_projections as sp

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "run_pipeline.py"
rz = bt.rz

DEFAULT_MODEL = "sonnet"
DEFAULT_HORIZON = "1-2w"
DEFAULT_STEP = 10                 # trading days: about one 1-2w horizon, so cases barely overlap
DEFAULT_MAX_COST = 20.0
FIRST_RESERVE = 0.60              # the reserve before any run has reported its cost (USD)
TIMEOUT = 1200                    # seconds per run of step 2
BOOTSTRAP = 10_000
KINDS = sp.KINDS                  # reach, turn, scenario
VERSIONS = ("claude", "baseline", "mechanical")
STEM = "case"                     # file stem inside a case folder

# Training data cutoff (the last month of training data) by model id, from Anthropic's models
# overview, row "Training data cutoff", read on 19 Nov 2026. Add a model only from that page.
CUTOFF_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview"
TRAINING_CUTOFF = {"claude-sonnet-5-5": "2026-06", "claude-opus-5-5": "2026-06",
                   "claude-haiku-5-5": "2026-06", "claude-fable-5-2": "2026-06"}
EXPECTED_MODEL = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}

CAVEATS = [
    "The input's now.time is in the past: each case is built as of a past close, and Claude Code "
    "also tells the model today's date. No web tool is given (research none, no events).",
    "Only as-of dates after the model's training data cutoff are used, so the model cannot have "
    "seen what followed. That leaves few dates and few cases: the intervals are wide.",
    "Cases of one ticker on nearby dates and the entries of one case share price paths, so the "
    "forecasts are not independent. The bootstrap resamples whole cases, which helps but does "
    "not remove the dependence between tickers on the same dates.",
    "The skill may move at most 0.10 of probability per side, so differences are small by design.",
    "Calibration of probabilities only: not a measure of returns, and not advice.",
]


class ValueTestError(Exception):
    """A setting that cannot be run; the message is the reason."""


def say(text, quiet=False):
    if not quiet:
        print(text, file=sys.stderr, flush=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---- look-ahead --------------------------------------------------------------

def month_end(month):
    """"2026-06" -> 2026-06-30."""
    first = date.fromisoformat(month + "-01")
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def default_cutoff(model):
    return TRAINING_CUTOFF.get(EXPECTED_MODEL.get(model, model))


def model_ok(model, day):
    """Why a run by `model` cannot be scored for an as-of `day`, or None when it can."""
    if not model:
        return "the run did not report its model"
    if model not in TRAINING_CUTOFF:
        return f"no known training cutoff for {model}"
    if day <= month_end(TRAINING_CUTOFF[model]):
        return f"as-of {day} is not after {model}'s training cutoff ({TRAINING_CUTOFF[model]})"
    return None


# ---- data and cases ----------------------------------------------------------

def load_data(out, ticker, start, horizon, quiet=False):
    """Daily candles of `ticker` from backtest.download(), kept in OUT/data/ so a resumed run
    builds identical inputs and scores against the same candles."""
    path = out / "data" / f"{ticker}.json"
    if path.is_file():
        return read_json(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bt.download(ticker, start - timedelta(days=bt.lookback(horizon)))
    data["downloaded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(path, data)
    say(f"{ticker}: {len(data['rows'])} daily candles downloaded", quiet)
    return data


def realised_rows(data, horizon):
    """The realised candles of the horizon's close basis, as backtest.run() makes them."""
    basis = build_prompt.PRESETS[horizon]["close_basis"]
    places = build_prompt.arguments().get_default("decimals")
    return [[row[0]] + [round(x, places) for x in row[1:]] for row in sp.aggregate(data["rows"], basis)]


def case_dir(out, ticker, day):
    return out / "cases" / ticker / day.isoformat()


def plan(tickers, datas, start, end, step, seed=0):
    """Every (ticker, day) to run, in a fixed shuffled order, so a run stopped by the budget
    still covers the tickers and dates evenly."""
    cases = [(t, day) for t in tickers for day in bt.as_of_days(datas[t]["rows"], start, end, step)]
    random.Random(seed).shuffle(cases)
    return cases


def prepare(out, ticker, horizon, data, day):
    """The case folder with input.json (written once). -> (folder, None) or (None, reason)."""
    folder = case_dir(out, ticker, day)
    path = folder / "input.json"
    if path.is_file():
        return folder, None
    d = bt.input_as_of(ticker, horizon, data, day)
    main = rz.main_series(d)
    keep = build_prompt.PRESETS[horizon]["main"][2]
    if len(main["rows"]) < keep:
        return None, f"less history than a live run sees ({keep} {main['candles']} candles)"
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(build_prompt.render(d) + "\n", encoding="utf-8")
    return folder, None


# ---- step 2 and its cost ---------------------------------------------------------

def pipeline_command(folder, model, timeout=TIMEOUT):
    return [sys.executable, str(PIPELINE), "--no-config", "--input", str(folder / "input.json"),
            "--no-entries", "--no-save", "--no-summary", "--quiet", "--model", model,
            "--timeout", str(timeout), "--out-dir", str(folder), "--name", STEM]


def launch(folder, model, timeout=TIMEOUT):
    """Run step 2 on the case's input through run_pipeline.py. The only place that starts
    Claude Code (tests stub it). -> (exit code, stdout, stderr)."""
    env = {**os.environ, "RZ_PROJECTIONS_DIR": str(folder / "no-history")}
    done = subprocess.run(pipeline_command(folder, model, timeout), capture_output=True, text=True,
                          cwd=folder, env=env, timeout=timeout + 300)
    return done.returncode, done.stdout, done.stderr


STEP2_STARTED = re.compile(r"\[2/\d\] Projection: Claude Code")
COST_LINE = re.compile(r"(?:done in|stopped after) [^\n]*?\$(\d+(?:\.\d+)?)")


def run_record(folder, model, code, stdout, stderr, seconds):
    """What one run cost and made, from the files it left: provenance.json (exact cost and
    the model Claude Code reported), else the cost on its last summary line (rounded to
    cents), else unknown."""
    record = {"requested_model": model, "exit": code, "seconds": round(seconds, 1),
              "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    provenance = folder / f"{STEM}_provenance.json"
    if provenance.is_file():
        claude = read_json(provenance).get("projection", {}).get("claude", {})
        record.update(model=claude.get("model"), models_used=claude.get("models_used"),
                      num_turns=claude.get("num_turns"))
        if isinstance(claude.get("total_cost_usd"), (int, float)):
            record.update(cost=float(claude["total_cost_usd"]), cost_source="provenance")
    if "cost" not in record:
        found = COST_LINE.findall(stderr or "")
        if found:
            record.update(cost=float(found[-1]), cost_source="summary line")
        elif not STEP2_STARTED.search(stderr or ""):
            record.update(cost=0.0, cost_source="Claude Code not started")
    record["status"] = "ok" if code == 0 and (folder / f"{STEM}_projection.json").is_file() else "failed"
    if record["status"] == "failed":
        record["error"] = "\n".join((stderr or stdout or "").strip().splitlines()[-8:])
    return record


class Budget:
    """The spent total and the runs in flight, shared by the worker threads."""

    def __init__(self, limit, records):
        self.limit, self.lock, self.flying = limit, threading.Lock(), 0
        self.costs = [r["cost"] for r in records if isinstance(r.get("cost"), (int, float))]
        self.unknown = sum(1 for r in records if not isinstance(r.get("cost"), (int, float)))

    def reserve(self):
        return max(self.costs) if self.costs else FIRST_RESERVE

    def spent(self):
        """Reported costs, plus the reserve for each run whose cost is unknown."""
        return sum(self.costs) + self.unknown * self.reserve()

    def take(self):
        """True, and one more run in flight, if one more run fits the limit."""
        with self.lock:
            if self.spent() + (self.flying + 1) * self.reserve() > self.limit:
                return False
            self.flying += 1
            return True

    def settle(self, record):
        with self.lock:
            self.flying -= 1
            if isinstance(record.get("cost"), (int, float)):
                self.costs.append(record["cost"])
            else:
                self.unknown += 1


def run_records(out):
    return [read_json(p) for p in sorted((out / "cases").glob("*/*/run.json"))] if (out / "cases").is_dir() else []


def run_cases(out, todo, model, limit, jobs=1, timeout=TIMEOUT, quiet=False):
    """Run step 2 on every case in `todo` that has no run.json yet, within the budget.
    -> (number launched, number left unrun by the budget)."""
    budget = Budget(limit, run_records(out))
    pending = [folder for folder in todo if not (folder / "run.json").is_file()]
    launched, left = [], []

    def one(folder):
        if not budget.take():
            left.append(folder)
            return
        launched.append(folder)
        clock = time.monotonic()
        try:
            code, stdout, stderr = launch(folder, model, timeout)
        except subprocess.TimeoutExpired as error:
            code, stdout, stderr = -1, "", f"timed out: {error}"
        record = run_record(folder, model, code, stdout, stderr, time.monotonic() - clock)
        # a run that never reached Claude cost nothing and may be tried again: no run.json
        write_json(folder / ("run.json" if record.get("cost_source") != "Claude Code not started"
                             else "not_started.json"), record)
        (folder / "pipeline.log").write_text((stdout or "") + "\n--- stderr ---\n" + (stderr or ""),
                                             encoding="utf-8")
        budget.settle(record)
        cost = f"${record['cost']:.2f}" if "cost" in record else "cost unknown"
        say(f"{folder.parent.name} {folder.name}: {record['status']}, {cost}, "
            f"{record['seconds']:.0f}s (spent ${budget.spent():.2f} of ${limit:.2f})", quiet)

    if jobs <= 1:
        for folder in pending:
            one(folder)
    else:
        with ThreadPoolExecutor(jobs) as pool:
            list(pool.map(one, pending))
    return len(launched), len(left)


# ---- scoring -----------------------------------------------------------------

def zones_of(o):
    """The real zones of a projection, nearest first, as rz.baseline() reads them."""
    return {side: [list(x["zone"]) for x in o[side] if None not in x["zone"]]
            for side in ("supports", "resistances")}


def case_brier(scored):
    """{kind: (mean squared error over the case's forecasts of that kind, n)}."""
    out = {}
    for kind in KINDS:
        pairs = [(p, y) for k, p, y in sp.forecasts(scored) if k == kind]
        if pairs:
            out[kind] = (sp.brier(pairs), len(pairs))
    return out


def moves(claude, base):
    """How far Claude moved the baseline of the same zones: per side, the probability moved
    (half the sum of |turn change|), and the largest change of any one number."""
    out, changes = {}, []
    for side in ("supports", "resistances"):
        diffs = [abs(a["turn"] - b["turn"]) for a, b in zip(claude[side], base[side])]
        diffs_reach = [abs(a["reach"] - b["reach"]) for a, b in zip(claude[side], base[side])]
        out[side] = round(sum(diffs) / 2, 4)
        changes += diffs + diffs_reach
    sc = {s["name"]: s["p"] for s in base["scenarios"]}
    scen = [abs(s["p"] - sc.get(s["name"], s["p"])) for s in claude["scenarios"]]
    out["scenarios"] = round(sum(scen) / 2, 4)
    changes += scen
    out["largest"] = round(max(changes), 4) if changes else 0.0
    out["moved"] = any(c > 0.005 for c in changes)
    return out


def score_case(folder, ticker, horizon, data, day, realised, today, paths=bt.DEFAULT_PATHS):
    """Judge the three versions of one case. -> a record, or one with `skipped`."""
    record = {"ticker": ticker, "as_of": day.isoformat()}
    run = read_json(folder / "run.json")
    record.update(model=run.get("model"), cost=run.get("cost"))
    if run.get("status") != "ok":
        return {**record, "skipped": "step 2 failed"}
    why = model_ok(run.get("model"), day)
    if why:
        return {**record, "skipped": "look-ahead: " + why}
    d = read_json(folder / "input.json")
    claude = read_json(folder / f"{STEM}_projection.json")
    if date.fromisoformat(claude["horizon"]["last"]) >= today:
        return {**record, "skipped": "horizon not ended yet"}
    loaded = rz.unpack(d)
    zones = zones_of(claude)
    if not zones["supports"] or not zones["resistances"]:
        return {**record, "skipped": "Claude returned no zone on one side"}
    base = bt.projection(d, zones, rz.baseline(loaded, zones, paths))
    versions = {}
    try:
        versions["claude"] = rz.outcome(d, claude, realised, today)
        versions["baseline"] = rz.outcome(d, base, realised, today)
    except ValueError as reason:
        return {**record, "skipped": str(reason)}
    if versions["claude"]["closes"]["counted"] < versions["claude"]["closes"]["scheduled"] / 2:
        return {**record, "skipped": "fewer than half of the scheduled candles exist"}
    mech = bt.run_case(ticker, horizon, data, day, realised, today, paths)
    if "skipped" not in mech:
        versions["mechanical"] = mech
    record.update(price=d["now"]["price"], last=claude["horizon"]["last"], zones=zones,
                  mechanical_zones=mech.get("zones"), moves=moves(claude, base),
                  scenario=versions["claude"]["scenario"],
                  brier={name: {k: v[0] for k, v in case_brier(v).items()} for name, v in versions.items()},
                  forecasts={name: [[k, p, bool(y)] for k, p, y in sp.forecasts(v)] for name, v in versions.items()})
    return record


def bootstrap(diffs, draws=BOOTSTRAP, seed=0):
    """Mean of the per-case differences and a 95% percentile interval, resampling cases."""
    x = np.asarray(diffs, float)
    if len(x) < 2:
        return {"mean": float(x.mean()) if len(x) else None, "low": None, "high": None}
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), (draws, len(x)))].mean(axis=1)
    return {"mean": round(float(x.mean()), 5), "low": round(float(np.percentile(means, 2.5)), 5),
            "high": round(float(np.percentile(means, 97.5)), 5)}


def pooled(scored, version, kind):
    pairs = [(p, y) for r in scored for k, p, y in r["forecasts"].get(version, []) if k == kind]
    return {"n": len(pairs), "brier": round(sp.brier(pairs), 4) if pairs else None,
            "base_rate": round(sum(y for _, y in pairs) / len(pairs), 3) if pairs else None}


def paired(scored, a, b, kind):
    """Per case, Brier of version a minus version b on one kind; cases that have both."""
    diffs = [r["brier"][a][kind] - r["brier"][b][kind] for r in scored
             if kind in r["brier"].get(a, {}) and kind in r["brier"].get(b, {})]
    out = {"cases": len(diffs), **bootstrap(diffs)}
    out["a_better"] = sum(1 for x in diffs if x < -1e-12)
    out["b_better"] = sum(1 for x in diffs if x > 1e-12)
    return out


def summarise(records):
    scored = [r for r in records if "skipped" not in r]
    skipped = {}
    for r in records:
        if "skipped" in r:
            reason = r["skipped"].split(":")[0]
            skipped[reason] = skipped.get(reason, 0) + 1
    out = {"cases": len(scored), "skipped": skipped,
           "tickers": sorted({r["ticker"] for r in scored}),
           "as_of_dates": sorted({r["as_of"] for r in scored}),
           "brier": {v: {k: pooled(scored, v, k) for k in KINDS} for v in VERSIONS},
           "paired": {}, "moves": {}}
    for label, a, b in (("claude_minus_baseline", "claude", "baseline"),
                        ("claude_minus_mechanical", "claude", "mechanical"),
                        ("baseline_minus_mechanical", "baseline", "mechanical")):
        out["paired"][label] = {k: paired(scored, a, b, k) for k in KINDS}
    if scored:
        m = [r["moves"] for r in scored]
        side = [x[s] for x in m for s in ("supports", "resistances")]
        out["moves"] = {"cases_moved": sum(1 for x in m if x["moved"]),
                        "mean_moved_per_side": round(float(np.mean(side)), 4),
                        "max_moved_per_side": round(float(np.max(side)), 4),
                        "mean_moved_scenarios": round(float(np.mean([x["scenarios"] for x in m])), 4),
                        "largest_single_change": round(float(max(x["largest"] for x in m)), 4)}
    return out


def ledger(records):
    known = [r["cost"] for r in records if isinstance(r.get("cost"), (int, float))]
    models = sorted({r.get("model") or "unreported" for r in records})
    return {"runs": len(records), "ok": sum(1 for r in records if r.get("status") == "ok"),
            "failed": sum(1 for r in records if r.get("status") != "ok"),
            "spent_usd": round(sum(known), 6), "cost_unknown": len(records) - len(known),
            "models": models}


# ---- report ------------------------------------------------------------------

def fmt(x, digits=3):
    return "-" if x is None else f"{x:.{digits}f}"


def render(report):
    s, m, money = report["settings"], report["summary"], report["ledger"]
    lines = [f"VALUE TEST: Claude's adjusted probabilities against the baseline, horizon {s['horizon']}",
             f"  model {s['model']} (ran: {', '.join(money['models']) or '-'}); training cutoff "
             f"{s['cutoff']} ({CUTOFF_SOURCE}); as-of dates after it only",
             f"  {money['runs']} run(s) of step 2: {money['ok']} ok, {money['failed']} failed; spent "
             f"${money['spent_usd']:.2f} of ${s['max_cost']:.2f}"
             + (f", {money['cost_unknown']} run(s) of unknown cost" if money["cost_unknown"] else ""),
             f"  {m['cases']} case(s) scored, {len(m['tickers'])} ticker(s), {len(m['as_of_dates'])} as-of date(s)"
             + ("; skipped: " + ", ".join(f"{n} {why}" for why, n in m["skipped"].items()) if m["skipped"] else "")]
    if s.get("unrun"):
        lines.append(f"  {s['unrun']} planned case(s) not run: the budget would have been exceeded")
    for text in CAVEATS:
        lines += textwrap.wrap(text, 100, initial_indent="  ", subsequent_indent="    ")
    lines += ["", "Brier score (lower is better), pooled over forecasts",
              f"  {'kind':<9} {'n':>5} {'claude':>8} {'baseline':>9} {'mechanical':>11}  (baseline = same zones, unadjusted)"]
    for k in KINDS:
        b = {v: m["brier"][v][k] for v in VERSIONS}
        lines.append(f"  {k:<9} {b['claude']['n']:>5} {fmt(b['claude']['brier']):>8} "
                     f"{fmt(b['baseline']['brier']):>9} {fmt(b['mechanical']['brier']):>11}")
    titles = {"claude_minus_baseline": "Claude minus the baseline on the same zones (the adjustment alone)",
              "claude_minus_mechanical": "Claude minus the mechanical zones' baseline (zones and adjustment)",
              "baseline_minus_mechanical": "Baseline on Claude's zones minus the mechanical zones' baseline (zones alone)"}
    for label, title in titles.items():
        lines += ["", title + ": mean per-case Brier difference, 95% bootstrap interval over cases",
                  f"  {'kind':<9} {'cases':>5} {'mean':>9} {'interval':>22} {'first better':>13} {'second better':>14}"]
        for k in KINDS:
            p = m["paired"][label][k]
            interval = f"[{fmt(p['low'], 4)}, {fmt(p['high'], 4)}]"
            lines.append(f"  {k:<9} {p['cases']:>5} {fmt(p['mean'], 4):>9} {interval:>22} "
                         f"{p['a_better']:>13} {p['b_better']:>14}")
    if m["moves"]:
        mv = m["moves"]
        lines += ["", "How much Claude moved the baseline of its own zones",
                  f"  cases with any change: {mv['cases_moved']} of {m['cases']}; probability moved per side: "
                  f"mean {mv['mean_moved_per_side']:.3f}, max {mv['max_moved_per_side']:.3f}; scenarios: mean "
                  f"{mv['mean_moved_scenarios']:.3f}; largest single change {mv['largest_single_change']:.2f}"]
    lines += ["", "A negative difference means the first version scored better. Not advice."]
    return "\n".join(lines)


# ---- the whole run -----------------------------------------------------------

def run(args, today=None):
    out = Path(args.out).resolve()   # step 2 runs in the case folder: no relative path may reach it
    out.mkdir(parents=True, exist_ok=True)
    settings_path = out / "settings.json"
    if args.tickers:
        settings = {"tickers": [t.upper() for t in args.tickers], "horizon": args.horizon,
                    "start": args.start.isoformat(), "end": args.end.isoformat(), "step": args.step,
                    "model": args.model, "cutoff": args.cutoff, "max_cost": args.max_cost,
                    "paths": args.paths}
        if settings_path.is_file():   # a resumed folder keeps its earlier tickers and dates too
            before = read_json(settings_path)
            settings["tickers"] = sorted(set(before.get("tickers", [])) | set(settings["tickers"]))
        write_json(settings_path, settings)
    elif settings_path.is_file():
        settings = {**read_json(settings_path), "max_cost": args.max_cost}
    else:
        raise ValueTestError(f"no tickers given and no {settings_path} to resume from")

    horizon, start, end = settings["horizon"], date.fromisoformat(settings["start"]), date.fromisoformat(settings["end"])
    if not settings["cutoff"]:
        raise ValueTestError(f"no known training cutoff for model {settings['model']}: pass --cutoff YYYY-MM "
                             f"from {CUTOFF_SOURCE}")
    if start <= month_end(settings["cutoff"]):
        raise ValueTestError(f"--start {start} is not after the training cutoff {settings['cutoff']}: "
                             "the model may have seen what followed")

    datas, failed = {}, {}
    for ticker in settings["tickers"]:
        try:
            datas[ticker] = load_data(out, ticker, start, horizon, args.quiet)
        except bt.BacktestError as reason:
            failed[ticker] = str(reason)
            say(f"{ticker}: {reason}", args.quiet)
    tickers = [t for t in settings["tickers"] if t in datas]
    todo, skipped = [], []
    for ticker, day in plan(tickers, datas, start, end, settings["step"]):
        folder, why = prepare(out, ticker, horizon, datas[ticker], day)
        if folder:
            todo.append((ticker, day, folder))
        else:
            skipped.append({"ticker": ticker, "as_of": day.isoformat(), "skipped": why})

    if not args.report_only:
        run_cases(out, [f for _, _, f in todo], settings["model"], settings["max_cost"],
                             args.jobs, args.timeout, args.quiet)

    records = list(skipped)
    for ticker, day, folder in todo:
        if not (folder / "run.json").is_file():
            continue
        data = datas[ticker]
        record = score_case(folder, ticker, horizon, data, day, realised_rows(data, horizon),
                            today or sp.today_in(data["tz"]), settings["paths"])
        write_json(folder / "scores.json", record)
        records.append(record)
    runs = run_records(out)
    report = {"settings": {**settings, "unrun": sum(1 for *_, f in todo if not (f / "run.json").is_file()),
                           "failed_downloads": failed, "cutoff_source": CUTOFF_SOURCE},
              "ledger": ledger(runs), "summary": summarise(records), "caveats": CAVEATS}
    write_json(out / "summary.json", report)
    text = render(report)
    (out / "report.txt").write_text(text + "\n", encoding="utf-8")
    return report, text


def arguments():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tickers", nargs="*", metavar="TICKER",
                        help="Yahoo symbols; none to resume or report the folder's own")
    parser.add_argument("--out", required=True, metavar="DIR",
                        help="folder for inputs, projections, baselines, scores and the report; "
                             "runs already in it are not paid for again")
    parser.add_argument("--horizon", choices=bt.HORIZONS, default=DEFAULT_HORIZON)
    parser.add_argument("--start", type=date.fromisoformat, help="first as-of date (default: the "
                        "first day after the model's training cutoff)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="last as-of date (default: today minus the horizon and a week)")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP, metavar="N",
                        help=f"trading days between two as-of dates (default {DEFAULT_STEP})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model for step 2 (default {DEFAULT_MODEL})")
    parser.add_argument("--cutoff", metavar="YYYY-MM",
                        help="the model's training data cutoff (default: from TRAINING_CUTOFF)")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST, metavar="USD",
                        help=f"total Claude Code cost allowed in --out, earlier runs included "
                             f"(default {DEFAULT_MAX_COST:.2f})")
    parser.add_argument("--jobs", type=int, default=1, help="runs of step 2 at the same time (default 1)")
    parser.add_argument("--timeout", type=int, default=TIMEOUT, help=f"seconds per run (default {TIMEOUT})")
    parser.add_argument("--paths", type=int, default=bt.DEFAULT_PATHS,
                        help=f"simulated paths of the recomputed baselines (default {bt.DEFAULT_PATHS:,})")
    parser.add_argument("--report-only", action="store_true", help="start no run; score what is in --out")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="no progress lines")
    return parser


def main(argv=None, today=None):
    parser = arguments()
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.step < 1 or args.max_cost < 0:
        parser.error("--jobs and --step must be at least 1, --max-cost not negative")
    args.cutoff = args.cutoff or default_cutoff(args.model)
    if args.cutoff and not re.fullmatch(r"\d{4}-\d{2}", args.cutoff):
        parser.error("--cutoff must be YYYY-MM")
    if args.cutoff:
        args.start = args.start or month_end(args.cutoff) + timedelta(days=1)
    args.end = args.end or (today or date.today()) - timedelta(days=rz.HORIZON_DAYS[args.horizon] + 7)
    try:
        report, text = run(args, today)
    except ValueTestError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else text)
    return 0 if report["summary"]["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
