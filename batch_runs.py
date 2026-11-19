#!/usr/bin/env python3
"""Run the pipeline over a watchlist: `zones batch FILE|TICKER... [pipeline options]`.

    zones batch watchlist.txt
    zones batch watchlist.txt NVDA AAPL --research none --jobs 3 --max-cost 5
    zones batch --retry-failed runs/batch/2026-11-19T153012 --research none

Each ticker is one ordinary `zones run TICKER [options]` in a process of its own,
so a ticker that fails (Yahoo, Claude Code, rz.py check, the chart) never stops
the others, and each run folder ends up exactly where a single run puts it.

A watchlist file holds one ticker per line, optionally followed by a horizon
(`PETR4.SA 1-2w`); `#` starts a comment and blank lines are skipped. A word that
names an existing file is a watchlist, anything else is a ticker. Files and
tickers come first, before the options. Every option this command does not
declare goes to each run as if typed there (`--horizon`, `--research`,
`--model`, `--timeout` ...), and the config files apply as they do to one run.
A horizon written on a watchlist line wins over `--horizon` for that ticker.

While the batch runs, one line per ticker goes to standard error when it starts
and when it ends (status, duration, cost). Everything a run prints goes to
its own log, logs/TICKER.log in the batch folder, RUNS_DIR/batch/TIME/, which
also gets summary.csv (one row per ticker). A short table follows at the end.

--max-cost USD stops starting new runs once the costs Claude Code reported add up
to the cap; the runs already going are finished, and the rest are listed as
skipped. Only reported costs count: a run whose Claude Code call reported no
cost (a timeout, for one) is shown as unknown, never estimated.

Exit code: 0 when every ticker succeeded, 1 when any failed or was skipped,
130 when interrupted (the summary is written all the same).

The result is analysis for study, not investment advice.
"""
import argparse
import csv
import math
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import run_pipeline as rp

PIPELINE = rp.ROOT / "run_pipeline.py"
DEFAULT_JOBS = 2     # runs wait on Claude Code, not on this machine; two halve the wall time while
                     # a --max-cost cap is overshot by at most one run, and Yahoo sees no burst
POLL = 0.2           # seconds between looks at the running processes
HEARTBEAT = 60       # seconds without a line before "still running: ..."
BATCHES = "batch"    # RUNS_DIR/batch/TIME/; TIME has no _HORIZON, so `zones runs` skips it
HORIZONS = rp.SETTINGS["horizon"].choices
TICKER = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,31}$")
LIST_SUFFIXES = (".txt", ".csv", ".lst", ".list", ".tickers")
# Options that name one run's files: in a batch every run would write the same file.
PER_RUN = ("--input", "--projection", "--out-dir", "--name", "--chart",
           "--show-config", "--init-config", "-o", "--output")
COLUMNS = ["ticker", "horizon", "status", "run_folder", "as_of", "price", "currency",
           "support_from", "support_to", "support_reach", "support_turn",
           "resistance_from", "resistance_to", "resistance_reach", "resistance_turn",
           "top_scenario", "top_scenario_p", "confidence", "cost_usd", "cost_note",
           "duration_s", "error", "log"]

# What a run prints, as run_pipeline.py prints it (tests/test_batch_runs.py runs the real
# pipeline in a child to pin these): the header of each step that starts Claude Code, the
# line summary() writes when a call ends, `Error: ...` and the file paths on stdout.
CLAUDE_START = re.compile(r"^\[\d+/\d+\] (?:Projection|Trade setups): Claude Code (?:runs|writes)")
CLAUDE_END = re.compile(r"^\s+(?:done in|stopped after) \d+:\d{2}\b(.*)$")
COST = re.compile(r"\$(\d+(?:\.\d+)?)\s*$")
ERROR = re.compile(r"^Error: (.*)$")


class BatchError(Exception):
    """A mistake on the command line or in a watchlist: one line, nothing has run."""


# ---- what to run ---------------------------------------------------------------

