#!/usr/bin/env python3
"""Step 2 without Claude: a complete projection from an input JSON, by fixed rules.

    python mechanical.py INPUT.json [-o PROJECTION.json]     -> the projection (stdout without -o)

`zones TICKER --engine mechanical` runs this instead of the skill. Every part follows
from the input by code, so the same input gives the same file, byte for byte:

- zones: backtest.py's zone rule (clustered_zones(), the one place it lives) on the facts
  of `rz.py levels` (rz.facts()), with the input's want.supports / want.resistances;
- probabilities: `rz.py sim` (rz.baseline(), the default sim method), copied unadjusted,
  as backtest.projection() copies them;
- evidence: the pivots, last-three-candle extremes, retracements and swing ends that formed
  each zone, with their prices and candles; strength by the rule in strength();
- structure from `levels`, horizon and scenarios from `sim`, the input's events passed
  through (not simulated), no drivers, confidence `low`, and a `method` naming all this.

Free text follows the input's reply.language (TEXT: en and pt; anything else gets English),
as trade_entries.py does. The output has SKILL.md's schema and passes `rz.py check`. It
makes no web search, so a `research` other than `none` is not honoured, and the projection
says so in confidence.limits. Analysis for study, not advice: no buy, sell or hold calls.
"""
import json
import sys
from pathlib import Path

import backtest
import trade_entries

rz = backtest.rz
ENGINE = "mechanical"
LEGEND_KEYS = ("zone", "reach", "turn", "hold")
KIND_ORDER = ("pivot_low", "pivot_high", "recent_low", "recent_high", "retracement", "swing_low", "swing_high")
MIN_ROWS = 52   # fewer rows in the main series is a limit, as SKILL.md says

