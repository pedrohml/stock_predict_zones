"""run_pipeline.py step 2: progress lines and the cost line, read from Claude Code's event stream.

Nothing here starts Claude Code. `fixtures/claude_stream.jsonl` is one real
`claude -p --output-format stream-json --verbose` run of the skill, cut down
(paths, ids and tool contents removed); the other streams are written by hand.
A small Python script stands in for the `claude` binary where a process is needed.
"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

import run_pipeline
from conftest import FIXTURES

STREAM = (FIXTURES / "claude_stream.jsonl").read_text(encoding="utf-8")
HELPER = "/repo/.venv/bin/python /repo/.claude/skills/reversal-zones-projection/scripts/rz.py"

# Stands in for `claude`: prints $FAKE_STREAM, writes the projection into the
# --add-dir folder unless $FAKE_NO_WRITE, exits with $FAKE_EXIT.
FAKE_CLAUDE = """#!{python}
import os, shutil, sys
args = sys.argv[1:]
if os.environ.get("FAKE_REJECTS_FLAGS"):
    sys.exit("error: option '--output-format <format>' argument 'stream-json' is invalid.")
if not os.environ.get("FAKE_NO_WRITE"):
    shutil.copyfile({projection!r}, os.path.join(args[args.index("--add-dir") + 1], "projection.json"))
sys.stdout.write(open(os.environ["FAKE_STREAM"], encoding="utf-8").read())
sys.stderr.write("x" * 200000)      # more than a pipe holds: stderr must be drained too
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture(autouse=True)
def store(tmp_path_factory, monkeypatch):
    """rz.py save writes here, never into the repository's own projections/."""
    folder = tmp_path_factory.mktemp("store") / "kept"
    monkeypatch.setenv("RZ_PROJECTIONS_DIR", str(folder))
    return folder


@pytest.fixture
def fake_claude(tmp_path, monkeypatch, projection_path):
    """fake_claude(stream, **env) makes `claude` a script that replays that stream."""
    def install(stream=STREAM, **env):
        script, replay = tmp_path / "fake-claude", tmp_path / "replay.jsonl"
        script.write_text(FAKE_CLAUDE.format(python=sys.executable, projection=str(projection_path)),
                          encoding="utf-8")
        script.chmod(0o755)
        replay.write_text(stream, encoding="utf-8")
        monkeypatch.setattr(run_pipeline, "CLAUDE", str(script))
        monkeypatch.setenv("FAKE_STREAM", str(replay))
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
    return install


def tool(name, ident=None, **given):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": name, "input": given}]}})


def tool_result(ident, content):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": ident, "content": content}]}})


def shown(capsys, stream, quiet=False):
    """The progress lines a stream produces, without their clock column."""
    progress = run_pipeline.Progress(quiet)
    for line in stream.splitlines():
        progress.feed(line)
    lines = capsys.readouterr().err.splitlines()
    assert all(re.fullmatch(r"      \S.* +\d+:\d\d", line) for line in lines), lines
    return [re.sub(r"\s+\d+:\d\d$", "", line).strip() for line in lines]


# ---- the parser ---------------------------------------------------------------

def test_a_recorded_run_reads_as_the_skills_workflow(capsys):
    assert shown(capsys, STREAM) == [
        "reading the input",
        "reading the skill's instructions",
        "measuring the candles (rz.py levels)",
        "choosing zones",
        "simulating price paths (rz.py sim)",
        "writing the projection",
        "validating (rz.py check)",
    ]


def test_the_recorded_result_gives_turns_and_cost():
    result = run_pipeline.claude_result(STREAM)       # not the last line of the stream
    assert (result["num_turns"], result["total_cost_usd"], result["is_error"]) == (8, 0.750123, False)
    assert run_pipeline.summary(result, 137.4) == "done in 2:17 · 8 turns · $0.75"
    assert run_pipeline.claude_reply(STREAM)[0].startswith("`check` prints OK")


