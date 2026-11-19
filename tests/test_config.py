"""run_pipeline.py: personal defaults from config files.

Precedence under test: command line > environment variable > project file
(./zones.toml) > user file > built-in default. Every test runs in an empty
temporary directory with a temporary user file; conftest.py's `no_real_config`
keeps the developer's own files out of every test in the suite.
"""
import os
import tomllib
from pathlib import Path

import pytest

import build_prompt
import plot_projection
import run_pipeline
from test_run_pipeline import calls, option, stub_build, stub_claude  # noqa: F401
from test_run_pipeline import run_main

HISTORY = run_pipeline.HISTORY_ENV


@pytest.fixture(autouse=True)
def cwd(tmp_path, monkeypatch):
    """An empty current directory, no history variable and no real viewer."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv(HISTORY, raising=False)
    monkeypatch.setattr(run_pipeline, "start_viewer",
                        lambda path: pytest.fail(f"a test tried to open {path}"))
    return work


@pytest.fixture
def user(tmp_path, monkeypatch):
    """user(text) writes the user file, in a folder that is not the current one."""
    path = tmp_path / "home" / "cfg" / "config.toml"
    monkeypatch.setenv(run_pipeline.CONFIG_ENV, str(path))

    def write(text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    return write


@pytest.fixture
def project(cwd, monkeypatch):
    """project(text) writes ./zones.toml and turns its lookup back on."""
    def write(text):
        monkeypatch.setattr(run_pipeline, "PROJECT_CONFIG", "zones.toml")
        path = cwd / "zones.toml"
        path.write_text(text, encoding="utf-8")
        return path
    return write


def fails(capsys, *argv):
    """The one error line of a command line that must exit 1."""
    with pytest.raises(SystemExit) as stopped:
        run_main(*argv)
    assert stopped.value.code == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("Error: ") and "\n" not in err and "Traceback" not in err
    return err


def shown(capsys, *argv):
    """{setting: (value, source)} from --show-config, and the whole text."""
    with pytest.raises(SystemExit) as stopped:
        run_main(*argv, "--show-config")
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    rows = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if line.startswith("  ") and parts[0] in run_pipeline.SETTINGS:
            value, _, source = parts[1].rpartition("  ")
            rows[parts[0]] = (value.strip(), source.strip())
    return rows, text


# ---- isolation ------------------------------------------------------------------

def test_the_suite_never_sees_a_real_config_file():
    """What conftest.py sets up for every test: a user file that does not exist, under
    pytest's own temp folder, and no ./zones.toml lookup."""
    path = run_pipeline.user_config()
    assert not path.exists() and Path.home() not in path.parents
    assert run_pipeline.PROJECT_CONFIG is None
    assert run_pipeline.config_files() == [("user file", path)]


def test_without_any_file_nothing_changes(capsys):
    args, rest = run_pipeline.parse(["PETR4.SA", "--horizon", "1-2w"])
    assert rest == ["PETR4.SA", "--horizon", "1-2w"]
    assert (args.timeout, args.model, args.quiet, args.open) == (1800, None, False, False)
    assert (args.no_entries, args.entries_by_claude, args.no_save) == (False, False, False)
    assert (args.theme, args.lang, args.candles, args.dpi) == (None, None, None, None)
    assert args.runs_dir == "runs" and HISTORY not in os.environ
    assert capsys.readouterr().err == ""


def test_the_old_switches_still_mean_the_same():
    args, _ = run_pipeline.parse(["X", "--no-entries", "--no-save", "--quiet", "--open",
                                  "--entries-by-claude"])
    assert (args.no_entries, args.no_save, args.quiet, args.open, args.entries_by_claude) == (True,) * 5


# ---- where the user file is -----------------------------------------------------

def test_user_file_location(tmp_path, monkeypatch):
    monkeypatch.delenv(run_pipeline.CONFIG_ENV)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert run_pipeline.user_config() == tmp_path / "home" / ".config" / "zones" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert run_pipeline.user_config() == tmp_path / "xdg" / "zones" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/is/ignored")
    assert run_pipeline.user_config() == tmp_path / "home" / ".config" / "zones" / "config.toml"
    monkeypatch.setenv(run_pipeline.CONFIG_ENV, str(tmp_path / "named.toml"))
    assert run_pipeline.user_config() == tmp_path / "named.toml"


