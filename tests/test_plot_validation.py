"""Step 3, plot_projection.py: bad files give a short message and a failing exit, never a traceback."""
import copy
import json
import subprocess
import sys

import matplotlib.pyplot as plt
import pytest

import plot_projection
from conftest import ROOT

EXAMPLES = ROOT / "examples"


@pytest.fixture(autouse=True)
def close_figures():
    yield
    plt.close("all")


@pytest.fixture
def run_main(monkeypatch, capsys, tmp_path, input_path, projection_path, write_json):
    """run_main(source=..., proj=..., extra args) -> (exit code, stderr, output path).

    `source` and `proj` are parsed documents (written to tmp_path), paths, or None for the fixtures."""
    def run(*extra, source=None, proj=None, output=None):
        def as_path(doc, default, name):
            if doc is None:
                return str(default)
            return str(doc) if not isinstance(doc, (dict, list)) else write_json(name, doc)
        target = tmp_path / "chart.png" if output is None else output
        argv = ["plot_projection.py", as_path(source, input_path, "in.json"),
                as_path(proj, projection_path, "proj.json"), "-o", str(target), "--dpi", "40", *extra]
        monkeypatch.setattr(sys, "argv", argv)
        capsys.readouterr()
        try:
            plot_projection.main()
            code = 0
        except SystemExit as stop:      # sys.exit(text) prints the text to stderr and exits with 1
            code, text = (1, stop.code) if isinstance(stop.code, str) else (stop.code or 0, "")
            return code, text + capsys.readouterr().err, target
        return code, capsys.readouterr().err, target
    return run


def assert_clean_failure(code, err, target, *expected):
    assert code != 0
    for text in expected:
        assert text in err, err
    assert "Traceback" not in err and "unexpected" not in err, err
    assert not target.exists()


# ---- one broken field at a time ------------------------------------------------

