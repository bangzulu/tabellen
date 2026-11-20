# Fordonspositioner – öppen-data-variant

En egen version av SL:s *Senast avlästa fordonspositioner* byggd på
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
4. Utan lokala nycklar: kör **Actions → Täckningskontroll → Run workflow**. Resultatet syns i
   jobbets sammanfattning och de sparade `.pb`-flödena kan laddas ner som artefakt.
   Nycklarna ligger som repo-secrets `GTFS_STATIC_KEY` och `GTFS_RT_KEY`.
5. Utveckla vidare mot sparade flöden: `USE_SAVED_FEEDS=true flask --app fp.server run --debug`

## Störningar

Hämtas ur GTFS-RT ServiceAlerts och visas överst i varje flik, med antal i flikens märke.
SL:s flöde saknar effekt och allvarlighetsgrad, så `fp/alerts.py` sorterar på text och koppling:

| Visas som | Vad |
|---|---|
| Störningar | linje- eller hållplatslarm med trafikpåverkan (inställt, ersättningsbuss, extrainsatt …) |
| Enskilda avgångar | larm på en viss tur; texten visas också på raden i tabellen |
| Hissar och entréer, Skyltar och övrigt | hopfällda; räknas inte i märket |

Plattformsskyltens korta texter ("Främre tåg", "Ingen påstigning") filtreras bort, och samma
störning som publiceras både på linjen och på enskilda stationer slås ihop. Interna rubriker
("Unv-Kår", "… RP") ersätts med beskrivningens första mening.

## Snabbhet

- Tidtabellen sparas som pickle på volymen: omstart tar under en sekund i stället för 5–15 s.
- Realtid och störningar hämtas parallellt. Data upp till 5 min gammal visas direkt medan
  ny hämtas i bakgrunden.
- `/api/board` ger alla flikar i ett komprimerat svar (~11 kB), byggt en gång per ny data.
  Flikbyten sker utan nya anrop och senaste data visas direkt vid omladdning (localStorage).

## Drift på Fly.io

`fly.toml` kör en maskin (shared-cpu-1x, 256 MB) i Stockholm som alltid är igång, ca 2 USD/månad
(Fly efterskänker fakturor under 5 USD). En volym på 1 GB håller tidtabellen mellan omstarter.

Engångsuppsättning, helt i webbläsaren:

1. Fly: skapa en organisationstoken under **Tokens** och kopiera den.
2. GitHub: lägg den som repo-secret `FLY_API_TOKEN` (bredvid `GTFS_STATIC_KEY` och `GTFS_RT_KEY`).
3. GitHub: **Actions → Deploy till Fly → Run workflow**. Appen skapas, nycklarna förs över
   och sidan hamnar på `https://tabellen.fly.dev`. Är namnet upptaget, byt `app` i `fly.toml`.

Därefter deployas varje push till `main` automatiskt (efter att testerna gått igenom).
Sidan är publik, men realtidsanrop cachas (TripUpdates 30 s, ServiceAlerts 120 s) och stoppas vid `RT_MONTHLY_BUDGET`.

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

## Verifierat mot riktig data (19 nov 2026)

- [x] Alla nio flikar finns i TripUpdates, med ungefär lika många rader som tidtabellen förväntar.
- [x] TripUpdates behåller passerade hållplatser, så avläsningsplatsen kommer direkt ur flödet.
- [x] Spårlinjerna har route_type 100 (pendeltåg), 401 (tunnelbana) och 900 (spårväg/lokalbanor).
      SL:s båtlinjer (route_type 1000) har krockande nummer (10, 13, 21 …) och filtreras bort.
- [x] `trip_short_name` är tomt för SL. Tågnumret tas från `samtrafiken_internal_trip_number`.
- [ ] Jämför avvikelse mot kitt vid samma tidpunkt för några tågnummer.
- [x] Gruppering sker på `direction_id` (0 → Riktning 1, 1 → Riktning 2) utan tolkning av norr/söder.
      Matchar inte nödvändigtvis kitts numrering, och det är medvetet.
- [ ] Nockebybanan (linje 12) finns i datan men inte i kitt. Lägg till som egen flik?

## Idéer från intervjuerna med trafikinformatörerna

Filtrering på avvikelser och sortering på försening finns redan. Nästa kandidater:
ServiceAlerts per linje, trend (ökar förseningen?), markering av tåg som slutat rapportera,
samt Nockebybanan (linje 12) som saknas i kitt.