def test_the_xdg_file_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv(run_pipeline.CONFIG_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "zones").mkdir(parents=True)
    (tmp_path / "xdg" / "zones" / "config.toml").write_text('language = "pt"\n', encoding="utf-8")
    assert run_pipeline.parse(["NVDA"])[1] == ["--language=pt", "NVDA"]


# ---- precedence -----------------------------------------------------------------

def test_precedence_of_a_step1_option(user, project):
    rest = lambda *argv: run_pipeline.parse(list(argv))[1]
    assert rest("NVDA") == ["NVDA"]                                      # built-in default: nothing added
    user('horizon = "1-2w"\n')
    assert rest("NVDA") == ["--horizon=1-2w", "NVDA"]                    # user file
    project('horizon = "today"\n')
    assert rest("NVDA") == ["--horizon=today", "NVDA"]                   # project file over user file
    assert rest("NVDA", "--horizon", "6-12m") == ["NVDA", "--horizon", "6-12m"]   # command line over both
    assert rest("NVDA", "--no-config") == ["NVDA"]


@pytest.mark.parametrize("typed", [["--horizon", "6-12m"], ["--horizon=6-12m"], ["--hor", "6-12m"],
                                   ["--hor=6-12m"]])
def test_an_option_typed_in_any_form_is_not_injected(user, typed):
    user('horizon = "1-2w"\nlanguage = "pt"\n')
    assert run_pipeline.parse(["NVDA", *typed])[1] == ["--language=pt", "NVDA", *typed]


def test_injected_options_come_first_so_what_is_typed_wins_in_build_prompt(user):
    """Even a spelling this script would not recognise is safe: argparse keeps the last one."""
    user('horizon = "1-2w"\nsupports = 4\nmax_rows = 52\nvolume = true\n')
    rest = run_pipeline.parse(["NVDA", "--resistances", "2"])[1]
    assert rest == ["--horizon=1-2w", "--supports=4", "--volume", "--max-rows=52", "NVDA",
                    "--resistances", "2"]
    parsed = build_prompt.arguments().parse_args(rest + ["--horizon", "today"])
    assert (parsed.horizon, parsed.supports, parsed.max_rows, parsed.volume) == ("today", 4, 52, True)


def test_precedence_of_an_own_option(user, project):
    timeout = lambda *argv: run_pipeline.parse(["NVDA", *argv])[0].timeout
    assert timeout() == run_pipeline.DEFAULT_TIMEOUT
    user("timeout = 600\n")
    assert timeout() == 600
    project("timeout = 900.5\n")
    assert timeout() == 900.5
    assert timeout("--timeout", "60") == 60
    assert timeout("--no-config") == run_pipeline.DEFAULT_TIMEOUT


def test_precedence_of_a_path(user, project, cwd, tmp_path, monkeypatch):
    def history(*argv):
        os.environ.pop(HISTORY, None) if "env" not in argv else None
        args = run_pipeline.parse(["NVDA", *[a for a in argv if a != "env"]])[0]
        return args.history_dir, os.environ.get(HISTORY)
    assert history() == ("projections", None)          # default: rz.py's own ./projections
    file = user('history_dir = "kept"\nruns_dir = "out"\n')
    assert history() == (str(file.parent / "kept"),) * 2
    project('history_dir = "here"\n')
    assert history() == (str(cwd / "here"),) * 2
    assert run_pipeline.parse(["NVDA"])[0].runs_dir == str(file.parent / "out")   # set by the user file alone
    monkeypatch.setenv(HISTORY, str(tmp_path / "from-env"))
    assert history("env") == (str(tmp_path / "from-env"),) * 2            # the variable over both files
    assert history("env", "--history-dir", "typed") == (str(cwd / "typed"),) * 2   # the command line over all
    assert run_pipeline.parse(["NVDA", "--runs-dir", "r"])[0].runs_dir == "r"


# ---- switches -------------------------------------------------------------------

def test_a_step1_switch_can_be_turned_off_for_one_run(user, project, capsys):
    rest = lambda *argv: run_pipeline.parse(["NVDA", *argv])[1]
    assert rest("--no-volume") == ["NVDA"]             # harmless without a file, and never passed on
    user("volume = true\n")
    assert rest() == ["--volume", "NVDA"]
    assert rest("--no-volume") == ["NVDA"]
    assert rest("--volume") == ["NVDA", "--volume"]
    project("volume = false\n")
    assert rest() == ["NVDA"]                          # the project file turns the user file's off
    assert rest("--volume") == ["NVDA", "--volume"]
    user("")
    project("")
    capsys.readouterr()
    assert "--volume or --no-volume" in fails(capsys, "NVDA", "--volume", "--no-volume")


