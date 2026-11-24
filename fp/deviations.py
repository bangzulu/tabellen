"""Störningsmeddelanden från SL Deviations (samma källa som sl.se/trafiklaget).

Ingen nyckel, men Trafiklab ber om högst ett anrop per minut. Meddelandena är
knutna till linjer med trafikslag (scope.lines[].transport_mode), så ett
pendeltågsmeddelande hamnar aldrig på tunnelbanan. Är källan nere används
GTFS-RT ServiceAlerts som reserv (se alerts.py).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

import requests

from . import config

log = logging.getLogger(__name__)

URL = "https://deviations.integration.sl.se/v1/messages"
MIN_INTERVAL_S = 60          # Trafiklabs fair use
STALE_LIMIT_S = 15 * 60      # äldre data än så räknas som otillgängligt
RAIL_MODES = {"METRO", "TRAIN", "TRAM"}


def _base(designation: str) -> str:
    return re.sub(r"\D+$", "", designation or "")


# designation → flik ("43" → PT, "27" → RB). Båtar och bussar filtreras bort på trafikslag.
LINE_TAB = {_base(line): t["code"] for t in config.TABS for line in t["lines"]}


def _epoch(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


class DeviationsCache:
    def __init__(self, url: str = URL):
        self.url = url
        self._data: list | None = None
        self._fetched = 0.0
        self._last_try = 0.0
        self._lock = threading.Lock()
        self._refreshing = False

    def _fetch(self) -> None:
        try:
            r = requests.get(self.url, params={"future": "false"}, timeout=8,
                             headers={"Accept-Encoding": "gzip"})
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError("oväntat svar")
            self._data, self._fetched = data, time.time()
        except Exception:
            log.warning("Kunde inte hämta SL Deviations", exc_info=True)
        finally:
            self._refreshing = False

    def get(self) -> tuple[list | None, float]:
        """(meddelanden, hämtningstid). None om källan inte gått att nå på länge."""
        now = time.time()
        with self._lock:
            due = now - self._last_try >= MIN_INTERVAL_S and not self._refreshing
            if due:
                self._last_try, self._refreshing = now, True
        if due:
            if self._data is None:
                self._fetch()                 # första gången: vänta
            else:
                threading.Thread(target=self._fetch, daemon=True).start()
        if self._data is None or now - self._fetched > STALE_LIMIT_S:
            return None, 0.0
        return self._data, self._fetched


def load_saved(path=config.DATA_DIR / "deviations.json") -> tuple[list | None, float]:
    """Utveckling utan nätverk (USE_SAVED_FEEDS): läs data/deviations.json om den finns."""
    import json
    try:
        return json.loads(path.read_text()), path.stat().st_mtime
    except (FileNotFoundError, ValueError):
        return None, 0.0


def rail_items(devs: list, now: int):
    """Ger (flikar, fält) för varje aktivt spårmeddelande."""
    for d in devs:
        scope = d.get("scope") or {}
        lines = [l for l in scope.get("lines") or [] if l.get("transport_mode") in RAIL_MODES]
        tabs: dict[str, list[str]] = {}
        for l in lines:
            tab = LINE_TAB.get(_base(l.get("designation", "")))
            if tab:
                tabs.setdefault(tab, []).append(l.get("designation", ""))
        if not tabs:
            continue
        pub = d.get("publish") or {}
        since, until = _epoch(pub.get("from")), _epoch(pub.get("upto"))
        if (since and since > now) or (until and until < now):
            continue
        mv = next((m for m in d.get("message_variants") or [] if m.get("language") == "sv"),
                  (d.get("message_variants") or [{}])[0])
        header = (mv.get("header") or "").strip()
        details = re.sub(r"\n{3,}", "\n\n", (mv.get("details") or "").replace("\r", "")).strip()
        if not header and not details:
            continue
        stops = [s.get("name") for s in scope.get("stop_areas") or [] if s.get("name")]
        yield tabs, {
            "id": str(d.get("deviation_case_id") or ""),
            "header": header,
            "details": details,
            "stops": list(dict.fromkeys(stops)),
            "since": since,
            "until": until,
            "priority": d.get("priority") or {},
            "facility": any((c or {}).get("group") == "FACILITY" for c in d.get("categories") or []),
        }
