"""Översikt över alla trafikslag: ett läge per trafikslag och några meningar att läsa upp.

Läget för spårtrafiken bygger på:
- akuta trafikstörningar ur störningsflödet (linje-/hållplatslarm med trafikord, se alerts.py).
  SL publicerar om planerade meddelanden dagligen, så publiceringstiden säger inget; det som
  skiljer är giltighetstiden: akuta störningar gäller några timmar (t.ex. 09:45–12:00),
  planerade ändringar dagar eller månader. Planerade påverkar inte läget men visas.
- inställda avgångar (realtid + larm på enskilda turer),
- förseningar: andel tåg minst 3 resp. 5 min sena och största förseningen,
- färre tåg i trafik än enligt tidtabellen.
Bussarnas läge kommer från bussöversikten (bus.py).
"""
from __future__ import annotations

from . import config

GROUPS = [
    ("Tunnelbana", ["TB1", "TB2", "TB3"]),
    ("Pendeltåg", ["PT"]),
    ("Lokalbanor och spårväg", ["RB", "SB", "LB", "TVB", "SpvC"]),
]
RANK = {"major": 2, "minor": 1, "normal": 0}
ACUTE_MAX_PERIOD_S = 12 * 3600
ACUTE_NO_END_S = 6 * 3600


def is_acute(since: int | None, until: int | None, now: int) -> bool:
    if since and until:
        return until - since <= ACUTE_MAX_PERIOD_S
    return bool(since and now - since <= ACUTE_NO_END_S)


def rail_status(n: int, planned: int, late3: int, late5: int, max_delay: float | None,
                cancelled: int, traffic_alerts: int) -> str:
    missing = planned - n
    short = missing >= 2 and planned and missing / planned >= 0.15
    # Färre tåg med realtid än enligt tidtabell kan vara saknad data (vanligt på linje 7),
    # så det ger som mest "störningar", aldrig "stora störningar" på egen hand.
    if (n >= 4 and late5 / n >= 0.30) or cancelled >= 4:
        return "major"
    if traffic_alerts or cancelled or short or (n >= 4 and late3 / n >= 0.20) or (max_delay or 0) >= 10:
        return "minor"
    return "normal"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " och " + items[-1]


def build_overview(rows, alerts: dict, planned_tab: dict, bus: dict | None, now: int) -> dict:
    modes = []
    for t in config.TABS:
        code = t["code"]
        mine = [r for r in rows if r.tab == code]
        running = [r for r in mine if r.status != "cancelled"]
        delays = [r.delay_min for r in running if r.delay_min is not None]
        ta = alerts.get(code)
        traffic = ta.traffic if ta else []
        acute = [a for a in traffic if is_acute(a.since, a.until, now)]
        planned_changes = [a for a in traffic if a not in acute]
        trip_alerts = ta.trips if ta else []
        cancelled = sum(1 for r in mine if r.status == "cancelled") + sum(
            1 for x in trip_alerts if "inställ" in x.text.lower())
        late3 = sum(1 for d in delays if d >= 3)
        late5 = sum(1 for d in delays if d >= 5)
        max_delay = max(delays) if delays else None
        planned = planned_tab.get(code, 0)
        st = rail_status(len(running), planned, late3, late5, max_delay, cancelled, len(acute))
        modes.append({
            "code": code, "name": t["name"], "status": st,
            "count": len(running), "planned": planned,
            "late3": late3, "late5": late5, "max": max_delay, "cancelled": cancelled,
            "alerts": [{"title": a.title, "lines": a.lines, "since": a.since, "until": a.until} for a in acute[:3]],
            "alert_count": len(acute),
            "planned_changes": [{"title": a.title, "lines": a.lines} for a in planned_changes[:3]],
            "planned_count": len(planned_changes),
            "trip_alert_count": len(trip_alerts),
        })

    bus_mode = None
    if bus:
        tot = bus["totals"]
        issues = [a for a in bus["areas"] if a["status"] != "normal"]
        st = "major" if any(a["status"] == "major" for a in issues) else "minor" if issues else "normal"
        bus_mode = {
            "code": "BUS", "name": "Buss", "status": st,
            "count": tot["buses"], "planned": tot["planned"], "share5": tot["share5"],
            "late5": tot["late5"], "avg": tot["avg"], "cancelled": tot["cancelled"],
            "areas": [{"name": a["name"], "status": a["status"], "reason": a["reasons"][0][0] if a["reasons"] else None}
                      for a in issues[:5]],
            "hotspots": [h["name"] for h in bus["hotspots"][:3]],
        }

    return {"groups": [{"name": g, "codes": codes} for g, codes in GROUPS],
            "modes": modes, "bus": bus_mode, "brief": _brief(modes, bus_mode)}


def _describe(m: dict) -> str:
    bits = []
    if m["alerts"]:
        bits.append(m["alerts"][0]["title"].rstrip("."))
    if m["cancelled"]:
        bits.append(f"{m['cancelled']} inställd{'a' if m['cancelled'] > 1 else ''} avgång{'ar' if m['cancelled'] > 1 else ''}")
    if m["late5"]:
        bits.append(f"{m['late5']} tåg minst 5 min sen{'a' if m['late5'] > 1 else 't'}")
    elif m["max"] and m["max"] >= 3:
        bits.append(f"förseningar upp till {round(m['max'])} min")
    missing = m["planned"] - m["count"]
    if missing >= 2 and m["planned"] and missing / m["planned"] >= 0.15:
        bits.append(f"{m['count']} tåg med realtid mot {m['planned']} enligt tidtabell")
    if not bits:
        return m["name"]
    head, rest = bits[0], bits[1:3]
    return f"{m['name']}: {head}" + (f" ({', '.join(rest)})" if rest else "")


def _brief(modes: list[dict], bus: dict | None) -> list[str]:
    out = []
    bad = sorted([m for m in modes if m["status"] != "normal"], key=lambda m: -RANK[m["status"]])
    ok = [m["name"] for m in modes if m["status"] == "normal"]
    if bad:
        out.extend(_describe(m) + "." for m in bad)
    if ok:
        out.append(("Normal trafik i all spårtrafik." if not bad else f"Normal trafik på {_join(ok)}."))
    if bus:
        if bus["areas"]:
            areas = [a["name"] + (f" ({a['reason']})" if a["reason"] else "") for a in bus["areas"][:4]]
            out.append(f"Buss: förseningar i {_join(areas)}; i hela länet är {int(bus['share5'] * 100 + 0.5)} % av bussarna minst 5 min sena.")
        else:
            out.append(f"Bussarna går i stort sett normalt; {int(bus['share5'] * 100 + 0.5)} % är minst 5 min sena.")
    return out
