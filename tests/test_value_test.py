"""value_test.py: Claude's adjustment against the baseline. Yahoo and Claude are always stubbed:
the candles are a synthetic random walk and a fake step 2 writes the files the pipeline would."""
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

import backtest as bt
import value_test as vt

TODAY = date(2026, 11, 19)
PATHS = 2000


def walk_rows(seed=3, first=date(2024, 1, 1), last=date(2026, 11, 18), price=30.0):
    rng, rows, day = np.random.default_rng(seed), [], first
    while day <= last:
        if day.weekday() < 5:
            open_, price = price, price * math.exp(rng.normal(0, 0.02))
            high = max(open_, price) * (1 + abs(rng.normal(0, 0.008)))
            low = min(open_, price) * (1 - abs(rng.normal(0, 0.008)))
            rows.append([day.isoformat(), open_, high, low, price])
        day += timedelta(days=1)
    return rows


DATA = {"rows": walk_rows(), "tz": "America/New_York", "currency": "USD", "venue": "Test Exchange"}


@pytest.fixture(autouse=True)
def yahoo(monkeypatch):
    calls = []

    def fake(ticker, start):
        calls.append(ticker)
        return {**DATA, "rows": [r for r in DATA["rows"] if r[0] >= start.isoformat()]}
    monkeypatch.setattr(bt, "download", fake)
    return calls


def fake_step2(shift=0.0, cost=0.40, model="claude-sonnet-5-5", fail=False):
    """A stand-in for launch(): writes what run_pipeline.py would (projection, provenance) from
    the mechanical zones and their baseline, moving `shift` of turn from S1 to the tail."""
    calls = []

    def launch(folder, requested, timeout=None):
        calls.append(folder)
        if fail:
            return 1, "", "Error: Claude Code failed\n      stopped after 0:40 · 3 turns · $0.12\n"
        d = json.loads((folder / "input.json").read_text())
        loaded = bt.rz.unpack(d)
        zones = bt.choose_zones(bt.rz.facts(*loaded))
        o = bt.projection(d, zones, bt.rz.baseline(loaded, zones, PATHS))
        if shift:
            o["supports"][0]["turn"] = round(o["supports"][0]["turn"] - shift, 2)
            o["supports"][-1]["turn"] = round(o["supports"][-1]["turn"] + shift, 2)
            o["supports"][-1]["reach"] = round(o["supports"][-1]["reach"] + shift, 2)
        (folder / "case_projection.json").write_text(json.dumps(o))
        prov = {"projection": {"claude": {"model": model, "total_cost_usd": cost, "num_turns": 7}}}
        (folder / "case_provenance.json").write_text(json.dumps(prov))
        return 0, "", "      done in 2:00 · 7 turns · $%.2f\n" % cost
    launch.calls = calls
    return launch


def main(tmp_path, *extra, tickers=("AAA", "BBB")):
    argv = [*tickers, "--out", str(tmp_path / "out"), "--start", "2026-07-01", "--end", "2026-10-30",
            "--paths", str(PATHS), "--quiet", *extra]
    return vt.main(argv, today=TODAY)


def summary(tmp_path):
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def test_conftest_refuses_the_real_step2(tmp_path):
    with pytest.raises(Exception):
        vt.launch(tmp_path, "sonnet")


def test_dates_before_the_training_cutoff_are_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vt, "launch", fake_step2())
    argv = ["AAA", "--out", str(tmp_path / "out"), "--start", "2026-06-15", "--quiet"]
    assert vt.main(argv, today=TODAY) == 1
    assert "training cutoff" in capsys.readouterr().err
    assert not (tmp_path / "out" / "cases").exists()


def test_default_start_is_the_day_after_the_cutoff():
    assert vt.month_end("2026-06") == date(2026, 6, 30)
    assert vt.default_cutoff("sonnet") == "2026-06"
    assert vt.model_ok("claude-sonnet-5-5", date(2026, 6, 30))
    assert vt.model_ok("claude-sonnet-5-5", date(2026, 7, 1)) is None
    assert "no known" in vt.model_ok("claude-unknown-9", date(2026, 7, 1))


def test_unadjusted_claude_equals_the_baseline(tmp_path, monkeypatch):
    fake = fake_step2()
    monkeypatch.setattr(vt, "launch", fake)
    assert main(tmp_path) == 0
    s = summary(tmp_path)
    assert s["summary"]["cases"] == len(fake.calls) > 4
    for kind in vt.KINDS:
        p = s["summary"]["paired"]["claude_minus_baseline"][kind]
        assert p["mean"] == 0 and p["a_better"] == p["b_better"] == 0
        # the fake used the mechanical zones, so all three versions agree
        assert s["summary"]["brier"]["claude"][kind]["brier"] == s["summary"]["brier"]["mechanical"][kind]["brier"]
    assert s["summary"]["moves"]["cases_moved"] == 0
    assert s["ledger"]["spent_usd"] == pytest.approx(0.40 * len(fake.calls))
    assert "VALUE TEST" in (tmp_path / "out" / "report.txt").read_text()


