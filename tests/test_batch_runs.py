"""batch_runs.py: `zones batch`, one pipeline run per ticker of a watchlist.

Offline and contained. Every test runs in an empty temporary directory. The
scheduling tests replace each run with a small script (FAKE_RUN) that prints what
the pipeline prints and makes a run folder from the fixtures; the end-to-end
tests at the bottom run the real pipeline in the child, with build_prompt.py
(Yahoo) stubbed and a script in place of `claude` that replays
tests/fixtures/claude_stream.jsonl, so the lines batch_runs.py reads are pinned
against the real ones.
"""
import csv
import json
import sys
from pathlib import Path

import pytest

import batch_runs
import run_pipeline
from conftest import FIXTURES, ROOT
from test_run_pipeline import store  # noqa: F401  (autouse: RZ_PROJECTIONS_DIR in a temp folder)

FOLDER = "2026-11-17T1736_1-3m"     # the run folder name of the fixture input

# Stands in for one `zones run TICKER ...`: what it does is read from $BATCH_FAKE
# ({TICKER: {"sleep", "lines", "exit", "folder"}}); it notes its start and end in $BATCH_TRACE.
FAKE_RUN = """
import json, os, shutil, sys, time
ticker, options = sys.argv[1], sys.argv[2:]
trace = os.environ["BATCH_TRACE"]
with open(os.path.join(trace, ticker + ".args"), "w") as handle:
    json.dump(options, handle)
with open(os.path.join(trace, ticker + ".start"), "w") as handle:
    handle.write(repr(time.time()))
plan = json.loads(os.environ["BATCH_FAKE"]).get(ticker, {{}})
time.sleep(plan.get("sleep", 0.05))
for line in plan.get("lines", []):
    print(line, file=sys.stderr)
if plan.get("folder", True):
    folder = os.path.join("runs", ticker, "{folder}")
    os.makedirs(folder)
    shutil.copyfile({input!r}, os.path.join(folder, "input.json"))
    shutil.copyfile({projection!r}, os.path.join(folder, "projection.json"))
    if plan.get("exit", 0) == 0:
        print("input:      " + os.path.join(folder, "input.json"))
with open(os.path.join(trace, ticker + ".end"), "w") as handle:
    handle.write(repr(time.time()))
sys.exit(plan.get("exit", 0))
"""


def ran_ok(cost=0.75):
    """The stderr lines of a run that succeeded after one Claude Code call."""
    return ["[1/5] Input: build_prompt.py X", "[2/5] Projection: Claude Code runs the skill (research: none). x",
            "      reading the input                                   0:01",
            "      done in 2:17 · 8 turns" + ("" if cost is None else f" · ${cost:.2f}"),
            "[3/5] Check: rz.py check", "      OK"]


@pytest.fixture(autouse=True)
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(batch_runs, "POLL", 0.01)
    return tmp_path


@pytest.fixture
def fake(tmp_path, monkeypatch, input_path, projection_path):
    """fake(TICKER={...}, ...) makes every run the FAKE_RUN script, with these plans."""
    script, trace = tmp_path / "fake_run.py", tmp_path / "trace"
    trace.mkdir()
    script.write_text(FAKE_RUN.format(folder=FOLDER, input=str(input_path), projection=str(projection_path)),
                      encoding="utf-8")
    monkeypatch.setenv("BATCH_TRACE", str(trace))
    monkeypatch.setattr(batch_runs, "child_command",
                        lambda ticker, horizon, options: [sys.executable, str(script), ticker, *options]
                        + (["--horizon", horizon] if horizon else []))

    def install(**plans):
        monkeypatch.setenv("BATCH_FAKE", json.dumps(plans))
        return trace
    return install


def summary_rows(cwd):
    [path] = list(Path(cwd).glob("runs/batch/*/summary.csv"))
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---- the watchlist ---------------------------------------------------------------