def test_summary_leaves_out_what_was_not_reported():
    assert run_pipeline.summary({"num_turns": 1}, 59.9) == "done in 0:59 · 1 turn"
    assert run_pipeline.summary({"total_cost_usd": 0.0}, 60) == "done in 1:00 · $0.00"
    assert run_pipeline.summary({"total_cost_usd": None, "num_turns": "?"}, 3725) == "done in 62:05"
    assert run_pipeline.summary(None, 5) == "done in 0:05"
    assert run_pipeline.summary({"num_turns": 3, "total_cost_usd": 0.4}, 42, failed=True) \
        == "stopped after 0:42 · 3 turns · $0.40"


def test_repeated_stages_read_sensibly(capsys):
    stream = "\n".join([
        tool("Skill", "t0", skill="reversal-zones-projection"),
        tool("Read", "t1", file_path="/work/input.json"),
        tool("Read", "t2", file_path="/work/input.json", offset=200),      # second half of a long input
        tool("Bash", "t3", command=f"{HELPER} levels /work/input.json"),
        tool("Write", "t4", file_path="/work/zones.json", content="{}"),
        tool("Bash", "t5", command=f"{HELPER} sim /work/input.json /work/zones.json"),
        tool("Edit", "t6", file_path="/work/zones.json", old_string="a", new_string="b"),
        tool("Edit", "t7", file_path="/work/zones.json", old_string="c", new_string="d"),
        tool("Bash", "t8", command=f"{HELPER} sim /work/input.json /work/zones.json"),
        tool("Write", "t9", file_path="/work/projection.json", content="{}"),
        tool("Bash", "t10", command=f"{HELPER} check /work/projection.json"),
        tool_result("t10", [{"type": "text", "text": "confidence.level is never high\n"}]),
        tool("Edit", "t11", file_path="/work/projection.json", old_string="high", new_string="low"),
        tool("Bash", "t12", command=f"{HELPER} check /work/projection.json"),
        tool("Bash", "t12", command=f"{HELPER} check /work/projection.json"),   # the same call, sent twice
        tool_result("t12", "OK"),
        tool("Read", "t13", file_path="/work/input.json"),                  # said once only
    ])
    assert shown(capsys, stream) == [
        "reading the skill's instructions",
        "reading the input",
        "measuring the candles (rz.py levels)",
        "choosing zones",
        "simulating price paths (rz.py sim)",
        "adjusting the zones",
        "simulating again (rz.py sim, run 2)",
        "writing the projection",
        "validating (rz.py check)",
        "the check found problems to fix",
        "revising the projection",
        "validating again (rz.py check, run 2)",
    ]


def test_unknown_events_tools_and_garbage_are_skipped(capsys):
    stream = "\n".join([
        "not json at all", "", "[1, 2, 3]", '"a string"', "null", "{broken",
        '{"type": "system", "subtype": "something_new", "detail": {"deep": [1]}}',
        '{"type": "rate_limit_event"}', '{"type": "from_the_future", "message": 7}',
        '{"type": "assistant"}', '{"type": "assistant", "message": null}',
        '{"type": "assistant", "message": {"content": "just text"}}',
        '{"type": "assistant", "message": {"content": [null, 5, {"type": "tool_use"}, {"type": "hologram"}]}}',
        '{"type": "user", "message": {"content": [{"type": "tool_result", "content": null}]}}',
        tool("Bash", "b1", command="ls -la /"), tool("Bash", "b2", command=None), tool("Bash", "b3"),
        tool("Read", "r1", file_path="/work/notes.txt"), tool("Write", "w1", file_path="/work/scratch.json"),
        tool("Teleport", "x1", where="elsewhere"), tool("Read", "r2", file_path=None),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x2", "name": "Bash", "input": "not a dict"}]}}),
        tool("Bash", "b4", command=f"{HELPER} check /work/projection.json"),
    ])
    assert shown(capsys, stream) == ["validating (rz.py check)"]
    assert run_pipeline.claude_result(stream) is None
    assert run_pipeline.claude_reply(stream) == ("not json at all\n\n[1, 2, 3]\n\"a string\"\nnull\n{broken", False)


