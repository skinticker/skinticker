"""CSFloat API-Client mit Rate-Limit-Bewusstsein, Backoff und Retry.

Liest bei jeder Antwort die X-Ratelimit-* Header aus und bremst automatisch,
bevor das Kontingent des jeweiligen Endpoints aufgebraucht ist. Bei 429
("too many requests") wird mit Backoff erneut versucht, statt sofort
aufzugeben.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://csfloat.com/api/v1"

# Prozessuebergreifende Ablage des Rate-Limit-Stands, damit das Dashboard
# (eigener Prozess) ihn anzeigen kann.
RATE_LIMIT_PATH = Path("data/rate_limits.json")

# Wenn weniger als so viele Requests im aktuellen Fenster uebrig sind, wird gebremst,
# bevor der naechste Call ueberhaupt raus geht.
SAFETY_BUFFER = 5
# Maximal so lange am Stueck auf ein Rate-Limit-Reset warten (Sekunden).
MAX_WAIT_SECONDS = 300
MAX_RETRIES = 5


@dataclass
class RateLimitState:
    """Merkt sich den zuletzt bekannten Rate-Limit-Stand fuer einen Endpoint.

    CSFloat hat pro Endpoint getrennte Kontingente (z.B. /listings: 200,
    /me: 50000) - deshalb wird der Zustand pro Pfad getrennt gehalten.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None  # Unix-Timestamp

    def update_from_headers(self, headers: requests.structures.CaseInsensitiveDict) -> None:
        if "X-Ratelimit-Limit" in headers:
            self.limit = int(headers["X-Ratelimit-Limit"])
        if "X-Ratelimit-Remaining" in headers:
            self.remaining = int(headers["X-Ratelimit-Remaining"])
        if "X-Ratelimit-Reset" in headers:
            self.reset_at = float(headers["X-Ratelimit-Reset"])

    def seconds_until_reset(self) -> float:
        if self.reset_at is None:
            return 0.0
        return max(0.0, self.reset_at - time.time())


class CSFloatClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("CSFLOAT_API_KEY fehlt oder ist leer.")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": api_key})
        self._rate_limits: dict[str, RateLimitState] = {}
        self._rate_limit_path = RATE_LIMIT_PATH

    def _persist_rate_limits(self, path: str, state: RateLimitState) -> None:
        """Schreibt den aktuellen Rate-Limit-Stand best-effort als JSON weg, damit
        das Dashboard ihn ohne eigene API-Calls anzeigen kann. Fehler werden
        bewusst verschluckt - Telemetrie darf niemals einen Request kippen."""
        if state.remaining is None or state.limit is None:
            return
        try:
            p = self._rate_limit_path
            data = {}
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = {}
            data[path] = {
                "limit": state.limit,
                "remaining": state.remaining,
                "reset_at": state.reset_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _wait_if_needed(self, path: str) -> None:
        state = self._rate_limits.get(path)
        if not state or state.remaining is None:
            return
        if state.remaining > SAFETY_BUFFER:
            return

        wait = min(state.seconds_until_reset(), MAX_WAIT_SECONDS)
        if wait > 0:
            logger.warning(
                "Rate-Limit fuer %s fast erreicht (%s/%s uebrig). Warte %.0fs bis zum Reset.",
                path, state.remaining, state.limit, wait,
            )
            time.sleep(wait)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{BASE_URL}{path}"

        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_if_needed(path)

            try:
                resp = self._session.request(method, url, timeout=15, **kwargs)
            except requests.RequestException as exc:
                wait = min(2 ** attempt, MAX_WAIT_SECONDS)
                logger.warning(
                    "Netzwerkfehler bei %s (Versuch %s/%s): %s. Warte %.0fs.",
                    path, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
                continue

            state = self._rate_limits.setdefault(path, RateLimitState())
            state.update_from_headers(resp.headers)
            self._persist_rate_limits(path, state)
            logger.info(
                "%s %s -> %s (Rate-Limit: %s/%s uebrig)",
                method, path, resp.status_code, state.remaining, state.limit,
            )

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (
                    state.seconds_until_reset() or (2 ** attempt)
                )
                wait = min(wait, MAX_WAIT_SECONDS) + random.uniform(0, 1)
                logger.warning(
                    "429 Too Many Requests bei %s (Versuch %s/%s). Warte %.1fs.",
                    path, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = min(2 ** attempt, MAX_WAIT_SECONDS)
                logger.warning(
                    "Serverfehler %s bei %s (Versuch %s/%s). Warte %.1fs.",
                    resp.status_code, path, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            # Andere Fehler (403, 404, ...): kein Retry, sofort melden.
            logger.error("CSFloat HTTP %s bei %s: %s", resp.status_code, path, resp.text[:300])
            resp.raise_for_status()

        raise RuntimeError(f"CSFloat-Request an {path} nach {MAX_RETRIES} Versuchen fehlgeschlagen.")

    def get_cheapest_listings(
        self,
        market_hash_name: str,
        limit: int = 5,
        max_price: int | None = None,
    ) -> list[dict]:
        """Gibt die guenstigsten sofort kaufbaren Listings fuer ein Item zurueck.

        Preise in den Ergebnissen sind in US-Cent (CSFloat-Konvention).
        """
        params = {
            "market_hash_name": market_hash_name,
            "type": "buy_now",
            "sort_by": "lowest_price",
            "limit": limit,
        }
        if max_price is not None:
            params["max_price"] = max_price

        body = self._request("GET", "/listings", params=params)
        return body.get("data", [])

    def get_my_inventory(self) -> list[dict]:
        """Eigenes CSFloat-Inventar inkl. reference.predicted_price (Cents)."""
        return self._request("GET", "/me/inventory")

    def get_price_list(self) -> list[dict]:
        """Oeffentliche Preisliste aller Items (min_price in Cents).

        Grosser Download - nur als Fallback fuer Items ohne predicted_price nutzen.
        """
        return self._request("GET", "/listings/price-list")

    def _get_my_trades(self, role: str, max_pages: int = 25, state: str = "verified") -> list[dict]:
        """Eigene Trades fuer role='buyer'/'seller' und state (z.B. 'verified' oder
        'pending' = noch im Escrow/Hold), paginiert.

        Jeder Trade hat contract.price (USD-Cent) und contract.item (Name, Float).
        /me-Endpoint mit niedrigem Limit (100/Fenster) - nur 1x/Tag abrufen.
        """
        trades = []
        for page in range(max_pages):
            body = self._request("GET", "/me/trades", params={
                "role": role, "state": state, "limit": 50, "page": page,
            })
            batch = body.get("trades", []) if isinstance(body, dict) else []
            if not batch:
                break
            trades.extend(batch)
            if len(batch) < 50:
                break
        return trades

    def get_my_buyer_trades(self, max_pages: int = 25) -> list[dict]:
        return self._get_my_trades("buyer", max_pages)

    def get_my_seller_trades(self, max_pages: int = 25) -> list[dict]:
        return self._get_my_trades("seller", max_pages)

    def get_pending_buyer_trades(self, max_pages: int = 10) -> list[dict]:
        """Gekaufte Items, die noch im CSFloat-Escrow/Trade-Hold sind (state=pending)."""
        return self._get_my_trades("buyer", max_pages, state="pending")

    def get_account(self) -> dict:
        """Eigener Account inkl. balance + pending_balance (US-Cent) via /me.

        Der /me-Endpoint ist inoffiziell (per Browser reverse-engineered) und kann
        sich jederzeit aendern - der Aufrufer sollte defensiv parsen. Betraege sind
        in Cents; fuer USD durch 100 teilen.
        """
        return self._request("GET", "/me")

    def rate_limit_state(self, path: str) -> RateLimitState | None:
        """Zuletzt gesehener Rate-Limit-Stand fuer einen Endpoint (oder None,
        solange fuer diesen Pfad noch kein Call abgesetzt wurde)."""
        return self._rate_limits.get(path)
