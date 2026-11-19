#!/usr/bin/env python3
"""Ticker to chart and ranked trade setups in one command: build_prompt.py, the skill
(headless Claude Code), rz.py check, plot_projection.py, then trade_entries.py.

    zones PETR4.SA
    zones NVDA --horizon 1-2w --research none --lang en --open
    zones --input FOLDER/input.json                                      # project and plot again
    zones --input FOLDER/input.json --projection FOLDER/projection.json  # plot again
    zones PETR4.SA --no-entries                                          # stop at the chart

After a run, the same command looks at what is there (`zones COMMAND --help`
for each):

    zones list [TICKER]             the saved projections, newest last, open or ended
    zones runs [TICKER]             the run folders under runs/ here, newest last
    zones score [TICKER | FOLDER]   saved projections against later prices (hits Yahoo)
    zones plot WHAT [plot options]  draw the chart of a run again
    zones show WHAT                 print the summary of a run
    zones entries WHAT              compute the trade setups of a run again
    zones backtest TICKER...        the baseline probabilities on past dates (hits Yahoo)
    zones batch FILE|TICKER...      one run per ticker of a watchlist, with a summary.csv
    zones cache [info|clear|path]   the local cache of downloaded candles

WHAT is a folder holding input.json and projection.json, or a ticker, meaning
its latest run under runs/ here, else its latest saved projection. A first
argument is a command only when it is exactly one of these lowercase words;
anything else is a ticker (`zones nvda` runs the pipeline for NVDA). For a
ticker spelled like a command write it in capitals (`zones LIST`), or after
`run` or `--` (`zones run list`).

`zones` is this script (`uv run zones` in the repository, or plain `zones` after
`uv tool install --editable .`); `python run_pipeline.py` does the same.

Each run gets a new folder of its own, runs/TICKER/TIME_HORIZON/ under the
current directory (TIME is the input's `now.time`; a second run of the same
input gets _2, and so on), holding input.json, projection.json, chart.png and entries.json,
plus provenance.json: which model, rules and data made the result, and at what cost.
Nothing is overwritten. With --out-dir the files go exactly there instead, named
after the ticker (petr4_input.json, petr4_projection.json, petr4.png, petr4_entries.json,
petr4_provenance.json), and are replaced by the next run.

A projection made by this run is also kept, with its input, under projections/
(`rz.py save`; folder set by $RZ_PROJECTIONS_DIR) unless --no-save is given.
That is the append-only record for scoring projections later; runs/ is only
this command's output and can be deleted at any time.

Options this script does not know (the ticker, --horizon, --research, --event,
--language ...) are passed to build_prompt.py untouched; see
`python build_prompt.py --help`.

Step 2 runs `claude -p` in this repository with a narrow tool allowlist: the
skill, the helper script, and reads and writes inside a throwaway work folder
(plus web search when the input's `research` is not `none`). While it runs,
one line per stage of the skill goes to standard error, then the duration,
turns and cost that Claude Code reports; --quiet leaves only that last line.

--engine mechanical makes the projection without Claude: mechanical.py chooses
the zones by backtest.py's fixed rule and copies the unadjusted rz.py sim
probabilities, in about a second and at no cost. It does no web research, so
the input asks for none (a research option typed or set in a config file gets
one warning). `engine = "mechanical"` in a config file makes it the default.

Step 5 writes three hypothetical setups (entry, stop, target), ranked best
first. trade_entries.py computes them from the projection by fixed rules, so it
makes no Claude Code call. With --entries-by-claude it sends
prompts/trade_entries.md, filled with the two files, to `claude -p` with no
tools at all, so that Claude writes the sentences; every number of that reply is
checked against the computed one. Pass --no-entries to skip the step.

Standing preferences (language, horizon, research, model, theme, where runs/
and the saved history go ...) can live in a TOML file instead of being typed on
every run: `zones --init-config` writes a commented template, `zones
--show-config` prints every setting with where its value comes from, and
--no-config ignores the files for one run. Precedence: command line >
environment variable > ./zones.toml > the user file > built-in default.

The result is analysis for study, not investment advice.
"""
import argparse
import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build_prompt.py"
PLOT = ROOT / "plot_projection.py"
ENTRIES = ROOT / "trade_entries.py"
SHOW = ROOT / "show_run.py"
MECHANICAL = ROOT / "mechanical.py"
ENGINES = ("claude", "mechanical")   # step 2: the skill through `claude -p`, or mechanical.py (no Claude)
SKILL = "reversal-zones-projection"
RZ = ROOT / ".claude" / "skills" / SKILL / "scripts" / "rz.py"
CLAUDE = "claude"
DEFAULT_TIMEOUT = 1800   # seconds; a run with research usually takes a few minutes
HEARTBEAT = 30           # seconds without a progress line before "still running"
TAIL_LINES = 15
WORK_INPUT, WORK_PROJECTION = "input.json", "projection.json"   # names inside the work folder
RUNS = "runs"            # default output: RUNS/TICKER/TIME_HORIZON/ under the current directory


class PipelineError(Exception):
    """A failure the user should see as one short message, without a traceback."""


def say(message):
    print(message, file=sys.stderr, flush=True)


def tail(text, lines=TAIL_LINES):
    kept = [line for line in (text or "").strip().splitlines() if line.strip()][-lines:]
    return "\n".join("    " + line for line in kept)


def stem_for(ticker):
    """PETR4.SA -> petr4, BTC-USD -> btc-usd, ^GSPC -> gspc."""
    stem = re.sub(r"[^a-z0-9_-]", "", str(ticker).lower().split(".")[0])
    if not stem:
        raise PipelineError(f"Cannot derive a file name from ticker '{ticker}'. Pass --name.")
    return stem


