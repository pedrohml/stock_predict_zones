"""The commands of `zones` after a run: list, runs, score, plot, show, entries.

Offline and contained: every test runs in an empty temporary directory with the
saved history in another one, Yahoo is never reached (`relay`, which would start
score_projections.py, is stubbed), no viewer opens and Claude Code is never
started. `rz.py`, `plot_projection.py` and `trade_entries.py` run for real where a
test says so; all three are offline.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import run_pipeline
from test_run_pipeline import PNG_MAGIC, store  # noqa: F401  (autouse: RZ_PROJECTIONS_DIR in a temp folder)

TICKER = "PETR4.SA"     # the ticker of tests/fixtures
NAMES = ["list", "runs", "score", "plot", "show", "entries"]


@pytest.fixture(autouse=True)
def cwd(tmp_path, monkeypatch):
    """An empty current directory, and no real viewer."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_pipeline, "start_viewer",
                        lambda path: pytest.fail(f"a test tried to open {path}"))
    return tmp_path


@pytest.fixture
def make(input_path, projection_path):
    """make(folder, stem=None, projection=True) -> a folder holding the fixture pair."""
    def copy(folder, stem=None, projection=True):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        input_name, projection_name, _ = run_pipeline.file_names(stem)
        shutil.copyfile(input_path, folder / input_name)
        if projection:
            shutil.copyfile(projection_path, folder / projection_name)
        return folder
    return copy


@pytest.fixture
def relayed(monkeypatch):
    """Records the scripts a command would hand the terminal to, and answers with `code`."""
    calls = []

    def fake(command):
        calls.append([str(part) for part in command])
        return calls.code
    calls = type("Calls", (list,), {"code": 0})()
    monkeypatch.setattr(run_pipeline, "relay", fake)
    return calls


def tree(folder):
    """Every file under a folder with its content, to show nothing was added or changed."""
    return {str(path.relative_to(folder)): path.read_bytes()
            for path in sorted(Path(folder).rglob("*")) if path.is_file()}


def main(*argv):
    return run_pipeline.main([str(arg) for arg in argv])


# ---- dispatch -----------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
def test_each_command_word_runs_its_command(monkeypatch, name):
    seen = []
    monkeypatch.setitem(run_pipeline.COMMANDS, name, lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(run_pipeline, "parse", lambda argv=None: pytest.fail("the pipeline parsed a command"))
    assert main(name, "X", "--flag") == 7
    assert seen == [["X", "--flag"]]


def test_the_commands_are_exactly_the_documented_ones():
    assert sorted(run_pipeline.COMMANDS) == sorted(NAMES)
    for name in NAMES:      # the top-level help names each one
        assert f"zones {name} " in run_pipeline.__doc__


@pytest.mark.parametrize("argv", [
    ["PETR4.SA"], ["nvda"], ["BTC-USD", "--horizon", "1-2w"], ["^GSPC"],
    ["LIST"], ["List"], ["Plot", "x"],                  # a command is the exact lowercase word only
    ["--input", "list"], ["--quiet", "list"],           # and only as the first argument
    ["--help"], [],
])
def test_everything_else_is_the_pipelines_unchanged(argv):
    assert run_pipeline.dispatch(argv) == (None, argv)


@pytest.mark.parametrize("argv, left", [
    (["run", "PETR4.SA", "--open"], ["PETR4.SA", "--open"]),
    (["run", "list"], ["list"]),            # the ticker LIST
    (["--", "list"], ["list"]),
    (["run", "run"], ["run"]),              # the ticker RUN
    (["run", "--input", "f.json"], ["--input", "f.json"]),
    (["run"], []),
])
def test_run_and_double_dash_force_the_pipeline(argv, left):
    assert run_pipeline.dispatch(argv) == (None, left)


def test_dispatch_reads_the_real_command_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["zones", "nvda", "--horizon", "1-2w"])
    assert run_pipeline.dispatch() == (None, ["nvda", "--horizon", "1-2w"])