def ticker_word(word, where):
    if not TICKER.match(word):
        raise BatchError(f"{where}: '{word}' does not look like a ticker")
    return word


def read_watchlist(path):
    """[(ticker, horizon or None)] from a file: `TICKER [HORIZON]` per line, # comments."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise BatchError(f"Cannot read the watchlist {path}: {error}")
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        words = line.split("#", 1)[0].replace(",", " ").split()
        if not words:
            continue
        where = f"{path}:{number}"
        if len(words) > 2:
            raise BatchError(f"{where}: expected 'TICKER' or 'TICKER HORIZON', got '{line.strip()}'")
        horizon = words[1] if len(words) == 2 else None
        if horizon is not None and horizon not in HORIZONS:
            raise BatchError(f"{where}: horizon '{horizon}' is not one of {', '.join(HORIZONS)}")
        found.append((ticker_word(words[0], where), horizon))
    return found


def gather(items):
    """[(ticker, horizon)] from the words given: files are read, anything else is a ticker.
    A ticker given twice with the same horizon runs once (letter case does not matter)."""
    found = []
    for item in items:
        path = Path(item)
        if path.is_file():
            found += read_watchlist(path)
        elif os.sep in item or "/" in item or item.lower().endswith(LIST_SUFFIXES):
            raise BatchError(f"Watchlist not found: {item}")
        else:
            found.append((ticker_word(item, "command line"), None))
    unique, seen = [], set()
    for ticker, horizon in found:
        key = (ticker.upper(), horizon)
        if key not in seen:
            seen.add(key)
            unique.append((ticker, horizon))
    return unique


def retry_list(folder):
    """[(ticker, horizon)] of the rows of an earlier batch that did not succeed."""
    path = Path(folder) / "summary.csv"
    if Path(folder).is_file() and Path(folder).name == "summary.csv":
        path = Path(folder)
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as error:
        raise BatchError(f"Cannot read {path}: {error}")
    if rows and not {"ticker", "horizon", "status"} <= set(rows[0]):
        raise BatchError(f"{path} is not a batch summary (no ticker, horizon and status columns)")
    return [(row["ticker"], row["horizon"] or None) for row in rows if row["status"] != "ok"]


# ---- the command line ----------------------------------------------------------

def parse(argv):
    """(args, items, options): the batch's own options, the files and tickers (first),
    and every other option, kept in order for each run."""
    parser = argparse.ArgumentParser(
        prog="zones batch", usage="%(prog)s FILE|TICKER... [options] [pipeline options]",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False)
    parser.add_argument("--jobs", "-j", type=int, default=DEFAULT_JOBS, metavar="N",
                        help=f"runs at the same time (default: {DEFAULT_JOBS})")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="start no new run once the reported costs reach this many dollars")
    parser.add_argument("--retry-failed", metavar="BATCH_FOLDER",
                        help="run again the tickers that failed or were skipped in that batch "
                             "(its summary.csv), each with the horizon its row shows")
    parser.add_argument("--runs-dir", metavar="DIR",
                        help="where the run folders and the batch folder go (passed on to each run)")
    argv = list(argv)
    split = next((i for i, word in enumerate(argv) if word.startswith("-")), len(argv))
    items, rest = argv[:split], argv[split:]
    args, options = parser.parse_known_args(rest)
    if args.jobs < 1:
        parser.error("--jobs must be 1 or more")
    if args.max_cost is not None and not 0 < args.max_cost < math.inf:
        parser.error("--max-cost must be a number of dollars above 0")
    for word in options:
        head = word.split("=", 1)[0]
        if head in PER_RUN:
            parser.error(f"{head} names one run's files and cannot be used in a batch")
    if not items and not args.retry_failed:
        parser.error("give a watchlist file or tickers first (or --retry-failed BATCH_FOLDER)")
    if args.runs_dir:
        options += ["--runs-dir", args.runs_dir]
    if not rp.typed("--open", options)[0] and "--no-open" not in options:
        options.append("--no-open")    # a config file's `open = true` would open one viewer per ticker
    return args, items, options


def runs_dir(args, options):
    """The runs folder a run started with these options uses, as run_pipeline.py settles it."""
    if args.runs_dir:
        return Path(args.runs_dir)
    if "--no-config" in options:
        return Path(rp.RUNS)
    rp.command_config()
    return rp.runs_home


def batch_folder(base):
    """A new BASE/batch/TIME/, never an existing one."""
    first = Path(base) / BATCHES / datetime.now().strftime("%Y-%m-%dT%H%M%S")
    target, n = first, 1
    while True:
        try:
            (target / "logs").mkdir(parents=True)
            return target
        except FileExistsError:
            n += 1
            target = first.with_name(f"{first.name}-{n}")


# ---- one run ---------------------------------------------------------------------

class Job:
    def __init__(self, number, ticker, horizon, log):
        self.number, self.ticker, self.horizon, self.log = number, ticker, horizon, log
        self.status, self.error, self.folder, self.facts = "pending", "", None, {}
        self.process = self.handle = self.started = self.seconds = None
        self.cost, self.cost_note, self.before = None, "", set()

    @property
    def label(self):
        return self.ticker + (f" {self.horizon}" if self.horizon else "")


def child_command(ticker, horizon, options):
    """`zones run TICKER [options]`: the pipeline itself, in a process of its own."""
    command = [sys.executable, str(PIPELINE), "run", ticker, *options]
    if horizon:
        command += ["--horizon", horizon]   # after the options: the watchlist line wins
    return command


def folders_of(base, ticker):
    return {path for top in rp.ticker_folders(base, ticker) for path in top.iterdir() if path.is_dir()}


def start(job, options, base):
    job.before = folders_of(base, job.ticker)
    job.handle = open(job.log, "w", encoding="utf-8")
    job.started, job.status = time.monotonic(), "running"
    env = dict(os.environ, PYTHONUNBUFFERED="1")   # the log keeps stdout and stderr in order
    try:
        job.process = subprocess.Popen(child_command(job.ticker, job.horizon, options),
                                       stdin=subprocess.DEVNULL, stdout=job.handle,
                                       stderr=subprocess.STDOUT, env=env)
    except OSError as error:
        job.handle.close()
        job.process = None
        job.status, job.error, job.seconds = "failed", f"could not start the run: {error}", 0.0
        job.cost, job.cost_note = 0.0, "no Claude Code call"


def cost_of(text):
    """(dollars or None, note) from what a run printed: the sum of the costs Claude
    Code reported, None when a call reported none (or ended without its line)."""
    calls = sum(1 for line in text.splitlines() if CLAUDE_START.match(line))
    ends = [found.group(1) for line in text.splitlines() if (found := CLAUDE_END.match(line))]
    reported = [float(found.group(1)) for rest in ends if (found := COST.search(rest))]
    missing = max(calls, len(ends)) - len(reported)
    if not calls and not ends:
        return 0.0, "no Claude Code call"
    if missing:
        note = (f"unknown: {missing} Claude Code call" + ("s" if missing > 1 else "")
                + " reported no cost (the run may have stopped mid-call)")
        return (round(sum(reported), 4) if reported else None), (
            "at least this; " + note if reported else note)
    return round(sum(reported), 4), ""


def error_of(text, code):
    """The run's own `Error: ...` line, with the line after it when it ends in ':'."""
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        found = ERROR.match(lines[index])
        if found:
            message = found.group(1).strip()
            following = [line.strip() for line in lines[index + 1:] if line.strip()]
            if message.endswith(":") and following:
                message += " " + following[0]
            elif message.endswith(":"):     # "... Last output:" with nothing after it
                message = re.sub(r"\s*Last output:$", "", message).rstrip(":")
            return rp.printable(message, 240)
    return f"exit code {code}" + (" (interrupted)" if code in (130, -2) else "")