def test_watchlist_lines_comments_horizons_and_duplicates(tmp_path):
    path = tmp_path / "watch.txt"
    path.write_text("# my list\n\nPETR4.SA\nVALE3.SA 1-2w   # weekly look\n  nvda,6-12m\n"
                    "petr4.sa\n^GSPC\nBTC-USD\n", encoding="utf-8")
    assert batch_runs.read_watchlist(path) == [
        ("PETR4.SA", None), ("VALE3.SA", "1-2w"), ("nvda", "6-12m"), ("petr4.sa", None),
        ("^GSPC", None), ("BTC-USD", None)]
    # a ticker given twice with the same horizon runs once, in any letter case
    assert batch_runs.gather([str(path), "NVDA", "PETR4.SA"]) == [
        ("PETR4.SA", None), ("VALE3.SA", "1-2w"), ("nvda", "6-12m"), ("^GSPC", None),
        ("BTC-USD", None), ("NVDA", None)]


@pytest.mark.parametrize("text, message", [
    ("PETR4.SA 1-6m\n", "horizon '1-6m'"),
    ("PETR4.SA 1-3m extra\n", "expected 'TICKER' or 'TICKER HORIZON'"),
    ("PETR4.SA\nrm -rf\n", ":2: horizon .-rf."),
    ("$$$\n", "does not look like a ticker"),
])
def test_watchlist_mistakes_name_the_line(tmp_path, text, message):
    path = tmp_path / "watch.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(batch_runs.BatchError, match=message.replace("$", r"\$")):
        batch_runs.read_watchlist(path)


def test_a_missing_watchlist_is_not_taken_for_a_ticker():
    with pytest.raises(batch_runs.BatchError, match="Watchlist not found"):
        batch_runs.gather(["watch.txt"])
    with pytest.raises(batch_runs.BatchError, match="Watchlist not found"):
        batch_runs.gather(["lists/mine"])


def test_items_come_first_and_options_go_to_every_run():
    args, items, options = batch_runs.parse(
        ["watch.txt", "NVDA", "--horizon", "1-2w", "--jobs", "3", "--research", "none",
         "--max-cost", "2.5", "--model", "sonnet"])
    assert items == ["watch.txt", "NVDA"]
    assert (args.jobs, args.max_cost) == (3, 2.5)
    assert options == ["--horizon", "1-2w", "--research", "none", "--model", "sonnet", "--no-open"]
    # --open typed on purpose is kept; --runs-dir is the batch's and each run's
    _, _, options = batch_runs.parse(["NVDA", "--open", "--runs-dir", "out"])
    assert options == ["--open", "--runs-dir", "out"]


@pytest.mark.parametrize("argv", [
    ["NVDA", "--out-dir", "x"], ["NVDA", "--input", "f.json"], ["NVDA", "--chart=c.png"],
    ["NVDA", "--jobs", "0"], ["NVDA", "--max-cost", "0"], ["NVDA", "--max-cost", "nan"],
    ["--research", "none"],
])
def test_options_that_cannot_work_in_a_batch_are_refused(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        batch_runs.parse(argv)
    assert stop.value.code == 2


# ---- what a run printed ------------------------------------------------------------

def test_cost_is_what_claude_code_reported_never_a_guess():
    assert batch_runs.cost_of("\n".join(ran_ok(0.75))) == (0.75, "")
    two = ran_ok(0.75) + ["[5/5] Trade setups: Claude Code writes up the three", "      done in 1:00 · 1 turn · $0.20"]
    assert batch_runs.cost_of("\n".join(two)) == (0.95, "")
    # Claude Code ran but reported no cost: unknown, not zero
    cost, note = batch_runs.cost_of("\n".join(ran_ok(None)))
    assert cost is None and note.startswith("unknown")
    # a timeout: the call started and its line never came
    cost, note = batch_runs.cost_of("[2/5] Projection: Claude Code runs the skill\nError: Claude Code did not finish")
    assert cost is None and note.startswith("unknown")
    # one call reported, the other did not: at least the reported part
    cost, note = batch_runs.cost_of("\n".join(ran_ok(0.5) + ["[5/5] Trade setups: Claude Code writes up"]))
    assert cost == 0.5 and note.startswith("at least")
    # no Claude Code call at all (Yahoo failed, or --projection): nothing was spent
    assert batch_runs.cost_of("[1/5] Input: build_prompt.py X\nError: build_prompt.py failed:") == (0.0, "no Claude Code call")
    # a stage line that happens to say "done in" is not the summary line
    assert batch_runs.cost_of("      web search 1: done in 2:00 $5\n") == (0.0, "no Claude Code call")


def test_the_error_is_the_runs_own_message():
    text = "[1/5] Input\nError: build_prompt.py failed:\n    No price data for XYZ\n"
    assert batch_runs.error_of(text, 1) == "build_prompt.py failed: No price data for XYZ"
    assert batch_runs.error_of("Traceback ...\nKeyError: 'x'", 1) == "exit code 1"
    assert batch_runs.error_of("Error: Claude Code did not finish within 4 s. Last output:\n", 1) == (
        "Claude Code did not finish within 4 s.")


def test_facts_take_the_nearest_real_zones(projection_path, tmp_path, projection_data):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "projection.json").write_text(json.dumps(projection_data), encoding="utf-8")
    got = batch_runs.facts(folder)
    price = projection_data["as_of"]["price"]
    assert got["price"] == price and got["confidence"] in ("low", "medium")
    assert got["support_to"] <= price + 1e-9 or got["support_from"] <= price
    real = [z for z in projection_data["supports"] if None not in z["zone"]]
    assert got["support_reach"] in [z["reach"] for z in real]
    top = max(projection_data["scenarios"], key=lambda s: s["p"])
    assert (got["top_scenario"], got["top_scenario_p"]) == (top["name"], top["p"])
    assert batch_runs.facts(tmp_path / "nowhere") == {}


