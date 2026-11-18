"""run_pipeline.py: the one-command wrapper, with Yahoo and Claude Code stubbed out.

`rz.py check` and `plot_projection.py` run for real (both are offline); only
`build_prompt.py` (network) and the `claude` call are replaced.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

import run_pipeline
import trade_entries
from conftest import entries_reply

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def option(command, flag):
    return command[command.index(flag) + 1]


@pytest.fixture(autouse=True)
def store(tmp_path_factory, monkeypatch):
    """rz.py save writes here, never into the repository's own projections/."""
    folder = tmp_path_factory.mktemp("store") / "kept"
    monkeypatch.setenv("RZ_PROJECTIONS_DIR", str(folder))
    return folder


@pytest.fixture
def calls():
    return {"build": [], "claude": [], "entries": [], "plot": []}


@pytest.fixture
def stub_build(monkeypatch, calls, input_path):
    """build_prompt.py writes the fixture input instead of calling Yahoo."""
    real_run = run_pipeline.run

    def fake_run(command, timeout=None):
        command = [str(part) for part in command]
        if command[1] == str(run_pipeline.BUILD):
            calls["build"].append(command)
            shutil.copyfile(input_path, option(command, "-o"))
            return run_pipeline.subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == str(run_pipeline.PLOT):
            calls["plot"].append(command)
        return real_run(command, timeout)
    monkeypatch.setattr(run_pipeline, "run", fake_run)


def between(text, tag):
    return json.loads(text.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0])


@pytest.fixture
def stub_claude(monkeypatch, calls, projection_path):
    """stub_claude(...) replaces call_claude. Step 2 (the call with a work folder) writes the
    fixture projection by default. Step 5 (the call without one) answers with setups computed
    from the two files in its prompt, or with `entries` when given: a str is sent as it is,
    anything else as JSON."""
    def install(text=None, code=0, envelope=None, write=True, entries=None, entries_code=0):
        def fake_call(command, timeout, progress=None, cwd=run_pipeline.ROOT):
            if "--add-dir" not in command:
                calls["entries"].append({"command": command, "cwd": cwd})
                reply = entries if entries is not None else entries_reply(
                    between(command[2], "input"), between(command[2], "projection"))
                reply = reply if isinstance(reply, str) else json.dumps(reply)
                return entries_code, json.dumps({"type": "result", "result": reply, "is_error": False,
                                                 "num_turns": 1, "total_cost_usd": 0.7}), ""
            calls["claude"].append(command)
            if write:
                target = Path(option(command, "--add-dir")) / "projection.json"
                target.write_text(projection_path.read_text(encoding="utf-8") if text is None else text,
                                  encoding="utf-8")
            return code, json.dumps(envelope or {"result": "Done.", "is_error": False}), ""
        monkeypatch.setattr(run_pipeline, "call_claude", fake_call)
    return install


def run_main(*argv):
    return run_pipeline.main([str(arg) for arg in argv])


# ---- file names ---------------------------------------------------------------

@pytest.mark.parametrize("ticker, stem", [
    ("PETR4.SA", "petr4"), ("NVDA", "nvda"), ("BTC-USD", "btc-usd"),
    ("^GSPC", "gspc"), ("EURUSD=X", "eurusdx"), ("vod.l", "vod"),
])
def test_stem_for(ticker, stem):
    assert run_pipeline.stem_for(ticker) == stem


@pytest.mark.parametrize("ticker", ["", ".SA", "../.."])
def test_stem_for_rejects_unusable_tickers(ticker):
    with pytest.raises(run_pipeline.PipelineError):
        run_pipeline.stem_for(ticker)


# ---- arguments ----------------------------------------------------------------

def test_unknown_arguments_pass_through_in_order():
    argv = ["PETR4.SA", "--horizon", "1-2w", "--out-dir", "runs", "--event", "2026-12-01: x",
            "--volume", "--lang", "en", "--language", "pt", "--max-rows", "52"]
    args, rest = run_pipeline.parse(argv)
    assert rest == ["PETR4.SA", "--horizon", "1-2w", "--event", "2026-12-01: x",
                    "--volume", "--language", "pt", "--max-rows", "52"]
    assert (args.out_dir, args.lang) == ("runs", "en")


def test_ticker_position_does_not_matter():
    assert run_pipeline.parse(["--horizon", "1-3m", "NVDA"])[1] == ["--horizon", "1-3m", "NVDA"]


