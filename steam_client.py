"""Steam Community Market Client (kostenlos, kein API-Key noetig).

Steam liefert keine Rate-Limit-Header, ist aber bekanntlich empfindlich bei
zu vielen Anfragen. Deshalb: fester Mindestabstand zwischen Calls + Backoff
bei 429/Fehlern, statt uns auf Header-Infos zu verlassen.
"""

from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

PRICEOVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
MIN_SECONDS_BETWEEN_CALLS = 2.5
MAX_RETRIES = 4

_last_call_at: float = 0.0


def _parse_price_eur(text: str | None) -> float | None:
    if not text:
        return None
    # Nur Ziffern, Komma und Punkt behalten (Waehrungssymbol/NBSP koennen je nach
    # Locale/Encoding variieren, z.B. "114,53\xa0€").
    digits = re.sub(r"[^\d,.]", "", text)
    digits = digits.replace(".", "").replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
    _last_call_at = time.monotonic()


def get_steam_price_eur(market_hash_name: str) -> float | None:
    """Gibt den aktuellen Steam-Marktpreis in EUR zurueck (lowest_price, sonst median_price)."""
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()

        try:
            resp = requests.get(
                PRICEOVERVIEW_URL,
                params={
                    "country": "DE",
                    "currency": 3,  # EUR
                    "appid": 730,
                    "market_hash_name": market_hash_name,
                },
                headers={"User-Agent": "Mozilla/5.0 (CSFloat-Sniper Python-Skript)"},
                timeout=15,
            )
        except requests.RequestException as exc:
            wait = 2 ** attempt
            logger.warning("Netzwerkfehler bei Steam-Abfrage (Versuch %s/%s): %s. Warte %ss.",
                            attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            data = resp.json()
            if not data.get("success"):
                logger.warning("Steam meldet 'success: false' fuer %s", market_hash_name)
                return None
            price = _parse_price_eur(data.get("lowest_price")) or _parse_price_eur(data.get("median_price"))
            if price is None:
                logger.debug("Steam lieferte 'success: true' ohne Preisfelder fuer %s: %s",
                             market_hash_name, data)
            return price

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = min(2 ** attempt * 2, 60)
            logger.warning("Steam HTTP %s bei %s (Versuch %s/%s). Warte %ss.",
                            resp.status_code, market_hash_name, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue

        logger.error("Steam HTTP %s bei %s: %s", resp.status_code, market_hash_name, resp.text[:200])
        return None

    logger.error("Steam-Abfrage fuer %s nach %s Versuchen fehlgeschlagen.", market_hash_name, MAX_RETRIES)
    return None