TEXT = {
    "en": {
        "legend": {
            "zone": "[from, to]; null means open-ended",
            "reach": "P(the zone comes into play: price {closes} into it, or beyond the zone before it)",
            "turn": "P(the horizon's most extreme {close} is made here: reached and not broken)",
            "hold": "turn / reach"},
        "closes": "closes", "close": "close", "trades": "trades", "trade": "trade",
        "when": {"1W": "week of {d}", "1M": "month of {m}", "other": "{d}"},
        "kinds": {
            "pivot_low": ("pivot low {list}", "pivot lows {list}"),
            "pivot_high": ("pivot high {list}", "pivot highs {list}"),
            "recent_low": ("low of the last three candles {list}", "lows of the last three candles {list}"),
            "recent_high": ("high of the last three candles {list}", "highs of the last three candles {list}"),
            "retracement": ("{pcts} retracement {list} of the swing {swing}",
                            "{pcts} retracements {list} of the swing {swing}"),
            "swing_low": ("low of the dominant swing {list}", "low of the dominant swing {list}"),
            "swing_high": ("high of the dominant swing {list}", "high of the dominant swing {list}")},
        "one": {"pivot_low": "pivot low", "pivot_high": "pivot high",
                "recent_low": "a low of the last three candles", "recent_high": "a high of the last three candles",
                "retracement": "the {pct} retracement", "swing_low": "the low of the dominant swing",
                "swing_high": "the high of the dominant swing"},
        "old_floor": ", an old floor now above the price", "old_ceiling": ", an old ceiling now below the price",
        "and": " and ", "swing": "{low} to {high}",
        "filled": "no fact from the rows: placed one zone width ({width}) beyond {previous}, because the "
                  "rule found fewer clusters than requested",
        "price": "the price",
        "next": "next reference {price}, {what}",
        "nothing": "no reference level in the rows beyond {edge}",
        "events": {"earnings": "earnings", "ex-dividend": "ex-dividend"},
        "effect": {"earnings": "a result can gap the price; its size is not in the input, so it is not simulated",
                   "ex-dividend": "price drops by the payout on the ex-date; the amount is not in the input, "
                                  "so the drop is not simulated",
                   "other": "not simulated: its size is not in the input"},
        "scenario": {"below": "below {x}", "above": "above {x}", "higher": "{x} or higher", "lower": "{x} or lower"},
        "limits": {
            "near": "{id}'s far edge {edge} is {gap} from the price, within one candle's assumed move of {move}",
            "mechanical": "zones and probabilities are mechanical: a fixed zone rule and the unadjusted "
                          "simulation, with no judgement of the chart and no news",
            "event": "{date} {what} is inside the horizon but not in the simulation (its size is not in the input)",
            "filled": "{ids}: the rows gave fewer clusters than requested, so {these} one zone width apart "
                      "with no fact from the rows behind {them}",
            "fewer": "{side}: {n} zone(s) instead of the {wanted} requested; the next one would fall below zero",
            "supports": "supports", "resistances": "resistances",
            "these": (("it is placed", "it"), ("they are placed", "them")),
            "research": "the input asked for research ({research}); the mechanical engine does none, so there "
                        "are no drivers and the events are only the input's",
            "short": "only {n} candles in the main series, fewer than {min}"},
        "method": "Mechanical engine, no language model. Zones: backtest.py's fixed rule (clusters of pivots, "
                  "last-three-candle extremes, retracements and swing ends lying within one zone width, "
                  "{width}, of each other; the nearest {supports} below and {resistances} above the price). "
                  "Probabilities: rz.py sim method {method}, copied unadjusted: {paths} simulated paths of "
                  "{closes} {basis} closes (fat-tailed, {drift}, volatility {start} per candle decaying to "
                  "{end}{forming}, no scheduled shocks). Strength: strong for three or more kinds of evidence "
                  "or three tested candles, moderate for two, weak for one or none.",
        "drift": "{rate} a year drift", "no_drift": "no drift", "thousands": ",", "decimal": ".",
        "forming": ", first step shortened for the forming candle",
        "basis": {"15m": "15-minute", "1h": "hourly", "1D": "daily", "1W": "weekly", "1M": "monthly"},
    },
    "pt": {
        "legend": {
            "zone": "[de, até]; null significa sem limite",
            "reach": "P(a zona entra em jogo: o preço {closes} dentro dela, ou além da zona anterior)",
            "turn": "P(o {close} mais extremo do horizonte ocorre aqui: alcançada e não rompida)",
            "hold": "turn / reach"},
        "closes": "fecha", "close": "fechamento", "trades": "negocia", "trade": "negócio",
        "when": {"1W": "semana de {d}", "1M": "mês {m}", "other": "{d}"},
        "kinds": {
            "pivot_low": ("pivô de baixa {list}", "pivôs de baixa {list}"),
            "pivot_high": ("pivô de alta {list}", "pivôs de alta {list}"),
            "recent_low": ("mínima dos três últimos candles {list}", "mínimas dos três últimos candles {list}"),
            "recent_high": ("máxima dos três últimos candles {list}", "máximas dos três últimos candles {list}"),
            "retracement": ("retração de {pcts} {list} da perna {swing}",
                            "retrações de {pcts} {list} da perna {swing}"),
            "swing_low": ("mínima da perna dominante {list}", "mínima da perna dominante {list}"),
            "swing_high": ("máxima da perna dominante {list}", "máxima da perna dominante {list}")},
        "one": {"pivot_low": "pivô de baixa", "pivot_high": "pivô de alta",
                "recent_low": "uma mínima dos três últimos candles", "recent_high": "uma máxima dos três últimos candles",
                "retracement": "a retração de {pct}", "swing_low": "a mínima da perna dominante",
                "swing_high": "a máxima da perna dominante"},
        "old_floor": ", antigo piso agora acima do preço", "old_ceiling": ", antigo teto agora abaixo do preço",
        "and": " e ", "swing": "{low} a {high}",
        "filled": "nenhum fato dos candles: posta uma largura de zona ({width}) além de {previous}, porque a "
                  "regra achou menos agrupamentos do que o pedido",
        "price": "o preço",
        "next": "próxima referência {price}, {what}",
        "nothing": "nenhum nível de referência nos candles além de {edge}",
        "events": {"earnings": "resultado trimestral", "ex-dividend": "data ex-dividendo"},
        "effect": {"earnings": "o resultado pode abrir um gap no preço; o tamanho não está na entrada, então não é simulado",
                   "ex-dividend": "o preço cai o valor do provento na data ex; o valor não está na entrada, "
                                  "então a queda não é simulada",
                   "other": "não simulado: o tamanho não está na entrada"},
        "scenario": {"below": "abaixo de {x}", "above": "acima de {x}", "higher": "{x} ou mais", "lower": "{x} ou menos"},
        "limits": {
            "near": "a borda distante de {id}, {edge}, está a {gap} do preço, dentro do movimento suposto de um "
                    "candle, {move}",
            "mechanical": "zonas e probabilidades são mecânicas: uma regra fixa de zonas e a simulação sem ajuste, "
                          "sem julgamento do gráfico e sem notícias",
            "event": "{date} {what} cai dentro do horizonte mas não está na simulação (o tamanho não está na entrada)",
            "filled": "{ids}: os candles deram menos agrupamentos do que o pedido, então {these} a uma largura "
                      "de zona de distância, sem fato dos candles por trás de {them}",
            "fewer": "{side}: {n} zona(s) em vez das {wanted} pedidas; a próxima ficaria abaixo de zero",
            "supports": "suportes", "resistances": "resistências",
            "these": (("ela foi posta", "ela"), ("elas foram postas", "elas")),
            "research": "a entrada pediu pesquisa ({research}); o motor mecânico não pesquisa, então não há "
                        "drivers e os eventos são só os da entrada",
            "short": "apenas {n} candles na série principal, menos que {min}"},
        "method": "Motor mecânico, sem modelo de linguagem. Zonas: a regra fixa de backtest.py (agrupamentos de "
                  "pivôs, extremos dos três últimos candles, retrações e pontas da perna a até uma largura de "
                  "zona, {width}, uns dos outros; os {supports} mais próximos abaixo e os {resistances} acima do "
                  "preço). Probabilidades: rz.py sim método {method}, copiadas sem ajuste: {paths} caminhos "
                  "simulados de {closes} fechamentos {basis} (caudas gordas, {drift}, volatilidade de {start} "
                  "por candle caindo para {end}{forming}, sem choques agendados). Força: strong para três ou "
                  "mais tipos de evidência ou três candles testados, moderate para dois, weak para um ou nenhum.",
        "drift": "deriva de {rate} ao ano", "no_drift": "sem deriva", "thousands": ".", "decimal": ",",
        "forming": ", primeiro passo encurtado pelo candle em formação",
        "basis": {"15m": "de 15 minutos", "1h": "horários", "1D": "diários", "1W": "semanais", "1M": "mensais"},
    },
}
FALLBACK_LANG = "en"