def load_json(path, what):
    path = Path(path)
    if not path.is_file():
        raise PipelineError(f"{what} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PipelineError(f"{what} is not valid JSON: {path} ({error})")
    if not isinstance(data, dict):
        raise PipelineError(f"{what} must be a JSON object: {path}")
    return data


def run(command, timeout=None):
    """Run a helper script and capture its output."""
    return subprocess.run([str(part) for part in command], capture_output=True,
                          text=True, timeout=timeout)


# ---- where the files go --------------------------------------------------------

def file_names(stem=None):
    """(input, projection, chart) file names: plain in a run folder, which
    already says which run it is, and prefixed with a stem anywhere else."""
    if stem:
        return f"{stem}_input.json", f"{stem}_projection.json", f"{stem}.png"
    return "input.json", "projection.json", "chart.png"


def run_folder(data, base=RUNS):
    """Make and return a new folder base/TICKER/TIME_HORIZON for this run, named
    the way `rz.py save` names its own: TIME is the input's `now.time` in the
    exchange's local time, and a name already taken gets _2, _3 ... so an
    earlier run is never touched."""
    safe = lambda text: re.sub(r"[^A-Za-z0-9._-]", "_", str(text)).strip(".")
    try:
        ticker, horizon = safe(data["asset"]["ticker"].upper()), safe(data["horizon"])
        stamp = datetime.fromisoformat(data["now"]["time"]).strftime("%Y-%m-%dT%H%M")
    except (KeyError, TypeError, ValueError, AttributeError):
        ticker = horizon = stamp = ""
    if not (ticker and horizon and stamp):
        raise PipelineError("The run folder is named after asset.ticker, now.time and horizon, "
                            "which this input JSON lacks. Pass --out-dir DIR to choose the folder.")
    first = Path(base) / ticker / f"{stamp}_{horizon}"
    target, n = first, 1
    while True:
        try:
            target.mkdir(parents=True)
            return target
        except FileExistsError:
            n += 1
            target = first.with_name(f"{first.name}_{n}")
        except OSError as error:
            raise PipelineError(f"Cannot create the run folder {target}: {error}")


def start_viewer(path):
    """Hand a file to the platform's default application, without waiting for it."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def open_chart(path):
    """--open is a convenience: the run has already succeeded, so a viewer that
    cannot be started is one warning line, never an error."""
    try:
        start_viewer(path)
    except Exception as error:
        say(f"Warning: could not open {path}: {error}")


def show_summary(result, lang=None):
    """Print show_run.py's summary of the finished run on standard output. Like --open
    it is a convenience after a run that has already succeeded: whatever goes wrong
    is one warning line, and the file paths are printed all the same. The script
    writes into a pipe here, so this side tells it whether a terminal is reading
    (colour and width); NO_COLOR reaches it through the environment."""
    command = [sys.executable, SHOW, result["input"], result["projection"]]
    if result["entries"]:
        command.append(result["entries"])
    if lang:
        command += ["--lang", lang]
    try:
        terminal = sys.stdout.isatty()
        if terminal:
            command += ["--color", "--width", shutil.get_terminal_size().columns]
        else:
            command.append("--no-color")
        done = run(command, timeout=60)
        if done.returncode != 0 or not done.stdout.strip():
            raise PipelineError((done.stderr.strip() or "it printed nothing").splitlines()[-1])
        # on a terminal the progress lines end just above: set the summary apart from them
        print(("\n" if terminal else "") + done.stdout.rstrip("\n") + "\n")
    except Exception as error:
        say(f"Warning: no summary of the result ({SHOW.name}): {printable(error)}")


# ---- step 1 -------------------------------------------------------------------

def build_input(passthrough):
    """Run build_prompt.py with the user's own arguments and return what it
    wrote as (bytes, data). It writes into a throwaway folder: where the input
    is kept depends on the ticker and time inside it, and the arguments are
    never parsed here."""
    if any(arg in ("-o", "--output") or arg.startswith("--output=") for arg in passthrough):
        raise PipelineError("Do not pass -o/--output: use --out-dir and --name to place the files.")
    with tempfile.TemporaryDirectory(prefix="rz_input_") as folder:
        draft = Path(folder) / WORK_INPUT
        done = run([sys.executable, BUILD, *passthrough, "-o", draft])
        if done.returncode != 0:
            raise PipelineError("build_prompt.py failed:\n" + tail(done.stderr or done.stdout))
        data = load_json(draft, "Input JSON from build_prompt.py")
        return draft.read_bytes(), data


# ---- step 2 -------------------------------------------------------------------

def claude_prompt(input_path, projection_path, work, python=sys.executable):
    helper = f"{python} {RZ}"
    return (
        f"Use the {SKILL} skill on the input JSON at {input_path}.\n\n"
        "This is a non-interactive pipeline run, so:\n"
        "- Read the input from that path; do not copy it.\n"
        f"- Run the helper only as: {helper} <levels|sim|check> <files>. "
        "One plain command per Bash call: no cd, pipes, redirects or chaining.\n"
        f"- Keep working files such as zones.json in {work} and create them with the Write tool.\n"
        f"- Save the final projection to {projection_path} and make sure the check command "
        "prints OK for that file.\n"
        "- Skip the skill's save step: do not run the helper's save command. The pipeline "
        "keeps the projection itself.\n"
        "- When the input's `research` is not `none`, follow the skill's Research section in "
        "full. Here that means WebSearch to search and WebFetch to open pages: every driver "
        "`source` and every event date or size comes from a page opened with WebFetch in this "
        "run, never from a search summary or from memory. If a search or a page fails or is "
        "denied, go on without it and say so in `confidence.limits`.\n"
        "- The file is the result. Reply with one short line instead of repeating the JSON."
    )


def claude_command(input_path, projection_path, work, research, model=None,
                   python=sys.executable):
    """The headless Claude Code command line. Nothing is bypassed: every tool
    not listed here is unavailable, and anything outside the allow rules is
    denied instead of prompting."""
    tools = ["Skill", "Read", "Write", "Edit", "Bash"]
    allowed = [
        f"Skill({SKILL})",
        f"Read(/{work}/**)",
        f"Write(/{work}/**)",
        f"Edit(/{work}/**)",
        f"Bash({python} {RZ} *)",
    ]
    if research != "none":
        tools += ["WebSearch", "WebFetch"]
        allowed += ["WebSearch", "WebFetch"]
    command = [
        CLAUDE, "-p", claude_prompt(input_path, projection_path, work, python),
        "--output-format", "stream-json", "--verbose",   # one JSON event per line (needs --verbose)
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        "--add-dir", str(work),
        "--tools", ",".join(tools),
        "--allowedTools", *allowed,
    ]
    if model:
        command += ["--model", model]
    return command


def clock(seconds):
    """95.4 -> '1:35'."""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def printable(text, limit=200):
    """Text that came from the model, safe to show on one terminal line."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))[:limit].strip()


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# The skill's workflow, recognised by the tool call that starts each stage:
# (first time, later times). None: said once only.
STAGES = {
    "input": ("reading the input", None),
    "skill": ("reading the skill's instructions", None),
    "levels": ("measuring the candles (rz.py levels)", "measuring the candles again (rz.py levels)"),
    "zones": ("choosing zones", "adjusting the zones"),
    "sim": ("simulating price paths (rz.py sim)", "simulating again (rz.py sim, run {n})"),
    "projection": ("writing the projection", "revising the projection"),
    "check": ("validating (rz.py check)", "validating again (rz.py check, run {n})"),
}
HELPER_CALL = re.compile(r"rz\.py['\"]?\s+(levels|sim|check)\b")
LABEL_WIDTH = 50


class Progress:
    """Turns Claude Code's event stream (one JSON object per line) into a few
    calm lines on stderr: one per stage of the skill, not one per tool call."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self.started = self.last_line = time.monotonic()
        self.seen, self.checks, self.counts = set(), set(), {}
        self.previous, self.searches = None, 0

    def elapsed(self):
        return time.monotonic() - self.started

    def line(self, label):
        self.last_line = time.monotonic()
        if not self.quiet:
            if len(label) > LABEL_WIDTH:
                label = label[:LABEL_WIDTH - 3] + "..."
            say(f"      {label:<{LABEL_WIDTH}} {clock(self.elapsed()):>5}")

    def beat(self):
        """Called regularly while Claude runs: a sign of life after a long silence."""
        if time.monotonic() - self.last_line >= HEARTBEAT:
            self.line("... still running")

    def feed(self, text):
        """One line of the stream. Progress is a courtesy, so anything that is
        not an event this class knows (other event types, other tools, text
        that is not JSON at all) is skipped without a word."""
        try:
            event = json.loads(text)
            if event.get("type") not in ("assistant", "user"):
                return
            for block in event["message"]["content"]:
                if isinstance(block, dict):
                    self.block(block)
        except (ValueError, KeyError, TypeError, AttributeError):
            pass

    def block(self, block):
        kind, ident = block.get("type"), block.get("id")
        if kind == "tool_result":
            if block.get("tool_use_id") in self.checks and result_text(block).strip() != "OK":
                self.line("the check found problems to fix")
            return
        if kind not in ("tool_use", "server_tool_use") or (ident is not None and ident in self.seen):
            return
        self.seen.add(ident)
        name, given = block.get("name"), block.get("input")
        given = given if isinstance(given, dict) else {}
        if name == "WebSearch":     # the query only, never what the search returned
            self.searches += 1
            self.previous = None
            self.line(f"web search {self.searches}: {printable(given.get('query', ''))}")
        elif name == "WebFetch":    # the host only, never the page
            self.previous = None
            host = urllib.parse.urlsplit(str(given.get("url", ""))).hostname
            self.line(f"reading a web page ({printable(host or 'unknown host')})")
        else:
            stage = self.stage(name, given)
            if stage and stage != self.previous:   # three edits in a row are one stage
                self.previous = stage
                count = self.counts[stage] = self.counts.get(stage, 0) + 1
                first, later = STAGES[stage]
                if count == 1:
                    self.line(first)
                elif later:
                    self.line(later.format(n=count))
            if stage == "check":
                self.checks.add(ident)

    @staticmethod
    def stage(name, given):
        if name == "Skill":
            return "skill"
        if name == "Bash":
            found = HELPER_CALL.search(str(given.get("command", "")))
            return found.group(1) if found else None
        file = str(given.get("file_path", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if name == "Read":
            return {WORK_INPUT: "input", "SKILL.md": "skill"}.get(file)
        if name in ("Write", "Edit"):
            return "projection" if file == WORK_PROJECTION else "zones" if "zone" in file.lower() else None
        return None


def result_text(block):
    """The text of a tool_result block, whose content is a string or a list of parts."""
    content = block.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def call_claude(command, timeout, progress=None, cwd=ROOT):
    """The only place that starts Claude Code. Returns (exit code, stdout, stderr).

    Two threads drain the pipes, so neither can fill up and stall Claude. This
    thread takes stdout line by line from a queue, hands each line to
    `progress` and watches the clock itself, so the timeout fires whether the
    stream is silent or busy."""
    progress = progress or Progress()
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise PipelineError(
            f"'{command[0]}' was not found. Step 2 needs Claude Code on PATH "
            "(https://claude.com/claude-code). To skip it, pass --projection FILE.")
    lines, out, err = queue.Queue(), [], []

    def read_stdout():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=lambda: err.append(process.stderr.read()), daemon=True)]
    for reader in readers:
        reader.start()
    streaming, timed_out = True, False
    try:
        while True:
            left = timeout - (time.monotonic() - started)
            if left <= 0:
                timed_out = True
                break
            wait = max(0.01, min(left, 1.0, HEARTBEAT / 4))
            if streaming:
                try:
                    line = lines.get(timeout=wait)
                except queue.Empty:
                    line = ""
                if line is None:
                    streaming = False       # stdout closed: only the exit code is left
                elif line:
                    out.append(line)
                    progress.feed(line)
            else:
                try:
                    process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    pass
            progress.beat()
    finally:                                # also on Ctrl-C: never leave Claude running
        if process.poll() is None:
            process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=5)
        for pipe, reader in zip((process.stdout, process.stderr), readers):
            if not reader.is_alive():
                pipe.close()
    while not lines.empty():
        line = lines.get_nowait()
        if line:
            out.append(line)
    out, err = "".join(out), "".join(err)
    if timed_out:
        raise PipelineError(
            f"Claude Code did not finish within {timeout:g} s (raise it with --timeout). "
            "Last output:\n" + tail(readable(out) or err))
    return process.returncode, out, err


def events(stdout):
    """The JSON objects in Claude's output: one per line (stream-json), or the
    whole text as one object (plain json). Lines that are not JSON come back as str."""
    try:
        whole = json.loads(stdout)
        if isinstance(whole, dict):
            return [whole]
    except ValueError:
        pass
    found = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        found.append(event if isinstance(event, dict) else line)
    return found


def claude_result(stdout):
    """The final result event (cost, turns, is_error, permission_denials), or None."""
    for event in reversed(events(stdout)):
        if isinstance(event, dict) and (event.get("type") == "result" or (
                "type" not in event and ("result" in event or "is_error" in event))):
            return event
    return None


def readable(stdout):
    """What a person can read in the stream: Claude's own sentences and any
    line that is not an event. Used for the tail of an error message."""
    kept = []
    for event in events(stdout):
        if isinstance(event, str):
            kept.append(event[:300])
        elif event.get("type") == "assistant":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    kept.append(str(block.get("text", "")))
    return "\n".join(kept)


def claude_reply(stdout):
    """(final text, failed?) from the result event, or whatever is readable without one."""
    result = claude_result(stdout)
    if result is None:
        return readable(stdout), False
    failed = bool(result.get("is_error"))
    text = str(result.get("result") or "")
    if failed and not text:    # e.g. error_max_turns carries no sentence
        text = " ".join(str(part) for part in [result.get("subtype"), *(result.get("errors") or [])] if part)
    denied = result.get("permission_denials") or []
    if denied:
        names = sorted({str(d.get("tool_name", "?")) for d in denied if isinstance(d, dict)})
        text += f"\n({len(denied)} tool call(s) denied by the allowlist: {', '.join(names)})"
    return text, failed


def summary(result, seconds, failed=False):
    """'done in 2:17 · 8 turns · $0.75', with only the figures Claude Code reported."""
    result = result or {}
    parts = [("stopped after " if failed else "done in ") + clock(seconds)]
    turns, cost = result.get("num_turns"), result.get("total_cost_usd")
    if is_number(turns):
        parts.append(f"{turns} turn" + ("" if turns == 1 else "s"))
    if is_number(cost):
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


def stream_hint(result, err):
    """An older Claude Code refuses the streaming flags before doing any work."""
    if result is None and re.search(r"stream-json|--verbose|unknown option|output-format", err or ""):
        return ("\nStep 2 reads Claude Code's event stream (--output-format stream-json --verbose). "
                "If this version rejects those flags, update Claude Code.")
    return ""


def project(input_path, data, projection_path, timeout, model=None, quiet=False):
    """Run the skill headless in a throwaway folder, then keep only the projection.
    Returns what Claude Code reported about the run (claude_facts())."""
    with tempfile.TemporaryDirectory(prefix="rz_pipeline_") as folder:
        work = Path(folder).resolve()
        work_input, work_projection = work / WORK_INPUT, work / WORK_PROJECTION
        shutil.copyfile(input_path, work_input)
        command = claude_command(work_input, work_projection, work,
                                 data.get("research", "none"), model)
        progress = Progress(quiet)
        code, out, err = call_claude(command, timeout, progress)
        result = claude_result(out)
        reply, failed = claude_reply(out)
        failed = failed or code != 0
        say("      " + summary(result, progress.elapsed(), failed))
        if failed:
            raise PipelineError(f"Claude Code failed (exit {code}). Last output:\n"
                                + tail(reply or err) + stream_hint(result, err))
        if not work_projection.is_file():
            raise PipelineError("Claude Code finished without writing the projection file. "
                                "Last output:\n" + tail(reply or err))
        try:
            load_json(work_projection, "Projection from Claude Code")
        except PipelineError as error:
            raise PipelineError(f"{error}\nLast output:\n{tail(reply)}")
        shutil.copyfile(work_projection, projection_path)
        return claude_facts(out, progress.elapsed(), model)


def mechanical_project(input_path, projection_path):
    """Step 2 with --engine mechanical: mechanical.py writes the projection from the input
    by fixed rules (backtest.py's zone rule, the unadjusted rz.py sim), with no Claude Code
    call. Returns what provenance.json records about it."""
    started = time.monotonic()
    done = run([sys.executable, MECHANICAL, input_path, "-o", projection_path])
    seconds = time.monotonic() - started
    if done.returncode != 0:
        raise PipelineError("mechanical.py failed:\n" + tail(done.stderr or done.stdout))
    load_json(projection_path, "Projection from mechanical.py")
    # not "done in M:SS": batch_runs.py reads that line as the end of a Claude Code call
    say(f"      made in {seconds:.1f} s · no Claude Code call · $0")
    facts = {"engine": MECHANICAL.name, "zone_rule": "backtest.py clustered_zones()", "wall_seconds": round(seconds, 1)}
    method = default_sim_method()
    if method is not None:
        facts["sim_method"] = method
    return facts


def research_warning(research):
    """The one line a research request gets from the mechanical engine, which searches nothing."""
    return (f"Warning: research '{research}' is not done by the mechanical engine (no web access, no "
            "Claude): no drivers, and the events are only the input's. Use --engine claude for research.")


# ---- steps 3 and 4 ------------------------------------------------------------

def check(projection_path):
    done = run([sys.executable, RZ, "check", projection_path])
    printed = done.stdout.strip()
    if done.returncode != 0 or printed != "OK":
        # no violations printed means rz.py itself crashed on a malformed file:
        # its last stderr line names the missing key, the traceback adds nothing
        raise PipelineError(f"{projection_path} failed rz.py check:\n"
                            + (tail(printed, 40) or tail(done.stderr, 1)))


def save(input_path, projection_path):
    """Keep the pair for later scoring (rz.py save). Returns the folder, or None
    with a warning: a projection that cannot be kept is still worth plotting."""
    done = run([sys.executable, RZ, "save", input_path, projection_path])
    printed = done.stdout.strip()
    if done.returncode != 0 or not printed.startswith("saved "):
        say("      Warning: the projection was not kept for later scoring:\n"
            + (tail(printed) or tail(done.stderr, 1)))
        return None
    return printed[len("saved "):]


def plot(input_path, projection_path, chart_path, args):
    command = [sys.executable, PLOT, input_path, projection_path, "-o", chart_path]
    for flag in ("theme", "lang", "candles", "dpi"):
        value = getattr(args, flag)
        if value is not None:   # unset options keep plot_projection.py's own defaults
            command += [f"--{flag}", value]
    done = run(command)
    if done.returncode != 0:
        raise PipelineError("plot_projection.py failed:\n" + tail(done.stderr or done.stdout))


# ---- step 5 -------------------------------------------------------------------

def entries_command(prompt, model=None):
    """Headless Claude Code with no tools: the prompt holds both files, the reply is the result."""
    command = [CLAUDE, "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk",
               "--strict-mcp-config", "--tools", ""]
    if model:
        command += ["--model", model]
    return command


def unfenced(text):
    """The reply without a Markdown code fence, should Claude have added one."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def checked_entries(input_path, projection_path, entries_path):
    """Keep the setups only if trade_entries.py agrees with every number. Returns the
    path, or None when the two files allow no setup."""
    done = run([sys.executable, ENTRIES, "check", input_path, projection_path, entries_path])
    if done.returncode != 0 or done.stdout.strip() != "OK":
        raise PipelineError(f"{entries_path} failed trade_entries.py check:\n"
                            + (tail(done.stdout, 40) or tail(done.stderr, 1)))
    data = load_json(entries_path, "Trade setups")
    if "error" in data:   # the files allow no setup, and the file says so
        entries_path.unlink()
        say(f"      No setups: {printable(data['error'])}")
        return None
    return entries_path


def built_entries(input_path, projection_path, entries_path):
    """The ranked setups computed by trade_entries.py: every number and sentence
    follows from the projection by fixed rules, so no Claude Code call is needed."""
    done = run([sys.executable, ENTRIES, "build", input_path, projection_path, "-o", entries_path])
    if done.returncode != 0:
        raise PipelineError("trade_entries.py build failed:\n" + tail(done.stderr or done.stdout))
    return checked_entries(input_path, projection_path, entries_path)


def trade_entries(input_path, projection_path, entries_path, timeout, model=None, quiet=False,
                  facts=None):
    """Ask Claude for the ranked setups (--entries-by-claude): the same numbers, with
    sentences written for this projection. Returns the path, or None when the two
    files allow no setup. `facts`, a dict, receives what Claude Code reported."""
    filled = run([sys.executable, ENTRIES, "prompt", input_path, projection_path])
    if filled.returncode != 0:
        raise PipelineError("trade_entries.py prompt failed:\n" + tail(filled.stderr or filled.stdout))
    progress = Progress(quiet)   # no stages to show in a single reply: only the sign of life
    try:
        with tempfile.TemporaryDirectory(prefix="rz_entries_") as folder:   # away from the repo: no project context
            code, out, err = call_claude(entries_command(filled.stdout, model), timeout, progress, cwd=folder)
    except PipelineError as error:
        if "was not found" in str(error):
            raise PipelineError(f"'{CLAUDE}' was not found. Step 5 needs Claude Code on PATH "
                                "(https://claude.com/claude-code). Without --entries-by-claude the "
                                "setups are computed with no Claude Code call.")
        raise
    result = claude_result(out)
    reply, failed = claude_reply(out)
    failed = failed or code != 0
    say("      " + summary(result, progress.elapsed(), failed))
    if facts is not None:
        facts.update(claude_facts(out, progress.elapsed(), model))
    if failed:
        raise PipelineError(f"Claude Code failed (exit {code}). Last output:\n" + tail(reply or err))
    try:
        data = json.loads(unfenced(reply))
    except ValueError:
        raise PipelineError("Claude Code did not reply with JSON. Last output:\n" + tail(reply))
    if not isinstance(data, dict):
        raise PipelineError("Claude Code did not reply with a JSON object. Last output:\n" + tail(reply))
    entries_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return checked_entries(input_path, projection_path, entries_path)


# ---- provenance --------------------------------------------------------------
#
# provenance.json answers, for one run: which model and which rules made this, from which
# data, when, and at what cost. The data half comes from the input (build_prompt.py writes
# its `provenance`); the run half cannot come from Claude, which does not reliably know its
# own model id or cost, so it is read here from Claude Code's own events. A field Claude
# Code does not report is left out, never guessed.

PROVENANCE_VERSION = 1   # of provenance.json; raise it when a field changes meaning or goes away
RULE_FILES = {           # what decides the result besides the model, by the name provenance.json gives it
    "SKILL.md": RZ.parent.parent / "SKILL.md",
    "rz.py": RZ,
    "build_prompt.py": BUILD,
    "trade_entries.py": ENTRIES,
    "trade_entries.md": ROOT / "prompts" / "trade_entries.md",
    "trade_entries.schema.json": ROOT / "prompts" / "trade_entries.schema.json",
}
# With --engine mechanical the skill decides nothing; mechanical.py and backtest.py's zone rule do.
MECHANICAL_RULE_FILES = {**{name: path for name, path in RULE_FILES.items() if name != "SKILL.md"},
                         "mechanical.py": MECHANICAL, "backtest.py": ROOT / "backtest.py"}


def sha256_of(path):
    """The file's sha256 in hex, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def git_state(root=ROOT):
    """{"commit": HEAD, "dirty": tracked files changed} of the checkout, or None without git.
    Untracked files are not counted: runs/ and projections/ live there, and the rule files'
    own hashes say exactly what ran."""
    try:
        head = run(["git", "-C", root, "rev-parse", "HEAD"], timeout=10)
        status = run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or status.returncode != 0 or not head.stdout.strip():
        return None
    return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip())}


def default_sim_method():
    """The sim method `rz.py sim` uses by default in this checkout (its SIM_METHOD line), or
    None when rz.py cannot be read. Read as text: this file shares no code with rz.py."""
    try:
        found = re.search(r"^SIM_METHOD = (\d+)", Path(RZ).read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return int(found.group(1)) if found else None


def rules_provenance(engine="claude"):
    """The rules this checkout applies: a short `version` that changes whenever one of
    RULE_FILES (MECHANICAL_RULE_FILES for the mechanical engine) does, to group results by,
    the git commit, and each file's sha256."""
    chosen = MECHANICAL_RULE_FILES if engine == "mechanical" else RULE_FILES
    files = {name: sha256_of(path) for name, path in chosen.items()}
    joined = "\n".join(f"{name} {digest}" for name, digest in sorted(files.items()))
    out = {"version": hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]}
    method = default_sim_method()
    if method is not None:
        out["sim_method"] = method
    state = git_state()
    if state:
        out["git"] = state
    out["files"] = files
    return out


def claude_facts(stdout, seconds=None, requested=None):
    """What Claude Code itself reported about one call, from its events: the model and the
    Claude Code version (the `system` init event), the models that answered (the result's
    `modelUsage`, else the assistant messages), duration, turns, cost and tokens (the
    `result` event). Only fields that are present; `requested_model` is what --model asked
    for and `wall_seconds` what this side measured."""
    init, answered = {}, []
    for event in events(stdout):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init" and not init:
            init = event
        elif event.get("type") == "assistant":
            name = (event.get("message") or {}).get("model") if isinstance(event.get("message"), dict) else None
            if isinstance(name, str) and name and name not in answered:
                answered.append(name)
    result = claude_result(stdout) or {}
    out = {}
    if requested:
        out["requested_model"] = requested
    for key in ("model", "claude_code_version", "session_id"):
        if isinstance(init.get(key), str) and init[key]:
            out[key] = init[key]
    usage = result.get("modelUsage")
    if isinstance(usage, dict) and usage:
        out["models_used"] = sorted(usage)
        out["model_usage"] = usage
    elif answered:
        out["models_used"] = sorted(answered)
    if "session_id" not in out and isinstance(result.get("session_id"), str):
        out["session_id"] = result["session_id"]
    for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd"):
        if is_number(result.get(key)):
            out[key] = result[key]
    if isinstance(result.get("usage"), dict):
        out["usage"] = result["usage"]
    if is_number(seconds):
        out["wall_seconds"] = round(seconds, 1)
    return out


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_provenance(input_path, data, projection_path, claude, engine="claude"):
    """provenance.json of a run that made its projection: `claude` is claude_facts() of
    step 2, or mechanical_project()'s facts with engine "mechanical" (then `made_by` is
    "mechanical", the facts go under `mechanical` and there is no `claude` block). `rules`
    describes this checkout, the one that ran the skill or mechanical.py, and the helper."""
    mechanical = engine == "mechanical"
    proj = load_json(projection_path, "Projection JSON")
    asset, as_of = proj.get("asset") or {}, proj.get("as_of") or {}
    out = {
        "schema_version": PROVENANCE_VERSION,
        "kind": "reversal_zones_provenance",
        "written_at": utc_now(),
        "subject": {"ticker": asset.get("ticker"), "as_of": as_of.get("time"),
                    "horizon": (proj.get("horizon") or {}).get("label")},
        "input": {"sha256": sha256_of(input_path)},
        "projection": {"sha256": sha256_of(projection_path), "made_by": "mechanical" if mechanical else "claude-code"},
        "rules": rules_provenance(engine),
    }
    if "schema_version" in data:
        out["input"]["schema_version"] = data["schema_version"]
    if isinstance(data.get("provenance"), dict):
        out["input"]["data"] = data["provenance"]
    if "schema_version" in proj:
        out["projection"]["schema_version"] = proj["schema_version"]
    out["projection"]["mechanical" if mechanical else "claude"] = claude
    return out


def provenance_beside(projection_path):
    """The provenance file of a given projection, or None: provenance.json next to
    projection.json, STEM_provenance.json next to STEM_projection.json."""
    path = Path(projection_path)
    name = "provenance.json" if path.name == "projection.json" else \
        path.name[:-len("projection.json")] + "provenance.json" if path.name.endswith("_projection.json") else None
    return path.with_name(name) if name and path.with_name(name).is_file() else None


def write_provenance(record, *paths):
    """Write the record to every path given (the run's file and the saved copy)."""
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    for path in paths:
        if path:
            Path(path).write_text(text, encoding="utf-8")


# ---- your defaults (config files) ----------------------------------------------
#
# Precedence, lowest to highest: built-in default < user file < project file <
# environment variable (history_dir only) < command line. `settle()` is the one
# place that implements it; README "Your defaults" is the one place that documents it.

CONFIG_ENV = "ZONES_CONFIG"          # names the user file, instead of the default location
PROJECT_CONFIG = "zones.toml"        # looked up in the current directory only; None: no lookup
HISTORY_ENV = "RZ_PROJECTIONS_DIR"   # what rz.py and score_projections.py read
CACHE_ENV = "ZONES_CACHE_DIR"        # what data_source.py reads (the candle cache and the rate-limit file)
PRECEDENCE = ("command line > environment variable > project file (./zones.toml) > "
              "user file > built-in default")


class Setting(NamedTuple):
    """One thing a config file may set. The key is the long option's name with
    underscores (`--max-rows` -> `max_rows`), so there is one vocabulary."""
    key: str
    group: str            # "step1": becomes a build_prompt.py option; otherwise read by this script
    kind: str             # text, choice, int, number, flag or path
    default: object       # None: nothing is passed on, and `unset` says what happens then
    note: str             # for the template
    choices: tuple = ()
    example: object = None    # the template's value when the default is not one
    unset: str = ""
    low: object = None
    high: object = None
    env: str = ""

    @property
    def flag(self):
        return "--" + self.key.replace("_", "-")


GROUPS = {
    "step1": "Step 1: the request (options of build_prompt.py)",
    "claude": "Steps 2 and 5: the engine, Claude Code, the saved history, trade setups",
    "chart": "Step 4: the chart",
    "files": "Where the files go",
}
# The step-1 names, choices and defaults repeat build_prompt.py's, and theme/lang repeat
# plot_projection.py's THEMES/TEXT: a config value is checked here, before anything runs,
# so a typo costs no download and no Claude Code call. tests/test_config.py pins both.
SETTINGS = {s.key: s for s in (
    Setting("language", "step1", "text", "en",
            "language of the projection's text and of the chart's labels, e.g. \"pt\""),
    Setting("horizon", "step1", "choice", "1-3m", "today, 1-2w, 1-3m or 6-12m",
            choices=("today", "1-2w", "1-3m", "6-12m")),
    Setting("research", "step1", "choice", "one_search", "web research: none, one_search or deep",
            choices=("none", "one_search", "deep")),
    Setting("supports", "step1", "int", 3, "how many support zones to ask for"),
    Setting("resistances", "step1", "int", 3, "how many resistance zones to ask for"),
    Setting("volume", "step1", "flag", False,
            "add a volume column to every candle (off for one run: --no-volume)"),
    Setting("max_rows", "step1", "int", None, "cap the rows of the main series (default: the horizon's preset)",
            example=52, unset="the horizon's preset"),
    Setting("shape", "step1", "choice", "table", "shape of the prose after the projection: table or list",
            choices=("table", "list")),
    Setting("max_words", "step1", "int", 200, "word limit for that prose"),
    Setting("links", "step1", "choice", "decisive_only", "links in the prose: none, decisive_only or all",
            choices=("none", "decisive_only", "all")),
    Setting("decimals", "step1", "int", 2, "price rounding"),

    Setting("engine", "claude", "choice", "claude",
            "step 2: claude (the skill, paid) or mechanical (fixed rules, free, no research)",
            choices=ENGINES),
    Setting("model", "claude", "text", None, "model for `claude -p` (default: Claude Code's own)",
            example="sonnet", unset="Claude Code's own"),
    Setting("timeout", "claude", "number", DEFAULT_TIMEOUT,
            "seconds before giving up on a Claude Code call", low=0),
    Setting("quiet", "claude", "flag", False, "no line per stage while Claude Code works"),
    Setting("save", "claude", "flag", True, "keep each new projection in history_dir, for scoring later"),
    Setting("entries", "claude", "flag", True, "ranked trade setups after the chart (step 5)"),
    Setting("entries_by_claude", "claude", "flag", False,
            "have Claude Code write the setups' sentences (a second call)"),

    Setting("theme", "chart", "choice", None, "light or dark", choices=("light", "dark"),
            example="light", unset="light, the chart's default"),
    Setting("lang", "chart", "choice", None,
            "chart labels only: pt or en (default: the same as `language`)", choices=("pt", "en"),
            example="en", unset="follows `language`"),
    Setting("candles", "chart", "int", None, "how many past candles to show (default: automatic)",
            example=60, unset="automatic", low=1),
    Setting("dpi", "chart", "int", None, "resolution of raster formats, 1 to 1200",
            example=160, unset="160, the chart's default", low=1, high=1200),
    Setting("open", "chart", "flag", False, "open the chart in the default viewer after a run"),
    Setting("summary", "chart", "flag", True, "print a summary of the result before the file paths"),

    Setting("runs_dir", "files", "path", RUNS,
            "one folder per run goes here (default: runs/ in the current directory)",
            example="~/zones/runs"),
    Setting("history_dir", "files", "path", "projections",
            "the saved projections, append-only (default: $RZ_PROJECTIONS_DIR, "
            "else projections/ in the current directory)",
            example="~/zones/projections", env=HISTORY_ENV),
    Setting("cache_dir", "files", "path", None,
            "the cache of downloaded candles and the shared Yahoo rate limit (default: "
            "$ZONES_CACHE_DIR, else $XDG_CACHE_HOME/zones, else ~/.cache/zones)",
            example="~/.cache/zones", unset="$XDG_CACHE_HOME/zones, else ~/.cache/zones", env=CACHE_ENV),
)}


def literal(value):
    """A value the way a TOML file spells it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "a list"
    return str(value)


def tilde(path):
    """A path for a person to read: the home folder as ~."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def user_config():
    """Where the user file is: $ZONES_CONFIG, else $XDG_CONFIG_HOME/zones/config.toml,
    else ~/.config/zones/config.toml on every platform (one path to remember and
    document, and where command-line tools are looked for on macOS too)."""
    named = os.environ.get(CONFIG_ENV)
    if named:
        return Path(named).expanduser().absolute()
    base = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(base) if os.path.isabs(base) else Path.home() / ".config"   # the XDG rule: ignore a relative one
    return base / "zones" / "config.toml"


def config_files():
    """[(label, path)], lowest precedence first. The project file is ./zones.toml and
    nothing else: no search through parent folders, so what applies is easy to see."""
    files = [("user file", user_config())]
    if PROJECT_CONFIG:
        project = Path.cwd() / PROJECT_CONFIG
        if project != files[0][1]:
            files.append(("project file", project))
    return files


def config_path(setting, text, path):
    """A folder named in a config file: ~ and $VARIABLES expand, and a relative one
    starts at the file's own folder, so it means the same from any directory."""
    expanded = os.path.expanduser(os.path.expandvars(text))
    unset = re.search(r"\$\{?(\w+)", expanded)
    if unset:
        raise PipelineError(f"{path}: '{setting.key}' names the environment variable "
                            f"{unset.group(1)}, which is not set")
    return os.path.normpath(Path(path).parent / expanded)


def config_value(setting, value, path):
    """The value as it will be used, or one PipelineError naming the file and the key."""
    def wrong(must):
        return PipelineError(f"{path}: '{setting.key}' must be {must}, not {literal(value)}")
    whole = isinstance(value, int) and not isinstance(value, bool)
    kind, low, high = setting.kind, setting.low, setting.high
    if kind == "flag":
        if not isinstance(value, bool):
            raise wrong("true or false")
    elif kind == "choice":
        if not isinstance(value, str) or value not in setting.choices:
            raise wrong("one of " + ", ".join(literal(c) for c in setting.choices))
    elif kind == "int":
        if not whole or (low is not None and value < low) or (high is not None and value > high):
            raise wrong("a whole number" + (f" from {low} to {high}" if high is not None
                                            else f", {low} or more" if low is not None else ""))
    elif kind == "number":
        if not (whole or isinstance(value, float)) or not low < value < float("inf"):
            raise wrong(f"a number above {low}")
    elif not isinstance(value, str) or not value.strip():
        raise wrong("a folder path in quotes" if kind == "path" else "text in quotes")
    elif kind == "path":
        return config_path(setting, value, path)
    return value


def read_config(path):
    """The checked settings of one file, or None when there is no such file."""
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except tomllib.TOMLDecodeError as error:
        raise PipelineError(f"{path} is not valid TOML: {error}")
    except (OSError, UnicodeDecodeError) as error:
        raise PipelineError(f"Cannot read the config file {path}: {error}")
    values = {}
    for key, value in raw.items():
        if key not in SETTINGS:
            near = difflib.get_close_matches(key.replace("-", "_").lower(), SETTINGS, 1)
            hint = (f" (did you mean '{near[0]}'?)" if near else
                    " (settings are plain `key = value` lines, not [tables])"
                    if isinstance(value, dict) else "")
            raise PipelineError(f"{path}: unknown setting '{key}'{hint}. "
                                "`zones --show-config` lists them all.")
        values[key] = config_value(SETTINGS[key], value, path)
    return values


def typed(flag, arguments):
    """Whether a step-1 option is among the arguments passed through, and its value:
    (True, value) for `--opt value` and `--opt=value`, (True, None) for a bare flag
    or a missing value, else (False, None). An abbreviation counts, because
    build_prompt.py's parser accepts one."""
    found = (False, None)
    for index, argument in enumerate(arguments):
        head, equals, value = argument.partition("=")
        if len(head) > 2 and head.startswith("--") and flag.startswith(head):
            after = arguments[index + 1] if index + 1 < len(arguments) else None
            found = (True, value if equals else after)   # the last one wins, as in argparse
    return found


def command_line(args, passthrough):
    """{key: value} of the settings given on the command line in this run."""
    given = {}
    for setting in SETTINGS.values():
        if setting.group != "step1":
            value = getattr(args, setting.key)
            if value is not None:
                given[setting.key] = value
            continue
        found, value = typed(setting.flag, passthrough)
        if setting.kind == "flag":
            off = getattr(args, "no_" + setting.key)
            if found and off:
                raise PipelineError(f"Give {setting.flag} or --no-{setting.flag[2:]}, not both.")
            if found or off:
                given[setting.key] = found
        elif found:
            given[setting.key] = value
    return given


def settle(given, layers, environ=None):
    """{key: (value, source)} for every setting. `given` is what the command line
    set, `layers` the files as [(path, values)], lowest precedence first."""
    environ = os.environ if environ is None else environ
    settled = {}
    for key, setting in SETTINGS.items():
        value, source = setting.default, "default"
        for path, values in layers:
            if key in values:
                value, source = values[key], str(path)
        if setting.env and environ.get(setting.env):
            value, source = environ[setting.env], f"environment ({setting.env})"
        if key in given:
            value, source = given[key], "command line"
        settled[key] = (value, source)
    return settled


def export_cache_dir(settled):
    """build_prompt.py, backtest.py and score_projections.py run as child processes and read
    the cache folder from ZONES_CACHE_DIR: a folder from the command line or a config file is
    exported once for the whole process, never over a variable the user set."""
    folder, source = settled["cache_dir"]
    if folder is not None and source != "default" and not source.startswith("environment"):
        os.environ[CACHE_ENV] = str(Path(folder).expanduser().absolute())


def from_file(source):
    return source not in ("default", "command line") and not source.startswith("environment")


def step1_arguments(settled):
    """The build_prompt.py options that come from a config file. An option typed in
    this run is "command line" in `settled`, so it is never repeated here."""
    extra = []
    for key, setting in SETTINGS.items():
        value, source = settled[key]
        if setting.group != "step1" or not from_file(source):
            continue
        if setting.kind != "flag":
            extra.append(f"{setting.flag}={value}")
        elif value:
            extra.append(setting.flag)
    return extra


def config_template():
    """The file --init-config writes: every setting, commented out, with its default."""
    lines = [
        "# Your defaults for `zones` (run_pipeline.py).",
        "#",
        "# Every setting below is commented out and shows the built-in default (or an",
        "# example, where the default is not a value). To make one yours, remove its",
        "# leading \"# \" and change the value.",
        "#",
        "# Precedence: command line > environment variable > ./zones.toml in the current",
        "# directory > this file > built-in default.",
        "# `zones --show-config` prints every setting and where its value comes from;",
        "# `--no-config` ignores the files for one run.",
        "#",
        "# Paths: ~ and $VARIABLES expand; a relative path starts at this file's folder,",
        "# not at the current directory.",
    ]
    for group, title in GROUPS.items():
        lines += ["", f"# ---- {title} ----"]
        for setting in SETTINGS.values():
            if setting.group == group:
                value = setting.default if setting.example is None else setting.example
                lines.append(f"# {setting.key} = {literal(value)}".ljust(38) + f"  # {setting.note}")
    return "\n".join(lines) + "\n"


def init_config():
    """Write the template to the user location, unless a file is already there."""
    path = user_config()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:   # "x": never over an existing file
            handle.write(config_template())
    except FileExistsError:
        return f"{path} already exists and was left as it is."
    except OSError as error:
        raise PipelineError(f"Cannot write the config file {path}: {error}")
    return f"Wrote {path}: every setting is commented out. Edit it, then check with `zones --show-config`."


def show_config(files, settled, ignored=False):
    """--show-config: the files looked at, then every setting, its value and its source."""
    lines = ["Config files (the project file wins over the user file):"]
    for label, path, values in files:
        state = ("ignored (--no-config)" if ignored else "not found" if values is None else
                 f"loaded, {len(values)} setting" + ("" if len(values) == 1 else "s"))
        lines.append(f"  {label + ':':<14}{tilde(path)}  [{state}]")
    lines.append(f"Precedence: {PRECEDENCE}")
    for group, title in GROUPS.items():
        lines += ["", title]
        for key, setting in SETTINGS.items():
            if setting.group != group:
                continue
            value, source = settled[key]
            if value is None:
                shown = f"({setting.unset})"
            elif setting.kind == "path":
                shown = tilde(Path(value).absolute())
            else:
                shown = literal(value) if isinstance(value, bool) else str(value)
            lines.append(f"  {key:<19}{shown:<30}  {tilde(source)}")
    lines += ["", "On the command line each setting is its option: --research none, --runs-dir DIR, "
                  "--open / --no-open ..."]
    return "\n".join(lines)


def configured(args, passthrough):
    """Fill `args` and the pass-through list from the command line, the environment,
    the config files and the defaults, in that order of precedence."""
    if args.init_config:
        print(init_config())
        raise SystemExit(0)
    files = [(label, path, None if args.no_config else read_config(path))
             for label, path in config_files()]
    loaded = [(path, values) for _, path, values in files if values is not None]
    settled = settle(command_line(args, passthrough), loaded)
    if args.show_config:
        print(show_config(files, settled, args.no_config))
        raise SystemExit(0)
    if loaded:   # one line, so a run that departs from the built-in defaults says why
        say("Config: " + "; ".join(f"{tilde(path)} ({', '.join(values) or 'no settings'})"
                                   for path, values in loaded))
    for key, setting in SETTINGS.items():
        if setting.group != "step1":
            setattr(args, key, settled[key][0])
    args.no_entries, args.no_save = not args.entries, not args.save   # the names pipeline() reads
    args.no_summary = not args.summary
    history, source = settled["history_dir"]
    if source != "default" and not source.startswith("environment"):
        # rz.py save and anything else started from here read the variable: set it once,
        # for the whole process, so every part of the command uses the same folder
        os.environ[HISTORY_ENV] = args.history_dir = str(Path(history).absolute())
    export_cache_dir(settled)
    if passthrough and not args.input:   # with --input there is no step 1 to configure
        passthrough = step1_arguments(settled) + passthrough   # first: anything typed comes later and wins
    return args, passthrough


# ---- commands after a run ------------------------------------------------------
#
# `zones list`, `runs`, `score`, `plot`, `show` and `entries` look at what earlier
# runs left behind. Like the pipeline they share no code with the other scripts:
# each one finds the files and runs the script that does the work.

WHAT_HELP = ("a folder holding input.json and projection.json (a run folder or a saved "
             f"projection), or a ticker: its latest run under {RUNS}/ here, else its latest "
             "saved projection ($RZ_PROJECTIONS_DIR, else ./projections)")
FOLDER_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{4})_(.+?)(?:_(\d+))?$")   # TIME_HORIZON[_N]


