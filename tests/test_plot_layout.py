"""Step 3, plot_projection.py: where the chart's text ends up.

These tests measure text boxes with the figure's renderer instead of comparing pixels,
so they hold across matplotlib and font versions."""
import argparse
import copy
import itertools
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import pytest
from matplotlib.text import Annotation

import plot_projection

LANGS = sorted(plot_projection.TEXT)
AX, PX, SX, NOTES, SIDE = range(5)      # the order build() creates its axes in


@pytest.fixture(autouse=True)
def close_figures():
    yield
    plt.close("all")


@pytest.fixture
def draw(input_data, projection_data, write_json):
    """draw(projection=None, source=None, **args) -> the figure for the fixtures or edited copies."""
    def make(projection=None, source=None, **overrides):
        values = {"input": write_json("layout_input.json", source or input_data),
                  "projection": write_json("layout_projection.json", projection or projection_data),
                  "theme": "light", "lang": None, "candles": None, "dpi": 50}
        values.update(overrides)
        return plot_projection.build(argparse.Namespace(**values))
    return make


def box(fig, artist):
    return artist.get_window_extent(fig.canvas.get_renderer())


def overlap(a, b, pad=0.0):
    return a.x0 < b.x1 + pad and b.x0 < a.x1 + pad and a.y0 < b.y1 + pad and b.y0 < a.y1 + pad


def scaled(input_data, projection_data, factor, digits):
    """The fixtures with every price multiplied: another asset with the same shape."""
    r = lambda x: round(x * factor, digits)
    source, proj = copy.deepcopy(input_data), copy.deepcopy(projection_data)
    for series in source["data"]["series"]:
        series["rows"] = [[row[0]] + [r(x) for x in row[1:5]] + row[5:] for row in series["rows"]]
    proj["as_of"]["price"] = r(proj["as_of"]["price"])
    for zone in proj["supports"] + proj["resistances"]:
        zone["zone"] = [None if edge is None else r(edge) for edge in zone["zone"]]
    for key in ("high", "low"):
        proj["structure"]["swing"][key][0] = r(proj["structure"]["swing"][key][0])
    low, high = r(40.6), r(49.6)
    for scenario in proj["scenarios"]:
        scenario["low"] = f"below {low}" if scenario["name"] in ("breakdown", "whipsaw") else f"{low} or higher"
        scenario["high"] = f"above {high}" if scenario["name"] in ("breakout", "whipsaw") else f"{high} or lower"
    return source, proj


# ---- 1. probability panel: percentage and label -------------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_percentage_and_label_keep_one_gap_for_any_digit_count(draw, projection_data, lang):
    shares = [(1.0, 0.05), (0.95, 0.17), (0.78, 0.08), (0.70, 0.70)]          # 5%, 17%, 8%, 70%
    for zone, (reach, turn) in zip(projection_data["supports"], shares):
        zone["reach"], zone["turn"] = reach, turn
    for zone, (reach, turn) in zip(projection_data["resistances"], [(1.0, 1.0), (0.09, 0.05), (0.04, 0.03), (1.0, 1.0)]):
        zone["reach"], zone["turn"] = reach, turn                              # 100% after a full bar, twice
    fig = draw(projection_data, lang=lang)
    panel = fig.axes[PX]
    notes = [text for text in panel.texts if isinstance(text, Annotation)]
    assert len(notes) == 8
    gaps = []
    for note in notes:
        share = note.xycoords                       # the bold percentage the label is attached to
        assert share.get_text().endswith("%")
        gaps.append(box(fig, note).x0 - box(fig, share).x1)
        assert box(fig, note).x1 <= box(fig, panel).x1, note.get_text()      # reach 1.00 still fits the panel
    assert {len(note.xycoords.get_text()) for note in notes} == {2, 3, 4}    # "5%", "17%", "100%"
    assert min(gaps) > 2 and max(gaps) - min(gaps) < 0.5, gaps


def test_probability_scale_is_unchanged_when_the_labels_fit(draw):
    assert draw().axes[PX].get_xlim() == (0, 1.72)


# ---- 2. side panel: caveats and events always fit -----------------------------------

LONG = ("the floor of S1 is only two percent below the current price, and the December dividend alone is a drop "
        "of roughly 2.7 percent, so the first support may be crossed by the ex-date mechanics rather than by any "
        "change in sentiment, which the simulation cannot tell apart from a real break lower")
