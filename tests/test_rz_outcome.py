"""rz.py outcome: what the price did to a projection, judged the way sim() counts a simulated path.

The first half proves the two agree: simulated paths are fed to outcome() as if they were
realised candles, and the frequencies must reproduce sim()'s numbers. The second half pins
each outcome on hand-built paths."""
import copy
import json
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from conftest import ZONES

FAR = date(2100, 1, 1)      # a "today" long after every horizon
PATHS = 1500
SHOCKS = [{"date": "2026-12-21", "drop": 1.19}, {"date": "2026-11-17", "jump": 0.08}]


# ---- inputs for every close basis ---------------------------------------------

def variant(input_data, basis, forming, touched):
    """A consistent input on `basis`, with the newest candle forming or closed."""
    d = copy.deepcopy(input_data)
    main = next(s for s in d["data"]["series"] if s["candles"] == "1W")
    d["data"]["series"] = [main]
    main["candles"] = d["want"]["close_basis"] = basis
    main["last_row"] = "forming" if forming else "closed"
    d["want"]["probability"] = "touched_once" if touched else "close_in_zone"
    rows = main["rows"]
    if basis == "1W":
        if not forming:   # the weekend before: the week of 9 Nov is the last one, and it is closed
            rows.pop()
            d["now"].update(time="2026-11-14T10:00-03:00", session="closed")
    elif basis == "1D":
        d["horizon"] = "1-2w"
        rows[-1][0] = "2026-11-17"
        d["now"]["session"] = "open" if forming else "after_hours"
    elif basis == "1M":
        d["horizon"] = "6-12m"
        if forming:
            rows[-1][0] = "2026-11-01"
        else:
            rows[-1][0] = "2026-10-01"
            d["now"].update(time="2026-10-31T19:00-03:00", session="closed")
    else:                 # 15m: three full sessions of 26 candles, then 10 candles of today
        d["horizon"] = "today"
        del rows[:-88]
        days = ["2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17"]
        for i, row in enumerate(rows):
            at = datetime.fromisoformat(days[i // 26] + "T10:00") + timedelta(minutes=15 * (i % 26))
            row[0] = at.strftime("%Y-%m-%dT%H:%M")
        d["now"].update(time="2026-11-17T12:20-03:00" if forming else "2026-11-17T12:30-03:00", session="open")
    return d


def candle_starts(d, main, dates):
    """The `start` a real candle closing on each scheduled date would carry."""
    basis, last = main["candles"], main["rows"][-1][0]
    if basis == "1W":
        return [(day - timedelta(days=4)).isoformat() for day in dates]
    if basis == "1M":
        return [day.replace(day=1).isoformat() for day in dates]
    if basis == "1D":
        return [day.isoformat() for day in dates]
    first = datetime.fromisoformat(last) + timedelta(minutes=0 if main["last_row"] == "forming" else 15)
    return [(first + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(dates))]


def as_candles(rz, d, zones, paths, merge_forming=True, method=None):
    """Simulated paths, each as the candle rows Yahoo would later show for it: the paths sim()
    counts under sim method `method` (None: the default), drift included."""
    main = rz.main_series(d)
    starts = [row[0] for row in main["rows"]]
    close = np.array([row[4] for row in main["rows"]], float)
    p, dates, _, _, sub = rz.walk(d, main, starts, close, zones, paths, rz.drift_of(main, method))
    labels, was = candle_starts(d, main, dates), main["rows"][-1]
    out = []
    for path in p:
        rows = []
        for i, label in enumerate(labels):
            seg = path[i * sub:(i + 1) * sub]
            high, low = float(seg.max()), float(seg.min())
            if i == 0 and main["last_row"] == "forming" and merge_forming:
                # a real candle also contains what happened in it before the projection
                high, low = max(high, was[2]), min(low, was[3])
            rows.append([label, float(seg[0]), high, low, float(seg[-1])])
        out.append(rows)
    return out


def frequencies(rz, d, proj, candle_paths):
    """Share of paths in which each event happened, according to outcome()."""
    total = {}
    for rows in candle_paths:
        got = rz.outcome(d, proj, rows, FAR)
        assert got["final"] and got["closes"]["counted"] == proj["horizon"]["closes"]
        for side in ("supports", "resistances"):
            assert sum(e["turned"] for e in got[side]) <= 1
            for e in got[side]:
                for key in ("reached", "turned"):
                    total[side, e["id"], key] = total.get((side, e["id"], key), 0) + e[key]
        assert sum(s["happened"] for s in got["scenarios"]) == 1
        total["scenario", got["scenario"]] = total.get(("scenario", got["scenario"]), 0) + 1
    return {key: count / len(candle_paths) for key, count in total.items()}


@pytest.fixture
def simulated(rz, run_sim, projection_from_sim):
    """(input, projection carrying sim()'s own numbers, sim output) for one variant."""
    def make(d, zones, method=None):
        sim_out = run_sim(zones, d, paths=PATHS, method=method)
        proj = projection_from_sim(sim_out, zones)
        proj["as_of"] = {"time": d["now"]["time"], "price": d["now"]["price"]}
        return proj, sim_out
    return make


EXACT = [(basis, forming, touched)
         for basis in ("1W", "1D", "1M", "15m") for forming in (True, False) for touched in (False, True)
         if not (forming and touched)]


@pytest.mark.parametrize("method", [1, 2])
@pytest.mark.parametrize("basis, forming, touched", EXACT)
def test_outcome_frequencies_reproduce_sim(rz, input_data, zones, simulated, basis, forming, touched, method):
    """The key test: outcome() applied to sim()'s own paths gives sim()'s reach, turn and
    scenarios, for every sim method (2 adds drift to the paths)."""
    d = variant(input_data, basis, forming, touched)
    zones["shocks"] = SHOCKS
    proj, sim_out = simulated(d, zones, method)
    assert sim_out["measure"] == ("touch" if touched else "close")
    assert sim_out["sim_method"]["version"] == method
    freq = frequencies(rz, d, proj, as_candles(rz, d, zones, PATHS, method=method))
    for side in ("supports", "resistances"):
        for entry in sim_out[side]:
            # sim rounds reach once, so reach must match to the cent; turn is a difference of two
            # rounded numbers and the scenarios are rounded sums, so they may be 0.01 apart
            assert round(freq[side, entry["id"], "reached"], 2) == entry["reach"], (side, entry)
            assert abs(freq[side, entry["id"], "turned"] - entry["turn"]) <= 0.0101, (side, entry)
    for scenario in sim_out["scenarios"]:
        assert abs(freq.get(("scenario", scenario["name"]), 0) - scenario["p"]) <= 0.0101, scenario
    assert abs(sum(v for k, v in freq.items() if k[0] == "scenario") - 1) < 1e-9


def test_outcome_agrees_with_sim_for_a_single_zone_per_side(rz, input_data, simulated):
    zones = {"supports": [[40.6, 42.2]], "resistances": [[47.1, 49.6]]}
    proj, sim_out = simulated(input_data, zones)
    freq = frequencies(rz, input_data, proj, as_candles(rz, input_data, zones, PATHS))
    for side in ("supports", "resistances"):
        for entry in sim_out[side]:
            assert round(freq[side, entry["id"], "reached"], 2) == entry["reach"]
    for scenario in sim_out["scenarios"]:
        assert abs(freq.get(("scenario", scenario["name"]), 0) - scenario["p"]) <= 0.0101


@pytest.mark.parametrize("basis", ["1W", "15m"])
def test_touch_on_a_forming_candle_can_only_undercount(rz, input_data, zones, simulated, basis):
    """The one place a realised path cannot be judged exactly like a simulated one. A finished
    candle does not say whether its high and low came before or after the projection, so for the
    candle that was forming outcome() counts an extreme only if it is new, else falls back to the
    close. Events are never invented; some touches inside the old range are missed."""
    d = variant(input_data, basis, True, True)
    proj, sim_out = simulated(d, zones)
    freq = frequencies(rz, d, proj, as_candles(rz, d, zones, PATHS))
    for side in ("supports", "resistances"):
        for entry in sim_out[side]:
            gap = entry["reach"] - freq[side, entry["id"], "reached"]
            assert -0.005 <= gap <= 0.02, (side, entry, gap)   # never more often than sim; here at most 0.01 less


# ---- hand-built paths ----------------------------------------------------------
# Fixture: weekly closes, the week of 2026-11-16 is forming, 13 closes to 2027-02-12, price 44.06.
# Supports 43.2-44.6 (the price starts inside), 40.6-42.2, 37.4-38.6; resistances 45.3-47.1,
# 47.1-49.6, 50.2-52.7. Middle lines: 40.6 and 49.6.

def weekly(closes, first="2026-11-16", lows=None, highs=None):
    day = date.fromisoformat(first)
    return [[(day + timedelta(days=7 * i)).isoformat(), c, (highs or closes)[i], (lows or closes)[i], c]
            for i, c in enumerate(closes)]


def flags(got, side, key):
    return {e["id"]: e[key] for e in got[side]}


def test_fixture_zones_are_the_ones_these_tests_assume(projection_data):
    for side in ("supports", "resistances"):
        assert [e["zone"] for e in projection_data[side][:3]] == ZONES[side]


def test_price_starting_inside_s1_and_going_nowhere(rz, input_data, projection_data):
    got = rz.outcome(input_data, projection_data, weekly([44.0] * 13), FAR)
    assert (got["state"], got["final"], got["measure"]) == ("ended", True, "close")
    assert got["closes"] == {"scheduled": 13, "counted": 13}
    assert got["window"] == ["2026-11-16", "2027-02-08"]
    assert got["supports"][0]["forced"] and not any(e["forced"] for e in got["supports"][1:] + got["resistances"])
    assert flags(got, "supports", "reached") == {"S1": True, "S2": False, "S3": False, "below": False}
    assert flags(got, "supports", "turned") == {"S1": True, "S2": False, "S3": False, "below": False}
    assert flags(got, "supports", "broken") == {"S1": False, "S2": False, "S3": False, "below": None}
    assert not any(flags(got, "resistances", "reached").values())
    assert not any(flags(got, "resistances", "turned").values())
    assert got["scenario"] == "range"
    assert [s["happened"] for s in got["scenarios"]] == [s["name"] == "range" for s in projection_data["scenarios"]]
    assert [(e["reach"], e["turn"]) for e in got["supports"]] == [(e["reach"], e["turn"]) for e in projection_data["supports"]]


def test_tail_reached(rz, input_data, projection_data):
    got = rz.outcome(input_data, projection_data, weekly([44.0, 41.0, 37.0] + [39.0] * 10), FAR)
    assert flags(got, "supports", "reached") == {"S1": True, "S2": True, "S3": True, "below": True}
    assert flags(got, "supports", "broken") == {"S1": True, "S2": True, "S3": True, "below": None}
    assert flags(got, "supports", "turned") == {"S1": False, "S2": False, "S3": False, "below": True}
    assert got["low"] == [37.0, "2026-11-30"] and got["scenario"] == "breakdown"


def test_whipsaw_and_a_zone_reached_because_the_one_before_broke(rz, input_data, projection_data):
    got = rz.outcome(input_data, projection_data, weekly([44.0, 40.0, 44.0, 50.0] + [46.0] * 9), FAR)
    assert got["scenario"] == "whipsaw"
    # 40.0 is beyond S2's far edge 40.6 but above S3's near edge 38.6: S3 counts as reached, as in sim()
    assert flags(got, "supports", "turned") == {"S1": False, "S2": False, "S3": True, "below": False}
    assert flags(got, "resistances", "reached") == {"R1": True, "R2": True, "R3": True, "above": False}
    assert flags(got, "resistances", "turned") == {"R1": False, "R2": False, "R3": True, "above": False}


def test_breakout(rz, input_data, projection_data):
    got = rz.outcome(input_data, projection_data, weekly([44.0, 48.0, 53.0] + [51.0] * 10), FAR)
    assert got["scenario"] == "breakout"
    assert flags(got, "resistances", "turned") == {"R1": False, "R2": False, "R3": False, "above": True}


def test_nothing_reached(rz, input_data, projection_data):
    projection_data["supports"][0]["zone"] = [43.2, 43.8]     # now the price starts above S1
    got = rz.outcome(input_data, projection_data, weekly([44.0, 45.0, 44.5] + [44.2] * 10), FAR)
    for side in ("supports", "resistances"):
        assert not any(e["forced"] or e["reached"] or e["turned"] or e["broken"] for e in got[side])
    assert got["scenario"] == "range"


def test_edges_reached_at_the_near_edge_broken_only_beyond_the_far_edge(rz, input_data, projection_data):
    got = rz.outcome(input_data, projection_data, weekly([43.2, 45.3] + [44.0] * 11), FAR)
    assert flags(got, "supports", "broken")["S1"] is False        # a close on the far edge is not a break
    assert flags(got, "resistances", "reached") == {"R1": True, "R2": False, "R3": False, "above": False}
    got = rz.outcome(input_data, projection_data, weekly([43.19, 45.29] + [44.0] * 11), FAR)
    assert flags(got, "supports", "broken")["S1"] is True and flags(got, "supports", "turned")["S2"] is True
    assert not any(flags(got, "resistances", "reached").values())
    # the scenario lines are strict as well: a close on 40.6 is not a breakdown
    assert rz.outcome(input_data, projection_data, weekly([40.6] * 13), FAR)["scenario"] == "range"
    assert rz.outcome(input_data, projection_data, weekly([40.59] * 13), FAR)["scenario"] == "breakdown"


def test_close_measure_ignores_wicks_and_touch_measure_counts_them(rz, input_data, projection_data):
    rows = weekly([44.0] * 13, lows=[44.0, 40.0] + [44.0] * 11, highs=[44.0] * 12 + [50.0])
    got = rz.outcome(input_data, projection_data, rows, FAR)
    assert got["scenario"] == "range" and got["low"][0] == 44.0
    input_data["want"]["probability"] = "touched_once"
    got = rz.outcome(input_data, projection_data, rows, FAR)
    assert (got["measure"], got["scenario"]) == ("touch", "whipsaw")
    assert got["low"] == [40.0, "2026-11-23"] and got["high"] == [50.0, "2027-02-08"]


def test_touch_on_the_forming_candle_counts_only_new_extremes(rz, input_data, projection_data):
    """The saved forming week had low 43.38 and high 47.26, both made before the projection."""
    input_data["want"]["probability"] = "touched_once"
    was = input_data["data"]["series"][-1]["rows"][-1]
    assert (was[0], was[2], was[3]) == ("2026-11-16", 47.26, 43.38)
    quiet = [["2026-11-16", 46.48, 47.26, 43.38, 44.5]] + weekly([44.5] * 12, "2026-11-23")
    got = rz.outcome(input_data, projection_data, quiet, FAR)
    assert (got["low"][0], got["high"][0]) == (44.5, 44.5)         # the old wick is not counted again
    assert not any(flags(got, "resistances", "reached").values())
    lower = [["2026-11-16", 46.48, 47.26, 42.9, 44.5]] + weekly([44.5] * 12, "2026-11-23")
    got = rz.outcome(input_data, projection_data, lower, FAR)
    assert got["low"] == [42.9, "2026-11-16"] and flags(got, "supports", "broken")["S1"] is True


def test_rows_outside_the_horizon_are_ignored(rz, input_data, projection_data):
    before = weekly([30.0, 30.0], "2026-11-02")       # already in the input
    after = weekly([60.0], "2027-02-15")              # the week after horizon.last, 2027-02-12
    got = rz.outcome(input_data, projection_data, before + weekly([44.0] * 13) + after, FAR)
    assert got["closes"]["counted"] == 13 and got["low"][0] == got["high"][0] == 44.0


def test_missing_candles_are_scored_on_what_exists(rz, input_data, projection_data):
    rows = weekly([44.0] * 13)
    del rows[3], rows[7]                               # two weeks without a single session
    got = rz.outcome(input_data, projection_data, rows, FAR)
    assert got["closes"] == {"scheduled": 13, "counted": 11} and got["final"]


@pytest.mark.parametrize("rows, message", [
    ([], "no candles after the projection"),
    (weekly([30.0], "2026-11-09"), "no candles after the projection"),
])
def test_nothing_to_judge_raises_value_error(rz, input_data, projection_data, rows, message):
    with pytest.raises(ValueError, match=message):
        rz.outcome(input_data, projection_data, rows, FAR)


def test_open_horizon_counts_only_closed_candles(rz, input_data, projection_data):
    rows = weekly([44.0, 40.0, 30.0])                  # weeks ending 20 Nov, 27 Nov and 4 Dec
    got = rz.outcome(input_data, projection_data, rows, date(2026, 12, 2))   # the third week is still running
    assert (got["state"], got["final"]) == ("open", False)
    assert got["closes"] == {"scheduled": 13, "counted": 2}
    assert got["low"] == [40.0, "2026-11-23"] and got["scenario"] == "breakdown"
    assert flags(got, "supports", "turned") == {"S1": False, "S2": False, "S3": True, "below": False}
    # on the Friday itself the week's close is not final either
    assert rz.outcome(input_data, projection_data, rows, date(2026, 12, 4))["closes"]["counted"] == 2
    assert rz.outcome(input_data, projection_data, rows, date(2026, 12, 5))["low"][0] == 30.0
    with pytest.raises(ValueError, match="no candle of the horizon has closed yet"):
        rz.outcome(input_data, projection_data, rows[:1], date(2026, 11, 18))


def test_open_horizon_with_touch_measure_counts_the_running_candle(rz, input_data, projection_data):
    input_data["want"]["probability"] = "touched_once"
    rows = weekly([44.0, 44.0], lows=[42.0, 39.0])
    got = rz.outcome(input_data, projection_data, rows, date(2026, 11, 25))
    assert not got["final"] and got["low"] == [39.0, "2026-11-23"]


def test_the_day_after_horizon_last_is_ended_like_rz_list(rz, input_data, projection_data):
    rows = weekly([44.0] * 13)
    assert rz.outcome(input_data, projection_data, rows, date(2027, 2, 12))["state"] == "open"
    assert rz.outcome(input_data, projection_data, rows, date(2027, 2, 13))["state"] == "ended"


# ---- which candles belong to the horizon, per close basis -------------------------

def picked(rz, d, last, rows, today=FAR):
    main = rz.main_series(d)
    kept, closed, ended, partial = rz.realised(d, main, {"horizon": {"last": last}}, rows, today)
    return [r[0] for r in kept], partial


def flat(starts):
    return [[s, 1.0, 1.0, 1.0, 1.0] for s in starts]


def test_closes_on(rz):
    assert rz.closes_on("1W", "2026-11-16") == date(2026, 11, 20)      # Monday -> that Friday
    assert rz.closes_on("1W", "2026-11-17") == date(2026, 11, 20)      # a week that began on a Tuesday
    assert rz.closes_on("1W", "2026-11-20") == date(2026, 11, 20)
    assert rz.closes_on("1M", "2026-02-01") == date(2026, 2, 28)
    assert rz.closes_on("1D", "2026-11-17") == date(2026, 11, 17)
    assert rz.closes_on("15m", "2026-11-17T10:15") == date(2026, 11, 17)


@pytest.mark.parametrize("basis, forming", [(b, f) for b in ("1W", "1D", "1M") for f in (True, False)])
def test_horizon_candles_are_the_ones_schedule_dated(rz, input_data, basis, forming):
    """With a candle for every scheduled close, and extra ones on both sides, realised() keeps
    exactly the scheduled ones."""
    d = variant(input_data, basis, forming, False)
    main = rz.main_series(d)
    dates, _ = rz.schedule(d, main, [r[0] for r in main["rows"]])
    wanted = candle_starts(d, main, dates)
    step = {"1W": 7, "1D": 1, "1M": 31}[basis]
    extra = [(date.fromisoformat(wanted[-1]) + timedelta(days=step)).isoformat()]
    if basis == "1D":
        extra = ["2026-12-05"]    # any later day
    last = main["rows"][-1][0]
    older = [(date.fromisoformat(last) - timedelta(days=step)).isoformat()]
    if not forming:
        older.append(last)        # a forming last row is instead the first candle of the horizon
    kept, partial = picked(rz, d, dates[-1].isoformat(), flat(older + wanted + extra))
    assert kept == wanted and partial is forming


def test_daily_holidays_leave_fewer_candles(rz, input_data):
    d = variant(input_data, "1D", True, False)
    main = rz.main_series(d)
    dates, _ = rz.schedule(d, main, [r[0] for r in main["rows"]])
    assert (dates[0], dates[-1], len(dates)) == (date(2026, 11, 17), date(2026, 11, 30), 10)
    real = [day.isoformat() for day in dates if day != date(2026, 11, 20)]      # a holiday
    kept, _ = picked(rz, d, "2026-11-30", flat(real + ["2026-12-01"]))
    assert kept == real and len(kept) == 9


def test_intraday_horizon_is_the_session_in_progress(rz, input_data):
    d = variant(input_data, "15m", True, False)
    rows = flat(["2026-11-17T12:00", "2026-11-17T12:15", "2026-11-17T12:30", "2026-11-17T16:15", "2026-11-18T10:00"])
    kept, partial = picked(rz, d, "2026-11-17", rows)
    assert kept == ["2026-11-17T12:15", "2026-11-17T12:30", "2026-11-17T16:15"] and partial is True


def test_intraday_horizon_after_the_close_is_the_next_session(rz, input_data):
    d = variant(input_data, "15m", False, False)
    d["now"].update(time="2026-11-17T19:00-03:00", session="closed")
    rows = flat(["2026-11-17T12:15", "2026-11-19T10:00", "2026-11-19T10:15", "2026-11-20T10:00"])   # the 18th was a holiday
    kept, partial = picked(rz, d, "2026-11-17", rows)
    assert kept == ["2026-11-19T10:00", "2026-11-19T10:15"] and partial is False
    main = rz.main_series(d)
    _, closed, ended, _ = rz.realised(d, main, {"horizon": {"last": "2026-11-17"}}, rows, date(2026, 11, 19))
    assert (closed, ended) == ([True, False], False)      # the newest candle of a running session may still move
    _, closed, ended, _ = rz.realised(d, main, {"horizon": {"last": "2026-11-17"}}, rows, date(2026, 11, 20))
    assert (closed, ended) == ([True, True], True)


# ---- the offline subcommand ------------------------------------------------------

def test_score_subcommand_prints_the_outcome(rz, input_path, projection_path, write_json, printed, monkeypatch):
    class Later(date):
        @classmethod
        def today(cls):
            return FAR
    monkeypatch.setattr(rz, "date", Later)
    printed()
    rz.score(str(input_path), str(projection_path), write_json("candles.json", weekly([44.0] * 13)))
    out = printed()
    assert out["state"] == "ended" and out["scenario"] == "range"
    assert list(out) == ["state", "final", "measure", "closes", "window", "low", "high",
                         "supports", "resistances", "scenario", "scenarios"]
    rz.score(str(input_path), str(projection_path), write_json("wrapped.json", {"rows": weekly([44.0] * 13)}))
    assert printed()["scenario"] == "range"


def test_score_subcommand_without_candles_exits_with_one_line(rz, input_path, projection_path, write_json, capsys):
    with pytest.raises(SystemExit) as stop:
        rz.score(str(input_path), str(projection_path), write_json("candles.json", []))
    assert stop.value.code == 1
    assert capsys.readouterr().out.strip() == "not scored: no candles after the projection"


def test_outcome_result_is_json_serialisable(rz, input_data, projection_data):
    json.dumps(rz.outcome(input_data, projection_data, weekly([44.0] * 13), FAR))