@pytest.mark.parametrize("argv, passed", [
    (["nvda", "--horizon", "1-2w"], ["nvda", "--horizon", "1-2w"]),   # build_prompt.py upper-cases it
    (["run", "list"], ["list"]),
    (["--", "plot"], ["plot"]),
    (["LIST"], ["LIST"]),
])
def test_a_ticker_reaches_step_one_as_typed(monkeypatch, capsys, argv, passed):
    seen = []

    def stop(args, passthrough):
        seen.append(passthrough)
        raise run_pipeline.PipelineError("stopped before step 1")
    monkeypatch.setattr(run_pipeline, "pipeline", stop)
    assert main(*argv) == 1
    assert seen == [passed]
    assert "stopped before step 1" in capsys.readouterr().err


def test_input_still_goes_to_the_pipeline(input_path, projection_path, capsys):
    assert main("--input", input_path, "--projection", projection_path, "--no-entries") == 0
    assert Path(f"runs/{TICKER}").is_dir() and "chart:" in capsys.readouterr().out


def test_help_is_the_pipelines_and_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as stop:
        main("--help")
    out = capsys.readouterr().out
    assert stop.value.code == 0 and "--out-dir" in out
    for name in NAMES:
        assert f"zones {name}" in out


@pytest.mark.parametrize("name", NAMES)
def test_each_command_has_its_own_help(capsys, name):
    with pytest.raises(SystemExit) as stop:
        main(name, "--help")
    assert stop.value.code == 0 and f"usage: zones {name}" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["plot", "show", "entries"])
def test_what_is_required_and_comes_first(capsys, name):
    for argv in ([], ["--horizon", "1-3m", TICKER]):
        with pytest.raises(SystemExit) as stop:
            main(name, *argv)
        assert stop.value.code == 2
    assert "WHAT" in capsys.readouterr().err


# ---- WHAT -> one run ----------------------------------------------------------

def test_a_folder_with_the_pair(make):
    folder = make("anywhere/at/all")
    found = run_pipeline.resolve("anywhere/at/all")
    assert found["folder"] == folder and not found["kept"]
    assert (found["input"], found["projection"]) == (folder / "input.json", folder / "projection.json")
    assert (found["chart"], found["entries"]) == (folder / "chart.png", folder / "entries.json")


def test_a_folder_with_prefixed_files(make):
    folder = make("flat", stem="test4")
    found = run_pipeline.resolve("flat")
    assert [found[key].name for key in ("input", "projection", "chart", "entries")] == [
        "test4_input.json", "test4_projection.json", "test4.png", "test4_entries.json"]
    make("flat", stem="other")
    with pytest.raises(run_pipeline.PipelineError, match=r"several pairs \(other, test4\).*--name"):
        run_pipeline.resolve("flat")
    assert run_pipeline.resolve("flat", name="other")["input"] == folder / "other_input.json"


@pytest.mark.parametrize("typed", [TICKER, TICKER.lower(), "Petr4.sa"])
def test_a_ticker_in_any_case_is_its_latest_run(make, capsys, typed):
    for name in ("2026-11-17T1700_1-3m", "2026-11-18T1515_1-3m", "2026-11-18T1515_1-3m_2",
                 "2026-11-18T1515_1-3m_10", "2026-11-18T1000_1-2w"):
        make(f"runs/{TICKER}/{name}")
    make("runs/OTHER/2026-12-01T1000_1-3m")
    make(f"runs/{TICKER}/2026-12-01T1000_1-3m", projection=False)     # a failed run: no pair
    found = run_pipeline.resolve(typed)
    assert found["folder"] == Path(f"runs/{TICKER}/2026-11-18T1515_1-3m_10")   # _10 is after _2
    assert f"Using runs/{TICKER}/2026-11-18T1515_1-3m_10" in capsys.readouterr().err


def test_horizon_narrows_a_ticker(make):
    for name in ("2026-11-17T1700_1-2w", "2026-11-17T1700_1-2w_2", "2026-11-18T1515_1-3m"):
        make(f"runs/{TICKER}/{name}")
    assert run_pipeline.resolve(TICKER, "1-2w")["folder"].name == "2026-11-17T1700_1-2w_2"
    assert run_pipeline.resolve(TICKER, "1-3m")["folder"].name == "2026-11-18T1515_1-3m"
    with pytest.raises(run_pipeline.PipelineError, match="with horizon 6-12m"):
        run_pipeline.resolve(TICKER, "6-12m")