def test_web_research_shows_queries_and_hosts_never_contents(capsys):
    stream = "\n".join([
        tool("WebSearch", "s1", query="Petrobras\x1b[31m dividend\n2026"),
        tool_result("s1", "SECRET PAGE CONTENTS " * 50),
        tool("WebSearch", "s2", query="q" * 300),
        tool("WebFetch", "f1", url="https://example.com/a/very/long/path?token=abc", prompt="summarise"),
        tool_result("f1", "SECRET PAGE CONTENTS"),
        tool("WebFetch", "f2", url="not a url"),
    ])
    lines = shown(capsys, stream)
    assert lines[0] == "web search 1: Petrobras [31m dividend 2026"
    assert lines[1].startswith("web search 2: qqq") and lines[1].endswith("...") and len(lines[1]) == 50
    assert lines[2:] == ["reading a web page (example.com)", "reading a web page (unknown host)"]
    assert "SECRET" not in "".join(lines) and "token" not in "".join(lines)


def test_quiet_progress_prints_nothing(capsys, monkeypatch):
    assert shown(capsys, STREAM, quiet=True) == []
    monkeypatch.setattr(run_pipeline, "HEARTBEAT", 0)
    run_pipeline.Progress(quiet=True).beat()
    assert capsys.readouterr().err == ""


def test_heartbeat_only_after_a_silence(capsys, monkeypatch):
    progress = run_pipeline.Progress()
    progress.beat()
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(run_pipeline, "HEARTBEAT", 0)
    progress.beat()
    assert "... still running" in capsys.readouterr().err


def test_readable_keeps_claudes_sentences_only():
    text = run_pipeline.readable(STREAM)
    assert text.splitlines()[0] == "Baseline looks good. Now I'll assemble the projection with a small, documented nudge."
    assert "tool_use" not in text and "rate_limit" not in text


# ---- the process --------------------------------------------------------------

def test_call_claude_feeds_progress_line_by_line():
    seen = []

    class Recorder(run_pipeline.Progress):
        def feed(self, text):
            seen.append(text)

    script = "import sys; print('one'); print('two'); sys.stderr.write('warn'); sys.exit(3)"
    assert run_pipeline.call_claude([sys.executable, "-c", script], 30, Recorder()) == (3, "one\ntwo\n", "warn")
    assert seen == ["one\n", "two\n"]


def test_timeout_fires_on_a_chatty_stream(monkeypatch, capsys):
    monkeypatch.setattr(run_pipeline, "HEARTBEAT", 0.2)
    chatty = ("import json, sys, time\n"
              "while True:\n"
              "    print(json.dumps({'type': 'system', 'subtype': 'thinking_tokens'}), flush=True)\n"
              "    sys.stderr.write('e' * 1000)\n"
              "    time.sleep(0.01)\n")
    started = time.monotonic()
    with pytest.raises(run_pipeline.PipelineError, match="did not finish within 0.8 s"):
        run_pipeline.call_claude([sys.executable, "-c", chatty], timeout=0.8)
    assert time.monotonic() - started < 10
    assert "still running" in capsys.readouterr().err      # events that print nothing do not count as life


def test_timeout_fires_when_stdout_closes_but_the_process_stays(capsys):
    lingering = "import os, time; os.close(1); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(run_pipeline.PipelineError, match="did not finish"):
        run_pipeline.call_claude([sys.executable, "-c", lingering], timeout=0.6)
    assert time.monotonic() - started < 10


def test_timeout_tail_is_readable_not_raw_events():
    script = (f"import time; print({tool('Bash', 'b', command='x')!r}, flush=True); "
              "print('{\"type\": \"assistant\", \"message\": {\"content\": "
              "[{\"type\": \"text\", \"text\": \"Working on the zones.\"}]}}', flush=True); time.sleep(60)")
    with pytest.raises(run_pipeline.PipelineError) as error:
        run_pipeline.call_claude([sys.executable, "-c", script], timeout=0.6, progress=run_pipeline.Progress(True))
    assert "Working on the zones." in str(error.value) and "tool_use" not in str(error.value)


# ---- the whole pipeline, with a script in place of `claude` -------------------

def run_main(*argv):
    # these runs are about step 2's stream, so they stop at the chart
    return run_pipeline.main([str(arg) for arg in argv] + ["--no-entries"])


