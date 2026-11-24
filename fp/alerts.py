"""Störningar kopplade till flikarna.

Linje- och stationsmeddelanden hämtas i första hand från SL Deviations (deviations.py),
samma källa som sl.se/trafiklaget, där varje meddelande är knutet till linjer med
trafikslag. GTFS-RT ServiceAlerts används för larm på enskilda turer och som reserv.

Om GTFS-RT ServiceAlerts:
SL:s flöde saknar effect och severity (alltid UNKNOWN), så sortering sker på text
och på vilka linjer, hållplatser och turer larmet gäller. Tre nivåer:
  route  – larmet anger route_id: gäller linjen
  stop   – bara hållplats: kopplas till flikarna som trafikerar hållplatsen
  trip   – enskild avgång: visas i listan "Enskilda avgångar" och på raden i tabellen
Kategorier: traffic (trafikpåverkan), access (hissar, entréer), info (skyltar, utrop).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .board import base_line
from .static_gtfs import StaticData, to_epoch

ACCESS_RE = re.compile(r"\b(hiss\w*|rulltrapp\w*|entré\w*|uppgång\w*|utgång\w*|trapp\w*|ramp\w*)\b", re.I)
# Allmän information som aldrig är en trafikstörning (trygghetsutrop, halka, påminnelser)
INFO_RE = re.compile(
    r"\b(skylt\w*|högtalar\w*|utrop\w*|informationssystem\w*|display\w*|trygghet\w*|otrygg\w*|"
    r"halka|hala plattformar|halt på)\b", re.I)
# Ord som visar att trafiken påverkas. Ett larm utan något av dem räknas inte som störning.
TRAFFIC_RE = re.compile(
    r"inställ|försen|ersätt|stopp|trafik|fel\b|växelfel|signalfel|spårfel|vagnfel|vagnbrist|en vagn|"
    r"kortare tåg|extrainsatt|stannar inte|stannar ej|vänder|byte|byt till|glesare|färre|tätare|"
    r"olycka|obehörig|polis|sjukdomsfall|strömavbrott|broöppning|omled|indrag|kör inte|går inte|"
    r"längre restid|ny avgångstid|avstängt mellan|ingen trafik",
    re.I,
)
INTERNAL_HEADER_RE = re.compile(r"(\bRP\)?\s*$|^Stn\.?\s*medd)", re.I)
CATEGORIES = ("traffic", "access", "info")
# Akut eller planerat avgörs av giltighetstiden: akuta störningar gäller några timmar,
# planerade ändringar dagar eller månader (SL publicerar om planerade dagligen).
ACUTE_MAX_PERIOD_S = 12 * 3600
ACUTE_NO_END_S = 6 * 3600
# Trafikslag som nämns i texten. Ett hållplatslarm om pendeltåg som SL kopplat till
# tunnelbanans plattformar ska inte visas på tunnelbanan.
MODE_WORDS = [
    (re.compile(r"pendel", re.I), {"PT"}),
    (re.compile(r"tunnelban|tub", re.I), {"TB1", "TB2", "TB3"}),
    (re.compile(r"roslagsban", re.I), {"RB"}),
    (re.compile(r"tvärban", re.I), {"TVB"}),
    (re.compile(r"saltsjöban", re.I), {"SB"}),
    (re.compile(r"lidingöban", re.I), {"LB"}),
    (re.compile(r"spårväg city|linje 7\b", re.I), {"SpvC"}),
]


def is_acute(since: int | None, until: int | None, now: int) -> bool:
    if since and until:
        return until - since <= ACUTE_MAX_PERIOD_S
    return bool(since and now - since <= ACUTE_NO_END_S)


def _named_tabs(text: str) -> set[str]:
    out: set[str] = set()
    for rx, tabs in MODE_WORDS:
        if rx.search(text):
            out |= tabs
    return out
TRIP_DONE_GRACE_S = 30 * 60  # visa larm för avgångar som avslutats för högst 30 min sedan


@dataclass
class AlertItem:
    id: str
    level: str               # route | stop
    category: str
    title: str
    text: str
    lines: list[str]
    stops: list[str]
    since: int | None
    until: int | None
    acute: bool = False


@dataclass
class TripAlert:
    id: str
    trip_id: str
    line: str
    train_number: str
    destination: str
    departs: int | None      # avgång från första hållplatsen
    text: str


@dataclass
class TabAlerts:
    traffic: list[AlertItem] = field(default_factory=list)
    access: list[AlertItem] = field(default_factory=list)
    info: list[AlertItem] = field(default_factory=list)
    trips: list[TripAlert] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Antal akuta störningar (märket på fliken). Planerade ändringar räknas inte."""
        return sum(1 for a in self.traffic if a.acute) + len(self.trips)