def relay(command):
    """Run a helper script on the user's own terminal (its output is this
    command's output) and return its exit code."""
    return subprocess.run([str(part) for part in command]).returncode


def need(*scripts):
    for script in scripts:
        if not script.is_file():    # installed as a copy, away from the repository
            raise PipelineError(f"{script} not found. This command works only from the repository "
                                "checkout: install it with `uv tool install --editable PATH`.")


runs_home = Path(RUNS)   # the commands' runs folder: command_config() sets it from runs_dir
chart_config = {}        # the chart settings a config file gave: command_config() fills it


def command_config():
    """The commands after a run follow the same config files as a run: `runs_dir`,
    `history_dir` (exported as RZ_PROJECTIONS_DIR the way `configured()` exports it,
    never over a variable the user set) and the chart settings for `plot` and `show`."""
    global runs_home, chart_config
    loaded = [(path, values) for _, path in config_files()
              if (values := read_config(path)) is not None]
    settled = settle({}, loaded)
    history, source = settled["history_dir"]
    if from_file(source):
        os.environ[HISTORY_ENV] = str(Path(history).absolute())
    export_cache_dir(settled)
    runs_home = Path(settled["runs_dir"][0])
    chart_config = {key: value for key, (value, source) in settled.items()
                    if SETTINGS[key].group == "chart" and from_file(source)}


