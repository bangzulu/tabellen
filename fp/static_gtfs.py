"""Hämtar och läser GTFS Regional Static för SL, filtrerat på spårtrafiken i TABS.

Bronze-nyckeln tillåter bara 50 nedladdningar per månad, så zip-filen laddas ner
högst en gång per kalenderdygn och helst efter 07:00 (Trafiklab publicerar 03–07).
"""
from __future__ import annotations

import csv
import io
import logging
import pickle
import sys
import zipfile
from array import array
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)
ZIP_PATH = config.DATA_DIR / f"{config.OPERATOR}.zip"


@dataclass(slots=True)
class StopTime:
    seq: int
    stop_id: str
    arr: int | None  # sekunder från tjänstedygnets start, kan vara > 24h
    dep: int | None


NO_TIME = -1  # saknad tid i arrayerna


@dataclass
class Trip:
    """Hållplatstiderna lagras kompakt (arrayer i stället för ett objekt per hållplats),
    så att tre dygns spårtrafik ryms i en liten maskin. `stops` bygger objekten vid behov."""
    trip_id: str
    route_id: str
    line: str
    tab: str
    headsign: str
    direction_id: int | None
    train_number: str
    service_id: str
    stop_ids: tuple[str, ...] = ()
    seqs: array = field(default_factory=lambda: array("i"))
    arrs: array = field(default_factory=lambda: array("i"))
    deps: array = field(default_factory=lambda: array("i"))

    @property
    def stops(self) -> list[StopTime]:
        return [StopTime(self.seqs[i], sid,
                         None if self.arrs[i] == NO_TIME else self.arrs[i],
                         None if self.deps[i] == NO_TIME else self.deps[i])
                for i, sid in enumerate(self.stop_ids)]

    def set_stops(self, rows: list[tuple[int, str, int | None, int | None]]) -> None:
        rows.sort(key=lambda r: r[0])
        self.stop_ids = tuple(sys.intern(r[1]) for r in rows)
        self.seqs = array("i", (r[0] for r in rows))
        self.arrs = array("i", (NO_TIME if r[2] is None else r[2] for r in rows))
        self.deps = array("i", (NO_TIME if r[3] is None else r[3] for r in rows))

    @property
    def origin_stop_id(self) -> str | None:
        return self.stop_ids[0] if self.stop_ids else None

    @property
    def destination_stop_id(self) -> str | None:
        return self.stop_ids[-1] if self.stop_ids else None


@dataclass
class BusData:
    """Bussar, kompakt: bara det som behövs för översikten (ingen hållplatstid per tur).
    Förseningen kommer direkt ur realtidsflödet, positionen från senast passerade hållplats."""
    areas: list[str] = field(default_factory=list)          # områdesnamn, index = area-id
    route_ids: list[str] = field(default_factory=list)
    route_index: dict[str, int] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)          # linjenummer per linjeindex
    kinds: list[str] = field(default_factory=list)          # blåbuss, Närtrafiken, Ersättningsbuss eller ""
    home_area: list[int] = field(default_factory=list)      # område där linjen har flest hållplatsbesök
    services: list[str] = field(default_factory=list)
    trip_index: dict[str, int] = field(default_factory=dict)
    trip_route: array = field(default_factory=lambda: array("i"))
    trip_service: array = field(default_factory=lambda: array("i"))
    trip_dep: array = field(default_factory=lambda: array("i"))   # första avgång (s från tjänstedygnets start)
    trip_arr: array = field(default_factory=lambda: array("i"))   # sista ankomst
    stop_index: dict[str, int] = field(default_factory=dict)
    stop_name: list[str] = field(default_factory=list)
    stop_lat: array = field(default_factory=lambda: array("f"))
    stop_lon: array = field(default_factory=lambda: array("f"))
    stop_area: array = field(default_factory=lambda: array("h"))


def is_bus_route_type(route_type: int) -> bool:
    return route_type == 3 or 700 <= route_type <= 799


