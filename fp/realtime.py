"""Hämtar GTFS-RT-flöden på begäran, med cache och månadsbudget för API-anrop."""
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
        self.lock = threading.Lock()
        self._cache: dict[str, tuple[float, gtfs_realtime_pb2.FeedMessage]] = {}

    def get(self, feed: str, max_age: int = config.RT_CACHE_SECONDS) -> gtfs_realtime_pb2.FeedMessage:
        if config.USE_SAVED_FEEDS:
            return load_feed_file(config.DATA_DIR / f"{feed}.pb")
        with self.lock:
            hit = self._cache.get(feed)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
            msg = self._fetch(feed)
            self._cache[feed] = (time.time(), msg)
            return msg

    def fetched_at(self, feed: str) -> float | None:
        hit = self._cache.get(feed)
        return hit[0] if hit else None

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
    """För tester och offline-utveckling med sparade .pb-filer."""
    msg = gtfs_realtime_pb2.FeedMessage()
    with open(path, "rb") as fh:
        msg.ParseFromString(fh.read())
    return msg