class MechanicalError(Exception):
    """An input this engine cannot project; the message is the reason."""


def language(d):
    """The TEXT entry for the input's reply.language (primary subtag), or English:
    trade_entries.language() on this file's own TEXT."""
    reply = d.get("reply") if isinstance(d, dict) else None
    asked = reply.get("language") if isinstance(reply, dict) else None
    code = asked.strip().lower().replace("_", "-").split("-")[0] if isinstance(asked, str) else ""
    return TEXT.get(code, TEXT[FALLBACK_LANG])


# ---- small formatters ------------------------------------------------------------

def price_text(value, digits, t):
    """A price with the input's decimals and the language's decimal mark (free text only:
    the scenario bounds keep sim's dot, which the chart and the summary read)."""
    return f"{float(value):.{digits}f}".replace(".", t["decimal"])


def percent(value, t, places=1):
    text = f"{value * 100:.{places}f}".rstrip("0").rstrip(".")
    return f"{text}%".replace(".", t["decimal"])


def when_text(start, basis, t):
    """A candle's start as the evidence names it: the week or month for weekly and monthly
    candles (their rows start on the Monday or the 1st), the start itself otherwise."""
    start = str(start)
    if basis == "1W":
        return t["when"]["1W"].format(d=start[:10])
    if basis == "1M":
        return t["when"]["1M"].format(m=start[:7])
    return t["when"]["other"].format(d=start)


def joined(items, t):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + t["and"] + items[-1]


