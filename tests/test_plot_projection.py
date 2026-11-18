"""Step 3, plot_projection.py: smoke tests on the Agg backend."""
import argparse
import copy

import matplotlib.pyplot as plt
import pytest

import plot_projection

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def close_figures():
    yield
    plt.close("all")


@pytest.fixture
def plot_args(input_path, projection_path):
    def make(**overrides):
        values = {"input": str(input_path), "projection": str(projection_path),
                  "theme": "light", "lang": None, "candles": None, "dpi": 50}
        values.update(overrides)
        return argparse.Namespace(**values)
    return make


def all_text(fig):
    found = [text.get_text() for text in fig.texts]
    for axis in fig.axes:
        found += [text.get_text() for text in axis.texts]
        found.append(axis.get_title(loc="left"))
    return found


@pytest.mark.parametrize("theme", sorted(plot_projection.THEMES))
@pytest.mark.parametrize("lang", sorted(plot_projection.TEXT))
def test_build_and_save_png(plot_args, tmp_path, theme, lang):
    fig = plot_projection.build(plot_args(theme=theme, lang=lang))
    assert isinstance(fig, plt.Figure)
    assert len(fig.axes) == 5
    target = tmp_path / f"{theme}_{lang}.png"
    fig.savefig(target, dpi=50, facecolor=fig.get_facecolor())
    data = target.read_bytes()
    assert data.startswith(PNG_MAGIC)
    assert len(data) > 5_000


def test_build_saves_svg(plot_args, tmp_path):
    fig = plot_projection.build(plot_args())
    target = tmp_path / "chart.svg"
    fig.savefig(target)
    assert "<svg" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("lang", sorted(plot_projection.TEXT))
def test_chart_text_is_translated(plot_args, lang):
    t = plot_projection.TEXT[lang]
    texts = all_text(plot_projection.build(plot_args(lang=lang)))
    assert t["title"].format(ticker="PETR4.SA", label=t["labels"]["1-3m"]) in texts
    assert t["footer"] in texts
    assert t["prob_title"] in texts
    assert t["scenarios"] in texts
    assert t["confidence"].format(level=t["levels"]["low"]) in texts
    for zone_id in ("S1", "S2", "S3", "R1", "R2", "R3"):
        assert zone_id in texts


# ---- chart language: one setting, taken from the input's reply.language ----------

def title_language(fig):
    """Which TEXT entry the figure's title was written with."""
    title = fig.texts[0].get_text()
    found = [lang for lang, t in plot_projection.TEXT.items()
             if title == t["title"].format(ticker="PETR4.SA", label=t["labels"]["1-3m"])]
    assert len(found) == 1, title
    return found[0]


@pytest.fixture
def input_in(input_data, write_json):
    """input_in(language) -> path of the fixture input asking for that reply.language."""
    def make(language):
        input_data["reply"]["language"] = language
        return write_json("input_lang.json", input_data)
    return make


@pytest.mark.parametrize("lang", sorted(plot_projection.TEXT))
def test_chart_follows_the_input_language_by_default(plot_args, input_in, lang, capsys):
    fig = plot_projection.build(plot_args(input=input_in(lang)))
    assert title_language(fig) == lang
    assert plot_projection.TEXT[lang]["footer"] in all_text(fig)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("asked", sorted(plot_projection.TEXT))
@pytest.mark.parametrize("lang", sorted(plot_projection.TEXT))
def test_explicit_lang_overrides_the_input_without_a_warning(plot_args, input_in, asked, lang, capsys):
    fig = plot_projection.build(plot_args(input=input_in(asked), lang=lang))
    assert title_language(fig) == lang
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("asked, expected", [
    ("pt-BR", "pt"), ("pt_BR", "pt"), ("PT", "pt"), (" pt-br ", "pt"), ("pt-Latn-BR", "pt"),
    ("en-US", "en"), ("en_GB", "en"), ("EN", "en"),
])
def test_region_variants_normalise(asked, expected, capsys):
    assert plot_projection.chart_language(None, {"reply": {"language": asked}}) == expected
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("asked", ["es", "es-MX", "Portuguese", "ptx"])
def test_unsupported_language_falls_back_with_one_note(plot_args, input_in, asked, capsys):
    fig = plot_projection.build(plot_args(input=input_in(asked)))
    assert title_language(fig) == plot_projection.FALLBACK_LANG
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert asked in err
    for lang in plot_projection.TEXT:       # the note lists every language the chart supports
        assert lang in err.split("supported:")[1]