def test_build_gets_the_passthrough_and_plot_only_what_was_given(tmp_path, stub_build, stub_claude, calls):
    stub_claude()
    assert run_main("PETR4.SA", "--horizon", "1-3m", "--research", "none", "--out-dir", tmp_path) == 0
    build = calls["build"][0]
    assert build[2:6] == ["PETR4.SA", "--horizon", "1-3m", "--research"]
    assert build[-2] == "-o"
    plot = calls["plot"][0]
    assert not {"--lang", "--theme", "--candles", "--dpi"} & set(plot)   # plot keeps its own defaults

    assert run_main("--input", tmp_path / "petr4_input.json", "--projection", tmp_path / "petr4_projection.json",
                    "--out-dir", tmp_path, "--lang", "en", "--theme", "dark", "--dpi", "50") == 0
    plot = calls["plot"][1]
    assert (option(plot, "--lang"), option(plot, "--theme"), option(plot, "--dpi")) == ("en", "dark", "50")
    assert "--candles" not in plot


# ---- the Claude Code command --------------------------------------------------

def test_claude_command_is_narrow(tmp_path):
    work = tmp_path.resolve()
    command = run_pipeline.claude_command(work / "input.json", work / "projection.json", work, "none")
    assert command[:2] == [run_pipeline.CLAUDE, "-p"]   # conftest swaps the binary so no test starts it
    assert not any("dangerously" in part or "bypass" in part for part in command)
    assert option(command, "--permission-mode") == "dontAsk"
    assert option(command, "--tools") == "Skill,Read,Write,Edit,Bash"
    allowed = command[command.index("--allowedTools") + 1:]
    assert allowed == [
        "Skill(reversal-zones-projection)",
        f"Read(/{work}/**)", f"Write(/{work}/**)", f"Edit(/{work}/**)",
        f"Bash({sys.executable} {run_pipeline.RZ} *)",
    ]
    prompt = command[2]
    assert str(work / "projection.json") in prompt and str(run_pipeline.RZ) in prompt
    assert "--model" not in command


def test_claude_command_adds_web_tools_only_with_research(tmp_path):
    for research, web in (("none", False), ("one_search", True), ("deep", True)):
        command = run_pipeline.claude_command("i", "p", tmp_path, research, model="sonnet")
        assert ("WebSearch" in option(command, "--tools")) is web
        assert ("WebSearch" in command) is web and ("WebFetch" in command) is web
        assert command[-2:] == ["--model", "sonnet"]


def test_prompt_makes_research_cite_pages_it_opened():
    """Real runs showed drivers written from the search summary and pinned to a
    page that did not say them, and a dividend amount one fetch away left out."""
    prompt = run_pipeline.claude_prompt("i", "p", "w")
    assert "`research` is not `none`" in prompt            # the rules are conditional on the input
    assert "skill's Research section" in prompt           # the rule itself lives in SKILL.md
    assert "opened with WebFetch" in prompt
    assert "from memory" in prompt
    assert not any(word in prompt.lower() for word in ("buy", "sell", "bypass"))


def test_claude_reply():
    assert run_pipeline.claude_reply('{"result": "Done.", "is_error": false}') == ("Done.", False)
    assert run_pipeline.claude_reply('{"result": "limit", "is_error": true}') == ("limit", True)
    assert run_pipeline.claude_reply("plain text") == ("plain text", False)
    text, _ = run_pipeline.claude_reply(json.dumps(
        {"result": "stuck", "permission_denials": [{"tool_name": "Bash"}, {"tool_name": "Bash"}]}))
    assert "2 tool call(s) denied" in text and "Bash" in text


def test_call_claude_returns_output():
    code, out, err = run_pipeline.call_claude([sys.executable, "-c", "print('hello')"], timeout=30)
    assert (code, out.strip(), err) == (0, "hello", "")


def test_call_claude_not_installed():
    with pytest.raises(run_pipeline.PipelineError, match="not found"):
        run_pipeline.call_claude(["surely-not-a-real-claude-binary"], timeout=5)


def test_call_claude_times_out_with_the_tail(monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "HEARTBEAT", 0.2)
    slow = [sys.executable, "-c", "import time; print('thinking', flush=True); time.sleep(60)"]
    with pytest.raises(run_pipeline.PipelineError) as error:
        run_pipeline.call_claude(slow, timeout=0.7)
    assert "did not finish within 0.7 s" in str(error.value) and "thinking" in str(error.value)
    assert "still running" in capsys.readouterr().err


# ---- orchestration ------------------------------------------------------------

