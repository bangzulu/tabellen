"""Hämtar GTFS-RT-flöden på begäran, med cache och månadsbudget för API-anrop.

Cachen är "stale-while-revalidate": är datat äldre än max_age men yngre än stale_ok
returneras det direkt och ett nytt hämtas i bakgrunden. Bara när datat saknas eller
är riktigt gammalt får anroparen vänta på Trafiklab.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import requests
from google.transit import gtfs_realtime_pb2

from . import config

log = logging.getLogger(__name__)
USAGE_PATH = config.DATA_DIR / "usage.json"


class BudgetExceeded(RuntimeError):
    pass


class Usage:
    """Räknar anrop per kalendermånad i data/usage.json."""

    def __init__(self, path=USAGE_PATH):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def month(self) -> str:
        return datetime.now().strftime("%Y-%m")

    def count(self) -> int:
        return self._load().get(self.month(), 0)

    def add(self, n: int = 1) -> int:
        with self.lock:
            data = self._load()
            key = self.month()
            data[key] = data.get(key, 0) + n
            self.path.write_text(json.dumps(data))
            return data[key]


class FeedCache:
    def __init__(self, usage: Usage | None = None):
        self.usage = usage or Usage()
        self._cache: dict[str, tuple[float, gtfs_realtime_pb2.FeedMessage]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, feed: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(feed, threading.Lock())

    def get(self, feed: str, max_age: int = config.RT_CACHE_SECONDS,
            stale_ok: int = config.RT_STALE_OK_SECONDS) -> gtfs_realtime_pb2.FeedMessage:
        if config.USE_SAVED_FEEDS:
            return load_feed_file(config.DATA_DIR / f"{feed}.pb")
        hit = self._cache.get(feed)
        age = time.time() - hit[0] if hit else None
        if hit and age < max_age:
            return hit[1]
        if hit and age < stale_ok:
            self._refresh_async(feed)
            return hit[1]
        return self._refresh_sync(feed, max_age)

    def fetched_at(self, feed: str) -> float | None:
        hit = self._cache.get(feed)
        return hit[0] if hit else None

    def _refresh_sync(self, feed: str, max_age: int) -> gtfs_realtime_pb2.FeedMessage:
        with self._lock(feed):
            hit = self._cache.get(feed)  # någon annan kan ha hämtat medan vi väntade
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
            msg = self._fetch(feed)
            self._cache[feed] = (time.time(), msg)
            return msg

    def _refresh_async(self, feed: str) -> None:
        lock = self._lock(feed)
        if not lock.acquire(blocking=False):
            return  # hämtning pågår redan

        def run():
            try:
                self._cache[feed] = (time.time(), self._fetch(feed))
            except Exception:
                log.warning("Bakgrundshämtning av %s misslyckades, behåller gammal data", feed, exc_info=True)
            finally:
                lock.release()

        threading.Thread(target=run, daemon=True, name=f"refresh-{feed}").start()

    def _fetch(self, feed: str) -> gtfs_realtime_pb2.FeedMessage:
        if not config.GTFS_RT_KEY:
            raise RuntimeError("GTFS_RT_KEY saknas i .env")
        if self.usage.count() >= config.RT_MONTHLY_BUDGET:
            raise BudgetExceeded(f"Månadsbudgeten {config.RT_MONTHLY_BUDGET} anrop är nådd.")
        resp = requests.get(
            config.RT_URLS[feed],
            params={"key": config.GTFS_RT_KEY},
            headers={"Accept-Encoding": "gzip"},
            timeout=20,
        )
        self.usage.add()
        resp.raise_for_status()
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(resp.content)
        log.info("%s: %d entiteter, feed-tid %s", feed, len(msg.entity), msg.header.timestamp)
        return msg


def load_feed_file(path) -> gtfs_realtime_pb2.FeedMessage:
    """För tester och offline-utveckling med sparade .pb-filer. Saknad fil ger tomt flöde."""
    msg = gtfs_realtime_pb2.FeedMessage()
    try:
        with open(path, "rb") as fh:
            msg.ParseFromString(fh.read())
    except FileNotFoundError:
        msg.header.gtfs_realtime_version = "2.0"
    return msg