def _text(ts) -> str:
    """Första översättningen (SL har bara svenska, utan språkkod)."""
    for t in ts.translation:
        if t.text:
            return re.sub(r"\n{3,}", "\n\n", t.text.replace("\r", "")).strip()
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def _tokens(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) >= 3}


def _is_display_text(header: str, desc: str) -> bool:
    """Plattformsskyltens korta texter ('Främre tåg', 'Ingen påstigning') är inga störningar."""
    return _norm(header) == _norm(desc) and len(header) < 40 and not TRAFFIC_RE.search(header)


def _category(header: str, desc: str, level: str) -> str:
    """Rubriken avgör först. I beskrivningen väger trafikord tyngre än 'skyltar', eftersom
    trafikmeddelanden ofta hänvisar till skyltarna. Larm utan något trafikord
    ('Trygghetsutrop ombord', 'Det kan vara halt på plattformen') räknas som övrigt.
    SL:s flöde har orsak (cause) men den är OTHER_CAUSE även för vanliga förseningar,
    och effekt saknas, så texten är det enda som skiljer."""
    if ACCESS_RE.search(header):
        return "access"
    if INFO_RE.search(header):
        return "info"
    if TRAFFIC_RE.search(header):
        return "traffic"
    if ACCESS_RE.search(desc):
        return "access"
    if TRAFFIC_RE.search(desc):
        return "traffic"
    return "info"


# SL Deviations har också meddelanden om en enskild avgång ("Försenad avgång": "... från
# Käppala kl 15:38 mot Ropsten ..."). Samma sak finns som turlarm i GTFS-RT, så de visas
# bara under Enskilda avgångar.
SINGLE_DEP_HEAD_RE = re.compile(r"^(försenad|inställd|ändrad|extra)\w*\s+(avgång|tur)\b", re.I)
SINGLE_DEP_RE = re.compile(r"\bfrån\s+(?P<orig>[^.,]+?)\s+kl\.?\s*(?P<h>\d{1,2})[:.](?P<m>\d{2})\s+mot\s+(?P<dest>[^.,]+?)(?=\s+(på grund|pga|p\.g\.a)|[.,]|$)", re.I)
SINGLE_DEP_KEEP_S = 3600   # visa avgångar utan turlarm i högst en timme efter avgång


def _norm(text: str) -> str:
    return re.sub(r"[\s.]+", " ", text.lower()).strip()


def _single_departure(f: dict, now: int) -> tuple[int | None, str] | None:
    """(avgångstid, destination) om meddelandet gäller en enskild avgång."""
    m = SINGLE_DEP_RE.search(f["details"]) or SINGLE_DEP_RE.search(f["header"])
    if not m and not SINGLE_DEP_HEAD_RE.search(f["header"]):
        return None
    if not m:
        return None, ""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(now, ZoneInfo("Europe/Stockholm"))
    dep = local.replace(hour=int(m["h"]) % 24, minute=int(m["m"]), second=0, microsecond=0).timestamp()
    if dep > now + 12 * 3600:      # "kl 23:50" strax efter midnatt gäller gårdagen
        dep -= 86400
    return int(dep), m["dest"].strip()


def _deviation_category(f: dict) -> str:
    """SL Deviations: hissar/entréer, skyltar och övrigt, annars trafik.
    importance_level 2 används för hissar, entréer och museitrafik, aldrig för trafikstörningar."""
    header, details = f["header"], f["details"]
    if f["facility"] or ACCESS_RE.search(header):
        return "access"
    if INFO_RE.search(header) or (f["priority"].get("importance_level") or 0) <= 2:
        return "info"
    if TRAFFIC_RE.search(header) or TRAFFIC_RE.search(details) or (f["priority"].get("importance_level") or 0) >= 7:
        return "traffic"
    return "info"


def _trip_departure(static: StaticData, trip, start_date: str, now: int) -> tuple[int | None, int | None]:
    """(avgång, ankomst till ändhållplats) för den förekomst av turen som ligger närmast nu."""
    dates = [start_date] if start_date else sorted(static.service_dates.get(trip.service_id, ()))
    best = None
    for sd in dates:
        dep, arr = to_epoch(sd, trip.stops[0].dep), to_epoch(sd, trip.stops[-1].arr)
        if dep is None:
            continue
        dist = 0 if dep <= now <= (arr or dep) else min(abs(dep - now), abs((arr or dep) - now))
        if best is None or dist < best[0]:
            best = (dist, dep, arr)
    return (best[1], best[2]) if best else (None, None)