def test_runs_here_come_before_the_history(make, store):
    make(store / TICKER / "2026-11-20T1000_1-3m")       # newer, but the history is the second place
    make(store / TICKER / "2026-11-10T1000_1-2w")
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    found = run_pipeline.resolve(TICKER)
    assert found["folder"] == run and not found["kept"]
    found = run_pipeline.resolve(TICKER.lower(), "1-2w")   # no such run: the history has one
    assert found["folder"] == store / TICKER / "2026-11-10T1000_1-2w" and found["kept"]
    assert found["label"] == f"{TICKER}_2026-11-10T1000_1-2w"


def test_the_default_history_is_projections_here(make, monkeypatch):
    monkeypatch.delenv("RZ_PROJECTIONS_DIR")
    kept = make(f"projections/{TICKER}/2026-11-18T1515_1-3m")
    found = run_pipeline.resolve(TICKER)
    assert found["folder"] == kept and found["kept"]
    assert run_pipeline.resolve(kept)["kept"]            # also when given as a folder


def test_a_ticker_folder_name_is_the_one_both_writers_use(make):
    make("runs/_GSPC/2026-11-18T1515_1-3m")
    assert run_pipeline.resolve("^gspc")["folder"] == Path("runs/_GSPC/2026-11-18T1515_1-3m")


def test_a_folder_of_runs_means_the_latest_inside(make):
    make(f"runs/{TICKER}/2026-11-17T1700_1-3m")
    make(f"runs/{TICKER}/2026-11-18T1515_1-2w")
    assert run_pipeline.resolve(f"runs/{TICKER}")["folder"].name == "2026-11-18T1515_1-2w"
    assert run_pipeline.resolve(f"runs/{TICKER}", "1-3m")["folder"].name == "2026-11-17T1700_1-3m"
    Path("empty").mkdir()
    with pytest.raises(run_pipeline.PipelineError, match="No input.json and projection.json in empty"):
        run_pipeline.resolve("empty")


@pytest.mark.parametrize("name", ["plot", "show", "entries"])
def test_nothing_found_is_one_error_naming_both_places(cwd, store, capsys, monkeypatch, name):
    monkeypatch.setattr(run_pipeline, "ROOT", cwd)
    (cwd / "show_run.py").write_text("", encoding="utf-8")
    assert main(name, "nvda") == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("Error: Nothing found for 'nvda'")
    assert str(cwd.resolve() / "runs" / "NVDA") in err and str(store.resolve() / "NVDA") in err


# ---- list ---------------------------------------------------------------------

LISTED = ("2026-11-17T17:00-03:00 PETR4.SA 1-3m 42.1 to 2027-02-12 open kept/PETR4.SA/a\n"
          "2026-11-18T16:00-05:00 NVDA 1-2w 140.5 to 2026-12-02 ended kept/NVDA/b\n")


@pytest.fixture
def listing(monkeypatch):
    """rz.py list, answered from here."""
    calls = []

    def fake_run(command, timeout=None):
        calls.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, calls.code, calls.out, calls.err)
    calls = type("Calls", (list,), {"code": 0, "out": LISTED, "err": ""})()
    monkeypatch.setattr(run_pipeline, "run", fake_run)
    return calls


def test_list_is_rz_list(listing, capsys):
    assert main("list") == 0
    assert listing == [[sys.executable, str(run_pipeline.RZ), "list"]]
    assert capsys.readouterr().out == LISTED


@pytest.mark.parametrize("typed", ["NVDA", "nvda"])
def test_list_filters_one_ticker_itself(listing, capsys, typed):
    assert main("list", typed) == 0
    assert listing == [[sys.executable, str(run_pipeline.RZ), "list"]]   # rz.py list has no filter
    assert capsys.readouterr().out == LISTED.splitlines(keepends=True)[1]


def test_list_with_nothing_says_where_it_looked(listing, store, capsys):
    assert main("list", "AAPL") == 0
    printed = capsys.readouterr()
    assert printed.out == "" and f"No saved projections under {store.resolve()} for AAPL." in printed.err


def test_list_relays_a_failure(listing, capsys):
    listing.code, listing.out, listing.err = 1, "", "Traceback ...\nKeyError: 'as_of'\n"
    assert main("list") == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "rz.py list failed" in printed.err and "KeyError: 'as_of'" in printed.err


