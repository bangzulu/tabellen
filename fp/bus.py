"""Bussöversikt för trafikinformatörer: läget per område i stället för enskilda avgångar.

Underlag:
- Realtid (TripUpdates): varje bussturs aktuella försening och var den är
  (senast passerade hållplats -> område).
- Tidtabell: hur många bussturer som borde vara ute just nu.
- Störningsmeddelanden (ServiceAlerts): larm på enskilda turer (förseningar,
  framkomlighetsproblem, inställt) räknas ihop per område och orsak; nya linje- och
  hållplatsmeddelanden lyfts fram, äldre räknas som planerade ändringar.

Områdets status (normal / förseningar / stora förseningar) bygger på andelen bussar
som är minst 5 resp. 10 minuter sena, plus antalet larm om förseningar och inställda turer.
Gränserna är satta efter ett vanligt eftermiddagsläge (~13 % minst 5 min sena i länet).
"""
from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter, defaultdict, deque

from google.transit import gtfs_realtime_pb2 as rt

from .alerts import _text
from .static_gtfs import NO_TIME, StaticData, candidate_service_dates, service_day_start

TU = rt.TripUpdate
MAX_DELAY_S = 90 * 60        # större värden är nästan alltid fel i datat
FINISHED_GRACE_S = 120
NEW_ALERT_S = 3 * 3600       # linje-/hållplatsmeddelanden yngre än så visas som nya
CANCEL_WINDOW_S = 60 * 60    # inställda turer som skulle gått inom ±1 h
CELL_LAT, CELL_LON = 0.01, 0.02   # rutnät för brännpunkter, ca 1 × 1 km

REASON_RE = re.compile(r"(?:p\.\s?g\.\s?a\.?|på grund av|pga\.?)\s+([^.,;]+)", re.I)
REASON_NAMES = {
    "framkomlighetsproblem": "framkomlighetsproblem", "framkomlighet": "framkomlighetsproblem",
    "köer": "köer", "kö": "köer", "trafikolycka": "olycka", "olycka": "olycka",
    "fordonsfel": "fordonsfel", "vagnfel": "fordonsfel", "tekniskt fel": "fordonsfel",
    "personalbrist": "personalbrist", "brist på förare": "personalbrist", "halka": "halka", "väglag": "halka",
}

# Historik per område för trend (andel minst 5 min sena), sparas vid varje nytt realtidsflöde
_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=240))
_last_feed_ts: int | None = None


def _reason(text: str) -> str | None:
    m = REASON_RE.search(text)
    if not m:
        return None
    r = m.group(1).strip().lower()
    for key, name in REASON_NAMES.items():
        if r.startswith(key):
            return name
    return r[:40]


def status_of(n: int, d5: int, d10: int, delay_alerts: int, cancelled: int, net_share5: float = 0.13) -> str:
    """Läget jämförs både mot fasta gränser och mot hela länet, så att ett område bara
    flaggas när det sticker ut (en vanlig eftermiddag är ~13 % av bussarna 5+ min sena;
    i rusningstrafik kan hela länet ligga högre utan att något enskilt område är värre)."""
    share5 = d5 / n if n else 0
    share10 = d10 / n if n else 0
    if (n >= 10 and share5 >= max(0.45, net_share5 + 0.25)) or (d10 >= 4 and share10 >= 0.25) \
            or cancelled >= 6 or delay_alerts >= 12:
        return "major"
    if (n >= 10 and share5 >= max(0.30, net_share5 + 0.12)) or (d10 >= 3 and share10 >= 0.12) \
            or cancelled >= 3 or delay_alerts >= 6:
        return "minor"
    return "normal"


STATUS_RANK = {"major": 2, "minor": 1, "normal": 0}


def _planned_per_route(static: StaticData, now: int) -> Counter:
    b = static.bus
    out: Counter = Counter()
    for sd in candidate_service_dates_for(now):
        day0 = int(service_day_start(sd).timestamp())
        ok = [sd in static.service_dates.get(svc, ()) for svc in b.services]
        for i in range(len(b.trip_route)):
            if not ok[b.trip_service[i]]:
                continue
            dep, arr = b.trip_dep[i], b.trip_arr[i]
            if dep != NO_TIME and arr != NO_TIME and day0 + dep <= now <= day0 + arr:
                out[b.trip_route[i]] += 1
    return out


def candidate_service_dates_for(now: int) -> list[str]:
    from datetime import datetime

    from .static_gtfs import TZ
    return candidate_service_dates(datetime.fromtimestamp(now, TZ))