def _split_first_sentence(text: str) -> tuple[str, str]:
    m = re.search(r"[.!?]\s+(?=[A-ZÅÄÖ])", text)  # inte "kl. 22:00"
    if not m:
        return text, ""
    return text[: m.start() + 1].strip(), text[m.end():].strip()


def _title_and_text(header: str, desc: str, level: str) -> tuple[str, str]:
    """Hållplatslarm och vissa linjelarm har interna rubriker ('Unv-Kår', '… RP').
    Då används beskrivningens första mening som rubrik."""
    header = re.sub(r"^Stn\.?\s*medd\.?\s*", "", header.strip()).rstrip(" .")
    internal = level == "stop" or len(header) < 15 or INTERNAL_HEADER_RE.search(header)
    if internal and desc:
        title, rest = _split_first_sentence(desc)
        if len(title) > 120:
            title = title[:117].rsplit(" ", 1)[0] + "…"
            rest = desc
        return title.rstrip("."), rest
    # Ta bort upprepad rubrik bara om den står som egen mening ("Rubrik. Text…"),
    # inte när beskrivningen fortsätter meningen ("Förseningar … för tunnelbanans …").
    if desc.lower().startswith(header.lower()) and re.match(r"\s*([.:!\n]|$)", desc[len(header):]):
        desc = desc[len(header):].lstrip(" .:!\n")
    return header, desc


def _active(alert, now: int) -> bool:
    if not alert.active_period:
        return True
    return any((not p.start or p.start <= now) and (not p.end or now <= p.end) for p in alert.active_period)


def _period(alert, now: int) -> tuple[int | None, int | None]:
    for p in alert.active_period:
        if (not p.start or p.start <= now) and (not p.end or now <= p.end):
            return p.start or None, p.end or None
    return None, None


def _dedupe(items: list[AlertItem]) -> list[AlertItem]:
    """Samma störning publiceras ofta både på linjen och på enskilda stationer
    med nästan samma text. Behåll linjelarmet, annars den längsta texten."""
    ordered = sorted(items, key=lambda a: (a.level != "route", -len(a.text) - len(a.title)))
    kept: list[tuple[AlertItem, set[str]]] = []
    for a in ordered:
        toks = _tokens(a.title + " " + a.text)
        dup = False
        for _, k in kept:
            small, big = (toks, k) if len(toks) <= len(k) else (k, toks)
            if small and len(small & big) / len(small) >= 0.7:
                dup = True
                break
        if not dup:
            kept.append((a, toks))
    return [a for a, _ in kept]


def _recency(item) -> tuple[int, int]:
    """Nyast först. SL:s larm-id är ett löpnummer som följer skapandetiden (stämmer med
    starttiden i ~98 % av fallen), vilket fungerar även när active_period anger en
    planerad starttid långt bak i tiden. Starttiden används som reserv."""
    num = int(item.id) if item.id.isdigit() else 0
    return num, getattr(item, "since", None) or 0


