#!/usr/bin/env python3
"""Ranked trade setups from one run: fill the prompt, and check Claude's reply.

    python trade_entries.py prompt INPUT.json PROJECTION.json            -> the filled prompt, on stdout
    python trade_entries.py check  INPUT.json PROJECTION.json REPLY.json -> OK, or what is wrong (exit 1)
    python trade_entries.py expected PROJECTION.json                     -> every valid setup, best first

The prompt is prompts/trade_entries.md and the reply's shape is
prompts/trade_entries.schema.json. Every number in a reply follows from the
projection by the template's rules, so `check` recomputes them here and
compares. The setups are hypothetical, for study: not a recommendation.
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "prompts" / "trade_entries.md"
SCHEMA = ROOT / "prompts" / "trade_entries.schema.json"
TOLERANCE = 0.011        # one hundredth, for a rounding that fell the other way
RANK_TOLERANCE = 0.021   # two setups this close in edge.per_order may swap places


def half_up(value, digits=2):
    return round(value + 1e-9, digits)


def fill(input_text, projection_text):
    """The prompt to send: the template below its opening comment, with both files in place."""
    template = TEMPLATE.read_text(encoding="utf-8")
    template = template[template.index("-->") + 3:].lstrip()
    return (template.replace("{{INPUT_JSON}}", input_text.strip())
                    .replace("{{PROJECTION_JSON}}", projection_text.strip()))


def bounded(zones):
    return [zone for zone in zones if None not in zone["zone"]]


def precondition(source, proj):
    """Why no setup can be built from this pair, or None. Mirrors 'Before you start'."""
    if str(source["asset"]["ticker"]).upper() != str(proj["asset"]["ticker"]).upper():
        return "input and projection are for different tickers"
    if source["now"]["time"] != proj["as_of"]["time"]:
        return "projection.as_of.time differs from input.now.time"
    if not bounded(proj["supports"]) or not bounded(proj["resistances"]):
        return "a side of the projection has no zone with two numeric edges"
    return None


def units(proj):
    price, vol = proj["as_of"]["price"], proj["structure"]["volatility"]["assumed"]
    move = half_up(price * vol[0])
    return {"move": move, "buffer": half_up(move / 4),
            "range": half_up(price * sum(vol) / 2 * math.sqrt(proj["horizon"]["closes"]))}


def travel(distance, reach_of_horizon):
    share = distance / reach_of_horizon
    return 0.9 if share <= 0.5 else 0.7 if share <= 1.0 else 0.5 if share <= 1.5 else 0.3


def setups(proj):
    """Every valid setup the template's rules allow, ranked by edge per order, then fill."""
    price, unit = proj["as_of"]["price"], units(proj)
    buffer, found = unit["buffer"], []

    def add(side, kind, zone_id, order, entry, stop, target, fill_p, win):
        ordered = stop < entry < target if side == "long" else target < entry < stop
        if not ordered:
            return
        risk, reward = half_up(abs(entry - stop)), half_up(abs(target - entry))
        ratio = half_up(reward / risk)
        if ratio < 0.5:
            return
        fill_p, win = half_up(fill_p), half_up(win)
        if_filled = half_up(win * ratio - (1 - win))
        found.append({
            "id": f"{side}-{zone_id}-{kind}", "side": side, "setup": kind, "order": order,
            "entry": entry, "stop": half_up(stop), "target": target,
            "risk": risk, "reward": reward, "ratio": ratio, "fill": fill_p, "win_if_filled": win,
            "if_filled": if_filled, "per_order": half_up(fill_p * if_filled),
            "quality": "favourable" if if_filled >= 0.20 else "marginal" if if_filled > 0 else "unfavourable"})

    for listed, opposite, support in ((proj["supports"], proj["resistances"], True),
                                      (proj["resistances"], proj["supports"], False)):
        zones, first_opposite = bounded(listed), bounded(opposite)[0]["zone"]
        for index, zone in enumerate(zones):
            low, high = zone["zone"]
            near, far = (high, low) if support else (low, high)

            # reversal: the zone holds; long at a support, short at a resistance
            inside = low <= price <= high
            entry, order, fill_p = (price, "market", 1.0) if inside else (near, "limit", zone["reach"])
            if inside or (entry <= price if support else entry >= price):
                target = first_opposite[0] if support else first_opposite[1]
                hold = zone["turn"] / zone["reach"] if zone["reach"] else 0.0
                add("long" if support else "short", "reversal", zone["id"], order, entry,
                    far - buffer if support else far + buffer, target, fill_p,
                    hold * travel(abs(target - entry), unit["range"]))

            # continuation: the zone breaks; not for the last zone on a side
            if index + 1 < len(zones) and index + 2 < len(listed) and (far < price if support else far > price):
                following, after = zones[index + 1], listed[index + 2]
                if following["reach"]:
                    add("short" if support else "long", "continuation", zone["id"], "stop", far,
                        near + buffer if support else near - buffer,
                        following["zone"][0] if support else following["zone"][1],
                        following["reach"], after["reach"] / following["reach"])

    found.sort(key=lambda item: (-item["per_order"], -item["fill"]))
    return found


