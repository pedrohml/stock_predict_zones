#!/usr/bin/env python3
"""Ranked trade setups from one run: build them, or fill the prompt and check Claude's reply.

    python trade_entries.py build  INPUT.json PROJECTION.json [-o FILE]  -> the setups as JSON, no Claude needed
    python trade_entries.py prompt INPUT.json PROJECTION.json            -> the filled prompt, on stdout
    python trade_entries.py check  INPUT.json PROJECTION.json REPLY.json -> OK, or what is wrong (exit 1)
    python trade_entries.py expected PROJECTION.json                     -> every valid setup, best first

The prompt is prompts/trade_entries.md and the reply's shape is
prompts/trade_entries.schema.json. Every number in a reply follows from the
projection by the template's rules, so `build` computes them here and `check`
recomputes them and compares. `build` also writes the sentences around the
numbers from fixed wording (TEXT), which is why it needs no language model; the
prompt is for when Claude should write them instead. The setups are
hypothetical, for study: not a recommendation.
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

    def add(side, kind, zone_id, order, entry, stop, target, fill_p, win, detail):
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
            "quality": "favourable" if if_filled >= 0.20 else "marginal" if if_filled > 0 else "unfavourable",
            "detail": dict(detail, zone=zone_id, buffer=buffer)})

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
                distance = abs(target - entry)
                add("long" if support else "short", "reversal", zone["id"], order, entry,
                    far - buffer if support else far + buffer, target, fill_p,
                    hold * travel(distance, unit["range"]),
                    {"edge": far, "target_zone": bounded(opposite)[0]["id"], "reach": zone["reach"],
                     "turn": zone["turn"], "hold": hold, "travel": travel(distance, unit["range"]),
                     "share": distance / unit["range"],
                     "between": [[z["id"], *z["zone"]] for z in zones[:index]]})

            # continuation: the zone breaks; not for the last zone on a side
            if index + 1 < len(zones) and index + 2 < len(listed) and (far < price if support else far > price):
                following, after = zones[index + 1], listed[index + 2]
                if following["reach"]:
                    add("short" if support else "long", "continuation", zone["id"], "stop", far,
                        near + buffer if support else near - buffer,
                        following["zone"][0] if support else following["zone"][1],
                        following["reach"], after["reach"] / following["reach"],
                        {"edge": near, "next": following["id"], "after": after["id"],
                         "reach_next": following["reach"], "reach_after": after["reach"]})

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


# ---- the sentences around the numbers, for `build` ---------------------------------------

TEXT = {
    "en": {
        "near": "{zone} near edge", "far": "{zone} far edge", "price": "current price, inside {zone}",
        "plus": "{zone} {which} edge {edge} + buffer {buffer}", "minus": "{zone} {which} edge {edge} − buffer {buffer}",
        "which": {"near": "near", "far": "far"},
        "basis_reversal": "fill = reach({zone}) {reach}; win = turn({zone}) {turn} / reach({zone}) {reach} × travel {travel} (distance {share} of range)",
        "basis_market": "fill = 1.00, a market order inside {zone}; win = turn({zone}) {turn} / reach({zone}) {reach} × travel {travel} (distance {share} of range)",
        "basis_continuation": "fill = reach({next}) {reach_next}; win = reach({after}) {reach_after} / reach({next}) {reach_next}",
        "why_reversal": "{zone} holds in {hold} of the paths that reach it, and the reward to {target_zone} is {ratio} times the risk.",
        "why_continuation": "{zone} breaks in {fill} of the paths, and once it does, {next} breaks too in {win} of them.",
        "between": "{zone} {low}–{high} lies between entry and target",
        "dividend": {"long": "{date} {what}: the drop works against a long and can jump the stop",
                     "short": "{date} {what}: a short pays the dividend, and the drop can fill the order on a gap"},
        "event": "{date} {what}: a price gap can jump the stop",
        "ceiling": "win_if_filled is a ceiling: the projection does not know whether the stop is touched before the target",
        "closes": "probabilities are for {basis} closes; stops and targets trigger inside the candle",
        "touches": "probabilities count a touch inside a {basis} candle, in any order within it",
        "uncalibrated": "the projection's probabilities are not backtested, and its own confidence is {level}",
        "fewer": "only {n} setup(s) pass the rules for this projection",
        "method": "Setups built from zone edges only; fill and win taken from the projection's reach and turn; ranked by edge per order. {valid} setups were valid, {kept} kept. Written by trade_entries.py, without a language model.",
        "note": "Hypothetical setups for study, derived mechanically from uncalibrated probabilities. Not a recommendation to buy or sell.",
    },
    "pt": {
        "near": "borda próxima de {zone}", "far": "borda distante de {zone}", "price": "preço atual, dentro de {zone}",
        "plus": "borda {which} de {zone} {edge} + margem {buffer}", "minus": "borda {which} de {zone} {edge} − margem {buffer}",
        "which": {"near": "próxima", "far": "distante"},
        "basis_reversal": "fill = reach({zone}) {reach}; win = turn({zone}) {turn} / reach({zone}) {reach} × travel {travel} (distância {share} do range)",
        "basis_market": "fill = 1.00, ordem a mercado dentro de {zone}; win = turn({zone}) {turn} / reach({zone}) {reach} × travel {travel} (distância {share} do range)",
        "basis_continuation": "fill = reach({next}) {reach_next}; win = reach({after}) {reach_after} / reach({next}) {reach_next}",
        "why_reversal": "{zone} segura em {hold} dos caminhos que chegam a ela, e o ganho até {target_zone} é {ratio} vezes o risco.",
        "why_continuation": "{zone} rompe em {fill} dos caminhos e, quando rompe, {next} também rompe em {win} deles.",
        "between": "{zone} {low}–{high} fica entre a entrada e o alvo",
        "dividend": {"long": "{date} {what}: a queda joga contra uma posição comprada e pode pular o stop",
                     "short": "{date} {what}: a posição vendida paga o dividendo, e a queda pode executar a ordem num gap"},
        "event": "{date} {what}: um gap de preço pode pular o stop",
        "ceiling": "win_if_filled é um teto: a projeção não sabe se o stop é tocado antes do alvo",
        "closes": "as probabilidades valem para fechamentos de {basis}; stops e alvos disparam dentro do candle",
        "touches": "as probabilidades contam um toque dentro de um candle de {basis}, em qualquer ordem",
        "uncalibrated": "as probabilidades da projeção não foram testadas contra o passado, e a confiança dela é {level}",
        "fewer": "apenas {n} setup(s) passam nas regras para esta projeção",
        "method": "Setups montados só com bordas de zonas; fill e win vêm do reach e do turn da projeção; ordenados pelo edge por ordem. {valid} setups válidos, {kept} mantidos. Escrito por trade_entries.py, sem modelo de linguagem.",
        "note": "Setups hipotéticos para estudo, derivados mecanicamente de probabilidades não calibradas. Não é recomendação de compra ou venda.",
    },
}
FALLBACK_LANG = "en"


def language(source):
    """The TEXT entry for the input's reply.language (primary subtag), or English."""
    reply = source.get("reply") if isinstance(source, dict) else None
    asked = reply.get("language") if isinstance(reply, dict) else None
    code = asked.strip().lower().replace("_", "-").split("-")[0] if isinstance(asked, str) else ""
    return TEXT.get(code, TEXT[FALLBACK_LANG])


