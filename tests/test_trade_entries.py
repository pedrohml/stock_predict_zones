"""trade_entries.py: the filled prompt, the setups its rules allow, and the reply checker."""
import copy
import json
from pathlib import Path

import math

import numpy as np
import pytest

import trade_entries
from conftest import entries_reply

ROOT = Path(trade_entries.__file__).resolve().parent
EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="module")
def example():
    """The committed run the template's own example was computed from."""
    return (json.loads((EXAMPLES / "petr4_input.json").read_text(encoding="utf-8")),
            json.loads((EXAMPLES / "petr4_projection.json").read_text(encoding="utf-8")))


def template_example():
    text = trade_entries.TEMPLATE.read_text(encoding="utf-8")
    return json.loads(text[text.index('\n{\n  "task"'):])


# ---- the prompt -----------------------------------------------------------------

def test_fill_puts_both_files_in_and_drops_the_comment():
    prompt = trade_entries.fill('{"a": 1}\n', '{"b": 2}\n')
    assert "{{" not in prompt and "<!--" not in prompt
    assert '<input>\n{"a": 1}\n</input>' in prompt and '<projection>\n{"b": 2}\n</projection>' in prompt


def test_prompt_command_prints_the_filled_template(capsys):
    assert trade_entries.main(["prompt", str(EXAMPLES / "petr4_input.json"), str(EXAMPLES / "petr4_projection.json")]) == 0
    assert '"ticker": "PETR4.SA"' in capsys.readouterr().out


# ---- the template's own example ---------------------------------------------------

def test_the_template_example_passes_its_own_check(example):
    assert trade_entries.problems(*example, template_example()) == []


def test_the_template_example_is_the_reference_pick(example):
    picked = trade_entries.pick(trade_entries.setups(*example))
    assert [entry["id"] for entry in template_example()["entries"]] == [item["id"] for item in picked]
    assert template_example()["units"] == trade_entries.units(example[1])
    assert template_example()["rules"] == trade_entries.RULES


# ---- the rules --------------------------------------------------------------------

def test_setups_follow_the_rules(example):
    found = trade_entries.setups(*example)
    assert found == sorted(found, key=lambda item: (-item["per_order"], -item["fill"], item["id"]))
    for item in found:
        if item["side"] == "long":
            assert item["stop"] < item["entry"] < item["target"]
        else:
            assert item["target"] < item["entry"] < item["stop"]
        assert item["ratio"] >= 0.5 and 0 <= item["win_if_filled"] <= 1 and 0 <= item["fill"] <= 1
        assert item["per_order"] == trade_entries.half_up(item["fill"] * item["if_filled"])
        assert item["quality"] == trade_entries.quality(item["if_filled"], trade_entries.band(example[0]))
        d = item["detail"]
        assert item["win_if_filled"] + d["loss"] + d["open"] == pytest.approx(1, abs=0.006)
    # continuation never starts from the last zone on a side
    assert not {"short-S3-continuation", "long-R3-continuation"} & {item["id"] for item in found}


def test_price_inside_a_zone_gives_a_market_order(input_data, projection_data):
    by_id = {item["id"]: item for item in trade_entries.setups(input_data, projection_data)}
    inside = by_id["long-S1-reversal"]          # the fixture's price 44.06 sits in S1 43.2-44.6
    assert (inside["order"], inside["entry"], inside["fill"]) == ("market", 44.06, 1.0)


def test_pick_keeps_three_distinct_entries(example, input_data, projection_data):
    for source, proj in (example, (input_data, projection_data)):
        found = trade_entries.setups(source, proj)
        picked = trade_entries.pick(found)
        assert len(picked) == 3 and len({item["entry"] for item in picked}) == 3
        assert picked == sorted(picked, key=lambda item: (-item["per_order"], -item["fill"], item["id"]))
    assert {item["side"] for item in picked} == {"long"}   # the fixture's best three: no short swapped in
    assert any(item["side"] == "short" for item in found)