EVENTS = [{"date": "2026-12-21", "what": "ex-dividend, payout of about 1.19 per share announced with the results"},
          {"date": "2027-01-15", "what": "fuel price policy review by the board"},
          {"date": None, "what": "OPEC+ meeting, date not confirmed, may move Brent by several dollars"}]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("limit", [LONG, "WWWWWWWW MMMMMMMM " * 14], ids=["long", "wide-letters"])
def test_side_panel_fits_above_the_footer(draw, projection_data, lang, limit):
    assert len(LONG) >= 250
    projection_data["confidence"]["limits"] = [f"{i}) {limit}" for i in (1, 2, 3)]
    projection_data["events"] = EVENTS
    fig = draw(projection_data, lang=lang)
    t = plot_projection.TEXT[lang]
    footer = next(text for text in fig.texts if text.get_text() == t["footer"])
    whole = box(fig, fig)
    texts = fig.axes[SIDE].texts
    assert len(texts) == 2
    for text in texts:
        area = box(fig, text)
        assert area.y0 > box(fig, footer).y1, "side panel text reaches the footer"
        assert whole.x0 <= area.x0 and area.x1 <= whole.x1 and area.y1 <= whole.y1
    body = texts[1].get_text()
    assert "…" in body                                        # cut, not shrunk
    assert texts[1].get_fontsize() == pytest.approx(8.8)
    for i in (1, 2, 3):                                       # every caveat keeps its beginning
        assert f"•  {i}) " in body
    assert t["events"] in body
    for event in EVENTS:                                      # and every event stays visible
        assert event["what"].split()[0] in body.split(t["events"])[1]


def test_side_panel_is_not_cut_when_it_fits(draw, projection_data):
    body = draw().axes[SIDE].texts[1].get_text()
    assert "…" not in body
    for limit in projection_data["confidence"]["limits"]:
        assert " ".join(body.split()).count(" ".join(limit.split())[:30])


def test_share_lines():
    share = plot_projection.share_lines
    assert share([2, 2, 1], 2, 11, keep=1) == [2, 2, 1]                 # everything fits
    assert share([5, 5, 5, 2, 2, 2], 2, 11, keep=3) == [2, 2, 2, 1, 1, 1]   # caveats first, then events
    assert share([5, 1, 5], 0, 8) == [4, 1, 3]
    assert share([5, 5, 5, 1, 1], 2, 4, keep=2) == [0, 0, 0, 1, 1]      # events outlast the caveats
    assert share([5, 5, 5, 1, 1], 2, 5, keep=2) == [1, 0, 0, 1, 1]      # the last caveat goes first
    assert share([], 0, 5) == []


# ---- 3. scenario bar ------------------------------------------------------------------

COMBINATIONS = [(58, 26, 10, 6), (50, 25, 19, 6), (90, 5, 3, 2), (97, 1, 1, 1), (25, 25, 25, 25),
                (61, 26, 11, 2), (37, 32, 29, 2)]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("shares", COMBINATIONS, ids=lambda shares: "/".join(map(str, shares)))
def test_scenario_labels_do_not_collide_or_leave_the_bar(draw, projection_data, lang, shares):
    for scenario, share in zip(projection_data["scenarios"], shares):
        scenario["p"] = share / 100
    fig = draw(projection_data, lang=lang)
    axis = fig.axes[SX]
    bar = box(fig, axis)
    areas = [box(fig, text) for text in axis.texts]
    for area, text in zip(areas, axis.texts):
        assert bar.x0 - 0.5 <= area.x0 and area.x1 <= bar.x1 + 0.5, text.get_text()
    for a, b in itertools.combinations(areas, 2):
        assert not overlap(a, b)
    inside = [text for text in axis.texts if text.get_position()[1] == 0]
    segments = sorted(axis.patches, key=lambda patch: patch.get_x())
    for text in inside:                              # a label on the bar stays within its own segment
        segment = next(box(fig, patch) for patch in segments
                       if patch.get_x() <= text.get_position()[0] <= patch.get_x() + patch.get_width())
        assert segment.x0 <= box(fig, text).x0 and box(fig, text).x1 <= segment.x1
    written = " ".join(text.get_text() for text in axis.texts)
    names = plot_projection.TEXT[lang]["names"]
    for scenario in projection_data["scenarios"]:    # every scenario is named and has its percentage
        assert names[scenario["name"]] in written
        assert f"{round(scenario['p'] * 100)}%" in written