# ---- the batch -------------------------------------------------------------------

def test_one_failure_never_stops_the_others(cwd, fake, capsys):
    fake(AAA={"lines": ran_ok(0.5)},
         BAD={"lines": ["[1/5] Input: build_prompt.py BAD", "Error: build_prompt.py failed:",
                        "    No price data found for BAD"], "exit": 1, "folder": False},
         CHK={"lines": ran_ok(0.25) + ["[3/5] Check: rz.py check", "Error: projection.json failed rz.py check:",
                                      "    S1: reach above 1"], "exit": 1},
         ZZZ={"lines": ran_ok(0.5)})
    assert batch_runs.main(["AAA", "BAD", "CHK", "ZZZ", "--jobs", "1"]) == 1
    out, err = capsys.readouterr()
    rows = {row["ticker"]: row for row in summary_rows(cwd)}
    assert [rows[t]["status"] for t in ("AAA", "BAD", "CHK", "ZZZ")] == ["ok", "failed", "failed", "ok"]
    assert rows["BAD"]["error"] == "build_prompt.py failed: No price data found for BAD"
    assert rows["BAD"]["cost_usd"] == "0.00" and rows["BAD"]["run_folder"] == ""
    # a run that failed after making its folder still points at it, with what it holds
    assert rows["CHK"]["run_folder"].endswith(f"CHK/{FOLDER}") and rows["CHK"]["price"]
    assert rows["CHK"]["cost_usd"] == "0.25" and "rz.py check" in rows["CHK"]["error"]
    assert "4 tickers: 2 ok, 2 failed, 0 skipped; reported cost $1.25." in out
    assert "BAD: failed: build_prompt.py failed" in out and "Analysis for study, not investment advice." in out
    # one line per ticker as it starts and one as it ends, not the runs' own lines
    assert sum("started" in line for line in err.splitlines()) == 4
    assert "reading the input" not in err
    log = Path(rows["CHK"]["log"])
    assert log.parent.name == "logs" and "S1: reach above 1" in log.read_text(encoding="utf-8")


def test_jobs_run_at_the_same_time_up_to_the_limit(cwd, fake):
    trace = fake(**{name: {"sleep": 0.4, "lines": ran_ok(0.1)} for name in ("AA", "BB", "CC", "DD", "EE")})
    assert batch_runs.main(["AA", "BB", "CC", "DD", "EE", "-j", "2"]) == 0
    spans = [(float((trace / f"{n}.start").read_text()), float((trace / f"{n}.end").read_text()))
             for n in ("AA", "BB", "CC", "DD", "EE")]
    overlap = max(sum(start <= moment < end for start, end in spans) for moment, _ in spans)
    assert overlap == 2
    assert all(row["status"] == "ok" for row in summary_rows(cwd))


