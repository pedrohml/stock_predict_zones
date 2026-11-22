"""backtest.py: the baseline probabilities on past dates. Yahoo is always stubbed; the candles
are a synthetic random walk, and the simulation runs with few paths to stay fast."""
import copy
import csv
import inspect
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

import backtest as bt
import build_prompt
import score_projections as sp

TODAY = date(2026, 11, 19)
PATHS = 2000


def walk_rows(seed=1, first=date(2015, 1, 1), last=date(2026, 11, 18), price=30.0):
    """Weekday candles of a fat-free random walk, [date, open, high, low, close], unrounded."""
    rng, rows, day = np.random.default_rng(seed), [], first
    while day <= last:
        if day.weekday() < 5:
            open_, price = price, price * math.exp(rng.normal(0, 0.02))
            high = max(open_, price) * (1 + abs(rng.normal(0, 0.008)))
            low = min(open_, price) * (1 - abs(rng.normal(0, 0.008)))
            rows.append([day.isoformat(), open_, high, low, price])
        day += timedelta(days=1)
    return rows


ROWS = walk_rows()
DATA = {"rows": ROWS, "tz": "America/New_York", "currency": "USD", "venue": "Test Exchange"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that forgets the stub fails instead of calling Yahoo."""
    def refuse(*args, **kwargs):
        raise AssertionError("test tried to download prices")
    monkeypatch.setattr(bt, "download", refuse)


@pytest.fixture
def yahoo(monkeypatch):
    """yahoo({ticker: rows | BacktestError}) stubs the download and records the calls."""
    calls = []

    def install(data=None):
        def fake(ticker, start):
            calls.append((ticker, start))
            rows = (data or {}).get(ticker, ROWS)
            if isinstance(rows, Exception):
                raise rows
            return {**DATA, "rows": [r for r in rows if r[0] >= start.isoformat()]}
        monkeypatch.setattr(bt, "download", fake)
        return calls
    return install


# ---- the input as of a past day ------------------------------------------------

@pytest.mark.parametrize("horizon", bt.HORIZONS)
def test_input_ignores_every_candle_after_the_as_of_day(horizon):
    day = date(2025, 3, 12)   # a Wednesday: the week and the month are still forming
    cut = {**DATA, "rows": [r for r in ROWS if r[0] <= day.isoformat()]}
    full = bt.input_as_of("AAA", horizon, DATA, day)
    assert full == bt.input_as_of("AAA", horizon, cut, day)
    for series in full["data"]["series"]:
        assert all(row[0] <= day.isoformat() for row in series["rows"])
    assert full["now"]["price"] == round(cut["rows"][-1][4], 2)


def test_input_has_the_shape_of_a_live_one():
    d = bt.input_as_of("aaa", "1-3m", DATA, date(2025, 3, 12))
    preset = build_prompt.PRESETS["1-3m"]
    assert d["asset"] == {"ticker": "AAA", "venue": "Test Exchange", "currency": "USD"}
    assert d["now"] == {"time": "2025-03-12T23:00-04:00", "price": d["now"]["price"], "session": "closed"}
    assert [s["candles"] for s in d["data"]["series"]] == ["1M", "1W"]   # context first
    context, main = d["data"]["series"]
    assert len(main["rows"]) == preset["main"][2] and len(context["rows"]) == preset["context"][2]
    assert main["last_row"] == "forming" and context["last_row"] == "closed"
    assert main["rows"][-1][0] == "2025-03-10"                            # the week's Monday
    assert context["rows"][-1][0] < main["rows"][0][0]                     # no overlap
    assert d["events"] == [] and d["research"] == "none"
    assert d["want"] == {"supports": 3, "resistances": 3, "probability": "close_in_zone", "close_basis": "1W"}
    assert json.loads(build_prompt.render(copy.deepcopy(d))) == d


def test_week_closed_on_a_friday():
    d = bt.input_as_of("AAA", "1-3m", DATA, date(2025, 3, 14))
    assert d["data"]["series"][-1]["last_row"] == "closed"
    daily = bt.input_as_of("AAA", "1-2w", DATA, date(2025, 3, 12))
    assert daily["data"]["series"][-1]["last_row"] == "closed"
    assert daily["data"]["series"][-1]["rows"][-1][0] == "2025-03-12"


def test_no_candle_on_the_day_is_an_error():
    with pytest.raises(ValueError):
        bt.input_as_of("AAA", "1-3m", DATA, date(2025, 3, 15))   # a Saturday


def test_weekly_candles_match_aggregate():
    frame = bt.frame_of(ROWS[:30], "1wk", "America/New_York")
    weeks = sp.aggregate(ROWS[:30], "1W")
    assert [t.strftime("%Y-%m-%d") for t in frame.index] == [w[0] for w in weeks]
    assert frame.iloc[-1].tolist() == weeks[-1][1:]


# ---- zones -------------------------------------------------------------------

def facts(price=50.0, lows=(), highs=(), width=(1.0, 2.0)):
    return {"price": price, "typical_zone_width": list(width),
            "pivot_lows": [["d", x] for x in lows], "pivot_highs": [["d", x] for x in highs],
            "recent_lows": [], "recent_highs": [], "retracements": {},
            "swing": {"low": [min(lows, default=price), "d"], "high": [max(highs, default=price), "d"]}}


def sound(zones, price):
    s, r = zones["supports"], zones["resistances"]
    assert s[0][1] <= price <= r[0][0]
    for side, zs in (("s", s), ("r", r)):
        edges = [e for z in zs for e in (reversed(z) if side == "s" else z)]
        assert edges == sorted(edges, reverse=side == "s")


@pytest.fixture
def geometry(monkeypatch):
    """The placement tests below, written for one width and single-candidate clusters (the rule
    before 22 Nov 2026): the two parameters set back, so they test the geometry alone."""
    monkeypatch.setattr(bt, "WIDTH_FACTOR", 1.0)
    monkeypatch.setattr(bt, "MIN_MEMBERS", 1)


def test_zone_rule_parameters():
    # the rule adopted on 22 Nov 2026 (README, "A stronger zone rule")
    assert (bt.WIDTH_FACTOR, bt.MIN_MEMBERS) == (1.5, 2)
    assert bt.zone_width(facts(width=(1.0, 2.0))) == 2.25            # 1.5 x the mean 1.5
    assert bt.zone_width(facts(width=(0.0, 0.0))) == 0.01            # at least one tick


def test_zones_drop_single_candidate_clusters():
    # width 2.25: 48 and 47.6 cluster (two members); 44 stands alone and is dropped; 40 is a pivot
    # and the swing's end (two members). Above: 52 and 52.4, then 56 dropped, then 60 twice.
    z = bt.clustered_zones(facts(lows=(48.0, 47.6, 44.0, 40.0), highs=(52.0, 52.4, 56.0, 60.0)))
    assert [len(x["members"]) for x in z["supports"]] == [2, 2, 0]
    assert [len(x["members"]) for x in z["resistances"]] == [2, 2, 0]
    assert z["supports"][0]["zone"] == [46.67, 48.92] and z["resistances"][0]["zone"] == [51.08, 53.33]
    assert z["supports"][1]["zone"] == [38.88, 41.12]                  # 44 made no zone
    assert z["supports"][2]["zone"] == [34.38, 36.63]                  # filler, one width beyond
    sound(bt.choose_zones(facts(lows=(48.0, 47.6, 44.0, 40.0), highs=(52.0, 52.4, 56.0, 60.0))), 50.0)


@pytest.mark.usefixtures("geometry")
def test_zones_cluster_nearby_candidates():
    z = bt.choose_zones(facts(lows=(48.0, 47.6, 44.0, 40.0), highs=(52.0, 52.4, 56.0, 60.0)))
    # width 1.5: 48 and 47.6 are one cluster at 47.8, 52 and 52.4 one at 52.2
    assert z == {"supports": [[47.05, 48.55], [43.25, 44.75], [39.25, 40.75]],
                 "resistances": [[51.45, 52.95], [55.25, 56.75], [59.25, 60.75]]}
    sound(z, 50.0)


@pytest.mark.usefixtures("geometry")
def test_zones_are_cut_at_the_price_and_pushed_apart():
    z = bt.choose_zones(facts(lows=(49.8, 48.0), highs=(50.4, 52.0), width=(1.0, 2.0)))
    assert z["supports"][0] == [49.05, 50.0]          # cut at the price
    assert z["resistances"][0] == [50.0, 51.15]
    assert z["resistances"][1] == [51.25, 52.75]
    # a cluster's mean can sit far enough out that the next zone would overlap it: pushed outward
    z = bt.choose_zones(facts(highs=(50.3, 51.7, 51.9)))
    assert z["resistances"][:2] == [[50.25, 51.75], [51.75, 53.25]]
    sound(z, 50.0)


@pytest.mark.usefixtures("geometry")
def test_zones_fill_a_side_without_candidates():
    z = bt.choose_zones(facts(lows=(45.0,), highs=()), supports=2, resistances=3)
    assert z["resistances"] == [[51.5, 53.0], [54.5, 56.0], [57.5, 59.0]]
    assert z["supports"] == [[44.25, 45.75], [41.25, 42.75]]
    sound(z, 50.0)


def test_zones_never_go_below_zero():
    z = bt.choose_zones(facts(price=1.0, lows=(), highs=(1.2,), width=(0.3, 0.5)))
    assert all(lo > 0 for lo, _ in z["supports"])
    assert len(z["supports"]) < 3


def test_zones_on_real_levels_make_a_valid_projection(rz):
    d = bt.input_as_of("AAA", "1-3m", DATA, date(2025, 3, 12))
    loaded = rz.unpack(d)
    zones = bt.choose_zones(rz.facts(*loaded))
    assert zones == bt.choose_zones(rz.facts(*rz.unpack(d)))    # deterministic
    sound(zones, d["now"]["price"])
    out = rz.baseline(loaded, zones, PATHS)
    assert rz.problems(bt.projection(d, zones, out), complete=False) == []


# ---- rz.py: the split into dict-returning functions --------------------------

def test_facts_and_baseline_are_what_levels_and_sim_print(rz, input_path, write_json, printed, zones):
    printed()
    rz.levels(str(input_path))
    assert printed() == json.loads(json.dumps(rz.facts(*rz.load(str(input_path)))))
    rz.sim(str(input_path), write_json("z.json", zones), PATHS)
    assert printed() == rz.baseline(rz.load(str(input_path)), zones, PATHS)


def test_default_paths_are_sims():
    assert bt.DEFAULT_PATHS == 200_000
    assert inspect.signature(bt.rz.sim).parameters["paths"].default == bt.DEFAULT_PATHS


# ---- the whole run -----------------------------------------------------------

def test_run_downloads_once_per_ticker_and_scores(yahoo):
    calls = yahoo()
    report = bt.run(["aaa", "BBB"], "1-3m", date(2024, 1, 1), date(2024, 6, 30), step=20,
                    paths=PATHS, today=TODAY, progress=False)
    assert [c[0] for c in calls] == ["AAA", "BBB"]
    assert calls[0][1] == date(2024, 1, 1) - timedelta(days=bt.lookback("1-3m"))
    records = report["records"]
    assert {r["ticker"] for r in records} == {"AAA", "BBB"}
    assert all("skipped" not in r and r["final"] for r in records)
    summary = report["summary"]
    assert summary["cases"] == len(records) and set(summary["per_ticker"]) == {"AAA", "BBB"}
    assert set(summary["overall"]["kinds"]) == {"reach", "turn", "scenario"}
    assert set(summary["overall"]["scenarios"]) <= set(bt.SCENARIOS)
    # the same numbers as score_projections.calibration() on the same records
    reference = sp.calibration(records)
    assert summary["overall"]["kinds"] == reference["kinds"]
    assert summary["overall"]["forecasts"] == reference["forecasts"]


def test_run_is_reproducible(yahoo):
    yahoo()
    a = bt.run(["AAA"], "1-2w", date(2024, 1, 1), date(2024, 3, 1), paths=PATHS, today=TODAY, progress=False)
    b = bt.run(["AAA"], "1-2w", date(2024, 1, 1), date(2024, 3, 1), paths=PATHS, today=TODAY, progress=False)
    assert a == b


def test_run_takes_the_sim_method(yahoo, rz):
    """Both sim methods stay runnable, so the comparison that chose the default is reproducible."""
    yahoo()
    span = (["AAA"], "1-3m", date(2024, 1, 2), date(2024, 6, 28))
    old = bt.run(*span, step=20, paths=PATHS, today=TODAY, progress=False, method=1)
    new = bt.run(*span, step=20, paths=PATHS, today=TODAY, progress=False)
    assert old["settings"]["sim_method"] == 1 and old["settings"]["drift_per_year"] == 0.0
    assert new["settings"]["sim_method"] == rz.SIM_METHOD == new["settings"]["default_sim_method"]
    assert "sim method 1 (drift 0% a year); rz.py sim uses method 2 by default" in bt.render(old)
    pairs = [(a, b) for a, b in zip(old["records"], new["records"]) if "skipped" not in a]
    assert pairs and all(a["zones"] == b["zones"] and a["scenario"] == b["scenario"] for a, b in pairs)
    up = lambda r: sum(e["reach"] for e in r["resistances"])
    assert sum(up(b) for _, b in pairs) > sum(up(a) for a, _ in pairs)
    d = bt.input_as_of("AAA", "1-3m", DATA, date.fromisoformat(pairs[0][0]["as_of"]))
    z = pairs[0][0]["zones"]
    assert [e["reach"] for e in pairs[0][0]["supports"]] == \
        [e["reach"] for e in rz.baseline(rz.unpack(d), z, PATHS, 1)["supports"]]


def test_cli_method_flag(yahoo, monkeypatch, capsys):
    yahoo()
    monkeypatch.setattr(sp, "today_in", lambda tz: TODAY)
    code = bt.main(["AAA", "--horizon", "1-2w", "--start", "2024-01-01", "--end", "2024-02-01",
                    "--paths", str(PATHS), "--method", "1", "--json", "--quiet"])
    assert code == 0 and json.loads(capsys.readouterr().out)["settings"]["sim_method"] == 1
    with pytest.raises(SystemExit):
        bt.main(["AAA", "--method", "9"])


def test_cases_scored_against_the_later_candles(yahoo, rz):
    yahoo()
    report = bt.run(["AAA"], "1-2w", date(2024, 5, 1), date(2024, 5, 1), paths=PATHS, today=TODAY, progress=False)
    (r,) = report["records"]
    d = bt.input_as_of("AAA", "1-2w", DATA, date(2024, 5, 1))
    later = [row for row in ROWS if "2024-05-02" <= row[0] <= r["last"]]
    assert r["closes"]["counted"] == len(later) == 10
    lowest = min(later, key=lambda row: row[4])
    assert r["low"] == [round(lowest[4], 2), lowest[0]]
    assert r["price"] == d["now"]["price"]


def test_unended_horizons_and_short_history_are_skipped(yahoo):
    yahoo({"NEW": [r for r in ROWS if r[0] >= "2023-06-01"]})
    report = bt.run(["AAA", "NEW"], "1-3m", date(2024, 1, 2), date(2026, 11, 10), step=60,
                    paths=PATHS, today=TODAY, progress=False)
    skipped = report["summary"]["skipped"]
    assert skipped["horizon not ended yet"] >= 2
    assert any(reason.startswith("less history") for reason in skipped)
    assert report["summary"]["per_ticker"]["AAA"]["cases"] > 0


def test_a_failed_download_is_reported_and_the_rest_runs(yahoo):
    yahoo({"BAD": bt.BacktestError("no daily candles for BAD")})
    report = bt.run(["BAD", "AAA"], "1-2w", date(2024, 1, 1), date(2024, 2, 1), paths=PATHS,
                    today=TODAY, progress=False)
    assert report["settings"]["failed"] == {"BAD": "no daily candles for BAD"}
    assert report["summary"]["cases"] > 0
    assert "BAD: not backtested" in bt.render(report)


def test_start_after_end_is_an_error(yahoo):
    yahoo()
    with pytest.raises(bt.BacktestError):
        bt.run(["AAA"], "1-3m", date(2025, 1, 1), date(2024, 1, 1), today=TODAY, progress=False)


def test_default_range_ends_before_the_last_horizon():
    start, end = bt.default_range("1-3m", TODAY)
    assert end == TODAY - timedelta(days=98) and start == end.replace(year=end.year - 3)


# ---- command line ------------------------------------------------------------

def test_cli_text_report(yahoo, monkeypatch, capsys):
    yahoo()
    monkeypatch.setattr(sp, "today_in", lambda tz: TODAY)
    code = bt.main(["AAA", "--horizon", "1-2w", "--start", "2024-01-01", "--end", "2024-04-01",
                    "--paths", str(PATHS), "--quiet"])
    out = capsys.readouterr()
    assert code == 0 and out.err == ""
    text = out.out
    assert "BACKTEST of the baseline probabilities, horizon 1-2w" in text
    assert f"NOTE: {PATHS:,} simulated paths per case, not the 200,000" in text
    assert "not a measure of returns" in text and "Zone rule" in text
    for line in ("OVERALL", "reach:", "turn:", "scenario (all four):", "mean forecast"):
        assert line in text
    for word in ("buy", "sell"):
        assert word not in text.lower()


def test_cli_json_and_csv(yahoo, monkeypatch, capsys, tmp_path):
    yahoo()
    monkeypatch.setattr(sp, "today_in", lambda tz: TODAY)
    target = tmp_path / "cases.csv"
    code = bt.main(["AAA", "BBB", "--horizon", "1-2w", "--start", "2024-01-01", "--end", "2024-02-01",
                    "--paths", str(PATHS), "--json", "--csv", str(target), "--quiet"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["settings"]["paths"] == PATHS and report["settings"]["default_paths"] == 200_000
    assert set(report["per_ticker"]) == {"AAA", "BBB"} and report["cases"] > 0
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    assert rows[0].keys() == set(bt.CSV_FIELDS)
    assert {r["kind"] for r in rows} == {"reach", "turn", "scenario"}
    assert len({(r["ticker"], r["as_of"]) for r in rows}) == report["cases"]


def test_cli_refuses_too_few_paths(capsys):
    with pytest.raises(SystemExit):
        bt.main(["AAA", "--paths", "10"])