def build_alerts(static: StaticData, feed, now: int, deviations: list | None = None,
                 ) -> tuple[dict[str, TabAlerts], dict[str, list[str]]]:
    """Returnerar (larm per flik, korta noteringar per trip_id för tabellraderna).
    Med deviations (SL Deviations) tas linje- och stationsmeddelanden därifrån och
    GTFS-RT används bara för enskilda turer."""
    per_tab: dict[str, dict[str, list[AlertItem]]] = {}
    trips_by_tab: dict[str, list[TripAlert]] = {}
    notes: dict[str, list[str]] = {}

    for e in feed.entity if feed is not None else ():
        if not e.HasField("alert"):
            continue
        a = e.alert
        if not _active(a, now):
            continue
        header, desc = _text(a.header_text), _text(a.description_text)
        if not header and not desc:
            continue
        if _is_display_text(header, desc):
            continue

        trip_ids, tabs, lines, stop_ids = {}, set(), set(), []
        has_route = False
        for ie in a.informed_entity:
            if ie.HasField("trip") and ie.trip.trip_id in static.trips:
                trip_ids.setdefault(ie.trip.trip_id, ie.trip.start_date)
            if ie.route_id and ie.route_id in static.rail_routes:
                line, tab = static.rail_routes[ie.route_id]
                tabs.add(tab)
                lines.add(line)
                has_route = True
            if ie.stop_id:
                stop_ids.append(ie.stop_id)

        # Enskilda avgångar
        if trip_ids:
            text = desc if len(desc) >= len(header) else header
            for tid, start_date in trip_ids.items():
                t = static.trips[tid]
                if not t.stops:
                    continue
                departs, arrives = _trip_departure(static, t, start_date, now)
                if arrives and arrives < now - TRIP_DONE_GRACE_S:
                    continue  # turen är redan slut
                notes.setdefault(tid, []).append(text)
                trips_by_tab.setdefault(t.tab, []).append(TripAlert(
                    id=e.id, trip_id=tid, line=t.line, train_number=t.train_number,
                    destination=t.headsign or static.stop_name(t.destination_stop_id),
                    departs=departs, text=text,
                ))
            continue

        if deviations is not None:
            continue  # linje- och stationsmeddelanden kommer från SL Deviations
        if not has_route:
            for sid in stop_ids:
                tabs |= static.stop_tabs.get(sid, set())
            named = _named_tabs(header + " " + desc)
            if named:
                tabs &= named
        if not tabs:
            continue  # buss, båt eller hållplatser utan spårtrafik

        level = "route" if has_route else "stop"
        title, text = _title_and_text(header, desc, level)
        since, until = _period(a, now)
        stops = list(dict.fromkeys(static.stop_name(s) for s in stop_ids))
        item = AlertItem(
            id=e.id, level=level, category=_category(header, desc, level), title=title, text=text,
            lines=sorted(lines, key=lambda x: (len(x), x)), stops=stops[:6],
            since=since, until=until, acute=is_acute(since, until, now),
        )
        for tab in tabs:
            per_tab.setdefault(tab, {c: [] for c in CATEGORIES})[item.category].append(item)

    if deviations is not None:
        from .deviations import rail_items
        for tabs_lines, f in rail_items(deviations, now):
            cat = _deviation_category(f)
            urgent = (f["priority"].get("urgency_level") or 0) >= 5
            single = _single_departure(f, now)
            if single is not None:
                departs, dest = single
                text = f["details"] or f["header"]
                for tab, lines in tabs_lines.items():
                    known = trips_by_tab.get(tab, [])
                    if any(_norm(text) in _norm(t.text) or _norm(t.text) in _norm(text)
                           or (departs and t.departs == departs and base_line(t.line) == base_line(lines[0]))
                           for t in known):
                        continue  # samma avgång finns redan som turlarm
                    if departs and departs < now - SINGLE_DEP_KEEP_S:
                        continue
                    trips_by_tab.setdefault(tab, []).append(TripAlert(
                        id=f["id"], trip_id="", line=lines[0], train_number="",
                        destination=dest, departs=departs, text=text))
                continue
            for tab, lines in tabs_lines.items():
                title, text = f["header"], f["details"]
                if text.lower().startswith(title.lower()) and re.match(r"\s*([.:!\n]|$)", text[len(title):]):
                    text = text[len(title):].lstrip(" .:!\n")
                item = AlertItem(
                    id=f["id"], level="route", category=cat, title=title.rstrip("."), text=text,
                    lines=sorted(set(lines), key=lambda x: (len(x), x)), stops=f["stops"][:6],
                    since=f["since"], until=f["until"],
                    acute=urgent or is_acute(f["since"], f["until"], now),
                )
                per_tab.setdefault(tab, {c: [] for c in CATEGORIES})[cat].append(item)

    out: dict[str, TabAlerts] = {}
    for tab in set(per_tab) | set(trips_by_tab):
        cats = per_tab.get(tab, {c: [] for c in CATEGORIES})
        dd = _dedupe if deviations is None else list  # Deviations har ett meddelande per ärende
        ta = TabAlerts(**{c: sorted(dd(cats[c]), key=_recency, reverse=True) for c in CATEGORIES})
        ta.trips = sorted(trips_by_tab.get(tab, []), key=lambda t: t.departs or 0, reverse=True)
        out[tab] = ta
    return out, notes


def to_json(ta: TabAlerts | None) -> dict:
    ta = ta or TabAlerts()
    return {
        "count": ta.count,
        "traffic": [asdict(a) for a in ta.traffic],
        "trips": [asdict(a) for a in ta.trips],
        "access": [asdict(a) for a in ta.access],
        "info": [asdict(a) for a in ta.info],
    }
