"""Shared fixtures. Everything here is offline: no test may touch the network."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RZ_PATH = ROOT / ".claude" / "skills" / "reversal-zones-projection" / "scripts" / "rz.py"

# The zones of tests/fixtures/projection.json, in the format `rz.py sim` reads.
ZONES = {
    "supports": [[43.2, 44.6], [40.6, 42.2], [37.4, 38.6]],
    "resistances": [[45.3, 47.1], [47.1, 49.6], [50.2, 52.7]],
}


@pytest.fixture(scope="session")
def rz():
    """The skill's helper script, which is not on the import path."""
    spec = importlib.util.spec_from_file_location("rz", RZ_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def input_path():
    return FIXTURES / "input.json"


@pytest.fixture(scope="session")
def projection_path():
    return FIXTURES / "projection.json"


@pytest.fixture
def input_data(input_path):
    return json.loads(input_path.read_text(encoding="utf-8"))


@pytest.fixture
def projection_data(projection_path):
    return json.loads(projection_path.read_text(encoding="utf-8"))


@pytest.fixture
def zones():
    return copy.deepcopy(ZONES)


@pytest.fixture
def write_json(tmp_path):
    """write_json(name, data) -> path of a JSON file in tmp_path."""
    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)
    return write


@pytest.fixture
def printed(capsys):
    """printed() -> the `key <json>` lines rz.py printed since the last call, as a dict."""
    def parse():
        out = {}
        for line in capsys.readouterr().out.splitlines():
            key, _, value = line.partition(" ")
            out[key] = json.loads(value)
        return out
    return parse


@pytest.fixture
def run_sim(rz, write_json, printed, input_path):
    """run_sim(zones, source=None, paths=...) -> parsed output of rz.sim."""
    def run(zones, source=None, **kwargs):
        source_path = str(input_path) if source is None else write_json("sim_input.json", source)
        printed()  # drop anything printed before
        rz.sim(source_path, write_json("zones.json", zones), **kwargs)
        return printed()
    return run


@pytest.fixture
def projection_from_sim(projection_data):
    """Turn a parsed `rz.sim` output into a projection, the way SKILL.md says
    to when nothing is adjusted: probabilities and horizon copied verbatim."""
    def make(sim_out, zones):
        proj = copy.deepcopy(projection_data)
        proj["horizon"] = sim_out["horizon"]
        for name in ("supports", "resistances"):
            support = name == "supports"
            real = zones[name]
            edge = real[-1][0] if support else real[-1][1]
            boxes = [list(z) for z in real] + [[None, edge] if support else [edge, None]]
            proj[name] = [{"id": entry["id"], "zone": box, "reach": entry["reach"],
                           "turn": entry["turn"], "evidence": ["synthetic"]}
                          for entry, box in zip(sim_out[name], boxes)]
        proj["scenarios"] = sim_out["scenarios"]
        return proj
    return make


def entries_reply(source, proj):
    """A reply to prompts/trade_entries.md that follows its rules, built without Claude:
    the numbers come from trade_entries.py, the sentences are placeholders."""
    import trade_entries
    reason = trade_entries.precondition(source, proj)
    if reason:
        return {"task": "trade_entries", "error": reason}
    entries = []
    for rank, item in enumerate(trade_entries.pick(trade_entries.setups(source, proj)), 1):
        entries.append({
            "rank": rank, "id": item["id"], "side": item["side"], "setup": item["setup"],
            "entry": {"order": item["order"], "price": item["entry"], "anchor": "zone edge"},
            "stop": {"price": item["stop"], "anchor": "zone edge and buffer"},
            "target": {"price": item["target"], "anchor": "zone edge"},
            "payoff": {"risk": item["risk"], "reward": item["reward"], "ratio": item["ratio"]},
            "odds": {"fill": item["fill"], "win_if_filled": item["win_if_filled"], "basis": "simulated paths"},
            "edge": {"if_filled": item["if_filled"], "per_order": item["per_order"]},
            "quality": item["quality"], "why": "placeholder", "watch": []})
    return {
        "task": "trade_entries", "rules": trade_entries.RULES, "asset": {"ticker": proj["asset"]["ticker"], "currency": proj["asset"]["currency"]},
        "as_of": proj["as_of"], "valid_until": proj["horizon"]["last"],
        "probability": source["want"]["probability"], "units": trade_entries.units(proj, trade_entries.places(source)), "entries": entries,
        "confidence": {"level": "low", "limits": ["the odds are counted on simulated paths"]},
        "method": "placeholder", "note": "Hypothetical setups for study. Not a recommendation to buy or sell."}


@pytest.fixture
def entries_data(input_data, projection_data):
    return entries_reply(input_data, projection_data)


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    """No test may start the real Claude Code: a run costs money and minutes. A test that
    forgets to stub a Claude step fails at once with "not found" instead."""
    import run_pipeline
    import value_test
    monkeypatch.setattr(run_pipeline, "CLAUDE", "claude-must-not-run-in-tests")

    def refuse(*args, **kwargs):   # value_test.py starts step 2 as a subprocess, out of reach of CLAUDE
        raise AssertionError("value_test.launch must be stubbed in tests")
    monkeypatch.setattr(value_test, "launch", refuse)


@pytest.fixture(autouse=True)
def no_real_cache(monkeypatch, tmp_path_factory):
    """No test may read or write the developer's candle cache (~/.cache/zones) or wait on
    the shared Yahoo rate limit: each test gets an empty cache folder of its own (child
    processes inherit the variable), and no spacing between requests. A test about the
    rate limit sets ZONES_YAHOO_SPACING itself. Retries never really wait in this process
    (data_source.sleep is a no-op; a test that counts the waits replaces it again)."""
    import data_source
    monkeypatch.setenv(data_source.CACHE_ENV, str(tmp_path_factory.mktemp("candle-cache")))
    monkeypatch.setenv(data_source.SPACING_ENV, "0")
    monkeypatch.setattr(data_source, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def no_real_config(monkeypatch, tmp_path_factory):
    """No test may read the developer's own defaults (run_pipeline.py's config files), or
    the suite passes on one machine only. The user file is a path that does not exist and
    ./zones.toml is not looked up; a test about config files sets ZONES_CONFIG, or
    run_pipeline.PROJECT_CONFIG, itself. A `history_dir` exports RZ_PROJECTIONS_DIR for the
    whole process, so the variable is put back after every test."""
    import os
    import run_pipeline
    monkeypatch.setenv(run_pipeline.CONFIG_ENV,
                       str(tmp_path_factory.getbasetemp() / "no-config" / "config.toml"))
    monkeypatch.setattr(run_pipeline, "PROJECT_CONFIG", None)
    kept = os.environ.get(run_pipeline.HISTORY_ENV)
    yield
    if kept is None:
        os.environ.pop(run_pipeline.HISTORY_ENV, None)
    else:
        os.environ[run_pipeline.HISTORY_ENV] = kept