def test_full_run_with_stubs(tmp_path, stub_build, stub_claude, calls, capsys):
    stub_claude()
    out_dir = tmp_path / "runs"
    assert run_main("PETR4.SA", "--research", "none", "--out-dir", out_dir, "--dpi", "50") == 0
    files = [out_dir / name for name in ("petr4_input.json", "petr4_projection.json", "petr4.png",
                                         "petr4_entries.json")]
    assert sorted(path.name for path in out_dir.iterdir()) == sorted(path.name for path in files)
    assert files[2].read_bytes().startswith(PNG_MAGIC)
    captured = capsys.readouterr()
    for path in files:
        assert str(path) in captured.out
    for step in ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]"):
        assert step in captured.err

    command = calls["claude"][0]
    work = Path(option(command, "--add-dir"))
    assert not work.exists()                               # the work folder is thrown away
    assert str(work / "input.json") in command[2]


def test_a_new_projection_is_kept_with_its_input(tmp_path, input_path, projection_path, store,
                                                 stub_build, stub_claude, capsys):
    stub_claude()
    assert run_main("PETR4.SA", "--out-dir", tmp_path / "runs", "--dpi", "50") == 0
    [folder] = [path for path in store.rglob("*") if path.is_dir() and (path / "projection.json").is_file()]
    assert (folder / "input.json").read_bytes() == input_path.read_bytes()
    assert (folder / "projection.json").read_bytes() == projection_path.read_bytes()
    assert f"saved:      {folder}" in capsys.readouterr().out
    assert "do not run the helper's save command" in run_pipeline.claude_prompt("i", "p", "w")


def test_no_save_keeps_nothing(tmp_path, store, stub_build, stub_claude, capsys):
    stub_claude()
    assert run_main("PETR4.SA", "--no-save", "--out-dir", tmp_path / "runs", "--dpi", "50") == 0
    assert not store.exists() and "saved:" not in capsys.readouterr().out


def test_a_projection_that_cannot_be_kept_is_still_plotted(tmp_path, input_data, write_json, store,
                                                           stub_build, stub_claude, capsys):
    input_data["now"]["time"] = "2026-11-18T10:00-03:00"   # no longer the projection's as_of.time
    stub_claude()
    assert run_main("--input", write_json("other_input.json", input_data),
                    "--out-dir", tmp_path / "runs", "--dpi", "50") == 0
    captured = capsys.readouterr()
    assert "not kept" in captured.err and "as_of.time" in captured.err
    assert not store.exists() and (tmp_path / "runs" / "petr4.png").is_file()
    # the same mismatch leaves nothing to build setups from: said in one line, and no file
    assert "No setups: projection.as_of.time differs from input.now.time" in captured.err
    assert not (tmp_path / "runs" / "petr4_entries.json").exists()


def test_name_and_chart_options(tmp_path, stub_build, stub_claude):
    stub_claude()
    chart = tmp_path / "pics" / "x.svg"
    chart.parent.mkdir()
    assert run_main("PETR4.SA", "--out-dir", tmp_path, "--name", "run7", "--chart", chart) == 0
    assert (tmp_path / "run7_input.json").is_file() and (tmp_path / "run7_projection.json").is_file()
    assert "<svg" in chart.read_text(encoding="utf-8")


def test_input_skips_the_download(tmp_path, input_path, stub_build, stub_claude, calls):
    stub_claude()
    assert run_main("--input", input_path, "--out-dir", tmp_path, "--dpi", "50") == 0
    assert calls["build"] == [] and len(calls["claude"]) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["petr4.png", "petr4_entries.json",
                                                                "petr4_projection.json"]


def test_projection_skips_claude(tmp_path, input_path, projection_path, stub_build, monkeypatch, calls):
    monkeypatch.setattr(run_pipeline, "call_claude", lambda *a, **k: pytest.fail("Claude must not run"))
    assert run_main("--input", input_path, "--projection", projection_path, "--no-entries",
                    "--out-dir", tmp_path, "--dpi", "50") == 0
    assert calls["build"] == []
    assert [path.name for path in tmp_path.iterdir()] == ["petr4.png"]   # nothing kept: not a new projection


@pytest.mark.parametrize("research, web", [("none", False), ("one_search", True)])
def test_web_tools_follow_the_input(tmp_path, input_data, write_json, stub_build, stub_claude, calls,
                                    research, web):
    stub_claude()
    input_data["research"] = research
    assert run_main("--input", write_json("asked.json", input_data),
                    "--out-dir", tmp_path / "out", "--dpi", "50") == 0
    assert ("WebSearch" in calls["claude"][0]) is web


