"""rz.py save / list: every projection is kept, with its input, for later scoring."""
import json
import os
from pathlib import Path

import pytest


def run(rz, capsys, command, *args):
    """Run a helper command; return (exit code, printed text)."""
    code = 0
    try:
        getattr(rz, command)(*args)
    except SystemExit as stop:
        code = stop.code
    return code, capsys.readouterr().out.strip()


@pytest.fixture
def pair(input_path, projection_path):
    return str(input_path), str(projection_path)


def test_save_keeps_input_and_projection_byte_for_byte(rz, capsys, pair, tmp_path):
    code, out = run(rz, capsys, "save", *pair, str(tmp_path))
    target = tmp_path / "PETR4.SA" / "2026-11-17T1736_1-3m"
    assert (code, out) == (0, f"saved {target}")
    assert (target / "input.json").read_bytes() == Path(pair[0]).read_bytes()
    assert (target / "projection.json").read_bytes() == Path(pair[1]).read_bytes()


def test_save_never_overwrites(rz, capsys, pair, tmp_path):
    for _ in range(3):
        assert run(rz, capsys, "save", *pair, str(tmp_path))[0] == 0
    names = sorted(p.name for p in (tmp_path / "PETR4.SA").iterdir())
    assert names == ["2026-11-17T1736_1-3m", "2026-11-17T1736_1-3m_2", "2026-11-17T1736_1-3m_3"]


def test_save_defaults_to_projections_in_the_working_directory(rz, capsys, pair, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RZ_PROJECTIONS_DIR", raising=False)
    assert run(rz, capsys, "save", *pair)[0] == 0
    assert (tmp_path / "projections" / "PETR4.SA" / "2026-11-17T1736_1-3m" / "projection.json").is_file()


def test_save_honours_the_environment_variable(rz, capsys, pair, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RZ_PROJECTIONS_DIR", str(tmp_path / "elsewhere"))
    assert run(rz, capsys, "save", *pair)[0] == 0
    assert (tmp_path / "elsewhere" / "PETR4.SA").is_dir() and not (tmp_path / "projections").exists()


def test_save_makes_the_ticker_safe_for_a_folder_name(rz, capsys, input_data, projection_data, write_json, tmp_path):
    for data in (input_data, projection_data):
        data["asset"]["ticker"] = "^BVSP/X"
    code, _ = run(rz, capsys, "save", write_json("in.json", input_data), write_json("out.json", projection_data),
                  str(tmp_path / "store"))
    assert code == 0
    assert [p.name for p in (tmp_path / "store").iterdir()] == ["_BVSP_X"]


@pytest.mark.parametrize("change, message", [
    (lambda i, p: p["confidence"].update(level="high"), "confidence is low or medium, never high"),
    (lambda i, p: i["asset"].update(ticker="NVDA"), "input and projection are for different tickers"),
    (lambda i, p: i["now"].update(time="2026-11-18T10:00-03:00"), "as_of.time must equal the input's now.time"),
])
def test_save_refuses_and_writes_nothing(rz, capsys, input_data, projection_data, write_json, tmp_path, change, message):
    change(input_data, projection_data)
    store = tmp_path / "store"
    code, out = run(rz, capsys, "save", write_json("in.json", input_data), write_json("out.json", projection_data), str(store))
    assert code == 1 and out.startswith("not saved:") and message in out
    assert not store.exists()


def test_list_is_oldest_first_and_marks_ended_horizons(rz, capsys, input_data, projection_data, write_json, tmp_path):
    store = str(tmp_path / "store")
    for when, last in (("2099-01-05T10:00-03:00", "2099-04-03"), ("2020-03-02T10:00-03:00", "2020-05-29")):
        input_data["now"]["time"] = projection_data["as_of"]["time"] = when
        projection_data["horizon"]["last"] = last
        assert run(rz, capsys, "save", write_json("in.json", input_data), write_json("out.json", projection_data), store)[0] == 0
    code, out = run(rz, capsys, "list_saved", store)
    lines = out.splitlines()
    assert code == 0 and len(lines) == 2
    assert lines[0].startswith("2020-03-02T10:00-03:00 PETR4.SA 1-3m 44.06 to 2020-05-29 ended ")
    assert lines[1].startswith("2099-01-05T10:00-03:00 PETR4.SA 1-3m 44.06 to 2099-04-03 open ")
    assert lines[0].endswith(os.path.join("PETR4.SA", "2020-03-02T1000_1-3m"))


def test_list_of_a_missing_folder_prints_nothing(rz, capsys, tmp_path):
    assert run(rz, capsys, "list_saved", str(tmp_path / "none")) == (0, "")


def test_saved_returns_the_parsed_projections(rz, capsys, pair, tmp_path):
    run(rz, capsys, "save", *pair, str(tmp_path))
    (folder, projection), = rz.saved(str(tmp_path))
    assert projection == json.loads(Path(pair[1]).read_text(encoding="utf-8"))
    assert Path(folder, "input.json").is_file()