def test_pick_keeps_the_best_three_whatever_their_side():
    def item(name, side, entry, per_order):
        return {"id": name, "side": side, "entry": entry, "per_order": per_order, "fill": 0.5}
    found = [item("a", "long", 10, 0.09), item("b", "long", 10, 0.08), item("c", "long", 11, 0.07),
             item("d", "long", 12, 0.03), item("e", "short", 9, 0.02)]
    assert [x["id"] for x in trade_entries.pick(found)] == ["a", "c", "d"]   # b repeats a's entry; e is not swapped in


@pytest.fixture
def shorts_worse(monkeypatch):
    """Every short's edge 1 R lower, so the example's best three distinct entries are all long."""
    real = trade_entries.odds

    def odds(sim, side, *rest):
        out = real(sim, side, *rest)
        return {**out, "if_filled": out["if_filled"] - (1.0 if side == "short" else 0.0)}
    monkeypatch.setattr(trade_entries, "odds", odds)


def test_a_one_sided_pick_is_kept_and_said_plainly(example, shorts_worse):
    source, proj = example
    found = trade_entries.setups(source, proj)
    reply = trade_entries.build(source, proj, found)
    assert [entry["side"] for entry in reply["entries"]] == ["long"] * 3
    assert [entry["id"] for entry in reply["entries"]] == [item["id"] for item in found if item["side"] == "long"][:3]
    line = trade_entries.TEXT["en"]["one_side"].format(side="long")
    assert line in reply["confidence"]["limits"] and "not a forecast of direction or a recommendation" in line
    assert trade_entries.problems(source, proj, reply) == [] and trade_entries.schema_problems(reply) == []

    # the rules-2 pick, the best short swapped in for the third, no longer passes the check
    short = next(item for item in found if item["side"] == "short")
    swapped = copy.deepcopy(reply)
    swapped["entries"][2] = trade_entries.entry_of(3, short, proj, trade_entries.TEXT["en"])
    assert trade_entries.schema_problems(swapped) == []
    assert any(line.startswith("ranking: got") for line in trade_entries.problems(source, proj, swapped))


def test_the_one_side_line_in_portuguese(example, shorts_worse):
    source, proj = copy.deepcopy(example)
    source.setdefault("reply", {})["language"] = "pt-BR"
    limits = trade_entries.build(source, proj)["confidence"]["limits"]
    assert trade_entries.TEXT["pt"]["one_side"].format(side="comprado") in limits
    assert set(trade_entries.TEXT["pt"]["sides"]) == set(trade_entries.TEXT["en"]["sides"]) == {"long", "short"}


def test_both_sides_kept_add_no_one_side_line(example):
    limits = trade_entries.build(*example)["confidence"]["limits"]
    assert not any(line.startswith("every setup kept is on the") for line in limits)


def test_an_entries_file_of_rules_2_is_named_as_such(input_data, projection_data, entries_data):
    old = copy.deepcopy(entries_data)
    old["rules"] = 2
    bad = trade_entries.problems(input_data, projection_data, old)
    assert len(bad) == 1 and "earlier version of the rules (rules 2, now 3)" in bad[0] and "other side" in bad[0]


def test_half_up_rounding_of_the_buffer(example):
    assert trade_entries.units(example[1]) == {"move": 2.78, "buffer": 0.70, "range": 8.94}   # 0.695 goes up


# ---- the checker ------------------------------------------------------------------

def test_a_reply_built_from_the_rules_passes(input_data, projection_data, entries_data):
    assert trade_entries.problems(input_data, projection_data, entries_data) == []


