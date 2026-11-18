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