def money(value):
    return f"{value:.2f}"


def share(value):
    return f"{round(value * 100):d}%"


def entry_of(rank, item, proj, t):
    """One setup in the reply's shape, with its sentences."""
    d, long, zone = item["detail"], item["side"] == "long", item["detail"]["zone"]
    away = t["minus"] if long else t["plus"]          # the stop sits beyond an edge, away from the entry
    if item["setup"] == "reversal":
        market = item["order"] == "market"
        entry_anchor = t["price" if market else "near"].format(zone=zone)
        stop_anchor = away.format(zone=zone, which=t["which"]["far"], edge=money(d["edge"]), buffer=money(d["buffer"]))
        target_anchor = t["near"].format(zone=d["target_zone"])
        basis = t["basis_market" if market else "basis_reversal"].format(
            zone=zone, reach=money(d["reach"]), turn=money(d["turn"]), travel=d["travel"], share=money(d["share"]))
        why = t["why_reversal"].format(zone=zone, hold=share(d["hold"]), target_zone=d["target_zone"],
                                       ratio=money(item["ratio"]))
        watch = [t["between"].format(zone=name, low=money(low), high=money(high)) for name, low, high in d["between"]]
    else:
        entry_anchor = t["far"].format(zone=zone)
        stop_anchor = away.format(zone=zone, which=t["which"]["near"], edge=money(d["edge"]), buffer=money(d["buffer"]))
        target_anchor = t["far"].format(zone=d["next"])
        basis = t["basis_continuation"].format(next=d["next"], after=d["after"],
                                               reach_next=money(d["reach_next"]), reach_after=money(d["reach_after"]))
        why = t["why_continuation"].format(zone=zone, fill=share(item["fill"]), next=d["next"],
                                           win=share(item["win_if_filled"]))
        watch = []
    for event in proj.get("events") or []:
        date = event.get("date")
        if date and proj["as_of"]["time"][:10] <= date[:10] <= proj["horizon"]["last"]:
            what = str(event.get("what", "event"))
            line = t["dividend"][item["side"]] if "dividend" in what.lower() else t["event"]
            watch.append(line.format(date=date[:10], what=what))
    return {
        "rank": rank, "id": item["id"], "side": item["side"], "setup": item["setup"],
        "entry": {"order": item["order"], "price": item["entry"], "anchor": entry_anchor},
        "stop": {"price": item["stop"], "anchor": stop_anchor},
        "target": {"price": item["target"], "anchor": target_anchor},
        "payoff": {"risk": item["risk"], "reward": item["reward"], "ratio": item["ratio"]},
        "odds": {"fill": item["fill"], "win_if_filled": item["win_if_filled"], "basis": basis},
        "edge": {"if_filled": item["if_filled"], "per_order": item["per_order"]},
        "quality": item["quality"], "why": why, "watch": watch}