# ---- evidence and strength ---------------------------------------------------------

def swing_text(facts, digits, t):
    """'27.90 to 59.56': the swing's dates are in `structure.swing`, and in the evidence of the
    zone that holds an end of it."""
    swing = facts["swing"]
    return t["swing"].format(low=price_text(swing["low"][0], digits, t), high=price_text(swing["high"][0], digits, t))


def evidence(members, facts, is_support, digits, t):
    """One string per kind of fact in a zone's cluster, kinds in KIND_ORDER, candles in time
    order: "pivot lows 40.65 (week of 2026-06-08) and 40.83 (week of 2026-08-03)"."""
    out = []
    for kind in KIND_ORDER:
        mine = [m for m in members if m[1] == kind]
        if not mine:
            continue
        one, many = t["kinds"][kind]
        if kind == "retracement":
            mine.sort(key=lambda m: m[2])
            text = (one if len(mine) == 1 else many).format(
                pcts=joined([percent(m[2], t) for m in mine], t),
                list=joined([price_text(m[0], digits, t) for m in mine], t), swing=swing_text(facts, digits, t))
        else:
            mine.sort(key=lambda m: str(m[2]))
            listed = joined([f"{price_text(m[0], digits, t)} ({when_text(m[2], facts['candles'], t)})" for m in mine], t)
            text = (one if len(mine) == 1 else many).format(list=listed)
            if kind == "pivot_low" and not is_support:
                text += t["old_floor"]
            elif kind == "pivot_high" and is_support:
                text += t["old_ceiling"]
        out.append(text)
    return out


def strength(members):
    """strong: three or more kinds of evidence (pivots, last-three-candle extremes, retracements,
    swing ends) or three or more tested candles (distinct candles among the pivots and the
    last three candles); moderate: two of either; weak: one, or none (a zone that only fills
    a side). SKILL.md's rule, made countable."""
    group = {"pivot_low": "pivot", "pivot_high": "pivot", "recent_low": "recent", "recent_high": "recent",
             "retracement": "retracement", "swing_low": "swing", "swing_high": "swing"}
    kinds = len({group[m[1]] for m in members})
    tests = len({str(m[2]) for m in members if group[m[1]] in ("pivot", "recent")})
    best = max(kinds, tests)
    return "strong" if best >= 3 else "moderate" if best == 2 else "weak"


def describe(candidate, facts, digits, t):
    """One candidate as the tail's next reference: 'pivot low (week of 2025-06-09)'."""
    _, kind, when = candidate
    if kind == "retracement":
        return t["one"]["retracement"].format(pct=percent(when, t))
    return f"{t['one'][kind]} ({when_text(when, facts['candles'], t)})"


def tail_evidence(facts, last_edge, is_support, digits, t):
    beyond = [c for c in backtest.candidates(facts) if (c[0] < last_edge if is_support else c[0] > last_edge)]
    if not beyond:
        return [t["nothing"].format(edge=price_text(last_edge, digits, t))]
    nearest = max(beyond, key=lambda c: c[0]) if is_support else min(beyond, key=lambda c: c[0])
    return [t["next"].format(price=price_text(nearest[0], digits, t), what=describe(nearest, facts, digits, t))]


# ---- the projection ------------------------------------------------------------------

def entries(chosen, sim_side, facts, is_support, digits, t):
    """The zone entries of one side, nearest first, then the tail, in SKILL.md's key order.
    Probabilities are sim's, verbatim; the boxes and the tail are backtest.projection()'s."""
    prefix = "S" if is_support else "R"
    out = []
    for i, (zone, sim) in enumerate(zip(chosen, sim_side)):
        members = zone["members"]
        if members:
            proof = evidence(members, facts, is_support, digits, t)
        else:
            previous = f"{prefix}{i}" if i else t["price"]
            proof = [t["filled"].format(width=price_text(backtest.zone_width(facts, digits), digits, t),
                                        previous=previous)]
        out.append({"id": sim["id"], "zone": list(zone["zone"]), "strength": strength(members),
                    "reach": sim["reach"], "turn": sim["turn"], "evidence": proof})
    tail = sim_side[len(chosen)]
    last = chosen[-1]["zone"]
    edge = last[0] if is_support else last[1]
    out.append({"id": tail["id"], "zone": [None, edge] if is_support else [edge, None],
                "reach": tail["reach"], "turn": tail["turn"],
                "evidence": tail_evidence(facts, edge, is_support, digits, t)})
    return out