def configured_options(keys, rest):
    """`--key value` for each config value in `keys` not typed in `rest`: first, so typed wins."""
    extra = []
    for key in keys:
        if key in chart_config and not typed(SETTINGS[key].flag, rest)[0]:
            extra += [SETTINGS[key].flag, str(chart_config[key])]
    return extra


def history():
    """The folder of saved projections, as rz.py picks it."""
    return Path(os.environ.get("RZ_PROJECTIONS_DIR") or "projections")


def kept(path):
    """Is this path inside the saved history? That record is append-only, so no
    command here writes there. A folder named `projections` anywhere on the path
    counts too: a history reached from another directory."""
    path = Path(path).resolve()
    return path.is_relative_to(history().resolve()) or "projections" in path.parts


def ticker_folder(ticker):
    """petr4.sa -> PETR4.SA, ^gspc -> _GSPC: the folder name run_folder() and rz.py save use."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(ticker).upper()).strip(".")


def ticker_folders(base, ticker):
    """The folders of this ticker directly under `base`, whatever their letter case."""
    wanted, base = ticker_folder(ticker), Path(base)
    if not wanted or not base.is_dir():
        return []
    return [path for path in sorted(base.iterdir())
            if path.is_dir() and path.name.upper().strip(".") == wanted]


def folder_key(folder):
    """Sort key that puts the newest run last: the time in the folder's name, then its _N."""
    found = FOLDER_NAME.match(Path(folder).name)
    return (found.group(1), int(found.group(3) or 1), str(folder)) if found else ("", 0, str(folder))