def test_own_switches_have_both_forms(user):
    user("open = true\nquiet = true\nentries = false\nsave = false\nentries_by_claude = true\n")
    args = run_pipeline.parse(["NVDA"])[0]
    assert (args.open, args.quiet, args.no_entries, args.no_save, args.entries_by_claude) == (True,) * 5
    args = run_pipeline.parse(["NVDA", "--no-open", "--no-quiet", "--entries", "--save",
                               "--no-entries-by-claude"])[0]
    assert (args.open, args.quiet, args.no_entries, args.no_save, args.entries_by_claude) == (False,) * 5


# ---- paths ----------------------------------------------------------------------

def test_paths_expand_and_start_at_the_file(user, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("ZONES_DATA", str(tmp_path / "data"))
    file = user('runs_dir = "~/zones/runs"\nhistory_dir = "$ZONES_DATA/history"\n')
    args = run_pipeline.parse(["NVDA"])[0]
    assert args.runs_dir == str(tmp_path / "home" / "zones" / "runs")
    assert args.history_dir == str(tmp_path / "data" / "history")
    user('runs_dir = "../runs"\nhistory_dir = "history"\n')
    del os.environ[HISTORY]     # exported by the first parse; set, it would win over the file
    args = run_pipeline.parse(["NVDA"])[0]
    assert args.runs_dir == str(file.parent.parent / "runs")      # not the current directory
    assert args.history_dir == str(file.parent / "history")


def test_an_unset_variable_in_a_path_is_an_error(user, capsys, monkeypatch):
    monkeypatch.delenv("ZONES_NOWHERE", raising=False)
    file = user('history_dir = "$ZONES_NOWHERE/history"\n')
    assert fails(capsys, "NVDA") == (f"Error: {file}: 'history_dir' names the environment variable "
                                     "ZONES_NOWHERE, which is not set")


# ---- a whole run ----------------------------------------------------------------

def test_a_run_from_another_folder_puts_everything_where_the_file_says(
        user, cwd, stub_build, stub_claude, calls, capsys):
    stub_claude()
    file = user('language = "pt"\nresearch = "none"\nmodel = "sonnet"\ntheme = "dark"\ndpi = 80\n'
                'runs_dir = "runs"\nhistory_dir = "history"\n')
    assert run_main("PETR4.SA") == 0
    captured = capsys.readouterr()
    assert calls["build"][0][2:-2] == ["--language=pt", "--research=none", "PETR4.SA"]
    assert option(calls["claude"][0], "--model") == "sonnet"
    assert (option(calls["plot"][0], "--theme"), option(calls["plot"][0], "--dpi")) == ("dark", "80")
    assert "--lang" not in calls["plot"][0]
    paths = dict(line.split(":", 1) for line in captured.out.splitlines()
                 if line.startswith(("input:", "projection:", "chart:", "entries:", "saved:")))   # not the summary
    assert Path(paths["chart"].strip()).parent.parent.parent == file.parent / "runs"
    assert Path(paths["saved"].strip()).parent.parent == file.parent / "history"   # rz.py save got it
    assert (Path(paths["saved"].strip()) / "projection.json").is_file()
    assert list(cwd.iterdir()) == []                                               # nothing where we stood
    line = captured.err.splitlines()[0]
    assert line == f"Config: {file} (language, research, model, theme, dpi, runs_dir, history_dir)"


def test_the_history_variable_wins_over_the_file(user, tmp_path, monkeypatch, stub_build, stub_claude, capsys):
    """tests/test_run_pipeline.py isolates its runs with the variable: a file cannot undo that."""
    stub_claude()
    monkeypatch.setenv(HISTORY, str(tmp_path / "env-history"))
    user('history_dir = "history"\n')
    assert run_main("PETR4.SA", "--no-entries") == 0
    assert f"saved:      {tmp_path / 'env-history'}" in capsys.readouterr().out
    assert os.environ[HISTORY] == str(tmp_path / "env-history")


def test_step1_settings_do_not_touch_a_run_from_files(user, input_path, projection_path, capsys):
    """With --input nothing is downloaded, so nothing is injected (and nothing complains)."""
    file = user('language = "pt"\nhorizon = "1-2w"\nvolume = true\nruns_dir = "runs"\n')
    assert run_main("--input", input_path, "--projection", projection_path, "--no-entries") == 0
    chart = Path(next(line for line in capsys.readouterr().out.splitlines()
                      if line.startswith("chart:")).split(":", 1)[1].strip())
    assert chart.is_file() and file.parent / "runs" in chart.parents


def test_no_ticker_is_still_the_same_error(user, capsys):
    user('language = "pt"\n')
    assert run_main() == 1
    assert "Give a ticker" in capsys.readouterr().err


# ---- errors ---------------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
    ('langauge = "pt"\n', "unknown setting 'langauge' (did you mean 'language'?). "
                          "`zones --show-config` lists them all."),
    ('runs-dir = "x"\n', "unknown setting 'runs-dir' (did you mean 'runs_dir'?). "
                         "`zones --show-config` lists them all."),
    ('[chart]\ntheme = "dark"\n', "unknown setting 'chart' (settings are plain `key = value` lines, "
                                  "not [tables]). `zones --show-config` lists them all."),
    ('supports = "three"\n', "'supports' must be a whole number, not \"three\""),
    ("supports = true\n", "'supports' must be a whole number, not true"),
    ("language = 5\n", "'language' must be text in quotes, not 5"),
    ('volume = "yes"\n', "'volume' must be true or false, not \"yes\""),
    ('horizon = "2w"\n', "'horizon' must be one of \"today\", \"1-2w\", \"1-3m\", \"6-12m\", not \"2w\""),
    ('theme = "blue"\n', "'theme' must be one of \"light\", \"dark\", not \"blue\""),
    ("dpi = 5000\n", "'dpi' must be a whole number from 1 to 1200, not 5000"),
    ("candles = 0\n", "'candles' must be a whole number, 1 or more, not 0"),
    ("timeout = 0\n", "'timeout' must be a number above 0, not 0"),
    ('timeout = "60"\n', "'timeout' must be a number above 0, not \"60\""),
    ("history_dir = 3\n", "'history_dir' must be a folder path in quotes, not 3"),
])
def test_a_bad_value_is_one_line_naming_the_file_and_the_key(user, capsys, text, message):
    file = user(text)
    assert fails(capsys, "NVDA") == f"Error: {file}: {message}"