def _trip_start(static: StaticData, ti: int, start_date: str, now: int) -> int | None:
    b = static.bus
    dep = b.trip_dep[ti]
    if dep == NO_TIME:
        return None
    dates = [start_date] if start_date else candidate_service_dates_for(now)
    times = [int(service_day_start(sd).timestamp()) + dep for sd in dates
             if not start_date and sd in static.service_dates.get(b.services[b.trip_service[ti]], ()) or start_date]
    return min(times, key=lambda t: abs(t - now)) if times else None


def build_bus_overview(static: StaticData, tu, sa, now: int | None = None) -> dict:
    global _last_feed_ts
    now = int(now or time.time())
    b = static.bus
    areas = b.areas

    # --- Realtid: position och försening per buss i trafik ---
    a_delays: dict[int, list[float]] = defaultdict(list)
    r_delays: dict[int, list[float]] = defaultdict(list)
    cells: dict[tuple[int, int], list] = defaultdict(list)
    vehicles = []
    cancelled_route: Counter = Counter()
    realtime_route: Counter = Counter()
    for e in tu.entity:
        if not e.HasField("trip_update"):
            continue
        u = e.trip_update
        ti = b.trip_index.get(u.trip.trip_id)
        if ti is None:
            continue
        ridx = b.trip_route[ti]
        if u.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            start = _trip_start(static, ti, u.trip.start_date, now)
            if start is not None and abs(start - now) <= CANCEL_WINDOW_S:
                cancelled_route[ridx] += 1
            continue
        last = nxt = None
        for s in u.stop_time_update:
            if s.schedule_relationship == TU.StopTimeUpdate.SKIPPED:
                continue
            ev = s.departure if s.departure.time else s.arrival
            if not ev.time:
                continue
            if ev.time <= now:
                last = (s, ev)
            elif nxt is None:
                nxt = (s, ev)
        if last is None:
            continue                                   # inte avgått än
        if nxt is None and last[1].time < now - FINISHED_GRACE_S:
            continue                                   # framme
        ev = (nxt or last)[1]
        delay = ev.delay if ev.HasField("delay") else None
        si = b.stop_index.get(last[0].stop_id)
        if si is None:
            continue
        realtime_route[ridx] += 1
        if delay is None or abs(delay) > MAX_DELAY_S:
            continue
        d = delay / 60
        aidx = b.stop_area[si]
        a_delays[aidx].append(d)
        r_delays[ridx].append(d)
        lat, lon = b.stop_lat[si], b.stop_lon[si]
        cells[(math.floor(lat / CELL_LAT), math.floor(lon / CELL_LON))].append((d, si, ridx))
        vehicles.append([round(lat, 4), round(lon, 4), round(d, 1), b.lines[ridx]])

    planned_route = _planned_per_route(static, now)

    # --- Störningsmeddelanden ---
    trip_alerts: dict[int, list] = defaultdict(list)        # område -> [(rad, linje, orsak, inställd)]
    new_alerts: dict[int, list] = defaultdict(list)
    planned_alerts: dict[int, list] = defaultdict(list)
    for e in (sa.entity if sa is not None else ()):
        if not e.HasField("alert"):
            continue
        a = e.alert
        if a.active_period and not any((not p.start or p.start <= now) and (not p.end or now <= p.end)
                                       for p in a.active_period):
            continue
        header, desc = _text(a.header_text), _text(a.description_text)
        trips = [ie.trip.trip_id for ie in a.informed_entity if ie.HasField("trip") and ie.trip.trip_id in b.trip_index]
        if trips:
            text = desc if len(desc) >= len(header) else header
            if header.strip().lower() in ("ingen påstigning", "främre tåg"):
                continue
            for tid in dict.fromkeys(trips):
                ridx = b.trip_route[b.trip_index[tid]]
                aidx = b.home_area[ridx]
                if aidx < 0:
                    continue
                cancelled = bool(re.search(r"inställ", text, re.I))
                trip_alerts[aidx].append({"line": b.lines[ridx], "text": text, "reason": _reason(header + ". " + desc),
                                          "cancelled": cancelled})
            continue
        route_idx = [b.route_index[ie.route_id] for ie in a.informed_entity if ie.route_id in b.route_index]
        stop_idx = [b.stop_index[ie.stop_id] for ie in a.informed_entity if ie.stop_id in b.stop_index]
        if not route_idx and not stop_idx:
            continue
        area_set = {b.home_area[r] for r in route_idx if b.home_area[r] >= 0} or {b.stop_area[s] for s in stop_idx}
        since = min((p.start for p in a.active_period if p.start), default=None)
        lines = sorted({b.lines[r] for r in route_idx}, key=lambda x: (len(x), x))
        if not lines:   # hållplatslarm: linjerna står ofta i texten ("Buss linje 3 mot …")
            lines = re.findall(r"\b(\d{1,3}[A-Z]?)\s+mot\b", desc)[:8]
        item = {"id": e.id, "title": header.strip().rstrip("."), "text": desc if desc != header else "",
                "lines": list(dict.fromkeys(lines)), "stops": list(dict.fromkeys(b.stop_name[s] for s in stop_idx))[:6],
                "since": since}
        is_new = since is not None and now - since <= NEW_ALERT_S
        for aidx in area_set:
            (new_alerts if is_new else planned_alerts)[aidx].append(item)

    all_d = [d for ds in a_delays.values() for d in ds]
    net_share5 = sum(1 for d in all_d if d >= 5) / len(all_d) if all_d else 0.13

    # --- Per område ---
    area_out = []
    for aidx, name in enumerate(areas):
        ds = a_delays.get(aidx, [])
        routes = [i for i, h in enumerate(b.home_area) if h == aidx]
        planned = sum(planned_route[i] for i in routes)
        realtime = sum(realtime_route[i] for i in routes)
        cancelled = sum(cancelled_route[i] for i in routes)
        tas = trip_alerts.get(aidx, [])
        delay_alerts = sum(1 for t in tas if not t["cancelled"])
        cancelled += sum(1 for t in tas if t["cancelled"])
        if not ds and not planned and not tas and not new_alerts.get(aidx):
            continue
        n = len(ds)
        d5 = sum(1 for d in ds if d >= 5)
        d10 = sum(1 for d in ds if d >= 10)
        st = status_of(n, d5, d10, delay_alerts, cancelled, net_share5)
        share5 = round(d5 / n, 3) if n else 0
        hist = _history[name]
        if _last_feed_ts != tu.header.timestamp and n:
            hist.append((now, share5))
        trend = None
        old = [s for t, s in hist if now - 20 * 60 <= t <= now - 10 * 60]
        if old and n:
            trend = round(share5 - statistics.mean(old), 3)

        # Linjer med problem i området
        line_rows = []
        for ridx in routes:
            lds = r_delays.get(ridx, [])
            l_alerts = [t for t in tas if t["line"] == b.lines[ridx]]
            l5 = sum(1 for d in lds if d >= 5)
            if not lds and not l_alerts and not cancelled_route[ridx]:
                continue
            line_rows.append({
                "line": b.lines[ridx], "kind": b.kinds[ridx], "buses": len(lds), "planned": planned_route[ridx],
                "late5": l5, "avg": round(statistics.mean(lds), 1) if lds else None,
                "max": round(max(lds), 1) if lds else None,
                "alerts": len(l_alerts), "cancelled": cancelled_route[ridx] + sum(1 for t in l_alerts if t["cancelled"]),
            })
        line_rows.sort(key=lambda r: (-(r["late5"] + 2 * r["cancelled"] + r["alerts"]), -(r["avg"] or 0)))
        reasons = Counter(t["reason"] for t in tas if t["reason"])
        area_out.append({
            "name": name, "status": st, "buses": n, "planned": planned, "realtime": realtime,
            "late5": d5, "late10": d10, "share5": share5,
            "avg": round(statistics.mean(ds), 1) if ds else None,
            "max": round(max(ds), 1) if ds else None,
            "trend": trend,
            "spark": _spark(hist, now),
            "cancelled": cancelled, "delay_alerts": delay_alerts,
            "reasons": reasons.most_common(4),
            "trip_alerts": tas[:12],
            "new_alerts": new_alerts.get(aidx, []),
            "planned_alerts": len(planned_alerts.get(aidx, [])),
            "planned_alert_list": [{"title": x["title"], "lines": x["lines"]} for x in planned_alerts.get(aidx, [])[:30]],
            "lines": [r for r in line_rows if r["late5"] or r["alerts"] or r["cancelled"]][:15],
        })
    area_out.sort(key=lambda a: (-STATUS_RANK[a["status"]], -(a["share5"] * min(a["buses"], 20)), -a["delay_alerts"]))

    # --- Brännpunkter: rutor med flera sena bussar ---
    hot = []
    for (ci, cj), items in cells.items():
        late = [x for x in items if x[0] >= 5]
        if len(late) < 3:
            continue
        name = Counter(b.stop_name[si] for _, si, _ in late).most_common(1)[0][0]
        si0 = late[0][1]
        hot.append({
            "name": name, "area": areas[b.stop_area[si0]],
            "lat": round(sum(b.stop_lat[x[1]] for x in late) / len(late), 4),
            "lon": round(sum(b.stop_lon[x[1]] for x in late) / len(late), 4),
            "buses": len(items), "late": len(late), "avg": round(statistics.mean(x[0] for x in late), 1),
            "lines": sorted({b.lines[x[2]] for x in late}, key=lambda s: (len(s), s))[:10],
            "_cell": (ci, cj),
        })
    hot.sort(key=lambda h: (-h["late"], -h["avg"]))
    chosen = []
    for h in hot:   # hoppa över grannrutor till en redan vald brännpunkt
        if all(abs(h["_cell"][0] - c["_cell"][0]) > 1 or abs(h["_cell"][1] - c["_cell"][1]) > 1 for c in chosen):
            chosen.append(h)
        if len(chosen) == 8:
            break
    for h in chosen:
        h.pop("_cell")

    n_all = len(all_d)
    totals = {
        "buses": n_all,
        "planned": sum(planned_route.values()),
        "realtime": sum(realtime_route.values()),
        "late5": sum(1 for d in all_d if d >= 5),
        "late10": sum(1 for d in all_d if d >= 10),
        "share5": round(sum(1 for d in all_d if d >= 5) / n_all, 3) if n_all else 0,
        "avg": round(statistics.mean(all_d), 1) if all_d else None,
        "cancelled": sum(a["cancelled"] for a in area_out),
        "delay_alerts": sum(a["delay_alerts"] for a in area_out),
    }
    _last_feed_ts = tu.header.timestamp
    return {
        "generated_at": now,
        "feed_timestamp": tu.header.timestamp or None,
        "alerts_timestamp": (sa.header.timestamp or None) if sa is not None else None,
        "totals": totals,
        "brief": _brief(totals, area_out, chosen),
        "areas": area_out,
        "hotspots": chosen,
        "vehicles": vehicles,
    }