def folder_horizon(folder):
    found = FOLDER_NAME.match(Path(folder).name)
    return found.group(2) if found else None


def pair(folder, name=None):
    """The file stem of the input/projection pair in this folder: "" for
    input.json and projection.json, STEM for STEM_input.json and
    STEM_projection.json (--out-dir, --name). None when the folder has no pair."""
    folder = Path(folder)
    both = lambda stem: all((folder / file).is_file() for file in file_names(stem)[:2])
    if name:
        return name if both(name) else None
    if both(None):
        return ""
    stems = [path.name[:-len("_input.json")] for path in sorted(folder.glob("*_input.json"))]
    stems = [stem for stem in stems if stem and both(stem)]
    if len(stems) > 1:
        raise PipelineError(f"{folder} holds several pairs ({', '.join(stems)}): pick one with --name STEM.")
    return stems[0] if stems else None


def folders_under(base, horizon=None, name=None):
    """Every folder at or below `base` that holds a pair, newest last."""
    found = []
    for here, _, _ in os.walk(base):
        if (horizon is None or folder_horizon(here) == horizon) and pair(here, name) is not None:
            found.append(Path(here))
    return sorted(found, key=folder_key)


def resolve(what, horizon=None, name=None):
    """WHAT -> the files of one run, as a dict of paths (see WHAT_HELP)."""
    given = Path(what)
    if given.is_dir():
        folder = given
        if pair(given, name) is None:       # e.g. runs/PETR4.SA: the latest run inside it
            inside = folders_under(given, horizon, name)
            if not inside:
                raise PipelineError(
                    f"No input.json and projection.json in {given} or in a folder below it"
                    + (f" for horizon {horizon}" if horizon else "") + ".")
            folder = inside[-1]
    else:
        folder, looked = None, []
        for base in (runs_home, history()):    # this directory's runs first, then the history
            looked.append(str(base.resolve() / ticker_folder(what)))
            inside = sorted((found for path in ticker_folders(base, what)
                             for found in folders_under(path, horizon, name)), key=folder_key)
            if inside:
                folder = inside[-1]
                break
        if folder is None:
            raise PipelineError(
                f"Nothing found for '{what}'" + (f" with horizon {horizon}" if horizon else "")
                + ": it is not a folder, and no run or saved projection of that ticker is in "
                + " or ".join(looked) + ".")
        say(f"Using {folder}")
    stem = pair(folder, name)
    input_name, projection_name, chart_name = file_names(stem or None)
    full = folder.resolve()
    return {"folder": folder, "input": folder / input_name, "projection": folder / projection_name,
            "chart": folder / chart_name, "kept": kept(folder),
            "entries": folder / (f"{stem}_entries.json" if stem else "entries.json"),
            "label": f"{full.parent.name}_{full.name}"}


