"""Steg 1: verifiera att den öppna datan räcker, per flik.

  python scripts/coverage_check.py --routes   # lista spårlinjer i statisk data (kontrollera TABS)
  python scripts/coverage_check.py            # hämta TripUpdates + VehiclePositions (2 anrop), spara och analysera
  python scripts/coverage_check.py --offline  # analysera senast sparade .pb-filer utan nya anrop
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fp import config  # noqa: E402
from fp.board import build_rows  # noqa: E402
from fp.realtime import FeedCache, load_feed_file  # noqa: E402
from fp.static_gtfs import candidate_service_dates, load_static, to_epoch, window_dates  # noqa: E402

SAVED = {name: config.DATA_DIR / f"{name}.pb" for name in ("TripUpdates", "VehiclePositions")}


def list_routes(static) -> None:
    print(f"{'linje':<6} {'route_type':<10} {'i TABS':<8} namn")
    for r in sorted(static.routes, key=lambda r: (int(r.get('route_type') or 0), r.get('route_short_name', ''))):
        rtype = int(r.get("route_type") or 0)
        if not config.is_rail_route_type(rtype):
            continue
        line = r.get("route_short_name", "")
        print(f"{line:<6} {rtype:<10} {config.LINE_TO_TAB.get(line, '-'):<8} {r.get('route_long_name', '')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", action="store_true")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    static = load_static(dates=window_dates())
    if args.routes:
        list_routes(static)
        return

    if args.offline:
        tu, vp = load_feed_file(SAVED["TripUpdates"]), load_feed_file(SAVED["VehiclePositions"])
    else:
        cache = FeedCache()
        tu, vp = cache.get("TripUpdates", 0), cache.get("VehiclePositions", 0)
        for name, msg in (("TripUpdates", tu), ("VehiclePositions", vp)):
            SAVED[name].write_bytes(msg.SerializeToString())
        print(f"Sparade flöden i {config.DATA_DIR} (använd --offline för att analysera igen)")

    now = int(time.time())
    print(f"Feed-ålder: TripUpdates {now - tu.header.timestamp}s, VehiclePositions {now - vp.header.timestamp}s\n")

    # Vilka trafikslag finns överhuvudtaget i VehiclePositions? (route_type via statisk routes.txt)
    route_type = {r["route_id"]: int(r.get("route_type") or 0) for r in static.routes}
    vp_types = Counter()
    for e in vp.entity:
        if e.HasField("vehicle"):
            t = static.trips.get(e.vehicle.trip.trip_id)
            rid = e.vehicle.trip.route_id or (t.route_id if t else None)
            vp_types[route_type.get(rid, "okänd")] += 1
    print("VehiclePositions per route_type:", dict(vp_types.most_common()), "\n")

    # Förväntade turer i rörelse just nu enligt tidtabell
    expected = Counter()
    for sd in candidate_service_dates():
        for t in static.trips_on(sd):
            first, last = to_epoch(sd, t.stops[0].dep), to_epoch(sd, t.stops[-1].arr)
            if first and last and first <= now <= last:
                expected[t.tab] += 1

    tu_ids = {e.trip_update.trip.trip_id for e in tu.entity if e.HasField("trip_update")}
    vp_ids = {e.vehicle.trip.trip_id for e in vp.entity if e.HasField("vehicle")}
    past_stu = Counter()
    total_stu = Counter()
    for e in tu.entity:
        trip = static.trips.get(e.trip_update.trip.trip_id)
        if not trip:
            continue
        for s in e.trip_update.stop_time_update:
            t = (s.departure.time if s.HasField("departure") else 0) or (s.arrival.time if s.HasField("arrival") else 0)
            total_stu[trip.tab] += 1
            if t and t <= now:
                past_stu[trip.tab] += 1

    rows = build_rows(static, tu, vp, now=now)
    by_tab = Counter(r.tab for r in rows)
    with_delay = Counter(r.tab for r in rows if r.delay_min is not None)
    via_vp = Counter(r.tab for r in rows if r.observed_source == "vehicleposition")
    tn = Counter(t.tab for t in static.trips.values() if t.train_number != t.line)
    tot = Counter(t.tab for t in static.trips.values())

    hdr = f"{'flik':<6}{'enl. tidtabell':>15}{'i TU':>7}{'i VP':>7}{'rader':>7}{'m. avvik.':>10}{'via VP':>8}{'passerade STU':>15}{'tågnr':>8}"
    print(hdr)
    print("-" * len(hdr))
    for tab in config.TABS:
        c = tab["code"]
        in_tu = sum(1 for i in tu_ids if static.trips.get(i) and static.trips[i].tab == c)
        in_vp = sum(1 for i in vp_ids if static.trips.get(i) and static.trips[i].tab == c)
        share = f"{past_stu[c]}/{total_stu[c]}" if total_stu[c] else "-"
        tnr = f"{100 * tn[c] // tot[c]}%" if tot[c] else "-"
        print(f"{c:<6}{expected[c]:>15}{in_tu:>7}{in_vp:>7}{by_tab[c]:>7}{with_delay[c]:>10}{via_vp[c]:>8}{share:>15}{tnr:>8}")

    print("""
Tolkning:
  rader ≈ enl. tidtabell  -> fliken går att bygga.
  i TU = 0 men i VP > 0   -> sätt USE_VEHICLE_POSITIONS=true (avvikelse saknas då oftast).
  passerade STU låg       -> flödet tar bort passerade hållplatser; avläsningsplats måste då
                             härledas från första kommande hållplats eller VehiclePositions.
  tågnr < 100%            -> trip_short_name saknas, fallback är linjenumret.""")


if __name__ == "__main__":
    main()