def output_folder(text):
    """The run folder, from the `input:` path the run prints when it succeeds."""
    for line in text.splitlines():
        if line.startswith("input:"):
            return Path(line.split(":", 1)[1].strip()).parent
    return None


def nearest(zones, price):
    """The zone closest to the price, among those with both edges (not the open tails)."""
    real = [zone for zone in zones or [] if isinstance(zone, dict)
            and isinstance(zone.get("zone"), list) and len(zone["zone"]) == 2
            and all(rp.is_number(edge) for edge in zone["zone"])]
    if not real or not rp.is_number(price):
        return None
    return min(real, key=lambda zone: max(0.0, min(zone["zone"]) - price, price - max(zone["zone"])))


def facts(folder):
    """The CSV's numbers, read from the run folder's projection (empty without one)."""
    try:
        proj = rp.load_json(Path(folder) / "projection.json", "Projection")
    except (rp.PipelineError, TypeError):
        return {}
    got = {}
    try:
        price = proj.get("as_of", {}).get("price")
        got.update(as_of=proj["as_of"].get("time", ""), price=price,
                   currency=proj.get("asset", {}).get("currency", ""),
                   horizon=proj.get("horizon", {}).get("label", ""),
                   confidence=proj.get("confidence", {}).get("level", ""))
        for side in ("support", "resistance"):
            zone = nearest(proj.get(side + "s"), price)
            if zone:
                low, high = sorted(zone["zone"])
                got.update({f"{side}_from": low, f"{side}_to": high,
                            f"{side}_reach": zone.get("reach"), f"{side}_turn": zone.get("turn")})
        scenarios = [s for s in proj.get("scenarios") or [] if isinstance(s, dict) and rp.is_number(s.get("p"))]
        if scenarios:
            top = max(scenarios, key=lambda s: s["p"])
            got.update(top_scenario=top.get("name", ""), top_scenario_p=top["p"])
    except (AttributeError, TypeError, KeyError):
        pass    # a malformed projection (it failed its check): keep what was read
    return got