def _spark(hist: deque, now: int) -> list:
    """Andel sena senaste timmen, ett värde per 5 min (None där data saknas)."""
    out = []
    for k in range(12, 0, -1):
        lo, hi = now - k * 300, now - (k - 1) * 300
        vals = [s for t, s in hist if lo < t <= hi]
        out.append(round(statistics.mean(vals), 3) if vals else None)
    return out


def _pct(x: float) -> str:
    return f"{int(x * 100 + 0.5)} %"   # samma avrundning som i webbläsaren


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " och " + names[-1]


def _brief(t: dict, areas: list[dict], hot: list[dict]) -> list[str]:
    """Några meningar som går att läsa upp eller skriva vidare."""
    out = []
    if not t["buses"]:
        return ["Ingen realtidsinformation för bussar just nu."]
    general = t["share5"] >= 0.25
    out.append(f"{t['buses']} bussar i trafik med realtid ({t['planned']} enligt tidtabell). "
               f"{_pct(t['share5'])} är minst 5 minuter sena"
               + (" – förseningar i stora delar av länet." if general else "."))
    major = [a for a in areas if a["status"] == "major"]
    minor = [a for a in areas if a["status"] == "minor"]

    def describe(a):
        bits = []
        if a["buses"] and a["late5"]:
            bits.append(f"{_pct(a['share5'])} sena")
        if a["delay_alerts"]:
            bits.append(f"{a['delay_alerts']} larm om försenade turer")
        if a["cancelled"]:
            bits.append(f"{a['cancelled']} inställd{'a' if a['cancelled'] > 1 else ''} tur{'er' if a['cancelled'] > 1 else ''}")
        if a["reasons"]:
            bits.append("främst " + a["reasons"][0][0])
        return f"{a['name']} ({', '.join(bits)})" if bits else a["name"]

    if major:
        out.append("Stora förseningar: " + _join([describe(a) for a in major[:4]]) + ".")
    if minor:
        out.append("Förseningar: " + _join([describe(a) for a in minor[:5]]) + ".")
    if not major and not minor:
        out.append("Inga områden sticker ut just nu.")
    if hot:
        out.append("Flest sena bussar kring " + _join([f"{h['name']} ({h['area']}, {h['late']} bussar, snitt {round(h['avg'])} min)"
                                                       for h in hot[:3]]) + ".")
    new = {x["title"]: a["name"] for a in areas for x in a["new_alerts"]}
    if new:
        out.append("Nya meddelanden: " + _join([f"{title} ({area})" for title, area in list(new.items())[:3]]) + ".")
    return out