def test_list_for_real_shows_a_saved_projection(input_path, projection_path, store, capsys):
    done = run_pipeline.run([sys.executable, run_pipeline.RZ, "save", input_path, projection_path])
    assert done.returncode == 0, done.stdout + done.stderr
    assert main("list", TICKER.lower()) == 0
    line = capsys.readouterr().out.splitlines()
    assert len(line) == 1 and line[0].split()[1] == TICKER and line[0].endswith(str(store / TICKER / "2026-11-17T1736_1-3m"))


# ---- runs ---------------------------------------------------------------------

def test_runs_lists_the_folders_here_newest_last(make, capsys):
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m_2")
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    make("runs/NVDA/2026-11-17T1600_1-2w", projection=False)      # a failed run shows what it has
    make("runs/NVDA/notes")                                        # not a run folder
    assert main("runs") == 0
    assert capsys.readouterr().out.splitlines() == [
        "2026-11-17T1600 NVDA 1-2w runs/NVDA/2026-11-17T1600_1-2w (input.json)",
        f"2026-11-18T1515 {TICKER} 1-3m runs/{TICKER}/2026-11-18T1515_1-3m (input.json projection.json)",
        f"2026-11-18T1515 {TICKER} 1-3m runs/{TICKER}/2026-11-18T1515_1-3m_2 (input.json projection.json)"]
    assert main("runs", "nvda") == 0
    assert capsys.readouterr().out.splitlines() == [
        "2026-11-17T1600 NVDA 1-2w runs/NVDA/2026-11-17T1600_1-2w (input.json)"]


def test_runs_with_nothing_says_where_it_looked(cwd, store, make, capsys):
    make(store / TICKER / "2026-11-18T1515_1-3m")       # the history is not a run
    assert main("runs") == 0
    printed = capsys.readouterr()
    assert printed.out == "" and f"No runs under {cwd.resolve() / 'runs'}." in printed.err
    assert main("runs", "aapl") == 0
    assert "for AAPL." in capsys.readouterr().err


# ---- score --------------------------------------------------------------------

SCORE = str(run_pipeline.ROOT / "score_projections.py")


@pytest.mark.parametrize("argv, passed", [
    ([], []),
    (["petr4.sa"], ["--ticker", "petr4.sa"]),
    (["NVDA", "--json"], ["--ticker", "NVDA", "--json"]),
    (["--json"], ["--json"]),
    (["--ticker", "NVDA"], ["--ticker", "NVDA"]),
])
def test_score_is_score_projections(relayed, argv, passed):
    assert main("score", *argv) == 0
    assert relayed == [[sys.executable, SCORE, *passed]]


def test_score_takes_a_folder_as_a_folder(relayed, make):
    make(f"kept/{TICKER}/2026-11-18T1515_1-3m")
    assert main("score", f"kept/{TICKER}/2026-11-18T1515_1-3m", "--json") == 0
    assert relayed == [[sys.executable, SCORE, f"kept/{TICKER}/2026-11-18T1515_1-3m", "--json"]]


def test_score_relays_the_exit_code(relayed):
    relayed.code = 1
    assert main("score") == 1


# ---- plot ---------------------------------------------------------------------

PLOT = str(run_pipeline.PLOT)


def test_plot_redraws_the_run_folders_own_chart(relayed, make, capsys):
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert main("plot", TICKER.lower()) == 0
    assert relayed == [[sys.executable, PLOT, str(run / "input.json"), str(run / "projection.json"),
                        "-o", str(run / "chart.png")]]
    assert capsys.readouterr().out == f"chart:      {run / 'chart.png'}\n"


def test_plot_passes_its_other_options_on(relayed, make):
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert main("plot", TICKER, "--theme", "dark", "--lang", "pt", "-o", "x.png", "--no-check",
                "--horizon", "1-3m", "--dpi", "80") == 0
    assert relayed == [[sys.executable, PLOT, str(run / "input.json"), str(run / "projection.json"),
                        "-o", "x.png", "--theme", "dark", "--lang", "pt", "--no-check", "--dpi", "80"]]


def test_plot_of_prefixed_files_writes_the_prefixed_chart(relayed, make):
    make("flat", stem="test4")
    assert main("plot", "flat") == 0
    assert relayed[0][2:] == ["flat/test4_input.json", "flat/test4_projection.json", "-o", "flat/test4.png"]