def test_malformed_toml_is_one_line(user, project, capsys):
    file = user('language = "pt\n')
    assert fails(capsys, "NVDA").startswith(f"Error: {file} is not valid TOML: ")
    user("")
    bad = project("horizon = = 3\n")
    assert fails(capsys, "NVDA").startswith(f"Error: {bad} is not valid TOML: ")
    assert fails(capsys, "--show-config").startswith(f"Error: {bad} is not valid TOML: ")


def test_a_missing_or_empty_file_is_not_an_error(user, capsys):
    assert run_pipeline.parse(["NVDA"])[1] == ["NVDA"]        # named by ZONES_CONFIG, not there
    assert capsys.readouterr().err == ""
    file = user("# nothing set yet\n")
    assert run_pipeline.parse(["NVDA"])[1] == ["NVDA"]
    assert capsys.readouterr().err.strip() == f"Config: {file} (no settings)"


def test_no_config_skips_even_a_broken_file(user, project, capsys):
    user("this is not toml\n")
    project('langauge = "pt"\n')
    args, rest = run_pipeline.parse(["NVDA", "--no-config"])
    assert rest == ["NVDA"] and capsys.readouterr().err == ""


# ---- --show-config and --init-config --------------------------------------------

def test_show_config_names_every_setting_and_its_source(user, project, cwd, tmp_path, monkeypatch, capsys):
    file = user('language = "pt"\nresearch = "none"\nhistory_dir = "history"\nopen = true\n')
    local = project('research = "deep"\nruns_dir = "out"\n')
    rows, text = shown(capsys, "PETR4.SA", "--horizon", "1-2w", "--model=opus", "--no-open")
    assert set(rows) == set(run_pipeline.SETTINGS)
    assert rows["language"] == ("pt", str(file))
    assert rows["research"] == ("deep", str(local))
    assert rows["horizon"] == ("1-2w", "command line")
    assert rows["model"] == ("opus", "command line")
    assert rows["open"] == ("false", "command line")
    assert rows["supports"] == ("3", "default")
    assert rows["theme"] == ("(light, the chart's default)", "default")
    assert rows["runs_dir"] == (str(cwd / "out"), str(local))
    assert rows["history_dir"] == (str(file.parent / "history"), str(file))
    assert f"{file}  [loaded, 4 settings]" in text and f"{local}  [loaded, 2 settings]" in text
    assert "command line > environment variable > project file" in text
    assert HISTORY not in os.environ                         # printing changes nothing

    monkeypatch.setenv(HISTORY, str(tmp_path / "env"))
    assert shown(capsys)[0]["history_dir"] == (str(tmp_path / "env"), f"environment ({HISTORY})")
    rows, text = shown(capsys, "--no-config")
    assert rows["language"] == ("en", "default") and "[ignored (--no-config)]" in text