def test_the_cost_cap_starts_nothing_new_and_lets_running_ones_finish(cwd, fake, capsys):
    fake(AA={"sleep": 0.05, "lines": ran_ok(0.6)}, BB={"sleep": 0.6, "lines": ran_ok(0.6)},
         CC={"lines": ran_ok(0.6)}, DD={"lines": ran_ok(0.6)})
    assert batch_runs.main(["AA", "BB", "CC", "DD", "--jobs", "2", "--max-cost", "0.5"]) == 1
    out, err = capsys.readouterr()
    rows = {row["ticker"]: row for row in summary_rows(cwd)}
    # AA ends first and passes the cap; BB was already running and finishes
    assert [rows[t]["status"] for t in ("AA", "BB", "CC", "DD")] == ["ok", "ok", "skipped", "skipped"]
    assert rows["CC"]["error"].startswith("cost cap: $0.60 reported of $0.50")
    assert rows["CC"]["cost_usd"] == "" and rows["CC"]["log"] == ""
    assert "Cost cap reached" in err and "2 skipped" in out


def test_an_unknown_cost_is_said_and_never_counted(cwd, fake, capsys):
    fake(AA={"lines": ran_ok(None)}, BB={"lines": ran_ok(None)})
    assert batch_runs.main(["AA", "BB", "--jobs", "1", "--max-cost", "0.01"]) == 0
    out, err = capsys.readouterr()
    rows = summary_rows(cwd)
    assert [row["cost_usd"] for row in rows] == ["", ""]
    assert all(row["cost_note"].startswith("unknown") for row in rows)
    assert "unknown cost for AA, BB" in out and "the cap does not count it" in err


def test_horizons_and_options_reach_each_run(cwd, fake, tmp_path):
    trace = fake(AA={"lines": ran_ok()}, BB={"lines": ran_ok()})
    watch = tmp_path / "w.txt"
    watch.write_text("AA 1-2w\nBB\n", encoding="utf-8")
    assert batch_runs.main([str(watch), "--research", "none", "--horizon", "6-12m"]) == 0
    assert json.loads((trace / "AA.args").read_text()) == [
        "--research", "none", "--horizon", "6-12m", "--no-open", "--horizon", "1-2w"]   # the line wins
    assert json.loads((trace / "BB.args").read_text())[-1] == "--no-open"
    assert [row["horizon"] for row in summary_rows(cwd)] == ["1-2w", "1-3m"]


def test_summary_csv_columns(cwd, fake):
    fake(AA={"lines": ran_ok(0.75)})
    assert batch_runs.main(["AA"]) == 0
    [path] = list(Path(cwd).glob("runs/batch/*/summary.csv"))
    header = path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == ["ticker", "horizon", "status", "run_folder", "as_of", "price", "currency",
                      "support_from", "support_to", "support_reach", "support_turn",
                      "resistance_from", "resistance_to", "resistance_reach", "resistance_turn",
                      "top_scenario", "top_scenario_p", "confidence", "cost_usd", "cost_note",
                      "duration_s", "error", "log"]
    [row] = summary_rows(cwd)
    assert row["status"] == "ok" and row["cost_usd"] == "0.75" and float(row["duration_s"]) > 0
    assert row["currency"] and row["support_from"] and row["resistance_to"] and row["top_scenario"]
    # the batch folder sits in runs/batch/ and is not mistaken for a run by `zones runs`
    assert path.parent.parent.name == "batch" and not run_pipeline.FOLDER_NAME.match(path.parent.name)


def test_retry_failed_runs_only_what_did_not_succeed(cwd, fake, capsys):
    fake(AA={"lines": ran_ok()}, BB={"lines": ["Error: boom"], "exit": 1, "folder": False},
         CC={"lines": ran_ok()})
    assert batch_runs.main(["AA", "BB", "CC", "--jobs", "1"]) == 1
    [first] = list(Path(cwd).glob("runs/batch/*"))
    fake(BB={"lines": ran_ok()})
    assert batch_runs.main(["--retry-failed", str(first)]) == 0
    second = [path for path in Path(cwd).glob("runs/batch/*") if path != first]
    with open(second[0] / "summary.csv", newline="", encoding="utf-8") as handle:
        assert [(row["ticker"], row["status"]) for row in csv.DictReader(handle)] == [("BB", "ok")]


def test_zones_batch_is_the_command(cwd, fake, capsys):
    fake(AA={"lines": ran_ok()})
    assert run_pipeline.main(["batch", "AA"]) == 0
    assert "1 ticker: 1 ok" in capsys.readouterr().out


