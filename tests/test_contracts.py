"""Conventions shared by build_prompt.py, rz.py and plot_projection.py.

The three pieces share no code. These tests pin what each side assumes about
the others (see "Contracts that span files" in CLAUDE.md).
"""
import argparse
import copy

import matplotlib.pyplot as plt
import pytest

import build_prompt
import plot_projection

LANGS = sorted(plot_projection.TEXT)


@pytest.fixture(autouse=True)
def close_figures():
    yield
    plt.close("all")


def plot(input_path, projection_path, **overrides):
    values = {"input": str(input_path), "projection": str(projection_path),
              "theme": "light", "lang": "en", "candles": None}
    values.update(overrides)
    return plot_projection.build(argparse.Namespace(**values))


# ---- horizon and candle labels -------------------------------------------------

@pytest.mark.parametrize("horizon", sorted(build_prompt.PRESETS))
def test_every_preset_horizon_is_known_to_rz(rz, horizon):
    assert horizon in rz.HORIZON_DAYS


@pytest.mark.parametrize("horizon", sorted(build_prompt.PRESETS))
def test_non_intraday_close_basis_is_known_to_rz(rz, horizon):
    preset = build_prompt.PRESETS[horizon]
    if preset["main"][0] in build_prompt.INTRADAY:
        # intraday bases take the other branch of rz.schedule() and of the plot's tick labels
        assert preset["close_basis"] not in rz.CANDLE_DAYS
        assert preset["close_basis"] in ("15m", "1h")
    else:
        assert preset["close_basis"] in rz.CANDLE_DAYS


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("horizon", sorted(build_prompt.PRESETS))
def test_every_preset_horizon_has_a_chart_label(horizon, lang):
    assert plot_projection.TEXT[lang]["labels"][horizon]


def test_rz_and_the_plot_know_no_horizon_the_presets_lack(rz):
    assert set(rz.HORIZON_DAYS) == set(build_prompt.PRESETS)
    for lang in LANGS:
        assert set(plot_projection.TEXT[lang]["labels"]) == set(build_prompt.PRESETS)


@pytest.mark.parametrize("horizon", sorted(build_prompt.PRESETS))
def test_label_of_the_main_interval_is_the_close_basis(horizon):
    preset = build_prompt.PRESETS[horizon]
    assert build_prompt.LABEL[preset["main"][0]] == preset["close_basis"]


@pytest.mark.parametrize("horizon", sorted(build_prompt.PRESETS))
def test_context_series_has_a_label_different_from_the_main_one(horizon):
    """Otherwise 'the series whose candles == close_basis' would be ambiguous."""
    preset = build_prompt.PRESETS[horizon]
    if preset["context"]:
        assert build_prompt.LABEL[preset["context"][0]] != preset["close_basis"]


def test_intraday_sets_agree():
    assert {build_prompt.LABEL[i] for i in build_prompt.INTRADAY} == {"15m", "1h"}   # plot_projection's `intraday`


def test_languages_have_the_same_keys():
    keys = [set(plot_projection.TEXT[lang]) for lang in LANGS]
    assert all(k == keys[0] for k in keys)
    for key in ("labels", "trend", "names", "levels"):
        nested = [set(plot_projection.TEXT[lang][key]) for lang in LANGS]
        assert all(n == nested[0] for n in nested)
    assert set(plot_projection.TEXT["en"]["names"]) == {"breakdown", "breakout", "range", "whipsaw"}
    assert set(plot_projection.TEXT["en"]["levels"]) == {"low", "medium"}   # never "high"


def test_fixture_matches_a_preset(input_data):
    preset = build_prompt.PRESETS[input_data["horizon"]]
    assert input_data["want"]["close_basis"] == preset["close_basis"]
    assert input_data["want"]["probability"] == preset["probability"]
    labels = [series["candles"] for series in input_data["data"]["series"]]
    assert labels == [build_prompt.LABEL[preset["context"][0]], build_prompt.LABEL[preset["main"][0]]]
    main, context = input_data["data"]["series"][1], input_data["data"]["series"][0]
    assert len(main["rows"]) == preset["main"][2]
    assert context["rows"][-1][0] < main["rows"][0][0]      # context covers only earlier history


# ---- main series selection -----------------------------------------------------

def plotted_bodies(fig):
    """(x, bottom) of each candle body, which identifies the rows that were drawn."""
    bodies = [p for p in fig.axes[0].patches if abs(p.get_width() - 0.62) < 1e-9]
    return [round(p.get_y(), 6) for p in sorted(bodies, key=lambda p: p.get_x())]


def expected_bodies(main, columns, shown):
    o, c = columns.index("open"), columns.index("close")
    return [round(min(row[o], row[c]), 6) for row in main["rows"][-shown:]]


@pytest.mark.parametrize("order", ["context_first", "main_first"])
def test_rz_and_plot_pick_the_same_main_series(rz, input_data, projection_data, write_json, order):
    data = copy.deepcopy(input_data)
    if order == "main_first":      # the fallback "last series" would now be the wrong one
        data["data"]["series"].reverse()
    source = write_json("input.json", data)
    _, main, *_ = rz.load(source)
    assert main["candles"] == data["want"]["close_basis"] == projection_data["horizon"]["close_basis"] == "1W"
    fig = plot(source, write_json("projection.json", projection_data), candles=30)
    assert plotted_bodies(fig) == expected_bodies(main, data["data"]["columns"], 30)


