"""Tester för störningslogiken med syntetiska ServiceAlerts."""
from google.transit import gtfs_realtime_pb2 as rt

from fp.alerts import build_alerts
from fp.board import build_rows
from fp.static_gtfs import to_epoch
from tests.test_board import SD, _feed, _tu, static  # noqa: F401  (fixture)


def _alert(feed, aid, header, desc="", routes=(), stops=(), trips=(), start=0, end=0):
    e = feed.entity.add(id=aid)
    a = e.alert
    a.header_text.translation.add(text=header)
    if desc:
        a.description_text.translation.add(text=desc)
    for r in routes:
        a.informed_entity.add(route_id=r)
    for s in stops:
        a.informed_entity.add(stop_id=s)
    for t in trips:
        ie = a.informed_entity.add()
        ie.trip.trip_id = t
    if start or end:
        a.active_period.add(start=start, end=end)
    return a


NOW_S = 13 * 3600 + 40 * 60


def test_route_alert_classified_and_mapped(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "1", "Förseningar och inställda avgångar",
           "Förseningar på grund av signalfel. Följ skyltarna på stationen.", routes=["R43"],
           start=now - 600, end=now + 3600)
    _alert(f, "2", "Avstängd hiss vid Odenplan", "Hissen är avstängd.", routes=["R43"])
    _alert(f, "3", "Skylt ur funktion vid Älvsjö", "Tekniskt fel.", routes=["R43"])
    _alert(f, "4", "Buss ersätter", "Gäller buss 7.", routes=["B7"])  # buss, ska ignoreras
    out, _ = build_alerts(static, f, now)
    pt = out["PT"]
    assert [a.title for a in pt.traffic] == ["Förseningar och inställda avgångar"]
    assert pt.traffic[0].lines == ["43"]
    assert [a.id for a in pt.access] == ["2"]
    assert [a.id for a in pt.info] == ["3"]
    assert pt.count == 1
    assert set(out) == {"PT"}


def test_stop_only_alert_uses_stop_tabs_and_description_title(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "s1", "Unv-Kår", "Bussar ersätter tågen mellan Odenplan och City. Läs mer på sl.se.", stops=["A"])
    _alert(f, "s2", "Halka", "Det kan vara halt på plattformen, var försiktig!", stops=["A"])
    out, _ = build_alerts(static, f, now)
    pt = out["PT"]
    assert pt.traffic[0].title == "Bussar ersätter tågen mellan Odenplan och City"
    assert pt.traffic[0].level == "stop"
    assert [a.id for a in pt.info] == ["s2"]   # hållplatslarm utan trafikord är övrigt


def test_display_texts_dropped_and_trip_alert_attached(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "d1", "Främre tåg", "Främre tåg", trips=["t1"])
    _alert(f, "t1", "Inställd", "Inställd mellan Odenplan och City på grund av växelfel.", trips=["t1"])
    out, notes = build_alerts(static, f, now)
    assert notes == {"t1": ["Inställd mellan Odenplan och City på grund av växelfel."]}
    trip = out["PT"].trips[0]
    assert trip.train_number == "2335" and trip.departs == to_epoch(SD, 13 * 3600 + 30 * 60)

    tu = _feed(now)
    _tu(tu, "t1", [(1, 0), (2, 0), (3, 0)])
    row = next(r for r in build_rows(static, tu, now=now, notes=notes) if r.trip_id == "t1")
    assert row.notes == notes["t1"]


def test_inactive_and_finished_trip_alerts_skipped(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "old", "Signalfel", "Signalfel vid City.", routes=["R43"], start=now - 7200, end=now - 3600)
    later = to_epoch(SD, 15 * 3600)  # turen t1 slutade 13:45, mer än 30 min sedan
    _alert(f, "t", "Inställd", "Inställd på grund av växelfel.", trips=["t1"])
    assert not build_alerts(static, f, now)[0]["PT"].traffic
    assert build_alerts(static, f, later)[1] == {}