def test_show_config_without_files(cwd, capsys, monkeypatch):
    monkeypatch.delenv(run_pipeline.CACHE_ENV)       # conftest points it at a temp folder
    rows, text = shown(capsys)
    assert {source for _, source in rows.values()} == {"default"}
    assert rows["runs_dir"][0] == str(cwd / "runs") and rows["history_dir"][0] == str(cwd / "projections")
    assert "[not found]" in text and "  project file:" not in text   # its lookup is off in tests
    assert rows["cache_dir"][0] == "($XDG_CACHE_HOME/zones, else ~/.cache/zones)"


def test_cache_dir_from_a_file_is_exported_for_the_child_processes(user, monkeypatch):
    monkeypatch.delenv(run_pipeline.CACHE_ENV)
    file = user('cache_dir = "candles"\n')
    run_pipeline.parse(["NVDA"])
    assert os.environ[run_pipeline.CACHE_ENV] == str(file.parent / "candles")
    monkeypatch.setenv(run_pipeline.CACHE_ENV, "/elsewhere")     # a variable the user set wins
    run_pipeline.parse(["NVDA"])
    assert os.environ[run_pipeline.CACHE_ENV] == "/elsewhere"
    run_pipeline.parse(["NVDA", "--cache-dir", "/typed"])         # and the command line wins over it
    assert os.environ[run_pipeline.CACHE_ENV] == "/typed"


def test_init_config_writes_once_and_never_overwrites(user, tmp_path, capsys):
    path = tmp_path / "home" / "cfg" / "config.toml"
    with pytest.raises(SystemExit) as stopped:
        run_main("--init-config")
    assert stopped.value.code == 0 and f"Wrote {path}" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == run_pipeline.config_template()
    assert run_pipeline.parse(["NVDA"])[1] == ["NVDA"]       # all comments: nothing is set
    path.write_text('language = "pt"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        run_main("--init-config")
    assert stopped.value.code == 0 and "already exists and was left as it is" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == 'language = "pt"\n'


def test_the_template_lists_every_setting_commented_out_with_its_default():
    template = run_pipeline.config_template()
    assert tomllib.loads(template) == {}
    lines = [line[2:] for line in template.splitlines() if " = " in line.split("  #")[0]]
    values = tomllib.loads("\n".join(lines))
    assert list(values) == list(run_pipeline.SETTINGS)
    for key, setting in run_pipeline.SETTINGS.items():
        if setting.example is None:
            assert values[key] == setting.default, key
        if setting.kind != "path":      # every line is a value the checks accept as it stands
            assert run_pipeline.config_value(setting, values[key], Path("x.toml")) == values[key]


# ---- the settings repeat the other scripts' options ------------------------------

def test_step1_settings_match_build_prompt():
    actions = {action.dest: action for action in build_prompt.arguments()._actions}
    for setting in run_pipeline.SETTINGS.values():
        if setting.group != "step1":
            continue
        action = actions[setting.key]
        assert setting.flag in action.option_strings
        assert action.default == setting.default, setting.key
        assert tuple(action.choices or ()) == setting.choices, setting.key
        kind = ("flag" if action.nargs == 0 else "choice" if action.choices else
                "int" if action.type is int else "text")
        assert kind == setting.kind, setting.key
    assert tuple(run_pipeline.SETTINGS["horizon"].choices) == tuple(build_prompt.PRESETS)


def test_chart_settings_match_plot_projection():
    assert set(run_pipeline.SETTINGS["theme"].choices) == set(plot_projection.THEMES)
    assert set(run_pipeline.SETTINGS["lang"].choices) == set(plot_projection.TEXT)


def test_a_file_can_turn_the_summary_off(user, input_path, projection_path, capsys):
    user("summary = false\n")
    assert run_main("--input", input_path, "--projection", projection_path, "--no-entries") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("input:")
    user("summary = false\n")
    assert run_main("--input", input_path, "--projection", projection_path, "--no-entries", "--summary") == 0
    assert not capsys.readouterr().out.startswith("input:")
