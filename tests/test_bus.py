"""Bussöversikten: status per område och sammanställning från syntetiska flöden."""
from google.transit import gtfs_realtime_pb2 as rt

from fp.bus import build_bus_overview, status_of
from fp.static_gtfs import to_epoch
from tests.test_board import SD, _feed, static  # noqa: F401


def test_status_thresholds():
    assert status_of(40, 5, 1, 0, 0, 0.13) == "normal"           # 12 % sena: vanligt läge
    assert status_of(40, 13, 2, 0, 0, 0.13) == "minor"           # 32 %
    assert status_of(40, 20, 4, 0, 0, 0.13) == "major"           # 50 %
    assert status_of(40, 13, 2, 0, 0, 0.30) == "normal"          # hela länet sent: området sticker inte ut
    assert status_of(4, 3, 0, 0, 0, 0.13) == "normal"            # för få bussar för andelsregeln
    assert status_of(20, 2, 0, 6, 0, 0.13) == "minor"            # många larm om försenade turer
    assert status_of(20, 2, 0, 0, 6, 0.13) == "major"            # många inställda


def test_overview_counts_bus_and_alert(static):
    now = to_epoch(SD, 13 * 3600 + 30 * 60)   # buss b1 avgår 13:30 från Odenplan
    tu = _feed(now)
    e = tu.entity.add(id="b1")
    e.trip_update.trip.trip_id = "b1"
    s = e.trip_update.stop_time_update.add(stop_sequence=1, stop_id="A")
    s.departure.time = now - 30
    s.departure.delay = 8 * 60
    sa = _feed(now)
    a = sa.entity.add(id="x").alert
    a.header_text.translation.add(text="Förseningar")
    a.description_text.translation.add(text="Försenad p.g.a. framkomlighetsproblem.")
    a.informed_entity.add().trip.trip_id = "b1"
    o = build_bus_overview(static, tu, sa, now=now)
    assert o["totals"]["buses"] == 1 and o["totals"]["late5"] == 1
    area = next(x for x in o["areas"] if x["buses"])
    assert area["delay_alerts"] == 1 and area["reasons"][0][0] == "framkomlighetsproblem"
    assert o["vehicles"][0][2] == 8.0
