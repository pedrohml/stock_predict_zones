"""show_run.py: the text summary of one run, and how run_pipeline.py relays it.

Offline like everything else: the summary reads two or three JSON files and
prints text. The pipeline tests plot again from the fixtures into tmp_path, so
no Claude Code, no Yahoo and no `runs/` or `projections/` folder in the repo.
"""
import copy
import io
import json
import re
import subprocess
import sys
from argparse import Namespace

import pytest

import plot_projection
import run_pipeline
import show_run
import trade_entries
from conftest import ROOT
from test_run_pipeline import store  # noqa: F401  (autouse: rz.py save never writes into the repo)

LANGS = sorted(show_run.TEXT)
WIDTHS = [30, 40, 60, 80, 100, 140]
ANSI = "\033["


@pytest.fixture
def entries(input_data, projection_data):
    return trade_entries.build(input_data, projection_data)


def text_of(source, proj, entries=None, lang=None, width=80, color=False, ascii_only=False):
    return show_run.render(show_run.summary(source, proj, entries, lang, width), width, color, ascii_only)


def run_main(capsys, *argv):
    code = show_run.main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def flat(text):
    return " ".join(text.split())


def section(text, title):
    """The lines of one block: from the line starting with `title` to the next blank line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
    return lines[start:end]


def zone_rows(text, title="Zones"):
    """The zone rows of the table, without its header line(s) and the price line(s)."""
    return [row for row in section(text, title) if not row.startswith(title) and not row.lstrip().startswith("──")]


def scaled(proj, factor):
    """The fixture projection at another price level, e.g. an index or a penny stock."""
    proj = copy.deepcopy(proj)
    proj["as_of"]["price"] *= factor
    for side in ("supports", "resistances"):
        for zone in proj[side]:
            zone["zone"] = [None if edge is None else edge * factor for edge in zone["zone"]]
    low, high = proj["supports"][1]["zone"][0], proj["resistances"][1]["zone"][1]
    for scenario in proj["scenarios"]:
        scenario["low"] = scenario["low"].replace("40.6", f"{low:.6f}")
        scenario["high"] = scenario["high"].replace("49.6", f"{high:.6f}")
    return proj


# ---- contracts with the chart ---------------------------------------------------

def test_languages_have_the_same_keys():
    first = show_run.TEXT[LANGS[0]]
    for lang in LANGS[1:]:
        other = show_run.TEXT[lang]
        assert set(other) == set(first)
        for key, value in first.items():
            assert type(other[key]) is type(value)
            if isinstance(value, dict):
                assert set(other[key]) == set(value), key
            if isinstance(value, list):
                assert len(other[key]) == len(value) == 12
    assert set(first["levels"]) == {"low", "medium"}   # never "high"


def test_the_summary_speaks_the_chart_languages_with_the_chart_words():
    assert LANGS == sorted(plot_projection.TEXT) and show_run.FALLBACK_LANG == plot_projection.FALLBACK_LANG
    shared = ["months", "labels", "trend", "trend_is", "names", "levels", "below", "above", "decimal",
              "thousands", "footer", "no_date", "scenarios", "events", "drivers", "confidence"]
    for lang in LANGS:
        for key in shared:
            assert show_run.TEXT[lang][key] == plot_projection.TEXT[lang][key], (lang, key)


@pytest.mark.parametrize("value", [0.00004321, 0.05, 0.5, 1, 42.77, 999.999, 1234.5, 9999.99, 10_000, 95_000, 1_234_567.8])
def test_numbers_are_written_as_on_the_chart(value):
    assert show_run.price_digits(value) == plot_projection.price_digits(value)
    for lang in LANGS:
        digits = show_run.price_digits(value)
        assert show_run.format_number(value, digits, show_run.TEXT[lang]) == \
            plot_projection.format_number(value, digits, plot_projection.TEXT[lang])


@pytest.mark.parametrize("asked", ["pt", "pt-BR", "PT_br", " en ", "EN-us", "es", "", None, 7])
def test_language_follows_the_input_like_the_chart(asked, capsys):
    source = {"reply": {"language": asked}}
    assert show_run.language(None, source) is show_run.TEXT[plot_projection.chart_language(None, source)]
    assert show_run.language(None, {}) is show_run.TEXT["en"]


def test_lang_overrides_and_must_be_known(input_data, projection_data, tmp_path, write_json, capsys):
    assert "Zonas" in text_of(input_data, projection_data, lang="pt")
    assert "Zonas" in text_of(input_data, projection_data, lang="pt-BR")
    assert "Zones" in text_of(dict(input_data, reply={"language": "pt"}), projection_data, lang="en")
    code, out, err = run_main(capsys, write_json("i.json", input_data), write_json("p.json", projection_data), "--lang", "xx")
    assert code == 1 and out == "" and err.startswith("Error: no labels for language 'xx'")


# ---- what it shows ---------------------------------------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_everything_in_the_projection_is_there(input_data, projection_data, lang):
    t = show_run.TEXT[lang]
    num = lambda x: show_run.format_number(x, 2, t)
    text = text_of(dict(input_data, reply={"language": lang}), projection_data)
    lines = text.splitlines()
    header = flat("\n".join(lines[:lines.index("")]))   # one line when it fits, as in English at 80 columns
    assert lang != "en" or lines[1] == ""
    for part in ("PETR4.SA", num(44.06), "BRL", f"17 {t['months'][10]} 2026 17:36", t["labels"]["1-3m"],
                 f"12 {t['months'][1]} 2027", t["trend"]["down"]):
        assert part in header
    for side in ("supports", "resistances"):
        for zone in projection_data[side]:
            row = next(line for line in lines if line.split()[:1] == [zone["id"]])
            for edge in zone["zone"]:
                assert edge is None or num(edge) in row
            assert f"{round(zone['reach'] * 100)}%" in row and f"{round(zone['turn'] * 100)}%" in row
            assert t["strengths"].get(zone.get("strength"), "") in row
    for scenario in projection_data["scenarios"]:
        row = next(line for line in lines if line.split()[:1] == [t["names"][scenario["name"]]])
        assert f"{round(scenario['p'] * 100)}%" in row
    assert num(40.6) in section(text, t["scenarios"])[1] and num(49.6) in section(text, t["scenarios"])[2]
    assert t["confidence"].format(level=t["levels"]["low"]) in lines
    assert sum(line.startswith("  • ") for line in lines) == show_run.LIMITS_SHOWN
    assert f"21 {t['months'][11]} 2026 ex-dividend" in text
    assert "↓ Brent fell from about US$118 to about US$98" in text
    assert "http" not in text                      # sources stay in the JSON
    assert t["footer"] in flat(text)               # no entries file, so the chart's disclaimer closes it
    assert "setup" not in text.lower()


def test_zones_read_top_to_bottom_like_the_chart(input_data, projection_data):
    projection_data["supports"].reverse()          # the order in the file does not matter
    projection_data["resistances"].reverse()
    rows = section(text_of(input_data, projection_data), "Zones")[1:]
    assert [row.split()[0] for row in rows] == ["above", "R3", "R2", "R1", "──", "S1", "S2", "S3", "below"]
    assert "price 44.06 (inside S1)" in rows[4]
    projection_data["as_of"]["price"] = 44.9       # between S1 and R1
    assert "inside" not in text_of(input_data, projection_data)


@pytest.mark.parametrize("lang", LANGS)
def test_setups_are_the_entries_file(input_data, projection_data, lang):
    source = dict(input_data, reply={"language": lang})
    entries = trade_entries.build(source, projection_data)
    t = show_run.TEXT[lang]
    num = lambda x: show_run.format_number(x, 2, t)
    text = text_of(source, projection_data, entries, width=140)
    block = section(text, t["setups"])
    assert f"12 {t['months'][1]} 2027" in block[0]
    assert len(entries["entries"]) == 3
    for entry in entries["entries"]:
        row = next(line for line in block if entry["id"] in line)
        assert row.split()[0] == str(entry["rank"])
        for shown in (t["sides"][entry["side"]], t["orders"][entry["entry"]["order"]], num(entry["entry"]["price"]),
                      num(entry["stop"]["price"]), num(entry["target"]["price"]), num(entry["payoff"]["ratio"]),
                      f"{round(entry['odds']['fill'] * 100)}%", f"{round(entry['odds']['win_if_filled'] * 100)}%",
                      num(entry["edge"]["per_order"]), t["qualities"][entry["quality"]]):
            assert shown in row, shown
    for label in ("fill", "win_if_filled", t["edge"], t["ratio"], t["quality"]):
        assert label in block[1]
    assert entries["note"] in flat(text)           # verbatim, wrapped but never cut
    assert t["footer"] not in flat(text)           # the note already says it


def test_the_note_survives_a_narrow_terminal(input_data, projection_data, entries):
    for width in WIDTHS:
        assert entries["note"] in flat(text_of(input_data, projection_data, entries, width=width))


def test_an_entries_file_without_setups_says_why(input_data, projection_data):
    text = text_of(input_data, projection_data, {"task": "trade_entries", "error": "no zone has both edges"})
    assert "No setups: no zone has both edges" in text
    assert "Sem setups: " in text_of(input_data, projection_data, {"error": "x"}, lang="pt")


def test_events_outside_the_horizon_are_left_out(input_data, projection_data):
    projection_data["events"] = [{"date": "2027-06-01", "what": "too late"}, {"date": "2026-01-01", "what": "too early"},
                                 {"date": None, "what": "some day"}, {"date": "2026-11-17", "what": "same day"},
                                 {"date": "2027-02-12", "what": "last day"}]
    text = text_of(input_data, projection_data)
    assert "too late" not in text and "too early" not in text
    assert "no date some day" in text and "17 Nov 2026 same day" in text and "12 Feb 2027 last day" in text


# ---- layout ----------------------------------------------------------------------

def aligned(rows, per_block, mark="%"):
    """Every row of a block has its `mark`s in the same columns (a table too wide for the
    terminal goes on in a second block with the same rows), and the marks are there."""
    blocks = [rows[i:i + per_block] for i in range(0, len(rows), per_block)]
    places = [{tuple(i for i, ch in enumerate(row) if ch == mark) for row in block} for block in blocks]
    return len(rows) % per_block == 0 and all(len(found) == 1 for found in places) and any(found != {()} for found in places)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("lang", LANGS)
def test_no_line_is_wider_than_asked_and_columns_stay_aligned(input_data, projection_data, entries, lang, width):
    t = show_run.TEXT[lang]
    text = text_of(input_data, projection_data, entries, lang=lang, width=width)
    assert all(show_run.wlen(line) <= width for line in text.splitlines()), text
    assert all(line == line.rstrip() for line in text.splitlines())
    zones = zone_rows(text, t["zones"])
    assert aligned(zones, 8)
    assert aligned(section(text, t["scenarios"])[1:], 4)
    assert ("█" in "".join(zones)) == (width >= 80)     # the bars are the first thing to go
    for entry in entries["entries"]:
        assert any(row.split()[:1] == [str(entry["rank"])] for row in section(text, t["setups"]))


def test_the_default_fits_a_screen(input_data, projection_data, entries):
    assert len(text_of(input_data, projection_data, entries, width=80).splitlines()) <= 40
    assert len(text_of(input_data, projection_data, width=80).splitlines()) <= 30


@pytest.mark.parametrize("factor, price, edge", [(2000, "88,120", "81,200"), (0.001, "0.04406", "0.04060"),
                                                 (1e6, "44,060,000", "40,600,000")])
@pytest.mark.parametrize("width", [40, 80])
def test_very_large_and_very_small_prices(input_data, projection_data, factor, price, edge, width):
    proj = scaled(projection_data, factor)
    entries = trade_entries.build(input_data, projection_data)   # the setups at that level too
    for entry in entries["entries"]:
        for key in ("entry", "stop", "target"):
            entry[key]["price"] *= factor
    text = text_of(input_data, proj, entries, width=width)
    assert price in text.splitlines()[0] and edge in text
    assert all(show_run.wlen(line) <= width for line in text.splitlines())
    assert aligned(zone_rows(text), 8)
    digits = show_run.price_digits(proj["as_of"]["price"])
    for entry in entries["entries"]:
        assert f"{entry['target']['price']:,.{digits}f}" in text
    assert f"below {edge}" in "\n".join(section(text, "Scenarios"))


def test_long_and_non_ascii_text_is_cut_to_the_width(input_data, projection_data):
    projection_data["asset"]["ticker"] = "É" * 60
    projection_data["supports"][0]["id"] = "S1-with-a-name-nobody-asked-for" * 3
    projection_data["supports"][0]["strength"] = "非常に強い"
    projection_data["drivers"] = [{"what": "原油価格が下落した。" * 30, "pull": "down"},
                                  {"what": "linha\nquebrada\tcom tabulação e \x1b[31mcores", "pull": "up"},
                                  {"what": "no pull given " * 20}] + [{"what": f"driver {i}", "pull": "up"} for i in range(4)]
    projection_data["confidence"]["limits"] = ["ação é não-ASCII " * 20, 5, "second"]
    projection_data["events"] = [{"date": "2026-12-01", "what": "e" * 300, "effect": "x" * 300}] * 5
    for width in WIDTHS:
        text = text_of(input_data, projection_data, width=width)
        assert all(show_run.wlen(line) <= width for line in text.splitlines())
        assert "\x1b" not in text and "\t" not in text
    text = text_of(input_data, projection_data)
    assert "↑ linha quebrada com tabulação e" in text and "• no pull given" in text and "+3 more" in text
    assert "+2 more" in text and "• second" in text
    assert show_run.wlen("原油") == 4 and show_run.clip("原油価格", 5) == "原油…"


@pytest.mark.parametrize("missing", [["structure"], ["drivers"], ["events"], ["scenarios"], ["confidence"],
                                     ["structure", "drivers", "events", "scenarios", "confidence", "legend", "method"]])
def test_optional_sections_may_be_missing(input_data, projection_data, missing):
    for key in missing:
        del projection_data[key]
    text = text_of(input_data, projection_data)
    assert "S1" in text and "R1" in text
    assert ("trend" in text) == ("structure" not in missing)
    assert ("Scenarios" in text) == ("scenarios" not in missing)
    assert ("Drivers" in text) == ("drivers" not in missing)
    assert ("Events" in text) == ("events" not in missing)
    assert ("Confidence: ?" in text) == ("confidence" in missing)


def test_malformed_optional_sections_are_skipped(input_data, projection_data, entries):
    projection_data.update(structure="down", drivers=[3, {"pull": "up"}], events={"date": "x"},
                           scenarios=[{"name": "range"}, "x", {"name": "odd", "p": 0.4, "low": "under", "high": None}],
                           confidence=["low"])
    projection_data["asset"]["currency"] = None
    del projection_data["supports"][0]["strength"]
    entries["entries"] = [{"rank": 1}, "x", {"id": "only-an-id", "entry": "at the edge", "quality": "unheard of"}]
    text = text_of(input_data, projection_data, entries)
    assert "odd" in text and "40%" in text and "under" in text
    assert "only-an-id" in text and "unheard of" in text and show_run.MISSING in text


def test_a_side_with_only_a_tail_or_nothing(input_data, projection_data):
    projection_data["supports"] = projection_data["supports"][-1:]
    rows = section(text_of(input_data, projection_data), "Zones")
    assert [row.split()[0] for row in rows[-2:]] == ["──", "below"] and "below 37.40" in rows[-1]
    projection_data["resistances"] = []
    rows = section(text_of(input_data, projection_data), "Zones")
    assert [row.split()[0] for row in rows[1:]] == ["──", "below"]


def test_scenarios_whose_lines_cannot_be_read_show_their_own_words(input_data, projection_data):
    for scenario in projection_data["scenarios"]:
        scenario["low"], scenario["high"] = "the lower line", "the upper line"
    assert "the lower line · the upper line" in text_of(input_data, projection_data)


# ---- colour ----------------------------------------------------------------------

class Stream(io.StringIO):
    def __init__(self, terminal):
        super().__init__()
        self.terminal = terminal

    def isatty(self):
        return self.terminal


@pytest.mark.parametrize("terminal, flags, environ, expected", [
    (True, {}, {}, True),
    (False, {}, {}, False),                          # piped or redirected
    (False, {"color": True}, {}, True),              # relayed to a terminal by the pipeline
    (True, {"no_color": True}, {}, False),
    (True, {"color": True, "no_color": True}, {}, False),
    (True, {}, {"NO_COLOR": "1"}, False),
    (False, {"color": True}, {"NO_COLOR": "1"}, False),
    (True, {}, {"NO_COLOR": ""}, True),              # empty means unset (no-color.org)
    (True, {}, {"TERM": "dumb"}, False),
])
def test_when_colour_is_used(terminal, flags, environ, expected):
    args = Namespace(**{"color": False, "no_color": False, **flags})
    assert show_run.use_color(args, Stream(terminal), environ) is expected


def test_colour_changes_nothing_but_the_codes(input_data, projection_data, entries):
    plain = text_of(input_data, projection_data, entries)
    coloured = text_of(input_data, projection_data, entries, color=True)
    assert ANSI not in plain and ANSI in coloured
    assert re.sub(r"\033\[\d+m", "", coloured) == plain
    assert f"{ANSI}31mR1{ANSI}0m" in coloured and f"{ANSI}32mS1{ANSI}0m" in coloured
    assert f"{ANSI}31m↓{ANSI}0m Brent" in coloured and f"{ANSI}1m── price 44.06 (inside S1) ─" in coloured
    entries["entries"][0]["quality"], entries["entries"][1]["quality"] = "favourable", "unfavourable"
    coloured = text_of(input_data, projection_data, entries, color=True)
    assert f"{ANSI}32mfavourable{ANSI}0m" in coloured and f"{ANSI}31munfavourable{ANSI}0m" in coloured
    assert "marginal" not in coloured or f"m marginal{ANSI}" not in coloured
    assert not re.search(r"\033\[\d+m\s+\033\[0m", coloured)       # no styled blanks


def test_command_line_colour(input_path, projection_path, capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert ANSI not in run_main(capsys, input_path, projection_path)[1]          # captured: not a terminal
    assert ANSI in run_main(capsys, input_path, projection_path, "--color")[1]
    assert ANSI not in run_main(capsys, input_path, projection_path, "--color", "--no-color")[1]
    monkeypatch.setenv("NO_COLOR", "1")
    assert ANSI not in run_main(capsys, input_path, projection_path, "--color")[1]


def test_a_terminal_without_the_drawing_characters_gets_ascii(input_data, projection_data, entries):
    text = text_of(input_data, projection_data, entries, ascii_only=True)
    assert text.isascii() and "-- price 44.06 (inside S1) --" in text and "v Brent" in text
    assert show_run.encodable(Namespace(encoding="utf-8")) and not show_run.encodable(Namespace(encoding="ascii"))
    assert not show_run.encodable(Namespace(encoding="cp1252")) and not show_run.encodable(Namespace(encoding=None))


# ---- command line ----------------------------------------------------------------

def test_files_and_folder_give_the_same_summary(tmp_path, input_path, projection_path, input_data, projection_data,
                                                entries, write_json, capsys):
    entries_path = write_json("e.json", entries)
    code, by_files, err = run_main(capsys, input_path, projection_path, entries_path)
    assert code == 0 and err == "" and by_files == text_of(input_data, projection_data, entries)
    for names in (("input.json", "projection.json", "entries.json"),              # a run or saved folder
                  ("petr4_input.json", "petr4_projection.json", "petr4_entries.json")):   # --out-dir
        folder =tmp_path / ("run_" + names[0].split(".")[0])
        folder.mkdir()
        for name, data in zip(names, (input_data, projection_data, entries)):
            (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert run_main(capsys, folder) == (0, by_files, "")
        (folder / names[2]).unlink()                                               # entries are optional
        assert run_main(capsys, folder) == (0, text_of(input_data, projection_data), "")


def test_width_option_and_the_default(input_path, projection_path, capsys):
    out = run_main(capsys, input_path, projection_path, "--width", 50)[1]
    assert max(map(show_run.wlen, out.splitlines())) <= 50 < max(
        map(show_run.wlen, run_main(capsys, input_path, projection_path)[1].splitlines())) <= 80
    assert max(map(show_run.wlen, run_main(capsys, input_path, projection_path, "--width", 3)[1].splitlines())) \
        <= show_run.MIN_WIDTH


def test_error_paths_are_one_line_and_no_traceback(tmp_path, input_path, projection_path, projection_data,
                                                   write_json, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    half = tmp_path / "half"
    half.mkdir()
    (half / "input.json").write_text("{}", encoding="utf-8")
    no_price = copy.deepcopy(projection_data)
    del no_price["as_of"]["price"]
    bad_zone = copy.deepcopy(projection_data)
    bad_zone["resistances"][0]["zone"] = [45.3, "47"]
    no_side = copy.deepcopy(projection_data)
    del no_side["supports"]
    cases = [
        ([tmp_path / "nope.json", projection_path], "Input JSON not found"),
        ([input_path, tmp_path / "nope.json"], "Projection JSON not found"),
        ([input_path, projection_path, tmp_path / "nope.json"], "Entries JSON not found"),
        ([broken, projection_path], "Input JSON is not valid JSON"),
        ([input_path, write_json("list.json", [1, 2])], "Projection JSON must be a JSON object"),
        ([projection_path, input_path], "swapped"),
        ([input_path, input_path], "not a projection"),
        ([input_path], "is not a folder"),
        ([empty], "no input.json in"),
        ([half], "no projection.json in"),
        ([input_path, projection_path, projection_path, projection_path], "give INPUT.json PROJECTION.json"),
        ([input_path, write_json("a.json", no_price)], "projection.as_of.price: expected a number"),
        ([input_path, write_json("b.json", bad_zone)], "projection.resistances[0].zone: expected"),
        ([input_path, write_json("c.json", no_side)], "projection.supports: expected a list"),
        ([input_path, write_json("d.json", {"supports": []})], "projection.asset: expected an object"),
    ]
    for argv, message in cases:
        code, out, err = run_main(capsys, *argv)
        assert code == 1 and out == "", argv
        assert err.startswith("Error: ") and message in err and err.count("\n") == 1 and "Traceback" not in err, err


def test_an_unforeseen_failure_is_still_one_line(input_path, projection_path, monkeypatch, capsys):
    monkeypatch.setattr(show_run, "summary", lambda *args: 1 / 0)
    code, out, err = run_main(capsys, input_path, projection_path)
    assert code == 1 and out == "" and err.startswith("Error: cannot summarise") and err.count("\n") == 1


def test_as_a_script_on_the_committed_example(tmp_path):
    """The real command, in a pipe: plain text, exit 0, and exit 1 with one line on a bad file."""
    examples = ROOT / "examples"
    done = subprocess.run([sys.executable, str(ROOT / "show_run.py"), str(examples / "petr4_input.json"),
                           str(examples / "petr4_projection.json")], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.startswith("PETR4.SA  42.77 BRL · 18 Nov 2026 15:15") and ANSI not in done.stdout
    assert all(len(line) <= 80 for line in done.stdout.splitlines())
    done = subprocess.run([sys.executable, str(ROOT / "show_run.py"), str(tmp_path)], capture_output=True, text=True)
    assert done.returncode == 1 and done.stdout == "" and done.stderr.startswith("Error: no input.json in")


def test_it_does_not_load_matplotlib():
    code = "import sys; sys.path.insert(0, sys.argv[1]); import show_run; print('matplotlib' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True)
    assert done.stdout.strip() == "False", done.stderr


# ---- analysis, not advice ---------------------------------------------------------

ADVICE = re.compile(r"\b(buy|sell|hold|recommend\w*|should|must|advice|advis\w+|best|size|leverage|"
                    r"compr[ae]r?|vend[ae]r?|recomend\w*|dev[ei]\w*|melhor|tamanho|alavanc\w*)\b", re.IGNORECASE)


def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    else:
        yield node


@pytest.mark.parametrize("lang", LANGS)
def test_the_labels_give_no_advice(lang, input_data, projection_data):
    t = show_run.TEXT[lang]
    for key, value in t.items():
        if key != "footer":                         # the one place the words appear: "not a recommendation to ..."
            for text in strings(value):
                assert not ADVICE.search(text), (key, text)
    assert re.search(r"not a recommendation|não recomendação", t["footer"])
    assert "hypothetical" in show_run.TEXT["en"]["setups"].lower() and "hipotéticos" in show_run.TEXT["pt"]["setups"]
    # the whole summary, apart from the entries file's own note, which is quoted as it is
    source = dict(input_data, reply={"language": lang})
    entries = trade_entries.build(source, projection_data)
    text = text_of(source, projection_data, entries, width=140)
    assert entries["note"] in text
    assert not ADVICE.search(text.replace(entries["note"], ""))
    assert t["confidence"].format(level=t["levels"]["low"]) in text   # the probabilities come with their confidence


# ---- relayed by run_pipeline.py ---------------------------------------------------

PATH_LINE = ("input:", "projection:", "chart:", "entries:", "saved:")


@pytest.fixture
def again(tmp_path, input_path, projection_path, capsys):
    """again(*options) -> (exit code, stdout lines, stderr) of a plot-again run into tmp_path."""
    def run(*options):
        code = run_pipeline.main(["--input", str(input_path), "--projection", str(projection_path),
                                  "--out-dir", str(tmp_path / "out"), *map(str, options)])
        captured = capsys.readouterr()
        return code, captured.out.splitlines(), captured.err
    return run


def test_pipeline_prints_the_summary_before_the_paths(again, tmp_path, input_data, projection_data):
    code, lines, err = again()
    assert code == 0 and "Warning" not in err
    first_path = next(i for i, line in enumerate(lines) if line.startswith(PATH_LINE))
    assert all(line.startswith(PATH_LINE) for line in lines[first_path:])       # the paths are last
    assert [line.split(":")[0] for line in lines[first_path:]] == ["input", "projection", "chart", "entries"]
    assert lines[first_path - 1] == "" and lines[0].startswith("PETR4.SA  44.06 BRL")
    entries = json.loads((tmp_path / "out" / "petr4_entries.json").read_text(encoding="utf-8"))
    assert "\n".join(lines[:first_path]) + "\n" == text_of(input_data, projection_data, entries) + "\n"
    assert ANSI not in "\n".join(lines)                                         # captured: not a terminal


def test_pipeline_no_summary_prints_the_paths_only(again, monkeypatch):
    monkeypatch.setattr(run_pipeline, "SHOW", "must-not-be-run.py")
    code, lines, err = again("--no-summary")
    assert code == 0 and "Warning" not in err
    assert len(lines) == 4 and all(line.startswith(PATH_LINE) for line in lines)


def test_pipeline_summary_without_entries_and_in_the_chart_language(again):
    code, lines, _ = again("--no-entries", "--lang", "pt")
    assert code == 0 and "Zonas" in "\n".join(lines) and not any("etups" in line for line in lines)
    assert plot_projection.TEXT["pt"]["footer"] in flat("\n".join(lines))


@pytest.mark.parametrize("script", [None, "raise SystemExit('Error: nothing to show')", "import nothing_like_this",
                                    "print()", "import time; time.sleep(30)"])
def test_a_summary_that_fails_only_warns(again, tmp_path, monkeypatch, script):
    show = tmp_path / "show.py"
    if script:
        show.write_text(script, encoding="utf-8")
    monkeypatch.setattr(run_pipeline, "SHOW", show)
    if script and "sleep" in script:
        real = run_pipeline.run
        monkeypatch.setattr(run_pipeline, "run", lambda command, timeout=None: real(command, timeout and 0.5))
    code, lines, err = again()
    assert code == 0
    assert len(lines) == 4 and all(line.startswith(PATH_LINE) for line in lines)   # the paths, and nothing else
    warnings = [line for line in err.splitlines() if "Warning" in line]
    assert len(warnings) == 1 and warnings[0].startswith("Warning: no summary of the result (show.py)")
    assert "Traceback" not in err and err.splitlines()[-1] == warnings[0]


@pytest.mark.parametrize("terminal", [True, False])
def test_pipeline_tells_the_summary_whether_a_terminal_is_reading(tmp_path, monkeypatch, terminal):
    seen, real = [], run_pipeline.run

    def spy(command, timeout=None):
        seen.append([str(part) for part in command])
        return real(command, timeout)
    monkeypatch.setattr(run_pipeline, "run", spy)
    monkeypatch.setattr(run_pipeline.shutil, "get_terminal_size", lambda *a: (72, 24) if a else Namespace(columns=72))
    monkeypatch.setattr(run_pipeline.sys, "stdout", Stream(terminal))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert run_pipeline.main(["--input", str(ROOT / "examples" / "petr4_input.json"), "--projection",
                              str(ROOT / "examples" / "petr4_projection.json"), "--out-dir", str(tmp_path)]) == 0
    printed = run_pipeline.sys.stdout.getvalue()
    command = next(c for c in seen if c[1] == str(run_pipeline.SHOW))
    if terminal:
        assert command[-3:] == ["--color", "--width", "72"] and "--no-color" not in command
        assert ANSI in printed and printed.startswith("\n")
        assert max(show_run.wlen(re.sub(r"\033\[\d+m", "", line)) for line in printed.splitlines()[:-4]) <= 72
    else:
        assert command[-1] == "--no-color" and "--color" not in command and ANSI not in printed


def test_no_color_reaches_the_summary_through_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pipeline.sys, "stdout", Stream(True))
    monkeypatch.setenv("NO_COLOR", "1")
    assert run_pipeline.main(["--input", str(ROOT / "examples" / "petr4_input.json"), "--projection",
                              str(ROOT / "examples" / "petr4_projection.json"), "--out-dir", str(tmp_path)]) == 0
    printed = run_pipeline.sys.stdout.getvalue()
    assert "PETR4.SA  42.77 BRL" in printed and ANSI not in printed