def target(found, given, beside, suffix):
    """Where a command writes: the file asked for with -o; else beside the pair,
    replacing the run's own file; else, for a saved projection, a file in the
    current directory named after the ticker and the folder. Never inside the
    saved history."""
    if given:
        if kept(Path(given).resolve().parent):
            raise PipelineError(f"{given} is inside the saved projections, which are append-only. "
                                "Choose a file somewhere else.")
        return Path(given)
    return Path(found["label"] + suffix) if found["kept"] else beside


def command_parser(name, arguments, summary, what=True):
    parser = argparse.ArgumentParser(prog=f"zones {name}", usage=f"%(prog)s {arguments}",
                                     description=summary, allow_abbrev=False)
    if what:
        parser.add_argument("what", metavar="WHAT", help=WHAT_HELP)
        parser.add_argument("--horizon", metavar="H",
                            help="with a ticker: only its runs of this horizon (today, 1-2w, 1-3m, 6-12m)")
        parser.add_argument("--name", metavar="STEM",
                            help="the pair is STEM_input.json and STEM_projection.json "
                                 "(found without this when the folder holds only one pair)")
    return parser


def what_first(parser, argv):
    """Parse `WHAT [options]`. WHAT must come first, so that every later word can
    belong to an option that is only passed on (`zones plot NVDA --theme dark`).
    Returns (args, the options this command does not declare)."""
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        parser.error("WHAT (a folder or a ticker) comes first, before the options")
    return parser.parse_known_args(argv)


def list_saved(argv):
    """zones list [TICKER]: `rz.py list`, optionally one ticker's lines."""
    parser = command_parser("list", "[TICKER]", "The saved projections (the history kept for scoring), newest "
                            "last: time, ticker, horizon, price, the horizon's last date, open or "
                            "ended, folder. Read from $RZ_PROJECTIONS_DIR, else ./projections.", what=False)
    parser.add_argument("ticker", nargs="?", metavar="TICKER", help="only this ticker, in any letter case")
    args = parser.parse_args(argv)
    need(RZ)
    done = run([sys.executable, RZ, "list"])
    if done.returncode != 0:
        say("Error: rz.py list failed:\n" + (tail(done.stderr, 1) or tail(done.stdout)))
        return done.returncode
    lines = done.stdout.splitlines()
    if args.ticker:     # rz.py list has no filter: the ticker is the second word of each line
        lines = [line for line in lines
                 if [word.upper() for word in line.split()[1:2]] == [args.ticker.upper()]]
    if not lines:
        say(f"No saved projections under {history().resolve()}"
            + (f" for {args.ticker.upper()}" if args.ticker else "") + ".")
    for line in lines:
        print(line)
    return 0


