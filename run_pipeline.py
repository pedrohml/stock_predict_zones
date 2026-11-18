#!/usr/bin/env python3
"""Ticker to chart in one command: build_prompt.py, the skill (headless Claude Code), rz.py check, plot_projection.py.

    python run_pipeline.py PETR4.SA --horizon 1-3m
    python run_pipeline.py NVDA --horizon 1-2w --research none --out-dir runs --lang en
    python run_pipeline.py --input petr4_input.json                                    # project and plot again
    python run_pipeline.py --input petr4_input.json --projection petr4_projection.json # plot again

A projection made by this run is also kept, with its input, under projections/
(`rz.py save`; folder set by $RZ_PROJECTIONS_DIR) unless --no-save is given.

Options this script does not know (the ticker, --horizon, --research, --event,
--language ...) are passed to build_prompt.py untouched; see
`python build_prompt.py --help`.

Step 2 runs `claude -p` in this repository with a narrow tool allowlist: the
skill, the helper script, and reads and writes inside a throwaway work folder
(plus web search when the input's `research` is not `none`). While it runs,
one line per stage of the skill goes to standard error, then the duration,
turns and cost that Claude Code reports; --quiet leaves only that last line.

The result is analysis for study, not investment advice.
"""
import argparse
import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build_prompt.py"
PLOT = ROOT / "plot_projection.py"
SKILL = "reversal-zones-projection"
RZ = ROOT / ".claude" / "skills" / SKILL / "scripts" / "rz.py"
CLAUDE = "claude"
DEFAULT_TIMEOUT = 1800   # seconds; a run with research usually takes a few minutes
HEARTBEAT = 30           # seconds without a progress line before "still running"
TAIL_LINES = 15
WORK_INPUT, WORK_PROJECTION = "input.json", "projection.json"   # names inside the work folder


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


# ---- step 1 -------------------------------------------------------------------

def build_input(passthrough, out_dir, name=None):
    """Run build_prompt.py with the user's own arguments. The file name comes
    from the ticker inside the JSON it writes, so the arguments are never
    parsed here."""
    if any(arg in ("-o", "--output") or arg.startswith("--output=") for arg in passthrough):
        raise PipelineError("Do not pass -o/--output: use --out-dir and --name to place the files.")
    draft = out_dir / ".input.json.part"
    done = run([sys.executable, BUILD, *passthrough, "-o", draft])
    if done.returncode != 0:
        draft.unlink(missing_ok=True)
        raise PipelineError("build_prompt.py failed:\n" + tail(done.stderr or done.stdout))
    try:
        data = load_json(draft, "Input JSON from build_prompt.py")
        stem = name or stem_for(data.get("asset", {}).get("ticker", ""))
    except PipelineError:
        draft.unlink(missing_ok=True)
        raise
    path = out_dir / f"{stem}_input.json"
    draft.replace(path)
    return path, data


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
    """Run the skill headless in a throwaway folder, then keep only the projection."""
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


# ---- command line -------------------------------------------------------------

def parse(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s TICKER [build_prompt.py options] [options]\n"
              "       %(prog)s --input FILE [--projection FILE] [options]",
        allow_abbrev=False)
    files = parser.add_argument_group("files")
    files.add_argument("--out-dir", default=".", metavar="DIR",
                       help="where the three files go (default: current directory)")
    files.add_argument("--name", metavar="STEM",
                       help="file name stem (default: the ticker, lowercased, "
                            "without the exchange suffix)")
    files.add_argument("--input", metavar="FILE",
                       help="use this input JSON and skip step 1 (no download)")
    files.add_argument("--projection", metavar="FILE",
                       help="use this projection JSON and skip step 2 (needs --input)")
    files.add_argument("--chart", metavar="FILE",
                       help="chart file (default: STEM.png in --out-dir; "
                            "the extension picks the format)")
    claude = parser.add_argument_group("step 2, Claude Code")
    claude.add_argument("--model", help="model for `claude -p` (default: Claude Code's own)")
    claude.add_argument("--no-save", action="store_true",
                        help="do not keep the new projection under projections/ "
                             "(see `rz.py save`)")
    claude.add_argument("--quiet", action="store_true",
                        help="no line per stage while Claude Code works; step headers, "
                             "errors and the duration and cost line stay")
    claude.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help=f"give up on Claude Code after this long (default: {DEFAULT_TIMEOUT})")
    chart = parser.add_argument_group(
        "step 4, passed to plot_projection.py only when given")
    chart.add_argument("--theme")
    chart.add_argument("--lang")
    chart.add_argument("--candles")
    chart.add_argument("--dpi")
    return parser.parse_known_args(argv)


def pipeline(args, passthrough):
    out_dir = Path(args.out_dir)
    if args.projection and not args.input:
        raise PipelineError("--projection needs --input: the chart is drawn from both files.")
    if args.input and passthrough:
        raise PipelineError("With --input nothing is downloaded, so these have no effect: "
                            + " ".join(passthrough))
    if not args.input and not passthrough:
        raise PipelineError("Give a ticker (e.g. PETR4.SA) or --input FILE. See --help.")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.input:
        input_path = Path(args.input)
        data = load_json(input_path, "Input JSON")
        say(f"[1/4] Input: using {input_path} (no download)")
    else:
        say("[1/4] Input: build_prompt.py " + " ".join(passthrough))
        input_path, data = build_input(passthrough, out_dir, args.name)
    stem = args.name or stem_for(data.get("asset", {}).get("ticker", ""))

    if args.projection:
        projection_path = Path(args.projection)
        load_json(projection_path, "Projection JSON")
        say(f"[2/4] Projection: using {projection_path} (Claude Code not run)")
    else:
        projection_path = out_dir / f"{stem}_projection.json"
        research = data.get("research", "none")
        say(f"[2/4] Projection: Claude Code runs the skill (research: {research}). "
            f"This can take several minutes; timeout {args.timeout:g} s")
        project(input_path, data, projection_path, args.timeout, args.model, args.quiet)

    say("[3/4] Check: rz.py check")
    check(projection_path)
    say("      OK")
    saved = None
    if not args.projection and not args.no_save:   # only a projection made in this run is new
        saved = save(input_path, projection_path)

    chart_path = Path(args.chart) if args.chart else out_dir / f"{stem}.png"
    say("[4/4] Chart: plot_projection.py")
    plot(input_path, projection_path, chart_path, args)
    return input_path, projection_path, chart_path, saved


def main(argv=None):
    args, passthrough = parse(argv)
    try:
        input_path, projection_path, chart_path, saved = pipeline(args, passthrough)
    except PipelineError as error:
        say(f"Error: {error}")
        return 1
    except KeyboardInterrupt:
        say("Interrupted.")
        return 130
    print(f"input:      {input_path}")
    print(f"projection: {projection_path}")
    print(f"chart:      {chart_path}")
    if saved:
        print(f"saved:      {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