def test_rz_and_plot_fall_back_to_the_last_series(rz, input_data, projection_data, write_json):
    data = copy.deepcopy(input_data)
    data["want"]["close_basis"] = projection_data["horizon"]["close_basis"] = "1D"   # no such series
    source = write_json("input.json", data)
    _, main, *_ = rz.load(source)
    assert main is not None and main["candles"] == "1W"
    fig = plot(source, write_json("projection.json", projection_data), candles=30)
    assert plotted_bodies(fig) == expected_bodies(main, data["data"]["columns"], 30)


def test_sim_reports_the_close_basis_the_plot_selects_on(run_sim, zones, input_data):
    out = run_sim(zones, paths=2_000)
    assert out["horizon"]["close_basis"] == input_data["want"]["close_basis"]
    assert out["horizon"]["label"] == input_data["horizon"]
    for lang in LANGS:
        assert out["horizon"]["label"] in plot_projection.TEXT[lang]["labels"]


# ---- scenario thresholds ---------------------------------------------------------

@pytest.mark.parametrize("supports, resistances, s_text, r_text", [
    ([[43.2, 44.6], [40.6, 42.2], [37.4, 38.6]], [[45.3, 47.1], [47.1, 49.6], [50.2, 52.7]], "40.6", "49.6"),
    ([[43, 44.6], [40, 42], [37, 38]], [[45.3, 47], [47, 50], [51, 53]], "40", "50"),      # whole numbers
    ([[43.25, 44.6], [40.65, 42.2]], [[45.3, 47.15], [47.15, 49.65]], "40.65", "49.65"),   # two zones a side
    ([[43.2, 44.6]], [[45.3, 47.1]], "43.2", "47.1"),                                      # one zone a side
])
def test_plot_recovers_the_scenario_thresholds_from_sim(
        run_sim, projection_from_sim, write_json, input_path, supports, resistances, s_text, r_text):
    zones = {"supports": supports, "resistances": resistances}
    out = run_sim(zones, paths=2_000)
    by_name = {s["name"]: s for s in out["scenarios"]}
    assert s_text in by_name["breakdown"]["low"]
    assert r_text in by_name["breakout"]["high"]

    projection = write_json("projection.json", projection_from_sim(out, zones))
    for lang in LANGS:
        t = plot_projection.TEXT[lang]
        note = t["scenario_note"].format(s=s_text.replace(".", t["decimal"]),
                                         r=r_text.replace(".", t["decimal"]))
        fig = plot(input_path, projection, lang=lang)
        notes = [text.get_text() for text in fig.axes[3].texts]
        assert any(text.startswith(note) for text in notes), notes
        plt.close(fig)


def test_scenario_names_from_sim_are_the_ones_check_and_the_plot_expect(run_sim, zones):
    names = {s["name"] for s in run_sim(zones, paths=2_000)["scenarios"]}
    for lang in LANGS:
        assert names == set(plot_projection.TEXT[lang]["names"])


# ---- ids, tails, last_row --------------------------------------------------------

def test_sim_ids_match_the_schema_ids(run_sim, zones, projection_data):
    out = run_sim(zones, paths=2_000)
    for side in ("supports", "resistances"):
        assert [e["id"] for e in out[side]] == [e["id"] for e in projection_data[side]]
    # tails are the entries with a null edge, and the plot tells them apart by that alone
    assert [z["id"] for z in projection_data["supports"] if None in z["zone"]] == ["below"]
    assert [z["id"] for z in projection_data["resistances"] if None in z["zone"]] == ["above"]


def test_sim_horizon_block_is_what_the_plot_reads(run_sim, zones, projection_data):
    out = run_sim(zones, paths=2_000)
    assert out["horizon"] == projection_data["horizon"]
    assert plot_projection.parse_day(out["horizon"]["last"]).isoformat() == out["horizon"]["last"]


def test_forming_last_row_removes_one_future_slot_in_the_plot(input_data, projection_path, write_json):
    """rz.py counts the forming candle as the first of `closes`; the plot must not draw it twice."""
    closed = copy.deepcopy(input_data)
    closed["data"]["series"][-1]["last_row"] = "closed"
    # few candles, so the 28%-of-history minimum width does not hide the difference
    forming_fig = plot(write_json("forming.json", input_data), projection_path, candles=10)
    closed_fig = plot(write_json("closed.json", closed), projection_path, candles=10)
    x_forming, x_closed = forming_fig.axes[0].get_xlim()[1], closed_fig.axes[0].get_xlim()[1]
    assert x_closed - x_forming == pytest.approx(1)
    assert x_forming == pytest.approx(10 - 0.5 + 12 + 0.3)      # 13 closes, one already on the chart


def test_example_projection_passes_check_and_plots(rz, capsys, projection_path, input_path):
    with pytest.raises(SystemExit) as stop:
        rz.check(str(projection_path))
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == "OK"
    assert len(plot(input_path, projection_path).axes) == 5
