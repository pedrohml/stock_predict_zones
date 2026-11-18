"""trade_entries.py: the filled prompt, the setups its rules allow, and the reply checker."""
import copy
import json
from pathlib import Path

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
    picked = trade_entries.pick(trade_entries.setups(example[1]))
    assert [entry["id"] for entry in template_example()["entries"]] == [item["id"] for item in picked]
    assert template_example()["units"] == trade_entries.units(example[1])


# ---- the rules --------------------------------------------------------------------

def test_setups_follow_the_rules(example):
    proj = example[1]
    found = trade_entries.setups(proj)
    assert found == sorted(found, key=lambda item: (-item["per_order"], -item["fill"]))
    for item in found:
        if item["side"] == "long":
            assert item["stop"] < item["entry"] < item["target"]
        else:
            assert item["target"] < item["entry"] < item["stop"]
        assert item["ratio"] >= 0.5 and 0 <= item["win_if_filled"] <= 1 and 0 <= item["fill"] <= 1
        assert item["per_order"] == pytest.approx(item["fill"] * item["if_filled"], abs=0.006)
    # continuation never starts from the last zone on a side
    assert not {"short-S3-continuation", "long-R3-continuation"} & {item["id"] for item in found}


def test_price_inside_a_zone_gives_a_market_order(projection_data):
    by_id = {item["id"]: item for item in trade_entries.setups(projection_data)}
    inside = by_id["long-S1-reversal"]          # the fixture's price 44.06 sits in S1 43.2-44.6
    assert (inside["order"], inside["entry"], inside["fill"]) == ("market", 44.06, 1.0)


def test_pick_keeps_three_distinct_entries_on_both_sides(example, projection_data):
    for proj in (example[1], projection_data):
        picked = trade_entries.pick(trade_entries.setups(proj))
        assert len(picked) == 3 and len({item["entry"] for item in picked}) == 3
        assert len({item["side"] for item in picked}) == 2


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