# ---- step 5 by default: the setups are computed, with no Claude Code call --------

def test_entries_are_computed_without_claude_by_default(tmp_path, input_data, projection_data, store,
                                                        stub_build, stub_claude, calls, capsys):
    stub_claude()
    out_dir = tmp_path / "runs"
    assert run_main("PETR4.SA", "--out-dir", out_dir, "--dpi", "50") == 0
    written = json.loads((out_dir / "petr4_entries.json").read_text(encoding="utf-8"))
    assert written == trade_entries.build(input_data, projection_data) and len(written["entries"]) == 3
    assert calls["entries"] == [] and len(calls["claude"]) == 1      # only the projection needed Claude
    captured = capsys.readouterr()
    assert f"entries:    {out_dir / 'petr4_entries.json'}" in captured.out
    assert "[5/5] Trade setups: trade_entries.py" in captured.err
    [kept] = list(store.rglob("entries.json"))                       # next to the pair it was derived from
    assert json.loads(kept.read_text(encoding="utf-8")) == written


def test_computed_entries_need_no_claude_at_all(tmp_path, input_path, projection_path, capsys):
    """conftest makes the claude binary unfindable: a re-plot with setups must still succeed."""
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50") == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["petr4.png", "petr4_entries.json"]


# ---- step 5 with --entries-by-claude: Claude writes the setups' sentences ------

def test_entries_are_written_checked_and_kept(tmp_path, input_data, projection_data, store,
                                              stub_build, stub_claude, calls, capsys):
    stub_claude()
    out_dir = tmp_path / "runs"
    assert run_main("PETR4.SA", "--out-dir", out_dir, "--dpi", "50", "--entries-by-claude") == 0
    written = json.loads((out_dir / "petr4_entries.json").read_text(encoding="utf-8"))
    assert written == entries_reply(input_data, projection_data) and len(written["entries"]) == 3
    captured = capsys.readouterr()
    assert f"entries:    {out_dir / 'petr4_entries.json'}" in captured.out
    assert "1 turn · $0.70" in captured.err                 # the cost of the second call is shown too
    [kept] = list(store.rglob("entries.json"))              # next to the pair it was derived from
    assert json.loads(kept.read_text(encoding="utf-8")) == written
    assert (kept.parent / "projection.json").is_file()

    [call] = calls["entries"]
    command = call["command"]
    assert option(command, "--tools") == "" and "--allowedTools" not in command   # no tools at all
    assert option(command, "--permission-mode") == "dontAsk"
    assert not any("dangerously" in part or "bypass" in part.lower() for part in command[3:])
    assert run_pipeline.ROOT not in Path(call["cwd"]).resolve().parents            # no project context
    assert "{{" not in command[2] and between(command[2], "projection") == projection_data


def test_no_entries_stops_at_the_chart(tmp_path, stub_build, stub_claude, calls, capsys):
    stub_claude()
    assert run_main("PETR4.SA", "--no-entries", "--out-dir", tmp_path / "runs", "--dpi", "50") == 0
    captured = capsys.readouterr()
    assert calls["entries"] == [] and "entries:" not in captured.out
    assert "[4/4]" in captured.err and "/5]" not in captured.err
    assert not (tmp_path / "runs" / "petr4_entries.json").exists()


def test_entries_from_existing_files(tmp_path, input_path, projection_path, store, stub_build, stub_claude, calls):
    stub_claude()
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50", "--entries-by-claude") == 0
    assert calls["claude"] == [] and len(calls["entries"]) == 1       # only the setups needed Claude
    assert sorted(path.name for path in tmp_path.iterdir()) == ["petr4.png", "petr4_entries.json"]
    assert not store.exists()                                          # nothing new to keep


def test_a_fenced_reply_is_accepted(tmp_path, input_path, projection_path, entries_data, stub_build, stub_claude):
    stub_claude(entries="```json\n" + json.dumps(entries_data) + "\n```")
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50", "--entries-by-claude") == 0
    assert json.loads((tmp_path / "petr4_entries.json").read_text(encoding="utf-8")) == entries_data


def test_wrong_numbers_fail_but_keep_the_other_files(tmp_path, input_path, projection_path, entries_data,
                                                     stub_build, stub_claude, capsys):
    entries_data["entries"][0]["stop"]["price"] += 1.0
    stub_claude(entries=entries_data)
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50", "--entries-by-claude") == 1
    captured = capsys.readouterr()
    assert "failed trade_entries.py check" in captured.err and "stop.price" in captured.err
    assert "Traceback" not in captured.err
    assert f"chart:      {tmp_path / 'petr4.png'}" in captured.out and "entries:" not in captured.out
    assert (tmp_path / "petr4_entries.json").is_file()                 # kept for inspection