def remove(*path):
    def change(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return change


def put(*path_and_value):
    *path, value = path_and_value

    def change(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = copy.deepcopy(value)
    return change


def only_tails(proj):
    proj["supports"], proj["resistances"] = proj["supports"][-1:], proj["resistances"][-1:]


def every_zone_open(proj):
    for zone in proj["supports"]:
        zone["zone"][0] = None
    for zone in proj["resistances"]:
        zone["zone"][1] = None


def two_tails(proj):
    proj["supports"].append(copy.deepcopy(proj["supports"][-1]))


def a_fifth_scenario(proj):
    proj["scenarios"].append({"name": "drift", "p": 0.0})


NOTHING_TO_DRAW = "projection: supports and resistances hold no zone with both edges"
A_ZONE = "expected [low, high] as two numbers, or one number and one null for a tail"
A_DAY = "expected an ISO date"

BROKEN_PROJECTIONS = [
    # required keys
    ("no asset", remove("asset"), "projection.asset: missing, expected an object"),
    ("no ticker", remove("asset", "ticker"), "projection.asset.ticker: missing"),
    ("ticker is a number", put("asset", "ticker", 4), "projection.asset.ticker: expected a non-empty string, got 4"),
    ("currency is null", put("asset", "currency", None), "projection.asset.currency: expected a non-empty string"),
    ("no as_of", remove("as_of"), "projection.as_of: missing"),
    ("no price", remove("as_of", "price"), "projection.as_of.price: missing, expected a number"),
    ("price is a string", put("as_of", "price", "44.06"), 'projection.as_of.price: expected a number, got "44.06"'),
    ("price is NaN", put("as_of", "price", float("nan")), "projection.as_of.price: expected a number, got NaN"),
    ("price is true", put("as_of", "price", True), "projection.as_of.price: expected a number, got true"),
    ("no time", remove("as_of", "time"), "projection.as_of.time: missing"),
    ("time is prose", put("as_of", "time", "yesterday"), f'projection.as_of.time: {A_DAY}'),
    ("time is dd/mm/yyyy", put("as_of", "time", "17/11/2026"), 'got "17/11/2026"'),
    ("no horizon", remove("horizon"), "projection.horizon: missing"),
    ("horizon as in the input", put("horizon", "1-3m"), 'projection.horizon: expected an object, got "1-3m"'),
    ("no label", remove("horizon", "label"), "projection.horizon.label: missing"),
    ("no close_basis", remove("horizon", "close_basis"), "projection.horizon.close_basis: missing"),
    ("no closes", remove("horizon", "closes"), "projection.horizon.closes: missing"),
    ("closes is a word", put("horizon", "closes", "thirteen"), "projection.horizon.closes: expected a whole number"),
    ("closes is zero", put("horizon", "closes", 0), "projection.horizon.closes: expected a whole number of at least 1, got 0"),
    ("no last", remove("horizon", "last"), "projection.horizon.last: missing"),
    ("last is prose", put("horizon", "last", "Feb 2027"), f"projection.horizon.last: {A_DAY}"),
    ("last is before as_of", put("horizon", "last", "2026-01-01"), "projection.horizon.last: 2026-01-01 is before as_of.time"),
    # zones
    ("no supports", remove("supports"), "projection.supports: missing, expected a list"),
    ("no resistances", remove("resistances"), "projection.resistances: missing, expected a list"),
    ("supports as in zones.json", put("supports", {"S1": [43.2, 44.6]}), "projection.supports: expected a list"),
    ("zone entry is a pair", put("supports", 1, [40.6, 42.2]), "projection.supports[1]: expected an object, got [40.6, 42.2]"),
    ("both lists empty", lambda p: p.update(supports=[], resistances=[]), NOTHING_TO_DRAW),
    ("only tails", only_tails, NOTHING_TO_DRAW),
    ("every zone has a null edge", every_zone_open, NOTHING_TO_DRAW),
    ("no zone id", remove("supports", 0, "id"), "projection.supports[0].id: missing"),
    ("no zone", remove("supports", 1, "zone"), f"projection.supports[1].zone: missing, {A_ZONE}"),
    ("zone of strings", put("supports", 1, "zone", ["40.6", "42.2"]), f'projection.supports[1].zone: {A_ZONE}, got ["40.6", "42.2"]'),
    ("zone of one", put("resistances", 0, "zone", [45.3]), f"projection.resistances[0].zone: {A_ZONE}, got [45.3]"),
    ("zone of three", put("resistances", 0, "zone", [1, 2, 3]), f"projection.resistances[0].zone: {A_ZONE}"),
    ("zone of nulls", put("supports", 0, "zone", [None, None]), f"projection.supports[0].zone: {A_ZONE}, got [null, null]"),
    ("zone is a number", put("supports", 0, "zone", 43.2), f"projection.supports[0].zone: {A_ZONE}, got 43.2"),
    ("zone upside down", put("supports", 0, "zone", [44.6, 43.2]), "projection.supports[0].zone: expected [low, high] with low below high"),
    ("tail open on the wrong side", put("supports", 3, "zone", [37.4, None]), "projection.supports[3].zone: expected [null, price] for the tail of the supports"),
    ("two tails on one side", two_tails, "projection.supports[4].zone: second open-ended zone in supports"),
    ("no reach", remove("resistances", 2, "reach"), "projection.resistances[2].reach: missing, expected a number from 0 to 1"),
    ("no turn", remove("resistances", 3, "turn"), "projection.resistances[3].turn: missing"),
    ("reach above 1", put("supports", 0, "reach", 1.5), "projection.supports[0].reach: expected a number from 0 to 1, got 1.5"),
    ("reach below 0", put("supports", 0, "reach", -0.1), "projection.supports[0].reach: expected a number from 0 to 1, got -0.1"),
    ("turn in percent", put("supports", 0, "turn", 20), "projection.supports[0].turn: expected a number from 0 to 1, got 20"),
    ("turn as text", put("supports", 0, "turn", "20%"), 'projection.supports[0].turn: expected a number from 0 to 1, got "20%"'),
    # sections the chart can do without, when present but wrong
    ("scenarios is a mapping", put("scenarios", {"range": 1.0}), "projection.scenarios: expected a list"),
    ("scenarios is null", put("scenarios", None), "projection.scenarios: expected a list, got null"),
    ("scenario without p", remove("scenarios", 0, "p"), "projection.scenarios[0].p: missing"),
    ("scenario p above 1", put("scenarios", 0, "p", 1.4), "projection.scenarios[0].p: expected a number from 0 to 1, got 1.4"),
    ("scenario p as text", put("scenarios", 0, "p", "0.5"), "projection.scenarios[0].p: expected a number from 0 to 1"),
    ("scenario without name", remove("scenarios", 2, "name"), "projection.scenarios[2].name: missing"),
    ("scenario named twice", put("scenarios", 3, "name", "range"), 'projection.scenarios[3].name: "range" appears twice'),
    ("scenario bound is a number", put("scenarios", 0, "low", 40.6), "projection.scenarios[0].low: expected a non-empty string, got 40.6"),
    ("five scenarios", a_fifth_scenario, "projection.scenarios: 5 scenarios, the chart has room for 4"),
    ("structure is null", put("structure", None), "projection.structure: expected an object, got null"),
    ("trend is a list", put("structure", "trend", ["down"]), "projection.structure.trend: expected a non-empty string"),
    ("swing is a list", put("structure", "swing", []), "projection.structure.swing: expected an object"),
    ("swing point is a number", put("structure", "swing", "high", 59.79), "projection.structure.swing.high: expected [price, date], got 59.79"),
    ("swing point without date", put("structure", "swing", "low", [28.86]), "projection.structure.swing.low: expected [price, date], got [28.86]"),
    ("swing price as text", put("structure", "swing", "high", 0, "59.79"), "projection.structure.swing.high: expected [price, date]"),
    ("from_high as text", put("structure", "from_high", "-26%"), 'projection.structure.from_high: expected a number, got "-26%"'),
    ("events is a mapping", put("events", {"date": "2026-12-21"}), "projection.events: expected a list"),
    ("event is a sentence", put("events", ["ex-dividend on 21 Dec"]), "projection.events[0]: expected an object"),
    ("event date is prose", put("events", 0, "date", "late December"), f"projection.events[0].date: {A_DAY}"),
    ("event without what", remove("events", 0, "what"), "projection.events[0].what: missing"),
    ("driver is a sentence", put("drivers", ["oil fell"]), "projection.drivers[0]: expected an object"),
    ("driver without what", remove("drivers", 0, "what"), "projection.drivers[0].what: missing"),
    ("confidence is a word", put("confidence", "low"), 'projection.confidence: expected an object, got "low"'),
    ("confidence level is a list", put("confidence", "level", ["low"]), "projection.confidence.level: expected a non-empty string"),
    ("limits is one string", put("confidence", "limits", "few candles"), "projection.confidence.limits: expected a list"),
    ("a limit is a number", put("confidence", "limits", 1, 7), "projection.confidence.limits[1]: expected a string, got 7"),
]


@pytest.mark.parametrize("change, message", [pytest.param(change, message, id=name)
                                              for name, change, message in BROKEN_PROJECTIONS])
def test_broken_projection_is_reported_not_raised(run_main, projection_data, change, message):
    change(projection_data)
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target, "Cannot plot", message)


def main_series(source):
    return source["data"]["series"][-1]


BROKEN_INPUTS = [
    ("no asset", remove("asset"), "input.asset: missing"),
    ("no data", remove("data"), "input.data: missing, expected an object"),
    ("no columns", remove("data", "columns"), "input.data.columns: missing"),
    ("columns renamed", put("data", "columns", ["date", "o", "h", "l", "c"]), "input.data.columns: expected the names start, open, high, low, close"),
    ("column missing", put("data", "columns", ["start", "open", "high", "low"]), "input.data.columns: expected the names"),
    ("no series", remove("data", "series"), "input.data.series: missing, expected a list"),
    ("series empty", put("data", "series", []), "input.data.series: expected at least one series"),
    ("series is one object", lambda s: s["data"].update(series=main_series(s)), "input.data.series: expected a list"),
    ("series item is a string", put("data", "series", 0, "1M"), 'input.data.series[0]: expected an object, got "1M"'),
    ("series without candles", remove("data", "series", 1, "candles"), "input.data.series[1].candles: missing"),
    ("series without last_row", remove("data", "series", 1, "last_row"), "input.data.series[1].last_row: missing"),
    ("last_row unknown", put("data", "series", 1, "last_row", "open"), 'input.data.series[1].last_row: expected "forming" or "closed", got "open"'),
    ("series without rows", remove("data", "series", 1, "rows"), "input.data.series[1].rows: missing"),
    ("rows empty", put("data", "series", 1, "rows", []), "input.data.series[1].rows: expected at least one candle"),
    ("row too short", put("data", "series", 1, "rows", 3, ["2025-01-06", 30.0]), "input.data.series[1].rows[3]: expected [start, open, high, low, close]"),
    ("row with a null", lambda s: main_series(s)["rows"][-1].__setitem__(3, None), "input.data.series[1].rows[103]: expected [start, open, high, low, close]"),
    ("row with a text price", lambda s: main_series(s)["rows"][0].__setitem__(4, "31.2"), "input.data.series[1].rows[0]: expected [start, open, high, low, close]"),
    ("row start is prose", lambda s: main_series(s)["rows"][50].__setitem__(0, "week 51"), "input.data.series[1].rows[50]: expected [start, open, high, low, close]"),
    ("row start is a number", lambda s: main_series(s)["rows"][0].__setitem__(0, 20241125), "input.data.series[1].rows[0]: expected [start, open, high, low, close]"),
    ("row is a mapping", lambda s: main_series(s)["rows"].__setitem__(2, {"start": "2024-12-09"}), "input.data.series[1].rows[2]: expected [start, open, high, low, close]"),
]


@pytest.mark.parametrize("change, message", [pytest.param(change, message, id=name)
                                              for name, change, message in BROKEN_INPUTS])
def test_broken_input_is_reported_not_raised(run_main, input_data, change, message):
    assert len(main_series(input_data)["rows"]) == 104
    change(input_data)
    code, err, target = run_main(source=input_data)
    assert_clean_failure(code, err, target, "Cannot plot", message)


def test_many_broken_rows_are_summarised(run_main, input_data):
    for row in main_series(input_data)["rows"]:
        row[2] = None
    code, err, target = run_main(source=input_data)
    assert_clean_failure(code, err, target, "input.data.series[1].rows[4]:", "input.data.series[1].rows: and 99 more rows like these")
    assert "rows[5]" not in err
    assert len(err.splitlines()) == 7


# ---- files, output path and options -----------------------------------------------

def test_missing_files(run_main, tmp_path):
    code, err, target = run_main(source=tmp_path / "nope_in.json", proj=tmp_path / "nope_proj.json")
    assert_clean_failure(code, err, target, "Cannot plot, 2 problems",
                         "nope_in.json: cannot read the file (No such file or directory)",
                         "nope_proj.json: cannot read the file (No such file or directory)")


def test_a_folder_instead_of_a_file(run_main, tmp_path):
    code, err, target = run_main(proj=tmp_path)
    assert_clean_failure(code, err, target, "cannot read the file")


@pytest.mark.parametrize("content, hint", [
    (b"this is not json {", "not valid JSON (Expecting value: line 1, column 1)"),
    (b"", "not valid JSON (the file is empty)"),
    (b'{"asset": {"ticker": "PETR4.SA"', "not valid JSON ("),
    (b'```json\n{"asset": {}}\n```', "not valid JSON (remove the Markdown code fence around the JSON)"),
    ('{"note": "retração"}'.encode("latin-1"), "not a UTF-8 text file"),
], ids=["prose", "empty", "truncated", "fenced", "latin-1"])
def test_unreadable_json(run_main, tmp_path, content, hint):
    for role in ("source", "proj"):
        bad = tmp_path / f"bad_{role}.json"
        bad.write_bytes(content)
        code, err, target = run_main(**{role: bad})
        assert_clean_failure(code, err, target, "Cannot plot, 1 problem", f"bad_{role}.json: {hint}")


def test_a_byte_order_mark_is_accepted(run_main, projection_path, tmp_path):
    marked = tmp_path / "bom.json"
    marked.write_bytes(b"\xef\xbb\xbf" + projection_path.read_bytes())
    code, err, target = run_main(proj=marked)
    assert code == 0 and target.exists()


def test_swapped_and_repeated_files(run_main, input_path, projection_path):
    code, err, target = run_main(source=projection_path, proj=input_path)
    assert_clean_failure(code, err, target, "the two files are swapped")
    code, err, target = run_main(source=projection_path)
    assert_clean_failure(code, err, target, "input: this is a projection")
    code, err, target = run_main(proj=input_path)
    assert_clean_failure(code, err, target, "projection: this is a build_prompt.py output")
    assert len(err.splitlines()) == 2


@pytest.mark.parametrize("doc", [[], [1], None, 5, "text"], ids=repr)
def test_json_that_is_not_an_object(run_main, doc, tmp_path):
    for role in ("source", "proj"):
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        code, err, target = run_main(**{role: path})
        assert_clean_failure(code, err, target, "expected a JSON object")


@pytest.mark.parametrize("name, message", [
    ("chart.xyz", ".xyz is not an image format"),
    ("chart", "a name without extension is not an image format"),
    ("missing/chart.png", "does not exist"),
    ("", "is a folder, not an image file"),
], ids=["extension", "no extension", "no folder", "a folder"])
def test_output_path_is_checked_before_drawing(run_main, tmp_path, monkeypatch, name, message):
    monkeypatch.setattr(plot_projection, "build", lambda args: pytest.fail("drew before checking the output"))
    code, err, target = run_main(output=tmp_path / name)
    assert code != 0 and message in err and "Traceback" not in err
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_output_that_cannot_be_written(run_main, tmp_path, monkeypatch):
    monkeypatch.setattr(plt.Figure, "savefig", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    code, err, target = run_main()
    assert_clean_failure(code, err, target, "chart.png: cannot write the image (Permission denied)")


@pytest.mark.parametrize("options, message", [
    (["--candles", "0"], "--candles: expected at least 1, got 0"),
    (["--candles", "-5"], "--candles: expected at least 1, got -5"),
    (["--dpi", "0"], "--dpi: expected 1 to 1200, got 0"),
    (["--dpi", "100000"], "--dpi: expected 1 to 1200, got 100000"),
])
def test_options_out_of_range(run_main, options, message):
    code, err, target = run_main(*options)
    assert_clean_failure(code, err, target, message)


# ---- everything at once --------------------------------------------------------------

def test_all_problems_are_reported_together(run_main, input_data, projection_data, tmp_path):
    del projection_data["as_of"]["price"]
    projection_data["supports"][1]["zone"] = [42.2, "x"]
    projection_data["scenarios"][0]["p"] = 7
    projection_data["events"][0]["date"] = "soon"
    del input_data["data"]["series"][1]["last_row"]
    code, err, target = run_main("--candles", "0", source=input_data, proj=projection_data,
                                 output=tmp_path / "chart.bmpx")
    lines = err.splitlines()
    assert code == 1
    assert lines[0] == "Cannot plot, 7 problems:"
    assert lines[1:] == [
        "  --candles: expected at least 1, got 0",
        f"  {tmp_path / 'chart.bmpx'}: .bmpx is not an image format this can write; use png, svg, pdf or jpg",
        "  input.data.series[1].last_row: missing, expected a non-empty string",
        "  projection.as_of.price: missing, expected a number",
        '  projection.supports[1].zone: expected [low, high] as two numbers, or one number and one null for a tail, got [42.2, "x"]',
        "  projection.scenarios[0].p: expected a number from 0 to 1, got 7",
        '  projection.events[0].date: expected an ISO date such as 2026-12-21 or 2026-11-18T15:15-03:00, got "soon"',
    ]


def test_command_line_exit_code_and_stderr(tmp_path, input_path, projection_data, write_json):
    projection_data["supports"] = projection_data["supports"][-1:]
    projection_data["resistances"] = projection_data["resistances"][-1:]
    done = subprocess.run(
        [sys.executable, str(ROOT / "plot_projection.py"), str(input_path),
         write_json("tails.json", projection_data), "-o", str(tmp_path / "chart.png")],
        capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.splitlines() == [
        "Cannot plot, 1 problem:",
        "  projection: supports and resistances hold no zone with both edges, so there is nothing to draw"]
    assert not (tmp_path / "chart.png").exists()


def test_a_bug_while_drawing_still_gives_no_traceback(run_main, monkeypatch):
    monkeypatch.setattr(plot_projection, "build", lambda args: {}["typical"])
    code, err, target = run_main()
    assert code == 1 and not target.exists()
    assert err.splitlines()[0] == "Cannot plot, unexpected KeyError while drawing: 'typical'"
    assert "Traceback" not in err


# ---- good files still plot ---------------------------------------------------------------

@pytest.mark.parametrize("folder, source, proj", [
    (EXAMPLES, "petr4_input.json", "petr4_projection.json"),
    (ROOT / "tests" / "fixtures", "input.json", "projection.json"),
], ids=["examples", "fixtures"])
def test_valid_pairs_plot_without_a_word(run_main, folder, source, proj):
    code, err, target = run_main(source=folder / source, proj=folder / proj)
    assert code == 0
    assert err.strip() == f"Wrote {target}"
    assert target.read_bytes().startswith(b"\x89PNG")


def test_valid_pairs_have_nothing_to_report(input_data, projection_data):
    assert plot_projection.validate(input_data, projection_data) == ([], [])
    assert plot_projection.rule_problems(projection_data) == ([], None)
    pair = [json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
            for name in ("petr4_input.json", "petr4_projection.json")]
    assert plot_projection.validate(*pair) == ([], [])
    assert plot_projection.rule_problems(pair[1]) == ([], None)


def test_sections_the_chart_can_do_without(run_main, projection_data):
    for key in ("structure", "events", "drivers", "confidence", "scenarios"):
        del projection_data[key]
    code, err, target = run_main(proj=projection_data)
    assert code == 0 and target.exists()
    assert err.strip() == f"Wrote {target}"


def test_tolerated_oddities_still_plot(run_main, projection_data):
    projection_data["structure"]["swing"]["high"][1] = "some day in October"   # no such row: no marker
    projection_data["structure"]["swing"]["low"] = None
    projection_data["structure"]["trend"] = "choppy"
    projection_data["horizon"]["label"] = "5y"
    projection_data["events"].append({"date": None, "what": "strike vote"})
    del projection_data["asset"]["currency"]
    del projection_data["confidence"]["limits"]
    projection_data["scenarios"][0]["low"] = "below the floor"
    code, err, target = run_main(proj=projection_data)
    assert code == 0 and target.exists()


# ---- the rules of rz.py check: block by default, warn with --no-check -------------------

def break_rules(proj):
    proj["scenarios"][2]["p"] = 0.29            # no longer sums to 1
    proj["confidence"]["level"] = "high"
    return ["scenarios must sum to 1", "confidence is low or medium, never high"]


def test_rule_violations_block_the_plot(run_main, projection_data):
    broken = break_rules(projection_data)
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target)
    assert err.splitlines() == ["Not plotted, the projection fails 2 checks:", *("  " + line for line in broken),
                                "Fix the projection, or pass --no-check to plot it as it is."]


def test_no_check_plots_and_warns(run_main, projection_data):
    broken = break_rules(projection_data)
    code, err, target = run_main("--no-check", proj=projection_data)
    assert code == 0 and target.read_bytes().startswith(b"\x89PNG")
    assert err.splitlines() == [*("Warning: " + line for line in broken), f"Wrote {target}"]


def test_no_check_does_not_excuse_structural_errors(run_main, projection_data):
    del projection_data["horizon"]["closes"]
    code, err, target = run_main("--no-check", proj=projection_data)
    assert_clean_failure(code, err, target, "Cannot plot", "projection.horizon.closes: missing")


def test_rules_wait_for_a_sound_structure(run_main, projection_data):
    break_rules(projection_data)
    projection_data["as_of"]["time"] = "today"
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target, "Cannot plot, 1 problem")
    assert "sum to 1" not in err


@pytest.mark.parametrize("change, message", [
    (lambda p: p.update(supports=[]), "supports: no zones"),
    (lambda p: p.update(scenarios=[]), "scenarios are breakdown, breakout, range, whipsaw"),
    (put("scenarios", 3, "name", "chop"), "scenarios are breakdown, breakout, range, whipsaw"),
    (put("supports", 1, "zone", [42.0, 43.5]), "supports: zones must run nearest first without overlapping"),
    (put("supports", 1, "reach", 0.9), "S2: reach must equal the sum of turn from here outward"),
    (put("resistances", 1, "evidence", []), "R2: no evidence"),
    (put("as_of", "price", 4406), "R1 lies below the price"),
    (remove("drivers", 0, "source"), "driver without a source URL"),
], ids=["one side empty", "scenarios empty", "scenario renamed", "overlap", "reach", "evidence", "price", "driver"])
def test_each_rule_reaches_the_plot(run_main, projection_data, change, message):
    change(projection_data)
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target, "Not plotted", message)
    code, err, target = run_main("--no-check", proj=projection_data)
    assert code == 0 and target.exists() and f"Warning: {message}" in err