def test_a_watchlist_error_runs_nothing(cwd, fake, tmp_path, capsys):
    watch = tmp_path / "w.txt"
    watch.write_text("AA 2y\n", encoding="utf-8")
    assert batch_runs.main([str(watch)]) == 1
    assert "horizon '2y'" in capsys.readouterr().err
    assert not Path("runs").exists()


# ---- the real pipeline in each child ------------------------------------------------

# One `zones run TICKER ...` with only the outside world replaced: build_prompt.py writes the
# fixture input (with this ticker), and `claude` is a script that replays the recorded stream
# and writes the fixture projection for the input's ticker. A ticker starting with NODATA
# fails at step 1, one starting with SLOW times out in step 2.
CHILD = """
import json, sys
sys.path.insert(0, {root!r})
import run_pipeline

def build_input(passthrough):
    ticker = next(word for word in passthrough if not word.startswith("-"))
    if ticker.startswith("NODATA"):
        raise run_pipeline.PipelineError("build_prompt.py failed:\\n    No price data found for " + ticker)
    data = json.load(open({input!r}, encoding="utf-8"))
    data["asset"]["ticker"] = ticker
    text = json.dumps(data)
    return text.encode("utf-8"), data

run_pipeline.build_input = build_input
run_pipeline.CLAUDE = {claude!r}
sys.exit(run_pipeline.main(sys.argv[1:]))
"""

CLAUDE = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
work = args[args.index("--add-dir") + 1]
ticker = json.load(open(os.path.join(work, "input.json"), encoding="utf-8"))["asset"]["ticker"]
if ticker.startswith("SLOW"):
    time.sleep(30)
projection = json.load(open({projection!r}, encoding="utf-8"))
projection["asset"]["ticker"] = ticker
json.dump(projection, open(os.path.join(work, "projection.json"), "w", encoding="utf-8"))
sys.stdout.write(open({stream!r}, encoding="utf-8").read())
"""


@pytest.fixture
def real_runs(tmp_path, monkeypatch, input_path, projection_path):
    claude = tmp_path / "fake-claude"
    claude.write_text(CLAUDE.format(python=sys.executable, projection=str(projection_path),
                                    stream=str(FIXTURES / "claude_stream.jsonl")), encoding="utf-8")
    claude.chmod(0o755)
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(root=str(ROOT), input=str(input_path), claude=str(claude)), encoding="utf-8")
    monkeypatch.setattr(batch_runs, "PIPELINE", child)


def test_real_pipeline_runs_cost_folders_and_failures(cwd, real_runs, capsys):
    code = batch_runs.main(["PETR4.SA", "NODATA1", "SLOWX", "VALE3.SA",
                            "--jobs", "4", "--timeout", "3", "--dpi", "40", "--no-summary"])
    out, err = capsys.readouterr()
    assert code == 1
    rows = {row["ticker"]: row for row in summary_rows(cwd)}
    for ticker in ("PETR4.SA", "VALE3.SA"):
        row = rows[ticker]
        assert row["status"] == "ok" and row["cost_usd"] == "0.75" and row["cost_note"] == ""
        folder = Path(row["run_folder"])
        assert folder == Path("runs") / ticker / FOLDER
        assert {"input.json", "projection.json", "chart.png", "entries.json"} <= {p.name for p in folder.iterdir()}
        assert row["confidence"] in ("low", "medium") and row["top_scenario"]
    assert rows["NODATA1"]["status"] == "failed" and rows["NODATA1"]["cost_usd"] == "0.00"
    assert rows["NODATA1"]["error"] == "build_prompt.py failed: No price data found for NODATA1"
    slow = rows["SLOWX"]
    assert slow["status"] == "failed" and "did not finish within 3 s" in slow["error"]
    assert slow["cost_usd"] == "" and slow["cost_note"].startswith("unknown")
    assert slow["run_folder"].endswith(f"SLOWX/{FOLDER}")      # the folder it made before failing
    assert slow["horizon"] == "1-3m"                            # read from that folder's name
    assert "4 tickers: 2 ok, 2 failed, 0 skipped; reported cost $1.50, plus an unknown cost for SLOWX." in out
