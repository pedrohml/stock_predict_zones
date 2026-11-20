"""score_entries.py: saved trade setups against realised daily candles. Yahoo is always stubbed,
the history is a temp folder, and the walk must be backtest_entries.walk() itself."""
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import backtest_entries as be
import score_entries as se
import score_projections as sp

# The fixture pair: PETR4.SA, as of Tue 2026-11-17 (17:36, after the close) at 44.06, valid until
# 2027-02-12. The last closed week in the input, 2026-11-09, closed at 46.48 (the scale check).
AS_OF = "2026-11-17"
VALID_UNTIL = "2027-02-12"


def entry(rank, side, setup, order, price, stop, target, fill, win, if_filled, per_order, quality):
    return {"rank": rank, "id": f"S{rank}", "side": side, "setup": setup,
            "entry": {"order": order, "price": price, "anchor": "zone edge"},
            "stop": {"price": stop, "anchor": "zone edge and buffer"},
            "target": {"price": target, "anchor": "zone edge"},
            "payoff": {"risk": round(abs(price - stop), 2), "reward": round(abs(target - price), 2),
                       "ratio": round(abs(target - price) / abs(price - stop), 2)},
            "odds": {"fill": fill, "win_if_filled": win, "basis": "simulated paths"},
            "edge": {"if_filled": if_filled, "per_order": per_order},
            "quality": quality, "why": "placeholder", "watch": []}


# A long limit that fills on the first day and reaches its target, a short limit that fills and is
# stopped, and a long stop order that never fills.
SETUPS = [entry(1, "long", "reversal", "limit", 43.0, 42.0, 46.0, 0.6, 0.4, 0.6, 0.36, "favourable"),
          entry(2, "short", "reversal", "limit", 47.0, 48.0, 45.0, 0.3, 0.5, 0.5, 0.15, "marginal"),
          entry(3, "long", "continuation", "stop", 50.0, 49.0, 52.0, 0.1, 0.4, 0.2, 0.02, "unfavourable")]


def reply(proj, rules=2, setups=SETUPS):
    out = {"task": "trade_entries", "asset": {"ticker": proj["asset"]["ticker"], "currency": "BRL"},
           "as_of": proj["as_of"], "valid_until": proj["horizon"]["last"], "probability": "close_in_zone",
           "units": {"move": 1.0, "buffer": 0.25, "range": 3.0}, "entries": copy.deepcopy(setups),
           "confidence": {"level": "low", "limits": ["x"]}, "method": "placeholder",
           "note": "Hypothetical setups for study. Not a recommendation to buy or sell."}
    if rules is not None:
        out = {"task": out.pop("task"), "rules": rules, **out}
    return out


SPECIAL = {   # day -> [open, high, low, close]
    "2026-11-17": [44.0, 50.5, 41.0, 44.0],   # the as-of day itself: would fill everything; never used
    "2026-11-18": [44.0, 44.2, 42.9, 43.5],   # the long limit fills at 43.0
    "2026-12-01": [44.5, 47.2, 44.4, 46.5],   # the long reaches 46.0; the short fills at 47.0
    "2026-12-03": [46.5, 48.3, 46.4, 47.5],   # the short is stopped at 48.0
}


def daily(start="2026-10-01", end="2027-03-31", special=SPECIAL, flat=46.0):
    """Weekday candles: 46.48 until the as-of week (the scale check), then `special` days, else
    a flat candle at `flat`."""
    rows, day = [], date.fromisoformat(start)
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            key = day.isoformat()
            if key in special:
                rows.append([key, *special[key]])
            else:
                p = 46.48 if day < date(2026, 11, 16) else (44.0 if key < "2026-12-01" else flat)
                rows.append([key, p, p, p, p])
        day += timedelta(days=1)
    return rows


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test tried to download prices")
    monkeypatch.setattr(sp, "download", refuse)


@pytest.fixture
def yahoo(monkeypatch):
    """yahoo(rows, splits=(), today=...) stubs the one download and the exchange's date."""
    calls = []

    def install(rows, splits=(), today=date(2027, 6, 1)):
        def fake(ticker, interval, start, tz):
            calls.append((ticker, interval, start, tz))
            return [r for r in rows if r[0] >= start.isoformat()], list(splits)
        monkeypatch.setattr(sp, "download", fake)
        monkeypatch.setattr(sp, "today_in", lambda tz: today)
        return calls
    return install