def build(source, proj):
    """The whole reply, computed: what the prompt asks Claude for, with fixed wording."""
    reason = precondition(source, proj)
    if reason:
        return {"task": "trade_entries", "error": reason}
    t, found = language(source), setups(proj)
    kept, measure = pick(found), source["want"]["probability"]
    if not kept:
        return {"task": "trade_entries", "error": "no setup passes the rules for this projection"}
    level = proj["confidence"]["level"]
    limits = [t["ceiling"],
              t["closes" if measure == "close_in_zone" else "touches"].format(basis=proj["horizon"]["close_basis"]),
              t["uncalibrated"].format(level=level)]
    if len(kept) < 3:
        limits.append(t["fewer"].format(n=len(kept)))
    return {
        "task": "trade_entries",
        "asset": {"ticker": proj["asset"]["ticker"], "currency": proj["asset"]["currency"]},
        "as_of": proj["as_of"], "valid_until": proj["horizon"]["last"], "probability": measure,
        "units": units(proj),
        "entries": [entry_of(rank, item, proj, t) for rank, item in enumerate(kept, 1)],
        "confidence": {"level": level, "limits": limits},
        "method": t["method"].format(valid=len(found), kept=len(kept)),
        "note": t["note"]}


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
        if argv and argv[0] == "build" and (len(argv) == 3 or (len(argv) == 5 and argv[3] == "-o")):
            text = json.dumps(build(load(argv[1]), load(argv[2])), indent=2, ensure_ascii=False) + "\n"
            if len(argv) == 5:
                Path(argv[4]).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            return 0
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
                print("*" if item["id"] in keep else " ", json.dumps({k: v for k, v in item.items() if k != "detail"}))
            return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
