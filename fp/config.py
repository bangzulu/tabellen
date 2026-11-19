"""Inställningar och flik-/linjemappning."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

OPERATOR = "sl"
GTFS_STATIC_KEY = os.getenv("GTFS_STATIC_KEY", "")
GTFS_RT_KEY = os.getenv("GTFS_RT_KEY", "")

STATIC_URL = f"https://opendata.samtrafiken.se/gtfs/{OPERATOR}/{OPERATOR}.zip"
RT_URLS = {
    "TripUpdates": f"https://opendata.samtrafiken.se/gtfs-rt/{OPERATOR}/TripUpdates.pb",
    "VehiclePositions": f"https://opendata.samtrafiken.se/gtfs-rt/{OPERATOR}/VehiclePositions.pb",
    "ServiceAlerts": f"https://opendata.samtrafiken.se/gtfs-rt/{OPERATOR}/ServiceAlerts.pb",
}

RT_CACHE_SECONDS = int(os.getenv("RT_CACHE_SECONDS", "30"))
USE_VEHICLE_POSITIONS = os.getenv("USE_VEHICLE_POSITIONS", "false").lower() == "true"
RT_MONTHLY_BUDGET = int(os.getenv("RT_MONTHLY_BUDGET", "28000"))
MAX_DATA_AGE_SECONDS = int(os.getenv("MAX_DATA_AGE_SECONDS", "120"))
# Utveckling utan API-anrop: läs data/TripUpdates.pb och data/VehiclePositions.pb
USE_SAVED_FEEDS = os.getenv("USE_SAVED_FEEDS", "false").lower() == "true"

TIMEZONE = "Europe/Stockholm"

# Flikar i samma ordning och med samma koder som kitt.sl.se (#PT, #RB ...).
# Mappas på route_short_name. VERIFIERA mot riktig data:
#   python scripts/coverage_check.py --routes
TABS = [
    {"code": "PT", "name": "Pendeltåg", "lines": ["40", "41", "42X", "43", "43X", "44", "48"]},
    {"code": "RB", "name": "Roslagsbanan", "lines": ["27", "27S", "28", "28S", "29"]},
    {"code": "TVB", "name": "Tvärbanan", "lines": ["30", "31"]},
    {"code": "SB", "name": "Saltsjöbanan", "lines": ["25", "26"]},
    {"code": "LB", "name": "Lidingöbanan", "lines": ["21"]},
    {"code": "SpvC", "name": "Linje 7", "lines": ["7"]},
    {"code": "TB1", "name": "Gröna linjen", "lines": ["17", "18", "19"]},
    {"code": "TB2", "name": "Röda linjen", "lines": ["13", "14"]},
    {"code": "TB3", "name": "Blå linjen", "lines": ["10", "11"]},
]
TAB_BY_CODE = {t["code"]: t for t in TABS}
LINE_TO_TAB = {line: t["code"] for t in TABS for line in t["lines"]}


def is_excluded_route_type(route_type: int) -> bool:
    """Bussar, långfärdsbussar och taxi har linjenummer som kan krocka med spårlinjer
    (t.ex. buss 7). Trafiklab använder utökade route_types, därför intervallen."""
    return route_type == 3 or 200 <= route_type <= 299 or 700 <= route_type <= 799 or 1500 <= route_type <= 1599