def test_unsupported_language_is_silent_when_lang_is_explicit(capsys):
    lang = sorted(plot_projection.TEXT)[-1]
    assert plot_projection.chart_language(lang, {"reply": {"language": "es"}}) == lang
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("source", [
    {}, {"reply": None}, {"reply": {}}, {"reply": {"language": None}},
    {"reply": {"language": ""}}, {"reply": {"language": "  "}}, {"reply": {"language": 7}},
])
def test_missing_language_falls_back_silently(source, capsys):
    assert plot_projection.chart_language(None, source) == plot_projection.FALLBACK_LANG
    assert capsys.readouterr().err == ""


def test_input_without_reply_block_still_plots(plot_args, input_data, write_json, capsys):
    del input_data["reply"]
    fig = plot_projection.build(plot_args(input=write_json("no_reply.json", input_data)))
    assert title_language(fig) == plot_projection.FALLBACK_LANG
    assert capsys.readouterr().err == ""


def test_fallback_language_has_chart_text():
    assert plot_projection.FALLBACK_LANG in plot_projection.TEXT


def test_cli_without_lang_follows_the_input(plot_args, input_in, tmp_path, monkeypatch, capsys):
    args = plot_args()
    for asked in sorted(plot_projection.TEXT):
        target = tmp_path / f"cli_{asked}.svg"       # svg keeps the labels as text
        monkeypatch.setattr("sys.argv", ["plot_projection.py", input_in(asked), args.projection, "-o", str(target)])
        plot_projection.main()
        assert plot_projection.TEXT[asked]["prob_title"] in target.read_text(encoding="utf-8")
        assert "Note" not in capsys.readouterr().err


def test_cli_lang_accepts_only_chart_languages(plot_args, monkeypatch, capsys):
    args = plot_args()
    monkeypatch.setattr("sys.argv", ["plot_projection.py", args.input, args.projection, "--lang", "xx"])
    with pytest.raises(SystemExit):
        plot_projection.main()
    err = capsys.readouterr().err
    choices = err.split("choose from")[1]
    for lang in plot_projection.TEXT:
        assert lang in choices


def test_themes_use_their_own_surface(plot_args):
    import matplotlib.colors as colors
    for theme, palette in plot_projection.THEMES.items():
        fig = plot_projection.build(plot_args(theme=theme))
        assert colors.to_hex(fig.get_facecolor()) == palette["surface"]


def test_candles_option_limits_the_history(plot_args):
    def bodies(fig):
        return [p for p in fig.axes[0].patches if abs(p.get_width() - 0.62) < 1e-9]
    assert len(bodies(plot_projection.build(plot_args()))) == 52      # max(40, 4 * 13 closes)
    assert len(bodies(plot_projection.build(plot_args(candles=20)))) == 20


def test_swing_markers_need_an_exact_row_start(plot_args, projection_data, write_json):
    def markers(fig):
        return sorted(line.get_marker() for line in fig.axes[0].lines if line.get_marker() in ("v", "^"))
    # the fixture's swing high (2026-10-19) is a visible weekly row; its low (2025-06-09) is off the chart
    assert markers(plot_projection.build(plot_args())) == ["v"]
    assert markers(plot_projection.build(plot_args(candles=104))) == ["^", "v"]
    projection_data["structure"]["swing"]["high"][1] = "2026-10-20"    # a Tuesday: no such row
    moved = write_json("moved.json", projection_data)
    assert markers(plot_projection.build(plot_args(projection=moved, candles=104))) == ["^"]


def test_projection_without_optional_sections(plot_args, projection_data, write_json):
    for key in ("structure", "events", "drivers", "confidence", "scenarios"):
        del projection_data[key]
    fig = plot_projection.build(plot_args(projection=write_json("bare.json", projection_data)))
    assert len(fig.axes) == 5


def test_projection_without_tails(plot_args, projection_data, write_json):
    projection_data["supports"] = projection_data["supports"][:-1]
    projection_data["resistances"] = projection_data["resistances"][:-1]
    with_tails = plot_projection.build(plot_args()).axes[0].get_ylim()
    without = plot_projection.build(
        plot_args(projection=write_json("no_tails.json", projection_data))).axes[0].get_ylim()
    assert without[0] > with_tails[0] and without[1] < with_tails[1]


def test_ticker_mismatch_only_warns(plot_args, projection_data, write_json, capsys):
    projection_data["asset"]["ticker"] = "VALE3.SA"
    fig = plot_projection.build(plot_args(projection=write_json("other.json", projection_data)))
    assert isinstance(fig, plt.Figure)
    assert "Warning" in capsys.readouterr().err


def test_parse_day():
    from datetime import date
    assert plot_projection.parse_day("2026-11-17T17:36-03:00") == date(2026, 11, 17)
    assert plot_projection.parse_day("2026-12-21") == date(2026, 12, 21)