@pytest.mark.parametrize("change, expected", [
    (lambda r: r["entries"][0]["stop"].update(price=r["entries"][0]["stop"]["price"] + 1), "stop.price"),
    (lambda r: r["entries"][1]["odds"].update(win_if_filled=0.99), "odds.win_if_filled"),
    (lambda r: r["entries"][0]["edge"].update(per_order=0.9), "edge.per_order"),
    (lambda r: r["entries"][0].update(quality="unfavourable"), "quality"),
    (lambda r: r["entries"].reverse(), "rank"),
    (lambda r: r["entries"][2].update(id="long-S9-reversal"), "not a setup the rules allow"),
    (lambda r: r["units"].update(buffer=2.0), "units.buffer"),
    (lambda r: r.update(valid_until="2030-01-01"), "valid_until"),
    (lambda r: r["confidence"].update(level="medium"), "confidence.level"),
    (lambda r: r["entries"].pop(), "expected 3"),
    (lambda r: r["entries"][0].update(size="10% of the account"), "size"),          # schema: no extra keys
    (lambda r: r["entries"][0]["payoff"].pop("ratio"), "ratio"),                    # schema: required
])
def test_the_checker_names_what_is_wrong(input_data, projection_data, entries_data, change, expected):
    projection_data["confidence"]["level"] = "low"
    reply = copy.deepcopy(entries_data)
    change(reply)
    bad = trade_entries.problems(input_data, projection_data, reply)
    assert bad and any(expected in line for line in bad), bad


def test_a_rounding_that_fell_the_other_way_is_accepted(input_data, projection_data, entries_data):
    entries_data["entries"][0]["payoff"]["ratio"] = round(entries_data["entries"][0]["payoff"]["ratio"] + 0.01, 2)
    assert trade_entries.problems(input_data, projection_data, entries_data) == []


def test_an_error_reply_is_right_only_when_the_files_disagree(input_data, projection_data):
    error = {"task": "trade_entries", "error": "times differ"}
    assert trade_entries.problems(input_data, projection_data, error) != []
    input_data["now"]["time"] = "2026-11-18T10:00-03:00"
    assert trade_entries.problems(input_data, projection_data, error) == []
    assert entries_reply(input_data, projection_data)["error"].startswith("projection.as_of.time")


def test_check_command(tmp_path, input_path, projection_path, entries_data, capsys):
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps(entries_data), encoding="utf-8")
    assert trade_entries.main(["check", str(input_path), str(projection_path), str(reply)]) == 0
    assert capsys.readouterr().out.strip() == "OK"
    entries_data["entries"][0]["target"]["price"] += 2
    reply.write_text(json.dumps(entries_data), encoding="utf-8")
    assert trade_entries.main(["check", str(input_path), str(projection_path), str(reply)]) == 1
    assert "target.price" in capsys.readouterr().out
    assert trade_entries.main(["check", str(input_path), str(projection_path), str(tmp_path / "missing.json")]) == 2
    assert trade_entries.main([]) == 2


# ---- build: the whole reply computed, sentences included -----------------------------

@pytest.mark.parametrize("language", ["en", "pt", "pt-BR", "es", None])
def test_build_passes_its_own_check_in_every_language(input_data, projection_data, language):
    if language is None:
        input_data.pop("reply", None)
    else:
        input_data.setdefault("reply", {})["language"] = language
    reply = trade_entries.build(input_data, projection_data)
    assert trade_entries.problems(input_data, projection_data, reply) == []
    assert [entry["rank"] for entry in reply["entries"]] == [1, 2, 3]
    wanted = trade_entries.TEXT["pt" if language in ("pt", "pt-BR") else "en"]
    assert reply["note"] == wanted["note"]
    text = json.dumps(reply, ensure_ascii=False).lower()
    assert "{" not in "".join(e["why"] + "".join(e["watch"]) + e["odds"]["basis"] for e in reply["entries"])
    assert not any(word in text for word in ("you should", "we recommend", "position size", "leverage"))


def test_every_language_has_every_sentence():
    keys = set(trade_entries.TEXT[trade_entries.FALLBACK_LANG])
    assert all(set(text) == keys for text in trade_entries.TEXT.values())


def test_build_reproduces_the_prompt_example(example):
    """The example in prompts/trade_entries.md was computed from the examples/ pair."""
    source, proj = example
    reply = trade_entries.build(source, proj)
    first = reply["entries"][0]
    assert (first["id"], first["entry"]["anchor"], first["stop"]["anchor"], first["target"]["anchor"]) == (
        "long-R1-continuation", "R1 far edge", "R1 near edge 43.23 − buffer 0.70", "R2 far edge")
    assert first["odds"]["basis"] == template_example()["entries"][0]["odds"]["basis"]
    second = reply["entries"][1]
    assert second["stop"]["anchor"] == "S1 near edge 42.52 + buffer 0.70"
    assert all(any("2026-12-21" in line for line in entry["watch"]) for entry in reply["entries"])
    assert "8 setups were valid, 3 kept" in reply["method"] and "20,000 simulated paths" in reply["method"]
    for got, shown in zip(reply["entries"], template_example()["entries"]):
        assert (got["odds"]["fill"], got["odds"]["win_if_filled"], got["edge"]) == (
            shown["odds"]["fill"], shown["odds"]["win_if_filled"], shown["edge"])
    assert trade_entries.problems(source, proj, reply) == []


