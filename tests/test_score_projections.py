"""score_projections.py: saved projections against realised candles. Yahoo is always stubbed."""
import copy
import json
import sys
import types
from datetime import date, timedelta

import pandas as pd
import pytest

import score_projections as sp


# The fixture pair: PETR4.SA, weekly closes, as of Tue 2026-11-17 at 44.06 with that week forming,
# 13 closes to 2027-02-12. The last closed week in the input, 2026-11-09, closed at 46.48.
# Middle lines 40.6 and 49.6; the price starts inside S1 (43.2-44.6).

def daily(after, start="2026-10-01", end="2027-03-31", before=46.48):
    """Weekday candles: flat at `before` until the forming week, then `after(day)`."""
    rows, day = [], date.fromisoformat(start)
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            price = before if day < date(2026, 11, 16) else after(day)
            rows.append([day.isoformat(), price, price, price, price])
        day += timedelta(days=1)
    return rows


def quiet(day):
    return 44.0


def dip_then_rally(day):
    """Closes the week of 30 Nov at 40.0 (breaks the middle support), later one week at 48.0."""
    if date(2026, 11, 30) <= day <= date(2026, 12, 4):
        return 40.0
    if date(2027, 1, 4) <= day <= date(2027, 1, 8):
        return 48.0
    return 44.0


@pytest.fixture
def store(rz, capsys, tmp_path, input_data, projection_data, write_json):
    """store(...) saves one more projection into a temp history and returns its folder."""
    root = tmp_path / "history"

    def add(ticker="PETR4.SA", change=None):
        d, o = copy.deepcopy(input_data), copy.deepcopy(projection_data)
        d["asset"]["ticker"] = o["asset"]["ticker"] = ticker
        if change:
            change(d, o)
        rz.save(write_json("in.json", d), write_json("out.json", o), str(root))
        return capsys.readouterr().out.split()[-1]
    add.root = str(root)
    return add