def test_spread():
    spread = plot_projection.spread
    assert spread([50, 200], [20, 20], 0, 300, 5) == [40, 190]             # room: centred where asked
    lefts = spread([280, 290, 298], [60, 50, 40], 0, 300, 8)               # crowded at the right edge
    assert lefts[-1] + 40 == 300
    assert all(b - (a + w) >= 8 for a, b, w in zip(lefts, lefts[1:], [60, 50]))
    assert spread([2, 5], [30, 30], 0, 300, 8) == [0, 38]                  # and at the left one


# ---- 4. intraday tick labels ----------------------------------------------------------

def intraday(input_data, projection_data):
    """A `today` pair: 15-minute candles over three sessions, the last candle forming."""
    stamps = [datetime(2026, 11, day, 10) + timedelta(minutes=15 * i) for day in (16, 17, 18) for i in range(28)]
    stamps = [stamp for stamp in stamps if stamp <= datetime(2026, 11, 18, 13, 15)]
    rows = []
    for i, stamp in enumerate(stamps):
        close = 44 + 0.3 * ((i * 7) % 11 - 5) / 5
        rows.append([stamp.strftime("%Y-%m-%dT%H:%M"), close - 0.05, close + 0.1, close - 0.12, round(close, 2)])
    source, proj = copy.deepcopy(input_data), copy.deepcopy(projection_data)
    source["horizon"] = "today"
    source["now"] = {"time": "2026-11-18T13:20-03:00", "price": 44.06, "session": "open"}
    source["data"]["series"] = [{"candles": "15m", "last_row": "forming", "rows": rows}]
    proj["as_of"]["time"] = source["now"]["time"]
    proj["horizon"] = {"label": "today", "close_basis": "15m", "closes": 15, "first": "2026-11-18", "last": "2026-11-18"}
    proj["structure"]["swing"] = {"high": [45.0, rows[5][0]], "low": [43.0, rows[9][0]]}
    proj["events"] = [{"date": "2026-11-18", "what": "minutes at 16:00"}]
    return source, proj


@pytest.mark.parametrize("lang, first", [("en", "16 Nov 13:45"), ("pt", "16/11 13:45")])
def test_intraday_ticks_use_the_language_format(draw, input_data, projection_data, lang, first):
    source, proj = intraday(input_data, projection_data)
    fig = draw(proj, source, lang=lang)
    fig.canvas.draw()
    axis = fig.axes[AX]
    labels = [label for label in axis.get_xticklabels() if ":" in label.get_text()]
    assert labels[0].get_text() == first
    assert len(labels) >= 4
    areas = [box(fig, label) for label in axis.get_xticklabels() if label.get_text()]
    for a, b in itertools.combinations(areas, 2):
        assert not overlap(a, b, pad=2)
    assert min(area.x0 for area in areas) >= box(fig, axis).x0       # nothing hangs over the price axis


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_formats_an_intraday_tick(lang):
    t = plot_projection.TEXT[lang]
    label = t["intraday_tick"].format(day=5, month=3, mon=t["months"][2], hour=9, minute=5)
    assert "09:05" in label and "5" in label.split()[0]


# ---- 5 and 6. prices: one format, the language's separators --------------------------------

def test_format_number_uses_the_language_separators():
    en, pt = plot_projection.TEXT["en"], plot_projection.TEXT["pt"]
    assert plot_projection.format_number(1234.5, 2, en) == "1,234.50"
    assert plot_projection.format_number(1234.5, 2, pt) == "1.234,50"
    assert plot_projection.format_number(95000, 0, en) == "95,000"
    assert plot_projection.format_number(0.04607, 5, pt) == "0,04607"
    for t in plot_projection.TEXT.values():
        assert t["thousands"] != t["decimal"]


def test_price_and_axis_decimals():
    assert [plot_projection.price_digits(x) for x in (95000, 10000, 1234.5, 44.06, 1, 0.5123, 0.05, 0.0004)] == \
        [0, 0, 2, 2, 2, 4, 5, 7]
    assert [plot_projection.step_digits(ticks) for ticks in
            ([30, 35, 40], [42.0, 42.5, 43.0], [1.25, 1.5, 1.75], [0.04, 0.045, 0.05], [70000, 80000], [5])] == \
        [0, 1, 2, 3, 0, 2]