def finish(job, base):
    job.seconds = time.monotonic() - job.started
    code = job.process.returncode
    job.handle.close()
    text = job.log.read_text(encoding="utf-8", errors="replace")
    job.cost, job.cost_note = cost_of(text)
    job.folder = output_folder(text)
    if job.folder is None:   # a failed run prints no paths: the folder it made, if one
        new = sorted(folders_of(base, job.ticker) - job.before)
        if job.horizon:
            new = [path for path in new if rp.folder_horizon(path) == job.horizon]
        job.folder = new[0] if len(new) == 1 else None
    job.facts = facts(job.folder) if job.folder else {}
    if job.folder and not job.facts.get("horizon"):
        job.facts["horizon"] = rp.folder_horizon(job.folder) or ""
    if code == 0:
        job.status = "ok"
    else:
        job.status, job.error = "failed", error_of(text, code)


# ---- the batch -------------------------------------------------------------------

def money(cost):
    return "cost ?" if cost is None else f"${cost:.2f}"


class Batch:
    def __init__(self, jobs, options, folder, base, workers=DEFAULT_JOBS, max_cost=None):
        self.jobs, self.options, self.folder, self.base = jobs, options, folder, base
        self.workers, self.max_cost = workers, max_cost
        self.width = max(len(job.label) for job in jobs)
        self.spent, self.capped, self.last_line = 0.0, False, time.monotonic()

    def line(self, job, text):
        self.last_line = time.monotonic()
        rp.say(f"  [{job.number}/{len(self.jobs)}] {job.label:<{self.width}}  {text}")

    def ended(self, job):
        where = str(job.folder) if job.folder else ""
        detail = where if job.status == "ok" else job.error
        self.line(job, f"{job.status:<7} {rp.clock(job.seconds):>5}  {money(job.cost):>8}  {detail}")
        if job.cost is not None:
            self.spent += job.cost
        elif job.cost_note.startswith("unknown") and self.max_cost is not None:
            rp.say(f"      the cost of {job.label} is unknown, so the cap does not count it")

    def run(self):
        pending, running = deque(self.jobs), []
        try:
            while pending or running:
                while pending and len(running) < self.workers and not self.capped:
                    job = pending.popleft()
                    start(job, self.options, self.base)
                    if job.process is None:
                        self.ended(job)
                        continue
                    running.append(job)
                    self.line(job, "started")
                time.sleep(POLL)
                for job in [job for job in running if job.process.poll() is not None]:
                    running.remove(job)
                    finish(job, self.base)
                    self.ended(job)
                    self.write()
                    if self.max_cost is not None and self.spent >= self.max_cost and not self.capped:
                        self.capped = True
                        if pending:
                            rp.say(f"  Cost cap reached: ${self.spent:.2f} reported of ${self.max_cost:.2f}. "
                                   f"No new runs; {len(running)} still running, {len(pending)} skipped.")
                if self.capped:
                    while pending:
                        skip(pending.popleft(), f"cost cap: ${self.spent:.2f} reported of ${self.max_cost:.2f}")
                if running and time.monotonic() - self.last_line >= HEARTBEAT:
                    self.last_line = time.monotonic()
                    rp.say("      still running: " + ", ".join(
                        f"{job.label} {rp.clock(time.monotonic() - job.started)}" for job in running))
        except KeyboardInterrupt:
            rp.say("Interrupted: stopping the runs that are going.")
            for job in running:   # they got the same Ctrl-C; give them a moment to say so
                try:
                    job.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    job.process.kill()
                    job.process.wait()
                finish(job, self.base)
            for job in pending:
                skip(job, "interrupted")
            self.write()
            raise
        self.write()

    def write(self):
        write_summary(self.folder / "summary.csv", self.jobs)


