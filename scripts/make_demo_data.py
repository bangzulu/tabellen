"""Skapar påhittad demodata i en egen mapp så frontend kan utvecklas utan API-nyckel.

  python scripts/make_demo_data.py
  DATA_DIR=demo USE_SAVED_FEEDS=true flask --app fp.server run --debug

Tidtabellen byggs runt nuvarande klockslag, så tågen syns som 'i trafik'.
"""
import os
import random
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parent.parent / "demo"))

from google.transit import gtfs_realtime_pb2 as rt  # noqa: E402

from fp import config  # noqa: E402
from fp.static_gtfs import TZ  # noqa: E402

LINES = {
    ("PT", "40"): ["Uppsala C", "Knivsta", "Arlanda C", "Märsta", "Sollentuna", "Stockholm Odenplan", "Stockholm City", "Älvsjö", "Huddinge", "Flemingsberg", "Tumba", "Södertälje centrum"],
    ("PT", "43"): ["Bålsta", "Kungsängen", "Spånga", "Sundbyberg", "Stockholm Odenplan", "Stockholm City", "Årstaberg", "Älvsjö", "Farsta strand", "Handen", "Västerhaninge", "Nynäshamn"],
    ("TB3", "10"): ["Kungsträdgården", "T-Centralen", "Fridhemsplan", "Västra skogen", "Solna centrum", "Hallonbergen", "Kista", "Hjulsta"],
    ("RB", "28"): ["Stockholms östra", "Universitetet", "Djursholms Ösby", "Täby centrum", "Roslags Näsby", "Österskär"],
}


def main():
    random.seed(4)
    out = Path(os.environ["DATA_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    sd = now.strftime("%Y%m%d")
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    epoch0 = int(now.timestamp()) - now_s

    stops = {}
    routes, trips, stop_times = [], [], []
    tu = rt.FeedMessage()
    tu.header.gtfs_realtime_version = "2.0"
    tu.header.timestamp = int(time.time()) - 8
    n = 0
    for (tab, line), names in LINES.items():
        routes.append([f"R{line}", line, f"Linje {line}", "401" if tab == "TB3" else "106"])
        for name in names:
            stops.setdefault(name, f"S{len(stops)}")
        for direction in (0, 1):
            seq_names = names if direction == 0 else names[::-1]
            for k in range(6):
                n += 1
                tid = f"T{n}"
                start = now_s - k * 9 * 60 - random.randint(0, 120)
                trips.append([f"R{line}", "S1", tid, seq_names[-1], str(direction), str(2200 + n * 2 + direction)])
                for i, name in enumerate(seq_names):
                    t = start + i * 4 * 60
                    hms = f"{t // 3600:02d}:{t % 3600 // 60:02d}:00"
                    stop_times.append([tid, hms, hms, stops[name], str(i + 1)])
                delay = random.choice([0, 0, 0, 0, 60, 120, 240, 480, 780, -60])
                e = tu.entity.add(id=tid)
                e.trip_update.trip.trip_id = tid
                e.trip_update.trip.start_date = sd
                if random.random() < 0.04:
                    e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED
                    continue
                for i, _ in enumerate(seq_names):
                    s = e.trip_update.stop_time_update.add(stop_sequence=i + 1)
                    s.arrival.time = epoch0 + start + i * 240 + delay
                    s.departure.time = epoch0 + start + i * 240 + delay

    def csv(rows):
        return "\n".join(",".join(r) for r in rows) + "\n"

    with zipfile.ZipFile(out / "sl.zip", "w") as zf:
        zf.writestr("routes.txt", csv([["route_id", "route_short_name", "route_long_name", "route_type"], *routes]))
        zf.writestr("trips.txt", csv([["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "trip_short_name"], *trips]))
        zf.writestr("stops.txt", csv([["stop_id", "stop_name", "parent_station"], *[[sid, name, ""] for name, sid in stops.items()]]))
        zf.writestr("stop_times.txt", csv([["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], *stop_times]))
        zf.writestr("calendar_dates.txt", csv([["service_id", "date", "exception_type"], ["S1", sd, "1"]]))
    (out / "TripUpdates.pb").write_bytes(tu.SerializeToString())
    sa = rt.FeedMessage()
    sa.header.gtfs_realtime_version = "2.0"
    sa.header.timestamp = tu.header.timestamp

    def alert(aid, header, desc, route=None, trip=None, stop=None):
        a = sa.entity.add(id=aid).alert
        a.header_text.translation.add(text=header)
        a.description_text.translation.add(text=desc)
        ie = a.informed_entity.add()
        if route:
            ie.route_id = route
        if stop:
            ie.stop_id = stop
        if trip:
            ie.trip.trip_id = trip

    alert("demo1", "Förseningar i pendeltågstrafiken",
          "Förseningar på grund av signalfel vid Älvsjö. Räkna med längre restid.", route="R43")
    alert("demo2", "Avstängd hiss vid Kista", "Hissen mellan plattformen och biljetthallen är avstängd.", route="R10")
    alert("demo3", "Inställd", "Inställd mellan Stockholm City och Älvsjö på grund av växelfel.", trip="T1")
    alert("demo4", "Främre tåg", "Främre tåg", trip="T2")  # plattformstext, ska filtreras bort
    (out / "ServiceAlerts.pb").write_bytes(sa.SerializeToString())

    vp = rt.FeedMessage()
    vp.header.gtfs_realtime_version = "2.0"
    vp.header.timestamp = tu.header.timestamp
    (out / "VehiclePositions.pb").write_bytes(vp.SerializeToString())
    print(f"Demodata skriven till {out} ({len(trips)} turer). Tidtabellen gäller i ungefär en timme.")


if __name__ == "__main__":
    main()
