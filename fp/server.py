"""Flask-app. Kör: flask --app fp.server run --debug

Snabbhet:
- Tidtabellen läses in i bakgrunden direkt vid start, från pickle-cache på volymen.
- Dygnsomladdningen sker i bakgrunden; den gamla tidtabellen används tills den nya är klar.
- Realtidsflödena serveras från cache och förnyas i bakgrunden (se realtime.py).
- /api/board levererar alla flikar i ett gzip-komprimerat svar som byggs en gång per
  ny data, så flikbyten i webbläsaren inte kräver nya anrop.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config
from .alerts import build_alerts, to_json
from .board import build_rows, group_for_tab, line_patterns, planned_counts
from .realtime import BudgetExceeded, FeedCache
from .static_gtfs import TZ, StaticData, load_static_cached, window_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fp")

app = Flask(__name__, static_folder=str(config.ROOT / "static"), static_url_path="/static")
feeds = FeedCache()
_pool = ThreadPoolExecutor(max_workers=3)

_static: StaticData | None = None
_static_day: str | None = None
_static_lock = threading.Lock()
_static_ready = threading.Event()
_reloading = False

_board_cache: tuple[tuple, bytes, bytes] | None = None  # (nyckel, json, gzip)
_patterns: tuple[int, dict] | None = None  # (id(static), linjemönster)
_board_lock = threading.Lock()


def _load_static_now() -> None:
    global _static, _static_day, _reloading
    try:
        now = datetime.now(TZ)
        data = load_static_cached(window_dates(now))
        with _static_lock:
            _static, _static_day = data, now.strftime("%Y%m%d")
        _static_ready.set()
    except Exception:
        log.exception("Kunde inte läsa in tidtabellen")
    finally:
        _reloading = False


def _start_reload() -> None:
    global _reloading
    with _static_lock:
        if _reloading:
            return
        _reloading = True
    threading.Thread(target=_load_static_now, daemon=True, name="static-load").start()


def get_static(timeout: float = 90) -> StaticData:
    """Returnerar tidtabellen. Byter dag efter 07:00 i bakgrunden utan att blockera."""
    now = datetime.now(TZ)
    if _static is None:
        _start_reload()
        if not _static_ready.wait(timeout):
            raise RuntimeError("Tidtabellen laddas fortfarande, försök igen om en stund.")
    elif _static_day != now.strftime("%Y%m%d") and now.hour >= 7:
        _start_reload()
    if _static is None:
        raise RuntimeError("Tidtabellen kunde inte läsas in.")
    return _static


# Börja läsa in tidtabellen så fort processen startar, inte vid första besöket.
_start_reload()


def _fetch_feeds():
    jobs = {
        "tu": _pool.submit(feeds.get, "TripUpdates"),
        "sa": _pool.submit(feeds.get, "ServiceAlerts", config.ALERTS_CACHE_SECONDS,
                           max(config.RT_STALE_OK_SECONDS, 3 * config.ALERTS_CACHE_SECONDS)),
    }
    if config.USE_VEHICLE_POSITIONS:
        jobs["vp"] = _pool.submit(feeds.get, "VehiclePositions")
    tu = jobs["tu"].result()
    vp = jobs["vp"].result() if "vp" in jobs else None
    try:
        sa = jobs["sa"].result()
    except Exception:  # störningar är ett tillägg; tabellen ska visas ändå
        log.warning("Kunde inte hämta ServiceAlerts", exc_info=True)
        sa = None
    return tu, vp, sa


def _patterns_for(static: StaticData) -> dict:
    global _patterns
    if _patterns is None or _patterns[0] != id(static):
        _patterns = (id(static), line_patterns(static))
    return _patterns[1]


def _build_payload(static: StaticData, tu, vp, sa) -> dict:
    now = int(time.time())
    patterns = _patterns_for(static)
    alerts, notes = build_alerts(static, sa, now) if sa is not None else ({}, {})
    rows = build_rows(static, tu, vp, now=now, notes=notes)
    planned_tab, planned_line = planned_counts(static, now)
    return {
        "generated_at": now,
        "feed_timestamp": tu.header.timestamp or None,
        "alerts_timestamp": (sa.header.timestamp or None) if sa is not None else None,
        "max_age_seconds": config.MAX_DATA_AGE_SECONDS,
        "tabs": {
            t["code"]: {
                # i trafik enligt realtid (inställda räknas inte) och enligt tidtabellen
                "count": sum(1 for r in rows if r.tab == t["code"] and r.status != "cancelled"),
                "planned": planned_tab.get(t["code"], 0),
                "groups": group_for_tab(rows, t["code"]),
                "alerts": to_json(alerts.get(t["code"])),
                "lines": [{**l, "planned": planned_line.get((t["code"], l["line"]), 0)}
                          for l in patterns.get(t["code"], [])],
            }
            for t in config.TABS
        },
    }


@app.get("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/tabs")
def tabs():
    resp = jsonify([{"code": t["code"], "name": t["name"], "lines": t["lines"]} for t in config.TABS])
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/api/board")
def board():
    global _board_cache
    try:
        static = get_static()
        tu, vp, sa = _fetch_feeds()
    except BudgetExceeded as exc:
        return jsonify(error=str(exc), code="budget"), 503
    except Exception as exc:  # nätverk, nyckel, ogiltig fil
        log.exception("Kunde inte hämta data")
        return jsonify(error=f"Kunde inte hämta data: {exc}", code="upstream"), 502

    key = (id(static), id(tu), id(vp), id(sa), config.USE_SAVED_FEEDS and int(time.time() // 15))
    with _board_lock:
        if _board_cache is None or _board_cache[0] != key:
            raw = json.dumps(_build_payload(static, tu, vp, sa), ensure_ascii=False,
                             separators=(",", ":")).encode()
            _board_cache = (key, raw, gzip.compress(raw, 6))
        _, raw, gz = _board_cache

    if "gzip" in request.headers.get("Accept-Encoding", ""):
        resp = Response(gz, mimetype="application/json")
        resp.headers["Content-Encoding"] = "gzip"
    else:
        resp = Response(raw, mimetype="application/json")
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/health")
def health():
    return jsonify(
        rt_calls_this_month=feeds.usage.count(),
        rt_monthly_budget=config.RT_MONTHLY_BUDGET,
        rt_cache_seconds=config.RT_CACHE_SECONDS,
        alerts_cache_seconds=config.ALERTS_CACHE_SECONDS,
        vehicle_positions=config.USE_VEHICLE_POSITIONS,
        static_loaded=_static is not None,
        static_trips=len(_static.trips) if _static else 0,
    )