@pytest.mark.parametrize("basis", ["1D", "15m"])
def test_close_basis_must_be_a_series_of_the_input(run_main, projection_data, basis):
    projection_data["horizon"]["close_basis"] = basis
    message = f'projection.horizon.close_basis: "{basis}" is not a series of the input (it has 1M, 1W)'
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target, "Not plotted, the projection fails 1 check:", message)
    code, err, target = run_main("--no-check", proj=projection_data)
    assert code == 0 and target.exists() and f"Warning: {message}" in err


# ---- the script copied without the skill ------------------------------------------------------

def test_without_rz_only_the_structure_is_checked(run_main, projection_data, monkeypatch, tmp_path):
    monkeypatch.setattr(plot_projection, "RZ_PATH", tmp_path / "gone" / "rz.py")
    break_rules(projection_data)
    code, err, target = run_main(proj=projection_data)
    assert code == 0 and target.exists()
    assert err.splitlines() == [
        f"Note: rz.py not found at {tmp_path / 'gone' / 'rz.py'}: only the structure of the files was checked, "
        "not the projection's rules.", f"Wrote {target}"]
    target.unlink()
    del projection_data["supports"]
    code, err, target = run_main(proj=projection_data)
    assert_clean_failure(code, err, target, "Cannot plot", "projection.supports: missing")


@pytest.mark.parametrize("helper, reason", [
    ("def problems(o):\n    return ['old']\n", "TypeError"),         # a copy from before `complete`
    ("def check(path):\n    pass\n", "AttributeError"),
    ("import a_module_nobody_installed\n", "ModuleNotFoundError"),
    ("def problems(:\n", "SyntaxError"),
], ids=["old signature", "no problems()", "import fails", "syntax error"])
def test_an_unusable_rz_is_noted_and_skipped(run_main, monkeypatch, tmp_path, helper, reason):
    other = tmp_path / "rz.py"
    other.write_text(helper, encoding="utf-8")
    monkeypatch.setattr(plot_projection, "RZ_PATH", other)
    code, err, target = run_main()
    assert code == 0 and target.exists()
    assert err.startswith(f"Note: rz.py could not be used ({reason}")
    assert len(err.splitlines()) == 2


def test_rz_path_points_at_the_skill():
    from conftest import RZ_PATH
    assert plot_projection.RZ_PATH == RZ_PATH and RZ_PATH.is_file()