def scenario_text(text, t):
    """sim's scenario bounds ("below 40.6", "40.6 or higher") in the reply's language. The number
    stays, which is what the chart and the summary read."""
    words = text.split()
    if len(words) == 2 and words[0] in ("below", "above"):
        return t["scenario"][words[0]].format(x=words[1])
    if len(words) == 3 and words[1] == "or" and words[2] in ("higher", "lower"):
        return t["scenario"][words[2]].format(x=words[0])
    return text


def in_horizon(event, first_day, last_day):
    day = str(event.get("date", ""))[:10]
    return bool(day) and first_day <= day <= last_day


def events_of(d, horizon, t):
    """The input's events inside the horizon, as the schema writes them. Not simulated: the
    input carries no sizes. certainty `medium`: a data feed's or the user's own date."""
    first_day, last = d["now"]["time"][:10], horizon["last"]
    out = []
    for event in d.get("events") or []:
        if not isinstance(event, dict) or not in_horizon(event, first_day, last):
            continue
        what = str(event.get("what") or "event")
        key = what if what in t["events"] else "other"
        out.append({"date": str(event["date"])[:10], "what": t["events"].get(what, what),
                    "effect": t["effect"][key], "certainty": "medium"})
    return out


def limits(d, facts, chosen, proj, digits, t):
    """confidence.limits, the one that most threatens the nearest zone first (SKILL.md)."""
    L, price = t["limits"], float(d["now"]["price"])
    out = []
    move = proj["structure"]["volatility"]["assumed"][0]
    near = []
    for name in ("supports", "resistances"):
        first = proj[name][0]
        if None in first["zone"]:
            continue
        edge = first["zone"][0] if name == "supports" else first["zone"][1]
        gap = abs(price - edge) / price
        if gap < move:
            near.append((gap, first["id"], edge))
    for gap, zone_id, edge in sorted(near)[:1]:
        out.append(L["near"].format(id=zone_id, edge=price_text(edge, digits, t), gap=percent(gap, t), move=percent(move, t)))
    out.append(L["mechanical"])
    for event in proj["events"]:
        out.append(L["event"].format(date=event["date"], what=event["what"]))
    filled = [proj[name][i]["id"] for name in ("supports", "resistances")
              for i, zone in enumerate(chosen[name]) if not zone["members"]]
    if filled:
        these, them = L["these"][len(filled) > 1]
        out.append(L["filled"].format(ids=", ".join(filled), these=these, them=them))
    for name, wanted in (("supports", d["want"]["supports"]), ("resistances", d["want"]["resistances"])):
        if len(chosen[name]) < wanted:
            out.append(L["fewer"].format(side=L[name], n=len(chosen[name]), wanted=wanted))
    research = d.get("research", "none")
    if research != "none":
        out.append(L["research"].format(research=research))
    if facts["rows"] < MIN_ROWS:
        out.append(L["short"].format(n=facts["rows"], min=MIN_ROWS))
    return out


def method_text(d, facts, out, digits, t):
    vol = out["volatility"]["assumed"]
    drift = out["sim_method"]["drift_per_year"] if out["sim_method"]["drift_per_candle"] else 0
    return t["method"].format(
        width=price_text(backtest.zone_width(facts, digits), digits, t),
        supports=d["want"]["supports"], resistances=d["want"]["resistances"],
        method=out["sim_method"]["version"], paths=f"{backtest.DEFAULT_PATHS:,}".replace(",", t["thousands"]),
        closes=out["horizon"]["closes"], basis=t["basis"].get(out["horizon"]["close_basis"], out["horizon"]["close_basis"]),
        drift=t["drift"].format(rate=percent(drift, t)) if drift else t["no_drift"],
        start=percent(vol[0], t), end=percent(vol[1], t),
        forming=t["forming"] if out["first_candle_share_left"] < 1 else "")