def test_plot_of_a_saved_projection_writes_here_not_there(relayed, make, store, capsys):
    kept = make(store / TICKER / "2026-11-18T1515_1-3m")
    for what in (TICKER, kept):         # found by ticker, or given as a folder
        relayed.clear()
        assert main("plot", what) == 0
        assert relayed[0][4:] == ["-o", f"{TICKER}_2026-11-18T1515_1-3m.png"]
    assert f"chart:      {TICKER}_2026-11-18T1515_1-3m.png" in capsys.readouterr().out


def test_plot_refuses_to_write_into_the_history(relayed, make, store, capsys):
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    kept = make(store / TICKER / "2026-11-18T1515_1-3m")
    assert main("plot", TICKER, "-o", kept / "chart.png") == 1
    assert main("plot", TICKER, "--output", store / "new.png") == 1
    assert relayed == [] and "append-only" in capsys.readouterr().err


def test_plot_relays_a_failure_and_opens_nothing(relayed, make, capsys):
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    relayed.code = 1
    assert main("plot", TICKER, "--open") == 1       # the autouse viewer would fail the test
    assert capsys.readouterr().out == ""


def test_plot_open_shows_the_new_chart(relayed, make, monkeypatch):
    opened = []
    monkeypatch.setattr(run_pipeline, "start_viewer", opened.append)
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert main("plot", TICKER, "--open") == 0
    assert opened == [run / "chart.png"]
    assert main("plot", TICKER) == 0 and len(opened) == 1


def test_plot_for_real(make, store, capfd):
    """plot_projection.py itself, once for a run and once for a saved projection."""
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert main("plot", run, "--theme", "dark", "--dpi", "50") == 0
    assert (run / "chart.png").read_bytes().startswith(PNG_MAGIC)
    shutil.rmtree("runs")
    kept = make(store / TICKER / "2026-11-18T1515_1-3m")
    before = tree(store)
    assert main("plot", TICKER.lower(), "--dpi", "50") == 0
    assert Path(f"{TICKER}_2026-11-18T1515_1-3m.png").read_bytes().startswith(PNG_MAGIC)
    assert tree(store) == before and sorted(path.name for path in kept.iterdir()) == ["input.json", "projection.json"]
    assert main("plot", TICKER, "--theme", "neon") == 2          # plot_projection.py's own usage error
    assert "--theme" in capfd.readouterr().err


# ---- show ---------------------------------------------------------------------

STAND_IN = "import sys\nprint('\\n'.join(sys.argv[1:]))\nsys.exit(3 if '--fail' in sys.argv else 0)\n"


def test_show_without_the_script_is_one_line(make, capsys):
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert not (run_pipeline.ROOT / "show_run.py").exists() or pytest.skip("show_run.py exists here")
    assert main("show", TICKER) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "The summary script is not available" in err


def test_show_runs_show_run_with_the_files_of_the_run(make, tmp_path_factory, monkeypatch, capfd):
    root = tmp_path_factory.mktemp("root")
    (root / "show_run.py").write_text(STAND_IN, encoding="utf-8")
    monkeypatch.setattr(run_pipeline, "ROOT", root)
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    assert main("show", TICKER.lower(), "--lang", "pt", "--no-color") == 0
    assert capfd.readouterr().out.splitlines() == [
        str(run / "input.json"), str(run / "projection.json"), "--lang", "pt", "--no-color"]
    (run / "entries.json").write_text("{}", encoding="utf-8")      # the setups too, when the run has them
    assert main("show", run, "--fail") == 3                         # and the script's exit code
    assert capfd.readouterr().out.splitlines() == [
        str(run / "input.json"), str(run / "projection.json"), str(run / "entries.json"), "--fail"]