def pick(found):
    """The three to keep: best first, distinct entry prices, and not all on one side."""
    chosen = []
    for item in found:
        if len(chosen) == 3:
            break
        if all(item["entry"] != kept["entry"] for kept in chosen):
            chosen.append(item)
    if len(chosen) == 3 and len({item["side"] for item in chosen}) == 1:
        other = next((item for item in found if item["side"] != chosen[0]["side"]
                      and all(item["entry"] != kept["entry"] for kept in chosen[:2])), None)
        if other:
            chosen[2] = other
            chosen.sort(key=lambda item: (-item["per_order"], -item["fill"]))
    return chosen


def schema_problems(reply):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["the jsonschema package is missing, so the reply's shape was not checked (run `uv sync`)"]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return [f"{'.'.join(str(part) for part in error.path) or 'reply'}: {error.message}"
            for error in sorted(Draft202012Validator(schema).iter_errors(reply), key=lambda e: list(map(str, e.path)))]


def problems(source, proj, reply):
    """Everything wrong with a reply, as lines. Empty means it is consistent with the projection."""
    reason = precondition(source, proj)
    if isinstance(reply, dict) and set(reply) == {"task", "error"}:
        return [] if reason else [f"the reply reports an error but the files are usable: {reply['error']}"]
    if reason:
        return [f"no setup can be built ({reason}), but the reply holds entries"]
    bad = schema_problems(reply)
    if bad:
        return bad

    if reply["as_of"] != proj["as_of"]:
        bad.append("as_of must equal the projection's as_of")
    if reply["valid_until"] != proj["horizon"]["last"]:
        bad.append(f"valid_until must be {proj['horizon']['last']}, the projection's horizon.last")
    if reply["probability"] != source["want"]["probability"]:
        bad.append(f"probability must be {source['want']['probability']}, the input's want.probability")
    if reply["confidence"]["level"] == "medium" and proj["confidence"]["level"] != "medium":
        bad.append("confidence.level may not be higher than the projection's")
    for name, value in units(proj).items():
        if abs(reply["units"][name] - value) > TOLERANCE:
            bad.append(f"units.{name}: {reply['units'][name]}, expected {value}")

    found = setups(proj)
    by_id, wanted, got = {item["id"]: item for item in found}, pick(found), reply["entries"]
    numbers = (("entry.price", lambda e: e["entry"]["price"], "entry"), ("stop.price", lambda e: e["stop"]["price"], "stop"),
               ("target.price", lambda e: e["target"]["price"], "target"), ("payoff.risk", lambda e: e["payoff"]["risk"], "risk"),
               ("payoff.reward", lambda e: e["payoff"]["reward"], "reward"), ("payoff.ratio", lambda e: e["payoff"]["ratio"], "ratio"),
               ("odds.fill", lambda e: e["odds"]["fill"], "fill"), ("odds.win_if_filled", lambda e: e["odds"]["win_if_filled"], "win_if_filled"),
               ("edge.if_filled", lambda e: e["edge"]["if_filled"], "if_filled"), ("edge.per_order", lambda e: e["edge"]["per_order"], "per_order"))
    for position, entry in enumerate(got, 1):
        name = entry["id"]
        if entry["rank"] != position:
            bad.append(f"{name}: rank {entry['rank']} at position {position}")
        expected = by_id.get(name)
        if expected is None:
            bad.append(f"{name}: not a setup the rules allow for this projection")
            continue
        for label, read, key in numbers:
            if abs(read(entry) - expected[key]) > TOLERANCE:
                bad.append(f"{name}.{label}: {read(entry)}, expected {expected[key]}")
        for label, value, key in (("side", entry["side"], "side"), ("setup", entry["setup"], "setup"),
                                  ("entry.order", entry["entry"]["order"], "order"), ("quality", entry["quality"], "quality")):
            if value != expected[key]:
                bad.append(f"{name}.{label}: {value}, expected {expected[key]}")
    if len(got) != len(wanted):
        bad.append(f"{len(got)} entries, expected {len(wanted)}: {', '.join(item['id'] for item in wanted)}")
    elif [entry["id"] for entry in got] != [item["id"] for item in wanted]:
        # a different pick is accepted only when the edges are too close to tell apart
        close = all(entry["id"] in by_id and abs(by_id[entry["id"]]["per_order"] - item["per_order"]) <= RANK_TOLERANCE
                    for entry, item in zip(got, wanted))
        if not close or len({entry["entry"]["price"] for entry in got}) != len(got):
            bad.append("ranking: got " + ", ".join(entry["id"] for entry in got)
                       + "; expected " + ", ".join(item["id"] for item in wanted))
    return bad


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 3 and argv[0] == "prompt":
            sys.stdout.write(fill(Path(argv[1]).read_text(encoding="utf-8"), Path(argv[2]).read_text(encoding="utf-8")))
            return 0
        if len(argv) == 4 and argv[0] == "check":
            bad = problems(load(argv[1]), load(argv[2]), load(argv[3]))
            print("OK" if not bad else "\n".join(bad))
            return 1 if bad else 0
        if len(argv) == 2 and argv[0] == "expected":
            proj = load(argv[1])
            found = setups(proj)
            keep = {item["id"] for item in pick(found)}
            print(json.dumps(units(proj)))
            for item in found:
                print("*" if item["id"] in keep else " ", json.dumps(item))
            return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
