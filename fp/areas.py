"""Geografiska områden för bussöversikten: kommuner, med Stockholms stad uppdelad i
Innerstaden, Västerort och Söderort (så som trafikradion brukar prata om staden).

Kommungränserna kommer från okfse/sweden-geojson (förenklade 2 %, ursprungligen
Valmyndighetens data) och ligger i static/kommuner.geojson, där bara kommuner med
SL-busshållplatser finns med. Samma fil ritas som karta i webbläsaren.
"""
from __future__ import annotations

import json
from functools import lru_cache

from . import config

GEOJSON = config.ROOT / "static" / "kommuner.geojson"

# Ungefärlig etikettplats för Stockholms delar (lat, lon)
STHLM_PARTS = {
    "Stockholm innerstad": (59.334, 18.070),
    "Västerort": (59.375, 17.925),
    "Söderort": (59.272, 18.040),
}


def stockholm_part(lat: float, lon: float) -> str:
    """Grov uppdelning av Stockholms kommun. Gränserna följer ungefär vattnet:
    söder om Årstaviken/Liljeholmen är Söderort, väster om Tranebergssund Västerort."""
    if lat < 59.305 or (lon < 18.03 and lat < 59.318):
        return "Söderort"
    if lon < 17.995 and lat >= 59.318:
        return "Västerort"
    if lat > 59.37:          # Kista, Rinkeby m.fl. ligger norr om innerstaden men väster om E4
        return "Västerort"
    return "Stockholm innerstad"


def _rings(geom: dict) -> list:
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def _in_ring(lat: float, lon: float, ring: list) -> bool:
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@lru_cache(maxsize=1)
def _features() -> list[tuple[str, list, tuple[float, float, float, float], tuple[float, float]]]:
    data = json.loads(GEOJSON.read_text(encoding="utf-8"))
    out = []
    for f in data["features"]:
        polys = _rings(f["geometry"])
        xs = [p[0] for poly in polys for p in poly[0]]
        ys = [p[1] for poly in polys for p in poly[0]]
        out.append((f["properties"]["name"], polys, (min(xs), min(ys), max(xs), max(ys)),
                    tuple(f["properties"]["label"])))
    return out


def area_of(lat: float, lon: float) -> str:
    """Områdets namn för en punkt. Punkter utanför alla kommuner (kust, förenklade gränser)
    hamnar i närmaste kommuns område."""
    best, best_d = None, None
    for name, polys, (x0, y0, x1, y1), (cy, cx) in _features():
        if x0 <= lon <= x1 and y0 <= lat <= y1 and any(
                _in_ring(lat, lon, p[0]) and not any(_in_ring(lat, lon, h) for h in p[1:]) for p in polys):
            return stockholm_part(lat, lon) if name == "Stockholm" else name
        d = (cy - lat) ** 2 + ((cx - lon) * 0.5) ** 2
        if best_d is None or d < best_d:
            best, best_d = name, d
    return stockholm_part(lat, lon) if best == "Stockholm" else (best or "Okänt område")


def area_labels() -> dict[str, tuple[float, float]]:
    """Etikettpunkt (lat, lon) per område, för kartan och sammanfattningen."""
    out = {name: label for name, _, _, label in _features() if name != "Stockholm"}
    out.update(STHLM_PARTS)
    return out
