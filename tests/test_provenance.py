"""Provenance: which data, rules and model made a result, and at what cost.

The data half is written by build_prompt.py into the input; the run half by
run_pipeline.py into provenance.json, beside the run's files and in the saved
history entry. Old files without either must keep working everywhere.
Nothing here touches the network or starts Claude Code.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

import build_prompt
import run_pipeline
import score_projections
import show_run
from conftest import FIXTURES, ROOT
from test_run_pipeline import calls, option, run_main, store, stub_build, stub_claude  # noqa: F401

STREAM = (FIXTURES / "claude_stream.jsonl").read_text(encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- data provenance: build_prompt.py --------------------------------------------

class FakeTicker:
    """Just enough of yfinance.Ticker for build_prompt.build(), offline."""
    ticker = "TEST.SA"
    calendar = {}

    def __init__(self, symbol):
        self.asked = []

    def history(self, period, interval, auto_adjust, actions):
        self.asked.append((interval, period, auto_adjust))
        freq = {"1d": "B", "1wk": "W-MON", "1mo": "MS"}[interval]
        count = {"5d": 5, "6mo": 120, "2y": 104, "5y": 60, "10y": 120}[period]
        index = pd.date_range(end="2026-11-16", periods=count, freq=freq, tz="America/Sao_Paulo")
        close = [40 + (i % 7) * 0.5 for i in range(count)]
        return pd.DataFrame({"Open": close, "High": [c + 1 for c in close], "Low": [c - 1 for c in close],
                             "Close": close, "Volume": [1000] * count}, index=index)

    def get_history_metadata(self):
        return {"exchangeTimezoneName": "America/Sao_Paulo", "currency": "BRL", "regularMarketPrice": 41.5}


@pytest.fixture
def built(monkeypatch):
    tickers = []

    def make(symbol):
        tickers.append(FakeTicker(symbol))
        return tickers[-1]
    monkeypatch.setattr(build_prompt.yf, "Ticker", make)
    args = build_prompt.arguments().parse_args(["TEST.SA", "--horizon", "1-3m"])
    return build_prompt.build(args), tickers[0]


def test_the_input_says_where_its_candles_came_from(built):
    prompt, ticker = built
    assert prompt["schema_version"] == build_prompt.SCHEMA_VERSION == 1
    record = prompt["provenance"]
    assert record["source"] == build_prompt.SOURCE and record["auto_adjust"] is False
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["downloaded_at"])
    assert all(asked[2] is False for asked in ticker.asked)          # what auto_adjust says is what was asked
    series = prompt["data"]["series"]
    assert [s["candles"] for s in record["series"]] == [s["candles"] for s in series] == ["1M", "1W"]
    for entry, one in zip(record["series"], series):
        assert (entry["rows"], entry["first"], entry["last"]) == (len(one["rows"]), one["rows"][0][0], one["rows"][-1][0])
    assert [(e["interval"], e["period"]) for e in record["series"]] == [("1mo", "5y"), ("1wk", "2y")]


def test_max_rows_is_recorded_as_asked(monkeypatch):
    monkeypatch.setattr(build_prompt.yf, "Ticker", FakeTicker)
    args = build_prompt.arguments().parse_args(["TEST.SA", "--horizon", "6-12m", "--max-rows", "50"])
    prompt = build_prompt.build(args)
    [entry] = prompt["provenance"]["series"]                          # 6-12m has no context series
    assert (entry["candles"], entry["rows"], entry["max_rows"]) == ("1M", 50, 50)


def test_the_data_hash_survives_rendering_and_catches_an_edit(built):
    prompt, _ = built
    loaded = json.loads(build_prompt.render(prompt))
    assert build_prompt.data_hash(loaded["data"]) == loaded["provenance"]["data_sha256"]
    loaded["data"]["series"][-1]["rows"][-1][4] += 0.01
    assert build_prompt.data_hash(loaded["data"]) != loaded["provenance"]["data_sha256"]


def test_the_new_formats_simulate_and_plot(built, tmp_path, run_sim, projection_data):
    import matplotlib.pyplot as plt
    import plot_projection
    prompt, _ = built
    source = json.loads(build_prompt.render(prompt))
    out = run_sim({"supports": [[38.5, 39.5]], "resistances": [[43.5, 44.5]]}, source=source, paths=2000)
    assert out["horizon"]["close_basis"] == "1W"
    # the plot reads both new fields as unknown extras, and draws
    source["now"]["time"], source["asset"]["ticker"] = projection_data["as_of"]["time"], "PETR4.SA"
    projection = dict(projection_data, schema_version=1)
    errors, _ = plot_projection.validate(source, projection)
    assert errors == []
    paths = {}
    for name, doc in (("input", source), ("projection", projection)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(doc), encoding="utf-8")
    plot_projection.build(argparse.Namespace(input=str(paths["input"]), projection=str(paths["projection"]),
                                             theme="light", lang="en", candles=None))
    plt.close("all")


# ---- the projection's schema_version: rz.py check -------------------------------------

def test_check_accepts_a_projection_with_or_without_schema_version(rz, projection_data):
    assert "schema_version" not in projection_data                   # the fixtures are the old format
    assert rz.problems(projection_data) == []
    assert rz.problems(dict(projection_data, schema_version=1)) == []
    assert rz.problems(dict(projection_data, schema_version=2)) == ["schema_version must be 1, got 2"]


def test_the_skill_example_carries_the_schema_version(rz):
    text = (ROOT / ".claude/skills/reversal-zones-projection/SKILL.md").read_text(encoding="utf-8")
    example = json.loads(text.split("## Output schema", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0])
    assert list(example)[:2] == ["task", "schema_version"] and example["schema_version"] in rz.SCHEMA_VERSIONS


# ---- what Claude Code reported: claude_facts() ----------------------------------------

def test_facts_from_the_recorded_stream():
    facts = run_pipeline.claude_facts(STREAM, 140.04, "opus")
    assert facts == {
        "requested_model": "opus", "model": "claude-opus-4-8", "claude_code_version": "2.1.323",
        "duration_ms": 137366, "duration_api_ms": 130722, "num_turns": 8, "total_cost_usd": 0.750123,
        "usage": {"input_tokens": 14, "cache_creation_input_tokens": 37579,
                  "cache_read_input_tokens": 210576, "output_tokens": 10759},
        "wall_seconds": 140.0}


def test_facts_never_guess_a_field_claude_code_did_not_report():
    assert run_pipeline.claude_facts("") == {}
    assert run_pipeline.claude_facts("not json\n{\"type\": \"result\"}\n") == {}
    plain = json.dumps({"result": "Done.", "is_error": False})          # --output-format json, no init event
    assert run_pipeline.claude_facts(plain) == {}


def test_models_used_come_from_model_usage_else_from_the_messages():
    result = {"type": "result", "num_turns": 2, "session_id": "abc",
              "modelUsage": {"claude-x": {"costUSD": 0.5}, "claude-haiku-y": {"costUSD": 0.01}}}
    facts = run_pipeline.claude_facts(json.dumps(result))
    assert facts["models_used"] == ["claude-haiku-y", "claude-x"] and facts["model_usage"] == result["modelUsage"]
    assert facts["session_id"] == "abc" and "model" not in facts
    stream = "\n".join(json.dumps(e) for e in [{"type": "assistant", "message": {"model": "claude-z", "content": []}},
                                               {"type": "result", "num_turns": 1}])
    assert run_pipeline.claude_facts(stream)["models_used"] == ["claude-z"]


# ---- the rules: hashes and git -----------------------------------------------------

def test_rules_name_every_rule_file_and_its_hash():
    rules = run_pipeline.rules_provenance()
    assert set(rules["files"]) >= {"SKILL.md", "rz.py", "trade_entries.py", "trade_entries.md"}
    for name, path in run_pipeline.RULE_FILES.items():
        assert rules["files"][name] == sha256(path)
    assert re.fullmatch(r"[0-9a-f]{12}", rules["version"])


def test_rules_name_the_default_sim_method(rz):
    """Read from rz.py as text; must be the number rz.py itself uses."""
    assert run_pipeline.default_sim_method() == rz.SIM_METHOD
    assert run_pipeline.rules_provenance()["sim_method"] == rz.SIM_METHOD


def test_rules_without_git_leave_the_commit_out(monkeypatch):
    def no_git(command, timeout=None):
        raise FileNotFoundError("git")
    monkeypatch.setattr(run_pipeline, "run", no_git)
    assert run_pipeline.git_state() is None
    assert "git" not in run_pipeline.rules_provenance()


def test_git_state_reads_commit_and_dirty(monkeypatch):
    answers = {"rev-parse": "0123456789abcdef\n", "status": " M rz.py\n"}
    monkeypatch.setattr(run_pipeline, "run", lambda command, timeout=None: run_pipeline.subprocess.CompletedProcess(
        command, 0, next(v for k, v in answers.items() if k in command), ""))
    assert run_pipeline.git_state() == {"commit": "0123456789abcdef", "dirty": True}


# ---- provenance.json of a run ------------------------------------------------------

@pytest.fixture
def stub_stream(monkeypatch, calls, projection_path):
    """Step 2 answers with the recorded stream and writes the fixture projection."""
    def fake_call(command, timeout, progress=None, cwd=run_pipeline.ROOT):
        calls["claude"].append(command)
        (Path(option(command, "--add-dir")) / "projection.json").write_text(
            projection_path.read_text(encoding="utf-8"), encoding="utf-8")
        return 0, STREAM, ""
    monkeypatch.setattr(run_pipeline, "call_claude", fake_call)


def test_a_run_records_its_provenance_beside_the_files_and_in_the_history(tmp_path, store, stub_build, stub_stream,
                                                                           input_path):
    out = tmp_path / "out"
    assert run_main("PETR4.SA", "--out-dir", out, "--model", "opus", "--dpi", "50") == 0
    record = json.loads((out / "petr4_provenance.json").read_text(encoding="utf-8"))
    [saved] = [p.parent for p in store.rglob("projection.json")]
    assert (saved / "provenance.json").read_bytes() == (out / "petr4_provenance.json").read_bytes()
    assert sorted(p.name for p in saved.iterdir()) == ["entries.json", "input.json", "projection.json", "provenance.json"]

    assert record["schema_version"] == run_pipeline.PROVENANCE_VERSION and record["kind"] == "reversal_zones_provenance"
    assert record["subject"] == {"ticker": "PETR4.SA", "as_of": "2026-11-17T17:36-03:00", "horizon": "1-3m"}
    assert record["input"] == {"sha256": sha256(out / "petr4_input.json")}             # the fixture predates provenance
    assert record["input"]["sha256"] == sha256(input_path)
    projection = record["projection"]
    assert projection["sha256"] == sha256(out / "petr4_projection.json") and projection["made_by"] == "claude-code"
    assert projection["claude"]["model"] == "claude-opus-4-8" and projection["claude"]["requested_model"] == "opus"
    assert projection["claude"]["total_cost_usd"] == 0.750123 and projection["claude"]["num_turns"] == 8
    assert record["rules"]["files"]["rz.py"] == sha256(run_pipeline.RZ)
    assert record["entries"] == {"sha256": sha256(out / "petr4_entries.json"), "made_by": "trade_entries.py"}


def test_data_provenance_of_the_input_is_carried_over(tmp_path, input_data, write_json, store, stub_stream):
    input_data["schema_version"] = 1
    input_data["provenance"] = {"source": "Yahoo Finance (yfinance)", "downloaded_at": "2026-11-17T20:36:02Z"}
    assert run_main("--input", write_json("new.json", input_data), "--out-dir", tmp_path / "o", "--no-entries",
                    "--dpi", "50") == 0
    record = json.loads((tmp_path / "o" / "petr4_provenance.json").read_text(encoding="utf-8"))
    assert record["input"]["schema_version"] == 1 and record["input"]["data"] == input_data["provenance"]


def test_entries_by_claude_record_their_own_call(tmp_path, store, stub_build, stub_claude, monkeypatch):
    stub_claude()
    assert run_main("PETR4.SA", "--out-dir", tmp_path, "--entries-by-claude", "--dpi", "50") == 0
    record = json.loads((tmp_path / "petr4_provenance.json").read_text(encoding="utf-8"))
    assert record["entries"]["made_by"] == "claude-code"
    assert record["entries"]["claude"]["num_turns"] == 1 and record["entries"]["claude"]["total_cost_usd"] == 0.7
    assert "model" not in record["projection"]["claude"]                # the stub reported none: none is guessed


def test_a_given_projection_never_overwrites_a_provenance(tmp_path, input_path, projection_path):
    """Plotting again into the folder of the run that made the projection keeps its record."""
    folder = tmp_path / "run"
    folder.mkdir()
    for name, source in (("petr4_input.json", input_path), ("petr4_projection.json", projection_path)):
        shutil.copyfile(source, folder / name)
    (folder / "petr4_provenance.json").write_text('{"kept": true}', encoding="utf-8")
    assert run_main("--input", folder / "petr4_input.json", "--projection", folder / "petr4_projection.json",
                    "--out-dir", folder, "--no-entries", "--dpi", "50") == 0
    assert json.loads((folder / "petr4_provenance.json").read_text()) == {"kept": True}


def test_a_given_projection_brings_its_provenance_into_a_new_run_folder(tmp_path, input_path, projection_path,
                                                                        monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, source in (("input.json", input_path), ("projection.json", projection_path)):
        shutil.copyfile(source, tmp_path / name)
    (tmp_path / "provenance.json").write_text('{"kept": true}', encoding="utf-8")
    assert run_main("--input", "input.json", "--projection", "projection.json", "--no-entries", "--dpi", "50") == 0
    [made] = list((tmp_path / "runs").rglob("provenance.json"))
    assert json.loads(made.read_text()) == {"kept": True}


# ---- the history: an extra file breaks nothing, old entries keep working ------------------

@pytest.fixture
def history(tmp_path, input_path, projection_path):
    """Two saved entries of the same projection: one with provenance.json, one without (old)."""
    root = tmp_path / "projections"
    for name, record in (("2026-11-17T1736_1-3m", {"projection": {"claude": {"model": "claude-opus-4-8"}},
                                                   "rules": {"version": "abcdef012345",
                                                             "git": {"commit": "0123456789", "dirty": False}}}),
                         ("2026-11-17T1736_1-3m_2", None)):
        folder = root / "PETR4.SA" / name
        folder.mkdir(parents=True)
        shutil.copyfile(input_path, folder / "input.json")
        shutil.copyfile(projection_path, folder / "projection.json")
        if record:
            (folder / "provenance.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def test_saved_and_list_ignore_the_provenance_file(rz, history, capsys):
    assert [Path(here).name for here, _ in rz.saved(str(history))] == ["2026-11-17T1736_1-3m", "2026-11-17T1736_1-3m_2"]
    rz.list_saved(str(history))
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_scores_say_which_model_made_each_projection(history, monkeypatch, capsys):
    monkeypatch.setattr(score_projections, "download", lambda *a: (_ for _ in ()).throw(
        score_projections.Unscorable("offline")))
    report = score_projections.run(str(history))
    first, second = report["projections"]
    assert first["made_by"] == {"model": "claude-opus-4-8", "rules": "abcdef012345", "commit": "0123456789",
                                "dirty": False}
    assert "made_by" not in second                                    # an old entry: nothing invented
    lines = score_projections.render_projection(first)
    assert "  made by claude-opus-4-8 · rules abcdef012345 · commit 0123456" in lines


def test_calibration_is_grouped_by_model():
    record = lambda model: {"final": True, "ticker": "X", "made_by": {"model": model} if model else {},
                            "supports": [{"forced": False, "reach": 0.8, "reached": True, "turn": 0.3, "turned": False}],
                            "resistances": [], "scenarios": [{"p": 0.5, "happened": True}]}
    c = score_projections.calibration([record("claude-a"), record("claude-a"), record(None)])
    assert c["by_model"]["claude-a"]["projections"] == 2
    assert c["by_model"][score_projections.UNRECORDED]["projections"] == 1
    assert c["by_model"]["claude-a"]["kinds"]["reach"] == {"n": 2, "brier": 0.04}
    text = "\n".join(score_projections.render_calibration(c, 0, 0))
    assert "claude-a: 2 projection(s); reach 0.040 (n 2)" in text


# ---- the text summary --------------------------------------------------------------

def test_show_names_the_model_and_rules_when_the_folder_has_provenance(history, capsys):
    folder = history / "PETR4.SA" / "2026-11-17T1736_1-3m"
    record = json.loads((folder / "provenance.json").read_text())
    record["projection"]["claude"].update(claude_code_version="2.1.323", num_turns=8, total_cost_usd=0.750123)
    (folder / "provenance.json").write_text(json.dumps(record), encoding="utf-8")
    assert show_run.main([str(folder), "--width", "120", "--no-color"]) == 0
    assert ("Made by claude-opus-4-8 · Claude Code 2.1.323 · 8 turns · $0.75 · rules abcdef012345"
            in capsys.readouterr().out)
    assert show_run.main([str(history / "PETR4.SA" / "2026-11-17T1736_1-3m_2"), "--no-color"]) == 0
    assert "Made by" not in capsys.readouterr().out


def test_show_finds_a_stemmed_provenance_next_to_the_files(tmp_path, input_path, projection_path):
    shutil.copyfile(projection_path, tmp_path / "petr4_projection.json")
    assert show_run.provenance_file(tmp_path / "petr4_projection.json") is None
    (tmp_path / "petr4_provenance.json").write_text("{}", encoding="utf-8")
    assert show_run.provenance_file(tmp_path / "petr4_projection.json") == tmp_path / "petr4_provenance.json"
    assert show_run.provenance_file(projection_path) is None