def build(d, method=None):
    """The projection of input `d` (a dict), in SKILL.md's schema and key order. `method` is the
    sim method (rz.SIM_METHODS; None for rz's default, which is what a live run uses)."""
    t = language(d)
    digits = trade_entries.places(d)
    loaded = rz.unpack(d)
    facts = rz.facts(*loaded)
    chosen = backtest.clustered_zones(facts, d["want"]["supports"], d["want"]["resistances"], digits)
    if not chosen["supports"] or not chosen["resistances"]:
        raise MechanicalError("the zone rule found no zone on one side of the price")
    zones = {name: [z["zone"] for z in side] for name, side in chosen.items()}
    out = rz.baseline(loaded, zones, method=method)
    core = backtest.projection(d, zones, out)   # asset, as_of, horizon, scenarios, boxes: verbatim from sim
    touched = d["want"]["probability"] == "touched_once"
    swing = facts["swing"]
    proj = {
        "task": "reversal_zones",
        "schema_version": 1,
        "asset": {"ticker": d["asset"]["ticker"], "currency": d["asset"].get("currency", "unsure")},
        "as_of": core["as_of"],
        "horizon": {key: core["horizon"][key] for key in ("label", "close_basis", "closes", "first", "last")},
        "structure": {
            "trend": facts["trend"],
            "swing": {"low": [round(float(swing["low"][0]), digits), swing["low"][1]],
                      "high": [round(float(swing["high"][0]), digits), swing["high"][1]]},
            "retraced": facts["retraced"], "from_high": facts["from_high"],
            "volatility": {"per": facts["candles"], **facts["volatility"]}},
        "legend": {key: t["legend"][key].format(closes=t["trades" if touched else "closes"],
                                                close=t["trade" if touched else "close"]) for key in LEGEND_KEYS},
        "supports": entries(chosen["supports"], out["supports"], facts, True, digits, t),
        "resistances": entries(chosen["resistances"], out["resistances"], facts, False, digits, t),
        "scenarios": [{"name": s["name"], "p": s["p"], "low": scenario_text(s["low"], t),
                       "high": scenario_text(s["high"], t)} for s in core["scenarios"]],
    }
    proj["events"] = events_of(d, proj["horizon"], t)
    proj["drivers"] = []
    proj["confidence"] = {"level": "low", "limits": limits(d, facts, chosen, proj, digits, t)}
    proj["method"] = method_text(d, facts, out, digits, t)
    bad = rz.problems(proj)
    if bad:   # the rule and the schema disagree: never hand on a projection check would refuse
        raise MechanicalError("the mechanical projection fails rz.py check: " + "; ".join(bad))
    return proj


def render(proj, indent=0):
    """The file's text: indented objects, with short lists of plain values (zones, swing ends,
    volatility) on one line as in SKILL.md's example, longer ones one item per line. Fixed
    formatting, so one input always gives the same bytes."""
    if isinstance(proj, dict):
        if not proj:
            return "{}"
        inner = " " * (indent + 2)
        body = ",\n".join(f"{inner}{json.dumps(key, ensure_ascii=False)}: {render(value, indent + 2)}"
                          for key, value in proj.items())
        text = "{\n" + body + "\n" + " " * indent + "}"
    elif isinstance(proj, list) and proj and (any(isinstance(item, (dict, list)) for item in proj)
                                              or len(json.dumps(proj, ensure_ascii=False)) > 100):
        inner = " " * (indent + 2)
        text = "[\n" + ",\n".join(inner + render(item, indent + 2) for item in proj) + "\n" + " " * indent + "]"
    else:
        text = json.dumps(proj, ensure_ascii=False)
    return text + ("\n" if indent == 0 else "")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] not in ("-o", "--output")):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        d = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        text = render(build(d))
    except MechanicalError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Error: cannot project {argv[0]} ({type(error).__name__}: {error})", file=sys.stderr)
        return 1
    if len(argv) == 3:
        Path(argv[2]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