@dataclass
class StaticData:
    trips: dict[str, Trip]
    stop_names: dict[str, str]
    service_dates: dict[str, set[str]]  # service_id -> {"YYYYMMDD", ...}
    routes: list[dict]  # alla rader i routes.txt, för coverage_check --routes
    loaded_from: str = ""
    rail_routes: dict[str, tuple[str, str]] = field(default_factory=dict)  # route_id -> (linje, flik)
    stop_tabs: dict[str, set[str]] = field(default_factory=dict)  # stop_id/parent -> flikar som stannar där
    stop_lat: dict[str, float] = field(default_factory=dict)  # för att lägga norr överst i linjevyn
    bus: BusData = field(default_factory=BusData)

    def stop_name(self, stop_id: str | None) -> str:
        if not stop_id:
            return ""
        return self.stop_names.get(stop_id, stop_id)

    def trips_on(self, service_date: str) -> list[Trip]:
        return [t for t in self.trips.values() if service_date in self.service_dates.get(t.service_id, ())]


def parse_gtfs_time(value: str) -> int | None:
    if not value:
        return None
    h, m, s = (int(x) for x in value.strip().split(":"))
    return h * 3600 + m * 60 + s


def service_day_start(service_date: str) -> datetime:
    """GTFS: tider räknas från 'mitt på dagen minus 12 h', vilket blir rätt även vid sommartidsbyte."""
    d = datetime.strptime(service_date, "%Y%m%d").date()
    noon = datetime.combine(d, time(12, 0), tzinfo=TZ)
    return noon - timedelta(hours=12)


def to_epoch(service_date: str, seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return int((service_day_start(service_date) + timedelta(seconds=seconds)).timestamp())


# --- Nedladdning ---------------------------------------------------------------

def _downloaded_today(path: Path, now: datetime) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, TZ)
    return mtime.date() == now.date()


def ensure_static_zip(force: bool = False) -> Path:
    now = datetime.now(TZ)
    if not force and ZIP_PATH.exists():
        if _downloaded_today(ZIP_PATH, now):
            return ZIP_PATH
        if now.time() < time(7, 0):
            # Gårdagens fil räcker tills dagens data säkert är publicerad.
            return ZIP_PATH
    if not config.GTFS_STATIC_KEY:
        if ZIP_PATH.exists():
            log.warning("GTFS_STATIC_KEY saknas, använder befintlig %s", ZIP_PATH)
            return ZIP_PATH
        raise RuntimeError("GTFS_STATIC_KEY saknas i .env och ingen lokal sl.zip finns.")

    log.info("Laddar ner statisk GTFS för %s ...", config.OPERATOR)
    resp = requests.get(
        config.STATIC_URL,
        params={"key": config.GTFS_STATIC_KEY},
        headers={"Accept-Encoding": "gzip"},
        timeout=120,
        stream=True,
    )
    resp.raise_for_status()
    tmp = ZIP_PATH.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            fh.write(chunk)
    tmp.replace(ZIP_PATH)
    return ZIP_PATH


# --- Inläsning -----------------------------------------------------------------

