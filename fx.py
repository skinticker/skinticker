"""USD->EUR Wechselkurs, mit Datei-Cache, um die kostenlose API nicht bei jedem Lauf neu zu belasten."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

CACHE_PATH = Path("data/fx_cache.json")
CACHE_TTL_SECONDS = 60 * 60  # 1 Stunde
FX_URL = "https://api.exchangerate-api.com/v4/latest/USD"
FALLBACK_RATE = 0.92  # wird nur genutzt, wenn weder API noch Cache verfuegbar sind


def get_usd_to_eur() -> float:
    cached = _read_cache()
    if cached is not None:
        return cached

    try:
        resp = requests.get(FX_URL, timeout=10)
        resp.raise_for_status()
        rate = resp.json()["rates"]["EUR"]
        _write_cache(rate)
        return rate
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.warning("USD->EUR Kurs konnte nicht abgerufen werden (%s).", exc)
        stale = _read_cache(ignore_ttl=True)
        if stale is not None:
            logger.warning("Nutze veralteten gecachten Kurs: %s", stale)
            return stale
        logger.warning("Nutze festen Fallback-Kurs: %s", FALLBACK_RATE)
        return FALLBACK_RATE


def _read_cache(ignore_ttl: bool = False) -> float | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    age = time.time() - data.get("fetched_at", 0)
    if not ignore_ttl and age > CACHE_TTL_SECONDS:
        return None
    return data.get("rate")


def _write_cache(rate: float) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"rate": rate, "fetched_at": time.time()}), encoding="utf-8")