@pytest.fixture
def history(rz, capsys, tmp_path, input_data, projection_data, write_json, monkeypatch):
    """history.add(entries=..., provenance=...) saves one more pair into a temp history (as
    RZ_PROJECTIONS_DIR) and writes the extra files beside it; returns the folder."""
    root = tmp_path / "history"
    monkeypatch.setenv("RZ_PROJECTIONS_DIR", str(root))

    class History:
        def add(self, entries="default", provenance=None, raw=None):
            rz.save(write_json("in.json", input_data), write_json("out.json", projection_data), str(root))
            folder = Path(capsys.readouterr().out.split()[-1])
            if raw is not None:
                (folder / "entries.json").write_text(raw, encoding="utf-8")
            elif entries is not None:
                data = reply(projection_data) if entries == "default" else entries
                (folder / "entries.json").write_text(json.dumps(data), encoding="utf-8")
            if provenance is not None:
                (folder / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
            return folder
    h = History()
    h.root, h.proj = root, projection_data
    return h


def cli(capsys, *argv):
    code = se.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def window(rows):
    return [r for r in rows if AS_OF < r[0] <= VALID_UNTIL]


# ---- the walk is backtest_entries.walk() -------------------------------------------------

def test_known_fills_and_exits_match_backtest_entries_walk(history, yahoo):
    rows = daily()
    yahoo(rows)
    history.add()
    report = se.run()
    (record,) = report["records"]
    assert record["status"] == "ended" and record["rules"] == 2 and record["horizon"] == "1-3m"
    long_, short, never = record["setups"]
    assert (long_["fill_date"], long_["fill_price"], long_["exit"], long_["exit_date"], long_["r"]) == \
        ("2026-11-18", 43.0, "target", "2026-12-01", 3.0)
    assert (short["fill_date"], short["exit"], short["exit_date"], short["r"]) == ("2026-12-01", "stop", "2026-12-03", -1.0)
    assert not never["filled"] and never["exit"] is None and never["r"] == 0.0
    for played, raw in zip(record["setups"], SETUPS):     # exactly the backtest's walk, both orderings
        s = be.setup_of(raw)
        assert {k: played[k] for k in be.walk(s, window(rows))} == be.walk(s, window(rows))
        assert played["r_other"] == be.walk(s, window(rows), optimistic=True)["r"]


def test_the_as_of_day_is_never_used(history, yahoo):
    """The as-of day's candle (high 50.5, low 41) would fill all three; it is left out."""
    special = {k: v for k, v in SPECIAL.items() if k == "2026-11-17"}
    yahoo(daily(special=special, flat=44.0))
    history.add()
    (record,) = se.run()["records"]
    assert [s["filled"] for s in record["setups"]] == [False, False, False]


def test_optimistic_takes_the_other_bound(history, yahoo):
    """A candle that reaches both the stop and the target after a fill: stop first by default."""
    special = {"2026-11-18": [44.0, 44.2, 42.9, 43.5], "2026-11-20": [44.0, 46.5, 41.5, 44.0]}
    yahoo(daily(special=special, flat=44.0))
    history.add()
    pessimistic = se.run()["records"][0]["setups"][0]
    optimistic = se.run(optimistic=True)["records"][0]["setups"][0]
    assert (pessimistic["exit"], pessimistic["r"], pessimistic["ambiguous"]) == ("stop", -1.0, True)
    assert (optimistic["exit"], optimistic["r"], optimistic["r_other"]) == ("target", 3.0, -1.0)


def test_downloads_once_per_ticker_from_the_earliest_day(history, yahoo):
    calls = yahoo(daily())
    history.add()
    history.add(entries=reply(history.proj, rules=None))
    se.run()
    assert calls == [("PETR4.SA", "1d", date(2026, 11, 17) - timedelta(days=se.LOOKBACK_DAYS), "America/Sao_Paulo")]


# ---- open and ended ----------------------------------------------------------------------

def test_open_setups_are_provisional_and_left_out_of_the_summary(history, yahoo):
    special = {"2026-11-18": SPECIAL["2026-11-18"]}       # the long fills, nothing else happens yet
    yahoo([r for r in daily(special=special, flat=44.0) if r[0] <= "2026-12-10"], today=date(2026, 12, 10))
    history.add()
    report = se.run()
    (record,) = report["records"]
    assert record["status"] == "open"
    long_, short, never = record["setups"]
    assert long_["filled"] and long_["exit"] == "open" and long_["r"] == 1.0   # marked at the close of 44.0
    assert not short["filled"] and short["exit"] is None
    s = report["summary"]
    assert (s["open_setups"], s["ended_setups"], s["by_rules"]) == (3, 0, {})
    assert "No setup has reached its valid_until yet" in se.render(report)
    assert "OPEN until 2027-02-12: PROVISIONAL" in "\n".join(se.render_record(record))


def test_a_stop_hit_while_open_is_final(history, yahoo):
    special = {"2026-11-18": SPECIAL["2026-11-18"], "2026-11-24": [43.5, 43.6, 41.8, 42.0]}
    yahoo([r for r in daily(special=special, flat=44.0) if r[0] <= "2026-12-10"], today=date(2026, 12, 10))
    history.add()
    long_ = se.run()["records"][0]["setups"][0]
    assert (long_["exit"], long_["exit_date"], long_["r"]) == ("stop", "2026-11-24", -1.0)


# ---- rules versions, authors and the summary ---------------------------------------------

def test_rules_1_and_rules_2_are_summarised_apart(history, yahoo):
    yahoo(daily())
    history.add(provenance={"entries": {"made_by": "trade_entries.py"}})
    history.add(entries=reply(history.proj, rules=None),
                provenance={"entries": {"made_by": "claude-code", "claude": {"model": "claude-x"}}})
    report = se.run()
    by = report["summary"]["by_rules"]
    assert set(by) == {"1", "2"}
    for version in ("1", "2"):
        o = by[version]["overall"]
        assert (o["setups"], o["fills"], o["fill_observed"]) == (3, 2, round(2 / 3, 4))
        assert o["fill_stated"] == round((0.6 + 0.3 + 0.1) / 3, 4)
        assert o["win_observed"] == 0.5 and o["r_per_order"] == round((3.0 - 1.0 + 0) / 3, 4)
        assert "brier_fill" in o and "brier_fill_base_rate" in o and "brier_win_base_rate" in o
        assert set(by[version]["by_horizon"]) == {"1-3m"}
        assert set(by[version]["by_quality"]) == {"favourable", "marginal", "unfavourable"}
    assert set(by["2"]["by_author"]) == {"computed"} and set(by["1"]["by_author"]) == {"claude"}
    assert "reach and turn by formulas" in by["1"]["meaning"]
    assert "simulated price paths" in by["2"]["meaning"]
    text = se.render(report)
    assert "RULES 1:" in text and "RULES 2:" in text and "not advice" in text
    lines = {r["rules"]: "\n".join(se.render_record(r)) for r in report["records"]}
    assert "computed by trade_entries.py" in lines[2]
    assert "written by Claude Code (--entries-by-claude), claude-x" in lines[1]


def test_without_provenance_the_author_is_unrecorded(history):
    folder = history.add()
    assert se.entries_made_by(str(folder)) == {"by": "unrecorded"}


def test_the_same_file_in_runs_and_history_counts_once(history, yahoo, tmp_path):
    import shutil
    yahoo(daily())
    kept = history.add()
    run_folder = tmp_path / "runs" / "PETR4.SA" / kept.name
    shutil.copytree(kept, run_folder)
    report = se.run(runs=str(tmp_path / "runs"))
    assert len(report["records"]) == 1 and report["records"][0]["source"] == "history"
    assert report["duplicates"] == 1


# ---- missing and unreadable files ---------------------------------------------------------

def test_a_missing_entries_file_is_counted_not_scored(history, yahoo, capsys):
    yahoo(daily())
    bare = history.add(entries=None)
    history.add()
    report = se.run()
    assert report["without_entries"] == [str(bare)] and len(report["records"]) == 1
    assert "1 saved projection(s) without entries.json" in se.render(report)


def test_only_missing_entries_files_exit_1_with_a_message(history, capsys):
    history.add(entries=None)
    code, out, err = cli(capsys)
    assert code == 1 and out == ""
    assert "1 saved projection(s), none with entries.json" in err


@pytest.mark.parametrize("raw, reason", [
    ("{not json", "entries.json is unreadable"),
    ('{"task": "trade_entries", "error": "no setup passes the rules"}', "holds no setups"),
    ('{"task": "trade_entries", "rules": 2, "entries": [{"rank": 1}]}', "malformed saved files"),
])
def test_unreadable_entries_are_reported_and_the_rest_scored(history, yahoo, capsys, raw, reason):
    yahoo(daily())
    history.add(raw=raw)
    history.add()
    code, out, _ = cli(capsys)
    assert code == 0 and reason in out and "RULES 2:" in out


def test_a_split_after_the_setups_is_not_scored(history, yahoo):
    yahoo(daily(), splits=[["2026-12-15", 2.0]])
    history.add()
    (record,) = se.run()["records"]
    assert "stock split after the setups" in record["not_scored"]


def test_ticker_filter_and_json(history, yahoo, capsys):
    yahoo(daily())
    history.add()
    code, out, _ = cli(capsys, "--ticker", "petr4.sa", "--json")
    data = json.loads(out)
    assert code == 0 and data["records"][0]["ticker"] == "PETR4.SA"
    assert data["settings"]["ordering"] == "pessimistic" and data["settings"]["caveats"] == se.CAVEATS
    code, _, err = cli(capsys, "--ticker", "NVDA")
    assert code == 1 and "for NVDA" in err


def test_no_cache_reaches_score_projections(history, yahoo, capsys, monkeypatch):
    monkeypatch.setattr(sp, "CACHE", True)
    yahoo(daily())
    history.add()
    cli(capsys, "--no-cache")
    assert sp.CACHE is False