@pytest.mark.parametrize("stub, message", [
    ({"entries": "Here are three ideas."}, "did not reply with JSON"),
    ({"entries_code": 1}, "Claude Code failed (exit 1)"),
])
def test_entries_step_failures(tmp_path, input_path, projection_path, stub_build, stub_claude, capsys, stub, message):
    stub_claude(**stub)
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50", "--entries-by-claude") == 1
    captured = capsys.readouterr()
    assert message in captured.err and "Traceback" not in captured.err
    assert (tmp_path / "petr4.png").is_file() and "chart:" in captured.out


def test_entries_without_claude_installed(tmp_path, input_path, projection_path, monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "CLAUDE", "surely-not-a-real-claude-binary")
    assert run_main("--input", input_path, "--projection", projection_path, "--out-dir", tmp_path, "--dpi", "50", "--entries-by-claude") == 1
    assert "Without --entries-by-claude" in capsys.readouterr().err


# ---- error paths: one short message, exit 1, no traceback ---------------------

def fails_with(capsys, *argv):
    assert run_main(*argv) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Traceback" not in captured.err
    return captured.err


def test_usage_errors(tmp_path, input_path, projection_path, capsys):
    assert "ticker" in fails_with(capsys)
    assert "needs --input" in fails_with(capsys, "--projection", projection_path)
    assert "--horizon 1-2w" in fails_with(capsys, "--input", input_path, "--horizon", "1-2w")
    assert "--out-dir" in fails_with(capsys, "NVDA", "-o", "x.json", "--out-dir", tmp_path)
    assert "not found" in fails_with(capsys, "--input", tmp_path / "missing.json")


def test_input_that_is_not_json(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{ nope", encoding="utf-8")
    assert "not valid JSON" in fails_with(capsys, "--input", bad)


def test_build_failure_shows_its_message(tmp_path, monkeypatch, capsys):
    def failing(command, timeout=None):
        return run_pipeline.subprocess.CompletedProcess(command, 1, "", "No data for 'XXXX'. Check the Yahoo symbol.\n")
    monkeypatch.setattr(run_pipeline, "run", failing)
    err = fails_with(capsys, "XXXX", "--out-dir", tmp_path)
    assert "build_prompt.py failed" in err and "Check the Yahoo symbol" in err
    assert list(tmp_path.iterdir()) == []


def test_claude_not_installed(tmp_path, input_path, monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "CLAUDE", "surely-not-a-real-claude-binary")
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "was not found" in err and "--projection" in err


def test_claude_failure_shows_the_tail(tmp_path, input_path, stub_claude, capsys):
    stub_claude(code=1, envelope={"result": "Usage limit reached", "is_error": True}, write=False)
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "Claude Code failed (exit 1)" in err and "Usage limit reached" in err
    assert list(tmp_path.iterdir()) == []


def test_claude_reports_an_error_with_exit_zero(tmp_path, input_path, stub_claude, capsys):
    stub_claude(envelope={"result": "max turns", "is_error": True})
    assert "Claude Code failed" in fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)


def test_projection_file_missing(tmp_path, input_path, stub_claude, capsys):
    stub_claude(write=False, envelope={"result": "Here is the JSON in chat instead."})
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "without writing the projection file" in err and "in chat instead" in err


def test_projection_file_not_json(tmp_path, input_path, stub_claude, capsys):
    stub_claude(text="```json\n{}\n```")
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "not valid JSON" in err
    assert list(tmp_path.iterdir()) == []


def test_check_failure_lists_the_violations(tmp_path, input_path, projection_data, stub_claude, capsys):
    projection_data["confidence"]["level"] = "high"
    projection_data["scenarios"][0]["p"] += 0.2
    stub_claude(text=json.dumps(projection_data))
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "failed rz.py check" in err
    assert "never high" in err and "scenarios must sum to 1" in err
    assert [path.name for path in tmp_path.iterdir()] == ["petr4_projection.json"]   # kept for inspection


def test_check_crash_is_a_short_error(tmp_path, input_path, stub_claude, capsys):
    stub_claude(text='{"task": "reversal_zones"}')     # valid JSON, wrong shape: rz.py raises KeyError
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "failed rz.py check" in err and "KeyError" in err