def test_an_adjustment_shows_up_as_a_paired_difference(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "launch", fake_step2(shift=0.05))
    main(tmp_path)
    s = summary(tmp_path)["summary"]
    assert s["moves"]["cases_moved"] == s["cases"]
    assert s["moves"]["max_moved_per_side"] == pytest.approx(0.05)
    turn = s["paired"]["claude_minus_baseline"]["turn"]
    assert turn["mean"] != 0 and turn["low"] <= turn["mean"] <= turn["high"]
    assert s["paired"]["baseline_minus_mechanical"]["turn"]["mean"] == 0


def test_budget_stops_launching_and_resume_never_pays_twice(tmp_path, monkeypatch):
    fake = fake_step2(cost=0.50)
    monkeypatch.setattr(vt, "launch", fake)
    main(tmp_path, "--max-cost", "1.6")
    # first reserve 0.60, then the highest cost seen (0.50): 0 + 0.6, 0.5 + 0.5, 1.0 + 0.5 fit, 1.5 + 0.5 not
    assert len(fake.calls) == 3
    s = summary(tmp_path)
    assert s["ledger"]["spent_usd"] == pytest.approx(1.5) and s["settings"]["unrun"] > 0

    again = fake_step2(cost=0.50)
    monkeypatch.setattr(vt, "launch", again)
    main(tmp_path, "--max-cost", "1.6")
    assert again.calls == []           # nothing new fits, and the three are not run again
    main(tmp_path, "--max-cost", "2.6")
    assert len(again.calls) == 2
    assert summary(tmp_path)["ledger"]["runs"] == 5


def test_parallel_jobs_respect_the_budget(tmp_path, monkeypatch):
    fake = fake_step2(cost=0.50)
    monkeypatch.setattr(vt, "launch", fake)
    main(tmp_path, "--max-cost", "2.0", "--jobs", "3")
    assert summary(tmp_path)["ledger"]["spent_usd"] <= 2.0


def test_a_failed_run_counts_its_cost_and_is_not_scored(tmp_path, monkeypatch):
    fake = fake_step2(fail=True)
    monkeypatch.setattr(vt, "launch", fake)
    main(tmp_path, tickers=("AAA",))
    s = summary(tmp_path)
    assert s["ledger"]["failed"] == len(fake.calls) and s["ledger"]["spent_usd"] == pytest.approx(0.12 * len(fake.calls))
    assert s["summary"]["cases"] == 0 and s["summary"]["skipped"]["step 2 failed"] == len(fake.calls)
    run = json.loads(next((tmp_path / "out" / "cases").glob("*/*/run.json")).read_text())
    assert run["cost_source"] == "summary line" and "Claude Code failed" in run["error"]


def test_a_model_with_a_later_or_unknown_cutoff_is_not_scored(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "launch", fake_step2(model="claude-unknown-9"))
    main(tmp_path, tickers=("AAA",))
    s = summary(tmp_path)["summary"]
    assert s["cases"] == 0 and s["skipped"]["look-ahead"] > 0


def test_inputs_hold_nothing_after_the_as_of_day(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "launch", fake_step2())
    main(tmp_path, tickers=("AAA",))
    for path in (tmp_path / "out" / "cases").glob("AAA/*/input.json"):
        d = json.loads(path.read_text())
        day = path.parent.name
        assert d["research"] == "none" and d["events"] == []
        assert d["now"]["time"].startswith(day)
        assert all(r[0] <= day for s in d["data"]["series"] for r in s["rows"])


def test_report_only_starts_no_run(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "launch", fake_step2())
    main(tmp_path, tickers=("AAA",))
    later = fake_step2()
    monkeypatch.setattr(vt, "launch", later)
    assert vt.main(["--out", str(tmp_path / "out"), "--report-only", "--quiet", "--paths", str(PATHS)],
                   today=TODAY) == 0
    assert later.calls == []


def test_bootstrap_is_reproducible():
    a = vt.bootstrap([0.1, -0.2, 0.05, 0.0])
    assert a == vt.bootstrap([0.1, -0.2, 0.05, 0.0]) and a["low"] <= a["mean"] <= a["high"]


def test_pipeline_command_never_saves_and_skips_entries(tmp_path):
    command = vt.pipeline_command(tmp_path, "sonnet")
    for flag in ("--no-save", "--no-entries", "--no-config", "--input"):
        assert flag in command
    assert command[command.index("--model") + 1] == "sonnet"


def test_a_run_that_never_reached_claude_costs_nothing_and_is_tried_again(tmp_path, monkeypatch):
    calls = []

    def broken(folder, model, timeout=None):
        calls.append(folder)
        return 1, "", "[1/4] Input: using x\nError: Input JSON not found: x\n"
    monkeypatch.setattr(vt, "launch", broken)
    main(tmp_path, tickers=("AAA",))
    first = len(calls)
    assert first > 0 and summary(tmp_path)["ledger"]["runs"] == 0
    monkeypatch.setattr(vt, "launch", fake_step2())
    main(tmp_path, tickers=("AAA",))
    assert summary(tmp_path)["ledger"]["runs"] == first
