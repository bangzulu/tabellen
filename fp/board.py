"""Bygger tabellraderna (motsvarar kitt.sl.se:s Trips) av statisk GTFS + GTFS-RT.

Kolumnmappning mot kitt:
  Avvikelse        -> delay_min   (positivt = sen, GTFS-standard. kitt visar sen som negativt)
  Destination      -> destination (trip_headsign, annars sista hållplatsen)
  Avläsningsplats  -> observed_stop (senast passerade hållplats)
  Avläsningstid    -> passed_at   (när tåget passerade, inte när posten uppdaterades)
  Ordinarie tid    -> scheduled_at (tidtabellstid vid avläsningsplatsen)
  Tågnummer        -> train_number (trip_short_name)
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

from google.transit import gtfs_realtime_pb2 as rt

from .static_gtfs import StaticData, Trip, candidate_service_dates, to_epoch

TU = rt.TripUpdate
VP = rt.VehiclePosition
FINISHED_GRACE_S = 120      # visa tåg vid ändhållplats i 2 min
CANCELLED_WINDOW_S = 30 * 60  # visa inställda turer som skulle gått inom ±30 min


@dataclass
class Row:
    tab: str
    line: str
    direction_id: int | None
    destination: str
    observed_stop: str
    observed_source: str        # "tripupdate" | "tripupdate-inferred" | "vehicleposition" | "schedule"
    passed_at: int | None
    scheduled_at: int | None
    delay_min: int | None
    next_stop: str
    next_expected_at: int | None
    train_number: str
    trip_id: str
    status: str                 # "running" | "terminus" | "cancelled"
    updated_at: int | None
    notes: list[str] = field(default_factory=list)  # störningstexter för just den här avgången


def _stu_time(ev) -> tuple[int | None, int | None]:
    """(tid, försening) från en StopTimeEvent, om de finns."""
    t = ev.time if ev.HasField("time") and ev.time else None
    d = ev.delay if ev.HasField("delay") else None
    return t, d


def _actual_times(trip: Trip, service_date: str, tu) -> list[dict]:
    """Beräknar faktisk/förväntad tid per hållplats. Försening propageras nedströms
    enligt GTFS-RT-specen när en hållplats saknar egen uppdatering."""
    by_seq = {s.stop_sequence: s for s in tu.stop_time_update if s.HasField("stop_sequence")}
    by_stop = {s.stop_id: s for s in tu.stop_time_update if s.stop_id}
    carried_delay: int | None = tu.delay if tu.HasField("delay") else None
    out = []
    for st in trip.stops:
        stu = by_seq.get(st.seq) or by_stop.get(st.stop_id)
        sched_arr = to_epoch(service_date, st.arr)
        sched_dep = to_epoch(service_date, st.dep) or sched_arr
        skipped = False
        arr = dep = None
        if stu is not None:
            skipped = stu.schedule_relationship == TU.StopTimeUpdate.SKIPPED
            at, ad = _stu_time(stu.arrival) if stu.HasField("arrival") else (None, None)
            dt, dd = _stu_time(stu.departure) if stu.HasField("departure") else (None, None)
            if ad is not None or dd is not None:
                carried_delay = dd if dd is not None else ad
            arr = at or (sched_arr + ad if ad is not None and sched_arr else None)
            dep = dt or (sched_dep + dd if dd is not None and sched_dep else None)
            if dep and sched_dep and dd is None:
                carried_delay = dep - sched_dep
            elif arr and sched_arr and ad is None and carried_delay is None:
                carried_delay = arr - sched_arr
        if arr is None and dep is None and carried_delay is not None:
            arr = sched_arr + carried_delay if sched_arr else None
            dep = sched_dep + carried_delay if sched_dep else None
        out.append({
            "stop_id": st.stop_id, "sched_arr": sched_arr, "sched_dep": sched_dep,
            "arr": arr, "dep": dep or arr, "skipped": skipped,
            "has_rt": stu is not None,
        })
    return out


def _pick_service_date(trip: Trip, static: StaticData, start_date: str, now: int) -> str | None:
    if start_date:
        return start_date
    for sd in candidate_service_dates():
        if sd in static.service_dates.get(trip.service_id, ()):
            first = to_epoch(sd, trip.stops[0].dep) if trip.stops else None
            last = to_epoch(sd, trip.stops[-1].arr) if trip.stops else None
            if first and last and first - 3600 <= now <= last + 3 * 3600:
                return sd
    return None


def _vp_index(vp_feed) -> dict[str, object]:
    idx = {}
    if vp_feed is None:
        return idx
    for e in vp_feed.entity:
        if e.HasField("vehicle") and e.vehicle.trip.trip_id:
            idx[e.vehicle.trip.trip_id] = e.vehicle
    return idx


def _from_vehicle_position(trip: Trip, static: StaticData, v) -> tuple[str, str] | None:
    """Avläsningsplats ur VehiclePosition: STOPPED_AT X -> X, annars föregående hållplats."""
    if not v.stop_id:
        return None
    ids = [s.stop_id for s in trip.stops]
    if v.stop_id not in ids:
        return None
    i = ids.index(v.stop_id)
    if v.current_status == VP.STOPPED_AT or i == 0:
        return v.stop_id, (ids[i + 1] if i + 1 < len(ids) else "")
    return ids[i - 1], v.stop_id


def build_rows(static: StaticData, tu_feed, vp_feed=None, now: int | None = None,
               notes: dict[str, list[str]] | None = None) -> list[Row]:
    now = int(now or time.time())
    vps = _vp_index(vp_feed)
    rows: list[Row] = []
    seen: set[str] = set()
    header_ts = tu_feed.header.timestamp or None

    for e in tu_feed.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        trip = static.trips.get(tu.trip.trip_id)
        if trip is None or not trip.stops:
            continue
        sd = _pick_service_date(trip, static, tu.trip.start_date, now)
        if sd is None:
            continue
        seen.add(trip.trip_id)
        updated = tu.timestamp or header_ts
        dest = trip.headsign or static.stop_name(trip.destination_stop_id)

        if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            first = to_epoch(sd, trip.stops[0].dep)
            if first and abs(first - now) <= CANCELLED_WINDOW_S:
                rows.append(Row(trip.tab, trip.line, trip.direction_id, dest,
                                static.stop_name(trip.origin_stop_id), "schedule", None, first, None,
                                "", None, trip.train_number, trip.trip_id, "cancelled", updated))
            continue

        times = _actual_times(trip, sd, tu)
        passed = [i for i, t in enumerate(times) if not t["skipped"] and t["dep"] and t["dep"] <= now]
        source = "tripupdate"
        first_rt = next((k for k, t in enumerate(times) if t["has_rt"]), None)
        if passed:
            i = passed[-1]
            obs = times[i]
            nxt = next((t for t in times[i + 1:] if not t["skipped"]), None)
        elif first_rt:
            # Flödet har tagit bort passerade hållplatser: hållplatsen före den första
            # uppdaterade är senast passerade. Förseningen tas från den uppdaterade.
            i = first_rt - 1
            ref = times[first_rt]
            d = (ref["dep"] - ref["sched_dep"]) if ref["dep"] and ref["sched_dep"] else None
            obs = dict(times[i])
            if d is not None and obs["sched_dep"]:
                obs["dep"] = obs["sched_dep"] + d
                obs["arr"] = (obs["sched_arr"] or obs["sched_dep"]) + d
            nxt = ref
            source = "tripupdate-inferred"
        else:
            v = vps.get(trip.trip_id)
            hit = _from_vehicle_position(trip, static, v) if v is not None else None
            if not hit:
                continue  # turen har inte startat
            ids = [s.stop_id for s in trip.stops]
            i = ids.index(hit[0])
            obs, nxt = times[i], (times[i + 1] if i + 1 < len(times) else None)
            source = "vehicleposition"

        is_last = nxt is None
        if is_last and obs["arr"] and obs["arr"] < now - FINISHED_GRACE_S:
            continue

        sched = obs["sched_dep"] if not is_last else obs["sched_arr"]
        actual = obs["dep"] if not is_last else obs["arr"]
        delay = int((actual - sched) / 60) if actual and sched else None

        rows.append(Row(
            tab=trip.tab, line=trip.line, direction_id=trip.direction_id, destination=dest,
            observed_stop=static.stop_name(obs["stop_id"]), observed_source=source,
            passed_at=actual if source.startswith("tripupdate") else (vps[trip.trip_id].timestamp or None),
            scheduled_at=sched, delay_min=delay,
            next_stop=static.stop_name(nxt["stop_id"]) if nxt else "",
            next_expected_at=(nxt["arr"] or nxt["dep"]) if nxt else None,
            train_number=trip.train_number, trip_id=trip.trip_id,
            status="terminus" if is_last else "running", updated_at=updated,
        ))

    # Fordon som bara finns i VehiclePositions
    for trip_id, v in vps.items():
        if trip_id in seen:
            continue
        trip = static.trips.get(trip_id)
        if trip is None:
            continue
        hit = _from_vehicle_position(trip, static, v)
        if not hit:
            continue
        rows.append(Row(trip.tab, trip.line, trip.direction_id,
                        trip.headsign or static.stop_name(trip.destination_stop_id),
                        static.stop_name(hit[0]), "vehicleposition", v.timestamp or None, None, None,
                        static.stop_name(hit[1]), None, trip.train_number, trip_id, "running",
                        v.timestamp or None))
    if notes:
        for r in rows:
            r.notes = notes.get(r.trip_id, [])
    return rows


def group_for_tab(rows: list[Row], tab: str) -> list[dict]:
    """Grupperar per direction_id. Etiketten kompletteras med vanligaste destinationerna,
    eftersom 'Riktning 1/2' ensamt är svårt att tolka."""
    groups: dict[int | None, list[Row]] = defaultdict(list)
    for r in rows:
        if r.tab == tab:
            groups[r.direction_id].append(r)
    out = []
    for direction in sorted(groups, key=lambda d: (d is None, d)):
        items = sorted(groups[direction], key=lambda r: (r.line, r.scheduled_at or 0))
        top = [d for d, _ in Counter(r.destination for r in items).most_common(3)]
        out.append({
            "direction_id": direction,
            "label": f"Riktning {direction + 1}" if direction is not None else "Okänd riktning",
            "destinations": top,
            "rows": [asdict(r) for r in items],
        })
    return out


def base_line(line: str, tab_lines: list[str]) -> str:
    """'27S' -> '27', '43X' -> '43' om grundlinjen finns i fliken, annars linjen själv."""
    base = line.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return base if base and base != line and base in tab_lines else line


def line_patterns(static: StaticData) -> dict[str, list[dict]]:
    """Stationsordning per linje och flik för linjevyn. Varje linje ritas efter sin längsta tur
    (varianter som 27S och 43X slås ihop med grundlinjen) med nordligaste änden överst."""
    from . import config

    best: dict[tuple[str, str], Trip] = {}
    variants: dict[tuple[str, str], set[str]] = defaultdict(set)
    for t in static.trips.values():
        key = (t.tab, base_line(t.line, config.TAB_BY_CODE[t.tab]["lines"]))
        variants[key].add(t.line)
        if key not in best or len(t.stop_ids) > len(best[key].stop_ids):
            best[key] = t

    out: dict[str, list[dict]] = defaultdict(list)
    for (tab, line), trip in best.items():
        names: list[str] = []
        for sid in trip.stop_ids:
            name = static.stop_name(sid)
            if not names or names[-1] != name:
                names.append(name)
        first, last = trip.stop_ids[0], trip.stop_ids[-1]
        flip = static.stop_lat.get(first, 0) < static.stop_lat.get(last, 0)
        down = trip.direction_id
        if flip:
            names.reverse()
            down = None if down is None else 1 - down
        out[tab].append({
            "line": line,
            "variants": sorted(variants[(tab, line)], key=lambda x: (len(x), x)),
            "stations": names,
            "down_direction_id": down,   # direction_id för tåg som går uppifrån och ned
        })
    for tab in out:
        out[tab].sort(key=lambda d: (len(d["line"]), d["line"]))
    return dict(out)


def planned_counts(static: StaticData, now: int) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Antal turer som enligt tidtabellen är ute just nu (mellan första avgång och sista
    ankomst), per flik och per (flik, grundlinje). Jämförs med realtidens antal."""
    from . import config
    from datetime import datetime

    from .static_gtfs import NO_TIME, TZ, candidate_service_dates, service_day_start

    per_tab: dict[str, int] = defaultdict(int)
    per_line: dict[tuple[str, str], int] = defaultdict(int)
    for sd in candidate_service_dates(datetime.fromtimestamp(now, TZ)):
        day0 = int(service_day_start(sd).timestamp())
        for t in static.trips.values():
            if not t.stop_ids or sd not in static.service_dates.get(t.service_id, ()):
                continue
            dep = t.deps[0] if t.deps[0] != NO_TIME else t.arrs[0]
            arr = t.arrs[-1] if t.arrs[-1] != NO_TIME else t.deps[-1]
            if dep == NO_TIME or arr == NO_TIME or not (day0 + dep <= now <= day0 + arr):
                continue
            per_tab[t.tab] += 1
            per_line[(t.tab, base_line(t.line, config.TAB_BY_CODE[t.tab]["lines"]))] += 1
    return dict(per_tab), dict(per_line)