def _reader(zf: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig"))


def load_static(path: Path | None = None, dates: set[str] | None = None) -> StaticData:
    """dates: läs bara in turer som går dessa tjänstedygn (YYYYMMDD). Sparar mycket minne;
    hela tidtabellen tar ~200 MB, tre dygn en bråkdel. None = alla turer."""
    path = path or ensure_static_zip()
    with zipfile.ZipFile(path) as zf:
        service_dates: dict[str, set[str]] = {}
        if "calendar_dates.txt" in zf.namelist():
            for cd in _reader(zf, "calendar_dates.txt"):
                if cd.get("exception_type", "1") == "1" and (dates is None or cd["date"] in dates):
                    service_dates.setdefault(cd["service_id"], set()).add(cd["date"])

        all_routes = list(_reader(zf, "routes.txt"))
        routes: dict[str, tuple[str, str]] = {}  # route_id -> (line, tab)
        bus = BusData()
        for r in all_routes:
            rtype = int(r.get("route_type") or 0)
            line = (r.get("route_short_name") or "").strip()
            if is_bus_route_type(rtype):
                bus.route_index[r["route_id"]] = len(bus.route_ids)
                bus.route_ids.append(r["route_id"])
                bus.lines.append(line)
                bus.kinds.append((r.get("route_desc") or "").strip())
                continue
            if config.is_boat_route_type(rtype):
                if line:
                    routes[r["route_id"]] = (line, config.BOAT_TAB)
                continue
            if not config.is_rail_route_type(rtype):
                continue
            tab = config.LINE_TO_TAB.get(line)
            if tab:
                routes[r["route_id"]] = (line, tab)

        trips: dict[str, Trip] = {}
        service_idx: dict[str, int] = {}
        for t in _reader(zf, "trips.txt"):
            if dates is not None and t["service_id"] not in service_dates:
                continue
            ridx = bus.route_index.get(t["route_id"])
            if ridx is not None:
                if t["service_id"] not in service_idx:
                    service_idx[t["service_id"]] = len(bus.services)
                    bus.services.append(t["service_id"])
                bus.trip_index[t["trip_id"]] = len(bus.trip_route)
                bus.trip_route.append(ridx)
                bus.trip_service.append(service_idx[t["service_id"]])
                bus.trip_dep.append(NO_TIME)
                bus.trip_arr.append(NO_TIME)
                continue
            if t["route_id"] not in routes:
                continue
            line, tab = routes[t["route_id"]]
            direction = t.get("direction_id")
            trips[t["trip_id"]] = Trip(
                trip_id=t["trip_id"],
                route_id=t["route_id"],
                line=line,
                tab=tab,
                headsign=(t.get("trip_headsign") or "").strip(),
                direction_id=int(direction) if direction not in (None, "") else None,
                # SL lämnar trip_short_name tomt; tågnumret finns i samtrafiken_internal_trip_number.
                train_number=((t.get("trip_short_name") or "").strip()
                              or (t.get("samtrafiken_internal_trip_number") or "").strip()
                              or line),
                service_id=t["service_id"],
            )

        # Hållplatser: namn, förälder, koordinater
        stops_raw = {s["stop_id"]: (s["stop_name"], s.get("parent_station") or "", s.get("stop_lat") or "",
                                    s.get("stop_lon") or "")
                     for s in _reader(zf, "stops.txt")}

        from .areas import area_of
        area_idx: dict[str, int] = {}

        def bus_stop(sid: str) -> int:
            i = bus.stop_index.get(sid)
            if i is None:
                name, parent, lat, lon = stops_raw.get(sid, (sid, "", "", ""))
                lat_f, lon_f = float(lat or 0), float(lon or 0)
                area = area_of(lat_f, lon_f) if lat else "Okänt område"
                if area not in area_idx:
                    area_idx[area] = len(bus.areas)
                    bus.areas.append(area)
                i = bus.stop_index[sys.intern(sid)] = len(bus.stop_name)
                bus.stop_name.append(sys.intern(stops_raw[parent][0] if parent in stops_raw else name))
                bus.stop_lat.append(lat_f)
                bus.stop_lon.append(lon_f)
                bus.stop_area.append(area_idx[area])
            return i

        # stop_times.txt är stor (alla bussar). Strömmas; spårturer sparas med tider,
        # bussturer bara med första/sista tid och vilka områden linjen trafikerar.
        pending: dict[str, list] = {}
        route_area_visits: dict[tuple[int, int], int] = {}
        for st in _reader(zf, "stop_times.txt"):
            bi = bus.trip_index.get(st["trip_id"])
            if bi is not None:
                dep = parse_gtfs_time(st.get("departure_time", "")) or parse_gtfs_time(st.get("arrival_time", ""))
                arr = parse_gtfs_time(st.get("arrival_time", "")) or dep
                if dep is not None and (bus.trip_dep[bi] == NO_TIME or dep < bus.trip_dep[bi]):
                    bus.trip_dep[bi] = dep
                if arr is not None and arr > bus.trip_arr[bi]:
                    bus.trip_arr[bi] = arr
                key = (bus.trip_route[bi], bus.stop_area[bus_stop(st["stop_id"])])
                route_area_visits[key] = route_area_visits.get(key, 0) + 1
                continue
            if st["trip_id"] not in trips:
                continue
            pending.setdefault(st["trip_id"], []).append((
                int(st["stop_sequence"]), st["stop_id"],
                parse_gtfs_time(st.get("arrival_time", "")),
                parse_gtfs_time(st.get("departure_time", "")),
            ))
        for tid, rows in pending.items():
            trips[tid].set_stops(rows)
        del pending

        # Linjens hemområde: där den har flest hållplatsbesök
        best: dict[int, tuple[int, int]] = {}
        for (ridx, aidx), n in route_area_visits.items():
            if ridx not in best or n > best[ridx][1]:
                best[ridx] = (aidx, n)
        bus.home_area = [best.get(i, (-1, 0))[0] for i in range(len(bus.route_ids))]
        del route_area_visits

        # Vilka flikar trafikerar varje hållplats (både plattform och föräldrastation),
        # används för att koppla störningar som bara anger hållplats.
        stop_tabs: dict[str, set[str]] = {}
        for trip in trips.values():
            for sid in trip.stop_ids:
                for key in (sid, stops_raw.get(sid, ("", "", "", ""))[1]):
                    if key:
                        stop_tabs.setdefault(sys.intern(key), set()).add(trip.tab)
        stop_names, stop_lat = {}, {}
        for sid in stop_tabs:
            name, parent, lat, _lon = stops_raw.get(sid, (sid, "", "", ""))
            stop_names[sid] = stops_raw[parent][0] if parent in stops_raw else name
            try:
                stop_lat[sid] = float(lat)
            except ValueError:
                pass
        del stops_raw

        needed = {t.service_id for t in trips.values()} | set(bus.services)
        service_dates = {k: v for k, v in service_dates.items() if k in needed}

    log.info("Statisk data: %d spårturer, %d linjer; %d bussturer, %d busslinjer, %d busshållplatser",
             len(trips), len(routes), len(bus.trip_route), len(bus.route_ids), len(bus.stop_name))
    return StaticData(trips=trips, stop_names=stop_names, service_dates=service_dates,
                      routes=all_routes, loaded_from=str(path),
                      rail_routes={rid: (line, tab) for rid, (line, tab) in routes.items()},
                      stop_tabs=stop_tabs, stop_lat=stop_lat, bus=bus)


CACHE_VERSION = 6  # höj när StaticData ändras


def load_static_cached(dates: set[str]) -> StaticData:
    """Som load_static men sparar resultatet som pickle bredvid zip-filen.
    Inläsning från zip tar 5–15 s, från cachen under en sekund."""
    path = ensure_static_zip()
    key = f"static-v{CACHE_VERSION}-{int(path.stat().st_mtime)}-{min(dates)}-{max(dates)}.pkl"
    cache = config.DATA_DIR / key
    if cache.exists():
        try:
            with cache.open("rb") as fh:
                data = pickle.load(fh)
            log.info("Statisk data från cache %s", key)
            return data
        except Exception:  # trasig eller inaktuell cache, läs om
            log.warning("Kunde inte läsa %s, läser zip igen", key, exc_info=True)
    data = load_static(path, dates=dates)
    for old in config.DATA_DIR.glob("static-*.pkl"):
        old.unlink(missing_ok=True)
    tmp = cache.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cache)
    return data


def today_service_date(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y%m%d")


def window_dates(now: datetime | None = None) -> set[str]:
    """Igår, idag och imorgon. Imorgon behövs för att klara natten fram till nästa omladdning."""
    now = now or datetime.now(TZ)
    return {(now.date() + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)}


def candidate_service_dates(now: datetime | None = None) -> list[str]:
    """Turer efter midnatt tillhör gårdagens tjänstedygn."""
    now = now or datetime.now(TZ)
    return [now.strftime("%Y%m%d"), (now.date() - timedelta(days=1)).strftime("%Y%m%d")]

