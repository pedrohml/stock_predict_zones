"""run_pipeline.py: where the files go, --open and the `zones` command.

Every test runs in an empty temporary directory, because the default output is
a `runs/` folder under the current one, and with a viewer that fails the test
if anything tries to open a file for real. Yahoo and Claude Code are stubbed
with the fixtures of test_run_pipeline.py.
"""
import importlib
import importlib.metadata
import shutil
import tomllib
from pathlib import Path

import pytest

import run_pipeline
from conftest import ROOT
from test_run_pipeline import PNG_MAGIC, calls, store, stub_build, stub_claude  # noqa: F401
from test_run_pipeline import run_main as run_everything

PLAIN = ["chart.png", "input.json", "projection.json"]
MADE = sorted(PLAIN + ["provenance.json"])   # a run that made its projection also says how


@pytest.fixture(autouse=True)
def cwd(tmp_path, monkeypatch):
    """An empty current directory, and no real viewer."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_pipeline, "start_viewer",
                        lambda path: pytest.fail(f"a test tried to open {path}"))
    return tmp_path


@pytest.fixture
def viewer(monkeypatch):
    """Records what --open would have opened."""
    opened = []
    monkeypatch.setattr(run_pipeline, "start_viewer", opened.append)
    return opened


def run_main(*argv):
    """These tests are about where the input, the projection and the chart go, so they
    stop at the chart. The trade-setups step is placed by the two tests at the end."""
    return run_everything(*argv, "--no-entries")


def names(folder):
    return sorted(path.name for path in Path(folder).iterdir())


def output_paths(capsys):
    """{'input': Path, 'projection': Path, 'chart': Path} from the lines main() prints."""
    lines = (line.split(":", 1) for line in capsys.readouterr().out.splitlines()
             if line.startswith(("input:", "projection:", "chart:", "entries:", "saved:")))   # not the summary
    return {key: Path(value.strip()) for key, value in lines}


# ---- the default: one new folder per run ---------------------------------------

def test_default_is_a_run_folder_named_like_rz_save(cwd, input_data, store, stub_build, stub_claude, capsys):
    stub_claude()
    assert run_main("PETR4.SA", "--dpi", "50") == 0
    assert (input_data["now"]["time"], input_data["horizon"]) == ("2026-11-17T17:36-03:00", "1-3m")
    folder = Path("runs") / "PETR4.SA" / "2026-11-17T1736_1-3m"       # relative to the current directory
    assert names(cwd) == ["runs"] and names(folder) == MADE
    assert (folder / "chart.png").read_bytes().startswith(PNG_MAGIC)
    printed = output_paths(capsys)
    assert [printed[key] for key in ("input", "projection", "chart")] == [
        folder / "input.json", folder / "projection.json", folder / "chart.png"]
    # the same folder name as the append-only history, which stays a separate place
    assert printed["saved"] == store / "PETR4.SA" / "2026-11-17T1736_1-3m"
    assert names(printed["saved"]) == ["input.json", "projection.json", "provenance.json"]
    assert (printed["saved"] / "provenance.json").read_bytes() == (folder / "provenance.json").read_bytes()


def test_a_repeated_run_never_overwrites(stub_build, stub_claude):
    stub_claude()
    base = Path("runs/PETR4.SA")
    assert run_main("PETR4.SA", "--dpi", "50") == 0
    first = base / "2026-11-17T1736_1-3m"
    (first / "chart.png").write_bytes(b"the first run's chart")
    assert run_main("PETR4.SA", "--dpi", "50") == 0
    assert run_main("PETR4.SA", "--dpi", "50") == 0
    assert names(base) == ["2026-11-17T1736_1-3m", "2026-11-17T1736_1-3m_2", "2026-11-17T1736_1-3m_3"]
    assert (first / "chart.png").read_bytes() == b"the first run's chart"
    assert all(names(folder) == MADE for folder in base.iterdir())


def test_another_time_or_horizon_is_another_folder(input_data, write_json, stub_claude):
    stub_claude()
    input_data["horizon"] = "1-2w"
    assert run_main("--input", write_json("a.json", input_data), "--no-save", "--dpi", "50") == 0
    assert names("runs/PETR4.SA") == ["2026-11-17T1736_1-2w"]


def test_input_alone_gets_a_complete_folder(cwd, input_path, stub_build, stub_claude, calls):
    stub_claude()
    shutil.copyfile(input_path, "mine.json")
    assert run_main("--input", "mine.json", "--dpi", "50") == 0      # a relative path, against the cwd
    assert calls["build"] == [] and len(calls["claude"]) == 1
    folder = Path("runs/PETR4.SA/2026-11-17T1736_1-3m")
    assert names(folder) == MADE
    assert (folder / "input.json").read_bytes() == input_path.read_bytes() == (cwd / "mine.json").read_bytes()


def test_plot_again_gets_a_new_folder_and_keeps_nothing(input_path, projection_path, store, monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "call_claude", lambda *a, **k: pytest.fail("Claude must not run"))
    for _ in range(2):
        assert run_main("--input", input_path, "--projection", projection_path, "--dpi", "50") == 0
    assert names("runs/PETR4.SA") == ["2026-11-17T1736_1-3m", "2026-11-17T1736_1-3m_2"]
    folder = Path("runs/PETR4.SA/2026-11-17T1736_1-3m_2")
    assert names(folder) == PLAIN
    assert (folder / "projection.json").read_bytes() == projection_path.read_bytes()
    assert output_paths(capsys)["chart"] == folder / "chart.png"
    assert not store.exists()                                         # not a new projection


def test_plot_again_into_a_named_chart_makes_no_folder(cwd, input_path, projection_path, capsys):
    assert run_main("--input", input_path, "--projection", projection_path,
                    "--chart", "dark.svg", "--theme", "dark") == 0
    assert names(cwd) == ["dark.svg"]
    printed = output_paths(capsys)
    assert (printed["input"], printed["projection"]) == (input_path, projection_path)


def test_name_prefixes_the_files_in_a_run_folder(stub_build, stub_claude):
    stub_claude()
    assert run_main("PETR4.SA", "--name", "run7", "--dpi", "50") == 0
    assert names("runs/PETR4.SA/2026-11-17T1736_1-3m") == ["run7.png", "run7_input.json", "run7_projection.json",
                                                                "run7_provenance.json"]


def test_chart_option_moves_only_the_chart(cwd, stub_build, stub_claude):
    stub_claude()
    assert run_main("PETR4.SA", "--chart", "x.svg") == 0
    assert names("runs/PETR4.SA/2026-11-17T1736_1-3m") == ["input.json", "projection.json", "provenance.json"]
    assert "<svg" in (cwd / "x.svg").read_text(encoding="utf-8")


# ---- --out-dir: the exact folder, as before ------------------------------------

def test_out_dir_is_flat_named_by_ticker_and_relative_to_the_cwd(cwd, stub_build, stub_claude):
    stub_claude()
    for _ in range(2):                                               # a second run replaces the first
        assert run_main("PETR4.SA", "--out-dir", "out", "--dpi", "50") == 0
    assert names(cwd) == ["out"]                                     # no runs/
    assert names("out") == ["petr4.png", "petr4_input.json", "petr4_projection.json", "petr4_provenance.json"]


def test_out_dir_dot_is_the_old_default(cwd, stub_build, stub_claude):
    stub_claude()
    assert run_main("PETR4.SA", "--out-dir", ".", "--name", "x", "--dpi", "50") == 0
    assert names(cwd) == ["x.png", "x_input.json", "x_projection.json", "x_provenance.json"]


def test_out_dir_leaves_given_files_where_they_are(cwd, input_path, projection_path, capsys):
    assert run_main("--input", input_path, "--projection", projection_path,
                    "--out-dir", "out", "--dpi", "50") == 0
    assert names("out") == ["petr4.png"]
    assert output_paths(capsys)["input"] == input_path


# ---- nothing is left behind by a run that could not start ----------------------

def test_failures_before_the_run_create_nothing(cwd, input_path, input_data, write_json, monkeypatch, capsys):
    assert run_main("--input", "missing.json") == 1
    assert run_main("--input", input_path, "--projection", "missing.json") == 1
    monkeypatch.setattr(run_pipeline, "run", lambda command, timeout=None:
                        run_pipeline.subprocess.CompletedProcess(command, 1, "", "No data for 'XXXX'.\n"))
    assert run_main("XXXX") == 1
    assert run_main("XXXX", "--out-dir", "out") == 1
    assert names(cwd) == []
    assert "Traceback" not in capsys.readouterr().err


def test_a_failed_step_two_keeps_the_input_for_another_try(stub_build, stub_claude, capsys):
    stub_claude(code=1, envelope={"result": "Usage limit reached", "is_error": True}, write=False)
    assert run_main("PETR4.SA") == 1
    assert names("runs/PETR4.SA/2026-11-17T1736_1-3m") == ["input.json"]


@pytest.mark.parametrize("damage", [
    lambda d: d["now"].pop("time"), lambda d: d["now"].update(time="yesterday"),
    lambda d: d.pop("horizon"), lambda d: d["asset"].update(ticker=".."),
])
def test_an_input_that_cannot_name_a_folder(cwd, input_data, write_json, damage, capsys):
    damage(input_data)
    path = write_json("odd.json", input_data)
    assert run_main("--input", path) == 1
    assert "--out-dir" in capsys.readouterr().err
    assert names(cwd) == ["odd.json"]


@pytest.mark.parametrize("ticker, folder", [("^GSPC", "_GSPC"), ("btc-usd", "BTC-USD"), ("../../x", "_.._X")])
def test_run_folder_stays_under_its_base(tmp_path, input_data, ticker, folder):
    input_data["asset"]["ticker"] = ticker
    made = run_pipeline.run_folder(input_data, tmp_path / "runs")
    assert made == tmp_path / "runs" / folder / "2026-11-17T1736_1-3m" and made.is_dir()


def test_a_copy_installed_away_from_the_repository_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "BUILD", tmp_path / "build_prompt.py")
    assert run_main("PETR4.SA") == 1
    assert "--editable" in capsys.readouterr().err


# ---- --open --------------------------------------------------------------------

def test_open_shows_the_chart_after_a_successful_run(input_path, projection_path, viewer, capsys):
    assert run_main("--input", input_path, "--projection", projection_path, "--dpi", "50", "--open") == 0
    assert viewer == [output_paths(capsys)["chart"]] and viewer[0].is_file()


def test_nothing_opens_unless_asked_or_when_the_run_fails(input_path, projection_path, viewer):
    assert run_main("--input", input_path, "--projection", projection_path, "--dpi", "50") == 0
    assert run_main("--input", input_path, "--projection", "missing.json", "--open") == 1
    assert viewer == []


@pytest.mark.parametrize("error", [FileNotFoundError("xdg-open"), OSError("no display"), RuntimeError("odd")])
def test_a_viewer_that_cannot_start_only_warns(input_path, projection_path, monkeypatch, capsys, error):
    def broken(path):
        raise error
    monkeypatch.setattr(run_pipeline, "start_viewer", broken)
    assert run_main("--input", input_path, "--projection", projection_path, "--dpi", "50", "--open") == 0
    captured = capsys.readouterr()
    assert "chart:" in captured.out
    [warning] = [line for line in captured.err.splitlines() if "Warning" in line]
    assert "could not open" in warning and str(error) in warning


@pytest.mark.parametrize("platform, program", [("darwin", "open"), ("linux", "xdg-open")])
def test_start_viewer_uses_the_platform_command(monkeypatch, platform, program):
    monkeypatch.undo()                                               # the real start_viewer, a fake Popen
    started = []
    monkeypatch.setattr(run_pipeline.sys, "platform", platform)
    monkeypatch.setattr(run_pipeline.subprocess, "Popen", lambda command, **kwargs: started.append(command))
    run_pipeline.start_viewer(Path("some") / "chart.png")
    assert started == [[program, str(Path("some") / "chart.png")]]


def test_start_viewer_on_windows(monkeypatch):
    monkeypatch.undo()
    started = []
    monkeypatch.setattr(run_pipeline.sys, "platform", "win32")
    monkeypatch.setattr(run_pipeline.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(run_pipeline.subprocess, "Popen", lambda *a, **k: pytest.fail("not on Windows"))
    run_pipeline.start_viewer("chart.png")
    assert started == ["chart.png"]


# ---- the `zones` command -------------------------------------------------------

def test_zones_entry_point_is_main(capsys):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    module, _, function = project["project"]["scripts"]["zones"].partition(":")
    target = getattr(importlib.import_module(module), function)
    assert target is run_pipeline.main
    with pytest.raises(SystemExit) as stop:
        target(["--help"])
    assert stop.value.code == 0 and "--open" in capsys.readouterr().out
    assert target([]) == 1                                           # no arguments: a usage error, not a crash


def test_zones_is_installed_from_this_checkout():
    """`uv sync` installs the project editable, so the command runs these files."""
    name = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    try:
        scripts = importlib.metadata.distribution(name).entry_points
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("the project is not installed in this environment (run `uv sync`)")
    assert [(script.name, script.value) for script in scripts] == [("zones", "run_pipeline:main")]
    assert Path(run_pipeline.__file__).resolve() == ROOT / "run_pipeline.py"
    assert run_pipeline.RZ.is_file() and run_pipeline.BUILD.is_file() and run_pipeline.PLOT.is_file()


# ---- the trade-setups file -------------------------------------------------------

def test_entries_go_into_the_run_folder_with_a_plain_name(cwd, stub_build, stub_claude):
    stub_claude()
    assert run_everything("PETR4.SA", "--dpi", "50") == 0
    assert names(Path("runs/PETR4.SA/2026-11-17T1736_1-3m")) == sorted(MADE + ["entries.json"])


def test_plot_again_with_entries_needs_a_folder_even_with_a_named_chart(cwd, input_path, projection_path,
                                                                        stub_claude):
    stub_claude()
    assert run_everything("--input", input_path, "--projection", projection_path,
                          "--chart", "mine.png", "--dpi", "50") == 0
    folder = Path("runs/PETR4.SA/2026-11-17T1736_1-3m")
    assert names(folder) == ["entries.json", "input.json", "projection.json"] and Path("mine.png").is_file()