def run_folders(argv):
    """zones runs [TICKER]: the run folders under runs/ in the current directory."""
    parser = command_parser("runs", "[TICKER]", f"The run folders under {RUNS}/ in the current directory, "
                            "newest last: time, ticker, horizon, the files each one holds, folder.",
                            what=False)
    parser.add_argument("ticker", nargs="?", metavar="TICKER", help="only this ticker, in any letter case")
    args = parser.parse_args(argv)
    base, rows = runs_home, []
    if args.ticker:
        tickers = ticker_folders(base, args.ticker)
    else:
        tickers = [path for path in sorted(base.iterdir()) if path.is_dir()] if base.is_dir() else []
    for ticker in tickers:
        for folder in ticker.iterdir():
            found = FOLDER_NAME.match(folder.name)
            files = sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []
            if found and files:    # a failed run still has its input.json, and shows as such
                rows.append((folder_key(folder), found.group(1), ticker.name, found.group(2),
                             " ".join(files), str(folder)))
    if not rows:
        say(f"No runs under {base.resolve()}" + (f" for {args.ticker.upper()}" if args.ticker else "") + ".")
    for _, stamp, ticker, horizon, files, folder in sorted(rows):
        print(stamp, ticker, horizon, folder, f"({files})")
    return 0


def score_saved(argv):
    """zones score [TICKER | FOLDER] [score_projections.py options]."""
    parser = command_parser("score", "[TICKER | FOLDER] [score_projections.py options]", "Score the saved projections against what the price did later "
                            "(score_projections.py; downloads prices from Yahoo Finance). Other "
                            "options, such as --json, go to score_projections.py.", what=False)
    parser.add_argument("target", nargs="?", metavar="TICKER | FOLDER",
                        help="a ticker: only its saved projections; or a folder of saved projections "
                             "or one saved projection (default: all of $RZ_PROJECTIONS_DIR, else ./projections)")
    if argv[:1] in (["-h"], ["--help"]):
        parser.parse_args(argv)
    # only a first word is the ticker or folder: any later one may be an option's value
    first, rest = (argv[:1], argv[1:]) if argv and not argv[0].startswith("-") else ([], argv)
    script = ROOT / "score_projections.py"
    need(script, RZ)
    if first and not Path(first[0]).is_dir():
        first = ["--ticker", first[0]]
    return relay([sys.executable, script, *first, *rest])


def plot_again(argv):
    """zones plot WHAT [-o FILE] [--open] [plot_projection.py options]."""
    parser = command_parser("plot", "WHAT [-o FILE] [--open] [--horizon H] [plot_projection.py options]", "Draw the chart of a run again (plot_projection.py). Other "
                            "options (--theme, --lang, --candles, --dpi, --no-check) go to "
                            "plot_projection.py.")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="chart file (default: the run folder's own chart.png, replaced; for a "
                             "saved projection, TICKER_FOLDER.png in the current directory, because "
                             "nothing is written into the saved history)")
    parser.add_argument("--open", action="store_true", help="open the chart in the default viewer")
    args, rest = what_first(parser, argv)
    need(PLOT)
    found = resolve(args.what, args.horizon, args.name)
    chart = target(found, args.output, found["chart"], ".png")
    rest = configured_options(("theme", "lang", "candles", "dpi"), rest) + rest
    code = relay([sys.executable, PLOT, found["input"], found["projection"], "-o", chart, *rest])
    if code == 0:
        print(f"chart:      {chart}")
        if args.open or chart_config.get("open"):
            open_chart(chart)
    return code


def show_again(argv):
    """zones show WHAT [show_run.py options]."""
    parser = command_parser("show", "WHAT [--horizon H] [show_run.py options]", "Print the summary of a run (show_run.py). Other options "
                            "(--lang, --width, --no-color) go to show_run.py.")
    args, rest = what_first(parser, argv)
    script = ROOT / "show_run.py"
    if not script.is_file():
        raise PipelineError(f"The summary script is not available ({script} not found).")
    found = resolve(args.what, args.horizon, args.name)
    files = [found["input"], found["projection"]] + ([found["entries"]] if found["entries"].is_file() else [])
    return relay([sys.executable, script, *files, *configured_options(("lang",), rest), *rest])


def entries_again(argv):
    """zones entries WHAT [-o FILE]: trade_entries.py build, then check."""
    parser = command_parser("entries", "WHAT [-o FILE] [--horizon H]", "Compute the three ranked trade setups of a run again "
                            "(trade_entries.py build, then check). No Claude Code call.")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="setups file (default: the run folder's own entries.json, replaced; for "
                             "a saved projection, TICKER_FOLDER_entries.json in the current "
                             "directory, because nothing is written into the saved history)")
    args, rest = what_first(parser, argv)
    if rest:
        parser.error("unrecognized arguments: " + " ".join(rest))
    need(ENTRIES)
    found = resolve(args.what, args.horizon, args.name)
    path = target(found, args.output, found["entries"], "_entries.json")
    with tempfile.TemporaryDirectory(prefix="rz_entries_") as folder:   # checked before it replaces anything
        draft = Path(folder) / "entries.json"
        for step in (["build", found["input"], found["projection"], "-o", draft],
                     ["check", found["input"], found["projection"], draft]):
            done = run([sys.executable, ENTRIES, *step])
            if done.returncode != 0 or (step[0] == "check" and done.stdout.strip() != "OK"):
                say(f"Error: trade_entries.py {step[0]} failed:\n"
                    + (tail(done.stdout, 40) or tail(done.stderr)))
                return done.returncode or 1
        data = load_json(draft, "Trade setups")
        if "error" in data:   # the files allow no setup, and the file says so
            say(f"No setups: {printable(data['error'])}")
            return 0
        try:
            shutil.copyfile(draft, path)
        except OSError as error:
            raise PipelineError(f"Cannot write {path}: {error}")
    print(f"entries:    {path}")
    return 0


def backtest(argv):
    """zones backtest TICKER... [backtest.py options]: backtest.py with the same arguments."""
    if argv[:1] in (["-h"], ["--help"]):
        command_parser("backtest", "TICKER [TICKER ...] [backtest.py options]", "Backtest the baseline "
                       "probabilities on past dates (backtest.py; downloads prices from Yahoo Finance, "
                       "no Claude call). Every option goes to backtest.py: `uv run python backtest.py "
                       "--help` lists them.", what=False).parse_args(argv)
    need(ROOT / "backtest.py", RZ)
    return relay([sys.executable, ROOT / "backtest.py", *argv])


def cache(argv):
    """zones cache [info|clear|path] [TICKER...]: data_source.py's own commands."""
    parser = command_parser("cache", "[info|clear|path] [TICKER ...]", "The local cache of downloaded "
                            "candles (data_source.py): info lists what it holds, clear deletes it (or "
                            "only the tickers named), path prints its folder. Finished daily, weekly "
                            "and monthly candles are kept there; the newest are always downloaded.",
                            what=False)
    parser.add_argument("command", nargs="?", choices=("info", "clear", "path"), default="info")
    parser.add_argument("tickers", nargs="*", metavar="TICKER")
    args = parser.parse_args(argv)
    script = ROOT / "data_source.py"
    need(script)
    return relay([sys.executable, script, args.command, *args.tickers])


def batch(argv):
    """zones batch FILE|TICKER... [options]: one pipeline run per ticker (batch_runs.py).
    In this process, not relayed: Ctrl-C must reach its loop, which then writes the summary."""
    need(ROOT / "batch_runs.py")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import batch_runs
    return batch_runs.main(argv)


COMMANDS = {"list": list_saved, "runs": run_folders, "score": score_saved,
            "plot": plot_again, "show": show_again, "entries": entries_again,
            "backtest": backtest, "batch": batch, "cache": cache}
FORCE_PIPELINE = ("run", "--")


