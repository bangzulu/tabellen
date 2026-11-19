"""Hämtar och läser GTFS Regional Static för SL, filtrerat på spårtrafiken i TABS.

Bronze-nyckeln tillåter bara 50 nedladdningar per månad, så zip-filen laddas ner
högst en gång per kalenderdygn och helst efter 07:00 (Trafiklab publicerar 03–07).
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)
ZIP_PATH = config.DATA_DIR / f"{config.OPERATOR}.zip"


@dataclass
class StopTime:
    seq: int
    stop_id: str
    arr: int | None  # sekunder från tjänstedygnets start, kan vara > 24h
    dep: int | None


@dataclass
class Trip:
    trip_id: str
    route_id: str
    line: str
    tab: str
    headsign: str
    direction_id: int | None
    train_number: str
    service_id: str
    stops: list[StopTime] = field(default_factory=list)

    @property
    def origin_stop_id(self) -> str | None:
        return self.stops[0].stop_id if self.stops else None

    @property
    def destination_stop_id(self) -> str | None:
        return self.stops[-1].stop_id if self.stops else None


@dataclass
class StaticData:
    trips: dict[str, Trip]
    stop_names: dict[str, str]
    service_dates: dict[str, set[str]]  # service_id -> {"YYYYMMDD", ...}
    routes: list[dict]  # alla rader i routes.txt, för coverage_check --routes
    loaded_from: str = ""

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


def load_static(path: Path | None = None) -> StaticData:
    path = path or ensure_static_zip()
    with zipfile.ZipFile(path) as zf:
        all_routes = list(_reader(zf, "routes.txt"))
        routes: dict[str, tuple[str, str]] = {}  # route_id -> (line, tab)
        for r in all_routes:
            rtype = int(r.get("route_type") or 0)
            line = (r.get("route_short_name") or "").strip()
            if config.is_excluded_route_type(rtype):
                continue
            tab = config.LINE_TO_TAB.get(line)
            if tab:
                routes[r["route_id"]] = (line, tab)

        trips: dict[str, Trip] = {}
        for t in _reader(zf, "trips.txt"):
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
                # Trafiklab: trip_short_name = tågnummer, fallback route_short_name
                train_number=(t.get("trip_short_name") or "").strip() or line,
                service_id=t["service_id"],
            )

        # stop_times.txt är stor (alla bussar). Strömmas och filtreras.
        for st in _reader(zf, "stop_times.txt"):
            trip = trips.get(st["trip_id"])
            if trip is None:
                continue
            trip.stops.append(StopTime(
                seq=int(st["stop_sequence"]),
                stop_id=st["stop_id"],
                arr=parse_gtfs_time(st.get("arrival_time", "")),
                dep=parse_gtfs_time(st.get("departure_time", "")),
            ))
        for trip in trips.values():
            trip.stops.sort(key=lambda s: s.seq)

        stops_raw = {s["stop_id"]: s for s in _reader(zf, "stops.txt")}
        stop_names = {}
        for sid, s in stops_raw.items():
            parent = stops_raw.get(s.get("parent_station") or "")
            stop_names[sid] = (parent or s)["stop_name"]

        needed = {t.service_id for t in trips.values()}
        service_dates: dict[str, set[str]] = {}
        if "calendar_dates.txt" in zf.namelist():
            for cd in _reader(zf, "calendar_dates.txt"):
                if cd["service_id"] in needed and cd.get("exception_type", "1") == "1":
                    service_dates.setdefault(cd["service_id"], set()).add(cd["date"])

    log.info("Statisk data: %d turer, %d linjer", len(trips), len(routes))
    return StaticData(trips=trips, stop_names=stop_names, service_dates=service_dates,
                      routes=all_routes, loaded_from=str(path))


def today_service_date(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y%m%d")


def candidate_service_dates(now: datetime | None = None) -> list[str]:
    """Turer efter midnatt tillhör gårdagens tjänstedygn."""
    now = now or datetime.now(TZ)
    return [now.strftime("%Y%m%d"), (now.date() - timedelta(days=1)).strftime("%Y%m%d")]