def skip(job, why):
    job.status, job.error = "skipped", why


def number(value, digits=None):
    if not rp.is_number(value):
        return "" if value is None else str(value)
    return f"{value:.{digits}f}" if digits is not None else f"{value:g}"


def row(job):
    got = job.facts
    cells = {key: got.get(key, "") for key in COLUMNS}
    cells.update(
        ticker=job.ticker, horizon=job.horizon or got.get("horizon", ""), status=job.status,
        run_folder=str(job.folder or ""), price=number(got.get("price")),
        cost_usd="" if job.status in ("pending", "skipped") else number(job.cost, 2),
        cost_note=job.cost_note if job.status not in ("pending", "skipped") else "",
        duration_s="" if job.seconds is None else f"{job.seconds:.1f}",
        error=job.error, log=str(job.log) if job.status not in ("pending", "skipped") else "")
    for key in ("support_from", "support_to", "resistance_from", "resistance_to",
                "support_reach", "support_turn", "resistance_reach", "resistance_turn", "top_scenario_p"):
        cells[key] = number(got.get(key))
    return cells


def write_summary(path, jobs):
    """summary.csv, rewritten after every run so a batch cut short still has one."""
    draft = path.with_suffix(".csv.tmp")
    with open(draft, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for job in jobs:
            writer.writerow(row(job))
    os.replace(draft, path)


def zone_text(cells, side):
    if cells[f"{side}_from"] == "":
        return "-"
    odds = "/".join(f"{float(cells[f'{side}_{key}']):.2f}" if cells[f"{side}_{key}"] else "?"
                    for key in ("reach", "turn"))
    return f"{cells[f'{side}_from']}-{cells[f'{side}_to']} ({odds})"


def table(jobs):
    """The concise table printed at the end: one line per ticker."""
    head = ["ticker", "status", "price", "support (reach/turn)", "resistance (reach/turn)",
            "top scenario", "conf.", "cost", "time"]
    lines = []
    for job in jobs:
        cells = row(job)
        scenario = (f"{cells['top_scenario']} {cells['top_scenario_p']}" if cells["top_scenario"] else "-")
        cost = ("-" if job.status == "skipped" else
                "?" if job.cost is None else f"${job.cost:.2f}" + ("+" if job.cost_note.startswith("at least") else ""))
        lines.append([job.label, job.status, cells["price"] or "-", zone_text(cells, "support"),
                      zone_text(cells, "resistance"), scenario, cells["confidence"] or "-", cost,
                      rp.clock(job.seconds) if job.seconds is not None else "-"])
    widths = [max(len(str(line[i])) for line in [head] + lines) for i in range(len(head))]
    render = lambda cells: "  ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip()
    return "\n".join([render(head), render(["-" * width for width in widths])] + [render(line) for line in lines])


def report(jobs, folder):
    """Printed on stdout at the end: the table, the failures, the counts and the cost."""
    print(table(jobs))
    trouble = [job for job in jobs if job.status != "ok"]
    if trouble:
        print()
        for job in trouble:
            where = f"  (log: {job.log})" if job.status == "failed" else ""
            print(f"{job.label}: {job.status}: {job.error}{where}")
    counts = {status: sum(job.status == status for job in jobs) for status in ("ok", "failed", "skipped")}
    known = [job.cost for job in jobs if job.cost is not None]
    unknown = [job.label for job in jobs if job.status in ("ok", "failed") and job.cost_note.startswith(("unknown", "at least"))]
    cost = f"reported cost ${sum(known):.2f}"
    if unknown:
        cost += f", plus an unknown cost for {', '.join(unknown)}"
    print(f"\n{len(jobs)} ticker{'s' if len(jobs) != 1 else ''}: {counts['ok']} ok, {counts['failed']} failed, "
          f"{counts['skipped']} skipped; {cost}.")
    print(f"summary:    {folder / 'summary.csv'}")
    print("Analysis for study, not investment advice.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args, items, options = parse(argv)
    try:
        wanted = gather(items)
        if args.retry_failed:
            again = retry_list(args.retry_failed)
            if not again:
                rp.say(f"Nothing to retry: every ticker in {args.retry_failed} succeeded.")
                if not wanted:
                    return 0
            seen = {(ticker.upper(), horizon) for ticker, horizon in wanted}
            wanted += [pair for pair in again if (pair[0].upper(), pair[1]) not in seen]
        if not wanted:
            raise BatchError("The watchlist holds no tickers.")
        found, prices = rp.typed("--prices", options)
        if found and prices and Path(prices).is_file() and len({t.upper() for t, _ in wanted}) > 1:
            raise BatchError(f"--prices {prices} is one ticker's file; for a batch give a folder "
                             "holding TICKER.csv or TICKER/ for each ticker")
        if not PIPELINE.is_file():
            raise BatchError(f"{PIPELINE} not found: run this from the repository checkout.")
        base = runs_dir(args, options)
        folder = batch_folder(base)
    except (BatchError, rp.PipelineError) as error:
        rp.say(f"Error: {error}")
        return 1
    used = set()
    jobs = []
    for number, (ticker, horizon) in enumerate(wanted, 1):
        name = rp.ticker_folder(ticker) + (f"_{horizon}" if horizon else "")
        while name.upper() in used:     # PETR4.SA and petr4.sa on a case-blind disk
            name += "_"
        used.add(name.upper())
        jobs.append(Job(number, ticker, horizon, folder / "logs" / f"{name}.log"))
    workers = min(args.jobs, len(jobs))
    cap = f", cost cap ${args.max_cost:.2f}" if args.max_cost is not None else ""
    rp.say(f"Batch of {len(jobs)} ticker{'s' if len(jobs) != 1 else ''}, {workers} at a time{cap}: {folder}")
    batch = Batch(jobs, options, folder, base, workers, args.max_cost)
    try:
        batch.run()
    except KeyboardInterrupt:
        report(jobs, folder)
        return 130
    report(jobs, folder)
    return 0 if all(job.status == "ok" for job in jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