@pytest.fixture
def yahoo(monkeypatch):
    """yahoo(rows or {ticker: rows | Unscorable}, splits=[], today=...) stubs the download."""
    calls = []

    def install(data, splits=(), today=date(2027, 6, 1)):
        def fake(ticker, interval, start, tz):
            calls.append((ticker, interval, start, tz))
            rows = data[ticker] if isinstance(data, dict) else data
            if isinstance(rows, Exception):
                raise rows
            return [r for r in rows if r[0][:10] >= start.isoformat()], list(splits)
        monkeypatch.setattr(sp, "download", fake)
        monkeypatch.setattr(sp, "today_in", lambda tz: today)
        return calls
    return install


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that forgets the stub fails instead of calling Yahoo."""
    def refuse(*args, **kwargs):
        raise AssertionError("test tried to download prices")
    monkeypatch.setattr(sp, "download", refuse)
    monkeypatch.delenv("RZ_PROJECTIONS_DIR", raising=False)


def cli(capsys, *argv):
    code = sp.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---- small pure pieces -----------------------------------------------------------

def test_aggregate_builds_weeks_and_months_from_days():
    rows = [["2026-11-12", 10, 12, 9, 11], ["2026-11-13", 11, 13, 10, 12],        # Thu, Fri
            ["2026-11-17", 12, 15, 11, 14], ["2026-11-19", 14, 14.5, 8, 9],        # Tue, Thu: Monday was a holiday
            ["2026-12-01", 9, 10, 9, 10]]
    assert sp.aggregate(rows, "1W") == [["2026-11-09", 10, 13, 9, 12], ["2026-11-16", 12, 15, 8, 9],
                                        ["2026-11-30", 9, 10, 9, 10]]
    assert sp.aggregate(rows, "1M") == [["2026-11-01", 10, 15, 8, 9], ["2026-12-01", 9, 10, 9, 10]]
    assert sp.aggregate(rows, "1D") == rows and sp.aggregate(rows, "1D") is not rows


def test_decimals_follow_the_input(input_data):
    main = input_data["data"]["series"][-1]
    assert sp.decimals(main) == 2
    main["rows"] = [["2026-01-01", 1.2345, 2.0, 1.0, 1.5]]
    assert sp.decimals(main) == 4


def test_buckets_group_by_tenths():
    pairs = [(0.0, False), (0.09, True), (0.1, False), (0.29, True), (0.3, True), (0.95, True), (1.0, False)]
    assert sp.buckets(pairs) == [
        {"from": 0, "to": 10, "n": 2, "mean_forecast": 0.045, "observed": 0.5},
        {"from": 10, "to": 20, "n": 1, "mean_forecast": 0.1, "observed": 0.0},
        {"from": 20, "to": 30, "n": 1, "mean_forecast": 0.29, "observed": 1.0},
        {"from": 30, "to": 40, "n": 1, "mean_forecast": 0.3, "observed": 1.0},
        {"from": 90, "to": 100, "n": 2, "mean_forecast": 0.975, "observed": 0.5}]
    # 0.07 * 100 is 7.000000000000001 and 0.7 * 100 is 70.00000000000001: neither may slip a bucket
    assert [b["from"] for b in sp.buckets([(0.07, True), (0.7, True), (0.6, True)])] == [0, 60, 70]


def test_brier_arithmetic():
    assert sp.brier([(1.0, True), (0.0, False)]) == 0
    assert sp.brier([(0.5, True), (0.5, False)]) == 0.25
    assert sp.brier([(0.8, True), (0.3, False), (0.6, False)]) == pytest.approx((0.04 + 0.09 + 0.36) / 3)


def record(final=True, ticker="X", reach=(0.9, 0.2), reached=(True, False), forced=False):
    entry = lambda i: {"id": f"S{i + 1}", "reach": reach[i], "turn": 0.1, "forced": forced and i == 0,
                       "reached": reached[i], "turned": reached[i]}
    return {"final": final, "ticker": ticker, "supports": [entry(0), entry(1)], "resistances": [],
            "scenarios": [{"name": "range", "p": 0.7, "happened": True}, {"name": "whipsaw", "p": 0.3, "happened": False}]}


def test_forecasts_skip_a_reach_that_was_true_by_definition():
    assert sp.forecasts(record()) == [("reach", 0.9, True), ("turn", 0.1, True), ("reach", 0.2, False),
                                      ("turn", 0.1, False), ("scenario", 0.7, True), ("scenario", 0.3, False)]
    assert ("reach", 0.9, True) not in sp.forecasts(record(forced=True))
    assert ("turn", 0.1, True) in sp.forecasts(record(forced=True))


def test_calibration_counts_only_ended_projections():
    c = sp.calibration([record(), record(ticker="x"), record(final=False), {"not_scored": "why", "ticker": "Y"}])
    assert (c["projections"], c["tickers"], c["forecasts"]) == (2, 1, 12)
    reach = c["kinds"]["reach"]
    assert (reach["n"], reach["base_rate"]) == (4, 0.5)
    assert reach["brier"] == pytest.approx((0.01 + 0.04) / 2)
    assert reach["brier_uninformed"] == reach["brier_base_rate"] == 0.25
    assert reach["buckets"] == [{"from": 20, "to": 30, "n": 2, "mean_forecast": 0.2, "observed": 0.0},
                                {"from": 90, "to": 100, "n": 2, "mean_forecast": 0.9, "observed": 1.0}]
    scenario = c["kinds"]["scenario"]
    assert scenario["brier"] == pytest.approx(0.09)
    assert scenario["brier_uninformed"] == pytest.approx((0.75 ** 2 + 0.25 ** 2) / 2)   # always 25%
    assert c["kinds"]["turn"]["brier_base_rate"] == 0.25
    assert len(c["caveats"]) == 3


def test_calibration_of_nothing():
    c = sp.calibration([record(final=False)])
    assert (c["projections"], c["forecasts"], c["kinds"]) == (0, 0, {})


# ---- scoring saved projections -----------------------------------------------------

def test_an_ended_projection_is_scored_from_weekly_candles_built_from_days(store, yahoo):
    folder = store()
    calls = yahoo(daily(dip_then_rally))
    report = sp.run(store.root)
    r, = report["projections"]
    assert (r["folder"], r["ticker"], r["horizon"], r["last"]) == (folder, "PETR4.SA", "1-3m", "2027-02-12")
    assert (r["state"], r["final"], r["measure"]) == ("ended", True, "close")
    assert r["closes"] == {"scheduled": 13, "counted": 13}
    assert r["window"] == ["2026-11-16", "2027-02-08"]
    assert r["low"] == [40.0, "2026-11-30"] and r["high"] == [48.0, "2027-01-04"]
    assert r["scenario"] == "breakdown" and r["notes"] == []
    assert {e["id"]: e["turned"] for e in r["supports"]} == {"S1": False, "S2": False, "S3": True, "below": False}
    assert {e["id"]: e["turned"] for e in r["resistances"]} == {"R1": False, "R2": True, "R3": False, "above": False}
    assert report["calibration"]["projections"] == 1
    # one download, daily, not dividend-adjusted is download()'s job; here: from before the forming candle
    assert calls == [("PETR4.SA", "1d", date(2026, 10, 3), "America/Sao_Paulo")]


def test_each_ticker_is_downloaded_once_from_the_earliest_date_needed(store, yahoo):
    def earlier(d, o):
        d["now"]["time"] = o["as_of"]["time"] = "2026-11-10T17:36-03:00"
        d["data"]["series"][-1]["rows"].pop()            # the week of 9 Nov is then the forming one
        d["now"]["price"] = o["as_of"]["price"] = 44.06
    store(); store(); store(change=earlier); store(ticker="VALE3.SA")
    calls = yahoo(daily(quiet, before=44.0))
    report = sp.run(store.root)
    assert sorted(calls) == [("PETR4.SA", "1d", date(2026, 9, 26), "America/Sao_Paulo"),
                             ("VALE3.SA", "1d", date(2026, 10, 3), "America/Sao_Paulo")]
    assert len(report["projections"]) == 4


def test_a_split_after_the_projection_is_not_scored(store, yahoo):
    store()
    yahoo(daily(quiet), splits=[["2026-06-01", 3.0], ["2026-12-10", 2.0]])
    r, = sp.run(store.root)["projections"]
    assert r["not_scored"] == ("stock split after the projection, on 2026-12-10 (2 for 1): "
                               "its zone prices are on the old scale")
    assert "state" not in r


def test_a_split_before_the_projection_does_not_matter(store, yahoo):
    store()
    yahoo(daily(quiet), splits=[["2026-11-17", 2.0]])     # on the day itself: already in the input's prices
    assert "not_scored" not in sp.run(store.root)["projections"][0]


def test_prices_on_another_scale_are_not_scored(store, yahoo):
    store()
    yahoo(daily(quiet, before=23.24))                      # the input says that week closed at 46.48
    r, = sp.run(store.root)["projections"]
    assert r["not_scored"].startswith("realised prices do not match the input's (candle 2026-11-09 closed at 46.48")


def test_holidays_are_noted_and_scored_on_what_exists(store, yahoo):
    store()
    rows = [r for r in daily(quiet) if not "2026-12-21" <= r[0] <= "2026-12-25"]     # a week without sessions
    yahoo(rows)
    r, = sp.run(store.root)["projections"]
    assert r["closes"] == {"scheduled": 13, "counted": 12} and r["final"]
    assert r["notes"] == ["12 of the 13 scheduled closes exist (market holidays or missing data); judged on those"]


def test_an_ended_projection_with_most_candles_missing_is_not_scored(store, yahoo):
    store()
    yahoo(daily(quiet, end="2026-12-18"))
    r, = sp.run(store.root)["projections"]
    assert r["not_scored"] == "only 5 of the 13 scheduled candles were found"


def test_an_open_projection_is_provisional(store, yahoo):
    store()
    yahoo(daily(dip_then_rally, end="2026-12-09"), today=date(2026, 12, 9))
    report = sp.run(store.root)
    r, = report["projections"]
    assert (r["state"], r["final"]) == ("open", False)
    assert r["closes"] == {"scheduled": 13, "counted": 3}       # the week of 7 Dec is still running
    assert r["scenario"] == "breakdown"
    assert report["calibration"]["projections"] == 0


def test_a_projection_made_today_waits(store, yahoo, capsys):
    store()
    yahoo(daily(quiet, end="2026-11-17"), today=date(2026, 11, 17))
    code, out, _ = cli(capsys, store.root)
    assert code == 0
    assert "NOT SCORED YET: no candle of the horizon has closed yet" in out
    assert "No horizon has ended yet" in out


def test_prices_are_rounded_like_the_input(store, yahoo):
    store()
    yahoo(daily(lambda day: 43.199999809))                 # float32 noise around S1's far edge, 43.2
    r, = sp.run(store.root)["projections"]
    assert r["low"][0] == 43.2 and r["supports"][0]["broken"] is False


def test_one_failed_download_does_not_stop_the_rest(store, yahoo, capsys):
    store(ticker="NOPE.SA"); store()
    yahoo({"NOPE.SA": sp.Unscorable("no 1d candles for NOPE.SA from Yahoo since 2026-10-03 "
                                    "(unknown symbol or network failure)"),
           "PETR4.SA": daily(quiet)})
    code, out, err = cli(capsys, store.root)
    assert code == 0 and err == ""
    assert "NOT SCORED: no 1d candles for NOPE.SA from Yahoo since 2026-10-03" in out
    assert "PETR4.SA 1-3m  as of 2026-11-17T17:36-03:00 at 44.06  ENDED 2027-02-12" in out
    assert "1 projection(s) of 1 ticker(s), 19 forecast probabilities; 0 open and 1 not scored are left out" in out
    assert "Traceback" not in out


def test_when_every_download_fails_the_exit_code_is_1(store, yahoo, capsys):
    store(); store()
    calls = yahoo(sp.Unscorable("could not fetch PETR4.SA from Yahoo (ConnectionError: offline)"))
    code, out, _ = cli(capsys, store.root)
    assert code == 1 and len(calls) == 1                   # the failure is cached too
    assert out.count("NOT SCORED: could not fetch PETR4.SA from Yahoo (ConnectionError: offline)") == 2


def test_broken_saved_folders_are_listed_with_a_reason(store, yahoo, tmp_path):
    first, second, third = store(), store(), store()
    (tmp_path / "history" / "PETR4.SA" / "2026-11-17T1736_1-3m" / "input.json").unlink()
    path = tmp_path / "history" / "PETR4.SA" / "2026-11-17T1736_1-3m_2" / "input.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["now"]
    path.write_text(json.dumps(data), encoding="utf-8")
    yahoo(daily(quiet))
    records = sp.run(str(tmp_path / "history"))["projections"]
    reasons = {r["folder"]: r.get("not_scored") for r in records}
    assert reasons[first] == "input.json is missing from the saved folder"
    assert reasons[second].startswith("malformed saved files (KeyError")
    assert reasons[third] is None


def test_an_unknown_candle_size_is_not_scored(store, yahoo):
    def odd(d, o):
        d["data"]["series"][-1]["candles"] = d["want"]["close_basis"] = "4h"
    store(change=odd)
    yahoo(daily(quiet))
    assert sp.run(store.root)["projections"][0]["not_scored"] == "no price source for 4h candles"


# ---- command line ---------------------------------------------------------------------

def test_cli_table(store, yahoo, capsys):
    folder = store()
    yahoo(daily(dip_then_rally))
    code, out, err = cli(capsys, store.root)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == "PETR4.SA 1-3m  as of 2026-11-17T17:36-03:00 at 44.06  ENDED 2027-02-12"
    assert lines[1] == ("  13 of 13 closes (close measure), 2026-11-16 to 2027-02-08; "
                        "low 40 (2026-11-30), high 48 (2027-01-04)")
    assert lines[2].split() == ["id", "zone", "reach", "reached", "broken", "turn", "turned"]
    assert [line.split()[0] for line in lines[3:11]] == ["above", "R3", "R2", "R1", "S1", "S2", "S3", "below"]
    assert lines[7].split() == ["S1", "43.2", "-", "44.6", "1.00", "yes", "(started", "inside)", "yes", "0.20", "no"]
    assert lines[10].split()[:4] == ["below", "open", "-", "37.4"] and lines[10].split()[5:7] == ["no", "-"]
    assert lines[11].startswith("  scenario: breakdown  (forecast: breakdown 0.")
    assert lines[12] == f"  {folder}"
    assert "so far" not in out and "PROVISIONAL" not in out
    text = out[out.index("CALIBRATION, ended projections only"):]
    assert "1 projection(s) of 1 ticker(s), 19 forecast probabilities" in text
    assert "Small samples are anecdote, not evidence" in text and "not independent" in text
    assert "does not show that the method works or fails" in text
    for kind, n in (("reach", 7), ("turn", 8), ("scenario", 4)):
        assert f"  {kind}: {n} forecasts, " in text
    assert "Brier score" in text and "mean forecast" in text
    for word in ("buy", "sell", "profit", "performance"):
        assert word not in out.lower()


def test_cli_marks_an_open_projection_as_so_far(store, yahoo, capsys):
    store()
    yahoo(daily(dip_then_rally, end="2026-12-09"), today=date(2026, 12, 9))
    code, out, _ = cli(capsys, store.root)
    assert code == 0
    assert "OPEN until 2027-02-12: PROVISIONAL, so far" in out
    assert "3 of 13 closes (close measure), 2026-11-16 to 2026-11-30; low so far 40 (2026-11-30)" in out
    assert "reached so far" in out and "broken so far" in out and "turned so far" in out
    assert "scenario so far: breakdown" in out
    assert "turned and the scenario can still change" in out
    assert "0 projection(s) of 0 ticker(s), 0 forecast probabilities; 1 open and 0 not scored are left out" in out


def test_cli_json(store, yahoo, capsys):
    store(); store(ticker="VALE3.SA")
    yahoo({"PETR4.SA": daily(quiet), "VALE3.SA": sp.Unscorable("no data")})
    code, out, _ = cli(capsys, store.root, "--json")
    report = json.loads(out)
    assert code == 0 and set(report) == {"projections", "calibration"}
    by_ticker = {r["ticker"]: r for r in report["projections"]}
    assert by_ticker["VALE3.SA"]["not_scored"] == "no data"
    assert by_ticker["PETR4.SA"]["scenario"] == "range"
    assert set(report["calibration"]["kinds"]) == {"reach", "turn", "scenario"}
    assert report["calibration"]["caveats"] == sp.CAVEATS


def test_cli_ticker_filter_and_single_folder(store, yahoo, capsys):
    first = store(); store(); store(ticker="VALE3.SA")
    calls = yahoo(daily(quiet))
    code, out, _ = cli(capsys, store.root, "--ticker", "vale3.sa", "--json")
    assert [r["ticker"] for r in json.loads(out)["projections"]] == ["VALE3.SA"]
    assert [c[0] for c in calls] == ["VALE3.SA"]
    code, out, _ = cli(capsys, first, "--json")
    assert [r["folder"] for r in json.loads(out)["projections"]] == [first]


def test_cli_reads_the_folder_from_the_environment(store, yahoo, capsys, monkeypatch):
    store()
    yahoo(daily(quiet))
    monkeypatch.setenv("RZ_PROJECTIONS_DIR", store.root)
    code, out, _ = cli(capsys, "--json")
    assert code == 0 and len(json.loads(out)["projections"]) == 1


def test_cli_without_saved_projections(tmp_path, capsys, store, yahoo):
    code, out, err = cli(capsys, str(tmp_path / "none"))
    assert (code, out) == (1, "") and err == f"No saved projections found under {tmp_path / 'none'}.\n"
    store()
    yahoo(daily(quiet))
    code, out, err = cli(capsys, store.root, "--ticker", "NVDA")
    assert code == 1 and err == f"No saved projections found under {store.root} for NVDA.\n"


# ---- the Yahoo call itself, against a fake yfinance ---------------------------------------

@pytest.fixture
def fake_yfinance(monkeypatch):
    seen = {}

    def install(result):
        class Ticker:
            def __init__(self, symbol):
                seen["symbol"] = symbol

            def history(self, **kwargs):
                seen.update(kwargs)
                if isinstance(result, Exception):
                    raise result
                return result
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
        return seen
    return install


@pytest.fixture
def real_download(monkeypatch):
    monkeypatch.setattr(sp, "download", REAL_DOWNLOAD)


REAL_DOWNLOAD = sp.download


def test_download_asks_for_unadjusted_prices_with_splits(fake_yfinance, real_download):
    index = pd.DatetimeIndex(["2026-11-16", "2026-11-17", "2026-11-18"], tz="America/Sao_Paulo")
    frame = pd.DataFrame({"Open": [1.0, 2.0, None], "High": [1.5, 2.5, None], "Low": [0.5, 1.5, None],
                          "Close": [1.25, 2.25, None], "Adj Close": [1.0, 2.0, None], "Volume": [10, 20, 0],
                          "Dividends": [0.0, 0.3, 0.0], "Stock Splits": [0.0, 2.0, 0.0]}, index=index)
    seen = fake_yfinance(frame)
    rows, splits = sp.download("PETR4.SA", "1d", date(2026, 11, 1), "America/Sao_Paulo")
    assert seen == {"symbol": "PETR4.SA", "start": "2026-11-01", "interval": "1d",
                    "auto_adjust": False, "actions": True}
    assert rows == [["2026-11-16", 1.0, 1.5, 0.5, 1.25], ["2026-11-17", 2.0, 2.5, 1.5, 2.25]]
    assert splits == [["2026-11-17", 2.0]]


def test_download_stamps_intraday_candles_in_exchange_time(fake_yfinance, real_download):
    index = pd.DatetimeIndex(["2026-11-17 13:00", "2026-11-17 13:15"], tz="UTC")
    frame = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5], "Close": [1.25, 2.25]}, index=index)
    fake_yfinance(frame)
    rows, splits = sp.download("PETR4.SA", "15m", date(2026, 11, 13), "America/Sao_Paulo")
    assert [r[0] for r in rows] == ["2026-11-17T10:00", "2026-11-17T10:15"] and splits == []


@pytest.mark.parametrize("result, interval, message", [
    (pd.DataFrame(), "1d", "no 1d candles for ZZZZ from Yahoo since 2026-11-01 (unknown symbol or network failure)"),
    (pd.DataFrame(), "15m", "no 15m candles for ZZZZ from Yahoo since 2026-11-01 (unknown symbol or network "
                            "failure; Yahoo keeps intraday candles for about 60 days)"),
    (ConnectionError("offline"), "1d", "could not fetch ZZZZ from Yahoo (ConnectionError: offline)"),
])
def test_download_failures_become_one_line(fake_yfinance, real_download, result, interval, message):
    fake_yfinance(result)
    with pytest.raises(sp.Unscorable) as stop:
        sp.download("ZZZZ", interval, date(2026, 11, 1), "America/Sao_Paulo")
    assert str(stop.value) == message and "\n" not in str(stop.value)