def test_a_reversal_names_the_zones_in_the_way(example):
    by_id = {item["id"]: item for item in trade_entries.setups(*example)}
    t = trade_entries.TEXT["en"]
    entry = trade_entries.entry_of(1, by_id["long-S2-reversal"], example[1], t)
    assert entry["stop"]["anchor"] == "S2 far edge 37.40 − buffer 0.70"
    assert entry["watch"][0] == "S1 40.65–42.52 lies between entry and target"


def test_no_positive_edge_is_said_plainly(input_data, projection_data, monkeypatch):
    real = trade_entries.odds
    monkeypatch.setattr(trade_entries, "odds", lambda *args: {**real(*args), "if_filled": -0.3})
    reply = trade_entries.build(input_data, projection_data)
    assert reply["entries"] and all(entry["quality"] == "unfavourable" for entry in reply["entries"])
    assert trade_entries.TEXT["en"]["no_edge"] in reply["confidence"]["limits"]
    assert trade_entries.problems(input_data, projection_data, reply) == []
    monkeypatch.setattr(trade_entries, "odds", real)
    reply = trade_entries.build(input_data, projection_data)
    has_positive = any(item["per_order"] > 0 for item in trade_entries.setups(input_data, projection_data))
    assert (trade_entries.TEXT["en"]["no_edge"] in reply["confidence"]["limits"]) == (not has_positive)


def test_an_entries_file_of_the_earlier_rules_is_named_as_such(input_data, projection_data, entries_data):
    old = copy.deepcopy(entries_data)
    del old["rules"]
    bad = trade_entries.problems(input_data, projection_data, old)
    assert len(bad) == 1 and "earlier version of the rules (rules 1" in bad[0] and "zones entries" in bad[0]


# ---- quality: the edge against the model's measured error at the horizon -----------

def test_the_quality_band_follows_the_horizon():
    assert trade_entries.band({"horizon": "1-2w"}) == 0.35
    assert trade_entries.band({"horizon": "1-3m"}) == 0.09
    assert trade_entries.band({"horizon": "6-12m"}) == 0.32
    widest = max(trade_entries.EDGE_BAND.values())
    assert trade_entries.band({"horizon": "today"}) == widest and trade_entries.band({}) == widest


@pytest.mark.parametrize("edge, label", [(0.13, "favourable"), (0.12, "marginal"), (0.0, "marginal"),
                                         (-0.12, "marginal"), (-0.13, "unfavourable"), (-0.5, "unfavourable")])
def test_an_edge_inside_the_band_cannot_be_told_from_zero(edge, label):
    assert trade_entries.quality(edge, 0.13) == label


def test_the_label_is_recomputed_for_the_horizon(example, monkeypatch):
    """The same edges get other labels under another band, and check recomputes them."""
    source, proj = example
    reply = trade_entries.build(source, proj)
    assert [e["quality"] for e in reply["entries"]] == ["favourable", "marginal", "marginal"]   # band 0.09
    assert "±0.09 R" in " ".join(reply["confidence"]["limits"])
    monkeypatch.setitem(trade_entries.EDGE_BAND, "1-3m", 0.35)
    assert trade_entries.problems(source, proj, reply) == [
        "long-R1-continuation.quality: favourable, expected marginal"]
    again = trade_entries.build(source, proj)
    assert {e["quality"] for e in again["entries"]} == {"marginal"} and trade_entries.problems(source, proj, again) == []
    assert "±0.35 R" in " ".join(again["confidence"]["limits"])


