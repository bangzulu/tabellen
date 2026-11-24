"""Tester med syntetisk GTFS + GTFS-RT, så logiken kan utvecklas utan API-nyckel."""
import io
import zipfile

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from fp.board import build_rows, group_for_tab
from fp.static_gtfs import load_static, to_epoch

SD = "20261105"


def _csv(rows):
    return "\n".join(",".join(r) for r in rows) + "\n"


@pytest.fixture
def static(tmp_path):
    files = {
        "routes.txt": _csv([["route_id", "route_short_name", "route_long_name", "route_type"],
                            ["R43", "43", "Pendeltåg 43", "106"],
                            ["B7", "7", "Buss 7", "700"],      # ska filtreras bort
                            ["T10", "10", "Blå linjen", "401"],
                            ["F10", "10", "Båtlinje 10", "1000"]]),   # båt: egen flik, inte blå linjen
        "trips.txt": _csv([["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "trip_short_name"],
                           ["R43", "S1", "t1", "Nynäshamn", "0", "2335"],
                           ["R43", "S1", "t2", "Bålsta", "1", "2340"],
                           ["R43", "S1", "t3", "Nynäshamn", "0", "2337"],
                           ["B7", "S1", "b1", "Buss", "0", ""],
                           ["T10", "S1", "m1", "Kungsträdgården", "1", ""],
                           ["F10", "S1", "f1", "Nacka strand", "0", ""]]),
        "stops.txt": _csv([["stop_id", "stop_name", "parent_station"],
                           ["A", "Odenplan", ""], ["B", "Stockholm City", ""], ["C", "Älvsjö", ""],
                           ["C1", "Älvsjö spår 1", "C"], ["D", "Västra skogen", ""], ["E", "Kungsträdgården", ""]]),
        "stop_times.txt": _csv([["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                                ["t1", "13:30:00", "13:30:00", "A", "1"], ["t1", "13:34:00", "13:35:00", "B", "2"],
                                ["t1", "13:45:00", "13:45:00", "C1", "3"],
                                ["t2", "13:30:00", "13:30:00", "C1", "1"], ["t2", "13:40:00", "13:40:00", "B", "2"],
                                ["t2", "13:45:00", "13:45:00", "A", "3"],
                                ["t3", "13:30:00", "13:30:00", "A", "1"], ["t3", "13:35:00", "13:35:00", "B", "2"],
                                ["t3", "13:45:00", "13:45:00", "C", "3"],
                                ["m1", "13:30:00", "13:30:00", "D", "1"], ["m1", "13:50:00", "13:50:00", "E", "2"],
                                ["b1", "13:30:00", "13:30:00", "A", "1"]]),
        "calendar_dates.txt": _csv([["service_id", "date", "exception_type"], ["S1", SD, "1"]]),
    }
    path = tmp_path / "sl.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return load_static(path)


def _feed(now):
    f = rt.FeedMessage()
    f.header.gtfs_realtime_version = "2.0"
    f.header.timestamp = now
    return f


def _tu(feed, trip_id, updates, cancelled=False):
    e = feed.entity.add(id=trip_id)
    e.trip_update.trip.trip_id = trip_id
    e.trip_update.trip.start_date = SD
    if cancelled:
        e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED
    for seq, delay in updates:
        s = e.trip_update.stop_time_update.add(stop_sequence=seq)
        s.departure.delay = delay
        s.arrival.delay = delay


def test_filters_and_maps(static):
    assert set(static.trips) == {"t1", "t2", "t3", "m1", "f1"}  # buss 7 bortfiltrerad
    assert static.trips["f1"].tab == "BAT" and static.trips["m1"].tab == "TB3"
    assert static.trips["t1"].tab == "PT" and static.trips["m1"].tab == "TB3"
    assert static.trips["t1"].train_number == "2335"
    assert static.trips["m1"].train_number == "10"  # fallback till linjenummer
    assert static.stop_name("C1") == "Älvsjö"       # namn från parent_station


def test_delay_and_observed_stop(static):
    now = to_epoch(SD, 13 * 3600 + 42 * 60)
    feed = _feed(now)
    _tu(feed, "t1", [(1, 0), (2, 300), (3, 300)])   # 5 min sen från City
    rows = build_rows(static, feed, now=now)
    r = next(r for r in rows if r.trip_id == "t1")
    assert r.observed_stop == "Stockholm City"
    assert r.delay_min == 5
    assert r.next_stop == "Älvsjö"
    assert r.passed_at == to_epoch(SD, 13 * 3600 + 40 * 60)


def test_feed_without_passed_stops_is_inferred(static):
    now = to_epoch(SD, 13 * 3600 + 38 * 60)
    feed = _feed(now)
    _tu(feed, "t2", [(3, 120)])                     # bara kommande hållplats finns kvar
    r = build_rows(static, feed, now=now)[0]
    assert r.observed_source == "tripupdate-inferred"
    assert r.observed_stop == "Stockholm City"
    assert r.delay_min == 2


def test_cancelled_trip_is_shown(static):
    now = to_epoch(SD, 13 * 3600 + 40 * 60)
    feed = _feed(now)
    _tu(feed, "t3", [], cancelled=True)
    r = build_rows(static, feed, now=now)[0]
    assert r.status == "cancelled" and r.delay_min is None


def test_finished_trip_hidden_after_grace(static):
    now = to_epoch(SD, 13 * 3600 + 50 * 60)
    feed = _feed(now)
    _tu(feed, "t1", [(1, 0), (2, 0), (3, 0)])
    assert build_rows(static, feed, now=now) == []


def test_vehicle_position_fallback(static):
    now = to_epoch(SD, 13 * 3600 + 40 * 60)
    tu = _feed(now)
    vp = _feed(now)
    v = vp.entity.add(id="v1").vehicle
    v.trip.trip_id = "m1"
    v.stop_id = "E"
    v.current_status = rt.VehiclePosition.IN_TRANSIT_TO
    v.timestamp = now - 5
    r = build_rows(static, tu, vp, now=now)[0]
    assert r.observed_stop == "Västra skogen" and r.next_stop == "Kungsträdgården"
    assert r.observed_source == "vehicleposition"


def test_grouping(static):
    now = to_epoch(SD, 13 * 3600 + 42 * 60)
    feed = _feed(now)
    _tu(feed, "t1", [(2, 0)])
    _tu(feed, "t2", [(2, 0)])
    groups = group_for_tab(build_rows(static, feed, now=now), "PT")
    assert [g["label"] for g in groups] == ["Riktning 1", "Riktning 2"]
    assert groups[0]["destinations"] == ["Nynäshamn"]


def test_line_patterns(static):
    from fp.board import base_line, line_patterns
    assert base_line("27S", ["27", "27S"]) == "27"
    assert base_line("42X", ["42X", "43"]) == "42X"
    pats = line_patterns(static)
    pt = {p["line"]: p for p in pats["PT"]}
    assert pt["43"]["stations"] == ["Odenplan", "Stockholm City", "Älvsjö"]   # parent-namn, i turordning
    assert pt["43"]["down_direction_id"] in (0, 1)