@pytest.mark.parametrize("lang, rule", [("en", "below 40.60 · Breakout = closes above 49.60"),
                                        ("pt", "abaixo de 40,60 · Alta = fecha acima de 49,60")])
def test_scenario_rule_shows_prices_like_the_zone_labels(draw, lang, rule):
    fig = draw(lang=lang)
    assert rule in fig.axes[NOTES].texts[0].get_text()
    zone_labels = [text.get_text() for text in fig.axes[AX].texts]
    assert ("40.60 – 42.20" if lang == "en" else "40,60 – 42,20") in zone_labels


@pytest.mark.parametrize("lang, factor, digits, price, zone, tick", [
    ("en", 95000 / 44.06, 1, "95,000", "93,146 – 96,164", "100,000"),
    ("pt", 95000 / 44.06, 1, "95.000", "93.146 – 96.164", "100.000"),
    ("en", 1234.5 / 44.06, 2, "1,234.50", "1,210.40 – 1,249.63", "1,200"),
    ("pt", 0.05 / 44.06, 5, "0,05000", "0,04902 – 0,05061", "0,050"),
])
def test_high_and_low_priced_assets(draw, input_data, projection_data, lang, factor, digits, price, zone, tick):
    source, proj = scaled(input_data, projection_data, factor, digits)
    fig = draw(proj, source, lang=lang)
    fig.canvas.draw()
    axis = fig.axes[AX]
    texts = {text.get_text(): text for text in axis.texts}
    assert zone in texts and price in texts
    assert price in fig.texts[1].get_text()                       # the line under the title
    ticks = [label.get_text() for label in axis.get_yticklabels()]
    assert tick in ticks
    shown = ticks[1:-1]                                           # the outer ones may lie off the axis
    assert len(set(shown)) == len(shown), ticks                   # enough decimals to tell them apart
    ranges = [text for label, text in texts.items() if " – " in label]
    for text in ranges:                                           # the price box clears every zone label
        assert not overlap(box(fig, texts[price]), box(fig, text), pad=2)
        assert box(fig, text).x1 <= box(fig, axis).x1


# ---- other collisions ---------------------------------------------------------------------

def test_labels_of_thin_touching_zones_do_not_overlap(draw, projection_data):
    for zone, edges in zip(projection_data["supports"], ([43.9, 44.05], [43.6, 43.9], [43.3, 43.55], [None, 43.3])):
        zone["zone"] = edges
    for zone, edges in zip(projection_data["resistances"], ([44.1, 44.3], [44.3, 44.45], [50.2, 52.7], [52.7, None])):
        zone["zone"] = edges
    fig = draw(projection_data)
    ids = [text for text in fig.axes[AX].texts if text.get_text() in ("S1", "S2", "S3", "R1", "R2", "R3")]
    assert len(ids) == 6
    for a, b in itertools.combinations(ids, 2):
        assert not overlap(box(fig, a), box(fig, b))
    shares = [text for text in fig.axes[PX].texts if text.get_text().endswith("%") and not isinstance(text, Annotation)]
    assert len(shares) == 8
    for a, b in itertools.combinations(shares, 2):
        assert not overlap(box(fig, a), box(fig, b))


def test_zone_labels_stay_on_their_zone_when_there_is_room(draw, projection_data):
    fig = draw()
    for zone in projection_data["supports"] + projection_data["resistances"]:
        if None not in zone["zone"]:
            label = next(text for text in fig.axes[AX].texts if text.get_text() == zone["id"])
            assert label.get_position()[1] == pytest.approx(sum(zone["zone"]) / 2)


def test_event_date_is_dropped_where_it_would_touch_the_horizon_date(draw, projection_data):
    projection_data["events"] = [{"date": "2026-12-21", "what": "ex-dividend"}, {"date": "2027-01-15", "what": "review"}]
    fig = draw(projection_data)
    fig.canvas.draw()
    axis = fig.axes[AX]
    labels = [label for label in axis.get_xticklabels() if label.get_text()]
    assert "21 Dec" in [label.get_text() for label in labels]
    for a, b in itertools.combinations(labels, 2):
        assert not overlap(box(fig, a), box(fig, b), pad=2)
    assert len([line for line in axis.lines if line.get_marker() == "D"]) == 2   # both events keep their marker
