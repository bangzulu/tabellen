"""Flask-app. Kör: flask --app fp.server run --debug"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

from . import config
from .board import build_rows, group_for_tab
from .realtime import BudgetExceeded, FeedCache
from .static_gtfs import TZ, StaticData, load_static, window_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fp")

app = Flask(__name__, static_folder=str(config.ROOT / "static"), static_url_path="/static")
feeds = FeedCache()
_static: StaticData | None = None
_static_day: str | None = None
_static_lock = threading.Lock()


def get_static() -> StaticData:
    """Laddar om statisk data en gång per dygn (efter 07:00 hämtas ny fil)."""
    global _static, _static_day
    now = datetime.now(TZ)
    day = now.strftime("%Y%m%d")
    with _static_lock:
        if _static is None or (_static_day != day and now.hour >= 7):
            _static = load_static(dates=window_dates(now))
            _static_day = day
        return _static


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/tabs")
def tabs():
    return jsonify([{"code": t["code"], "name": t["name"], "lines": t["lines"]} for t in config.TABS])


@app.get("/api/board")
def board():
    tab = request.args.get("tab", "PT")
    if tab not in config.TAB_BY_CODE:
        return jsonify(error=f"Okänd flik: {tab}"), 400
    try:
        static = get_static()
        tu = feeds.get("TripUpdates")
        vp = feeds.get("VehiclePositions") if config.USE_VEHICLE_POSITIONS else None
    except BudgetExceeded as exc:
        return jsonify(error=str(exc), code="budget"), 503
    except Exception as exc:  # nätverk, nyckel, ogiltig fil
        log.exception("Kunde inte hämta data")
        return jsonify(error=f"Kunde inte hämta data: {exc}", code="upstream"), 502

    now = int(time.time())
    feed_ts = tu.header.timestamp or None
    rows = build_rows(static, tu, vp, now=now)
    return jsonify(
        tab=tab,
        generated_at=now,
        feed_timestamp=feed_ts,
        max_age_seconds=config.MAX_DATA_AGE_SECONDS,
        stale=bool(feed_ts and now - feed_ts > config.MAX_DATA_AGE_SECONDS),
        groups=group_for_tab(rows, tab),
        counts={t["code"]: sum(1 for r in rows if r.tab == t["code"]) for t in config.TABS},
    )


@app.get("/api/health")
def health():
    return jsonify(
        rt_calls_this_month=feeds.usage.count(),
        rt_monthly_budget=config.RT_MONTHLY_BUDGET,
        rt_cache_seconds=config.RT_CACHE_SECONDS,
        vehicle_positions=config.USE_VEHICLE_POSITIONS,
        static_loaded=_static is not None,
        static_trips=len(_static.trips) if _static else 0,
    )
