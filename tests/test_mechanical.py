"""mechanical.py and `zones --engine mechanical`: a complete projection with no Claude call.

Everything is offline. The engine's zones must be backtest.py's, its probabilities the
unadjusted `rz.py sim`, its file must pass `rz.py check`, and one input must always give
the same bytes.
"""
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import backtest
import batch_runs
import mechanical
import run_pipeline
import score_projections
import show_run
from conftest import FIXTURES, ROOT
from test_run_pipeline import calls, option, run_main, store, stub_build, stub_claude  # noqa: F401

EXAMPLE_INPUT = ROOT / "examples" / "petr4_input.json"
SCRIPT = ROOT / "mechanical.py"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def with_language(d, code):
    d = copy.deepcopy(d)
    d["reply"]["language"] = code
    return d


@pytest.fixture(params=["fixture", "example"])
def source(request, input_path):
    return load(input_path if request.param == "fixture" else EXAMPLE_INPUT)


# ---- the projection ------------------------------------------------------------------

@pytest.mark.parametrize("code", ["en", "pt", "pt-BR"])
def test_a_mechanical_projection_passes_check(source, code, rz):
    proj = mechanical.build(with_language(source, code))
    assert rz.problems(proj) == []                      # complete=True: what `rz.py check` applies
    assert proj["confidence"]["level"] == "low" and proj["drivers"] == []