def test_every_language_explains_the_band_without_advice():
    for text in trade_entries.TEXT.values():
        line = text["quality"].format(band="0.13")
        assert "±0.13 R" in line and "marginal" in line
    assert "none of the three is a recommendation" in trade_entries.TEXT["en"]["quality"]
    assert "nenhum dos três é recomendação" in trade_entries.TEXT["pt"]["quality"]


def test_build_reports_why_nothing_can_be_built(input_data, projection_data):
    projection_data["as_of"]["time"] = "2020-01-01T10:00-03:00"
    assert trade_entries.build(input_data, projection_data) == {
        "task": "trade_entries", "error": "projection.as_of.time differs from input.now.time"}


def test_build_command_writes_the_file(tmp_path, input_path, projection_path, capsys):
    target = tmp_path / "entries.json"
    assert trade_entries.main(["build", str(input_path), str(projection_path), "-o", str(target)]) == 0
    assert trade_entries.main(["check", str(input_path), str(projection_path), str(target)]) == 0
    assert capsys.readouterr().out.strip() == "OK"
    assert trade_entries.main(["build", str(input_path), str(projection_path)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(target.read_text(encoding="utf-8"))


# ---- prices of any size: rounding to the candle rows' precision ----------------------

def scaled(example, factor, places):
    """The example pair with every price multiplied by `factor`, rounded to `places` decimals,
    like build_prompt.py --decimals PLACES on an asset priced that way."""
    source, proj = copy.deepcopy(example)
    for series in source["data"]["series"]:
        for row in series["rows"]:
            row[1:5] = [round(x * factor, places) for x in row[1:5]]
    source["now"]["price"] = proj["as_of"]["price"] = round(source["now"]["price"] * factor, places)
    for side in ("supports", "resistances"):
        for zone in proj[side]:
            zone["zone"] = [None if x is None else round(x * factor, places) for x in zone["zone"]]
    return source, proj


def test_places_reads_the_precision_of_the_rows(example, input_data):
    assert trade_entries.places(example[0]) == trade_entries.places(input_data) == 2      # never fewer than 2
    assert trade_entries.places(scaled(example, 0.001, 5)[0]) == 5
    assert trade_entries.places(scaled(example, 1e-7, 12)[0]) == trade_entries.MAX_PLACES
    assert trade_entries.places({}) == 2


def test_a_price_around_four_cents_builds_and_checks(example):
    """Was a ZeroDivisionError: risk was rounded to two decimals, and 0.002 became 0.00."""
    source, proj = scaled(example, 0.001, 5)
    reply = trade_entries.build(source, proj)
    assert trade_entries.problems(source, proj, reply) == []
    assert reply["units"] == {"move": 0.00278, "buffer": 0.0007, "range": 0.00894}
    # the same setups as at 42.77, every price a thousandth: the rules do not depend on the scale
    big = trade_entries.build(*example)
    assert [e["id"] for e in reply["entries"]] == [e["id"] for e in big["entries"]]
    for small, large in zip(reply["entries"], big["entries"]):
        assert small["payoff"]["ratio"] == large["payoff"]["ratio"]
        assert small["stop"]["price"] == pytest.approx(large["stop"]["price"] / 1000, abs=1e-9)
    assert reply["entries"][0]["stop"]["anchor"] == "R1 near edge 0.04323 − buffer 0.00070"


def test_the_checker_is_as_strict_on_small_prices(example):
    source, proj = scaled(example, 0.001, 5)
    reply = trade_entries.build(source, proj)
    reply["entries"][0]["stop"]["price"] += 0.001                    # 0.011 would have hidden this
    assert any("stop.price" in line for line in trade_entries.problems(source, proj, reply))


def test_a_risk_that_rounds_to_zero_is_no_setup(example):
    """Rows at two decimals cannot describe a 0.04 asset: zones and buffer collapse, every
    setup is discarded, and the error reply says so instead of dividing by zero."""
    source, proj = scaled(example, 0.001, 2)
    assert trade_entries.setups(source, proj) == []
    reply = trade_entries.build(source, proj)
    assert reply == {"task": "trade_entries", "error": "no setup passes the rules for this projection"}
    assert trade_entries.problems(source, proj, reply) == []


@pytest.mark.parametrize("zone", [[0.0, 0.01], [0.004, 0.006], [-0.5, 0.01]])
def test_a_price_at_or_below_zero_is_no_setup(example, zone):
    """A support near zero (a collapsed stock) once made a short target <= 0 and a log of it."""
    source, proj = copy.deepcopy(example)
    [z for z in proj["supports"] if None not in z["zone"]][-1]["zone"] = zone
    found = trade_entries.setups(source, proj)
    assert found and all(min(s["entry"], s["stop"], s["target"]) > 0 for s in found)
    reply = trade_entries.build(source, proj)
    assert trade_entries.problems(source, proj, reply) == []


def test_zone_edges_finer_than_the_rows_never_divide_by_zero(example):
    source, proj = copy.deepcopy(example)
    support = proj["supports"][1]
    support["zone"] = [round(support["zone"][1] - 0.004, 3), support["zone"][1]]   # finer than the rows
    proj["structure"]["volatility"]["assumed"] = [0.0001, 0.0001]                  # buffer 0.00
    found = trade_entries.setups(source, proj)
    assert all(item["risk"] > 0 for item in found)
    assert "long-S2-reversal" not in {item["id"] for item in found}


def test_expected_command_takes_the_input_for_the_precision(tmp_path, example, capsys):
    source, proj = scaled(example, 0.001, 5)
    paths = []
    for name, doc in (("i.json", source), ("p.json", proj)):
        paths.append(tmp_path / name)
        paths[-1].write_text(json.dumps(doc), encoding="utf-8")
    assert trade_entries.main(["expected", str(paths[0]), str(paths[1])]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0]) == {"move": 0.00278, "buffer": 0.0007, "range": 0.00894}
    assert trade_entries.main(["expected", str(paths[1])]) == 2          # the input is needed for the paths


# ---- the path estimate: paths() and odds() -------------------------------------------

def days(highs, lows, last):
    """A hand-made simulation for odds(): one row per path, prices (not logs) per day."""
    return {"high": np.log(np.array(highs, float)), "low": np.log(np.array(lows, float)),
            "last": np.array(last, float), "steps": len(highs[0]), "method": 2}


def test_odds_on_known_paths():
    """A long limit at 100, stop 95, target 110 (ratio 2), on four paths of three days."""
    sim = days(highs=[[104, 103, 111],    # fills on day 1, target on day 3: +2
                      [104, 101, 102],    # fills on day 1, stop on day 2: -1
                      [106, 107, 108],    # never fills: 0
                      [101, 103, 104]],   # fills on day 1, still open, last close 103: +0.6
               lows=[[99, 98, 104],
                     [99, 94, 97],
                     [101, 102, 103],
                     [99.5, 99, 101]],
               last=[110, 96, 107, 103])
    got = trade_entries.odds(sim, "long", "limit", 100, 95, 110, 2.0)
    assert got["fill"] == 0.75
    assert got["win"] == pytest.approx(1 / 3) and got["loss"] == pytest.approx(1 / 3)
    assert got["open"] == pytest.approx(1 / 3)
    assert got["if_filled"] == pytest.approx((2 - 1 + 0.6) / 3)


def test_odds_count_a_day_with_both_levels_as_the_stop():
    """Pessimistic, as backtest_entries.walk() by default: stop and target on one day is a loss,
    and so is a stop touched on the fill day; a stop order may reach its target that same day."""
    both = days(highs=[[104, 111]], lows=[[99, 94]], last=[100])
    assert trade_entries.odds(both, "long", "limit", 100, 95, 110, 2.0)["loss"] == 1.0
    fill_day = days(highs=[[104, 120]], lows=[[94, 101]], last=[115])
    assert trade_entries.odds(fill_day, "long", "limit", 100, 95, 110, 2.0)["loss"] == 1.0
    breakout = days(highs=[[111, 112]], lows=[[101, 108]], last=[110])       # long stop at 105, 110 that day
    assert trade_entries.odds(breakout, "long", "stop", 105, 100, 110, 1.0)["win"] == 1.0
    limit_same_day = days(highs=[[111, 109]], lows=[[99, 101]], last=[108])  # a limit fill does not count it
    assert trade_entries.odds(limit_same_day, "long", "limit", 100, 95, 110, 2.0)["win"] == 0.0


def test_odds_match_the_gamblers_ruin_on_brownian_paths():
    """Driftless Brownian paths in log price, fine steps: from the entry, the chance of reaching
    +b before -a is a / (a + b). A short at the same levels wins the other way round."""
    rng = np.random.default_rng(1)
    logs = np.log(100) + np.cumsum(rng.standard_normal((5_000, 1_500)) * 0.004, axis=1)
    sim = {"high": logs, "low": logs, "last": np.exp(logs[:, -1]), "steps": 1_500, "method": 2}
    a, b = math.log(100 / 96), math.log(108 / 100)
    got = trade_entries.odds(sim, "long", "market", 100, 96, 108, 2.0)
    assert got["fill"] == 1.0 and got["open"] < 0.03
    assert got["win"] / (got["win"] + got["loss"]) == pytest.approx(a / (a + b), abs=0.025)
    short = trade_entries.odds(sim, "short", "market", 100, 108, 96, 0.5)
    assert short["win"] / (short["win"] + short["loss"]) == pytest.approx(b / (a + b), abs=0.025)


def test_paths_are_reproducible_and_leave_walk_alone(example, rz):
    source = example[0]
    one, two = trade_entries.paths(source), trade_entries.paths(source)
    assert all(np.array_equal(one[key], two[key]) for key in ("high", "low", "last"))
    assert trade_entries.setups(*example) == trade_entries.setups(*example)
    # the paths are the first PATHS rows of the baseline's own walk(): same seed, same draws
    d, main, t, h, l, c = rz.unpack(source)
    drift = rz.drift_of(main)
    small = rz.walk(d, main, t, c, {}, trade_entries.PATHS, drift)[0]
    large = rz.walk(d, main, t, c, {}, 3 * trade_entries.PATHS, drift)[0]
    assert np.array_equal(small, large[:trade_entries.PATHS])
    assert np.allclose(one["last"], small[:, -1])       # the bridge keeps walk()'s closes exactly
    assert (one["high"] >= one["low"]).all()
    # weekly closes become trading days: five a week, the forming week its share
    assert one["steps"] >= 5 * (example[1]["horizon"]["closes"] - 1) + 1


def test_wider_daily_highs_and_lows_only_add_touches(example, monkeypatch):
    """SPREAD 0 counts only the bridge's daily points; a wider day can only fill more often."""
    wide = {item["id"]: item["fill"] for item in trade_entries.setups(*example)}
    monkeypatch.setattr(trade_entries, "SPREAD", 0.0)
    narrow = {item["id"]: item["fill"] for item in trade_entries.setups(*example)}
    assert all(wide[key] >= narrow[key] for key in narrow if key in wide)
    assert any(wide[key] > narrow[key] for key in narrow if key in wide)


def test_prompt_carries_the_computed_setups(example):
    source, proj = example
    prompt = trade_entries.fill(json.dumps(source), json.dumps(proj))
    listed = prompt[prompt.index("<setups>\n") + 9:prompt.index("\n</setups>")]
    rows = json.loads(listed)
    assert [row["id"] for row in rows] == [item["id"] for item in trade_entries.setups(source, proj)]
    assert all("win_if_filled" in row and "loss" in row["detail"] for row in rows)
    assert "{{" not in prompt


def test_an_order_that_almost_never_fills_is_no_setup(input_data, projection_data, monkeypatch):
    """Its edge per order rounds to zero and would rank above every negative one."""
    real = trade_entries.odds
    monkeypatch.setattr(trade_entries, "odds", lambda *args: {**real(*args), "fill": 0.04})
    assert trade_entries.setups(input_data, projection_data) == []
    reply = trade_entries.build(input_data, projection_data)
    assert reply == {"task": "trade_entries", "error": trade_entries.NO_SETUP}
    assert trade_entries.problems(input_data, projection_data, reply) == []