def test_duplicates_merged_keeping_route_alert(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    text = "Bussar ersätter tågen mellan Odenplan och Stockholm City hela helgen på grund av banarbete."
    _alert(f, "r", "Bussar ersätter tågen", text + " Så här går bussarna: linje 40B.", routes=["R43"])
    _alert(f, "s", "Öst", text, stops=["A"])
    out, _ = build_alerts(static, f, now)
    assert [a.id for a in out["PT"].traffic] == ["r"]


def test_title_handling():
    from fp.alerts import _title_and_text
    h = "Förseningar och inställda avgångar"
    d = "Förseningar och inställda avgångar för gröna linjen på grund av vagnfel."
    assert _title_and_text(h, d, "route") == (h, d)                      # meningen fortsätter: behåll
    assert _title_and_text("Signalfel", "Signalfel. Tåg står still.", "route") == ("Signalfel", "Tåg står still.")
    t, _ = _title_and_text("Stn.medd. X", "Extrainsatta avgångar från Globen cirka kl. 22:00-23:20. Läs mer.", "stop")
    assert t == "Extrainsatta avgångar från Globen cirka kl. 22:00-23:20"


def test_newest_alert_first(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    # Lägre id = äldre larm, även om dess planerade starttid är senare
    _alert(f, "14050001000000001", "Signalfel vid City", "Signalfel. Förseningar.", routes=["R43"], start=now - 60)
    _alert(f, "14050002000000002", "Inställda avgångar", "Inställt på grund av vagnfel.", routes=["R43"], start=now - 7200)
    out, _ = build_alerts(static, f, now)
    assert [a.id for a in out["PT"].traffic] == ["14050002000000002", "14050001000000001"]


def test_planned_counts(static):
    from fp.board import planned_counts
    per_tab, per_line = planned_counts(static, to_epoch(SD, NOW_S))
    assert per_tab["PT"] == 3 and per_line[("PT", "43")] == 3   # t1, t2, t3 går 13:30–13:45
    assert per_tab["TB3"] == 1


def test_general_information_is_not_a_disruption(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "1", "Trygghetsutrop ombord. Dagtid varje dag",
           "Om du känner dig otrygg eller ser någon som behöver hjälp, chatta med vår trygghetscentral i SL-appen.",
           routes=["R43"])
    _alert(f, "2", "Ingen trafik mellan Odenplan och City", "Byt till buss.", routes=["R43"],
           start=now - 600, end=now + 3600)
    out, _ = build_alerts(static, f, now)
    assert [a.id for a in out["PT"].info] == ["1"]
    assert [a.id for a in out["PT"].traffic] == ["2"]
    assert out["PT"].count == 1


def test_stop_alert_naming_other_mode_is_skipped(static):
    """SL kopplar ibland pendeltågsmeddelanden till tunnelbanans plattformar."""
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "m1", "Tunnelbanans nätter", "Omlagd trafik för tunnelbanan i natt, byt till buss.", stops=["A"])
    out, _ = build_alerts(static, f, now)
    assert "PT" not in out


def test_planned_change_not_counted_on_tab(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "p1", "Inställda tåg mellan A och B", "Banarbete, bussar ersätter.", routes=["R43"],
           start=now - 86400, end=now + 3 * 86400)
    out, _ = build_alerts(static, f, now)
    assert out["PT"].traffic[0].acute is False
    assert out["PT"].count == 0


def _dev(cid, header, lines, details="", frm=None, upto=None, imp=5, urg=1, cats=None, stops=()):
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    return {
        "deviation_case_id": cid, "publish": {"from": iso(frm), "upto": iso(upto)},
        "priority": {"importance_level": imp, "influence_level": 5, "urgency_level": urg},
        "message_variants": [{"header": header, "details": details, "language": "sv"}],
        "scope": {"lines": [{"designation": d, "transport_mode": m} for m, d in lines],
                  "stop_areas": [{"name": n} for n in stops]},
        "categories": cats,
    }


def test_deviations_source(static):
    now = to_epoch(SD, NOW_S)
    f = _feed(now)
    _alert(f, "g1", "Signalfel", "Förseningar.", routes=["R43"], start=now - 60, end=now + 600)  # ignoreras
    devs = [
        _dev(100, "Pendeltågen avgår från Stockholms central natt", [("TRAIN", "40"), ("TRAIN", "43")],
             "Pendeltågen avgår från Stockholms central natt. Banarbete.", now - 3600, now + 3 * 86400, imp=7),
        _dev(200, "Inställd delsträcka", [("METRO", "13")], "Tågen går inte mellan X och Y.", now - 600, now + 1800, imp=7, urg=5),
        _dev(150, "Avstängd hiss vid Rissne", [("METRO", "10")], frm=now - 3600, upto=now + 86400, imp=2,
             cats=[{"group": "FACILITY", "type": "LIFT"}]),
        _dev(160, "Avstängd hiss vid Alvik", [("METRO", "17")], frm=now - 3600, upto=now + 86400, imp=2,
             cats=[{"group": "FACILITY", "type": "LIFT"}]),
        _dev(170, "Avstängd hiss vid Brommaplan", [("METRO", "18")], frm=now - 3600, upto=now + 86400, imp=2,
             cats=[{"group": "FACILITY", "type": "LIFT"}]),
        _dev(300, "Bryggan Ängsö indragen", [("SHIP", "17")], frm=now - 3600, upto=now + 86400),  # båt, inte gröna linjen
        _dev(400, "Skylt ur funktion vid Igelboda", [("TRAM", "25")], frm=now - 3600, upto=now + 86400),
        _dev(500, "Framtida", [("METRO", "14")], frm=now + 3600, upto=now + 7200),
    ]
    out, _ = build_alerts(static, f, now, deviations=devs)
    pt = out["PT"]
    assert [a.id for a in pt.traffic] == ["100"]
    assert pt.traffic[0].lines == ["40", "43"] and pt.traffic[0].acute is False
    assert pt.traffic[0].text == "Banarbete."
    assert pt.count == 0
    tb2 = out["TB2"]
    assert [a.id for a in tb2.traffic] == ["200"] and tb2.count == 1
    assert [a.id for a in out["TB1"].access] == ["170", "160"]   # inga hopslagningar, nyast först
    assert not out["TB1"].traffic
    assert [a.id for a in out["TB3"].access] == ["150"]
    assert [a.id for a in out["SB"].info] == ["400"]