def test_a_run_shows_stages_then_duration_turns_and_cost(tmp_path, input_path, fake_claude, capsys):
    fake_claude()
    assert run_main("--input", input_path, "--out-dir", tmp_path, "--dpi", "50") == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("[2/4]") or err[1].startswith("[2/4]")
    start = next(i for i, line in enumerate(err) if line.startswith("[2/4]"))
    stages = [re.sub(r"\s+\d+:\d\d$", "", line).strip() for line in err[start + 1:start + 8]]
    assert stages[0] == "reading the input" and stages[-1] == "validating (rz.py check)"
    assert re.fullmatch(r"      done in 0:0\d · 8 turns · \$0\.75", err[start + 8])
    assert err[start + 9].startswith("[3/4]")
    assert (tmp_path / "petr4.png").is_file()


def test_quiet_keeps_headers_and_the_summary(tmp_path, input_path, fake_claude, capsys):
    fake_claude()
    assert run_main("--input", input_path, "--out-dir", tmp_path, "--dpi", "50", "--quiet") == 0
    err = capsys.readouterr().err
    assert "reading the input" not in err and "rz.py levels" not in err
    assert re.search(r"\[2/4\].*\n      done in 0:0\d · 8 turns · \$0\.75\n\[3/4\]", err)


def test_no_cost_in_the_stream_no_cost_on_screen(tmp_path, input_path, fake_claude, capsys):
    lines = [json.loads(line) for line in STREAM.splitlines()]
    for event in lines:
        event.pop("total_cost_usd", None)
    fake_claude("\n".join(json.dumps(event) for event in lines) + "\n")
    assert run_main("--input", input_path, "--out-dir", tmp_path, "--dpi", "50") == 0
    err = capsys.readouterr().err
    assert re.search(r"done in 0:0\d · 8 turns\n", err) and "$" not in err


def test_a_stream_without_a_result_event_still_works(tmp_path, input_path, fake_claude, capsys):
    fake_claude("garbage line\n" + "\n".join(line for line in STREAM.splitlines() if '"result"' not in line))
    assert run_main("--input", input_path, "--out-dir", tmp_path, "--dpi", "50") == 0
    assert re.search(r"validating \(rz\.py check\) +0:0\d\n      done in 0:0\d\n", capsys.readouterr().err)


def fails_with(capsys, *argv):
    assert run_main(*argv) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Traceback" not in captured.err
    return captured.err


def test_is_error_in_the_result_event(tmp_path, input_path, fake_claude, capsys):
    result = {"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 3,
              "total_cost_usd": 0.31, "result": "Usage limit reached",
              "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ls"}}]}
    stream = "\n".join(STREAM.splitlines()[:11] + [json.dumps(result), '{"type": "system", "subtype": "task_summary"}'])
    fake_claude(stream + "\n")                              # exit code 0, as Claude Code can do
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path / "out")
    assert re.search(r"stopped after 0:0\d · 3 turns · \$0\.31\n", err)
    assert "Claude Code failed (exit 0)" in err and "Usage limit reached" in err
    assert "1 tool call(s) denied by the allowlist: Bash" in err
    assert '"type"' not in err and list((tmp_path / "out").iterdir()) == []


def test_non_zero_exit_shows_claudes_last_words(tmp_path, input_path, fake_claude, capsys):
    fake_claude("\n".join(STREAM.splitlines()[:-2]) + "\nAPI Error: overloaded\n", FAKE_EXIT=1, FAKE_NO_WRITE=1)
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "Claude Code failed (exit 1)" in err and "API Error: overloaded" in err
    assert "stopped after" in err and '"type"' not in err


def test_missing_projection_after_a_stream(tmp_path, input_path, fake_claude, capsys):
    fake_claude(FAKE_NO_WRITE=1)
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "without writing the projection file" in err and "`check` prints OK" in err


def test_an_older_cli_that_rejects_the_flags(tmp_path, input_path, fake_claude, capsys):
    fake_claude(FAKE_REJECTS_FLAGS=1)
    err = fails_with(capsys, "--input", input_path, "--out-dir", tmp_path)
    assert "Claude Code failed (exit 1)" in err and "argument 'stream-json' is invalid" in err
    assert "update Claude Code" in err