def test_the_command_writes_a_file_rz_check_accepts(tmp_path, input_path):
    out = tmp_path / "projection.json"
    done = subprocess.run([sys.executable, SCRIPT, input_path, "-o", out], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    check = subprocess.run([sys.executable, run_pipeline.RZ, "check", out], capture_output=True, text=True)
    assert check.stdout.strip() == "OK"


def test_keys_follow_the_skills_schema_in_order(input_path):
    proj = mechanical.build(load(input_path))
    assert list(proj) == ["task", "schema_version", "asset", "as_of", "horizon", "structure", "legend",
                          "supports", "resistances", "scenarios", "events", "drivers", "confidence", "method"]
    assert list(proj["supports"][0]) == ["id", "zone", "strength", "reach", "turn", "evidence"]
    assert list(proj["supports"][-1]) == ["id", "zone", "reach", "turn", "evidence"]
    assert list(proj["structure"]) == ["trend", "swing", "retraced", "from_high", "volatility"]
    assert list(proj["structure"]["swing"]) == ["low", "high"]
    assert proj["asset"] == {"ticker": "PETR4.SA", "currency": "BRL"}


def test_same_input_gives_the_same_bytes(tmp_path, input_path):
    files = []
    for name in ("a.json", "b.json"):
        files.append(tmp_path / name)
        done = subprocess.run([sys.executable, SCRIPT, input_path, "-o", files[-1]], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    assert files[0].read_bytes() == files[1].read_bytes()
    assert files[0].read_text(encoding="utf-8") == mechanical.render(mechanical.build(load(input_path)))


@pytest.mark.parametrize("wanted", [(3, 3), (2, 4), (1, 1), (5, 2)])
def test_zones_are_backtest_zones_and_probabilities_the_unadjusted_sim(source, wanted, rz):
    d = copy.deepcopy(source)
    d["want"]["supports"], d["want"]["resistances"] = wanted
    proj = mechanical.build(d)
    loaded = rz.unpack(d)
    zones = backtest.choose_zones(rz.facts(*loaded), *wanted)
    assert [z["zone"] for z in proj["supports"][:-1]] == zones["supports"]
    assert [z["zone"] for z in proj["resistances"][:-1]] == zones["resistances"]
    assert len(zones["supports"]) == wanted[0] and len(zones["resistances"]) == wanted[1]
    out = rz.baseline(loaded, zones)                               # the default sim method, no shocks
    for side in ("supports", "resistances"):
        assert [(z["id"], z["reach"], z["turn"]) for z in proj[side]] == \
               [(z["id"], z["reach"], z["turn"]) for z in out[side]]
    assert [s["p"] for s in proj["scenarios"]] == [s["p"] for s in out["scenarios"]]
    assert proj["horizon"] == {k: out["horizon"][k] for k in ("label", "close_basis", "closes", "first", "last")}
    assert f"sim method {rz.SIM_METHOD}" in proj["method"]


def test_clustered_zones_is_choose_zones_with_members(source, rz):
    facts = rz.facts(*rz.unpack(source))
    chosen = backtest.clustered_zones(facts)
    assert {side: [z["zone"] for z in zones] for side, zones in chosen.items()} == backtest.choose_zones(facts)
    known = set(backtest.candidates(facts))
    for zones in chosen.values():
        for zone in zones:
            assert set(zone["members"]) <= known


def test_evidence_names_every_fact_of_each_zone(source, rz):
    proj = mechanical.build(source)
    facts = rz.facts(*rz.unpack(source))
    chosen = backtest.clustered_zones(facts)
    for side in ("supports", "resistances"):
        for entry, zone in zip(proj[side], chosen[side]):
            text = " ".join(entry["evidence"])
            assert 1 <= len(entry["evidence"]) <= len(mechanical.KIND_ORDER)
            for price, kind, when in zone["members"]:
                assert f"{price:.2f}" in text, (entry["id"], price, kind)
                if kind not in ("retracement",):
                    assert str(when)[:7] in text
            assert entry["strength"] == mechanical.strength(zone["members"])
        tail = proj[side][-1]
        assert tail["id"] in ("below", "above") and len(tail["evidence"]) == 1


@pytest.mark.parametrize("members, expected", [
    ([], "weak"),
    ([(40.0, "retracement", 0.618)], "weak"),
    ([(40.0, "pivot_low", "2026-01-05"), (40.2, "pivot_low", "2026-03-02")], "moderate"),
    ([(40.0, "pivot_low", "2026-01-05"), (40.1, "retracement", 0.5)], "moderate"),
    ([(40.0, "pivot_low", "2026-01-05"), (40.1, "pivot_high", "2025-06-02"), (40.2, "recent_low", "2026-11-16")], "strong"),
    ([(40.0, "pivot_low", "2026-01-05"), (40.1, "retracement", 0.5), (40.2, "swing_low", "2026-01-05")], "strong"),
])
def test_strength_rule(members, expected):
    assert mechanical.strength(members) == expected


def test_a_side_with_too_few_clusters_is_filled_and_said_so(input_path):
    d = load(input_path)
    d["want"]["supports"] = 9
    proj = mechanical.build(d)
    filled = [z for z in proj["supports"][:-1] if z["evidence"][0].startswith("no fact from the rows")]
    assert filled and all(z["strength"] == "weak" for z in filled)
    assert any(", ".join(z["id"] for z in filled) in limit for limit in proj["confidence"]["limits"])


def test_portuguese_text_and_english_keys(input_path):
    en = mechanical.build(load(input_path))
    pt = mechanical.build(with_language(load(input_path), "pt"))
    assert pt["legend"]["reach"].startswith("P(a zona entra em jogo")
    assert pt["method"].startswith("Motor mecânico") and "200.000" in pt["method"]
    assert re.search(r"\d,\d{2}", pt["supports"][0]["evidence"][0])   # pt free text: decimal comma
    assert not re.search(r"\d,\d{2}", " ".join(en["supports"][0]["evidence"]))
    assert pt["supports"][0]["evidence"] != en["supports"][0]["evidence"]
    assert pt["events"][0]["what"] == "data ex-dividendo" and en["events"][0]["what"] == "ex-dividend"
    for a, b in zip(en["scenarios"], pt["scenarios"]):     # names stay English, the bounds keep their number
        assert a["name"] == b["name"] and a["p"] == b["p"]
        for key in ("low", "high"):
            assert re.findall(r"\d+(?:\.\d+)?", a[key]) == re.findall(r"\d+(?:\.\d+)?", b[key])
    assert [z["id"] for z in pt["supports"]] == [z["id"] for z in en["supports"]]
    assert pt["confidence"]["level"] == "low"


def test_an_unknown_language_gets_english(input_path):
    assert mechanical.build(with_language(load(input_path), "de")) == mechanical.build(load(input_path))


def test_touched_once_changes_the_legend(input_path):
    d = load(input_path)
    d["want"]["probability"] = "touched_once"
    assert "trades into it" in mechanical.build(d)["legend"]["reach"]


def test_research_cannot_be_honoured_and_the_limits_say_so(input_path):
    d = load(input_path)
    assert d["research"] == "one_search"
    asked = mechanical.build(d)["confidence"]["limits"]
    assert any("research (one_search)" in limit for limit in asked)
    d["research"] = "none"
    assert not any("research" in limit for limit in mechanical.build(d)["confidence"]["limits"])


def test_events_inside_the_horizon_pass_through_unsimulated(input_path):
    d = load(input_path)
    d["events"] = [{"date": "2026-12-21", "what": "ex-dividend"}, {"date": "2027-06-01", "what": "earnings"},
                   {"date": "2027-01-10", "what": "my note"}]
    proj = mechanical.build(d)
    assert [(e["date"], e["what"], e["certainty"]) for e in proj["events"]] == \
           [("2026-12-21", "ex-dividend", "medium"), ("2027-01-10", "my note", "medium")]   # June is past the horizon
    assert "not simulated" in proj["events"][0]["effect"]
    assert "shocks" not in proj


@pytest.mark.parametrize("code", ["en", "pt"])
def test_no_advice_language(input_path, code):
    text = mechanical.render(mechanical.build(with_language(load(input_path), code))).lower()
    for word in ("buy", "sell", "hold call", "compre", "venda", "compra", "stop", "target", "alvo"):
        assert word not in text, word


def test_the_command_refuses_a_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    done = subprocess.run([sys.executable, SCRIPT, bad], capture_output=True, text=True)
    assert done.returncode == 1 and done.stderr.startswith("Error: ") and "Traceback" not in done.stderr


# ---- `zones --engine mechanical` ----------------------------------------------------------

@pytest.fixture
def claude_forbidden(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the mechanical engine started Claude Code")
    monkeypatch.setattr(run_pipeline, "call_claude", refuse)


def test_mechanical_run_never_starts_claude(tmp_path, stub_build, claude_forbidden, calls, capsys, store, rz):
    assert run_main("PETR4.SA", "--engine", "mechanical", "--out-dir", tmp_path, "--dpi", "40") == 0
    err = capsys.readouterr().err
    assert "[2/5] Projection: mechanical engine, no Claude Code call" in err
    assert re.search(r"made in \d+\.\d s · no Claude Code call · \$0", err)
    assert calls["build"][0][-3:-2] == ["--research=none"]      # last before -o: the input asks for none
    proj = load(tmp_path / "petr4_projection.json")
    assert proj == mechanical.build(load(tmp_path / "petr4_input.json"))
    assert {p.name for p in tmp_path.iterdir()} >= {"petr4_input.json", "petr4_projection.json", "petr4.png",
                                                    "petr4_entries.json", "petr4_provenance.json"}
    saved = next(store.glob("PETR4.SA/*"))
    assert {p.name for p in saved.iterdir()} == {"input.json", "projection.json", "entries.json", "provenance.json"}


def test_mechanical_provenance(tmp_path, stub_build, claude_forbidden):
    assert run_main("PETR4.SA", "--engine", "mechanical", "--out-dir", tmp_path, "--dpi", "40") == 0
    record = load(tmp_path / "petr4_provenance.json")
    projection = record["projection"]
    assert projection["made_by"] == "mechanical" and "claude" not in projection
    assert projection["mechanical"]["engine"] == "mechanical.py"
    assert projection["mechanical"]["sim_method"] == backtest.rz.SIM_METHOD
    files = record["rules"]["files"]
    assert {"mechanical.py", "backtest.py", "rz.py", "build_prompt.py"} <= set(files) and "SKILL.md" not in files
    assert files["mechanical.py"] == run_pipeline.sha256_of(SCRIPT)
    assert record["rules"]["version"] != run_pipeline.rules_provenance()["version"]   # its own group
    assert record["entries"]["made_by"] == "trade_entries.py"


def test_a_research_option_gets_one_warning(tmp_path, stub_build, claude_forbidden, calls, capsys):
    assert run_main("PETR4.SA", "--research", "deep", "--engine", "mechanical", "--out-dir", tmp_path,
                    "--no-entries", "--dpi", "40") == 0
    err = capsys.readouterr().err
    assert err.count("Warning: research 'deep' is not done by the mechanical engine") == 1
    assert calls["build"][0][-3] == "--research=none"
    assert run_main("PETR4.SA", "--engine", "mechanical", "--out-dir", tmp_path, "--no-entries", "--dpi", "40") == 0
    assert "Warning" not in capsys.readouterr().err


def test_a_given_input_that_asks_for_research_is_warned_about(tmp_path, input_path, claude_forbidden, capsys):
    assert run_main("--input", input_path, "--engine", "mechanical", "--out-dir", tmp_path, "--no-entries",
                    "--dpi", "40") == 0
    assert "Warning: research 'one_search' is not done" in capsys.readouterr().err


def test_claude_stays_the_default(tmp_path, stub_build, stub_claude, calls):
    stub_claude()
    assert run_main("PETR4.SA", "--out-dir", tmp_path, "--no-entries", "--dpi", "40") == 0
    assert len(calls["claude"]) == 1
    assert "--research=none" not in calls["build"][0]
    assert load(tmp_path / "petr4_provenance.json")["projection"]["made_by"] == "claude-code"


@pytest.fixture
def user(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setenv(run_pipeline.CONFIG_ENV, str(path))

    def write(text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    return write


def test_config_and_command_line_precedence(user, tmp_path, stub_build, stub_claude, calls):
    assert run_pipeline.parse(["NVDA"])[0].engine == "claude"                      # built-in default
    user('engine = "mechanical"\n')
    assert run_pipeline.parse(["NVDA"])[0].engine == "mechanical"                  # the user file
    assert run_pipeline.parse(["NVDA", "--engine", "claude"])[0].engine == "claude"   # typed wins
    assert run_pipeline.parse(["NVDA", "--no-config"])[0].engine == "claude"
    stub_claude()
    assert run_main("PETR4.SA", "--out-dir", tmp_path, "--no-entries", "--dpi", "40") == 0
    assert calls["claude"] == []                                                    # the file's engine ran
    assert run_main("PETR4.SA", "--engine", "claude", "--out-dir", tmp_path, "--no-entries", "--dpi", "40") == 0
    assert len(calls["claude"]) == 1


def test_a_wrong_engine_in_a_config_file_stops_the_run(user, capsys):
    user('engine = "gpt"\n')
    with pytest.raises(SystemExit) as stopped:
        run_pipeline.parse(["NVDA"])
    assert stopped.value.code == 1
    assert "'engine' must be one of \"claude\", \"mechanical\"" in capsys.readouterr().err


# ---- batch, scoring and the summary -------------------------------------------------------

def test_batch_passes_the_engine_to_every_run():
    args, items, options = batch_runs.parse(["NVDA", "PETR4.SA", "--engine", "mechanical", "--jobs", "3"])
    assert items == ["NVDA", "PETR4.SA"] and options[:2] == ["--engine", "mechanical"] and args.jobs == 3
    assert batch_runs.child_command("NVDA", None, options)[3:6] == ["NVDA", "--engine", "mechanical"]


def test_a_mechanical_run_costs_nothing_in_a_batch(tmp_path, stub_build, claude_forbidden, capsys):
    assert run_main("PETR4.SA", "--engine", "mechanical", "--out-dir", tmp_path, "--dpi", "40") == 0
    printed = "\n".join(capsys.readouterr())
    assert batch_runs.cost_of(printed) == (0.0, "no Claude Code call")


def test_a_real_batch_with_the_mechanical_engine_is_free(tmp_path, monkeypatch, input_path):
    """The real pipeline in each child, as test_batch_runs.py runs it, with a `claude` that
    fails the test if it is ever started."""
    from test_batch_runs import CHILD, summary_rows
    marker = tmp_path / "claude-was-started"
    claude = tmp_path / "fake-claude"
    claude.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'w').close()\nraise SystemExit(1)\n",
                      encoding="utf-8")
    claude.chmod(0o755)
    child = tmp_path / "child.py"
    child.write_text(CHILD.format(root=str(ROOT), input=str(input_path), claude=str(claude)), encoding="utf-8")
    monkeypatch.setattr(batch_runs, "PIPELINE", child)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    code = batch_runs.main(["PETR4.SA", "VALE3.SA", "--engine", "mechanical", "--jobs", "2",
                            "--dpi", "40", "--no-summary"])
    assert code == 0 and not marker.exists()
    rows = summary_rows(work)
    assert [(r["status"], r["cost_usd"], r["cost_note"]) for r in rows] == [("ok", "0.00", "no Claude Code call")] * 2


def test_scoring_groups_the_mechanical_engine_on_its_own(tmp_path):
    folder = tmp_path / "saved"
    folder.mkdir()
    (folder / "provenance.json").write_text(json.dumps(
        {"projection": {"made_by": "mechanical", "mechanical": {"engine": "mechanical.py"}},
         "rules": {"version": "abc123def456"}}), encoding="utf-8")
    assert score_projections.made_by(str(folder)) == {"model": "mechanical", "rules": "abc123def456"}
    assert score_projections.model_of({"made_by": score_projections.made_by(str(folder))}) == "mechanical"


@pytest.mark.parametrize("lang, words", [("en", "Made by the mechanical engine (no Claude Code call, $0)"),
                                         ("pt", "Feito pelo motor mecânico (sem chamada ao Claude Code, US$ 0)")])
def test_the_summary_says_who_made_it(lang, words):
    record = {"projection": {"made_by": "mechanical", "mechanical": {}}, "rules": {"version": "abc123def456"}}
    line = show_run.made_by(record, show_run.TEXT[lang])
    assert line.startswith(words) and line.endswith("abc123def456")