def test_show_command_line(relayed, make, monkeypatch, cwd):
    (cwd / "show_run.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(run_pipeline, "ROOT", cwd)
    make("flat", stem="test4")
    assert main("show", "flat", "--width", "100") == 0
    assert relayed == [[sys.executable, str(cwd / "show_run.py"),
                        "flat/test4_input.json", "flat/test4_projection.json", "--width", "100"]]


# ---- entries ------------------------------------------------------------------

def checks(source, projection, entries):
    done = run_pipeline.run([sys.executable, run_pipeline.ENTRIES, "check", source, projection, entries])
    return done.stdout.strip()


def test_entries_for_real_replaces_the_run_folders_file(make, capsys):
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    (run / "entries.json").write_text("stale", encoding="utf-8")
    assert main("entries", TICKER.lower()) == 0
    assert capsys.readouterr().out == f"entries:    {run / 'entries.json'}\n"
    assert json.loads((run / "entries.json").read_text(encoding="utf-8"))["task"] == "trade_entries"
    assert checks(run / "input.json", run / "projection.json", run / "entries.json") == "OK"


def test_entries_of_a_saved_projection_are_written_here(make, store, capsys):
    kept = make(store / TICKER / "2026-11-18T1515_1-3m")
    (kept / "entries.json").write_text("as saved", encoding="utf-8")
    before = tree(store)
    assert main("entries", TICKER) == 0
    here = Path(f"{TICKER}_2026-11-18T1515_1-3m_entries.json")
    assert checks(kept / "input.json", kept / "projection.json", here) == "OK"
    assert main("entries", kept, "-o", "mine.json") == 0 and Path("mine.json").is_file()
    assert main("entries", TICKER, "-o", kept / "entries.json") == 1
    assert "append-only" in capsys.readouterr().err
    assert tree(store) == before


def test_entries_runs_build_then_check(make, monkeypatch, entries_data):
    run, calls = make(f"runs/{TICKER}/2026-11-18T1515_1-3m"), []

    def fake_run(command, timeout=None):
        command = [str(part) for part in command]
        calls.append(command)
        if command[2] == "build":
            Path(command[-1]).write_text(json.dumps(entries_data), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "OK\n" if command[2] == "check" else "", "")
    monkeypatch.setattr(run_pipeline, "run", fake_run)
    assert main("entries", run) == 0
    pair = [str(run / "input.json"), str(run / "projection.json")]
    draft = calls[0][-1]
    assert calls == [[sys.executable, str(run_pipeline.ENTRIES), "build", *pair, "-o", draft],
                     [sys.executable, str(run_pipeline.ENTRIES), "check", *pair, draft]]
    assert json.loads((run / "entries.json").read_text(encoding="utf-8")) == entries_data
    assert not Path(draft).exists()                 # built and checked away from the run folder


@pytest.mark.parametrize("failing, code", [("build", 2), ("check", 1)])
def test_entries_relays_a_failure_and_keeps_the_old_file(make, monkeypatch, capsys, failing, code):
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    (run / "entries.json").write_text("earlier", encoding="utf-8")

    def fake_run(command, timeout=None):
        command = [str(part) for part in command]
        bad = command[2] == failing
        if command[2] == "build":
            Path(command[-1]).write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, code if bad else 0,
                                           "entries[0].stop.price: wrong\n" if bad else "OK\n", "")
    monkeypatch.setattr(run_pipeline, "run", fake_run)
    assert main("entries", TICKER) == code
    printed = capsys.readouterr()
    assert printed.out == "" and f"trade_entries.py {failing} failed" in printed.err
    assert "stop.price: wrong" in printed.err
    assert (run / "entries.json").read_text(encoding="utf-8") == "earlier"


def test_entries_that_cannot_be_made_write_nothing(make, monkeypatch, capsys):
    run = make(f"runs/{TICKER}/2026-11-18T1515_1-3m")

    def fake_run(command, timeout=None):
        if command[2] == "build":
            Path(command[-1]).write_text('{"task": "trade_entries", "error": "no zone is near"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "OK\n", "")
    monkeypatch.setattr(run_pipeline, "run", fake_run)
    assert main("entries", run) == 0
    assert "No setups: no zone is near" in capsys.readouterr().err
    assert not (run / "entries.json").exists()


def test_entries_takes_no_other_options(make, capsys):
    make(f"runs/{TICKER}/2026-11-18T1515_1-3m")
    with pytest.raises(SystemExit) as stop:
        main("entries", TICKER, "--theme", "dark")
    assert stop.value.code == 2 and "unrecognized arguments: --theme dark" in capsys.readouterr().err


# ---- away from the repository -------------------------------------------------

@pytest.mark.parametrize("argv", [["list"], ["score"], ["plot", "x"], ["entries", "x"]])
def test_a_copy_installed_away_from_the_repository_says_so(cwd, make, monkeypatch, capsys, argv):
    make("x")
    for name in ("RZ", "PLOT", "ENTRIES", "ROOT"):
        monkeypatch.setattr(run_pipeline, name, cwd / "missing" / name)
    assert main(*argv) == 1
    assert "works only from the repository checkout" in capsys.readouterr().err
