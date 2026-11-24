"""Kort historik (senaste ~2 timmarna) för kurvorna "Senaste timmen".

Ett värde sparas per nytt realtidsflöde, alltså bara när någon har sidan öppen; inga extra
anrop mot Trafiklab. Historiken skrivs till DATA_DIR (Fly-volymen) så den överlever omstarter.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from collections import defaultdict, deque

from . import config

log = logging.getLogger(__name__)

PATH = config.DATA_DIR / "history.json"
KEEP_S = 2 * 3600
SAVE_EVERY_S = 60
BUCKET_S = 300

_lock = threading.Lock()
_series: dict[str, deque] = defaultdict(lambda: deque(maxlen=400))
_last_ts: dict[str, int] = {}
_saved_at = 0.0


def _load() -> None:
    try:
        raw = json.loads(PATH.read_text())
    except (FileNotFoundError, ValueError):
        return
    cutoff = time.time() - KEEP_S
    for key, pts in raw.items():
        _series[key].extend((int(t), float(v)) for t, v in pts if t >= cutoff)
        if _series[key]:
            _last_ts[key] = _series[key][-1][0]


def _save(now: float) -> None:
    global _saved_at
    cutoff = now - KEEP_S
    data = {k: [p for p in d if p[0] >= cutoff] for k, d in _series.items()}
    tmp = PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({k: v for k, v in data.items() if v}, separators=(",", ":")))
        os.replace(tmp, PATH)
        _saved_at = now
    except OSError:
        log.warning("Kunde inte spara historik", exc_info=True)


def record(key: str, ts: int | None, value: float) -> None:
    """Spara ett värde för flödestiden ts (en gång per flöde och nyckel)."""
    if not ts:
        return
    with _lock:
        if _last_ts.get(key) == ts:
            return
        _last_ts[key] = ts
        _series[key].append((int(ts), round(value, 3)))
        now = time.time()
        if now - _saved_at >= SAVE_EVERY_S:
            _save(now)


def points(key: str, since: int) -> list[float]:
    with _lock:
        return [v for t, v in _series.get(key, ()) if t >= since]


def window(key: str, lo: int, hi: int) -> list[float]:
    with _lock:
        return [v for t, v in _series.get(key, ()) if lo <= t <= hi]


def spark(key: str, now: int, buckets: int = 12) -> list[float | None]:
    """Medelvärde per 5 min senaste timmen (None där data saknas)."""
    with _lock:
        pts = list(_series.get(key, ()))
    out = []
    for k in range(buckets, 0, -1):
        lo, hi = now - k * BUCKET_S, now - (k - 1) * BUCKET_S
        vals = [v for t, v in pts if lo < t <= hi]
        out.append(round(statistics.mean(vals), 3) if vals else None)
    return out


_load()