def dispatch(argv=None):
    """Tell `zones list` from `zones PETR4.SA`. Returns (exit code, argv): the
    exit code of a command that ran here, or None with the arguments the
    pipeline is to parse.

    The first argument is a command only when it is exactly one of the lowercase
    words in COMMANDS. Everything else is the pipeline's, as before: a ticker in
    any letter case (`zones nvda`), `--input FILE`, `--help`, nothing at all.
    `zones run ARGS` and `zones -- ARGS` are the pipeline whatever ARGS is, for
    a ticker spelled like a command (`zones run list`; `zones LIST` works too)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in FORCE_PIPELINE:
        return None, argv[1:]
    if not argv or argv[0] not in COMMANDS:
        return None, argv
    try:
        command_config()
        return COMMANDS[argv[0]](argv[1:]), argv
    except PipelineError as error:
        say(f"Error: {error}")
        return 1, argv
    except KeyboardInterrupt:
        say("Interrupted.")
        return 130, argv


# ---- command line -------------------------------------------------------------

def parse(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s TICKER [build_prompt.py options] [options]\n"
              "       %(prog)s --input FILE [--projection FILE] [options]",
        allow_abbrev=False)
    switch = argparse.BooleanOptionalAction   # --open / --no-open: either side of a config file's value
    files = parser.add_argument_group("files")
    files.add_argument("--out-dir", metavar="DIR",
                       help="put the files exactly here, named STEM_input.json, "
                            "STEM_projection.json and STEM.png (default: a new folder "
                            f"{RUNS}/TICKER/TIME_HORIZON/ with input.json, projection.json "
                            "and chart.png)")
    files.add_argument("--name", metavar="STEM",
                       help="file name stem (default with --out-dir: the ticker, lowercased, "
                            "without the exchange suffix; in a run folder: none)")
    files.add_argument("--input", metavar="FILE",
                       help="use this input JSON and skip step 1 (no download)")
    files.add_argument("--projection", metavar="FILE",
                       help="use this projection JSON and skip step 2 (needs --input)")
    files.add_argument("--chart", metavar="FILE",
                       help="chart file (default: chart.png in the run folder, or STEM.png "
                            "in --out-dir; the extension picks the format)")
    files.add_argument("--open", action=switch, default=None,
                       help="open the chart in the default viewer when the run succeeds")
    files.add_argument("--summary", action=switch, default=None,
                       help="print a summary of the result (show_run.py) before the file "
                            "paths; --no-summary prints the paths only")
    files.add_argument("--runs-dir", metavar="DIR",
                       help=f"where the run folders are made (default: {RUNS}/ in the current directory)")
    files.add_argument("--cache-dir", metavar="DIR",
                       help=f"the cache of downloaded candles (default: ${CACHE_ENV}, else "
                            "~/.cache/zones); build_prompt.py --no-cache skips it for one run")
    files.add_argument("--history-dir", metavar="DIR",
                       help=f"where new projections are kept (default: ${HISTORY_ENV}, else "
                            "projections/ in the current directory)")
    claude = parser.add_argument_group("steps 2 and 5")
    claude.add_argument("--engine", choices=ENGINES, default=None,
                        help="who makes the projection: claude (default; the skill through Claude Code, "
                             "paid, a few minutes, web research) or mechanical (mechanical.py: backtest.py's "
                             "zone rule and the unadjusted rz.py sim, no Claude Code call, about a second, "
                             "no research)")
    claude.add_argument("--entries", action=switch, default=None,
                        help="--no-entries stops at the chart: no ranked trade setups (step 5)")
    claude.add_argument("--entries-by-claude", action=switch, default=None,
                        help="have Claude Code write the setups' sentences (a second call, a few "
                             "minutes); the numbers are the same, computed by trade_entries.py")
    claude.add_argument("--model", help="model for `claude -p` (default: Claude Code's own)")
    claude.add_argument("--save", action=switch, default=None,
                        help="--no-save does not keep the new projection under projections/ "
                             "(see `rz.py save`)")
    claude.add_argument("--quiet", action=switch, default=None,
                        help="no line per stage while Claude Code works; step headers, "
                             "errors and the duration and cost line stay")
    claude.add_argument("--timeout", type=float, metavar="SECONDS",
                        help=f"give up on each Claude Code call after this long (default: {DEFAULT_TIMEOUT})")
    chart = parser.add_argument_group(
        "step 4, passed to plot_projection.py only when given")
    chart.add_argument("--theme")
    chart.add_argument("--lang")
    chart.add_argument("--candles")
    chart.add_argument("--dpi")
    defaults = parser.add_argument_group(
        "your defaults",
        "Standing preferences are read from the user file ($ZONES_CONFIG, else\n"
        "$XDG_CONFIG_HOME/zones/config.toml, else ~/.config/zones/config.toml) and from\n"
        "zones.toml in the current directory. Precedence: command line > environment\n"
        "variable > project file > user file > built-in default. A switch that a file\n"
        "turns on is turned off for one run by its --no- form.")
    defaults.add_argument("--show-config", action="store_true",
                          help="print the config files found and every setting with where its "
                               "value comes from, then exit")
    defaults.add_argument("--init-config", action="store_true",
                          help="write a commented template to the user file, unless it exists, then exit")
    defaults.add_argument("--no-config", action="store_true", help="ignore the config files in this run")
    for setting in SETTINGS.values():   # step-1 switches: build_prompt.py has no "off" form
        if setting.group == "step1" and setting.kind == "flag":
            defaults.add_argument(f"--no-{setting.flag[2:]}", action="store_true",
                                  help=f"do not pass {setting.flag} to build_prompt.py although a "
                                       "config file sets it")
    args, passthrough = parser.parse_known_args(argv)
    try:
        return configured(args, passthrough)
    except PipelineError as error:   # a config file problem: one line, like every other error
        say(f"Error: {error}")
        raise SystemExit(1)


def pipeline(args, passthrough):
    if args.projection and not args.input:
        raise PipelineError("--projection needs --input: the chart is drawn from both files.")
    if args.input and passthrough:
        raise PipelineError("With --input nothing is downloaded, so these have no effect: "
                            + " ".join(passthrough))
    if not args.input and not passthrough:
        raise PipelineError("Give a ticker (e.g. PETR4.SA) or --input FILE. See --help.")
    mechanical = args.engine == "mechanical"
    for script in (BUILD, PLOT, RZ, ENTRIES) + ((MECHANICAL,) if mechanical else ()):
        if not script.is_file():    # installed as a copy, away from the repository
            raise PipelineError(f"{script} not found. This command works only from the repository "
                                "checkout: install it with `uv tool install --editable PATH`.")
    steps = 4 if args.no_entries else 5

    fresh = None
    if args.input:
        input_path = Path(args.input)
        data = load_json(input_path, "Input JSON")
        say(f"[1/{steps}] Input: using {input_path} (no download)")
        if mechanical and not args.projection and data.get("research", "none") != "none":
            say(research_warning(data.get("research")))   # the projection's limits say so too
    else:
        if mechanical:   # nothing searches, so the input asks for nothing: it is the record of the request
            found, asked = typed("--research", passthrough)
            if found and asked not in (None, "none"):   # typed, or from a config file
                say(research_warning(asked))
            passthrough = [*passthrough, "--research=none"]   # last: argparse keeps the last one
        say(f"[1/{steps}] Input: build_prompt.py " + " ".join(passthrough))
        fresh, data = build_input(passthrough)
    if args.projection:
        projection_path = Path(args.projection)
        load_json(projection_path, "Projection JSON")

    # Nothing is created before this point, so a mistyped ticker or file leaves no folder behind.
    if args.out_dir is not None:        # the exact folder; files given with --input/--projection stay put
        out_dir, own = Path(args.out_dir), False
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = args.name or stem_for(data.get("asset", {}).get("ticker", ""))
    elif args.projection and args.chart and args.no_entries:   # plot again into a named file: nothing else to place
        out_dir, own, stem = None, False, args.name
    else:                               # a new folder for this run, complete with all three files
        out_dir, own, stem = run_folder(data, args.runs_dir), True, args.name
    input_name, projection_name, chart_name = file_names(stem)
    if fresh is not None:
        input_path = out_dir / input_name
        input_path.write_bytes(fresh)
    elif own:
        input_path = Path(shutil.copyfile(input_path, out_dir / input_name))

    claude = None
    if args.projection:
        say(f"[2/{steps}] Projection: using {projection_path} (Claude Code not run)")
        if own:
            projection_path = Path(shutil.copyfile(projection_path, out_dir / projection_name))
    elif mechanical:
        projection_path = out_dir / projection_name
        say(f"[2/{steps}] Projection: mechanical engine, no Claude Code call (backtest.py's zone rule, "
            "unadjusted rz.py sim)")
        claude = mechanical_project(input_path, projection_path)
    else:
        projection_path = out_dir / projection_name
        research = data.get("research", "none")
        say(f"[2/{steps}] Projection: Claude Code runs the skill (research: {research}). "
            f"This can take several minutes; timeout {args.timeout:g} s")
        claude = project(input_path, data, projection_path, args.timeout, args.model, args.quiet) or {}

    say(f"[3/{steps}] Check: rz.py check")
    check(projection_path)
    say("      OK")
    saved = None
    if not args.projection and not args.no_save:   # only a projection made in this run is new
        saved = save(input_path, projection_path)
    # which model, rules and data made the projection: beside the run's files and in the saved
    # entry. Only a run that made it can say; a given projection brings its own file, if any.
    record = provenance_path = saved_provenance = None
    provenance_name = f"{stem}_provenance.json" if stem else "provenance.json"
    if claude is not None:
        record = run_provenance(input_path, data, projection_path, claude, "mechanical" if mechanical else "claude")
        provenance_path = out_dir / provenance_name if out_dir else None
        saved_provenance = Path(saved) / "provenance.json" if saved else None
        write_provenance(record, provenance_path, saved_provenance)
    elif own and provenance_beside(args.projection):   # a complete copy, like the other files
        shutil.copyfile(provenance_beside(args.projection), out_dir / provenance_name)

    chart_path = Path(args.chart) if args.chart else out_dir / chart_name
    say(f"[4/{steps}] Chart: plot_projection.py")
    plot(input_path, projection_path, chart_path, args)
    result = {"input": input_path, "projection": projection_path, "chart": chart_path,
              "saved": saved, "entries": None, "failed": None}
    if args.no_entries:
        return result

    if args.entries_by_claude:
        say("[5/5] Trade setups: Claude Code writes up the three ranked setups (entry, stop, "
            f"target). This takes a few minutes; timeout {args.timeout:g} s")
    else:
        say("[5/5] Trade setups: trade_entries.py ranks three (entry, stop, target)")
    entries_path = out_dir / (f"{stem}_entries.json" if stem else "entries.json")
    facts = {}
    try:   # a failure here must not hide the three files already made
        if args.entries_by_claude:
            result["entries"] = trade_entries(input_path, projection_path, entries_path,
                                              args.timeout, args.model, args.quiet, facts)
        else:
            result["entries"] = built_entries(input_path, projection_path, entries_path)
    except PipelineError as error:
        result["failed"] = str(error)
    if result["entries"] and saved:   # keep the setups with the pair they were derived from
        shutil.copyfile(entries_path, Path(saved) / "entries.json")
    if result["entries"] and record:   # the same run completes its own record, as it adds entries.json
        record["entries"] = {"sha256": sha256_of(entries_path),
                             "made_by": "claude-code" if args.entries_by_claude else "trade_entries.py"}
        if args.entries_by_claude:
            record["entries"]["claude"] = facts
        write_provenance(record, provenance_path, saved_provenance)
    return result


def main(argv=None):
    code, argv = dispatch(argv)   # `zones list`, `zones plot ...`: not the pipeline
    if code is not None:
        return code
    args, passthrough = parse(argv)
    try:
        result = pipeline(args, passthrough)
    except PipelineError as error:
        say(f"Error: {error}")
        return 1
    except KeyboardInterrupt:
        say("Interrupted.")
        return 130
    if not args.no_summary:   # before the paths, which stay last and easy to copy
        show_summary(result, args.lang)
    print(f"input:      {result['input']}")
    print(f"projection: {result['projection']}")
    print(f"chart:      {result['chart']}")
    if result["entries"]:
        print(f"entries:    {result['entries']}")
    if result["saved"]:
        print(f"saved:      {result['saved']}")
    if args.open:
        open_chart(result["chart"])
    if result["failed"]:
        say(f"Error: {result['failed']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
