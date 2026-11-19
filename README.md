# Fordonspositioner – öppen-data-variant

En egen version av SL:s *Senast avlästa fordonspositioner* (kitt.sl.se), byggd på
Trafiklabs GTFS Regional (statisk + realtid) för SL. Flask-backend, en HTML-sida utan byggsteg.

## Kom igång utan nyckel (demodata)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/make_demo_data.py
DATA_DIR=demo USE_SAVED_FEEDS=true flask --app fp.server run --debug
# http://127.0.0.1:5000/#PT
```

![Demo](docs/demo.png)

Demodatan är påhittad. Flödet blir "inaktuellt" efter 2 minuter – kör skriptet igen.

## Med riktig data

1. Skapa ett projekt på developer.trafiklab.se och aktivera **GTFS Regional Static** och
   **GTFS Regional Realtime**. Kopiera `.env.example` till `.env` och fyll i nycklarna.
2. **Verifiera täckningen först** (laddar ner statisk data + 2 realtidsanrop):
   ```bash
   python scripts/coverage_check.py --routes   # stämmer linjenumren i fp/config.py TABS?
   python scripts/coverage_check.py            # rader per flik jämfört med tidtabellen
   python scripts/coverage_check.py --offline  # analysera igen utan nya anrop
   ```
3. Starta: `flask --app fp.server run --debug`
4. Utveckla vidare mot sparade flöden: `USE_SAVED_FEEDS=true flask --app fp.server run --debug`

## Struktur

```
fp/config.py        nycklar, gränser, flikar -> linjenummer
fp/static_gtfs.py   nedladdning (max 1/dygn) och filtrerad inläsning av sl.zip
fp/realtime.py      GTFS-RT på begäran, cache och månadsbudget (data/usage.json)
fp/board.py         slår ihop statisk + realtid till tabellrader
fp/server.py        /api/tabs, /api/board?tab=PT, /api/health
static/index.html   gränssnittet
scripts/            coverage_check.py, make_demo_data.py
tests/              syntetiska tester: python -m pytest
```

## Fält jämfört med kitt

| kitt | Här | Källa |
|---|---|---|
| Avvikelse | `delay_min`, **+ = sen** (kitt: − = sen) | TripUpdates, försening propageras nedströms |
| Destination | `destination` | `trip_headsign` |
| Avläsningsplats | `observed_stop` | senast passerade hållplats i TripUpdates, reserv: VehiclePositions |
| Avläsningstid | `passed_at` = när tåget passerade | faktisk tid vid avläsningsplatsen |
| Ordinarie avgångstid | `scheduled_at` vid **samma** hållplats | `stop_times.txt` |
| Tågnummer | `train_number` | `trip_short_name`, fallback linjenummer |
| Riktning 1/2 | `direction_id` + vanligaste destinationerna | `trips.txt` |
| – | `next_stop`, `next_expected_at`, `status` (inställd/ändhållplats), `line` | nytt |

`observed_source` visar hur avläsningsplatsen togs fram: `tripupdate`, `tripupdate-inferred`
(flödet saknade passerade hållplatser, markeras ≈), `vehicleposition` (◦) eller `schedule`.

## Kvoter

| Nyckel | Bronze | Konsekvens |
|---|---|---|
| Static | 50/månad | laddas ner max en gång per dygn, efter 07:00 |
| Realtime | 30 000/månad | hämtas bara när någon tittar, cache 30 s. Kontinuerlig drift = ~86 000/mån → **Silver** |

`RT_MONTHLY_BUDGET` stoppar hämtning innan nyckeln spärras. `USE_VEHICLE_POSITIONS=true` dubblar anropen.

## Öppna frågor att verifiera med riktig data

- [ ] Finns alla nio flikar i TripUpdates (särskilt Roslagsbanan, Saltsjöbanan, Lidingöbanan, Linje 7)?
- [ ] Behåller SL:s TripUpdates passerade hållplatser, eller bara kommande?
- [ ] Vilka `route_type` och `route_short_name` används? (`--routes`)
- [ ] Är `trip_short_name` satt för alla spårturer?
- [ ] Hur väl stämmer avvikelsen mot kitt vid samma tidpunkt? Jämför några tågnummer manuellt.
- [ ] `direction_id` 0/1 – matchar det kitts Riktning 1/2?

## Idéer från intervjuerna med trafikinformatörerna

Filtrering på avvikelser och sortering på försening finns redan. Nästa kandidater:
ServiceAlerts per linje, trend (ökar förseningen?), markering av tåg som slutat rapportera,
samt Nockebybanan (linje 12) som saknas i kitt.
